"""Controller-/Tastenbelegungs-Profile pro Spiel.

Ein Profil gehört zu einem Spiel und kennt
  * die exe des Spiels (für das automatische Umschalten),
  * eine Liste von Konfigurationsdateien, in denen das Spiel bzw. Steam Input die Belegung speichert.
Eine *Variante* ist ein gespeicherter Stand dieser Dateien (z. B. „Controller“, „Maus+Tastatur“).
Beim Aktivieren wird der aktuelle Stand vorher unter ``_vor_letzter_aktivierung`` gesichert;
schlägt das Zurückschreiben einer Datei fehl, werden alle bereits geschriebenen Dateien zurückgerollt.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from .library import SteamInstall
from .screenshots import safe_name
from .util import data_dir, running_processes

BACKUP_VARIANT = "_vor_letzter_aktivierung"


@dataclass
class Profile:
    game: str
    exe: str = ""
    files: list[str] = field(default_factory=list)
    start_variant: str = ""     # beim Spielstart aktivieren ("" = aus)
    exit_variant: str = ""      # nach Spielende aktivieren ("" = aus)


class ProfileStore:
    def __init__(self, root: Path | None = None):
        self.root = root or data_dir() / "profiles"
        self.index = self.root / "profiles.json"
        self.profiles: list[Profile] = []
        self.load()

    # ---------------------------------------------------------- Verwaltung
    def load(self):
        try:
            raw = json.loads(self.index.read_text(encoding="utf-8"))
            self.profiles = [Profile(**p) for p in raw]
        except (OSError, ValueError, TypeError):
            self.profiles = []

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(p) for p in self.profiles], indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.index)

    def get(self, game: str) -> Profile | None:
        return next((p for p in self.profiles if p.game == game), None)

    def add(self, p: Profile):
        if self.get(p.game):
            raise ValueError(f"Profil „{p.game}“ existiert bereits")
        self.profiles.append(p)
        self.save()

    def remove(self, game: str):
        """Entfernt nur den Profileintrag; gespeicherte Varianten bleiben auf der Platte."""
        self.profiles = [p for p in self.profiles if p.game != game]
        self.save()

    def game_dir(self, p: Profile) -> Path:
        return self.root / safe_name(p.game)

    def variants(self, p: Profile) -> list[str]:
        d = self.game_dir(p)
        if not d.is_dir():
            return []
        return sorted(x.name for x in d.iterdir() if (x / "manifest.json").is_file())

    def variant_files(self, p: Profile, variant: str) -> list[tuple[Path, Path]]:
        """(gespeicherte Datei, Originalpfad)"""
        vd = self.game_dir(p) / variant
        man = json.loads((vd / "manifest.json").read_text(encoding="utf-8"))
        return [(vd / e["stored"], Path(e["original"])) for e in man["files"]]

    # ---------------------------------------------------------- Varianten
    def save_variant(self, p: Profile, variant: str) -> int:
        if not p.files:
            raise ValueError("Dem Profil sind keine Dateien zugeordnet.")
        variant = safe_name(variant)
        vd = self.game_dir(p) / variant
        tmp = vd.with_name(vd.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        entries = []
        try:
            for i, f in enumerate(p.files):
                src = Path(f)
                if not src.is_file():
                    raise FileNotFoundError(f"Datei fehlt: {src}")
                stored = f"{i:02d}_{src.name}"
                shutil.copy2(src, tmp / stored)
                entries.append({"stored": stored, "original": str(src)})
            (tmp / "manifest.json").write_text(
                json.dumps({"files": entries, "saved": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2,
                           ensure_ascii=False), encoding="utf-8")
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        if vd.exists():
            shutil.rmtree(vd)
        tmp.rename(vd)
        return len(entries)

    def delete_variant(self, p: Profile, variant: str):
        shutil.rmtree(self.game_dir(p) / variant)

    def activate(self, p: Profile, variant: str) -> int:
        files = self.variant_files(p, variant)
        for stored, _ in files:
            if not stored.is_file():
                raise FileNotFoundError(f"Gespeicherte Datei fehlt: {stored}")
        # aktuellen Stand sichern (nur Dateien, die existieren)
        if variant != BACKUP_VARIANT:
            existing = [str(o) for _, o in files if o.is_file()]
            if existing:
                tmp_p = Profile(p.game, files=existing)
                self.save_variant(tmp_p, BACKUP_VARIANT)
        written: list[tuple[Path, Path | None]] = []   # (Ziel, Kopie des alten Inhalts)
        scratch = self.game_dir(p) / ".rollback"
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            for i, (stored, orig) in enumerate(files):
                old = None
                if orig.is_file():
                    old = scratch / f"{i:02d}"
                    shutil.copy2(orig, old)
                orig.parent.mkdir(parents=True, exist_ok=True)
                written.append((orig, old))
                shutil.copy2(stored, orig)
        except BaseException:
            for orig, old in reversed(written):
                try:
                    if old is not None:
                        shutil.copy2(old, orig)
                    elif orig.exists():
                        orig.unlink()
                except OSError:
                    pass
            raise
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return len(files)


def steam_input_files(steam: SteamInstall, appid: str) -> list[Path]:
    """Steam-Input-Konfigurationen eines Spiels (alle Konten)."""
    out: list[Path] = []
    if not steam.available:
        return out
    base = steam.root / "steamapps" / "common" / "Steam Controller Configs"
    for uid in steam.users():
        d = base / uid / "config" / str(appid)
        if d.is_dir():
            out += sorted(x for x in d.glob("*.vdf") if x.is_file())
    return out


class AutoSwitcher:
    """Hintergrund-Thread: aktiviert Varianten, wenn die exe eines Profils startet bzw. endet."""

    def __init__(self, store: ProfileStore, log: Callable[[str], None], interval: float = 2.0):
        self.store = store
        self.log = log
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._was_running: set[str] = set()

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.active:
            return
        self._stop.clear()
        self._was_running = running_processes()   # bereits laufende Spiele nicht umschalten
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.log("Automatisches Umschalten aktiv.")

    def stop(self):
        self._stop.set()
        self.log("Automatisches Umschalten gestoppt.")

    def check_once(self, now: set[str] | None = None):
        now = running_processes() if now is None else now
        for p in list(self.store.profiles):
            exe = p.exe.lower()
            if not exe:
                continue
            started = exe in now and exe not in self._was_running
            ended = exe not in now and exe in self._was_running
            variant = p.start_variant if started else p.exit_variant if ended else ""
            if variant:
                try:
                    n = self.store.activate(p, variant)
                    self.log(f"{p.game}: {'Start' if started else 'Ende'} erkannt → „{variant}“ aktiviert ({n} Dateien)")
                except Exception as e:   # noqa: BLE001 – im Hintergrund nur protokollieren
                    self.log(f"{p.game}: Umschalten auf „{variant}“ fehlgeschlagen: {e}")
        self._was_running = now

    def _run(self):
        while not self._stop.wait(self.interval):
            self.check_once()
