"""Spiele ohne Neu-Download auf ein anderes Laufwerk umziehen (Steam und Epic).

Ablauf von ``execute``:
  1. Kopieren in temporäre Ordner am Ziel (bzw. Umbenennen auf demselben Laufwerk),
     danach Prüfung von Dateianzahl und Bytes.
  2. Launcher-Metadaten anpassen (mit Sicherungskopie).
  3. Quelle zuerst in ``.gw_alt_*`` umbenennen (scheitert z. B. bei gesperrten Dateien
     → kompletter Rollback), dann löschen.
Jeder Fehler in Schritt 1–3a rollt alles zurück. Erst wenn das Ziel vollständig und die
Metadaten umgestellt sind, wird die Quelle entfernt.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import vdf
from .library import Game, SteamInstall, epic_launcher_dat
from .util import data_dir, dir_size, fmt_size, running_processes

SAFETY_MARGIN = 2 * 1024 ** 3   # 2 GB Reserve auf dem Ziellaufwerk
STEAM_PROCS = {"steam.exe", "steamwebhelper.exe"}
EPIC_PROCS = {"epicgameslauncher.exe"}

Progress = Callable[[int, int, str], None]


class MoveError(Exception):
    pass


class Cancelled(MoveError):
    pass


@dataclass
class Item:
    src: Path
    dst: Path
    is_dir: bool
    size: int = 0
    files: int = 0


@dataclass
class MetaEdit:
    path: Path
    description: str
    apply: Callable[[str], str]   # alter Text -> neuer Text


@dataclass
class MovePlan:
    game: Game
    items: list[Item] = field(default_factory=list)
    edits: list[MetaEdit] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    same_volume: bool = False
    target_free: int = 0
    block_procs: set[str] = field(default_factory=set)
    backup_root: Path = field(default_factory=lambda: data_dir() / "backups")

    @property
    def total_bytes(self) -> int:
        return sum(i.size for i in self.items)

    @property
    def total_files(self) -> int:
        return sum(i.files for i in self.items)

    @property
    def ok(self) -> bool:
        return not self.blockers

    def describe(self) -> str:
        lines = [f"Spiel: {self.game.name} ({self.game.platform})",
                 f"Umfang: {fmt_size(self.total_bytes)} in {self.total_files} Dateien",
                 "Modus: " + ("Umbenennen auf demselben Laufwerk (schnell)" if self.same_volume
                              else "Kopieren + prüfen, danach Quelle löschen"),
                 f"Freier Platz am Ziel: {fmt_size(self.target_free)}", "", "Verschoben wird:"]
        for i in self.items:
            lines.append(f"  {i.src}\n    → {i.dst}")
        if self.edits:
            lines.append("")
            lines.append("Angepasste Launcher-Dateien (mit Sicherung):")
            for e in self.edits:
                lines.append(f"  {e.path}: {e.description}")
        if self.warnings:
            lines.append("")
            lines += ["Hinweis: " + w for w in self.warnings]
        if self.blockers:
            lines.append("")
            lines += ["NICHT MÖGLICH: " + b for b in self.blockers]
        return "\n".join(lines)


# ------------------------------------------------------------------ Planung
def _volume(p: Path) -> str:
    p = Path(os.path.abspath(p))
    return os.path.normcase(p.drive or p.anchor)


def _existing_ancestor(p: Path) -> Path:
    p = Path(os.path.abspath(p))
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p))).rstrip("\\/")


def _finish_plan(plan: MovePlan, target: Path, procs: set[str]):
    src_vol = _volume(plan.items[0].src) if plan.items else ""
    plan.same_volume = src_vol == _volume(target)
    try:
        plan.target_free = shutil.disk_usage(_existing_ancestor(target)).free
    except OSError as e:
        plan.blockers.append(f"Ziel nicht lesbar: {e}")
    for it in plan.items:
        if not it.src.exists():
            plan.blockers.append(f"Quelle fehlt: {it.src}")
            continue
        if it.is_dir:
            it.size, it.files = dir_size(it.src)
        else:
            it.size, it.files = it.src.stat().st_size, 1
        if it.dst.exists():
            plan.blockers.append(f"Am Ziel existiert bereits: {it.dst}")
    if not plan.same_volume and plan.total_bytes + SAFETY_MARGIN > plan.target_free:
        plan.blockers.append(
            f"Zu wenig Platz: benötigt {fmt_size(plan.total_bytes)} + {fmt_size(SAFETY_MARGIN)} Reserve, "
            f"frei {fmt_size(plan.target_free)}")
    plan.block_procs = procs
    running = procs & running_processes()
    if running:
        plan.blockers.append("Bitte zuerst beenden: " + ", ".join(sorted(running)))


def plan_steam_move(game: Game, target_lib: Path, steam: SteamInstall) -> MovePlan:
    plan = MovePlan(game)
    src_lib = game.library
    target_lib = Path(target_lib)
    if _norm(src_lib) == _norm(target_lib):
        plan.blockers.append("Quelle und Ziel sind dieselbe Bibliothek.")
    if not any(_norm(target_lib) == _norm(l) for l in steam.libraries()):
        plan.blockers.append("Ziel ist keine Steam-Bibliothek. Lege sie zuerst in Steam an "
                             "(Einstellungen → Speicher → Laufwerk hinzufügen).")
    if game.extra.get("update_pending"):
        plan.warnings.append("Für das Spiel steht ein Update/Download aus – Steam setzt es nach dem Umzug fort.")
    sa, ta = src_lib / "steamapps", target_lib / "steamapps"
    installdir = game.extra.get("installdir") or game.install_dir.name
    if not installdir:
        plan.blockers.append("Installationsordner unbekannt.")
    plan.items.append(Item(sa / "common" / installdir, ta / "common" / installdir, True))
    ws = sa / "workshop" / "content" / game.id
    if ws.is_dir():
        plan.items.append(Item(ws, ta / "workshop" / "content" / game.id, True))
    wacf = sa / "workshop" / f"appworkshop_{game.id}.acf"
    if wacf.is_file():
        plan.items.append(Item(wacf, ta / "workshop" / wacf.name, False))
    plan.items.append(Item(game.manifest, ta / game.manifest.name, False))

    def edit_lf(text: str) -> str:
        data = vdf.loads(text)
        lf = vdf.get_ci(data, "libraryfolders", {})
        src_e = dst_e = None
        for v in lf.values():
            if isinstance(v, dict) and "path" in v:
                if _norm(v["path"]) == _norm(src_lib):
                    src_e = v
                elif _norm(v["path"]) == _norm(target_lib):
                    dst_e = v
        if src_e is None or dst_e is None:
            raise MoveError("Bibliothek in libraryfolders.vdf nicht gefunden")
        size = src_e.get("apps", {}).pop(game.id, str(game.size or 0))
        dst_e.setdefault("apps", {})[game.id] = size
        return vdf.dumps(data)

    for lf in (steam.libraryfolders_vdf, steam.root / "config" / "libraryfolders.vdf"):
        if lf.is_file():
            plan.edits.append(MetaEdit(lf, f"App {game.id} der Zielbibliothek zuordnen", edit_lf))
    _finish_plan(plan, target_lib, STEAM_PROCS)
    return plan


def plan_epic_move(game: Game, target_parent: Path, launcher_dat: Path | None = None) -> MovePlan:
    launcher_dat = launcher_dat or epic_launcher_dat()
    plan = MovePlan(game)
    target_parent = Path(target_parent)
    src = game.install_dir
    dst = target_parent / src.name
    if _norm(src.parent) == _norm(target_parent):
        plan.blockers.append("Quelle und Ziel sind derselbe Ordner.")
    if _norm(dst).startswith(_norm(src) + os.sep):
        plan.blockers.append("Ziel liegt innerhalb des Spielordners.")
    plan.items.append(Item(src, dst, True))
    old, new = str(src), str(dst)

    def fix(p: str) -> str:
        np = _norm(p) if p else ""
        if p and (np == _norm(old) or np.startswith(_norm(old) + os.sep)):
            return new + p[len(old):]
        return p

    def edit_item(text: str) -> str:
        d = json.loads(text)
        for k in ("InstallLocation", "ManifestLocation", "StagingLocation"):
            if k in d:
                d[k] = fix(d[k])
        return json.dumps(d, indent="\t", ensure_ascii=False)

    def edit_dat(text: str) -> str:
        d = json.loads(text)
        for e in d.get("InstallationList", []):
            if _norm(e.get("InstallLocation", "")) == _norm(old):
                e["InstallLocation"] = new
        return json.dumps(d, indent="\t", ensure_ascii=False)

    plan.edits.append(MetaEdit(game.manifest, "InstallLocation/ManifestLocation/StagingLocation", edit_item))
    if launcher_dat.is_file():
        plan.edits.append(MetaEdit(launcher_dat, "InstallLocation", edit_dat))
    _finish_plan(plan, target_parent, EPIC_PROCS)
    return plan


# ------------------------------------------------------------------ Ausführung
class _Copier:
    def __init__(self, total: int, progress: Progress | None, cancel: threading.Event | None):
        self.done = 0
        self.total = total
        self.progress = progress
        self.cancel = cancel
        self._last = 0.0

    def tick(self, n: int, text: str, force=False):
        self.done += n
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled("Abgebrochen")
        now = time.monotonic()
        if self.progress and (force or now - self._last > 0.1):
            self._last = now
            self.progress(self.done, self.total, text)

    def copy_file(self, src: Path, dst: Path):
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            while True:
                buf = fi.read(8 * 1024 * 1024)
                if not buf:
                    break
                fo.write(buf)
                self.tick(len(buf), src.name)
        shutil.copystat(src, dst, follow_symlinks=False)

    def copy_tree(self, src: Path, dst: Path):
        dst.mkdir(parents=True)
        for e in os.scandir(src):
            s, d = Path(e.path), dst / e.name
            if e.is_symlink():
                raise MoveError(f"Verknüpfung im Spielordner wird nicht unterstützt: {s}")
            if e.is_dir():
                self.copy_tree(s, d)
            else:
                self.copy_file(s, d)


def _rmtree(p: Path):
    def onerr(func, path, exc):
        os.chmod(path, 0o666)
        func(path)
    if p.is_dir():
        shutil.rmtree(p, onexc=onerr)
    elif p.exists():
        p.unlink()


def _tmp_name(p: Path, tag: str) -> Path:
    return p.with_name(f".gw_{tag}_{p.name}")


def execute(plan: MovePlan, progress: Progress | None = None,
            cancel: threading.Event | None = None, log: Callable[[str], None] = print) -> list[str]:
    """Führt den Plan aus. Gibt Hinweise zurück (z. B. nicht löschbare Reste).
    Wirft MoveError nach vollständigem Rollback."""
    if not plan.ok:
        raise MoveError("Plan hat Blocker:\n" + "\n".join(plan.blockers))
    running = plan.block_procs & running_processes()
    if running:
        raise MoveError("Bitte zuerst beenden: " + ", ".join(sorted(running)))

    copier = _Copier(plan.total_bytes, progress, cancel)
    undo: list[tuple[str, Path, Path | None]] = []   # (Art, a, b)
    backup_dir = plan.backup_root / time.strftime("%Y%m%d-%H%M%S")
    leftovers: list[str] = []

    def rollback():
        log("Fehler – rolle zurück …")
        for kind, a, b in reversed(undo):
            try:
                if kind == "rename":          # a wurde nach b umbenannt
                    if b.exists() and not a.exists():
                        os.replace(b, a)
                elif kind == "created":       # a wurde neu angelegt
                    _rmtree(a)
                elif kind == "edit":          # a aus Sicherung b wiederherstellen
                    shutil.copy2(b, a)
                log(f"  zurückgerollt: {a}")
            except OSError as e:
                log(f"  ROLLBACK-FEHLER bei {a}: {e}")
                leftovers.append(f"Rollback unvollständig: {a} ({e})")

    try:
        # 1. Daten ans Ziel bringen
        for it in plan.items:
            it.dst.parent.mkdir(parents=True, exist_ok=True)
            if plan.same_volume:
                log(f"Benenne um: {it.src} → {it.dst}")
                os.rename(it.src, it.dst)
                undo.append(("rename", it.src, it.dst))
                copier.tick(it.size, it.dst.name, force=True)
                continue
            tmp = _tmp_name(it.dst, "neu")
            if tmp.exists():
                _rmtree(tmp)       # Rest eines abgebrochenen Laufs
            log(f"Kopiere: {it.src} → {it.dst}")
            undo.append(("created", tmp, None))
            if it.is_dir:
                copier.copy_tree(it.src, tmp)
                size, files = dir_size(tmp)
            else:
                copier.copy_file(it.src, tmp)
                size, files = tmp.stat().st_size, 1
            if (size, files) != (it.size, it.files):
                raise MoveError(f"Prüfung fehlgeschlagen für {it.dst}: {files}/{it.files} Dateien, "
                                f"{size}/{it.size} Bytes")
            os.rename(tmp, it.dst)
            undo[-1] = ("created", it.dst, None)

        # 2. Metadaten
        for ed in plan.edits:
            backup_dir.mkdir(parents=True, exist_ok=True)
            bak = backup_dir / (ed.path.name + f".{len(undo)}.bak")
            shutil.copy2(ed.path, bak)
            text = ed.path.read_text(encoding="utf-8-sig")
            new = ed.apply(text)
            tmp = ed.path.with_name(ed.path.name + ".gwtmp")
            tmp.write_text(new, encoding="utf-8")
            undo.append(("edit", ed.path, bak))
            os.replace(tmp, ed.path)
            log(f"Angepasst: {ed.path} (Sicherung: {bak})")

        # 3a. Quelle beiseitelegen (prüft gleichzeitig, dass nichts gesperrt ist)
        old_paths = []
        if not plan.same_volume:
            for it in plan.items:
                old = _tmp_name(it.src, "alt")
                if old.exists():
                    _rmtree(old)
                os.rename(it.src, old)
                undo.append(("rename", it.src, old))
                old_paths.append(old)
    except BaseException as e:
        rollback()
        if isinstance(e, MoveError):
            raise
        raise MoveError(f"{type(e).__name__}: {e}") from e

    # 3b. Endgültig löschen – ab hier ist der Umzug konsistent abgeschlossen
    for old in old_paths:
        try:
            log(f"Lösche alte Kopie: {old}")
            _rmtree(old)
        except OSError as e:
            leftovers.append(f"Konnte alte Kopie nicht vollständig löschen: {old} ({e})")
    if progress:
        progress(plan.total_bytes, plan.total_bytes, "fertig")
    log("Umzug abgeschlossen.")
    return leftovers
