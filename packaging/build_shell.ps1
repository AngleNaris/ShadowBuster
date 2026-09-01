# ══════════════════════════════════════════════════════════════════
# ShadowBuster — UI 外壳打包（PyInstaller onedir）
#
# 说明：
#   - 本步骤只打包 PySide6 + QtWebEngine 的 UI 外壳（ShadowBuster.exe）。
#   - AI 推理运行时（torch/demucs/look2hear + 模型）不塞进外壳，而是由
#     Inno 安装器作为独立 runtime 目录装配（见 DEPLOY.md）。
#   - 产物: dist/ShadowBuster/ShadowBuster.exe
# ══════════════════════════════════════════════════════════════════
$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$py   = "$env:USERPROFILE\.local\share\uv\python\cpython-3.12.11-windows-x86_64-none\python.exe"
if (-not (Test-Path $py)) {
    $py = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $py) { throw "找不到 python" }
$venvPy = "D:\_3.AI\audio_upscale\UniverSR\.venv\Scripts\python.exe"

Set-Location $root
Write-Host "[1/3] PyInstaller 构建外壳 ..."
& $venvPy -m PyInstaller --noconfirm --clean `
    --onedir `
    --windowed `
    --name ShadowBuster `
    --icon "$root\ui\logo.ico" `
    --add-data "$root\ui;ui" `
    --exclude-module torch `
    --exclude-module torchaudio `
    --exclude-module torchvision `
    --exclude-module demucs `
    --exclude-module scipy `
    --exclude-module soundfile `
    --exclude-module audioread `
    --exclude-module librosa `
    "$root\main.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败(exit=$LASTEXITCODE)" }

# [2/3] 体积裁剪：外壳是 QtWidgets + QtWebEngine 应用，运行期不需要
#   QML 模块目录（qml/）与 Qt 自身模块的翻译文件；WebEngine 自己的 locale
#   只留中文与英文兜底（界面仅中文，其余语言回退 en-US.pak）。
#   注意：Qt6Quick/Qml 等 DLL 是 Qt6WebEngineCore 的二进制依赖，不能删。
Write-Host "[2/3] 裁剪 QML 目录与语言包 ..."
$bundle = "$root\dist\ShadowBuster\_internal\PySide6"
if (Test-Path "$bundle\qml") { Remove-Item "$bundle\qml" -Recurse -Force }
if (Test-Path "$bundle\translations") {
    Get-ChildItem "$bundle\translations" -Exclude "qtwebengine_locales" |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    $locales = "$bundle\translations\qtwebengine_locales"
    Get-ChildItem $locales -Exclude "zh-CN.pak", "zh-TW.pak", "en-US.pak" |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

Write-Host "[3/3] 产物: $root\dist\ShadowBuster\ShadowBuster.exe"
Write-Host "下一步: 运行 runtime_sync.ps1 装配 AI 运行时，再用 Inno 打安装包。"
