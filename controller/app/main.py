"""Web UI and JSON API for Pirate Radio."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
import segno
import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import API_VERSION, __version__
from . import devices as devmod
from . import media, transfer
from .art import find_station_logo, image_file, save_image
from .config import SECRET_MASK, Config, ConfigStore, Podcast, Station, dump
from .engine import Engine, short_error
from .liq import Liquidsoap
from .navidrome import Navidrome
from .sources import USER_AGENT, Invidious, Item, cache_dir, feed_title, podcast_latest, video_id_from_url
from .sessions import COOKIE, DAYS
from .store import Store
from .titles import peek

logging.basicConfig(level=os.environ.get("LOG_LEVEL_CONTROLLER", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("rradio.main")

STATIC = Path(__file__).parent / "static"
UI_USER = os.environ.get("UI_USERNAME", "admin")
UI_PASSWORD = os.environ.get("UI_PASSWORD", "")
PUBLIC_STREAM_BASE = os.environ.get("PUBLIC_STREAM_BASE", "").rstrip("/")
ICECAST_PUBLIC_PORT = os.environ.get("ICECAST_PUBLIC_PORT", "8000")
WIDGET_PUBLIC = os.environ.get("WIDGET_PUBLIC", "").lower() in ("1", "true", "yes")

store = ConfigStore()
state = Store()
devices = state.devices
sessions = state.sessions
engine = Engine(store, state, Liquidsoap())


@asynccontextmanager
async def lifespan(_: FastAPI):
    engine.start()
    yield
    await engine.stop()


app = FastAPI(title="rRadio", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)


# What a "member" device may call; everything else needs an admin device or the UI password.
MEMBER_ROUTES = {
    "GET": re.compile(r"/api/(status|events|widget|me|art/.+|images/.+|history/\d+/audio|navidrome/playlists)"),
    "POST": re.compile(r"/api/(skip|program/end|play-url|voice|station|media/play|sources/[^/]+/[^/]+/play-latest|spots/[^/]+/fire|history/\d+/play-next)"),
    "DELETE": re.compile(r"/api/(queue/.+|history/\d+/replay)"),
}


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional password for the UI, or a device key from an app (Authorization: Bearer).
    Playlist links and the logo stay open for players and dashboards."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        dev = await run_in_threadpool(devices.by_token, auth[7:].strip())
        if dev is None:
            return JSONResponse({"detail": "unknown or removed device key; pair the app again"}, status_code=401)
        rule = MEMBER_ROUTES.get(request.method)
        if dev.role != "admin" and not (rule and rule.fullmatch(request.url.path)):
            return JSONResponse({"detail": "this device is not allowed to do that"}, status_code=403)
        token = devmod.current.set(dev)
        try:
            return await call_next(request)
        finally:
            devmod.current.reset(token)
    path = request.url.path
    open_paths = ("/listen.", "/healthz", "/static/", "/login", "/api/login", "/api/art/now/", "/api/pair") + (("/api/widget",) if WIDGET_PUBLIC else ())
    if UI_PASSWORD and not path.startswith(open_paths):
        # The browser logs in on /login and gets a cookie; dashboards and scripts may send Basic auth.
        ok = await run_in_threadpool(sessions.valid, request.cookies.get(COOKIE, ""))
        if not ok and auth.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                ok = credentials_ok(user, pw)
            except Exception:
                ok = False
        if not ok:
            if request.method == "GET" and not path.startswith("/api/"):
                return RedirectResponse("/login" + ("" if path == "/" else "?next=" + quote(path)), status_code=303)
            return JSONResponse({"detail": "log in first"}, status_code=401)
    return await call_next(request)


def credentials_ok(user: str, pw: str) -> bool:
    return secrets.compare_digest(user.encode(), UI_USER.encode()) and secrets.compare_digest(pw.encode(), UI_PASSWORD.encode())


def restricted() -> devmod.Device | None:
    """The app device behind this request if it is limited to some channels or actions."""
    dev = devmod.current.get()
    return dev if dev and dev.role != "admin" else None


def channel_or_404(cid: str | None):
    dev = restricted()
    if cid is None and dev and dev.channels:  # default to the device's first channel
        cid = next((c.id for c in store.config.channels if dev.may_use(c.id)), None)
    ch = store.config.channel(cid)
    if ch is None:
        raise HTTPException(404, "unknown channel")
    if dev and not dev.may_use(ch.id):
        raise HTTPException(403, "this device may not use that channel")
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


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    if not UI_PASSWORD or await run_in_threadpool(sessions.valid, request.cookies.get(COOKIE, "")):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC / "login.html", headers={"Cache-Control": "no-cache"})


class Login(BaseModel):
    username: str = ""
    password: str = ""


@app.post("/api/login")
async def login(body: Login, request: Request):
    """Check the UI user name and password and set a session cookie (30 days)."""
    if not UI_PASSWORD:
        return {"ok": True}
    addr = request.client.host if request.client else ""
    if sessions.blocked(addr):
        raise HTTPException(429, "Too many wrong passwords. Wait five minutes and try again.")
    if not credentials_ok(body.username.strip() or UI_USER, body.password):
        sessions.failed(addr)
        log.warning("wrong UI password from %s", addr)
        raise HTTPException(401, "Wrong user name or password.")
    token = await run_in_threadpool(sessions.create)
    resp = JSONResponse({"ok": True})
    https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"
    resp.set_cookie(COOKIE, token, max_age=DAYS * 86400, httponly=True, samesite="lax", secure=https, path="/")
    return resp


@app.post("/api/logout")
async def logout(request: Request):
    await run_in_threadpool(sessions.end, request.cookies.get(COOKIE, ""))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True, "engine_up": engine.liq_up, "version": __version__, "api": API_VERSION}


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
    dev = restricted()
    if dev:
        channel = channel_or_404(channel).id
    return build_status(request, channel, dev)


EVENTS_PING = 20  # seconds between keep-alive comments on /api/events
EVENTS_MAX = 1800  # seconds before the server ends a feed; the client reconnects


@app.get("/api/events", include_in_schema=False)
async def events(request: Request, channel: str | None = None):
    """The same data as /api/status as a live feed (Server-Sent Events): sent once
    at the start and then only when something changes, instead of being polled.
    A comment line every 20 seconds keeps proxies from closing the connection;
    after 30 minutes the server ends it and the browser reconnects (and logs in again)."""
    dev = restricted()
    if dev:
        channel = channel_or_404(channel).id

    async def feed():
        started, seen = time.monotonic(), -1
        yield "retry: 3000\n\n"
        while time.monotonic() - started < EVENTS_MAX and not await request.is_disconnected():
            if engine.version != seen:
                seen = engine.version
                yield f"data: {json.dumps(build_status(request, channel, dev), default=str)}\n\n"
            else:
                yield ": ping\n\n"
            await engine.wait_change(seen, EVENTS_PING)

    return StreamingResponse(feed(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def build_status(request: Request, channel: str | None, dev: devmod.Device | None) -> dict:
    data = engine.status(channel)
    if dev:
        data["channels"] = [c for c in data["channels"] if dev.may_use(c["id"])]
        data["incoming"] = [i for i in data["incoming"] if dev.may_use(i["channel"])]
    data["streams"] = stream_urls(request, data.get("channel", {}).get("id"))
    for c in data.get("channels", []):
        urls = stream_urls(request, c["id"])
        c["stream"] = urls.get("mp3") or next(iter(urls.values()), "")
    data["station_name"] = store.config.settings.station_name
    return data


@app.get("/api/widget")
async def widget(channel: str | None = None):
    """Read-only summary for dashboards (e.g. Homepage's customapi widget)."""
    return engine.widget(channel_or_404(channel).id if restricted() else channel)


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


def _zip(data: bytes, name: str) -> Response:
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/channels/{cid}/export", include_in_schema=False)
async def export_channel(cid: str):
    ch = channel_or_404(cid)
    data = await run_in_threadpool(transfer.export_channel, store.config, ch)
    return _zip(data, f"rradio-{ch.mount}.zip")


@app.get("/api/backup", include_in_schema=False)
async def backup():
    data = await run_in_threadpool(transfer.export_all, store.config)
    return _zip(data, f"rradio-backup-{time.strftime('%Y-%m-%d')}.zip")


@app.post("/api/import")
async def import_file(file: UploadFile = File(...), name: str = Form(""), replace: bool = Form(False)):
    """A channel export adds a channel; a full backup replaces the whole config."""
    data = await file.read()
    old_sources = {s.id for s in store.config.video + store.config.podcasts}
    try:
        cfg, summary = await run_in_threadpool(transfer.import_zip, store.config, data, name, store.path, replace)
    except transfer.NeedsConfirm as e:
        raise HTTPException(409, str(e))
    except ValidationError as e:
        raise HTTPException(422, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))
    except (ValueError, KeyError, yaml.YAMLError) as e:
        raise HTTPException(400, str(e))
    cfg = store.save(cfg)
    engine.write_channels()
    for s in cfg.video + cfg.podcasts:
        if s.id not in old_sources:
            engine.check_now(s.id)
    log.info(summary)
    return {"summary": summary, "config": cfg.masked()}


