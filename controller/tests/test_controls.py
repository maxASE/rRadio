"""Live feed, voice messages and the everyday controls."""

import json
from pathlib import Path


def test_status_shape(admin):
    s = admin.get("/api/status").json()
    for key in ("channel", "channels", "now", "queue", "history", "time", "streams"):
        assert key in s


def test_events_sends_status_first(admin, app_main, monkeypatch):
    # The test client can't signal a disconnect, so let the feed end by itself.
    monkeypatch.setattr(app_main, "EVENTS_PING", 0.1)
    monkeypatch.setattr(app_main, "EVENTS_MAX", 0.5)
    with admin.stream("GET", "/api/events") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if line.startswith("data: "):
                data = json.loads(line[6:])
                assert "now" in data and "queue" in data
                break


def test_voice_message_goes_to_ram_and_is_deleted_after(admin, app_main, ram_dir):
    audio = b"\x1aE\xdf\xa3" + b"\0" * 4000  # WebM magic; the content is not decoded here
    r = admin.post("/api/voice", files={"file": ("v.webm", audio, "audio/webm;codecs=opus")},
                   data={"channel": "all", "title": "Dinner"})
    assert r.status_code == 200, r.text
    chans = r.json()["channels"]
    assert len(chans) == 2
    items = [i for i in app_main.engine.items if i.kind == "voice"]
    assert {i.channel for i in items} == set(chans)
    assert all(i.state == "ready" and i.path.startswith(str(ram_dir)) for i in items)
    path = items[0].path
    for it in items:
        app_main.engine._finish(it, "played")
    assert not any(p.name == path.rsplit("/", 1)[-1] for p in ram_dir.iterdir())


def test_voice_rejects_non_audio(admin):
    r = admin.post("/api/voice", files={"file": ("x.txt", b"x" * 2000, "text/plain")}, data={"channel": "all"})
    assert r.status_code == 415


def test_play_url_and_remove_from_queue(admin, app_main):
    r = admin.post("/api/play-url", json={"url": "https://example.com/clip.mp3"})
    assert r.status_code == 200, r.text
    key = r.json()["key"]
    assert any(i.uid == key for i in app_main.engine.items)
    assert admin.delete(f"/api/queue/{key}").status_code == 200
    assert not any(i.uid == key for i in app_main.engine.items)


def test_end_program_without_one(admin):
    assert admin.post("/api/program/end").status_code == 409


def test_config_round_trip(admin):
    cfg = admin.get("/api/config").json()
    cfg["settings"]["station_name"] = "Test Radio"
    assert admin.put("/api/config", json=cfg).status_code == 200
    assert admin.get("/api/status").json()["station_name"] == "Test Radio"


def test_voice_message_can_be_played_again_for_a_day(admin, app_main):
    audio = b"\0\0\0\x18ftypM4A " + b"\0" * 4000
    r = admin.post("/api/voice", files={"file": ("v.m4a", audio, "audio/mp4")}, data={"channel": "all", "title": "Again"})
    assert r.status_code == 200, r.text
    for it in [i for i in app_main.engine.items if i.kind == "voice"]:
        app_main.engine._finish(it, "played")
    entry = next(h for h in admin.get("/api/status").json()["history"] if h["title"] == "Again")
    assert entry["replay"] is True
    got = admin.get(f"/api/history/{entry['id']}/audio")
    assert got.status_code == 200
    assert got.content == audio


def test_news_and_skipped_clips_can_be_played_again(admin, app_main):
    from app.sources import Item

    spot = Item(kind="spot", source_id="news", source_name="News", item_id="n1", title="News 13:00",
                media="https://example.com/n.mp3", channel="kid")
    app_main.engine._finish(spot, "played")
    clip = Item(kind="podcast", source_id="p", source_name="Pod", item_id="e1", title="Episode",
                media="https://example.com/e.mp3", channel="kid")
    app_main.engine._finish(clip, "skipped")
    history = admin.get("/api/status", params={"channel": "kid"}).json()["history"]
    assert next(h for h in history if h["title"] == "News 13:00")["replay"] is True
    assert next(h for h in history if h["title"] == "Episode")["replay"] is True


