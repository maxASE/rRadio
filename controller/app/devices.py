"""Device keys for apps: paired by QR code, each with its own role and channels.

A key is shown once at pairing and only its SHA-256 is stored, in state.db
(not config.yaml, so exports and backups never carry it). Roles:

- admin: everything the web UI can do.
- member: listen and control its own channels (status, skip, queue, cut-ins,
  station), but no settings, sources or schedule.

A device paired by a user belongs to that user (`user_id`): it is a member
that can control the user's own channels and listen to public ones.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field

ROLES = ("admin", "member")
PAIRING_MINUTES = 10
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O or 1/I for typing by hand


@dataclass
class Device:
    id: str
    name: str
    role: str
    channels: list[str]  # empty = all channels
    created: float
    last_seen: float | None = None
    user_id: str = ""  # the user who paired it; "" = paired by the admin

    def may_use(self, cid: str) -> bool:
        return self.role == "admin" or not self.channels or cid in self.channels

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "role": self.role, "channels": self.channels,
                "created": self.created, "last_seen": self.last_seen, "user_id": self.user_id}


@dataclass
class Pairing:
    code: str
    name: str
    role: str
    channels: list[str]
    expires: float
    attempts: int = field(default=0)
    user_id: str = ""


# The device behind the current request; None for the web UI (Basic auth or no password).
current: ContextVar[Device | None] = ContextVar("rradio_device", default=None)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Devices:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock):
        self._db, self._lock = db, lock
        self._pairings: dict[str, Pairing] = {}
        self._failures: list[float] = []
        # A fixed code that pairs any number of phones as listeners (all channels, no admin
        # rights) and never expires, for a demo radio that app store reviewers use
        # (REVIEW_PAIRING_CODE, 8+ letters). Never set it on a radio with real people's data.
        code = self._norm(os.environ.get("REVIEW_PAIRING_CODE", ""))
        self.review_code = code if len(code) >= 8 else ""
        with self._lock, self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    role TEXT NOT NULL,
                    channels TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL,
                    last_seen REAL
                )"""
            )
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(devices)")}
            if "user_id" not in cols:
                self._db.execute("ALTER TABLE devices ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def _row(r) -> Device:
        return Device(id=r[0], name=r[1], role=r[2], channels=[c for c in r[3].split(",") if c],
                      created=r[4], last_seen=r[5], user_id=r[6])

    def list(self, user_id: str | None = None) -> list[Device]:
        """All devices, or only those of one user."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, name, role, channels, created, last_seen, user_id FROM devices ORDER BY created"
            ).fetchall()
        return [d for d in map(self._row, rows) if user_id is None or d.user_id == user_id]

    def proof(self, device_id: str, nonce: str) -> str | None:
        """Proves this server knows a device's key without the key travelling: HMAC-SHA256
        of [nonce], keyed with the key's hash. Nil for a device it doesn't know."""
        with self._lock:
            r = self._db.execute("SELECT token_hash FROM devices WHERE id=?", (device_id,)).fetchone()
        if r is None:
            return None
        return hmac.new(r[0].encode(), nonce.encode(), hashlib.sha256).hexdigest()

    def by_token(self, token: str) -> Device | None:
        with self._lock:
            r = self._db.execute(
                "SELECT id, name, role, channels, created, last_seen, user_id FROM devices WHERE token_hash=?",
                (_hash(token),),
            ).fetchone()
        if r is None:
            return None
        dev = self._row(r)
        now = time.time()
        if not dev.last_seen or now - dev.last_seen > 60:  # one write a minute is plenty
            with self._lock, self._db:
                self._db.execute("UPDATE devices SET last_seen=? WHERE id=?", (now, dev.id))
            dev.last_seen = now
        return dev

    def update(self, did: str, name: str | None = None, role: str | None = None,
               channels: list[str] | None = None) -> Device | None:
        sets, args = [], []
        if name is not None:
            sets.append("name=?"); args.append(name.strip()[:60] or "Device")
        if role is not None:
            if role not in ROLES:
                raise ValueError(f"role must be one of {', '.join(ROLES)}")
            sets.append("role=?"); args.append(role)
        if channels is not None:
            sets.append("channels=?"); args.append(",".join(channels))
        if sets:
            with self._lock, self._db:
                self._db.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id=?", (*args, did))
        return next((d for d in self.list() if d.id == did), None)

    def delete(self, did: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM devices WHERE id=?", (did,)).rowcount > 0

    def delete_user(self, user_id: str) -> None:
        """Revoke every device of a user (when the user is removed)."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM devices WHERE user_id=?", (user_id,))
        for c in [c for c, p in self._pairings.items() if p.user_id == user_id]:
            del self._pairings[c]

    # --- pairing -------------------------------------------------------------------

    def start_pairing(self, name: str, role: str, channels: list[str], user_id: str = "") -> Pairing:
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        self._expire()
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(10))
        p = Pairing(code=code, name=name.strip()[:60] or "Device", role=role, channels=channels,
                    expires=time.time() + PAIRING_MINUTES * 60, user_id=user_id)
        self._pairings[code] = p
        return p

    def cancel_pairing(self, code: str) -> None:
        self._pairings.pop(self._norm(code), None)

    def pending(self, user_id: str | None = None) -> list[Pairing]:
        self._expire()
        return [p for p in self._pairings.values() if user_id is None or p.user_id == user_id]

    def redeem(self, code: str, device_name: str = "") -> tuple[Device, str]:
        """Swap a one-time pairing code for a device and its key (shown only now)."""
        now = time.time()
        self._failures = [t for t in self._failures if now - t < 60]
        if len(self._failures) >= 20:
            raise PermissionError("too many wrong codes, wait a minute")
        self._expire()
        if self.review_code and self._norm(code) == self.review_code:
            p = Pairing(code="", name="Review phone", role="member", channels=[], expires=now, user_id="")
        else:
            p = self._pairings.pop(self._norm(code), None)
        if p is None:
            self._failures.append(now)
            raise LookupError("unknown or expired pairing code")
        token = "rr_" + secrets.token_urlsafe(32)
        dev = Device(id=secrets.token_hex(4), name=(device_name.strip()[:60] or p.name), role=p.role,
                     channels=p.channels, created=now, last_seen=now, user_id=p.user_id)
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO devices (id, name, token_hash, role, channels, created, last_seen, user_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (dev.id, dev.name, _hash(token), dev.role, ",".join(dev.channels), dev.created, dev.last_seen, dev.user_id),
            )
        return dev, token

    @staticmethod
    def _norm(code: str) -> str:
        return "".join(ch for ch in code.upper() if ch.isalnum())

    def _expire(self) -> None:
        now = time.time()
        for c in [c for c, p in self._pairings.items() if p.expires < now]:
            del self._pairings[c]
