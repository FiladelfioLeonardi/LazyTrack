"""The Windows version of macos.py: same functions, same answers, using the Windows API
through ctypes (built into Python, nothing to install).

What's different from the Mac:
  - Browsers don't tell other programs the address of the open tab. We read the window
    title instead ("Lofi beats - YouTube - Google Chrome") and guess the site from it,
    so the YouTube / Netflix / Reddit... categories still work.
  - "Is a video playing?" needs admin rights on Windows, so we don't ask. LazyTrack then
    falls back to the older rule: YouTube and streaming sites count as watching for a
    while even without touching the mouse.
  - Popups are small tkinter windows (tkinter comes with Python from python.org).
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from ctypes import wintypes
from typing import List, Optional

log = logging.getLogger(__name__)

NO_WINDOW = 0x08000000   # CREATE_NO_WINDOW: helper processes don't flash a black console

try:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
except (AttributeError, OSError):   # not on Windows (lets the file be imported for tests)
    user32 = kernel32 = None

if user32:   # exact argument/return types, so 64-bit handles aren't cut to 32 bits
    H = wintypes.HANDLE
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.OpenInputDesktop.restype = H
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.CloseDesktop.argtypes = [H]
    kernel32.OpenProcess.restype = H
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [H, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [H]
    kernel32.GetTickCount.restype = wintypes.DWORD

# program file name -> the app name LazyTrack uses (the same names as on the Mac where possible,
# so the default categories in config.py match on both)
APP_NAMES = {
    "chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "firefox.exe": "Firefox",
    "brave.exe": "Brave Browser", "opera.exe": "Opera", "opera_gx.exe": "Opera", "vivaldi.exe": "Vivaldi",
    "arc.exe": "Arc", "zen.exe": "Zen Browser",
    "code.exe": "Code", "cursor.exe": "Cursor", "devenv.exe": "Visual Studio", "clion64.exe": "CLion",
    "pycharm64.exe": "PyCharm", "idea64.exe": "IntelliJ IDEA", "windowsterminal.exe": "Terminal",
    "cmd.exe": "Terminal", "powershell.exe": "Terminal", "pwsh.exe": "Terminal", "wt.exe": "Terminal",
    "unrealeditor.exe": "UnrealEditor", "notepad++.exe": "Notepad++",
    "discord.exe": "Discord", "whatsapp.exe": "WhatsApp", "telegram.exe": "Telegram", "slack.exe": "Slack",
    "steam.exe": "Steam", "epicgameslauncher.exe": "Epic Games Launcher", "minecraftlauncher.exe": "Minecraft",
    "spotify.exe": "Spotify", "explorer.exe": "File Explorer", "vlc.exe": "VLC",
    "riotclientservices.exe": "Riot Client", "league of legends.exe": "League of Legends",
    "valorant-win64-shipping.exe": "VALORANT", "fortniteclient-win64-shipping.exe": "Fortnite", "cs2.exe": "Counter-Strike 2",
    "battle.net.exe": "Battle.net", "eadesktop.exe": "EA app", "xboxpcapp.exe": "Xbox", "robloxplayerbeta.exe": "Roblox",
}
BROWSERS = {"Google Chrome", "Microsoft Edge", "Firefox", "Brave Browser", "Opera", "Vivaldi", "Arc", "Zen Browser"}
BROWSER_SCRIPTS = BROWSERS   # activity.py asks `app in BROWSER_SCRIPTS` before reading the tab
_TITLE_SUFFIXES = [" - Google Chrome", " - Microsoft​ Edge", " - Microsoft Edge", " — Mozilla Firefox", " - Mozilla Firefox",
                   " - Brave", " - Opera", " - Vivaldi", " - Arc", " — Zen Browser", " - Zen Browser"]
# words in a tab title -> the site, so the url-based categories still work
_SITES = [("youtube", "youtube.com"), ("netflix", "netflix.com"), ("twitch", "twitch.tv"), ("disney+", "disneyplus.com"),
          ("prime video", "primevideo.com"), ("crunchyroll", "crunchyroll.com"), ("instagram", "instagram.com"),
          ("tiktok", "tiktok.com"), ("reddit", "reddit.com"), ("facebook", "facebook.com"), ("whatsapp", "web.whatsapp.com"),
          ("/ x", "x.com"), ("on x:", "x.com"), ("github", "github.com"), ("stack overflow", "stackoverflow.com"),
          ("intra", "intra.42.fr"), ("cppreference", "cppreference.com"), ("cplusplus", "cplusplus.com")]


def _window_title(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _exe_of(hwnd) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def app_name(exe: str, title: str) -> str:
    """chrome.exe -> Google Chrome. Store apps all run inside ApplicationFrameHost.exe, so
    for those the window title is the app's name."""
    low = exe.lower()
    if low == "applicationframehost.exe":
        return title.split(" - ")[-1].strip() or "Windows app"
    if low in APP_NAMES:
        return APP_NAMES[low]
    return exe[:-4] if low.endswith(".exe") else exe


