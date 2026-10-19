"""Start LazyTrack when you log in to your computer (on by default, a switch in Settings > App).

Mac: a login item made with launchd (~/Library/LaunchAgents/com.lazytrack.app.plist opens LazyTrack in the
background), and the tracker's own launchd entry starts at login too. Turning it off removes the first and
tells the second not to run at login (it still runs whenever you open LazyTrack).
Windows: a LazyTrack shortcut in the Startup folder (what the installer makes).
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import config

log = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent.parent
FLAG = config.HOME / "no-autostart"          # the Mac launcher looks at this too


def _mac_agent() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.lazytrack.app.plist"


def _mac_tracker() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / "com.lazytrack.tracker.plist"


def _mac_app() -> Path:
    from .rebrand import mac_app
    return mac_app()


def _win_link() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Mochi.lnk"


def _win_old_link() -> Path:   # from before the app was called Mochi
    return _win_link().with_name("LazyTrack.lnk")


def supported() -> bool:
    """Only for an installed LazyTrack (the app on a Mac, the installer on Windows), not a developer copy."""
    if sys.platform == "darwin":
        return _mac_app().exists()
    if sys.platform == "win32":
        return (APP_DIR / "windows" / "LazyTrack.vbs").exists()
    return False


def enabled() -> bool:
    if sys.platform == "darwin":
        return _mac_agent().exists() and not FLAG.exists()
    if sys.platform == "win32":
        return _win_link().exists() or _win_old_link().exists()
    return False


AGENT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.lazytrack.app</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-g</string><string>-a</string><string>{app}</string><string>--args</string><string>--background</string></array>
  <key>RunAtLoad</key><true/>
</dict></plist>
"""

SHORTCUT_VBS = """Set sh = CreateObject("WScript.Shell")
Set s = sh.CreateShortcut(sh.SpecialFolders("Startup") & "\\Mochi.lnk")
s.TargetPath = sh.ExpandEnvironmentStrings("%WINDIR%") & "\\System32\\wscript.exe"
s.Arguments = Chr(34) & "{dir}\\windows\\LazyTrack.vbs" & Chr(34) & " /background"
s.WorkingDirectory = "{dir}"
s.IconLocation = "{dir}\\assets\\lazytrack.ico"
s.Description = "Mochi"
s.Save
"""


def set_enabled(on: bool) -> bool:
    """Turns starting at login on or off. Returns the new state."""
    if not supported():
        return False
    if sys.platform == "darwin":
        tracker = _mac_tracker()
        if on:
            FLAG.unlink(missing_ok=True)
            _mac_agent().parent.mkdir(parents=True, exist_ok=True)
            _mac_agent().write_text(AGENT.format(app=_mac_app()))
            if tracker.exists():
                tracker.write_text(tracker.read_text().replace("<key>RunAtLoad</key><false/>", "<key>RunAtLoad</key><true/>"))
        else:
            FLAG.parent.mkdir(parents=True, exist_ok=True)
            FLAG.write_text("Mochi doesn't start at login (Settings > App).\n")
            subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/com.lazytrack.app"], capture_output=True)
            _mac_agent().unlink(missing_ok=True)
            if tracker.exists():   # stays loaded for now (LazyTrack keeps running), just doesn't start at the next login
                tracker.write_text(tracker.read_text().replace("<key>RunAtLoad</key><true/>", "<key>RunAtLoad</key><false/>"))
    elif sys.platform == "win32":
        if on:
            with tempfile.NamedTemporaryFile("w", suffix=".vbs", delete=False, encoding="utf-8") as f:
                f.write(SHORTCUT_VBS.format(dir=str(APP_DIR)))
                path = f.name
            try:
                subprocess.run(["wscript.exe", "//B", path], capture_output=True, timeout=20, creationflags=0x08000000)
            finally:
                os.unlink(path)
        else:
            _win_link().unlink(missing_ok=True)
            _win_old_link().unlink(missing_ok=True)
    log.info("start at login: %s", "on" if enabled() else "off")
    return enabled()
