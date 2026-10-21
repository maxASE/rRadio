"""Reads the stream title (ICY metadata) of radio streams, without playing them.

Title-triggered programs use this to see when a show starts and ends. A
watcher only runs while a program's window is open: it keeps one HTTP
connection to the stream, throws the audio away and keeps the titles.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from .sources import USER_AGENT

log = logging.getLogger("titles")

LOG_SIZE = 40
_TITLE_RE = re.compile(r"StreamTitle='(.*?)';(?=\s*\w+=|\s*$)", re.S)
_PLAYLISTS = ("mpegurl", "scpls", "x-pls")


def parse_title(block: bytes) -> str | None:
    """The StreamTitle of one ICY metadata block, or None if it has none."""
    raw = block.rstrip(b"\0")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    m = _TITLE_RE.search(text)
    return m.group(1).strip() if m else None


def _playlist_target(text: str) -> str | None:
    """First stream URL in an .m3u or .pls playlist."""
    if "#EXT-X-" in text:
        raise ValueError("HLS streams (.m3u8) carry no stream titles that can be read here")
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("file") and "=" in line:
            line = line.split("=", 1)[1].strip()
        if line.startswith(("http://", "https://")):
            return line
    return None


async def read_titles(url: str, on_title, stop_after: int | None = None) -> None:
    """Connect to `url` and call on_title(title) for each title it sends.

    Returns after `stop_after` titles (None = until the stream ends)."""
    headers = {"Icy-MetaData": "1", "User-Agent": USER_AGENT}
    timeout = httpx.Timeout(15, read=60)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=headers) as c:
        for _ in range(3):  # follow playlists that point to the stream
            async with c.stream("GET", url) as r:
                r.raise_for_status()
                ctype = r.headers.get("content-type", "").lower()
                metaint = int(r.headers.get("icy-metaint", "0") or 0)
                if not metaint and (any(p in ctype for p in _PLAYLISTS) or url.lower().endswith((".m3u", ".pls"))):
                    body = (await r.aread())[:65536].decode("utf-8", "replace")
                    target = _playlist_target(body)
                    if not target:
                        raise ValueError("the playlist has no stream URL")
                    url = target
                    continue
                if not metaint:
                    raise ValueError("this stream sends no titles (no ICY metadata)")
                seen = 0
                buf = bytearray()
                need, in_meta = metaint, False
                async for chunk in r.aiter_raw():
                    buf += chunk
                    while True:
                        if not in_meta:
                            if len(buf) < need:
                                need -= len(buf)
                                buf.clear()
                                break
                            del buf[:need]
                            in_meta, need = True, -1
                        if need == -1:
                            if not buf:
                                break
                            need = buf[0] * 16
                            del buf[0]
                        if len(buf) < need:
                            break
                        block = bytes(buf[:need])
                        del buf[:need]
                        in_meta, need = False, metaint
                        if block:
                            title = parse_title(block)
                            if title is not None:
                                on_title(title)
                                seen += 1
                                if stop_after and seen >= stop_after:
                                    return
                return
        raise ValueError("too many nested playlists")


async def peek(url: str, timeout: float = 20) -> str:
    """The title a stream sends right now."""
    found: list[str] = []
    await asyncio.wait_for(read_titles(url, found.append, stop_after=1), timeout)
    if not found:
        raise ValueError("the stream ended before it sent a title")
    return found[0]


@dataclass
class Watcher:
    url: str
    title: str | None = None
    title_at: float = 0.0
    connected: bool = False
    error: str = ""
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_SIZE))  # (ts, title), newest last
    task: asyncio.Task | None = None

    def _on_title(self, title: str) -> None:
        self.connected, self.error = True, ""
        if title != self.title:
            self.title, self.title_at = title, time.time()
            self.log.append((self.title_at, title))
            log.info("Title on %s: %s", self.url, title)

    async def run(self) -> None:
        backoff = 5
        while True:
            try:
                await read_titles(self.url, self._on_title)
                self.error = "the stream ended"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.error = (str(e) or type(e).__name__)[:300]
                log.warning("Title watcher for %s: %s", self.url, self.error)
            if self.connected:
                backoff = 5
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)

    def public(self) -> dict:
        return {
            "title": self.title,
            "title_at": self.title_at or None,
            "watching": self.task is not None,
            "connected": self.connected,
            "error": self.error,
            "log": [{"ts": ts, "title": t} for ts, t in reversed(self.log)],
        }


class Watchers:
    """One watcher per stream URL, running only while some program needs it."""

    def __init__(self):
        self.by_url: dict[str, Watcher] = {}

    def want(self, urls: set[str]) -> None:
        for url in urls:
            w = self.by_url.setdefault(url, Watcher(url))
            if w.task is None:
                w.title, w.connected, w.error = None, False, ""
                w.task = asyncio.create_task(w.run())
        for url, w in self.by_url.items():
            if url not in urls and w.task is not None:
                w.task.cancel()
                w.task, w.connected = None, False
        # Forget logs of streams no program uses any more.
        for url in [u for u, w in self.by_url.items() if w.task is None and not w.log]:
            del self.by_url[url]

    def get(self, url: str) -> Watcher | None:
        return self.by_url.get(url)

    def stop(self) -> None:
        self.want(set())
