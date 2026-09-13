"""
value_analysis_gui.py
=====================
Value Analysis — price against the earnings that are supposed to justify it.

Launched from the dashboard as its own OS process, like every other tool
in the toolkit, so a hung SEC fetch cannot take the launcher with it.

WHAT THE CHART SHOWS
--------------------
A white price line over three coloured bands. Each band is bounded by a
multiple of the company's OWN earnings, so the bands rise and fall with
the earnings rather than sitting at fixed prices:

  * GREEN  — under both reference multiples
  * AMBER  — between them
  * RED    — over both

The two references are the NORMAL multiple (what this stock actually
traded at over the window, median monthly) and the BENCHMARK (15x, or the
growth rate capped at 30x for faster compounders). They are ordered by
value, not by name: a company the market has always distrusted trades
under 15x for decades, and calling normal the ceiling would paint that
stock backwards.

Reading it is one glance — where is the white line? — and the point of
the bands moving is that they separate "the stock went up" from "the
earnings went up". The right-hand shaded section is consensus, not
history. Loss years get an x on the axis and no band at all, because a
multiple of a loss is not a number.

Below it, the multiple actually paid over time, so a re-rating shows as a
re-rating instead of hiding inside the word "normal".

The y axis defaults to LOG, where equal percentage moves take equal
height. That is the honest default for this chart: the bands are a
multiple of earnings, a multiple is a percentage, and a 30% gap to the
normal line has to look the same at $10 as at $500 or the band widths
mislead. Linear is one dropdown away for reading a price off the axis,
and the panel says what each choice costs.

Everything is computed in value_engine.py, which has no GUI imports and
can be checked from a REPL:

    python value_engine.py MSFT eps 15
"""

# ── GLOBAL yfinance RATE LIMITER ────────────────────────────────
# Must be imported BEFORE anything that touches yfinance.
import yfinance_throttle  # noqa: F401  # installs global throttle

import csv
import os
import queue
import threading
import tkinter as tk
from datetime import date, datetime
from tkinter import filedialog

import value_engine as ve

NAN = float("nan")

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
PURPLE = "#8957e5"
BORDER = "#313244"

FONT_HD = ("Consolas", 15, "bold")
FONT_LG = ("Consolas", 11, "bold")
FONT    = ("Consolas", 10)
FONT_SM = ("Consolas", 9)

METRIC_CHOICES = [
    ("Diluted EPS",                  "eps"),
    ("Operating cash flow / share",  "ocf"),
    ("Revenue / share",              "revenue"),
    ("Dividends / share",            "dividends"),
]
WINDOW_CHOICES = ["5", "10", "15", "20"]
FORECAST_CHOICES = ["0", "1", "2", "3", "4", "5"]
BASIS_CHOICES = [
    ("Consensus growth on GAAP actual", "growth"),
    ("Consensus level (adjusted)",      "absolute"),
]


def _scale_note(a, log_scale):
    """Why the y axis looks the way it does, in one short paragraph.

    The log axis is the right default here and also the one that surprises
    people, so it gets explained rather than defended."""
    span = None
    if a.price_values:
        lo = min(v for v in a.price_values if v > 0)
        span = max(a.price_values) / lo
    if log_scale:
        out = ["  LOG scale: equal PERCENTAGE moves take equal height, so",
               "  $10 to $20 is drawn the same height as $50 to $100. The",
               "  bands are a multiple of earnings, and a multiple is a",
               "  percentage — 30% over the normal line has to look the same",
               "  at $10 as at $500, or the band widths mislead."]
        if span and span >= 8:
            out.append(f"  This stock has spanned {span:,.0f}x. On a linear "
                       f"axis its")
            out.append("  early years would flatten into the bottom edge.")
        return out
    out = ["  LINEAR scale: equal DOLLAR moves take equal height. Easier to",
           "  read a price off, but a given percentage gap to a band looks",
           "  bigger at high prices than at low ones, so the bands are no",
           "  longer comparable across the chart."]
    if span and span >= 8:
        out.append(f"  Careful here: this stock has spanned {span:,.0f}x, so "
                   f"the early")
        out.append("  years are compressed against the bottom.")
    return out


