"""Small wrappers around macOS APIs. Everything degrades gracefully on other OSes."""
from __future__ import annotations

import logging
import subprocess
import time
from typing import List, Optional

log = logging.getLogger(__name__)

# AppleScript that returns "URL\ntitle" of the active tab
_CHROMIUM = 'tell application "{app}" to tell active tab of front window to return (URL) & linefeed & (title)'
BROWSER_SCRIPTS = {
    "Google Chrome": _CHROMIUM.format(app="Google Chrome"),
    "Brave Browser": _CHROMIUM.format(app="Brave Browser"),
    "Microsoft Edge": _CHROMIUM.format(app="Microsoft Edge"),
    "Arc": _CHROMIUM.format(app="Arc"),
    "Vivaldi": _CHROMIUM.format(app="Vivaldi"),
    "Chromium": _CHROMIUM.format(app="Chromium"),
    "Safari": 'tell application "Safari" to tell front document to return (URL) & linefeed & (name)',
}
BROWSERS = set(BROWSER_SCRIPTS) | {"Firefox", "Opera", "Zen Browser"}


def osascript(script: str, timeout: float = 3.0) -> Optional[str]:
    try:
        out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.rstrip("\n")


def frontmost_app() -> str:
    try:
        from AppKit import NSWorkspace  # type: ignore
    except ImportError:
        return ""
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return str(app.localizedName()) if app else ""


def browser_tab(app: str):
    """Returns (url, title) of the active tab, or ("", "")."""
    script = BROWSER_SCRIPTS.get(app)
    if not script:
        return "", ""
    out = osascript(script)
    if not out:
        return "", ""
    url, _, title = out.partition("\n")
    return url.strip(), title.strip()


def idle_seconds() -> float:
    try:
        import Quartz  # type: ignore
        return float(Quartz.CGEventSourceSecondsSinceLastEventType(
            Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType))
    except Exception:
        return 0.0


# Apps that keep the screen awake without anyone watching: not a sign you're there.
_NOT_VIDEO = ("caffeinate", "amphetamine", "keepingyouawake", "theine", "lungo", "windowserver",
              "powerd", "useractivityd", "python", "lazytrack")
_video_cache = {"t": 0.0, "v": None}


def parse_video_assertions(text: str) -> bool:
    """True if some app asks macOS to keep the display on, which is what video players
    (YouTube in a browser, Netflix, VLC, IINA...) and video calls do while playing."""
    import re
    for line in text.splitlines():
        m = re.match(r"\s*pid \d+\((.+?)\):.*\b(PreventUserIdleDisplaySleep|NoDisplaySleepAssertion)\b", line)
        if m and not any(x in m.group(1).lower() for x in _NOT_VIDEO):
            return True
    return False


def video_playing() -> Optional[bool]:
    """True/False on macOS, None where it can't be checked. Cached for 10 s."""
    now = time.time()
    if now - _video_cache["t"] < 10:
        return _video_cache["v"]
    try:
        out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True, timeout=3)
        v: Optional[bool] = parse_video_assertions(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        v = None
    _video_cache.update(t=now, v=v)
    return v


def screen_locked() -> bool:
    try:
        import Quartz  # type: ignore
        d = Quartz.CGSessionCopyCurrentDictionary()
        return bool(d and d.get("CGSSessionScreenIsLocked", 0))
    except Exception:
        return False


def pump_runloop(seconds: float) -> None:
    """Sleep while letting Cocoa process events. Without this, the frontmost app
    reported by NSWorkspace never updates in a long-running script."""
    deadline = time.time() + seconds
    try:
        from Foundation import NSDate, NSRunLoop  # type: ignore
    except ImportError:
        time.sleep(seconds)
        return
    loop = NSRunLoop.currentRunLoop()
    while True:
        left = deadline - time.time()
        if left <= 0:
            return
        step = min(0.5, left)
        t0 = time.time()
        loop.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(step))
        # runUntilDate returns instantly when there are no sources; don't busy-spin
        if time.time() - t0 < step * 0.5:
            time.sleep(step)


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, message: str) -> None:
    if osascript(f'display notification "{_esc(message)}" with title "{_esc(title)}" sound name "Pop"') is None:
        log.info("[notification] %s: %s", title, message)


def ask(message: str, buttons: List[str], default: str, timeout: int = 120, cancel=None) -> Optional[str]:
    """Shows a dialog. Returns the pressed button, or None on timeout / no GUI / cancelled.
    macOS dialogs have at most 3 buttons; with more, it's a short list to pick from.
    cancel: a function; when it returns True (answered somewhere else) the dialog closes."""
    if len(buttons) <= 3:
        btns = ", ".join('"' + _esc(b) + '"' for b in buttons)
        script = (f'display dialog "{_esc(message)}" with title "LazyTrack" buttons {{{btns}}} '
                  f'default button "{_esc(default)}" giving up after {int(timeout)}')
    else:
        items = ", ".join('"' + _esc(b) + '"' for b in buttons)
        # plain osascript (like the 3-button dialog), so no new "control System Events" permission is needed
        script = (f'activate\n'
                  f'choose from list {{{items}}} with title "LazyTrack" '
                  f'with prompt "{_esc(message)}" default items {{"{_esc(default)}"}} '
                  f'OK button name "That\'s it" cancel button name "Not now"')
    try:
        proc = subprocess.Popen(["osascript", "-e", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None
    deadline = time.time() + timeout + 5
    while proc.poll() is None:
        if (cancel and cancel()) or time.time() > deadline:
            proc.kill()
            return None
        time.sleep(0.3)
    out = (proc.stdout.read() or "").strip()
    if not out or out == "false" or "gave up:true" in out:
        return None
    if out in buttons:                       # choose from list returns the item itself
        return out
    for part in out.split(", "):             # display dialog: "button returned:Cigarette, gave up:false"
        if part.startswith("button returned:"):
            return part[len("button returned:"):]
    return None
