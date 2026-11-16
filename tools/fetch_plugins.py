"""Fetch the pinned third-party VST3 plugins into <repo>/plugins (git-ignored).

ZL Compressor and ZL Limiter (AGPL-3.0) are used by the mastering engine.
Each Windows MSI is verified by SHA-256 and unpacked with an
administrative install (msiexec /a), which only extracts files and registers nothing.
runtime_sync.ps1 copies <repo>/plugins into runtime/plugins.

    python tools/fetch_plugins.py
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "plugins"
ZL_COMPRESSOR = {
    "bundle": "ZL Compressor.vst3",
    "url": "https://github.com/ZL-Audio/ZLCompressor/releases/download/0.6.1/"
           "ZL.Compressor-0.6.1-Windows-x86-64.msi",
    "sha256": "4e6e14da03cbd4513d890fac01889982e3cd96b2cdeb73678507a09b0c463f7f",
}
ZL_LIMITER = {
    "bundle": "ZL Limiter.vst3",
    "url": "https://github.com/ZL-Audio/ZLLimiter/releases/download/0.0.1/"
           "ZL.Limiter-0.0.1-Windows-x86-64.msi",
    "sha256": "c58ecd0afe2ab2d1040a1fc7c39bd667eb45acdc84ac752602fd2f7af0be1550",
}


def fetch(spec):
    target = PLUGINS / spec["bundle"]
    if target.is_dir():
        print("present:", target)
        return
    if sys.platform != "win32":
        raise SystemExit("Only the Windows build is pinned; install the plugin manually on this platform")
    with tempfile.TemporaryDirectory(prefix="sb-plugin-") as tmp:
        msi = Path(tmp) / "plugin.msi"
        urllib.request.urlretrieve(spec["url"], msi)
        digest = hashlib.sha256(msi.read_bytes()).hexdigest()
        if digest != spec["sha256"]:
            raise SystemExit(f"SHA-256 mismatch for {spec['url']}: {digest}")
        extracted = Path(tmp) / "x"
        subprocess.run(["msiexec", "/a", str(msi), "/qn", f"TARGETDIR={extracted}"], check=True)
        bundle = next(extracted.rglob(spec["bundle"]))
        PLUGINS.mkdir(exist_ok=True)
        shutil.copytree(bundle, target)
    print("installed:", target)


if __name__ == "__main__":
    fetch(ZL_COMPRESSOR)
    fetch(ZL_LIMITER)
