"""tkinter-Oberfläche von GameWarden."""
from __future__ import annotations

import os
import queue
import threading
import time
import tkinter as tk
import traceback
from collections import defaultdict
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import library, mover, profiles, screenshots
from .util import Config, fmt_date, fmt_size, open_in_explorer, running_processes

APP_TITLE = "GameWarden"


class Worker:
    """Führt eine Funktion im Hintergrund aus und liefert das Ergebnis im GUI-Thread."""

    def __init__(self, root: tk.Misc):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self._poll()

    def run(self, fn, on_done=None, on_error=None):
        def target():
            try:
                res = fn()
                self.q.put((on_done, res))
            except Exception as e:   # noqa: BLE001
                tb = traceback.format_exc()
                self.q.put((on_error or self._default_error, (e, tb)))
        threading.Thread(target=target, daemon=True).start()

    def call(self, fn, *args):
        """Thread-sicher eine Funktion im GUI-Thread ausführen."""
        self.q.put((lambda a: fn(*a), args))

    def _default_error(self, err):
        e, tb = err
        messagebox.showerror(APP_TITLE, f"{e}\n\n{tb[-1500:]}")

    def _poll(self):
        try:
            while True:
                cb, res = self.q.get_nowait()
                if cb:
                    cb(res)
        except queue.Empty:
            pass
        self.root.after(80, self._poll)


def _tree(parent, columns, headings, widths, anchors=None, height=18):
    frame = ttk.Frame(parent)
    tv = ttk.Treeview(frame, columns=columns, show="headings", height=height)
    for c, h, w in zip(columns, headings, widths):
        tv.heading(c, text=h)
        tv.column(c, width=w, anchor=(anchors or {}).get(c, "w"), stretch=c == columns[0])
    sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
    tv.configure(yscrollcommand=sb.set)
    tv.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return frame, tv


