"""Mochi's day: he learns when you sleep, and gets hungry up to 3 times a day at sensible times.

Sleep
- Learned from the last 14 nights of activity (computer, phone, anything logged): each night's sleep is the
  longest break of 3+ hours that starts between 8 pm and 6 am. Bedtime and wake-up are the typical (median)
  times. Until 3 nights are known: 00:30 to 08:30.
- Mochi sleeps inside that window, but never while you're up: anything on the computer (or a log) in the last
  15 minutes keeps him awake. Late night gaming? He's up with you.

Meals (at most 3 a day, never while you sleep)
- Breakfast about an hour after you wake up (7:00-11:00), lunch around the middle of your day (11:30-14:30),
  dinner about 3.5 hours before bed (17:30-21:30). A meal that doesn't fit your awake hours that day is skipped
  (wake up at 1 pm: no breakfast).
- From the meal time he's hungry for up to 3 hours (until the next meal or midnight at the latest). Feed him in the app, on the phone
  or on the egg. Fed: +1 HP. Missed: -3 HP. So ignoring Mochi for days slowly costs HP (about -7 a day with the
  +2 he heals), while a normal day of feeding him costs nothing.
- Each day's meal times are fixed the first time that day is looked at, so learning more later never changes
  a day that already happened. Meals count from the day this came in (kv "meals_since"), not before.
"""
from __future__ import annotations

import json
import statistics
import threading
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import db

FED_HP, MISSED_HP = 1.0, -3.0
HUNGRY_HOURS = 3
DEFAULT_BED, DEFAULT_WAKE = 30, 8 * 60 + 30          # minutes after midnight
MEALS = [   # name, emoji, earliest, latest (minutes after midnight)
    ("breakfast", "🥞", 7 * 60, 11 * 60),
    ("lunch", "🍜", 11 * 60 + 30, 14 * 60 + 30),
    ("dinner", "🍛", 17 * 60 + 30, 21 * 60 + 30),
]
_lock = threading.Lock()
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


# ---------------------------------------------------------------- meals
def _plan_for(sch: dict) -> List[tuple]:
    """[(name, minutes after midnight)] for a day with this sleep schedule."""
    wake, bed = sch["wake"], sch["bed"]
    end = bed if bed > wake else bed + 1440                 # bedtime, counted on the same day
    want = {"breakfast": wake + 60, "lunch": wake + (end - wake) * 0.42, "dinner": end - 210}
    out = []
    for name, _, lo, hi in MEALS:
        t = min(hi, max(lo, want[name]))
        if wake + 30 <= t <= end - 60:
            out.append((name, round(t)))
    return out


def plan(d: date) -> List[dict]:
    """The meals of day d: [{"n", "e", "start", "end"}] (timestamps). Fixed once made."""
    key = d.isoformat()
    with _lock:
        plans = json.loads(db.kv_get("meal_plans") or "{}")
        if key not in plans:
            plans[key] = [[n, m] for n, m in _plan_for(schedule())]
            keep = (d - timedelta(days=21)).isoformat()
            plans = {k: v for k, v in plans.items() if k >= keep}
            db.kv_set("meal_plans", json.dumps(plans))
        rows = plans[key]
    base = _midnight(d)
    emoji = {n: e for n, e, _, _ in MEALS}
    out = []
    for i, (n, m) in enumerate(rows):
        start = base + m * 60
        end = min(start + HUNGRY_HOURS * 3600, base + 86400)   # a day's meals are over at midnight
        if i + 1 < len(rows):
            end = min(end, base + rows[i + 1][1] * 60)
        out.append({"n": n, "e": emoji.get(n, "🍙"), "start": start, "end": end})
    return out


def since() -> date:
    v = db.kv_get("meals_since")
    if not v:
        v = date.today().isoformat()
        db.kv_set("meals_since", v)
    return date.fromisoformat(v)


def meals(d: date, now: Optional[float] = None) -> List[dict]:
    """Day d's meals with their state: later / hungry / fed / missed."""
    now = now or time.time()
    c = db.conn()
    out = []
    for m in plan(d):
        fed = c.execute("SELECT MIN(ts) FROM events WHERE kind='feed' AND ts >= ? AND ts < ?", (m["start"], m["end"])).fetchone()[0]
        state = "fed" if fed else "later" if now < m["start"] else "hungry" if now < m["end"] else "missed"
        out.append({**m, "state": state, "fed": fed, "at": datetime.fromtimestamp(m["start"]).strftime("%H:%M")})
    return out


def day_part(d: date, until: Optional[datetime] = None) -> Optional[dict]:
    """What the meals of day d did to HP (so far, for today). None before meals came in."""
    if d < since():
        return None
    now = until.timestamp() if until else _midnight(d) + 86400
    ms = meals(d, now)
    fed = sum(1 for m in ms if m["state"] == "fed")
    missed = sum(1 for m in ms if m["state"] == "missed")
    return {"d": fed * FED_HP + missed * MISSED_HP, "fed": fed, "missed": missed, "planned": len(ms)}


def hungry(now: Optional[float] = None) -> Optional[dict]:
    now = now or time.time()
    return next((m for m in meals(date.today(), now) if m["state"] == "hungry"), None)


def feed(device: str = "app", source: str = "manual", ts: Optional[float] = None) -> dict:
    """Feeds Mochi if he's hungry. {"ok", "meal"} or {"ok": False, "why": "full"}."""
    ts = ts or time.time()
    m = hungry(ts)
    if not m:
        nxt = next((x for x in meals(date.today(), ts) if x["state"] == "later"), None)
        return {"ok": False, "why": "full", "next": nxt["at"] if nxt else None}
    db.add_event("feed", device, source=source, ts=ts)
    return {"ok": True, "meal": m["n"]}


def now_state() -> dict:
    """Everything the app, friends' cards and the egg need right now."""
    sch = schedule()
    t = time.time()
    ms = meals(date.today(), t)
    h = next((m for m in ms if m["state"] == "hungry"), None)
    return {"asleep": asleep(t), "bed": _clock(sch["bed"]), "wake": _clock(sch["wake"]), "learned": sch["learned"],
            "nights": sch["nights"], "hungry": h["n"] if h else None, "hungry_until": h["end"] if h else None,
            "meals": [{"n": m["n"], "e": m["e"], "at": m["at"], "state": m["state"]} for m in ms],
            "fed_hp": FED_HP, "missed_hp": MISSED_HP}


def device_plan() -> dict:
    """For the egg's offline mode (no internet, or LazyTrack closed): today's and tomorrow's meals and your sleep
    window, so it can keep Mochi's day going on its own clock. Meals: [letter, start, end (minutes after
    midnight, your time), fed 0/1]; bed/wake in minutes after midnight."""
    sch = schedule()
    t = time.time()
    today = date.today()

    def rows(d: date) -> list:
        base = _midnight(d)
        return [[m["n"][0], int((m["start"] - base) // 60), int((m["end"] - base) // 60), 1 if m["state"] == "fed" else 0]
                for m in meals(d, t)]
    return {"day": today.isoformat(), "bed": sch["bed"], "wake": sch["wake"], "m": rows(today), "m2": rows(today + timedelta(days=1))}


def tick(notify) -> None:
    """Every ~30 s (from the sync worker): when a meal starts, tells you once (notification + sound)."""
    since()
    m = hungry()
    if not m or asleep():
        return
    key = f"{date.today().isoformat()}:{m['n']}"
    if db.kv_get("meal_told") == key:
        return
    db.kv_set("meal_told", key)
    notify(m)
