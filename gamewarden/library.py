"""Einlesen der installierten Spiele aus Steam und Epic (nur lesend)."""
from __future__ import annotations

import json
import os
import winreg
from dataclasses import dataclass, field
from pathlib import Path

from . import vdf
from .util import days_since

EPIC_MANIFESTS = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
EPIC_LAUNCHER_DAT = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Epic" / "UnrealEngineLauncher" / "LauncherInstalled.dat"


# Umgebungsvariablen GAMEWARDEN_STEAM_ROOT / _EPIC_MANIFESTS / _EPIC_DAT lenken auf Testdaten um.
def epic_manifest_dir() -> Path:
    return Path(os.environ.get("GAMEWARDEN_EPIC_MANIFESTS") or EPIC_MANIFESTS)


def epic_launcher_dat() -> Path:
    return Path(os.environ.get("GAMEWARDEN_EPIC_DAT") or EPIC_LAUNCHER_DAT)

# Steam-Einträge, die keine Spiele sind
STEAM_TOOLS = {"228980"}  # Steamworks Common Redistributables


@dataclass
class Game:
    platform: str            # "Steam" | "Epic"
    id: str                  # Steam-AppID bzw. Epic-AppName
    name: str
    install_dir: Path
    size: int | None = None
    last_played: int | None = None   # Unix-Zeit
    playtime_min: int | None = None
    library: Path | None = None      # Steam: Bibliotheksordner; Epic: übergeordneter Ordner
    manifest: Path | None = None     # appmanifest_*.acf bzw. *.item
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.id}"

    @property
    def drive(self) -> str:
        return self.install_dir.drive.upper() or "?"

    @property
    def days_unplayed(self) -> int | None:
        return days_since(self.last_played)


# ------------------------------------------------------------------ Steam
def find_steam_path() -> Path | None:
    env = os.environ.get("GAMEWARDEN_STEAM_ROOT")
    if env:
        return Path(env) if (Path(env) / "steamapps").is_dir() else None
    for hive, sub, val in (
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
    ):
        try:
            with winreg.OpenKey(hive, sub) as k:
                p = Path(winreg.QueryValueEx(k, val)[0])
                if (p / "steamapps").is_dir():
                    return p
        except OSError:
            continue
    default = Path(r"C:\Program Files (x86)\Steam")
    return default if (default / "steamapps").is_dir() else None


