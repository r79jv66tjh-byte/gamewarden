"""Screenshots aus Steam, NVIDIA, Xbox Game Bar und eigenen Ordnern finden und nach Spiel sortieren."""
from __future__ import annotations

import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .library import SteamInstall

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".jxr", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".mkv", ".webm"}
UNKNOWN = "Unbekannt"
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# "Spielname 2024-05-01 12-00-00.png" / "Spielname 05_01_2024 12_00_00.png" / "Spielname Screenshot 2024.05.01 - ..."
_DATE = r"(?:\d{4}[-_.]\d{1,2}[-_.]\d{1,2}|\d{1,2}[-_.]\d{1,2}[-_.]\d{4})"
_GAMEBAR = re.compile(r"^(?P<game>.*?\D)[\s_-]*(?:Screenshot\s+)?" + _DATE)


@dataclass
class Shot:
    path: Path
    game: str
    source: str        # Steam / NVIDIA / Xbox Game Bar / Ordner
    size: int
    mtime: float
    copy_only: bool = False   # Steam verwaltet seine Screenshots selbst → nie verschieben


@dataclass
class Source:
    name: str
    path: Path
    kind: str          # "steam" | "nvidia" | "gamebar" | "folder"
    game: str | None = None   # fester Spielname für eigene Ordner


def safe_name(name: str) -> str:
    name = _BAD_CHARS.sub("", name).strip(" .")
    return name or UNKNOWN


def default_sources(steam: SteamInstall | None = None, extra: list[dict] | None = None) -> list[Source]:
    home = Path.home()
    out: list[Source] = []
    steam = steam or SteamInstall()
    if steam.available:
        for uid in steam.users():
            p = steam.root / "userdata" / uid / "760" / "remote"
            if p.is_dir():
                out.append(Source(f"Steam ({uid})", p, "steam"))
    for p in (home / "Videos" / "NVIDIA", home / "Pictures" / "NVIDIA"):
        if p.is_dir():
            out.append(Source("NVIDIA", p, "nvidia"))
    p = home / "Videos" / "Captures"
    if p.is_dir():
        out.append(Source("Xbox Game Bar", p, "gamebar"))
    p = home / "Pictures" / "Screenshots"
    if p.is_dir():
        out.append(Source("Windows-Screenshots", p, "gamebar"))
    for e in extra or []:
        p = Path(e["path"])
        if p.is_dir():
            out.append(Source(e.get("name") or p.name, p, "folder", e.get("game") or None))
    return out


def _guess_from_filename(stem: str) -> str | None:
    for rx in [_GAMEBAR]:
        m = rx.match(stem)
        if m:
            g = m.group("game").strip(" _-")
            if g and g.lower() not in ("screenshot", "screenshots", "bildschirmfoto", "capture"):
                return g
    return None


def scan(sources: list[Source], steam_names: dict[str, str] | None = None,
         include_videos: bool = False) -> list[Shot]:
    steam_names = steam_names or {}
    exts = IMAGE_EXT | (VIDEO_EXT if include_videos else set())
    shots: list[Shot] = []
    for src in sources:
        for root, dirs, files in os.walk(src.path):
            r = Path(root)
            if src.kind == "steam":
                dirs[:] = [d for d in dirs if d != "thumbnails"]
            for f in files:
                p = r / f
                if p.suffix.lower() not in exts:
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                rel = p.relative_to(src.path).parts
                game, copy_only = UNKNOWN, False
                if src.kind == "steam":
                    copy_only = True
                    appid = rel[0] if rel else ""
                    if rel[1:2] != ("screenshots",):
                        continue
                    game = steam_names.get(appid) or f"Steam-App {appid}"
                elif src.kind == "nvidia":
                    game = rel[0] if len(rel) > 1 else (_guess_from_filename(p.stem) or UNKNOWN)
                elif src.kind == "gamebar":
                    game = _guess_from_filename(p.stem) or UNKNOWN
                else:
                    game = src.game or (rel[0] if len(rel) > 1 else None) or _guess_from_filename(p.stem) \
                        or src.name
                shots.append(Shot(p, safe_name(game), src.name, st.st_size, st.st_mtime, copy_only))
    return shots


@dataclass
class Op:
    shot: Shot
    dst: Path
    action: str     # "kopieren" | "verschieben" | "überspringen"
    note: str = ""


def plan_sort(shots: list[Shot], target: Path, move: bool) -> list[Op]:
    """Erstellt eine Vorschau. Gleiche Datei (Name+Größe) am Ziel wird übersprungen,
    Namenskonflikte bekommen einen Zähler."""
    ops: list[Op] = []
    taken: set[str] = set()
    target = Path(target)
    for s in shots:
        try:
            if s.path.resolve().is_relative_to(target.resolve()):
                continue   # liegt schon im Zielordner
        except OSError:
            pass
        folder = target / s.game
        dst = folder / s.path.name
        if dst.exists() and dst.stat().st_size == s.size:
            ops.append(Op(s, dst, "überspringen", "schon vorhanden"))
            continue
        n = 1
        while dst.exists() or str(dst).lower() in taken:
            dst = folder / f"{s.path.stem}_{n}{s.path.suffix}"
            n += 1
        taken.add(str(dst).lower())
        act = "verschieben" if move and not s.copy_only else "kopieren"
        note = "Steam-Screenshot: nur Kopie" if move and s.copy_only else ""
        ops.append(Op(s, dst, act, note))
    return ops


def execute_sort(ops: list[Op], progress: Callable[[int, int], None] | None = None,
                 cancel: threading.Event | None = None) -> tuple[int, int]:
    """Führt die Vorschau aus. Bei einem Fehler wird alles bisher Erledigte zurückgenommen.
    Rückgabe: (kopiert, verschoben)."""
    done: list[Op] = []
    todo = [o for o in ops if o.action != "überspringen"]
    created_dirs: list[Path] = []
    started: Op | None = None
    try:
        for i, op in enumerate(todo):
            if cancel is not None and cancel.is_set():
                raise RuntimeError("Abgebrochen")
            if not op.dst.parent.exists():
                op.dst.parent.mkdir(parents=True)
                created_dirs.append(op.dst.parent)
            if op.dst.exists():
                raise FileExistsError(f"Ziel existiert inzwischen: {op.dst}")
            started = op
            if op.action == "verschieben":
                shutil.move(str(op.shot.path), str(op.dst))
            else:
                shutil.copy2(op.shot.path, op.dst)
            done.append(op)
            started = None
            if progress:
                progress(i + 1, len(todo))
    except BaseException:
        if started is not None and started.action == "kopieren" and started.dst.exists():
            try:
                started.dst.unlink()    # halb kopierte Datei
            except OSError:
                pass
        for op in reversed(done):
            try:
                if op.action == "verschieben":
                    shutil.move(str(op.dst), str(op.shot.path))
                else:
                    op.dst.unlink()
            except OSError:
                pass
        for d in reversed(created_dirs):
            try:
                d.rmdir()
            except OSError:
                pass
        raise
    return (sum(o.action == "kopieren" for o in done), sum(o.action == "verschieben" for o in done))