def test_played_clips_are_fetched_again_from_their_source(admin, app_main):
    from app.sources import Item

    clip = Item(kind="podcast", source_id="p", source_name="Pod", item_id="e2", title="Played episode",
                media="https://example.com/e2.mp3", channel="kid")
    app_main.engine._finish(clip, "played")
    entry = next(h for h in admin.get("/api/status", params={"channel": "kid"}).json()["history"] if h["title"] == "Played episode")
    assert entry["replay"] is True
    assert app_main.state.replay_of(entry["id"]) == ("kid", {"media": "https://example.com/e2.mp3"})


def test_unknown_history_entry_cannot_be_played(admin):
    assert admin.get("/api/history/999999/audio").status_code == 404


def test_history_clip_can_be_queued_again(admin, app_main):
    from app.sources import Item

    clip = Item(kind="video", source_id="vid", source_name="Riks", item_id="v1", title="Again please",
                media="dQw4w9WgXcQ", channel="kid", duration=702)
    app_main.engine._finish(clip, "played")
    entry = next(h for h in admin.get("/api/status", params={"channel": "kid"}).json()["history"] if h["title"] == "Again please")
    assert entry["duration"] == 702  # the app trusts this over the player's guess
    app_main.engine.downloads.put_nowait = lambda key: None  # no Invidious in tests
    r = admin.post(f"/api/history/{entry['id']}/play-next")
    assert r.status_code == 200, r.text
    queued = next(i for i in app_main.engine.items if i.uid == r.json()["key"])
    assert (queued.channel, queued.media, queued.priority) == ("kid", "dQw4w9WgXcQ", 1)
    app_main.engine.items.remove(queued)


def test_news_can_be_queued_again(admin, app_main):
    from app.sources import Item

    spot = Item(kind="spot", source_id="news", source_name="News", item_id="n2", title="Old news",
                media="https://example.com/n2.mp3", channel="kid")
    app_main.engine._finish(spot, "played")
    entry = next(h for h in admin.get("/api/status", params={"channel": "kid"}).json()["history"] if h["title"] == "Old news")
    app_main.engine.downloads.put_nowait = lambda key: None
    r = admin.post(f"/api/history/{entry['id']}/play-next")
    assert r.status_code == 200, r.text
    queued = next(i for i in app_main.engine.items if i.uid == r.json()["key"])
    assert queued.media == "https://example.com/n2.mp3"
    app_main.engine.items.remove(queued)

def test_a_kept_program_is_recorded_and_listened_back(admin, app_main, ram_dir):
    import asyncio

    from app.config import Program

    engine = app_main.engine
    ch = engine.config.channels[0]
    cs = engine.state(ch.id)
    show = Program(name="Live am Morgen", url="http://radio.example/live", keep_day=True,
                   image="http://radio.example/logo.png")

    async def fake_record(p, path):  # no network in tests: write a little "audio"
        path.write_bytes(b"ID3" + b"\0" * 2000)
        await asyncio.sleep(3600)

    async def run():
        engine._record = fake_record
        replay = engine._start_recording(ch, cs, show)
        await asyncio.sleep(0.01)
        task = cs.recording
        engine._stop_recording(cs)
        await asyncio.sleep(0)
        return replay, task

    try:
        replay, task = asyncio.run(run())
    finally:
        del engine._record
    assert task.cancelled() and cs.recording is None
    assert replay["path"].startswith(str(ram_dir / "replay"))
    engine.store.add_history("program", show.name, show.name, "started", channel=ch.id, replay=replay)
    entry = next(h for h in admin.get("/api/status", params={"channel": ch.id}).json()["history"]
                 if h["title"] == show.name and h["replay"])
    assert entry["art"].startswith("/api/art/")  # the app shows the show's picture while listening back
    r = admin.get(f"/api/history/{entry['id']}/audio")
    assert r.status_code == 200 and r.content.startswith(b"ID3")
    # The app jumps around in the recording (the live timeline), so it must answer ranges.
    r = admin.get(f"/api/history/{entry['id']}/audio", headers={"Range": "bytes=1000-1999"})
    assert r.status_code == 206 and len(r.content) == 1000
    # A three-hour show is for listening back, not for putting on the radio again.
    assert admin.post(f"/api/history/{entry['id']}/play-next").status_code == 409
    # Removed from memory before its 24 hours are up.
    assert admin.delete(f"/api/history/{entry['id']}/replay").status_code == 200
    assert not Path(replay["path"]).exists()
    assert admin.get(f"/api/history/{entry['id']}/audio").status_code == 404
    assert admin.delete(f"/api/history/{entry['id']}/replay").status_code == 404


