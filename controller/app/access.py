"""Who may do what: the admin, users with channels of their own, and app devices.

- The admin (the .env login, a user with the role "admin", an admin device,
  or anyone when no UI_PASSWORD is set) may do everything.
- A user controls the channels they own and listens to everyone's public
  channels. Video channels and podcasts are per person: a user sees and
  cuts in only the ones they added. Saving the config as a user only changes
  their own channels and sources; everything else is kept as it is.
- A member device is limited to its channels; one paired by a user acts as
  that user (and is further limited to its channels, if it has any).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from . import media
from .config import Channel, Config
from .devices import Device
from .users import User


@dataclass
class Access:
    admin: bool = True
    user: User | None = None
    device: Device | None = None
    anonymous: bool = False  # not logged in, on a page that needs no login

    @property
    def uid(self) -> str:
        return self.user.id if self.user else ""

    def _device_ok(self, ch: Channel) -> bool:
        return self.device is None or self.device.may_use(ch.id)

    def can_control(self, ch: Channel) -> bool:
        """Change it: stations, skip, cut-ins, wind down; for users also its program."""
        if self.admin:
            return True
        if self.anonymous:
            return False
        if self.user is None:  # a member device paired by the admin
            return self._device_ok(ch)
        return ch.owner == self.user.id and self._device_ok(ch)

    def can_listen(self, ch: Channel) -> bool:
        """Listen and see what is on (and its schedule)."""
        if self.can_control(ch):
            return True
        return self.user is not None and ch.public and self._device_ok(ch)

    def owns_source(self, src) -> bool:
        return self.admin or src.owner == self.uid


ADMIN = Access()
# Pages that need no login (playlists, the now-playing picture, pairing) run as this
# when a password is set, so they get no rights unless they ask for them explicitly.
ANON = Access(admin=False, anonymous=True)

# Who is behind the current request; the admin when no password is set.
current: ContextVar[Access] = ContextVar("rradio_access", default=ADMIN)


def visible_config(cfg: Config, acc: Access) -> dict:
    """The config as this person sees it: other people's private channels and
    other people's video channels and podcasts left out."""
    data = cfg.masked()
    if not acc.admin:
        data["channels"] = [d for c, d in zip(cfg.channels, data["channels"]) if acc.can_listen(c)]
        for key in ("video", "podcasts"):
            data[key] = [s for s in data[key] if s["owner"] == acc.uid]
    return data


def merge_user_config(cur: Config, body: dict, user: User) -> dict:
    """A user's save of the whole config, applied only to what is theirs: their
    channels (added, changed, removed) and the video channels and podcasts they
    added. Settings and other people's channels and sources stay as they are.
    Raises ValueError for a spot that plays files outside the user's own folder."""
    data = cur.model_dump()

    def merge(old: list[dict], new: list) -> list[dict]:
        new = [x for x in new if isinstance(x, dict)]
        sent = {x.get("id"): x for x in new if x.get("id")}
        known = {x["id"]: x for x in old}
        out = []
        for x in old:
            if x.get("owner", "") != user.id:
                out.append(x)  # someone else's: read-only
            elif x["id"] in sent:
                out.append({**sent[x["id"]], "owner": user.id})
        for x in new:  # added by the user
            if x.get("id") not in known:
                out.append({**x, "owner": user.id})
        return out

    for key in ("video", "podcasts"):
        data[key] = merge(data[key], body.get(key) or [])
    data["channels"] = merge(data["channels"], body.get("channels") or [])
    own_sources = {s.get("id") for key in ("video", "podcasts") for s in data[key] if s.get("owner") == user.id}
    for ch in data["channels"]:
        if ch.get("owner") != user.id:
            continue
        ch["sources"] = [s for s in ch.get("sources") or [] if s in own_sources]
        for spot in ch.get("spots") or []:
            files = spot.get("files") if isinstance(spot, dict) else None
            for f in files or []:
                try:
                    media.resolve_in(str(f), user.folder)
                except ValueError:
                    raise ValueError(f"spots can only play your own files (in {user.folder}), not {f!r}") from None
    return data
