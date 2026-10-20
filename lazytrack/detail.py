"""Per-stat detail: one day hour by hour, compared with the day before, plus 30-day history."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import db, summary

EVENT_KINDS = set(summary.HABIT_KINDS + summary.GOOD_KINDS)
METRICS = {"sleep", "steps"}


def _bounds(d: date):
    a = datetime.combine(d, datetime.min.time())
    return a.timestamp(), (a + timedelta(days=1)).timestamp()


def kind_info(kind: str) -> dict:
    from . import habits
    h = habits.info(kind)
    if h:
        return {"type": "event", "unit": "count", "bad": bool(h["bad"])}
    if kind == "commits":
        return {"type": "commits", "unit": "count", "bad": False}
    if kind == "github":
        return {"type": "github", "unit": "count", "bad": False}
    if kind in METRICS:
        return {"type": "metric", "unit": "seconds" if kind == "sleep" else "count", "bad": False}
    # anything else is a screen-time category ("screen" = all of them)
    return {"type": "activity", "unit": "seconds", "bad": kind not in ("coding",)}


# ---------------------------------------------------------------- per-day totals

def _activity_where(kind: str):
    if kind == "screen":
        return "", ()
    if kind == "screen_pc":
        return " AND source='mac'", ()
    if kind == "screen_phone":
        return " AND source='phone'", ()
    return " AND category=?", (kind,)


def total(kind: str, d: date, until: Optional[float] = None) -> float:
    """Total for one day. `until` cuts the day off at a time (for 'by this time yesterday')."""
    info = kind_info(kind)
    a, b = _bounds(d)
    if until is not None:
        b = min(b, until)
    c = db.conn()
    if info["type"] == "event":
        return c.execute("SELECT COUNT(*) FROM events WHERE kind=? AND ts>=? AND ts<?", (kind, a, b)).fetchone()[0]
    if info["type"] == "commits":
        return c.execute("SELECT COUNT(*) FROM commits WHERE ts>=? AND ts<?", (a, b)).fetchone()[0]
    if info["type"] == "metric":
        r = c.execute("SELECT MAX(value) FROM metrics WHERE metric=? AND day=?", (kind, d.isoformat())).fetchone()[0]
        return r or 0
    if info["type"] == "github":
        r = c.execute("SELECT count FROM gh_days WHERE day=?", (d.isoformat(),)).fetchone()
        return r[0] if r else 0
    extra, args = _activity_where(kind)
    r = c.execute("SELECT SUM(MIN(ts_end, ?) - MAX(ts_start, ?)) FROM activity "
                  "WHERE ts_end > ? AND ts_start < ?" + extra, (b, a, a, b, *args)).fetchone()[0]
    return max(0.0, r or 0.0)


def hourly(kind: str, d: date) -> Optional[List[float]]:
    """24 buckets (count or seconds per hour). None for daily-only metrics."""
    info = kind_info(kind)
    if info["type"] in ("metric", "github"):
        return None
    a, b = _bounds(d)
    out = [0.0] * 24
    c = db.conn()

    def bucket(ts: float) -> int:
        return min(23, max(0, datetime.fromtimestamp(ts).hour))

    if info["type"] in ("event", "commits"):
        if info["type"] == "event":
            rows = c.execute("SELECT ts FROM events WHERE kind=? AND ts>=? AND ts<?", (kind, a, b))
        else:
            rows = c.execute("SELECT ts FROM commits WHERE ts>=? AND ts<?", (a, b))
        for (ts,) in rows:
            out[bucket(ts)] += 1
        return out

    extra, args = _activity_where(kind)
    rows = c.execute("SELECT ts_start, ts_end FROM activity WHERE ts_end > ? AND ts_start < ?" + extra,
                     (a, b, *args)).fetchall()
    for s, e in rows:
        s, e = max(s, a), min(e, b)
        while s < e:
            dt = datetime.fromtimestamp(s)
            hour_end = (dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).timestamp()
            seg_end = min(e, hour_end)
            out[bucket(s)] += seg_end - s
            s = seg_end
    return out


# ---------------------------------------------------------------- timeline: exact moments / blocks

def timeline(kind: str, d: date) -> Optional[List[dict]]:
    """Events and commits: one entry per moment. Screen time: merged time blocks. Metrics: None."""
    info = kind_info(kind)
    a, b = _bounds(d)
    c = db.conn()
    if info["type"] == "event":
        return [{"ts": r["ts"], "id": r["id"], "source": r["source"]} for r in c.execute(
            "SELECT id, ts, source FROM events WHERE kind=? AND ts>=? AND ts<? ORDER BY ts", (kind, a, b))]
    if info["type"] == "commits":
        return [{"ts": r["ts"], "label": r["repo"] + ": " + r["message"]} for r in c.execute(
            "SELECT ts, repo, message FROM commits WHERE ts>=? AND ts<? ORDER BY ts", (a, b))]
    if info["type"] in ("metric", "github"):
        return None
    extra, args = _activity_where(kind)
    rows = c.execute("SELECT MAX(ts_start, ?) AS s, MIN(ts_end, ?) AS e FROM activity "
                     "WHERE ts_end > ? AND ts_start < ?" + extra + " ORDER BY s", (a, b, a, b, *args)).fetchall()
    blocks: List[dict] = []
    for s, e in rows:
        if blocks and s - blocks[-1]["end"] <= 120:  # gaps under 2 minutes join the block
            blocks[-1]["end"] = max(blocks[-1]["end"], e)
        else:
            blocks.append({"start": s, "end": e})
    return [x for x in blocks if x["end"] - x["start"] >= 30]


# ---------------------------------------------------------------- what happened that day

def items(kind: str, d: date) -> List[dict]:
    info = kind_info(kind)
    a, b = _bounds(d)
    c = db.conn()
    if info["type"] == "event":
        return [dict(r) for r in c.execute(
            "SELECT id, ts, device, source, note FROM events WHERE kind=? AND ts>=? AND ts<? ORDER BY ts",
            (kind, a, b))]
    if info["type"] == "commits":
        return [dict(r) for r in c.execute(
            "SELECT ts, repo, message FROM commits WHERE ts>=? AND ts<? ORDER BY ts", (a, b))]
    if info["type"] == "metric":
        return []
    if info["type"] == "github":
        from . import github
        return github.status().get("recent_repos", [])
    # activity: where the time went (apps / sites)
    extra, args = _activity_where(kind)
    label = "CASE WHEN domain != '' THEN domain ELSE app END"
    return [{"name": r[0], "device": r[1], "seconds": r[2]} for r in c.execute(
        f"SELECT {label} AS name, device, SUM(MIN(ts_end, ?) - MAX(ts_start, ?)) AS s FROM activity "
        "WHERE ts_end > ? AND ts_start < ?" + extra + " GROUP BY name, device ORDER BY s DESC LIMIT 15",
        (b, a, a, b, *args))]


def gaps(item_list: List[dict]) -> dict:
    """For events: first, last, average and longest time between them."""
    ts = [i["ts"] for i in item_list]
    if not ts:
        return {}
    out = {"first": ts[0], "last": ts[-1]}
    if len(ts) > 1:
        diffs = [b - a for a, b in zip(ts, ts[1:])]
        out["avg_gap"] = sum(diffs) / len(diffs)
        out["longest_gap"] = max(diffs)
    return out


def streak(kind: str, d: date, bad: bool, history: Dict[str, float]) -> dict:
    """Bad habit: days in a row without it, before d. Good habit: days in a row with it, up to d."""
    cur = d - timedelta(days=1) if bad else d
    n = 0
    while True:
        v = history.get(cur.isoformat())
        if v is None:
            v = total(kind, cur)
        if (v == 0) if bad else (v > 0):
            n += 1
            cur -= timedelta(days=1)
            if n >= 365:
                break
        else:
            break
    # a good habit not done *yet today* shouldn't break the streak
    if not bad and n == 0 and d == date.today():
        y = d - timedelta(days=1)
        while total(kind, y) > 0 and n < 365:
            n += 1
            y -= timedelta(days=1)
    return {"days": n, "kind": "clean" if bad else "in_a_row"}


# ---------------------------------------------------------------- everything for the page

def detail(kind: str, day: Optional[str] = None) -> dict:
    d = date.fromisoformat(day) if day else date.today()
    y = d - timedelta(days=1)
    info = kind_info(kind)
    is_today = d == date.today()

    history: Dict[str, float] = {}
    for i in range(29, -1, -1):
        dd = d - timedelta(days=i)
        history[dd.isoformat()] = total(kind, dd)

    prev = [history[(d - timedelta(days=i)).isoformat()] for i in range(1, 30)]
    prev7 = prev[:7]
    val = history[d.isoformat()]
    yval = history[y.isoformat()]

    by_now = None
    if is_today:
        now = datetime.now()
        same_time_yesterday = (now - timedelta(days=1)).timestamp()
        by_now = total(kind, y, until=same_time_yesterday)

    # the 30-day chart: always the last 30 days up to today, so opening an older day from it
    # doesn't shift the bars (and newer days stay tappable). A day more than 30 days back gets
    # a window around it instead, so its bar is still there to see.
    today = date.today()
    end = today if (today - d).days < 30 else min(today, d + timedelta(days=15))
    chart = []
    for i in range(29, -1, -1):
        dd = end - timedelta(days=i)
        key = dd.isoformat()
        chart.append({"day": key, "value": history[key] if key in history else total(kind, dd)})

    its = items(kind, d)
    return {
        "kind": kind,
        **info,
        "day": d.isoformat(),
        "is_today": is_today,
        "value": val,
        "yesterday": yval,
        "yesterday_by_now": by_now,
        "avg7": sum(prev7) / len(prev7),
        "avg30": sum(prev) / len(prev),
        "hourly": hourly(kind, d),
        "hourly_yesterday": hourly(kind, y),
        "timeline": timeline(kind, d),
        "timeline_yesterday": timeline(kind, y),
        "day_start": _bounds(d)[0],
        "yesterday_start": _bounds(y)[0],
        "history": chart,
        "items": its,
        "gaps": gaps(its) if info["type"] == "event" else {},
        "streak": streak(kind, d, info["bad"], history) if info["type"] in ("event", "commits", "github") else None,
        "now": time.time(),
    }
