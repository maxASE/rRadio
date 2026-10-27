"""User accounts: own channels, public channels, and what users may not do."""

import io
import itertools

from fastapi.testclient import TestClient

_n = itertools.count()


def quiet(client, cid, **kw):
    """A control call that needs no Liquidsoap."""
    return client.post("/api/quiet", json={"channel": cid, "until": None}, **kw)


def make_user(admin, app_main, role="user"):
    name = f"user{next(_n)}"
    r = admin.post("/api/users", json={"name": name, "password": "pw-" + name + "-long", "role": role})
    assert r.status_code == 200, r.text
    c = TestClient(app_main.app, follow_redirects=False)
    assert c.post("/api/login", json={"username": name, "password": "pw-" + name + "-long"}).status_code == 200
    return r.json(), c


def add_channel(client, name, mount, **extra):
    cfg = client.get("/api/config").json()
    cfg["channels"].append({"name": name, "mount": mount, "stations": [{"name": "S", "url": "http://s.example/x"}], **extra})
    r = client.put("/api/config", json=cfg)
    assert r.status_code == 200, r.text
    return next(c for c in r.json()["channels"] if c["mount"] == mount)


def test_admin_only_manages_users(admin, app_main):
    _, c = make_user(admin, app_main)
    assert c.get("/api/users").status_code == 403
    assert c.post("/api/users", json={"name": "x", "password": "12345678"}).status_code == 403
    assert admin.post("/api/users", json={"name": "admin", "password": "12345678"}).status_code == 422
    assert admin.post("/api/users", json={"name": "short", "password": "1"}).status_code == 422


def test_user_owns_new_channel_and_cannot_touch_others(admin, app_main):
    u, c = make_user(admin, app_main)
    ch = add_channel(c, "Mine", f"mine-{u['name']}")
    assert ch["owner"] == u["id"] and ch["public"] is False
    cfg = c.get("/api/config").json()
    # The admin's private channels are not visible, and settings can't be changed.
    assert all(x["owner"] == u["id"] or x["public"] for x in cfg["channels"])
    cfg["settings"]["station_name"] = "Hacked"
    cfg["channels"].append({"id": "kid", "name": "Stolen", "mount": "stolen", "owner": u["id"]})
    assert c.put("/api/config", json=cfg).status_code == 200
    full = admin.get("/api/config").json()
    assert full["settings"]["station_name"] != "Hacked"
    assert next(x for x in full["channels"] if x["id"] == "kid")["name"] == "Kid"
    assert quiet(c, "kid").status_code == 403
    assert c.get("/api/status?channel=kid").status_code == 403
    assert c.get("/api/backup").status_code == 403
    assert quiet(c, ch["id"]).status_code == 200
    # Removing their channel from the list deletes it; others' channels stay.
    cfg = c.get("/api/config").json()
    cfg["channels"] = [x for x in cfg["channels"] if x["id"] != ch["id"]]
    c.put("/api/config", json=cfg)
    full = admin.get("/api/config").json()
    assert not any(x["id"] == ch["id"] for x in full["channels"]) and any(x["id"] == "kid" for x in full["channels"])


def test_public_channel_is_listen_only_for_others(admin, app_main):
    owner, a = make_user(admin, app_main)
    _, b = make_user(admin, app_main)
    ch = add_channel(a, "Shared", f"shared-{owner['name']}")
    assert b.get(f"/api/status?channel={ch['id']}").status_code == 403
    cfg = a.get("/api/config").json()
    next(x for x in cfg["channels"] if x["id"] == ch["id"])["public"] = True
    assert a.put("/api/config", json=cfg).status_code == 200

    st = b.get(f"/api/status?channel={ch['id']}")
    assert st.status_code == 200
    me = next(x for x in st.json()["channels"] if x["id"] == ch["id"])
    assert me["can_edit"] is False and me["owner_name"] == owner["name"]
    assert b.get(f"/api/week?channel={ch['id']}").status_code == 200
    assert quiet(b, ch["id"]).status_code == 403
    assert b.post("/api/station", json={"channel": ch["id"], "id": "x"}).status_code == 403
    # Changing it through the config is ignored.
    cfg = b.get("/api/config").json()
    seen = next(x for x in cfg["channels"] if x["id"] == ch["id"])
    seen["name"], seen["stations"] = "Taken over", []
    assert b.put("/api/config", json=cfg).status_code == 200
    kept = next(x for x in admin.get("/api/config").json()["channels"] if x["id"] == ch["id"])
    assert kept["name"] == "Shared" and kept["stations"]
    # The admin can edit anything.
    assert quiet(admin, ch["id"]).status_code == 200


def test_user_media_is_kept_to_their_folder(admin, app_main):
    u, c = make_user(admin, app_main)
    r = c.post("/api/media", files={"file": ("jingle.mp3", io.BytesIO(b"x" * 2000), "audio/mpeg")})
    assert r.status_code == 200 and r.json()["path"] == f"users/{u['name']}/jingle.mp3"
    admin.post("/api/media", files={"file": ("admin-only.mp3", io.BytesIO(b"x" * 2000), "audio/mpeg")})
    paths = [f["path"] for f in c.get("/api/media").json()["files"]]
    assert paths == [f"users/{u['name']}/jingle.mp3"]
    assert c.delete("/api/media/admin-only.mp3").status_code == 404
    # A spot may only play the user's own files.
    cfg = c.get("/api/config").json()
    cfg["channels"].append({"name": "Spots", "mount": f"spots-{u['name']}",
                            "spots": [{"name": "Ad", "files": ["admin-only.mp3"]}]})
    assert c.put("/api/config", json=cfg).status_code == 422
    cfg["channels"][-1]["spots"][0]["files"] = [f"users/{u['name']}/jingle.mp3"]
    assert c.put("/api/config", json=cfg).status_code == 200


