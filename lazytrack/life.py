"""Mochi's life: HP that carries over from day to day, targets, deaths and babies.

How it works
- A pet starts at 100 HP. Every night the finished day is "settled": its score is added to
  HP and HP goes into the next day. At 0 HP the pet dies and a baby hatches (the next
  generation, back at 100 HP). The goal: keep one pet alive as long as you can.
- Every tracked thing is scored against YOUR target, not a fixed number, so it's fair for a
  heavy smoker and a light one alike:
    things to do less of (cigarettes, YouTube minutes...): exactly on target = 0 HP,
      under = heals, over = hurts.  score = W * (target - today) / target, between -1.5W and +W
      (a target of 0 means quitting: none = +W/2, each one -W/2)
    things to do more of (gym, water, coding...): measured over the last 7 days against
      the goal, so a weekly goal (gym 3x a week) works the same as a daily one.
      score = W * (min(done / goal, 1.25) - 0.4): nothing = -0.4W, goal hit = +0.6W
  W is how much the thing matters: small 3, medium 6, big 10 HP.
- A day can take at most 25 HP and give at most 15, and every day the pet heals 2 by
  itself, so one terrible day never kills a healthy pet (it takes 4+ in a row).
- Days with no data at all (the computer was off, nothing logged) only get the +2.
- Targets for things to cut down get 10% tighter every week you stay under them on 5+
  days (if "auto" is on), so you slowly work your way down.
- No target yet (a new habit and no history): it is "learning" for 7 days and doesn't
  count; then the target becomes your average of those days.

Stored in the kv table: "life" (the pet's state + a log of settled days) and "rules"
(target/goal, per day/week, size, auto, on) per tracked thing.
"""
from __future__ import annotations

import json
import math
import threading
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import db, habits, summary

SIZES = {"small": 3, "medium": 6, "big": 10}
REGEN = 2            # HP a pet heals every day by itself
MAX_LOSS = 25        # the most one day can take
MAX_GAIN = 15        # the most one day can give
LEARN_DAYS = 7
_lock = threading.Lock()
_test_hp: Optional[float] = None

# things from the screen that can count for HP: minutes (or commits), and which way is good
SCREEN_SCORED = {
    "coding": {"bad": False, "unit": "min"}, "commits": {"bad": False, "unit": "count"},
    "youtube": {"bad": True, "unit": "min"}, "social": {"bad": True, "unit": "min"},
    "streaming": {"bad": True, "unit": "min"}, "gaming": {"bad": True, "unit": "min"},
    "browsing": {"bad": True, "unit": "min"}, "screen": {"bad": True, "unit": "min"},
}
SCREEN_ON_BY_DEFAULT = {"coding", "commits", "screen"}
SCREEN_NAMES = {"coding": "Coding", "commits": "Commits", "youtube": "YouTube", "social": "Social and chat", "streaming": "Streaming",
                "gaming": "Gaming", "browsing": "Other websites", "screen": "Total screen time"}
SCREEN_EMOJI = {"coding": "💻", "commits": "🐙", "youtube": "▶️", "social": "💬", "streaming": "🎬", "gaming": "🎮", "browsing": "🌐", "screen": "⏱️"}

# sensible starting goals for the good stuff: (goal, per)
GOALS = {"gym": (3, "week"), "walk": (5, "week"), "water": (6, "day"), "healthy_meal": (2, "day"), "reading": (4, "week"),
         "meditation": (4, "week"), "stretch": (4, "week"), "coding": (60, "day"), "commits": (2, "day")}
DEFAULT_SIZE = {"cigarette": "big", "joint": "big", "vape": "medium", "alcohol": "medium", "junk_food": "medium",
                "energy_drink": "small", "sweets": "small", "coffee": "small", "gym": "big", "walk": "medium", "water": "small",
                "healthy_meal": "medium", "reading": "small", "meditation": "small", "stretch": "small",
                "coding": "medium", "commits": "small", "screen": "small", "youtube": "small", "social": "small",
                "streaming": "small", "gaming": "small", "browsing": "small"}


# ---------------------------------------------------------------- what's tracked, and its numbers
def trackables() -> List[dict]:
    """Everything that can count for HP: the chosen habits + the scorable screen stats."""
    h = habits.get()
    out = [{"k": x["k"], "n": x["n"], "e": x.get("e", ""), "bad": bool(x["bad"]), "unit": "count",
            "size": DEFAULT_SIZE.get(x["k"]) or ("big" if abs(x.get("hp", 6)) >= 10 else "small" if abs(x.get("hp", 6)) <= 3 else "medium")}
           for x in h["habits"]]
    for k in h["screen"]:
        if k in SCREEN_SCORED:
            out.append({"k": k, "n": SCREEN_NAMES[k], "e": SCREEN_EMOJI[k], "bad": SCREEN_SCORED[k]["bad"],
                        "unit": SCREEN_SCORED[k]["unit"], "size": DEFAULT_SIZE.get(k, "small"), "screen": True})
    return out


