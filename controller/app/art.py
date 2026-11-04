"""Pictures for what is on air: fetched and cached by the controller, so the
browser only ever talks to rRadio (never to the video site or a station's server)."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

from starlette.concurrency import run_in_threadpool

from .sources import client

log = logging.getLogger("art")

MAX_BYTES = 3 * 1024 * 1024
# Pictures you upload; a station's `image` then reads /api/images/<file>.
IMAGES_DIR = Path(os.environ.get("IMAGES_DIR", Path(os.environ.get("CONFIG_PATH", "/data/config.yaml")).parent / "images"))
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp", "image/svg+xml": ".svg"}
UPLOADED = "/api/images/"


def save_image(data: bytes, ctype: str) -> str:
    """Store an uploaded picture under its hash and return its URL path."""
    ext = IMAGE_TYPES.get(ctype)
    if not ext:
        raise ValueError("pick a PNG, JPEG, GIF, WebP or SVG picture")
    if len(data) > MAX_BYTES:
        raise ValueError("picture too big (3 MB at most)")
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(data).hexdigest()[:16] + ext
    (IMAGES_DIR / name).write_bytes(data)
    return UPLOADED + name


def image_file(name: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{16}\.(png|jpg|gif|webp|svg)", name):
        return None
    p = IMAGES_DIR / name
    return p if p.is_file() else None
# radio-browser.info runs a few mirrors; the first one that answers wins.
RADIO_BROWSER_URLS = os.environ.get(
    "RADIO_BROWSER_URLS", "https://de1.api.radio-browser.info https://de2.api.radio-browser.info https://fi1.api.radio-browser.info"
).split()


def art_key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


# Pictures fetched once are kept on disk too, so a restart (or a busy day pushing them out
# of memory) doesn't send every client's request back to the video site or a station's
# server, which made channel pictures slow to show up again and again.
ART_CACHE_DIR = Path(os.environ.get("ART_CACHE_DIR", Path(os.environ.get("CACHE_DIR", "/data/cache")) / "art"))
ART_DISK_FILES = 2000


class ArtCache:
    """Hands out /api/art/<key> paths for URLs the engine chose, and serves them.

    Only URLs registered here can be fetched, so this is no open proxy. A picture is fetched
    once however many clients ask for it at the same time, kept in memory and on disk, and
    fetched ahead as soon as its path is handed out, so the first phone to show it doesn't wait.
    """

    def __init__(self, size: int = 256, disk: Path | None = ART_CACHE_DIR):
        self.urls: OrderedDict[str, str] = OrderedDict()  # key -> remote URL
        self.images: OrderedDict[str, tuple[bytes, str] | None] = OrderedDict()  # key -> image, None if it failed
        self.failed_at: dict[str, float] = {}
        self.failures: dict[str, int] = {}  # key -> failures in a row
        self.errors: dict[str, str] = {}  # key -> why it failed
        self.loading: dict[str, asyncio.Task] = {}
        self.size = size
        self.disk = disk

    def path(self, url: str) -> str | None:
        """The local path to show `url` at, or None if it failed recently."""
        if not url:
            return None
        if url.startswith(UPLOADED):
            return url
        key = art_key(url)
        if key in self.failed_at and time.time() - self.failed_at[key] < self._pause(key):
            return None
        new = key not in self.urls
        self.urls[key] = url
        self.urls.move_to_end(key)
        while len(self.urls) > self.size * 4:
            self.urls.popitem(last=False)
        if new and key not in self.images:
            self._warm(key)
        return f"/api/art/{key}"

    def _pause(self, key: str) -> float:
        """How long a failed picture is left out: a minute after one failure (a slow answer
        once shouldn't hide a channel's picture for long), ten after more."""
        return 60 if self.failures.get(key, 0) <= 1 else 600

    def _warm(self, key: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # not inside the server (tests, scripts)
        if key not in self.loading:
            self.loading[key] = loop.create_task(self._load(key))

    def _disk_file(self, key: str) -> Path | None:
        return self.disk / key if self.disk and re.fullmatch(r"[0-9a-f]{16}", key) else None

    def _read_disk(self, key: str) -> tuple[bytes, str] | None:
        f = self._disk_file(key)
        if not f or not f.is_file():
            return None
        try:
            data = f.read_bytes()
            ctype, _, body = data.partition(b"\n")
            os.utime(f)  # recently used: kept when old ones are cleared out
            return body, ctype.decode()
        except OSError:
            return None

    def _write_disk(self, key: str, image: tuple[bytes, str]) -> None:
        f = self._disk_file(key)
        if not f:
            return
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(image[1].encode() + b"\n" + image[0])
            tmp.replace(f)
            files = list(f.parent.iterdir())
            if len(files) > ART_DISK_FILES * 1.1:  # clear out the least recently used
                files.sort(key=lambda p: p.stat().st_mtime)
                for old in files[: len(files) - ART_DISK_FILES]:
                    old.unlink(missing_ok=True)
        except OSError as e:
            log.info("picture cache on disk: %s", e)

    def _remember(self, key: str, image: tuple[bytes, str]) -> None:
        self.images[key] = image
        self.images.move_to_end(key)
        while len(self.images) > self.size:
            self.images.popitem(last=False)

    async def get(self, key: str) -> tuple[bytes, str] | None:
        if key in self.images:
            self.images.move_to_end(key)
            return self.images[key]
        task = self.loading.get(key)
        if task is None:
            if key not in self.urls:
                f = self._disk_file(key)
                if not f or not f.is_file():
                    return None
            task = self.loading[key] = asyncio.get_running_loop().create_task(self._load(key))
        # Shielded: a client giving up doesn't cancel the fetch the others are waiting on.
        return await asyncio.shield(task)

    async def _load(self, key: str) -> tuple[bytes, str] | None:
        try:
            image = await run_in_threadpool(self._read_disk, key)
            if image:
                self._remember(key, image)
                return image
            url = self.urls.get(key)
            if not url:
                return None
            try:
                async with client(15) as c:
                    image = await _fetch_image(c, url)
            except Exception as e:
                reason = str(e) or type(e).__name__
                log.warning("picture %s failed: %s", url, reason)
                self.failed_at[key] = time.time()
                self.failures[key] = self.failures.get(key, 0) + 1
                self.errors[key] = reason
                return None
            self.errors.pop(key, None)
            self.failures.pop(key, None)
            self.failed_at.pop(key, None)
            self._remember(key, image)
            await run_in_threadpool(self._write_disk, key, image)
            return image
        finally:
            self.loading.pop(key, None)

    def error(self, url: str) -> str | None:
        """Why the picture at `url` could not be fetched, if it failed lately."""
        return self.errors.get(art_key(url))


# Some image hosts refuse anything that doesn't look like a browser showing their
# own page (hotlink protection), so a refused request is tried again as one.
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"


def _as_browser(url: str) -> dict:
    p = urlsplit(url)
    return {"User-Agent": BROWSER_UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8", "Referer": f"{p.scheme}://{p.netloc}/"}


async def _fetch_image(c, url: str, page_ok: bool = True) -> tuple[bytes, str]:
    """Download a picture. A web page instead of a picture (say a Wikipedia file
    page) is searched for its og:image or icon, once."""
    try:
        return await _fetch_once(c, url, page_ok, {})
    except PermissionError:
        try:
            return await _fetch_once(c, url, page_ok, _as_browser(url))
        except PermissionError as e:
            raise ValueError(f"{e}; the site blocks other sites from using its pictures, so upload the file instead") from None


async def _fetch_once(c, url: str, page_ok: bool, headers: dict) -> tuple[bytes, str]:
    async with c.stream("GET", url, headers=headers) as r:
        if r.status_code in (401, 403):
            raise PermissionError(f"the server answered {r.status_code} {r.reason_phrase}".strip())
        if r.status_code >= 400:
            raise ValueError(f"the server answered {r.status_code} {r.reason_phrase}".strip())
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if not ctype.startswith("image/") and not (page_ok and "html" in ctype):
            raise ValueError(f"that address is not a picture ({ctype or 'no type'})")
        data = b""
        async for chunk in r.aiter_bytes():
            data += chunk
            if len(data) > MAX_BYTES:
                raise ValueError("picture too big (3 MB at most)")
        if ctype.startswith("image/"):
            return data, ctype
        page = _Icons()
        page.feed(data.decode(r.encoding or "utf-8", "replace"))
        if not page.found:
            raise ValueError("that address is a web page with no picture on it; use the picture's own address")
        # The page's own picture (og:image) beats the site's icon here.
        og = [u for score, u in page.found if score == 2000]
        best = og[0] if og else max(page.found, key=lambda x: x[0])[1]
        return await _fetch_image(c, urljoin(str(r.url), best), page_ok=False)


class _Icons(HTMLParser):
    """Collects a page's icons and its og:image, best first."""

    def __init__(self):
        super().__init__()
        self.found: list[tuple[int, str]] = []

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag == "link" and a.get("href"):
            rel = a.get("rel", "").lower()
            size = max((int(x) for x in re.findall(r"(\d+)x\d+", a.get("sizes", ""))), default=0)
            if "apple-touch-icon" in rel:
                self.found.append((3000 + size, a["href"]))
            elif "icon" in rel.split():
                self.found.append((1000 + size, a["href"]))
        elif tag == "meta" and a.get("property", a.get("name", "")).lower() in ("og:image", "og:logo") and a.get("content"):
            self.found.append((2000, a["content"]))


