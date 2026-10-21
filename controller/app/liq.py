"""Client for the Liquidsoap HTTP API defined in liquidsoap/radio.liq."""

from __future__ import annotations

import os

import httpx

LIQ_URL = os.environ.get("LIQUIDSOAP_URL", "http://liquidsoap:8005")


def _quote(v: str) -> str:
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def _uri(path: str, title: str, artist: str) -> str:
    return f"annotate:title={_quote(title)},artist={_quote(artist)}:{path}"


class Liquidsoap:
    def __init__(self, base: str = LIQ_URL):
        self._c = httpx.AsyncClient(base_url=base, timeout=5)

    async def _post(self, path: str, ch: str | None = None, body: str = "", **params) -> dict:
        if ch is not None:
            params["ch"] = ch
        r = await self._c.post(path, content=body, params=params)
        r.raise_for_status()
        return r.json()

    async def status(self) -> dict:
        """{"streams": [...], "channels": [{"id": ..., "mode": ..., ...}, ...]}"""
        r = await self._c.get("/status")
        r.raise_for_status()
        return r.json()

    async def push(self, ch: str, path: str, title: str, artist: str) -> int:
        return int((await self._post("/push", ch, _uri(path, title, artist)))["rid"])

    async def push_music(self, ch: str, path: str, title: str, artist: str) -> int:
        return int((await self._post("/music/push", ch, _uri(path, title, artist)))["rid"])

    async def flush_music(self, ch: str) -> None:
        await self._post("/music/flush", ch)

    async def skip(self, ch: str) -> None:
        await self._post("/skip", ch)

    async def skip_music(self, ch: str) -> None:
        await self._post("/music/skip", ch)

    async def set_streams(self, formats: list[str]) -> None:
        await self._post("/streams", body=",".join(formats))

    async def set_base(self, ch: str, kind: str, url: str, name: str) -> None:
        await self._post("/base", ch, url, kind=kind, name=name)

    async def restart(self) -> None:
        await self._post("/restart")
