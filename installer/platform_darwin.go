//go:build darwin

package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

func osa(script string) error { return exec.Command("osascript", "-e", "activate", "-e", script).Run() }
func q(s string) string       { return strings.ReplaceAll(strings.ReplaceAll(s, `\`, `\\`), `"`, `\"`) }

func ask(title, msg string) bool {
	return osa(fmt.Sprintf(`display dialog "%s" with title "%s" buttons {"Cancel", "Continue"} default button "Continue" with icon note`, q(msg), q(title))) == nil
}
func tell(title, msg string) {
	osa(fmt.Sprintf(`display dialog "%s" with title "%s" buttons {"OK"} default button "OK" with icon note`, q(msg), q(title)))
}
func yesNo(title, msg string) bool {
	out, _ := exec.Command("osascript", "-e", "activate", "-e", fmt.Sprintf(`button returned of (display dialog "%s" with title "%s" buttons {"Keep it", "Delete it"} default button "Keep it")`, q(msg), q(title))).Output()
	return strings.TrimSpace(string(out)) == "Delete it"
}

func home() string      { h, _ := os.UserHomeDir(); return h }
func appDir() string    { return filepath.Join(home(), "Library", "Application Support", "LazyTrack") }
func appBundle() string { return filepath.Join(home(), "Applications", "LazyTrack.app") }
func agentPlist() string {
	return filepath.Join(home(), "Library", "LaunchAgents", "com.lazytrack.app.plist")
}

func pythonExe(dir string) string  { return filepath.Join(dir, "python", "bin", "python3") }
func venvPython(dir string) string { return filepath.Join(dir, ".venv", "bin", "python") }
func requirementsFile() string     { return "requirements.txt" }
func uninstallerName() string      { return "uninstall-lazytrack" }
func hideWindow(cmd *exec.Cmd)     {}
func openURL(u string)             { exec.Command("open", u).Start() }
func stopRunning(dir string) { // only the installed copy (run.sh starts it by its full path), never another one
	exec.Command("pkill", "-f", filepath.Join(dir, ".venv", "bin", "python")+" -m lazytrack").Run()
}

const infoPlist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>LazyTrack</string>
  <key>CFBundleDisplayName</key><string>LazyTrack</string>
  <key>CFBundleIdentifier</key><string>com.lazytrack.app</string>
  <key>CFBundleExecutable</key><string>LazyTrack</string>
  <key>CFBundleIconFile</key><string>lazytrack</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSCameraUsageDescription</key><string>LazyTrack uses the webcam to notice when you smoke. Pictures never leave your Mac.</string>
  <key>NSAppleEventsUsageDescription</key><string>LazyTrack reads which website is open in your browser to count your screen time.</string>
</dict></plist>
`

// ~/Applications/LazyTrack.app (opens LazyTrack) + start at login
func makeShortcuts(dir string) error {
	app := appBundle()
	os.RemoveAll(app)
	for _, d := range []string{"Contents/MacOS", "Contents/Resources"} {
		if err := os.MkdirAll(filepath.Join(app, d), 0o755); err != nil {
			return err
		}
	}
	os.WriteFile(filepath.Join(app, "Contents", "Info.plist"), []byte(infoPlist), 0o644)
	if err := copyFile(filepath.Join(dir, "mac", "launcher.sh"), filepath.Join(app, "Contents", "MacOS", "LazyTrack")); err != nil {
		return err
	}
	copyFile(filepath.Join(dir, "assets", "lazytrack.icns"), filepath.Join(app, "Contents", "Resources", "lazytrack.icns"))
	os.Chmod(filepath.Join(dir, "mac", "run.sh"), 0o755)
	os.Chmod(filepath.Join(dir, "mac", "uninstall.command"), 0o755)
	copyFile(filepath.Join(dir, "mac", "uninstall.command"), filepath.Join(home(), "Applications", "Uninstall LazyTrack.command"))
	os.MkdirAll(filepath.Dir(agentPlist()), 0o755)
	agent := fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.lazytrack.app</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-g</string><string>-a</string><string>%s</string><string>--args</string><string>--background</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
`, app)
	os.WriteFile(agentPlist(), []byte(agent), 0o644)
	exec.Command("codesign", "--force", "--deep", "-s", "-", app).Run() // a local signature, so macOS remembers the camera permission
	exec.Command("touch", app).Run()                                    // so Finder picks up the icon
	return nil
}

func launch(dir string) error {
	return exec.Command("open", "-g", "-a", appBundle(), "--args", "--background").Run()
}

func uninstall() {
	if !ask("Uninstall LazyTrack", "Remove LazyTrack from this Mac?") {
		return
	}
	stopRunning(appDir())
	exec.Command("launchctl", "unload", agentPlist()).Run()
	os.Remove(agentPlist())
	os.RemoveAll(appBundle())
	os.Remove(filepath.Join(home(), "Applications", "Uninstall LazyTrack.command"))
	if yesNo("Uninstall LazyTrack", "Also delete your LazyTrack data (logs, stats, account on this Mac)?") {
		os.RemoveAll(filepath.Join(home(), ".lazytrack"))
	}
	os.RemoveAll(appDir())
	tell("LazyTrack", "LazyTrack was removed. Bye from Mochi!")
}
