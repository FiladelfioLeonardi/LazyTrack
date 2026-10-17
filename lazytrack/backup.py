"""Backups of your LazyTrack data, so it can never be lost in one go again.

Once a day (and right before every update) LazyTrack copies its database and settings into
"LazyTrack Backups" in your home folder: ~/LazyTrack Backups on a Mac, C:\\Users\\you\\LazyTrack Backups
on Windows. That folder is outside LazyTrack's own folders, so uninstalling (even with "delete my
data") never touches it. The last 14 days are kept.

Restoring copies a backup back into the live database (SQLite's own backup, safe while running),
then LazyTrack restarts.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import List

from . import config, db

log = logging.getLogger(__name__)
KEEP = 14


def folder() -> Path:
    return Path(os.environ.get("LAZYTRACK_BACKUPS", str(Path.home() / "LazyTrack Backups")))


def _name(when: datetime) -> str:
    return "lazytrack-" + when.strftime("%Y-%m-%d_%H-%M-%S")


def make(reason: str = "daily") -> Path:
    """Writes a backup now and returns its database file."""
    d = folder()
    d.mkdir(parents=True, exist_ok=True)
    base = _name(datetime.now())
    n = 2
    while (d / (base + ".db")).exists():   # two in the same second (e.g. right before a restore)
        base = f"{_name(datetime.now())}-{n}"
        n += 1
    target = d / (base + ".db")
    tmp = d / (base + ".db.part")
    out = sqlite3.connect(str(tmp))
    try:
        db.conn().backup(out)          # a consistent copy even while LazyTrack is writing
    finally:
        out.close()
    os.replace(tmp, target)
    if config.CONFIG_PATH.exists():
        shutil.copy2(config.CONFIG_PATH, d / (base + ".config.json"))
    (d / (base + ".json")).write_text(json.dumps({"reason": reason, "at": time.time()}))
    db.kv_set("backup_last", str(time.time()))
    _prune()
    log.info("backup: saved %s (%s)", target.name, reason)
    return target


def _prune() -> None:
    """Keeps the 14 newest daily backups and, separately, the 14 newest others (before updates,
    by you...), so a day with lots of updates never pushes out the daily ones."""
    daily, other = [], []
    for b in listing():
        (daily if b["reason"] == "daily" else other).append(b)
    for b in daily[KEEP:] + other[KEEP:]:
        for p in folder().glob(b["name"] + ".*"):
            try:
                p.unlink()
            except OSError:
                pass


def maybe_daily() -> None:
    """Called every minute or so: one backup per day."""
    try:
        last = float(db.kv_get("backup_last") or 0)
    except ValueError:
        last = 0
    if datetime.fromtimestamp(last).date() != datetime.now().date():
        try:
            make("daily")
        except Exception:
            log.exception("backup failed")


def listing() -> List[dict]:
    out = []
    for p in sorted(folder().glob("lazytrack-*.db"), reverse=True):
        meta = {}
        try:
            meta = json.loads((folder() / (p.stem + ".json")).read_text())
        except (OSError, ValueError):
            pass
        st = p.stat()
        out.append({"name": p.stem, "size": st.st_size, "at": meta.get("at", st.st_mtime), "reason": meta.get("reason", "")})
    return sorted(out, key=lambda b: -b["at"])


def restore(name: str) -> None:
    """Puts a backup back. A backup of right now is made first, so a restore can be undone too."""
    src = folder() / (Path(name).name + ".db")
    if not src.name.startswith("lazytrack-") or not src.exists():
        raise FileNotFoundError("That backup isn't there anymore.")
    make("before restoring " + src.stem)
    inp = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        inp.backup(db.conn())
    finally:
        inp.close()
    cfg = folder() / (src.stem + ".config.json")
    if cfg.exists():
        shutil.copy2(cfg, config.CONFIG_PATH)
    log.info("backup: restored %s", src.name)
