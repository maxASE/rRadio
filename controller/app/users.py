"""User accounts: people who log in to the web UI and run channels of their own.

The admin is the UI_USERNAME/UI_PASSWORD login from .env (it is not stored
here); the admin adds users under Settings › Users. A user owns the channels
they create and can make one public, so that everyone else can listen to it
and see what is on, but not change it. Users with the role "admin" can do
everything the admin can.

Passwords are stored as scrypt hashes in state.db, never in config.yaml, so
exports and backups never carry them.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass

ROLES = ("user", "admin")
MIN_PASSWORD = 8
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


@dataclass
class User:
    id: str
    name: str
    role: str
    created: float

    @property
    def admin(self) -> bool:
        return self.role == "admin"

    @property
    def folder(self) -> str:
        """The user's own folder in the media library (with a trailing slash)."""
        return f"users/{self.name}/"

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "role": self.role, "created": self.created}


def hash_password(pw: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, salt, _digest = stored.split("$")
        return secrets.compare_digest(hash_password(pw, bytes.fromhex(salt)), stored)
    except ValueError:
        return False


class Users:
    def __init__(self, db: sqlite3.Connection, lock: threading.Lock, reserved: str = "admin"):
        self._db, self._lock = db, lock
        self.reserved = reserved.lower()  # the .env admin's name; no user may take it
        with self._lock, self._db:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    pw_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    created REAL NOT NULL
                )"""
            )

    @staticmethod
    def _row(r) -> User:
        return User(id=r[0], name=r[1], role=r[2], created=r[3])

    def list(self) -> list[User]:
        with self._lock:
            rows = self._db.execute("SELECT id, name, role, created FROM users ORDER BY name").fetchall()
        return [self._row(r) for r in rows]

    def get(self, uid: str) -> User | None:
        with self._lock:
            r = self._db.execute("SELECT id, name, role, created FROM users WHERE id=?", (uid,)).fetchone()
        return self._row(r) if r else None

    def by_name(self, name: str) -> User | None:
        with self._lock:
            r = self._db.execute("SELECT id, name, role, created FROM users WHERE name=?", (name.strip().lower(),)).fetchone()
        return self._row(r) if r else None

    def check(self, name: str, pw: str) -> User | None:
        """The user with this name and password, or None."""
        with self._lock:
            r = self._db.execute("SELECT id, name, role, created, pw_hash FROM users WHERE name=?",
                                 (name.strip().lower(),)).fetchone()
        if r is None:
            hash_password(pw)  # same time for unknown names
            return None
        return self._row(r) if check_password(pw, r[4]) else None

    def _name(self, name: str) -> str:
        name = name.strip().lower()
        if not _NAME_RE.match(name):
            raise ValueError("user names are lowercase letters, digits, '.', '-' or '_' (up to 32)")
        if name == self.reserved:
            raise ValueError(f"{name!r} is the admin's name")
        return name

    @staticmethod
    def _password(pw: str) -> str:
        if len(pw) < MIN_PASSWORD:
            raise ValueError(f"the password needs at least {MIN_PASSWORD} characters")
        return hash_password(pw)

    def create(self, name: str, password: str, role: str = "user") -> User:
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        user = User(id=secrets.token_hex(4), name=self._name(name), role=role, created=time.time())
        pw = self._password(password)
        try:
            with self._lock, self._db:
                self._db.execute("INSERT INTO users (id, name, pw_hash, role, created) VALUES (?, ?, ?, ?, ?)",
                                 (user.id, user.name, pw, user.role, user.created))
        except sqlite3.IntegrityError:
            raise ValueError(f"there is already a user called {user.name!r}") from None
        return user

    def update(self, uid: str, password: str | None = None, role: str | None = None) -> User | None:
        sets, args = [], []
        if password is not None:
            sets.append("pw_hash=?"); args.append(self._password(password))
        if role is not None:
            if role not in ROLES:
                raise ValueError(f"role must be one of {', '.join(ROLES)}")
            sets.append("role=?"); args.append(role)
        if sets:
            with self._lock, self._db:
                self._db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", (*args, uid))
        return self.get(uid)

    def delete(self, uid: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM users WHERE id=?", (uid,)).rowcount > 0