class StationChoice(BaseModel):
    """A station of the channel by `id`, or a Navidrome `playlist` by name
    ("" = random songs from the whole library)."""

    id: str = ""
    playlist: str | None = None
    channel: str | None = None


@app.post("/api/station")
async def choose_station(body: StationChoice):
    channel_or_404(body.channel)
    cfg = store.config.model_copy(deep=True)
    ch = cfg.channel(body.channel)
    if ch is None:
        raise HTTPException(404, "unknown channel")
    if body.playlist is not None:
        # A playlist the channel already has as a station is reused; any
        # other becomes a new Navidrome station on this channel.
        name = body.playlist.strip()
        same = next((s for s in ch.stations if s.kind == "navidrome" and s.playlist.lower() == name.lower()), None)
        if same is None:
            if name:
                try:
                    lists = await _navidrome().playlists()
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(502, str(e))
                match = next((p for p in lists if p["id"] == name or p["name"].lower() == name.lower()), None)
                if match is None:
                    raise HTTPException(404, f"no Navidrome playlist called {name!r}")
                name = match["name"]
            same = Station(name=name or "Navidrome library", kind="navidrome", playlist=name)
            ch.stations.append(same)
        body.id = same.id
    if body.id not in {s.id for s in ch.stations}:
        raise HTTPException(404, "unknown station")
    ch.current_station = body.id
    store.save(cfg)
    return {"ok": True, "id": body.id}


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
    dev = restricted()
    it = next((i for i in engine.items if i.uid == key), None)
    if dev and it and not dev.may_use(it.channel):
        raise HTTPException(403, "this device may not use that channel")
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


