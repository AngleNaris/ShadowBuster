param(
    [Parameter(Mandatory=$true)][string]$AppDir,
    [string]$AssetsDir = "",
    [ValidateSet('offscreen','windows')][string]$Platform = 'offscreen',
    [Parameter(Mandatory=$true)][string]$ReportPath
)
$ErrorActionPreference = "Stop"
$report = [IO.Path]::GetFullPath($ReportPath)
if (Test-Path -LiteralPath $report) { throw "GUI 检查报告已存在，需使用新路径：$report" }
New-Item -ItemType Directory -Path (Split-Path $report -Parent) -Force | Out-Null
$vars = @('QT_QPA_PLATFORM','QTWEBENGINE_CHROMIUM_FLAGS','SB_PROCESSING_CACHE_DIR','SB_ASSETS')
$previous = @{}
foreach ($name in $vars) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    $env:QT_QPA_PLATFORM = $Platform
    $env:QTWEBENGINE_CHROMIUM_FLAGS = '--no-sandbox --disable-gpu --disable-dev-shm-usage'
    $env:SB_PROCESSING_CACHE_DIR = $report + '.data'
    if ($AssetsDir) { $env:SB_ASSETS = [IO.Path]::GetFullPath($AssetsDir) }
    else { Remove-Item Env:\SB_ASSETS -ErrorAction SilentlyContinue }
    $exe = Join-Path $AppDir 'ShadowBuster.exe'
    $proc = Start-Process -FilePath $exe -ArgumentList '--gui-smoke-test',('"'+$report+'"') -WorkingDirectory $AppDir -WindowStyle Hidden -PassThru -RedirectStandardOutput ($report+'.stdout.log') -RedirectStandardError ($report+'.stderr.log')
    if (-not $proc.WaitForExit(45000)) {
        $proc.Kill()
        throw '冻结 GUI 检查超时（Qt 导入失败也会被此检查捕获）'
    }
    if ($proc.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $report)) { throw "冻结 GUI 检查失败(exit=$($proc.ExitCode))，日志：$report.stderr.log" }
    $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if ($result.status -ne 'pass' -or $result.bridge_version -ne $result.version) { throw "GUI/桥接检查失败：$report" }
    $result | ConvertTo-Json -Depth 4
} finally {
    foreach ($name in $vars) { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
}
