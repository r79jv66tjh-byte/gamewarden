# GameWarden

Windows-Tool (Python 3.13 + tkinter, deutsche Oberfläche) für die eigene Spielesammlung.

| Reiter | Funktion |
|---|---|
| **Bibliothek** | Liest alle Steam-Bibliotheken (`libraryfolders.vdf`, `appmanifest_*.acf`, Spielzeiten aus `userdata/*/config/localconfig.vdf`) und Epic-Spiele (`ProgramData\Epic\…\Manifests\*.item`). Zeigt Größe, Laufwerk, zuletzt gespielt und Spielzeit, dazu eine Zusammenfassung pro Laufwerk und Deinstallations-Vorschläge (einstellbar: Tage ohne Spielen, Mindestgröße). |
| **Verschieben** (aus der Bibliothek) | Zieht ein Steam- oder Epic-Spiel ohne Neu-Download auf ein anderes Laufwerk um. |
| **Screenshots** | Sammelt Screenshots aus Steam (`userdata/*/760/remote`), NVIDIA (`Videos\NVIDIA`), Xbox Game Bar (`Videos\Captures`), Windows-Screenshots und eigenen Ordnern und sortiert sie nach `<Ziel>\<Spiel>\`. |
| **Controller-Profile** | Speichert Tastenbelegungs-/Controller-Konfigurationsdateien eines Spiels (eigene Dateien oder Steam-Input-Configs) als benannte Varianten und schaltet sie beim Start/Ende der Spiel-exe automatisch um. |

## Starten

```
python -m gamewarden
```

oder Doppelklick auf `GameWarden.pyw` (ohne Konsolenfenster). Keine Fremdpakete nötig.

## Sicherheitsregeln

GameWarden liest Launcher-Daten nur, bis du eine Aktion ausdrücklich bestätigst.

**Spiele-Umzug**
- Erst **Vorschau**: Was wohin geht, Größe und Dateianzahl, freier Platz am Ziel, welche Launcher-Dateien geändert werden.
- **Blocker** verhindern den Start: zu wenig Platz (Bedarf + 2 GB Reserve), Steam bzw. Epic Games Launcher läuft, Ziel existiert schon, Steam-Ziel ist keine in Steam angelegte Bibliothek.
- Ablauf: auf demselben Laufwerk wird nur umbenannt. Sonst wird zuerst in einen temporären Ordner `.gw_neu_*` kopiert und danach Dateianzahl und Bytes geprüft. Anschließend werden die Launcher-Dateien angepasst (Sicherung unter `%APPDATA%\GameWarden\backups\`). Dann wird die Quelle in `.gw_alt_*` umbenannt (das schlägt fehl, wenn noch etwas gesperrt ist) und erst danach gelöscht.
- **Bei jedem Fehler oder Abbruch** vor dem Löschen der Quelle wird alles zurückgerollt: Kopien entfernen, Umbenennungen rückgängig machen, Launcher-Dateien wiederherstellen.
- Steam: Verschoben werden Spielordner, `appmanifest_<id>.acf`, Workshop-Inhalte und `appworkshop_<id>.acf`. Die App-Zuordnung in `steamapps\libraryfolders.vdf` und `config\libraryfolders.vdf` wird angepasst.
- Epic: Verschoben wird der Spielordner inklusive `.egstore`. Angepasst werden `InstallLocation`, `ManifestLocation` und `StagingLocation` im `.item` sowie `LauncherInstalled.dat`.
- **Deinstallieren** löscht nie selbst. Es öffnet nur Steams eigenen Deinstallations-Dialog (`steam://uninstall/<id>`).

**Screenshots**
- Standard ist **Kopieren**. Verschieben ist optional; Steam-Screenshots werden immer nur kopiert, weil Steam sie in `screenshots.vdf` verwaltet.
- Vorschau mit Aktion pro Datei. Gleiche Dateien (Name + Größe) werden übersprungen, bei Namenskonflikten wird nummeriert.
- Bei einem Fehler wird der ganze Lauf zurückgenommen.
- Spielname: Bei Steam aus der AppID, bei NVIDIA aus dem Unterordner, bei Game Bar/eigenen Ordnern aus dem Dateinamen (`Spiel 2024-05-01 …`). Sonst landet die Datei in `Unbekannt`.

