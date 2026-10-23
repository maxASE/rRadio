"""App pairing and what a limited device may do."""

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
