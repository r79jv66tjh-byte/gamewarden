"""Tests der Claude-Schnittstelle (cli.py + toolhub.json) – nur an Testdaten.

Steam, Epic, Datenordner und Benutzerordner werden per Umgebungsvariablen auf temporäre
Ordner umgelenkt (GAMEWARDEN_STEAM_ROOT, GAMEWARDEN_EPIC_MANIFESTS, GAMEWARDEN_EPIC_DAT,
GAMEWARDEN_DATA, USERPROFILE), damit keine echten Spiele oder Screenshots berührt werden.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402
from gamewarden import mover, profiles  # noqa: E402
from test_gamewarden import FakeSteam, write  # noqa: E402

# Beispielwerte für Pflicht-Argumente (werden in setUp mit Testpfaden gefüllt)
SAMPLE_ARGS = {
    "move-game": lambda t: {"game": "Testspiel", "target": str(t.fs.lib2)},
    "activate-profile": lambda t: {"game": "Spiel X", "variant": "Controller"},
}


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        b = Path(self.tmp.name)
        self.fs = FakeSteam(b)
        # Epic
        self.epic_src = b / "Epic" / "MyGame"
        write(self.epic_src / "game.exe", size=100)
        mdir = b / "EpicManifests"
        write(mdir / "A.item", json.dumps({"DisplayName": "Mein Epic-Spiel", "AppName": "MyGame",
                                           "AppCategories": ["games"], "InstallLocation": str(self.epic_src),
                                           "InstallSize": 100}))
        dat = write(b / "LauncherInstalled.dat", json.dumps({"InstallationList": [
            {"InstallLocation": str(self.epic_src), "AppName": "MyGame"}]}))
        # Benutzerordner mit Screenshots
        self.home = b / "home"
        write(self.home / "Videos" / "Captures" / "Hytale 2026-02-23 00-28-43.png", size=20)
        write(self.home / "Pictures" / "Screenshots" / "Screenshot 2025-08-31 220434.png", size=20)
        write(self.fs.root / "userdata" / "42" / "760" / "remote" / "100" / "screenshots" / "1.jpg", size=10)
        # Datenordner mit Profil
        self.data = b / "data"
        cfg1 = write(b / "game" / "input.ini", "pad=1")
        store = profiles.ProfileStore(self.data / "profiles")
        p = profiles.Profile("Spiel X", exe="x.exe", files=[str(cfg1)])
        store.add(p)
        store.save_variant(p, "Controller")
        cfg1.write_text("pad=0", encoding="utf-8")
        self.cfg1 = cfg1

        self.env = {
            "GAMEWARDEN_STEAM_ROOT": str(self.fs.root),
            "GAMEWARDEN_EPIC_MANIFESTS": str(mdir),
            "GAMEWARDEN_EPIC_DAT": str(dat),
            "GAMEWARDEN_DATA": str(self.data),
            "USERPROFILE": str(self.home),
            "HOME": str(self.home),
        }
        p = mock.patch.dict(os.environ, self.env)
        p.start()
        self.addCleanup(p.stop)
        self.manifest = json.loads((ROOT / "toolhub.json").read_text(encoding="utf-8"))

    def tearDown(self):
        self.tmp.cleanup()

    # -------------------------------------------------------------- Hilfen
    def run_sub(self, argv):
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        p = subprocess.run([sys.executable, *argv], cwd=ROOT, capture_output=True, env=env,
                           stdin=subprocess.DEVNULL, timeout=120)
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")

    def run_inproc(self, argv):
        if argv and argv[0] == "cli.py":
            argv = argv[1:]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def argv_for(self, cmd, extra=None, dry=None):
        """Baut die Befehlszeile wie ToolHub: run + Argumente (+ --dry-run bei schreibenden)."""
        argv = list(cmd["run"][1:])            # „python“ weglassen
        args = dict(SAMPLE_ARGS.get(cmd["name"], lambda t: {})(self))
        args.update(extra or {})
        flags = {a["name"]: a for a in cmd["args"]}
        for k, v in args.items():
            a = flags[k]
            if a["type"] == "boolean":
                if v:
                    argv.append(a["flag"])
            else:
                argv += [a["flag"], str(v)]
        if dry if dry is not None else not cmd["readonly"]:
            argv.append("--dry-run")
        return argv

    # -------------------------------------------------------------- Manifest
    def test_manifest_format(self):
        m = self.manifest
        self.assertEqual(m["name"], "GameWarden")
        names = [c["name"] for c in m["commands"]]
        self.assertEqual(len(names), len(set(names)))
        parser_cmds = set(cli.build_parser()._subparsers._group_actions[0].choices)
        for c in m["commands"]:
            self.assertEqual(c["run"][:2], ["python", "cli.py"])
            self.assertIn(c["run"][2], parser_cmds)
            self.assertTrue(c["description"])
            self.assertIsInstance(c["readonly"], bool)
            for a in c["args"]:
                self.assertIn(a["type"], {"string", "integer", "number", "boolean"})
                self.assertTrue(a["flag"].startswith("--"))
            if not c["readonly"]:
                self.assertIn("dry_run", [a["name"] for a in c["args"]], c["name"])

    # -------------------------------------------------------------- alle Befehle als Subprozess
    def test_every_command_returns_json(self):
        for cmd in self.manifest["commands"]:
            with self.subTest(cmd=cmd["name"]):
                code, out, err = self.run_sub(self.argv_for(cmd))
                self.assertEqual(code, 0, err)
                data = json.loads(out)
                self.assertIsInstance(data, dict)
                if not cmd["readonly"]:
                    self.assertTrue(data["dry_run"])
        # Dry-Runs haben nichts verändert
        self.assertTrue((self.fs.root / "steamapps" / "common" / "Test Game").is_dir())
        self.assertEqual(self.cfg1.read_text(encoding="utf-8"), "pad=0")
        self.assertFalse((self.home / "Pictures" / "Spiele-Screenshots").exists())

    def test_every_argument_accepted(self):
        """Jedes deklarierte Argument wird von cli.py akzeptiert."""
        samples = {"string": "x", "integer": 3, "number": 1.5, "boolean": True}
        for cmd in self.manifest["commands"]:
            extra = {a["name"]: samples[a["type"]] for a in cmd["args"]
                     if not a.get("required") and a["name"] != "dry_run"}
            if "sort" in extra:
                extra["sort"] = "name"
            if "platform" in extra:
                extra["platform"] = "steam"
            if "target" in extra:
                extra["target"] = str(Path(self.tmp.name) / "ziel")
            if "folder" in extra:
                extra["folder"] = str(self.home)
            with self.subTest(cmd=cmd["name"]):
                code, out, err = self.run_inproc(self.argv_for(cmd, extra, dry=not cmd["readonly"]))
                self.assertEqual(code, 0, err)
                json.loads(out)

    # -------------------------------------------------------------- Inhalte
    def test_games_content(self):
        code, out, _ = self.run_inproc(["games", "--json", "--sort", "name"])
        d = json.loads(out)
        self.assertEqual(d["count"], 2)
        self.assertEqual([g["name"] for g in d["games"]], ["Mein Epic-Spiel", "Testspiel"])
        steam = d["games"][1]
        self.assertEqual((steam["key"], steam["last_played"], steam["playtime_hours"]),
                         ("Steam:100", "2023-11-14", 2.0))
        self.assertIn("seit", steam["suggestion"])

    def test_screenshots_content(self):
        d = json.loads(self.run_inproc(["screenshots", "--details"])[1])
        games = {g["game"]: g["count"] for g in d["games"]}
        self.assertEqual(games, {"Testspiel": 1, "Hytale": 1, "Unbekannt": 1})

    # -------------------------------------------------------------- schreibend (bestätigt)
    def test_move_game_confirmed(self):
        with mock.patch.object(mover, "running_processes", return_value=set()):
            code, out, err = self.run_inproc(["move-game", "--game", "Steam:100", "--target", str(self.fs.lib2)])
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertTrue(d["moved"])
        self.assertTrue((self.fs.lib2 / "steamapps" / "common" / "Test Game" / "game.exe").is_file())
        self.assertFalse((self.fs.root / "steamapps" / "common" / "Test Game").exists())

    def test_move_game_blocked(self):
        with mock.patch.object(mover, "running_processes", return_value={"steam.exe"}):
            code, out, err = self.run_inproc(["move-game", "--game", "Testspiel", "--target", str(self.fs.lib2)])
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertIn("steam.exe", err)
            code, out, _ = self.run_inproc(["move-game", "--game", "Testspiel", "--target", str(self.fs.lib2),
                                            "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["ok"])
        self.assertTrue((self.fs.root / "steamapps" / "common" / "Test Game").is_dir())

    def test_sort_screenshots_confirmed(self):
        target = Path(self.tmp.name) / "sortiert"
        code, out, err = self.run_inproc(["sort-screenshots", "--target", str(target), "--move"])
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertEqual((d["copied"], d["moved"]), (1, 2))   # Steam nur kopiert
        self.assertTrue((target / "Hytale" / "Hytale 2026-02-23 00-28-43.png").is_file())
        self.assertTrue((self.fs.root / "userdata" / "42" / "760" / "remote" / "100" / "screenshots"
                         / "1.jpg").is_file())

    def test_activate_profile_confirmed(self):
        code, out, err = self.run_inproc(["activate-profile", "--game", "Spiel X", "--variant", "Controller"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["written"], 1)
        self.assertEqual(self.cfg1.read_text(encoding="utf-8"), "pad=1")

    def test_errors(self):
        for argv in (["move-game", "--game", "gibtsnicht", "--target", "C:\\", "--dry-run"],
                     ["activate-profile", "--game", "Spiel X", "--variant", "nein", "--dry-run"],
                     ["games", "--sort", "quatsch"],
                     ["unbekannt"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_sub(["cli.py", *argv])
                self.assertNotEqual(code, 0)
                self.assertEqual(out.strip(), "")
                self.assertTrue(err.strip())


if __name__ == "__main__":
    unittest.main()