def test_users_have_their_own_subscriptions(admin, app_main):
    u, c = make_user(admin, app_main)
    _, other = make_user(admin, app_main)
    admin_cfg = admin.get("/api/config").json()
    admin_cfg["video"].append({"id": f"adm{u['id']}", "name": "Admin's", "channel_id": "UCadmin", "enabled": False})
    assert admin.put("/api/config", json=admin_cfg).status_code == 200
    # A new user sees none of the admin's subscriptions.
    assert c.get("/api/config").json()["video"] == []
    cfg = c.get("/api/config").json()
    cfg["podcasts"].append({"id": f"pod{u['id']}", "name": "Mine", "url": "http://feed.example/rss", "enabled": False})
    cfg["channels"].append({"name": "Subs", "mount": f"subs-{u['name']}", "all_sources": False,
                            "sources": [f"pod{u['id']}", f"adm{u['id']}"]})
    assert c.put("/api/config", json=cfg).status_code == 200
    full = app_main.store.config
    mine = next(ch for ch in full.channels if ch.mount == f"subs-{u['name']}")
    assert mine.sources == [f"pod{u['id']}"]  # someone else's source can't be ticked
    pod = full.source("podcasts", f"pod{u['id']}")
    adm = full.source("video", f"adm{u['id']}")
    assert pod.owner == u["id"] and mine.wants(pod) and not mine.wants(adm)
    # A source added later is only cut in where it is ticked.
    assert not full.channel("kid").wants(adm) and not full.channel("kid").wants(pod)
    # Other users don't see it, and can't change it.
    assert other.get("/api/config").json()["podcasts"] == []
    cfg = other.get("/api/config").json()
    cfg["podcasts"].append({"id": f"pod{u['id']}", "name": "Renamed", "url": "http://x.example/rss"})
    other.put("/api/config", json=cfg)
    assert app_main.store.config.source("podcasts", f"pod{u['id']}").name == "Mine"


def test_user_device_acts_as_user(admin, app_main, anon):
    u, c = make_user(admin, app_main)
    ch = add_channel(c, "Phone", f"phone-{u['name']}")
    p = c.post("/api/devices/pair", json={"name": "Phone", "role": "admin"}).json()
    d = anon.post("/api/pair", json={"code": p["code"]}).json()
    assert d["device"]["role"] == "member" and d["device"]["user_id"] == u["id"]
    auth = {"Authorization": f"Bearer {d['token']}"}
    ids = [x["id"] for x in anon.get("/api/me", headers=auth).json()["channels"]]
    assert ch["id"] in ids and "kid" not in ids
    assert quiet(anon, ch["id"], headers=auth).status_code == 200
    assert quiet(anon, "kid", headers=auth).status_code == 403
    # Users see only their own devices.
    assert [x["id"] for x in c.get("/api/devices").json()["devices"]] == [d["device"]["id"]]
    assert all(x["user_id"] == "" or x["user_id"] for x in admin.get("/api/devices").json()["devices"])


def test_remove_user_hands_channels_to_admin(admin, app_main, anon):
    u, c = make_user(admin, app_main)
    ch = add_channel(c, "Gone", f"gone-{u['name']}", public=True)
    p = c.post("/api/devices/pair", json={"name": "Phone"}).json()
    d = anon.post("/api/pair", json={"code": p["code"]}).json()
    assert admin.delete(f"/api/users/{u['id']}").status_code == 200
    kept = next(x for x in admin.get("/api/config").json()["channels"] if x["id"] == ch["id"])
    assert kept["owner"] == "" and kept["public"] is False
    assert c.get("/api/status").status_code == 401
    assert anon.get("/api/me", headers={"Authorization": f"Bearer {d['token']}"}).status_code == 401


def test_password_change(admin, app_main):
    u, c = make_user(admin, app_main)
    pw = "pw-" + u["name"] + "-long"
    assert c.post("/api/me/password", json={"old": "wrong", "new": "another-pw"}).status_code == 403
    assert c.post("/api/me/password", json={"old": pw, "new": "another-pw"}).status_code == 200
    assert c.get("/api/me").json()["user"]["name"] == u["name"]  # still logged in here
    fresh = TestClient(app_main.app)
    assert fresh.post("/api/login", json={"username": u["name"], "password": pw}).status_code == 401
    assert fresh.post("/api/login", json={"username": u["name"], "password": "another-pw"}).status_code == 200


def test_user_reaches_member_features_on_own_channel(admin, app_main):
    u, c = make_user(admin, app_main)
    ch = add_channel(c, "Saved", f"saved-{u['name']}")
    assert c.get(f"/api/saved?channel={ch['id']}").status_code == 200
    assert c.get(f"/api/week/numbers?channel={ch['id']}").status_code == 200
    assert c.get("/api/saved?channel=kid").status_code == 403


def test_every_source_becomes_ticks(app_main):
    from app.config import Config

    cfg = Config.model_validate({
        "video": [{"id": "a1", "name": "A", "channel_id": "UCa"}, {"id": "u1", "name": "U", "channel_id": "UCu", "owner": "x"}],
        "podcasts": [{"id": "p1", "name": "P", "url": "http://p.example/rss"}],
        "channels": [{"id": "c", "name": "C", "all_sources": True}, {"id": "d", "name": "D", "owner": "x"}],
    })
    c, d = cfg.channels
    assert not c.all_sources and c.sources == ["a1", "p1"]  # the owner's sources only
    assert not d.all_sources and d.sources == ["u1"]
