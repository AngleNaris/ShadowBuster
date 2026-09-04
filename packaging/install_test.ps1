param(
    [string]$Version = "1.5.0",
    [string]$InstallDir = (Join-Path $PSScriptRoot "test_install")
)

$installer = Join-Path $PSScriptRoot "out\ShadowBuster-Setup-$Version.exe"
if (-not (Test-Path $installer)) {
    throw "Installer not found: $installer"
}

$dirArg = '/DIR="' + $InstallDir + '"'
$installFull = [IO.Path]::GetFullPath($InstallDir).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
if ($env:LOCALAPPDATA) {
    $userDataRoot = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA "ShadowBuster")).TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
    $userDataPrefix = $userDataRoot + [IO.Path]::DirectorySeparatorChar
    if ($installFull.Equals($userDataRoot, [StringComparison]::OrdinalIgnoreCase) -or
        $installFull.StartsWith($userDataPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "InstallDir 不得指向用户 GPU 数据目录: $installFull"
    }
}
if (Test-Path -LiteralPath $InstallDir) {
    Remove-Item -LiteralPath $InstallDir -Recurse -Force
}
$p = Start-Process -FilePath $installer -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',$dirArg -PassThru -Wait
Write-Output ('EXIT=' + $p.ExitCode)
if ($p.ExitCode -ne 0) { exit $p.ExitCode }

$runtimePython = Join-Path $InstallDir "runtime\env\python.exe"
if (-not (Test-Path -LiteralPath $runtimePython -PathType Leaf)) {
    throw "安装后缺少 runtime Python: $runtimePython"
}

$probe = @"
import importlib
import numpy
required = ('torch', 'torchaudio', 'demucs', 'numpy', 'soundfile', 'scipy', 'librosa', 'numba', 'statsmodels', 'pyloudnorm', 'joblib', 'cryptography', 'look2hear')
for name in required:
    importlib.import_module(name)
assert numpy.__version__ == '2.5.2', numpy.__version__
print('installed runtime imports OK', numpy.__version__)
"@
$previousPythonPath = $env:PYTHONPATH
$previousNoUserSite = $env:PYTHONNOUSERSITE
$modulePaths = @(
    (Join-Path $InstallDir "runtime\Apollo"),
    (Join-Path $InstallDir "runtime\Soren_src")
) | Where-Object { Test-Path -LiteralPath $_ -PathType Container }
$env:PYTHONPATH = ($modulePaths -join [IO.Path]::PathSeparator)
$env:PYTHONNOUSERSITE = "1"
try {
    $probeOutput = & $runtimePython -c $probe 2>&1
    $probeOutput | Write-Output
    if ($LASTEXITCODE -ne 0) {
        throw "安装后 runtime 依赖验证失败(exit=$LASTEXITCODE): $($probeOutput -join ' ')"
    }
} finally {
    if ($null -eq $previousPythonPath) {
        Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $previousPythonPath
    }
    if ($null -eq $previousNoUserSite) {
        Remove-Item Env:\PYTHONNOUSERSITE -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONNOUSERSITE = $previousNoUserSite
    }
}
