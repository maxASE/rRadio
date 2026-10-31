"""What users and visitors who are not logged in must not get to: other people's
files, other channels (by picking a clashing id), and admin rights on open pages."""

import io
import re

from app import media

from test_users import make_user

# Paths that stay open without a login (players, dashboards, the log-in page, pairing).
OPEN = ("/listen.", "/healthz", "/static", "/login", "/api/login", "/api/art/now/", "/api/pair", "/api/hello")


def upload(client, name, folder=""):
    return client.post("/api/media", files={"file": (name, io.BytesIO(b"x" * 2000), "audio/mpeg")}, data={"folder": folder})


def test_users_cannot_leave_their_folder(admin, app_main):
    assert upload(admin, "admin-jingle.mp3", "jingles").status_code == 200
    other, other_c = make_user(admin, app_main)
    assert upload(other_c, "other.mp3").status_code == 200
    u, c = make_user(admin, app_main)
    home = f"users/{u['name']}"

    # Deleting someone else's file, with the dots escaped so the client sends them as they are.
    for path in (f"{home}/%2E%2E/{other['name']}/other.mp3", f"{home}/%2E%2E/%2E%2E/jingles/admin-jingle.mp3"):
        assert c.delete(f"/api/media/{path}").status_code == 404
    assert (media.MEDIA_DIR / f"users/{other['name']}/other.mp3").is_file()
    assert (media.MEDIA_DIR / "jingles/admin-jingle.mp3").is_file()

    # Uploading into the inbox, which would cut in on the first channel.
    assert upload(c, "evil.mp3", f"{home}/../../inbox").status_code == 400
    assert not (media.MEDIA_DIR / "inbox/evil.mp3").exists()
    # Playing someone else's file.
    r = c.post("/api/media/play", json={"path": f"{home}/../{other['name']}/other.mp3"})
    assert r.status_code == 404

    # A spot that plays the admin's files.
    cfg = c.get("/api/config").json()
    cfg["channels"].append({"name": "Spy", "mount": f"spy-{u['name']}", "stations": [{"name": "S", "url": "http://s.example/x"}],
                            "spots": [{"name": "x", "files": [f"{home}/../../jingles/"]}]})
    assert c.put("/api/config", json=cfg).status_code == 422

    # Their own folder still works.
    assert upload(c, "mine.mp3", "sub").json()["path"] == f"{home}/sub/mine.mp3"
    assert c.delete(f"/api/media/{home}/sub/mine.mp3").status_code == 200


def test_a_channel_id_cannot_take_another_channels_place(admin, app_main):
    _, c = make_user(admin, app_main)
    cfg = c.get("/api/config").json()
    main_mount = admin.get("/api/config").json()["channels"][0]["mount"]
    cfg["channels"].append({"id": main_mount, "name": "Shadow", "mount": "shadow-x",
                            "stations": [{"name": "S", "url": "http://s.example/x"}]})
    assert c.put("/api/config", json=cfg).status_code == 422
    assert admin.get("/api/config").json()["channels"][0]["mount"] == main_mount


def test_open_pages_get_no_admin_rights(admin, app_main, anon):
    # Playlists stay open for players, also for channels that aren't public.
    assert anon.get("/listen.m3u?channel=kid").status_code == 200
    assert anon.get("/listen.m3u?channel=nope").status_code == 404
    # Everything else needs a log-in.
    path_args = re.compile(r"\{[^}]+\}")
    for route in app_main.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api/") or path.startswith(OPEN) or (path == "/api/widget" and app_main.WIDGET_PUBLIC):
            continue
        url = path_args.sub("1", path)
        for method in route.methods - {"HEAD", "OPTIONS"}:
            r = anon.request(method, url)
            assert r.status_code == 401, f"{method} {path} answered {r.status_code} without a log-in"


def test_open_pages_run_as_nobody(app_main, anon, monkeypatch):
    seen = {}
    real = app_main.listen_channel

    def spy(cid):
        seen["access"] = app_main.acc()
        return real(cid)

    monkeypatch.setattr(app_main, "listen_channel", spy)
    anon.get("/listen.pls?channel=kid")
    assert "access" not in seen  # anonymous: the playlist looks the channel up by itself
    assert app_main.acc() is app_main.ADMIN  # outside a request nothing changed
    assert app_main.ANON.can_control(app_main.store.config.channels[0]) is False
    assert app_main.ANON.can_listen(app_main.store.config.channels[0]) is False
