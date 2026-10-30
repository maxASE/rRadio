"""Fetching new items from Invidious and podcast feeds, and downloading audio."""

from __future__ import annotations

import asyncio
import calendar
import dataclasses
import hashlib
import logging
import os
import re
import shutil
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse, parse_qs

import feedparser
import httpx

from .config import Filters, Podcast, VideoChannel

log = logging.getLogger("sources")

CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/data/cache"))
# A RAM disk (tmpfs) shared with Liquidsoap; used when "keep downloads in RAM" is on.
RAM_CACHE_DIR = Path(os.environ.get("RAM_CACHE_DIR", "/ram"))


# A download or recording needs at least this much room on the RAM disk, or it goes to disk.
RAM_MIN_FREE = 64 << 20


def ram_usage() -> dict | None:
    """The RAM disk (RAM_CACHE_SIZE) in bytes, and whether it is nearly full (under 15 % or
    128 MB free), for a warning in the web UI. None without a RAM disk."""
    if not RAM_CACHE_DIR.is_dir():
        return None
    u = shutil.disk_usage(RAM_CACHE_DIR)
    return {"size": u.total, "used": u.used, "low": u.free < max(u.total * 0.15, 128 << 20)}


def cache_dir(in_ram: bool) -> Path:
    """Where downloads go. Falls back to disk if no RAM disk is mounted, or it is nearly full."""
    if (in_ram and RAM_CACHE_DIR.is_dir() and os.access(RAM_CACHE_DIR, os.W_OK)
            and shutil.disk_usage(RAM_CACHE_DIR).free >= RAM_MIN_FREE):
        return RAM_CACHE_DIR
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR
USER_AGENT = "PirateRadio/1.0 (+https://github.com/)"
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass
class Item:
    kind: str  # video | podcast | manual | spot | media
    source_id: str
    source_name: str
    item_id: str
    title: str
    media: str  # video id for video, audio URL otherwise
    duration: float | None = None
    published: float | None = None
    arrived: float = field(default_factory=time.time)
    state: str = "waiting"  # waiting, downloading, ready, queued, playing, failed
    path: str | None = None
    rid: int | None = None
    pushed_at: float = 0.0
    progress: float | None = None
    error: str = ""
    channel: str = ""
    ready_at: float = 0.0
    priority: int = 0  # spots and dropped-in files jump ahead of other clips
    keep_file: bool = False  # own media: never delete after playing
    between_songs: bool = False  # wait for the next song change on the station
    during_programs: bool = False  # may play while a scheduled program is on
    art: str = ""  # picture to show while it plays; fetched by the controller, never the browser
    live: str = ""  # "live", "upcoming" or "processing": a live video Invidious can't play yet
    wait_since: float = 0.0  # on the waiting list since then (lives and failed downloads)
    wait_tries: int = 0

    @property
    def key(self) -> str:
        """Same for one item on every channel, so it downloads only once."""
        return f"{self.source_id}:{self.item_id}"

    @property
    def uid(self) -> str:
        return f"{self.channel}/{self.key}"

    def copy_for(self, channel: str) -> "Item":
        return dataclasses.replace(self, channel=channel)

    def public(self) -> dict:
        return {
            "key": self.uid,
            "kind": self.kind,
            "source": self.source_name,
            "title": self.title,
            "duration": self.duration,
            "arrived": self.arrived,
            "state": self.state,
            "progress": self.progress,
            "error": self.error,
            "during_programs": self.during_programs,
        }


def passes_filters(f: Filters, title: str, duration: float | None) -> str | None:
    """Return a reason string if the item should be skipped."""
    if duration is not None:
        if f.min_minutes and duration < f.min_minutes * 60:
            return "too short"
        if f.max_minutes and duration > f.max_minutes * 60:
            return "too long"
    if f.include and not re.search(f.include, title, re.I):
        return "does not match include filter"
    if f.exclude and re.search(f.exclude, title, re.I):
        return "matches exclude filter"
    return None


def client(timeout: float = 30) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


# One client per event loop (and factory, which tests replace) for short API calls, so
# polls of Invidious, feeds and the music server reuse their connections.
_shared: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict]" = weakref.WeakKeyDictionary()


def shared(factory: Callable[[], httpx.AsyncClient] | None = None) -> httpx.AsyncClient:
    """The shared client made by `factory` (default: client()). Never close it."""
    factory = factory or client
    per_loop = _shared.setdefault(asyncio.get_running_loop(), {})
    c = per_loop.get(factory)
    if c is None or c.is_closed:
        c = per_loop[factory] = factory()
    return c


