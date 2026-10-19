"""Exercise the pool's hardware init scripts in Chromium, using local responses."""
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from executor import browser_pool, tiktok_client


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


_READ_HARDWARE = """() => {
    const readGl = (kind) => {
        const gl = document.createElement('canvas').getContext(kind);
        if (!gl) throw new Error(kind + ' unavailable');
        const debug = gl.getExtension('WEBGL_debug_renderer_info');
        if (!debug) throw new Error(kind + ' debug renderer extension unavailable');
        const vendor = gl.getParameter(debug.UNMASKED_VENDOR_WEBGL);
        const renderer = gl.getParameter(debug.UNMASKED_RENDERER_WEBGL);
        const maxTextureSize = gl.getParameter(gl.MAX_TEXTURE_SIZE);
        return {vendor, renderer, maxTextureSize, error: gl.getError()};
    };
    return {
        width: screen.width, height: screen.height,
        viewportWidth: innerWidth, viewportHeight: innerHeight,
        cpu: navigator.hardwareConcurrency, memory: navigator.deviceMemory,
        gl: readGl('webgl'), gl2: readGl('webgl2'),
        plugins: navigator.plugins.length, chrome: !!window.chrome,
    };
}"""


@pytest.fixture(params=[False, True], ids=["basic", "human_like"])
async def hardware_pool(monkeypatch, request):
    monkeypatch.setattr(browser_pool.settings, "USE_PROXY", False)
    monkeypatch.setattr(browser_pool.settings, "PLAYWRIGHT_HEADLESS", True)
    monkeypatch.setattr(browser_pool.settings, "HUMAN_LIKE_BEHAVIOR", request.param)
    monkeypatch.setattr(tiktok_client, "_get_cookies_for_account", lambda _: '{"sessionid":"hardware-fixture-session"}')
    pool = browser_pool.BrowserPool(max_browsers=1)
    try:
        yield pool
    finally:
        await pool.close_all()


async def open_hardware_page(pool, account_id):
    account = SimpleNamespace(
        id=account_id, label="hardware-test", proxy=None, user_agent=None,
        country_code="US", proxy_country_code=None,
    )
    context, page = await pool.get_page_for_account(account)
    await context.route("**/*", lambda route: route.fulfill(
        content_type="text/html", body="<html><body>Local hardware fixture</body></html>",
    ))
    await page.goto("https://hardware.fixture.test/")
    profile = browser_pool._desktop_hardware_profile_for_account(account)
    return context, page, profile


def assert_hardware(actual, expected, *, top_level=True):
    assert actual["width"] == expected["width"]
    assert actual["height"] == expected["height"]
    if top_level:
        assert actual["viewportWidth"] == expected["width"]
        assert actual["viewportHeight"] == expected["height"] - 90
    assert actual["cpu"] == expected["hardwareConcurrency"]
    assert actual["memory"] == expected["deviceMemory"]
    for key in ("gl", "gl2"):
        assert actual[key]["vendor"] == expected["webglVendor"]
        assert actual[key]["renderer"] == expected["webglRenderer"]
        assert actual[key]["maxTextureSize"] > 0 and actual[key]["error"] == 0
    assert actual["plugins"] > 0 and actual["chrome"]


async def test_account_hardware_persists_across_pages_frames_and_context_recreation(hardware_pool):
    context, page, profile = await open_hardware_page(hardware_pool, 1219)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    actual = await page.evaluate(_READ_HARDWARE, isolated_context=False)
    assert_hardware(actual, profile)

    await page.evaluate("() => { const frame = document.createElement('iframe'); frame.src = '/child'; document.body.append(frame); }", isolated_context=False)
    await page.wait_for_function("document.querySelector('iframe').contentDocument?.readyState === 'complete'")
    frame = next(frame for frame in page.frames if frame != page.main_frame)
    assert_hardware(await frame.evaluate(_READ_HARDWARE, isolated_context=False), profile, top_level=False)

    second_page = await context.new_page()
    await second_page.goto("https://hardware.fixture.test/second")
    assert_hardware(await second_page.evaluate(_READ_HARDWARE, isolated_context=False), profile)
    assert not errors

    await hardware_pool.close_context_for_account(1219)
    _, recreated, same_profile = await open_hardware_page(hardware_pool, 1219)
    assert same_profile == profile
    assert_hardware(await recreated.evaluate(_READ_HARDWARE, isolated_context=False), profile)

    _, other, other_profile = await open_hardware_page(hardware_pool, 1220)
    assert other_profile != profile
    assert_hardware(await other.evaluate(_READ_HARDWARE, isolated_context=False), other_profile)


