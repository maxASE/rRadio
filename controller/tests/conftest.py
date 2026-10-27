"""Test setup: the controller runs against a temporary data folder with a UI
password, without starting the engine (no Liquidsoap or Icecast needed)."""

import base64
import os
import sys
import tempfile
from pathlib import Path

import pytest

DATA = Path(tempfile.mkdtemp(prefix="rradio-test-"))
(DATA / "ram").mkdir()
os.environ.update(
    CONFIG_PATH=str(DATA / "config.yaml"),
    STATE_DB=str(DATA / "state.db"),
    CHANNELS_FILE=str(DATA / "channels.json"),
    MEDIA_DIR=str(DATA / "media"),
    CACHE_DIR=str(DATA / "cache"),
    RAM_CACHE_DIR=str(DATA / "ram"),
    SAVED_DIR=str(DATA / "saved"),
    UI_USERNAME="admin",
    UI_PASSWORD="secret-pw",
)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(b"admin:secret-pw").decode()}


@pytest.fixture(scope="session")
def app_main():
    # A second channel, so per-channel limits can be tested.
    c = TestClient(main.app)
    cfg = c.get("/api/config", headers=BASIC).json()
    if not any(ch["id"] == "kid" for ch in cfg["channels"]):
        kid = dict(cfg["channels"][0], id="kid", name="Kid", mount="kid")
        cfg["channels"].append(kid)
        assert c.put("/api/config", headers=BASIC, json=cfg).status_code == 200
    return main


@pytest.fixture
def admin(app_main):
    return TestClient(app_main.app, headers=BASIC)


@pytest.fixture
def anon(app_main):
    return TestClient(app_main.app, follow_redirects=False)


@pytest.fixture
def ram_dir():
    return DATA / "ram"
