"""Friends: talks to the shared server (Supabase) so LazyTracks on different computers can
find each other, share a profile card and chat.

Everything goes through this file: the dashboard only ever talks to your own LazyTrack,
and your LazyTrack talks to Supabase. Your detailed data never leaves your computer; only
the card (Mochi, HP and the stats you choose to share) and your chat messages go up.

Supabase pieces used (all plain HTTPS + JSON, no extra libraries):
  /auth/v1/...   logging in (email + password), refreshing the login
  /rest/v1/...   the tables made by the SQL script: profiles, cards, friendships, messages
The privacy rules live in the database itself (row level security), so even a modified
app can't read someone's card or messages unless they're friends.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from . import config, db

log = logging.getLogger(__name__)
_lock = threading.Lock()


class CloudError(Exception):
    pass


def _cfg():
    c = config.load().get("cloud", {})
    return c.get("url", "").rstrip("/"), c.get("key", "")


def _http(method: str, path: str, body=None, token: Optional[str] = None, prefer: str = "") -> object:
    url, key = _cfg()
    if not url or not key:
        raise CloudError("The friends server isn't set up.")
    headers = {"apikey": key, "Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if prefer:
        headers["Prefer"] = prefer
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read() or b"{}")
        except ValueError:
            j = {}
        msg = j.get("msg") or j.get("message") or j.get("error_description") or j.get("error") or f"HTTP {e.code}"
        code = j.get("error_code") or j.get("code") or ""
        raise CloudError(_friendly(str(msg), str(code), e.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CloudError("Can't reach the friends server. Check your internet.") from None


def _friendly(msg: str, code: str, status: int) -> str:
    m = msg.lower()
    if "invalid login" in m or code == "invalid_credentials":
        return "That email and password don't match."
    if code == "email_exists" or "already been registered" in m:
        return "That email is already used by another account."
    if "already registered" in m or code == "user_already_exists":
        return "That email already has an account. Log in instead."
    if "email" in m and ("invalid" in m or code == "email_address_invalid"):
        return "That email doesn't look right."
    if "password" in m and ("weak" in m or "least" in m or code == "weak_password"):
        return "The password is too weak for the friends server. Use at least 6 characters."
    if "rate limit" in m or status == 429:
        return "Too many tries. Wait a minute and try again."
    if "not confirmed" in m or code == "email_not_confirmed":
        return "Confirm your email first (check your inbox), then log in."
    if "row-level security" in m or status == 403:
        return "Only friends can do that. Add them first."
    if "jwt" in m and "expired" in m:
        return "Your friends login expired. Connect again."
    return msg[:200]


# ---------------------------------------------------------------- login

def state() -> dict:
    uid = db.kv_get("cloud_uid")
    return {"connected": bool(uid), "uid": uid, "username": db.kv_get("cloud_username"), "email": db.kv_get("cloud_email")}


def _store(sess: dict, email: str) -> None:
    user = sess.get("user") or {}
    meta = user.get("user_metadata") or {}
    db.kv_set("cloud_access", sess["access_token"])
    db.kv_set("cloud_refresh", sess["refresh_token"])
    db.kv_set("cloud_expires", time.time() + float(sess.get("expires_in", 3600)) - 60)
    db.kv_set("cloud_uid", user.get("id"))
    db.kv_set("cloud_email", email)
    if meta.get("username"):
        db.kv_set("cloud_username", meta["username"])


def _token() -> str:
    """A valid access token, refreshed when it's about to run out."""
    with _lock:
        tok = db.kv_get("cloud_access")
        if not tok:
            raise CloudError("Not connected to friends yet.")
        if time.time() < float(db.kv_get("cloud_expires") or 0):
            return tok
        sess = _http("POST", "/auth/v1/token?grant_type=refresh_token", {"refresh_token": db.kv_get("cloud_refresh")})
        _store(sess, db.kv_get("cloud_email") or "")
        return sess["access_token"]


