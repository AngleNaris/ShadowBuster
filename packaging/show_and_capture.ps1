param(
    [Parameter(Mandatory = $true)]
    [int]$ProcId,
    [Parameter(Mandatory = $true)]
    [string]$OutPath
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$sig = @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
[DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);
public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
'@
$null = Add-Type -MemberDefinition $sig -Name Win32Util -Namespace ShadowBuster
$win32 = [ShadowBuster.Win32Util]

$proc = Get-Process -Id $ProcId
$handle = $proc.MainWindowHandle
if ($handle -eq [IntPtr]::Zero) { throw "process $ProcId has no main window" }

$win32::ShowWindow($handle, 9) | Out-Null   # SW_RESTORE
$win32::SetForegroundWindow($handle) | Out-Null
Start-Sleep -Milliseconds 900

$rect = New-Object ShadowBuster.Win32Util+RECT
$win32::GetWindowRect($handle, [ref]$rect) | Out-Null
Write-Output ("window rect: {0},{1} {2}x{3}" -f $rect.Left, $rect.Top, ($rect.Right - $rect.Left), ($rect.Bottom - $rect.Top))

$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bmp.Size)
$bmp.Save($OutPath)
$g.Dispose()
$bmp.Dispose()
Write-Output ("saved {0}" -f $OutPath)
