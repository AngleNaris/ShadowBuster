# ══════════════════════════════════════════════════════════════════
# ShadowBuster — 装配 AI 运行时目录（供 Inno 打包进安装包）
#
# 目标结构（安装后位于程序目录，默认 C:\Program Files\ShadowBuster）：
#   ShadowBuster/
#     ShadowBuster.exe          ← UI 外壳（build_shell.ps1 产出）
#     runtime/
#       env/python.exe           ← 便携推理解释器（python-build-standalone，可重定位）
#       Apollo/                   ← lew + bass_enhance + look2hear + ckpts
#       Soren_src/                ← core_decrypted + test_model + model + profiles
#       ffmpeg/bin/ffmpeg.exe
#
# 关键决策：
#   1) 【GPU】运行时用 CUDA 版 torch 2.7.1+cu128（与开发环境同版本）。
#      无 NVIDIA 卡的机器 torch.cuda.is_available()=False，后端自动回退 CPU，
#      一套运行时同时覆盖两种环境；CUDA 运行库随轮子内置，用户只需较新驱动。
#   2) 【可重定位】不用 venv！venv 的 pyvenv.cfg 硬编码构建机解释器绝对路径，
#      拷到陌生机器起不来。改用 python-build-standalone 整目录拷贝 +
#      pip --target 安装 site-packages，装到哪都能跑。
#   3) 【离线优先】demucs 的 htdemucs 权重预置进 runtime\torch_home\hub\checkpoints\，
#      后端已把 TORCH_HOME 指向这里，安装后全程不联网、用户无需下载任何模型。
#   4) 【依赖完备】按开发 venv 的版本精确复刻全部推理依赖（Soren 要
#      librosa/scipy/numba/statsmodels/pyloudnorm/cryptography，
#      look2hear 要 pytorch_lightning/omegaconf/rich/torch-complex 等）。
# ══════════════════════════════════════════════════════════════════
$ErrorActionPreference = "Stop"

$root     = Split-Path $PSScriptRoot -Parent
$stage    = "$root\packaging\stage\runtime"
$srcApollo= "D:\_3.AI\audio_upscale\Apollo"
$srcSoren = "D:\_3.AI\audio_upscale\Soren_src"
$srcPy    = "C:\Users\Administrator\AppData\Roaming\uv\python\cpython-3.12.11-windows-x86_64-none"
$venvPy   = "D:\_3.AI\audio_upscale\UniverSR\.venv\Scripts\python.exe"
$ffmpeg   = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if ($ffmpeg) {
    $ffmpegItem = Get-Item -LiteralPath $ffmpeg -Force
    if ($ffmpegItem.LinkType -and $ffmpegItem.Target) {
        $ffmpeg = [string]$ffmpegItem.Target[0]
    }
}

function Assert-NonEmptyFile([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label 不存在: $Path"
    }
    if ((Get-Item -LiteralPath $Path).Length -le 0) {
        throw "$Label 为空文件: $Path"
    }
}

function Assert-TreeHasNonEmptyFile([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Label 目录不存在: $Path"
    }
    $file = Get-ChildItem -LiteralPath $Path -File -Recurse -ErrorAction Stop |
        Where-Object { $_.Length -gt 0 } | Select-Object -First 1
    if (-not $file) { throw "$Label 中没有非空文件: $Path" }
}

