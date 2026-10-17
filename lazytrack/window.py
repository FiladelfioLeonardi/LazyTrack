"""LazyTrack in its own app window instead of a browser tab.

The tracker (`lazytrack run`) and the window are separate processes: closing the window only
closes the window, LazyTrack keeps tracking in the background. Opening it again (Start menu,
desktop, Applications, or LazyTrack starting with the computer) brings the window back.

The window shows the same page as http://localhost:8042, in the system's own web view:
Microsoft Edge WebView2 on Windows (built into Windows 10/11), WebKit on Mac. If that isn't
available for some reason, it falls back to the browser like before.

Only one window: a second `lazytrack window` just tells the first one (over a local port) to
come to the front, then quits.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import config

log = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent.parent
FRONT_PORT = 8043          # "come to the front" messages between window processes
WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"


def _url(cfg: dict) -> str:
    return f"http://localhost:{cfg['dashboard']['port']}"


def _bring_existing_to_front() -> bool:
    """True if a LazyTrack window is open and came to the front. It has to answer "ok": a window
    process that's stuck (its window already closed) doesn't, and then a new window opens."""
    try:
        with socket.create_connection(("127.0.0.1", FRONT_PORT), timeout=1) as s:
            s.sendall(b"front\n")
            s.settimeout(3)
            return s.recv(8).startswith(b"ok")
    except OSError:
        return False


def _has_webview() -> bool:
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def open_app(cfg: dict) -> None:
    """Show LazyTrack: the app window (in its own process), or the browser if there's no window support."""
    if _bring_existing_to_front():
        return
    if not _has_webview():
        webbrowser.open(_url(cfg))
        return
    py = Path(sys.executable)
    if WIN and (py.parent / "pythonw.exe").exists():
        py = py.parent / "pythonw.exe"          # no console window
    kw = {"cwd": str(APP_DIR)}
    if WIN:
        kw["creationflags"] = 0x00000008 | 0x08000000   # DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen([str(py), "-m", "lazytrack", "window"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    except OSError:
        log.exception("couldn't open the window, using the browser")
        webbrowser.open(_url(cfg))


def _wait_for_server(url: str, seconds: int = 90) -> bool:
    for _ in range(seconds * 2):
        try:
            urllib.request.urlopen(url + "/api/auth/state", timeout=2).close()
            return True
        except Exception:
            time.sleep(0.5)
    return False


class _Api:
    """What the page can ask the window for (window.pywebview.api.*)."""

    _window = None   # private: pywebview doesn't hand underscore names to the page

    def front(self) -> None:
        """Bring the window to the front (the smoking question just popped up in it)."""
        w = self._window
        if w is None:
            return
        try:
            w.restore()
            w.show()
            w.on_top = True
            w.on_top = False
        except Exception:
            pass

    def open_external(self, url: str) -> None:
        if str(url).startswith(("http://", "https://", "mailto:")):
            webbrowser.open(url)


def _icon(ext: str) -> str:
    return str(APP_DIR / "assets" / f"lazytrack.{ext}")


def _mac_branding() -> None:
    """Shows "LazyTrack" with Mochi's icon in the Dock and menu bar, instead of "Python"."""
    try:
        from AppKit import NSApplication, NSImage
        from Foundation import NSBundle
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        info["CFBundleName"] = "LazyTrack"
        img = NSImage.alloc().initWithContentsOfFile_(_icon("icns"))
        if img:
            NSApplication.sharedApplication().setApplicationIconImage_(img)
    except Exception:
        log.debug("mac branding failed", exc_info=True)


def _win_branding(window) -> None:
    """Mochi's icon on the window and the taskbar, grouped as LazyTrack (not Python)."""
    try:
        from System import Action
        from System.Drawing import Icon
        form = window.native
        form.Invoke(Action(lambda: setattr(form, "Icon", Icon(_icon("ico")))))
    except Exception:
        log.debug("windows icon failed", exc_info=True)


MAC_STUB = '#!/bin/bash\nexec "$HOME/Library/Application Support/LazyTrack/mac/launcher.sh" "$@"\n'


def fix_mac_app() -> None:
    """Macs installed before the app window existed have an old copy of the launcher inside
    ~/Applications/LazyTrack.app (it opened the browser). Swap it for the small stub that runs
    the always-up-to-date mac/launcher.sh, so the app opens the window."""
    if not MAC:
        return
    app = Path.home() / "Applications" / "LazyTrack.app"
    exe = app / "Contents" / "MacOS" / "LazyTrack"
    try:
        if exe.exists() and "mac/launcher.sh" not in exe.read_text():
            exe.write_text(MAC_STUB)
            exe.chmod(0o755)
            subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(app)], capture_output=True, timeout=60)
            log.info("updated the LazyTrack app launcher")
    except Exception:
        log.debug("couldn't update the mac app launcher", exc_info=True)


def cmd_window(cfg: dict, args=None) -> None:
    """`lazytrack window`: the app window. Waits for LazyTrack to be running, then shows it."""
    if _bring_existing_to_front():
        return
    url = _url(cfg)
    try:
        import webview
    except Exception:
        webbrowser.open(url)
        return
    # one window only: listen for "come to the front" from later launches
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", FRONT_PORT))
        srv.listen(4)
    except OSError:
        srv = None
    if WIN:
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LazyTrack.App")
        except Exception:
            pass
    if MAC:
        _mac_branding()
    if not _wait_for_server(url):
        webbrowser.open(url)
        return

    api = _Api()
    window = webview.create_window("LazyTrack", url, width=1200, height=880, min_size=(380, 560),
                                   background_color="#efeaff", text_select=True, js_api=api)
    api._window = window

    def front() -> bool:
        try:
            window.restore()
            window.show()
            window.on_top = True
            window.on_top = False
            return True
        except Exception:
            return False

    def listen():
        while srv:
            try:
                c, _ = srv.accept()
                ok = front()
                try:
                    c.sendall(b"ok\n" if ok else b"no\n")
                finally:
                    c.close()
            except OSError:
                return

    def closed():
        # closing the window ends this process for sure (on a Mac it could otherwise linger without a
        # window, keep the "come to the front" port, and make the next launch do nothing)
        try:
            if srv:
                srv.close()
        except OSError:
            pass
        threading.Timer(1.0, lambda: os._exit(0)).start()

    try:
        window.events.closed += closed
    except Exception:
        log.debug("no closed event in this pywebview", exc_info=True)

    if srv:
        threading.Thread(target=listen, daemon=True).start()
    if WIN:
        window.events.shown += lambda: _win_branding(window)
    try:
        # private_mode off + a storage folder: stay logged in and keep the theme between launches
        kw = {"private_mode": False, "storage_path": str(config.HOME / "window")}
        import inspect
        if "icon" in inspect.signature(webview.start).parameters:   # newer pywebview: icon on Windows and Mac
            kw["icon"] = _icon("ico" if WIN else "png")
        webview.start(**kw)
    except Exception:
        log.exception("the app window couldn't start, using the browser")
        webbrowser.open(url)
    finally:
        if srv:
            srv.close()
