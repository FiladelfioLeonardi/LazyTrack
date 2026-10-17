"""Tracks which app / website is in front, merged into time segments."""
from __future__ import annotations

import logging
import time
from typing import List, Optional
from urllib.parse import urlparse

from . import db, system

log = logging.getLogger(__name__)


# iPhone app names -> category, used when the phone sends category "auto"
PHONE_APPS = {
    "youtube": "youtube", "netflix": "streaming", "twitch": "streaming", "prime video": "streaming",
    "disney+": "streaming", "instagram": "social", "tiktok": "social", "x": "social", "twitter": "social",
    "snapchat": "social", "reddit": "social", "bereal.": "social", "bereal": "social", "threads": "social",
    "facebook": "social", "whatsapp": "social", "telegram": "social", "discord": "social", "messages": "social",
    "github": "coding", "safari": "browsing", "chrome": "browsing",
}


def categorize_phone(app: str, rules: List[dict]) -> str:
    cat = categorize(app, "", "", rules)
    if cat in ("other", "browsing"):
        cat = PHONE_APPS.get(app.strip().lower(), cat)
    return cat


def _url_match(pattern: str, host: str, url: str) -> bool:
    """'x.com' matches x.com and www.x.com but not dropbox.com; patterns with '/' match anywhere in the URL."""
    pattern = pattern.lower()
    if "/" in pattern:
        return pattern in url
    return host == pattern or host.endswith("." + pattern)


def categorize(app: str, title: str, url: str, rules: List[dict]) -> str:
    al, tl, ul = app.lower(), title.lower(), url.lower()
    host = (urlparse(ul).hostname or "") if ul else ""
    for r in rules:
        if any(a.lower() == al for a in r.get("apps", [])):
            return r["name"]
        if ul and any(_url_match(s, host, ul) for s in r.get("url_contains", [])):
            return r["name"]
        if tl and any(s.lower() in tl for s in r.get("title_contains", [])):
            return r["name"]
    return "browsing" if app in system.BROWSERS else "other"


# what you're doing right now, for friends ("Coding for 25 min"). Only the category, never titles or sites.
NOW = {"what": "", "since": 0.0, "t": 0.0}


def _now(what: str, t: float) -> None:
    if NOW["what"] != what:
        NOW.update(what=what, since=t)
    NOW["t"] = t


class ActivityTracker:
    """Call sample() every few seconds. Must run on the main thread on macOS."""

    def __init__(self, cfg: dict):
        self.cfg = cfg["activity"]
        self.device = cfg["device_name"]
        self.cur: Optional[dict] = None  # {"id", "key", "end"}
        self.last_video = 0.0           # last time a video/call was playing

    def sample(self) -> Optional[dict]:
        now = time.time()
        if system.screen_locked():
            self.cur = None
            _now("away", now)
            return None
        app = system.frontmost_app()
        if not app:
            self.cur = None
            _now("away", now)
            return None
        url, title = system.browser_tab(app) if app in system.BROWSER_SCRIPTS else ("", "")
        category = categorize(app, title, url, self.cfg["categories"])

        if self.is_away(now, category):
            _now("away", now)
            return None
        _now(category, now)
        self.record(app, category, title, url, now)
        return {"app": app, "category": category, "title": title, "url": url}

    def is_away(self, now: float, category: str) -> bool:
        """Away = no key press, click, scroll or mouse move for idle_seconds (default 5 min),
        unless a video or call is playing. When you go away, the minutes since your last
        activity are taken back off the record."""
        idle = system.idle_seconds()          # any input: keys (arrows too), scrolling, trackpad, mouse
        last_input = now - idle
        video = system.video_playing()
        if video:
            self.last_video = now
        if video is None:                    # can't ask macOS: fall back to guessing from the site
            limit = (self.cfg["passive_idle_seconds"] if category in self.cfg["passive_categories"]
                     else self.cfg["idle_seconds"])
            away = idle > limit
            last_active = last_input
        else:
            last_active = max(last_input, self.last_video)
            away = now - last_active > self.cfg["idle_seconds"] or idle > self.cfg["video_max_idle_seconds"]
            if idle > self.cfg["video_max_idle_seconds"]:
                last_active = last_input
        if away:
            if self.cur:
                c = db.conn()
                c.execute("UPDATE activity SET ts_end = MAX(ts_start, ?) WHERE id = ? AND ts_end > ?",
                          (last_active, self.cur["id"], last_active))
                c.commit()
            self.cur = None
        return away

    def record(self, app: str, category: str, title: str, url: str, now: float) -> None:
        domain = (urlparse(url).hostname or "") if url else ""
        key = (app, category, title)
        c = db.conn()
        gap = self.cfg["interval_seconds"] * 3
        start = now
        if self.cur and now - self.cur["end"] <= gap:
            if self.cur["key"] == key:
                c.execute("UPDATE activity SET ts_end=? WHERE id=?", (now, self.cur["id"]))
                c.commit()
                self.cur["end"] = now
                return
            start = self.cur["end"]  # switched app: new segment starts where the last ended
        cur = c.execute(
            "INSERT INTO activity(device, app, title, domain, category, ts_start, ts_end) VALUES(?,?,?,?,?,?,?)",
            (self.device, app, title[:300], domain, category, start, now))
        c.commit()
        self.cur = {"id": cur.lastrowid, "key": key, "end": now}