function Assert-SameFile([string]$Source, [string]$Staged, [string]$Label) {
    Assert-NonEmptyFile $Source "$Label 源文件"
    Assert-NonEmptyFile $Staged "$Label stage 文件"
    $sourceHash = (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash
    $stagedHash = (Get-FileHash -LiteralPath $Staged -Algorithm SHA256).Hash
    if ($sourceHash -ne $stagedHash) {
        throw "$Label 源文件与 stage 哈希不一致: $Source -> $Staged"
    }
}

function Assert-SameTree([string]$SourceRoot, [string]$StagedRoot, [string]$Label) {
    Assert-TreeHasNonEmptyFile $SourceRoot "$Label 源目录"
    Assert-TreeHasNonEmptyFile $StagedRoot "$Label stage 目录"
    $sourceRootFull = (Resolve-Path -LiteralPath $SourceRoot).Path.TrimEnd('\')
    foreach ($sourceFile in Get-ChildItem -LiteralPath $sourceRootFull -File -Recurse -ErrorAction Stop) {
        $relative = $sourceFile.FullName.Substring($sourceRootFull.Length).TrimStart('\')
        Assert-SameFile $sourceFile.FullName (Join-Path $StagedRoot $relative) "$Label/$relative"
    }
}

# 从空 stage 重建，避免旧运行时文件混入发布包。
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }

Assert-NonEmptyFile $venvPy "依赖安装 Python"
Assert-NonEmptyFile "$srcApollo\lew_upscale.py" "Lew 入口"
Assert-NonEmptyFile "$srcApollo\bass_enhance.py" "BASS 入口"
Assert-TreeHasNonEmptyFile "$srcApollo\look2hear" "look2hear 源码"
Assert-TreeHasNonEmptyFile "$srcApollo\ckpts" "Apollo checkpoint"
Assert-NonEmptyFile "$srcSoren\core_decrypted.py" "Soren 入口"
Assert-NonEmptyFile "$srcSoren\test_model.py" "Soren 模型入口"
Assert-TreeHasNonEmptyFile "$srcSoren\model" "Soren 模型"
Assert-TreeHasNonEmptyFile "$srcSoren\profiles" "Soren profiles"
Assert-TreeHasNonEmptyFile "$srcSoren\secured_genres" "Soren secured genres"
if (-not $ffmpeg) { throw "未找到 ffmpeg，请先安装或加入 PATH" }
Assert-NonEmptyFile $ffmpeg "ffmpeg"

New-Item -ItemType Directory -Force -Path "$stage\Apollo", "$stage\Soren_src", "$stage\ffmpeg\bin" | Out-Null

Write-Host "[1/5] 拷贝 Apollo 工具链 ..."
Copy-Item "$srcApollo\lew_upscale.py", "$srcApollo\bass_enhance.py", "$srcApollo\low_punch.py" "$stage\Apollo\" -ErrorAction SilentlyContinue
Copy-Item "$srcApollo\look2hear" "$stage\Apollo\" -Recurse -ErrorAction SilentlyContinue
Copy-Item "$srcApollo\ckpts" "$stage\Apollo\" -Recurse -ErrorAction SilentlyContinue

Write-Host "[2/5] 拷贝 Soren 母带链 ..."
Copy-Item "$srcSoren\core_decrypted.py", "$srcSoren\test_model.py" "$stage\Soren_src\" -ErrorAction Stop
Copy-Item "$srcSoren\model", "$srcSoren\profiles", "$srcSoren\secured_genres" "$stage\Soren_src\" -Recurse -ErrorAction SilentlyContinue

Write-Host "[3/5] 拷贝 ffmpeg ..."
if ($ffmpeg) { Copy-Item $ffmpeg "$stage\ffmpeg\bin\" -ErrorAction SilentlyContinue }

Write-Host "[4/5] 装配便携 Python + CUDA torch（下载约 3GB，耐心等）..."
Copy-Item $srcPy "$stage\env" -Recurse
$site = "$stage\env\Lib\site-packages"
& $venvPy -m pip install --quiet --target $site `
    torch==2.7.1 torchaudio==2.7.1 --index-url https://download.pytorch.org/whl/cu128
if ($LASTEXITCODE -ne 0) { throw "torch cu128 安装失败(exit=$LASTEXITCODE)" }
# Use the staged interpreter for the remaining installs so pip sees the CUDA
# Torch already present in its own site-packages instead of resolving another
# incompatible Torch build from PyPI.
& "$stage\env\python.exe" -m pip install --quiet --break-system-packages `
    numpy==2.5.2 soundfile==0.14.0 scipy==1.18.0 librosa==1.0.0 `
    numba==0.67.0 llvmlite==0.49.0 statsmodels==0.14.6 pyloudnorm==0.2.0 `
    joblib==1.5.3 cryptography==50.0.0 setuptools==78.1.0 `
    demucs==4.1.0 einops==0.8.2 julius==0.2.8 lameenc==1.8.4 tqdm==4.70.0 `
    pytorch-lightning==2.6.5 lightning-utilities==0.15.3 `
    rich==15.0.0 huggingface_hub==0.36.2 torch-complex==0.4.4 soxr==1.1.0
if ($LASTEXITCODE -ne 0) { throw "推理依赖安装失败(exit=$LASTEXITCODE)" }

# omegaconf 2.0.6 的元数据是旧式写法（PyYAML>=5.1.*），pip>=24.1 拒装；
# 用临时 pip 24.0 安装后删掉，保证与开发环境完全同版本。
$pip240 = "$stage\_pip240"
& "$stage\env\python.exe" -m pip install --quiet --break-system-packages --target $pip240 "pip==24.0"
$env:PYTHONPATH = $pip240
& "$stage\env\python.exe" -m pip install --quiet --break-system-packages omegaconf==2.0.6
$rc = $LASTEXITCODE
Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
Remove-Item $pip240 -Recurse -Force -ErrorAction SilentlyContinue
if ($rc -ne 0) { throw "omegaconf 安装失败(exit=$rc)" }

# [4a] 清理 *.dist-info\licenses 深层许可目录：
# torch 的第三方许可树路径极深，安装到 Program Files 会超 Windows MAX_PATH(260)，
# 解压会报"找不到路径"并整包回滚。这些许可只是元数据、运行不需要，剪掉。
Write-Host "  [4a] 清理深层 licenses（防 MAX_PATH）..."
Get-ChildItem $site -Directory -Filter "*dist-info" -Recurse -ErrorAction SilentlyContinue |
  ForEach-Object {
      $lic = Join-Path $_.FullName "licenses"
      if (Test-Path $lic) { Remove-Item $lic -Recurse -Force }
  }

# [5/5] 预置 htdemucs 权重。Demucs 4.1 优先使用 Hugging Face Hub，
# 因此 TORCH_HOME 与 HF_HOME 都必须定向到 runtime，安装后才能真正离线。
Write-Host "[5/5] 预置 demucs 权重（离线）..."
$torchHome = "$stage\torch_home"
$hfHome = "$stage\hf_home"
New-Item -ItemType Directory -Force -Path $torchHome, $hfHome | Out-Null
$seed = "$stage\_seed"
$env:TORCH_HOME = $torchHome
$env:HF_HOME = $hfHome
& "$stage\env\python.exe" -c "import soundfile as sf, numpy as np; sf.write(r'$seed.wav', np.zeros((44100,2)), 44100)"
& "$stage\env\python.exe" -m demucs --two-stems bass -n htdemucs -o "$seed\_out" "$seed.wav"
$demucsRc = $LASTEXITCODE
if ($demucsRc -ne 0) { throw "Demucs 权重预置失败(exit=$demucsRc)" }

# Hugging Face snapshots normally contain symbolic links into blobs. Inno and
# target machines cannot be assumed to preserve them, so materialize links as
# ordinary files before offline validation and hashing.
Get-ChildItem -LiteralPath $hfHome -File -Recurse -Force -ErrorAction Stop |
    Where-Object { $_.LinkType -and $_.Target } |
    ForEach-Object {
        $linkPath = $_.FullName
        $target = [string]$_.Target[0]
        if (-not [System.IO.Path]::IsPathRooted($target)) {
            $target = [System.IO.Path]::GetFullPath((Join-Path $_.DirectoryName $target))
        }
        Assert-NonEmptyFile $target "HF 模型 blob"
        Remove-Item -LiteralPath $linkPath -Force
        Copy-Item -LiteralPath $target -Destination $linkPath -Force
        Assert-NonEmptyFile $linkPath "实体化 HF 模型"
    }

$modelFiles = @(
    Get-ChildItem -LiteralPath $torchHome, $hfHome -File -Recurse -ErrorAction Stop |
        Where-Object { $_.Length -gt 1MB }
)
if ($modelFiles.Count -eq 0) { throw "Demucs 模型缓存缺失或为空: $hfHome" }

# 禁止联网后再次加载模型；这里通过才证明安装包中的缓存可独立工作。
$env:HF_HUB_OFFLINE = "1"
& "$stage\env\python.exe" -c "from demucs.pretrained import get_model; m=get_model('htdemucs'); print('offline htdemucs models', len(m.models))"
$offlineModelRc = $LASTEXITCODE
Remove-Item Env:\HF_HUB_OFFLINE -ErrorAction SilentlyContinue
if ($offlineModelRc -ne 0) { throw "Demucs 离线模型加载失败(exit=$offlineModelRc)" }

# 发布前静态自检：关键依赖可导入，ffmpeg / 模型存在，关键源与 stage 完全一致。
Write-Host "  [5a] 验证运行时依赖与关键文件 ..."
$requiredImports = @(
    "torch", "torchaudio", "demucs", "numpy", "soundfile", "scipy", "librosa",
    "numba", "statsmodels", "pyloudnorm", "joblib", "cryptography", "look2hear"
)
$importNames = $requiredImports -join ","
$importProbe = "import importlib,torch,torchaudio; [importlib.import_module(n) for n in '$importNames'.split(',')]; assert torch.__version__ == '2.7.1+cu128', torch.__version__; assert torchaudio.__version__ == '2.7.1+cu128', torchaudio.__version__; print('runtime imports OK', torch.__version__)"
$previousPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = "$stage\Apollo;$stage\Soren_src"
& "$stage\env\python.exe" -c $importProbe
$importRc = $LASTEXITCODE
if ($null -eq $previousPythonPath) {
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
} else {
    $env:PYTHONPATH = $previousPythonPath
}
if ($importRc -ne 0) { throw "关键依赖 import 验证失败(exit=$importRc)" }

Assert-SameFile "$srcApollo\lew_upscale.py" "$stage\Apollo\lew_upscale.py" "Lew 入口"
Assert-SameFile "$srcApollo\bass_enhance.py" "$stage\Apollo\bass_enhance.py" "BASS 入口"
Assert-SameTree "$srcApollo\look2hear" "$stage\Apollo\look2hear" "look2hear 源码"
Assert-SameTree "$srcApollo\ckpts" "$stage\Apollo\ckpts" "Apollo checkpoint"
Assert-SameFile "$srcSoren\core_decrypted.py" "$stage\Soren_src\core_decrypted.py" "Soren 入口"
Assert-SameFile "$srcSoren\test_model.py" "$stage\Soren_src\test_model.py" "Soren 模型入口"
Assert-SameTree "$srcSoren\model" "$stage\Soren_src\model" "Soren 模型"
Assert-SameTree "$srcSoren\profiles" "$stage\Soren_src\profiles" "Soren profiles"
Assert-SameTree "$srcSoren\secured_genres" "$stage\Soren_src\secured_genres" "Soren secured genres"
Assert-NonEmptyFile "$stage\ffmpeg\bin\ffmpeg.exe" "stage ffmpeg"
Assert-SameFile $ffmpeg "$stage\ffmpeg\bin\ffmpeg.exe" "ffmpeg"

# 关键 manifest 使用相对路径 + SHA-256，供安装包内容审计和复验。
$manifestPath = "$stage\critical-manifest.sha256"
$manifestRoots = @(
    "$stage\Apollo\lew_upscale.py", "$stage\Apollo\bass_enhance.py",
    "$stage\Apollo\look2hear", "$stage\Apollo\ckpts",
    "$stage\Soren_src\core_decrypted.py", "$stage\Soren_src\test_model.py",
    "$stage\Soren_src\model",
    "$stage\Soren_src\profiles", "$stage\Soren_src\secured_genres",
    "$stage\ffmpeg\bin\ffmpeg.exe", "$stage\torch_home", "$stage\hf_home"
)
$manifestFiles = foreach ($item in $manifestRoots) {
    if (Test-Path -LiteralPath $item -PathType Container) {
        Get-ChildItem -LiteralPath $item -File -Recurse -ErrorAction Stop
    } else {
        Get-Item -LiteralPath $item -ErrorAction Stop
    }
}
$stageFull = (Resolve-Path -LiteralPath $stage).Path.TrimEnd('\')
$manifestLines = $manifestFiles | Sort-Object FullName -Unique | ForEach-Object {
    if ($_.Length -le 0) { throw "manifest 关键文件为空: $($_.FullName)" }
    $relative = $_.FullName.Substring($stageFull.Length).TrimStart('\').Replace('\', '/')
    "{0}  {1}" -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $relative
}
if (@($manifestLines).Count -eq 0) { throw "关键 manifest 为空" }
Set-Content -LiteralPath $manifestPath -Value $manifestLines -Encoding utf8
Assert-NonEmptyFile $manifestPath "关键 manifest"

Remove-Item "$stage\_seed", "$stage\_seed.wav" -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "  已预置权重: $hfHome"
Write-Host "  关键 manifest: $manifestPath"

Write-Host "runtime 装配完成: $stage"
Write-Host "下一步: iscc installer.iss 生成安装包。"