def _value(r: dict, k: str) -> float:
    """A thing's amount in a summary.compute() result: counts, or minutes for screen time."""
    if k == "commits":
        return r["commits"]
    if k == "screen":
        return r["screen_total"] / 60
    if k in SCREEN_SCORED:
        return r["categories"].get(k, 0) / 60
    return r["events"].get(k, 0)


def _day(d: date) -> dict:
    s = summary.day_start(d)
    return summary.compute(s, s + timedelta(days=1))


def _has_data(r: dict) -> bool:
    # feeding Mochi doesn't make a day "tracked" (meals are scored on their own)
    return bool(r["screen_total"] or any(k != "feed" for k in r["events"]) or r["commits"])


# ---------------------------------------------------------------- rules (targets and goals)
def _load(key: str, default):
    try:
        v = json.loads(db.kv_get(key) or "null")
    except ValueError:
        v = None
    return default if v is None else v


def suggestions(days: int = 14) -> Dict[str, dict]:
    """Your normal, from the last `days` days with data: a starting target (less of) or goal (more of)."""
    rows = [r for r in (_day(date.today() - timedelta(days=i)) for i in range(1, days + 1)) if _has_data(r)]
    out = {}
    for t in trackables():
        avg = sum(_value(r, t["k"]) for r in rows) / len(rows) if rows else None
        if t["bad"]:
            tgt = None if avg is None else (_round_min(avg) if t["unit"] == "min" else int(round(avg)))
            out[t["k"]] = {"target": tgt, "avg": None if avg is None else round(avg, 1), "days": len(rows)}
        else:
            goal, per = GOALS.get(t["k"], (1, "day"))
            if avg is not None and per == "day" and avg > goal:
                goal = _round_min(avg) if t["unit"] == "min" else int(round(avg))
            out[t["k"]] = {"goal": goal, "per": per, "avg": None if avg is None else round(avg, 1), "days": len(rows)}
    return out


def _round_min(m: float) -> int:
    return int(5 * round(m / 5))


def rules() -> Dict[str, dict]:
    """The rule for every tracked thing, filling in defaults for new ones."""
    saved = _load("rules", {})
    sug = None
    out = {}
    for t in trackables():
        r = dict(saved.get(t["k"]) or {})
        if not r:
            sug = sug or suggestions()
            s = sug.get(t["k"], {})
            r = {"size": t["size"], "on": t["k"] not in SCREEN_SCORED or t["k"] in SCREEN_ON_BY_DEFAULT}
            if t["bad"]:
                r.update(target=s.get("target"), auto=True)
            else:
                r.update(goal=s.get("goal", 1), per=s.get("per", "day"))
        r.setdefault("size", t["size"])
        r.setdefault("on", True)
        out[t["k"]] = r
    return out


def save_rules(body: dict) -> Dict[str, dict]:
    """body: {key: {target|goal, per, size, auto, on}}. Unknown keys and bad values are ignored."""
    cur = rules()
    known = {t["k"]: t for t in trackables()}
    for k, v in (body or {}).items():
        if k not in known or not isinstance(v, dict):
            continue
        r = cur[k]
        if v.get("size") in SIZES:
            r["size"] = v["size"]
        if "on" in v:
            r["on"] = bool(v["on"])
        if known[k]["bad"]:
            if "target" in v:
                r["target"] = None if v["target"] in (None, "") else max(0, min(100000, _num(v["target"], r.get("target") or 0)))
            if "auto" in v:
                r["auto"] = bool(v["auto"])
        else:
            if "goal" in v:
                r["goal"] = max(1, min(100000, _num(v["goal"], r.get("goal") or 1)))
            if v.get("per") in ("day", "week"):
                r["per"] = v["per"]
        r["set_by_you"] = True
    db.kv_set("rules", json.dumps(cur))
    db.kv_set("rules_confirmed", "1")
    return cur


def _num(v, default):
    try:
        x = float(v)
        return int(x) if x == int(x) else round(x, 1)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- scoring
