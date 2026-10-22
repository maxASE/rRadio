"""Log-in page, sessions and Basic auth."""

from conftest import BASIC


def test_health_is_open_and_versioned(anon):
    r = anon.get("/healthz")
    assert r.status_code == 200
    assert r.json()["api"] >= 1 and r.json()["version"]


def test_pages_redirect_to_login(anon):
    r = anon.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert anon.get("/login").status_code == 200


def test_api_says_401_without_popup(anon):
    r = anon.get("/api/status")
    assert r.status_code == 401
    assert "www-authenticate" not in r.headers


def test_login_logout(anon):
    assert anon.post("/api/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    r = anon.post("/api/login", json={"username": "admin", "password": "secret-pw"})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()
    assert anon.get("/api/status").status_code == 200
    assert anon.get("/login").status_code == 303  # already logged in
    assert anon.post("/api/logout").status_code == 200
    assert anon.get("/api/status").status_code == 401


def test_basic_auth_still_works(anon):
    assert anon.get("/api/widget", headers=BASIC).status_code == 200


def test_open_paths(anon):
    assert anon.get("/listen.m3u").status_code == 200
    assert anon.get("/static/style.css").status_code == 200
