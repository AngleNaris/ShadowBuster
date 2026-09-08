$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='ffmpeg.exe'" |
  Where-Object { $_.ExecutablePath -like "*ShadowBuster*" }
if ($procs) {
  Write-Output "LEFTOVER:"
  $procs | ForEach-Object { Write-Output ("  pid=$($_.ProcessId) $($_.ExecutablePath)") }
} else {
  Write-Output "NO LEFTOVER RUNTIME PROCESSES"
}