@pytest.mark.parametrize("ua,family,platform", [
    ("Mozilla/5.0 iPhone Mobile", "iphone", "iPhone"),
    ("Mozilla/5.0 iPad Mobile", "ipad", "iPad"),
    ("Mozilla/5.0 Android Mobile", "android", "Linux armv8l"),
    ("Mozilla/5.0 (Linux; Android 8.0.0; SM-G965U) AppleWebKit/537.36 Chrome/145.0.0.0 Mobile Safari/537.36", "android", "Linux armv8l"),
    ("Mozilla/5.0 Android Tablet", "android_tablet", "Linux armv8l"),
    (None, "windows", "Win32"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36", "mac", "MacIntel"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36", "linux", "Linux x86_64"),
])
async def test_device_identity_matches_context_and_request_headers(hardware_pool, ua, family, platform):
    account = SimpleNamespace(id=1221, label="device-test", proxy=None, user_agent=ua, country_code="US", proxy_country_code=None)
    context, page = await hardware_pool.get_page_for_account(account)
    captured = []
    async def serve(route):
        captured.append(await route.request.all_headers())
        await route.fulfill(content_type="text/html", body='<html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body>Local</body></html>')
    await context.route("**/*", serve)
    await page.goto("https://hardware.fixture.test/device")
    profile = context._account_device_profile
    assert profile["identity"]["family"] == "windows"
    family, platform = "windows", "Win32"
    snapshot = """({cpu:navigator.hardwareConcurrency, platform:navigator.platform,
        memory:navigator.deviceMemory ?? null, ua:navigator.userAgent,
        ch:navigator.userAgentData?.toJSON() ?? null})"""
    observed = await page.evaluate("() => " + snapshot, isolated_context=False)
    assert observed["cpu"] == profile["identity"]["hardware"]["hardwareConcurrency"]
    assert observed["platform"] == platform
    assert captured[0]["user-agent"] == observed["ua"]
    if family in {"iphone", "ipad"}:
        assert observed["memory"] is None and observed["ch"] is None
        assert "sec-ch-ua-platform" not in captured[0]
    else:
        assert observed["ch"]["platform"] == profile["override"]["userAgentMetadata"]["platform"]
        assert captured[0]["sec-ch-ua-platform"] == '"' + observed["ch"]["platform"] + '"'
        assert observed["ch"]["mobile"] == (family == "android")
        assert captured[0]["sec-ch-ua-mobile"] == ("?1" if family == "android" else "?0")
    sizes = await page.evaluate("() => ({w:innerWidth,h:innerHeight,sw:screen.width,sh:screen.height,touch:navigator.maxTouchPoints,dpr:devicePixelRatio})", isolated_context=False)
    assert sizes["w"] <= sizes["sw"] and sizes["h"] <= sizes["sh"]
    assert bool(sizes["touch"]) == profile["identity"]["mobile"]
    gl_profile = await page.evaluate(_READ_HARDWARE, isolated_context=False)
    for kind in ("gl", "gl2"):
        assert gl_profile[kind]["vendor"] == profile["identity"]["hardware"]["webglVendor"]
        assert gl_profile[kind]["renderer"] == profile["identity"]["hardware"]["webglRenderer"]
        assert gl_profile[kind]["error"] == 0
    if profile["identity"]["preset"] == "Galaxy S9+":
        assert sizes["sw"] == 320 and sizes["sh"] == 658 and sizes["dpr"] == 4.5
    # Short-lived workers must keep working with the account bootstrap.
    worker = await page.evaluate("""() => new Promise((resolve,reject) => {
        const url=URL.createObjectURL(new Blob(['postMessage(42)'], {type:'text/javascript'}));
        const worker=new Worker(url);
        worker.onerror=reject;
        worker.onmessage=e=>{resolve(e.data);worker.terminate();URL.revokeObjectURL(url)};
    })""", isolated_context=False)
    assert worker == 42
    assert not page.is_closed()


