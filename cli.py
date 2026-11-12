"""GameWarden-Kommandozeile für Claude/ToolHub.

Jeder Befehl schreibt genau ein JSON-Objekt auf stdout. Fehler: Meldung auf stderr, Exit-Code ≠ 0.
Befehle, die Dateien ändern (move-game, sort-screenshots, activate-profile), unterstützen --dry-run.

    python cli.py games --json --sort size --limit 10
    python cli.py move-game --game "Golf With Your Friends" --target "C:\\Program Files (x86)\\Steam" --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from gamewarden import library, mover, profiles, screenshots
from gamewarden.util import Config, fmt_size, running_processes


class CliError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def _iso(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else None


def _game_json(g: library.Game, sugg: dict[str, str]) -> dict:
    return {
        "key": g.key, "platform": g.platform, "id": g.id, "name": g.name,
        "install_dir": str(g.install_dir), "drive": g.drive,
        "size_bytes": g.size, "size": fmt_size(g.size),
        "last_played": _iso(g.last_played) if g.platform == "Steam" else None,
        "days_unplayed": g.days_unplayed if g.platform == "Steam" else None,
        "playtime_hours": round(g.playtime_min / 60, 1) if g.playtime_min else None,
        "suggestion": sugg.get(g.key),
    }


def _load(args) -> tuple[library.SteamInstall, list[library.Game]]:
    steam = library.SteamInstall()
    return steam, library.all_games(steam)


def _find_game(games: list[library.Game], query: str) -> library.Game:
    q = query.strip().lower()
    for test in (lambda g: g.key.lower() == q, lambda g: g.id.lower() == q, lambda g: g.name.lower() == q):
        hits = [g for g in games if test(g)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise CliError(f"Mehrdeutig: {', '.join(g.key for g in hits)} – bitte den key angeben.")
    hits = [g for g in games if q in g.name.lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise CliError("Mehrdeutig: " + "; ".join(f"{g.key} ({g.name})" for g in hits[:15]))
    raise CliError(f"Kein installiertes Spiel gefunden für „{query}“.")


def _sugg(games, args):
    days = args.days if getattr(args, "days", None) is not None else 365
    gb = args.min_gb if getattr(args, "min_gb", None) is not None else 0
    return library.suggestions(games, days, gb)


# ------------------------------------------------------------------ lesend
def cmd_games(args):
    _, games = _load(args)
    sugg = _sugg(games, args)
    if args.platform:
        games = [g for g in games if g.platform.lower() == args.platform.lower()]
    if args.filter:
        f = args.filter.lower()
        games = [g for g in games if f in g.name.lower() or f in g.id.lower()]
    if args.drive:
        games = [g for g in games if g.drive.rstrip(":").upper() == args.drive.rstrip(":\\").upper()]
    keys = {"size": (lambda g: g.size or 0, True), "name": (lambda g: g.name.lower(), False),
            "last": (lambda g: g.last_played or 0, True), "playtime": (lambda g: g.playtime_min or 0, True),
            "drive": (lambda g: (g.drive, -(g.size or 0)), False)}
    if args.sort not in keys:
        raise CliError(f"Unbekannte Sortierung „{args.sort}“ (erlaubt: {', '.join(keys)})")
    k, rev = keys[args.sort]
    games.sort(key=k, reverse=rev)
    total = len(games)
    if args.limit:
        games = games[: args.limit]
    return {"count": total, "shown": len(games), "total_size": fmt_size(sum(g.size or 0 for g in games)),
            "games": [_game_json(g, sugg) for g in games]}


def cmd_drives(args):
    import shutil
    _, games = _load(args)
    per = defaultdict(lambda: {"games": 0, "size_bytes": 0})
    for g in games:
        per[g.drive]["games"] += 1
        per[g.drive]["size_bytes"] += g.size or 0
    out = []
    for d in sorted(per):
        e = {"drive": d, **per[d], "size": fmt_size(per[d]["size_bytes"])}
        try:
            u = shutil.disk_usage(d + "\\")
            e.update(total=fmt_size(u.total), free=fmt_size(u.free), free_bytes=u.free,
                     used_percent=round(100 * (u.total - u.free) / u.total, 1))
        except OSError:
            pass
        out.append(e)
    return {"drives": out}


def cmd_suggestions(args):
    _, games = _load(args)
    sugg = _sugg(games, args)
    hits = sorted((g for g in games if g.key in sugg), key=lambda g: -(g.size or 0))
    if args.limit:
        hits = hits[: args.limit]
    return {"criteria": {"days_unplayed": args.days, "min_gb": args.min_gb},
            "count": len(sugg), "reclaimable": fmt_size(sum(g.size or 0 for g in games if g.key in sugg)),
            "games": [_game_json(g, sugg) for g in hits],
            "note": "GameWarden deinstalliert nichts selbst; Deinstallation über Steam/Epic."}


def cmd_libraries(args):
    import shutil
    steam, games = _load(args)
    libs = []
    for lib in steam.libraries():
        try:
            free = shutil.disk_usage(lib).free
        except OSError:
            free = None
        n = [g for g in games if g.platform == "Steam" and g.library == lib]
        libs.append({"platform": "Steam", "path": str(lib), "games": len(n),
                     "size": fmt_size(sum(g.size or 0 for g in n)), "free": fmt_size(free), "free_bytes": free})
    epic = defaultdict(list)
    for g in games:
        if g.platform == "Epic":
            epic[str(g.library)].append(g)
    for p, n in epic.items():
        libs.append({"platform": "Epic", "path": p, "games": len(n), "size": fmt_size(sum(g.size or 0 for g in n))})
    return {"steam_root": str(steam.root) if steam.root else None,
            "steam_running": bool(mover.STEAM_PROCS & running_processes()),
            "epic_running": bool(mover.EPIC_PROCS & running_processes()),
            "libraries": libs}


def _shot_sources(args, steam):
    extra = list(Config().get("shot_folders", []))
    if getattr(args, "folder", None):
        extra.append({"path": args.folder, "name": Path(args.folder).name})
    return screenshots.default_sources(steam, extra)


def cmd_screenshots(args):
    steam, games = _load(args)
    names = {g.id: g.name for g in games if g.platform == "Steam"}
    srcs = _shot_sources(args, steam)
    shots = screenshots.scan(srcs, names, args.videos)
    by = defaultdict(list)
    for s in shots:
        by[s.game].append(s)
    out = []
    for game, lst in sorted(by.items(), key=lambda kv: -len(kv[1])):
        e = {"game": game, "count": len(lst), "size": fmt_size(sum(s.size for s in lst)),
             "sources": sorted({s.source for s in lst}),
             "newest": time.strftime("%Y-%m-%d %H:%M", time.localtime(max(s.mtime for s in lst)))}
        if args.details:
            e["files"] = [str(s.path) for s in sorted(lst, key=lambda s: -s.mtime)]
        out.append(e)
    return {"sources": [{"name": s.name, "kind": s.kind, "path": str(s.path)} for s in srcs],
            "count": len(shots), "games": out}


def cmd_profiles(args):
    store = profiles.ProfileStore()
    out = []
    for p in store.profiles:
        out.append({"game": p.game, "exe": p.exe,
                    "files": [{"path": f, "exists": Path(f).is_file()} for f in p.files],
                    "variants": store.variants(p), "start_variant": p.start_variant or None,
                    "exit_variant": p.exit_variant or None,
                    "running": bool(p.exe) and p.exe.lower() in running_processes()})
    return {"store": str(store.root), "profiles": out}


# ------------------------------------------------------------------ schreibend
def cmd_move_game(args):
    steam, games = _load(args)
    g = _find_game(games, args.game)
    target = Path(args.target)
    if g.platform == "Steam":
        plan = mover.plan_steam_move(g, target, steam)
    else:
        plan = mover.plan_epic_move(g, target)
    preview = {
        "game": _game_json(g, {}), "target": str(target), "ok": plan.ok,
        "blockers": plan.blockers, "warnings": plan.warnings,
        "mode": "rename" if plan.same_volume else "copy-verify-delete",
        "bytes": plan.total_bytes, "size": fmt_size(plan.total_bytes), "files": plan.total_files,
        "target_free": fmt_size(plan.target_free),
        "items": [{"from": str(i.src), "to": str(i.dst)} for i in plan.items],
        "metadata_edits": [{"file": str(e.path), "change": e.description} for e in plan.edits],
    }
    if args.dry_run:
        return {"dry_run": True, **preview}
    if not plan.ok:
        raise CliError("Umzug nicht möglich: " + "; ".join(plan.blockers), 2)
    try:
        leftovers = mover.execute(plan, log=lambda s: print(s, file=sys.stderr))
    except mover.MoveError as e:
        raise CliError(f"Umzug fehlgeschlagen, alles zurückgerollt: {e}", 3) from e
    return {"dry_run": False, "moved": True, **preview, "ok": True, "leftovers": leftovers,
            "new_install_dir": str(plan.items[0].dst)}


def cmd_sort_screenshots(args):
    steam, games = _load(args)
    names = {g.id: g.name for g in games if g.platform == "Steam"}
    cfg = Config()
    target = Path(args.target or cfg.get("shot_target") or Path.home() / "Pictures" / "Spiele-Screenshots")
    shots = screenshots.scan(_shot_sources(args, steam), names, args.videos)
    if args.game:
        shots = [s for s in shots if s.game.lower() == args.game.lower()]
    ops = screenshots.plan_sort(shots, target, args.move)
    cnt = Counter(o.action for o in ops)
    res = {"target": str(target), "dry_run": bool(args.dry_run), "counts": dict(cnt),
           "operations": [{"action": o.action, "from": str(o.shot.path), "to": str(o.dst), "game": o.shot.game,
                           "note": o.note} for o in ops[: args.limit or None]]}
    if args.dry_run:
        return res
    try:
        copied, moved = screenshots.execute_sort(ops)
    except Exception as e:   # noqa: BLE001
        raise CliError(f"Sortieren fehlgeschlagen, alles zurückgenommen: {e}", 3) from e
    res.update(copied=copied, moved=moved)
    return res


def cmd_activate_profile(args):
    store = profiles.ProfileStore()
    p = store.get(args.game) or next((x for x in store.profiles if x.game.lower() == args.game.lower()), None)
    if not p:
        raise CliError(f"Kein Profil „{args.game}“. Vorhanden: {', '.join(x.game for x in store.profiles) or '-'}")
    if args.variant not in store.variants(p):
        raise CliError(f"Variante „{args.variant}“ fehlt. Vorhanden: {', '.join(store.variants(p)) or '-'}")
    files = store.variant_files(p, args.variant)
    res = {"game": p.game, "variant": args.variant, "dry_run": bool(args.dry_run),
           "overwrites": [str(o) for _, o in files], "backup_variant": profiles.BACKUP_VARIANT}
    if args.dry_run:
        return res
    try:
        res["written"] = store.activate(p, args.variant)
    except Exception as e:   # noqa: BLE001
        raise CliError(f"Aktivieren fehlgeschlagen, zurückgerollt: {e}", 3) from e
    return res


# ------------------------------------------------------------------ Parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cli.py", description="GameWarden – JSON-Kommandozeile")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--json", action="store_true", help="JSON ausgeben (Standard, nur für Kompatibilität)")
        p.set_defaults(fn=fn)
        return p

    p = add("games", cmd_games, "Installierte Steam-/Epic-Spiele auflisten")
    p.add_argument("--sort", default="size", help="size|name|last|playtime|drive")
    p.add_argument("--platform", help="steam|epic")
    p.add_argument("--filter", help="Teil des Namens oder der ID")
    p.add_argument("--drive", help="Laufwerk, z. B. D")
    p.add_argument("--limit", type=int)
    p.add_argument("--days", type=int, help="Schwelle für Deinstallations-Vorschlag (Tage)")
    p.add_argument("--min-gb", dest="min_gb", type=float)

    add("drives", cmd_drives, "Spiele und freier Platz pro Laufwerk")

    p = add("suggestions", cmd_suggestions, "Deinstallations-Kandidaten")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--min-gb", dest="min_gb", type=float, default=0)
    p.add_argument("--limit", type=int)

    add("libraries", cmd_libraries, "Steam-Bibliotheken und Epic-Ordner")

    p = add("screenshots", cmd_screenshots, "Screenshots nach Spiel zählen")
    p.add_argument("--folder", help="zusätzlicher Ordner")
    p.add_argument("--videos", action="store_true")
    p.add_argument("--details", action="store_true", help="Dateipfade mit ausgeben")

    add("profiles", cmd_profiles, "Controller-/Tastenbelegungs-Profile auflisten")

    p = add("move-game", cmd_move_game, "Spiel auf anderes Laufwerk verschieben")
    p.add_argument("--game", required=True, help="key (Steam:730), AppID, Name oder eindeutiger Namensteil")
    p.add_argument("--target", required=True, help="Steam: Zielbibliothek; Epic: Zielordner")
    p.add_argument("--dry-run", dest="dry_run", action="store_true")

    p = add("sort-screenshots", cmd_sort_screenshots, "Screenshots in <Ziel>\\<Spiel>\\ einsortieren")
    p.add_argument("--target")
    p.add_argument("--move", action="store_true", help="verschieben statt kopieren (Steam immer kopieren)")
    p.add_argument("--game", help="nur dieses Spiel")
    p.add_argument("--folder", help="zusätzlicher Quellordner")
    p.add_argument("--videos", action="store_true")
    p.add_argument("--limit", type=int, help="max. Einträge in der Ausgabe-Liste")
    p.add_argument("--dry-run", dest="dry_run", action="store_true")

    p = add("activate-profile", cmd_activate_profile, "Gespeicherte Profil-Variante aktivieren")
    p.add_argument("--game", required=True)
    p.add_argument("--variant", required=True)
    p.add_argument("--dry-run", dest="dry_run", action="store_true")
    return ap


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    ap = build_parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    try:
        result = args.fn(args)
    except CliError as e:
        print(str(e), file=sys.stderr)
        return e.code
    except Exception as e:   # noqa: BLE001
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
