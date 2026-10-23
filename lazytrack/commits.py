"""Finds your local git repos and counts your commits."""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import List

from . import db

log = logging.getLogger(__name__)

SKIP = {"Library", "Applications", "AppData", "Program Files", "scoop", "node_modules", "Pictures", "Music", "Movies",
        "Public", "go", "venv", ".venv", "build", "dist", "Intermediate", "DerivedDataCache", "Saved"}


def find_repos(roots: List[str], max_depth: int) -> List[Path]:
    found: List[Path] = []

    def walk(p: Path, depth: int) -> None:
        if (p / ".git").exists():
            found.append(p)
            return
        if depth >= max_depth:
            return
        try:
            entries = list(os.scandir(p))
        except OSError:
            return
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in SKIP:
                    walk(Path(e.path), depth + 1)
            except OSError:
                continue

    for r in roots:
        p = Path(os.path.expanduser(r))
        if p.is_dir():
            walk(p, 0)
    return found


def _git(args: List[str], cwd=None) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30,
                             creationflags=0x08000000 if os.name == "nt" else 0)   # CREATE_NO_WINDOW
        return out.stdout if out.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def author_emails(ccfg: dict) -> set:
    emails = {e.strip().lower() for e in ccfg.get("author_emails", []) if e.strip()}
    if not emails:
        e = _git(["config", "--global", "user.email"]).strip().lower()
        if e:
            emails.add(e)
    return emails


def sync(cfg: dict) -> int:
    """Imports new commits. Returns how many were new."""
    ccfg = cfg["commits"]
    emails = author_emails(ccfg)
    first_run = db.kv_get("commits_synced") is None
    since = "365 days ago" if first_run else "14 days ago"
    c = db.conn()
    new = 0
    for repo in find_repos(ccfg["roots"], ccfg["max_depth"]):
        out = _git(["log", "--all", "--no-merges", f"--since={since}",
                    "--pretty=format:%H%x09%ct%x09%ae%x09%s"], cwd=str(repo))
        for line in out.splitlines():
            parts = line.split("\t", 3)
            if len(parts) < 4:
                continue
            h, ts, email, msg = parts
            if emails and email.lower() not in emails:
                continue
            cur = c.execute("INSERT OR IGNORE INTO commits(hash, repo, ts, message) VALUES(?,?,?,?)",
                            (h, repo.name, float(ts), msg[:300]))
            new += cur.rowcount
    c.commit()
    db.kv_set("commits_synced", "1")
    if new:
        log.info("commits: %d new", new)
    return new