def _how_to_read(a):
    """The chart's own key, in words, written against THIS ticker's numbers.

    The bands are the point of the chart and they are not self-explanatory
    on first sight, so the panel says what they are every time rather than
    assuming the reader remembers."""
    label = ve.METRIC_LABELS.get(a.metric, a.metric).lower()
    refs = a.references()
    out = [
        f"The white line is price. The coloured bands are what {a.ticker}",
        f"would be worth at a fixed multiple of its own {label},",
        "so the bands rise and fall as the earnings do.",
        "",
    ]
    if len(refs) > 1:
        (lo_m, lo_n), (hi_m, hi_n) = refs[0], refs[-1]
        out += [f"  GREEN   below {lo_m:.1f}x — the {lo_n} line",
                f"  AMBER   between {lo_m:.1f}x and {hi_m:.1f}x",
                f"  RED     above {hi_m:.1f}x — the {hi_n} line",
                ""]
    elif refs:
        out += [f"  GREEN   below {refs[0][0]:.1f}x",
                f"  RED     above {refs[0][0]:.1f}x",
                ""]
    if a.normal_pe:
        out += [f"  normal {a.normal_pe:.1f}x — the multiple this stock has",
                f"    actually traded at (median monthly over "
                f"{a.window_years} years)."]
    out += [f"  benchmark {a.benchmark_pe:.1f}x — the outside yardstick:",
            f"    {a.benchmark_rule.split('—')[-1].strip()}.",
            "",
            "Price above a band means the market is paying more per dollar",
            "of earnings than that reference asks. It does not mean the",
            "stock falls — earnings can rise into the price instead. The",
            "lower panel shows which of the two has been happening.",
            "",
            "The shaded right-hand section is consensus, not history."]
    return out


def _read_block(a, log_scale):
    return _how_to_read(a) + [""] + _scale_note(a, log_scale)


def _clip(text, n):
    return text if len(text) <= n else text[:n - 1] + "…"


def _month_grid(start, end):
    """One point per month from `start` to `end`. The zones are filled on
    this grid rather than on the fiscal years, so the bands bend smoothly
    with earnings instead of stepping once a year."""
    out, y, m = [], start.year, start.month
    while date(y, m, 1) <= end:
        out.append(date(y, m, 1))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out or [start]


def _bracketed_positive(fiscal_dates, fiscal_values, months):
    """True for each month whose surrounding fiscal years are BOTH profitable.

    The valuation lines are interpolated between fiscal years, so a year
    that swings from profit to loss drags the line through zero. Nothing
    drawn against that stretch means anything, and this marks it."""
    out = []
    n = len(fiscal_dates)
    for d in months:
        if n == 0 or d < fiscal_dates[0] or d > fiscal_dates[-1]:
            out.append(False)
            continue
        i = 0
        while i < n - 1 and fiscal_dates[i + 1] < d:
            i += 1
        j = min(i + 1, n - 1)
        out.append(fiscal_values[i] > 0 and fiscal_values[j] > 0)
    return out


class ValueAnalysisApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Value Analysis")
        self.root.geometry("1500x900")
        self.root.configure(bg=BG)
        self.root.minsize(1050, 680)

        self.analysis = None
        self._busy = False
        self._canvas = None
        self._figure = None
        self._auto_job = None
        self._results = queue.Queue()

        self._build_controls()
        self._build_body()
        self._build_status()
        self._show_placeholder()
        self._arm_auto_refresh()
        self._poll_results()

    # ─────────────────────────────────────────
    # CHROME
    # ─────────────────────────────────────────
    def _build_controls(self):
        bar = tk.Frame(self.root, bg=BG2)
        bar.pack(fill="x", side="top")

        row1 = tk.Frame(bar, bg=BG2)
        row1.pack(fill="x", padx=14, pady=(10, 2))

        tk.Label(row1, text="◆ VALUE ANALYSIS", font=FONT_HD, bg=BG2,
                 fg=ACCENT).pack(side="left", padx=(0, 18))

        tk.Label(row1, text="Ticker", font=FONT_SM, bg=BG2,
                 fg=FG_DIM).pack(side="left")
        self.ticker_var = tk.StringVar(value="MSFT")
        ent = tk.Entry(row1, textvariable=self.ticker_var, width=9,
                       font=FONT_LG, bg=BG3, fg=FG, insertbackground=ACCENT,
                       relief="flat", justify="center")
        ent.pack(side="left", padx=(6, 16), ipady=3)
        ent.bind("<Return>", lambda _e: self.run())
        ent.focus_set()

        self.metric_var = self._dropdown(
            row1, "Metric", [c[0] for c in METRIC_CHOICES],
            METRIC_CHOICES[0][0], width=26)
        self.window_var = self._dropdown(row1, "Window (yrs)",
                                         WINDOW_CHOICES, "15", width=4)
        self.forecast_var = self._dropdown(row1, "Forecast (yrs)",
                                           FORECAST_CHOICES, "3", width=3)

        self.run_btn = tk.Button(row1, text="▶  Analyze", font=FONT_LG,
                                 bg=ACCENT, fg="#000000", relief="flat",
                                 cursor="hand2", padx=18, pady=4,
                                 command=self.run)
        self.run_btn.pack(side="left", padx=(18, 0))

        row2 = tk.Frame(bar, bg=BG2)
        row2.pack(fill="x", padx=14, pady=(2, 10))

        self.basis_var = self._dropdown(
            row2, "Estimate basis", [c[0] for c in BASIS_CHOICES],
            BASIS_CHOICES[0][0], width=32)

        tk.Label(row2, text="Share-class divisor", font=FONT_SM, bg=BG2,
                 fg=FG_DIM).pack(side="left")
        self.divisor_var = tk.StringVar(value="1")
        tk.Entry(row2, textvariable=self.divisor_var, width=7, font=FONT,
                 bg=BG3, fg=FG, insertbackground=ACCENT, relief="flat",
                 justify="center").pack(side="left", padx=(6, 16), ipady=2)

        tk.Label(row2, text="SEC CIK override", font=FONT_SM, bg=BG2,
                 fg=FG_DIM).pack(side="left")
        self.cik_var = tk.StringVar(value="")
        tk.Entry(row2, textvariable=self.cik_var, width=10, font=FONT,
                 bg=BG3, fg=FG, insertbackground=ACCENT, relief="flat",
                 justify="center").pack(side="left", padx=(6, 12), ipady=2)

        self.scale_var = self._dropdown(row2, "Scale", ["Log", "Linear"],
                                        "Log", width=7)

        self.show_div = tk.BooleanVar(value=False)
        tk.Checkbutton(row2, text="dividends", variable=self.show_div,
                       font=FONT_SM, bg=BG2, fg=FG_DIM, selectcolor=BG3,
                       activebackground=BG2, activeforeground=FG,
                       relief="flat", bd=0, highlightthickness=0,
                       cursor="hand2").pack(side="left", padx=(0, 16))

        for text, cmd in (("⬇  Export CSV", self.export_csv),
                          ("🖼  Save chart", self.save_png),
                          ("⧉  Copy summary", self.copy_summary)):
            tk.Button(row2, text=text, font=FONT_SM, bg=BG3, fg=FG,
                      activebackground=BORDER, activeforeground=FG,
                      relief="flat", cursor="hand2", padx=10, pady=3,
                      command=cmd).pack(side="left", padx=3)

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

    def _dropdown(self, parent, label, options, default, width=14):
        tk.Label(parent, text=label, font=FONT_SM, bg=BG2,
                 fg=FG_DIM).pack(side="left")
        var = tk.StringVar(value=default)
        om = tk.OptionMenu(parent, var, *options)
        om.config(font=FONT, bg=BG3, fg=FG, activebackground=BORDER,
                  activeforeground=FG, relief="flat", highlightthickness=0,
                  width=width, anchor="w", cursor="hand2")
        om["menu"].config(font=FONT, bg=BG3, fg=FG,
                          activebackground=BORDER, activeforeground=FG)
        om.pack(side="left", padx=(6, 16))
        return var

    def _build_body(self):
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)

        self.chart_frame = tk.Frame(body, bg=BG)
        self.chart_frame.pack(side="left", fill="both", expand=True)

        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")

        right = tk.Frame(body, bg=BG, width=520)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        tk.Label(right, text="THE NUMBERS", font=FONT_LG, bg=BG,
                 fg=TEAL).pack(anchor="w", padx=14, pady=(12, 4))

        wrap = tk.Frame(right, bg=BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        sb = tk.Scrollbar(wrap, bg=BG2, troughcolor=BG, relief="flat",
                          bd=0, highlightthickness=0)
        sb.pack(side="right", fill="y")
        self.txt = tk.Text(wrap, font=FONT_SM, bg=BG2, fg=FG, relief="flat",
                           wrap="word", padx=12, pady=10, bd=0,
                           yscrollcommand=sb.set, insertbackground=ACCENT)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.config(command=self.txt.yview)

        self.txt.tag_config("head",  foreground=ACCENT, font=FONT_LG)
        self.txt.tag_config("warn",  foreground=RED)
        self.txt.tag_config("note",  foreground=FG_DIM)
        self.txt.tag_config("good",  foreground=GREEN)
        self.txt.tag_config("blue",  foreground=BLUE)

    def _build_status(self):
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x", side="bottom")
        bar = tk.Frame(self.root, bg=BG2, height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status = tk.Label(bar, text="Ready", font=FONT_SM, bg=BG2,
                               fg=FG_DIM, anchor="w")
        self.status.pack(side="left", padx=14)
        self.source_lbl = tk.Label(bar, text="", font=FONT_SM, bg=BG2,
                                   fg=FG_DIM, anchor="e")
        self.source_lbl.pack(side="right", padx=14)

    def _set_status(self, text, color=FG_DIM):
        self.status.config(text=text, fg=color)

    # ─────────────────────────────────────────
    # AUTO REFRESH
    # ─────────────────────────────────────────
    def _arm_auto_refresh(self):
        """Changing a dropdown re-runs the analysis on its own.

        Armed only after the widgets exist, so building them does not fire
        a fetch, and it stays quiet until one analysis has been asked for
        explicitly — the app should not hit the SEC on launch. The ticker
        box is deliberately NOT wired: it would fire on every keystroke.
        """
        for var in (self.metric_var, self.window_var, self.forecast_var,
                    self.basis_var):
            var.trace_add("write", self._auto_rerun)
        # These two are drawing choices, not new data — redraw, never refetch.
        self.show_div.trace_add("write", self._auto_redraw)
        self.scale_var.trace_add("write", self._auto_redraw)

    def _auto_rerun(self, *_):
        if self.analysis is None:
            return          # nothing has been run yet; wait to be asked
        # Debounced: clicking through a menu writes the variable several
        # times, and each write must not become its own network fetch.
        if self._auto_job:
            self.root.after_cancel(self._auto_job)
        self._auto_job = self.root.after(250, self._fire_auto)

    def _fire_auto(self):
        self._auto_job = None
        if self._busy:
            self._auto_job = self.root.after(400, self._fire_auto)
            return
        self.run()

    def _auto_redraw(self, *_):
        """Redraw from the analysis already in hand — no network, no refetch.
        The panel goes with it, because the scale note describes the axis
        currently on screen."""
        if self.analysis and not self.analysis.error:
            self._render_text(self.analysis)
            self._draw(self.analysis)

    # ─────────────────────────────────────────
    # RUN
    # ─────────────────────────────────────────
    def run(self):
        if self._busy:
            return
        ticker = self.ticker_var.get().strip().upper()
        if not ticker:
            self._set_status("Enter a ticker first", ACCENT)
            return

        metric = dict(METRIC_CHOICES)[self.metric_var.get()]
        basis = dict(BASIS_CHOICES)[self.basis_var.get()]
        window = int(self.window_var.get())
        fyears = int(self.forecast_var.get())
        try:
            divisor = float(self.divisor_var.get() or 1)
        except ValueError:
            self._set_status("Share-class divisor must be a number", RED)
            return
        cik_raw = self.cik_var.get().strip()
        try:
            cik = int(cik_raw) if cik_raw else None
        except ValueError:
            self._set_status("CIK override must be a number", RED)
            return

        self._busy = True
        self.run_btn.config(state="disabled", text="…  Working")
        self._set_status(f"Fetching {ticker} — SEC filings, then price "
                         f"history and consensus…", ACCENT)

        def work():
            try:
                a = ve.analyze(ticker, metric=metric, window_years=window,
                               forecast_years=fyears, forecast_basis=basis,
                               basis_divisor=divisor, cik=cik)
            except Exception as exc:      # a crash here must not kill the app
                a = ve.ValueAnalysis(ticker=ticker, metric=metric)
                a.error = f"{type(exc).__name__}: {exc}"
            # Hand the result back through a queue rather than calling
            # root.after() from this thread. Tk is not thread-safe, and
            # registering a callback from a worker raises outright when the
            # interpreter is not sitting in mainloop.
            self._results.put(a)

        threading.Thread(target=work, daemon=True).start()

    def _poll_results(self):
        """Drain finished analyses on the main thread — the only thread
        allowed to touch Tk."""
        try:
            while True:
                self._done(self._results.get_nowait())
        except queue.Empty:
            pass
        self.root.after(120, self._poll_results)

    def _done(self, a):
        self._busy = False
        self.run_btn.config(state="normal", text="▶  Analyze")
        self.analysis = a
        if a.error:
            self._set_status(f"{a.ticker}: no chart — see the panel", RED)
            self.source_lbl.config(text="")
            self._render_text(a)
            self._show_placeholder(a.error)
            return
        self._set_status(
            f"{a.ticker} — {len(a.fiscal_dates)} fiscal years, "
            f"{len(a.forecast_dates)} forecast, "
            f"{len(a.warnings)} warning(s)",
            RED if a.warnings else GREEN)
        self.source_lbl.config(
            text=f"SEC CIK {a.cik} · {a.source_tag or ''}")
        self._render_text(a)
        self._draw(a)

    # ─────────────────────────────────────────
    # TEXT PANEL
    # ─────────────────────────────────────────
    def _render_text(self, a):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        if a.error:
            self.txt.insert("end", f"{a.ticker}\n", "head")
            self.txt.insert("end", "\n" + a.error + "\n", "warn")
            self.txt.config(state="disabled")
            return

        self.txt.insert("end", "HOW TO READ THE CHART\n", "head")
        for line in _read_block(a, self.scale_var.get() != "Linear"):
            self.txt.insert("end", line + "\n", "note")
        self.txt.insert("end", "\n")

        for line in a.summary_text().split("\n"):
            tag = ""
            if line.startswith("  !"):
                tag = "warn"
            elif line.startswith("  -") or line.startswith("Source:"):
                tag = "note"
            elif line.endswith("=" * 10) or set(line.strip()) == {"="}:
                tag = "note"
            elif line and line == line.upper() and not line.startswith(" "):
                tag = "head"
            elif "vs price" in line:
                tag = "good" if "+" in line.split("vs price")[0][-9:] else "warn"
            elif "%/yr" in line:
                tag = "blue"
            self.txt.insert("end", line + "\n", tag)
        self.txt.config(state="disabled")

    # ─────────────────────────────────────────
    # CHART
    # ─────────────────────────────────────────
    def _clear_chart(self):
        for w in self.chart_frame.winfo_children():
            w.destroy()
        self._canvas = None
        self._figure = None

    def _show_placeholder(self, msg=None):
        self._clear_chart()
        wrap = tk.Frame(self.chart_frame, bg=BG)
        wrap.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(wrap, text="◆", font=("Consolas", 44), bg=BG,
                 fg=BORDER).pack()
        tk.Label(wrap, text=msg or "Enter a ticker and press Analyze",
                 font=FONT, bg=BG, fg=FG_DIM if not msg else RED,
                 wraplength=560, justify="center").pack(pady=(12, 0))
        if not msg:
            tk.Label(wrap,
                     text="Fundamentals come from SEC XBRL filings, price "
                          "from Yahoo.\nThe first fetch for a ticker "
                          "downloads a few MB and is then cached for a day.",
                     font=FONT_SM, bg=BG, fg=BORDER,
                     justify="center").pack(pady=(10, 0))

    def _draw(self, a):
        self._clear_chart()
        try:
            import matplotlib
            matplotlib.use("TkAgg")
            from matplotlib.figure import Figure
            from matplotlib.lines import Line2D
            import matplotlib.patheffects as pe
            from matplotlib.patches import Patch
            from matplotlib.backends.backend_tkagg import (
                FigureCanvasTkAgg, NavigationToolbar2Tk)
            from matplotlib.ticker import (FuncFormatter, LogLocator,
                                           MaxNLocator)
        except ImportError:
            self._show_placeholder(
                "matplotlib is not installed — pip install matplotlib")
            return

        fig = Figure(figsize=(10, 7), dpi=100, facecolor=BG)
        gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.16,
                              left=0.075, right=0.98, top=0.90, bottom=0.07)
        ax = fig.add_subplot(gs[0])
        ax2 = fig.add_subplot(gs[1], sharex=ax)

        for axis in (ax, ax2):
            axis.set_facecolor(BG)
            for spine in axis.spines.values():
                spine.set_color(BORDER)
            axis.tick_params(colors=FG_DIM, labelsize=8)
            axis.grid(True, color=BORDER, linewidth=0.5, alpha=0.55)

        f_dates = a.fiscal_dates + a.forecast_dates
        f_vals = a.fiscal_values + a.forecast_values

        pd_, pv = [], []
        if a.price_dates:
            pd_ = [d for d in a.price_dates if d >= a.fiscal_dates[0]]
            pv = [v for d, v in zip(a.price_dates, a.price_values)
                  if d >= a.fiscal_dates[0]]

        # ── the two reference multiples, ordered by VALUE not by name ──
        # "normal" is not always the higher of the two. A company the market
        # has always distrusted trades under 15x for decades, and treating
        # normal as the ceiling would paint that stock backwards.
        refs = sorted((m, n) for m, n in ((a.normal_pe, "normal"),
                                          (a.benchmark_pe, "benchmark")) if m)
        lo_mult, lo_name = refs[0] if refs else (None, "")
        hi_mult, hi_name = refs[-1] if len(refs) > 1 else (None, "")

        # ── one monthly grid spanning history AND forecast ──
        grid_end = max(f_dates[-1], pd_[-1]) if pd_ else f_dates[-1]
        grid = _month_grid(a.fiscal_dates[0], grid_end)
        drawable = _bracketed_positive(f_dates, f_vals, grid)
        band = [(m if (m and k and m > 0) else NAN) for m, k
                in zip(ve.interpolate(f_dates, f_vals, grid), drawable)]

        # ── y limits, fixed BEFORE anything is filled ──
        # A fill would otherwise drag the autoscale to the axis floor.
        log_scale = self.scale_var.get() != "Linear"
        cands = list(pv)
        for mult, _n in refs:
            cands += [v * mult for v in f_vals if v and v > 0]
        lo_y, hi_y = (min(cands or [1.0]), max(cands or [10.0]))
        if pv:
            lo_y = max(lo_y, min(pv) / 8.0)
            hi_y = min(hi_y, max(pv) * 8.0)
        if log_scale:
            ax.set_yscale("log")
            lo_y, hi_y = lo_y * 0.7, hi_y * 1.4
        else:
            # Linear pads by a share of the RANGE, not by a ratio: a ratio
            # pad on a linear axis leaves a huge dead strip under a $500
            # stock and none under a $5 one.
            pad = (hi_y - lo_y) * 0.08 or max(hi_y * 0.08, 0.5)
            lo_y, hi_y = max(0.0, lo_y - pad), hi_y + pad
        ax.set_ylim(lo_y, hi_y)
        ax.set_autoscale_on(False)
        lo_y, hi_y = ax.get_ylim()

        # ── THE ZONES ────────────────────────
        # Three stacked bands, each bounded by a multiple of the SAME
        # earnings stream, so the bands move with earnings. A band rising
        # under a flat price means the company grew into its valuation; a
        # flat band under a rising price means the market repriced it.
        lo_line = [b * lo_mult for b in band] if lo_mult else None
        hi_line = [b * hi_mult for b in band] if hi_mult else None
        GREEN_A, AMBER_A, RED_A = 0.20, 0.20, 0.13

        if lo_line:
            ax.fill_between(grid, lo_y, lo_line, color=GREEN, alpha=GREEN_A,
                            linewidth=0, zorder=1)
        if lo_line and hi_line:
            ax.fill_between(grid, lo_line, hi_line, color=ACCENT, alpha=AMBER_A,
                            linewidth=0, zorder=1)
        top_line = hi_line or lo_line
        if top_line:
            ax.fill_between(grid, top_line, hi_y, color=RED, alpha=RED_A,
                            linewidth=0, zorder=1)

        # the zone edges, in the colour of the zone below each
        if lo_line:
            ax.plot(grid, lo_line, color=GREEN, linewidth=1.4, zorder=4)
        if hi_line:
            ax.plot(grid, hi_line, color=ACCENT, linewidth=1.4, zorder=4)

        # ── the forecast half is consensus, not history ──
        if a.forecast_dates:
            cut = a.fiscal_dates[-1]
            ax.axvspan(cut, grid_end, color=BG, alpha=0.42, zorder=2)
            ax.axvline(cut, color=FG_DIM, linewidth=0.9, linestyle=":",
                       zorder=5)
            ax.text(cut, 0.985, "  consensus →",
                    transform=ax.get_xaxis_transform(), color=FG_DIM,
                    fontsize=8, va="top")

        # ── dividends, only when asked for ──
        show_div = (self.show_div.get() and a.metric != "dividends"
                    and any(a.dividends) and lo_mult)
        if show_div:
            dv = [(d or 0.0) * lo_mult for d in a.dividends]
            dv += [(v * a.payout_ratio * lo_mult) if a.payout_ratio else 0.0
                   for v in a.forecast_values]
            ax.plot(f_dates, [x if x > 0 else NAN for x in dv], color=PURPLE,
                    linewidth=1.2, linestyle="-.", alpha=0.9, zorder=5)

        # ── price, on top of everything ──
        # Outlined, because it has to stay readable crossing three bands.
        if pd_:
            ax.plot(pd_, pv, color="#FFFFFF", linewidth=1.7, zorder=6,
                    path_effects=[pe.Stroke(linewidth=3.2, foreground=BG),
                                  pe.Normal()])

        # ── loss years: no multiple exists, so no zone is drawn ──
        losses = [d for d, v in zip(a.fiscal_dates, a.fiscal_values) if v <= 0]
        for d in losses:
            ax.plot([d], [0.045], marker="x", markersize=7, color=RED,
                    transform=ax.get_xaxis_transform(), zorder=7,
                    clip_on=False)

        money = FuncFormatter(
            lambda v, _p: f"${v:,.0f}" if v >= 1 else f"${v:,.2f}")
        ax.yaxis.set_major_formatter(money)
        if log_scale:
            # A decade tick every 10x leaves at most two labels on a normal
            # price range, so the 2x and 5x rungs are labelled as well.
            ax.yaxis.set_minor_locator(LogLocator(base=10.0, subs=(2.0, 5.0),
                                                  numticks=24))
            ax.yaxis.set_minor_formatter(money)
            ax.tick_params(axis="y", which="minor", labelsize=7, colors=FG_DIM)
        else:
            ax.yaxis.set_major_locator(MaxNLocator(nbins=9))
        ax.set_ylabel(
            "price per share — log, equal % moves"
            if log_scale else "price per share — linear, equal $ moves",
            color=FG_DIM, fontsize=9)

        label = ve.METRIC_LABELS.get(a.metric, a.metric)
        ax.set_title(f"{a.ticker} — price against {label}", color=FG,
                     fontsize=12, pad=12, loc="left")

        # The company whose filings are actually being plotted, named in
        # full on the same line. A ticker is four letters and easy to fat
        # finger; this is the line that says whether the chart is the
        # company you meant.
        name = a.entity_name or a.quote_name
        if name:
            # When the filer and the quote disagree the second name is the
            # whole point, so it is shown and coloured as a warning rather
            # than buried in the panel.
            if a.quote_name and a.quote_name.strip().lower() != (
                    name.strip().lower()):
                name = f"{_clip(name, 30)}  ·  {_clip(a.quote_name, 30)}"
            ax.set_title(_clip(name, 64),
                         color=RED if a.name_mismatch else FG_DIM,
                         fontsize=9, pad=13, loc="right")

        # ── the key, written as what each band MEANS ──
        handles = [Line2D([], [], color=FG, linewidth=1.6, label="price")]
        if lo_mult and hi_mult:
            handles += [
                Patch(facecolor=GREEN, alpha=GREEN_A,
                      label=f"under both — cheaper than {lo_name} "
                            f"{lo_mult:.1f}×"),
                Patch(facecolor=ACCENT, alpha=AMBER_A,
                      label=f"between {lo_name} {lo_mult:.1f}× and "
                            f"{hi_name} {hi_mult:.1f}×"),
                Patch(facecolor=RED, alpha=RED_A,
                      label=f"over both — richer than {hi_name} "
                            f"{hi_mult:.1f}×"),
            ]
        elif lo_mult:
            handles += [
                Patch(facecolor=GREEN, alpha=GREEN_A,
                      label=f"under {lo_name} {lo_mult:.1f}×"),
                Patch(facecolor=RED, alpha=RED_A,
                      label=f"over {lo_name} {lo_mult:.1f}×"),
            ]
        if losses:
            handles.append(Line2D([], [], color=RED, marker="x",
                                  linestyle="none",
                                  label="loss year — no multiple exists"))
        if show_div:
            handles.append(Line2D([], [], color=PURPLE, linestyle="-.",
                                  label=f"dividends × {lo_mult:.0f}"))
        leg = ax.legend(handles=handles, loc="upper left", facecolor=BG2,
                        edgecolor=BORDER, fontsize=8, framealpha=0.95,
                        handlelength=1.7)
        for t in leg.get_texts():
            t.set_color(FG)

        # ── lower panel: the multiple actually paid ──
        if a.monthly_pe:
            mp = [(d, pe) for d, pe in zip(a.price_dates, a.monthly_pe)
                  if pe and 0 < pe < 500 and d >= a.fiscal_dates[0]]
            if mp:
                ax2.plot([p[0] for p in mp], [p[1] for p in mp],
                         color=TEAL, linewidth=1.2)
                if lo_mult:
                    ax2.axhline(lo_mult, color=GREEN, linewidth=1.0,
                                linestyle="--")
                if hi_mult:
                    ax2.axhline(hi_mult, color=ACCENT, linewidth=1.0,
                                linestyle="--")
                # A multiple printed on collapsing earnings runs to hundreds
                # and flattens the rest of the history into a floor. Cut the
                # axis at the 95th percentile and let the spike run off.
                vs = sorted(p[1] for p in mp)
                p95 = vs[max(0, int(len(vs) * 0.95) - 1)]
                top = max(p95, hi_mult or 0, lo_mult or 0) * 1.25
                ax2.set_ylim(0, top)
                if vs[-1] > top:
                    ax2.text(0.995, 0.9, f"peak {vs[-1]:,.0f}× off-scale",
                             transform=ax2.transAxes, ha="right", va="top",
                             color=FG_DIM, fontsize=7)
        ax2.set_ylabel("multiple paid", color=FG_DIM, fontsize=9)
        ax2.tick_params(labelsize=8)

        canvas = FigureCanvasTkAgg(fig, master=self.chart_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)

        tb_frame = tk.Frame(self.chart_frame, bg=BG2)
        tb_frame.pack(fill="x")
        tb_frame.option_add("*Background", BG2)
        tb_frame.option_add("*Foreground", FG)
        toolbar = NavigationToolbar2Tk(canvas, tb_frame)
        toolbar.config(bg=BG2)
        for child in toolbar.winfo_children():
            try:
                child.config(bg=BG2, fg=FG)
            except tk.TclError:
                pass
        toolbar.update()

        self._canvas, self._figure = canvas, fig

    # ─────────────────────────────────────────
    # EXPORT
    # ─────────────────────────────────────────
    def export_csv(self):
        a = self.analysis
        if not a or a.error:
            self._set_status("Nothing to export — run an analysis first",
                             ACCENT)
            return
        rows = a.to_rows()
        default = (f"value_{a.ticker}_{a.metric}_"
                   f"{datetime.now():%Y%m%d_%H%M}.csv")
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialfile=default,
            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([f"# {a.ticker} value analysis — "
                            f"{ve.METRIC_LABELS.get(a.metric, a.metric)}"])
                w.writerow([f"# source: SEC XBRL {a.source_tag}; "
                            f"CIK {a.cik}; price = Yahoo split-adjusted close"])
                w.writerow([f"# normal {a.normal_pe}, "
                            f"benchmark {a.benchmark_pe}, "
                            f"current {a.current_pe}"])
                for warn in a.warnings:
                    w.writerow([f"# WARNING: {warn}"])
                w.writerow(list(rows[0].keys()))
                for r in rows:
                    w.writerow(list(r.values()))
            self._set_status(f"Wrote {os.path.basename(path)}", GREEN)
        except OSError as exc:
            self._set_status(f"Export failed: {exc}", RED)

    def save_png(self):
        if not self._figure:
            self._set_status("No chart to save yet", ACCENT)
            return
        a = self.analysis
        default = f"value_{a.ticker}_{datetime.now():%Y%m%d_%H%M}.png"
        path = filedialog.asksaveasfilename(
            defaultextension=".png", initialfile=default,
            filetypes=[("PNG", "*.png")])
        if not path:
            return
        try:
            self._figure.savefig(path, facecolor=BG, dpi=150)
            self._set_status(f"Saved {os.path.basename(path)}", GREEN)
        except Exception as exc:
            self._set_status(f"Save failed: {exc}", RED)

    def copy_summary(self):
        a = self.analysis
        if not a:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(a.summary_text())
        self._set_status("Summary copied to clipboard", GREEN)


def main():
    root = tk.Tk()
    app = ValueAnalysisApp(root)

    def on_close():
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
