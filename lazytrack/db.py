"""SQLite storage. One connection per thread."""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device TEXT NOT NULL,
    app TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    ts_start REAL NOT NULL,
    ts_end REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_start ON activity(ts_start);
CREATE INDEX IF NOT EXISTS idx_activity_end ON activity(ts_end);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    device TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS commits (
    hash TEXT PRIMARY KEY,
    repo TEXT NOT NULL,
    ts REAL NOT NULL,
    message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_commits_ts ON commits(ts);

CREATE TABLE IF NOT EXISTS ft_logtime (
    day TEXT PRIMARY KEY,
    seconds INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ft_projects (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    final_mark INTEGER,
    validated INTEGER,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS metrics (
    day TEXT NOT NULL,
    metric TEXT NOT NULL,
    device TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (day, metric, device)
);

CREATE TABLE IF NOT EXISTS gh_days (
    day TEXT PRIMARY KEY,
    count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        config.HOME.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(config.DB_PATH), timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        # added later: where a screen-time record comes from. Old rows are all from the Mac.
        cols = {r[1] for r in c.execute("PRAGMA table_info(activity)")}
        if "source" not in cols:
            c.execute("ALTER TABLE activity ADD COLUMN source TEXT NOT NULL DEFAULT 'mac'")
            c.commit()
        _local.conn = c
    return c


def kv_get(key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value) -> None:
    c = conn()
    c.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
              (key, None if value is None else str(value)))
    c.commit()


def add_event(kind: str, device: str, source: str = "manual", note: str = "", ts: Optional[float] = None) -> int:
    c = conn()
    cur = c.execute("INSERT INTO events(ts, device, kind, source, note) VALUES(?, ?, ?, ?, ?)",
                    (ts or time.time(), device, kind, source, note))
    c.commit()
    return cur.lastrowid


def set_metric(day: str, metric: str, device: str, value: float) -> None:
    c = conn()
    c.execute("INSERT INTO metrics(day, metric, device, value) VALUES(?,?,?,?) "
              "ON CONFLICT(day, metric, device) DO UPDATE SET value=excluded.value", (day, metric, device, value))
    c.commit()


def delete_event(event_id: int) -> bool:
    c = conn()
    cur = c.execute("DELETE FROM events WHERE id=?", (event_id,))
    c.commit()
    return cur.rowcount > 0
