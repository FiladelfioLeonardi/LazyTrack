"""Stats queries: today, weekly summary, daily history, 42 status."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

from . import db

HABIT_KINDS = ["cigarette", "joint", "junk_food", "alcohol", "energy_drink"]
GOOD_KINDS = ["gym", "water", "walk"]
ACTIVE_PROJECT_STATUS = {"in_progress", "searching_a_group", "creating_group", "waiting_for_correction"}


def day_start(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


def week_start(offset_weeks: int = 0) -> datetime:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return day_start(monday + timedelta(weeks=offset_weeks))


def compute(start: datetime, end: datetime) -> dict:
    a, b = start.timestamp(), end.timestamp()
    c = db.conn()
    cats = {r["category"]: r["s"] for r in c.execute(
        "SELECT category, SUM(MIN(ts_end, ?) - MAX(ts_start, ?)) AS s FROM activity "
        "WHERE ts_end > ? AND ts_start < ? GROUP BY category ORDER BY s DESC", (b, a, a, b))}
    apps = [{"app": r["app"], "seconds": r["s"]} for r in c.execute(
        "SELECT app, SUM(MIN(ts_end, ?) - MAX(ts_start, ?)) AS s FROM activity "
        "WHERE ts_end > ? AND ts_start < ? GROUP BY app ORDER BY s DESC LIMIT 8", (b, a, a, b))]
    by_source = {r["source"]: r["s"] for r in c.execute(
        "SELECT source, SUM(MIN(ts_end, ?) - MAX(ts_start, ?)) AS s FROM activity "
        "WHERE ts_end > ? AND ts_start < ? GROUP BY source", (b, a, a, b))}
    events = {r["kind"]: r["n"] for r in c.execute(
        "SELECT kind, COUNT(*) AS n FROM events WHERE ts >= ? AND ts < ? GROUP BY kind", (a, b))}
    commits = c.execute("SELECT COUNT(*) FROM commits WHERE ts >= ? AND ts < ?", (a, b)).fetchone()[0]
    d0, d1 = start.date().isoformat(), end.date().isoformat()
    logtime = c.execute("SELECT COALESCE(SUM(seconds), 0) FROM ft_logtime WHERE day >= ? AND day < ?",
                        (d0, d1)).fetchone()[0]
    # per day take the max over devices (phone + watch could both report)
    sleep = [r[0] for r in c.execute("SELECT MAX(value) FROM metrics WHERE metric='sleep' AND day >= ? AND day < ? "
                                     "GROUP BY day", (d0, d1))]
    steps = [r[0] for r in c.execute("SELECT MAX(value) FROM metrics WHERE metric='steps' AND day >= ? AND day < ? "
                                     "GROUP BY day", (d0, d1))]
    return {
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "categories": cats,
        "screen_total": sum(cats.values()),
        "screen_pc": by_source.get("mac", 0) or 0,
        "screen_phone": by_source.get("phone", 0) or 0,
        "top_apps": apps,
        "events": events,
        "commits": commits,
        "logtime_42": logtime,
        "sleep_avg": sum(sleep) / len(sleep) if sleep else 0,
        "sleep_nights": len(sleep),
        "steps_total": sum(steps),
        "steps_avg": sum(steps) / len(steps) if steps else 0,
    }


def today() -> dict:
    s = day_start(date.today())
    return compute(s, s + timedelta(days=1))


def weekly(offset: int = 0) -> dict:
    s = week_start(offset)
    return compute(s, s + timedelta(days=7))


def daily(days: int = 14) -> List[dict]:
    out = []
    for i in range(days - 1, -1, -1):
        d = date.today() - timedelta(days=i)
        s = day_start(d)
        r = compute(s, s + timedelta(days=1))
        out.append({"day": d.isoformat(), "categories": r["categories"], "events": r["events"],
                    "screen_pc": r["screen_pc"], "screen_phone": r["screen_phone"],
                    "commits": r["commits"], "logtime_42": r["logtime_42"],
                    "sleep": r["sleep_avg"], "steps": r["steps_total"]})
    return out


def _days_until(d: Optional[date]) -> Optional[int]:
    return None if d is None else (d - date.today()).days


def _parse_day(s) -> Optional[date]:
    if not s or s == "None":
        return None
    try:
        if "T" in str(s):
            return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone().date()
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def ft_status(cfg: Optional[dict] = None) -> dict:
    f = (cfg or {}).get("fortytwo", {})
    manual_bh = _parse_day(f.get("manual_blackhole"))
    api_bh = _parse_day(db.kv_get("ft_blackholed_at"))
    bh_date = manual_bh or api_bh
    days_left = _days_until(bh_date)
    ms_date = _parse_day(f.get("milestone_date"))
    c = db.conn()
    projects = [dict(r) for r in c.execute(
        "SELECT name, status, final_mark, validated, updated_at FROM ft_projects ORDER BY updated_at DESC")]
    return {
        "configured": db.kv_get("ft_login") is not None,
        "connected": db.kv_get("ft_login") is not None,
        "has_deadlines": bh_date is not None or ms_date is not None,
        "login": db.kv_get("ft_login"),
        "level": db.kv_get("ft_level"),
        "blackhole_days": days_left,
        "blackhole_date": bh_date.isoformat() if bh_date else None,
        "blackhole_source": "manual" if manual_bh else "intra" if api_bh else None,
        "milestone_days": _days_until(ms_date),
        "milestone_date": ms_date.isoformat() if ms_date else None,
        "milestone_name": f.get("milestone_name") or "",
        "active": [p for p in projects if p["status"] in ACTIVE_PROJECT_STATUS],
        "recent_done": [p for p in projects if p["status"] == "finished"][:5],
        "synced_at": db.kv_get("ft_synced_at"),
    }


def hm(seconds: float) -> str:
    seconds = int(seconds or 0)
    h, m = divmod(seconds // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _delta(now: float, before: float, fmt=hm) -> str:
    d = (now or 0) - (before or 0)
    if not before and not now:
        return ""
    sign = "+" if d >= 0 else "-"
    return f" ({sign}{fmt(abs(d))})"


def format_week(this: dict, last: dict, ft: dict) -> str:
    n = lambda x: str(int(x))  # noqa: E731
    last_day = date.fromisoformat(this["end"]) - timedelta(days=1)
    lines = [f"Week {this['start']} → {last_day.isoformat()}  (vs last week)", ""]
    lines.append("SCREEN")
    for cat in sorted(set(this["categories"]) | set(last["categories"]),
                      key=lambda k: -this["categories"].get(k, 0)):
        lines.append(f"  {cat:<10} {hm(this['categories'].get(cat, 0)):>8}"
                     f"{_delta(this['categories'].get(cat, 0), last['categories'].get(cat, 0))}")
    lines.append(f"  {'mac':<10} {hm(this['screen_pc']):>8}{_delta(this['screen_pc'], last['screen_pc'])}")
    lines.append(f"  {'phone':<10} {hm(this['screen_phone']):>8}{_delta(this['screen_phone'], last['screen_phone'])}")
    lines.append(f"  {'total':<10} {hm(this['screen_total']):>8}{_delta(this['screen_total'], last['screen_total'])}")
    lines += ["", "WORK",
              f"  commits    {this['commits']:>8}{_delta(this['commits'], last['commits'], n)}",
              f"  42 logtime {hm(this['logtime_42']):>8}{_delta(this['logtime_42'], last['logtime_42'])}"]
    if ft.get("blackhole_days") is not None:
        lines.append(f"  blackhole  {ft['blackhole_days']:>5} days left")
    if ft.get("milestone_days") is not None:
        lines.append(f"  milestone  {ft['milestone_days']:>5} days left")
    for p in ft.get("active", []):
        lines.append(f"  ▸ {p['name']} ({p['status'].replace('_', ' ')})")
    if this["sleep_nights"] or last["sleep_nights"]:
        lines += ["", "BODY",
                  f"  sleep avg  {hm(this['sleep_avg']):>8}{_delta(this['sleep_avg'], last['sleep_avg'])}",
                  f"  steps/day  {int(this['steps_avg']):>8}{_delta(this['steps_avg'], last['steps_avg'], n)}"]
    lines += ["", "HABITS"]
    from . import habits   # here to avoid an import loop
    for h in habits.get()["habits"]:
        k = h["k"]
        if this["events"].get(k) or last["events"].get(k):
            lines.append(f"  {h['n'][:12]:<12} {this['events'].get(k, 0):>6}"
                         f"{_delta(this['events'].get(k, 0), last['events'].get(k, 0), n)}")
    return "\n".join(lines)


def notification_line(this: dict, last: dict) -> str:
    cats = this["categories"]
    parts = [f"YouTube {hm(cats.get('youtube', 0))}", f"coding {hm(cats.get('coding', 0))}",
             f"{this['commits']} commits"]
    smokes = this["events"].get("cigarette", 0)
    prev = last["events"].get("cigarette", 0)
    if smokes or prev:
        parts.append(f"🚬 {smokes} ({'+' if smokes >= prev else '-'}{abs(smokes - prev)})")
    if this["sleep_nights"]:
        parts.append(f"😴 {hm(this['sleep_avg'])}/night")
    if this["events"].get("gym"):
        parts.append(f"gym ×{this['events']['gym']}")
    return " · ".join(parts)


def maybe_send_weekly(cfg: dict) -> bool:
    """Called every minute; sends the summary once per week at the configured time."""
    from . import system
    now = datetime.now()
    sc = cfg["summary"]
    if now.weekday() != int(sc["weekday"]) or now.hour < int(sc["hour"]):
        return False
    week_id = now.strftime("%G-W%V")
    if db.kv_get("summary_sent") == week_id:
        return False
    this, last = weekly(0), weekly(-1)
    system.notify("Your week 📊", notification_line(this, last))
    db.kv_set("summary_sent", week_id)
    db.kv_set("summary_text", format_week(this, last, ft_status(cfg)))
    db.kv_set("summary_at", time.time())
    return True
