"""lazytrack command line: run | check | camtest | dashboard | summary | sync | log"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time

from . import config, db, summary, system

log = logging.getLogger("lazytrack")
START = "start.bat" if system.IS_WINDOWS else "start.command"   # what you double-click to start
LT = "lt" if system.IS_WINDOWS else "./lt"                     # the command-line shortcut


class SyncWorker(threading.Thread):
    """Pulls commits and 42 data in the background."""

    def __init__(self, cfg: dict, stop: threading.Event):
        super().__init__(name="sync", daemon=True)
        self.cfg, self.stop_ev = cfg, stop

    def run(self) -> None:
        from . import commits, fortytwo, github
        next_commits = next_ft = next_gh = 0.0
        while not self.stop_ev.is_set():
            now = time.time()
            if now >= next_commits:
                try:
                    commits.sync(self.cfg)
                except Exception:
                    log.exception("commit sync failed")
                next_commits = now + 60 * self.cfg["commits"]["sync_minutes"]
            if now >= next_ft:
                try:
                    fortytwo.sync(self.cfg)
                except Exception as e:
                    log.warning("42 sync failed: %s", e)
                next_ft = now + 60 * self.cfg["fortytwo"]["sync_minutes"]
            if now >= next_gh:
                try:
                    github.sync(self.cfg)
                except Exception as e:
                    log.warning("GitHub sync failed: %s", e)
                next_gh = now + 60 * self.cfg["github"]["sync_minutes"]
            self.stop_ev.wait(30)


def _camera_permission(cfg: dict) -> None:
    """macOS only shows the camera permission prompt when asked from the main
    thread, so open the camera once here before the detector thread starts."""
    try:
        from .smoke import open_camera
        cap = open_camera(int(cfg["smoke"]["camera_index"]))
        ok = cap.isOpened()
        cap.release()
        os.environ["OPENCV_AVFOUNDATION_SKIP_AUTH"] = "1"
        if not ok:
            log.warning("camera not accessible yet - allow it in %s",
                        "Settings > Privacy & security > Camera" if system.IS_WINDOWS else "System Settings > Privacy & Security > Camera")
    except ImportError:
        log.warning("opencv not installed, smoke detection off")


def cmd_run(cfg: dict, args) -> None:
    from .activity import ActivityTracker
    from . import dashboard

    stop = threading.Event()
    dashboard.start(cfg)
    if cfg["dashboard"].get("open_on_start", True) and not os.environ.get("LAZYTRACK_RESTARTED"):
        import webbrowser
        webbrowser.open(f"http://localhost:{cfg['dashboard']['port']}")
    SyncWorker(cfg, stop).start()
    from .updater import UpdateWorker
    UpdateWorker(cfg, stop).start()
    if cfg["smoke"]["enabled"] and not args.no_camera:
        _camera_permission(cfg)
        from .smoke import SmokeDetector
        SmokeDetector(cfg, stop).start()

    tracker = ActivityTracker(cfg)
    interval = float(cfg["activity"]["interval_seconds"])
    last_summary_check = 0.0
    log.info("tracking. Ctrl+C to stop.")
    try:
        while True:  # main thread = activity tracker (Cocoa needs it)
            try:
                tracker.sample()
            except Exception:
                log.exception("activity sample failed")
            if time.time() - last_summary_check > 60:
                last_summary_check = time.time()
                try:
                    summary.maybe_send_weekly(cfg)
                except Exception:
                    log.exception("weekly summary failed")
            system.pump_runloop(interval)
    except KeyboardInterrupt:
        log.info("stopping")
        stop.set()


def cmd_check(cfg: dict, args) -> None:
    from .activity import categorize
    print("Switch between a few apps / browser tabs for the next 15 seconds...\n")
    for _ in range(5):
        app = system.frontmost_app()
        url, title = system.browser_tab(app) if app in system.BROWSER_SCRIPTS else ("", "")
        cat = categorize(app, title, url, cfg["activity"]["categories"])
        print(f"  app={app!r:<22} category={cat:<10} idle={system.idle_seconds():5.1f}s "
              f"locked={system.screen_locked()} title={title[:50]!r}")
        system.pump_runloop(3)
    if not system.frontmost_app():
        print("\n!! Could not read the app in front." + ("" if system.IS_WINDOWS else " Is pyobjc installed? (./install.sh)"))
    from . import commits
    repos = commits.find_repos(cfg["commits"]["roots"], cfg["commits"]["max_depth"])
    print(f"\nGit: found {len(repos)} repos, author emails: {sorted(commits.author_emails(cfg['commits'])) or 'ANY'}")
    for r in repos[:10]:
        print(f"  {r}")
    print("\n42 API:", "configured" if cfg["fortytwo"]["client_id"] else "not configured (optional)")
    try:
        from .smoke import open_camera
        cap = open_camera(int(cfg["smoke"]["camera_index"]))
        print("Camera:", "OK" if cap.isOpened() else "NOT accessible - allow camera access in your privacy settings")
        cap.release()
    except ImportError:
        print("Camera: opencv not installed")
    print("\nTesting a notification...")
    system.notify("LazyTrack", "Notifications work 👍")


def cmd_camtest(cfg: dict, args) -> None:
    from .smoke import camtest
    camtest(cfg)


def cmd_collect(cfg: dict, args) -> None:
    from .smoke import collect
    collect(cfg, args.label, args.seconds)


def cmd_train(cfg: dict, args) -> None:
    from .smoke import train
    train(cfg, args.epochs)


def cmd_dashboard(cfg: dict, args) -> None:
    from . import dashboard
    dashboard.start(cfg)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def cmd_summary(cfg: dict, args) -> None:
    off = -1 if args.last else 0
    print(summary.format_week(summary.weekly(off), summary.weekly(off - 1), summary.ft_status(cfg)))


def cmd_sync(cfg: dict, args) -> None:
    from . import commits, fortytwo
    print(f"commits: {commits.sync(cfg)} new")
    try:
        print("42:", "synced" if fortytwo.sync(cfg) else "not configured")
    except Exception as e:
        print("42 sync failed:", e)
    from . import github
    try:
        print("GitHub:", "synced" if github.sync(cfg) else "not configured")
    except Exception as e:
        print("GitHub sync failed:", e)


def cmd_log(cfg: dict, args) -> None:
    from .dashboard import ALLOWED_KINDS
    if args.kind not in ALLOWED_KINDS:
        sys.exit(f"kind must be one of: {', '.join(sorted(ALLOWED_KINDS))}")
    db.add_event(args.kind, cfg["device_name"], note=args.note or "")
    print(f"logged {args.kind}")


def _tailscale_ip() -> str:
    import shutil
    import subprocess
    for exe in [shutil.which("tailscale"), "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                r"C:\Program Files\Tailscale\tailscale.exe"]:
        if not exe or not os.path.exists(exe):
            continue
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=10)
            ip = out.stdout.strip().splitlines()[0] if out.returncode == 0 and out.stdout.strip() else ""
            if ip:
                return ip
        except (OSError, subprocess.TimeoutExpired, IndexError):
            pass
    return ""


def _ping(url: str, timeout: float = 3.0) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def cmd_phone(cfg: dict, args) -> None:
    """Makes the dashboard reachable from the phone (over Tailscale) and shows a QR code for it."""
    import json
    import secrets
    import urllib.request
    from urllib.parse import quote

    ip = _tailscale_ip()
    if not ip:
        sys.exit("Tailscale isn't running on this computer.\n"
                 + ("Install it from tailscale.com/download, log in, " if system.IS_WINDOWS else
                    "Open the Tailscale app (menu bar) and log in, or install it first:\n  brew install --cask tailscale\n")
                 + f"then run {LT} phone again.")

    # 1. allow connections from the phone, protected by a token
    raw = json.loads(config.CONFIG_PATH.read_text())
    dash = raw.setdefault("dashboard", {})
    changed = False
    if not dash.get("token"):
        dash["token"] = secrets.token_urlsafe(18)
        changed = True
    if dash.get("host", "127.0.0.1") != "0.0.0.0":
        dash["host"] = "0.0.0.0"
        changed = True
    if changed:
        config.CONFIG_PATH.write_text(json.dumps(raw, indent=2))
    port = int(dash.get("port", cfg["dashboard"]["port"]))
    token = dash["token"]
    local = f"http://127.0.0.1:{port}"
    remote = f"http://{ip}:{port}"

    # 2. make sure the running LazyTrack uses the new setting
    print("Checking LazyTrack...")
    if not _ping(local + "/api/ping"):
        print(f"\n!! LazyTrack isn't running. Double-click {START}, then run {LT} phone again.")
        return
    if not _ping(f"{remote}/api/ping?token={quote(token)}"):
        print("Restarting LazyTrack so your phone can reach it...")
        try:
            urllib.request.urlopen(urllib.request.Request(local + "/api/restart", data=b"{}", method="POST"), timeout=3)
        except Exception:
            pass
        import time as _t
        _t.sleep(2)
        for _ in range(20):
            if _ping(f"{remote}/api/ping?token={quote(token)}"):
                break
            _t.sleep(0.7)

    # 3. test it the same way the phone will
    if _ping(f"{remote}/api/ping?token={quote(token)}"):
        print("✅ Your Mac is reachable through Tailscale.")
    else:
        print("\n!! LazyTrack runs, but it isn't reachable on the Tailscale address yet.")
        print("   - If a popup asked to allow incoming connections for Python, click Allow.")
        print("   - Or: System Settings > Network > Firewall > Options > allow Python.")
        print(f"   - Make sure the {START} window was restarted after this command.")
        print(f"   Then run {LT} phone again.")
        return

    dashboard_url = f"{remote}/?token={quote(token)}"
    app_link = f"lazytrack://setup?url={quote(remote, safe='')}&token={quote(token, safe='')}"
    print(f"\nOpen this on your iPhone (Tailscale must be ON on the phone):\n  {dashboard_url}\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=2)
        qr.add_data(dashboard_url)
        qr.print_ascii(invert=True)
        png = config.HOME / "phone-dashboard.png"
        qr.make_image().save(str(png))
        os.system(f"open '{png}'")
        print("\nScan it with the iPhone Camera, tap the yellow link, then Share > Add to Home Screen.")
    except ImportError:
        print("(No QR code: run the installer again)")
    print(f"\nLater, for the LazyTrack iPhone app (once it's installed), open this link on the phone:\n  {app_link}\n")


def main() -> None:
    p = argparse.ArgumentParser(prog="lazytrack", description="Track your life without lifting a finger.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start everything (tracker, camera, sync, dashboard)")
    r.add_argument("--no-camera", action="store_true")
    sub.add_parser("check", help="test permissions and what gets detected")
    sub.add_parser("camtest", help="live camera preview with the smoking score")
    co = sub.add_parser("collect", help="record training photos: smoking / not_smoking")
    co.add_argument("label", choices=["smoking", "not_smoking"])
    co.add_argument("--seconds", type=int, default=60)
    tr = sub.add_parser("train", help="train your personal smoking model")
    tr.add_argument("--epochs", type=int, default=25)
    sub.add_parser("dashboard", help="only serve the dashboard")
    s = sub.add_parser("summary", help="print the weekly summary")
    s.add_argument("--last", action="store_true", help="last week instead of this week")
    sub.add_parser("sync", help="sync commits and 42 now")
    sub.add_parser("init", help="create the config file")
    sub.add_parser("phone", help="connect the iPhone app (shows a QR code)")
    lg = sub.add_parser("log", help="log an event, e.g. `lazytrack log gym`")
    lg.add_argument("kind")
    lg.add_argument("--note")
    args = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    cfg = config.load()
    if args.cmd == "init":
        print(f"config: {config.CONFIG_PATH}")
        return
    {"run": cmd_run, "check": cmd_check, "camtest": cmd_camtest, "dashboard": cmd_dashboard,
     "summary": cmd_summary, "sync": cmd_sync, "log": cmd_log, "phone": cmd_phone, "collect": cmd_collect, "train": cmd_train}[args.cmd](cfg, args)
