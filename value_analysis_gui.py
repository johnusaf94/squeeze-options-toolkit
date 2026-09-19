"""
value_analysis_gui.py
=====================
Value Analysis — price against the earnings that are supposed to justify it.

Launched from the dashboard as its own OS process, like every other tool
in the toolkit, so a hung SEC fetch cannot take the launcher with it.

WHAT THE CHART SHOWS
--------------------
A white price line over coloured bands. Every band edge is a multiple of
the company's OWN earnings, so the bands rise and fall with the earnings
rather than sitting at fixed prices:

  * AMBER  — between the two reference multiples (the corridor)
  * GREEN  — below it, deepening the further below a band sits
  * RED    — above it, deepening the further above

Thin lines at round multiples (2x, 4x, 8x ... or finer) cut those zones
into countable steps, labelled on the right edge. They are a ruler: no
price axis, log or linear, can say "this is 6x sales", and these lines can.

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

THE VALUE METER
---------------
The right panel leads with one score, 0 (expensive) to 100 (cheap), built
from four visible parts — where today's multiple ranks in its own history,
its distance from the normal multiple and (for earnings) from the benchmark,
and the return consensus growth alone would deliver — each shown with its
weight. A confidence reading
pulls shaky scores toward 50, so erratic or thinly-filed earnings cannot
produce a loud number. The full write-up sits behind a toggle.

Everything is computed in value_engine.py, which has no GUI imports and
can be checked from a REPL:

    python value_engine.py MSFT eps 15
"""

# ── GLOBAL yfinance RATE LIMITER ────────────────────────────────
# Must be imported BEFORE anything that touches yfinance.
import yfinance_throttle  # noqa: F401  # installs global throttle

import csv
import math
import os
import queue
import threading
import tkinter as tk
from datetime import date, datetime, timedelta
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
COVER_EPS = "#B48EAD"       # earnings cover — the conventional figure
COVER_FCF = "#C792EA"       # cash cover — the one that decides

# Cash cover is drawn heavier: profit is an opinion, the cash left after
# capital spending is not.
COVER_SERIES = (("eps_cover", COVER_EPS, 1.0, "earnings cover"),
                ("fcf_cover", COVER_FCF, 1.6, "cash cover"))
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
# What the vertical scale has to contain. A fast grower's forecast runs
# orders of magnitude above today's price, and on a linear axis including
# it flattens the entire history into the bottom strip.
FIT_CHOICES = ["Auto", "Price only", "All data"]
FORECAST_CHOICES = ["0", "1", "2", "3", "4", "5"]
BASIS_CHOICES = [
    ("Consensus growth on GAAP actual", "growth"),
    ("Consensus level (adjusted)",      "absolute"),
]


# Band fills use stronger versions of the palette colours. The pastels wash
# out at the low opacity a stack of fills needs; these keep one step
# distinguishable from the next on a near-black ground.
FILL_GREEN = "#2EA043"
FILL_AMBER = "#D29922"
FILL_RED   = "#E5534B"
RUNG       = "#9AA4B8"

LADDER_CHOICES = [
    ("Every 2×",          "double"),
    ("Finer (1.5× / 2×)", "finer"),
    ("Off",               "off"),
]

# How far past the corridor a band must sit before its colour is at full
# strength: three doublings. A band 8x cheaper than the corridor is as
# green as the chart gets.
RAMP_SPAN = 8.0

# A ruler line this close to a reference line is dropped. Two lines a few
# pixels apart read as one smudge, and the reference is the one that
# carries meaning.
REF_CLEARANCE = 1.12

# How far the ruler reaches from the corridor: four doublings below it and
# three above. Beyond that a line is only ever on screen where earnings
# collapse toward zero, and all it draws is a fan converging on the dip.
RULER_BELOW = 16.0
RULER_ABOVE = 8.0


def _ladder(kmin, kmax, mode):
    """Round multiples between kmin and kmax, for the thin ruler lines.

    Geometric, so the lines sit evenly spaced on a log axis: each doubles
    the last (1, 2, 4, 8 ...) or doubles with a round midpoint (1, 1.5, 2,
    3, 4, 6, 8 ...). Round because "price is at 6x sales" is something a
    reader can hold in their head, and 5.66x is not."""
    if mode == "off" or not kmin or not kmax or kmin <= 0 or kmax <= kmin:
        return []
    steps = (1.0,) if mode == "double" else (1.0, 1.5)
    out = []
    e = math.floor(math.log2(kmin)) - 1
    while len(out) < 60:
        for s in steps:
            k = s * 2.0 ** e
            if k > kmax:
                return out
            if k >= kmin:
                out.append(k)
        e += 1
    return out


def _clear_of(k, ref_mults):
    return all(abs(math.log(k / r)) >= math.log(REF_CLEARANCE)
               for r in ref_mults if r)


# Yields people actually quote. A dividend chart's grid should read 2%, 3%,
# 4% — not 50x, 33x, 25x, which are the same lines written upside down.
YIELD_RUNGS_COARSE = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0)
YIELD_RUNGS_FINE = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 6.0,
                    7.0, 8.0, 10.0, 12.0, 15.0, 20.0)


# Where along each line its label is printed, staggered so neighbouring
# lines do not stack their labels in one column.
LABEL_FRACTIONS = (0.13, 0.31, 0.49, 0.67, 0.85)
REF_LABEL_FRACTION = 0.93


def _label_index(band, frac):
    """An index near `frac` along the series where the value is real, so a
    label never lands in a stretch the line does not exist in."""
    n = len(band)
    if not n:
        return None
    start = min(n - 1, max(0, int(n * frac)))
    for off in range(n):
        for i in (start - off, start + off):
            if 0 <= i < n and band[i] == band[i]:
                return i
    return None


def _yield_rungs(kmin, kmax, mode, ref_mults):
    """Ruler lines at round YIELDS instead of round multiples, returned as
    the multiples that draw them."""
    if mode == "off":
        return []
    grid = YIELD_RUNGS_FINE if mode == "finer" else YIELD_RUNGS_COARSE
    return [100.0 / y for y in grid
            if kmin <= 100.0 / y <= kmax and _clear_of(100.0 / y, ref_mults)]


def _usd(v):
    return "—" if v is None else f"${v:,.2f}"


def _usd_short(v):
    """Rail prices drop precision as they grow — the column is narrow and
    the cents on a $1,143 line are noise."""
    if v is None:
        return "—"
    if abs(v) >= 100:
        return f"${v:,.0f}"
    return f"${v:,.1f}" if abs(v) >= 10 else f"${v:,.2f}"