def connect(email: str, password: str, username: str, display_name: str, new: bool) -> dict:
    """new=True makes the account on the friends server, new=False logs into an existing one."""
    email = email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise CloudError("That email doesn't look right.")
    if new:
        username = username.strip()
        if _http("POST", "/rest/v1/rpc/username_taken", {"name": username}):
            raise CloudError(f"Someone already has the name @{username} on the friends server. Pick another one.")
        sess = _http("POST", "/auth/v1/signup", {"email": email, "password": password,
                                                  "data": {"username": username, "display_name": display_name or username}})
        if not sess or not sess.get("access_token"):
            raise CloudError("Almost: the friends server wants the email confirmed. Turn off \"Confirm email\" in Supabase, "
                             "or click the link in your inbox, then log in here.")
    else:
        sess = _http("POST", "/auth/v1/token?grant_type=password", {"email": email, "password": password})
    _store(sess, email)
    me = _http("GET", f"/rest/v1/profiles?id=eq.{sess['user']['id']}&select=username,display_name", token=sess["access_token"])
    if me:
        db.kv_set("cloud_username", me[0]["username"])
        db.kv_set("cloud_display", me[0]["display_name"])
    return state()


def update_account(username: str = None, display_name: str = None, email: str = None, password: str = None) -> str:
    """Changes the account on the friends server. Returns a note for the user ("" if nothing to say)."""
    tok, me = _token(), state()["uid"]
    note = ""
    if username is not None and username.lower() != (db.kv_get("cloud_username") or "").lower():
        if _http("POST", "/rest/v1/rpc/username_taken", {"name": username}):
            raise CloudError(f"Someone already has the username @{username}. Pick another one.")
        _http("PATCH", f"/rest/v1/profiles?id=eq.{me}", {"username": username}, token=tok, prefer="return=minimal")
        db.kv_set("cloud_username", username)
    if display_name is not None:
        _http("PATCH", f"/rest/v1/profiles?id=eq.{me}", {"display_name": display_name}, token=tok, prefer="return=minimal")
    change = {}
    if email is not None and email.lower() != (db.kv_get("cloud_email") or ""):
        change["email"] = email.lower()
    if password is not None:
        change["password"] = password
    if username is not None or display_name is not None:
        change["data"] = {k: v for k, v in (("username", username), ("display_name", display_name)) if v is not None}
    if change:
        user = _http("PUT", "/auth/v1/user", change, token=tok) or {}
        if "email" in change:
            if (user.get("email") or "").lower() == change["email"]:
                db.kv_set("cloud_email", change["email"])
            else:
                note = "Check your inbox: the friends server sent a link to confirm the new email."
    return note


def disconnect() -> None:
    for k in ("cloud_access", "cloud_refresh", "cloud_expires", "cloud_uid", "cloud_username", "cloud_email"):
        db.kv_set(k, None)


# ---------------------------------------------------------------- the card friends see

_last_card = {"t": 0.0, "body": ""}


def _doing() -> dict:
    """What you're doing right now (a category like "coding"), fresh from the screen tracker."""
    try:
        from .activity import NOW
    except Exception:
        return {}
    if not NOW["what"] or time.time() - NOW["t"] > 90:
        return {"what": "away", "since": NOW["since"] or time.time()}
    out = {"what": NOW["what"], "since": NOW["since"]}
    if NOW.get("label") and NOW["what"] != "away":
        out["label"] = NOW["label"]
    return out


def pet_fx(now: Optional[float] = None) -> dict:
    """How Mochi is doing right now: high (weed), drunk (alcohol), coughy (cigarettes), 0-3 each.
    The same rules as the app (petState in dashboard.html), worked out here from the logs, so friends
    and the egg see it live even while LazyTrack's window is closed. They wear off with time."""
    import datetime as _dt
    now = now or time.time()
    midnight = _dt.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    c = db.conn()
    def count(k):
        return c.execute("SELECT COUNT(*) FROM events WHERE kind=? AND ts>=? AND ts<=?", (k, midnight, now)).fetchone()[0]
    def hours_since(k):
        row = c.execute("SELECT MAX(ts) FROM events WHERE kind=? AND ts<=?", (k, now)).fetchone()
        return (now - row[0]) / 3600 if row and row[0] else None
    joints, drinks, cigs = count("joint"), count("alcohol"), count("cigarette")
    high = min(3, joints)
    drunk = 3 if drinks >= 5 else 2 if drinks >= 3 else 1 if drinks else 0
    hj, hd, hc = hours_since("joint"), hours_since("alcohol"), hours_since("cigarette")
    if high:
        # weed wears off within an hour of the last joint: full for 40 min, a bit less until the hour, then sober
        high = 0 if hj is None else high if hj < 2 / 3 else max(1, high - 1) if hj < 1 else 0
    if drunk:
        drunk = max(0, drunk - 1) if hd is None else drunk if hd < 5 else drunk - 1 if hd < 8 else 0
    coughy = 0 if not cigs else 3 if cigs >= 10 else 2 if cigs >= 6 else 1 if (hc is not None and hc < 0.25) else 1 if cigs >= 4 else 0
    return {"high": high, "drunk": drunk, "coughy": coughy}