# ====================================================================== Bibliothek
class LibraryTab(ttk.Frame):
    COLS = ("name", "platform", "drive", "size", "last", "playtime", "hint")

    def __init__(self, master, app: "App"):
        super().__init__(master, padding=6)
        self.app = app
        self.games: list[library.Game] = []
        self.sugg: dict[str, str] = {}
        self.sort_col, self.sort_rev = "size", True

        bar = ttk.Frame(self)
        bar.pack(fill="x")
        ttk.Button(bar, text="Neu einlesen", command=self.reload).pack(side="left")
        ttk.Label(bar, text="  Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.refresh())
        ttk.Entry(bar, textvariable=self.filter_var, width=24).pack(side="left")
        self.only_sugg = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="nur Deinstallations-Vorschläge", variable=self.only_sugg,
                        command=self.refresh).pack(side="left", padx=8)
        ttk.Label(bar, text="Vorschlag ab").pack(side="left")
        self.days_var = tk.IntVar(value=app.cfg.get("unplayed_days", 365))
        ttk.Spinbox(bar, from_=7, to=3650, increment=30, width=6, textvariable=self.days_var,
                    command=self._thresholds).pack(side="left")
        ttk.Label(bar, text="Tagen ohne Spielen und ab").pack(side="left")
        self.gb_var = tk.IntVar(value=app.cfg.get("big_gb", 0))
        ttk.Spinbox(bar, from_=0, to=500, increment=5, width=5, textvariable=self.gb_var,
                    command=self._thresholds).pack(side="left")
        ttk.Label(bar, text="GB").pack(side="left")

        self.summary = ttk.Label(self, text="Lade …", foreground="#444")
        self.summary.pack(fill="x", pady=(6, 4))

        heads = ("Spiel", "Plattform", "Laufwerk", "Größe", "Zuletzt gespielt", "Spielzeit", "Vorschlag")
        frame, self.tv = _tree(self, self.COLS, heads, (300, 70, 70, 90, 120, 80, 280),
                               {"size": "e", "playtime": "e", "drive": "center", "platform": "center"})
        frame.pack(fill="both", expand=True)
        for c in self.COLS:
            self.tv.heading(c, command=lambda c=c: self._sort(c))
        self.tv.bind("<Double-1>", lambda e: self.open_folder())
        self.tv.tag_configure("sugg", foreground="#a33")

        bb = ttk.Frame(self)
        bb.pack(fill="x", pady=(6, 0))
        ttk.Button(bb, text="Ordner öffnen", command=self.open_folder).pack(side="left")
        ttk.Button(bb, text="Auf anderes Laufwerk verschieben …", command=self.move).pack(side="left", padx=6)
        ttk.Button(bb, text="Deinstallieren (über Steam) …", command=self.uninstall).pack(side="left")
        ttk.Button(bb, text="Echte Ordnergröße messen", command=self.measure).pack(side="left", padx=6)

    def _thresholds(self):
        try:
            self.app.cfg.set("unplayed_days", int(self.days_var.get()))
            self.app.cfg.set("big_gb", int(self.gb_var.get()))
        except (tk.TclError, ValueError):
            return
        self.refresh()

    def reload(self):
        self.summary.config(text="Lese Steam- und Epic-Bibliotheken …")
        self.app.worker.run(lambda: library.all_games(self.app.steam), self._loaded)

    def _loaded(self, games):
        self.games = games
        self.app.on_games_loaded(games)
        self.refresh()

    def selected(self) -> library.Game | None:
        sel = self.tv.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Bitte zuerst ein Spiel auswählen.")
            return None
        return next((g for g in self.games if g.key == sel[0]), None)

    def _sort(self, col):
        if self.sort_col == col:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_col, self.sort_rev = col, col in ("size", "playtime")
        self.refresh()

    def refresh(self):
        try:
            days, gb = int(self.days_var.get()), float(self.gb_var.get())
        except (tk.TclError, ValueError):
            days, gb = 365, 0
        self.sugg = library.suggestions(self.games, days, gb)
        flt = self.filter_var.get().lower().strip()
        rows = [g for g in self.games
                if (not flt or flt in g.name.lower() or flt in g.id.lower())
                and (not self.only_sugg.get() or g.key in self.sugg)]
        keyf = {
            "name": lambda g: g.name.lower(), "platform": lambda g: g.platform, "drive": lambda g: g.drive,
            "size": lambda g: g.size or 0, "last": lambda g: g.last_played or 0,
            "playtime": lambda g: g.playtime_min or 0, "hint": lambda g: self.sugg.get(g.key, ""),
        }[self.sort_col]
        rows.sort(key=keyf, reverse=self.sort_rev)
        self.tv.delete(*self.tv.get_children())
        for g in rows:
            pt = f"{g.playtime_min / 60:.0f} h" if g.playtime_min else "–"
            last = fmt_date(g.last_played) if g.platform == "Steam" else "–"
            self.tv.insert("", "end", iid=g.key, tags=("sugg",) if g.key in self.sugg else (),
                           values=(g.name, g.platform, g.drive, fmt_size(g.size), last, pt,
                                   self.sugg.get(g.key, "")))
        # Zusammenfassung pro Laufwerk
        per = defaultdict(lambda: [0, 0])
        for g in self.games:
            per[g.drive][0] += g.size or 0
            per[g.drive][1] += 1
        parts = []
        for d in sorted(per):
            try:
                import shutil
                free = fmt_size(shutil.disk_usage(d + "\\").free)
            except OSError:
                free = "?"
            parts.append(f"{d} {per[d][1]} Spiele, {fmt_size(per[d][0])} (frei: {free})")
        sug_bytes = sum(g.size or 0 for g in self.games if g.key in self.sugg)
        self.summary.config(text=f"{len(self.games)} Spiele  |  " + "  |  ".join(parts)
                            + f"  |  Vorschläge: {len(self.sugg)} Spiele, {fmt_size(sug_bytes)} freizugeben")

    def open_folder(self):
        g = self.selected()
        if g:
            open_in_explorer(g.install_dir)

    def measure(self):
        g = self.selected()
        if not g:
            return
        from .util import dir_size

        def done(res):
            size, files = res
            messagebox.showinfo(APP_TITLE, f"{g.name}\n{g.install_dir}\n\n{fmt_size(size)} in {files} Dateien\n"
                                           f"(laut Launcher: {fmt_size(g.size)})")
        self.app.worker.run(lambda: dir_size(g.install_dir), done)

    def uninstall(self):
        g = self.selected()
        if not g:
            return
        if g.platform != "Steam":
            messagebox.showinfo(APP_TITLE, "Epic-Spiele bitte im Epic Games Launcher deinstallieren "
                                           "(Bibliothek → … → Deinstallieren).")
            return
        if messagebox.askyesno(APP_TITLE, f"„{g.name}“ ({fmt_size(g.size)}) deinstallieren?\n\n"
                                          "GameWarden löscht selbst nichts – Steam öffnet seinen eigenen "
                                          "Deinstallations-Dialog, den du dort noch einmal bestätigen musst."):
            os.startfile(f"steam://uninstall/{g.id}")

    def move(self):
        g = self.selected()
        if g:
            MoveDialog(self, self.app, g)