@app.get("/api/art/now/{channel}")
async def art_now(channel: str):
    """The picture of whatever plays right now on a channel (by id or mount; an
    extension like .jpg is ignored), falling back to the rRadio logo. One fixed
    link for players that show a station picture; open without the UI password."""
    cid = re.sub(r"\.(jpe?g|png|gif|webp)$", "", channel)
    ch = store.config.channel(cid) or next((c for c in store.config.channels if c.mount == cid), None)
    if ch is None:
        raise HTTPException(404, "unknown channel")
    path = engine.now_playing(ch).get("art") or ""
    image = None
    if path.startswith("/api/images/"):
        f = image_file(path.rsplit("/", 1)[-1])
        if f:
            image = (f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream")
    elif path.startswith("/api/art/"):
        image = await engine.art.get(path.rsplit("/", 1)[-1])
    if image is None:
        image = ((STATIC / "apple-touch-icon.png").read_bytes(), "image/png")
    data, ctype = image
    headers = {"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
               "X-Content-Type-Options": "nosniff"}
    return Response(data, media_type=ctype, headers=headers)


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


@app.post("/api/feeds/check")
async def check_feed(body: Lookup):
    """The newest episode of a feed, to try a spot's feed URL before saving."""
    try:
        eps = await podcast_latest(Podcast(name="check", url=body.query.strip(), max_minutes=0))
    except Exception as e:
        raise HTTPException(502, short_error(e))
    if not eps:
        raise HTTPException(502, "the feed has no audio episodes")
    newest = max(eps, key=lambda i: i.published or 0)
    return {"title": newest.title, "published": newest.published, "episodes": len(eps)}


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


VOICE_TYPES = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/aac": "aac", "audio/x-m4a": "m4a",
               "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "video/webm": "webm"}
VOICE_MAX = 25 * 1024 * 1024


@app.post("/api/voice")
async def voice_message(file: UploadFile = File(...), channel: str = Form("all"), title: str = Form("")):
    """A recorded voice message, cut in right away on one channel or on all
    ("all"). The file goes to the RAM disk (when there is one, whatever the
    download setting) and is deleted once it has played everywhere."""
    dev = devmod.current.get()
    if channel == "all":
        targets = [c for c in store.config.channels if dev is None or dev.may_use(c.id)]
        if not targets:
            raise HTTPException(404, "no channel to play on")
    else:
        targets = [channel_or_404(channel)]
    ctype = (file.content_type or "").split(";")[0].strip().lower()
    ext = VOICE_TYPES.get(ctype) or Path(file.filename or "").suffix.lstrip(".").lower()
    if ext not in set(VOICE_TYPES.values()):
        raise HTTPException(415, f"not an audio recording ({ctype or 'unknown type'})")
    data = await file.read(VOICE_MAX + 1)
    if len(data) > VOICE_MAX:
        raise HTTPException(413, "recording is too long (25 MB at most)")
    if len(data) < 1000:
        raise HTTPException(400, "the recording is empty")
    path = cache_dir(True) / f"voice-{secrets.token_hex(6)}.{ext}"
    await run_in_threadpool(path.write_bytes, data)
    who = dev.name if dev else "web"
    it = Item(kind="voice", source_id="voice", source_name="Voice message", item_id=path.name,
              title=title.strip()[:80] or f"Voice message from {who}", media=str(path), path=str(path),
              priority=1, during_programs=True)
    for ch in targets:
        engine.enqueue(it.copy_for(ch.id))
    log.info("voice message %s (%d kB) for %s", path.name, len(data) // 1024, ", ".join(c.id for c in targets))
    return {"ok": True, "channels": [c.id for c in targets], "title": it.title}


REPLAY_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges")


@app.get("/api/history/{entry_id}/audio")
async def history_audio(entry_id: int, request: Request):
    """"Listen again": the audio of a clip from the history, for one listener only
    (the radio is not interrupted). Kept voice messages are served from the
    replay folder; anything else is fetched again from where it came from
    (videos through Invidious), passing Range on so the player can seek."""
    found = state.replay_of(entry_id)
    if not found:
        raise HTTPException(404, "this can no longer be played again")
    channel, info = found
    dev = restricted()
    if dev and channel and not dev.may_use(channel):
        raise HTTPException(403, "this device may not use that channel")
    if info.get("path"):
        return FileResponse(info["path"])
    media_ref = info["media"]
    if media_ref.startswith(("http://", "https://")):
        urls = [media_ref]
    else:  # a video id; AAC (itag 140), because phones can't play WebM/Opus
        urls = await engine.invidious().audio_urls(media_ref, "140")
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    if request.headers.get("range"):
        headers["Range"] = request.headers["range"]
    client = httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=60))
    for url in urls:
        try:
            upstream = await client.send(client.build_request("GET", url, headers=headers), stream=True)
        except httpx.HTTPError as e:
            log.info("listen again: %s failed: %s", url, e)
            continue
        if upstream.status_code in (200, 206):
            async def body(upstream=upstream):
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await upstream.aclose()
                    await client.aclose()

            out = {k: v for k, v in upstream.headers.items() if k.lower() in REPLAY_HEADERS}
            return StreamingResponse(body(), status_code=upstream.status_code, headers=out)
        log.info("listen again: %s answered %s", url, upstream.status_code)
        await upstream.aclose()
    await client.aclose()
    raise HTTPException(502, "the source of this clip didn't answer")