async def close_shared() -> None:
    for c in _shared.pop(asyncio.get_running_loop(), {}).values():
        await c.aclose()


# --- Invidious -------------------------------------------------------------


# Channel avatars by channel id; they rarely change, so one lookup per run.
_avatars: dict[str, str] = {}


class Invidious:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def image(self, url: str) -> str:
        """An image URL from Invidious, routed through Invidious so the video site never sees us."""
        if url.startswith("//"):
            url = "https:" + url
        m = re.match(r"https?://yt\d*\.ggpht\.com/(.+)", url)
        if m:
            return f"{self.base}/ggpht/{m.group(1)}"
        return f"{self.base}{url}" if url.startswith("/") else url

    def avatar(self, thumbs: list[dict]) -> str:
        """The channel picture closest to 176 px wide."""
        thumbs = [t for t in thumbs or [] if t.get("url")]
        if not thumbs:
            return ""
        best = min(thumbs, key=lambda t: abs(int(t.get("width") or 0) - 176))
        return self.image(best["url"])

    def thumbnail(self, video_id: str) -> str:
        return f"{self.base}/vi/{video_id}/mqdefault.jpg"

    async def channel_avatar(self, ucid: str) -> str:
        if ucid not in _avatars:
            try:
                ch = await self._get(f"/api/v1/channels/{ucid}")
                _avatars[ucid] = self.avatar(ch.get("authorThumbnails", []))
            except Exception as e:
                log.info("avatar for %s failed: %s", ucid, e)
                return ""
        return _avatars[ucid]

    async def _get(self, path: str, **params) -> dict | list:
        r = await shared().get(f"{self.base}{path}", params=params)
        r.raise_for_status()
        return r.json()

    async def stats(self) -> dict:
        return await self._get("/api/v1/stats")

    async def resolve_channel(self, text: str) -> dict:
        """Accept a channel id, @handle, channel URL or a search term."""
        text = text.strip()
        ucid = None
        if re.fullmatch(r"UC[A-Za-z0-9_-]{22}", text):
            ucid = text
        else:
            m = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", text)
            if m:
                ucid = m.group(1)
            elif "://" in text:
                url = text
                data = await self._get("/api/v1/resolveurl", url=url)
                ucid = data.get("ucid")
        if ucid:
            ch = await self._get(f"/api/v1/channels/{ucid}")
            return {"channel_id": ucid, "name": ch.get("author", ucid)}
        results = await self._get("/api/v1/search", q=text, type="channel")
        for r in results:
            if r.get("type") == "channel":
                return {"channel_id": r["authorId"], "name": r["author"]}
        raise LookupError(f"no channel found for {text!r}")

    async def search_videos(self, text: str, limit: int = 8) -> list[dict]:
        """Videos for a search term, for "Play next" from the apps' search."""
        results = await self._get("/api/v1/search", q=text, type="video")
        out = []
        for v in results:
            if v.get("type") != "video" or v.get("liveNow") or v.get("isUpcoming") or not v.get("videoId"):
                continue
            out.append({"title": v.get("title", ""), "author": v.get("author", ""),
                        "url": v["videoId"],
                        "duration": v.get("lengthSeconds") or None, "art": self.thumbnail(v["videoId"])})
            if len(out) >= limit:
                break
        return out

    async def latest(self, ch: VideoChannel) -> list[Item]:
        """The channel's newest videos, plus its lives if `ch.lives` (the video site lists them
        on a separate Live tab). Lives that can't be played yet carry `live`."""
        data = await self._get(f"/api/v1/channels/{ch.channel_id}/videos")
        videos = data.get("videos", []) if isinstance(data, dict) else data
        if ch.lives:
            try:
                data = await self._get(f"/api/v1/channels/{ch.channel_id}/streams")
                videos = videos + (data.get("videos", []) if isinstance(data, dict) else data)
            except Exception as e:  # older Invidious, or a channel without lives
                log.info("lives of %s failed: %s", ch.name, e)
        avatar = await self.channel_avatar(ch.channel_id) if videos else ""
        items, ids = [], set()
        for v in videos:
            if v.get("premium") or not v.get("videoId") or v["videoId"] in ids:
                continue
            live = live_state(v)
            if live and not ch.lives:
                continue
            ids.add(v["videoId"])
            items.append(
                Item(
                    kind="video",
                    source_id=ch.id,
                    source_name=ch.name,
                    item_id=v["videoId"],
                    title=v.get("title", v["videoId"]),
                    media=v["videoId"],
                    duration=float(v.get("lengthSeconds") or 0) or None,
                    published=float(v["published"]) if v.get("published") else None,
                    art=avatar or self.thumbnail(v["videoId"]),
                    live=live,
                )
            )
        return items

    async def playable(self, video_id: str) -> tuple[str, dict]:
        """Whether Invidious can play a video now: ("", info) when it can, otherwise
        the reason it can't yet ("live", "upcoming", "processing") and what it said."""
        try:
            v = await self._get(f"/api/v1/videos/{video_id}")
        except httpx.HTTPStatusError as e:
            # Invidious answers lives and recordings still being processed with an error.
            return "processing", {"error": f"Invidious said {e.response.status_code}"}
        state = live_state(v)
        if not state and not any(str(f.get("type", "")).startswith("audio/") for f in v.get("adaptiveFormats", [])):
            state = "processing"
        return state, v

    async def video_item(self, video_id: str) -> Item:
        v = await self._get(f"/api/v1/videos/{video_id}")
        return Item(
            kind="manual",
            source_id="manual",
            source_name=v.get("author", "Video"),
            item_id=f"{video_id}-{int(time.time())}",
            title=v.get("title", video_id),
            media=video_id,
            duration=float(v.get("lengthSeconds") or 0) or None,
            art=self.avatar(v.get("authorThumbnails", [])) or self.thumbnail(video_id),
        )

    async def audio_urls(self, video_id: str, itag: str = "140") -> list[str]:
        """Candidate audio URLs, preferred itag first. All go through Invidious (local=true)."""
        urls = [f"{self.base}/latest_version?id={video_id}&itag={itag}&local=true"]
        try:
            v = await self._get(f"/api/v1/videos/{video_id}", local="true")
            fmts = [f for f in v.get("adaptiveFormats", []) if str(f.get("type", "")).startswith("audio/")]
            fmts.sort(key=lambda f: (str(f.get("itag")) != itag, -int(f.get("bitrate") or 0)))
            for f in fmts:
                u = f.get("url", "")
                urls.append(u if u.startswith("http") else f"{self.base}{u}")
        except Exception as e:  # the latest_version URL may still work
            log.info("video info for %s failed: %s", video_id, e)
        return urls


