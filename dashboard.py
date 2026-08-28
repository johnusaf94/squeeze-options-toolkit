"""
dashboard.py
============
Full-screen control center for the investing toolkit.

A big workspace with an options bar across the top — click a tool to
launch it. Each app still runs as its OWN independent OS process
(subprocess.Popen), so a crash or hang in one CANNOT touch the others.
This preserves the process-isolation the 4-app split exists to provide —
the dashboard is a launcher and status board, never a host.

Run:  python dashboard.py

Keep this file in the same folder as the four app files:
  stock_analysis_gui.py · squeeze_searcher_gui.py
  squeeze_analyzer_gui.py · portfolio_builder_gui.py
"""

import tkinter as tk
import subprocess
import sys
import os

BG     = "#0A0E14"
BG2    = "#12171F"
BG3    = "#1A2030"
FG     = "#CDD6F4"
FG_DIM = "#6C7086"
ACCENT = "#F4C430"
GREEN  = "#A6E3A1"
RED    = "#F38BA8"
BLUE   = "#89B4FA"
TEAL   = "#94E2D5"
BORDER = "#313244"

FONT_HD = ("Consolas", 20, "bold")
FONT_LG = ("Consolas", 13, "bold")
FONT    = ("Consolas", 11)
FONT_SM = ("Consolas", 9)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

APPS = [
    {
        "key": "stock", "tab": "📊  Stock Analysis",
        "file": "stock_analysis_gui.py", "title": "Stock Analysis",
        "desc": "Long-term quality scoring",
        "detail": "Buffett · Weiss · Bogle · Dalio · Druckenmiller · Minervini\n"
                  "FEY valuation · composite scoring · LM Studio Q&A",
        "color": BLUE,
    },
    {
        "key": "searcher", "tab": "🎯  Squeeze Searcher",
        "file": "squeeze_searcher_gui.py", "title": "Squeeze Searcher",
        "desc": "Universe scan + deep analysis",
        "detail": "Two-stage funnel · conviction matrix · catalyst timing\n"
                  "cross-sectional percentile · CSV export · auto-logging",
        "color": ACCENT,
    },
    {
        "key": "analyzer", "tab": "🔬  Squeeze Analyzer",
        "file": "squeeze_analyzer_gui.py", "title": "Squeeze Analyzer",
        "desc": "Single-stock deep dive",
        "detail": "Keith Gill + Chamath frameworks · focused one-ticker\n"
                  "analysis with model selector and Ask Claude",
        "color": TEAL,
    },
    {
        "key": "stocksearch", "tab": "🔎  Stock Searcher",
        "file": "stock_searcher_gui.py", "title": "Stock Searcher",
        "desc": "Quality investment universe scan",
        "detail": "Buffett/Weiss/Bogle/Dalio/Druckenmiller across tiered\n"
                  "universe · top 5 per framework + composite · CSV export",
        "color": "#A78BFA",
    },
    {
        "key": "journal", "tab": "📓  Options Journal",
        "file": "options_journal_gui.py", "title": "Options Journal",
        "desc": "Open positions & realized results",
        "detail": "Live marks (mid AND bid) · predicted EV vs realized\n"
                  "per-model-version scoring · Fidelity CSV import",
        "color": "#8957e5",
    },
    {
        "key": "portfolio", "tab": "🏗  Portfolio Builder",
        "file": "portfolio_builder_gui.py", "title": "Portfolio Builder",
        "desc": "Discovery & allocation",
        "detail": "Build and weight a portfolio from discovered candidates\n"
                  "with allocation logic and visual breakdown",
        "color": GREEN,
    },
]


class Dashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("Investing Toolkit")
        self.root.geometry("1100x720")
        self.root.configure(bg=BG)
        self.root.minsize(820, 540)

        self._procs = {}
        self._tab_btns = {}
        self._selected = None

        self._build_top_bar()
        self._build_workspace()
        self._build_status_bar()
        self._poll_processes()
        self._show_welcome()

    # ── TOP OPTIONS BAR ──────────────────────────
    def _build_top_bar(self):
        bar = tk.Frame(self.root, bg=BG2, height=64)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        tk.Label(bar, text="◆", font=("Consolas", 16), bg=BG2,
                 fg=ACCENT).pack(side="left", padx=(18, 6))
        tk.Label(bar, text="TOOLKIT", font=FONT_LG, bg=BG2,
                 fg=FG).pack(side="left", padx=(0, 20))

        for app in APPS:
            b = tk.Button(
                bar, text=app["tab"], font=FONT,
                bg=BG2, fg=FG_DIM, activebackground=BG3,
                activeforeground=FG, relief="flat", cursor="hand2",
                padx=14, pady=6, bd=0,
                command=lambda a=app: self._select(a["key"]),
            )
            b.pack(side="left", padx=2, pady=12)
            self._tab_btns[app["key"]] = b

        tk.Button(bar, text="✕  Quit", font=FONT_SM, bg=BG2, fg=RED,
                  activebackground=BG3, relief="flat", cursor="hand2",
                  padx=12, command=self._quit).pack(side="right",
                                                     padx=(4, 16))
        tk.Button(bar, text="⧉  Launch All", font=FONT_SM, bg=BG2,
                  fg=FG_DIM, activebackground=BG3, relief="flat",
                  cursor="hand2", padx=12,
                  command=self._launch_all).pack(side="right", padx=4)

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

    # ── BIG WORKSPACE ────────────────────────────
    def _build_workspace(self):
        self.workspace = tk.Frame(self.root, bg=BG)
        self.workspace.pack(fill="both", expand=True)

    def _clear_workspace(self):
        for w in self.workspace.winfo_children():
            w.destroy()

    def _show_welcome(self):
        self._clear_workspace()
        wrap = tk.Frame(self.workspace, bg=BG)
        wrap.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(wrap, text="◆", font=("Consolas", 46), bg=BG,
                 fg=BORDER).pack()
        tk.Label(wrap, text="Select a tool above to begin",
                 font=FONT_LG, bg=BG, fg=FG_DIM).pack(pady=(14, 6))
        tk.Label(wrap,
                 text="Each tool opens in its own window and runs "
                      "independently.\nA crash in one will never affect "
                      "the others.",
                 font=FONT_SM, bg=BG, fg=BORDER,
                 justify="center").pack()

    def _show_app_panel(self, app):
        self._clear_workspace()
        card = tk.Frame(self.workspace, bg=BG2)
        card.place(relx=0.5, rely=0.5, anchor="center",
                   width=560, height=380)

        tk.Frame(card, bg=app["color"], height=4).pack(fill="x")
        inner = tk.Frame(card, bg=BG2)
        inner.pack(fill="both", expand=True, padx=36, pady=26)

        tk.Label(inner, text=app["title"], font=FONT_HD, bg=BG2,
                 fg=app["color"]).pack(anchor="w")
        tk.Label(inner, text=app["desc"], font=FONT, bg=BG2,
                 fg=FG).pack(anchor="w", pady=(4, 16))
        tk.Label(inner, text=app["detail"], font=FONT_SM, bg=BG2,
                 fg=FG_DIM, justify="left").pack(anchor="w")

        running = self._is_running(app["key"])
        tk.Label(inner,
                 text=("● Running in its own window"
                       if running else "○ Not running"),
                 font=FONT_SM, bg=BG2,
                 fg=(GREEN if running else FG_DIM)).pack(anchor="w",
                                                          pady=(22, 12))

        btn_row = tk.Frame(inner, bg=BG2)
        btn_row.pack(anchor="w", fill="x")
        tk.Button(btn_row,
                  text=("↻  Relaunch" if running else "▶  Launch"),
                  font=("Consolas", 12, "bold"),
                  bg=app["color"], fg="#000000", relief="flat",
                  cursor="hand2", padx=22, pady=8,
                  command=lambda a=app: self._launch(a)).pack(side="left")
        if running:
            tk.Button(btn_row, text="■  Stop", font=FONT_SM,
                      bg=BG3, fg=RED, relief="flat", cursor="hand2",
                      padx=14, pady=8,
                      command=lambda k=app["key"]: self._stop(k)
                      ).pack(side="left", padx=8)

    # ── STATUS BAR ───────────────────────────────
    def _build_status_bar(self):
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x",
                                                       side="bottom")
        bar = tk.Frame(self.root, bg=BG2, height=30)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self._status = tk.Label(bar, text="Ready", font=FONT_SM,
                                 bg=BG2, fg=FG_DIM)
        self._status.pack(side="left", padx=14)
        self._running_lbl = tk.Label(bar, text="no tools running",
                                      font=FONT_SM, bg=BG2, fg=FG_DIM)
        self._running_lbl.pack(side="right", padx=14)

    # ── PROCESS MANAGEMENT ───────────────────────
    def _app_by_key(self, key):
        return next((a for a in APPS if a["key"] == key), None)

    def _is_running(self, key):
        p = self._procs.get(key)
        return p is not None and p.poll() is None

    def _select(self, key):
        self._selected = key
        for k, b in self._tab_btns.items():
            a = self._app_by_key(k)
            if k == key:
                b.config(fg=a["color"], bg=BG3)
            else:
                b.config(fg=FG_DIM, bg=BG2)
        self._show_app_panel(self._app_by_key(key))

    def _launch(self, app):
        fpath = os.path.join(BASE_DIR, app["file"])
        if not os.path.exists(fpath):
            self._status.config(
                text=f"❌ {app['file']} not found in this folder",
                fg=RED)
            return
        try:
            proc = subprocess.Popen([sys.executable, fpath],
                                     cwd=BASE_DIR)
            self._procs[app["key"]] = proc
            self._status.config(
                text=f"✓ Launched {app['title']} "
                     f"(independent process, pid {proc.pid})",
                fg=GREEN)
            if self._selected == app["key"]:
                self._show_app_panel(app)
        except Exception as e:
            self._status.config(text=f"❌ Launch failed: {e}", fg=RED)

    def _stop(self, key):
        p = self._procs.get(key)
        if p and p.poll() is None:
            try:
                p.terminate()
                self._status.config(
                    text=f"■ Stopped {self._app_by_key(key)['title']}",
                    fg=FG_DIM)
            except Exception as e:
                self._status.config(text=f"❌ Stop failed: {e}", fg=RED)
        if self._selected == key:
            self._show_app_panel(self._app_by_key(key))

    def _launch_all(self):
        missing = []
        for app in APPS:
            fpath = os.path.join(BASE_DIR, app["file"])
            if not os.path.exists(fpath):
                missing.append(app["file"])
                continue
            try:
                proc = subprocess.Popen([sys.executable, fpath],
                                         cwd=BASE_DIR)
                self._procs[app["key"]] = proc
            except Exception:
                missing.append(app["file"])
        if missing:
            self._status.config(
                text=f"⚠️ Launched available; missing: "
                     f"{', '.join(missing)}", fg=ACCENT)
        else:
            self._status.config(
                text="✓ All four tools launched "
                     "(four independent processes)", fg=GREEN)

    def _poll_processes(self):
        alive = [self._app_by_key(k)["title"]

                 for k in self._procs if self._is_running(k)]
        if alive:
            self._running_lbl.config(
                text="● " + "   ● ".join(alive), fg=GREEN)
        else:
            self._running_lbl.config(text="no tools running",
                                      fg=FG_DIM)
        self.root.after(2000, self._poll_processes)

    def _quit(self):
        # Independent processes are intentionally left running —
        # closing the launcher should not kill tools mid-scan.
        self.root.quit()


if __name__ == "__main__":
    root = tk.Tk()
    app = Dashboard(root)
    root.mainloop()

