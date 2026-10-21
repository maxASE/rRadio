"""Web UI and JSON API for Pirate Radio."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import media
from .art import find_station_logo, image_file, save_image
from .config import SECRET_MASK, Config, ConfigStore, dump
from .engine import Engine
from .liq import Liquidsoap
from .navidrome import Navidrome
from .sources import Invidious, Item, feed_title, video_id_from_url
from .store import Store
from .titles import peek

logging.basicConfig(level=os.environ.get("LOG_LEVEL_CONTROLLER", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

STATIC = Path(__file__).parent / "static"
UI_USER = os.environ.get("UI_USERNAME", "admin")
UI_PASSWORD = os.environ.get("UI_PASSWORD", "")
PUBLIC_STREAM_BASE = os.environ.get("PUBLIC_STREAM_BASE", "").rstrip("/")
ICECAST_PUBLIC_PORT = os.environ.get("ICECAST_PUBLIC_PORT", "8000")
WIDGET_PUBLIC = os.environ.get("WIDGET_PUBLIC", "").lower() in ("1", "true", "yes")

store = ConfigStore()
engine = Engine(store, Store(), Liquidsoap())


@asynccontextmanager
async def lifespan(_: FastAPI):
    engine.start()
    yield
    await engine.stop()


app = FastAPI(title="Pirate Radio", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional password for the UI. Playlist links and the logo stay open for players and dashboards."""
    open_paths = ("/listen.", "/healthz", "/static/logo.svg", "/static/apple-touch-icon.png") + (("/api/widget",) if WIDGET_PUBLIC else ())
    if UI_PASSWORD and not request.url.path.startswith(open_paths):
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, UI_USER) and secrets.compare_digest(pw, UI_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Pirate Radio"'})
    return await call_next(request)


def channel_or_404(cid: str | None):
    ch = store.config.channel(cid)
    if ch is None:
        raise HTTPException(404, "unknown channel")
    return ch


@app.middleware("http")
async def no_stale_ui(request: Request, call_next):
    """Make browsers revalidate the page and its scripts, so an update never
    pairs a new page with an old cached app.js."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-cache"
    return response


def stream_urls(request: Request, cid: str | None = None) -> dict[str, str]:
    if PUBLIC_STREAM_BASE:
        base = PUBLIC_STREAM_BASE
    else:
        base = f"{request.url.scheme}://{request.url.hostname}:{ICECAST_PUBLIC_PORT}"
    ch = store.config.channel(cid) or store.config.channel(None)
    mount = ch.mount if ch else "radio"
    return {fmt: f"{base}/{mount}.{fmt}" for fmt in store.config.settings.streams}


# --- pages ---------------------------------------------------------------------


def _versioned_index() -> str:
    """index.html with ?v=<content hash> on app.js and style.css, so a proxy or
    browser that caches them anyway (Nginx Proxy Manager's "Cache Assets")
    still fetches the new ones after an update."""
    html = (STATIC / "index.html").read_text()
    for name in ("app.js", "style.css"):
        v = hashlib.sha1((STATIC / name).read_bytes()).hexdigest()[:10]
        html = html.replace(f'"/static/{name}"', f'"/static/{name}?v={v}"')
    return html


INDEX_HTML = _versioned_index()


@app.get("/", include_in_schema=False)
async def index():
    return Response(INDEX_HTML, media_type="text/html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True, "engine_up": engine.liq_up}


def _playlist(request: Request, fmt: str, channel: str | None) -> tuple[str, str, str]:
    ch = channel_or_404(channel)
    urls = stream_urls(request, ch.id)
    return urls.get(fmt) or next(iter(urls.values())), ch.name, ch.mount


@app.get("/listen.m3u", include_in_schema=False)
async def m3u(request: Request, fmt: str = "mp3", channel: str | None = None):
    url, name, mount = _playlist(request, fmt, channel)
    body = f"#EXTM3U\n#EXTINF:-1,{name}\n{url}\n"
    return PlainTextResponse(body, media_type="audio/x-mpegurl",
                             headers={"Content-Disposition": f'attachment; filename="{mount}.m3u"'})


@app.get("/listen.pls", include_in_schema=False)
async def pls(request: Request, fmt: str = "mp3", channel: str | None = None):
    url, name, mount = _playlist(request, fmt, channel)
    body = f"[playlist]\nNumberOfEntries=1\nFile1={url}\nTitle1={name}\nLength1=-1\nVersion=2\n"
    return PlainTextResponse(body, media_type="audio/x-scpls",
                             headers={"Content-Disposition": f'attachment; filename="{mount}.pls"'})


# --- API -------------------------------------------------------------------------


@app.get("/api/status")
async def status(request: Request, channel: str | None = None):
    """What is on air on one channel (the first one if none is given)."""
    data = engine.status(channel)
    data["streams"] = stream_urls(request, data.get("channel", {}).get("id"))
    for c in data.get("channels", []):
        urls = stream_urls(request, c["id"])
        c["stream"] = urls.get("mp3") or next(iter(urls.values()), "")
    data["station_name"] = store.config.settings.station_name
    return data


@app.get("/api/widget")
async def widget(channel: str | None = None):
    """Read-only summary for dashboards (e.g. Homepage's customapi widget)."""
    return engine.widget(channel)


@app.get("/api/config")
async def get_config():
    return store.config.masked()


@app.put("/api/config")
async def put_config(body: dict):
    st = body.get("settings")
    if isinstance(st, dict) and st.get("navidrome_password") == SECRET_MASK:
        st["navidrome_password"] = store.config.settings.navidrome_password
    try:
        cfg = Config.model_validate(body)
    except ValidationError as e:
        raise HTTPException(422, detail=[{"loc": ".".join(map(str, x["loc"])), "msg": x["msg"]} for x in e.errors()])
    old_sources = {s.id for s in store.config.video + store.config.podcasts}
    cfg = store.save(cfg)
    engine.write_channels()
    for s in cfg.video + cfg.podcasts:
        if s.id not in old_sources:
            engine.check_now(s.id)  # do the initial sync right away
    return cfg.masked()


@app.get("/api/config.yaml", include_in_schema=False)
async def config_yaml():
    return PlainTextResponse(yaml.safe_dump(dump(store.config, mask=True), sort_keys=False, allow_unicode=True),
                             media_type="text/yaml",
                             headers={"Content-Disposition": 'attachment; filename="config.yaml"'})


class StationChoice(BaseModel):
    id: str
    channel: str | None = None


@app.post("/api/station")
async def choose_station(body: StationChoice):
    cfg = store.config.model_copy(deep=True)
    ch = cfg.channel(body.channel)
    if ch is None or body.id not in {s.id for s in ch.stations}:
        raise HTTPException(404, "unknown station")
    ch.current_station = body.id
    store.save(cfg)
    return {"ok": True}


@app.post("/api/program/end")
async def end_program(channel: str | None = None):
    """End the program on air early; it stays off until its window closes."""
    name = engine.end_program(channel_or_404(channel).id)
    if name is None:
        raise HTTPException(409, "No program is on air")
    return {"ok": True, "name": name}


@app.post("/api/skip")
async def skip(channel: str | None = None):
    """Skip the clip on air, or the song when Navidrome is playing."""
    await engine.skip(channel_or_404(channel).id)
    return {"ok": True}


@app.delete("/api/queue/{key:path}")
async def remove(key: str):
    if not engine.remove(key):
        raise HTTPException(404, "not in queue or already on air")
    return {"ok": True}


@app.post("/api/check")
async def check(source_id: str | None = None):
    engine.check_now(source_id)
    return {"ok": True}


@app.post("/api/sources/{kind}/{source_id}/play-latest")
async def play_latest(kind: str, source_id: str, channel: str | None = None):
    try:
        it = await engine.play_latest(kind, source_id, channel_or_404(channel).id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")
    return it.public()


class PlayUrl(BaseModel):
    url: str
    channel: str | None = None


@app.post("/api/play-url")
async def play_url(body: PlayUrl):
    ch = channel_or_404(body.channel)
    url = body.url.strip()
    vid = video_id_from_url(url)
    try:
        if vid:
            it = await engine.invidious().video_item(vid)
        elif url.startswith(("http://", "https://")):
            it = Item(kind="manual", source_id="manual", source_name="Manual", item_id=url,
                      title=url.rsplit("/", 1)[-1][:80] or url, media=url)
        else:
            raise HTTPException(400, "enter a video link or an audio file URL")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")
    it.channel = ch.id
    engine.enqueue(it)
    return it.public()


class Lookup(BaseModel):
    query: str
    name: str = ""


@app.post("/api/video/resolve")
async def resolve_channel(body: Lookup):
    try:
        return await engine.invidious().resolve_channel(body.query)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"Invidious: {type(e).__name__}: {e}")


@app.post("/api/station/logo")
async def station_logo(body: Lookup):
    """Find a stream's logo on radio-browser.info or the station's website."""
    try:
        return await find_station_logo(body.query, body.name)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.post("/api/images")
async def upload_image(file: UploadFile = File(...)):
    """Upload a logo or picture; returns the URL to put in an `image` field."""
    data = await file.read(3 * 1024 * 1024 + 1)
    try:
        return {"url": await run_in_threadpool(save_image, data, (file.content_type or "").lower())}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/images/{name}", include_in_schema=False)
async def get_image(name: str):
    p = image_file(name)
    if p is None:
        raise HTTPException(404, "no such picture")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff",
                                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})


@app.get("/api/art/{key}", include_in_schema=False)
async def art(key: str):
    """The picture for something on air, as handed out in /api/status."""
    image = await engine.art.get(key)
    if image is None:
        raise HTTPException(404, engine.art.errors.get(key, "no picture"))
    data, ctype = image
    # Pictures come from elsewhere: never let one run script on this origin.
    headers = {"Cache-Control": "private, max-age=86400", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
               "X-Content-Type-Options": "nosniff"}
    return Response(data, media_type=ctype, headers=headers)


@app.post("/api/podcast/resolve")
async def resolve_podcast(body: Lookup):
    try:
        return {"name": await feed_title(body.query.strip())}
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


class InvidiousTest(BaseModel):
    url: str | None = None


@app.post("/api/invidious/test")
async def test_invidious(body: InvidiousTest):
    inv = Invidious(body.url or store.config.settings.invidious_url)
    try:
        stats = await inv.stats()
        return {"ok": True, "version": stats.get("software", {}).get("version", "?")}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=200)