async def test_webgl_argument_conversion_and_canvas_stability(hardware_pool):
    _, page, _ = await open_hardware_page(hardware_pool, 1222)
    result = await page.evaluate("""() => {
        const canvas=document.createElement('canvas'), gl=canvas.getContext('webgl');
        const ext=gl.getExtension('WEBGL_debug_renderer_info');
        let calls=0;
        const numeric=gl.getParameter(ext.UNMASKED_RENDERER_WEBGL);
        const string=gl.getParameter(String(ext.UNMASKED_RENDERER_WEBGL));
        const object=gl.getParameter({valueOf(){calls++;return ext.UNMASKED_RENDERER_WEBGL}});
        let invalidReceiver=false;
        try {gl.getParameter.call({},37446)} catch(e) {invalidReceiver=e instanceof TypeError}
        const c=document.createElement('canvas').getContext('2d');
        c.fillStyle='#7f7f7f';c.fillRect(0,0,10,10);
        return {numeric,string,object,calls,invalidReceiver,
            first:[...c.getImageData(0,0,2,2).data],second:[...c.getImageData(0,0,2,2).data]};
    }""", isolated_context=False)
    assert result["numeric"] == result["string"] == result["object"]
    assert result["calls"] == 1 and result["invalidReceiver"]
    assert result["first"] == result["second"]


async def test_fingerprints_cover_exports_workers_and_offline_audio(hardware_pool):
    from copy import deepcopy
    from device_probe import READ_DEVICE, _native_mask_passed
    snapshots = []
    for account_id in (1219, 1224, 1219):
        context, page, _ = await open_hardware_page(hardware_pool, account_id)
        snapshots.append(await page.evaluate(READ_DEVICE, isolated_context=False))
        await hardware_pool.close_context_for_account(account_id)
    first, other, recreated = snapshots
    # A forged positive flag must not hide a missing hook, leaked body or stack.
    for damage in ("missing_hook", "direct_source", "borrowed_source", "invalid_error", "wrapper_stack"):
        broken = deepcopy(first["nativeMask"])
        if damage == "missing_hook":
            broken["hooks"] = [h for h in broken["hooks"] if h["name"] != "HTMLCanvasElement.toDataURL"]
        elif damage == "direct_source":
            broken["hooks"][0]["direct"] = "function toString() { return 'fake'; }"
        elif damage == "borrowed_source":
            broken["hooks"][0]["borrowed"] = "function toString() { return 'fake'; }"
        elif damage == "invalid_error":
            broken["invalid"]["isTypeError"] = False
        else:
            broken["invalid"]["stack"] += "\n    at Object.apply (device_identity.js:1:1)"
        assert broken["passed"] is True
        assert not _native_mask_passed(broken)
    for sample in snapshots:
        assert sample["nativeMask"]["passed"], sample["nativeMask"]
        assert sample["webgl"] == sample["offscreenWebgl"]
        assert sample["webgl2"] == sample["offscreenWebgl2"]
        for name in ("worker", "moduleWorker"):
            assert "error" not in sample[name]
            assert sample[name]["identity"] == sample["identity"]
            assert sample[name]["webgl"] == sample["webgl"]
            assert sample[name]["webgl2"] == sample["webgl2"]
            assert sample[name]["nativeMask"]["passed"], sample[name]["nativeMask"]
            for key in ("offscreen", "offscreenPixels", "pixels"):
                assert sample[name]["fingerprints"][key] == sample["fingerprints"][key]
        fp = sample["fingerprints"]
        for key in ("pixels", "dataURL", "blob", "offscreen", "audio"):
            assert fp[key] == fp[key + "Repeat"]
        assert fp["audio"] == fp["audioCopyFirst"] == fp["audioCopy"]
        assert fp["audioOffsetMatches"]
        assert sample["highEntropy"]["uaFullVersion"] == context._account_device_profile["override"]["userAgentMetadata"]["fullVersion"]
    for key in ("pixels", "dataURL", "blob", "offscreen", "offscreenPixels", "audio"):
        assert first["fingerprints"][key] == recreated["fingerprints"][key]
        assert first["fingerprints"][key] != other["fingerprints"][key]
    assert first["identity"]["memory"] == recreated["identity"]["memory"] != other["identity"]["memory"]
    assert first["webgl"] == recreated["webgl"] != other["webgl"]


