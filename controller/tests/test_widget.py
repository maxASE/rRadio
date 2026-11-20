"""The dashboard widget: healthy at a glance, private unless asked."""

PRIVATE = ("title", "on_air", "mode", "last_cutin", "next_program")


def set_show(admin, on):
    cfg = admin.get("/api/config").json()
    cfg["settings"]["widget_show_playing"] = on
    assert admin.put("/api/config", json=cfg).status_code == 200


def test_widget_hides_what_is_playing_by_default(admin):
    w = admin.get("/api/widget").json()
    for k in ("status", "queue", "ram", "ram_percent", "ram_low", "listeners"):
        assert k in w
    assert not any(k in w for k in PRIVATE)
    assert w["status"] in ("On air", "Offline") or w["status"].startswith("Silent")


def test_widget_adds_up_all_channels(admin, app_main):
    from app.sources import Item
    engine = app_main.engine
    chans = engine.config.channels
    assert len(chans) >= 2
    w = admin.get("/api/widget").json()
    assert w["channels"] == len(chans)
    items = [Item(kind="manual", source_id="t", source_name="T", item_id=f"w{n}", title=f"x{n}", media=f"http://x/{n}", channel=c.id)
             for n, c in enumerate(chans[:2])]
    engine.items.extend(items)
    try:
        assert admin.get("/api/widget").json()["queue"] == w["queue"] + 2
        assert admin.get(f"/api/widget?channel={chans[1].mount}").json()["queue"] >= 1
        assert admin.get(f"/api/widget?channel={chans[1].mount}").json()["channels"] == 1
    finally:
        for i in items:
            engine.items.remove(i)


def test_widget_ram_percent(admin):
    w = admin.get("/api/widget").json()
    assert isinstance(w["ram_percent"], int) and w["ram"].startswith(f"{w['ram_percent']}%")


def test_widget_shows_playing_when_turned_on(admin):
    set_show(admin, True)
    try:
        w = admin.get("/api/widget").json()
        assert all(k in w for k in PRIVATE)
    finally:
        set_show(admin, False)