class MoveDialog(tk.Toplevel):
    def __init__(self, master, app: "App", game: library.Game):
        super().__init__(master)
        self.app, self.game = app, game
        self.title(f"Verschieben: {game.name}")
        self.geometry("820x560")
        self.transient(master)
        self.plan: mover.MovePlan | None = None
        self.cancel = threading.Event()
        self.running = False

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text=f"Aktuell: {game.install_dir}").pack(anchor="w")
        row = ttk.Frame(top)
        row.pack(fill="x", pady=6)
        ttk.Label(row, text="Ziel:").pack(side="left")
        self.target = tk.StringVar()
        if game.platform == "Steam":
            libs = [str(l) for l in app.steam.libraries()
                    if os.path.normcase(str(l)) != os.path.normcase(str(game.library))]
            self.cb = ttk.Combobox(row, textvariable=self.target, values=libs, state="readonly", width=60)
            self.cb.pack(side="left", padx=4)
            if libs:
                self.cb.current(0)
            ttk.Label(top, text="Steam-Ziel muss eine in Steam angelegte Bibliothek sein "
                                "(Steam → Einstellungen → Speicher).", foreground="#666").pack(anchor="w")
        else:
            ttk.Entry(row, textvariable=self.target, width=60).pack(side="left", padx=4)
            ttk.Button(row, text="Ordner …", command=self._pick).pack(side="left")
            ttk.Label(top, text="Der Spielordner wird in den gewählten Ordner verschoben "
                                f"(→ <Ziel>\\{game.install_dir.name}).", foreground="#666").pack(anchor="w")
        ttk.Button(row, text="Vorschau erstellen", command=self.make_plan).pack(side="left", padx=6)

        self.text = tk.Text(self, wrap="word", height=20, font=("Consolas", 9))
        self.text.pack(fill="both", expand=True, padx=8)
        self.pb = ttk.Progressbar(self, maximum=1000)
        self.pb.pack(fill="x", padx=8, pady=4)
        self.status = ttk.Label(self, text="")
        self.status.pack(fill="x", padx=8)
        bb = ttk.Frame(self, padding=8)
        bb.pack(fill="x")
        self.go_btn = ttk.Button(bb, text="Umzug starten", command=self.start, state="disabled")
        self.go_btn.pack(side="left")
        self.cancel_btn = ttk.Button(bb, text="Abbrechen (mit Rollback)", command=self.cancel.set,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        ttk.Button(bb, text="Schließen", command=self._close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _pick(self):
        d = filedialog.askdirectory(parent=self, title="Zielordner wählen")
        if d:
            self.target.set(d)

    def _set_text(self, s):
        self.text.delete("1.0", "end")
        self.text.insert("end", s)

    def log(self, s):
        self.text.insert("end", s + "\n")
        self.text.see("end")

    def make_plan(self):
        t = self.target.get().strip()
        if not t:
            messagebox.showinfo(APP_TITLE, "Bitte ein Ziel wählen.", parent=self)
            return
        self.go_btn.config(state="disabled")
        self._set_text("Berechne Größe und prüfe Platz …")

        def build():
            if self.game.platform == "Steam":
                return mover.plan_steam_move(self.game, Path(t), self.app.steam)
            return mover.plan_epic_move(self.game, Path(t))

        def done(plan):
            self.plan = plan
            self._set_text(plan.describe())
            self.go_btn.config(state="normal" if plan.ok else "disabled")
        self.app.worker.run(build, done)

    def start(self):
        p = self.plan
        if not p or not p.ok:
            return
        launcher = "Steam" if self.game.platform == "Steam" else "den Epic Games Launcher"
        if not messagebox.askyesno(APP_TITLE,
                                   f"„{self.game.name}“ ({fmt_size(p.total_bytes)}) jetzt verschieben?\n\n"
                                   f"Nach\n{p.items[0].dst}\n\n{launcher} muss dabei geschlossen bleiben. "
                                   "Bei einem Fehler wird alles zurückgerollt.", parent=self):
            return
        self.running = True
        self.cancel.clear()
        self.go_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.log("\n--- Start " + time.strftime("%H:%M:%S"))
        t0 = time.monotonic()
        w = self.app.worker

        def prog(done, total, name):
            def ui(done=done, total=total, name=name):
                self.pb["value"] = 1000 * done / max(total, 1)
                el = time.monotonic() - t0
                rate = done / el if el > 0 else 0
                self.status.config(text=f"{fmt_size(done)} / {fmt_size(total)}  "
                                        f"({fmt_size(rate)}/s)  {name}")
            w.call(ui)

        def ok(leftovers):
            self.running = False
            self.cancel_btn.config(state="disabled")
            msg = "Umzug abgeschlossen. Starte jetzt " + launcher + " – das Spiel sollte ohne Download spielbar sein."
            if leftovers:
                msg += "\n\nHinweise:\n" + "\n".join(leftovers)
            self.log(msg)
            messagebox.showinfo(APP_TITLE, msg, parent=self)
            self.app.library.reload()

        def err(e):
            self.running = False
            self.cancel_btn.config(state="disabled")
            self.log(f"FEHLER: {e[0]}")
            messagebox.showerror(APP_TITLE, f"Umzug nicht durchgeführt, Änderungen zurückgerollt:\n\n{e[0]}",
                                 parent=self)
            self.app.library.reload()

        w.run(lambda: mover.execute(p, prog, self.cancel, log=lambda s: w.call(self.log, s)), ok, err)

    def _close(self):
        if self.running:
            messagebox.showwarning(APP_TITLE, "Umzug läuft noch. Zum Stoppen „Abbrechen“ verwenden.", parent=self)
            return
        self.destroy()


# ====================================================================== Screenshots
class ScreenshotTab(ttk.Frame):
    def __init__(self, master, app: "App"):
        super().__init__(master, padding=6)
        self.app = app
        self.shots: list[screenshots.Shot] = []

        top = ttk.Frame(self)
        top.pack(fill="x")
        left = ttk.LabelFrame(top, text="Quellen", padding=4)
        left.pack(side="left", fill="both", expand=True)
        self.src_list = tk.Listbox(left, height=6)
        self.src_list.pack(side="left", fill="both", expand=True)
        sb = ttk.Frame(left)
        sb.pack(side="left", fill="y", padx=4)
        ttk.Button(sb, text="Ordner hinzufügen …", command=self.add_source).pack(fill="x")
        ttk.Button(sb, text="Eigenen Ordner entfernen", command=self.remove_source).pack(fill="x", pady=2)

        right = ttk.LabelFrame(top, text="Sortieren nach", padding=4)
        right.pack(side="left", fill="both", padx=(6, 0))
        self.target = tk.StringVar(value=app.cfg.get("shot_target",
                                                     str(Path.home() / "Pictures" / "Spiele-Screenshots")))
        r = ttk.Frame(right)
        r.pack(fill="x")
        ttk.Entry(r, textvariable=self.target, width=48).pack(side="left")
        ttk.Button(r, text="…", width=3, command=self.pick_target).pack(side="left")
        ttk.Label(right, text="Aufbau: <Ziel>\\<Spiel>\\<Datei>", foreground="#666").pack(anchor="w")
        self.move_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="verschieben statt kopieren (Steam-Screenshots werden immer nur kopiert)",
                        variable=self.move_var).pack(anchor="w")
        self.video_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(right, text="auch Videos (mp4/mkv/webm)", variable=self.video_var).pack(anchor="w")
        rb = ttk.Frame(right)
        rb.pack(fill="x", pady=4)
        ttk.Button(rb, text="Scannen", command=self.scan).pack(side="left")
        ttk.Button(rb, text="Vorschau & Sortieren …", command=self.preview).pack(side="left", padx=4)

        self.info = ttk.Label(self, text="")
        self.info.pack(fill="x", pady=4)
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True)
        self.tv = ttk.Treeview(frame, columns=("count", "size", "source", "date"), show="tree headings")
        self.tv.heading("#0", text="Spiel / Datei")
        for c, h, w in (("count", "Anzahl", 70), ("size", "Größe", 90), ("source", "Quelle", 150),
                        ("date", "Datum", 120)):
            self.tv.heading(c, text=h)
            self.tv.column(c, width=w, stretch=False, anchor="e" if c in ("count", "size") else "w")
        self.tv.column("#0", width=420)
        s2 = ttk.Scrollbar(frame, command=self.tv.yview)
        self.tv.configure(yscrollcommand=s2.set)
        self.tv.pack(side="left", fill="both", expand=True)
        s2.pack(side="right", fill="y")
        self.tv.bind("<Double-1>", self._open)
        self.fill_sources()

    def sources(self):
        return screenshots.default_sources(self.app.steam, self.app.cfg.get("shot_folders", []))

    def fill_sources(self):
        self.src_list.delete(0, "end")
        for s in self.sources():
            tag = " (eigener Ordner)" if s.kind == "folder" else ""
            self.src_list.insert("end", f"{s.name}{tag}: {s.path}")

    def add_source(self):
        d = filedialog.askdirectory(title="Screenshot-Ordner wählen")
        if not d:
            return
        game = simpledialog.askstring(APP_TITLE, "Gehören alle Bilder zu einem Spiel? Dann Spielname eingeben.\n"
                                                 "Leer lassen = Unterordner/Dateinamen als Spiel verwenden.",
                                      parent=self) or ""
        lst = self.app.cfg.get("shot_folders", [])
        lst.append({"path": d, "name": Path(d).name, "game": game.strip()})
        self.app.cfg.set("shot_folders", lst)
        self.fill_sources()

    def remove_source(self):
        sel = self.src_list.curselection()
        if not sel:
            return
        src = self.sources()[sel[0]]
        if src.kind != "folder":
            messagebox.showinfo(APP_TITLE, "Automatisch gefundene Quellen können nicht entfernt werden.")
            return
        lst = [e for e in self.app.cfg.get("shot_folders", []) if Path(e["path"]) != src.path]
        self.app.cfg.set("shot_folders", lst)
        self.fill_sources()

    def pick_target(self):
        d = filedialog.askdirectory(title="Zielordner für sortierte Screenshots")
        if d:
            self.target.set(d)

    def scan(self):
        self.info.config(text="Suche Screenshots …")
        srcs = self.sources()
        names = self.app.steam_names
        vids = self.video_var.get()
        self.app.worker.run(lambda: screenshots.scan(srcs, names, vids), self._scanned)

    def _scanned(self, shots):
        self.shots = shots
        by = defaultdict(list)
        for s in shots:
            by[s.game].append(s)
        self.tv.delete(*self.tv.get_children())
        for game in sorted(by, key=lambda g: (g == screenshots.UNKNOWN, g.lower())):
            lst = sorted(by[game], key=lambda s: -s.mtime)
            gid = self.tv.insert("", "end", text=game, values=(len(lst), fmt_size(sum(s.size for s in lst)),
                                                               ", ".join(sorted({s.source for s in lst})), ""))
            for s in lst:
                self.tv.insert(gid, "end", iid=str(s.path), text=s.path.name,
                               values=("", fmt_size(s.size), s.source,
                                       time.strftime("%d.%m.%Y %H:%M", time.localtime(s.mtime))))
        self.info.config(text=f"{len(shots)} Dateien aus {len(by)} Spielen "
                              f"({fmt_size(sum(s.size for s in shots))}). Doppelklick öffnet eine Datei.")

    def _open(self, _e):
        sel = self.tv.selection()
        if sel and Path(sel[0]).is_file():
            os.startfile(sel[0])

    def preview(self):
        if not self.shots:
            messagebox.showinfo(APP_TITLE, "Bitte zuerst scannen.")
            return
        target = Path(self.target.get().strip())
        if not str(target):
            return
        self.app.cfg.set("shot_target", str(target))
        sel_games = {self.tv.item(i, "text") for i in self.tv.selection() if not self.tv.parent(i)}
        shots = [s for s in self.shots if not sel_games or s.game in sel_games]
        ops = screenshots.plan_sort(shots, target, self.move_var.get())
        SortPreview(self, self.app, ops, target)