def _zone_style(k1, k2, lo, hi):
    """Fill colour and opacity for the band between multiples k1 and k2.

    Amber inside the reference corridor, green below it, red above it —
    and the further a band sits from the corridor, the stronger its colour,
    so depth reads as distance. An open end, where a band runs off to the
    chart's floor or ceiling, counts as one doubling past its inner edge."""
    k1 = k1 or k2 / 2.0
    k2 = k2 or k1 * 2.0
    mid = math.sqrt(k1 * k2)
    top = hi or lo
    if lo and mid < lo:
        d = min(1.0, math.log(lo / mid) / math.log(RAMP_SPAN))
        return FILL_GREEN, 0.10 + 0.30 * d
    if top and mid > top:
        d = min(1.0, math.log(mid / top) / math.log(RAMP_SPAN))
        return FILL_RED, 0.09 + 0.29 * d
    return FILL_AMBER, 0.24


# What the metric is called in a sentence: "7.6x sales", not "7.6x revenue /
# share".
METRIC_SHORT = {"eps": "earnings", "ocf": "cash flow", "revenue": "sales",
                "dividends": "dividends"}


def _mix(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t)
                                   for x, y in zip(a, b))


def _meter_color(score):
    """Red at 0, amber at 50, green at 100: the chart's own zone colours, so
    the meter and the bands speak the same language."""
    s = max(0.0, min(100.0, score))
    if s < 50:
        return _mix(FILL_RED, FILL_AMBER, s / 50.0)
    return _mix(FILL_AMBER, FILL_GREEN, (s - 50.0) / 50.0)


def _ladder_today(a, mode):
    """Every line near the price, priced at TODAY's metric, with the price
    slotted in where it falls — the dollar scale the chart cannot print.

    Returns (text, tag) pairs for the panel."""
    m, price = a.blended_now, a.price_now
    if not m or m <= 0 or not price:
        return []
    now = price / m
    refs = a.references()
    kmin, kmax = now / 4.5, now * 4.5
    if refs:
        kmin = min(kmin, refs[0][0] / 1.6)
        kmax = max(kmax, refs[-1][0] * 1.6)
    ref_mults = [r for r, _n in refs]
    if ve.is_yield_metric(a.metric):
        ladder = _yield_rungs(kmin, kmax, mode, ref_mults)
    else:
        ladder = [k for k in _ladder(kmin, kmax, mode)
                  if _clear_of(k, ref_mults)]
    rows = [(k, ve.mult_text(a.metric, k), "note") for k in ladder]
    rows += [(r, f"{n} {ve.mult_text(a.metric, r)}", "amber") for r, n in refs]
    rows.append((now, f"▶ price at {ve.mult_text(a.metric, now)}", "price"))
    rows.sort(key=lambda row: -row[0])
    label = ve.METRIC_PHRASE.get(a.metric, a.metric)
    if ve.is_yield_metric(a.metric):
        head = (f"  each line = the price at which today's {label} "
                f"({_usd(m)}) yields that")
    else:
        head = f"  each line = multiple × today's {label} ({_usd(m)})"
    out = [(head, "note"), ("", "")]
    out += [(f"  {name:<24}{_usd(k * m):>12}", tag) for k, name, tag in rows]
    return out


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


def _how_to_read(a, mode="double"):
    """The chart's own key, in words, written against THIS ticker's numbers.

    The bands are the point of the chart and they are not self-explanatory
    on first sight, so the panel says what they are every time rather than
    assuming the reader remembers."""
    label = ve.METRIC_PHRASE.get(a.metric, a.metric)
    refs = a.references()
    yld = ve.is_yield_metric(a.metric)
    mt = lambda k: ve.mult_text(a.metric, k)              # noqa: E731

    if yld:
        out = [f"The white line is price. Every other line is the price at",
               f"which {a.ticker}'s dividend would yield that percentage,",
               "so they all rise as the dividend rises.",
               "",
               "A multiple and a yield are the same number upside down:",
               "22x the dividend IS a 4.6% yield. Yield is how the number",
               "is quoted, so that is what the labels say.",
               ""]
    else:
        out = [f"The white line is price. Every other line is what {a.ticker}",
               f"would be worth at a fixed multiple of its own {label},",
               "so they all rise and fall with the business, not the stock.",
               ""]

    if len(refs) > 1:
        (lo_m, lo_n), (hi_m, hi_n) = refs[0], refs[-1]
        if yld:
            out += [f"  AMBER   the corridor: {hi_n} {mt(hi_m)} to "
                    f"{lo_n} {mt(lo_m)}",
                    f"  GREEN   yield above {mt(lo_m)} — the deeper, the "
                    f"cheaper",
                    f"  RED     yield below {mt(hi_m)} — the deeper, the "
                    f"richer",
                    ""]
        else:
            out += [f"  AMBER   the corridor: {lo_n} {mt(lo_m)} to "
                    f"{hi_n} {mt(hi_m)}",
                    f"  GREEN   below {mt(lo_m)} — the deeper, the cheaper",
                    f"  RED     above {mt(hi_m)} — the deeper, the richer",
                    ""]
    elif refs:
        out += [f"  GREEN   below {mt(refs[0][0])} — the deeper, the cheaper",
                f"  RED     above {mt(refs[0][0])} — the deeper, the richer",
                ""]

    if mode != "off" and yld:
        out += ["  THIN LINES are the yield grid, marked on the right edge:",
                "    2%, 3%, 4% ... Price sitting between the 3% and the 4%",
                "    line means the dividend yields between 3% and 4%.",
                ""]
    elif mode != "off":
        steps = ("2x, 4x, 8x, 16x" if mode == "double"
                 else "2x, 3x, 4x, 6x, 8x")
        out += ["  THIN LINES are a ruler, marked on the right edge:",
                f"    {steps} ... of {label}. Price sitting",
                "    between the 4x and 8x lines is being paid 4 to 8",
                "    times that figure.",
                ""]

    if a.normal_pe and yld:
        out += [f"  normal {mt(a.normal_pe)} — the yield this stock has",
                f"    actually paid (median monthly over "
                f"{a.window_years} years)."]
    elif a.normal_pe:
        out += [f"  normal {mt(a.normal_pe)} — the multiple this stock has",
                f"    actually traded at (median monthly over "
                f"{a.window_years} years)."]

    if yld and a.benchmark_pe:
        out += [f"  {a.benchmark_name} {mt(a.benchmark_pe)} — what government",
                "    debt pays for taking no risk at all. Above that line",
                "    the dividend beats bonds; below it you are paid less",
                "    than cash for holding equity, betting the dividend",
                "    grows into the gap.",
                "",
                "Price above a band means a LOWER yield than that reference.",
                "It does not mean the stock falls — the dividend can grow",
                "into the price instead. The lower panel plots this yield",
                "and the Treasury together, so the gap is visible."]
    elif yld:
        out += ["  No outside reference: the Treasury yield could not be",
                "    fetched, so only this stock's own normal yield is drawn.",
                "",
                "Price above a band means a LOWER yield than that reference."]
    else:
        out += [f"  benchmark {mt(a.benchmark_pe)} — the outside yardstick:",
                f"    {a.benchmark_rule.split('—')[-1].strip()}.",
                "",
                "Price above a band means the market is paying more per",
                "dollar of earnings than that reference asks. It does not",
                "mean the stock falls — earnings can rise into the price",
                "instead. The lower panel shows which has been happening."]
    out += ["", "The shaded right-hand section is consensus, not history."]
    return out


