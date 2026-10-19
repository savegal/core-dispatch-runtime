"""Offline Chromium identity audit. No real accounts or external requests.

Run: python device_probe.py --output output/device-probe/after.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from executor import browser_pool, tiktok_client
from executor.http.request_profile import RequestProfile, resolve_request_geo
from services import cookie_validator


READ_DEVICE = r"""async () => {
    function nativeMaskAudit() {
        const entries = [[Function.prototype, 'toString', false]];
        for (const [Type, names] of [
            [globalThis.HTMLCanvasElement, ['toDataURL', 'toBlob']],
            [globalThis.CanvasRenderingContext2D, ['getImageData']],
            [globalThis.OffscreenCanvasRenderingContext2D, ['getImageData']],
            [globalThis.OffscreenCanvas, ['convertToBlob']],
            [globalThis.AudioBuffer, ['getChannelData', 'copyFromChannel']],
            [globalThis.WebGLRenderingContext, ['getParameter']],
            [globalThis.WebGL2RenderingContext, ['getParameter']],
        ]) if (Type) for (const name of names) entries.push([Type.prototype, name, false]);
        for (const key of ['platform','vendor','hardwareConcurrency','deviceMemory','webdriver','language','languages']) {
            const proto = Object.getPrototypeOf(navigator);
            if (Object.getOwnPropertyDescriptor(proto, key)?.get) entries.push([proto,key,true]);
        }
        for (const name of ['Worker','importScripts']) if (typeof globalThis[name] === 'function') entries.push([globalThis,name,false]);
        const hooks = entries.map(([owner,key,getter]) => {
            const fn = getter ? Object.getOwnPropertyDescriptor(owner,key).get : owner[key];
            const expected = 'function ' + (getter ? 'get ' : '') + key + '() { [native code] }';
            const direct = fn.toString(), borrowed = Function.prototype.toString.call(fn);
            return {name:(owner.constructor?.name || 'global')+'.'+key+(getter?':get':''),
                expected,direct,borrowed,passed:direct===expected && borrowed===expected};
        });
        let invalid;
        try {Function.prototype.toString.call(123);invalid={passed:false,threw:false};}
        catch(error) {
            const stack=String(error.stack);
            invalid={threw:true,isTypeError:error instanceof TypeError,name:error.name,message:error.message,stack,
                passed:error instanceof TypeError && error.message==="Function.prototype.toString requires that 'this' be a Function"
                    && stack.split('\n')[1]==='    at Number.toString (<anonymous>)'
                    && !/device_identity|installAccountIdentity|toStringHandler|nativeHook|Object\.apply|Reflect\.apply/.test(stack)};
        }
        return {hooks,invalid,passed:hooks.every(h=>h.passed)&&invalid.passed};
    }
    function readWebGL(offscreen, kind='webgl') {
        const canvas=offscreen ? new OffscreenCanvas(4,4) : document.createElement('canvas');
        const gl=canvas.getContext(kind);
        if (!gl) return {available:false};
        const ext=gl.getExtension('WEBGL_debug_renderer_info');
        if (!ext) return {available:true,debug:false};
        const result={available:true,debug:true,vendor:gl.getParameter(ext.UNMASKED_VENDOR_WEBGL),
            renderer:gl.getParameter(ext.UNMASKED_RENDERER_WEBGL),error:gl.getError()};
        gl.getExtension('WEBGL_lose_context')?.loseContext();
        return result;
    }
    const identity = () => ({ua:navigator.userAgent, platform:navigator.platform,
        cpu:navigator.hardwareConcurrency, memory:navigator.deviceMemory ?? null,
        ch:navigator.userAgentData?.toJSON() ?? null,
        timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,
        language:navigator.language});
    async function fingerprints() {
        const hash = async data => Array.from(new Uint8Array(await crypto.subtle.digest(
            'SHA-256', typeof data === 'string' ? new TextEncoder().encode(data) : data
        )), b=>b.toString(16).padStart(2,'0')).join('');
        const canvas = typeof document !== 'undefined' ? document.createElement('canvas') : new OffscreenCanvas(64,32);
        canvas.width=64; canvas.height=32;
        const ctx=canvas.getContext('2d'); ctx.fillStyle='#7f7f7f'; ctx.fillRect(0,0,64,32);
        ctx.fillStyle='#abc'; ctx.font='12px Arial'; ctx.fillText('device probe',2,20);
        const pixels=await hash(ctx.getImageData(0,0,64,32).data);
        const result={pixels, pixelsRepeat:await hash(ctx.getImageData(0,0,64,32).data)};
        if (canvas.toDataURL) {
            result.dataURL=await hash(canvas.toDataURL());
            result.dataURLRepeat=await hash(canvas.toDataURL());
            result.blob=await hash(await (await new Promise(r=>canvas.toBlob(r))).arrayBuffer());
            result.blobRepeat=await hash(await (await new Promise(r=>canvas.toBlob(r))).arrayBuffer());
        }
        const off=new OffscreenCanvas(64,32), offctx=off.getContext('2d');
        offctx.fillStyle='#7f7f7f';offctx.fillRect(0,0,64,32);
        result.offscreen=await hash(await (await off.convertToBlob()).arrayBuffer());
        result.offscreenRepeat=await hash(await (await off.convertToBlob()).arrayBuffer());
        result.offscreenPixels=await hash(offctx.getImageData(0,0,64,32).data);
        if (typeof OfflineAudioContext !== 'undefined') {
            const audio=new OfflineAudioContext(1,4096,44100);
            const osc=audio.createOscillator();osc.type='triangle';osc.frequency.value=10000;
            const compressor=audio.createDynamicsCompressor();osc.connect(compressor);compressor.connect(audio.destination);osc.start();
            const buffer=await audio.startRendering();
            const copiedFirst=new Float32Array(buffer.length);buffer.copyFromChannel(copiedFirst,0);
            const channel=buffer.getChannelData(0);
            result.audio=await hash(channel.buffer);
            result.audioCopyFirst=await hash(copiedFirst.buffer);
            result.audioRepeat=await hash(buffer.getChannelData(0).buffer);
            const copied=new Float32Array(buffer.length);buffer.copyFromChannel(copied,0);
            result.audioCopy=await hash(copied.buffer);
            const segment=new Float32Array(64);buffer.copyFromChannel(segment,0,31);
            result.audioOffsetMatches=segment.every((v,i)=>v===channel[i+31]);
        }
        return result;
    }
    const worker = async type => {
        const source='(' + (async function(){
            postMessage({identity:IDENTITY(), fingerprints:await FINGERPRINTS(),nativeMask:NATIVE_MASK(),
                webgl:WEBGL(true),webgl2:WEBGL(true,'webgl2')});
        }).toString().replace('IDENTITY','('+identity.toString()+')').replace('FINGERPRINTS','('+fingerprints.toString()+')')
            .replace('NATIVE_MASK','('+nativeMaskAudit.toString()+')').replaceAll('WEBGL','('+readWebGL.toString()+')') + ')()';
        const url=URL.createObjectURL(new Blob([source],{type:'text/javascript'}));
        try { return await new Promise((resolve,reject)=>{
            const w=new Worker(url,{type}); const timer=setTimeout(()=>{w.terminate();reject(new Error('Worker timeout'))},10000);
            w.onerror=e=>{clearTimeout(timer);w.terminate();reject(new Error(e.message))};
            w.onmessage=e=>{clearTimeout(timer);w.terminate();resolve(e.data)};
        }); } finally {URL.revokeObjectURL(url)}
    };
    const gl=document.createElement('canvas').getContext('webgl');
    const ext=gl.getExtension('WEBGL_debug_renderer_info');
    return {identity:identity(),fingerprints:await fingerprints(),
        nativeMask:nativeMaskAudit(),webgl:readWebGL(false),webgl2:readWebGL(false,'webgl2'),
        offscreenWebgl:readWebGL(true),offscreenWebgl2:readWebGL(true,'webgl2'),
        highEntropy:await navigator.userAgentData?.getHighEntropyValues(['architecture','bitness','model','platformVersion','fullVersionList','uaFullVersion']),
        worker:await worker('classic').catch(e=>({error:e.message})),moduleWorker:await worker('module').catch(e=>({error:e.message})),
        touch:navigator.maxTouchPoints,dpr:devicePixelRatio,
        plugins:navigator.plugins.length, chrome:!!window.chrome,
        gpu:gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)};
}"""


async def passport_headers(account):
    captured = {}

    class Response:
        status = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self, **kwargs):
            return {"message": "success", "data": {"username": "local-probe"}}

    class Session(Response):
        def __init__(self, **kwargs):
            pass

        def get(self, url, **kwargs):
            captured.update(kwargs["headers"])
            return Response()

    with patch.object(cookie_validator.aiohttp, "ClientSession", Session):
        await cookie_validator.validate_account_http(account)
    captured.pop("Cookie", None)
    return captured


async def run_probe():
    settings = browser_pool.settings
    report = {"samples": []}
    with patch.object(settings, "USE_PROXY", False), patch.object(settings, "PLAYWRIGHT_HEADLESS", True), patch.object(
        tiktok_client, "_get_cookies_for_account", lambda _: '{"sessionid":"local-probe","tt-target-idc":"useast5"}'
    ):
        pool = browser_pool.BrowserPool(max_browsers=1)
        try:
            for human_like in (False, True):
                with patch.object(settings, "HUMAN_LIKE_BEHAVIOR", human_like):
                    for account_id in (1219, 1224, 1219):
                        account = SimpleNamespace(id=account_id, label="local-device-probe", proxy=None,
                            user_agent=None, country_code="US", proxy_country_code="SG",
                            cookies='{"sessionid":"local-probe"}', cookie_file=None)
                        context, page = await pool.get_page_for_account(account)
                        captured = []

                        async def serve(route):
                            captured.append(await route.request.all_headers())
                            await route.fulfill(content_type="text/html", body="<html><body>Local probe</body></html>")

                        await context.route("**/*", serve)
                        await page.goto("https://device.fixture.test/")
                        result = await page.evaluate(READ_DEVICE, isolated_context=False)
                        result.update(account_id=account_id, human_like=human_like, headers=captured[0],
                            passport=await passport_headers(account),
                            expected_hardware=context._account_device_profile['identity']['hardware'])
                        from executor.http.playwright_http_client import _extract_runtime_profile
                        runtime = await _extract_runtime_profile(page)
                        result["request_geo"] = resolve_request_geo(runtime, {"tt-target-idc": "useast5"})
                        result["request_headers"] = RequestProfile.publish_main_profile().build_headers(runtime_profile=runtime)
                        report["samples"].append(result)
                        await pool.close_context_for_account(account_id)

            browser = await pool._playwright.chromium.launch(headless=True)
            report["engine_version"] = browser.version
            native = await browser.new_context()
            native_page = await native.new_page()
            report["system"] = await native_page.evaluate("() => ({timezone:Intl.DateTimeFormat().resolvedOptions().timeZone,language:navigator.language})", isolated_context=False)
            from executor.http.request_profile import _executor_language
            report["system"]["locale"] = _executor_language()
            await native.close()
            from executor.device_profiles import build_device_profile
            report["legacy_profiles"] = {}
            for name, ua in {"old_chrome":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                             "iphone":"Mozilla/5.0 iPhone Mobile", "android":"Mozilla/5.0 Android Mobile"}.items():
                account = SimpleNamespace(id=1,user_agent=ua,cookies='{"sessionid":"local-probe"}',cookie_file=None,proxy=None)
                profile = build_device_profile(account, pool._playwright.devices, browser.version)
                report["legacy_profiles"][name] = {"options":profile["options"],"identity":profile["identity"],"override":profile["override"],"passport":await passport_headers(account)}
            await browser.close()
        finally:
            await pool.close_all()
    return report


def _native_mask_passed(audit, *, worker=False):
    """Verify raw results independently; a JavaScript 'passed' flag is insufficient."""
    common = {"Function.toString", "OffscreenCanvasRenderingContext2D.getImageData",
              "OffscreenCanvas.convertToBlob", "WebGLRenderingContext.getParameter",
              "WebGL2RenderingContext.getParameter"}
    if worker:
        required = common | {"DedicatedWorkerGlobalScope.Worker", "DedicatedWorkerGlobalScope.importScripts",
            *(f"WorkerNavigator.{key}:get" for key in ("platform", "hardwareConcurrency", "deviceMemory", "language", "languages"))}
    else:
        required = common | {"HTMLCanvasElement.toDataURL", "HTMLCanvasElement.toBlob",
            "CanvasRenderingContext2D.getImageData", "AudioBuffer.getChannelData", "AudioBuffer.copyFromChannel",
            "Window.Worker", *(f"Navigator.{key}:get" for key in ("platform", "vendor", "hardwareConcurrency", "deviceMemory", "webdriver"))}
    if not isinstance(audit, dict) or audit.get("passed") is not True:
        return False
    hooks = audit.get("hooks", [])
    if not required.issubset({hook.get("name") for hook in hooks}):
        return False
    for hook in hooks:
        name = str(hook.get("name", "")).rsplit(".", 1)[-1]
        function_name = "get " + name[:-4] if name.endswith(":get") else name
        expected = f"function {function_name}() {{ [native code] }}"
        if hook.get("direct") != expected or hook.get("borrowed") != expected:
            return False
    invalid = audit.get("invalid", {})
    stack = invalid.get("stack", "")
    return (invalid.get("threw") is True and invalid.get("isTypeError") is True
            and invalid.get("name") == "TypeError"
            and invalid.get("message") == "Function.prototype.toString requires that 'this' be a Function"
            and len(stack.splitlines()) > 1 and stack.splitlines()[1] == "    at Number.toString (<anonymous>)"
            and not re.search(r"device_identity|installAccountIdentity|toStringHandler|nativeHook|Object\.apply|Reflect\.apply", stack))


def audit_checks(report):
    """Measured invariants, usable for the original and the updated runtime."""
    from executor.browser_identity import chromium_brands, desktop_user_agent, sec_ch_ua
    samples = report["samples"]
    version = report["engine_version"]
    expected_ua = desktop_user_agent(version)
    expected_brands = chromium_brands(version)

    def stable_unique(keys):
        for key in keys:
            per_account = {}
            for sample in samples:
                per_account.setdefault(sample["account_id"], set()).add(sample["fingerprints"][key])
            if any(len(values) != 1 for values in per_account.values()):
                return False
            if len({next(iter(values)) for values in per_account.values()}) != len(per_account):
                return False
        return True

    def workers_match(keys):
        return all(all(worker.get("identity", {}).get(key) == sample["identity"][key] for key in keys)
                   for sample in samples for worker in (sample["worker"], sample["moduleWorker"]))

    def hardware_matches():
        from executor.device_profiles import desktop_hardware
        observed = {}
        for sample in samples:
            expected = desktop_hardware(SimpleNamespace(id=sample['account_id']))
            if "deviceMemory" not in expected:
                return False
            for realm in (sample, sample['worker'], sample['moduleWorker']):
                if realm.get('identity', {}).get('memory') != expected['deviceMemory']:
                    return False
                for key in ('webgl','webgl2'):
                    gl = realm.get(key, {})
                    if not (gl.get('available') and gl.get('debug') and gl.get('error') == 0
                            and gl.get('vendor') == expected['webglVendor'] and gl.get('renderer') == expected['webglRenderer']):
                        return False
            if sample.get('offscreenWebgl') != sample.get('webgl') or sample.get('offscreenWebgl2') != sample.get('webgl2'):
                return False
            values = (sample['identity']['memory'], sample['webgl']['vendor'], sample['webgl']['renderer'])
            if sample['account_id'] in observed and observed[sample['account_id']] != values:
                return False
            observed[sample['account_id']] = values
        return len({v[0] for v in observed.values()}) > 1 and len({v[1:] for v in observed.values()}) > 1

    from executor.http.request_profile import _extract_region_from_locale
    system_locale = report['system'].get('locale')

    return {
        "1. Canvas (toDataURL / toBlob / getImageData)": stable_unique(["dataURL", "blob", "pixels"]),
        "2. Audio (getChannelData / copyFromChannel)": stable_unique(["audio"]) and all(
            s["fingerprints"]["audio"] == s["fingerprints"]["audioRepeat"] == s["fingerprints"]["audioCopy"] == s["fingerprints"]["audioCopyFirst"]
            and s["fingerprints"]["audioOffsetMatches"] for s in samples),
        "3. Main / Workers: CPU, memory and seeded WebGL": workers_match(["cpu", "memory", "platform"]) and hardware_matches(),
        "4. Legacy iPhone / Android -> desktop": all(not p["options"].get("is_mobile", False)
            and not p["options"].get("has_touch", False) and p["options"].get("device_scale_factor") == 1
            for p in report["legacy_profiles"].values()),
        "5. Reduced UA matches installed engine": all(s["identity"]["ua"] == expected_ua for s in samples)
            and all(p["options"]["user_agent"] == expected_ua for p in report["legacy_profiles"].values()),
        "6. GREASE in metadata and sec-ch-ua": all(s["identity"]["ch"]["brands"] == expected_brands
            and s["headers"].get("sec-ch-ua") == sec_ch_ua(version) for s in samples),
        "7. Windows / Win32 / non-mobile hints": all(s["identity"]["platform"] == "Win32"
            and s["headers"].get("sec-ch-ua-platform") == '"Windows"' and s["headers"].get("sec-ch-ua-mobile") == "?0"
            for s in samples) and all(p["identity"]["platform"] == "Win32" for p in report["legacy_profiles"].values()),
        "8. Noise independent of HUMAN_LIKE_BEHAVIOR": stable_unique(["pixels", "dataURL", "blob", "offscreen", "audio"]),
        "9. OffscreenCanvas noise in all realms": stable_unique(["offscreen", "offscreenPixels"]) and all(
            worker.get("fingerprints", {}).get(key) == s["fingerprints"][key]
            for s in samples for worker in (s["worker"], s["moduleWorker"]) for key in ("offscreen", "offscreenPixels", "pixels")),
        "10. Passport UA matches browser and engine": all(s["passport"]["User-Agent"] == s["identity"]["ua"] == expected_ua for s in samples)
            and all(p["passport"]["User-Agent"] == expected_ua for p in report["legacy_profiles"].values()),
        "11. Direct geo follows OS locale, not timezone country": all(
            s["identity"]["timezone"] == s["request_geo"].get("tz_name") == report["system"]["timezone"]
            and s['request_geo'].get('region') == _extract_region_from_locale(system_locale or s['identity']['language'])
            and s['request_geo'].get('browser_language') == (system_locale or s['identity']['language']) for s in samples),
        "12. Exact native hook strings and native TypeError stack": bool(samples) and all(
            _native_mask_passed(realm.get('nativeMask'), worker=worker)
            for s in samples for realm,worker in ((s,False),(s['worker'],True),(s['moduleWorker'],True))),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="output/device-probe/after.json")
    parser.add_argument("--before", help="Original JSON to compare; also writes comparison.md")
    parser.add_argument("--verify", action="store_true", help="Exit with failure if an invariant fails")
    args = parser.parse_args()
    result = asyncio.run(run_probe())
    result["checks"] = audit_checks(result)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Device probe: {len(result['samples'])} samples, Chromium {result['engine_version']}, report: {output}")
    for label, passed in result["checks"].items():
        print(f"{'PASS' if passed else 'FAIL'} {label}")
    if args.before:
        before = json.loads(Path(args.before).read_text(encoding="utf-8"))
        before_checks = audit_checks(before)
        rows = ["| Check | Before | After |", "|---|---|---|"]
        rows += [f"| {name} | {'PASS' if before_checks[name] else 'FAIL'} | {'PASS' if passed else 'FAIL'} |" for name, passed in result["checks"].items()]
        output.with_name("comparison.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    if args.verify and not all(result["checks"].values()):
        raise SystemExit(1)