class SortPreview(tk.Toplevel):
    def __init__(self, master, app, ops, target):
        super().__init__(master)
        self.app, self.ops = app, ops
        self.title("Vorschau: Screenshots sortieren")
        self.geometry("900x520")
        self.transient(master)
        cnt = defaultdict(int)
        for o in ops:
            cnt[o.action] += 1
        ttk.Label(self, padding=6, text=f"Ziel: {target}   |   kopieren: {cnt['kopieren']}   "
                                        f"verschieben: {cnt['verschieben']}   überspringen: {cnt['überspringen']}"
                  ).pack(fill="x")
        frame, tv = _tree(self, ("action", "src", "dst", "note"), ("Aktion", "Quelle", "Ziel", "Hinweis"),
                          (90, 330, 330, 150))
        frame.pack(fill="both", expand=True, padx=6)
        for o in ops[:5000]:
            tv.insert("", "end", values=(o.action, str(o.shot.path), str(o.dst), o.note))
        if len(ops) > 5000:
            tv.insert("", "end", values=("…", f"{len(ops) - 5000} weitere", "", ""))
        self.pb = ttk.Progressbar(self, maximum=max(1, cnt["kopieren"] + cnt["verschieben"]))
        self.pb.pack(fill="x", padx=6, pady=4)
        bb = ttk.Frame(self, padding=6)
        bb.pack(fill="x")
        self.go = ttk.Button(bb, text="Ausführen", command=self.run,
                             state="normal" if cnt["kopieren"] + cnt["verschieben"] else "disabled")
        self.go.pack(side="left")
        ttk.Button(bb, text="Schließen", command=self.destroy).pack(side="right")

    def run(self):
        n = sum(o.action != "überspringen" for o in self.ops)
        if not messagebox.askyesno(APP_TITLE, f"{n} Dateien jetzt kopieren/verschieben?\n"
                                              "Bei einem Fehler wird alles zurückgenommen.", parent=self):
            return
        self.go.config(state="disabled")
        w = self.app.worker

        def prog(i, total):
            w.call(lambda: self.pb.configure(value=i))

        def done(res):
            messagebox.showinfo(APP_TITLE, f"Fertig: {res[0]} kopiert, {res[1]} verschoben.", parent=self)
            self.destroy()
            self.app.shots.scan()

        def err(e):
            messagebox.showerror(APP_TITLE, f"Abgebrochen und zurückgenommen:\n{e[0]}", parent=self)
            self.go.config(state="normal")
        w.run(lambda: screenshots.execute_sort(self.ops, prog), done, err)


