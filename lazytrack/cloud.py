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


def push_card(card: dict) -> bool:
    """Upload your card. Skipped if nothing changed and it went up less than 2 minutes ago."""
    body = json.dumps(card, sort_keys=True)
    if body == _last_card["body"] and time.time() - _last_card["t"] < 120:
        return False
    uid = state()["uid"]
    _http("POST", "/rest/v1/cards", {"user_id": uid, "card": card, "updated_at": _now()}, token=_token(),
          prefer="resolution=merge-duplicates,return=minimal")
    _last_card.update(t=time.time(), body=body)
    return True


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
                "card": c.get("card"), "updated_at": c.get("updated_at"), "unread": unread.get(uid, 0)}

    out = {"me": person(me), "friends": [], "incoming": [], "outgoing": []}
    for r in rows:
        who = {**person(other[r["id"]]), "id": r["id"]}
        if r["status"] == "accepted":
            out["friends"].append(who)
        elif r["addressee"] == me:
            out["incoming"].append(who)
        else:
            out["outgoing"].append(who)
    out["friends"].sort(key=lambda f: (-f["unread"], f["display_name"].lower()))
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


def _uuid(s: str) -> str:
    s = str(s).strip().lower()
    if len(s) != 36 or any(ch not in "0123456789abcdef-" for ch in s):
        raise CloudError("bad friend id")
    return s
