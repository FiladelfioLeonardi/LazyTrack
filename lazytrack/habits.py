"""Which habits this LazyTrack tracks. Stored as a JSON list in the kv table, so every
person picks their own: built-in ones from CATALOG, or custom ones they make up.

A habit is a dict:
  k     key stored in events.kind ("cigarette", or "c_cold_shower" for custom ones)
  n     name for lists ("Cigarettes")       one  name for one of them ("cigarette")
  s     short label for the stat sheet      e    emoji
  bad   True = you want less of it          hp   HP per log (negative for bad ones)
  cap   most HP it can give in a day (good habits only)
  smoke True for things smoke-free hours heal (cigarette, joint, vape)
  fill  color token for its button          main True = has a button on Home without "More"
  custom True when the user made it up

Removing a habit only takes it off the list. Its old logs stay in the database, so
adding it back brings the history back too.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List

from . import db

CATALOG: List[dict] = [
    # less of
    {"k": "cigarette", "n": "Cigarettes", "one": "cigarette", "s": "CIG", "e": "🚬", "bad": True, "hp": -8, "smoke": True, "fill": "--pink"},
    {"k": "joint", "n": "Joints", "one": "joint", "s": "WEED", "e": "🌿", "bad": True, "hp": -10, "smoke": True, "fill": "--green"},
    {"k": "vape", "n": "Vaping", "one": "vape break", "s": "VAPE", "e": "💨", "bad": True, "hp": -5, "smoke": True, "fill": "--lilac"},
    {"k": "junk_food", "n": "Junk food", "one": "junk food", "s": "JUNK", "e": "🍔", "bad": True, "hp": -6, "fill": "--yellow"},
    {"k": "alcohol", "n": "Alcohol", "one": "drink", "s": "BOOZE", "e": "🍺", "bad": True, "hp": -6, "fill": "--orange"},
    {"k": "energy_drink", "n": "Energy drinks", "one": "energy drink", "s": "NRG", "e": "⚡", "bad": True, "hp": -4, "fill": "--lilac"},
    {"k": "sweets", "n": "Sweets", "one": "sweet snack", "s": "SUGAR", "e": "🍬", "bad": True, "hp": -3, "fill": "--pink"},
    {"k": "coffee", "n": "Coffee", "one": "coffee", "s": "COFFEE", "e": "☕", "bad": True, "hp": -2, "fill": "--orange"},
    # more of
    {"k": "gym", "n": "Gym", "one": "gym session", "s": "GYM", "e": "🏋️", "bad": False, "hp": 15, "cap": 15, "fill": "--blue"},
    {"k": "walk", "n": "Walks", "one": "walk", "s": "WALK", "e": "🚶", "bad": False, "hp": 5, "cap": 10, "fill": "--lime"},
    {"k": "water", "n": "Water", "one": "glass of water", "s": "H2O", "e": "💧", "bad": False, "hp": 1, "cap": 4, "fill": "--blue"},
    {"k": "healthy_meal", "n": "Healthy meals", "one": "healthy meal", "s": "MEAL", "e": "🥗", "bad": False, "hp": 4, "cap": 12, "fill": "--green"},
    {"k": "reading", "n": "Reading", "one": "reading session", "s": "READ", "e": "📖", "bad": False, "hp": 5, "cap": 10, "fill": "--yellow"},
    {"k": "meditation", "n": "Meditation", "one": "meditation", "s": "ZEN", "e": "🧘", "bad": False, "hp": 5, "cap": 10, "fill": "--lilac"},
    {"k": "stretch", "n": "Stretching", "one": "stretch", "s": "FLEX", "e": "🤸", "bad": False, "hp": 3, "cap": 6, "fill": "--lime"},
]
BY_KEY: Dict[str, dict] = {h["k"]: h for h in CATALOG}

# screen-time stats people can show in the stat sheet (tracked automatically, nothing to log)
SCREEN_STATS = [
    {"k": "coding", "s": "CODE", "n": "Coding"},
    {"k": "commits", "s": "GIT", "n": "Commits"},
    {"k": "youtube", "s": "YT", "n": "YouTube"},
    {"k": "social", "s": "SOCIAL", "n": "Social and chat"},
    {"k": "streaming", "s": "TV", "n": "Streaming"},
    {"k": "gaming", "s": "GAME", "n": "Gaming"},
    {"k": "browsing", "s": "WEB", "n": "Other websites"},
    {"k": "screen_pc", "s": "PC", "n": "Screen time on the computer"},
    {"k": "screen_phone", "s": "PHONE", "n": "Screen time on the phone"},
    {"k": "screen", "s": "SCREEN", "n": "Total screen time"},
]
SCREEN_KEYS = {s["k"] for s in SCREEN_STATS}

# what an install had before habits were modular: keeps old setups looking exactly the same
LEGACY = ["cigarette", "joint", "junk_food", "alcohol", "energy_drink", "gym", "walk", "water"]
LEGACY_MAIN = {"cigarette", "joint", "junk_food", "gym"}
LEGACY_SCREEN = ["coding", "commits", "youtube", "social", "screen_pc", "screen_phone", "screen"]
MODULES = {"school": False, "github": False}   # 42 deadlines / GitHub streak: off until you pick them

FILLS = ["--pink", "--green", "--yellow", "--blue", "--lilac", "--orange", "--lime"]
SIZES = {"small": 3, "medium": 6, "big": 10}
_HEX = re.compile(r"#[0-9a-fA-F]{6}")   # a pet color: #rrggbb


def _load(key: str, default):
    raw = db.kv_get(key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def get() -> dict:
    hs = _load("habits", None)
    if hs is None:
        hs = [{**BY_KEY[k], "main": k in LEGACY_MAIN} for k in LEGACY]
    return {
        "habits": hs,
        "screen": _load("screen_stats", LEGACY_SCREEN),
        "modules": {**_default_modules(), **_load("modules", {})},
        "share": _load("share", None),   # None = the default: good stuff shared, bad habits private
        "pet": _load("pet", {}),         # what Mochi wears: {"body": "cat", "hat": "beanie", ...}
        "catalog": CATALOG,
        "screen_catalog": SCREEN_STATS,
    }


def _default_modules() -> dict:
    """Before anyone picked: on only where it's already set up (older installs keep what they had)."""
    from . import config
    try:
        cfg = config.load()
    except Exception:
        return dict(MODULES)
    return {"school": bool((cfg.get("fortytwo") or {}).get("login")), "github": bool((cfg.get("github") or {}).get("username"))}