class NavidromeTest(BaseModel):
    url: str | None = None
    user: str | None = None
    password: str | None = None


def _navidrome(body: NavidromeTest | None = None) -> Navidrome:
    st = store.config.settings
    body = body or NavidromeTest()
    pw = body.password if body.password and body.password != SECRET_MASK else st.navidrome_secret
    url, user = body.url or st.navidrome_url, body.user or st.navidrome_user
    if not url or not user:
        raise HTTPException(400, "enter the Navidrome URL and user name")
    return Navidrome(url, user, pw)


@app.post("/api/navidrome/test")
async def test_navidrome(body: NavidromeTest):
    try:
        nd = _navidrome(body)
        version = await nd.ping()
        return {"ok": True, "version": version, "playlists": len(await nd.playlists())}
    except HTTPException as e:
        return {"ok": False, "error": e.detail}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/navidrome/playlists")
async def navidrome_playlists():
    try:
        return await _navidrome().playlists()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


# --- own media -------------------------------------------------------------------


@app.get("/api/media")
async def list_media():
    return await run_in_threadpool(media.library)


@app.post("/api/media")
async def upload_media(file: UploadFile = File(...), folder: str = Form("")):
    try:
        rel = await run_in_threadpool(media.save_upload, file.file, file.filename or "upload.mp3", folder)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        raise HTTPException(500, f"cannot save: {e}")
    return {"path": rel}


