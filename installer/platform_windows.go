//go:build windows

package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
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

// the progress window: Microsoft Edge (on every Windows 10/11) in app mode, so it's a plain window
// without tabs or an address bar. Its own profile folder, so it's a separate Edge we can close.
func showProgress(u string) func() {
	var edge string
	for _, base := range []string{os.Getenv("ProgramFiles(x86)"), os.Getenv("ProgramFiles"), os.Getenv("LOCALAPPDATA")} {
		if p := filepath.Join(base, "Microsoft", "Edge", "Application", "msedge.exe"); base != "" && fileExists(p) {
			edge = p
			break
		}
	}
	if edge == "" {
		openURL(u)
		return func() {}
	}
	prof := filepath.Join(os.TempDir(), "lazytrack-installer-window")
	cmd := exec.Command(edge, "--app="+u, "--window-size=580,760", "--user-data-dir="+prof,
		"--no-first-run", "--no-default-browser-check", "--disable-sync", "--disable-extensions")
	if cmd.Start() != nil {
		openURL(u)
		return func() {}
	}
	return func() {
		if cmd.Process != nil {
			cmd.Process.Kill()
		}
		runTimed(appDir(), 10*time.Second, "taskkill", "/F", "/T", "/FI", "WINDOWTITLE eq Installing LazyTrack*")
	}
}

// opens LazyTrack's own window (LazyTrack.vbs starts LazyTrack first if it isn't running)
func openApp(dir string) {
	cmd := exec.Command(filepath.Join(os.Getenv("WINDIR"), "System32", "wscript.exe"), filepath.Join(dir, "windows", "LazyTrack.vbs"))
	cmd.Dir = dir
	cmd.Start()
}

// stops a LazyTrack that runs from this folder (so its files can be replaced). Uses Windows' own
// process list directly (no PowerShell, which can hang on some PCs).
var (
	kernel32                  = syscall.NewLazyDLL("kernel32.dll")
	createToolhelp32Snapshot  = kernel32.NewProc("CreateToolhelp32Snapshot")
	process32FirstW           = kernel32.NewProc("Process32FirstW")
	process32NextW            = kernel32.NewProc("Process32NextW")
	queryFullProcessImageName = kernel32.NewProc("QueryFullProcessImageNameW")
)

type processEntry32 struct {
	Size            uint32
	Usage           uint32
	ProcessID       uint32
	DefaultHeapID   uintptr
	ModuleID        uint32
	Threads         uint32
	ParentProcessID uint32
	PriClassBase    int32
	Flags           uint32
	ExeFile         [260]uint16
}

func stopRunning(dir string) {
	snap, _, _ := createToolhelp32Snapshot.Call(0x2 /* TH32CS_SNAPPROCESS */, 0)
	if snap == 0 || snap == ^uintptr(0) {
		return
	}
	defer syscall.CloseHandle(syscall.Handle(snap))
	prefix := strings.ToLower(filepath.Clean(dir) + string(os.PathSeparator))
	self := os.Getpid()
	var e processEntry32
	e.Size = uint32(unsafe.Sizeof(e))
	for ok, _, _ := process32FirstW.Call(snap, uintptr(unsafe.Pointer(&e))); ok != 0; ok, _, _ = process32NextW.Call(snap, uintptr(unsafe.Pointer(&e))) {
		if int(e.ProcessID) == self || e.ProcessID == 0 {
			continue
		}
		h, err := syscall.OpenProcess(0x1000|0x0001 /* QUERY_LIMITED_INFORMATION | TERMINATE */, false, e.ProcessID)
		if err != nil {
			continue
		}
		buf := make([]uint16, 1024)
		n := uint32(len(buf))
		r, _, _ := queryFullProcessImageName.Call(uintptr(h), 0, uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&n)))
		if r != 0 {
			path := strings.ToLower(syscall.UTF16ToString(buf[:n]))
			if strings.HasPrefix(path, prefix) && !strings.HasSuffix(path, "uninstall lazytrack.exe") {
				syscall.TerminateProcess(h, 1)
			}
		}
		syscall.CloseHandle(h)
	}
	time.Sleep(time.Second) // let Windows release the files
}

