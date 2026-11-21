"""Build an obfuscated (minified) extension ZIP for distribution.

The readable source lives in gemini-cookie-sync-extension/. This script
minifies/obfuscates the JavaScript into gemini-cookie-sync-extension/dist/
and builds gemini-connector.zip from there, so end users downloading the ZIP
get hard-to-read code while the repository keeps the clean source.

Requires: npm install terser  (in a node workspace), or set TERSER_PATH.
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

# Locate terser: env override -> local node_modules -> global install -> PATH.
def _find_terser():
    override = os.environ.get("TERSER_PATH")
    if override and os.path.exists(override):
        return override
    local = os.path.join(BASE, "node_modules", ".bin",
                         "terser.cmd" if os.name == "nt" else "terser")
    if os.path.exists(local):
        return local
    return "terser"  # rely on PATH

TERSER = _find_terser()

JS_FILES = ["background.js", "content.js", "popup.js"]
OTHER_FILES = ["manifest.json", "popup.html", "README.txt", "SETUP.md"]


def minify_js(src_path: str, dst_path: str) -> None:
    try:
        result = subprocess.run(
            [TERSER, src_path, "--compress", "passes=3", "--mangle", "--output", dst_path],
            capture_output=True, text=True,
            shell=(os.name == "nt" and TERSER == "terser"),
        )
        if result.returncode == 0 and os.path.exists(dst_path):
            return
        print("terser unavailable, using built-in minifier:", result.stderr[:200])
    except (FileNotFoundError, OSError):
        print("terser not found, using built-in minifier")
    # Fallback: strip comments and leading whitespace (no dependency needed).
    with open(src_path, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith("//")]
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(" ".join(lines))


def main():
    if os.path.exists(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST, exist_ok=True)

    # Minify JS into dist/ (renamed to the same filenames so manifest/popup work).
    for name in JS_FILES:
        src = os.path.join(SRC, name)
        dst = os.path.join(DIST, name)
        if os.path.exists(src):
            minify_js(src, dst)
            print(f"minified {name}: {os.path.getsize(src)} -> {os.path.getsize(dst)} bytes")

    # Copy non-JS files. Rewrite the popup.html script reference stays the same
    # (popup.js in dist is the minified one).
    for name in OTHER_FILES:
        src = os.path.join(SRC, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(DIST, name))

    # Verify manifest references files that exist in dist.
    manifest = json.load(open(os.path.join(DIST, "manifest.json"), encoding="utf-8"))
    assert manifest["background"]["service_worker"] in JS_FILES
    assert manifest["action"]["default_popup"] == "popup.html"
    for cs in manifest.get("content_scripts", []):
        for js in cs.get("js", []):
            assert js in JS_FILES, js

    # Build ZIP from dist.
    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(os.listdir(DIST)):
            path = os.path.join(DIST, name)
            if os.path.isfile(path):
                zf.write(path, arcname=name)

    print(f"built {ZIP_PATH} ({os.path.getsize(ZIP_PATH)} bytes)")


if __name__ == "__main__":
    main()
