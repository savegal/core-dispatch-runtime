import json
import pytest
import subprocess


def test_canvas_solid_fill_and_antialiasing_noise():
    node_script = """
    const fs = require('fs');
    const code = fs.readFileSync('executor/device_identity.js', 'utf8');

    // Extract noisePixels and mix functions
    const fnMatch = code.match(/const mix = ([\\s\\S]*?\\n    \\};)[\\s\\S]*?const noisePixels = ([\\s\\S]*?\\n    \\};)/);
    if (!fnMatch) throw new Error('Could not find mix and noisePixels in device_identity.js');

    const mixCode = fnMatch[1].replace(/;\\s*$/, '');
    const noisePixelsCode = fnMatch[2].replace(/;\\s*$/, '');

    const mix = eval('(' + mixCode + ')');
    const getNoisePixels = (canvasSeed) => {
        const profile = { canvasSeed };
        return eval('(' + noisePixelsCode + ')');
    };

    const noisePixelsA = getNoisePixels(12345);
    const noisePixelsB = getNoisePixels(67890);

    // 1. Solid Red 20x20 fill test (like TikTok antifraud check)
    const width = 20, height = 20;
    const solidRed = new Uint8ClampedArray(width * height * 4);
    for (let i = 0; i < solidRed.length; i += 4) {
        solidRed[i] = 255;
        solidRed[i + 1] = 0;
        solidRed[i + 2] = 0;
        solidRed[i + 3] = 255;
    }
    const redImg = { data: new Uint8ClampedArray(solidRed), width, height };
    const noisyRed = noisePixelsA(redImg);

    let redDiff = 0;
    for (let i = 0; i < solidRed.length; i++) {
        if (noisyRed.data[i] !== solidRed[i]) redDiff++;
    }
    if (redDiff !== 0) {
        throw new Error('Solid fill was modified: ' + redDiff + ' bytes changed! Antifraud will detect tampering.');
    }

    // 2. Antialiased text edge test
    const textData = new Uint8ClampedArray(width * height * 4);
    for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
            const offset = (y * width + x) * 4;
            textData[offset + 3] = 255;
            if (x < 5) {
                textData[offset] = 0; textData[offset + 1] = 0; textData[offset + 2] = 0;
            } else if (x <= 15) {
                const val = (x - 5) * 20; // values in range 0..200
                textData[offset] = val; textData[offset + 1] = val; textData[offset + 2] = val;
            } else {
                textData[offset] = 255; textData[offset + 1] = 255; textData[offset + 2] = 255;
            }
        }
    }

    const textImgA = { data: new Uint8ClampedArray(textData), width, height };
    const textImgB = { data: new Uint8ClampedArray(textData), width, height };

    const noisyA = noisePixelsA(textImgA);
    const noisyB = noisePixelsB(textImgB);

    let diffA = 0;
    for (let i = 0; i < textData.length; i++) {
        if (noisyA.data[i] !== textData[i]) diffA++;
    }
    if (diffA === 0) throw new Error('Antialiased text was not modified at all!');
    if (diffA > 12) throw new Error('Too many pixels modified on antialiased text: ' + diffA);

    // 3. Different accounts (seeds) produce different modifications
    let diffAB = 0;
    for (let i = 0; i < textData.length; i++) {
        if (noisyA.data[i] !== noisyB.data[i]) diffAB++;
    }
    if (diffAB === 0) throw new Error('Different seeds produced identical results!');

    // 4. Black text on transparent background (R=0, G=0, B=0, Alpha changes 0..200..255)
    const transparentBlack = new Uint8ClampedArray(width * height * 4);
    for (let y = 0; y < height; y++) {
        for (let x = 0; x < width; x++) {
            const offset = (y * width + x) * 4;
            transparentBlack[offset] = 0; transparentBlack[offset + 1] = 0; transparentBlack[offset + 2] = 0;
            if (x < 5) transparentBlack[offset + 3] = 0;
            else if (x <= 15) transparentBlack[offset + 3] = (x - 5) * 20;
            else transparentBlack[offset + 3] = 255;
        }
    }
    const blackImg = { data: new Uint8ClampedArray(transparentBlack), width, height };
    const noisyBlack = noisePixelsA(blackImg);
    let blackDiff = 0;
    for (let i = 0; i < transparentBlack.length; i++) {
        if (noisyBlack.data[i] !== transparentBlack[i]) blackDiff++;
    }
    if (blackDiff === 0) throw new Error('Black text on transparent canvas was not modified (alpha edge missed)!');

    // 5. No adjacent pixel cascade (two consecutive horizontal pixels should never both be modified)
    for (let y = 0; y < height; y++) {
        for (let x = 1; x < width - 1; x++) {
            const off1 = (y * width + x) * 4;
            const off2 = (y * width + (x + 1)) * 4;
            let mod1 = false, mod2 = false;
            for (let c = 0; c < 4; c++) {
                if (noisyA.data[off1 + c] !== textData[off1 + c]) mod1 = true;
                if (noisyA.data[off2 + c] !== textData[off2 + c]) mod2 = true;
            }
            if (mod1 && mod2) {
                throw new Error('Cascade defect: adjacent horizontal pixels were both modified at x=' + x + ', y=' + y);
            }
        }
    }

    console.log(JSON.stringify({ ok: true, redDiff, diffA, diffAB, blackDiff }));
    """
    res = subprocess.run(["node", "-e", node_script], capture_output=True, text=True, check=True)
    out = json.loads(res.stdout.strip())
    assert out["ok"] is True
    assert out["redDiff"] == 0
    assert 0 < out["diffA"] <= 12
    assert out["diffAB"] > 0