@app.post("/api/history/{entry_id}/play-next")
async def history_play_next(entry_id: int, channel: str | None = None):
    """"Play next": a clip from the history goes back into the queue of its channel (or
    of `channel`), ahead of ordinary clips, and plays for everyone listening."""
    entry = state.entry(entry_id)
    if not entry or not entry["replay"]:
        raise HTTPException(404, "this can no longer be played again")
    if entry["kind"] == "program":
        raise HTTPException(409, "a kept program can only be listened to in the app, not put on the radio")
    ch = channel_or_404(channel or entry["channel"] or None)
    info = entry["replay"]
    it = Item(kind=entry["kind"], source_id="again", source_name=entry["source"], item_id=f"{entry_id}@{time.time()}",
              title=entry["title"], media=info.get("media") or info["path"], channel=ch.id, priority=1, during_programs=True)
    if info.get("path"):
        it.path, it.keep_file = info["path"], True
    engine.enqueue(it)
    return it.public()


@app.delete("/api/history/{entry_id}/replay")
async def history_forget(entry_id: int):
    """Removes what is kept of a history entry (a recorded program or a voice message)
    from memory before its 24 hours are up. Clips fetched again have nothing to remove."""
    found = state.replay_of(entry_id)
    if not found:
        raise HTTPException(404, "nothing is kept of this")
    channel, info = found
    dev = restricted()
    if dev and channel and not dev.may_use(channel):
        raise HTTPException(403, "this device may not use that channel")
    path = Path(info.get("path") or "")
    if path.parent.name != "replay":  # own media files are not ours to delete
        raise HTTPException(409, "nothing is stored for this; it is fetched again from its source")
    engine.drop_recording(str(path))
    path.unlink(missing_ok=True)
    state.set_replay(entry_id, None)
    return {"ok": True}


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
    dev = restricted()
    found = store.config.spot(spot_id)
    if dev and found and not dev.may_use(found[0].id):
        raise HTTPException(403, "this device may not use that channel")
    try:
        await engine.fire_spot(spot_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(502, short_error(e))
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


# --- app devices -------------------------------------------------------------------


def _check_channels(ids: list[str]) -> list[str]:
    known = {c.id for c in store.config.channels}
    bad = [i for i in ids if i not in known]
    if bad:
        raise HTTPException(422, f"unknown channel: {', '.join(bad)}")
    return ids


def _channels_of(dev: devmod.Device | None) -> list[dict]:
    return [{"id": c.id, "name": c.name, "mount": c.mount}
            for c in store.config.channels if dev is None or dev.may_use(c.id)]


@app.get("/api/devices")
async def list_devices():
    """Paired apps and pairing codes still waiting to be scanned."""
    return {
        "devices": [d.public() for d in await run_in_threadpool(devices.list)],
        "pending": [{"code": p.code, "name": p.name, "role": p.role, "channels": p.channels, "expires": p.expires}
                    for p in devices.pending()],
        "password_set": bool(UI_PASSWORD),
    }


class PairStart(BaseModel):
    name: str = ""
    role: str = "member"
    channels: list[str] = []
    server: str = ""  # the address the app should use; the UI sends its own origin


@app.post("/api/devices/pair")
async def start_pairing(body: PairStart, request: Request):
    """A one-time code (valid 10 minutes) and a QR code for the app to scan."""
    try:
        p = devices.start_pairing(body.name, body.role, _check_channels(body.channels))
    except ValueError as e:
        raise HTTPException(422, str(e))
    server = (body.server.strip() or str(request.base_url)).rstrip("/")
    uri = "rradio://pair?" + urlencode({"server": server, "code": p.code})
    qr = segno.make(uri, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")
    return {"code": p.code, "expires": p.expires, "server": server, "uri": uri, "qr_svg": qr}


@app.delete("/api/devices/pair/{code}")
async def cancel_pairing(code: str):
    devices.cancel_pairing(code)
    return {"ok": True}


class DeviceEdit(BaseModel):
    name: str | None = None
    role: str | None = None
    channels: list[str] | None = None


@app.patch("/api/devices/{did}")
async def edit_device(did: str, body: DeviceEdit):
    try:
        dev = await run_in_threadpool(devices.update, did, body.name, body.role,
                                      None if body.channels is None else _check_channels(body.channels))
    except ValueError as e:
        raise HTTPException(422, str(e))
    if dev is None:
        raise HTTPException(404, "unknown device")
    return dev.public()


@app.delete("/api/devices/{did}")
async def remove_device(did: str):
    """Revoke a device: its key stops working at once."""
    if not await run_in_threadpool(devices.delete, did):
        raise HTTPException(404, "unknown device")
    return {"ok": True}


class PairRedeem(BaseModel):
    code: str
    device_name: str = ""


@app.post("/api/pair")
async def pair(body: PairRedeem, request: Request):
    """Called by the app after scanning the QR code. Open without the UI password:
    the one-time code is the proof. The key is returned only here."""
    try:
        dev, token = await run_in_threadpool(devices.redeem, body.code, body.device_name)
    except PermissionError as e:
        raise HTTPException(429, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    log.info("paired device %s (%s, %s)", dev.name, dev.id, dev.role)
    return {"token": token, "device": dev.public(), "station_name": store.config.settings.station_name,
            "channels": _channels_of(dev), "streams": {c["id"]: stream_urls(request, c["id"]) for c in _channels_of(dev)}}


@app.get("/api/me")
async def me(request: Request):
    """Who is asking: the app's device (with its role and channels), or the web UI."""
    dev = devmod.current.get()
    return {"version": __version__, "api": API_VERSION,
            "device": dev.public() if dev else None, "role": dev.role if dev else "admin", "login": bool(UI_PASSWORD),
            "station_name": store.config.settings.station_name,
            "channels": _channels_of(dev), "streams": {c["id"]: stream_urls(request, c["id"]) for c in _channels_of(dev)}}