def _with_now(card: dict) -> dict:
    card = {k: v for k, v in card.items() if k not in ("now", "offline")}
    if card.get("share_now", True):
        card["now"] = _doing()
        if not card.get("share_detail", True):      # only the kind of thing (gaming), not which game
            card["now"].pop("label", None)
    try:   # always fresh, even when the card itself was made a while ago
        card["fx"] = pet_fx()
        card["built"] = int(time.time())
    except Exception as e:
        log.debug("pet fx failed: %s", e)
    try:   # sleeping (when you sleep, never while you're up): for friends and the egg
        card["asleep"] = _rhythm()
        from . import rhythm
        card["plan"] = rhythm.device_plan()   # the egg keeps Mochi's day going offline
        if card["asleep"]:
            card["mood"] = "sleepy"
        elif card.get("mood") == "sleepy":
            card["mood"] = "happy"
    except Exception as e:
        log.debug("rhythm failed: %s", e)
    return card


def _rhythm() -> bool:
    from . import rhythm
    return rhythm.asleep()


def push_card(card: dict) -> bool:
    """Upload your card. Skipped if nothing changed and it went up less than 2 minutes ago."""
    try:   # the ⭐ next to your name: set here from your real license, not by the page
        from . import plus
        card["plus"] = bool(plus.active())
    except Exception:
        card.pop("plus", None)
    db.kv_set("cloud_last_card", json.dumps(card, sort_keys=True))
    card = _with_now(card)
    body = json.dumps(card, sort_keys=True)
    if body == _last_card["body"] and time.time() - _last_card["t"] < 120:
        return False
    uid = state()["uid"]
    _http("POST", "/rest/v1/cards", {"user_id": uid, "card": card, "updated_at": _now()}, token=_token(),
          prefer="resolution=merge-duplicates,return=minimal")
    d = _doing()
    _last_card.update(t=time.time(), body=body, what=d.get("what"), label=d.get("label"), fx=card.get("fx"),
                      rh=card.get("asleep"))
    return True


ONLINE_SECONDS = 300   # a friend whose LazyTrack checked in within 5 minutes shows as online


def heartbeat() -> None:
    """While LazyTrack runs (window open or not), re-send your last card every ~2 minutes, so friends
    see you as online. The card's own "built" time tells them how fresh Mochi's mood is."""
    if not state()["connected"]:
        return
    try:
        fx = pet_fx()
    except Exception:
        fx = None
    # started doing something else, or Mochi got drunk/high/sober: tell friends (and the egg) now
    d = _doing()
    try:
        rh = _rhythm()
    except Exception:
        rh = None
    changed = ((d.get("what"), d.get("label")) != (_last_card.get("what"), _last_card.get("label")) or (fx is not None and fx != _last_card.get("fx"))
               or (rh is not None and rh != _last_card.get("rh")))
    if time.time() - _last_card["t"] < (45 if changed else 120):
        return
    body = db.kv_get("cloud_last_card")
    if not body:
        return
    try:
        card = _with_now(json.loads(body))
        _http("POST", "/rest/v1/cards", {"user_id": state()["uid"], "card": card, "updated_at": _now()},
              token=_token(), prefer="resolution=merge-duplicates,return=minimal")
        _last_card.update(t=time.time(), body=json.dumps(card, sort_keys=True), what=d.get("what"),
                          label=d.get("label"), fx=card.get("fx"), rh=card.get("asleep"))
    except (CloudError, ValueError) as e:
        log.debug("heartbeat failed: %s", e)


def go_offline() -> None:
    """LazyTrack is quitting: friends see you offline right away (not 5 minutes later)."""
    if not state()["connected"]:
        return
    body = db.kv_get("cloud_last_card")
    if not body:
        return
    try:
        card = {k: v for k, v in json.loads(body).items() if k != "now"}
        card["offline"] = True
        _http("POST", "/rest/v1/cards", {"user_id": state()["uid"], "card": card, "updated_at": _now()},
              token=_token(), prefer="resolution=merge-duplicates,return=minimal")
    except (CloudError, ValueError) as e:
        log.debug("going offline failed: %s", e)


