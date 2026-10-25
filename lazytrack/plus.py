"""Mochi Plus: a license key bought through Lemon Squeezy unlocks the Plus wardrobe.

Nothing that's free today becomes Plus. The key is checked with Lemon Squeezy when you enter it and then
about once a day; if the internet is gone, Plus keeps working for 14 days since the last good check.
"""
from __future__ import annotations

import json
import logging
import platform
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import db

log = logging.getLogger(__name__)

# Fill these in once the Lemon Squeezy product exists. Empty checkout = the buy button says "coming soon".
# Plus (the subscription) starts later; its test-mode checkout was https://mochipage.lemonsqueezy.com/checkout/buy/173b5880-15c9-4f38-8c9b-71cd0030e2a8
CHECKOUT_URL = ""
FOUNDER_URL = ""      # the one-time "Founder" pack (Plus forever); empty = not offered
EGG_URL = ""          # the $5 refundable Egg reservation (a Stripe Payment Link, not Lemon Squeezy: it bans physical goods); empty = not offered
SELLING = False       # False while the Lemon Squeezy store is still in test mode: the buy buttons show but can't be pressed
STORE_ID = 501362   # only keys from this shop unlock Plus

API = "https://api.lemonsqueezy.com/v1/licenses/"
GRACE = 14 * 86400    # offline grace after the last good check
RECHECK = 86400       # check again once a day
_lock = threading.Lock()


class PlusError(Exception):
    pass


def _call(action: str, fields: dict) -> dict:
    req = urllib.request.Request(API + action, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:   # Lemon Squeezy answers bad keys with 4xx + a JSON body
        try:
            return json.loads(e.read() or b"{}")
        except ValueError:
            raise PlusError(f"The license server said HTTP {e.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise PlusError("Can't reach the license server. Check your internet.") from None


def _store_ok(j: dict) -> bool:
    return not STORE_ID or int((j.get("meta") or {}).get("store_id") or 0) == STORE_ID


def _mask(key: str) -> str:
    return ("•••• " + key[-4:]) if len(key) >= 4 else ""


def status() -> dict:
    key = db.kv_get("plus_key") or ""
    ok_at = float(db.kv_get("plus_ok_at") or 0)
    active = bool(key) and time.time() - ok_at < GRACE
    if key and time.time() - ok_at > RECHECK:
        threading.Thread(target=_recheck, daemon=True).start()
    return {"active": active, "key": _mask(key), "checkout": CHECKOUT_URL, "founder": FOUNDER_URL, "egg": EGG_URL, "selling": SELLING,
            "checked": ok_at or None, "problem": db.kv_get("plus_problem") or ""}


def active() -> bool:
    return status()["active"]


def _recheck() -> None:
    if not _lock.acquire(blocking=False):
        return
    try:
        key, inst = db.kv_get("plus_key") or "", db.kv_get("plus_instance") or ""
        if not key:
            return
        try:
            j = _call("validate", {"license_key": key, "instance_id": inst} if inst else {"license_key": key})
        except PlusError:
            return   # offline: the grace period covers it
        if j.get("valid") and _store_ok(j):
            db.kv_set("plus_ok_at", str(time.time()))
            db.kv_set("plus_problem", "")
        else:   # refunded, disabled or expired: Plus stops now
            db.kv_set("plus_ok_at", "0")
            db.kv_set("plus_problem", str(j.get("error") or "This key isn't valid any more."))
    finally:
        _lock.release()


def activate(key: str) -> dict:
    key = (key or "").strip()
    if not 8 <= len(key) <= 100:
        raise PlusError("That doesn't look like a license key. Copy it from your receipt email.")
    j = _call("activate", {"license_key": key, "instance_name": (platform.node() or "Mochi")[:60]})
    if not j.get("activated") or not _store_ok(j):
        raise PlusError(str(j.get("error") or "That key didn't work.")[:200])
    db.kv_set("plus_key", key)
    db.kv_set("plus_instance", str((j.get("instance") or {}).get("id") or ""))
    db.kv_set("plus_ok_at", str(time.time()))
    db.kv_set("plus_problem", "")
    log.info("Plus activated")
    return status()


def deactivate() -> dict:
    """Frees this computer's activation, so the key can be used on another one."""
    key, inst = db.kv_get("plus_key") or "", db.kv_get("plus_instance") or ""
    if key and inst:
        try:
            _call("deactivate", {"license_key": key, "instance_id": inst})
        except PlusError:
            pass
    for k in ("plus_key", "plus_instance", "plus_ok_at", "plus_problem"):
        db.kv_set(k, "")
    return status()