async def test_canvas_exports_preserve_source_and_native_errors(hardware_pool):
    _, page, _ = await open_hardware_page(hardware_pool, 1222)
    result = await page.evaluate(r"""async () => {
        const canvas=document.createElement('canvas');canvas.width=64;canvas.height=32;
        const ctx=canvas.getContext('2d');ctx.fillStyle='#7f7f7f';ctx.fillRect(0,0,64,32);
        const before=[...ctx.getImageData(0,0,64,32).data];
        const dataUrl=canvas.toDataURL();
        const blobs=await Promise.all([0,1].map(()=>new Promise(r=>canvas.toBlob(r))));
        const images=await Promise.all(blobs.map(b=>createImageBitmap(b)));
        const equal=(a,b)=>a.length===b.length&&a.every((v,i)=>v===b[i]);
        const read=image=>{const c=new OffscreenCanvas(64,32),g=c.getContext('2d');g.drawImage(image,0,0);return [...g.getImageData(0,0,64,32).data]};
        const blobPixels=images.map(read);
        const urlImage=new Image();urlImage.src=dataUrl;await urlImage.decode();
        const urlPixels=read(urlImage);
        const cropped=[...ctx.getImageData('3','2','5','7').data];
        const numeric=[...ctx.getImageData(3,2,5,7).data];
        let conversions=0;const arg={valueOf(){conversions++;return 3}};
        ctx.getImageData(arg,2,5,7);
        const errors=[];
        try {ctx.getImageData(0,0,0,1)}catch(e){errors.push(e.name)}
        try {ctx.getImageData.call({},0,0,1,1)}catch(e){errors.push(e.name)}
        try {canvas.toDataURL.call({})}catch(e){errors.push(e.name)}
        canvas.width=0;const empty=canvas.toDataURL();
        return {same:equal(before,urlPixels)&&blobPixels.every(p=>equal(before,p)),
            stable:equal(blobPixels[0],blobPixels[1]),crop:equal(cropped,numeric),conversions,errors,empty};
    }""", isolated_context=False)
    assert result == {"same": True, "stable": True, "crop": True, "conversions": 1,
                      "errors": ["IndexSizeError", "TypeError", "TypeError"], "empty": "data:,"}


async def test_audio_reads_keep_native_argument_conversion_and_views(hardware_pool):
    _, page, _ = await open_hardware_page(hardware_pool, 1224)
    result = await page.evaluate("""() => {
        const buffer=new AudioBuffer({length:1024,sampleRate:44100,numberOfChannels:2});
        let channelCalls=0,offsetCalls=0;
        const channel={valueOf(){channelCalls++;return 1}};
        const offset={valueOf(){offsetCalls++;return 31}};
        const copied=new Float32Array(128);
        buffer.copyFromChannel(copied,channel,offset);
        const view=buffer.getChannelData(channel);
        const stable=view===buffer.getChannelData(1);
        const same=copied.every((v,i)=>Object.is(v,view[i+31]));
        // Application writes still take effect through the live channel view.
        view[11]=0.25;const writes=buffer.getChannelData(1)[11]===0.25;
        let illegal=false;try {buffer.getChannelData.call({},0)}catch(e){illegal=e instanceof TypeError}
        return {channelCalls,offsetCalls,stable,same,writes,illegal};
    }""", isolated_context=False)
    assert result == {"channelCalls": 2, "offsetCalls": 1, "stable": True,
                      "same": True, "writes": True, "illegal": True}


