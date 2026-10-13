# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

# Qt uses Windows' unversioned ICU API. Native tools on the caller's PATH can
# provide an incompatible icuuc.dll with version-suffixed exports.
if sys.platform == "win32":
    windows = Path(os.environ["SystemRoot"])
    os.environ["PATH"] = os.pathsep.join(map(str, (
        windows / "System32", windows, Path(sys.executable).parent, Path(sys.base_prefix),
    )))

root = Path(SPECPATH).resolve()


a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "ui"), "ui")],
    hiddenimports=["numpy", "soundfile"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "torchaudio", "torchvision", "demucs", "scipy", "audioread", "librosa"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ShadowBuster",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(root / "ui" / "logo.ico")],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ShadowBuster",
)