# ====================================================================== Profile
class ProfileTab(ttk.Frame):
    def __init__(self, master, app: "App"):
        super().__init__(master, padding=6)
        self.app = app
        self.store = profiles.ProfileStore()
        self.switcher = profiles.AutoSwitcher(self.store, lambda s: app.worker.call(self.log, s))

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True)
        left = ttk.Frame(pw)
        pw.add(left, weight=1)
        ttk.Label(left, text="Spiele mit Profilen").pack(anchor="w")
        self.plist = tk.Listbox(left, exportselection=False)
        self.plist.pack(fill="both", expand=True)
        self.plist.bind("<<ListboxSelect>>", lambda e: self.show())
        lb = ttk.Frame(left)
        lb.pack(fill="x", pady=4)
        ttk.Button(lb, text="Neu …", command=self.new_profile).pack(side="left")
        ttk.Button(lb, text="Entfernen", command=self.remove_profile).pack(side="left", padx=4)

        right = ttk.Frame(pw, padding=(8, 0))
        pw.add(right, weight=3)
        r = ttk.Frame(right)
        r.pack(fill="x")
        ttk.Label(r, text="Spiel-exe:").pack(side="left")
        self.exe_var = tk.StringVar()
        ttk.Entry(r, textvariable=self.exe_var, width=30).pack(side="left", padx=4)
        ttk.Button(r, text="Aus laufenden Prozessen …", command=self.pick_exe).pack(side="left")
        ttk.Button(r, text="Übernehmen", command=self.apply_exe).pack(side="left", padx=4)

        ff = ttk.LabelFrame(right, text="Dateien mit der Belegung", padding=4)
        ff.pack(fill="both", expand=True, pady=6)
        self.flist = tk.Listbox(ff, height=6)
        self.flist.pack(side="left", fill="both", expand=True)
        fb = ttk.Frame(ff)
        fb.pack(side="left", fill="y", padx=4)
        ttk.Button(fb, text="Datei hinzufügen …", command=self.add_file).pack(fill="x")
        ttk.Button(fb, text="Steam-Input-Dateien …", command=self.add_steam_input).pack(fill="x", pady=2)
        ttk.Button(fb, text="Entfernen", command=self.remove_file).pack(fill="x")

        vf = ttk.LabelFrame(right, text="Varianten", padding=4)
        vf.pack(fill="both", expand=True)
        self.vlist = tk.Listbox(vf, height=6, exportselection=False)
        self.vlist.pack(side="left", fill="both", expand=True)
        vb = ttk.Frame(vf)
        vb.pack(side="left", fill="y", padx=4)
        ttk.Button(vb, text="Aktuellen Stand speichern als …", command=self.save_variant).pack(fill="x")
        ttk.Button(vb, text="Aktivieren …", command=self.activate).pack(fill="x", pady=2)
        ttk.Button(vb, text="Löschen …", command=self.delete_variant).pack(fill="x")

        af = ttk.LabelFrame(right, text="Automatisch umschalten", padding=4)
        af.pack(fill="x", pady=6)
        ttk.Label(af, text="Beim Spielstart:").grid(row=0, column=0, sticky="w")
        self.start_cb = ttk.Combobox(af, state="readonly", width=28)
        self.start_cb.grid(row=0, column=1, padx=4)
        ttk.Label(af, text="Nach Spielende:").grid(row=0, column=2, sticky="w")
        self.exit_cb = ttk.Combobox(af, state="readonly", width=28)
        self.exit_cb.grid(row=0, column=3, padx=4)
        ttk.Button(af, text="Übernehmen", command=self.apply_auto).grid(row=0, column=4)
        self.auto_var = tk.BooleanVar(value=app.cfg.get("autoswitch", False))
        ttk.Checkbutton(af, text="Überwachung aktiv (solange GameWarden läuft)", variable=self.auto_var,
                        command=self.toggle_auto).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        self.logbox = tk.Text(self, height=6, font=("Consolas", 9))
        self.logbox.pack(fill="x", pady=(6, 0))
        self.fill_profiles()
        if self.auto_var.get():
            self.switcher.start()

    def log(self, s):
        self.logbox.insert("end", time.strftime("%H:%M:%S ") + s + "\n")
        self.logbox.see("end")

    def current(self) -> profiles.Profile | None:
        sel = self.plist.curselection()
        if not sel:
            return None
        return self.store.get(self.plist.get(sel[0]))

    def need(self):
        p = self.current()
        if not p:
            messagebox.showinfo(APP_TITLE, "Bitte links ein Spiel auswählen.")
        return p

    def fill_profiles(self, select: str | None = None):
        self.plist.delete(0, "end")
        for i, p in enumerate(sorted(self.store.profiles, key=lambda p: p.game.lower())):
            self.plist.insert("end", p.game)
            if p.game == select:
                self.plist.selection_set(i)
        self.show()

    def show(self):
        p = self.current()
        self.flist.delete(0, "end")
        self.vlist.delete(0, "end")
        self.exe_var.set(p.exe if p else "")
        vals = [""]
        if p:
            for f in p.files:
                mark = "" if Path(f).is_file() else "   (fehlt!)"
                self.flist.insert("end", f + mark)
            vs = self.store.variants(p)
            for v in vs:
                self.vlist.insert("end", v)
            vals += [v for v in vs if v != profiles.BACKUP_VARIANT]
        self.start_cb["values"] = vals
        self.exit_cb["values"] = vals
        self.start_cb.set(p.start_variant if p else "")
        self.exit_cb.set(p.exit_variant if p else "")

    def new_profile(self):
        names = sorted({g.name for g in self.app.games})
        dlg = ChooseDialog(self, "Neues Profil", "Spiel wählen oder Namen eintippen:", names, editable=True)
        name = (dlg.result or "").strip()
        if not name:
            return
        game = next((g for g in self.app.games if g.name == name), None)
        exe = ""
        if game and game.platform == "Epic" and game.extra.get("exe"):
            exe = Path(game.extra["exe"]).name
        try:
            self.store.add(profiles.Profile(name, exe=exe))
        except ValueError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return
        self.fill_profiles(name)

    def remove_profile(self):
        p = self.need()
        if p and messagebox.askyesno(APP_TITLE, f"Profil „{p.game}“ entfernen?\n"
                                                "Gespeicherte Varianten bleiben in "
                                                f"{self.store.game_dir(p)} erhalten."):
            self.store.remove(p.game)
            self.fill_profiles()

    def pick_exe(self):
        procs = sorted(running_processes())
        dlg = ChooseDialog(self, "Prozess wählen", "Laufender Prozess des Spiels:", procs)
        if dlg.result:
            self.exe_var.set(dlg.result)
            self.apply_exe()

    def apply_exe(self):
        p = self.need()
        if p:
            p.exe = self.exe_var.get().strip()
            self.store.save()
            self.log(f"{p.game}: exe = {p.exe or '(keine)'}")

    def add_file(self):
        p = self.need()
        if not p:
            return
        fs = filedialog.askopenfilenames(title="Konfigurationsdatei(en) mit der Tastenbelegung")
        for f in fs:
            f = str(Path(f))
            if f not in p.files:
                p.files.append(f)
        self.store.save()
        self.show()

    def add_steam_input(self):
        p = self.need()
        if not p:
            return
        game = next((g for g in self.app.games if g.name == p.game and g.platform == "Steam"), None)
        appid = game.id if game else simpledialog.askstring(APP_TITLE, "Steam-AppID des Spiels:", parent=self)
        if not appid:
            return
        files = profiles.steam_input_files(self.app.steam, appid)
        if not files:
            messagebox.showinfo(APP_TITLE, f"Keine Steam-Input-Konfiguration für App {appid} gefunden.\n"
                                           "Lege in Steam zuerst eine Controller-Belegung für das Spiel an.")
            return
        for f in files:
            if str(f) not in p.files:
                p.files.append(str(f))
        self.store.save()
        self.show()
        self.log(f"{p.game}: {len(files)} Steam-Input-Datei(en) hinzugefügt. "
                 "Hinweis: Steam sollte beim Umschalten geschlossen sein.")

    def remove_file(self):
        p = self.need()
        sel = self.flist.curselection()
        if p and sel:
            del p.files[sel[0]]
            self.store.save()
            self.show()

    def _variant(self):
        sel = self.vlist.curselection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Bitte eine Variante auswählen.")
            return None
        return self.vlist.get(sel[0])

    def save_variant(self):
        p = self.need()
        if not p:
            return
        name = simpledialog.askstring(APP_TITLE, "Name der Variante (z. B. Controller, Maus+Tastatur):",
                                      parent=self)
        if not name:
            return
        name = screenshots.safe_name(name)
        if name in self.store.variants(p) and not messagebox.askyesno(
                APP_TITLE, f"Variante „{name}“ existiert. Mit dem aktuellen Stand überschreiben?"):
            return
        try:
            n = self.store.save_variant(p, name)
            self.log(f"{p.game}: Variante „{name}“ gespeichert ({n} Dateien)")
        except Exception as e:   # noqa: BLE001
            messagebox.showerror(APP_TITLE, str(e))
        self.show()

    def activate(self):
        p = self.need()
        v = p and self._variant()
        if not v:
            return
        files = self.store.variant_files(p, v)
        lst = "\n".join(str(o) for _, o in files)
        if not messagebox.askyesno(APP_TITLE, f"Variante „{v}“ aktivieren?\n\nÜberschrieben werden:\n{lst}\n\n"
                                              f"Der jetzige Stand wird als „{profiles.BACKUP_VARIANT}“ gesichert."):
            return
        try:
            n = self.store.activate(p, v)
            self.log(f"{p.game}: „{v}“ aktiviert ({n} Dateien)")
        except Exception as e:   # noqa: BLE001
            messagebox.showerror(APP_TITLE, f"Fehlgeschlagen, zurückgerollt:\n{e}")
        self.show()

    def delete_variant(self):
        p = self.need()
        v = p and self._variant()
        if v and messagebox.askyesno(APP_TITLE, f"Gespeicherte Variante „{v}“ löschen?\n"
                                                "(Die Dateien des Spiels bleiben unverändert.)"):
            self.store.delete_variant(p, v)
            for attr in ("start_variant", "exit_variant"):
                if getattr(p, attr) == v:
                    setattr(p, attr, "")
            self.store.save()
            self.show()

    def apply_auto(self):
        p = self.need()
        if not p:
            return
        if (self.start_cb.get() or self.exit_cb.get()) and not p.exe:
            messagebox.showinfo(APP_TITLE, "Für automatisches Umschalten muss die Spiel-exe gesetzt sein.")
            return
        p.start_variant, p.exit_variant = self.start_cb.get(), self.exit_cb.get()
        self.store.save()
        self.log(f"{p.game}: Start → „{p.start_variant or '-'}“, Ende → „{p.exit_variant or '-'}“")

    def toggle_auto(self):
        self.app.cfg.set("autoswitch", self.auto_var.get())
        if self.auto_var.get():
            self.switcher.start()
        else:
            self.switcher.stop()