**Controller-Profile**
- Varianten liegen unter `%APPDATA%\GameWarden\profiles\<Spiel>\<Variante>\`.
- Vor jedem Aktivieren wird der aktuelle Stand als `_vor_letzter_aktivierung` gesichert. Scheitert eine Datei, werden die schon geschriebenen zurückgesetzt.
- Automatisches Umschalten fragt alle 2 s die laufenden Prozesse ab. Ein Spiel, das beim Einschalten schon läuft, wird nicht umgeschaltet.
- Grenzen: Manche Spiele lesen ihre Konfiguration sofort beim Start. Dann ist „Nach Spielende“ (für den nächsten Start vorbereiten) zuverlässiger als „Beim Spielstart“. Steam-Input-Dateien bitte nur bei geschlossenem Steam umschalten, weil Steam sie sonst überschreibt.

## Claude-Schnittstelle

Claude kann GameWarden über **ToolHub** (MCP-Server) bedienen. Dafür gibt es die Kommandozeile `cli.py` und die Befehlsbeschreibung `toolhub.json`.

- Jeder Befehl gibt **ein JSON-Objekt auf stdout** aus. Bei Fehlern steht die Meldung auf stderr und der Exit-Code ist ungleich 0: 1 = Fehler, 2 = Umzug blockiert (z. B. Steam läuft), 3 = Aktion fehlgeschlagen und zurückgerollt.
- Lesende Befehle haben `"readonly": true`. Schreibende Befehle haben `"readonly": false` und können `--dry-run`, das nur die Vorschau als JSON liefert. ToolHub führt sie ohne Bestätigung automatisch als Dry-Run aus.

| Befehl | readonly | Zweck |
|---|---|---|
| `games` | ja | Spiele mit Größe, Laufwerk, zuletzt gespielt, Spielzeit und Hinweis (`--sort`, `--platform`, `--filter`, `--drive`, `--limit`, `--days`, `--min-gb`) |
| `drives` | ja | Spiele und freier Platz pro Laufwerk |
| `suggestions` | ja | Deinstallations-Kandidaten und freiwerdender Platz (`--days`, `--min-gb`, `--limit`) |
| `libraries` | ja | Steam-Bibliotheken und Epic-Ordner (mögliche Umzugsziele), läuft Steam/Epic? |
| `screenshots` | ja | Screenshots nach Spiel gezählt (`--folder`, `--videos`, `--details`) |
| `profiles` | ja | Controller-Profile mit Varianten |
| `move-game` | nein | Spiel umziehen (`--game`, `--target`, `--dry-run`) |
| `sort-screenshots` | nein | Screenshots nach `<Ziel>\<Spiel>\` kopieren oder verschieben (`--target`, `--move`, `--game`, `--dry-run`) |
| `activate-profile` | nein | Profil-Variante aktivieren (`--game`, `--variant`, `--dry-run`) |

Beispiele:

```
python cli.py games --json --sort size --limit 10
python cli.py suggestions --days 365 --min-gb 20
python cli.py move-game --game "Golf With Your Friends" --target "C:\Program Files (x86)\Steam" --dry-run
```

`--game` akzeptiert den `key` aus `games` (z. B. `Steam:730`), die AppID, den Namen oder einen eindeutigen Namensteil. Die Sicherheitsregeln (Platzprüfung, Blocker, Rollback, Steam-Screenshots nur kopieren) gelten genauso wie in der Oberfläche. `move-game` hat in `toolhub.json` ein Zeitlimit von 2 h, weil große Spiele lange kopieren.

Für Tests lenken die Umgebungsvariablen `GAMEWARDEN_STEAM_ROOT`, `GAMEWARDEN_EPIC_MANIFESTS`, `GAMEWARDEN_EPIC_DAT` und `GAMEWARDEN_DATA` alle Zugriffe auf Testdaten um. `tests/test_cli.py` führt jeden Befehl aus `toolhub.json` aus und prüft, dass gültiges JSON herauskommt; schreibende Befehle laufen dabei nur an Testdaten.

## Einstellungen

`%APPDATA%\GameWarden\config.json`: Schwellen der Vorschläge, eigene Screenshot-Ordner, Screenshot-Ziel, Überwachung an/aus.

## Tests

Die schreibenden Funktionen (Umzug inkl. Rollback-Fälle, Screenshot-Sortierung, Profil-Aktivierung, Auto-Umschalten) werden nur an Testdaten in temporären Ordnern geprüft:

```
python -m unittest discover -s tests -v
```

## Aufbau

```
gamewarden/
  vdf.py          Parser/Writer für Valve-KeyValues
  library.py      Steam/Epic einlesen, Deinstallations-Vorschläge
  mover.py        Umzugsplanung + Ausführung mit Rollback
  screenshots.py  Quellen, Scan, Sortier-Vorschau + Ausführung
  profiles.py     Profil-/Variantenspeicher, Auto-Umschalter
  util.py         Größen, Prozessliste (Toolhelp32), Konfiguration
  gui.py          tkinter-Oberfläche
cli.py            JSON-Kommandozeile für Claude/ToolHub
toolhub.json      Befehlsbeschreibung für ToolHub
```

## Bekannte Grenzen
- Bei Epic gibt es keine Angabe „zuletzt gespielt“. Deinstallations-Vorschläge gibt es deshalb nur für Steam.
- Steam-Screenshots von nicht mehr installierten Spielen heißen `Steam-App <id>`.
- GOG, Ubisoft Connect, EA App usw. werden noch nicht gelesen.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
