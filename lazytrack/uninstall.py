"""Uninstalling LazyTrack on Windows: windows/uninstall.ps1, a plain script (no .exe, so antivirus can't block
it, and no admin rights needed). LazyTrack points Settings > Apps > LazyTrack > Uninstall at it every time it
starts (older installs pointed at "Uninstall LazyTrack.exe"), and Settings > App in LazyTrack can run it too."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent.parent
SCRIPT = APP_DIR / "windows" / "uninstall.ps1"
KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\LazyTrack"


def supported() -> bool:
    return sys.platform == "win32" and SCRIPT.exists() and (APP_DIR / ".venv").exists()


def _args() -> list:
    ps = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return [str(ps), "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(SCRIPT)]


def fix_apps_entry() -> None:
    """Settings > Apps runs the script (only where the installer made that entry)."""
    if not supported():
        return
    import winreg
    cmd = " ".join(f'"{a}"' if " " in a or a.endswith(".exe") or a.endswith(".ps1") else a for a in _args())
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
            try:
                if winreg.QueryValueEx(k, "UninstallString")[0] == cmd:
                    return
            except FileNotFoundError:
                pass
            winreg.SetValueEx(k, "UninstallString", 0, winreg.REG_SZ, cmd)
            log.info("uninstall: Settings > Apps now uses windows/uninstall.ps1")
    except FileNotFoundError:
        pass   # not installed with the installer (a dev checkout)
    except OSError as e:
        log.info("uninstall: couldn't update the Apps entry: %s", e)


def start() -> None:
    """Runs the uninstaller (it asks first, then stops LazyTrack, this program included)."""
    if not supported():
        raise RuntimeError("Uninstalling from here only works on Windows. On a Mac, use the uninstall script in the LazyTrack folder.")
    subprocess.Popen(_args(), cwd=os.environ.get("TEMP") or None,
                     creationflags=0x08000000 | 0x00000200)   # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
