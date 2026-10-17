"""The few settings people actually change, editable from the dashboard instead of config.json."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import date

from . import config

log = logging.getLogger(__name__)

# smoking-detector sensitivity -> how much evidence before it asks
SENSITIVITY = {"low": 6.0, "normal": 4.0, "high": 2.5}
SECRET_MASK = "••••••••"


def _raw() -> dict:
    try:
        return json.loads(config.CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def get(cfg: dict) -> dict:
    s, f, g = cfg["smoke"], cfg["fortytwo"], cfg.get("github", {})
    trigger = float(s.get("trigger_score", 4.0))
    sens = min(SENSITIVITY, key=lambda k: abs(SENSITIVITY[k] - trigger))
    return {
        "device_name": cfg.get("device_name", "macbook"),
        "idle_minutes": max(1, round(int(cfg["activity"].get("idle_seconds", 300)) / 60)),
        "open_dashboard_on_start": bool(cfg["dashboard"].get("open_on_start", True)),
        "smoke_enabled": bool(s.get("enabled", True)),
        "smoke_sensitivity": sens,
        "smoke_cooldown_minutes": int(s.get("cooldown_minutes", 10)),
        "save_training_photos": bool(s.get("save_labeled_frames", True)),
        "summary_weekday": int(cfg["summary"]["weekday"]),
        "summary_hour": int(cfg["summary"]["hour"]),
        "ft_login": f.get("login", ""),
        "ft_client_id": f.get("client_id", ""),
        "ft_client_secret": SECRET_MASK if f.get("client_secret") else "",
        "ft_manual_blackhole": f.get("manual_blackhole", ""),
        "ft_milestone_date": f.get("milestone_date", ""),
        "ft_milestone_name": f.get("milestone_name", ""),
        "github_username": g.get("username", ""),
        "github_token": SECRET_MASK if g.get("token") else "",
    }


def save(body: dict) -> dict:
    """Writes only the keys that were sent, keeps everything else in config.json as it is."""
    raw = _raw()

    def section(name: str) -> dict:
        raw.setdefault(name, {})
        return raw[name]

    if "device_name" in body:
        name = str(body["device_name"]).strip()[:40]
        if name:
            raw["device_name"] = name
    if "idle_minutes" in body:
        section("activity")["idle_seconds"] = 60 * max(1, min(60, int(body["idle_minutes"])))
    if "open_dashboard_on_start" in body:
        section("dashboard")["open_on_start"] = bool(body["open_dashboard_on_start"])
    if "smoke_enabled" in body:
        section("smoke")["enabled"] = bool(body["smoke_enabled"])
    if body.get("smoke_sensitivity") in SENSITIVITY:
        section("smoke")["trigger_score"] = SENSITIVITY[body["smoke_sensitivity"]]
    if "smoke_cooldown_minutes" in body:
        section("smoke")["cooldown_minutes"] = max(1, min(120, int(body["smoke_cooldown_minutes"])))
    if "save_training_photos" in body:
        section("smoke")["save_labeled_frames"] = bool(body["save_training_photos"])
    if "summary_weekday" in body:
        section("summary")["weekday"] = max(0, min(6, int(body["summary_weekday"])))
    if "summary_hour" in body:
        section("summary")["hour"] = max(0, min(23, int(body["summary_hour"])))
    if "ft_login" in body:
        section("fortytwo")["login"] = str(body["ft_login"]).strip().lower()[:30]
    if "ft_client_id" in body:
        section("fortytwo")["client_id"] = str(body["ft_client_id"]).strip()[:200]
    secret = str(body.get("ft_client_secret", SECRET_MASK)).strip()
    if secret != SECRET_MASK:  # untouched field comes back masked
        section("fortytwo")["client_secret"] = secret[:200]
    for key, name in (("ft_manual_blackhole", "manual_blackhole"), ("ft_milestone_date", "milestone_date")):
        if key in body:
            v = str(body[key]).strip()[:10]
            try:
                if v:
                    date.fromisoformat(v)
                section("fortytwo")[name] = v
            except ValueError:
                pass
    if "ft_milestone_name" in body:
        section("fortytwo")["milestone_name"] = str(body["ft_milestone_name"]).strip()[:60]
    if "github_username" in body:
        section("github")["username"] = str(body["github_username"]).strip().lstrip("@")[:39]
    gtok = str(body.get("github_token", SECRET_MASK)).strip()
    if gtok != SECRET_MASK:
        section("github")["token"] = gtok[:255]

    config.CONFIG_PATH.write_text(json.dumps(raw, indent=2))
    log.info("settings saved")
    return get(config.load())


def restart_soon(delay: float = 0.6) -> None:
    """Restarts LazyTrack in the same Terminal window (after the HTTP reply has gone out)."""
    def go():
        log.info("restarting to apply settings...")
        if sys.platform == "win32":
            # execv doesn't really replace the process on Windows; start.bat restarts us when we exit with 3
            os._exit(3)
        os.environ["LAZYTRACK_RESTARTED"] = "1"  # don't open another browser tab
        os.execv(sys.executable, [sys.executable, "-m", "lazytrack", *sys.argv[1:]])
    threading.Timer(delay, go).start()
