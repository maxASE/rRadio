"""Live feed, voice messages and the everyday controls."""

import json


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


def test_spots_and_skipped_clips_are_not_kept(admin, app_main):
    from app.sources import Item

    spot = Item(kind="spot", source_id="news", source_name="News", item_id="n1", title="News 13:00",
                media="https://example.com/n.mp3", channel="kid")
    app_main.engine._finish(spot, "played")
    clip = Item(kind="podcast", source_id="p", source_name="Pod", item_id="e1", title="Episode",
                media="https://example.com/e.mp3", channel="kid")
    app_main.engine._finish(clip, "skipped")
    history = admin.get("/api/status", params={"channel": "kid"}).json()["history"]
    assert next(h for h in history if h["title"] == "News 13:00")["replay"] is False
    assert next(h for h in history if h["title"] == "Episode")["replay"] is False


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
                media="dQw4w9WgXcQ", channel="kid")
    app_main.engine._finish(clip, "played")
    entry = next(h for h in admin.get("/api/status", params={"channel": "kid"}).json()["history"] if h["title"] == "Again please")
    app_main.engine.downloads.put_nowait = lambda key: None  # no Invidious in tests
    r = admin.post(f"/api/history/{entry['id']}/play-next")
    assert r.status_code == 200, r.text
    queued = next(i for i in app_main.engine.items if i.uid == r.json()["key"])
    assert (queued.channel, queued.media, queued.priority) == ("kid", "dQw4w9WgXcQ", 1)
    app_main.engine.items.remove(queued)


def test_spots_cannot_be_queued_again(admin, app_main):
    from app.sources import Item

    spot = Item(kind="spot", source_id="news", source_name="News", item_id="n2", title="Old news",
                media="https://example.com/n2.mp3", channel="kid")
    app_main.engine._finish(spot, "played")
    entry = next(h for h in admin.get("/api/status", params={"channel": "kid"}).json()["history"] if h["title"] == "Old news")
    assert admin.post(f"/api/history/{entry['id']}/play-next").status_code == 404
