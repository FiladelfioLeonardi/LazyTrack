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


# what you're doing right now, for friends and the egg ("playing LoL", "coding in VS Code").
# The label only ever comes from the list below (or a game's own name), never from window titles,
# chats or other sites: an unknown app just shows its category.
NOW = {"what": "", "label": "", "since": 0.0, "t": 0.0}

_GAMES = {   # app / exe name (lower case) -> short name
    "league of legends": "LoL", "leagueclient": "LoL", "leagueclientux": "LoL", "league of legends (tm) client": "LoL",
    "valorant": "Valorant", "valorant-win64-shipping": "Valorant", "fortnite": "Fortnite",
    "fortniteclient-win64-shipping": "Fortnite", "counter-strike 2": "CS2", "cs2": "CS2", "minecraft": "Minecraft",
    "minecraft launcher": "Minecraft", "roblox": "Roblox", "robloxplayerbeta": "Roblox", "rocket league": "Rocket League",
    "rocketleague": "Rocket League", "overwatch": "Overwatch", "apex legends": "Apex", "r5apex": "Apex", "dota 2": "Dota 2",
    "dota2": "Dota 2", "gta5": "GTA V", "grand theft auto v": "GTA V", "elden ring": "Elden Ring", "eldenring": "Elden Ring",
    "the sims 4": "The Sims", "ts4_x64": "The Sims", "hearthstone": "Hearthstone", "world of warcraft": "WoW", "wow": "WoW",
    "stardew valley": "Stardew Valley", "among us": "Among Us", "genshin impact": "Genshin", "genshinimpact": "Genshin",
    "hollow knight": "Hollow Knight", "terraria": "Terraria", "rust": "Rust", "tslgame": "PUBG", "pubg": "PUBG",
    "call of duty": "CoD", "cod": "CoD", "fc25": "EA FC", "fc26": "EA FC", "ea sports fc 25": "EA FC", "ea sports fc 26": "EA FC",
    "marvel rivals": "Marvel Rivals", "deadlock": "Deadlock", "balatro": "Balatro", "fall guys": "Fall Guys",
    "rainbow six siege": "R6", "rainbowsix": "R6", "destiny 2": "Destiny 2", "destiny2": "Destiny 2", "palworld": "Palworld",
    "lethal company": "Lethal Company", "phasmophobia": "Phasmophobia", "cyberpunk2077": "Cyberpunk", "cyberpunk 2077": "Cyberpunk",
}
_APPS = {    # app name (lower case) -> (what it says, category)
    "steam": ("on Steam", "gaming"), "riot client": ("opening a Riot game", "gaming"),
    "epic games launcher": ("on Epic Games", "gaming"), "battle.net": ("on Battle.net", "gaming"),
    "code": ("coding in VS Code", "coding"), "visual studio code": ("coding in VS Code", "coding"),
    "cursor": ("coding in Cursor", "coding"), "xcode": ("coding in Xcode", "coding"), "visual studio": ("coding in Visual Studio", "coding"),
    "clion": ("coding in CLion", "coding"), "pycharm": ("coding in PyCharm", "coding"), "intellij idea": ("coding in IntelliJ", "coding"),
    "terminal": ("in the terminal", "coding"), "iterm2": ("in the terminal", "coding"), "warp": ("in the terminal", "coding"),
    "windowsterminal": ("in the terminal", "coding"), "unrealeditor": ("in Unreal Engine", "coding"),
    "arduino ide": ("tinkering in Arduino", "coding"), "blender": ("in Blender", "other"), "figma": ("in Figma", "other"),
    "discord": ("on Discord", "social"), "whatsapp": ("on WhatsApp", "social"), "telegram": ("on Telegram", "social"),
    "messages": ("texting", "social"), "slack": ("on Slack", "social"), "netflix": ("watching Netflix", "streaming"),
    "spotify": ("on Spotify", "other"), "music": ("listening to music", "other"), "twitch": ("watching Twitch", "streaming"),
    "vlc": ("watching something", "streaming"), "zoom.us": ("in a call", "other"), "zoom": ("in a call", "other"),
    "facetime": ("in a call", "other"), "microsoft teams": ("in a call", "other"), "notion": ("in Notion", "other"),
    "obsidian": ("taking notes", "other"), "microsoft word": ("writing in Word", "other"), "pages": ("writing", "other"),
    "microsoft excel": ("in Excel", "other"), "microsoft powerpoint": ("making slides", "other"), "keynote": ("making slides", "other"),
    "photoshop": ("in Photoshop", "other"), "adobe photoshop": ("in Photoshop", "other"), "elegooslicer": ("slicing a 3D print", "other"),
    "claude": ("talking to Claude", "other"), "chatgpt": ("on ChatGPT", "other"),
}
_SITES = [   # website -> (what it says, category); matched like the category rules
    ("youtube.com", "watching YouTube", "youtube"), ("youtu.be", "watching YouTube", "youtube"),
    ("netflix.com", "watching Netflix", "streaming"), ("twitch.tv", "watching Twitch", "streaming"),
    ("primevideo.com", "watching Prime Video", "streaming"), ("disneyplus.com", "watching Disney+", "streaming"),
    ("crunchyroll.com", "watching anime", "streaming"), ("instagram.com", "on Instagram", "social"),
    ("tiktok.com", "on TikTok", "social"), ("x.com", "on X", "social"), ("twitter.com", "on X", "social"),
    ("reddit.com", "on Reddit", "social"), ("facebook.com", "on Facebook", "social"), ("web.whatsapp.com", "on WhatsApp", "social"),
    ("github.com", "on GitHub", "coding"), ("stackoverflow.com", "on Stack Overflow", "coding"),
    ("intra.42.fr", "on the 42 intra", "coding"), ("chatgpt.com", "on ChatGPT", "other"), ("claude.ai", "talking to Claude", "other"),
    ("op.gg", "checking op.gg", "gaming"), ("u.gg", "checking LoL builds", "gaming"), ("amazon.de", "shopping on Amazon", "other"),
    ("amazon.com", "shopping on Amazon", "other"), ("open.spotify.com", "on Spotify", "other"),
]


def label(app: str, url: str, category: str):
    """("playing LoL", "gaming") for the app / site in front, or ("", category) when it isn't a known one."""
    al = (app or "").strip().lower()
    if al in _GAMES:
        return "playing " + _GAMES[al], "gaming"
    if al in _APPS:
        return _APPS[al]
    if url:
        ul = url.lower()
        host = urlparse(ul).hostname or ""
        for site, text, cat in _SITES:
            if _url_match(site, host, ul):
                return text, cat
    if category == "gaming" and app:             # a game we don't know yet: its own name is fine to show
        return "playing " + app.strip()[:24], "gaming"
    return "", category


def _now(what: str, t: float, lab: str = "") -> None:
    if NOW["what"] != what or NOW["label"] != lab:
        NOW.update(what=what, label=lab, since=t)
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
        if category in ("other", "browsing") and not url:
            try:
                if system.frontmost_is_game():       # anything from Steam / Riot / Epic counts as a game
                    category = "gaming"
            except Exception:
                pass
        lab, what = label(app, url, category)
        _now(what, now, lab)
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