class SteamInstall:
    def __init__(self, root: Path | None = None):
        self.root = root or find_steam_path()

    @property
    def available(self) -> bool:
        return self.root is not None

    @property
    def libraryfolders_vdf(self) -> Path:
        return self.root / "steamapps" / "libraryfolders.vdf"

    def libraries(self) -> list[Path]:
        if not self.root:
            return []
        libs: list[Path] = []
        try:
            data = vdf.get_ci(vdf.load(self.libraryfolders_vdf), "libraryfolders", {})
            for v in data.values():
                if isinstance(v, dict) and v.get("path"):
                    libs.append(Path(v["path"]))
                elif isinstance(v, str) and os.path.isdir(v):   # altes Format
                    libs.append(Path(v))
        except OSError:
            pass
        if not any(_same(p, self.root) for p in libs):
            libs.insert(0, self.root)
        return [p for p in libs if (p / "steamapps").is_dir()]

    def users(self) -> list[str]:
        ud = self.root / "userdata" if self.root else None
        if not ud or not ud.is_dir():
            return []
        return [p.name for p in ud.iterdir() if p.is_dir() and p.name.isdigit() and p.name != "0"]

    def playtimes(self) -> dict[str, tuple[int, int]]:
        """AppID -> (LastPlayed, Playtime in Minuten), über alle Steam-Konten zusammengeführt."""
        out: dict[str, tuple[int, int]] = {}
        for uid in self.users():
            cfg = self.root / "userdata" / uid / "config" / "localconfig.vdf"
            try:
                data = vdf.load(cfg)
            except (OSError, vdf.VdfError):
                continue
            apps = _dig(data, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps") or {}
            for appid, info in apps.items():
                if not isinstance(info, dict):
                    continue
                lp = _int(vdf.get_ci(info, "LastPlayed"))
                pt = _int(vdf.get_ci(info, "Playtime"))
                old = out.get(appid, (0, 0))
                out[appid] = (max(old[0], lp), max(old[1], pt))
        return out

    def games(self) -> list[Game]:
        times = self.playtimes()
        result: list[Game] = []
        for lib in self.libraries():
            sa = lib / "steamapps"
            for acf in sa.glob("appmanifest_*.acf"):
                try:
                    st = vdf.get_ci(vdf.load(acf), "AppState", {})
                except (OSError, vdf.VdfError):
                    continue
                appid = str(st.get("appid", acf.stem.split("_", 1)[-1]))
                if appid in STEAM_TOOLS:
                    continue
                installdir = st.get("installdir", "")
                lp, pt = times.get(appid, (0, 0))
                lp = max(lp, _int(st.get("LastPlayed")))
                g = Game(
                    platform="Steam",
                    id=appid,
                    name=st.get("name") or f"Steam-App {appid}",
                    install_dir=sa / "common" / installdir,
                    size=_int(st.get("SizeOnDisk")) or None,
                    last_played=lp or None,
                    playtime_min=pt or None,
                    library=lib,
                    manifest=acf,
                    extra={
                        "installdir": installdir,
                        "state_flags": _int(st.get("StateFlags")),
                        "update_pending": _int(st.get("BytesToDownload")) > _int(st.get("BytesDownloaded")),
                    },
                )
                result.append(g)
        return result

    def app_names(self) -> dict[str, str]:
        """AppID -> Name für alle installierten Spiele (für Screenshots)."""
        names = {}
        for lib in self.libraries():
            for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
                try:
                    st = vdf.get_ci(vdf.load(acf), "AppState", {})
                    names[str(st.get("appid"))] = st.get("name", "")
                except (OSError, vdf.VdfError):
                    pass
        return names


# ------------------------------------------------------------------ Epic
def epic_games(manifest_dir: Path | None = None) -> list[Game]:
    manifest_dir = manifest_dir or epic_manifest_dir()
    result: list[Game] = []
    if not manifest_dir.is_dir():
        return result
    for item in manifest_dir.glob("*.item"):
        try:
            d = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("bIsIncompleteInstall"):
            continue
        cats = d.get("AppCategories") or []
        if cats and "games" not in cats:
            continue
        loc = d.get("InstallLocation")
        if not loc:
            continue
        p = Path(loc)
        result.append(Game(
            platform="Epic",
            id=d.get("AppName", item.stem),
            name=d.get("DisplayName") or d.get("AppName", item.stem),
            install_dir=p,
            size=_int(d.get("InstallSize")) or None,
            library=p.parent,
            manifest=item,
            extra={"exe": d.get("LaunchExecutable", "")},
        ))
    return result


def all_games(steam: SteamInstall | None = None) -> list[Game]:
    steam = steam or SteamInstall()
    games = steam.games() if steam.available else []
    games += epic_games()
    return games


# ------------------------------------------------------------------ Analyse
def suggestions(games: list[Game], unplayed_days: int = 365, big_gb: float = 20.0) -> dict[str, str]:
    """Spiel-Key -> Begründung, warum es ein Deinstallations-Kandidat ist."""
    out = {}
    big = big_gb * 1024 ** 3
    for g in games:
        reasons = []
        d = g.days_unplayed
        if g.platform == "Steam":
            if d is None:
                reasons.append("nie gespielt")
            elif d >= unplayed_days:
                reasons.append(f"seit {d} Tagen nicht gespielt")
        if reasons and big_gb > 0:
            if not g.size or g.size < big:
                continue
            reasons.append(f"≥ {big_gb:g} GB")
        if reasons:
            out[g.key] = ", ".join(reasons)
    return out


# ------------------------------------------------------------------ intern
def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _dig(d: dict, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = vdf.get_ci(d, k)
    return d


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
