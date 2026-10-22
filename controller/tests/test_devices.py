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
