"""Hilfsfunktionen: Größen, Datumsangaben, Prozesse, Konfiguration."""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "GameWarden"


def data_dir() -> Path:
    """Datenordner (Konfiguration, Profile, Sicherungen); per GAMEWARDEN_DATA umlenkbar (Tests)."""
    env = os.environ.get("GAMEWARDEN_DATA")
    return Path(env) if env else APP_DIR


def fmt_size(n: int | float | None) -> str:
    if n is None:
        return "–"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_date(ts: int | None) -> str:
    if not ts:
        return "nie / unbekannt"
    return time.strftime("%d.%m.%Y", time.localtime(ts))


def days_since(ts: int | None) -> int | None:
    if not ts:
        return None
    return int((time.time() - ts) // 86400)


def dir_size(path: Path) -> tuple[int, int]:
    """(Bytes, Dateien) eines Ordners, Symlinks/Junctions werden nicht verfolgt."""
    total = files = 0
    stack = [str(path)]
    while stack:
        p = stack.pop()
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        pass
        except OSError:
            pass
    return total, files


# ---------------------------------------------------------------- Prozesse
class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def running_processes() -> set[str]:
    """Kleingeschriebene exe-Namen aller laufenden Prozesse."""
    names: set[str] = set()
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        if snap in (None, wintypes.HANDLE(-1).value):
            return names
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            names.add(entry.szExeFile.lower())
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
        k32.CloseHandle(snap)
    except (OSError, AttributeError):
        pass
    return names


def open_in_explorer(path: Path):
    path = Path(path)
    if path.is_file():
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif path.exists():
        os.startfile(str(path))


# ---------------------------------------------------------------- Konfiguration
class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "config.json"
        self.data: dict = {}
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