def frontmost_app() -> str:
    if not user32:
        return ""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    exe = _exe_of(hwnd)
    if exe.lower() in ("lockapp.exe", "logonui.exe"):
        return ""
    return app_name(exe, _window_title(hwnd)) if exe else ""


def tab_from_title(title: str):
    """'Lofi beats - YouTube - Google Chrome' -> ('https://youtube.com/', 'Lofi beats - YouTube')"""
    for suf in _TITLE_SUFFIXES:
        if title.endswith(suf):
            title = title[: -len(suf)]
            break
    low = title.lower()
    for word, site in _SITES:
        if word in low:
            return f"https://{site}/", title
    return "", title


def browser_tab(app: str):
    """Returns (url, title) of the browser window in front. The url is guessed from the title."""
    if not user32 or app not in BROWSERS:
        return "", ""
    return tab_from_title(_window_title(user32.GetForegroundWindow()))


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


if user32:
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]


def idle_seconds() -> float:
    """Seconds since the last key press, click, scroll or mouse move (any app)."""
    if not user32:
        return 0.0
    info = _LASTINPUTINFO(cbSize=ctypes.sizeof(_LASTINPUTINFO))
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    now = kernel32.GetTickCount() & 0xFFFFFFFF
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0   # & handles the 49-day wraparound


def video_playing() -> Optional[bool]:
    return None   # needs admin on Windows (powercfg /requests); None = "can't tell", see activity.py


def screen_locked() -> bool:
    """When the screen is locked, Windows switches input to a secure desktop that normal
    programs can't open, and the lock screen (LockApp.exe) is the window in front."""
    if not user32:
        return False
    desk = user32.OpenInputDesktop(0, False, 0x0100)   # DESKTOP_SWITCHDESKTOP
    if not desk:
        return True
    user32.CloseDesktop(desk)
    hwnd = user32.GetForegroundWindow()
    return bool(hwnd) and _exe_of(hwnd).lower() == "lockapp.exe"


def pump_runloop(seconds: float) -> None:
    time.sleep(seconds)   # nothing to pump on Windows


def _pythonw() -> str:
    """pythonw.exe runs Python without a console window."""
    w = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return w if os.path.exists(w) else sys.executable


_TOAST = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$($env:LT_TITLE)</text><text>$($env:LT_MSG)</text></binding></visual></toast>")
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "&apos;").replace('"', "&quot;")


def notify(title: str, message: str) -> None:
    """A Windows notification (the ones that slide in at the bottom right)."""
    env = {**os.environ, "LT_TITLE": _xml(title), "LT_MSG": _xml(message)}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _TOAST],
                           env=env, capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise OSError(r.stderr.strip()[:200])
    except (OSError, subprocess.TimeoutExpired) as e:
        log.info("[notification] %s: %s (%s)", title, message, e)


_ASK = r"""
import json, sys, tkinter as tk
a = json.loads(sys.argv[1]); res = {"v": ""}
r = tk.Tk(); r.title("LazyTrack"); r.attributes("-topmost", True); r.resizable(False, False); r.configure(bg="#efeaff")
def pick(b): res.update(v=b); r.destroy()
tk.Label(r, text=a["msg"], font=("Segoe UI", 13), justify="left", bg="#efeaff", fg="#1d1b20", padx=26, pady=18).pack()
f = tk.Frame(r, bg="#efeaff", padx=18, pady=14); f.pack()
for i, b in enumerate(a["buttons"]):
    main = b == a["default"]
    btn = tk.Button(f, text=b, width=16, font=("Segoe UI", 11, "bold" if main else "normal"), command=lambda b=b: pick(b),
                    bg="#ffe45e" if main else "#ffffff", relief="solid", bd=2, cursor="hand2")
    btn.grid(row=i // 2, column=i % 2, padx=6, pady=5)
    if main:
        r.bind("<Return>", lambda e, b=b: pick(b))
r.bind("<Escape>", lambda e: r.destroy())
r.after(int(a["timeout"]) * 1000, r.destroy)
r.update_idletasks()
w, h = r.winfo_width(), r.winfo_height()
r.geometry(f"+{(r.winfo_screenwidth() - w) // 2}+{(r.winfo_screenheight() - h) // 3}")
r.lift(); r.focus_force(); r.mainloop()
print(res["v"])
"""


def ask(message: str, buttons: List[str], default: str, timeout: int = 120, cancel=None) -> Optional[str]:
    """Shows a small window with buttons. Returns the pressed button, or None on timeout or when
    cancel() turns True (answered on another device, e.g. the phone)."""
    args = json.dumps({"msg": message, "buttons": buttons, "default": default, "timeout": timeout})
    try:
        proc = subprocess.Popen([_pythonw(), "-c", _ASK, args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=NO_WINDOW, text=True, encoding="utf-8")
    except OSError:
        return None
    deadline = time.time() + timeout + 15
    while proc.poll() is None:
        if (cancel and cancel()) or time.time() > deadline:
            proc.kill()
            return None
        time.sleep(0.3)
    out = (proc.stdout.read() or "").strip()
    return out if out in buttons else None
