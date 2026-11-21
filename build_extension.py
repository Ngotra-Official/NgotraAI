"""Build a hardened (obfuscated) extension ZIP for distribution.

The readable source lives in gemini-cookie-sync-extension/. This script
obfuscates the JavaScript into gemini-cookie-sync-extension/dist/ and builds
gemini-connector.zip from there, so end users get hard-to-read code while the
repository keeps the clean source.

Layers (best available is used):
  1. javascript-obfuscator  — string-array encoding, control-flow flattening,
     dead-code injection, identifier mangling, self-defending (anti-tamper).
  2. terser                 — compression + identifier mangling fallback.
  3. built-in minifier      — comment/whitespace stripping last resort.

Install tools with: npm install javascript-obfuscator terser
"""
import os
import json
import shutil
import subprocess
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "gemini-cookie-sync-extension")
DIST = os.path.join(SRC, "dist")
ZIP_PATH = os.path.join(BASE, "gemini-connector.zip")
BIN = os.path.join(BASE, "node_modules", ".bin")

EXT = ".cmd" if os.name == "nt" else ""


def _bin(name):
    local = os.path.join(BIN, name + EXT)
    if os.path.exists(local):
        return local
    return name  # rely on PATH


OBFUSCATOR = _bin("javascript-obfuscator")
TERSER = _bin("terser")

JS_FILES = ["background.js", "content.js", "popup.js"]
OTHER_FILES = ["manifest.json", "popup.html", "icon.png", "README.txt", "SETUP.md"]

# Strong but Chrome-extension-safe options.
# renameGlobals is OFF so top-level helpers keep working across the content
# script / service worker boundary, and no property mangling is used so
# chrome.* APIs stay intact.
OBFUSCATOR_OPTIONS = {
    "compact": True,
    # Control-flow flattening and dead-code injection are intentionally OFF:
    # they can break Chrome extension scope and slow the popup down.
    "controlFlowFlattening": False,
    "deadCodeInjection": False,
    "debugProtection": False,
    "disableConsoleOutput": True,
    "identifierNamesGenerator": "hexadecimal",
    "log": False,
    "numbersToExpressions": True,
    "renameGlobals": False,
    "rotateStringArray": True,
    "selfDefending": True,
    "simplify": True,
    "stringArray": True,
    "stringArrayEncoding": ["base64"],
    "stringArrayIndexShift": True,
    "stringArrayThreshold": 0.75,
    "stringArrayWrappersCount": 1,
    "transformObjectKeys": False,
    "unicodeEscapeSequence": False,
}


def _obfuscate(src, dst):
    config_path = os.path.join(DIST, "_obfuscator_config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(OBFUSCATOR_OPTIONS, f)
    try:
        result = subprocess.run(
            [OBFUSCATOR, src, "--config", config_path, "--output", dst],
            capture_output=True, text=True,
            shell=(os.name == "nt" and OBFUSCATOR == "javascript-obfuscator"),
        )
        if result.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            return True
        print("obfuscator failed:", result.stderr[:300])
    except (FileNotFoundError, OSError):
        print("javascript-obfuscator not available")
    return False


def _terser_minify(src, dst):
    try:
        result = subprocess.run(
            [TERSER, src, "--compress", "passes=3", "--mangle", "--output", dst],
            capture_output=True, text=True,
            shell=(os.name == "nt" and TERSER == "terser"),
        )
        if result.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
            return True
        print("terser failed:", result.stderr[:200])
    except (FileNotFoundError, OSError):
        print("terser not available")
    return False


def build_js(src_path, dst_path):
    if _obfuscate(src_path, dst_path):
        return "obfuscated"
    if _terser_minify(src_path, dst_path):
        return "minified (terser)"
    with open(src_path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("//")]
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(" ".join(lines))
    return "stripped (fallback)"


def main():
    if os.path.exists(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST, exist_ok=True)

    for name in JS_FILES:
        src = os.path.join(SRC, name)
        dst = os.path.join(DIST, name)
        if os.path.exists(src):
            mode = build_js(src, dst)
            print(f"{name}: {mode} {os.path.getsize(src)} -> {os.path.getsize(dst)} bytes")

    for name in OTHER_FILES:
        src = os.path.join(SRC, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(DIST, name))

    # Remove the temporary config from the distribution.
    cfg = os.path.join(DIST, "_obfuscator_config.json")
    if os.path.exists(cfg):
        os.remove(cfg)

    manifest = json.load(open(os.path.join(DIST, "manifest.json"), encoding="utf-8"))
    assert manifest["background"]["service_worker"] in JS_FILES
    assert manifest["action"]["default_popup"] == "popup.html"
    for size, path in manifest["icons"].items():
        assert os.path.exists(os.path.join(DIST, path)), path

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(os.listdir(DIST)):
            path = os.path.join(DIST, name)
            if os.path.isfile(path):
                zf.write(path, arcname=name)

    print(f"built {ZIP_PATH} ({os.path.getsize(ZIP_PATH)} bytes)")


if __name__ == "__main__":
    main()
