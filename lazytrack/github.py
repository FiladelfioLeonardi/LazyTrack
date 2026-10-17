"""GitHub stats: contributions per day, streaks, repos, stars, languages.

With a token: the GraphQL contribution calendar (the green squares on your profile, a full year,
private contributions included if you enabled that on your profile).
Without a token: public data only (public events of the last ~90 days, public repos).
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import db

log = logging.getLogger(__name__)
API = "https://api.github.com"

GRAPHQL = """
query($login: String!) {
  user(login: $login) {
    login name avatarUrl
    followers { totalCount }
    contributionsCollection {
      totalCommitContributions totalPullRequestContributions totalIssueContributions
      totalPullRequestReviewContributions restrictedContributionsCount
      contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } }
    }
    repositories(ownerAffiliations: OWNER, first: 100, orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      nodes { name stargazerCount isPrivate pushedAt url primaryLanguage { name } }
    }
  }
}
"""


def _session(token: str):
    import requests
    s = requests.Session()
    s.headers.update({"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                      "User-Agent": "LazyTrack"})
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def _from_graphql(data: dict) -> dict:
    u = data["user"]
    cc = u["contributionsCollection"]
    days = {d["date"]: d["contributionCount"]
            for w in cc["contributionCalendar"]["weeks"] for d in w["contributionDays"]}
    repos = [{"name": r["name"], "stars": r["stargazerCount"], "private": r["isPrivate"], "pushed_at": r["pushedAt"],
              "url": r["url"], "language": (r.get("primaryLanguage") or {}).get("name")}
             for r in u["repositories"]["nodes"]]
    return {
        "login": u["login"], "name": u.get("name"), "avatar": u.get("avatarUrl"),
        "followers": u["followers"]["totalCount"], "repo_count": u["repositories"]["totalCount"],
        "year_total": cc["contributionCalendar"]["totalContributions"],
        "breakdown": {"commits": cc["totalCommitContributions"], "pull_requests": cc["totalPullRequestContributions"],
                      "issues": cc["totalIssueContributions"], "reviews": cc["totalPullRequestReviewContributions"],
                      "private": cc["restrictedContributionsCount"]},
        "days": days, "repos": repos, "source": "full",
    }


def _from_rest(s, login: str) -> dict:
    u = s.get(f"{API}/users/{login}", timeout=20)
    u.raise_for_status()
    u = u.json()
    repos_raw = s.get(f"{API}/users/{login}/repos", params={"per_page": 100, "sort": "pushed"}, timeout=20).json()
    repos = [{"name": r["name"], "stars": r["stargazers_count"], "private": r["private"], "pushed_at": r["pushed_at"],
              "url": r["html_url"], "language": r.get("language")} for r in repos_raw if not r.get("fork")]
    days: Dict[str, int] = defaultdict(int)
    for page in (1, 2, 3):
        ev = s.get(f"{API}/users/{login}/events/public", params={"per_page": 100, "page": page}, timeout=20)
        if ev.status_code != 200:
            break
        items = ev.json()
        for e in items:
            day = datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")).astimezone().date().isoformat()
            if e["type"] == "PushEvent":
                days[day] += max(1, int(e.get("payload", {}).get("distinct_size") or e.get("payload", {}).get("size") or 1))
            elif e["type"] in ("PullRequestEvent", "IssuesEvent", "PullRequestReviewEvent", "CreateEvent"):
                days[day] += 1
        if len(items) < 100:
            break
    return {
        "login": u["login"], "name": u.get("name"), "avatar": u.get("avatar_url"), "followers": u.get("followers", 0),
        "repo_count": u.get("public_repos", 0), "year_total": None, "breakdown": None,
        "days": dict(days), "repos": repos, "source": "public",
    }


def sync(cfg: dict) -> bool:
    g = cfg.get("github", {})
    login = (g.get("username") or "").strip()
    if not login:
        return False
    token = (g.get("token") or "").strip()
    s = _session(token)
    if token:
        r = s.post(f"{API}/graphql", json={"query": GRAPHQL, "variables": {"login": login}}, timeout=30)
        r.raise_for_status()
        j = r.json()
        if j.get("errors"):
            raise RuntimeError(j["errors"][0].get("message", "GitHub GraphQL error"))
        stats = _from_graphql(j["data"])
    else:
        stats = _from_rest(s, login)
    store(stats)
    log.info("github: synced %s (%s)", login, stats["source"])
    return True


def store(stats: dict) -> None:
    c = db.conn()
    for day, n in stats["days"].items():
        c.execute("INSERT INTO gh_days(day, count) VALUES(?, ?) ON CONFLICT(day) DO UPDATE SET count=excluded.count",
                  (day, int(n)))
    c.commit()
    meta = {k: v for k, v in stats.items() if k != "days"}
    db.kv_set("gh_meta", json.dumps(meta))
    db.kv_set("gh_synced_at", time.time())


# ---------------------------------------------------------------- reading it back

def day_count(d: date) -> int:
    r = db.conn().execute("SELECT count FROM gh_days WHERE day=?", (d.isoformat(),)).fetchone()
    return r[0] if r else 0


def _streaks(counts: Dict[str, int], today: date) -> dict:
    # current: days in a row up to today (today not done yet doesn't break it)
    cur, d = 0, today
    if not counts.get(d.isoformat()):
        d -= timedelta(days=1)
    while counts.get(d.isoformat()):
        cur += 1
        d -= timedelta(days=1)
    best = run = 0
    if counts:
        start = date.fromisoformat(min(counts))
        d = start
        while d <= today:
            run = run + 1 if counts.get(d.isoformat()) else 0
            best = max(best, run)
            d += timedelta(days=1)
    return {"current": cur, "longest": best}


def status() -> dict:
    meta_raw = db.kv_get("gh_meta")
    if not meta_raw:
        return {"configured": False}
    meta = json.loads(meta_raw)
    today = date.today()
    since = (today - timedelta(days=400)).isoformat()
    counts = {r[0]: r[1] for r in db.conn().execute("SELECT day, count FROM gh_days WHERE day >= ?", (since,))}
    monday = today - timedelta(days=today.weekday())
    week = sum(counts.get((monday + timedelta(days=i)).isoformat(), 0) for i in range((today - monday).days + 1))
    lw_start = monday - timedelta(days=7)
    last_week_so_far = sum(counts.get((lw_start + timedelta(days=i)).isoformat(), 0) for i in range((today - monday).days + 1))
    # 12 weeks for the little heatmap, Monday-first columns
    grid_start = monday - timedelta(weeks=11)
    heat = [{"day": (grid_start + timedelta(days=i)).isoformat(),
             "count": counts.get((grid_start + timedelta(days=i)).isoformat(), 0)}
            for i in range((today - grid_start).days + 1)]
    langs = Counter(r["language"] for r in meta.get("repos", []) if r.get("language"))
    return {
        "configured": True,
        "login": meta["login"], "name": meta.get("name"), "source": meta.get("source"),
        "today": counts.get(today.isoformat(), 0),
        "week": week, "last_week_so_far": last_week_so_far,
        "year_total": meta.get("year_total") if meta.get("year_total") is not None else sum(counts.values()),
        "breakdown": meta.get("breakdown"),
        "streak": _streaks(counts, today),
        "repo_count": meta.get("repo_count"), "followers": meta.get("followers"),
        "stars": sum(r.get("stars", 0) for r in meta.get("repos", [])),
        "languages": [l for l, _ in langs.most_common(4)],
        "recent_repos": meta.get("repos", [])[:5],
        "heat": heat,
        "synced_at": db.kv_get("gh_synced_at"),
    }
