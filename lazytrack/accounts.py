"""The LazyTrack account: email + username + password. The same account lives on the friends
server (see cloud.py), so you can log into it on other devices. On this computer it's
mirrored locally, so logging in here works even offline.

One account per install: the person whose computer it runs on. All the data that was
already there belongs to that account.

Passwords are never stored, only a scrypt hash (slow on purpose, so guessing is slow).
Logging in gives a random session token that lives in a cookie for a year, so you
log in once per device.

Later, the friends system will register this same account on a shared server.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Optional

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    pw_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    created REAL NOT NULL,
    setup_done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created REAL NOT NULL,
    last_seen REAL NOT NULL,
    agent TEXT NOT NULL DEFAULT ''
);
"""
SESSION_DAYS = 365
USERNAME = re.compile(r"^[A-Za-z0-9_.-]{3,20}$")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(ValueError):
    pass


def _c():
    c = db.conn()
    c.executescript(SCHEMA)
    if "email" not in {r[1] for r in c.execute("PRAGMA table_info(users)")}:   # added later
        c.execute("ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
        c.commit()
    return c


def _hash(pw: str, salt: str) -> str:
    return hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32).hex()


def has_account() -> bool:
    return _c().execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def has_data() -> bool:
    c = _c()
    return any(c.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone() for t in ("events", "activity", "commits"))


def public(row) -> dict:
    return {"username": row["username"], "display_name": row["display_name"], "email": row["email"],
            "setup_done": bool(row["setup_done"])}


def check_new(username: str, password: str, email: str = "") -> None:
    """The rules for a username, password and email. Raises AuthError with a friendly message."""
    if not USERNAME.match(username.strip()):
        raise AuthError("Username: 3 to 20 letters, numbers, dots, dashes or underscores.")
    if len(password) < 6:
        raise AuthError("Password: at least 6 characters.")
    if email and not EMAIL.match(email.strip()):
        raise AuthError("That email doesn't look right.")


def signup(username: str, password: str, display_name: str = "", email: str = "") -> str:
    username = username.strip()
    if has_account():
        raise AuthError("This Mochi already has an account. Log in instead.")
    check_new(username, password, email)
    salt = secrets.token_hex(16)
    c = _c()
    cur = c.execute("INSERT INTO users(username, display_name, pw_hash, salt, created, email) VALUES(?,?,?,?,?,?)",
                    (username, (display_name.strip() or username)[:30], _hash(password, salt), salt, time.time(),
                     email.strip().lower()))
    c.commit()
    return _new_session(cur.lastrowid)


def find(login: str):
    """The local account by username or email."""
    login = login.strip()
    return _c().execute("SELECT * FROM users WHERE username=? OR (email<>'' AND email=?)",
                        (login, login.lower())).fetchone()


def password_ok(row, password: str) -> bool:
    return bool(row) and hmac.compare_digest(_hash(password, row["salt"]), row["pw_hash"])


def login(login_name: str, password: str) -> str:
    row = find(login_name)
    if not password_ok(row, password):
        time.sleep(0.6)   # makes guessing slow
        raise AuthError("That username or email and password don't match. Try your email with the password you use for friends.")
    return _new_session(row["id"])


def update(user_id: int, username: str = None, display_name: str = None, email: str = None, password: str = None) -> None:
    c = _c()
    if username is not None:
        c.execute("UPDATE users SET username=? WHERE id=?", (username.strip(), user_id))
    if display_name is not None:
        c.execute("UPDATE users SET display_name=? WHERE id=?", ((display_name.strip() or username or "")[:30], user_id))
    if email is not None:
        c.execute("UPDATE users SET email=? WHERE id=?", (email.strip().lower(), user_id))
    if password is not None:
        salt = secrets.token_hex(16)
        c.execute("UPDATE users SET pw_hash=?, salt=? WHERE id=?", (_hash(password, salt), salt, user_id))
    c.commit()


def _new_session(user_id: int, agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    c = _c()
    now = time.time()
    c.execute("INSERT INTO sessions(token, user_id, created, last_seen, agent) VALUES(?,?,?,?,?)",
              (token, user_id, now, now, agent[:200]))
    c.commit()
    return token


def user_for(token: Optional[str]):
    if not token:
        return None
    c = _c()
    row = c.execute("SELECT u.*, s.last_seen FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token=?",
                    (token,)).fetchone()
    if not row or time.time() - row["last_seen"] > SESSION_DAYS * 86400:
        return None
    if time.time() - row["last_seen"] > 3600:
        c.execute("UPDATE sessions SET last_seen=? WHERE token=?", (time.time(), token))
        c.commit()
    return row


def owner():
    """The account of this LazyTrack (there's only one). Used for paired devices like your phone."""
    return _c().execute("SELECT * FROM users ORDER BY id LIMIT 1").fetchone()


def logout(token: str) -> None:
    c = _c()
    c.execute("DELETE FROM sessions WHERE token=?", (token,))
    c.commit()


def set_setup_done(user_id: int, done: bool = True) -> None:
    c = _c()
    c.execute("UPDATE users SET setup_done=? WHERE id=?", (1 if done else 0, user_id))
    c.commit()


def delete_account() -> None:
    """Removes the login only. Every log, screen time and setting stays."""
    c = _c()
    c.execute("DELETE FROM sessions")
    c.execute("DELETE FROM users")
    c.commit()