def test_a_restart_carries_on_with_the_program(app_main, ram_dir):
    import asyncio

    from app.config import Program

    engine = app_main.engine
    ch = engine.config.channels[0]
    cs = engine.state(ch.id)
    show = Program(name="Live am Abend", url="http://radio.example/live", keep_day=True)
    old = ram_dir / "replay" / "program-old.mp3"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"ID3" + b"\0" * 100)
    engine.store.add_history("program", show.name, show.name, "started", channel=ch.id,
                             replay={"path": str(old), "until": 1e12})
    entry_id, _, replay = engine.store.open_program(ch.id, show.name, 0)
    assert engine.store.open_program(ch.id, "Other show", 0) is None

    async def fake_record(p, path):
        await asyncio.sleep(3600)

    async def run(resume):
        engine._record = fake_record
        out = engine._start_recording(ch, cs, show, resume=resume)
        engine._stop_recording(cs)
        await asyncio.sleep(0)
        return out

    try:
        # The file survived (recordings on disk): it goes on in the same file.
        assert asyncio.run(run(replay)) == replay
        # The RAM disk was emptied: a new file that says when it starts.
        old.unlink()
        fresh = asyncio.run(run(replay))
    finally:
        del engine._record
    assert fresh["path"] != str(old) and fresh["from"] > 0
    engine.store.set_replay(entry_id, fresh)
    entry = next(h for h in engine.store.history(5, ch.id) if h["id"] == entry_id)
    assert entry["replay"] and entry["replay_from"] == fresh["from"]
    engine.store.add_history("program", show.name, show.name, "ended", channel=ch.id)
    assert engine.store.open_program(ch.id, show.name, 0) is None


def test_programs_are_not_kept_unless_asked(app_main):
    from app.config import Program

    engine = app_main.engine
    ch = engine.config.channels[0]
    assert engine._start_recording(ch, engine.state(ch.id), Program(name="P", url="http://radio.example/live")) is None


def test_a_title_show_that_ended_stays_over_after_a_restart(app_main):
    """Hafenradio keeps sending the show's title for hours after it ends; a restart in the
    window must not start the show again from that title."""
    from types import SimpleNamespace

    from app.config import Program

    engine = app_main.engine
    ch = engine.config.channels[0]
    cs = engine.state(ch.id)
    show = Program(name="Live am Morgen", url="http://radio.example/live", trigger="title",
                   title_start="Jetzt: Hafenradio Live", start="00:00", end="23:59")
    ch.programs.append(show)
    engine.titles.by_url[show.watch_url] = SimpleNamespace(title="Jetzt: Hafenradio Live - Der Tag beginnt mit Hafenradio")
    try:
        now = engine.now()
        engine.store.add_history("program", show.name, show.name, "started", channel=ch.id)
        engine.store.add_history("program", show.name, show.name, "ended", "title: Musik", channel=ch.id)
        cs.shows.pop(show.id, None)  # as after a restart
        engine._shows_tick(ch, cs, now)
        assert cs.shows[show.id]["on"] is None and cs.shows[show.id]["ended_at"]
        # Without an earlier end in the window, the same title starts it.
        engine.store.ended_since = lambda *a: None
        cs.shows.pop(show.id)
        engine._shows_tick(ch, cs, now)
        assert cs.shows[show.id]["on"] is not None
    finally:
        del engine.store.ended_since
        del engine.titles.by_url[show.watch_url]
        ch.programs.remove(show)
        cs.shows.pop(show.id, None)


