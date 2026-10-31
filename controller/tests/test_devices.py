"""App pairing and what a limited device may do."""

import httpx
import pytest


def pair(admin, anon, **body):
    r = admin.post("/api/devices/pair", json={"name": "Phone", "server": "https://radio.example", **body})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["uri"].startswith("rradio://pair?") and "<svg" in p["qr_svg"]
    r = anon.post("/api/pair", json={"code": p["code"].lower(), "device_name": "Test phone"})
    assert r.status_code == 200, r.text
    return r.json()


def test_code_works_once(admin, anon):
    p = admin.post("/api/devices/pair", json={"name": "Once"}).json()
    assert anon.post("/api/pair", json={"code": p["code"]}).status_code == 200
    assert anon.post("/api/pair", json={"code": p["code"]}).status_code == 404


def test_member_is_limited_to_its_channel(admin, anon):
    d = pair(admin, anon, role="member", channels=["kid"])
    auth = {"Authorization": f"Bearer {d['token']}"}
    assert [c["id"] for c in d["channels"]] == ["kid"]
    me = anon.get("/api/me", headers=auth).json()
    assert me["role"] == "member" and me["api"] >= 1
    st = anon.get("/api/status", headers=auth)
    assert st.status_code == 200 and st.json()["channel"]["id"] == "kid"
    assert [c["id"] for c in st.json()["channels"]] == ["kid"]
    main_id = admin.get("/api/config").json()["channels"][0]["id"]
    assert anon.get(f"/api/status?channel={main_id}", headers=auth).status_code == 403
    assert anon.get("/api/config", headers=auth).status_code == 403
    assert anon.get("/api/devices", headers=auth).status_code == 403


@pytest.mark.parametrize("role", ["admin"])
def test_promote_and_revoke(admin, anon, role):
    d = pair(admin, anon, role="member")
    auth = {"Authorization": f"Bearer {d['token']}"}
    did = d["device"]["id"]
    assert admin.patch(f"/api/devices/{did}", json={"role": role}).status_code == 200
    assert anon.get("/api/config", headers=auth).status_code == 200
    assert admin.delete(f"/api/devices/{did}").status_code == 200
    assert anon.get("/api/me", headers=auth).status_code == 401


def test_unknown_key(anon):
    assert anon.get("/api/status", headers={"Authorization": "Bearer rr_nope"}).status_code == 401


def test_member_picks_a_navidrome_playlist_as_station(admin, anon, app_main, monkeypatch):
    async def playlists(self):
        return [{"id": "p1", "name": "Morning", "songs": 12}]

    monkeypatch.setattr(app_main.Navidrome, "playlists", playlists)
    cfg = admin.get("/api/config").json()
    cfg["settings"].update(navidrome_url="http://nd.example", navidrome_user="max")
    assert admin.put("/api/config", json=cfg).status_code == 200
    d = pair(admin, anon, role="member", channels=["kid"])
    auth = {"Authorization": f"Bearer {d['token']}"}
    assert anon.get("/api/navidrome/playlists", headers=auth).json()[0]["name"] == "Morning"
    r = anon.post("/api/station", headers=auth, json={"channel": "kid", "playlist": "morning"})
    assert r.status_code == 200, r.text
    st = anon.get("/api/status?channel=kid", headers=auth).json()
    picked = next(s for s in st["stations"] if s["id"] == st["current_station"])
    assert picked["kind"] == "navidrome" and picked["playlist"] == "Morning"
    # The same playlist again reuses that station.
    again = anon.post("/api/station", headers=auth, json={"channel": "kid", "playlist": "Morning"}).json()
    assert again["id"] == picked["id"]
    assert anon.post("/api/station", headers=auth, json={"channel": "kid", "playlist": "Nope"}).status_code == 404


def test_member_quiets_its_channel_until_morning(admin, anon, app_main):
    import time

    d = pair(admin, anon, role="member", channels=["kid"])
    auth = {"Authorization": f"Bearer {d['token']}"}
    until = time.time() + 3600
    r = anon.post("/api/quiet", headers=auth, json={"channel": "kid", "until": until})
    assert r.status_code == 200 and r.json()["quiet_until"] == until
    assert anon.get("/api/status?channel=kid", headers=auth).json()["quiet_until"] == until
    cs = app_main.engine.state("kid")
    assert cs.quiet_until == until
    # A time in the past, or null, ends it.
    assert anon.post("/api/quiet", headers=auth, json={"channel": "kid", "until": None}).json()["quiet_until"] is None
    assert anon.get("/api/status?channel=kid", headers=auth).json()["quiet_until"] is None
    assert anon.post("/api/quiet", headers=auth, json={"channel": "kid", "until": 5}).json()["quiet_until"] is None


