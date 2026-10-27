"""YAML-backed configuration.

The whole config lives in one YAML file (by default /data/config.yaml) so it
can be edited by hand, versioned or backed up. The web UI reads and writes the
same file through these models.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, available_timezones

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/data/config.yaml"))

STREAM_FORMATS = ["mp3", "aac", "opus"]
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def new_id() -> str:
    return secrets.token_hex(4)


def _check_time(v: str) -> str:
    v = v.strip()
    if re.match(r"^\d:", v):
        v = "0" + v
    if not _TIME_RE.match(v):
        raise ValueError("time must look like HH:MM")
    return v


def _check_days(v: list[str]) -> list[str]:
    v = [d.lower()[:3] for d in v]
    bad = [d for d in v if d not in DAYS]
    if bad:
        raise ValueError(f"unknown day(s): {', '.join(bad)}")
    return [d for d in DAYS if d in v]


class BaseSource(BaseModel):
    """What plays under the cut-ins: a radio stream, or music from Navidrome.

    kind "stream" plays `url`. kind "navidrome" plays the Navidrome playlist
    named in `playlist` (its name or id), or random songs from the whole
    library when `playlist` is empty.
    """

    id: str = Field(default_factory=new_id)
    name: str
    kind: Literal["stream", "navidrome"] = "stream"
    url: str = ""
    playlist: str = ""
    shuffle: bool = True
    image: str = ""  # logo to show while it plays (streams rarely send one)

    @model_validator(mode="after")
    def _url(self):
        if self.kind == "stream" and not self.url:
            raise ValueError("a stream needs a URL")
        return self

    @property
    def key(self) -> str:
        """Changes whenever a different thing should play."""
        return f"{self.kind}:{self.url}" if self.kind == "stream" else f"navidrome:{self.playlist}:{self.shuffle}"


class Station(BaseSource):
    pass


class Program(BaseSource):
    """A station that replaces the current one during a weekly time window.

    trigger "time" plays the whole window. trigger "title" watches the stream
    title (ICY metadata) of `title_url` (default: the program's own `url`)
    during the window: the program starts when a title matches `title_start`
    and ends when a title matches `title_end`, or when for `end_after_minutes`
    no title matched `title_start` or `title_keep` (e.g. news in between).
    The window's end always ends it. Patterns are case-insensitive regexes.
    """

    days: list[str] = Field(default_factory=lambda: list(DAYS))
    start: str = "07:00"
    end: str = "09:00"
    enabled: bool = True
    trigger: Literal["time", "title"] = "time"
    title_url: str = ""
    title_start: str = ""
    title_keep: str = ""
    title_end: str = ""
    end_after_minutes: float = 3
    cutins_interrupt: bool = False  # new uploads and episodes may play during it
    keep_day: bool = False  # record it (in RAM if downloads go there) for 24 h, to listen back in the apps

    _days = field_validator("days")(classmethod(lambda cls, v: _check_days(v)))
    _time = field_validator("start", "end")(classmethod(lambda cls, v: _check_time(v)))

    @field_validator("title_start", "title_keep", "title_end")
    @classmethod
    def _regex(cls, v: str) -> str:
        v = v.strip()
        if v:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"bad title pattern {v!r}: {e}") from None
        return v

    @field_validator("end_after_minutes")
    @classmethod
    def _grace(cls, v: float) -> float:
        return max(0.0, v)

    @model_validator(mode="after")
    def _title(self):
        if self.trigger == "title":
            if not self.title_start:
                raise ValueError("a title-triggered program needs a start pattern")
            if not self.watch_url:
                raise ValueError("a title-triggered program needs a stream to watch")
        return self

    @property
    def watch_url(self) -> str:
        """The stream whose titles start and end a title-triggered program."""
        return self.title_url or (self.url if self.kind == "stream" else "")


class Filters(BaseModel):
    min_minutes: float = 0
    max_minutes: float = 90
    include: str = ""
    exclude: str = ""

    @field_validator("include", "exclude")
    @classmethod
    def _regex(cls, v: str) -> str:
        if v:
            re.compile(v)
        return v


class VideoChannel(Filters):
    id: str = Field(default_factory=new_id)
    name: str
    channel_id: str
    enabled: bool = True
    owner: str = ""  # id of the user who added it; "" = the admin
    min_minutes: float = 1  # skips very short videos by default


class Podcast(Filters):
    id: str = Field(default_factory=new_id)
    name: str
    url: str
    enabled: bool = True
    owner: str = ""


class Spot(BaseModel):
    """Own content or a news bulletin that cuts in on a timer.

    content "media": one of `files` (paths in the media folder; a folder name
    ending in "/" means every file in it), in turn or at random.
    content "feed": the newest episode of the podcast feed `feed_url`.

    trigger "times": at each of `times`. "every": every `every_minutes`,
    counted from midnight plus `offset_minutes` (60/0 = on the hour).
    "songs": after every `every_songs` songs on the station.
    All triggers only fire on `days` and between `active_from` and `active_to`
    (equal times = all day).
    """

    id: str = Field(default_factory=new_id)
    name: str
    enabled: bool = True
    content: Literal["media", "feed"] = "media"
    files: list[str] = Field(default_factory=list)
    order: Literal["rotate", "random"] = "rotate"
    feed_url: str = ""
    only_new: bool = True
    trigger: Literal["times", "every", "songs"] = "every"
    times: list[str] = Field(default_factory=list)
    every_minutes: int = 60
    offset_minutes: int = 0
    every_songs: int = 4
    days: list[str] = Field(default_factory=lambda: list(DAYS))
    active_from: str = "00:00"
    active_to: str = "00:00"
    between_songs: bool = False
    during_programs: bool = True
    image: str = ""  # picture while it plays; a feed's own cover is used when empty

    _days = field_validator("days")(classmethod(lambda cls, v: _check_days(v)))
    _time = field_validator("active_from", "active_to")(classmethod(lambda cls, v: _check_time(v)))

    @field_validator("times")
    @classmethod
    def _times(cls, v: list[str]) -> list[str]:
        return sorted({_check_time(t.strip()) for t in v if t.strip()})

    @field_validator("every_minutes")
    @classmethod
    def _every(cls, v: int) -> int:
        return min(1440, max(1, v))

    @field_validator("every_songs")
    @classmethod
    def _songs(cls, v: int) -> int:
        return max(1, v)

    @model_validator(mode="after")
    def _content(self):
        if self.content == "feed" and not self.feed_url:
            raise ValueError("a news spot needs a feed URL")
        return self


SECRET_MASK = "********"


class Settings(BaseModel):
    station_name: str = "Pirate Radio"
    timezone: str = os.environ.get("TZ", "UTC") or "UTC"
    invidious_url: str = os.environ.get("INVIDIOUS_URL", "http://invidious:3000")
    poll_minutes: float = 10
    max_age_hours: float = 48
    ytdlp_fallback: bool = False
    # Audio format fetched from Invidious: "small" = Opus ~70 kbps (itag 250),
    # about half the size of "aac" = AAC 128 kbps (itag 140), so it downloads faster.
    video_audio: Literal["small", "aac"] = "small"
    # Download clips to the RAM disk instead of the data volume (saves SSD writes).
    cache_in_ram: bool = True
    # Keep voice messages for a day after they played, so the apps can play them again
    # from the history. Other clips are fetched again from their source when asked.
    replay_keep: bool = True
    streams: list[str] = Field(default_factory=lambda: ["mp3"])
    # Navidrome (or any Subsonic server). The password may also come from
    # NAVIDROME_PASSWORD in .env, which keeps it out of this file.
    navidrome_url: str = os.environ.get("NAVIDROME_URL", "")
    navidrome_user: str = os.environ.get("NAVIDROME_USER", "")
    navidrome_password: str = ""
    navidrome_bitrate: int = 192  # kbps Navidrome transcodes to MP3; 0 = original file

    @property
    def navidrome_secret(self) -> str:
        return self.navidrome_password or os.environ.get("NAVIDROME_PASSWORD", "")

    @field_validator("streams")
    @classmethod
    def _streams(cls, v: list[str]) -> list[str]:
        v = [f for f in STREAM_FORMATS if f in {x.lower() for x in v}]
        if not v:
            raise ValueError("enable at least one stream format")
        return v

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        if v not in available_timezones():
            raise ValueError(f"unknown time zone {v!r}")
        return v

    @field_validator("poll_minutes")
    @classmethod
    def _poll(cls, v: float) -> float:
        return max(1.0, v)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


_MOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_RESERVED_MOUNTS = {"admin", "status", "status-json", "server_version", "style"}


class Channel(BaseModel):
    """One radio stream of its own, e.g. one per family member.

    It plays at /<mount>.mp3 (and .aac/.opus), with its own stations,
    programs and spots. It cuts in new items from every video channel and
    podcast its owner added (the admin's or the user's own), or only from those
    listed in `sources` when `all_sources` is off.

    `owner` is the id of the user it belongs to ("" = the admin). A `public`
    channel shows up for every user, who can listen but not change it.
    """

    id: str = Field(default_factory=new_id)
    name: str
    mount: str = ""
    stations: list[Station] = Field(default_factory=list)
    current_station: str | None = None
    programs: list[Program] = Field(default_factory=list)
    spots: list[Spot] = Field(default_factory=list)
    all_sources: bool = True
    sources: list[str] = Field(default_factory=list)
    owner: str = ""
    public: bool = False

    @model_validator(mode="after")
    def _check(self) -> "Channel":
        if not self.mount:
            self.mount = re.sub(r"[^a-z0-9_-]+", "-", self.name.lower()).strip("-")[:32] or self.id
        if not _MOUNT_RE.match(self.mount) or self.mount in _RESERVED_MOUNTS:
            raise ValueError(f"stream name {self.mount!r} must be lowercase letters, digits, - or _")
        ids = {s.id for s in self.stations}
        if self.current_station not in ids:
            self.current_station = self.stations[0].id if self.stations else None
        return self

    def station(self) -> Station | None:
        return next((s for s in self.stations if s.id == self.current_station), None)

    def wants(self, src: "VideoChannel | Podcast") -> bool:
        return (self.all_sources and src.owner == self.owner) or src.id in self.sources


def _migrate(data: dict) -> dict:
    """Configs from before channels kept one channel's lists at the top level."""
    old = {k: data.pop(k) for k in ("stations", "current_station", "programs", "spots") if k in data}
    if not data.get("channels"):
        name = (data.get("settings") or {}).get("station_name") or "Pirate Radio"
        data["channels"] = [{"id": "main", "name": name, "mount": "radio", **old}]
    return data


class Config(BaseModel):
    settings: Settings = Field(default_factory=Settings)
    channels: list[Channel] = Field(default_factory=list)
    video: list[VideoChannel] = Field(default_factory=list)
    podcasts: list[Podcast] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _old(cls, data):
        return _migrate(dict(data)) if isinstance(data, dict) else data

    @model_validator(mode="after")
    def _unique(self) -> "Config":
        for attr in ("id", "mount"):
            seen = [getattr(c, attr) for c in self.channels]
            dup = {x for x in seen if seen.count(x) > 1}
            if dup:
                raise ValueError(f"two channels use the same {'stream name' if attr == 'mount' else 'id'}: {', '.join(dup)}")
        return self

    def channel(self, cid: str | None) -> Channel | None:
        """The channel with this id or stream name; None picks the first one."""
        if not cid:
            return self.channels[0] if self.channels else None
        return next((c for c in self.channels if c.id == cid), None) or next(
            (c for c in self.channels if c.mount == cid), None
        )

    def spot(self, sid: str) -> tuple[Channel, Spot] | None:
        for c in self.channels:
            for s in c.spots:
                if s.id == sid:
                    return c, s
        return None

    def source(self, kind: str, sid: str) -> VideoChannel | Podcast | None:
        items = self.video if kind == "video" else self.podcasts
        return next((s for s in items if s.id == sid), None)

    def masked(self) -> dict:
        """model_dump() without the Navidrome password (shown as a mask)."""
        data = self.model_dump()
        if data["settings"]["navidrome_password"]:
            data["settings"]["navidrome_password"] = SECRET_MASK
        return data


_FIRST = ("id", "name", "kind", "mount", "channel_id", "url", "enabled")


def _ordered(entries: list[dict]) -> list[dict]:
    return [{**{k: e[k] for k in _FIRST if k in e}, **{k: v for k, v in e.items() if k not in _FIRST}} for e in entries]


def dump(cfg: Config, mask: bool = False) -> dict:
    """Plain dict with the most useful keys first in every list entry."""
    data = cfg.masked() if mask else cfg.model_dump()
    for key in ("video", "podcasts"):
        data[key] = _ordered(data[key])
    for ch in data["channels"]:
        for key in ("stations", "programs", "spots"):
            ch[key] = _ordered(ch[key])
    data["channels"] = _ordered(data["channels"])
    return data


class ConfigStore:
    """Loads and atomically saves the YAML config; thread-safe."""

    def __init__(self, path: Path = CONFIG_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._mtime = 0.0
        self.config = self._load()

    def _load(self) -> Config:
        if self.path.exists():
            self._mtime = self.path.stat().st_mtime
            data = yaml.safe_load(self.path.read_text()) or {}
            cfg = Config.model_validate(data)
            if "channels" not in data:  # upgraded from before channels: save the new layout
                self._write(cfg)
            return cfg
        cfg = Config()
        default_url = os.environ.get("DEFAULT_STATION_URL")
        if default_url:
            cfg.channels[0].stations.append(Station(name="Default station", url=default_url))
            cfg = Config.model_validate(cfg.model_dump())
        self._write(cfg)
        return cfg

    def _write(self, cfg: Config) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        header = "# Pirate Radio config. Edited by the web UI; hand edits are fine too.\n"
        tmp.write_text(header + yaml.safe_dump(dump(cfg), sort_keys=False, allow_unicode=True))
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime

    def save(self, cfg: Config) -> Config:
        cfg = Config.model_validate(cfg.model_dump())
        with self._lock:
            self._write(cfg)
            self.config = cfg
        return cfg

    def reload_if_changed(self) -> bool:
        """Pick up hand edits of the YAML file. Returns True if reloaded."""
        try:
            m = self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        if m == self._mtime:
            return False
        self._mtime = m
        try:
            self.config = self._load()
            return True
        except Exception:  # keep the last good config on a bad hand edit
            return False