def test_week_lists_programs_and_spots(admin, app_main):
    cfg = admin.get("/api/config").json()
    ch = cfg["channels"][0]
    ch["programs"] = [{"name": "Late show", "url": "https://radio.example/late", "start": "22:00", "end": "02:00",
                       "days": ["mon"]}]
    ch["spots"] = [{"name": "News", "content": "feed", "feed_url": "https://news.example/feed", "trigger": "every",
                    "every_minutes": 60, "active_from": "08:00", "active_to": "10:00", "days": ["mon"]}]
    assert admin.put("/api/config", json=cfg).status_code == 200
    w = admin.get(f"/api/week?channel={ch['id']}&day=2026-10-21").json()
    assert [d["date"] for d in w["days"]][0] == "2026-10-19" and len(w["days"]) == 7
    mon, tue = w["days"][0], w["days"][1]
    assert [(p["start"], p["end"]) for p in mon["programs"]] == [(22 * 60, 1440)]
    assert [(p["start"], p["end"]) for p in tue["programs"]] == [(0, 120)]  # past midnight
    assert [s["minute"] for s in mon["spots"]] == [8 * 60, 9 * 60]
    assert tue["spots"] == []
    assert admin.get("/api/week?day=nope").status_code == 400
    # A show that really ran carries its own times.
    from datetime import datetime
    tz = app_main.store.config.settings.tz
    t0 = datetime(2026, 10, 19, 22, 5, tzinfo=tz).timestamp()
    st = app_main.engine.store
    with st._lock, st._db:
        st._db.execute("INSERT INTO history (ts, kind, source, title, status, detail, channel, replay)"
                       " VALUES (?, 'program', 'Late show', 'Late show', 'started', '', ?, '')", (t0, ch["id"]))
        st._db.execute("INSERT INTO history (ts, kind, source, title, status, detail, channel, replay)"
                       " VALUES (?, 'program', 'Late show', 'Late show', 'ended', '', ?, '')", (t0 + 3000, ch["id"]))
    mon = admin.get(f"/api/week?channel={ch['id']}&day=2026-10-21").json()["days"][0]
    assert mon["programs"][0]["ran"]["from"] == 22 * 60 + 5 and mon["programs"][0]["ran"]["to"] == 22 * 60 + 55


def test_the_clip_on_air_can_be_listened_back(admin, app_main, monkeypatch, tmp_path):
    from app.sources import Item

    eng = app_main.engine
    cid = app_main.store.config.channels[0].id
    it = Item(kind="video", source_id="src", source_name="Riks", item_id="abc", title="Clip",
              media="dQw4w9WgXcQ", state="playing", channel=cid)
    eng.items.append(it)
    try:
        assert eng.now_audio(cid) == {"media": "dQw4w9WgXcQ"}
        assert eng.now_audio(cid, key=it.key) == {"media": "dQw4w9WgXcQ"}
        assert eng.now_audio(cid, key="src:other") is None
        assert admin.get(f"/api/now/audio?channel={cid}&key=src:other").status_code == 410

        async def fake(info, request):
            return app_main.JSONResponse(info)

        monkeypatch.setattr(app_main, "replay_response", fake)
        assert admin.get(f"/api/now/audio?channel={cid}&key={it.key}").json() == {"media": "dQw4w9WgXcQ"}
        # the radio's own copy, when the phone can play it, so it can jump anywhere at once
        for name, want in (("clip.m4a", True), ("clip.webm", False)):
            f = tmp_path / name
            f.write_bytes(b"audio")
            it.path = str(f)
            assert eng.now_audio(cid) == ({"path": str(f)} if want else {"media": "dQw4w9WgXcQ"})
    finally:
        eng.items.remove(it)
    assert admin.get(f"/api/now/audio?channel={cid}").status_code == 410