class ChooseDialog(tk.Toplevel):
    def __init__(self, master, title, prompt, items, editable=False):
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.result = None
        self.items = items
        ttk.Label(self, text=prompt, padding=6).pack(anchor="w")
        self.var = tk.StringVar()
        e = ttk.Entry(self, textvariable=self.var, width=50)
        e.pack(fill="x", padx=6)
        self.var.trace_add("write", lambda *_: self._filter())
        self.lb = tk.Listbox(self, height=16)
        self.lb.pack(fill="both", expand=True, padx=6, pady=4)
        self.lb.bind("<Double-1>", lambda _e: self.ok())
        self.lb.bind("<<ListboxSelect>>", lambda _e: None)
        bb = ttk.Frame(self, padding=6)
        bb.pack(fill="x")
        ttk.Button(bb, text="OK", command=self.ok).pack(side="right")
        ttk.Button(bb, text="Abbrechen", command=self.destroy).pack(side="right", padx=4)
        self.editable = editable
        self._filter()
        e.focus_set()
        self.bind("<Return>", lambda _e: self.ok())
        self.grab_set()
        self.wait_window()

    def _filter(self):
        f = self.var.get().lower()
        self.lb.delete(0, "end")
        for i in self.items:
            if f in i.lower():
                self.lb.insert("end", i)

    def ok(self):
        sel = self.lb.curselection()
        if sel:
            self.result = self.lb.get(sel[0])
        elif self.editable and self.var.get().strip():
            self.result = self.var.get().strip()
        elif self.lb.size() == 1:
            self.result = self.lb.get(0)
        self.destroy()


# ====================================================================== App
class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(APP_TITLE + " – Steam/Epic-Bibliothek, Umzug, Screenshots, Profile")
        self.root.geometry("1180x720")
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        self.cfg = Config()
        self.steam = library.SteamInstall()
        self.games: list[library.Game] = []
        self.steam_names: dict[str, str] = {}
        self.worker = Worker(self.root)
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True)
        self.library = LibraryTab(nb, self)
        self.shots = ScreenshotTab(nb, self)
        self.profiles = ProfileTab(nb, self)
        nb.add(self.library, text="Bibliothek")
        nb.add(self.shots, text="Screenshots")
        nb.add(self.profiles, text="Controller-Profile")
        if not self.steam.available:
            messagebox.showwarning(APP_TITLE, "Steam wurde nicht gefunden – es werden nur Epic-Spiele angezeigt.")
        self.library.reload()

    def on_games_loaded(self, games):
        self.games = games
        self.steam_names = {g.id: g.name for g in games if g.platform == "Steam"}

    def run(self):
        self.root.mainloop()


def main():
    App().run()