// the Start menu, desktop and startup shortcuts: made by Windows Script Host (fast, no PowerShell)
const shortcutsVBS = `Set sh = CreateObject("WScript.Shell")
d = sh.ExpandEnvironmentStrings("%LT_DIR%")
Sub mk(path, arg)
  Set s = sh.CreateShortcut(path)
  s.TargetPath = sh.ExpandEnvironmentStrings("%WINDIR%") & "\System32\wscript.exe"
  s.Arguments = Chr(34) & d & "\windows\LazyTrack.vbs" & Chr(34) & arg
  s.WorkingDirectory = d
  s.IconLocation = d & "\assets\lazytrack.ico"
  s.Description = "LazyTrack"
  s.Save
End Sub
mk sh.SpecialFolders("Programs") & "\LazyTrack.lnk", ""
mk sh.SpecialFolders("Desktop") & "\LazyTrack.lnk", ""
mk sh.SpecialFolders("Startup") & "\LazyTrack.lnk", " /background"
`

// removes the shortcuts again (uninstall)
const unshortcutsVBS = `Set sh = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
For Each f In Array("Programs", "Desktop", "Startup")
  p = sh.SpecialFolders(f) & "\LazyTrack.lnk"
  If fs.FileExists(p) Then fs.DeleteFile p
Next
`

const uninstallKey = `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\LazyTrack`

// runs a program hidden, with a time limit (nothing in the installer may hang forever)
func runTimed(dir string, limit time.Duration, name string, args ...string) error {
	ctx, cancel := context.WithTimeout(context.Background(), limit)
	defer cancel()
	cmd := exec.CommandContext(ctx, name, args...)
	cmd.Env = append(os.Environ(), "LT_DIR="+dir)
	hideWindow(cmd)
	out, err := cmd.CombinedOutput()
	if len(out) > 0 {
		logLine(string(out))
	}
	return err
}

func runVBS(dir, script string) error {
	f := filepath.Join(os.TempDir(), "lazytrack-setup.vbs")
	if err := os.WriteFile(f, []byte(strings.ReplaceAll(script, "\n", "\r\n")), 0o644); err != nil {
		return err
	}
	defer os.Remove(f)
	return runTimed(dir, 60*time.Second, filepath.Join(os.Getenv("WINDIR"), "System32", "cscript.exe"), "//nologo", "//B", f)
}

func makeShortcuts(dir string) error {
	if err := runVBS(dir, shortcutsVBS); err != nil {
		return err
	}
	// the uninstall entry in Settings > Apps (best effort: the app works without it)
	for _, v := range [][]string{
		{"DisplayName", "REG_SZ", "LazyTrack"}, {"DisplayIcon", "REG_SZ", dir + `\assets\lazytrack.ico`},
		{"Publisher", "REG_SZ", "LazyTrack"}, {"InstallLocation", "REG_SZ", dir},
		{"UninstallString", "REG_SZ", uninstallCmd(dir)},
		{"NoModify", "REG_DWORD", "1"}, {"NoRepair", "REG_DWORD", "1"}, {"EstimatedSize", "REG_DWORD", "2500000"},
	} {
		runTimed(dir, 20*time.Second, "reg", "add", uninstallKey, "/v", v[0], "/t", v[1], "/d", v[2], "/f")
	}
	return nil
}

func launch(dir string) error {
	cmd := exec.Command(filepath.Join(os.Getenv("WINDIR"), "System32", "wscript.exe"), filepath.Join(dir, "windows", "LazyTrack.vbs"), "/background")
	cmd.Dir = dir
	return cmd.Start()
}

// Settings > Apps runs windows\uninstall.ps1: a script, so antivirus can't block it (it does that to unsigned
// .exe files, and Windows then says you don't have the rights), and it needs no admin rights.
func psExe() string {
	return filepath.Join(os.Getenv("WINDIR"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
}

func uninstallCmd(dir string) string {
	return `"` + psExe() + `" -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "` + filepath.Join(dir, "windows", "uninstall.ps1") + `"`
}

func uninstall() {
	dir := appDir()
	if ps := filepath.Join(dir, "windows", "uninstall.ps1"); fileExists(ps) {   // the script does it all (and asks)
		cmd := exec.Command(psExe(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", ps)
		cmd.Dir = os.TempDir()
		hideWindow(cmd)
		if cmd.Start() == nil {
			return
		}
	}
	if !ask("Uninstall LazyTrack", "Remove LazyTrack from this computer?") {
		return
	}
	stopRunning(dir)
	runVBS(dir, unshortcutsVBS)
	runTimed(dir, 20*time.Second, "reg", "delete", uninstallKey, "/f")
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
