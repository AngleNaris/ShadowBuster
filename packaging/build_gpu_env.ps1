# ══════════════════════════════════════════════════════════════════
# ShadowBuster — GPU 环境包制作（zip + 分卷 + 清单）
#
# 前置：runtime_sync.ps1 -Flavor gpu 已产出 packaging\stage\runtime_gpu\env
# 产物：packaging\out\gpu-env\<version>\ 下的 zip / part* / json
# 下一步：按脚本末尾打印的命令把分卷与清单上传到 v<version> Release。
# ══════════════════════════════════════════════════════════════════
$ErrorActionPreference = "Stop"

$root = Split-Path $PSScriptRoot -Parent
$workspace = Split-Path $root -Parent
$venvCandidates = @(
    $env:SB_PYTHON,
    "$root\.venv\Scripts\python.exe",
    "$workspace\UniverSR\.venv\Scripts\python.exe",
    (Get-Command python -ErrorAction SilentlyContinue).Source
) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) }
$venvPy = $venvCandidates | Select-Object -First 1
if (-not $venvPy) { throw "找不到制作 GPU 环境包的 Python" }

# 版本单一来源：studio_backend.APP_VERSION
$backend = Get-Content -LiteralPath "$root\studio_backend.py" -Raw
if ($backend -match 'APP_VERSION\s*=\s*"(\d+\.\d+\.\d+)"') {
    $version = $Matches[1]
} else {
    throw "无法从 studio_backend.py 读取 APP_VERSION"
}

$src = "$root\packaging\stage\runtime_gpu\env"
if (-not (Test-Path -LiteralPath "$src\python.exe" -PathType Leaf)) {
    throw "缺少 GPU 环境源: $src（先运行 runtime_sync.ps1 -Flavor gpu）"
}
if (-not (Test-Path -LiteralPath "$src\Lib\site-packages\numpy\__init__.py" -PathType Leaf)) {
    throw "GPU 环境缺少 numpy: $src"
}
if (-not (Get-ChildItem -LiteralPath "$src\Lib\site-packages\numpy" -Filter "*.pyd" -File -Recurse -ErrorAction SilentlyContinue)) {
    throw "GPU 环境缺少 numpy 原生扩展: $src"
}
$probe = "import importlib,torch,torchaudio; [importlib.import_module(n) for n in 'torch,torchaudio,demucs,numpy,soundfile,scipy,librosa,numba,statsmodels,pyloudnorm,joblib,cryptography'.split(',')]; assert torch.__version__ == '2.7.1+cu128', torch.__version__; assert torchaudio.__version__ == '2.7.1+cu128', torchaudio.__version__; print('GPU runtime preflight OK', torch.__version__)"
$oldPath = $env:PYTHONPATH
$env:PYTHONPATH = "$root\packaging\stage\runtime_gpu\Apollo;$root\packaging\stage\runtime_gpu\Soren_src"
try {
    & "$src\python.exe" -c $probe
    if ($LASTEXITCODE -ne 0) { throw "GPU runtime 依赖验证失败(exit=$LASTEXITCODE)" }
} finally {
    if ($null -eq $oldPath) { Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue }
    else { $env:PYTHONPATH = $oldPath }
}

$out = "$root\packaging\out\gpu-env"
New-Item -ItemType Directory -Force -Path $out | Out-Null

Write-Host "GPU 环境包 v$version（源: $src，输出: $out）..."
& $venvPy "$root\packaging\make_gpu_env_package.py" $src $out $version
if ($LASTEXITCODE -ne 0) { throw "GPU 环境包制作失败(exit=$LASTEXITCODE)" }

# 兜底断言：每个分卷必须 < 2GiB（GitHub 硬上限）
$parts = Get-ChildItem -LiteralPath $out -Filter "gpu-env-$version.part*"
foreach ($p in $parts) {
    if ($p.Length -ge 2GB) { throw "分卷超限: $($p.Name) ($($p.Length) bytes)" }
}
Write-Host "GPU 环境包完成: $out"
Write-Host "下一步: 上传分卷与清单到 v$version Release（命令见上方输出）。"