@app.delete("/api/media/{path:path}")
async def delete_media(path: str):
    try:
        media.delete(path)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, "no such file")
    return {"ok": True}


class PlayMedia(BaseModel):
    path: str
    channel: str | None = None


@app.post("/api/media/play")
async def play_media(body: PlayMedia):
    ch = channel_or_404(body.channel)
    try:
        p = media.resolve(body.path)
    except ValueError:
        raise HTTPException(404, "no such file")
    if not p.is_file():
        raise HTTPException(404, "no such file")
    return engine.play_media(p, ch.id).public()


@app.get("/api/spots/{spot_id}/info")
async def spot_info(spot_id: str):
    """A spot's picture and, for a feed, its newest episode."""
    try:
        return await engine.spot_info(spot_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.post("/api/spots/{spot_id}/fire")
async def fire_spot(spot_id: str):
    """Play a spot now, ignoring its schedule."""
    try:
        await engine.fire_spot(spot_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")
    return {"ok": True}


class TitleCheck(BaseModel):
    url: str
    title_start: str = ""
    title_keep: str = ""
    title_end: str = ""


@app.post("/api/programs/title-check")
async def title_check(body: TitleCheck):
    """The title a stream sends right now, and which program patterns it matches."""
    try:
        title = await peek(body.url)
    except TimeoutError:
        raise HTTPException(502, "no title within 20 seconds")
    except Exception as e:
        raise HTTPException(502, str(e) or type(e).__name__)
    out = {"title": title}
    for k in ("title_start", "title_keep", "title_end"):
        pat = getattr(body, k).strip()
        try:
            out[k] = bool(pat) and re.search(pat, title, re.I) is not None
        except re.error as e:
            raise HTTPException(422, f"bad pattern {pat!r}: {e}")
    return out
