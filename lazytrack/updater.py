"""Updates LazyTrack by itself from GitHub.

When LazyTrack runs, this checks the GitHub repo a minute after starting and then every hour.
A check is one tiny request (about 1 KB, the same thing `git` asks GitHub), so it costs nothing.
Everyone follows the "stable" branch: versions marked ready. "Test versions" in Settings follows
"main", which gets every change right away. If there's a newer version, it downloads it (a zip of the repo, no git needed),
copies the new code over the old, installs new Python packages if the list changed, and
restarts. Your data is never touched: it lives in ~/.lazytrack, not in the code folder.

The installed version is the commit id (sha) of the code, kept in the file VERSION next to
the code. A folder that is a git checkout (has .git) is never overwritten: that's a developer
copy, update it with git instead.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from . import db

log = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent.parent      # the folder with install.sh / install.bat
VERSION_FILE = APP_DIR / "VERSION"
KEEP = {".venv", ".git", "__pycache__", "VERSION"}      # never overwritten or removed
KEEP_IF_THERE = {"ios/project.yml"}   # filled in with your own Apple settings by ios/setup.sh
_lock = threading.Lock()


def _get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "LazyTrack-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def installed() -> str:
    try:
        return VERSION_FILE.read_text().strip()
    except OSError:
        return ""


def head(repo: str, branch: str) -> str:
    """The newest commit id on a branch, asked the way git does it: tiny, and not counted against GitHub's
    API limit (60 an hour per network, which a school full of LazyTracks would run out of)."""
    raw = _get(f"https://github.com/{repo}.git/info/refs?service=git-upload-pack")
    want = f"refs/heads/{branch}".encode()
    for line in raw.split(b"\n"):
        part = line.split(b"\x00")[0]
        if part.endswith(b" " + want) and len(part) >= 44:
            return part[-len(want) - 41:-len(want) - 1].decode()
    raise ValueError(f"branch {branch} not found")


def latest(repo: str, branch: str) -> dict:
    """{"sha": ..., "message": first line of the commit message, "date": ...}"""
    try:
        sha = head(repo, branch)
        if sha == installed():
            return {"sha": sha, "message": "", "date": ""}   # nothing new: no need to ask for the message
    except Exception:
        pass
    j = json.loads(_get(f"https://api.github.com/repos/{repo}/commits/{branch}"))
    c = j.get("commit", {})
    return {"sha": j["sha"], "message": (c.get("message") or "").split("\n")[0][:140],
            "date": (c.get("committer") or {}).get("date", "")}


def status(cfg: dict) -> dict:
    u = cfg.get("update", {})
    return {"repo": u.get("repo", ""), "auto": bool(u.get("auto", True)), "version": installed()[:7],
            "dev": (APP_DIR / ".git").exists(), "last_check": float(db.kv_get("update_checked") or 0),
            "updated_at": float(db.kv_get("update_done_at") or 0), "message": db.kv_get("update_message") or "",
            "available": pending()}


def pending() -> dict | None:
    """A newer version a check found but that isn't installed yet ({"sha", "message"}), or None."""
    try:
        p = json.loads(db.kv_get("update_available") or "null")
    except ValueError:
        return None
    return p if p and p.get("sha") and p["sha"] != installed() else None


def _req_file() -> str:
    return "requirements-windows.txt" if sys.platform == "win32" else "requirements.txt"


