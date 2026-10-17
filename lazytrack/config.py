"""Config loading. Everything lives in ~/.lazytrack/ (override with LAZYTRACK_HOME)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

HOME = Path(os.environ.get("LAZYTRACK_HOME", str(Path.home() / ".lazytrack")))
CONFIG_PATH = HOME / "config.json"
DB_PATH = HOME / "lazytrack.db"
LABELED_DIR = HOME / "labeled"

DEFAULT_CONFIG = {
    "device_name": "macbook",
    "activity": {
        # how often we look at which app is in front
        "interval_seconds": 5,
        # no key press, click, scroll or mouse move for this long = you're away, stop counting
        # (and the minutes since your last activity are taken off again)
        "idle_seconds": 300,
        # while a video or call plays you count as present, up to this long without any input
        "video_max_idle_seconds": 10800,
        # only used if macOS can't tell whether a video is playing
        "passive_categories": ["youtube", "streaming"],
        "passive_idle_seconds": 1800,
        # first matching rule wins; "apps" = exact app name, others = substring match
        "categories": [
            {"name": "youtube", "url_contains": ["youtube.com", "youtu.be"], "title_contains": ["- youtube"]},
            {"name": "streaming", "apps": ["Netflix", "Prime Video", "Disney+", "Twitch", "VLC"],
             "url_contains": ["netflix.com", "twitch.tv", "disneyplus.com", "primevideo.com", "crunchyroll.com"]},
            {"name": "social", "apps": ["Discord", "WhatsApp", "Telegram", "Messages", "Slack"],
             "url_contains": ["instagram.com", "tiktok.com", "x.com", "twitter.com", "reddit.com", "facebook.com", "web.whatsapp.com"]},
            {"name": "coding", "apps": ["Code", "Visual Studio Code", "Cursor", "Xcode", "Terminal", "iTerm2", "Warp", "UnrealEditor",
                                        "Visual Studio", "CLion", "PyCharm", "IntelliJ IDEA"],
             "url_contains": ["github.com", "stackoverflow.com", "intra.42.fr", "cplusplus.com", "cppreference.com"]},
            {"name": "gaming", "apps": ["Steam", "Epic Games Launcher", "Minecraft", "Riot Client", "League of Legends", "VALORANT",
                                        "Fortnite", "Counter-Strike 2", "Battle.net", "EA app", "Xbox", "Roblox"]},
        ],
    },
    "commits": {
        "roots": ["~"],
        "max_depth": 4,
        # empty = use `git config --global user.email`
        "author_emails": [],
        "sync_minutes": 15,
    },
    "fortytwo": {
        "login": "",
        "client_id": "",
        "client_secret": "",
        "sync_minutes": 60,
        # used when 42 isn't connected (or as an override): "YYYY-MM-DD"
        "manual_blackhole": "",
        "milestone_date": "",
        "milestone_name": "",
    },
    "github": {"username": "", "token": "", "sync_minutes": 30},
    "smoke": {
        "enabled": True,
        "camera_index": 0,
        "fps": 4,
        "yolo_every_n_frames": 2,
        # "auto" = your personal model once trained (./lt train), else YOLO-World
        "detector": "auto",
        "personal_threshold": 0.6,
        # general detector (before you train your own): bigger = sees small/partly hidden cigarettes better
        "model": "yolov8m-worldv2.pt",
        "classes": ["cigarette", "lit cigarette", "cigarette in hand", "cigarette in mouth",
                    "burning cigarette tip", "rolled joint", "cigar"],
        "confidence": 0.15,
        # evidence builds up with every detection and fades out (half-life in seconds),
        # so a finger over the cigarette for a few seconds doesn't reset everything
        "evidence_half_life": 20,
        "trigger_score": 4.0,
        "motion_threshold": 4.0,
        "cooldown_minutes": 10,
        "false_alarm_cooldown_minutes": 2,
        "pause_when_idle_seconds": 600,
        "save_labeled_frames": True,
    },
    "dashboard": {"host": "127.0.0.1", "port": 8042, "token": "", "open_on_start": True},
    # 0 = Monday ... 6 = Sunday
    "summary": {"weekday": 6, "hour": 20},
    # auto-updates: the GitHub repo LazyTrack downloads new versions from ("owner/name")
    "update": {"repo": "FiladelfioLeonardi/LazyTrack", "branch": "main", "auto": True, "check_hours": 6},
    # the shared friends server (Supabase). The key is the public "publishable" one, made to sit in apps.
    "cloud": {"url": "https://zmrzumltciteeznzjwaw.supabase.co", "key": "sb_publishable_mmX_kRPufTOQk1M4fmjjFg_HenbqicQ"},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


# What new configs start with. Everything else uses DEFAULT_CONFIG, so improved
# defaults reach you automatically - only put things here you actually change.
STARTER_CONFIG = {
    "device_name": "macbook",
    "fortytwo": {"login": "", "client_id": "", "client_secret": ""},
    "smoke": {"enabled": True},
}

# Values old versions wrote into config.json. If yours still has them unchanged,
# they're dropped so the improved defaults apply.
LEGACY_DEFAULTS = {
    "activity": {"idle_seconds": [120]},
    "smoke": {
        "model": ["yolov8s-worldv2.pt"],
        "classes": [["cigarette", "joint", "cigar"], ["cigarette", "lit cigarette", "rolled joint", "cigar"]],
        "confidence": [0.2],
        "hits_needed": [3],
        "window": [6],
    },
}


def _drop_legacy(user: dict) -> bool:
    changed = False
    for section, keys in LEGACY_DEFAULTS.items():
        sec = user.get(section)
        if not isinstance(sec, dict):
            continue
        for key, old_values in keys.items():
            if key in sec and sec[key] in old_values:
                del sec[key]
                changed = True
    return changed


def load() -> dict:
    HOME.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(STARTER_CONFIG, indent=2))
    user = json.loads(CONFIG_PATH.read_text())
    if _drop_legacy(user):
        CONFIG_PATH.write_text(json.dumps(user, indent=2))
    return _merge(DEFAULT_CONFIG, user)
