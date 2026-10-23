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
