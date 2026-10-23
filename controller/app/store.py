"""Small SQLite state: which items were already seen, and play history."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .devices import Devices
from .sessions import Sessions

DB_PATH = Path(os.environ.get("STATE_DB", "/data/state.db"))


def replay_field(raw: str, key: str):
    """One field of an entry's replay info, e.g. the picture shown while it played."""
    try:
        return (json.loads(raw) if raw else {}).get(key)
    except ValueError:
        return None


def replay_available(raw: str) -> bool:
    """A kept file must still be there; a source to fetch again always is."""
    if not raw:
        return False
    try:
        info = json.loads(raw)
    except ValueError:
        return False
    if info.get("path"):
        return Path(info["path"]).is_file()
    return bool(info.get("media"))


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
            if "replay" not in cols:
                # JSON: where "listen again" gets the audio (a kept file, or the source to fetch again).
                self._db.execute("ALTER TABLE history ADD COLUMN replay TEXT NOT NULL DEFAULT ''")
        self.devices = Devices(self._db, self._lock)
        self.sessions = Sessions(self._db, self._lock)

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

    def add_history(self, kind: str, source: str, title: str, status: str, detail: str = "", channel: str = "",
                    replay: dict | None = None) -> None:
        """channel "" means the entry is not about one channel (e.g. a filtered upload)."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO history (ts, kind, source, title, status, detail, channel, replay) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), kind, source, title, status, detail, channel, json.dumps(replay) if replay else ""),
            )
            self._db.execute(
                "DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 500)"
            )

    def history(self, limit: int = 30, channel: str | None = None) -> list[dict]:
        """Entries for one channel plus the ones that concern all channels."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, kind, source, title, status, detail, replay FROM history"
                " WHERE ? IS NULL OR channel IN (?, '') ORDER BY id DESC LIMIT ?",
                (channel, channel, limit),
            ).fetchall()
        keys = ["id", "ts", "kind", "source", "title", "status", "detail"]
        return [{**dict(zip(keys, r)), "replay": replay_available(r[7]),
                 "art": replay_field(r[7], "art") or "", "replay_from": replay_field(r[7], "from"),
                 "duration": replay_field(r[7], "duration")} for r in rows]

    def entry(self, entry_id: int) -> dict | None:
        """One history entry with its replay info (None if it can't be played again)."""
        with self._lock:
            row = self._db.execute(
                "SELECT kind, source, title, channel, replay FROM history WHERE id = ?", (entry_id,)
            ).fetchone()
        if not row:
            return None
        kind, source, title, channel, replay = row
        return {"kind": kind, "source": source, "title": title, "channel": channel,
                "replay": json.loads(replay) if replay_available(replay) else None}

    def replay_of(self, entry_id: int) -> tuple[str, dict] | None:
        """(channel, replay info) of one history entry, if it can be listened to again."""
        with self._lock:
            row = self._db.execute("SELECT channel, replay FROM history WHERE id = ?", (entry_id,)).fetchone()
        if not row or not replay_available(row[1]):
            return None
        return row[0], json.loads(row[1])

    def open_program(self, channel: str, name: str, since: float) -> tuple[int, float, dict] | None:
        """(id, start, replay info) of program `name` when its "started" entry is still the
        newest program entry of the channel: it was on air when the server restarted."""
        with self._lock:
            row = self._db.execute(
                "SELECT id, ts, title, status, replay FROM history WHERE kind = 'program' AND channel = ?"
                " ORDER BY id DESC LIMIT 1",
                (channel,),
            ).fetchone()
        if not row or row[2] != name or row[3] != "started" or row[1] < since:
            return None
        try:
            replay = json.loads(row[4]) if row[4] else {}
        except ValueError:
            replay = {}
        return row[0], row[1], replay

    def set_replay(self, entry_id: int, replay: dict | None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE history SET replay = ? WHERE id = ?",
                             (json.dumps(replay) if replay else "", entry_id))

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
