"""The brain: polls sources, downloads new items and decides what is on air.

Every channel is decided on its own, with the same rules (agreed with the user):
* A new clip interrupts the radio right away; several clips play back to back.
* Scheduled programs and clips: whichever started (or arrived) first wins.
  If both happen in the same second, the winner is picked at random.
* Spots (own content, news bulletins) jump ahead of waiting clips, and by
  default also play during programs. They never cut a playing clip short.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import shutil
import time
import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

from .art import ArtCache
from .config import DAYS, BaseSource, Channel, Config, ConfigStore, Podcast, Program, Spot
from .liq import Liquidsoap
from .media import MEDIA_DIR, Inbox, ensure_dirs, expand
from .navidrome import Feeder, Navidrome, scrub
from .sources import (
    Invidious,
    Item,
    download,
    download_url,
    passes_filters,
    podcast_latest,
    keep_for_replay,
    prune_replays,
    purge_cache,
    REPLAY_KEEP,
    USER_AGENT,
    cache_dir,
    ram_usage,
    CACHE_DIR,
)
from .store import Store
from .titles import Watchers

log = logging.getLogger("engine")

ICECAST_STATUS_URL = os.environ.get("ICECAST_STATUS_URL", "http://icecast:8000/status-json.xsl")
CHANNELS_FILE = Path(os.environ.get("CHANNELS_FILE", "/data/channels.json"))
SAVED_DIR = Path(os.environ.get("SAVED_DIR", "/data/saved"))  # recordings saved for later, on disk
TICK = 1.0
WAIT_NOTE = {"live": "live now, plays once it is over", "upcoming": "live soon, plays once it is over",
             "processing": "waits until Invidious can play the recording"}
INIT_MARK = "__init__"
# Lives and failed video downloads wait on a list (kept in the state DB) until
# Invidious can play them: checked this often while a live is on, then with growing gaps.
WAIT_CHECK = 900
WAIT_GAP_MAX = 3600
LIVE_MAX = 2 * 86400  # still live after this long: a 24/7 stream, which never ends
UPCOMING_MAX = 14 * 86400
_WAIT_FIELDS = ("kind", "source_id", "source_name", "item_id", "title", "media", "duration", "published", "art", "live")
HOLD_MAX = 600  # a spot waits at most this long for a song change
RECORD_MIN_FREE = 64 << 20  # a kept program stops recording when less space than this is left
RESTART_EVERY = 30  # seconds between Liquidsoap restarts for channel changes


@dataclass
class DueProgram:
    program: Program
    start: datetime
    end: datetime


def program_window(p: Program, now: datetime) -> tuple[datetime, datetime] | None:
    """The window of `p` that contains `now`, if any (it may cross midnight)."""
    sh, sm = map(int, p.start.split(":"))
    eh, em = map(int, p.end.split(":"))
    for back in (0, 1):  # today's window, or yesterday's if it crosses midnight
        day = now - timedelta(days=back)
        if DAYS[day.weekday()] not in p.days:
            continue
        start = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = day.replace(hour=eh, minute=em, second=0, microsecond=0)
        if end <= start:
            end += timedelta(days=1)
        if start <= now < end:
            return start, end
    return None


def due_program(ch: Channel, now: datetime, shows: dict[str, datetime] | None = None) -> DueProgram | None:
    """The program on now; the earliest-started wins.

    A title-triggered program only counts once its show was seen to start;
    `shows` maps its id to that time."""
    shows = shows or {}
    found: list[DueProgram] = []
    for p in ch.programs:
        if not p.enabled or (p.kind == "stream" and not p.url):
            continue
        win = program_window(p, now)
        if not win:
            continue
        if p.trigger == "title":
            if p.id not in shows:
                continue
            found.append(DueProgram(p, max(shows[p.id], win[0]), win[1]))
        else:
            found.append(DueProgram(p, *win))
    if not found:
        return None
    first = min(d.start for d in found)
    return random.choice([d for d in found if d.start == first])


def _search(pattern: str, title: str) -> bool:
    return bool(pattern) and re.search(pattern, title, re.I) is not None


def show_step(p: Program, show: dict, title: str | None, now: float) -> str | None:
    """Advance a title-triggered program's show by one tick.

    `show` holds {"on": start timestamp or None, "ok": last time a title
    kept it on}. Returns "started" or "ended" when that happens."""
    if title is None:  # nothing read yet: keep what we know
        return None
    if show.get("on") is None:
        if _search(p.title_start, title) and not _search(p.title_end, title):
            show["on"] = show["ok"] = now
            return "started"
        return None
    if _search(p.title_end, title):
        show["on"] = None
        return "ended"
    if _search(p.title_start, title) or _search(p.title_keep, title):
        show["ok"] = now
    elif now - show["ok"] >= p.end_after_minutes * 60:
        show["on"] = None
        return "ended"
    return None


def spot_key(s: Spot) -> str:
    """What a spot plays; an error is shown only while this stays the same."""
    return s.feed_url if s.content == "feed" else "\n".join(s.files)


def short_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"{e.response.status_code} {e.response.reason_phrase} from {e.request.url}"
    return (str(e) or type(e).__name__)[:300]


def next_program(ch: Channel, now: datetime) -> dict | None:
    best = None
    for p in ch.programs:
        if not p.enabled:
            continue
        sh, sm = map(int, p.start.split(":"))
        for ahead in range(8):
            day = now + timedelta(days=ahead)
            if DAYS[day.weekday()] not in p.days:
                continue
            start = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
            if start > now:
                if best is None or start < best[1]:
                    best = (p, start)
                break
    if not best:
        return None
    return {"name": best[0].name, "start": best[1].isoformat(), "by_title": best[0].trigger == "title"}


def _minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def spot_active(spot: Spot, now: datetime) -> bool:
    """Right day and inside the active window (equal from/to = all day)."""
    if DAYS[now.weekday()] not in spot.days:
        return False
    a, b, m = _minutes(spot.active_from), _minutes(spot.active_to), now.hour * 60 + now.minute
    if a == b:
        return True
    return a <= m < b if a < b else (m >= a or m < b)


def spot_due_minute(spot: Spot, now: datetime) -> bool:
    """Whether a time-based spot fires in the minute `now` is in."""
    if spot.trigger == "songs" or not spot_active(spot, now):
        return False
    m = now.hour * 60 + now.minute
    if spot.trigger == "times":
        return f"{now.hour:02d}:{now.minute:02d}" in spot.times
    return (m - spot.offset_minutes) % spot.every_minutes == 0


def next_spot(spot: Spot, now: datetime) -> datetime | None:
    if not spot.enabled or spot.trigger == "songs":
        return None
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(8 * 1440):
        if spot_due_minute(spot, t):
            return t
        t += timedelta(minutes=1)
    return None


@dataclass
class Track:
    """A song pushed to a channel's music queue."""

    rid: int
    path: str
    title: str
    artist: str
    pushed_at: float = field(default_factory=time.time)
    art: str = ""


# What waits while a channel is quiet (wind down); things picked by hand still play.
QUIET_HOLDS = ("video", "podcast", "voice", "inbox")


@dataclass
class ChannelState:
    id: str
    liq: dict | None = None
    on_program: DueProgram | None = None
    looked_for_open: bool = False  # after a restart: closed a program left "started"?
    tie_breaks: dict = field(default_factory=dict)
    skipped: set = field(default_factory=set)
    listeners: int | None = None
    # base: what is set in Liquidsoap
    base_key: str | None = None
    feeder: Feeder | None = None
    music: list[Track] = field(default_factory=list)
    music_fetching: bool = False
    music_retry_at: float = 0.0
    music_error: str = ""
    # song changes on the base, for "between songs" and "every N songs"
    base_song: tuple | None = None
    song_changed_at: float = 0.0
    spots: dict = field(default_factory=dict)  # spot id -> {"slot", "songs", "turn", "error", "last"}
    shows: dict = field(default_factory=dict)  # title-triggered program id -> {"on", "ok", "why"}
    last_clip: tuple | None = None  # (title, artist) of the last clip pushed, to spot its metadata lingering
    ended: dict = field(default_factory=dict)  # program id -> {"until": window end, "at": when} for programs ended by hand
    recording: asyncio.Task | None = None  # the program on air being kept for 24 h
    quiet_until: float = 0.0  # wind down in the apps: no news or new clips until then


