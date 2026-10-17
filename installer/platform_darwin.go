//go:build darwin

package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
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

// the progress window: a small WebKit window made with JavaScript for Automation (built into macOS)
const progressJXA = `ObjC.import("Cocoa"); ObjC.import("WebKit");
function run(argv) {
  var app = $.NSApplication.sharedApplication;
  app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
  var win = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer($.NSMakeRect(0, 0, 560, 700), 15, $.NSBackingStoreBuffered, false);
  win.title = "Installing LazyTrack";
  win.releasedWhenClosed = false;
  var wv = $.WKWebView.alloc.initWithFrameConfiguration(win.contentView.bounds, $.WKWebViewConfiguration.alloc.init);
  wv.autoresizingMask = 18;
  win.contentView.addSubview(wv);
  wv.loadRequest($.NSURLRequest.requestWithURL($.NSURL.URLWithString(argv[0])));
  win.center;
  win.makeKeyAndOrderFront(null);
  app.activateIgnoringOtherApps(true);
  app.run;
}`

func showProgress(u string) func() {
	f := filepath.Join(os.TempDir(), "lazytrack-progress.js")
	if os.WriteFile(f, []byte(progressJXA), 0o644) != nil {
		openURL(u)
		return func() {}
	}
	cmd := exec.Command("osascript", "-l", "JavaScript", f, u)
	if cmd.Start() != nil {
		openURL(u)
		return func() {}
	}
	return func() {
		if cmd.Process != nil {
			cmd.Process.Kill()
		}
	}
}

// opens LazyTrack's own window (the app starts it if LazyTrack isn't running yet)
func openApp(dir string) { exec.Command("open", "-a", appBundle()).Run() }
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
	// a tiny stub that runs mac/launcher.sh from the app folder, so updates to the launcher reach this app too
	stub := "#!/bin/bash\nexec /bin/bash \"$HOME/Library/Application Support/LazyTrack/mac/launcher.sh\" \"$@\"\n"
	if err := os.WriteFile(filepath.Join(app, "Contents", "MacOS", "LazyTrack"), []byte(stub), 0o755); err != nil {
		return err
	}
	os.Chmod(filepath.Join(dir, "mac", "launcher.sh"), 0o755)
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
	exec.Command("launchctl", "bootout", fmt.Sprintf("gui/%d/com.lazytrack.tracker", os.Getuid())).Run()
	os.Remove(filepath.Join(home(), "Library", "LaunchAgents", "com.lazytrack.tracker.plist"))
	os.RemoveAll(appBundle())
	os.Remove(filepath.Join(home(), "Applications", "Uninstall LazyTrack.command"))
	if yesNo("Uninstall LazyTrack", "Also delete your LazyTrack data (logs, stats, account on this Mac)?\n\nIt goes to the Trash, so you can still put it back until you empty the Trash.") {
		// to the Trash, never gone for good in one click
		os.Rename(filepath.Join(home(), ".lazytrack"), filepath.Join(home(), ".Trash", "LazyTrack data "+time.Now().Format("2006-01-02 15-04")))
	}
	os.RemoveAll(appDir())
	tell("LazyTrack", "LazyTrack was removed. Bye from Mochi!")
}