def _online(updated_at) -> bool:
    if not updated_at:
        return False
    try:
        from datetime import datetime, timezone
        t = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() < ONLINE_SECONDS
    except ValueError:
        return False


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _in(ids) -> str:
    return "in.(" + ",".join(ids) + ")"


# ---------------------------------------------------------------- friends

def friends() -> dict:
    tok, me = _token(), state()["uid"]
    rows = _http("GET", "/rest/v1/friendships?select=id,requester,addressee,status,created_at", token=tok) or []
    other = {r["id"]: (r["addressee"] if r["requester"] == me else r["requester"]) for r in rows}
    ids = sorted(set(other.values()) | {me})
    profs = {p["id"]: p for p in _http("GET", f"/rest/v1/profiles?id={_in(ids)}&select=id,username,display_name", token=tok) or []}
    cards = {c["user_id"]: c for c in _http("GET", f"/rest/v1/cards?user_id={_in(ids)}&select=user_id,card,updated_at", token=tok) or []}
    unread = {}
    for m in _http("GET", f"/rest/v1/messages?receiver=eq.{me}&read_at=is.null&select=sender", token=tok) or []:
        unread[m["sender"]] = unread.get(m["sender"], 0) + 1

    def person(uid):
        p = profs.get(uid, {"username": "?", "display_name": "?"})
        c = cards.get(uid) or {}
        return {"uid": uid, "username": p["username"], "display_name": p["display_name"] or p["username"],
                "card": c.get("card"), "updated_at": c.get("updated_at"), "online": _online(c.get("updated_at")) and not (c.get("card") or {}).get("offline"),
                "unread": unread.get(uid, 0)}

    out = {"me": person(me), "friends": [], "incoming": [], "outgoing": []}
    for r in rows:
        who = {**person(other[r["id"]]), "id": r["id"]}
        if r["status"] == "accepted":
            out["friends"].append(who)
        elif r["addressee"] == me:
            out["incoming"].append(who)
        else:
            out["outgoing"].append(who)
    out["friends"].sort(key=lambda f: (-f["unread"], not f["online"], f["display_name"].lower()))
    return out


def request(username: str) -> str:
    """Send a friend request by username. If they already asked you, this accepts it."""
    username = username.strip().lstrip("@")
    if not username:
        raise CloudError("Type their username.")
    tok, me = _token(), state()["uid"]
    found = _http("GET", "/rest/v1/profiles?username=ilike." + urllib.parse.quote(username) + "&select=id,username", token=tok) or []
    them = next((p for p in found if p["username"].lower() == username.lower()), None)
    if not them:
        raise CloudError(f"No one called @{username} yet. Check the spelling, or ask them to connect to friends first.")
    if them["id"] == me:
        raise CloudError("That's you!")
    rows = _http("GET", f"/rest/v1/friendships?select=id,requester,addressee,status", token=tok) or []
    for r in rows:
        if {r["requester"], r["addressee"]} == {me, them["id"]}:
            if r["status"] == "accepted":
                return f"You and @{them['username']} are already friends."
            if r["addressee"] == me:
                accept(r["id"])
                return f"@{them['username']} had already asked you, so you're friends now."
            return f"Request already sent. Waiting for @{them['username']}."
    _http("POST", "/rest/v1/friendships", {"requester": me, "addressee": them["id"]}, token=tok, prefer="return=minimal")
    return f"Request sent to @{them['username']}."


def accept(fid: int) -> None:
    _http("PATCH", f"/rest/v1/friendships?id=eq.{int(fid)}", {"status": "accepted"}, token=_token(), prefer="return=minimal")


def remove(fid: int) -> None:
    _http("DELETE", f"/rest/v1/friendships?id=eq.{int(fid)}", token=_token(), prefer="return=minimal")


# ---------------------------------------------------------------- chat

