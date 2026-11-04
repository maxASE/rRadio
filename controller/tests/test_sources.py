"""Subsonic servers other than Navidrome, and the radio-browser.info station search."""

import asyncio

import httpx

from app import art, navidrome


def mock(monkeypatch, module, handler):
    def client(timeout: float = 30):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(module, "client", client)


def ok(**body):
    return httpx.Response(200, json={"subsonic-response": {"status": "ok", "version": "1.16.1", **body}})


def test_password_login_when_token_is_refused(monkeypatch):
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        if "t" in req.url.params:
            return httpx.Response(200, json={"subsonic-response": {"status": "failed", "error": {"code": 41, "message": "no tokens"}}})
        assert req.url.params["p"] == "enc:" + b"pw".hex()
        return ok(type="funkwhale", serverVersion="1.4")

    mock(monkeypatch, navidrome, handler)
    nd = navidrome.Navidrome("http://fw.example/api/subsonic/rest/", "max", "pw")
    assert nd.base == "http://fw.example/api/subsonic"
    assert asyncio.run(nd.ping()) == "funkwhale 1.4"
    # Remembered: the next call and stream URLs go straight to the password.
    asyncio.run(nd.ping())
    assert ["t" in p for p in seen] == [True, False, False]
    assert "p=enc:" in nd.stream_url("1", 0)


def test_wrong_password_still_fails(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"subsonic-response": {"status": "failed", "error": {"code": 40, "message": "Wrong username or password"}}})

    mock(monkeypatch, navidrome, handler)
    nd = navidrome.Navidrome("http://gonic.example", "max", "bad")
    try:
        asyncio.run(nd.ping())
        raise AssertionError("ping should fail")
    except RuntimeError as e:
        assert "Wrong username" in str(e)
    assert ("http://gonic.example", "max") not in navidrome._password_login


def test_single_items_and_numeric_ids(monkeypatch):
    # Older servers send one item as an object, and some use numbers as ids.
    def handler(req):
        if req.url.path.endswith("getPlaylists"):
            return ok(playlists={"playlist": {"id": 7, "name": "Morning", "songCount": 1}})
        return ok(playlist={"entry": {"id": 42, "title": "Song", "artist": "A", "coverArt": 9}})

    mock(monkeypatch, navidrome, handler)
    nd = navidrome.Navidrome("http://ampache.example", "max", "pw")
    songs = asyncio.run(nd.playlist_songs("morning"))
    assert [(s.id, s.title, s.cover) for s in songs] == [("42", "Song", "9")]


def test_station_search_merges_names_and_genres(monkeypatch):
    def station(uuid, name, votes):
        return {"stationuuid": uuid, "name": name, "url": "http://x/" + uuid, "url_resolved": "http://y/" + uuid,
                "favicon": "", "countrycode": "SE", "tags": "jazz,smooth", "codec": "MP3", "bitrate": 128, "votes": votes}

    def handler(req):
        p = req.url.params
        assert p["hidebroken"] == "true" and p["countrycode"] == "SE"
        if "name" in p:
            return httpx.Response(200, json=[station("a", "Jazz FM", 5), station("b", "Both", 1)])
        return httpx.Response(200, json=[station("b", "Both", 1), station("c", "Smooth", 50)])

    mock(monkeypatch, art, handler)
    found = asyncio.run(art.search_stations("jazz", "se"))
    assert [s["name"] for s in found] == ["Smooth", "Jazz FM", "Both"]
    assert found[0]["url"] == "http://y/c" and found[0]["tags"] == ["jazz", "smooth"]


def test_station_search_endpoint(admin, app_main, monkeypatch):
    async def search(q, country):
        return [{"uuid": "u", "name": q}]

    monkeypatch.setattr(app_main, "search_stations", search)
    assert admin.get("/api/stations/search?q=P3").json() == [{"uuid": "u", "name": "P3"}]
    assert admin.get("/api/stations/search").status_code == 400
    assert admin.post("/api/stations/picked", json={"uuid": "nope"}).status_code == 204


def test_pictures_are_fetched_once_and_kept_on_disk(monkeypatch, tmp_path):
    calls = []

    async def handler(req):
        calls.append(str(req.url))
        await asyncio.sleep(0.05)
        return httpx.Response(200, content=b"PNGDATA", headers={"content-type": "image/png"})

    mock(monkeypatch, art, handler)

    async def run():
        cache = art.ArtCache(disk=tmp_path)
        key = cache.path("https://inv.example/ggpht/avatar").rsplit("/", 1)[-1]  # also starts fetching ahead
        # Many clients at once (every tab, the lists, the web page): one fetch.
        got = await asyncio.gather(*(cache.get(key) for _ in range(5)))
        assert all(g == (b"PNGDATA", "image/png") for g in got)
        assert len(calls) == 1
        # After a restart the picture comes from disk, not from the remote server again.
        again = art.ArtCache(disk=tmp_path)
        assert await again.get(key) == (b"PNGDATA", "image/png")
        assert len(calls) == 1

    asyncio.run(run())


def test_a_picture_that_failed_once_comes_back_after_a_minute(monkeypatch, tmp_path):
    mock(monkeypatch, art, lambda req: httpx.Response(504))

    async def run():
        cache = art.ArtCache(disk=tmp_path)
        url = "https://inv.example/ggpht/slow"
        key = cache.path(url).rsplit("/", 1)[-1]
        assert await cache.get(key) is None
        assert cache.path(url) is None  # left out for a moment
        cache.failed_at[key] -= 61
        assert cache.path(url) == f"/api/art/{key}"  # one failure: back after a minute

    asyncio.run(run())
