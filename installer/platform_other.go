//go:build !windows && !darwin

package main

// Linux: only used to test the installer from here (no shortcuts, no dialogs).

import (
	"os"
	"os/exec"
	"path/filepath"
)

func ask(title, msg string) bool { return true }
func tell(title, msg string)     {}
func appDir() string {
	if d := os.Getenv("LT_TEST_DIR"); d != "" {
		return d
	}
	h, _ := os.UserHomeDir()
	return filepath.Join(h, ".local", "share", "LazyTrack")
}
func pythonExe(dir string) string  { return filepath.Join(dir, "python", "bin", "python3") }
func venvPython(dir string) string { return filepath.Join(dir, ".venv", "bin", "python") }
func requirementsFile() string     { return firstNonEmpty(os.Getenv("LT_TEST_REQS"), "requirements.txt") }
func uninstallerName() string      { return "uninstall-lazytrack" }
func hideWindow(cmd *exec.Cmd)     {}
func openURL(u string) {
	os.WriteFile(filepath.Join(os.TempDir(), "lt-installer-url"), []byte(u), 0o644)
}
func makeShortcuts(dir string) error { return nil }
func showProgress(u string) func()   { openURL(u); return func() {} }
func openApp(dir string) {
	os.WriteFile(filepath.Join(os.TempDir(), "lt-installer-opened"), []byte(dir), 0o644)
}
func stopRunning(dir string) {}
func uninstall()             { os.RemoveAll(appDir()) }
func launch(dir string) error {
	cmd := exec.Command(venvPython(dir), "-m", "lazytrack", "run")
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "LAZYTRACK_RESTARTED=1")
	return cmd.Start()
}
func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}
