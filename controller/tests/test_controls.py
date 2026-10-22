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
