"""Mochi's day: he learns when you sleep.

Sleep
- Learned from the last 14 nights of activity (computer, phone, anything logged): each night's sleep is the
  longest break of 3+ hours that starts between 8 pm and 6 am. Bedtime and wake-up are the typical (median)
  times. Until 3 nights are known: 00:30 to 08:30.
- Mochi sleeps inside that window, but never while you're up: anything on the computer (or a log) in the last
  15 minutes keeps him awake. Late night gaming? He's up with you.

(Meals and hunger were removed on 2026-10-23. Old "feed" events and the kv keys meal_plans / meals_since stay
in the database untouched; nothing reads them any more.)
"""
from __future__ import annotations

import statistics
import time
from datetime import date, datetime, timedelta
from typing import List, Optional

from . import db

DEFAULT_BED, DEFAULT_WAKE = 30, 8 * 60 + 30          # minutes after midnight
_cache = {"t": 0.0, "val": None}


def _clock(m: float) -> str:
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def _midnight(d: date) -> float:
    return datetime.combine(d, datetime.min.time()).timestamp()


# ---------------------------------------------------------------- sleep
def _busy(a: float, b: float) -> List[List[float]]:
    """When you were doing something between a and b: activity, plus a minute around every log."""
    c = db.conn()
    spans = [[max(a, r[0]), min(b, r[1])] for r in
             c.execute("SELECT ts_start, ts_end FROM activity WHERE ts_end >= ? AND ts_start <= ? ORDER BY ts_start", (a, b))]
    spans += [[r[0], r[0] + 60] for r in c.execute("SELECT ts FROM events WHERE ts >= ? AND ts <= ?", (a, b))]
    spans.sort()
    out: List[List[float]] = []
    for s, e in spans:
        if out and s <= out[-1][1] + 120:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _night(d: date) -> Optional[tuple]:
    """(bed, wake) of the night after day d, as minutes after d's 6 pm, or None if it can't tell."""
    a = _midnight(d) + 18 * 3600
    spans = _busy(a, a + 21 * 3600)          # 6 pm to 3 pm the next day
    best = None
    for (s0, e0), (s1, _) in zip(spans, spans[1:]):
        gap = s1 - e0
        start = (e0 - a) / 60
        if gap >= 3 * 3600 and 120 <= start <= 720 and (best is None or gap > best[1] - best[0]):
            best = (e0, s1)
    if not best:
        return None
    return (best[0] - a) / 60, (best[1] - a) / 60


def schedule() -> dict:
    """Your usual sleep: {"bed": minutes after midnight, "wake": ..., "nights": how many it learned from}."""
    if _cache["val"] and time.time() - _cache["t"] < 1800:
        return _cache["val"]
    today = date.today()
    nights = [n for n in (_night(today - timedelta(days=i)) for i in range(1, 15)) if n]
    if len(nights) >= 3:
        bed = (statistics.median(n[0] for n in nights) + 18 * 60) % 1440
        wake = (statistics.median(n[1] for n in nights) + 18 * 60) % 1440
    else:
        bed, wake = DEFAULT_BED, DEFAULT_WAKE
    val = {"bed": round(bed), "wake": round(wake), "nights": len(nights), "learned": len(nights) >= 3}
    _cache.update(t=time.time(), val=val)
    return val


def _active(now: float, minutes: int = 15) -> bool:
    c = db.conn()
    row = c.execute("SELECT MAX(ts_end) FROM activity").fetchone()
    if row and row[0] and now - row[0] < minutes * 60:
        return True
    row = c.execute("SELECT MAX(ts) FROM events WHERE ts <= ?", (now,)).fetchone()
    if row and row[0] and now - row[0] < minutes * 60:
        return True
    try:
        from . import system
        return system.idle_seconds() < minutes * 60 and not system.screen_locked()
    except Exception:
        return False


def in_sleep_window(m: float, sch: dict) -> bool:
    bed, wake = sch["bed"], sch["wake"]
    return bed <= m < wake if bed < wake else (m >= bed or m < wake)


def asleep(now: Optional[float] = None) -> bool:
    now = now or time.time()
    dt = datetime.fromtimestamp(now)
    return in_sleep_window(dt.hour * 60 + dt.minute, schedule()) and not _active(now)


def now_state() -> dict:
    """Everything the app, friends' cards and the egg need right now."""
    sch = schedule()
    return {"asleep": asleep(), "bed": _clock(sch["bed"]), "wake": _clock(sch["wake"]), "learned": sch["learned"],
            "nights": sch["nights"]}


def device_plan() -> dict:
    """For the egg's offline mode (no internet, or LazyTrack closed): your sleep window in minutes after
    midnight. "m"/"m2" (meals) stay as empty lists so eggs with older firmware just see no meals."""
    sch = schedule()
    return {"day": date.today().isoformat(), "bed": sch["bed"], "wake": sch["wake"], "m": [], "m2": []}
