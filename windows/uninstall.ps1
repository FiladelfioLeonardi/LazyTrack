# Removes LazyTrack from this Windows computer. Used by Settings > Apps > LazyTrack > Uninstall and by
# LazyTrack's own Settings > App > Uninstall. A plain script (no .exe), so antivirus can't block it, and it
# needs no admin rights: everything LazyTrack installs lives in your own user folders.
#
# If it ever doesn't start, by hand: quit LazyTrack (restart the PC), delete %LOCALAPPDATA%\LazyTrack, the
# LazyTrack shortcuts (Start menu, desktop, shell:startup) and, if you want, your data in %USERPROFILE%\.lazytrack.
param([switch]$Yes)
$ErrorActionPreference = 'SilentlyContinue'
Add-Type -AssemblyName System.Windows.Forms
$dir = Split-Path -Parent $PSScriptRoot          # ...\AppData\Local\LazyTrack
$title = 'Uninstall Mochi'
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true                           # the questions show up in front, not behind other windows
function Ask($msg, $buttons = 'OKCancel', $icon = 'Question', $def = 'Button1') {
  [System.Windows.Forms.MessageBox]::Show($owner, $msg, $title, $buttons, $icon, $def)
}

if (-not $Yes -and (Ask 'Remove Mochi from this computer?') -ne 'OK') { exit }
$delData = (Ask ("Also delete your Mochi data (logs, stats, account on this computer)?`n`n" +
  'Choose No to keep it, so a reinstall picks up where you left off.') 'YesNo' 'Question' 'Button2') -eq 'Yes'

# 1. stop LazyTrack: everything running from its folder, plus the hidden scripts that start and restart it
$me = $PID
Get-CimInstance Win32_Process | Where-Object {
  $_.ProcessId -ne $me -and (
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase)) -or
    ($_.CommandLine -and ($_.CommandLine.IndexOf($dir, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
                          $_.CommandLine -match 'run-hidden\.bat|LazyTrack\.vbs')))
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Sleep -Seconds 2                           # let Windows let go of the files

# 2. the shortcuts and the entry in Settings > Apps
$sh = New-Object -ComObject WScript.Shell
foreach ($f in 'Programs', 'Desktop', 'Startup') {
  foreach ($n in 'Mochi.lnk', 'LazyTrack.lnk') {
    Remove-Item -LiteralPath (Join-Path $sh.SpecialFolders.Item($f) $n) -Force -ErrorAction SilentlyContinue
  }
}
Remove-Item -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\LazyTrack' -Recurse -Force

# 3. the files (read-only ones too), a few tries in case something is still closing
Set-Location $env:TEMP
$targets = @($dir)
if ($delData) { $targets += (Join-Path $env:USERPROFILE '.lazytrack') }
$left = @()
foreach ($t in $targets) {
  for ($i = 0; $i -lt 6 -and (Test-Path -LiteralPath $t); $i++) {
    Get-ChildItem -LiteralPath $t -Recurse -Force | ForEach-Object { $_.Attributes = 'Normal' }
    Remove-Item -LiteralPath $t -Recurse -Force
    if (Test-Path -LiteralPath $t) { Start-Sleep -Seconds 1 }
  }
  if (Test-Path -LiteralPath $t) { $left += $t }
}

if ($left.Count) {
  Ask ("Mochi is removed, but Windows kept a few files because something still has them open:`n`n" +
    ($left -join "`n") + "`n`nRestart the computer, then delete that folder.") 'OK' 'Warning' | Out-Null
  Start-Process explorer.exe (Split-Path -Parent $left[0])
} else {
  Ask 'Mochi was removed. Bye!' 'OK' 'Information' | Out-Null
}
