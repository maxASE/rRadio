"""Your own audio: jingles, station IDs, ads, announcements.

Files live in MEDIA_DIR (by default /data/media), in folders if you like.
Anything dropped into the `inbox` folder plays once on the first channel
(or on the channel named by a subfolder, e.g. inbox/<channel mount>/) and is
then moved to `played`.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import BinaryIO

MEDIA_DIR = Path(os.environ.get("MEDIA_DIR", "/data/media"))
INBOX = "inbox"
PLAYED = "played"
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav", ".webm", ".mp4"}


def ensure_dirs() -> None:
    for d in (MEDIA_DIR, MEDIA_DIR / INBOX, MEDIA_DIR / PLAYED):
        d.mkdir(parents=True, exist_ok=True)


def _is_audio(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in AUDIO_EXT and not p.name.startswith(".")


def resolve(rel: str) -> Path:
    """A path inside MEDIA_DIR; refuses anything that points outside it."""
    p = (MEDIA_DIR / rel.lstrip("/")).resolve()
    root = MEDIA_DIR.resolve()
    if p != root and root not in p.parents:
        raise ValueError("path outside the media folder")
    return p


def resolve_in(rel: str, folder: str) -> Path:
    """Like resolve(), but the path must also stay inside `folder` (a user's
    users/<name>/). Checked after resolving, so "users/anna/../bob/x.mp3" is refused."""
    p = resolve(rel)
    home = resolve(folder)
    if p != home and home not in p.parents:
        raise ValueError("path outside your folder")
    return p


def library() -> dict:
    """All audio files (outside inbox/) and the folders that hold them."""
    if not MEDIA_DIR.exists():
        return {"files": [], "folders": []}
    files, folders = [], set()
    for p in sorted(MEDIA_DIR.rglob("*")):
        rel = p.relative_to(MEDIA_DIR)
        if rel.parts[0] == INBOX or not _is_audio(p):
            continue
        st = p.stat()
        folder = str(rel.parent) if rel.parent != Path(".") else ""
        if folder:
            folders.add(folder)
        files.append({"path": str(rel), "name": p.name, "folder": folder, "size": st.st_size, "mtime": st.st_mtime})
    return {"files": files, "folders": sorted(folders)}


def expand(entries: list[str]) -> list[Path]:
    """Spot file list to paths: "folder/" stands for every file in that folder."""
    out: list[Path] = []
    for e in entries:
        try:
            p = resolve(e)
        except ValueError:
            continue
        if e.endswith("/") or p.is_dir():
            if p.is_dir():
                out += sorted(x for x in p.rglob("*") if _is_audio(x))
        elif _is_audio(p):
            out.append(p)
    return out


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ()]+", "_", Path(name).name).strip(" .")
    return name or f"upload-{int(time.time())}"


def save_upload(fileobj: BinaryIO, filename: str, folder: str = "") -> str:
    folder = folder.strip().strip("/")
    if Path(filename).suffix.lower() not in AUDIO_EXT:
        raise ValueError(f"not an audio file (allowed: {', '.join(sorted(AUDIO_EXT))})")
    target_dir = resolve(folder) if folder else MEDIA_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _safe_name(filename)
    stem, n = target.stem, 1
    while target.exists():
        target = target.with_name(f"{stem} ({n}){target.suffix}")
        n += 1
    tmp = target.with_name("." + target.name + ".part")
    with tmp.open("wb") as f:
        shutil.copyfileobj(fileobj, f, 1 << 20)
    tmp.replace(target)
    return str(target.relative_to(MEDIA_DIR))


def delete(rel: str) -> None:
    p = resolve(rel)
    if not _is_audio(p):
        raise FileNotFoundError(rel)
    p.unlink()


class Inbox:
    """Finds files dropped into inbox/ once they stopped growing."""

    def __init__(self):
        self._sizes: dict[Path, int] = {}
        self.taken: set[Path] = set()

    def scan(self) -> list[Path]:
        root = MEDIA_DIR / INBOX
        if not root.exists():
            return []
        ready, sizes = [], {}
        for p in root.rglob("*"):
            if not _is_audio(p) or p in self.taken:
                continue
            size = p.stat().st_size
            sizes[p] = size
            if self._sizes.get(p) == size and size > 0:
                ready.append(p)
        self._sizes = sizes
        self.taken.update(ready)
        return sorted(ready, key=lambda p: p.stat().st_mtime)

    def channel_hint(self, p: Path) -> str | None:
        """inbox/<mount>/file.mp3 targets the channel with that stream name."""
        rel = p.relative_to(MEDIA_DIR / INBOX)
        return rel.parts[0] if len(rel.parts) > 1 else None

    def done(self, p: Path) -> None:
        self.taken.discard(p)
        if not p.exists():
            return
        dest = MEDIA_DIR / PLAYED / p.name
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}-{int(time.time())}{dest.suffix}")
        try:
            p.replace(dest)
        except OSError:
            p.unlink(missing_ok=True)
