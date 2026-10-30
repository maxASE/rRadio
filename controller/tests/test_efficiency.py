"""Doing less work: one status for every live feed, reused connections, parallel
downloads, and no Liquidsoap restart for a renamed channel."""

import asyncio
import json
from pathlib import Path

from starlette.requests import Request

from app import engine as engine_mod
from app import sources
from app.access import ADMIN
from app.sources import Item


def test_live_feeds_share_one_status_per_change(admin, app_main, monkeypatch):
    eng = app_main.engine
    built = []
    real = eng.status
    monkeypatch.setattr(eng, "status", lambda cid: built.append(cid) or real(cid))
    eng.note_changes()
    first = eng.shared_status("kid")
    assert eng.shared_status("kid") is first and built == ["kid"]
    # What one feed changes for its viewer stays out of the shared copy.
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": [], "scheme": "http",
                       "server": ("radio.test", 80), "query_string": b""})
    mine = app_main.build_status(request, "kid", ADMIN, shared=True)
    assert "stream" in mine["channels"][0] and "stream" not in first["channels"][0]
    # A change builds it again.
    eng.version += 1
    assert eng.shared_status("kid") is not first and built == ["kid", "kid"]


def test_the_status_works_out_each_channel_once(app_main, monkeypatch):
    eng = app_main.engine
    calls = []
    real = eng.now_playing
    monkeypatch.setattr(eng, "now_playing", lambda ch: calls.append(ch.id) or real(ch))
    eng.status("kid")
    assert sorted(calls) == sorted(c.id for c in eng.config.channels)


def test_api_calls_reuse_one_client():
    async def run():
        a, b = sources.shared(), sources.shared()
        assert a is b
        await sources.close_shared()
        assert a.is_closed and sources.shared() is not a
        await sources.close_shared()

    asyncio.run(run())


def test_downloads_run_side_by_side(admin, app_main, monkeypatch, tmp_path):
    eng = app_main.engine
    running, most = 0, 0

    async def slow(item, *a, **kw):
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.2)
        running -= 1
        p = tmp_path / f"{item.item_id}.mp3"
        p.write_bytes(b"x" * 20000)
        return p

    monkeypatch.setattr(engine_mod, "download", slow)

    async def run():
        eng.downloads = asyncio.Queue()
        for n in range(3):
            eng.enqueue(Item(kind="podcast", source_id="par", source_name="Pod", item_id=f"par{n}", title=f"Ep {n}",
                             media=f"https://pod.test/{n}.mp3", channel="kid"))
        workers = [asyncio.create_task(eng._download_loop()) for _ in range(engine_mod.DOWNLOAD_WORKERS)]
        await asyncio.sleep(0.5)
        for w in workers:
            w.cancel()

    asyncio.run(run())
    assert most == 3
    mine = [i for i in eng.items if i.source_id == "par"]
    assert all(i.state == "ready" for i in mine)
    for i in mine:
        eng.remove(i.uid)


def test_ytdlp_gives_up_after_the_deadline(monkeypatch, tmp_path):
    monkeypatch.setattr(sources.shutil, "which", lambda name: "/bin/sleep")
    monkeypatch.setattr(sources, "DOWNLOAD_DEADLINE", 0.3)
    real = asyncio.create_subprocess_exec
    monkeypatch.setattr(sources.asyncio, "create_subprocess_exec", lambda *a, **kw: real("sleep", "5", **kw))

    async def run():
        try:
            await sources._ytdlp("abcdefghijk", tmp_path / "x")
        except RuntimeError as e:
            return str(e)

    assert "longer than" in asyncio.run(run())


def test_renaming_a_channel_does_not_restart_liquidsoap(app_main, monkeypatch):
    eng = app_main.engine
    restarts = []

    async def restart():
        restarts.append(1)

    monkeypatch.setattr(eng.liq, "restart", restart)
    monkeypatch.setattr(eng, "_restart_at", 0.0)
    liq = [{**c, "name": c["name"] + " (old name)"} for c in eng._channel_sig()]
    assert asyncio.run(eng._sync_channels(liq)) is False and not restarts
    assert json.loads(Path(engine_mod.CHANNELS_FILE).read_text()) == eng._channel_sig()
    # A new or removed channel still needs one.
    assert asyncio.run(eng._sync_channels(liq[:1])) is True and restarts