async def _radio_browser(c, stream_url: str, name: str) -> dict | None:
    parts = urlsplit(stream_url)
    tries = [stream_url]
    if parts.query:
        tries.append(urlunsplit(parts._replace(query="")))
    other = "https" if parts.scheme == "http" else "http"
    tries += [urlunsplit(urlsplit(u)._replace(scheme=other)) for u in list(tries)]
    error = None
    for base in RADIO_BROWSER_URLS:
        base = base.rstrip("/")
        try:
            for u in tries:
                r = await c.get(f"{base}/json/stations/byurl", params={"url": u})
                r.raise_for_status()
                for st in r.json():
                    if st.get("favicon"):
                        return {"image": st["favicon"], "found_on": "radio-browser.info"}
            if name:
                r = await c.get(f"{base}/json/stations/byname/{name}", params={"order": "votes", "reverse": "true", "limit": 10})
                r.raise_for_status()
                for st in r.json():
                    if st.get("favicon") and st.get("name", "").strip().lower() == name.strip().lower():
                        return {"image": st["favicon"], "found_on": "radio-browser.info"}
            return None
        except Exception as e:  # try the next mirror
            error = e
    raise RuntimeError(f"radio-browser.info unreachable ({type(error).__name__})")


async def search_stations(query: str, country: str = "", limit: int = 30) -> list[dict]:
    """Stations on radio-browser.info whose name, tag (genre) or country matches,
    most voted first. Broken streams are left out."""
    query, country = query.strip(), country.strip()
    params = {"order": "votes", "reverse": "true", "hidebroken": "true", "limit": limit}
    if len(country) == 2:
        params["countrycode"] = country.upper()
    elif country:
        params["country"] = country
    error = None
    async with client(10) as c:
        for base in RADIO_BROWSER_URLS:
            base = base.rstrip("/")
            try:
                found: dict[str, dict] = {}
                # A word like "jazz" is more often a tag than a station name, so ask both.
                for field in ("name", "tag"):
                    if not query and field == "tag":
                        continue
                    r = await c.get(f"{base}/json/stations/search", params={**params, field: query} if query else params)
                    r.raise_for_status()
                    for st in r.json():
                        found.setdefault(st["stationuuid"], st)
                stations = sorted(found.values(), key=lambda st: -int(st.get("votes") or 0))[:limit]
                return [_station(st) for st in stations]
            except Exception as e:  # try the next mirror
                error = e
    raise RuntimeError(f"radio-browser.info unreachable ({type(error).__name__})")