def messages(other: str, after: int = 0) -> list:
    tok, me = _token(), state()["uid"]
    other = _uuid(other)
    q = (f"/rest/v1/messages?or=(and(sender.eq.{me},receiver.eq.{other}),and(sender.eq.{other},receiver.eq.{me}))"
         f"&id=gt.{int(after)}&order=id.desc&limit=100&select=id,sender,receiver,body,created_at,read_at")
    rows = list(reversed(_http("GET", q, token=tok) or []))
    if any(r["receiver"] == me and not r["read_at"] for r in rows):
        _http("PATCH", f"/rest/v1/messages?sender=eq.{other}&receiver=eq.{me}&read_at=is.null", {"read_at": _now()},
              token=tok, prefer="return=minimal")
    for r in rows:
        r["mine"] = r["sender"] == me
    return rows


def send(other: str, body: str) -> None:
    body = body.strip()
    if not body:
        raise CloudError("Type a message first.")
    _http("POST", "/rest/v1/messages", {"sender": state()["uid"], "receiver": _uuid(other), "body": body[:1000]},
          token=_token(), prefer="return=minimal")


def unread() -> int:
    """Unread messages + friend requests waiting for you (the badge on the Friends tab)."""
    if not state()["connected"]:
        return 0
    tok, me = _token(), state()["uid"]
    msgs = _http("GET", f"/rest/v1/messages?receiver=eq.{me}&read_at=is.null&select=id", token=tok) or []
    reqs = _http("GET", f"/rest/v1/friendships?addressee=eq.{me}&status=eq.pending&select=id", token=tok) or []
    return len(msgs) + len(reqs)


def unread_detail() -> dict:
    """Like unread(), split up, so the app can play the right sound for each."""
    if not state()["connected"]:
        return {"count": 0, "messages": 0, "requests": 0}
    tok, me = _token(), state()["uid"]
    msgs = _http("GET", f"/rest/v1/messages?receiver=eq.{me}&read_at=is.null&select=id", token=tok) or []
    reqs = _http("GET", f"/rest/v1/friendships?addressee=eq.{me}&status=eq.pending&select=id", token=tok) or []
    return {"count": len(msgs) + len(reqs), "messages": len(msgs), "requests": len(reqs), "pokes": new_pokes()}


def _uuid(s: str) -> str:
    s = str(s).strip().lower()
    if len(s) != 36 or any(ch not in "0123456789abcdef-" for ch in s):
        raise CloudError("bad friend id")
    return s


# ---------------------------------------------------------------- ideas box (the + button)
# Ideas go to the "ideas" table (supabase/ideas.sql). If they can't be sent right now (offline, not
# connected, the table isn't set up yet) they wait in the local outbox and go out with the next one.
IDEA_AREAS = {"mochi", "habits", "tracking", "friends", "other",
              # bug reports share the box: "bug-<where>"
              "bug-app", "bug-mochi", "bug-tracking", "bug-friends", "bug-egg", "bug-other"}
IDEA_STATUS = {"new", "planned", "done", "no"}


def _outbox() -> list:
    try:
        return json.loads(db.kv_get("ideas_outbox") or "[]")
    except ValueError:
        return []


def submit_idea(title: str, body: str, area: str, want: int, platform: str, version: str) -> dict:
    """Sends an idea (and any waiting ones). Returns {"sent": n, "waiting": n}."""
    title, body = str(title).strip()[:120], str(body).strip()[:2000]
    if len(title) < 3:
        raise CloudError("Write at least 3 letters in the first box.")
    idea = {"title": title, "body": body, "area": area if area in IDEA_AREAS else "other",
            "want": max(1, min(3, int(want or 2))), "platform": str(platform)[:20], "version": str(version)[:12]}
    waiting = _outbox() + [idea]
    sent = 0
    try:
        tok = _token()
        while waiting:
            _http("POST", "/rest/v1/ideas", waiting[0], token=tok, prefer="return=minimal")
            waiting.pop(0)
            sent += 1
    except CloudError:
        pass
    db.kv_set("ideas_outbox", json.dumps(waiting[-50:]))
    return {"sent": sent, "waiting": len(waiting)}


