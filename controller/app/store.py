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
from .users import Users

# How long the history keeps what was not saved.
HISTORY_DAYS = 7

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
        # Write-ahead log: a write no longer waits for a full sync of the database file,
        # which keeps the many small writes (history, seen items) quick on an SD card.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
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
                CREATE TABLE IF NOT EXISTS waiting (
                    key TEXT PRIMARY KEY,
                    item TEXT NOT NULL,
                    since REAL NOT NULL,
                    next_check REAL NOT NULL,
                    tries INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT ''
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
            if "saved" not in cols:
                # When an app saved the entry for later (0 = not saved); saved entries never age out.
                self._db.execute("ALTER TABLE history ADD COLUMN saved REAL NOT NULL DEFAULT 0")
            # Programs kept before their length was written down when they ended.
            with self._db:
                for entry_id, channel in self._db.execute(
                    "SELECT id, channel FROM history WHERE kind = 'program' AND status = 'ended'"
                ).fetchall():
                    self._note_program_length(channel, entry_id)
                # "waiting" lines of lives that have played (or were given up) since, from
                # before those lines were removed then.
                self._db.execute(
                    "DELETE FROM history WHERE kind = 'video' AND status = 'waiting' AND saved = 0 AND EXISTS"
                    " (SELECT 1 FROM history h WHERE h.kind = 'video' AND h.source = history.source"
                    " AND h.title = history.title AND h.status != 'waiting' AND h.id > history.id)")
        self.devices = Devices(self._db, self._lock)
        self.sessions = Sessions(self._db, self._lock)
        self.users = Users(self._db, self._lock, os.environ.get("UI_USERNAME", "admin"))

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
            self._db.execute("DELETE FROM waiting WHERE key LIKE ?", (source + ":%",))

    # Clips that can't be played yet (live videos, failed downloads), retried until they can.

    def wait_put(self, key: str, item: dict, since: float, next_check: float, tries: int, reason: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO waiting (key, item, since, next_check, tries, reason) VALUES (?, ?, ?, ?, ?, ?)",
                (key, json.dumps(item), since, next_check, tries, reason),
            )

    def wait_drop(self, key: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM waiting WHERE key=?", (key,)).rowcount > 0

    def waiting(self) -> list[dict]:
        """Oldest first."""
        with self._lock:
            rows = self._db.execute("SELECT key, item, since, next_check, tries, reason FROM waiting ORDER BY since").fetchall()
        keys = ["key", "item", "since", "next_check", "tries", "reason"]
        return [{**dict(zip(keys, r)), "item": json.loads(r[1])} for r in rows]

    def add_history(self, kind: str, source: str, title: str, status: str, detail: str = "", channel: str = "",
                    replay: dict | None = None) -> None:
        """channel "" means the entry is not about one channel (e.g. a filtered upload)."""
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO history (ts, kind, source, title, status, detail, channel, replay) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), kind, source, title, status, detail, channel, json.dumps(replay) if replay else ""),
            )
            if kind == "program" and status == "ended":
                self._note_program_length(channel)
            # Unsaved entries go after a week (the Week tab, its numbers and search look no
            # further back) and beyond the newest 500; saved ones stay until unsaved.
            self._db.execute(
                "DELETE FROM history WHERE saved = 0 AND (ts < ? OR id NOT IN (SELECT id FROM history ORDER BY id DESC LIMIT 500))",
                (time.time() - HISTORY_DAYS * 86400,),
            )

    def drop_waiting_entries(self, kind: str, source: str, title: str) -> None:
        """Removes the "waiting" entries of a clip that has stopped waiting (it plays now, or
        was given up), so a live shows once in the lists, not again for each step. Saved
        ones stay."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM history WHERE kind = ? AND source = ? AND title = ? AND status = 'waiting'"
                             " AND saved = 0", (kind, source, title))

    def _note_program_length(self, channel: str, until: int | None = None) -> None:
        """Writes the length of a kept program's recording into its "started" entry once the
        program ended (the entry just before the newest "ended" one, or before `until`), so
        the apps show it before playing, like a clip's. Called within the lock."""
        rows = self._db.execute(
            "SELECT id, ts, status, replay FROM history WHERE kind = 'program' AND channel = ?"
            " AND (? IS NULL OR id <= ?) ORDER BY id DESC LIMIT 2",
            (channel, until, until),
        ).fetchall()
        if len(rows) < 2 or rows[0][2] != "ended" or rows[1][2] != "started" or not rows[1][3]:
            return
        try:
            info = json.loads(rows[1][3])
        except ValueError:
            return
        if not info.get("path") or info.get("duration"):
            return
        length = round(rows[0][1] - (info.get("from") or rows[1][1]))
        if length > 0:
            self._db.execute("UPDATE history SET replay = ? WHERE id = ?",
                             (json.dumps({**info, "duration": length}), rows[1][0]))

    _ENTRY = "SELECT id, ts, kind, source, title, status, detail, replay, saved FROM history"

    @staticmethod
    def _entries(rows) -> list[dict]:
        keys = ["id", "ts", "kind", "source", "title", "status", "detail"]
        return [{**dict(zip(keys, r)), "replay": replay_available(r[7]),
                 "art": replay_field(r[7], "art") or "", "replay_from": replay_field(r[7], "from"),
                 "duration": replay_field(r[7], "duration"), "saved": r[8] or None} for r in rows]

    def history(self, limit: int = 30, channel: str | None = None) -> list[dict]:
        """Entries for one channel plus the ones that concern all channels. Video and
        podcast entries without a channel (skipped before any channel got them, from older
        versions) stay out: they may come from someone else's private sources."""
        with self._lock:
            rows = self._db.execute(
                self._ENTRY + " WHERE ? IS NULL OR (channel = ? OR (channel = '' AND kind NOT IN ('video', 'podcast'))) ORDER BY id DESC LIMIT ?",
                (channel, channel, limit),
            ).fetchall()
        return self._entries(rows)

    def saved(self, channel: str) -> list[dict]:
        """The channel's entries saved for later, the last saved first."""
        with self._lock:
            rows = self._db.execute(
                self._ENTRY + " WHERE saved > 0 AND (channel = ? OR (channel = '' AND kind NOT IN ('video', 'podcast'))) ORDER BY saved DESC", (channel,)
            ).fetchall()
        return self._entries(rows)

    def search(self, channel: str, text: str, limit: int = 30) -> list[dict]:
        """Entries of the channel whose title or source contains `text`, newest first; a
        program shows up once (its start), not again for its end."""
        like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        with self._lock:
            rows = self._db.execute(
                self._ENTRY + " WHERE (channel = ? OR (channel = '' AND kind NOT IN ('video', 'podcast'))) AND (title LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\')"
                " AND NOT (kind = 'program' AND status = 'ended') ORDER BY id DESC LIMIT ?",
                (channel, like, like, limit),
            ).fetchall()
        return self._entries(rows)

    def between(self, channel: str, start: float, end: float) -> list[dict]:
        """Every entry of a channel between two times, oldest first, for the week's numbers."""
        with self._lock:
            rows = self._db.execute(
                self._ENTRY + " WHERE channel = ? AND ts >= ? AND ts < ? ORDER BY id", (channel, start, end)
            ).fetchall()
        return self._entries(rows)

    def set_saved(self, entry_id: int, saved: bool) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("UPDATE history SET saved = ? WHERE id = ?", (time.time() if saved else 0, entry_id))
        return cur.rowcount > 0

    def saved_replays(self) -> list[tuple[int, dict]]:
        """(id, replay info) of every saved entry that has a kept file."""
        with self._lock:
            rows = self._db.execute("SELECT id, replay FROM history WHERE saved > 0 AND replay LIKE '%\"path\"%'").fetchall()
        out = []
        for entry_id, raw in rows:
            try:
                out.append((entry_id, json.loads(raw)))
            except ValueError:
                pass
        return out

    def programs_between(self, channel: str, start: float, end: float) -> list[dict]:
        """A channel's program starts and ends between two times, oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, ts, title, status, replay FROM history"
                " WHERE kind = 'program' AND channel = ? AND ts >= ? AND ts < ? ORDER BY id",
                (channel, start, end),
            ).fetchall()
        return [{"id": r[0], "ts": r[1], "title": r[2], "status": r[3], "replay": replay_available(r[4])} for r in rows]

    def entry(self, entry_id: int) -> dict | None:
        """One history entry with its replay info (None if it can't be played again)."""
        with self._lock:
            row = self._db.execute(
                "SELECT kind, source, title, channel, replay, saved FROM history WHERE id = ?", (entry_id,)
            ).fetchone()
        if not row:
            return None
        kind, source, title, channel, replay, saved = row
        return {"kind": kind, "source": source, "title": title, "channel": channel,
                "replay": json.loads(replay) if replay_available(replay) else None, "saved": saved or None}

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

    def last_program(self, channel: str) -> dict | None:
        """The newest program entry ("started" or "ended") of a channel."""
        with self._lock:
            row = self._db.execute(
                "SELECT title, status FROM history WHERE kind = 'program' AND channel = ? ORDER BY id DESC LIMIT 1",
                (channel,),
            ).fetchone()
        return {"title": row[0], "status": row[1]} if row else None

    def ended_since(self, channel: str, name: str, since: float) -> float | None:
        """When program `name` last ended on the channel, if that was after `since`."""
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(ts) FROM history WHERE kind = 'program' AND status = 'ended' AND channel = ? AND title = ?"
                " AND ts >= ?",
                (channel, name, since),
            ).fetchone()
        return row[0] if row and row[0] else None

    def set_replay(self, entry_id: int, replay: dict | None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE history SET replay = ? WHERE id = ?",
                             (json.dumps(replay) if replay else "", entry_id))

    def replay_users(self, path: str) -> int:
        """How many history entries are kept in file `path`."""
        with self._lock:
            rows = self._db.execute("SELECT replay FROM history WHERE replay LIKE ?", (f"%{Path(path).name}%",)).fetchall()
        return sum(1 for (raw,) in rows if json.loads(raw).get("path") == path)

    def replay_owners(self, paths: list[str]) -> dict[str, dict]:
        """For each kept file in `paths`, the first history entry kept in it (title, channel, ts)."""
        names = {Path(p).name for p in paths}
        found: dict[str, dict] = {}
        with self._lock:
            rows = self._db.execute("SELECT ts, title, channel, replay FROM history WHERE replay != '' ORDER BY ts").fetchall()
        for ts, title, channel, raw in rows:
            path = json.loads(raw).get("path")
            if path in paths and path not in found and Path(path).name in names:
                found[path] = {"title": title, "channel": channel, "ts": ts}
        return found

    def move_replay(self, old: str, new: str) -> None:
        """Points the entries kept in file `old` at `new` (a recording moved to disk)."""
        with self._lock, self._db:
            rows = self._db.execute("SELECT id, replay FROM history WHERE replay LIKE ?", (f"%{Path(old).name}%",)).fetchall()
            for entry_id, raw in rows:
                info = json.loads(raw)
                if info.get("path") == old:
                    self._db.execute("UPDATE history SET replay = ? WHERE id = ?", (json.dumps({**info, "path": new}), entry_id))

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
