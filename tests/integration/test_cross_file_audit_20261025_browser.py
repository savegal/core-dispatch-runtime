import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from executor.device_profiles import build_device_profile, identity_script
from executor.http.playwright_http_client import PlaywrightHttpClient
from executor.http.request_profile import RequestProfile
from tests.integration.test_http_browser_transport import browser_page as browser_page

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PAGE = "https://www.tiktok.com/@fixture/video/7635267329407913230"
TARGET = "https://www.tiktok.com/@other/video/7641761192486194454"


@pytest.mark.parametrize("referrer", [TARGET + "#fragment", "https://foreign.fixture.test/private"])
async def test_fetch_sets_only_same_origin_referrer(browser_page, referrer):
    requests = []
    async def serve(route):
        if urlparse(route.request.url).path.rstrip("/") == "/api/comment/publish":
            requests.append({"url": route.request.url, "headers": await route.request.all_headers()})
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comment":{"cid":"7641761192486194454"}}')
        else:
            await route.fulfill(content_type="text/html", body="<html>Local referrer fixture</html>")
    await browser_page.route("**/*", serve)
    await browser_page.goto(PAGE)
    client = await PlaywrightHttpClient.from_page(browser_page)
    result = await client._execute_request(RequestProfile.publish_main_profile(),
                                            {"text": "fixture", "referer": referrer})
    assert result[0]
    assert len(requests) == 1
    assert requests[0]["headers"]["referer"] == (TARGET if referrer.startswith(TARGET) else PAGE)
    assert parse_qs(urlparse(requests[0]["url"]).query)["referer"] == [referrer]


@pytest.mark.parametrize("mode", ["integer-format", "destination-offset", "pixel-pack-buffer", "native-coercion"])
async def test_webgl2_other_layouts_keep_native_results_and_argument_coercion(browser_page, mode):
    await browser_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<html>Local WebGL fixture</html>"))
    await browser_page.goto(PAGE)
    await browser_page.evaluate("() => {window.nativeReadPixelsForAudit = WebGL2RenderingContext.prototype.readPixels}", isolated_context=False)
    profile = build_device_profile(SimpleNamespace(id=9271, device_seed="e" * 64), {}, "145.0.7632.6")
    await browser_page.evaluate(identity_script(profile["identity"]), isolated_context=False)
    data = await browser_page.evaluate("""mode => {
        const canvas = document.createElement('canvas'); canvas.width=32; canvas.height=2;
        const gl = canvas.getContext('webgl2');
        if (!gl) throw new Error('WebGL2 unavailable');
        const width=32, height=2, bytes=width*height*4;
        const texture=gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, texture);
        const source=new Uint8Array(bytes);
        for (let i=0;i<bytes;i++) source[i]=30+(i*17)%190;
        const integer=mode==='integer-format';
        gl.texImage2D(gl.TEXTURE_2D,0,integer?gl.RGBA8UI:gl.RGBA8,width,height,0,
                      integer?gl.RGBA_INTEGER:gl.RGBA,gl.UNSIGNED_BYTE,source);
        const fb=gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER,fb);
        gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.COLOR_ATTACHMENT0,gl.TEXTURE_2D,texture,0);
        if (gl.checkFramebufferStatus(gl.FRAMEBUFFER)!==gl.FRAMEBUFFER_COMPLETE) throw new Error('Incomplete fixture framebuffer');
        const run=reader=>{
            let conversions=0;
            const x=mode==='native-coercion'?{valueOf(){conversions++;return 0}}:0;
            const output=new Uint8Array(bytes+32);
            for(let i=0;i<output.length;i++) output[i]=20+(i*13)%200;
            if(mode==='pixel-pack-buffer'){
                const buffer=gl.createBuffer(); gl.bindBuffer(gl.PIXEL_PACK_BUFFER,buffer);
                gl.bufferData(gl.PIXEL_PACK_BUFFER,output,gl.STREAM_READ);
                reader.call(gl,x,0,width,height,gl.RGBA,gl.UNSIGNED_BYTE,0);
                gl.getBufferSubData(gl.PIXEL_PACK_BUFFER,0,output);
                gl.bindBuffer(gl.PIXEL_PACK_BUFFER,null); gl.deleteBuffer(buffer);
            }else{
                const args=[x,0,width,height,integer?gl.RGBA_INTEGER:gl.RGBA,gl.UNSIGNED_BYTE,output];
                if(mode==='destination-offset') args.push(16);
                reader.apply(gl,args);
            }
            return {bytes:Array.from(output),error:gl.getError(),conversions};
        };
        return {native:run(window.nativeReadPixelsForAudit),wrapped:run(gl.readPixels)};
    }""", mode, isolated_context=False)
    assert data["native"]["error"] == data["wrapped"]["error"] == 0
    assert data["native"] == data["wrapped"]
    if mode == "native-coercion":
        assert data["wrapped"]["conversions"] == 1


