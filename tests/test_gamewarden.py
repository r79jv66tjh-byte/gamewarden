"""Tests der schreibenden Funktionen – ausschließlich an Testdaten in temporären Ordnern.

    python -m unittest discover -s tests -v
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gamewarden import library, mover, profiles, screenshots, vdf


def write(p: Path, text="x", size=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    if size is not None:
        p.write_bytes(os.urandom(size))
    else:
        p.write_text(text, encoding="utf-8")
    return p


class FakeSteam:
    """Baut eine Steam-Installation mit zwei Bibliotheken nach."""

    def __init__(self, base: Path):
        self.root = base / "Steam"
        self.lib2 = base / "Lib2"
        (self.lib2 / "steamapps").mkdir(parents=True)
        lf = {"libraryfolders": {
            "0": {"path": str(self.root), "label": "", "apps": {"100": "3000"}},
            "1": {"path": str(self.lib2), "label": "", "apps": {}},
        }}
        write(self.root / "steamapps" / "libraryfolders.vdf", vdf.dumps(lf))
        write(self.root / "config" / "libraryfolders.vdf", vdf.dumps(lf))
        acf = {"AppState": {"appid": "100", "name": "Testspiel", "installdir": "Test Game",
                            "SizeOnDisk": "3000", "LastPlayed": "0"}}
        write(self.root / "steamapps" / "appmanifest_100.acf", vdf.dumps(acf))
        game = self.root / "steamapps" / "common" / "Test Game"
        write(game / "game.exe", size=1000)
        write(game / "data" / "a.pak", size=1500)
        write(game / "data" / "sub" / "b.cfg", size=500)
        write(self.root / "steamapps" / "workshop" / "content" / "100" / "mod1" / "m.bin", size=10)
        write(self.root / "steamapps" / "workshop" / "appworkshop_100.acf", '"AppWorkshop"\n{\n}\n')
        uc = {"UserLocalConfigStore": {"Software": {"Valve": {"Steam": {"apps": {
            "100": {"LastPlayed": "1700000000", "Playtime": "120"}}}}}}}
        write(self.root / "userdata" / "42" / "config" / "localconfig.vdf", vdf.dumps(uc))
        self.steam = library.SteamInstall(self.root)


class VdfTest(unittest.TestCase):
    def test_roundtrip(self):
        txt = '"a"\n{\n\t"b"\t\t"C:\\\\Pfad\\\\x"\n\t"c"\n\t{\n\t\t"d"\t\t"1"\n\t}\n}\n'
        d = vdf.loads(txt)
        self.assertEqual(d["a"]["b"], "C:\\Pfad\\x")
        self.assertEqual(vdf.loads(vdf.dumps(d)), d)

    def test_broken(self):
        with self.assertRaises(vdf.VdfError):
            vdf.loads('"a" { "b" "c" ')


class SteamMoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fs = FakeSteam(Path(self.tmp.name))
        self.backups = Path(self.tmp.name) / "backups"
        p = mock.patch.object(mover, "running_processes", return_value=set())
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def game(self):
        [g] = self.fs.steam.games()
        return g

    def plan(self, same_volume):
        pl = mover.plan_steam_move(self.game(), self.fs.lib2, self.fs.steam)
        pl.backup_root = self.backups
        pl.same_volume = same_volume
        return pl

    def test_library_read(self):
        g = self.game()
        self.assertEqual((g.name, g.size, g.last_played, g.playtime_min), ("Testspiel", 3000, 1700000000, 120))

    def check_moved(self):
        sa2 = self.fs.lib2 / "steamapps"
        self.assertTrue((sa2 / "common" / "Test Game" / "data" / "sub" / "b.cfg").is_file())
        self.assertTrue((sa2 / "appmanifest_100.acf").is_file())
        self.assertTrue((sa2 / "workshop" / "content" / "100" / "mod1" / "m.bin").is_file())
        self.assertFalse((self.fs.root / "steamapps" / "common" / "Test Game").exists())
        self.assertFalse((self.fs.root / "steamapps" / "appmanifest_100.acf").exists())
        self.assertEqual(list((self.fs.root / "steamapps" / "common").iterdir()), [])
        for lf in (self.fs.steam.libraryfolders_vdf, self.fs.root / "config" / "libraryfolders.vdf"):
            d = vdf.load(lf)["libraryfolders"]
            self.assertNotIn("100", d["0"]["apps"])
            self.assertEqual(d["1"]["apps"]["100"], "3000")
        [g] = self.fs.steam.games()
        self.assertEqual(g.library, self.fs.lib2)

    def test_plan_ok(self):
        pl = self.plan(False)
        self.assertTrue(pl.ok, pl.blockers)
        self.assertEqual(pl.total_files, 6)
        self.assertIn("Testspiel", pl.describe())

    def test_copy_move(self):
        mover.execute(self.plan(False), log=lambda s: None)
        self.check_moved()

    def test_rename_move(self):
        mover.execute(self.plan(True), log=lambda s: None)
        self.check_moved()

    def assert_untouched(self):
        sa = self.fs.root / "steamapps"
        self.assertTrue((sa / "common" / "Test Game" / "data" / "a.pak").is_file())
        self.assertTrue((sa / "appmanifest_100.acf").is_file())
        self.assertEqual(sorted(p.name for p in (sa / "common").iterdir()), ["Test Game"])
        self.assertFalse((self.fs.lib2 / "steamapps" / "common" / "Test Game").exists())
        self.assertFalse((self.fs.lib2 / "steamapps" / "appmanifest_100.acf").exists())
        leftovers = [p for p in self.fs.lib2.rglob("*") if p.name.startswith(".gw_")]
        self.assertEqual(leftovers, [])
        d = vdf.load(self.fs.steam.libraryfolders_vdf)["libraryfolders"]
        self.assertIn("100", d["0"]["apps"])
        self.assertNotIn("100", d["1"]["apps"])

    def test_rollback_on_copy_error(self):
        pl = self.plan(False)
        orig = mover._Copier.copy_file
        calls = {"n": 0}

        def boom(self_, src, dst):
            calls["n"] += 1
            if calls["n"] == 5:
                raise OSError("Platte voll (simuliert)")
            return orig(self_, src, dst)
        with mock.patch.object(mover._Copier, "copy_file", boom):
            with self.assertRaises(mover.MoveError):
                mover.execute(pl, log=lambda s: None)
        self.assert_untouched()

    def test_rollback_on_locked_source(self):
        """Quelle lässt sich nicht beiseitelegen (z. B. gesperrte Datei) → alles zurück, auch Metadaten."""
        pl = self.plan(False)
        real_rename = os.rename

        def rename(a, b):
            if ".gw_alt_" in str(b):
                raise PermissionError("Datei in Benutzung (simuliert)")
            return real_rename(a, b)
        with mock.patch.object(mover.os, "rename", rename):
            with self.assertRaises(mover.MoveError):
                mover.execute(pl, log=lambda s: None)
        self.assert_untouched()

    def test_rollback_on_cancel(self):
        import threading
        ev = threading.Event()
        ev.set()
        with self.assertRaises(mover.Cancelled):
            mover.execute(self.plan(False), cancel=ev, log=lambda s: None)
        self.assert_untouched()

    def test_rollback_rename_mode(self):
        pl = self.plan(True)
        with mock.patch.object(pl.edits[0], "apply", side_effect=ValueError("kaputt")):
            with self.assertRaises(mover.MoveError):
                mover.execute(pl, log=lambda s: None)
        self.assert_untouched()

    def test_blockers(self):
        with mock.patch.object(mover, "running_processes", return_value={"steam.exe"}):
            pl = mover.plan_steam_move(self.game(), self.fs.lib2, self.fs.steam)
        self.assertFalse(pl.ok)
        self.assertTrue(any("steam.exe" in b for b in pl.blockers))
        with mock.patch.object(mover.shutil, "disk_usage", return_value=mock.Mock(free=1000)),                 mock.patch.object(mover, "_volume", side_effect=lambda p: str(p)):
            pl = mover.plan_steam_move(self.game(), self.fs.lib2, self.fs.steam)
        self.assertTrue(any("Zu wenig Platz" in b for b in pl.blockers))
        pl = mover.plan_steam_move(self.game(), Path(self.tmp.name) / "keineLib", self.fs.steam)
        self.assertTrue(any("keine Steam-Bibliothek" in b for b in pl.blockers))
        with self.assertRaises(mover.MoveError):
            mover.execute(pl)


class EpicMoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        b = Path(self.tmp.name)
        self.src = b / "Epic" / "MyGame"
        write(self.src / "Bin" / "game.exe", size=2000)
        write(self.src / ".egstore" / "x.manifest", size=50)
        self.mdir = b / "Manifests"
        item = {"DisplayName": "Mein Spiel", "AppName": "MyGame", "AppCategories": ["games"],
                "InstallLocation": str(self.src), "ManifestLocation": str(self.src) + "/.egstore",
                "StagingLocation": str(self.src) + "/.egstore/bps", "InstallSize": 2050}
        write(self.mdir / "ABC.item", json.dumps(item))
        self.dat = write(b / "LauncherInstalled.dat", json.dumps(
            {"InstallationList": [{"InstallLocation": str(self.src), "AppName": "MyGame"}]}))
        self.target = b / "Neu"
        self.target.mkdir()
        p = mock.patch.object(mover, "running_processes", return_value=set())
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_move(self):
        [g] = library.epic_games(self.mdir)
        pl = mover.plan_epic_move(g, self.target, self.dat)
        pl.backup_root = Path(self.tmp.name) / "bk"
        pl.same_volume = False
        self.assertTrue(pl.ok, pl.blockers)
        mover.execute(pl, log=lambda s: None)
        new = self.target / "MyGame"
        self.assertTrue((new / "Bin" / "game.exe").is_file())
        self.assertFalse(self.src.exists())
        item = json.loads((self.mdir / "ABC.item").read_text(encoding="utf-8"))
        self.assertEqual(item["InstallLocation"], str(new))
        self.assertEqual(item["StagingLocation"], str(new) + "/.egstore/bps")
        dat = json.loads(self.dat.read_text(encoding="utf-8"))
        self.assertEqual(dat["InstallationList"][0]["InstallLocation"], str(new))

    def test_launcher_running(self):
        [g] = library.epic_games(self.mdir)
        with mock.patch.object(mover, "running_processes", return_value={"epicgameslauncher.exe"}):
            pl = mover.plan_epic_move(g, self.target, self.dat)
        self.assertFalse(pl.ok)


class ScreenshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        b = Path(self.tmp.name)
        self.steam = b / "760" / "remote"
        write(self.steam / "100" / "screenshots" / "2024_1.jpg", size=10)
        write(self.steam / "100" / "screenshots" / "thumbnails" / "2024_1.jpg", size=5)
        self.bar = b / "Captures"
        write(self.bar / "Hytale 2026-02-23 00-28-43.png", size=20)
        write(self.bar / "Screenshot 2025-08-31 220434.png", size=20)
        self.nv = b / "NVIDIA"
        write(self.nv / "Apex Legends" / "shot.png", size=30)
        self.target = b / "Sortiert"
        self.sources = [screenshots.Source("Steam", self.steam, "steam"),
                        screenshots.Source("Bar", self.bar, "gamebar"),
                        screenshots.Source("NV", self.nv, "nvidia")]

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_and_sort(self):
        shots = screenshots.scan(self.sources, {"100": "Testspiel"})
        games = sorted(s.game for s in shots)
        self.assertEqual(games, ["Apex Legends", "Hytale", "Testspiel", "Unbekannt"])
        ops = screenshots.plan_sort(shots, self.target, move=True)
        acts = {o.shot.game: o.action for o in ops}
        self.assertEqual(acts["Testspiel"], "kopieren")      # Steam nie verschieben
        self.assertEqual(acts["Hytale"], "verschieben")
        screenshots.execute_sort(ops)
        self.assertTrue((self.target / "Testspiel" / "2024_1.jpg").is_file())
        self.assertTrue((self.steam / "100" / "screenshots" / "2024_1.jpg").is_file())
        self.assertTrue((self.target / "Hytale" / "Hytale 2026-02-23 00-28-43.png").is_file())
        self.assertFalse((self.bar / "Hytale 2026-02-23 00-28-43.png").exists())
        # zweiter Lauf: alles schon vorhanden
        again = screenshots.plan_sort(screenshots.scan(self.sources[:1], {"100": "Testspiel"}), self.target, False)
        self.assertEqual([o.action for o in again], ["überspringen"])

    def test_name_conflict(self):
        write(self.target / "Apex Legends" / "shot.png", size=99)
        ops = screenshots.plan_sort(screenshots.scan(self.sources[2:]), self.target, False)
        self.assertEqual(ops[0].dst.name, "shot_1.png")

    def test_rollback(self):
        shots = screenshots.scan(self.sources)
        ops = screenshots.plan_sort(shots, self.target, move=True)
        real = screenshots.shutil.move
        n = {"i": 0}

        def flaky(a, b):
            n["i"] += 1
            if n["i"] == 2:
                raise OSError("simuliert")
            return real(a, b)
        with mock.patch.object(screenshots.shutil, "move", flaky):
            with self.assertRaises(OSError):
                screenshots.execute_sort(ops)
        self.assertTrue((self.bar / "Hytale 2026-02-23 00-28-43.png").is_file())
        self.assertTrue((self.nv / "Apex Legends" / "shot.png").is_file())
        self.assertEqual([p for p in self.target.rglob("*") if p.is_file()], [])


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        b = Path(self.tmp.name)
        self.cfg1 = write(b / "game" / "input.ini", "pad=1")
        self.cfg2 = write(b / "game" / "keys.cfg", "jump=A")
        self.store = profiles.ProfileStore(b / "store")
        self.p = profiles.Profile("Spiel X", exe="x.exe", files=[str(self.cfg1), str(self.cfg2)])
        self.store.add(self.p)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_activate(self):
        self.store.save_variant(self.p, "Controller")
        self.cfg1.write_text("pad=0", encoding="utf-8")
        self.cfg2.write_text("jump=Space", encoding="utf-8")
        self.store.save_variant(self.p, "Tastatur")
        self.assertEqual(self.store.variants(self.p), ["Controller", "Tastatur"])
        self.store.activate(self.p, "Controller")
        self.assertEqual(self.cfg2.read_text(encoding="utf-8"), "jump=A")
        self.assertIn(profiles.BACKUP_VARIANT, self.store.variants(self.p))
        self.store.activate(self.p, profiles.BACKUP_VARIANT)
        self.assertEqual(self.cfg2.read_text(encoding="utf-8"), "jump=Space")
        # Persistenz
        self.assertEqual(profiles.ProfileStore(self.store.root).get("Spiel X").files, self.p.files)

    def test_activate_rollback(self):
        self.store.save_variant(self.p, "Controller")
        self.cfg1.write_text("NEU1", encoding="utf-8")
        self.cfg2.write_text("NEU2", encoding="utf-8")
        real = profiles.shutil.copy2

        def flaky(a, b):
            if Path(b) == self.cfg2 and "Controller" in str(a):
                raise PermissionError("gesperrt (simuliert)")
            return real(a, b)
        with mock.patch.object(profiles.shutil, "copy2", flaky):
            with self.assertRaises(PermissionError):
                self.store.activate(self.p, "Controller")
        self.assertEqual(self.cfg1.read_text(encoding="utf-8"), "NEU1")
        self.assertEqual(self.cfg2.read_text(encoding="utf-8"), "NEU2")

    def test_autoswitch(self):
        self.store.save_variant(self.p, "Controller")
        self.cfg1.write_text("pad=0", encoding="utf-8")
        self.store.save_variant(self.p, "Tastatur")
        self.p.start_variant, self.p.exit_variant = "Controller", "Tastatur"
        logs = []
        sw = profiles.AutoSwitcher(self.store, logs.append)
        sw._was_running = set()
        sw.check_once({"explorer.exe"})
        self.assertEqual(self.cfg1.read_text(encoding="utf-8"), "pad=0")
        sw.check_once({"explorer.exe", "x.exe"})
        self.assertEqual(self.cfg1.read_text(encoding="utf-8"), "pad=1")
        sw.check_once({"explorer.exe", "x.exe"})          # läuft weiter → nichts
        sw.check_once({"explorer.exe"})
        self.assertEqual(self.cfg1.read_text(encoding="utf-8"), "pad=0")
        self.assertEqual(len(logs), 2)


if __name__ == "__main__":
    unittest.main()
