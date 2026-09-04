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
$workspace = Split-Path $root -Parent
$shellCandidates = @(
    $env:SB_SHELL_PYTHON,
    "$root\.venv\Scripts\python.exe",
    "$workspace\UniverSR\.venv\Scripts\python.exe",
    (Get-Command python -ErrorAction SilentlyContinue).Source
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
$venvPy = $shellCandidates | Select-Object -First 1
if (-not $venvPy) {
    throw "找不到外壳构建 Python，请设置 SB_SHELL_PYTHON 或在项目 .venv 中安装 PySide6、PyInstaller、numpy"
}

& $venvPy -c "import numpy, PyInstaller; print('shell build dependencies OK', numpy.__version__)"
if ($LASTEXITCODE -ne 0) { throw "外壳构建 Python 缺少 PyInstaller 或 numpy: $venvPy" }

Set-Location $root
Write-Host "[1/3] PyInstaller 构建外壳 ..."
& $venvPy -m PyInstaller --noconfirm --clean `
    "$root\ShadowBuster.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败(exit=$LASTEXITCODE)" }

$bundleRoot = "$root\dist\ShadowBuster\_internal"
$numpyBundle = Join-Path $bundleRoot "numpy"
if (-not (Test-Path -LiteralPath $numpyBundle -PathType Container)) {
    throw "PyInstaller 产物缺少 numpy 目录：$numpyBundle"
}
if (-not (Get-ChildItem -LiteralPath $numpyBundle -Filter "*.pyd" -File -Recurse -ErrorAction SilentlyContinue)) {
    throw "PyInstaller 产物缺少 numpy 二进制扩展：$numpyBundle"
}
$warnFile = "$root\build\ShadowBuster\warn-ShadowBuster.txt"
if (Test-Path -LiteralPath $warnFile) {
    $numpyWarnings = Select-String -LiteralPath $warnFile -Pattern "^missing module named numpy\s+-" -CaseSensitive
    if ($numpyWarnings) { throw "PyInstaller 报告 numpy 缺失：$($numpyWarnings -join ' ')" }
}

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