def test_health_is_for_admins_and_checks_invidious_once_a_minute(admin, anon, app_main, monkeypatch):
    calls = []

    async def get(self, url, **kw):
        calls.append(url)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(app_main, "_invidious_check", (0.0, {}))
    h = admin.get("/api/health").json()
    assert h["invidious"]["ok"] is False and "refused" in h["invidious"]["error"]
    assert h["disk"]["size"] > 0 and isinstance(h["disk"]["ok"], bool)
    assert h["stream"]["channels"] == len(h["listeners"]) and h["stream"]["ok"] is False  # no engine in tests
    assert {"channel", "name", "listeners"} <= set(h["listeners"][0])
    admin.get("/api/health")
    assert len(calls) == 1 and calls[0].endswith("/api/v1/stats")

    d = pair(admin, anon, role="member")
    assert anon.get("/api/health", headers={"Authorization": f"Bearer {d['token']}"}).status_code == 403


def test_admin_switches_streams_with_data_per_hour(admin, app_main):
    old = list(app_main.store.config.settings.streams)
    try:
        s = admin.put("/api/streams", json={"on": ["aac", "mp3"]}).json()["streams"]
        mp3 = next(x for x in s if x["id"] == "mp3")
        assert mp3 == {"id": "mp3", "format": "MP3", "kbps": 128, "mb_per_hour": 58, "on": True, "listeners": None}
        assert [x["id"] for x in s if x["on"]] == ["mp3", "aac"]
        assert app_main.store.config.settings.streams == ["mp3", "aac"]
        assert admin.put("/api/streams", json={"on": []}).status_code == 422
        assert admin.put("/api/streams", json={"on": ["flac"]}).status_code == 422
    finally:
        admin.put("/api/streams", json={"on": old})


def test_streams_count_listeners_per_format(admin, app_main, monkeypatch):
    monkeypatch.setattr(app_main.engine, "stream_listeners", {"mp3": 3})
    s = {x["id"]: x["listeners"] for x in admin.get("/api/streams").json()["streams"]}
    assert s == {"mp3": 3, "aac": 0, "opus": 0, "aac48": 0, "aac192": 0}


def test_phone_playing_a_clip_again_counts_as_listening(admin, anon, app_main):
    d = pair(admin, anon, role="member")
    auth = {"Authorization": f"Bearer {d['token']}"}
    cid = d["channels"][0]["id"]
    assert anon.post("/api/listening", json={"channel": cid}, headers=auth).status_code == 200
    phones = {x["channel"]: x["phones"] for x in admin.get("/api/health").json()["listeners"]}
    assert phones[cid] == 1
    assert admin.get("/api/streams").json()["listening_again"] == 1
    # Saying it again doesn't count the phone twice; stopping takes it off at once.
    anon.post("/api/listening", json={"channel": cid}, headers=auth)
    assert admin.get("/api/streams").json()["listening_again"] == 1
    anon.post("/api/listening", json={"channel": cid, "playing": False}, headers=auth)
    assert admin.get("/api/streams").json()["listening_again"] == 0


def test_streams_missing_on_icecast_are_reported_after_a_while(admin, app_main, monkeypatch):
    eng = app_main.engine
    monkeypatch.setattr(eng, "liq_up", True)
    monkeypatch.setattr(eng, "_missing_since", {})
    monkeypatch.setattr(eng, "missing_streams", [])
    cfg = app_main.store.config
    mounts = [c.mount for c in cfg.channels]
    # Icecast has every stream that is on except the first channel's last format.
    want = [f"{m}.{f}" for m in mounts for f in cfg.settings.streams]
    srcs = [{"listenurl": f"http://icecast:8000/{w}"} for w in want[:-1]]
    eng._check_mounts(srcs, now=1000.0)
    assert eng.missing_streams == []  # just started: give Liquidsoap time to connect
    eng._check_mounts(srcs, now=1000.0 + eng.MOUNT_GRACE)
    assert eng.missing_streams == [want[-1]]
    assert admin.get("/api/streams").json()["missing"] == [want[-1]]
    assert admin.get("/api/health").json()["missing_streams"] == [want[-1]]
    eng._check_mounts(srcs + [{"listenurl": f"http://icecast:8000/{want[-1]}"}], now=2000.0)
    assert eng.missing_streams == []


def test_hello_proves_the_radio_without_the_key(admin, anon):
    import hashlib
    import hmac

    d = pair(admin, anon, role="member")
    nonce = "0123456789abcdef0123"
    r = anon.get("/api/hello", params={"device": d["device"]["id"], "nonce": nonce})
    assert r.status_code == 200
    key = hashlib.sha256(d["token"].encode()).hexdigest().encode()
    assert r.json() == {"rradio": True, "proof": hmac.new(key, nonce.encode(), hashlib.sha256).hexdigest()}
    assert anon.get("/api/hello", params={"device": "nope", "nonce": nonce}).status_code == 404
    assert anon.get("/api/hello", params={"device": d["device"]["id"], "nonce": "short"}).status_code == 400