def ideas() -> dict:
    """Your own ideas (with their status), or every idea if you're the admin."""
    if not state()["connected"]:
        return {"admin": False, "ideas": [], "waiting": len(_outbox())}
    tok = _token()
    try:
        admin = bool(_http("POST", "/rest/v1/rpc/is_admin", {}, token=tok))
        rows = _http("GET", "/rest/v1/ideas?select=id,title,body,area,want,platform,version,status,created_at,user_id,profiles(username)"
                            "&order=created_at.desc&limit=300", token=tok) or []
    except CloudError as e:
        if "could not find" in str(e).lower() or "does not exist" in str(e).lower():
            return {"admin": False, "ideas": [], "waiting": len(_outbox()), "missing": True}
        raise
    me = state()["uid"]
    for r in rows:
        r["username"] = (r.pop("profiles", None) or {}).get("username") or ""
        r["mine"] = r.get("user_id") == me
    return {"admin": admin, "ideas": rows, "waiting": len(_outbox())}


def set_idea_status(idea_id: int, status: str) -> None:
    if status not in IDEA_STATUS:
        raise CloudError("unknown status")
    _http("PATCH", f"/rest/v1/ideas?id=eq.{int(idea_id)}", {"status": status}, token=_token(), prefer="return=minimal")


# ---------------------------------------------------------------- admin page + announcements
# Set up by supabase/admin.sql. The database decides who's an admin (the admins table); the
# app only asks, so a modified app still can't read anything an admin can.
_admin = {"t": 0.0, "uid": "", "v": False, "why": ""}
_news = {"t": 0.0, "v": []}
NEWS_KINDS = {"info", "new", "warn"}


def _missing(e: Exception) -> bool:
    s = str(e).lower()
    return "could not find" in s or "does not exist" in s or "schema cache" in s


def admin_status(fresh: bool = False) -> dict:
    """{"admin": bool, "why": ""|"not_connected"|"setup_missing"|"not_admin"|"offline"}.
    A yes is kept for 10 minutes, a no for 1 minute (so running admin.sql shows up quickly)."""
    st = state()
    if not st["connected"]:
        return {"admin": False, "why": "not_connected"}
    keep = 600 if _admin["v"] else 60
    if not fresh and _admin["uid"] == st["uid"] and time.time() - _admin["t"] < keep:
        return {"admin": _admin["v"], "why": _admin["why"]}
    try:
        v = bool(_http("POST", "/rest/v1/rpc/is_admin", {}, token=_token()))
        why = "" if v else "not_admin"
    except CloudError as e:
        if not _missing(e):
            return {"admin": _admin["v"] if _admin["uid"] == st["uid"] else False, "why": "offline"}
        v, why = False, "setup_missing"
    _admin.update(t=time.time(), uid=st["uid"], v=v, why=why)
    return {"admin": v, "why": why}


def is_admin(fresh: bool = False) -> bool:
    return admin_status(fresh)["admin"]


def _need_admin() -> str:
    if not is_admin():
        raise CloudError("Only the admin can do that.")
    return _token()


def admin_overview() -> dict:
    tok = _need_admin()
    return {"stats": _http("POST", "/rest/v1/rpc/admin_stats", {}, token=tok) or {},
            "users": _http("POST", "/rest/v1/rpc/admin_users", {}, token=tok) or []}


def news() -> list:
    """The live announcements for Home (kept for 10 minutes)."""
    if not state()["connected"]:
        return []
    if time.time() - _news["t"] < 600:
        return _news["v"]
    try:
        v = _http("GET", "/rest/v1/announcements?select=id,body,kind,created_at&active=is.true&order=created_at.desc&limit=3",
                  token=_token()) or []
    except CloudError as e:
        if not _missing(e):
            return _news["v"]
        v = []
    _news.update(t=time.time(), v=v)
    return v


def admin_news() -> list:
    return _http("GET", "/rest/v1/announcements?select=id,body,kind,active,created_at&order=created_at.desc&limit=50",
                 token=_need_admin()) or []


def post_news(body: str, kind: str) -> None:
    body = str(body).strip()[:280]
    if len(body) < 3:
        raise CloudError("Write a few words first.")
    _http("POST", "/rest/v1/announcements", {"body": body, "kind": kind if kind in NEWS_KINDS else "info"},
          token=_need_admin(), prefer="return=minimal")
    _news["t"] = 0


def set_news(news_id: int, active: bool) -> None:
    _http("PATCH", f"/rest/v1/announcements?id=eq.{int(news_id)}", {"active": bool(active)}, token=_need_admin(), prefer="return=minimal")
    _news["t"] = 0


def delete_news(news_id: int) -> None:
    _http("DELETE", f"/rest/v1/announcements?id=eq.{int(news_id)}", token=_need_admin(), prefer="return=minimal")
    _news["t"] = 0