def test_saved_for_later_keeps_the_file_on_disk(admin, app_main, ram_dir):
    import time

    from app.engine import SAVED_DIR

    folder = ram_dir / "replay"
    folder.mkdir(exist_ok=True)
    clip = folder / "voice-later.m4a"
    clip.write_bytes(b"voice")
    app_main.state.add_history("voice", "Dad", "Save me", "played", channel="kid",
                               replay={"path": str(clip), "until": time.time() + 60})
    entry = next(h for h in admin.get("/api/status", params={"channel": "kid"}).json()["history"] if h["title"] == "Save me")
    assert entry["saved"] is None
    assert admin.post(f"/api/history/{entry['id']}/save").status_code == 200
    saved = admin.get("/api/saved", params={"channel": "kid"}).json()
    assert saved[0]["id"] == entry["id"] and saved[0]["saved"]
    _, info = app_main.state.replay_of(entry["id"])
    kept = Path(info["path"])
    assert kept.parent == SAVED_DIR and kept.read_bytes() == b"voice" and "until" not in info
    assert not clip.exists()  # no second copy left taking up RAM
    assert admin.get("/api/saved", params={"channel": app_main.store.config.channels[0].id}).json() == []
    # Taken back: the saved copy goes, and with it the only audio of a voice message.
    assert admin.delete(f"/api/history/{entry['id']}/save").status_code == 200
    assert not kept.exists()
    assert admin.get("/api/saved", params={"channel": "kid"}).json() == []


def test_a_clip_from_its_source_can_be_saved_and_searched(admin, app_main):
    app_main.state.add_history("video", "Bundestag", "Rede zum Haushalt", "played", channel="kid",
                               replay={"media": "abc123", "duration": 600})
    app_main.state.add_history("video", "Other", "Something else", "played", channel="kid", replay={"media": "x"})
    found = admin.get("/api/search", params={"channel": "kid", "q": "haushalt"}).json()
    assert [h["title"] for h in found["history"]] == ["Rede zum Haushalt"]
    assert found["videos"] == []  # Videos only when asked
    entry = found["history"][0]
    assert admin.post(f"/api/history/{entry['id']}/save").status_code == 200
    assert app_main.state.entry(entry["id"])["replay"] == {"media": "abc123", "duration": 600}
    assert admin.get("/api/search", params={"channel": "kid", "q": "50%"}).json()["history"] == []
    assert admin.get("/api/search", params={"channel": "kid", "q": "h"}).json()["history"] == []
    admin.delete(f"/api/history/{entry['id']}/save")
    assert app_main.state.entry(entry["id"])["replay"] == {"media": "abc123", "duration": 600}


def test_up_next_can_be_reordered(admin, app_main):
    from app.sources import Item

    eng = app_main.engine
    items = [Item(kind="video", source_id="vid", source_name="S", item_id=f"r{n}", title=f"R{n}",
                  media="m", channel="kid", state="ready", arrived=100.0 + n) for n in range(3)]
    eng.items.extend(items)
    try:
        r = admin.post("/api/queue/order", json={"channel": "kid", "keys": [items[2].uid, items[0].uid, items[1].uid, "nope"]})
        assert r.json() == {"ok": True, "moved": 3}
        queue = [q["title"] for q in admin.get("/api/status", params={"channel": "kid"}).json()["queue"] if q["title"].startswith("R")]
        assert queue == ["R2", "R0", "R1"]
    finally:
        for it in items:
            eng.items.remove(it)


