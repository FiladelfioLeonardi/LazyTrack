"""Connecting a phone, from inside the app (Settings → Your phone).

The phone opens this computer's LazyTrack over Tailscale, a free private network between your
own devices, so it works from anywhere without opening anything to the internet. Steps:
  1. Tailscale on the computer and on the phone, same account (the app checks the computer side)
  2. "Connect my phone": LazyTrack starts listening on the network, protected by a secret token
     (written to config.json; LazyTrack restarts once to use it)
  3. a QR code with the address + token: scan it, then "Add to Home Screen" and it's an app icon
"""
from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import subprocess
from urllib.parse import quote

from . import config, system


def _tailscale_exes() -> list:
    found = [shutil.which("tailscale"), "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
             r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe"]
    return [e for e in found if e and os.path.exists(e)]


def tailscale_ip() -> str:
    for exe in _tailscale_exes():
        try:
            kw = {"creationflags": 0x08000000} if system.IS_WINDOWS else {}   # no console window
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=10, **kw)
            lines = out.stdout.strip().splitlines()
            if out.returncode == 0 and lines:
                return lines[0].strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ""


def _raw() -> dict:
    try:
        return json.loads(config.CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return {}


def enable() -> bool:
    """Lets the phone reach LazyTrack (with a token). True if LazyTrack has to restart for it."""
    raw = _raw()
    dash = raw.setdefault("dashboard", {})
    changed = False
    if not dash.get("token"):
        dash["token"] = secrets.token_urlsafe(18)
        changed = True
    if dash.get("host", "127.0.0.1") != "0.0.0.0":
        dash["host"] = "0.0.0.0"
        changed = True
    if changed:
        config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.CONFIG_PATH.write_text(json.dumps(raw, indent=2))
    return changed


def _qr_svg(text: str) -> str:
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception:
        return ""


def status(running_cfg: dict) -> dict:
    """What Settings → Your phone shows. running_cfg = the config LazyTrack started with."""
    ip = tailscale_ip()
    installed = bool(_tailscale_exes())
    raw = _raw().get("dashboard", {})
    token = raw.get("token") or ""
    saved_open = raw.get("host") == "0.0.0.0" and bool(token)
    live_open = running_cfg["dashboard"].get("host") == "0.0.0.0"
    port = int(running_cfg["dashboard"].get("port", 8042))
    out = {"tailscale": installed, "ip": ip, "enabled": saved_open, "live": live_open and saved_open,
           "windows": system.IS_WINDOWS, "url": "", "qr": ""}
    if ip and saved_open and live_open:
        out["url"] = f"http://{ip}:{port}/?token={quote(token)}"
        out["qr"] = _qr_svg(out["url"])
    return out