def delete_idea(idea_id: int) -> None:
    _http("DELETE", f"/rest/v1/ideas?id=eq.{int(idea_id)}", token=_need_admin(), prefer="return=minimal")


# ---------------------------------------------------------------- the egg + pokes (supabase/egg.sql)
_egg = {"setup": "", "missing_until": 0.0}
POKE_KINDS = {"poke", "love", "wave"}


def _egg_ready() -> bool:
    return state()["connected"] and time.time() > _egg["missing_until"]


def _egg_missing(e: Exception) -> bool:
    """egg.sql not run yet: stay quiet and try again in 10 minutes."""
    if _missing(e):
        _egg["missing_until"] = time.time() + 600
        return True
    return False


def egg_sync() -> int:
    """Every ~30 s: tells the egg which habits it can log, and picks up what was logged on it.
    Returns how many logs came in."""
    if not _egg_ready():
        return 0
    from . import habits as _habits
    tok = _token()
    try:
        def short(h):   # fits the egg's list: "glass of water", not "reading session" (-> "Reading")
            one = h.get("one") or ""
            return (one if one and len(one) <= 14 else (h.get("n") or h["k"]))[:14]
        all_h = _habits.get()["habits"]
        hs = [{"k": h["k"], "n": short(h), "bad": bool(h.get("bad"))} for h in all_h][:16]
        today = _egg_today(all_h)
        body = json.dumps([hs, today], sort_keys=True)
        if body != _egg["setup"]:
            _http("POST", "/rest/v1/egg_setup", {"user_id": state()["uid"], "habits": {"list": hs, "today": today},
                                                 "updated_at": _now()},
                  token=tok, prefer="resolution=merge-duplicates,return=minimal")
            _egg["setup"] = body
        rows = _http("GET", "/rest/v1/egg_inbox?select=id,kind,ts&order=id.asc&limit=50", token=tok) or []
    except CloudError as e:
        if not _egg_missing(e):
            log.debug("egg sync failed: %s", e)
        return 0
    if not rows:
        return 0
    import datetime as _dt
    known, n = set(_habits.keys()), 0   # anything else (like "feed" from older egg firmware) is dropped
    for r in rows:
        if r.get("kind") not in known:
            continue
        try:
            ts = _dt.datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = time.time()
        db.add_event(r["kind"], "egg", source="egg", ts=min(ts, time.time()))
        n += 1
    _http("DELETE", f"/rest/v1/egg_inbox?id=in.({','.join(str(int(r['id'])) for r in rows)})", token=tok, prefer="return=minimal")
    if n:
        _last_card["t"] = 0.0      # Mochi changed (high, drunk...): friends and the egg get it on the next heartbeat
        log.info("egg: %d log(s) came in", n)
    return n


def _egg_today(all_h: list) -> list:
    """Everything about today for the egg's Today screen (private, unlike the friend card):
    every habit you track, plus screen time, each with yesterday's value ("y") for the egg's
    Yesterday vs today screen. Seconds are rounded to minutes so it doesn't re-upload every 30 seconds."""
    from datetime import date, timedelta
    from . import summary
    s0 = summary.day_start(date.today())
    today = _egg_stats(all_h, summary.today())
    yday = {e["s"]: e["v"] for e in _egg_stats(all_h, summary.compute(s0 - timedelta(days=1), s0))}
    for e in today:
        e["y"] = yday.get(e["s"], 0)
    return today


