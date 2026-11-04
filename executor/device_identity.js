function installAccountIdentity(profile) {
    const nativeToString = Function.prototype.toString;
    const nativeSources = new WeakMap();
    // Keep native function shape/name/length by using the original as the
    // Proxy target. Only registered hooks receive an alternate source string.
    const nativeHook = (original, implementation) => {
        const hooked = new Proxy(original, {
            apply(target, receiver, args) { return Reflect.apply(implementation, receiver, args); },
        });
        nativeSources.set(hooked, Reflect.apply(nativeToString, original, []));
        return hooked;
    };
    const replaceMethod = (owner, name, implementation) => {
        Object.defineProperty(owner, name, {
            ...Object.getOwnPropertyDescriptor(owner, name),
            value: nativeHook(owner[name], implementation),
        });
    };
    const toStringHandler = {
        apply(target, receiver, args) {
            if (nativeSources.has(receiver)) return nativeSources.get(receiver);
            try { return Reflect.apply(target, receiver, args); }
            catch (error) {
                // Forward the engine's own TypeError. Remove only this trap's
                // frame while keeping the native frame and the caller's stack.
                if (error instanceof TypeError && typeof error.stack === 'string') {
                    const nativeFrame = error.stack.split('\n')[1];
                    Error.captureStackTrace(error, toStringHandler.apply);
                    const stack = error.stack.split('\n');
                    if (nativeFrame) stack.splice(1, 0, nativeFrame);
                    error.stack = stack.join('\n');
                }
                throw error;
            }
        },
    };
    const toString = new Proxy(nativeToString, toStringHandler);
    nativeSources.set(toString, Reflect.apply(nativeToString, nativeToString, []));
    Object.defineProperty(Function.prototype, 'toString', {
        ...Object.getOwnPropertyDescriptor(Function.prototype, 'toString'), value: toString,
    });
    const workerRealm = typeof window === 'undefined';
    const proto = Object.getPrototypeOf(navigator);
    const values = {};
    if (profile.platform) {
        values.platform = profile.platform;
    }
    if (profile.hardware?.hardwareConcurrency) {
        values.hardwareConcurrency = profile.hardware.hardwareConcurrency;
    }
    if (profile.hardware?.deviceMemory) {
        values.deviceMemory = profile.hardware.deviceMemory;
    }
    if (profile.family === 'windows') {
        values.maxTouchPoints = 0;
    }
    for (const [key, value] of Object.entries(values)) {
        const descriptor = Object.getOwnPropertyDescriptor(proto, key);
        if (descriptor?.get) Object.defineProperty(proto, key, {...descriptor,
            get: nativeHook(descriptor.get, function() {
                Reflect.apply(descriptor.get, this, []);
                return value;
            })});
    }
    if (!workerRealm && profile.hardware?.width && profile.hardware?.height) {
        for (const [key, size] of [['outerWidth', profile.geometry.outerWidth], ['outerHeight', profile.geometry.outerHeight]]) {
            const descriptor = Object.getOwnPropertyDescriptor(window, key) || Object.getOwnPropertyDescriptor(Object.getPrototypeOf(window), key);
            if (descriptor?.get) Object.defineProperty(window, key, {...descriptor,
                get: nativeHook(descriptor.get, function() {
                    Reflect.apply(descriptor.get, this, []);
                    return size;
                })});
        }
        if (globalThis.Screen?.prototype) {
            for (const [key, size] of [
                ['availWidth', profile.geometry.availWidth],
                ['availHeight', profile.geometry.availHeight],
                ['colorDepth', 24],
                ['pixelDepth', 24],
            ]) {
                const descriptor = Object.getOwnPropertyDescriptor(Screen.prototype, key);
                if (descriptor?.get) Object.defineProperty(Screen.prototype, key, {...descriptor,
                    get: nativeHook(descriptor.get, function() {
                        Reflect.apply(descriptor.get, this, []);
                        return size;
                    })});
            }
        }
    }
    if (globalThis.GPUAdapter?.prototype && profile.hardware?.webglRenderer) {
        const infoDesc = Object.getOwnPropertyDescriptor(GPUAdapter.prototype, 'info');
        if (infoDesc?.get) {
            const rawRenderer = String(profile.hardware.webglRenderer || '');
            const rawVendor = String(profile.hardware.webglVendor || '').toLowerCase();
            const gpuVendor = rawVendor.includes('amd') ? 'amd' : (rawVendor.includes('intel') ? 'intel' : 'nvidia');
            const gpuArch = gpuVendor === 'amd' ? 'rdna-2' : (gpuVendor === 'intel' ? 'alder-lake' : 'ampere');
            const m = rawRenderer.match(/ANGLE \([^,]+,\s*([^,]+?)(?:\s+Direct3D|\s*,)/i);
            const gpuDesc = m ? m[1].trim() : rawRenderer;
            Object.defineProperty(GPUAdapter.prototype, 'info', {
                ...infoDesc,
                get: nativeHook(infoDesc.get, function() {
                    const nativeInfo = Reflect.apply(infoDesc.get, this, []);
                    if (!nativeInfo || typeof nativeInfo !== 'object') return nativeInfo;
                    return new Proxy(nativeInfo, {
                        get(target, prop, receiver) {
                            if (prop === 'vendor') return gpuVendor;
                            if (prop === 'architecture') return gpuArch;
                            if (prop === 'device' || prop === 'description') return gpuDesc;
                            const val = Reflect.get(target, prop, target);
                            return typeof val === 'function' ? val.bind(target) : val;
                        },
                    });
                }),
            });
        }
    }

    const mix = value => {
        value = Math.imul(value ^ (value >>> 16), 0x45d9f3b);
        return (value ^ (value >>> 16)) >>> 0;
    };

    for (const Context of [globalThis.WebGLRenderingContext, globalThis.WebGL2RenderingContext]) {
        if (!Context) continue;
        const original = Context.prototype.getParameter;
        replaceMethod(Context.prototype, 'getParameter', function getParameter(parameter) {
                if (!arguments.length) return Reflect.apply(original, this, arguments);
                let key;
                const converted = {valueOf() {key = +parameter >>> 0; return key;}};
                const value = Reflect.apply(original, this, [converted]);
                if (value !== null && key === 37445) return profile.hardware.webglVendor;
                if (value !== null && key === 37446) return profile.hardware.webglRenderer;
                return value;
        });
        const originalReadPixels = Context.prototype.readPixels;
        if (typeof originalReadPixels === 'function') {
            replaceMethod(Context.prototype, 'readPixels', function readPixels(...args) {
                const result = Reflect.apply(originalReadPixels, this, args);
                const pixels = args[6];
                // Only the RGBA byte-array overload at offset zero uses the
                // layout below. Leave other WebGL2 layouts and native coercions alone.
                if (args[4] !== 0x1908 || args[5] !== 0x1401 ||
                    (args[7] !== undefined && args[7] !== 0) ||
                    !args.slice(0, 4).every(value => typeof value === 'number')) return result;
                const width = args[2] >> 0, height = args[3] >> 0;
                const sx = args[0] >> 0, sy = args[1] >> 0;
                if (pixels && ArrayBuffer.isView(pixels) && pixels.BYTES_PER_ELEMENT === 1 &&
                    pixels.length >= width * height * 4 && width > 1 && height > 0) {
                    let count = 0;
                    for (let y = 0; y < height && count < 12; y++) {
                        for (let x = 1; x < width && count < 12; x++) {
                            const offset = (y * width + x) * 4;
                            if (offset + 3 >= pixels.length) break;
                            const prevOffset = offset - 4;
                            if (!pixels[offset + 3] && !pixels[prevOffset + 3]) continue;
                            if (pixels[offset] !== pixels[prevOffset] ||
                                pixels[offset + 1] !== pixels[prevOffset + 1] ||
                                pixels[offset + 2] !== pixels[prevOffset + 2] ||
                                pixels[offset + 3] !== pixels[prevOffset + 3]) {
                                let targetChannel = -1;
                                for (const c of [2, 1, 0, 3]) {
                                    const val = pixels[offset + c];
                                    if (val > 4 && val < 251) {
                                        targetChannel = c;
                                        break;
                                    }
                                }
                                if (targetChannel !== -1) {
                                    const seed = mix(profile.canvasSeed ^ Math.imul(x + sx + 1, 0x1f123bb5) ^ Math.imul(y + sy + 1, 0x5f356495));
                                    if ((seed & 3) === 0) {
                                        pixels[offset + targetChannel] ^= 1;
                                        count++;
                                        x++;
                                    }
                                }
                            }
                        }
                    }
                }
                return result;
            });
        }
    }

    // Set lowest bit on at most 12 antialiased boundary pixels.
    // Pure uniform fills (fillRect) and solid areas remain completely untouched.
    const noisePixels = (image, sx = 0, sy = 0) => {
        const {data, width, height} = image;
        if (!data || width <= 1 || height <= 0) return image;
        let count = 0;
        for (let y = 0; y < height && count < 12; y++) {
            for (let x = 1; x < width && count < 12; x++) {
                const offset = (y * width + x) * 4;
                const prevOffset = offset - 4;
                if (!data[offset + 3] && !data[prevOffset + 3]) continue;
                if (data[offset] !== data[prevOffset] ||
                    data[offset + 1] !== data[prevOffset + 1] ||
                    data[offset + 2] !== data[prevOffset + 2] ||
                    data[offset + 3] !== data[prevOffset + 3]) {
                    let targetChannel = -1;
                    for (const c of [2, 1, 0, 3]) {
                        const val = data[offset + c];
                        if (val > 4 && val < 251) {
                            targetChannel = c;
                            break;
                        }
                    }
                    if (targetChannel !== -1) {
                        const seed = mix(profile.canvasSeed ^ Math.imul(x + sx + 1, 0x1f123bb5) ^ Math.imul(y + sy + 1, 0x5f356495));
                        if ((seed & 3) === 0) {
                            data[offset + targetChannel] ^= 1;
                            count++;
                            x++;
                        }
                    }
                }
            }
        }
        return image;
    };
    const originals = new Map();
    for (const Type of [globalThis.CanvasRenderingContext2D, globalThis.OffscreenCanvasRenderingContext2D]) {
        if (!Type) continue;
        const original = Type.prototype.getImageData;
        originals.set(Type, original);
        replaceMethod(Type.prototype, 'getImageData', function getImageData(...args) {
                const coordinates = [];
                const converted = args.map((value, i) => i >= 4 ? value : {valueOf() {
                    const number = +value; coordinates[i] = number >> 0; return number;
                }});
                const image = Reflect.apply(original, this, converted);
                const [x, y, w, h] = coordinates;
                return noisePixels(image, x + Math.min(0, w), y + Math.min(0, h));
        });
        if (typeof Type.prototype.measureText === 'function') {
            const origMeasureText = Type.prototype.measureText;
            replaceMethod(Type.prototype, 'measureText', function measureText(...args) {
                const metrics = Reflect.apply(origMeasureText, this, args);
                if (metrics && typeof metrics.width === 'number' && metrics.width > 0) {
                    const textStr = String(args[0] ?? '');
                    const h = mix(profile.canvasSeed ^ Math.imul(textStr.length + 1, 0x1f123bb5));
                    const delta = ((h / 0xffffffff) - 0.5) * 0.001;
                    try {
                        Object.defineProperty(metrics, 'width', {
                            value: metrics.width + delta,
                            configurable: true,
                        });
                    } catch (_) {}
                }
                return metrics;
            });
        }
    }
    // Encode a copy: concurrent blob encodes never modify the source canvas.
    const noisySurface = (source, offscreen) => {
        const copy = offscreen ? new OffscreenCanvas(source.width, source.height) : document.createElement('canvas');
        copy.width = source.width; copy.height = source.height;
        if (!copy.width || !copy.height) return copy;
        const ctx = copy.getContext('2d');
        ctx.drawImage(source, 0, 0);
        const Type = offscreen ? OffscreenCanvasRenderingContext2D : CanvasRenderingContext2D;
        const image = Reflect.apply(originals.get(Type), ctx, [0, 0, copy.width, copy.height]);
        ctx.putImageData(noisePixels(image), 0, 0);
        return copy;
    };
    for (const [Type, names, offscreen] of [
        [globalThis.HTMLCanvasElement, ['toDataURL', 'toBlob'], false],
        [globalThis.OffscreenCanvas, ['convertToBlob'], true],
    ]) {
        if (!Type) continue;
        for (const name of names) {
            const original = Type.prototype[name];
            replaceMethod(Type.prototype, name, function(...args) {
                    if (!(this instanceof Type)) return Reflect.apply(original, this, args);
                    try {
                        return Reflect.apply(original, noisySurface(this, offscreen), args);
                    } catch (error) {
                        // Preserve native errors/promises for tainted, detached,
                        // invalid or zero-sized surfaces.
                        return Reflect.apply(original, this, args);
                    }
            });
        }
    }

    if (globalThis.AudioBuffer) {
        const Type = AudioBuffer.prototype;
        const get = Type.getChannelData, copy = Type.copyFromChannel;
        const states = new WeakMap();
        const perturb = (buffer, channel, data) => {
            let channels = states.get(buffer);
            if (!channels) { channels = new Map(); states.set(buffer, channels); }
            let state = channels.get(channel);
            if (!state || state.data !== data) {
                state = {data, values: new Map()}; channels.set(channel, state);
            }
            for (let i = 0; i < data.length; i += 97) {
                const value = data[i];
                if (state.values.has(i) && Object.is(state.values.get(i), value)) continue;
                const noise = (mix(profile.audioSeed ^ Math.imul(channel + 1, 0x1f123bb5) ^ i) / 0xffffffff - 0.5) * 2e-7;
                data[i] = value + noise;
                state.values.set(i, data[i]);
            }
            return data;
        };
        replaceMethod(Type, 'getChannelData', function getChannelData(channel) {
            if (!arguments.length) return Reflect.apply(get, this, arguments);
            let index;
            const converted = {valueOf() {const number = +channel; index = number >>> 0; return number;}};
            const data = Reflect.apply(get, this, [converted]);
            return perturb(this, index, data);
        });
        replaceMethod(Type, 'copyFromChannel', function copyFromChannel(destination, channel, start = 0) {
            if (arguments.length < 2) return Reflect.apply(copy, this, arguments);
            let index, offset;
            const convertedChannel = {valueOf() {const number = +channel; index = number >>> 0; return number;}};
            const convertedStart = {valueOf() {const number = +start; offset = number >>> 0; return number;}};
            Reflect.apply(copy, this, [destination, convertedChannel, convertedStart]);
            perturb(this, index, Reflect.apply(get, this, [index]));
            return Reflect.apply(copy, this, [destination, index, offset]);
        });
    }

    if (workerRealm && profile.workerBase && profile.workerType !== 'module') {
        const original = globalThis.importScripts;
        replaceMethod(globalThis, 'importScripts', function importScripts(...urls) {
            return Reflect.apply(original, this, urls.map(url => new URL(String(url), profile.workerBase).href));
        });
    }

    if (globalThis.Worker) {
        const NativeWorker = Worker;
        const activeWorkerBlobs = new Set();
        if (globalThis.URL && typeof URL.revokeObjectURL === 'function') {
            const nativeRevoke = URL.revokeObjectURL;
            replaceMethod(URL, 'revokeObjectURL', function revokeObjectURL(url) {
                if (!arguments.length) return Reflect.apply(nativeRevoke, this, arguments);
                const key = String(url);
                if (activeWorkerBlobs.has(key)) {
                    setTimeout(() => {
                        activeWorkerBlobs.delete(key);
                        try { Reflect.apply(nativeRevoke, URL, [key]); } catch (e) {}
                    }, 1500);
                    return undefined;
                }
                return Reflect.apply(nativeRevoke, this, arguments);
            });
        }
        const AccountWorker = new Proxy(NativeWorker, {
            construct(Target, args, newTarget) {
                if (!args.length) return Reflect.construct(Target, args, newTarget);
                const base = profile.workerBase || globalThis.location.href;
                const url = new URL(String(args[0]), base);
                // Native constructor enforces cross-origin restrictions.
                if (url.origin !== globalThis.location.origin && !['blob:', 'data:'].includes(url.protocol)) {
                    return Reflect.construct(Target, args, newTarget);
                }
                if (url.protocol === 'blob:') activeWorkerBlobs.add(url.href);
                const options = args[1];
                const workerProfile = {...profile,
                    workerBase:url.href, workerType:options?.type || 'classic'};
                let source = '(' + installAccountIdentity.toString() + ')(' + JSON.stringify(workerProfile) + ');';
                if (options?.type === 'module') {
                    // Dynamic import yields before the module registers its
                    // listeners. Keep startup messages queued as native Worker
                    // startup does, including transferred ports/buffers.
                    source += 'const pendingMessages=[];const queueMessage=e=>{e.stopImmediatePropagation();pendingMessages.push(e)};'
                        + 'addEventListener("message",queueMessage,true);addEventListener("messageerror",queueMessage,true);'
                        + 'try{await import(' + JSON.stringify(url.href) + ');}finally{URL.revokeObjectURL(location.href);}'
                        + 'removeEventListener("message",queueMessage,true);removeEventListener("messageerror",queueMessage,true);'
                        + 'for(const event of pendingMessages)dispatchEvent(new MessageEvent(event.type,{'
                        + 'data:event.data,origin:event.origin,lastEventId:event.lastEventId,source:event.source,ports:[...event.ports]}));';
                } else {
                    source += 'try{importScripts(' + JSON.stringify(url.href) + ');}finally{URL.revokeObjectURL(location.href);}';
                }
                const bootstrap = URL.createObjectURL(new Blob([source], {type:'text/javascript'}));
                try { return Reflect.construct(Target, [bootstrap, options], newTarget); }
                catch (error) { activeWorkerBlobs.delete(url.href); URL.revokeObjectURL(bootstrap); throw error; }
            },
        });
        nativeSources.set(AccountWorker, Reflect.apply(nativeToString, NativeWorker, []));
        globalThis.Worker = AccountWorker;
    }
}
