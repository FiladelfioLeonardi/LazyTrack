//go:build windows

package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"unsafe"
)

var (
	user32      = syscall.NewLazyDLL("user32.dll")
	messageBoxW = user32.NewProc("MessageBoxW")
)

func box(title, msg string, flags uintptr) int {
	t, _ := syscall.UTF16PtrFromString(title)
	m, _ := syscall.UTF16PtrFromString(msg)
	r, _, _ := messageBoxW.Call(0, uintptr(unsafe.Pointer(m)), uintptr(unsafe.Pointer(t)), flags|0x00010000 /* MB_SETFOREGROUND */)
	return int(r)
}

func ask(title, msg string) bool { return box(title, msg, 0x1|0x20 /* OK/Cancel, question */) == 1 }
func tell(title, msg string)     { box(title, msg, 0x40 /* info */) }
func yesNo(title, msg string) bool {
	return box(title, msg, 0x4|0x20|0x100 /* Yes/No, question, default No */) == 6
}

func appDir() string {
	base := os.Getenv("LOCALAPPDATA")
	if base == "" {
		home, _ := os.UserHomeDir()
		base = filepath.Join(home, "AppData", "Local")
	}
	return filepath.Join(base, "LazyTrack")
}

func pythonExe(dir string) string  { return filepath.Join(dir, "python", "python.exe") }
func venvPython(dir string) string { return filepath.Join(dir, ".venv", "Scripts", "python.exe") }
func requirementsFile() string     { return "requirements-windows.txt" }
func uninstallerName() string      { return "Uninstall LazyTrack.exe" }

func hideWindow(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: 0x08000000 /* CREATE_NO_WINDOW */}
}

func openURL(u string) { exec.Command("rundll32", "url.dll,FileProtocolHandler", u).Start() }

func powershell(dir, script string) error {
	cmd := exec.Command("powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script)
	cmd.Env = append(os.Environ(), "LT_DIR="+dir)
	hideWindow(cmd)
	out, err := cmd.CombinedOutput()
	if len(out) > 0 {
		logLine(string(out))
	}
	return err
}

// stops a LazyTrack that runs from this folder (so its files can be replaced)
func stopRunning(dir string) {
	powershell(dir, `Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($env:LT_DIR, 'OrdinalIgnoreCase') -and $_.ExecutablePath -notlike '*Uninstall LazyTrack.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`)
}

const shortcutsPS = `
$d = $env:LT_DIR
$sh = New-Object -ComObject WScript.Shell
function mk($path, $arg) {
  $s = $sh.CreateShortcut($path)
  $s.TargetPath = "$env:WINDIR\System32\wscript.exe"
  $s.Arguments = '"' + $d + '\windows\LazyTrack.vbs"' + $arg
  $s.WorkingDirectory = $d
  $s.IconLocation = "$d\assets\lazytrack.ico"
  $s.Description = "LazyTrack"
  $s.Save()
}
mk ([Environment]::GetFolderPath('Programs') + '\LazyTrack.lnk') ''
mk ([Environment]::GetFolderPath('Desktop') + '\LazyTrack.lnk') ''
mk ([Environment]::GetFolderPath('Startup') + '\LazyTrack.lnk') ' /background'
$k = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\LazyTrack'
New-Item -Path $k -Force | Out-Null
Set-ItemProperty -Path $k -Name DisplayName -Value 'LazyTrack'
Set-ItemProperty -Path $k -Name DisplayIcon -Value "$d\assets\lazytrack.ico"
Set-ItemProperty -Path $k -Name Publisher -Value 'LazyTrack'
Set-ItemProperty -Path $k -Name InstallLocation -Value $d
Set-ItemProperty -Path $k -Name UninstallString -Value ('"' + $d + '\Uninstall LazyTrack.exe" --uninstall')
Set-ItemProperty -Path $k -Name NoModify -Value 1 -Type DWord
Set-ItemProperty -Path $k -Name NoRepair -Value 1 -Type DWord
Set-ItemProperty -Path $k -Name EstimatedSize -Value 2500000 -Type DWord
`

func makeShortcuts(dir string) error { return powershell(dir, shortcutsPS) }

func launch(dir string) error {
	cmd := exec.Command(filepath.Join(os.Getenv("WINDIR"), "System32", "wscript.exe"), filepath.Join(dir, "windows", "LazyTrack.vbs"), "/background")
	cmd.Dir = dir
	return cmd.Start()
}

func uninstall() {
	dir := appDir()
	if !ask("Uninstall LazyTrack", "Remove LazyTrack from this computer?") {
		return
	}
	stopRunning(dir)
	powershell(dir, `
foreach ($f in @('Programs', 'Desktop', 'Startup')) { Remove-Item -Force -ErrorAction SilentlyContinue ([Environment]::GetFolderPath($f) + '\LazyTrack.lnk') }
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\LazyTrack'`)
	if yesNo("Uninstall LazyTrack", "Also delete your LazyTrack data (logs, stats, account on this computer)?\n\nChoose No to keep it, so a reinstall picks up where you left off.") {
		home, _ := os.UserHomeDir()
		os.RemoveAll(filepath.Join(home, ".lazytrack"))
	}
	// this program runs from the folder it removes: let cmd delete it a moment after we quit
	cmd := exec.Command("cmd", "/c", "ping 127.0.0.1 -n 3 >nul & rmdir /s /q \""+dir+"\"")
	hideWindow(cmd)
	cmd.Start()
	tell("LazyTrack", "LazyTrack was removed. Bye from Mochi 👋")
}