def _egg_stats(all_h: list, t: dict) -> list:
    from . import habits as _habits
    ev, cats = t["events"], t["categories"]
    out = []
    for h in all_h:
        name = (h.get("n") or h["k"])[:12]
        out.append({"s": name, "v": int(ev.get(h["k"], 0)), "bad": bool(h.get("bad"))})
    def mins(v):
        return int((v or 0) // 60) * 60
    # the same screen stats you picked for Home in the app, always shown (0 too), in that order
    names = {"coding": "Coding", "youtube": "YouTube", "social": "Socials", "streaming": "Streaming", "gaming": "Gaming",
             "browsing": "Web", "screen_pc": "PC screen", "screen_phone": "Phone", "screen": "Screen", "commits": "Commits"}
    got = _habits.get()
    for k in got["screen"]:
        if k not in names:
            continue
        if k == "commits":
            out.append({"s": names[k], "v": int(t["commits"] or 0)})
        elif k in ("screen_pc", "screen_phone"):
            out.append({"s": names[k], "v": mins(t[k]), "u": "s"})
        elif k == "screen":
            out.append({"s": names[k], "v": mins(t["screen_total"]), "u": "s"})
        else:
            out.append({"s": names[k], "v": mins(cats.get(k, 0)), "u": "s", "bad": k in ("youtube", "social", "streaming")})
    if got["modules"].get("school"):
        out.append({"s": "42 logtime", "v": mins(t["logtime_42"]), "u": "s"})
    if t["steps_total"]:
        out.append({"s": "Steps", "v": int(t["steps_total"])})
    if t["sleep_nights"]:
        out.append({"s": "Sleep", "v": mins(t["sleep_avg"]), "u": "s"})
    return out[:24]


def poke(other: str, kind: str = "poke") -> None:
    kind = kind if kind in POKE_KINDS else "poke"
    try:
        _http("POST", "/rest/v1/pokes", {"sender": state()["uid"], "receiver": _uuid(other), "kind": kind},
              token=_token(), prefer="return=minimal")
    except CloudError as e:
        if _missing(e):
            raise CloudError("Pokes need one more setup step: run supabase/egg.sql in Supabase.") from None
        if "row-level security" in str(e).lower():
            raise CloudError("Easy! You can poke again in a minute.") from None
        raise


def new_pokes() -> list:
    """Pokes the app hasn't shown yet: [{"id", "from", "kind"}], marked seen for the app (the egg has its own mark)."""
    if not _egg_ready():
        return []
    tok, me = _token(), state()["uid"]
    try:
        rows = _http("GET", f"/rest/v1/pokes?receiver=eq.{me}&seen_app=is.null&order=id.asc&limit=20&select=id,sender,kind,created_at",
                     token=tok) or []
    except CloudError as e:
        _egg_missing(e)
        return []
    if not rows:
        return []
    ids = sorted({r["sender"] for r in rows})
    profs = {p["id"]: p for p in _http("GET", f"/rest/v1/profiles?id={_in(ids)}&select=id,username,display_name", token=tok) or []}
    _http("PATCH", f"/rest/v1/pokes?id=in.({','.join(str(int(r['id'])) for r in rows)})", {"seen_app": _now()},
          token=tok, prefer="return=minimal")
    out = []
    for r in rows:
        p = profs.get(r["sender"], {})
        out.append({"id": r["id"], "from": p.get("display_name") or p.get("username") or "A friend", "kind": r["kind"]})
    return out


# ---------------------------------------------------------------- your egg, from the computer / phone (supabase/egg_status.sql)
EGG_CMDS = {"sing", "find", "sync"}


def egg_status() -> dict:
    """What your egg last said about itself: {"ok", "setup", "egg": {...} or None, "age": seconds since it checked in}."""
    if not state()["connected"]:
        return {"ok": False, "setup": True, "egg": None, "why": "Connect to friends first (Friends tab)."}
    try:
        rows = _http("GET", "/rest/v1/egg_status?select=*", token=_token()) or []
    except CloudError as e:
        if _missing(e):
            return {"ok": False, "setup": False, "egg": None, "why": "Run supabase/egg_status.sql in Supabase once."}
        return {"ok": False, "setup": True, "egg": None, "why": str(e)}
    if not rows:
        return {"ok": True, "setup": True, "egg": None, "age": None}
    import datetime as _dt
    r = rows[0]
    try:
        seen = _dt.datetime.fromisoformat(str(r.get("seen_at")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        seen = 0
    return {"ok": True, "setup": True, "egg": r, "age": max(0, int(time.time() - seen)) if seen else None}


def egg_command(cmd: str) -> None:
    if cmd not in EGG_CMDS:
        raise CloudError("Unknown egg command.")
    try:
        _http("POST", "/rest/v1/egg_commands", {"user_id": state()["uid"], "cmd": cmd}, token=_token(), prefer="return=minimal")
    except CloudError as e:
        if _missing(e):
            raise CloudError("This needs one more setup step: run supabase/egg_status.sql in Supabase.") from None
        if "row-level security" in str(e).lower():
            raise CloudError("Your egg has a few commands waiting already. Give it a moment.") from None
        raise
