"""Local web dashboard + JSON API (the phone app will talk to this API later)."""
from __future__ import annotations

import json
import sys
import logging
import threading
import time
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import accounts, cloud, config, db, detail, github, habits, life, settings, summary, system, updater
from .activity import categorize_phone

log = logging.getLogger(__name__)
HTML = Path(__file__).parent / "static" / "dashboard.html"
ALLOWED_METRICS = {"sleep", "steps"}


def make_handler(cfg: dict):
    token = cfg["dashboard"].get("token") or ""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet
            log.debug(fmt, *args)

        def _send(self, code: int, body, ctype="application/json", cookie: str = None):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if cookie is not None:   # "" logs out
                age = accounts.SESSION_DAYS * 86400 if cookie else 0
                self.send_header("Set-Cookie", f"lt_session={cookie}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax")
            self.end_headers()
            self.wfile.write(data)

        def _paired(self, qs) -> bool:
            """A paired device (the phone app, Shortcuts, the phone link) sends the token."""
            return bool(token) and (self.headers.get("X-Token") == token or qs.get("token", [""])[0] == token)

        def _session(self) -> str:
            raw = self.headers.get("Cookie") or ""
            try:
                m = SimpleCookie(raw).get("lt_session")
            except Exception:
                return ""
            return m.value if m else ""

        def _user(self, qs=None):
            """Who's asking: the logged-in session, or for a paired device (your phone) the
            owner of this LazyTrack, since there's one account per install."""
            u = accounts.user_for(self._session())
            if u is None and qs is not None and self._paired(qs):
                u = accounts.owner()
            return u

        def _authed(self, qs) -> bool:
            if self._paired(qs):
                return True
            if accounts.has_account():
                return self._user() is not None   # once there's an account, you log in
            # no account yet: works like before (the Mac itself is trusted)
            return not token or self._local()

        def _local(self) -> bool:
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return {}

        def do_GET(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            if u.path == "/icon.png":
                return self._send(200, (HTML.parent / "icon.png").read_bytes(), "image/png")
            if u.path in ("/", "/index.html"):   # the page itself shows the login screen
                return self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/ping":
                return self._send(200, {"ok": True})
            if u.path == "/api/auth/state":
                user = self._user(qs)
                return self._send(200, {
                    "has_account": accounts.has_account(), "has_data": accounts.has_data(),
                    "logged_in": user is not None or self._paired(qs), "paired": self._paired(qs),
                    "user": self._me(user) if user else None, "local": self._local(), "os": sys.platform,
                    "update": updater.status(config.load())})
            if not self._authed(qs):
                return self._send(401, {"error": "log in first"})
            if u.path == "/api/habits":
                return self._send(200, habits.get())
            if u.path == "/api/phone":
                if not self._local():
                    return self._send(403, {"error": "only on the computer itself"})
                from . import phone
                return self._send(200, phone.status(cfg))
            if u.path == "/api/life":
                return self._send(200, {**life.status(), "suggestions": life.suggestions()})
            if u.path.startswith("/api/cloud/"):
                return self._cloud_get(u.path, qs)
            if u.path == "/api/camera/status":
                from . import smoke
                live = {k: v for k, v in smoke.LIVE.items() if k not in ("frame",)}
                sc = config.load()["smoke"]
                return self._send(200, {**live, "enabled": bool(sc["enabled"]), "asked": bool(sc.get("asked")), "now": time.time(),
                                        "can_view": self._local(), "paused_until": smoke.paused_until()})
            if u.path == "/api/camera/question":
                from . import smoke
                if self._local() and qs.get("visible", [""])[0] == "1":
                    smoke.APP_SEEN["t"] = time.time()   # the app is on screen here: it asks, not the system popup
                q = smoke.QUESTION
                return self._send(200, {"open": q["open"], "id": q["id"], "source": q["source"], "asked_at": q["asked_at"],
                                        "text": smoke.QUESTION_TEXT, "buttons": smoke.BUTTONS,
                                        "last": smoke.last_smokes() if q["open"] else {}})
            if u.path == "/api/camera/frame.jpg":
                # the live view: only on the computer itself, camera pictures never go over the network
                if not self._local():
                    return self._send(403, {"error": "only on the computer itself"})
                from . import smoke
                smoke.LIVE["want_until"] = time.time() + 4
                fr = smoke.LIVE["frame"]
                if not fr or time.time() - smoke.LIVE["frame_t"] > 5:
                    return self._send(204, b"", "image/jpeg")
                return self._send(200, fr, "image/jpeg")
            if u.path == "/api/settings":
                if not self._local():
                    return self._send(403, {"error": "settings can only be changed on the Mac itself"})
                return self._send(200, settings.get(config.load()))
            if u.path == "/api/detail":
                kind = qs.get("kind", [""])[0]
                day = qs.get("day", [""])[0] or None
                if not kind or len(kind) > 40:
                    return self._send(400, {"error": "kind missing"})
                try:
                    return self._send(200, detail.detail(kind, day))
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
            if u.path == "/api/overview":
                day = qs.get("day", [""])[0] or None
                try:
                    sel = date.fromisoformat(day) if day else date.today()
                except ValueError:
                    return self._send(400, {"error": "bad day"})
                return self._send(200, {
                    "day": sel.isoformat(),
                    "is_today": sel == date.today(),
                    "today": summary.compute(summary.day_start(sel), summary.day_start(sel) + timedelta(days=1)),
                    "yesterday": summary.compute(summary.day_start(sel) - timedelta(days=1), summary.day_start(sel)),
                    "yesterday_by_now": summary.compute(summary.day_start(sel) - timedelta(days=1),
                                                        datetime.now() - timedelta(days=1)) if sel == date.today() else None,
                    "week": summary.weekly(0),
                    # last week only up to the same weekday + time, so a half-finished week compares fairly
                    "last_week": summary.compute(summary.week_start(-1),
                                                 summary.week_start(-1) + (datetime.now() - summary.week_start(0))),
                    "daily": summary.daily(14),
                    "ft": summary.ft_status(config.load()),
                    "github": github.status(),
                    "summary_text": summary.format_week(summary.weekly(0), summary.weekly(-1), summary.ft_status(config.load())),
                    "life": life.status(),
                    "life_day": life.logged_day(sel.isoformat()) if sel != date.today() else None,
                    "recent_events": [dict(r) for r in db.conn().execute(
                        "SELECT id, ts, device, kind, source FROM events ORDER BY ts DESC LIMIT 15")],
                    "now": time.time(),
                })
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            try:
                self._post()
            except (TypeError, ValueError, KeyError, AttributeError) as e:
                self._send(400, {"error": f"bad request: {e}"})

        def _post(self):
            u = urlparse(self.path)
            qs = parse_qs(u.query)
            b = self._body()
            if u.path.startswith("/api/auth/"):
                return self._auth(u.path, b)
            if u.path == "/api/restart" and self._local():   # ./lt phone restarts it from the Terminal
                self._send(200, {"ok": True})
                settings.restart_soon()
                return None
            if not self._authed(qs):
                return self._send(401, {"error": "log in first"})
            if u.path.startswith("/api/cloud/"):
                return self._cloud_post(u.path, b)
            if u.path == "/api/lab/system-popup":
                # the admin Test lab: the real Mac/Windows question, with your real "last cigarette" line; logs nothing
                if not self._local():
                    return self._send(403, {"error": "Only on the computer itself."})
                from . import smoke
                line = smoke.last_line()
                ans = system.ask(smoke.QUESTION_TEXT + (f"\n\n{line}" if line else ""), smoke.BUTTONS, "Cigarette", timeout=60)
                return self._send(200, {"answer": ans})
            if u.path == "/api/camera/pause":
                from . import smoke
                return self._send(200, {"ok": True, "until": smoke.pause(float(b.get("minutes", 60)))})
            if u.path == "/api/camera/answer":
                from . import smoke
                return self._send(200, {"ok": smoke.answer_question(int(b.get("id", 0)), str(b.get("answer", "")))})
            if u.path == "/api/update/check":
                return self._send(200, updater.check(config.load(), install=bool(b.get("install"))))
            if u.path == "/api/phone/enable":
                if not self._local():
                    return self._send(403, {"error": "only on the computer itself"})
                from . import phone
                restart = phone.enable()
                self._send(200, {"ok": True, "restarting": restart})
                if restart:
                    settings.restart_soon(1.0)   # start listening for the phone (with the token)
                return None
            if u.path == "/api/life/rules":
                life.save_rules(b.get("rules") or {})
                return self._send(200, life.status())
            if u.path == "/api/life/seen":
                life.mark_seen()
                return self._send(200, {"ok": True})
            if u.path in ("/api/habits", "/api/setup"):
                try:
                    out = habits.save(b)
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                user = self._user(qs)
                if u.path == "/api/setup" and user:
                    accounts.set_setup_done(user["id"])
                return self._send(200, out)
            if u.path in ("/api/settings", "/api/restart"):
                if not self._local():
                    return self._send(403, {"error": "settings can only be changed on the Mac itself"})
                if u.path == "/api/settings":
                    return self._send(200, settings.save(b))
                self._send(200, {"ok": True})
                settings.restart_soon()
                return None
            if u.path == "/api/event":
                kind = str(b.get("kind", ""))
                allowed = habits.allowed_kinds()
                if kind not in allowed:
                    return self._send(400, {"error": f"kind must be one of {sorted(allowed)}"})
                eid = db.add_event(kind, str(b.get("device", "dashboard")), source=str(b.get("source", "manual")),
                                   note=str(b.get("note", ""))[:200],
                                   ts=float(b["ts"]) if b.get("ts") else None)
                return self._send(200, {"ok": True, "id": eid})
            if u.path == "/api/event/delete":
                return self._send(200, {"ok": db.delete_event(int(b.get("id", 0)))})
            if u.path == "/api/metric":
                # {"day": "2026-10-15", "metric": "sleep"|"steps", "value": 27000, "device": "iphone"}
                metric = str(b.get("metric", ""))
                if metric not in ALLOWED_METRICS:
                    return self._send(400, {"error": f"metric must be one of {sorted(ALLOWED_METRICS)}"})
                try:
                    db.set_metric(str(b["day"])[:10], metric, str(b.get("device", "phone")), float(b["value"]))
                except (KeyError, ValueError) as e:
                    return self._send(400, {"error": f"bad metric: {e}"})
                return self._send(200, {"ok": True})
            if u.path == "/api/activity":
                # {"device","app","category" (or "auto"),"start","end","title"}
                if str(b.get("category", "auto")) in ("", "auto"):
                    b["category"] = categorize_phone(str(b.get("app", "")), cfg["activity"]["categories"])
                try:
                    if float(b["end"]) < float(b["start"]):
                        return self._send(400, {"error": "end before start"})
                    c = db.conn()
                    c.execute("INSERT INTO activity(device, app, title, domain, category, ts_start, ts_end, source) "
                              "VALUES(?,?,?,?,?,?,?,'phone')",
                              (str(b["device"]), str(b["app"]), str(b.get("title", ""))[:300], "",
                               str(b.get("category", "other")), float(b["start"]), float(b["end"])))
                    c.commit()
                except (KeyError, ValueError) as e:
                    return self._send(400, {"error": f"bad activity: {e}"})
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})

        def _cloud_login(self, email: str, pw: str):
            """Log into the friends server; None if that email + password isn't an account there."""
            try:
                return cloud.connect(email, pw, "", "", new=False)
            except cloud.CloudError as e:
                if "don't match" in str(e):
                    return None
                raise

        def _link_local(self, st: dict, email: str, pw: str) -> str:
            """After logging into the friends server: make this computer's account match it."""
            owner = accounts.owner()
            disp = db.kv_get("cloud_display") or st["username"]
            if owner is None:
                return accounts.signup(st["username"], pw, disp, email)
            if owner["email"] and owner["email"].lower() != email.lower():
                cloud.disconnect()
                raise accounts.AuthError(f"This LazyTrack belongs to @{owner['username']}. Log in with that account.")
            accounts.update(owner["id"], username=st["username"], display_name=disp, email=email, password=pw)
            return accounts.login(email, pw)

        def _update_account(self, b: dict):
            user = self._user()
            if not user:
                return self._send(401, {"error": "log in first"})
            if not accounts.password_ok(user, str(b.get("current_password", ""))):
                time.sleep(0.6)
                raise accounts.AuthError("Your current password isn't right.")
            name = str(b.get("username", "")).strip() or user["username"]
            disp = str(b.get("display_name", "")).strip() or user["display_name"]
            email = str(b.get("email", "")).strip().lower()
            new_pw = str(b.get("new_password", "")) or None
            accounts.check_new(name, new_pw or "xxxxxx", email)
            note = ""
            if cloud.state()["connected"]:
                note = cloud.update_account(username=name if name != user["username"] else None,
                                            display_name=disp if disp != user["display_name"] else None,
                                            email=email or None, password=new_pw)
            elif email:   # first time with an email: join the account on the friends server, or make it
                pw_now = new_pw or str(b.get("current_password", ""))
                st = self._cloud_login(email, pw_now)
                if st:   # already made there (earlier try, or another computer): use that one
                    name, disp = st["username"], db.kv_get("cloud_display") or disp
                else:
                    try:
                        cloud.connect(email, pw_now, name, disp, new=True)
                    except cloud.CloudError as e:
                        if "already has an account" in str(e):
                            raise cloud.CloudError("That email is already used by another account.") from None
                        raise
            accounts.update(user["id"], username=name, display_name=disp,
                            email=email if email and not note else None, password=new_pw)
            fresh = accounts.user_for(self._session())
            return self._send(200, {"ok": True, "note": note, "user": self._me(fresh)})

        def _cloud_get(self, path: str, qs):
            try:
                if path == "/api/cloud/state":
                    return self._send(200, cloud.state())
                if path == "/api/cloud/friends":
                    return self._send(200, cloud.friends())
                if path == "/api/cloud/messages":
                    return self._send(200, cloud.messages(qs.get("with", [""])[0], int(qs.get("after", ["0"])[0] or 0)))
                if path == "/api/cloud/unread":
                    return self._send(200, cloud.unread_detail())
                if path == "/api/cloud/ideas":
                    return self._send(200, cloud.ideas())
                if path == "/api/cloud/admin":
                    return self._send(200, cloud.admin_status(qs.get("fresh", [""])[0] == "1"))
                if path == "/api/cloud/news":
                    return self._send(200, {"news": cloud.news()})
                if path == "/api/cloud/admin/overview":
                    return self._send(200, cloud.admin_overview())
                if path == "/api/cloud/admin/news":
                    return self._send(200, {"news": cloud.admin_news()})
            except cloud.CloudError as e:
                return self._send(502, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def _cloud_post(self, path: str, b: dict):
            try:
                if path == "/api/cloud/connect":
                    user = self._user()
                    name = str(b.get("username") or (user["username"] if user else ""))
                    disp = user["display_name"] if user else name
                    return self._send(200, cloud.connect(str(b.get("email", "")), str(b.get("password", "")), name, disp,
                                                         new=bool(b.get("new"))))
                if path == "/api/cloud/disconnect":
                    cloud.disconnect()
                    return self._send(200, {"ok": True})
                if path == "/api/cloud/idea":
                    from . import updater
                    return self._send(200, cloud.submit_idea(b.get("title", ""), b.get("body", ""), str(b.get("area", "")),
                                                             int(b.get("want") or 2), sys.platform, updater.installed()[:7]))
                if path == "/api/cloud/idea/status":
                    cloud.set_idea_status(int(b.get("id", 0)), str(b.get("status", "")))
                    return self._send(200, {"ok": True})
                if path == "/api/cloud/card":
                    from . import updater
                    card = dict(b.get("card") or {})
                    card["app"] = {"v": updater.installed()[:7], "os": sys.platform}   # for the admin's "versions in use"
                    return self._send(200, {"ok": True, "sent": cloud.push_card(card)})
                if path == "/api/cloud/admin/news":
                    cloud.post_news(b.get("body", ""), str(b.get("kind", "")))
                    return self._send(200, {"ok": True})
                if path == "/api/cloud/admin/news/set":
                    if b.get("delete"):
                        cloud.delete_news(int(b.get("id", 0)))
                    else:
                        cloud.set_news(int(b.get("id", 0)), bool(b.get("active")))
                    return self._send(200, {"ok": True})
                if path == "/api/cloud/idea/delete":
                    cloud.delete_idea(int(b.get("id", 0)))
                    return self._send(200, {"ok": True})
                if path == "/api/cloud/request":
                    return self._send(200, {"ok": True, "message": cloud.request(str(b.get("username", "")))})
                if path == "/api/cloud/accept":
                    cloud.accept(int(b.get("id", 0)))
                    return self._send(200, {"ok": True})
                if path == "/api/cloud/remove":
                    cloud.remove(int(b.get("id", 0)))
                    return self._send(200, {"ok": True})
                if path == "/api/cloud/send":
                    cloud.send(str(b.get("to", "")), str(b.get("body", "")))
                    return self._send(200, {"ok": True})
            except cloud.CloudError as e:
                return self._send(502, {"error": str(e)})
            return self._send(404, {"error": "not found"})

        def _me(self, user) -> dict:
            out = accounts.public(user)
            if not out["email"] and cloud.state()["connected"]:
                out["email"] = cloud.state()["email"] or ""
            out["cloud"] = cloud.state()["connected"]
            return out

        def _auth(self, path: str, b: dict):
            """One account everywhere: email + username + password, on the friends server and
            mirrored here. Signing up checks with the server that the name and email are free."""
            try:
                if path == "/api/auth/signup":
                    if accounts.has_account():
                        raise accounts.AuthError("This LazyTrack already has an account. Log in instead.")
                    email, name = str(b.get("email", "")).strip(), str(b.get("username", "")).strip()
                    pw, disp = str(b.get("password", "")), str(b.get("display_name", "")).strip()
                    if not email:
                        raise accounts.AuthError("Add your email. It's how you log in on your other devices.")
                    accounts.check_new(name, pw, email)
                    st = self._cloud_login(email, pw)   # this email + password already an account? then it's a log-in
                    if st:
                        name, disp = st["username"] or name, db.kv_get("cloud_display") or disp
                    else:
                        cloud.connect(email, pw, name, disp or name, new=True)   # checks name + email are free
                    tok = accounts.signup(name, pw, disp, email)
                    return self._send(200, {"ok": True, "user": self._me(accounts.user_for(tok))}, cookie=tok)
                if path == "/api/auth/login":
                    login, pw = str(b.get("login") or b.get("username") or "").strip(), str(b.get("password", ""))
                    row = accounts.find(login)
                    if accounts.password_ok(row, pw):
                        tok = accounts.login(login, pw)       # the copy on this computer: works offline too
                        if row["email"] and not cloud.state()["connected"]:
                            try:   # friends come back by themselves when you log in
                                cloud.connect(row["email"], pw, "", "", new=False)
                            except cloud.CloudError:
                                pass   # offline or no account there yet: the app still works, friends wait
                    elif "@" in login:
                        # the account on the friends server is the real one: log in there and copy/refresh it here
                        st = cloud.connect(login, pw, "", "", new=False)
                        tok = self._link_local(st, login, pw)
                    elif accounts.has_account():
                        tok = accounts.login(login, pw)       # raises "don't match"
                    else:
                        raise accounts.AuthError("On a new computer, log in with your email.")
                    return self._send(200, {"ok": True, "user": self._me(accounts.user_for(tok))}, cookie=tok)
                if path == "/api/auth/update":
                    return self._update_account(b)
            except accounts.AuthError as e:
                return self._send(400, {"error": str(e)})
            except cloud.CloudError as e:
                return self._send(400, {"error": str(e)})
            if path == "/api/auth/logout":
                accounts.logout(self._session())
                cloud.disconnect()   # logging out logs out of friends too; logging in connects again
                return self._send(200, {"ok": True}, cookie="")
            user = self._user()
            if not user:
                return self._send(401, {"error": "log in first"})
            if path == "/api/auth/setup_again":
                accounts.set_setup_done(user["id"], False)
                return self._send(200, {"ok": True})
            if path == "/api/auth/delete":
                if not self._local():
                    return self._send(403, {"error": "only on the computer itself"})
                accounts.delete_account()   # the login goes, every bit of data stays
                return self._send(200, {"ok": True}, cookie="")
            return self._send(404, {"error": "not found"})

    return Handler


def start(cfg: dict) -> ThreadingHTTPServer:
    d = cfg["dashboard"]
    if d["host"] not in ("127.0.0.1", "localhost") and not d.get("token"):
        raise SystemExit("Refusing to expose the dashboard on the network without dashboard.token set in config.json")
    server = ThreadingHTTPServer((d["host"], int(d["port"])), make_handler(cfg))
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    log.info("dashboard: http://%s:%s", "localhost" if d["host"] == "127.0.0.1" else d["host"], d["port"])
    return server
