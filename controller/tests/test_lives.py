"""Live videos wait until Invidious can play them, then cut in."""

import asyncio

from app.sources import Invidious, live_state


def add_video(admin, name="Live Channel"):
    """A video source that cuts into every channel."""
    cfg = admin.get("/api/config").json()
    cfg["video"].append({"name": name, "channel_id": "UC" + "x" * 22})
    cfg = admin.put("/api/config", json=cfg).json()
    src = next(s for s in cfg["video"] if s["name"] == name)
    for ch in cfg["channels"]:
        ch["sources"].append(src["id"])
    r = admin.put("/api/config", json=cfg)
    assert r.status_code == 200, r.text
    return src


class FakeInvidious(Invidious):
    """Invidious answering from `pages` (path -> JSON) instead of over the network."""

    def __init__(self, pages):
        super().__init__("http://inv.test")
        self.pages = pages

    async def _get(self, path, **params):
        return self.pages[path]

    async def channel_avatar(self, ucid):
        return ""


def video(vid, **kw):
    return {"type": "video", "videoId": vid, "title": f"Show {vid}", "lengthSeconds": 600, "published": 1, **kw}


def test_live_state():
    assert live_state(video("a")) == ""
    assert live_state(video("a", liveNow=True)) == "live"
    assert live_state(video("a", isUpcoming=True, lengthSeconds=0)) == "upcoming"
    assert live_state(video("a", lengthSeconds=0)) == "processing"


def test_latest_reads_the_live_tab(admin, app_main):
    sid = add_video(admin, "Tabs")["id"]
    src = app_main.engine.config.source("video", sid)
    ucid = src.channel_id
    inv = FakeInvidious({
        f"/api/v1/channels/{ucid}/videos": {"videos": [video("upload00001")]},
        f"/api/v1/channels/{ucid}/streams": {"videos": [video("streamnow01", liveNow=True, lengthSeconds=0),
                                                         video("upload00001")]},
    })
    items = asyncio.run(inv.latest(src))
    assert [(i.media, i.live) for i in items] == [("upload00001", ""), ("streamnow01", "live")]
    src.lives = False
    try:
        assert [i.media for i in asyncio.run(inv.latest(src))] == ["upload00001"]
    finally:
        src.lives = True


def test_live_waits_then_cuts_in(admin, app_main, monkeypatch):
    eng = app_main.engine
    sid = add_video(admin, "Waits")["id"]
    src = app_main.engine.config.source("video", sid)
    ucid, vid = src.channel_id, "liveshow001"
    pages = {
        f"/api/v1/channels/{ucid}/videos": {"videos": []},
        f"/api/v1/channels/{ucid}/streams": {"videos": []},
    }
    inv = FakeInvidious(pages)
    monkeypatch.setattr(eng, "invidious", lambda: inv)
    st = {}
    asyncio.run(eng._check("video", src, st))  # first look: nothing old is played

    # A live starts: it waits instead of being queued.
    pages[f"/api/v1/channels/{ucid}/streams"] = {"videos": [video(vid, liveNow=True, lengthSeconds=0)]}
    asyncio.run(eng._check("video", src, st))
    key = f"{src.id}:{vid}"
    assert not any(i.key == key for i in eng.items)
    w = next(w for w in app_main.engine.store.waiting() if w["key"] == key)
    assert w["reason"] == "live"
    status = admin.get("/api/status").json()
    assert any(x["key"] == key and x["reason"] == "live" for x in status["waiting"])
    waiting_entries = [h for h in eng.store.history(50) if h["title"] == f"Show {vid}" and h["status"] == "waiting"]
    assert waiting_entries

    # Over, but still being processed: Invidious errors, so it keeps waiting with a gap.
    pages[f"/api/v1/videos/{vid}"] = video(vid, lengthSeconds=0)
    app_main.engine.store.wait_put(key, w["item"], w["since"], 0, 0, "live")
    asyncio.run(eng._check_waiting())
    w = next(w for w in app_main.engine.store.waiting() if w["key"] == key)
    assert w["reason"] == "processing" and w["tries"] == 1 and w["next_check"] > w["since"]

    # Invidious can play it now: it goes on every channel's queue with its real length.
    pages[f"/api/v1/videos/{vid}"] = video(vid, lengthSeconds=1800, adaptiveFormats=[{"type": "audio/mp4", "itag": 140}])
    app_main.engine.store.wait_put(key, w["item"], w["since"], 0, w["tries"], w["reason"])
    asyncio.run(eng._check_waiting())
    assert not any(w["key"] == key for w in app_main.engine.store.waiting())
    queued = [i for i in eng.items if i.key == key]
    assert {i.channel for i in queued} == {c.id for c in app_main.engine.config.channels if c.wants(src)}
    assert all(i.duration == 1800 and not i.live for i in queued)
    # Its "waiting" line goes: once it has played the list shows it once, not twice.
    assert not any(h["title"] == f"Show {vid}" and h["status"] == "waiting" for h in eng.store.history(50))
    for i in queued:
        eng._finish(i, "played")
    assert [h["status"] for h in eng.store.history(50) if h["title"] == f"Show {vid}"] == ["played"] * len(queued)


def test_gives_up_after_the_wait_limit(admin, app_main, monkeypatch):
    eng = app_main.engine
    sid = add_video(admin, "Gives up")["id"]
    src = app_main.engine.config.source("video", sid)
    vid = "neverready1"
    inv = FakeInvidious({f"/api/v1/videos/{vid}": video(vid, lengthSeconds=0)})
    monkeypatch.setattr(eng, "invidious", lambda: inv)
    key = f"{src.id}:{vid}"
    item = {"kind": "video", "source_id": src.id, "source_name": src.name, "item_id": vid, "title": "Old live",
            "media": vid, "duration": None, "published": None, "art": "", "live": "processing", "first": 1}
    app_main.engine.store.wait_put(key, item, 1, 0, 5, "processing")  # waiting since 1970
    asyncio.run(eng._check_waiting())
    assert not any(w["key"] == key for w in app_main.engine.store.waiting())
    assert any(h["title"] == "Old live" and h["status"] == "failed" for h in app_main.engine.store.history(50))


def test_forget_waiting(admin, app_main):
    src = add_video(admin, "Forget")
    key = f"{src['id']}:forgetme001"
    item = {"kind": "video", "source_id": src["id"], "source_name": "Forget", "item_id": "forgetme001",
            "title": "Forget me", "media": "forgetme001", "duration": None, "published": None, "art": "", "live": "live"}
    app_main.engine.store.wait_put(key, item, 0, 9e12, 0, "live")
    assert admin.delete(f"/api/waiting/{key}").status_code == 200
    assert admin.delete(f"/api/waiting/{key}").status_code == 404


def test_failed_video_download_waits_and_retries(admin, app_main, monkeypatch):
    from app import engine as engine_mod
    from app.sources import Item

    eng = app_main.engine
    sid = add_video(admin, "Retries")["id"]
    src = app_main.engine.config.source("video", sid)

    async def broken(*a, **kw):
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(engine_mod, "download", broken)
    it = Item(kind="video", source_id=src.id, source_name=src.name, item_id="justended1", title="Just ended",
              media="justended1")
    eng.enqueue_all(it, src)
    asyncio.run(eng._download(it.key))
    assert not any(i.key == it.key for i in eng.items)
    w = next(w for w in eng.store.waiting() if w["key"] == it.key)
    assert w["reason"].startswith("download failed") and w["next_check"] > w["since"]
    eng.drop_waiting(it.key)