async def test_native_hook_shapes_and_type_errors_match_pristine_realm(hardware_pool):
    context, page, _ = await open_hardware_page(hardware_pool, 1224)
    script = r"""() => {
        const methods=[HTMLCanvasElement.prototype.toDataURL,HTMLCanvasElement.prototype.toBlob,
            CanvasRenderingContext2D.prototype.getImageData,OffscreenCanvas.prototype.convertToBlob,
            OffscreenCanvasRenderingContext2D.prototype.getImageData,
            AudioBuffer.prototype.getChannelData,AudioBuffer.prototype.copyFromChannel,
            WebGLRenderingContext.prototype.getParameter,WebGL2RenderingContext.prototype.getParameter,
            Function.prototype.toString,Worker];
        const shapes=methods.map(fn=>{
            let constructible=true;try{Reflect.construct(function(){},[],fn)}catch(e){constructible=false}
            return {name:fn.name,length:fn.length,source:fn.toString(),
                borrowed:Function.prototype.toString.call(fn),keys:Object.getOwnPropertyNames(fn),constructible};
        });
        function nativeCrashCaller(value) {
            try {Function.prototype.toString.call(value);return {threw:false}}
            catch(error){
                const stack=String(error.stack);
                return {threw:true,isTypeError:error instanceof TypeError,message:error.message,
                    nativeFrames:stack.split('\n').slice(0,2),caller:stack.includes('nativeCrashCaller'),
                    clean:!/device_identity|installAccountIdentity|toStringHandler|nativeHook|Object\.apply/.test(stack)};
            }
        }
        const errors=[123,null,undefined,{},[],Symbol('x')].map(nativeCrashCaller);
        function regular(a) { return a + 1; }
        return {shapes,errors,regular:regular.toString(),bound:regular.bind(null).toString()};
    }"""
    pristine = await context.browser.new_context()
    try:
        native_page = await pristine.new_page()
        expected = await native_page.evaluate(script, isolated_context=False)
    finally:
        await pristine.close()
    observed = await page.evaluate(script, isolated_context=False)
    assert observed == expected


async def test_worker_urls_imports_and_immediate_messages(hardware_pool):
    context, page, _ = await open_hardware_page(hardware_pool, 1223)
    scripts = {
        "/classic.js": "importScripts('./dep.js');onmessage=e=>postMessage({data:e.data,dep, cpu:navigator.hardwareConcurrency});",
        "/module.js": "import {dep} from './dep.mjs';onmessage=e=>postMessage({data:e.data,dep,cpu:navigator.hardwareConcurrency});",
        "/dep.js": "globalThis.dep=42;", "/dep.mjs": "export const dep=42;",
    }
    async def serve_script(route):
        await route.fulfill(content_type="text/javascript", body=scripts[urlparse(route.request.url).path])
    await context.route("**/*.js", serve_script)
    await context.route("**/*.mjs", serve_script)
    result = await page.evaluate("""async () => {
        const run=type=>new Promise((resolve,reject)=>{
            const w=new Worker(type==='module'?'/module.js':'/classic.js',{type});
            const timer=setTimeout(()=>{w.terminate();reject(new Error(type+' worker timeout'))},8000);
            w.onmessage=e=>{clearTimeout(timer);w.terminate();resolve(e.data)};
            w.onerror=e=>{clearTimeout(timer);w.terminate();reject(new Error(e.message))};
            w.postMessage('hello');
        });
        return {classic:await run('classic'),module:await run('module'),cpu:navigator.hardwareConcurrency};
    }""", isolated_context=False)
    assert result["classic"] == result["module"] == {"data": "hello", "dep": 42, "cpu": result["cpu"]}
