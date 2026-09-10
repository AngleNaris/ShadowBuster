# -*- mode: python ; coding: utf-8 -*-
# ShadowBuster macOS（Apple Silicon）打包 spec。
# 与 ShadowBuster.spec（Windows）保持同构：同一入口、同一 datas/hiddenimports/
# excludes 契约（tests/test_shell_dependencies.py 校验的 soundfile 语义不变），
# 差异仅在产物形态（.app BUNDLE）与图标（.icns）。
# runtime/（推理解释器 + Apollo/Soren 组件 + ffmpeg + 权重）不打进 PyInstaller
# datas，由 build_macos.sh 装配后拷入 Contents/MacOS/runtime —— studio_backend
# 的 _resolve_runtime() 按 ROOT/runtime 发现它。
from pathlib import Path
import re
import sys

root = Path(SPECPATH).resolve()
icns = root / "ui" / "logo.icns"
icon = [str(icns)] if icns.exists() else []
# 版本号单一来源与 studio_backend.APP_VERSION 同步（Finder 显示包版本）
m = re.search(r'APP_VERSION = "([\d.]+)"', (root / "studio_backend.py").read_text(encoding="utf-8"))
app_version = m.group(1) if m else "0.0.0"


a = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "ui"), "ui")],
    hiddenimports=["numpy", "soundfile",
                   # macOS Dock 图标绕过系统模板化（NSDockTile.contentView）
                   "objc", "Foundation", "AppKit"],
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
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ShadowBuster",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ShadowBuster.app",
        icon=str(icns) if icns.exists() else None,
        bundle_identifier="com.anglenaris.shadowbuster",
        info_plist={
            "CFBundleName": "ShadowBuster",
            "CFBundleDisplayName": "ShadowBuster",
            "CFBundleShortVersionString": app_version,
            "CFBundleVersion": app_version,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
