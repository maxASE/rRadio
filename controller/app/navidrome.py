"""Music from Navidrome, or any other server that speaks the Subsonic API
(Gonic, Airsonic, Ampache, LMS, Funkwhale, …)."""

from __future__ import annotations

import hashlib
import random
import re
import secrets
from dataclasses import dataclass, field

from .config import BaseSource, Settings
from .sources import client

API_VERSION = "1.16.1"


def scrub(text: str) -> str:
    """Remove Subsonic credentials from URLs in error messages."""
    return re.sub(r"([?&](?:t|s|p|u)=)[^&\s'\"]+", r"\1…", text)


@dataclass
class Song:
    id: str
    title: str
    artist: str
    album: str = ""
    duration: float | None = None
    cover: str = ""  # cover art id


# Servers that refused token login and take the password instead, by (url, user).
# Funkwhale and LDAP users on Navidrome or Airsonic only accept the password.
_password_login: set[tuple[str, str]] = set()


def _items(value) -> list[dict]:
    """A list from the JSON, which older servers send as a bare object when there is one item."""
    if not value:
        return []
    return value if isinstance(value, list) else [value]


class Navidrome:
    """A Subsonic API client. Named after Navidrome, the server most people run."""

    def __init__(self, url: str, user: str, password: str):
        # Accept the API address too (…/rest), as some servers show that one.
        self.base = re.sub(r"/rest/?$", "", url.strip().rstrip("/"))
        self.user = user
        self.password = password

    @classmethod
    def from_settings(cls, st: Settings) -> "Navidrome":
        if not st.navidrome_url or not st.navidrome_user:
            raise RuntimeError("no music server set up (Settings → Music server)")
        return cls(st.navidrome_url, st.navidrome_user, st.navidrome_secret)

    @property
    def _with_password(self) -> bool:
        return (self.base, self.user) in _password_login

    def _auth(self) -> dict:
        common = {"u": self.user, "v": API_VERSION, "c": "rradio"}
        if self._with_password:
            return common | {"p": "enc:" + self.password.encode().hex()}
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return common | {"t": token, "s": salt}

    async def _get(self, endpoint: str, **params) -> dict:
        data = await self._request(endpoint, params)
        # 41: token login not supported. Some servers answer 40 (wrong user or
        # password) instead, so try the password once before giving up.
        code = data.get("error", {}).get("code")
        if data.get("status") != "ok" and code in (40, 41) and not self._with_password:
            _password_login.add((self.base, self.user))
            retry = await self._request(endpoint, params)
            if retry.get("status") == "ok":
                return retry
            _password_login.discard((self.base, self.user))
        if data.get("status") != "ok":
            raise RuntimeError(data.get("error", {}).get("message", "the music server refused the request"))
        return data

    async def _request(self, endpoint: str, params: dict) -> dict:
        async with client() as c:
            try:
                r = await c.get(f"{self.base}/rest/{endpoint}", params={**self._auth(), "f": "json", **params})
                r.raise_for_status()
                body = r.json()
            except ValueError:
                raise RuntimeError(f"{self.base} answered, but not with Subsonic JSON. Check the URL.") from None
            except Exception as e:
                raise RuntimeError(scrub(f"{type(e).__name__}: {e}")) from None
        return body.get("subsonic-response", {})

    async def ping(self) -> str:
        """The server's name and version, e.g. "navidrome 0.53.3"."""
        data = await self._get("ping")
        version = data.get("serverVersion") or data.get("version", "?")
        return f"{data['type']} {version}" if data.get("type") else version

    async def playlists(self) -> list[dict]:
        data = await self._get("getPlaylists")
        return [
            {"id": str(p["id"]), "name": p.get("name", str(p["id"])), "songs": p.get("songCount", 0)}
            for p in _items(data.get("playlists", {}).get("playlist"))
        ]

    async def playlist_songs(self, playlist: str) -> list[Song]:
        """Songs of a playlist given by id or (case-insensitive) name."""
        lists = await self.playlists()
        match = next((p for p in lists if p["id"] == playlist), None) or next(
            (p for p in lists if p["name"].lower() == playlist.lower()), None
        )
        if not match:
            raise LookupError(f"no playlist called {playlist!r} on the music server")
        data = await self._get("getPlaylist", id=match["id"])
        return [_song(s) for s in _items(data.get("playlist", {}).get("entry"))]

    async def random_songs(self, size: int = 50) -> list[Song]:
        data = await self._get("getRandomSongs", size=size)
        return [_song(s) for s in _items(data.get("randomSongs", {}).get("song"))]

    def stream_url(self, song_id: str, bitrate: int) -> str:
        params = {**self._auth(), "id": song_id}
        if bitrate:
            params |= {"format": "mp3", "maxBitRate": bitrate}
        else:
            params["format"] = "raw"
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.base}/rest/stream?{query}"

    def cover_url(self, cover_id: str, size: int = 300) -> str:
        params = {**self._auth(), "id": cover_id, "size": size}
        return f"{self.base}/rest/getCoverArt?" + "&".join(f"{k}={v}" for k, v in params.items())


def _song(s: dict) -> Song:
    return Song(
        id=str(s["id"]),
        title=s.get("title", str(s["id"])),
        artist=s.get("artist", ""),
        album=s.get("album", ""),
        duration=float(s["duration"]) if s.get("duration") else None,
        cover=str(s.get("coverArt", "")),
    )


@dataclass
class Feeder:
    """Hands out the next song for one station: a playlist in order or
    shuffled, or random songs from the whole library."""

    key: str
    pending: list[Song] = field(default_factory=list)

    async def next(self, nd: Navidrome, src: BaseSource) -> Song:
        if not self.pending:
            if src.playlist:
                songs = await nd.playlist_songs(src.playlist)
                if src.shuffle:
                    random.shuffle(songs)
            else:
                songs = await nd.random_songs()
            if not songs:
                raise LookupError("no songs found" + (f" in {src.playlist!r}" if src.playlist else ""))
            self.pending = songs
        return self.pending.pop(0)