async def test_worker_language_stays_native_with_partial_unused_profile(browser_page):
    await browser_page.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<html>Local worker fixture</html>"))
    await browser_page.goto(PAGE)
    profile = build_device_profile(SimpleNamespace(id=9272, device_seed="f" * 64), {}, "145.0.7632.6")
    profile["identity"]["language"] = "unused-partial-value"
    assert "languages" not in profile["identity"]
    bootstrap = "const before={language:navigator.language,languages:Array.from(navigator.languages)};"
    bootstrap += identity_script(profile["identity"])
    bootstrap += "postMessage({before,after:{language:navigator.language,languages:navigator.languages}});"
    actual = await browser_page.evaluate("""source => new Promise((resolve,reject)=>{
        const url=URL.createObjectURL(new Blob([source],{type:'text/javascript'}));
        const worker=new Worker(url);
        const timer=setTimeout(()=>{worker.terminate();URL.revokeObjectURL(url);reject(new Error('worker timeout'))},5000);
        worker.onerror=error=>{clearTimeout(timer);worker.terminate();URL.revokeObjectURL(url);reject(new Error(error.message))};
        worker.onmessage=event=>{clearTimeout(timer);worker.terminate();URL.revokeObjectURL(url);resolve(event.data)};
    })""", bootstrap, isolated_context=False)
    assert actual["before"] == actual["after"]
    assert isinstance(actual["after"]["languages"], list) and actual["after"]["languages"]


async def test_browser_runtime_uses_the_context_fp_without_native_fp_cookie(browser_page):
    from executor.http.playwright_http_client import seed_web_identity_from_account
    requests = []
    async def serve(route):
        if urlparse(route.request.url).path == "/api/comment/publish/":
            requests.append(parse_qs(urlparse(route.request.url).query))
            await route.fulfill(content_type="application/json", body='{"status_code":0,"comment":{"cid":"7641761192486194454"}}')
        else:
            await route.fulfill(content_type="text/html", body="<html>No SSR or FP cookie</html>")
    await browser_page.route("**/*", serve)
    account = SimpleNamespace(id=9273, device_seed="a"*64, cookies=json.dumps({"sessionid":"fixture-session"}))
    browser_page.context._pool_account_id = account.id
    browser_page.context._pool_account_obj = account
    seeded = seed_web_identity_from_account(account, context=browser_page.context)
    await browser_page.context.add_cookies([{"name":"sessionid","value":"fixture-session","domain":".tiktok.com","path":"/"}])
    await browser_page.goto(PAGE)
    client = await PlaywrightHttpClient.from_page(browser_page)
    assert client.runtime_profile["account_id"] == account.id
    result = await client._execute_request(RequestProfile.publish_main_profile(), {"text":"fixture"})
    assert result[0]
    assert requests[0]["verifyFp"] == [seeded["verify_fp"]]