def score(t: dict, r: dict, v: float, week: float) -> dict:
    """One thing's HP for a day. v = that day's amount, week = the last 7 days' total (good ones)."""
    W = SIZES.get(r.get("size"), 6)
    if not r.get("on", True):
        return {"d": 0.0, "off": True}
    if t["bad"]:
        T = r.get("target")
        if T is None:
            return {"d": 0.0, "learning": True}
        if T > 0:
            d = W * max(-1.5, min(1.0, (T - v) / T))
            step = 15 if t["unit"] == "min" else 1          # what one more costs: a unit, or 15 minutes
            nxt = W * max(-1.5, min(1.0, (T - v - step) / T)) - d
        else:
            # quitting: a clean day is +W/2 (not the full W: never doing it shouldn't be free HP), each one -W/2
            d = 0.5 * W if v <= 0 else max(-1.5 * W, -0.5 * W * (v / (15 if t["unit"] == "min" else 1)))
            nxt = max(-1.5 * W, -0.5 * W * ((v + (15 if t["unit"] == "min" else 1)) / (15 if t["unit"] == "min" else 1))) - d
        return {"d": d, "next": nxt}
    goal = r.get("goal") or 1
    need = goal if r.get("per") == "week" else goal * 7
    ratio = week / need
    d = W * (min(ratio, 1.25) - 0.4)
    one = 15 if t["unit"] == "min" else 1
    gain = W * (min((week + one) / need, 1.25) - 0.4) - d
    return {"d": d, "gain": gain, "done": round(ratio, 2)}


def day_score(d: date, rs: Optional[dict] = None, until: Optional[datetime] = None) -> dict:
    """The score of day d (up to `until` for today): {"delta", "raw", "data", "parts": [...]}."""
    rs = rs or rules()
    s = summary.day_start(d)
    end = until or s + timedelta(days=1)
    today = summary.compute(s, end)
    week = summary.compute(s - timedelta(days=6), end)
    parts, total = [], 0.0
    for t in trackables():
        r = rs.get(t["k"], {})
        v, wk = _value(today, t["k"]), _value(week, t["k"])
        sc = score(t, r, v, wk)
        total += sc["d"]
        parts.append({"k": t["k"], "n": t["n"], "e": t["e"], "bad": t["bad"], "unit": t["unit"], "v": round(v, 1), "week": round(wk, 1),
                      "target": r.get("target"), "goal": r.get("goal"), "per": r.get("per"), "size": r.get("size"),
                      **{k: (round(x, 1) if isinstance(x, float) else x) for k, x in sc.items()}})
    data = _has_data(today)
    raw = total + REGEN if data else REGEN
    try:   # meals: fed +1, missed -3 (rhythm.py); they count even on days without other data
        from . import rhythm
        meals = rhythm.day_part(d, until)
    except Exception:
        meals = None
    if meals:
        raw += meals["d"]
    return {"delta": max(-MAX_LOSS, min(MAX_GAIN, raw)), "raw": round(raw, 1), "data": data, "parts": parts, "meals": meals}


# ---------------------------------------------------------------- the pet's life
def _new_pet(gen: int, born: date) -> dict:
    return {"gen": gen, "born": born.isoformat(), "hp": 100.0}


def _state() -> dict:
    st = _load("life", None)
    if not st:
        st = {**_new_pet(1, date.today()), "settled": (date.today() - timedelta(days=1)).isoformat(),
              "deaths": [], "log": [], "best": {"days": 0, "gen": 1}, "started": date.today().isoformat()}
        db.kv_set("life", json.dumps(st))
    return st


def _tighten(st: dict, rs: dict, sunday: date) -> List[str]:
    """Every week you stay under a target on 5+ days, it gets ~10% tighter."""
    out = []
    days = [_day(sunday - timedelta(days=i)) for i in range(7)]
    days = [r for r in days if _has_data(r)]
    if len(days) < 5:
        return out
    for t in trackables():
        r = rs.get(t["k"]) or {}
        T = r.get("target")
        if not t["bad"] or not r.get("auto", True) or not r.get("on", True) or not T:
            continue
        if sum(1 for x in days if _value(x, t["k"]) <= T) >= 5:
            new = max(0, _round_min(T * 0.9) if t["unit"] == "min" else min(T - 1, int(math.floor(T * 0.9))))
            if t["unit"] == "min":
                new = min(new, T - 5) if T >= 5 else 0
            r["target"] = max(0, new)
            out.append(f"{t['n']}: target {T} -> {r['target']}")
    return out


def _learn(rs: dict, st: dict) -> bool:
    """Habits still learning (no target) get one once there are 7 days of data."""
    changed = False
    for t in trackables():
        r = rs.get(t["k"]) or {}
        if t["bad"] and r.get("on", True) and r.get("target") is None:
            sug = suggestions(LEARN_DAYS).get(t["k"], {})
            if sug.get("days", 0) >= LEARN_DAYS and sug.get("target") is not None:
                r["target"] = sug["target"]
                changed = True
    return changed