def _station(st: dict) -> dict:
    return {
        "uuid": st["stationuuid"],
        "name": st.get("name", "").strip(),
        "url": st.get("url_resolved") or st.get("url", ""),
        "image": st.get("favicon", ""),
        "homepage": st.get("homepage", ""),
        "country": st.get("countrycode", ""),
        "tags": [t for t in (st.get("tags") or "").split(",") if t][:4],
        "codec": st.get("codec", ""),
        "bitrate": int(st.get("bitrate") or 0),
        "votes": int(st.get("votes") or 0),
    }


async def count_station_click(uuid: str) -> None:
    """Tell radio-browser.info a station was picked; its popularity ranking relies on this."""
    async with client(10) as c:
        for base in RADIO_BROWSER_URLS:
            try:
                (await c.get(f"{base.rstrip('/')}/json/url/{uuid}")).raise_for_status()
                return
            except Exception:
                continue


async def _homepages(c, stream_url: str) -> list[str]:
    """The station's website: from the stream's icy-url header, else guessed from its host."""
    pages = []
    try:
        async with c.stream("GET", stream_url, headers={"Icy-MetaData": "1"}) as r:
            if r.headers.get("icy-url", "").startswith("http"):
                pages.append(r.headers["icy-url"])
    except Exception as e:
        log.info("stream %s for icy-url: %s", stream_url, e)
    host = urlsplit(stream_url).hostname or ""
    if host:
        pages.append(f"https://{host}/")
        labels = host.split(".")
        if len(labels) > 2:
            pages.append(f"https://www.{'.'.join(labels[-2:])}/")
    return list(dict.fromkeys(pages))


async def _site_icon(c, page: str) -> str | None:
    r = await c.get(page)
    r.raise_for_status()
    if "html" not in r.headers.get("content-type", ""):
        return None
    p = _Icons()
    p.feed(r.text[:500_000])
    if not p.found:
        return None
    return urljoin(str(r.url), max(p.found, key=lambda x: x[0])[1])


async def find_station_logo(stream_url: str, name: str = "") -> dict:
    """A logo for a stream: radio-browser.info first, then the station's own website."""
    stream_url = stream_url.strip()
    problems = []
    async with client(10) as c:
        try:
            found = await _radio_browser(c, stream_url, name)
            if found:
                return found
            problems.append("not listed on radio-browser.info")
        except Exception as e:
            problems.append(str(e))
        for page in await _homepages(c, stream_url):
            try:
                icon = await _site_icon(c, page)
            except Exception as e:
                log.info("icon from %s: %s", page, e)
                continue
            if icon:
                return {"image": icon, "found_on": urlsplit(page).hostname}
        problems.append("no icon on the station's website")
    raise LookupError("No logo found (" + "; ".join(problems) + "). Paste a logo URL or upload a picture instead.")