def keys() -> List[str]:
    return [h["k"] for h in get()["habits"]]


def allowed_kinds() -> set:
    """What /api/event accepts: the chosen habits, plus every built-in one, so the webcam
    and the phone app can still log things even if they're hidden right now."""
    return set(keys()) | set(BY_KEY)


def info(kind: str):
    for h in get()["habits"]:
        if h["k"] == kind:
            return h
    return BY_KEY.get(kind)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:24] or "habit"


def make_custom(d: dict, taken: set) -> dict:
    """Turns {"n": "Cold showers", "e": "🚿", "bad": false, "size": "medium"} into a full habit."""
    name = str(d.get("n", "")).strip()[:30]
    if not name:
        raise ValueError("a custom habit needs a name")
    bad = bool(d.get("bad"))
    size = SIZES.get(str(d.get("size", "medium")), 6)
    k = d.get("k") if str(d.get("k", "")).startswith("c_") else "c_" + _slug(name)
    base, i = k, 2
    while k in taken and not d.get("k"):
        k, i = f"{base}_{i}", i + 1
    words = re.findall(r"[A-Za-z0-9]+", name)
    letters = words[0].upper() if words else ""   # "Cold showers" -> COLD
    return {
        "k": k, "n": name, "one": str(d.get("one") or name).strip()[:30].lower(), "s": (str(d.get("s") or "").strip().upper() or letters[:6] or "HABIT")[:6],
        "e": (str(d.get("e") or "").strip() or ("❌" if bad else "⭐"))[:4], "bad": bad,
        "hp": -size if bad else size, **({} if bad else {"cap": size * 3}),
        "fill": d.get("fill") if d.get("fill") in FILLS else FILLS[len(taken) % len(FILLS)], "custom": True,
    }


def save(body: dict) -> dict:
    """body: {"habits": [key or custom dict, ...], "screen": [...], "modules": {...}}.
    Built-ins are picked by key; custom ones are sent as dicts."""
    if "habits" in body:
        out, taken = [], set()
        for item in body["habits"][:40]:
            if isinstance(item, str) and item in BY_KEY:
                h = dict(BY_KEY[item])
            elif isinstance(item, dict) and item.get("k") in BY_KEY:
                h = dict(BY_KEY[item["k"]])
            elif isinstance(item, dict):
                h = make_custom(item, taken)
            else:
                continue
            if h["k"] in taken:
                continue
            taken.add(h["k"])
            out.append(h)
        # buttons on Home without "More": up to two of each kind, then whatever is left
        bad, good = [h for h in out if h["bad"]], [h for h in out if not h["bad"]]
        main = {h["k"] for h in bad[:2] + good[:2]}
        for h in out:
            if len(main) >= 4:
                break
            main.add(h["k"])
        for h in out:
            h["main"] = h["k"] in main
        db.kv_set("habits", json.dumps(out))
    if "screen" in body:
        db.kv_set("screen_stats", json.dumps([k for k in body["screen"] if k in SCREEN_KEYS]))
    if "pet" in body and isinstance(body["pet"], dict):
        slots = ("body", "skin", "eyes", "mouth", "hat", "glasses", "neck", "outfit", "held", "place")
        colors = ("hatColor", "glassesColor", "neckColor", "outfitColor", "heldColor")
        pet = {k: str(v)[:24] for k, v in body["pet"].items() if k in slots}
        pet.update({k: v.lower() for k, v in body["pet"].items() if k in colors and isinstance(v, str) and _HEX.fullmatch(v)})
        db.kv_set("pet", json.dumps(pet))
    if "share" in body and isinstance(body["share"], list):
        db.kv_set("share", json.dumps([str(k)[:40] for k in body["share"]][:60]))
    if "modules" in body:
        db.kv_set("modules", json.dumps({k: bool(body["modules"].get(k, False)) for k in MODULES}))
    return get()
