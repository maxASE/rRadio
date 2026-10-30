"""Export and import: one channel with everything it uses, or a full backup.

A channel export is a zip with channel.yaml (the channel, the video
channels and podcasts it cuts in from), the own audio its spots play under
media/, and uploaded pictures under images/. A full backup holds config.yaml
plus all own audio and pictures. The Navidrome password is never exported.
"""

from __future__ import annotations

import io
import re
import time
import zipfile
from pathlib import Path

import yaml

from . import media
from .art import IMAGES_DIR, UPLOADED, image_file
from .config import Config, Channel, dump, new_id

FORMAT = 1
MAX_TOTAL = 2 * 1024**3  # refuse archives that unpack to more than 2 GB
README = """rRadio export. Import it under Settings › Import on another rRadio.
Not included: the Navidrome server, user and password (set them under
Settings on the new instance), history and downloaded clips. Navidrome
stations and programs refer to playlists by name, so the new instance's
Navidrome needs playlists with the same names.
"""


def _images_of(chs: list[Channel]) -> set[str]:
    out = set()
    for ch in chs:
        for x in [*ch.stations, *ch.programs, *ch.spots]:
            if x.image.startswith(UPLOADED) and image_file(x.image[len(UPLOADED):]):
                out.add(x.image[len(UPLOADED):])
    return out


def _add_images(z: zipfile.ZipFile, names: set[str]) -> None:
    for n in sorted(names):
        z.write(IMAGES_DIR / n, f"images/{n}")


def _add_media(z: zipfile.ZipFile, paths: list[Path]) -> None:
    root = media.MEDIA_DIR.resolve()
    for p in sorted(set(paths)):
        z.write(p, "media/" + str(p.resolve().relative_to(root)))