class Engine:
    def __init__(self, cfg: ConfigStore, store: Store, liq: Liquidsoap):
        self.cfg = cfg
        self.store = store
        self.booted = time.time()
        self.liq = liq
        self.items: list[Item] = []  # cut-ins of every channel
        self.downloads: asyncio.Queue[str] = asyncio.Queue()  # item keys
        self.progress: dict[str, float | None] = {}
        self.art = ArtCache()
        self.feed_cache: dict[str, tuple[float, Item]] = {}  # feed URL -> (fetched at, newest episode)
        self.channels: dict[str, ChannelState] = {}
        self.liq_up = False
        self.liq_streams: list[str] = []
        self.source_state: dict[str, dict] = {}
        self.inbox = Inbox()
        self._restart_at = 0.0
        self._wake_poll = asyncio.Event()
        self._force_check: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        self.titles = Watchers()
        # Bumped whenever something /api/status shows changes, so the live feed
        # (/api/events) only rebuilds and sends the status when there is news.
        self.version = 0
        self._signature: str | None = None
        self._waiting_rev = 0
        self._changed = asyncio.Event()

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        purge_cache(keep=set())
        try:
            ensure_dirs()
        except OSError as e:
            log.warning("media folder %s is not writable: %s", MEDIA_DIR, e)
        self.write_channels()
        for coro in (self._tick_loop(), self._poll_loop(), self._download_loop(), self._icecast_loop(), self._inbox_loop(), self._waiting_loop()):
            self._tasks.append(asyncio.create_task(coro))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for cs in self.channels.values():
            self._stop_recording(cs)
        self.titles.stop()

    @property
    def config(self) -> Config:
        return self.cfg.config

    def now(self) -> datetime:
        return datetime.now(self.config.settings.tz)

    def invidious(self) -> Invidious:
        return Invidious(self.config.settings.invidious_url)

    def state(self, cid: str) -> ChannelState:
        if cid not in self.channels:
            self.channels[cid] = ChannelState(cid)
        return self.channels[cid]

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.append(t)
        t.add_done_callback(lambda t: self._tasks.remove(t) if t in self._tasks else None)

    # --- channels in Liquidsoap ----------------------------------------------

    def _channel_sig(self) -> list[dict]:
        return [{"id": c.id, "mount": c.mount, "name": c.name} for c in self.config.channels]

    def write_channels(self) -> None:
        """The channel list Liquidsoap reads when it starts."""
        data = json.dumps(self._channel_sig())
        try:
            if not CHANNELS_FILE.exists() or CHANNELS_FILE.read_text() != data:
                tmp = CHANNELS_FILE.with_suffix(".tmp")
                tmp.write_text(data)
                tmp.replace(CHANNELS_FILE)
        except OSError as e:
            log.warning("cannot write %s: %s", CHANNELS_FILE, e)

    async def _sync_channels(self, liq_channels: list[dict]) -> bool:
        """Restart Liquidsoap when its channels differ from the config. True if restarting."""
        have = [{"id": c["id"], "mount": c.get("mount"), "name": c.get("name")} for c in liq_channels]
        if have == self._channel_sig():
            return False
        if time.time() < self._restart_at:
            return True
        self.write_channels()
        log.info("Channels changed; restarting the audio engine")
        self._restart_at = time.time() + RESTART_EVERY
        # Clips on air are lost with the restart; play them again afterwards.
        for it in self.items:
            if it.rid is not None:
                it.rid, it.state = None, "ready"
        for cs in self.channels.values():
            for t in cs.music:
                self._drop_file(t.path)
            cs.music.clear()
            cs.base_key = None
        try:
            await self.liq.restart()
        except httpx.HTTPError:
            pass
        return True

    # --- public actions ----------------------------------------------------

    def enqueue(self, item: Item) -> None:
        if any(i.uid == item.uid for i in self.items):
            return
        item.arrived = time.time()
        self.items.append(item)
        if item.path:
            item.state, item.ready_at = "ready", time.time()
        else:
            item.state = "waiting"
            if item.priority:
                self._spawn(self._download(item.key))
            else:
                self.downloads.put_nowait(item.key)
        log.info("New item for %s from %s: %s", item.channel, item.source_name, item.title)

    def enqueue_all(self, item: Item, src=None) -> int:
        """Queue `item` on every channel that takes cut-ins from its source."""
        n = 0
        for ch in self.config.channels:
            if src is None or ch.wants(src):
                self.enqueue(item.copy_for(ch.id))
                n += 1
        return n

    async def skip(self, cid: str) -> None:
        cur = next((i for i in self.items if i.channel == cid and i.state == "playing"), None)
        if cur:
            self.state(cid).skipped.add(cur.uid)
            await self.liq.skip(cid)
        else:
            await self.liq.skip_music(cid)

    def remove(self, uid: str) -> bool:
        for it in self.items:
            if it.uid == uid and it.state in ("waiting", "downloading", "ready", "failed"):
                self._finish(it, "removed")
                return True
        return False

    def reorder(self, cid: str, keys: list[str]) -> int:
        """Puts the channel's waiting clips named in `keys` in that order ("Up next" in the
        apps). They swap places among themselves; the clip already handed to the radio and
        clips not named stay where they are. Answers how many moved."""
        mine = {i.uid: i for i in self.items if i.channel == cid and i.state in ("waiting", "downloading", "ready")}
        picked = [mine[k] for k in dict.fromkeys(keys) if k in mine]
        if not picked:
            return 0
        # The order is (-priority, arrived): hand the slots out again in the new order.
        slots = sorted(((i.priority, i.arrived) for i in picked), key=lambda s: (-s[0], s[1]))
        for it, (priority, arrived) in zip(picked, slots):
            it.priority, it.arrived = priority, arrived
        self._note_changes()
        return len(picked)

    def persist_saved(self) -> None:
        """Moves the files of entries saved for later out of the replay folder (emptied after
        a day, and with the container when it is in RAM) to SAVED_DIR. A program still being
        recorded waits until it is over."""
        recording = {cs.recording.get_name() for cs in self.channels.values() if cs.recording}
        for entry_id, info in self.store.saved_replays():
            path = Path(info.get("path", ""))
            if not path.is_file() or path.parent == SAVED_DIR or MEDIA_DIR in path.parents or str(path) in recording:
                continue
            try:
                SAVED_DIR.mkdir(parents=True, exist_ok=True)
                target = SAVED_DIR / f"{entry_id}-{path.name}"
                shutil.copyfile(path, target)
            except OSError as e:
                log.warning("Could not save %s for later: %s", path, e)
                continue
            info = {k: v for k, v in info.items() if k != "until"}
            self.store.set_replay(entry_id, {**info, "path": str(target)})
            log.info("Saved %s for later as %s", path.name, target)

    def unsave(self, entry_id: int) -> None:
        """No longer saved: its saved copy goes; a clip fetched again from its source stays."""
        self.store.set_saved(entry_id, False)
        found = self.store.replay_of(entry_id)
        if found and Path(found[1].get("path", "")).parent == SAVED_DIR:
            Path(found[1]["path"]).unlink(missing_ok=True)
            self.store.set_replay(entry_id, None)

    def week_numbers(self, cid: str, days: int = 7) -> dict:
        """What the channel played over the last `days` days, for "Your week" in the apps."""
        now = self.now()
        first = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        entries = self.store.between(cid, first.timestamp(), time.time())
        per_day = {(first + timedelta(days=n)).date().isoformat(): {"clips": 0, "news": 0, "program_minutes": 0}
                   for n in range(days)}
        sources: dict[tuple[str, str], dict] = {}
        totals = {"clips": 0, "news": 0, "voice": 0, "program_minutes": 0, "clip_minutes": 0}
        started: dict[str, float] = {}
        tz = now.tzinfo
        for e in entries:
            day = per_day.get(datetime.fromtimestamp(e["ts"], tz).date().isoformat())
            if e["kind"] == "program":
                if e["status"] == "started":
                    started[e["title"]] = e["ts"]
                elif e["status"] == "ended" and e["title"] in started:
                    minutes = round((e["ts"] - started.pop(e["title"])) / 60)
                    totals["program_minutes"] += minutes
                    if day:
                        day["program_minutes"] += minutes
                    src = sources.setdefault(("program", e["title"]), {"kind": "program", "name": e["title"], "count": 0, "minutes": 0})
                    src["count"] += 1
                    src["minutes"] += minutes
                continue
            if e["status"] != "played":
                continue
            news = e["kind"] == "spot"
            totals["news" if news else "clips"] += 1
            if e["kind"] == "voice":
                totals["voice"] += 1
            minutes = (e["duration"] or 0) / 60
            totals["clip_minutes"] += round(minutes)
            if day:
                day["news" if news else "clips"] += 1
            src = sources.setdefault((e["kind"], e["source"]), {"kind": e["kind"], "name": e["source"], "count": 0, "minutes": 0})
            src["count"] += 1
            src["minutes"] += round(minutes)
        # A program still on air counts up to now.
        for title, ts in started.items():
            minutes = round((time.time() - ts) / 60)
            totals["program_minutes"] += minutes
            day = per_day.get(datetime.fromtimestamp(ts, tz).date().isoformat())
            if day:
                day["program_minutes"] += minutes
        top = sorted(sources.values(), key=lambda s: (-s["minutes"], -s["count"]))[:8]
        return {"channel": cid, "from": first.date().isoformat(), "today": now.date().isoformat(),
                "days": [{"date": d, **v} for d, v in per_day.items()], "totals": totals, "top": top}

    def check_now(self, source_id: str | None = None) -> None:
        if source_id:
            self._force_check.add(source_id)
        else:
            self._force_check.update(s.id for s in self.config.video + self.config.podcasts)
        self._wake_poll.set()

    async def play_latest(self, kind: str, source_id: str, cid: str) -> Item:
        src = self.config.source(kind, source_id)
        if not src:
            raise LookupError("unknown source")
        items = await (self.invidious().latest(src) if kind == "video" else podcast_latest(src))
        if not items:
            raise LookupError("no playable items found")
        items = [i for i in items if not i.live]
        if not items:
            raise LookupError("only lives Invidious can't play yet")
        newest = max(items, key=lambda i: i.published or 0)
        newest.item_id += f"-manual-{int(time.time())}"
        newest.channel = cid
        self.enqueue(newest)
        return newest

    def play_media(self, path: Path, cid: str, title: str | None = None, kind: str = "media") -> Item:
        rel = str(path.relative_to(MEDIA_DIR)) if MEDIA_DIR in path.parents else path.name
        it = Item(kind=kind, source_id=kind, source_name="Own content" if kind == "media" else "Inbox",
                  item_id=f"{rel}@{time.time()}", title=title or path.stem, media=str(path), path=str(path),
                  channel=cid, keep_file=True, priority=1, during_programs=True)
        self.enqueue(it)
        return it

    async def fire_spot(self, spot_id: str) -> None:
        found = self.config.spot(spot_id)
        if not found:
            raise LookupError("unknown spot")
        await self._fire(found[0], found[1], manual=True)

    # --- spots ---------------------------------------------------------------

    async def _fire(self, ch: Channel, spot: Spot, manual: bool = False) -> None:
        ss = self.state(ch.id).spots.setdefault(spot.id, {})
        ss["last"] = time.time()
        # A song-count spot fires on a song change, which already is the gap between songs.
        between = spot.between_songs and not manual and spot.trigger != "songs"
        common = dict(kind="spot", source_id=spot.id, source_name=spot.name, channel=ch.id, priority=1,
                      between_songs=between, during_programs=spot.during_programs or manual)
        try:
            if spot.content == "media":
                files = expand(spot.files)
                if not files:
                    raise LookupError("no audio files found for this spot")
                if spot.order == "random":
                    path = random.choice(files)
                else:
                    turn = ss.get("turn", 0)
                    path = files[turn % len(files)]
                    ss["turn"] = turn + 1
                self.enqueue(Item(item_id=f"{path.name}@{time.time()}", title=path.stem, media=str(path),
                                  path=str(path), keep_file=True, art=spot.image, **common))
            else:
                newest = await self._newest_episode(spot, fresh=True)
                seen_key = f"spot:{spot.id}"
                if spot.only_new and not manual and self.store.is_seen(seen_key, newest.item_id):
                    self.store.add_history("spot", spot.name, newest.title, "skipped", "no new episode", ch.id)
                    ss["error"] = ""
                    return
                self.store.mark_seen(seen_key, [newest.item_id])
                self.enqueue(Item(item_id=f"{newest.item_id}@{int(time.time())}", title=newest.title,
                                  media=newest.media, duration=newest.duration, published=newest.published,
                                  art=spot.image or newest.art, **common))
            ss["error"] = ""
        except Exception as e:
            ss["error"], ss["error_for"] = short_error(e), spot_key(spot)
            log.warning("spot %s failed: %s", spot.name, ss["error"])
            if manual:
                raise

    def _spots_tick(self, ch: Channel, cs: ChannelState, song_changed: bool) -> None:
        if cs.quiet_until > time.time():
            return  # winding down: timed spots are skipped, not saved up
        now = self.now()
        slot = now.strftime("%Y-%m-%d %H:%M")
        for spot in ch.spots:
            if not spot.enabled:
                continue
            ss = cs.spots.setdefault(spot.id, {})
            if spot.trigger == "songs":
                if song_changed and spot_active(spot, now):
                    ss["songs"] = ss.get("songs", 0) + 1
                    if ss["songs"] >= spot.every_songs:
                        ss["songs"] = 0
                        self._spawn(self._fire(ch, spot))
            elif ss.get("slot") != slot and spot_due_minute(spot, now):
                ss["slot"] = slot
                self._spawn(self._fire(ch, spot))

    # --- polling -----------------------------------------------------------

    async def _poll_loop(self) -> None:
        while True:
            try:
                if self.cfg.reload_if_changed():
                    self.write_channels()
                cfg = self.config
                interval = cfg.settings.poll_minutes * 60
                now = time.time()
                for kind, sources in (("video", cfg.video), ("podcast", cfg.podcasts)):
                    for src in sources:
                        st = self.source_state.setdefault(src.id, {"last_check": 0, "error": ""})
                        forced = src.id in self._force_check
                        if not forced and (not src.enabled or now - st["last_check"] < interval):
                            continue
                        self._force_check.discard(src.id)
                        await self._check(kind, src, st)
            except Exception:
                log.exception("poll loop error")
            self._wake_poll.clear()
            try:
                await asyncio.wait_for(self._wake_poll.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    async def _check(self, kind: str, src, st: dict) -> None:
        st["last_check"] = time.time()
        try:
            items = await (self.invidious().latest(src) if kind == "video" else podcast_latest(src))
            st["error"] = ""
        except Exception as e:
            st["error"] = f"{type(e).__name__}: {e}"[:300]
            log.warning("checking %s failed: %s", src.name, st["error"])
            return
        st["items"] = len(items)
        if items:
            newest = max(items, key=lambda i: i.published or 0)
            st["latest"] = {"title": newest.title, "published": newest.published}
        if not self.store.has_any(src.id):
            # First look at this source: remember what exists, play only what comes later.
            self.store.mark_seen(src.id, [INIT_MARK] + [i.item_id for i in items])
            log.info("Initial sync of %s: %d existing items ignored", src.name, len(items))
            return
        new = [i for i in items if not self.store.is_seen(src.id, i.item_id)]
        if not new:
            return
        self.store.mark_seen(src.id, [i.item_id for i in new])
        max_age = self.config.settings.max_age_hours * 3600
        # Oldest first; feeds list newest first, which breaks ties in published time.
        for _, it in sorted(enumerate(new), key=lambda x: (x[1].published or 0, -x[0])):
            # A live's length is only known once it is over; the waiting list checks it then.
            reason = passes_filters(src, it.title, None if it.live else it.duration)
            if not reason and max_age and not it.live and it.published and time.time() - it.published > max_age:
                reason = "older than max age"
            if not reason and not any(c.wants(src) for c in self.config.channels):
                reason = "no channel plays this source"
            if reason:
                self.store.add_history(kind, src.name, it.title, "skipped", reason)
                continue
            if it.live:
                self._defer(it, it.live)
                self.store.add_history(kind, src.name, it.title, "waiting", WAIT_NOTE[it.live])
                continue
            self.enqueue_all(it, src)

    # --- Waiting to play -------------------------------------------------------

    def _wait_put(self, it: Item, reason: str, next_check: float, first: float | None = None) -> None:
        data = {k: getattr(it, k) for k in _WAIT_FIELDS}
        data["first"] = first or time.time()
        self.store.wait_put(it.key, data, it.wait_since or time.time(), next_check, it.wait_tries, reason)
        self._waiting_rev += 1

    def _wait_drop(self, key: str, d: dict, status: str = "", detail: str = "") -> None:
        if self.store.wait_drop(key):
            self._waiting_rev += 1
        if status:
            self.store.add_history("video", d["source_name"], d["title"], status, detail)

    def _defer(self, it: Item, reason: str) -> None:
        """Put a video clip on the waiting list instead of the queue."""
        now = time.time()
        if reason in ("live", "upcoming"):
            gap = WAIT_CHECK
        else:  # still being processed, or the download failed: wait longer each time
            gap = min(WAIT_CHECK * 2 ** it.wait_tries, WAIT_GAP_MAX)
        it.wait_since = it.wait_since or now
        self._wait_put(it, reason, now + gap)

    def drop_waiting(self, key: str) -> bool:
        w = next((w for w in self.store.waiting() if w["key"] == key), None)
        if not w:
            return False
        self._wait_drop(key, w["item"], "removed")
        return True

    def waiting_for(self, ch: Channel) -> list[dict]:
        """What waits to play and will cut into `ch` once Invidious can play it."""
        out = []
        for w in self.store.waiting():
            d = w["item"]
            src = self.config.source("video", d["source_id"])
            if src and ch.wants(src):
                out.append({"key": w["key"], "title": d["title"], "source": d["source_name"], "reason": w["reason"],
                            "since": d.get("first") or w["since"], "next_check": w["next_check"], "tries": w["tries"]})
        return out

    async def _waiting_loop(self) -> None:
        while True:
            try:
                await self._check_waiting()
            except Exception:
                log.exception("waiting list error")
            await asyncio.sleep(60)

    async def _check_waiting(self) -> None:
        limit = self.config.settings.live_wait_hours * 3600
        for w in self.store.waiting():
            now = time.time()
            if w["next_check"] > now:
                continue
            d, key = w["item"], w["key"]
            src = self.config.source("video", d["source_id"])
            if not src:
                self._wait_drop(key, d)
                continue
            if not any(c.wants(src) for c in self.config.channels):
                self._wait_drop(key, d, "skipped", "no channel plays this source")
                continue
            it = Item(**{k: d.get(k) for k in _WAIT_FIELDS})
            it.wait_since, it.wait_tries = w["since"], w["tries"]
            first = d.get("first") or w["since"]
            try:
                state, info = await self.invidious().playable(it.media)
            except Exception as e:  # Invidious unreachable: not the video's fault, try again later
                log.info("waiting check of %s failed: %s", it.title, e)
                self._wait_put(it, w["reason"], now + WAIT_CHECK, first)
                continue
            if state in ("live", "upcoming"):
                if now - first > (LIVE_MAX if state == "live" else UPCOMING_MAX):
                    self._wait_drop(key, d, "skipped", f"still {state} after {round((now - first) / 86400)} days")
                    continue
                # The clock for giving up starts when the live is over.
                it.wait_since, it.wait_tries = now, 0
                nxt = now + WAIT_CHECK
                if state == "upcoming" and info.get("premiereTimestamp"):
                    nxt = max(nxt, float(info["premiereTimestamp"]))
                self._wait_put(it, state, nxt, first)
                continue
            if state:
                if now - it.wait_since > limit:
                    self._wait_drop(key, d, "failed", f"Invidious still can't play it after {round(limit / 3600)} h")
                    continue
                it.wait_tries += 1
                self._defer(it, state if w["reason"] in ("live", "upcoming", "processing") else w["reason"])
                continue
            it.live = ""
            it.duration = float(info.get("lengthSeconds") or 0) or it.duration
            reason = passes_filters(src, it.title, it.duration)
            if reason:
                self._wait_drop(key, d, "skipped", reason)
                continue
            self._wait_drop(key, d)
            log.info("%s can be played now", it.title)
            self.enqueue_all(it, src)

    # --- downloading -------------------------------------------------------

    async def _download_loop(self) -> None:
        while True:
            key = await self.downloads.get()
            await self._download(key)

    async def _download(self, key: str) -> None:
        """Download one item once for every channel that queued it."""
        group = [i for i in self.items if i.key == key and i.state == "waiting"]
        if not group:
            return
        for it in group:
            it.state = "downloading"
        first = group[0]
        log.info("Downloading %s", first.title)

        def progress(p):
            for i in group:
                i.progress = p

        try:
            st = self.config.settings
            path = await download(first, self.invidious(), st.ytdlp_fallback, st.video_audio, progress, st.cache_in_ram)
            alive = [i for i in group if i in self.items]
            if not alive:  # removed while downloading
                path.unlink(missing_ok=True)
                return
            for it in alive:
                it.path, it.state, it.ready_at = str(path), "ready", time.time()
            log.info("Downloaded %s", first.title)
        except Exception as e:
            err = (str(e) or type(e).__name__)[:300]
            log.warning("download of %s failed: %s", first.title, err)
            src = self.config.source("video", first.source_id) if first.kind == "video" else None
            limit = self.config.settings.live_wait_hours * 3600
            if src and time.time() - (first.wait_since or time.time()) < limit:
                # Often a live video hasn't finished processing: try again later.
                for it in group:
                    if it in self.items:
                        self.items.remove(it)
                retry = dataclasses.replace(first, wait_tries=first.wait_tries + (1 if first.wait_since else 0))
                self._defer(retry, f"download failed: {err}")
                if not first.wait_since:
                    self.store.add_history("video", first.source_name, first.title, "waiting", "download failed, trying again later")
                return
            for it in group:
                it.state, it.error = "failed", err
                self.store.add_history(it.kind, it.source_name, it.title, "failed", err, it.channel)
                asyncio.get_running_loop().call_later(600, self._drop_failed, it)

    def _drop_failed(self, it: Item) -> None:
        if it in self.items and it.state == "failed":
            self.items.remove(it)

    # --- dropped-in files ----------------------------------------------------

    async def _inbox_loop(self) -> None:
        while True:
            try:
                for p in self.inbox.scan():
                    hint = self.inbox.channel_hint(p)
                    chans = self.config.channels
                    ch = next((c for c in chans if c.mount == hint or c.id == hint), None) or (chans[0] if chans else None)
                    if ch:
                        log.info("Inbox: %s on %s", p.name, ch.name)
                        self.play_media(p, ch.id, kind="inbox")
            except Exception:
                log.exception("inbox error")
            await asyncio.sleep(3)

    # --- on-air decisions ----------------------------------------------------

    def _drop_file(self, path: str | None) -> None:
        if path and not any(i.path == path for i in self.items):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def _finish(self, it: Item, status: str) -> None:
        if it in self.items:
            self.items.remove(it)
        # A clip ended early ("End clip") is the one most likely wanted back.
        replay = self._replay_info(it) if status in ("played", "skipped") else None
        if it.kind == "inbox" and it.path:
            self.inbox.done(Path(it.path))
        elif not it.keep_file:
            self._drop_file(it.path)
        self.store.add_history(it.kind, it.source_name, it.title, status, channel=it.channel, replay=replay)

    def _replay_info(self, it: Item) -> dict | None:
        """How "listen again" finds this clip later. Clips and news episodes are fetched
        again from where they came from; only voice messages, which exist nowhere else,
        are kept for a day."""
        if it.kind == "inbox":
            return None
        art = {"art": it.art} if it.art else {}  # so the apps can show its picture again
        if it.duration:  # players misjudge the length of some streams (the site's AAC: twice as long)
            art["duration"] = it.duration
        if it.kind == "voice":
            if not self.config.settings.replay_keep or not it.path or not Path(it.path).is_file():
                return None
            prune_replays(REPLAY_KEEP)
            kept = keep_for_replay(Path(it.path), self.config.settings.cache_in_ram)
            return {"path": str(kept), "until": time.time() + REPLAY_KEEP, **art}
        if it.keep_file and it.path:  # own media stays on disk anyway
            return {"path": it.path, **art}
        return {"media": it.media, **art} if it.media else None

    def now_audio(self, cid: str, key: str | None = None) -> dict | None:
        """Where the apps get the cut-in playing on a channel now, to scroll back in it on
        the phone: the file itself for own media and voice messages (phones can play
        those), else its source again (the radio's copy may be WebM/Opus, which iPhones
        can't play). None when nothing replayable plays, or `key` is not what plays."""
        it = next((i for i in self.items if i.channel == cid and i.state == "playing"), None)
        if it is None or (key and it.key != key):
            return None
        if it.path and (it.keep_file or it.kind in ("voice", "inbox")) and Path(it.path).is_file():
            return {"path": it.path}
        return {"media": it.media} if it.media and it.kind != "inbox" else None

    def _item_wins(self, cs: ChannelState, it: Item, due: DueProgram) -> bool:
        start = due.start.timestamp()
        if int(it.arrived) < int(start):
            return True
        if int(it.arrived) > int(start):
            return False
        key = (it.uid, start)
        if key not in cs.tie_breaks:
            cs.tie_breaks[key] = random.random() < 0.5
        return cs.tie_breaks[key]

    async def _tick_loop(self) -> None:
        while True:
            try:
                await self._tick()
            except httpx.HTTPError as e:
                if self.liq_up:
                    log.warning("Liquidsoap unreachable: %s", e)
                self.liq_up = False
                for cs in self.channels.values():
                    cs.liq = None
            except Exception:
                log.exception("tick error")
            self._note_changes()
            await asyncio.sleep(TICK)

    # --- change tracking for the live feed --------------------------------------

    _VOLATILE = ("cutin_elapsed", "cutin_remaining", "since")  # the UI counts these up itself

    def _note_changes(self) -> None:
        chans = {}
        for cid, cs in self.channels.items():
            liq = {k: v for k, v in (cs.liq or {}).items() if k not in self._VOLATILE}
            prog = (cs.on_program.program.name, cs.on_program.end) if cs.on_program else None
            chans[cid] = (liq, prog, cs.listeners, cs.music_error, cs.spots, cs.shows, cs.ended)
        items = [(i.uid, i.state, i.channel, round((i.progress or 0) * 20)) for i in self.items]
        sig = json.dumps([id(self.config), self.liq_up, chans, items, self.source_state, self._waiting_rev], sort_keys=True, default=str)
        if sig != self._signature:
            self._signature = sig
            self.version += 1
            ev, self._changed = self._changed, asyncio.Event()
            ev.set()

    async def wait_change(self, version: int, timeout: float) -> None:
        """Return as soon as the version moves past `version`, or after `timeout` seconds."""
        if self.version != version:
            return
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
        except TimeoutError:
            pass

    async def _tick(self) -> None:
        st = await self.liq.status()
        self.liq_up = True
        self.liq_streams = st.get("streams", [])
        liq_channels = st.get("channels", [])
        if await self._sync_channels(liq_channels):
            return
        by_id = {c["id"]: c for c in liq_channels}
        cfg = self.config
        self.titles.want(self._watched_urls())
        for ch in cfg.channels:
            cs = self.state(ch.id)
            cs.liq = by_id.get(ch.id)
            if cs.liq:
                await self._tick_channel(ch, cs, cs.liq)
        # Clips of channels that were deleted.
        ids = {c.id for c in cfg.channels}
        for it in [i for i in self.items if i.channel not in ids]:
            self._finish(it, "removed")
        if set(self.liq_streams) != set(cfg.settings.streams):
            await self.liq.set_streams(cfg.settings.streams)

    async def _tick_channel(self, ch: Channel, cs: ChannelState, st: dict) -> None:
        cur, queued = st["cutin_current"], set(st["cutin_queue"])
        mine = [i for i in self.items if i.channel == ch.id]

        # Reconcile what we pushed with what Liquidsoap reports. A request can
        # be briefly invisible while Liquidsoap resolves it, hence the grace time.
        for it in [i for i in mine if i.rid is not None]:
            if it.rid == cur:
                it.state = "playing"
            elif it.rid in queued:
                it.state = "queued"
            elif it.state == "playing" or time.time() - it.pushed_at > 15:
                self._finish(it, "skipped" if it.uid in cs.skipped else "played")
                cs.skipped.discard(it.uid)
                mine.remove(it)
        music_ids = {st["music_current"], *st["music_queue"]}
        for t in list(cs.music):
            if t.rid not in music_ids and time.time() - t.pushed_at > 15:
                cs.music.remove(t)
                self._drop_file(t.path)

        # Song changes on the base (not during cut-ins).
        song_changed = False
        if st["mode"] == "base" and st.get("title"):
            song = (st.get("title", ""), st.get("artist", ""))
            if song != cs.base_song:
                song_changed = cs.base_song is not None
                cs.base_song = song
                cs.song_changed_at = time.time()
        self._spots_tick(ch, cs, song_changed)

        now = self.now()
        self._shows_tick(ch, cs, now)
        shows = {pid: datetime.fromtimestamp(sh["on"], now.tzinfo) for pid, sh in cs.shows.items() if sh.get("on")}
        due = due_program(ch, now, shows)
        cs.ended = {pid: e for pid, e in cs.ended.items() if e["until"] > now}
        if due and due.program.id in cs.ended:
            due = None  # ended by hand for the rest of this window
        on_air_clips = [i for i in mine if i.state in ("queued", "playing")]

        # Program window over (or replaced by an earlier one): back to the station.
        if cs.on_program and (not due or due.program.id != cs.on_program.program.id):
            p = cs.on_program.program
            if due:
                why = f"replaced by {due.program.name}"
            elif p.id in cs.ended:
                why = "ended by hand"
            elif p.trigger == "title":
                why = cs.shows.get(p.id, {}).get("why", "") or "window over"
            else:
                why = "window over"
            self.store.add_history("program", p.name, p.name, "ended", why, channel=ch.id)
            log.info("Program %s ended on %s (%s)", p.name, ch.name, why)
            cs.on_program = None
            self._stop_recording(cs)
            self.persist_saved()  # a program saved while it was still on air

        def holds(p: Program, it: Item) -> bool:
            return not it.during_programs and not p.cutins_interrupt

        def eligible(it: Item) -> bool:
            if cs.quiet_until > time.time() and it.kind in QUIET_HOLDS:
                return False  # waits for the morning
            if cs.on_program and holds(cs.on_program.program, it):
                return False
            if not cs.on_program and due and holds(due.program, it) and not self._item_wins(cs, it, due):
                return False
            if it.between_songs and not on_air_clips:
                return cs.song_changed_at > it.ready_at or time.time() - it.ready_at > HOLD_MAX
            return True

        ready = sorted((i for i in mine if i.state == "ready"), key=lambda i: (-i.priority, i.arrived))
        nxt = next((i for i in ready if eligible(i)), None)
        if nxt and not queued:
            nxt.rid = await self.liq.push(ch.id, nxt.path, nxt.title, nxt.source_name)
            cs.last_clip = (nxt.title, nxt.source_name)
            log.info("Cut-in on %s: %s (%s)%s", ch.name, nxt.title, nxt.source_name,
                     f" during program {cs.on_program.program.name}" if cs.on_program else
                     f" before program {due.program.name}" if due else "")
            nxt.pushed_at = time.time()
            nxt.state = "queued"
            on_air_clips.append(nxt)

        # A minute after start (stream titles have been read by then): a program that was
        # on when the server stopped and isn't any more ended, or the apps would show it
        # as still on air.
        if not cs.looked_for_open and (cs.on_program or (not due and time.time() - self.booted > 60)):
            cs.looked_for_open = True
            open_ = None if cs.on_program else self.store.last_program(ch.id)
            if open_ and open_["status"] == "started":
                self.store.add_history("program", open_["title"], open_["title"], "ended", "over while rRadio restarted",
                                       channel=ch.id)

        if due and cs.on_program is None and not on_air_clips:
            window = program_window(due.program, now)
            since = window[0].timestamp() if window else time.time() - 12 * 3600
            running = self.store.open_program(ch.id, due.program.name, since)
            if running:  # the server restarted during the program: carry on with it
                entry_id, started, old = running
                cs.on_program = DueProgram(due.program, datetime.fromtimestamp(started, now.tzinfo), due.end)
                replay = self._start_recording(ch, cs, due.program, resume=old)
                if replay != old:
                    self.store.set_replay(entry_id, replay)
                log.info("Program %s goes on on %s after a restart", due.program.name, ch.name)
            else:
                cs.on_program = due
                why = cs.shows.get(due.program.id, {}).get("why", "") if due.program.trigger == "title" else ""
                replay = self._start_recording(ch, cs, due.program)
                self.store.add_history("program", due.program.name, due.program.name, "started", why, channel=ch.id,
                                       replay=replay)
                log.info("Program %s started on %s%s", due.program.name, ch.name, f" ({why})" if why else "")

        base = cs.on_program.program if cs.on_program else ch.station()
        await self._set_base(ch, cs, st, base)

    # --- programs kept for 24 h ----------------------------------------------------

    def _start_recording(self, ch: Channel, cs: ChannelState, p: Program, resume: dict | None = None) -> dict | None:
        """Records a program marked "keep for 24 hours" from its own stream, next to the
        kept voice messages, so the apps can listen back to it. The history entry of its
        start points at the file, which grows until the program ends.

        After a restart during the program (`resume`, the old replay info), recording goes
        on in the same file if it is still there. Otherwise (the RAM disk is emptied with
        the container) a new one starts, and "from" says when, since it misses the start."""
        self._stop_recording(cs)
        if not p.keep_day or p.kind != "stream" or not p.url:
            return resume or None
        if resume and resume.get("path") and Path(resume["path"]).is_file():
            cs.recording = asyncio.create_task(self._record(p, Path(resume["path"])), name=resume["path"])
            log.info("Keeping program %s on %s: recording goes on in %s", p.name, ch.name, resume["path"])
            return resume
        prune_replays(REPLAY_KEEP)
        folder = cache_dir(self.config.settings.cache_in_ram) / "replay"
        folder.mkdir(parents=True, exist_ok=True)
        ext = next((e for e in ("aac", "m4a", "ogg", "opus") if p.url.lower().split("?")[0].endswith("." + e)), "mp3")
        path = folder / f"program-{ch.id}-{int(time.time())}.{ext}"
        path.touch()
        cs.recording = asyncio.create_task(self._record(p, path), name=str(path))
        log.info("Keeping program %s on %s for 24 h in %s", p.name, ch.name, path)
        art = self.base_art(p, cs, "", "")
        return {"path": str(path), "until": time.time() + REPLAY_KEEP, **({"art": art} if art else {}),
                **({"from": time.time()} if resume is not None else {})}

    def drop_recording(self, path: str) -> None:
        """Stops recording into `path` (a kept program removed while still on air)."""
        for cs in self.channels.values():
            if cs.recording and cs.recording.get_name() == path:
                self._stop_recording(cs)

    def _stop_recording(self, cs: ChannelState) -> None:
        if cs.recording:
            cs.recording.cancel()
            cs.recording = None

    async def _record(self, p: Program, path: Path) -> None:
        """Copies the program's stream into `path` until cancelled, reconnecting after a
        drop. When the RAM disk gets nearly full, the recording moves to disk and goes on
        there; it only stops early when the disk is nearly full too."""
        headers = {"User-Agent": USER_AGENT}
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(30, read=60)) as client:
            out = path.open("ab")
            try:
                while True:
                    try:
                        async with client.stream("GET", p.url, headers=headers) as r:
                            r.raise_for_status()
                            unchecked = 0
                            async for chunk in r.aiter_raw():
                                out.write(chunk)
                                unchecked += len(chunk)
                                if unchecked > 1 << 20:  # about once a minute
                                    unchecked = 0
                                    out.flush()
                                    if shutil.disk_usage(path.parent).free < RECORD_MIN_FREE:
                                        moved = await self._record_to_disk(p, path, out)
                                        if not moved:
                                            log.warning("Stopped keeping %s: %s is nearly full", p.name, path.parent)
                                            return
                                        path, out = moved
                    except asyncio.CancelledError:
                        raise
                    except (httpx.HTTPError, OSError) as e:
                        log.info("Keeping %s: stream dropped (%s), reconnecting", p.name, e)
                    out.flush()
                    await asyncio.sleep(5)
            finally:
                out.close()

    async def _record_to_disk(self, p: Program, path: Path, out):
        """Moves a recording from the full RAM disk to the disk cache: (new path, open file),
        or None when it already is on disk or the disk has no room either. Listeners
        part way through keep the old file until they load it again."""
        target = CACHE_DIR / "replay" / path.name
        if path.parent == target.parent:
            return None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(target.parent).free < RECORD_MIN_FREE + path.stat().st_size:
                return None
            out.close()
            await asyncio.to_thread(shutil.copyfile, path, target)
        except OSError as e:
            log.warning("Keeping %s: could not move it to disk (%s)", p.name, e)
            if out.closed:
                return path, path.open("ab")
            return None
        self.store.move_replay(str(path), str(target))
        task = asyncio.current_task()
        if task:  # persist_saved and drop_recording know the recording by this name
            task.set_name(str(target))
        path.unlink(missing_ok=True)
        log.info("Keeping %s: %s is nearly full, recording goes on in %s", p.name, path.parent, target)
        return target, target.open("ab")

    # --- title-triggered programs ------------------------------------------------

    def _watched_urls(self) -> set[str]:
        now = self.now()
        return {
            p.watch_url
            for ch in self.config.channels
            for p in ch.programs
            if p.enabled and p.trigger == "title" and p.watch_url and program_window(p, now)
        }

    def _shows_tick(self, ch: Channel, cs: ChannelState, now: datetime) -> None:
        """Start and end title-triggered programs from the titles their stream sends."""
        ids = set()
        for p in ch.programs:
            if p.trigger != "title" or not p.enabled:
                continue
            ids.add(p.id)
            window = program_window(p, now)
            if p.id not in cs.shows:
                cs.shows[p.id] = {"on": None, "ok": 0.0, "why": ""}
                # After a restart: a show that already ended in this window stays over.
                ended = self.store.ended_since(ch.id, p.name, window[0].timestamp()) if window else None
                if ended:
                    cs.shows[p.id].update(ended_at=ended, why="ended earlier in this window")
            show = cs.shows[p.id]
            if not window:
                if show["on"] is not None:
                    show["on"], show["why"] = None, "window over"
                show.pop("ended_at", None)
                continue
            if show.get("ended_at") and show["on"] is None:
                # Once over, it stays over until the window closes: some stations keep
                # sending the show's title for hours afterwards (Hafenradio does).
                continue
            w = self.titles.get(p.watch_url)
            title = w.title if w else None
            change = show_step(p, show, title, time.time())
            if change == "started":
                show["why"] = f"title: {title}"
                show.pop("ended_at", None)
                log.info("Program %s started on %s: %s", p.name, ch.name, title)
            elif change == "ended":
                show["why"] = f"title: {title}"
                show["ended_at"] = time.time()
                log.info("Program %s ended on %s: %s", p.name, ch.name, title)
        for pid in [k for k in cs.shows if k not in ids]:
            del cs.shows[pid]

    def open_windows(self, ch: Channel) -> list[dict]:
        """Programs whose window is open now, and whether they are on, still to come or over."""
        cs, now = self.state(ch.id), self.now()
        out = []
        for p in ch.programs:
            win = program_window(p, now) if p.enabled else None
            if not win:
                continue
            show = cs.shows.get(p.id, {})
            ended_at = None
            if cs.on_program and cs.on_program.program.id == p.id:
                state = "on"
            elif p.id in cs.ended:
                state, ended_at = "ended", cs.ended[p.id]["at"]
            elif p.trigger == "title" and show.get("on") is None:
                ended_at = show.get("ended_at")
                state = "ended" if ended_at else "waiting"
            else:
                state = "starting"  # due, waiting for a clip to finish
            out.append({
                "id": p.id, "name": p.name, "by_title": p.trigger == "title", "until": win[1].isoformat(),
                "state": state,
                "ended_at": datetime.fromtimestamp(ended_at, now.tzinfo).isoformat() if ended_at else None,
            })
        return out

    def program_status(self, ch: Channel) -> dict:
        cs, now = self.state(ch.id), self.now()
        out = {}
        for p in ch.programs:
            if p.trigger != "title":
                continue
            show = cs.shows.get(p.id, {})
            w = self.titles.get(p.watch_url)
            out[p.id] = {
                "window_open": program_window(p, now) is not None,
                "show_on": show.get("on"),
                "on_air": bool(cs.on_program and cs.on_program.program.id == p.id),
                **(w.public() if w else {"title": None, "watching": False, "connected": False, "error": "", "log": []}),
            }
        return out

    async def _set_base(self, ch: Channel, cs: ChannelState, st: dict, base: BaseSource | None) -> None:
        if base is None:
            return
        if base.kind == "stream":
            if st["base_kind"] != "stream" or st["base_url"] != base.url or cs.base_key != base.key:
                await self.liq.set_base(ch.id, "stream", base.url, base.name)
                cs.base_key = base.key
            return
        # Navidrome: a different playlist starts fresh; the same one carries on.
        if cs.base_key != base.key:
            if cs.feeder and cs.feeder.key != base.key:
                await self.liq.flush_music(ch.id)
                cs.feeder = None
            cs.music_error, cs.music_retry_at = "", 0.0
        if st["base_kind"] != "music" or cs.base_key != base.key:
            await self.liq.set_base(ch.id, "music", "", base.name)
            cs.base_key = base.key
        if not st["music_queue"] and not cs.music_fetching and time.time() >= cs.music_retry_at:
            cs.music_fetching = True
            self._spawn(self._next_song(ch.id, cs, base))

    async def _next_song(self, cid: str, cs: ChannelState, src: BaseSource) -> None:
        try:
            st = self.config.settings
            nd = Navidrome.from_settings(st)
            if cs.feeder is None or cs.feeder.key != src.key:
                cs.feeder = Feeder(src.key)
            song = await cs.feeder.next(nd, src)
            name = "music-" + hashlib.sha1(f"{cid}:{song.id}:{time.time()}".encode()).hexdigest()[:16]
            path = await download_url(nd.stream_url(song.id, st.navidrome_bitrate), name, in_ram=st.cache_in_ram)
            if cs.base_key != src.key:  # the station changed meanwhile
                path.unlink(missing_ok=True)
                return
            rid = await self.liq.push_music(cid, str(path), song.title, song.artist)
            art = nd.cover_url(song.cover) if song.cover else ""
            cs.music.append(Track(rid, str(path), song.title, song.artist, art=art))
            cs.music_error = ""
        except Exception as e:
            cs.music_error = scrub(str(e) or type(e).__name__)[:300]
            cs.music_retry_at = time.time() + 30
            log.warning("Navidrome on %s: %s", cid, cs.music_error)
        finally:
            cs.music_fetching = False

    # --- listeners -----------------------------------------------------------

    async def _icecast_loop(self) -> None:
        async with httpx.AsyncClient(timeout=5) as c:
            while True:
                try:
                    data = (await c.get(ICECAST_STATUS_URL)).json()
                    srcs = data.get("icestats", {}).get("source", [])
                    if isinstance(srcs, dict):
                        srcs = [srcs]
                    for ch in self.config.channels:
                        self.state(ch.id).listeners = sum(
                            int(s.get("listeners", 0)) for s in srcs if f"/{ch.mount}." in s.get("listenurl", "")
                        )
                except Exception:
                    for cs in self.channels.values():
                        cs.listeners = None
                await asyncio.sleep(15)

    # --- status for the UI -----------------------------------------------------

    def now_playing(self, ch: Channel) -> dict:
        cs = self.state(ch.id)
        st = cs.liq
        if not st:
            return {"mode": "offline", "source": "", "title": "", "artist": ""}
        playing = next((i for i in self.items if i.channel == ch.id and i.state == "playing"), None)
        if st["mode"] == "cutin" and playing:
            return {
                "mode": "cutin",
                "kind": playing.kind,
                "source": playing.source_name,
                "title": playing.title,
                "elapsed": st["cutin_elapsed"],
                "duration": st["cutin_elapsed"] + st["cutin_remaining"],
                "art": self.art.path(playing.art),
                "key": playing.key,
                # The apps can scroll back in it while it plays (GET /api/now/audio).
                "listen_back": self.now_audio(ch.id) is not None,
            }
        base = cs.on_program.program if cs.on_program else ch.station()
        mode = "program" if cs.on_program else "station"
        name = base.name if base else ""
        title, artist = st.get("title", ""), st.get("artist", "")
        if cs.last_clip is None:  # after a restart: the last clip from the history
            last = self.store.last_cutin(ch.id)
            cs.last_clip = (last["title"], last["source"]) if last else ()
        # No metadata of its own, or what is left over from the last clip.
        if title == name or not st["base_up"] or (title, artist) == cs.last_clip:
            title = artist = ""
        if base is None:
            return {"mode": "silence", "source": "", "base_kind": "stream", "title": "", "artist": "",
                    "error": "No station picked for this channel. Tick one under Sources."}
        np = {
            "mode": mode if st["base_up"] else "silence",
            "source": name,
            "base_kind": base.kind if base else "stream",
            "title": title,
            "artist": artist,
        }
        if base and base.kind == "navidrome" and cs.music_error:
            np["error"] = cs.music_error
        np["art"] = self.art.path(self.base_art(base, cs, title, artist)) if st["base_up"] else None
        if cs.on_program:
            np["until"] = cs.on_program.end.isoformat()
            np["by_title"] = cs.on_program.program.trigger == "title"
            np["program_id"] = cs.on_program.program.id
            np["since"] = cs.on_program.start.isoformat()
        return np

    def base_art(self, base: BaseSource, cs: ChannelState, title: str, artist: str) -> str:
        """The song's cover on Navidrome, else the station's logo. A program
        without a logo borrows one from a station with the same stream."""
        if base.kind == "navidrome" and title:
            track = next((t for t in reversed(cs.music) if (t.title, t.artist) == (title, artist)), None)
            if track and track.art:
                return track.art
        if base.image:
            return base.image
        twin = next((s for c in self.config.channels for s in c.stations if s.key == base.key and s.image), None)
        return twin.image if twin else ""

    def end_program(self, cid: str) -> str | None:
        """End the program on air for the rest of its window; the station comes back."""
        ch = self.config.channel(cid)
        cs = self.state(cid)
        if ch is None or cs.on_program is None:
            return None
        p = cs.on_program.program
        cs.ended[p.id] = {"until": cs.on_program.end, "at": time.time()}
        cs.on_program = None
        self.store.add_history("program", p.name, p.name, "ended", "ended by hand", channel=cid)
        return p.name

    async def _newest_episode(self, spot: Spot, fresh: bool = False) -> Item:
        """The newest episode of a feed spot; cached for 10 minutes unless `fresh`."""
        hit = self.feed_cache.get(spot.feed_url)
        if hit and not fresh and time.time() - hit[0] < 600:
            return hit[1]
        eps = await podcast_latest(Podcast(name=spot.name, url=spot.feed_url, max_minutes=0))
        if not eps:
            raise LookupError("the feed has no episodes")
        newest = max(eps, key=lambda i: i.published or 0)
        self.feed_cache[spot.feed_url] = (time.time(), newest)
        return newest

    async def spot_info(self, spot_id: str) -> dict:
        """Details for the schedule's spot popup: the feed's newest episode and a picture."""
        found = self.config.spot(spot_id)
        if not found:
            raise LookupError("unknown spot")
        spot = found[1]
        out = {"art": self.art.path(spot.image), "episode": None}
        if spot.content == "feed" and spot.feed_url:
            try:
                ep = await self._newest_episode(spot)
            except Exception as e:
                out["error"] = (str(e) or type(e).__name__)[:300]
                return out
            out["episode"] = {"title": ep.title, "published": ep.published, "duration": ep.duration,
                              "new": not self.store.is_seen(f"spot:{spot.id}", ep.item_id)}
            out["art"] = self.art.path(spot.image or ep.art)
        return out

    def week(self, cid: str | None, day: date | None = None) -> dict:
        """The schedule of the week (Monday to Sunday) that `day` is in, for the
        apps: programs and timed spots per day, in minutes since midnight. On
        days that have begun, programs that ran carry their real times (`ran`)."""
        ch = self.config.channel(cid) or self.config.channel(None)
        now = self.now()
        day = day or now.date()
        monday = day - timedelta(days=day.weekday())
        tz = now.tzinfo
        start = datetime.combine(monday, datetime.min.time(), tz)
        ran = self.store.programs_between(ch.id, start.timestamp(), (start + timedelta(days=7)).timestamp()) if ch else []
        days = []
        for i in range(7):
            d = monday + timedelta(days=i)
            midnight = datetime.combine(d, datetime.min.time(), tz)
            programs, spots, every_songs = [], [], []
            for p in ch.programs if ch else []:
                if not p.enabled:
                    continue
                a, b = _minutes(p.start), _minutes(p.end)
                spans = []
                if DAYS[i] in p.days:
                    spans.append((a, b if b > a else 1440))
                if 0 < b <= a and DAYS[(i + 6) % 7] in p.days:
                    spans.append((0, b))  # yesterday's, past midnight
                for a2, b2 in spans:
                    entry = {"name": p.name, "start": a2, "end": b2, "trigger": p.trigger,
                             "keep_day": p.keep_day, "art": self.art.path(p.image)}
                    # What really happened in that window, from the history.
                    w0, w1 = midnight + timedelta(minutes=a2), midnight + timedelta(minutes=b2)
                    started = next((h for h in ran if h["title"] == p.name and h["status"] == "started"
                                    and w0.timestamp() - 60 <= h["ts"] < w1.timestamp()), None)
                    if started:
                        ended = next((h for h in ran if h["title"] == p.name and h["status"] == "ended"
                                      and h["id"] > started["id"]), None)
                        to_min = lambda ts: round((ts - midnight.timestamp()) / 60)
                        entry["ran"] = {"from": max(to_min(started["ts"]), a2),
                                        "to": min(to_min(ended["ts"]), b2) if ended else None,
                                        "history_id": started["id"], "replay": started["replay"]}
                    programs.append(entry)
            for sp in ch.spots if ch else []:
                if not sp.enabled or DAYS[i] not in sp.days:
                    continue
                if sp.trigger == "songs":
                    every_songs.append({"name": sp.name, "every_songs": sp.every_songs})
                    continue
                for m in range(1440):
                    t = midnight + timedelta(minutes=m)
                    if spot_due_minute(sp, t):
                        spots.append({"name": sp.name, "minute": m})
            programs.sort(key=lambda e: e["start"])
            spots.sort(key=lambda e: e["minute"])
            days.append({"date": d.isoformat(), "day": DAYS[i], "programs": programs,
                         "spots": spots, "song_spots": every_songs})
        station = ch.station() if ch else None
        return {"channel": ch.id if ch else None, "station": station.name if station else "",
                "today": now.date().isoformat(), "now": now.hour * 60 + now.minute, "days": days}

    def set_quiet(self, cid: str, until: float | None) -> float | None:
        """Hold news, spots and new clips on a channel until `until` (None ends it)."""
        cs = self.state(cid)
        cs.quiet_until = until if until and until > time.time() else 0.0
        self._note_changes()
        return cs.quiet_until or None

    def config_images(self) -> dict:
        """Where the UI can preview each picture set in the config, by its URL."""
        urls = {s.image for c in self.config.channels for s in [*c.stations, *c.programs, *c.spots] if s.image}
        return {u: self.art.path(u) for u in sorted(urls)}

    def next_spot_of(self, ch: Channel) -> dict | None:
        """The channel's soonest timed spot or news, for "Next up" on Home."""
        now = self.now()
        due = [(t, s) for s in ch.spots if (t := next_spot(s, now))]
        if not due:
            return None
        t, s = min(due, key=lambda x: x[0])
        return {"name": s.name, "at": t.isoformat()}

    def spot_status(self, ch: Channel) -> dict:
        cs, now = self.state(ch.id), self.now()
        out = {}
        for s in ch.spots:
            ss = cs.spots.get(s.id, {})
            nxt = next_spot(s, now)
            out[s.id] = {
                "name": s.name,
                "next": nxt.isoformat() if nxt else None,
                "last": ss.get("last"),
                # An error from before the spot was edited no longer applies.
                "error": ss.get("error", "") if ss.get("error_for") == spot_key(s) else "",
                "songs": ss.get("songs", 0),
            }
        return out

    def status(self, cid: str | None) -> dict:
        cfg = self.config
        ch = cfg.channel(cid) or cfg.channel(None)
        if ch is None:
            return {"now": {"mode": "offline"}, "engine_up": self.liq_up, "channels": [], "queue": [], "history": []}
        cs = self.state(ch.id)
        station = ch.station()
        return {
            "channel": {"id": ch.id, "name": ch.name, "mount": ch.mount},
            "channels": [
                {"id": c.id, "name": c.name, "mount": c.mount, "mode": self.now_playing(c)["mode"],
                 "now": self.now_playing(c), "next_program": next_program(c, self.now()),
                 "next_spot": self.next_spot_of(c),
                 "windows": self.open_windows(c),
                 "listeners": self.state(c.id).listeners}
                for c in cfg.channels
            ],
            "incoming": [
                {**i.public(), "channel": i.channel}
                for i in sorted(self.items, key=lambda i: (-i.priority, i.arrived))
                if i.state not in ("playing", "failed")
            ],
            "now": self.now_playing(ch),
            # The RAM disk filling up: the web UI warns admins to raise RAM_CACHE_SIZE.
            "ram": ram_usage(),
            "engine_up": self.liq_up and cs.liq is not None,
            "station": station.model_dump() if station else None,
            # The channel's stations, for a picker in the app.
            "stations": [
                {"id": s.id, "name": s.name, "kind": s.kind, "playlist": s.playlist, "art": self.art.path(s.image)}
                for s in ch.stations
            ],
            "current_station": ch.current_station,
            "next_program": next_program(ch, self.now()),
            "queue": [i.public() for i in sorted(self.items, key=lambda i: (-i.priority, i.arrived)) if i.channel == ch.id],
            "sources": self.source_state,
            # Live videos and failed downloads that cut in once Invidious can play them.
            "waiting": self.waiting_for(ch),
            "spots": self.spot_status(ch),
            "images": self.config_images(),
            "image_errors": {u: e for u in self.config_images() if (e := self.art.error(u))},
            "programs": self.program_status(ch),
            "windows": self.open_windows(ch),
            "listeners": cs.listeners,
            "quiet_until": cs.quiet_until if cs.quiet_until > time.time() else None,
            "history": [{**h, "art": self.art.path(h["art"])} for h in self.store.history(25, ch.id)],
            "time": self.now().isoformat(),
        }

    def widget(self, cid: str | None) -> dict:
        """Flat summary for dashboards such as Homepage's customapi widget."""
        ch = self.config.channel(cid) or self.config.channel(None)
        st = self.status(cid)
        now = st["now"]
        labels = {"cutin": "Cut-in", "program": "Program", "station": "Radio", "silence": "Silence", "offline": "Offline"}
        title = " - ".join(x for x in (now.get("artist"), now.get("title")) if x)
        mine = [i for i in self.items if ch and i.channel == ch.id]
        queue = [i for i in mine if i.state not in ("playing", "failed")]
        last = next((i for i in mine if i.state == "playing"), None)
        if last:
            last_cutin = {"source": last.source_name, "title": last.title, "ts": last.pushed_at}
        else:
            last_cutin = self.store.last_cutin(ch.id if ch else None)
        tz = self.config.settings.tz
        nxt = st.get("next_program")
        return {
            "status": labels.get(now["mode"], now["mode"]),
            "on_air": now.get("source") or "",
            "title": title or now.get("source") or "",
            "queue": len(queue),
            "downloading": sum(1 for i in queue if i.state in ("waiting", "downloading")),
            "listeners": st.get("listeners"),
            "last_cutin": f"{last_cutin['source']}: {last_cutin['title']}" if last_cutin else "",
            "last_cutin_at": datetime.fromtimestamp(last_cutin["ts"], tz).isoformat() if last_cutin else None,
            "next_program": nxt["name"] if nxt else "",
            "next_program_at": nxt["start"] if nxt else None,
            "station": ch.name if ch else self.config.settings.station_name,
        }