def live_state(v: dict) -> str:
    """Why a video from Invidious can't be played as a clip yet, or ""."""
    if v.get("liveNow"):
        return "live"
    if v.get("isUpcoming"):
        return "upcoming"
    if (v.get("lengthSeconds") or 0) <= 0:
        return "processing"
    return ""


def video_id_from_url(url: str) -> str | None:
    url = url.strip()
    if _VIDEO_ID.match(url):
        return url
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if _VIDEO_ID.match(p.path.strip("/")):  # short links
        return p.path.strip("/")[:11] or None
    if "v" in parse_qs(p.query):
        return parse_qs(p.query)["v"][0]
    m = re.search(r"/(?:shorts|live|embed)/([A-Za-z0-9_-]{11})", p.path)
    return m.group(1) if m else None


# --- Podcasts ---------------------------------------------------------------


def _parse_duration(v) -> float | None:
    if not v:
        return None
    try:
        parts = [float(p) for p in str(v).split(":")]
    except ValueError:
        return None
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return secs or None


async def podcast_latest(p: Podcast) -> list[Item]:
    r = await shared().get(p.url)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    cover = (feed.feed.get("image") or {}).get("href", "")
    items = []
    for e in feed.entries:
        enc = next(
            (x for x in e.get("enclosures", []) if str(x.get("type", "audio")).startswith(("audio", "video"))),
            None,
        )
        if not enc or not enc.get("href"):
            continue
        published = e.get("published_parsed") or e.get("updated_parsed")
        items.append(
            Item(
                kind="podcast",
                source_id=p.id,
                source_name=p.name or feed.feed.get("title", "Podcast"),
                item_id=hashlib.sha1((e.get("id") or enc["href"]).encode()).hexdigest()[:16],
                title=e.get("title", "Episode"),
                media=enc["href"],
                duration=_parse_duration(e.get("itunes_duration")),
                published=calendar.timegm(published) if published else None,
                art=(e.get("image") or {}).get("href", "") or cover,
            )
        )
    return items


async def feed_title(url: str) -> str:
    r = await shared().get(url)
    r.raise_for_status()
    return feedparser.parse(r.content).feed.get("title", "")


# --- Downloading -----------------------------------------------------------


