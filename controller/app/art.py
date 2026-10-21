"""Pictures for what is on air: fetched and cached by the controller, so the
browser only ever talks to rRadio (never to the video site or a station's server)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

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


class ArtCache:
    """Hands out /api/art/<key> paths for URLs the engine chose, and serves them.

    Only URLs registered here can be fetched, so this is no open proxy.
    """

    def __init__(self, size: int = 64):
        self.urls: OrderedDict[str, str] = OrderedDict()  # key -> remote URL
        self.images: OrderedDict[str, tuple[bytes, str] | None] = OrderedDict()  # key -> image, None if it failed
        self.failed_at: dict[str, float] = {}
        self.errors: dict[str, str] = {}  # key -> why it failed
        self.size = size

    def path(self, url: str) -> str | None:
        """The local path to show `url` at, or None if it failed recently."""
        if not url:
            return None
        if url.startswith(UPLOADED):
            return url
        key = art_key(url)
        if key in self.failed_at and time.time() - self.failed_at[key] < 600:
            return None
        self.urls[key] = url
        self.urls.move_to_end(key)
        while len(self.urls) > self.size * 4:
            self.urls.popitem(last=False)
        return f"/api/art/{key}"

    async def get(self, key: str) -> tuple[bytes, str] | None:
        if key in self.images:
            self.images.move_to_end(key)
            return self.images[key]
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
            self.errors[key] = reason
            return None
        self.errors.pop(key, None)
        self.images[key] = image
        while len(self.images) > self.size:
            self.images.popitem(last=False)
        return image

    def error(self, url: str) -> str | None:
        """Why the picture at `url` could not be fetched, if it failed lately."""
        return self.errors.get(art_key(url))


async def _fetch_image(c, url: str, page_ok: bool = True) -> tuple[bytes, str]:
    """Download a picture. A web page instead of a picture (say a Wikipedia file
    page) is searched for its og:image or icon, once."""
    async with c.stream("GET", url) as r:
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