def _read_block(a, log_scale, mode="double"):
    return _how_to_read(a, mode) + [""] + _scale_note(a, log_scale)


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


def _blank_runs(grid, band, until, min_months=6):
    """Contiguous stretches with no positive metric, where no multiple
    exists and nothing can be drawn.

    Short gaps are skipped, since a label on a two-month sliver is noise,
    and so are runs that begin after the last filing — that is the end of
    the data rather than a loss."""
    runs, start = [], None
    for d, b in zip(grid, band):
        blank = not (b == b)              # NaN
        if blank and start is None:
            start = d
        elif not blank and start is not None:
            runs.append((start, d))
            start = None
    if start is not None:
        runs.append((start, grid[-1]))
    return [(s, e) for s, e in runs
            if s <= until and (e - s).days >= min_months * 30]


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
        self._drag = None
        self._toolbar = None
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
        row2.pack(fill="x", padx=14, pady=(2, 4))

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
                 justify="center").pack(side="left", padx=(6, 16), ipady=2)

        for text, cmd in (("⬇  Export CSV", self.export_csv),
                          ("🖼  Save chart", self.save_png),
                          ("⧉  Copy summary", self.copy_summary)):
            tk.Button(row2, text=text, font=FONT_SM, bg=BG3, fg=FG,
                      activebackground=BORDER, activeforeground=FG,
                      relief="flat", cursor="hand2", padx=10, pady=3,
                      command=cmd).pack(side="left", padx=3)

        # View choices redraw the chart already on screen and never refetch,
        # so they sit on their own row rather than among the inputs.
        row3 = tk.Frame(bar, bg=BG2)
        row3.pack(fill="x", padx=14, pady=(2, 10))
        tk.Label(row3, text="VIEW", font=FONT_SM, bg=BG2,
                 fg=TEAL).pack(side="left", padx=(0, 14))
        self.scale_var = self._dropdown(row3, "Scale", ["Log", "Linear"],
                                        "Log", width=7)
        self.fit_var = self._dropdown(row3, "Fit", FIT_CHOICES, "Auto",
                                      width=10)
        self.ladder_var = self._dropdown(
            row3, "Multiple lines", [c[0] for c in LADDER_CHOICES],
            LADDER_CHOICES[0][0], width=17)
        self.show_cover = tk.BooleanVar(value=False)
        tk.Checkbutton(row3, text="cover", variable=self.show_cover,
                       font=FONT_SM, bg=BG2, fg=FG_DIM, selectcolor=BG3,
                       activebackground=BG2, activeforeground=FG,
                       relief="flat", bd=0, highlightthickness=0,
                       cursor="hand2").pack(side="left", padx=(0, 10))

        self.show_div = tk.BooleanVar(value=False)
        tk.Checkbutton(row3, text="dividends", variable=self.show_div,
                       font=FONT_SM, bg=BG2, fg=FG_DIM, selectcolor=BG3,
                       activebackground=BG2, activeforeground=FG,
                       relief="flat", bd=0, highlightthickness=0,
                       cursor="hand2").pack(side="left", padx=(0, 16))

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

        self._meter = {}
        self._build_meter(right)

        # The full write-up is one click away, but it is reference material,
        # not the first thing a reader should have to parse.
        self._details_open = False
        self.details_btn = tk.Button(
            right, text="▸  Show full analysis", font=FONT_SM, bg=BG,
            fg=FG_DIM, activebackground=BG2, activeforeground=FG,
            relief="flat", bd=0, cursor="hand2", anchor="w",
            command=self._toggle_details)
        self.details_btn.pack(fill="x", padx=14, pady=(2, 6))

        wrap = tk.Frame(right, bg=BG)
        self.details_wrap = wrap
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
        self.txt.tag_config("amber", foreground=ACCENT)
        self.txt.tag_config("price", foreground="#FFFFFF",
                            font=("Consolas", 9, "bold"))

    # ─────────────────────────────────────────
    # VALUE METER
    # ─────────────────────────────────────────
    def _build_meter(self, parent):
        card = tk.Frame(parent, bg=BG2)
        card.pack(fill="x", padx=12, pady=(12, 6))

        head = tk.Frame(card, bg=BG2)
        head.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(head, text="VALUE METER", font=FONT_SM, bg=BG2,
                 fg=TEAL).pack(side="left")
        self.meter_sub = tk.Label(head, text="", font=FONT_SM, bg=BG2,
                                  fg=FG_DIM)
        self.meter_sub.pack(side="right")

        top = tk.Frame(card, bg=BG2)
        top.pack(fill="x", padx=14)
        self.meter_score = tk.Label(top, text="—", width=3, anchor="w",
                                    font=("Consolas", 40, "bold"),
                                    bg=BG2, fg=FG_DIM)
        self.meter_score.pack(side="left")
        col = tk.Frame(top, bg=BG2)
        col.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.meter_label = tk.Label(col, text="No reading", anchor="w",
                                    font=("Consolas", 16, "bold"),
                                    bg=BG2, fg=FG_DIM)
        self.meter_label.pack(anchor="w", pady=(12, 0))
        self.meter_caption = tk.Label(col, text="Run an analysis to score it.",
                                      font=FONT_SM, bg=BG2, fg=FG_DIM,
                                      anchor="w", justify="left",
                                      wraplength=350)
        self.meter_caption.pack(anchor="w")

        self.gauge = tk.Canvas(card, height=48, bg=BG2, highlightthickness=0)
        self.gauge.pack(fill="x", padx=14, pady=(6, 0))
        self.gauge.bind("<Configure>", lambda _e: self._paint_gauge())

        conf = tk.Frame(card, bg=BG2)
        conf.pack(fill="x", padx=14, pady=(2, 8))
        tk.Label(conf, text="Confidence", font=FONT_SM, bg=BG2,
                 fg=FG_DIM).pack(side="left")
        self.conf_bar = tk.Canvas(conf, width=150, height=8, bg=BG2,
                                  highlightthickness=0)
        self.conf_bar.pack(side="left", padx=8)
        self.conf_text = tk.Label(conf, text="", font=FONT_SM, bg=BG2,
                                  fg=FG_DIM)
        self.conf_text.pack(side="left")

        # Everything below the confidence row explains the score. It folds
        # away while the full analysis is open — which repeats it as text —
        # so the write-up gets the height instead of a squeezed strip.
        self.meter_more = tk.Frame(card, bg=BG2)
        self.meter_more.pack(fill="x")
        tk.Frame(self.meter_more, bg=BORDER, height=1).pack(fill="x", padx=14)
        self.why_frame = tk.Frame(self.meter_more, bg=BG2)
        self.why_frame.pack(fill="x", padx=14, pady=(8, 0))
        self.scen_frame = tk.Frame(self.meter_more, bg=BG2)
        self.scen_frame.pack(fill="x", padx=14)
        self.caution_frame = tk.Frame(self.meter_more, bg=BG2)
        self.caution_frame.pack(fill="x", padx=14)
        tk.Label(self.meter_more, font=("Consolas", 8), bg=BG2, fg=FG_DIM,
                 anchor="w", justify="left", wraplength=440,
                 text="Summarises the chart against this stock's own past. "
                      "Not a forecast, and not tested against what prices "
                      "did next.").pack(anchor="w", padx=14, pady=(6, 10))

    def _toggle_details(self):
        self._details_open = not self._details_open
        if self._details_open:
            self.meter_more.pack_forget()
            self.details_wrap.pack(fill="both", expand=True, padx=12,
                                   pady=(0, 12))
            self.details_btn.config(text="▾  Hide full analysis")
        else:
            self.details_wrap.pack_forget()
            self.meter_more.pack(fill="x")
            self.details_btn.config(text="▸  Show full analysis")

    @staticmethod
    def _paint_bar(canvas, score, width, color, height=8):
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill=BG3, outline="")
        if score is not None:
            canvas.create_rectangle(0, 0, max(2.0, width * score / 100.0),
                                    height, fill=color, outline="")

    def _paint_gauge(self):
        """Red-to-green scale with the score marked. When confidence has
        pulled the score toward the middle, a dashed tick shows where the
        raw reading was, so the pull is visible rather than silent."""
        g = self.gauge
        g.delete("all")
        w = g.winfo_width()
        if w < 60:
            return
        x0, x1 = 6, w - 6
        top, bot = 16, 30
        steps = 90
        for i in range(steps):
            xa = x0 + (x1 - x0) * i / steps
            xb = x0 + (x1 - x0) * (i + 1) / steps
            g.create_rectangle(xa, top, xb + 1, bot, outline="",
                               fill=_meter_color(100.0 * (i + 0.5) / steps))
        for cut in (f for f, _n in ve.METER_BANDS if f > 0):
            x = x0 + (x1 - x0) * cut / 100.0
            g.create_line(x, top, x, bot, fill=BG2, width=2)
        g.create_text(x0, bot + 10, text="expensive", anchor="w",
                      fill=FG_DIM, font=FONT_SM)
        g.create_text((x0 + x1) / 2, bot + 10, text="fair",
                      fill=FG_DIM, font=FONT_SM)
        g.create_text(x1, bot + 10, text="cheap", anchor="e",
                      fill=FG_DIM, font=FONT_SM)
        m = self._meter or {}
        if m.get("score") is None:
            return
        raw = m.get("raw")
        if raw is not None and abs(raw - m["score"]) >= 3:
            xr = x0 + (x1 - x0) * raw / 100.0
            g.create_line(xr, top - 3, xr, bot + 3, fill=FG, width=1,
                          dash=(2, 2))
        x = x0 + (x1 - x0) * m["score"] / 100.0
        g.create_polygon(x - 7, 3, x + 7, 3, x, 13, fill="#FFFFFF",
                         outline="")
        g.create_line(x, top - 2, x, bot + 2, fill="#FFFFFF", width=3)

    def _paint_scenarios(self, a):
        """Bear / base / bull, and the split between what growth delivers
        and what only a change of heart in the market delivers."""
        s = a.scenarios() if a else None
        if not s:
            return
        head = tk.Frame(self.scen_frame, bg=BG2)
        head.pack(fill="x", pady=(2, 2))
        tk.Label(head, text=f"IF HELD TO {s['target']}", font=FONT_SM,
                 bg=BG2, fg=TEAL).pack(side="left")
        exp = s["expected"]
        if exp is not None:
            tk.Label(head, text=f"{exp:+.0%}/yr expected",
                     font=("Consolas", 9, "bold"), bg=BG2,
                     fg=_meter_color(50 + 50 * math.tanh(exp / 0.25))
                     ).pack(side="right")

        for row in reversed(s["rows"]):           # bull at the top
            ann = row["annualised"]
            rr = row["rerating"]
            text = (f"  {row['name']:<5}{row['probability']:>4.0%}"
                    f"{_usd(row['exit_price']):>11}"
                    f"{('—' if rr is None else f'×{rr:.2f}'):>8}"
                    f"{('—' if ann is None else f'{ann:+.0%}/yr'):>10}")
            tk.Label(self.scen_frame, text=text, font=FONT_SM, bg=BG2,
                     fg=(FG_DIM if ann is None else
                         _meter_color(50 + 50 * math.tanh(ann / 0.25))),
                     anchor="w").pack(anchor="w")

        grow = s["expected_growth"]
        if grow is not None:
            tk.Label(self.scen_frame,
                     text=f"  {'growth alone, no re-rating':<28}"
                          f"{f'{grow:+.0%}/yr':>10}",
                     font=FONT_SM, bg=BG2, fg=FG_DIM,
                     anchor="w").pack(anchor="w")
        note = ("odds are this stock's own multiple percentiles, not a "
                "forecast; earnings span the analyst high-low")
        if s["floored"]:
            note += "; already below its 25th percentile, so the bear holds"
        elif s["capped"]:
            note += "; already above its 75th percentile, so the bull holds"
        tk.Label(self.scen_frame, text=note, font=("Consolas", 8), bg=BG2,
                 fg=FG_DIM, anchor="w", justify="left",
                 wraplength=440).pack(anchor="w", pady=(1, 4))

    def _render_meter(self, a):
        m = a.value_meter() if a else {}
        self._meter = m
        for w in self.why_frame.winfo_children():
            w.destroy()
        for w in self.caution_frame.winfo_children():
            w.destroy()
        for w in self.scen_frame.winfo_children():
            w.destroy()
        # A packed frame keeps its last size once its children are gone.
        # Without this the card stays tall when a scored ticker is replaced
        # by one with no reading.
        self.why_frame.config(height=1)
        self.caution_frame.config(height=1)
        self.scen_frame.config(height=1)
        self.meter_sub.config(
            text=f"{a.ticker} · {ve.METRIC_LABELS.get(a.metric, a.metric)}"
            if a else "")

        if not m or m.get("score") is None:
            self.meter_score.config(text="—", fg=FG_DIM)
            self.meter_label.config(text="No reading", fg=FG_DIM)
            self.meter_caption.config(text=(m or {}).get("reason") or "",
                                      fg=FG_DIM)
            self.conf_text.config(text="")
            self._paint_bar(self.conf_bar, None, 150, FG_DIM)
            self._paint_gauge()
            return

        color = _meter_color(m["score"])
        self.meter_score.config(text=f"{m['score']:.0f}", fg=color)
        self.meter_label.config(text=m["label"], fg=color)
        if ve.is_yield_metric(a.metric):
            caption = (f"{a.ticker} yielding "
                       f"{ve.mult_text(a.metric, a.current_pe)}  ·  "
                       f"{_usd(a.price_now)}")
        else:
            caption = (f"{a.ticker} at {a.current_pe:.1f}× "
                       f"{METRIC_SHORT.get(a.metric, a.metric)}  ·  "
                       f"{_usd(a.price_now)}")
        if abs(m["raw"] - m["score"]) >= 3:
            caption += (f"\nraw reading {m['raw']:.0f}, pulled toward fair "
                        f"by confidence")
        self.meter_caption.config(text=caption, fg=FG_DIM)

        conf = m["confidence"]
        conf_color = {"solid": TEAL, "mixed": ACCENT}.get(
            m["confidence_label"], RED)
        self._paint_bar(self.conf_bar, conf, 150, conf_color)
        self.conf_text.config(text=f"{conf:.0f} · {m['confidence_label']}",
                              fg=conf_color)

        for comp in m["components"]:
            row = tk.Frame(self.why_frame, bg=BG2)
            row.pack(fill="x", pady=(0, 7))
            line = tk.Frame(row, bg=BG2)
            line.pack(fill="x")
            shade = _meter_color(comp["score"])
            tk.Label(line, text=comp["title"], font=FONT, bg=BG2, fg=FG,
                     anchor="w").pack(side="left")
            tk.Label(line, text=f"  {comp['weight']:.0%} of score",
                     font=FONT_SM, bg=BG2, fg=FG_DIM,
                     anchor="w").pack(side="left")
            tk.Label(line, text=f"{comp['score']:.0f}", width=3, anchor="e",
                     font=("Consolas", 10, "bold"), bg=BG2,
                     fg=shade).pack(side="right")
            bar = tk.Canvas(line, width=110, height=8, bg=BG2,
                            highlightthickness=0)
            bar.pack(side="right", padx=(0, 8), pady=5)
            self._paint_bar(bar, comp["score"], 110, shade)
            tk.Label(row, text=comp["detail"], font=FONT_SM, bg=BG2,
                     fg=FG_DIM, anchor="w", justify="left",
                     wraplength=440).pack(anchor="w")

        self._paint_scenarios(a)

        if m["cautions"]:
            tk.Label(self.caution_frame, text="Confidence reduced by",
                     font=FONT_SM, bg=BG2, fg=ACCENT,
                     anchor="w").pack(anchor="w", pady=(2, 0))
            for why in m["cautions"]:
                tk.Label(self.caution_frame, text=f"  · {why}", font=FONT_SM,
                         bg=BG2, fg=ACCENT, anchor="w", justify="left",
                         wraplength=440).pack(anchor="w")
        self._paint_gauge()

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
        # View choices are drawing, not new data — redraw, never refetch.
        for var in (self.show_div, self.show_cover, self.scale_var,
                    self.ladder_var, self.fit_var):
            var.trace_add("write", self._auto_redraw)

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
            self._render_meter(a)
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
        self._render_meter(a)
        self._render_text(a)
        self._draw(a)

    # ─────────────────────────────────────────
    # TEXT PANEL
    # ─────────────────────────────────────────
    def _ladder_mode(self):
        return dict(LADDER_CHOICES).get(self.ladder_var.get(), "double")

    def _render_text(self, a):
        self.txt.config(state="normal")
        self.txt.delete("1.0", "end")
        if a.error:
            self.txt.insert("end", f"{a.ticker}\n", "head")
            self.txt.insert("end", "\n" + a.error + "\n", "warn")
            self.txt.config(state="disabled")
            return

        mode = self._ladder_mode()
        rows = _ladder_today(a, mode)
        if rows:
            self.txt.insert("end", "WHAT EACH LINE IS WORTH TODAY\n", "head")
            for line, tag in rows:
                self.txt.insert("end", line + "\n", tag)
            self.txt.insert("end", "\n")

        self.txt.insert("end", "HOW TO READ THE CHART\n", "head")
        for line in _read_block(a, self.scale_var.get() != "Linear", mode):
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
    def _on_scroll(self, event):
        """Wheel zooms the vertical scale about the cursor.

        No single default can show both a $6 price and a forecast line at
        $1,700, so the reader gets to move the window themselves."""
        ax = event.inaxes
        if ax is None or event.ydata is None:
            return
        lo, hi = ax.get_ylim()
        step = (1.0 / 1.15) if event.button == "up" else 1.15
        y = event.ydata
        if ax.get_yscale() == "log":
            if min(lo, hi, y) <= 0:
                return
            llo, lhi, ly = math.log10(lo), math.log10(hi), math.log10(y)
            ax.set_ylim(10.0 ** (ly + (llo - ly) * step),
                        10.0 ** (ly + (lhi - ly) * step))
        else:
            ax.set_ylim(y + (lo - y) * step, y + (hi - y) * step)
        if self._canvas:
            self._canvas.draw_idle()

    def _on_click(self, event):
        """Double-click resets the view; a press starts a drag."""
        if getattr(event, "dblclick", False) and self.analysis:
            self._drag = None
            self._draw(self.analysis)
            return
        if (event.button == 1 and event.inaxes is not None
                and not getattr(self._toolbar, "mode", "")):
            self._drag = {"ax": event.inaxes, "x": event.x, "y": event.y}

    def _on_motion(self, event):
        """Drag moves the view in both directions.

        Panning is done in PIXELS and applied incrementally: shifting the
        limits by a distance in data units would tear on a log axis, where
        the same number of dollars is a different distance at each end."""
        drag = self._drag
        if not drag or event.x is None or event.y is None:
            return
        ax = drag["ax"]
        px, py = event.x - drag["x"], event.y - drag["y"]
        if px == 0 and py == 0:
            return
        inv = ax.transData.inverted()
        (xa, ya) = ax.get_xlim()[0], ax.get_ylim()[0]
        (xb, yb) = ax.get_xlim()[1], ax.get_ylim()[1]
        da = ax.transData.transform((xa, ya))
        db = ax.transData.transform((xb, yb))
        na = inv.transform((da[0] - px, da[1] - py))
        nb = inv.transform((db[0] - px, db[1] - py))
        ax.set_xlim(na[0], nb[0])
        ax.set_ylim(na[1], nb[1])
        drag["x"], drag["y"] = event.x, event.y
        if self._canvas:
            self._canvas.draw_idle()

    def _on_release(self, _event):
        self._drag = None

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
            from matplotlib.dates import date2num
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
                              left=0.075, right=0.925, top=0.90, bottom=0.07)
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
        refs = a.references()
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
        # A log axis absorbs a runaway forecast; a linear one cannot. So the
        # default keeps the forecast in view on log and drops it on linear,
        # where including it would squash fifteen years of price into an
        # unreadable strip. Either way the reader can override it.
        fit = self.fit_var.get()
        with_forecast = fit == "All data" or (fit == "Auto" and log_scale)

        scale_vals = f_vals if with_forecast else list(a.fiscal_values)
        cands = list(pv)
        for mult, _n in refs:
            cands += [v * mult for v in scale_vals if v and v > 0]
            if a.blended_now and a.blended_now > 0:
                cands.append(a.blended_now * mult)      # today's lines
        lo_y, hi_y = (min(cands or [1.0]), max(cands or [10.0]))
        if pv:
            lo_y = max(lo_y, min(pv) / 8.0)
            hi_y = min(hi_y, max(pv) * (8.0 if with_forecast else 2.5))
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
        # Stacked bands, each bounded by a multiple of the SAME earnings
        # stream, so every band moves with earnings. A band rising under a
        # flat price means the company grew into its valuation; a flat band
        # under a rising price means the market repriced it.
        #
        # The thin ruler lines cut the three zones into steps that can be
        # counted — "price is between 4x and 8x sales" — and each step's
        # colour deepens with its distance from the reference corridor, so
        # one glance gives both which zone the price is in and how far in.
        mode = self._ladder_mode()
        ref_mults = [m for m, _n in refs]
        valid = [b for b in band if b == b]
        floor_y = lo_y if lo_y > 0 else hi_y * 0.02
        rungs = []
        if valid and ref_mults:
            kmin = max(floor_y / max(valid), ref_mults[0] / RULER_BELOW)
            kmax = min(hi_y / min(valid), ref_mults[-1] * RULER_ABOVE)
            if ve.is_yield_metric(a.metric):
                rungs = _yield_rungs(kmin, kmax, mode, ref_mults)
            else:
                rungs = [k for k in _ladder(kmin, kmax, mode)
                         if _clear_of(k, ref_mults)]
        breaks = sorted(set(rungs + ref_mults))

        # The open ends run far past the current view. Filling only to the
        # visible edge means zooming out tears a hole of dead space above
        # and below the bands, which is what the axes are for, not the data.
        FAR = 1000.0
        floor_fill = (lo_y / FAR) if log_scale else 0.0
        ceil_fill = hi_y * FAR
        if breaks:
            edges = [None] + breaks + [None]
            for k1, k2 in zip(edges[:-1], edges[1:]):
                color, alpha = _zone_style(k1, k2, lo_mult, hi_mult)
                lower = [b * k1 for b in band] if k1 else floor_fill
                upper = [b * k2 for b in band] if k2 else ceil_fill
                ax.fill_between(grid, lower, upper, color=color, alpha=alpha,
                                linewidth=0, zorder=1)

        # On a linear axis a reader can scroll below zero, where no price
        # can live. Say so instead of leaving a black void.
        if not log_scale:
            ax.axhspan(-ceil_fill, 0.0, facecolor=RED, alpha=0.10, zorder=0)
            ax.axhline(0.0, color=RED, linewidth=0.8, alpha=0.55, zorder=2)
            ax.annotate("below $0 — no price lives here", xy=(0.5, 0.0),
                        xycoords=("axes fraction", "data"),
                        xytext=(0, -7), textcoords="offset points",
                        ha="center", va="top", color=RED, fontsize=7.5,
                        alpha=0.85, zorder=6)

        for k in rungs:
            ax.plot(grid, [b * k for b in band], color=RUNG, linewidth=0.75,
                    alpha=0.6, zorder=3)
        # the corridor edges, heavier than the ruler
        for mult in ref_mults:
            ax.plot(grid, [b * mult for b in band], color=ACCENT,
                    linewidth=1.8, zorder=4)

        # ── stretches where no multiple exists ──
        # A loss year has no multiple, so no band is drawn across it. Saying
        # that on the chart is the difference between "not applicable" and
        # "this tool is broken": Wynn's 2020-2022 cash-flow losses blank four
        # years, and an unlabelled hole reads as missing data.
        for run_lo, run_hi in _blank_runs(grid, band, a.fiscal_dates[-1]):
            for axis in (ax, ax2):
                axis.axvspan(run_lo, run_hi, facecolor=BG3, alpha=0.5,
                             edgecolor=FG_DIM, linewidth=0.6, zorder=0)
            loss_here = any(
                v <= 0 for d, v in zip(a.fiscal_dates, a.fiscal_values)
                if run_lo - timedelta(days=400) <= d
                <= run_hi + timedelta(days=400))
            wide = (run_hi - run_lo).days > 700
            ax.text(run_lo + (run_hi - run_lo) / 2, 0.07 if wide else 0.5,
                    "no multiple — loss years" if loss_here else "no multiple",
                    transform=ax.get_xaxis_transform(), color=FG,
                    fontsize=8, ha="center", va="center",
                    rotation=0 if wide else 90, zorder=7)

        # ── the forecast half is consensus, not history ──
        if a.forecast_dates:
            cut = a.fiscal_dates[-1]
            ax.axvspan(cut, grid_end, color=BG, alpha=0.42, zorder=2)
            ax.axvline(cut, color=FG_DIM, linewidth=0.9, linestyle=":",
                       zorder=5)
            ax.text(cut, 0.985,
                    "  consensus →" if a.forecast_source == "consensus"
                    else "  extrapolated →",
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

        # Where the price stands today, read directly as a multiple — the
        # one number the ruler lines exist to let a reader estimate.
        if a.price_now and a.current_pe and lo_y < a.price_now < hi_y:
            today = date.today()
            ax.plot([today], [a.price_now], marker="o", markersize=5.5,
                    color="#FFFFFF", markeredgecolor=BG, markeredgewidth=1.2,
                    zorder=9)
            ax.annotate(f"now {ve.mult_text(a.metric, a.current_pe)}",
                        xy=(today, a.price_now),
                        xytext=(8, 9), textcoords="offset points",
                        color="#FFFFFF", fontsize=8, fontweight="bold",
                        zorder=9,
                        bbox=dict(boxstyle="round,pad=0.25", fc=BG2,
                                  ec=BORDER, alpha=0.92))

        # ── the scenario cone ────────────────
        # Where bear and bull land, drawn geometrically so the path is a
        # straight line on the log axis and a curve on the linear one —
        # the same compounding either way.
        scen = a.scenarios()
        if scen and a.price_now:
            t0, p0 = date.today(), a.price_now
            span_days = max((scen["target"] - t0).days, 1)
            steps = [t0 + timedelta(days=int(span_days * i / 12.0))
                     for i in range(13)]

            def path(exit_price):
                return [p0 * (exit_price / p0) ** (i / 12.0)
                        for i in range(13)]

            by = {r["name"]: r for r in scen["rows"]}
            ax.fill_between(steps, path(by["bear"]["exit_price"]),
                            path(by["bull"]["exit_price"]), color=TEAL,
                            alpha=0.18, linewidth=0, zorder=3)
            for name, style in (("bear", ":"), ("base", "--"), ("bull", ":")):
                row = by[name]
                ys = path(row["exit_price"])
                ax.plot(steps, ys, color=TEAL, linewidth=1.0,
                        linestyle=style, alpha=0.9, zorder=5)
                ax.plot([steps[-1]], [ys[-1]], marker="o", markersize=3.5,
                        color=TEAL, zorder=7)
                # The endpoints are named in the right rail, beside every
                # other line, rather than floating next to their dots.

        # ── loss years: no multiple exists, so no zone is drawn ──
        losses = [d for d, v in zip(a.fiscal_dates, a.fiscal_values) if v <= 0]
        for d in losses:
            ax.plot([d], [0.045], marker="x", markersize=7, color=RED,
                    transform=ax.get_xaxis_transform(), zorder=7,
                    clip_on=False)

        money = FuncFormatter(
            lambda v, _p: f"${v:,.0f}" if v >= 1 else f"${v:,.2f}")
        ax.yaxis.set_major_formatter(money)
        # The price scale is repeated on the right. On a chart this wide the
        # left edge is a long way from where the eye is when reading the
        # recent end of the line.
        ax.yaxis.set_ticks_position("both")
        ax.tick_params(axis="y", which="both", labelright=True,
                       labelleft=True)
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

        # Say so when the forecast leaves the top of the scale, rather than
        # letting the lines simply vanish off the edge.
        if a.forecast_dates and not with_forecast and refs:
            beyond = max((v * m for v in a.forecast_values if v and v > 0
                          for m, _n in refs), default=0.0)
            if beyond > hi_y:
                ax.text(a.fiscal_dates[-1], 0.94,
                        "  forecast runs above this scale — Fit: All data",
                        transform=ax.get_xaxis_transform(), color=FG_DIM,
                        fontsize=7.5, va="top", zorder=8)

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

        # ── the right rail ───────────────────
        # A gutter of black inside the axes where every line says what it is
        # AND what price it is at, beside the scenario endpoints. Parked in
        # the figure margin they were clipped; drawn on the lines they
        # collided with the price. Here they have room, they sit next to the
        # bear/base/bull outcomes, and they stay with their lines.
        x0, x1 = date2num(grid[0]), date2num(grid[-1])
        span = max(x1 - x0, 1.0)
        ax.set_xlim(x0 - span * 0.01, x1 + span * 0.19)
        rail_x = x1 + span * 0.015

        rail = []
        end_b = band[-1] if band else NAN
        if end_b == end_b:
            rail += [(mult * end_b, 0,
                      f"{name} {ve.mult_text(a.metric, mult)}  "
                      f"{_usd_short(mult * end_b)}", ACCENT, "bold")
                     for mult, name in refs]
            rail += [(k * end_b, 2,
                      f"{ve.mult_text(a.metric, k)}  "
                      f"{_usd_short(k * end_b)}", FG_DIM, "normal")
                     for k in rungs]
        if scen:
            rail += [(r["exit_price"], 1,
                      f"{r['name']} {_usd_short(r['exit_price'])}" +
                      ("" if r["annualised"] is None
                       else f"  {r['annualised']:+.0%}/yr"), TEAL, "bold")
                     for r in scen["rows"]]

        placed = []
        for y, prio, text, color, weight in sorted(rail,
                                                   key=lambda r: (r[1], -r[0])):
            if not lo_y < y < hi_y:
                continue
            py = ax.transData.transform((rail_x, y))[1]
            if any(abs(py - q) < 11 for q in placed):
                if prio == 2:
                    continue                  # a ruler line can be dropped
                while any(abs(py - q) < 11 for q in placed):
                    py += 11                  # a reference is nudged clear
                y = ax.transData.inverted().transform((0, py))[1]
            placed.append(py)
            ax.text(rail_x, y, text, color=color, fontsize=7.5,
                    fontweight=weight, va="center", ha="left", zorder=8)

        # Decided before the legend is built, drawn after it with the lower
        # panel — the legend has to know what the panel will show.
        cover_shown = []
        if self.show_cover.get() and a.coverage:
            cover_shown = [s for s in COVER_SERIES
                           if any(r[s[0]] is not None for r in a.coverage)]

        # ── the key, written as what each colour MEANS ──
        handles = [Line2D([], [], color=FG, linewidth=1.6, label="price")]
        mt = lambda k: ve.mult_text(a.metric, k)          # noqa: E731
        yld = ve.is_yield_metric(a.metric)
        if lo_mult and hi_mult:
            if yld:
                # The lower multiple is the HIGHER yield, so the green band
                # is the one above a yield, not below a multiple.
                cheap = f"yield above {lo_name} {mt(lo_mult)} — deeper is cheaper"
                mid = f"between {hi_name} {mt(hi_mult)} and {lo_name} {mt(lo_mult)}"
                rich = f"yield below {hi_name} {mt(hi_mult)} — deeper is richer"
            else:
                cheap = f"cheaper than {lo_name} {mt(lo_mult)} — deeper is cheaper"
                mid = f"between {lo_name} {mt(lo_mult)} and {hi_name} {mt(hi_mult)}"
                rich = f"richer than {hi_name} {mt(hi_mult)} — deeper is richer"
            handles += [
                Patch(facecolor=FILL_GREEN, alpha=0.32, label=cheap),
                Patch(facecolor=FILL_AMBER, alpha=0.26, label=mid),
                Patch(facecolor=FILL_RED, alpha=0.30, label=rich),
            ]
        elif lo_mult:
            handles += [
                Patch(facecolor=FILL_GREEN, alpha=0.32,
                      label=(f"yield above {lo_name} {mt(lo_mult)}" if yld
                             else f"cheaper than {lo_name} {mt(lo_mult)}")),
                Patch(facecolor=FILL_RED, alpha=0.30,
                      label=(f"yield below {lo_name} {mt(lo_mult)}" if yld
                             else f"richer than {lo_name} {mt(lo_mult)}")),
            ]
        if rungs:
            handles.append(Line2D(
                [], [], color=RUNG, linewidth=0.9,
                label=("thin lines — the yield grid, marked right" if yld
                       else f"thin lines — multiples of "
                            f"{ve.METRIC_PHRASE.get(a.metric, a.metric)}"
                            f", marked right")))
        if yld and a.treasury_history:
            handles.append(Line2D([], [], color=BLUE, linestyle="--",
                                  label="10-yr Treasury yield (lower panel)"))
        if scen:
            handles.append(Line2D(
                [], [], color=TEAL, linestyle="--",
                label=f"scenario cone — bear to bull, "
                      f"{scen['expected']:+.0%}/yr expected"
                      if scen["expected"] is not None
                      else "scenario cone — bear to bull"))
        if losses:
            handles.append(Line2D([], [], color=RED, marker="x",
                                  linestyle="none",
                                  label="loss year — no multiple exists"))
        if show_div:
            handles.append(Line2D([], [], color=PURPLE, linestyle="-.",
                                  label=f"dividends × {lo_mult:.0f}"))
        for _key, color, width, label in cover_shown:
            handles.append(Line2D([], [], color=color, linewidth=width,
                                  label=f"{label} (lower panel, right axis)"))
        leg = ax.legend(handles=handles, loc="upper left", facecolor=BG2,
                        edgecolor=BORDER, fontsize=8, framealpha=0.95,
                        handlelength=1.7)
        for t in leg.get_texts():
            t.set_color(FG)

        # ── lower panel: the multiple actually paid ──
        # The same zones, drawn flat, because on this axis the multiple IS
        # the height: the white line entering a colour here is the same
        # event as the price entering that colour above.
        # For a dividend the axis is flipped into yield, so the line reads
        # the way the number is actually quoted — and cheap is UP.
        def on_ax2(m):
            return (100.0 / m) if (yld and m and m > 0) else m

        if a.monthly_pe:
            mp = [(d, x) for d, x in zip(a.price_dates, a.monthly_pe)
                  if x and 0 < x < 500 and d >= a.fiscal_dates[0]]
            if mp:
                # A multiple printed on collapsing earnings runs to hundreds
                # and flattens the rest of the history into a floor. Cut the
                # axis at the 95th percentile and let the spike run off.
                vs = sorted(on_ax2(x) for _d, x in mp)
                p95 = vs[max(0, int(len(vs) * 0.95) - 1)]
                # The risk-free line shares this axis, so it has to fit on it.
                tre = [(d, v) for d, v in a.treasury_history
                       if yld and d >= a.fiscal_dates[0]]
                top = max([p95] + [on_ax2(m) for m in ref_mults if m]
                          + [v for _d, v in tre]) * 1.25
                ax2.set_ylim(0, top)
                ax2.set_autoscaley_on(False)
                if breaks:
                    edges = [None] + breaks + [None]
                    for k1, k2 in zip(edges[:-1], edges[1:]):
                        color, alpha = _zone_style(k1, k2, lo_mult, hi_mult)
                        # An open end runs to the top of the panel, and which
                        # end that is flips with the axis.
                        y1 = on_ax2(k1) if k1 else (top if yld else 0.0)
                        y2 = on_ax2(k2) if k2 else (0.0 if yld else top)
                        ax2.axhspan(min(y1, y2), max(y1, y2), color=color,
                                    alpha=alpha, linewidth=0, zorder=0)
                for mult in ref_mults:
                    ax2.axhline(on_ax2(mult), color=ACCENT, linewidth=1.0,
                                linestyle="--", zorder=2)
                ax2.plot([p[0] for p in mp], [on_ax2(p[1]) for p in mp],
                         color="#FFFFFF", linewidth=1.3, zorder=3,
                         path_effects=[pe.Stroke(linewidth=2.8,
                                                 foreground=BG),
                                       pe.Normal()])
                # What the same money earned risk-free, on the same axis.
                # The gap between the two lines IS the question.
                if tre:
                    ax2.plot([d for d, _v in tre], [v for _d, v in tre],
                             color=BLUE, linewidth=1.2, linestyle="--",
                             zorder=4)
                    ax2.annotate("10-yr Treasury", xy=tre[-1],
                                 xytext=(5, 7), textcoords="offset points",
                                 color=BLUE, fontsize=7, va="center",
                                 ha="left", zorder=5)
                if vs[-1] > top:
                    ax2.text(0.995, 0.9,
                             f"peak {vs[-1]:,.1f}% off-scale" if yld
                             else f"peak {vs[-1]:,.0f}× off-scale",
                             transform=ax2.transAxes, ha="right", va="top",
                             color=FG_DIM, fontsize=7)
        ax2.set_ylabel("dividend yield (%)" if yld else "multiple paid",
                       color=FG_DIM, fontsize=9)

        # ── dividend cover, on its own scale ──
        # How many times over the company paid for its dividend. Cash cover
        # is drawn heavier than earnings cover because it is the one that
        # decides whether the payment survives: profit is an opinion, the
        # cash left after capital spending is not.
        if cover_shown:
            cov = [r for r in a.coverage
                   if r["eps_cover"] is not None or r["fcf_cover"] is not None]
            if cov:
                ax3 = ax2.twinx()
                ax3.set_facecolor("none")
                ax3.patch.set_visible(False)
                top, bottom = 1.6, 0.0
                for key, color, width, _label in cover_shown:
                    pts = [(r["date"], r[key]) for r in cov
                           if r[key] is not None]
                    if not pts:
                        continue
                    top = max(top, max(v for _d, v in pts))
                    # A year the dividend was paid out of a cash LOSS is the
                    # single most important point on this line, so the axis
                    # is allowed below zero to show it.
                    bottom = min(bottom, min(v for _d, v in pts))
                    ax3.plot([d for d, _v in pts], [v for _d, v in pts],
                             color=color, linewidth=width, marker="o",
                             markersize=2.6, zorder=6)
                # Below this line the dividend costs more than the company
                # made that year.
                ax3.axhline(1.0, color=RED, linewidth=0.9, linestyle=":",
                            zorder=5)
                ax3.set_ylim(min(bottom * 1.2, 0.0), min(top * 1.2, 8.0))
                ax3.set_ylabel("dividend cover (×)", color=COVER_FCF,
                               fontsize=8)
                ax3.tick_params(axis="y", colors=COVER_FCF, labelsize=7)
                for spine in ax3.spines.values():
                    spine.set_color(BORDER)
                if top > 8.0:
                    ax3.text(0.995, 0.06, f"cover peaks at {top:,.0f}×",
                             transform=ax3.transAxes, ha="right",
                             color=COVER_FCF, fontsize=7)
        ax2.tick_params(labelsize=8)

        canvas = FigureCanvasTkAgg(fig, master=self.chart_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)
        canvas.mpl_connect("scroll_event", self._on_scroll)
        canvas.mpl_connect("button_press_event", self._on_click)
        canvas.mpl_connect("motion_notify_event", self._on_motion)
        canvas.mpl_connect("button_release_event", self._on_release)

        tb_frame = tk.Frame(self.chart_frame, bg=BG2)
        tb_frame.pack(fill="x")
        tk.Label(tb_frame,
                 text="drag to move · scroll to zoom · double-click to reset",
                 font=("Consolas", 8), bg=BG2,
                 fg=FG_DIM).pack(side="right", padx=10)
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

        self._canvas, self._figure, self._toolbar = canvas, fig, toolbar

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
                meter = a.value_meter()
                if meter["score"] is not None:
                    w.writerow([f"# value meter {meter['score']:.0f}/100 "
                                f"({meter['label']}), confidence "
                                f"{meter['confidence']:.0f}"])
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
