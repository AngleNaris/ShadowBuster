param(
    [string]$Version = "1.4.0",
    [string]$InstallDir = (Join-Path $PSScriptRoot "test_install")
)

$installer = Join-Path $PSScriptRoot "out\ShadowBuster-Setup-$Version.exe"
if (-not (Test-Path $installer)) {
    throw "Installer not found: $installer"
}

$dirArg = '/DIR="' + $InstallDir + '"'
$p = Start-Process -FilePath $installer -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART',$dirArg -PassThru -Wait
Write-Output ('EXIT=' + $p.ExitCode)
exit $p.ExitCode
