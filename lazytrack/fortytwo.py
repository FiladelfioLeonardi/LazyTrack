"""42 intra API: logtime, projects, blackhole."""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, Optional

from . import db

log = logging.getLogger(__name__)
API = "https://api.intra.42.fr"


class Client:
    def __init__(self, client_id: str, client_secret: str):
        import requests
        self.s = requests.Session()
        self.cid, self.secret = client_id, client_secret
        self.token: Optional[str] = None
        self.expires = 0.0

    def _auth(self) -> None:
        if self.token and time.time() < self.expires - 60:
            return
        r = self.s.post(f"{API}/oauth/token", data={
            "grant_type": "client_credentials", "client_id": self.cid, "client_secret": self.secret}, timeout=20)
        r.raise_for_status()
        j = r.json()
        self.token = j["access_token"]
        self.expires = time.time() + j.get("expires_in", 7200)

    def get(self, path: str, params=None):
        for attempt in range(5):
            self._auth()
            r = self.s.get(f"{API}{path}", params=params,
                           headers={"Authorization": f"Bearer {self.token}"}, timeout=20)
            if r.status_code == 429:  # rate limit: 2 requests / second
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"42 API kept rate limiting {path}")


def parse_duration(s: str) -> int:
    """'05:23:12.000000' -> seconds"""
    try:
        h, m, sec = s.split(":")
        return int(h) * 3600 + int(m) * 60 + int(float(sec))
    except ValueError:
        return 0


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()


def split_by_day(start: datetime, end: datetime) -> Dict[str, int]:
    out: Dict[str, int] = defaultdict(int)
    cur = start
    while cur < end:
        next_midnight = datetime.combine(cur.date() + timedelta(days=1), datetime.min.time(), cur.tzinfo)
        seg_end = min(end, next_midnight)
        out[cur.date().isoformat()] += int((seg_end - cur).total_seconds())
        cur = seg_end
    return out


def _logtime(cl: Client, login: str, begin: date) -> Dict[str, int]:
    try:
        stats = cl.get(f"/v2/users/{login}/locations_stats", {"begin_at": begin.isoformat()})
        if isinstance(stats, dict):
            return {d: parse_duration(v) for d, v in stats.items()}
    except Exception as e:  # fall back to raw sessions
        log.info("locations_stats unavailable (%s), using locations", e)
    totals: Dict[str, int] = defaultdict(int)
    now = datetime.now().astimezone()
    page = 1
    while True:
        locs = cl.get(f"/v2/users/{login}/locations", {
            "range[begin_at]": f"{begin.isoformat()}T00:00:00Z,{now.isoformat()}",
            "per_page": 100, "page": page})
        if not locs:
            break
        for loc in locs:
            start = _parse_ts(loc["begin_at"])
            end = _parse_ts(loc["end_at"]) if loc.get("end_at") else now
            for d, s in split_by_day(start, end).items():
                totals[d] += s
        if len(locs) < 100:
            break
        page += 1
        time.sleep(0.5)
    return dict(totals)


def sync(cfg: dict) -> bool:
    f = cfg["fortytwo"]
    if not (f.get("login") and f.get("client_id") and f.get("client_secret")):
        return False
    login = f["login"]
    cl = Client(f["client_id"], f["client_secret"])
    user = cl.get(f"/v2/users/{login}")

    cursus_users = user.get("cursus_users", [])
    main = next((cu for cu in cursus_users if cu.get("cursus", {}).get("slug") == "42cursus"),
                cursus_users[-1] if cursus_users else None)
    if main:
        db.kv_set("ft_blackholed_at", main.get("blackholed_at"))
        db.kv_set("ft_level", main.get("level"))
    db.kv_set("ft_login", login)

    c = db.conn()
    main_id = main["cursus_id"] if main else None
    for pu in user.get("projects_users", []):
        if main_id and main_id not in pu.get("cursus_ids", [main_id]):
            continue
        c.execute("""INSERT INTO ft_projects(id, name, status, final_mark, validated, updated_at)
                     VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                     name=excluded.name, status=excluded.status, final_mark=excluded.final_mark,
                     validated=excluded.validated, updated_at=excluded.updated_at""",
                  (pu["id"], pu["project"]["name"], pu.get("status", ""), pu.get("final_mark"),
                   None if pu.get("validated?") is None else int(bool(pu.get("validated?"))),
                   pu.get("updated_at")))

    begin = date.today() - timedelta(days=60)
    for day, seconds in _logtime(cl, login, begin).items():
        c.execute("INSERT INTO ft_logtime(day, seconds) VALUES(?,?) "
                  "ON CONFLICT(day) DO UPDATE SET seconds=excluded.seconds", (day, seconds))
    c.commit()
    db.kv_set("ft_synced_at", time.time())
    log.info("42: synced %s", login)
    return True
