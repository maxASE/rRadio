"""Browser log-in sessions for the web UI (a cookie instead of the Basic-auth popup).

Like device keys, only a SHA-256 of each session token is stored, in state.db,
so a restart keeps people logged in and a backup never carries one.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time

COOKIE = "rradio_session"
DAYS = 30
_MAX_FAILURES = 10  # wrong passwords per address ...
_WINDOW = 300  # ... in this many seconds


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock):
        self._db, self._lock = db, lock
        self._failures: dict[str, list[float]] = {}
        with self._lock, self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    created REAL NOT NULL,
                    expires REAL NOT NULL
                )"""
            )

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions WHERE expires < ?", (now,))
            self._db.execute("INSERT INTO sessions (token_hash, created, expires) VALUES (?, ?, ?)",
                             (_hash(token), now, now + DAYS * 86400))
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            row = self._db.execute("SELECT expires FROM sessions WHERE token_hash=?", (_hash(token),)).fetchone()
        return row is not None and row[0] > time.time()

    def end(self, token: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions WHERE token_hash=?", (_hash(token),))

    def end_all(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM sessions")

    # --- wrong passwords -------------------------------------------------------------

    def blocked(self, addr: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(addr, []) if now - t < _WINDOW]
        self._failures[addr] = recent
        return len(recent) >= _MAX_FAILURES

    def failed(self, addr: str) -> None:
        self._failures.setdefault(addr, []).append(time.time())