def catch_up() -> dict:
    """Settles every finished day since the last time (at most 60), handling deaths."""
    with _lock:
        st = _state()
        last = date.fromisoformat(st["settled"])
        yesterday = date.today() - timedelta(days=1)
        if last >= yesterday:
            return st
        rs = rules()
        d = max(last + timedelta(days=1), yesterday - timedelta(days=59))
        changed_rules = False
        while d <= yesterday:
            sc = day_score(d, rs)
            st["hp"] = min(100.0, st["hp"] + sc["delta"])
            st["log"].append({"day": d.isoformat(), "gen": st["gen"], "delta": round(sc["delta"], 1), "hp": round(max(0, st["hp"]), 1),
                              "parts": {**{p["k"]: p["d"] for p in sc["parts"] if p.get("d")},
                                        **({"meals": sc["meals"]["d"]} if sc.get("meals") and sc["meals"]["d"] else {})}})
            if d.weekday() == 6:
                notes = _tighten(st, rs, d)
                if notes:
                    st["log"][-1]["tightened"] = notes
                    changed_rules = True
            if st["hp"] <= 0:
                age = (d - date.fromisoformat(st["born"])).days + 1
                st["deaths"].append({"gen": st["gen"], "born": st["born"], "died": d.isoformat(), "days": age,
                                     "look": habits.get()["pet"], "cause": _cause(st), "seen": False})
                if age > st["best"]["days"]:
                    st["best"] = {"days": age, "gen": st["gen"]}
                st.update(_new_pet(st["gen"] + 1, d + timedelta(days=1)))
            d += timedelta(days=1)
        st["settled"] = yesterday.isoformat()
        st["log"] = st["log"][-120:]
        if _learn(rs, st) or changed_rules:
            db.kv_set("rules", json.dumps(rs))
        db.kv_set("life", json.dumps(st))
        return st


def _cause(st: dict) -> dict:
    """What hurt most over the last 3 days."""
    hurt: Dict[str, float] = {}
    for e in st["log"][-3:]:
        for k, v in e["parts"].items():
            if v < 0:
                hurt[k] = hurt.get(k, 0) + v
    if not hurt:
        return {"k": "", "n": "a few rough days"}
    k = min(hurt, key=hurt.get)
    t = next((x for x in trackables() if x["k"] == k), {"n": k, "e": ""})
    return {"k": k, "n": t["n"], "e": t.get("e", "")}


def status() -> dict:
    """Everything the app shows: the pet, today's live score, the record and the targets."""
    st = catch_up()
    rs = rules()
    today = day_score(date.today(), rs, until=datetime.now())
    age = (date.today() - date.fromisoformat(st["born"])).days
    now = max(0.0, min(100.0, st["hp"] + today["delta"]))
    displayed_start = _test_hp if _test_hp is not None else st["hp"]
    displayed_now = _test_hp if _test_hp is not None else now
    best = st["best"] if st["best"]["days"] >= age else {"days": age, "gen": st["gen"]}
    unseen = next((x for x in reversed(st["deaths"]) if not x.get("seen")), None)
    return {
        "gen": st["gen"], "born": st["born"], "age": age, "baby": age < 3,
        "hp_start": round(displayed_start, 1), # HP at the start of today
        "hp": round(displayed_now),            # with today's score so far
        "test_hp": _test_hp,
        "today": today, "critical": displayed_now < 20, "dying": displayed_now <= 0,
        "best": best, "deaths": len(st["deaths"]), "graveyard": st["deaths"][-10:], "unseen_death": unseen,
        "log": st["log"][-30:], "rules": rs, "confirmed": db.kv_get("rules_confirmed") == "1",
        "limits": {"regen": REGEN, "max_loss": MAX_LOSS, "max_gain": MAX_GAIN, "sizes": SIZES},
        "rhythm": _rhythm(),
    }


def set_test_hp(value: object) -> float:
    """Temporarily replace the displayed HP without changing the saved life state."""
    global _test_hp
    try:
        hp = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError("HP must be a number from 0 to 100.") from e
    if not math.isfinite(hp) or hp < 0 or hp > 100:
        raise ValueError("HP must be a number from 0 to 100.")
    with _lock:
        _test_hp = hp
    return hp


def reset_test_hp() -> None:
    """Return the dashboard to the real saved HP."""
    global _test_hp
    with _lock:
        _test_hp = None


def _rhythm() -> Optional[dict]:
    try:
        from . import rhythm
        return rhythm.now_state()
    except Exception:
        return None


def mark_seen() -> None:
    with _lock:
        st = _state()
        for x in st["deaths"]:
            x["seen"] = True
        db.kv_set("life", json.dumps(st))


def logged_day(day: str) -> Optional[dict]:
    """The settled result of a past day (for looking back at old days)."""
    st = _state()
    return next((e for e in reversed(st["log"]) if e["day"] == day), None)