def export_channel(cfg: Config, ch: Channel) -> bytes:
    used = lambda s: ch.wants(s)  # noqa: E731
    doc = {
        "rradio_export": FORMAT,
        "channel": dump(Config(channels=[ch]))["channels"][0],
        "video": [s.model_dump() for s in cfg.video if used(s)],
        "podcasts": [s.model_dump() for s in cfg.podcasts if used(s)],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("channel.yaml", yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
        z.writestr("README.txt", README)
        _add_media(z, [p for s in ch.spots if s.content == "media" for p in media.expand(s.files)])
        _add_images(z, _images_of([ch]))
    return buf.getvalue()


def export_all(cfg: Config) -> bytes:
    data = dump(cfg)
    data["settings"]["navidrome_password"] = ""
    lib = [media.MEDIA_DIR / f["path"] for f in media.library()["files"] if not f["path"].startswith(media.PLAYED + "/")]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("config.yaml", yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
        z.writestr("README.txt", README)
        _add_media(z, lib)
        _add_images(z, {p.name for p in IMAGES_DIR.glob("*") if image_file(p.name)} if IMAGES_DIR.exists() else set())
    return buf.getvalue()


# --- import ----------------------------------------------------------------------


def _open(data: bytes) -> zipfile.ZipFile:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("not a zip file; pick a file exported from rRadio")
    if sum(i.file_size for i in z.infolist()) > MAX_TOTAL:
        raise ValueError("archive too big")
    for i in z.infolist():
        if i.filename.startswith("/") or ".." in Path(i.filename).parts:
            raise ValueError(f"unsafe path in archive: {i.filename}")
    return z


def _write_images(z: zipfile.ZipFile) -> int:
    n = 0
    for i in z.infolist():
        name = i.filename.removeprefix("images/")
        if i.filename.startswith("images/") and re.fullmatch(r"[0-9a-f]{16}\.(png|jpg|gif|webp|svg)", name):
            IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            if not (IMAGES_DIR / name).exists():  # named by content hash
                (IMAGES_DIR / name).write_bytes(z.read(i))
                n += 1
    return n


def _media_members(z: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, str]]:
    return [(i, i.filename.removeprefix("media/")) for i in z.infolist() if i.filename.startswith("media/") and not i.is_dir()]


def _clashes(z: zipfile.ZipFile, prefix: str = "") -> bool:
    """Would any file land on an existing file with other content?"""
    for i, rel in _media_members(z):
        try:
            dest = media.resolve(prefix + rel)
        except ValueError:
            continue
        if dest.exists() and (dest.stat().st_size != i.file_size or dest.read_bytes() != z.read(i)):
            return True
    return False


def _write_media(z: zipfile.ZipFile, prefix: str = "") -> int:
    """Unpack media/ (under `prefix`, if given); returns how many files were written."""
    n = 0
    for i, rel in _media_members(z):
        try:
            dest = media.resolve(prefix + rel)
        except ValueError:
            continue
        data = z.read(i)
        if dest.exists() and dest.read_bytes() == data:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        n += 1
    return n


def _unique(want: str, taken: set[str], sep: str) -> str:
    if want not in taken:
        return want
    k = 2
    while f"{want}{sep}{k}" in taken:
        k += 1
    return f"{want}{sep}{k}"


def import_channel(cfg: Config, z: zipfile.ZipFile, name: str = "", owner: str = "", folder: str = "") -> tuple[Config, str]:
    """Adds the channel (owned by `owner`, a user id; "" = the admin). A user's
    import puts its own audio under their `folder` (e.g. users/anna/)."""
    doc = yaml.safe_load(z.read("channel.yaml")) or {}
    if doc.get("rradio_export") != FORMAT:
        raise ValueError("unknown export format")
    data = cfg.model_dump()

    # Sources: reuse ones this instance already has (same video channel or feed), add the rest.
    remap, added = {}, 0
    for key, match in (("video", "channel_id"), ("podcasts", "url")):
        mine = data[key]
        for s in doc.get(key) or []:
            same = next((x for x in mine if x[match] == s.get(match) and x.get("owner", "") == owner), None)
            if same:
                remap[s["id"]] = same["id"]
            else:
                nid = new_id() if any(x["id"] == s.get("id") for x in mine) or not s.get("id") else s["id"]
                remap[s.get("id")] = nid
                mine.append({**s, "id": nid, "owner": owner})
                added += 1

    ch = dict(doc["channel"])
    ch["name"] = _unique((name or ch.get("name") or "Imported").strip(), {c["name"] for c in data["channels"]}, " ")
    ch["id"] = new_id()
    ch["owner"], ch["public"] = owner, False
    ch["mount"] = _unique(re.sub(r"[^a-z0-9_-]+", "-", ch["name"].lower()).strip("-")[:28] or ch["id"],
                          {c["mount"] for c in data["channels"]}, "-")
    ids = {}
    for key in ("stations", "programs", "spots"):
        for x in ch.get(key) or []:
            ids[x.get("id")] = x["id"] = new_id()
    ch["current_station"] = ids.get(ch.get("current_station"))
    if ch.get("all_sources", True):  # older exports: every source that came with it
        ch["sources"] = [remap.get(s.get("id"), s.get("id")) for key in ("video", "podcasts") for s in doc.get(key) or []]
    else:
        ch["sources"] = [remap.get(s, s) for s in ch.get("sources") or []]
    ch["all_sources"] = False

    Config.model_validate({**data, "channels": [*data["channels"], ch]})  # fail before writing files
    # Own audio keeps its paths, unless a file there differs: then the whole
    # import goes into a folder named after the channel, so spots that play a
    # folder don't mix in this instance's files.
    prefix = folder
    if _clashes(z, prefix):
        prefix += _unique(ch["mount"], {p.name for p in (media.MEDIA_DIR / prefix).glob("*")}, "-") + "/"
    if prefix:
        for s in ch.get("spots") or []:
            s["files"] = [prefix + f for f in s.get("files") or []]
            for f in s["files"] if folder else []:
                try:
                    media.resolve_in(f, folder)
                except ValueError:
                    raise ValueError(f"spot {s.get('name')!r} plays a file outside your folder: {f!r}") from None
    nfiles = _write_media(z, prefix)
    _write_images(z)

    data["channels"].append(ch)
    new = Config.model_validate(data)
    parts = [f"Imported {ch['name']}", f"{len(ch.get('stations') or [])} stations, {len(ch.get('programs') or [])} programs, {len(ch.get('spots') or [])} spots"]
    if added:
        parts.append(f"{added} new sources")
    if nfiles:
        parts.append(f"{nfiles} audio files" + (f" in the folder {prefix}" if prefix else ""))
    return new, ". ".join([parts[0], ", ".join(parts[1:])]) + "."


def restore_all(cfg: Config, z: zipfile.ZipFile, config_path: Path) -> tuple[Config, str]:
    data = yaml.safe_load(z.read("config.yaml")) or {}
    st = data.setdefault("settings", {})
    if not st.get("navidrome_password"):
        st["navidrome_password"] = cfg.settings.navidrome_password
    new = Config.model_validate(data)
    if config_path.exists():  # keep the config this restore replaces
        config_path.with_name(f"config.before-restore-{time.strftime('%Y%m%d-%H%M%S')}.yaml").write_bytes(config_path.read_bytes())
    _write_media(z)
    _write_images(z)
    return new, f"Restored {len(new.channels)} channels, {len(new.video) + len(new.podcasts)} sources and {len(_media_members(z))} audio files."


class NeedsConfirm(Exception):
    """A full backup replaces everything; the caller has to say so."""


def import_zip(cfg: Config, data: bytes, name: str, config_path: Path, replace: bool = False,
               owner: str = "", folder: str = "") -> tuple[Config, str]:
    z = _open(data)
    names = set(z.namelist())
    if "channel.yaml" in names:
        return import_channel(cfg, z, name, owner, folder)
    if "config.yaml" in names:
        if owner:
            raise ValueError("only the admin can restore a full backup")
        if not replace:
            raise NeedsConfirm("This is a full backup. Restoring it replaces all channels, sources and settings.")
        return restore_all(cfg, z, config_path)
    raise ValueError("no channel.yaml or config.yaml in this file")
