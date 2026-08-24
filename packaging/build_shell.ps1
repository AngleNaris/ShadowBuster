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
Write-Host "[1/2] PyInstaller 构建外壳 ..."
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

Write-Host "[2/2] 产物: $root\dist\ShadowBuster\ShadowBuster.exe"
Write-Host "下一步: 运行 runtime_sync.ps1 装配 AI 运行时，再用 Inno 打安装包。"
