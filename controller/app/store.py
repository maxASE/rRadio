"""Small SQLite state: which items were already seen, and play history."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("STATE_DB", "/data/state.db"))


class Store:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen (
                    source TEXT NOT NULL,
                    item TEXT NOT NULL,
                    ts REAL NOT NULL,
                    PRIMARY KEY (source, item)
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                """
            )
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(history)")}
            if "channel" not in cols:
                self._db.execute("ALTER TABLE history ADD COLUMN channel TEXT NOT NULL DEFAULT ''")

    def has_any(self, source: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM seen WHERE source=? LIMIT 1", (source,)).fetchone()
        return row is not None

    def is_seen(self, source: str, item: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM seen WHERE source=? AND item=?", (source, item)
            ).fetchone()
        return row is not None

    def mark_seen(self, source: str, items: list[str]) -> None:
        now = time.time()
        with self._lock, self._db:
            self._db.executemany(
                "INSERT OR IGNORE INTO seen (source, item, ts) VALUES (?, ?, ?)",
                [(source, i, now) for i in items],
            )

    def forget_source(self, source: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM seen WHERE source=?", (source,))

    def add_history(self, kind: str, source: str, title: str, status: str, detail: str = "", channel: str = "") -> None:
        """channel "" means the entry is not about one channel (e.g. a filtered upload)."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO history (ts, kind, source, title, status, detail, channel) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), kind, source, title, status, detail, channel),
            )
            self._db.execute(
                "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 500)"
            )

    def history(self, limit: int = 30, channel: str | None = None) -> list[dict]:
        """Entries for one channel plus the ones that concern all channels."""
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, kind, source, title, status, detail FROM history"
                " WHERE ? IS NULL OR channel IN (?, '') ORDER BY id DESC LIMIT ?",
                (channel, channel, limit),
            ).fetchall()
        keys = ["ts", "kind", "source", "title", "status", "detail"]
        return [dict(zip(keys, r)) for r in rows]

    def last_cutin(self, channel: str | None = None) -> dict | None:
        """The most recent clip that went on air (filtered-out items excluded)."""
        with self._lock:
            row = self._db.execute(
                "SELECT ts, kind, source, title FROM history"
                " WHERE kind != 'program' AND (status = 'played' OR (status = 'skipped' AND detail = ''))"
                " AND (? IS NULL OR channel = ?)"
                " ORDER BY id DESC LIMIT 1",
                (channel, channel),
            ).fetchone()
        return dict(zip(["ts", "kind", "source", "title"], row)) if row else None