def test_week_numbers_count_what_played(admin, app_main):
    app_main.state.add_history("spot", "Ekot", "Ekot 07:00", "played", channel="kid", replay={"media": "e", "duration": 120})
    nums = admin.get("/api/week/numbers", params={"channel": "kid"}).json()
    assert len(nums["days"]) == 7 and nums["days"][-1]["date"] == nums["today"]
    assert nums["totals"]["news"] >= 1 and nums["days"][-1]["news"] >= 1
    assert any(t["name"] == "Ekot" for t in nums["top"])


def test_history_forgets_unsaved_entries_after_a_week(app_main):
    state = app_main.state
    state.add_history("voice", "Dad", "Old news", "played", channel="kid")
    state.add_history("voice", "Dad", "Old but saved", "played", channel="kid")
    old, kept = [e["id"] for e in state.history(2, "kid")][::-1]
    state.set_saved(kept, True)
    with state._lock, state._db:
        state._db.execute("UPDATE history SET ts = ts - 8 * 86400 WHERE id IN (?, ?)", (old, kept))
    state.add_history("voice", "Dad", "Today", "played", channel="kid")
    ids = {e["id"] for e in state.history(500, "kid")}
    assert old not in ids and kept in ids


def test_a_full_ram_disk_moves_the_recording_to_disk(app_main, ram_dir):
    import asyncio

    from app.config import Program
    from app.sources import CACHE_DIR

    engine = app_main.engine
    ch = engine.config.channels[0]
    show = Program(name="Live am Morgen", url="http://radio.example/live", keep_day=True)
    path = ram_dir / "replay" / "program-full.mp3"
    path.parent.mkdir(parents=True, exist_ok=True)
    engine.store.add_history("program", show.name, show.name, "started", channel=ch.id,
                             replay={"path": str(path), "until": 1e12})
    entry_id = engine.store.history(1, ch.id)[0]["id"]

    async def run():
        out = path.open("ab")
        out.write(b"ID3" + b"\0" * 100)
        out.flush()
        target, out = await engine._record_to_disk(show, path, out)
        out.write(b"more")  # the recording goes on in the new file
        out.close()
        with target.open("ab") as f:
            again = await engine._record_to_disk(show, target, f)
        return target, asyncio.current_task().get_name(), again

    target, name, again = asyncio.run(run())
    assert target == CACHE_DIR / "replay" / path.name and name == str(target)
    assert not path.exists() and target.read_bytes() == b"ID3" + b"\0" * 100 + b"more"
    assert engine.store.replay_of(entry_id)[1]["path"] == str(target)
    assert again is None  # already on disk: when that is full too, the recording stops


def test_the_status_says_when_the_ram_disk_is_nearly_full(admin, app_main, ram_dir, monkeypatch):
    import shutil as sh

    from app import sources

    ram = admin.get("/api/status").json()["ram"]
    assert ram["size"] > 0 and ram["used"] >= 0 and "low" in ram
    (ram_dir / "replay").mkdir(exist_ok=True)
    before = sum(f.stat().st_size for f in (ram_dir / "replay").glob("*"))  # left by other tests
    (ram_dir / "replay" / "show.m4a").write_bytes(b"\0" * 1000)
    full = sh._ntuple_diskusage(512 << 20, 500 << 20, 12 << 20)
    monkeypatch.setattr(sources.shutil, "disk_usage", lambda p: full)
    ram = admin.get("/api/status").json()["ram"]
    assert ram == {"size": 512 << 20, "used": 500 << 20, "kept": before + 1000, "low": True}
    (ram_dir / "replay" / "show.m4a").unlink()
    assert sources.cache_dir(True) == sources.CACHE_DIR  # downloads go to disk meanwhile