def _hash(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def apply(repo: str, branch: str, info: dict) -> None:
    """Download the repo at that commit and copy it over the code folder."""
    try:   # your data first, always
        from . import backup
        backup.make("before updating to " + info["sha"][:7])
    except Exception:
        log.exception("backup before update failed")
    data = _get(f"https://codeload.github.com/{repo}/zip/{info['sha']}", timeout=120)
    req_before = _hash(APP_DIR / _req_file())
    with tempfile.TemporaryDirectory() as tmp:
        zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
        roots = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if len(roots) != 1 or not (roots[0] / "lazytrack" / "__init__.py").exists():
            raise RuntimeError("the download doesn't look like LazyTrack")
        src = roots[0]
        for item in src.rglob("*"):
            rel = item.relative_to(src)
            if rel.parts[0] in KEEP or "__pycache__" in rel.parts:
                continue
            dest = APP_DIR / rel
            if rel.as_posix() in KEEP_IF_THERE and dest.exists():
                continue
            if item.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmpdest = dest.with_name(dest.name + ".new")
                shutil.copy2(item, tmpdest)
                os.replace(tmpdest, dest)     # swap in one step, so a file is never half-written
        # the download doesn't keep "can be run" on files: put it back on every script (the Mac app's
        # launcher.sh too, or the app can't start at all)
        if sys.platform != "win32":
            for p in [APP_DIR / "lt", *APP_DIR.glob("*.sh"), *APP_DIR.glob("*.command"), *APP_DIR.glob("mac/*.sh"), *APP_DIR.glob("mac/*.command")]:
                if p.exists():
                    p.chmod(0o755)
    if _hash(APP_DIR / _req_file()) != req_before:
        log.info("updater: new packages needed, installing...")
        py = Path(sys.executable)
        kw = {}
        if os.name == "nt":   # Mochi runs as pythonw (no console): pip needs python.exe, hidden
            py, kw = py.with_name("python.exe"), {"creationflags": 0x08000000, "capture_output": True}
        subprocess.run([str(py), "-m", "pip", "install", "-q", "-r", str(APP_DIR / _req_file())], cwd=APP_DIR, timeout=1800, **kw)
    VERSION_FILE.write_text(info["sha"] + "\n")
    db.kv_set("update_available", "")
    db.kv_set("update_done_at", time.time())
    db.kv_set("update_message", info.get("message", ""))


def check(cfg: dict, install: bool = True) -> dict:
    """Looks for a new version; installs it (and restarts) if install=True. Returns what happened."""
    from . import config as _config
    try:
        cfg = _config.load()          # fresh: "Test versions" may have just been switched
    except Exception:
        pass
    u = cfg.get("update", {})
    repo, branch = u.get("repo", ""), u.get("branch", "stable")
    if not repo:
        return {"state": "off", "text": "Updates aren't set up yet."}
    if (APP_DIR / ".git").exists():
        return {"state": "dev", "text": "This is a git copy: update it with git pull."}
    with _lock:
        db.kv_set("update_checked", time.time())
        try:
            info = latest(repo, branch)
        except Exception as e:
            return {"state": "error", "text": f"Couldn't reach GitHub ({e.__class__.__name__})."}
        if info["sha"] == installed():
            db.kv_set("update_available", "")
            return {"state": "current", "text": "You have the newest version."}
        if not install:
            # remembered, so the app keeps showing "Update now" (and no second check) until it's installed
            db.kv_set("update_available", json.dumps({"sha": info["sha"], "message": info["message"]}))
            return {"state": "available", "text": info["message"] or "A new version is ready."}
        log.info("updater: updating %s -> %s (%s)", installed()[:7] or "?", info["sha"][:7], info["message"])
        apply(repo, branch, info)
    restart_soon()
    return {"state": "updated", "text": info["message"] or "Updated."}


def restart_soon(delay: float = 1.0) -> None:
    from . import settings
    settings.restart_soon(delay)


class UpdateWorker(threading.Thread):
    """Checks a minute after starting, then every hour."""

    def __init__(self, cfg: dict, stop: threading.Event):
        super().__init__(name="updater", daemon=True)
        self.cfg, self.stop_ev = cfg, stop

    def run(self) -> None:
        if self.stop_ev.wait(60):
            return
        while not self.stop_ev.is_set():
            from . import config as _config
            try:
                u = _config.load().get("update", {})
            except Exception:
                u = self.cfg.get("update", {})
            if u.get("auto", True) and u.get("repo"):
                try:
                    r = check(self.cfg)
                    log.info("updater: %s", r["text"])
                except Exception:
                    log.exception("update failed")
            if self.stop_ev.wait(3600 * max(0.25, float(u.get("check_hours", 1)))):
                return
