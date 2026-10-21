"""Music from Navidrome, or any other server that speaks the Subsonic API."""

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


class Navidrome:
    def __init__(self, url: str, user: str, password: str):
        self.base = url.rstrip("/")
        self.user = user
        self.password = password

    @classmethod
    def from_settings(cls, st: Settings) -> "Navidrome":
        if not st.navidrome_url or not st.navidrome_user:
            raise RuntimeError("Navidrome is not set up (Settings → Navidrome)")
        return cls(st.navidrome_url, st.navidrome_user, st.navidrome_secret)

    def _auth(self) -> dict:
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        return {"u": self.user, "t": token, "s": salt, "v": API_VERSION, "c": "rradio"}

    async def _get(self, endpoint: str, **params) -> dict:
        async with client() as c:
            try:
                r = await c.get(f"{self.base}/rest/{endpoint}", params={**self._auth(), "f": "json", **params})
                r.raise_for_status()
            except Exception as e:
                raise RuntimeError(scrub(f"{type(e).__name__}: {e}")) from None
        data = r.json().get("subsonic-response", {})
        if data.get("status") != "ok":
            raise RuntimeError(data.get("error", {}).get("message", "Navidrome refused the request"))
        return data

    async def ping(self) -> str:
        data = await self._get("ping")
        return data.get("serverVersion") or data.get("version", "?")

    async def playlists(self) -> list[dict]:
        data = await self._get("getPlaylists")
        return [
            {"id": p["id"], "name": p.get("name", p["id"]), "songs": p.get("songCount", 0)}
            for p in data.get("playlists", {}).get("playlist", [])
        ]

    async def playlist_songs(self, playlist: str) -> list[Song]:
        """Songs of a playlist given by id or (case-insensitive) name."""
        lists = await self.playlists()
        match = next((p for p in lists if p["id"] == playlist), None) or next(
            (p for p in lists if p["name"].lower() == playlist.lower()), None
        )
        if not match:
            raise LookupError(f"no Navidrome playlist called {playlist!r}")
        data = await self._get("getPlaylist", id=match["id"])
        return [_song(s) for s in data.get("playlist", {}).get("entry", [])]

    async def random_songs(self, size: int = 50) -> list[Song]:
        data = await self._get("getRandomSongs", size=size)
        return [_song(s) for s in data.get("randomSongs", {}).get("song", [])]

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
        id=s["id"],
        title=s.get("title", s["id"]),
        artist=s.get("artist", ""),
        album=s.get("album", ""),
        duration=float(s["duration"]) if s.get("duration") else None,
        cover=s.get("coverArt", ""),
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