def test_listen_again_fetches_clips_in_small_pieces(admin, app_main, monkeypatch):
    # The video site sends a long Range slowly: a whole clip is fetched in CHUNK pieces and
    # passed on as one answer, and a player's Range still gets what it asked for.
    import re

    import httpx

    from app import sources
    from app.sources import Item

    audio = bytes(range(256)) * 50_000  # 12.8 MB
    asked = []

    def video(request):
        if request.url.path == "/latest":
            return httpx.Response(302, headers={"location": "https://cdn.example/videoplayback"})
        a, b = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["range"]).groups())
        asked.append((a, b))
        return httpx.Response(206, content=audio[a:b + 1], headers={
            "content-type": "audio/mp4", "content-range": f"bytes {a}-{min(b, len(audio) - 1)}/{len(audio)}"})

    monkeypatch.setattr(sources, "client", lambda timeout=30: httpx.AsyncClient(
        transport=httpx.MockTransport(video), follow_redirects=True))
    clip = Item(kind="podcast", source_id="p", source_name="Pod", item_id="e3", title="Long clip",
                media="https://cdn.example/latest", channel="kid")
    app_main.engine._finish(clip, "played")
    entry = next(h for h in admin.get("/api/status", params={"channel": "kid"}).json()["history"] if h["title"] == "Long clip")

    r = admin.get(f"/api/history/{entry['id']}/audio")
    assert r.status_code == 200
    assert r.headers["content-length"] == str(len(audio))
    assert r.content == audio
    assert all(b - a < sources.CHUNK for a, b in asked)

    asked.clear()
    r = admin.get(f"/api/history/{entry['id']}/audio", headers={"Range": "bytes=5000000-"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 5000000-{len(audio) - 1}/{len(audio)}"
    assert r.content == audio[5_000_000:]
    assert asked[0][0] == 5_000_000

    r = admin.get(f"/api/history/{entry['id']}/audio", headers={"Range": "bytes=0-1"})
    assert r.status_code == 206 and r.content == audio[:2]


def test_parse_range():
    from app.sources import parse_range

    assert parse_range("bytes=0-1") == (0, 1)
    assert parse_range("bytes=100-") == (100, None)
    assert parse_range("bytes=-500") is None
    assert parse_range("bytes=0-1,5-9") is None
    assert parse_range(None) is None


def test_a_kept_program_has_its_length_once_it_ended(tmp_path):
    """The Saved list shows a program's length (and how much is left) before it is played,
    like a clip's."""
    import time

    from app.store import Store

    rec = tmp_path / "program-kid-1.mp3"
    rec.write_bytes(b"ID3")
    st = Store(tmp_path / "state.db")
    with st._lock, st._db:  # kept before lengths were written down: an hour and a half
        st._db.execute("INSERT INTO history (ts, kind, source, title, status, detail, channel, replay)"
                       " VALUES (1000, 'program', 'Old show', 'Old show', 'started', '', 'kid', ?)",
                       (json.dumps({"path": str(rec)}),))
        st._db.execute("INSERT INTO history (ts, kind, source, title, status, detail, channel, replay)"
                       " VALUES (6400, 'program', 'Old show', 'Old show', 'ended', '', 'kid', '')")
    st = Store(tmp_path / "state.db")  # a restart fills it in
    assert [h["duration"] for h in st.history(channel="kid")] == [None, 5400]

    st.add_history("program", "Morgen", "Morgen", "started", channel="kid", replay={"path": str(rec)})
    assert st.history(channel="kid")[0]["duration"] is None  # still growing
    started = st.history(channel="kid")[0]["id"]
    with st._lock, st._db:
        st._db.execute("UPDATE history SET ts = ts - 600 WHERE id = ?", (started,))
    st.add_history("program", "Morgen", "Morgen", "ended", channel="kid")
    assert st.entry(started)["replay"]["duration"] in (600, 601)
    # A recording that missed the start (a restart during the show) is shorter.
    st.add_history("program", "Abend", "Abend", "started", channel="kid",
                   replay={"path": str(rec), "from": time.time() - 120})
    st.add_history("program", "Abend", "Abend", "ended", channel="kid")
    assert st.history(channel="kid")[1]["duration"] in (120, 121)