def _ext_for(content_type: str, url: str) -> str:
    ct = content_type.split(";")[0].strip().lower()
    known = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/aac": ".aac",
        "audio/ogg": ".ogg",
        "audio/opus": ".opus",
        "audio/webm": ".webm",
        "audio/flac": ".flac",
        "audio/x-flac": ".flac",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
    }
    if ct in known:
        return known[ct]
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".webm", ".mp4", ".flac", ".wav"} else ".bin"


CHUNK = 4 << 20  # The video site throttles one-piece downloads; 4 MB ranges stay fast
DOWNLOAD_DEADLINE = 20 * 60


async def _download(url: str, stem: Path, progress: Callable[[float | None], None] | None = None) -> Path:
    """Download in Range chunks when the server allows it, reporting progress (0..1)."""
    async with asyncio.timeout(DOWNLOAD_DEADLINE), client(timeout=60) as c:
        got, total, path, tmp, f = 0, None, None, None, None
        try:
            while total is None or got < total:
                headers = {"Range": f"bytes={got}-{got + CHUNK - 1}"}
                async with c.stream("GET", url, headers=headers) as r:
                    r.raise_for_status()
                    if path is None:
                        ct = r.headers.get("content-type", "")
                        if ct.startswith(("text/", "application/json")):
                            raise RuntimeError(f"got {ct} instead of audio")
                        path = stem.with_suffix(_ext_for(ct, str(r.url)))
                        tmp = path.with_suffix(path.suffix + ".part")
                        f = tmp.open("wb")
                        url = str(r.url)  # reuse the redirect target for later ranges
                    if r.status_code == 206 and "/" in r.headers.get("content-range", ""):
                        total = int(r.headers["content-range"].rsplit("/", 1)[1])
                    else:  # no range support: this response is the whole file
                        total = None
                    async for chunk in r.aiter_bytes(1 << 16):
                        f.write(chunk)
                        got += len(chunk)
                        if progress is not None:
                            progress(got / total if total else None)
                    if total is None:
                        break
        except BaseException:
            if tmp:
                tmp.unlink(missing_ok=True)
            raise
        finally:
            if f:
                f.close()
    if got < 10_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("download too small to be audio")
    tmp.replace(path)
    return path


FIRST_PIECE = 1 << 20  # small, so playback starts quickly
PIECES_AHEAD = 4  # pieces fetched at once when passing a clip on to a phone
PIECE_TRIES = 3


@dataclass
class Relay:
    """An upstream answer passed on to a listener: status, headers and the bytes."""
    status: int
    headers: dict[str, str]
    body: object  # async iterator of bytes


def parse_range(value: str | None) -> tuple[int, int | None] | None:
    """`bytes=a-b` or `bytes=a-` as (a, b or None); anything else (suffix or several
    ranges, which players don't send) as None, served as the whole file."""
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d+)-(\d*)\s*", value or "")
    if not m or (m[2] and int(m[2]) < int(m[1])):
        return None
    return int(m[1]), int(m[2]) if m[2] else None


async def relay_in_pieces(url: str, wanted: tuple[int, int | None] | None) -> Relay | None:
    """Fetches `url` (the bytes in `wanted`, or all of it) for passing on to a listener.
    The video site sends a long Range at about playback speed, so a 40-minute clip downloaded
    in one request takes many minutes; small ranges come fast. So the bytes are fetched
    in CHUNK pieces, PIECES_AHEAD at a time, and passed on in order, as one answer.
    None when the URL doesn't answer with 200 or 206."""
    start, end = wanted or (0, None)
    c = client(timeout=60)
    try:
        first_end = start + FIRST_PIECE - 1 if end is None else min(end, start + FIRST_PIECE - 1)
        r = await c.send(c.build_request("GET", url, headers={"Range": f"bytes={start}-{first_end}",
                                                               "Accept-Encoding": "identity"}), stream=True)
    except BaseException:
        await c.aclose()
        raise
    if r.status_code not in (200, 206):
        log.info("listen again: %s answered %s", url, r.status_code)
        await r.aclose()
        await c.aclose()
        return None
    total_text = r.headers.get("content-range", "").rpartition("/")[2]
    headers = {"content-type": r.headers.get("content-type", "application/octet-stream")}
    if r.status_code == 200 or not total_text.isdigit():
        # No ranges (or no length) here: the whole answer passed on as it comes.
        headers.update({k: v for k, v in r.headers.items() if k.lower() in ("content-length", "content-range")})

        async def whole():
            try:
                async for chunk in r.aiter_raw():
                    yield chunk
            finally:
                await r.aclose()
                await c.aclose()

        return Relay(r.status_code, headers, whole())

    total = int(total_text)
    end = total - 1 if end is None else min(end, total - 1)
    url = str(r.url)  # the redirect target, for the other pieces
    try:
        first = await r.aread()
    except BaseException:
        await c.aclose()
        raise
    finally:
        await r.aclose()

    async def piece(a: int, b: int) -> bytes:
        for attempt in range(PIECE_TRIES):
            try:
                got = await c.get(url, headers={"Range": f"bytes={a}-{b}", "Accept-Encoding": "identity"})
                if got.status_code == 206 and len(got.content) == b - a + 1:
                    return got.content
                problem = f"answered {got.status_code} with {len(got.content)} bytes"
            except httpx.HTTPError as e:
                problem = str(e) or type(e).__name__
            log.info("listen again: bytes %d-%d of %s %s (try %d)", a, b, url, problem, attempt + 1)
        raise RuntimeError(f"bytes {a}-{b} could not be fetched")

    async def body():
        pending: list[asyncio.Task] = []
        try:
            yield first
            nxt = start + len(first)
            while pending or nxt <= end:
                while nxt <= end and len(pending) < PIECES_AHEAD:
                    stop = min(nxt + CHUNK - 1, end)
                    pending.append(asyncio.create_task(piece(nxt, stop)))
                    nxt = stop + 1
                yield await pending.pop(0)
        finally:
            for t in pending:
                t.cancel()
            await c.aclose()

    headers.update({"content-length": str(end - start + 1), "accept-ranges": "bytes"})
    if wanted is None:
        return Relay(200, headers, body())
    headers["content-range"] = f"bytes {start}-{end}/{total}"
    return Relay(206, headers, body())


