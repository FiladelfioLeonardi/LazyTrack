"""The app is called Mochi now (it was LazyTrack). Runs at every start and renames what people see on
installs made before the rename: the Mac app in ~/Applications and the Windows shortcuts.

Folders people never look at keep their old names (~/.lazytrack, Application Support/LazyTrack,
%LOCALAPPDATA%\\LazyTrack), so data, updates and backups keep working exactly as before.
Every step is safe to run again and gives up quietly if something is in the way.
"""
from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)
NAME = "Mochi"


def mac_app() -> Path:
    """The app in ~/Applications: Mochi.app, or LazyTrack.app on an install that wasn't renamed yet."""
    apps = Path.home() / "Applications"
    new = apps / "Mochi.app"
    return new if new.exists() or not (apps / "LazyTrack.app").exists() else apps / "LazyTrack.app"


def _mac() -> None:
    apps = Path.home() / "Applications"
    old, new = apps / "LazyTrack.app", apps / "Mochi.app"
    if old.exists() and not new.exists():
        os.rename(old, new)
        log.info("rebrand: LazyTrack.app is Mochi.app now")
    if not new.exists():
        return
    info = new / "Contents" / "Info.plist"
    try:
        with open(info, "rb") as f:
            pl = plistlib.load(f)
        if pl.get("CFBundleName") != NAME or pl.get("CFBundleDisplayName") != NAME:
            pl["CFBundleName"] = pl["CFBundleDisplayName"] = NAME
            for key in ("NSCameraUsageDescription", "NSAppleEventsUsageDescription"):
                if isinstance(pl.get(key), str):
                    pl[key] = pl[key].replace("LazyTrack", NAME)
            with open(info, "wb") as f:
                plistlib.dump(pl, f)
            subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(new)], capture_output=True, timeout=60)
            subprocess.run(["touch", str(new)], capture_output=True, timeout=10)
    except (OSError, plistlib.InvalidFileException, subprocess.SubprocessError):
        log.debug("rebrand: couldn't update Info.plist", exc_info=True)
    # start at login opens the app by its path
    agent = Path.home() / "Library" / "LaunchAgents" / "com.lazytrack.app.plist"
    try:
        if agent.exists() and str(old) in agent.read_text():
            agent.write_text(agent.read_text().replace(str(old), str(new)))
    except OSError:
        pass
    un_old, un_new = apps / "Uninstall LazyTrack.command", apps / "Uninstall Mochi.command"
    try:
        if un_old.exists() and not un_new.exists():
            os.rename(un_old, un_new)
    except OSError:
        pass


def _win_folders() -> list:
    appdata = Path(os.environ.get("APPDATA", ""))
    programs = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    out = [programs, programs / "Startup", home / "Desktop"]
    if os.environ.get("OneDrive"):
        out.append(Path(os.environ["OneDrive"]) / "Desktop")
    return out


def _win() -> None:
    for folder in _win_folders():
        old, new = folder / "LazyTrack.lnk", folder / "Mochi.lnk"
        try:
            if old.exists():
                if new.exists():
                    old.unlink()
                else:
                    os.rename(old, new)
                log.info("rebrand: %s is Mochi.lnk now", old)
        except OSError:
            log.debug("rebrand: couldn't rename %s", old, exc_info=True)


def migrate() -> None:
    try:
        if sys.platform == "darwin":
            _mac()
        elif sys.platform == "win32":
            _win()
    except Exception:
        log.debug("rebrand failed", exc_info=True)