async def _ytdlp(video_id: str, stem: Path) -> Path:
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not installed")
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "-q", "--no-playlist", "-f", "bestaudio[ext=m4a]/bestaudio",
        "-o", f"{stem}.%(ext)s", "--", video_id,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), DOWNLOAD_DEADLINE)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"yt-dlp took longer than {DOWNLOAD_DEADLINE // 60} minutes") from None
    if proc.returncode != 0:
        raise RuntimeError(err.decode(errors="replace").strip()[-300:])
    found = [p for p in stem.parent.glob(stem.name + ".*") if not p.name.endswith(".part")]
    if not found:
        raise RuntimeError("yt-dlp produced no file")
    return found[0]


AUDIO_ITAGS = {"small": "250", "aac": "140"}


async def download_url(url: str, name: str, progress=None, in_ram: bool = False) -> Path:
    return await _download(url, cache_dir(in_ram) / name, progress)


async def download(
    item: Item, inv: Invidious, ytdlp_fallback: bool, video_audio: str = "aac", progress=None, in_ram: bool = False
) -> Path:
    stem = cache_dir(in_ram) / hashlib.sha1(item.key.encode()).hexdigest()[:16]
    if item.media.startswith(("http://", "https://")):
        return await _download(item.media, stem, progress)
    errors = []
    for url in await inv.audio_urls(item.media, AUDIO_ITAGS.get(video_audio, "140")):
        try:
            return await _download(url, stem, progress)
        except TimeoutError:
            errors.append(f"download took longer than {DOWNLOAD_DEADLINE // 60} minutes")
        except Exception as e:
            errors.append(str(e) or type(e).__name__)
    if ytdlp_fallback:
        try:
            return await _ytdlp(item.media, stem)
        except Exception as e:
            errors.append(f"yt-dlp: {e}")
    raise RuntimeError("; ".join(errors[-2:]) or "no audio found")


REPLAY_KEEP = 24 * 3600  # seconds a kept voice message stays


def keep_for_replay(path: Path, in_ram: bool) -> Path:
    """A copy of a played clip in the replay folder (next to the cache, which
    purge_cache never touches) for "listen again". One copy per file."""
    folder = cache_dir(in_ram) / "replay"
    folder.mkdir(exist_ok=True)
    target = folder / path.name
    if not target.exists():
        shutil.copyfile(path, target)
    return target


def prune_replays(max_age: float) -> None:
    """Delete kept clips older than max_age seconds."""
    limit = time.time() - max_age
    for d in (CACHE_DIR, RAM_CACHE_DIR):
        folder = d / "replay"
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < limit:
                    p.unlink()
            except OSError:
                pass


def purge_cache(keep: set[str]) -> None:
    for d in (CACHE_DIR, RAM_CACHE_DIR):
        if not d.is_dir() or not os.access(d, os.W_OK):
            continue
        for p in d.iterdir():
            if p.is_file() and str(p) not in keep:
                p.unlink(missing_ok=True)
