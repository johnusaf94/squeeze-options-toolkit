"""
squeeze_watcher.py
------------------
A live single-position monitor for a stock you're actively trading (built with
short squeeze entries in mind, but works long or short).

You give it a ticker, your entry price, share count, and optional target / stop.
It polls the market on an interval (default 120s), redraws an intraday chart with
your entry / target / stop drawn on it, shows live unrealized P&L, and "dings"
on every update (with a louder alert if price crosses your target or stop).

Runs standalone in PyCharm. Single file, no framework required beyond:
    pip install yfinance matplotlib pandas

NOTE ON DATA: this uses Yahoo Finance (via yfinance) for the free prototype.
Yahoo intraday quotes for US equities can be delayed ~15 minutes. That's fine
for gauging how a position is developing, but it is NOT tick-accurate for
timing an exact fill. Swapping in a broker/real-time feed is the #1 v2 upgrade
(see the roadmap I sent alongside this file).
"""

import sys
import json
import queue
import threading
import time
from pathlib import Path
from datetime import datetime

# ----- graceful dependency handling -------------------------------------------
_missing = []
try:
    import pandas as pd
except Exception:
    _missing.append("pandas")
try:
    import yfinance as yf
except Exception:
    _missing.append("yfinance")
try:
    import matplotlib
    matplotlib.use("TkAgg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    import matplotlib.dates as mdates
except Exception:
    _missing.append("matplotlib")

import tkinter as tk
from tkinter import ttk, messagebox

if _missing:
    print("Missing required packages: " + ", ".join(_missing))
    print("Install them with:\n    pip install " + " ".join(_missing))
    sys.exit(1)

CONFIG_PATH = Path(__file__).with_name("squeeze_watcher_config.json")
MIN_INTERVAL = 30  # seconds; be polite to the data source

# Optional gamma-terrain overlay (flip / call wall / put wall on the chart).
# Requires gamma_terrain.py + squeeze_deep.py in the same project.
try:
    from gamma_terrain import fetch_expiries_yf, compute_terrain
    _GAMMA_OK = True
except Exception:
    _GAMMA_OK = False


# ----- pure logic (unit-testable, no GUI/network) -----------------------------
def compute_pnl(direction, entry, last, shares):
    """Return (total_pnl, per_share_pnl, pct_return)."""
    per_share = (entry - last) if direction == "Short" else (last - entry)
    total = per_share * shares
    pct = (per_share / entry * 100.0) if entry else 0.0
    return total, per_share, pct


def compute_zone(direction, last, target, stop):
    """Return 'TARGET', 'STOP', or 'NEUTRAL' given current price and levels.

    For a Short: target is below entry (cover for profit), stop is above (danger).
    For a Long: target is above entry, stop is below.
    """
    if direction == "Short":
        if stop is not None and last >= stop:
            return "STOP"
        if target is not None and last <= target:
            return "TARGET"
    else:  # Long
        if stop is not None and last <= stop:
            return "STOP"
        if target is not None and last >= target:
            return "TARGET"
    return "NEUTRAL"


def levels_look_sane(direction, entry, target, stop):
    """Return a soft warning string if target/stop are on unexpected sides, else ''."""
    msgs = []
    if direction == "Short":
        if target is not None and target >= entry:
            msgs.append("target is not below entry (unusual for a short)")
        if stop is not None and stop <= entry:
            msgs.append("stop is not above entry (unusual for a short)")
    else:
        if target is not None and target <= entry:
            msgs.append("target is not above entry (unusual for a long)")
        if stop is not None and stop >= entry:
            msgs.append("stop is not below entry (unusual for a long)")
    return "; ".join(msgs)


# ----- audio ------------------------------------------------------------------
def ding(kind="update"):
    """Cross-platform beep. 'alert' is a louder two-tone; 'update' is a soft blip."""
    try:
        import winsound  # Windows
        if kind == "alert":
            winsound.Beep(988, 250)
            winsound.Beep(659, 350)
        else:
            winsound.MessageBeep(winsound.MB_OK)
        return
    except Exception:
        pass
    # Fallback: terminal bell
    try:
        print("\a", end="", flush=True)
    except Exception:
        pass


# ----- data fetch -------------------------------------------------------------
def fetch_quote(symbol, interval="2m", period="1d"):
    """Fetch intraday series. Returns dict with ok/last/series/asof/error."""
    try:
        tk_obj = yf.Ticker(symbol)
        df = tk_obj.history(period=period, interval=interval)
        if df is None or df.empty:
            # fall back to a wider window (handles thin pre/post-market or holidays)
            df = tk_obj.history(period="5d", interval="5m")
        if df is None or df.empty:
            return {"ok": False, "error": f"No data returned for '{symbol}'."}
        closes = df["Close"].dropna()
        if closes.empty:
            return {"ok": False, "error": f"No close prices for '{symbol}'."}
        last = float(closes.iloc[-1])
        asof = closes.index[-1]
        return {"ok": True, "last": last, "series": closes, "asof": asof, "error": None}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ----- background worker ------------------------------------------------------
class DataWorker(threading.Thread):
    def __init__(self, symbol, interval_s, out_queue):
        super().__init__(daemon=True)
        self.symbol = symbol
        self.interval_s = interval_s
        self.q = out_queue
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            result = fetch_quote(self.symbol)
            self.q.put(result)
            # sleep in small slices so Stop is responsive
            waited = 0.0
            while waited < self.interval_s and not self._stop.is_set():
                time.sleep(0.25)
                waited += 0.25


# ----- the app ----------------------------------------------------------------
class SqueezeWatcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Squeeze Watcher")
        self.geometry("1040x680")
        self.minsize(900, 600)

        self.q = queue.Queue()
        self.worker = None
        self.interval_s = 120
        self.next_update_ts = None
        self.last_zone = "NEUTRAL"
        self.params = None  # dict captured when Start pressed
        self.terrain = None  # gamma terrain dict (fetched once per Start)

        self._build_ui()
        self._load_config()
        self.after(200, self._poll_queue)
        self.after(1000, self._tick_countdown)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self):
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # --- control row ---
        ctrl = ttk.LabelFrame(root, text="Position", padding=8)
        ctrl.pack(fill="x")

        self.v_ticker = tk.StringVar()
        self.v_dir = tk.StringVar(value="Short")
        self.v_entry = tk.StringVar()
        self.v_shares = tk.StringVar()
        self.v_target = tk.StringVar()
        self.v_stop = tk.StringVar()
        self.v_interval = tk.StringVar(value="120")
        self.v_ding = tk.BooleanVar(value=True)
        self.v_gamma = tk.BooleanVar(value=_GAMMA_OK)

        def field(parent, label, var, width=9):
            f = ttk.Frame(parent)
            ttk.Label(f, text=label).pack(anchor="w")
            e = ttk.Entry(f, textvariable=var, width=width)
            e.pack()
            f.pack(side="left", padx=6)
            return e

        field(ctrl, "Ticker", self.v_ticker, width=8)

        dirf = ttk.Frame(ctrl)
        ttk.Label(dirf, text="Direction").pack(anchor="w")
        ttk.Combobox(dirf, textvariable=self.v_dir, values=["Short", "Long"],
                     width=6, state="readonly").pack()
        dirf.pack(side="left", padx=6)

        field(ctrl, "Entry $", self.v_entry)
        field(ctrl, "Shares", self.v_shares)
        field(ctrl, "Target $", self.v_target)
        field(ctrl, "Stop $", self.v_stop)
        field(ctrl, "Every (s)", self.v_interval, width=6)

        ttk.Checkbutton(ctrl, text="Ding", variable=self.v_ding).pack(side="left", padx=6)
        gam = ttk.Checkbutton(ctrl, text="Gamma lvls", variable=self.v_gamma)
        gam.pack(side="left", padx=2)
        if not _GAMMA_OK:
            gam.config(state="disabled")  # gamma_terrain.py/squeeze_deep.py not found

        self.btn_start = ttk.Button(ctrl, text="Start", command=self.start)
        self.btn_start.pack(side="left", padx=(12, 4))
        self.btn_stop = ttk.Button(ctrl, text="Stop", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left")

        # --- readouts row ---
        read = ttk.LabelFrame(root, text="Live", padding=8)
        read.pack(fill="x", pady=(8, 8))

        self.r_last = self._readout(read, "Last")
        self.r_change = self._readout(read, "Chg vs entry")
        self.r_pnl = self._readout(read, "Unreal. P&L")
        self.r_target = self._readout(read, "To target")
        self.r_stop = self._readout(read, "To stop")
        self.r_next = self._readout(read, "Next update")

        # --- chart ---
        self.fig = Figure(figsize=(9, 4.2), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvasTkAgg(self.fig, master=root)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self._draw_placeholder()

        # --- status bar ---
        self.v_status = tk.StringVar(value="Idle. Enter a position and press Start.")
        status = ttk.Label(root, textvariable=self.v_status, relief="sunken",
                           anchor="w", padding=4)
        status.pack(fill="x", pady=(8, 0))

    def _readout(self, parent, label):
        f = ttk.Frame(parent)
        ttk.Label(f, text=label, foreground="#666").pack(anchor="w")
        val = ttk.Label(f, text="--", font=("TkDefaultFont", 12, "bold"))
        val.pack(anchor="w")
        f.pack(side="left", padx=12)
        return val

    def _draw_placeholder(self):
        self.ax.clear()
        self.ax.text(0.5, 0.5, "Waiting for data...", ha="center", va="center",
                     transform=self.ax.transAxes, color="#999")
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        self.canvas.draw()

    # ---------- start / stop ----------
    def _parse_float(self, s):
        s = (s or "").strip()
        if s == "":
            return None
        return float(s)

    def start(self):
        try:
            ticker = self.v_ticker.get().strip().upper()
            if not ticker:
                raise ValueError("Ticker is required.")
            direction = self.v_dir.get()
            entry = self._parse_float(self.v_entry.get())
            shares = self._parse_float(self.v_shares.get())
            target = self._parse_float(self.v_target.get())
            stop = self._parse_float(self.v_stop.get())
            interval = int(float(self.v_interval.get()))
            if entry is None or entry <= 0:
                raise ValueError("Entry must be a positive number.")
            if shares is None or shares <= 0:
                raise ValueError("Shares must be a positive number.")
            if interval < MIN_INTERVAL:
                interval = MIN_INTERVAL
                self.v_interval.set(str(MIN_INTERVAL))
        except ValueError as e:
            messagebox.showerror("Invalid input", str(e))
            return

        warn = levels_look_sane(direction, entry, target, stop)

        self.params = dict(ticker=ticker, direction=direction, entry=entry,
                           shares=shares, target=target, stop=stop)
        self.interval_s = interval
        self.last_zone = "NEUTRAL"
        self._save_config()

        # (re)start worker
        self.stop(quiet=True)
        self.terrain = None
        if self.v_gamma.get() and _GAMMA_OK:
            threading.Thread(target=self._fetch_terrain, args=(ticker,),
                             daemon=True).start()
        self.worker = DataWorker(ticker, interval, self.q)
        self.worker.start()
        self.next_update_ts = time.time() + interval

        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        base = f"Watching {ticker} ({direction}) | entry ${entry:.2f} x {shares:g} sh | every {interval}s."
        if warn:
            base += f"  ⚠ {warn}."
        self.v_status.set(base + "  Fetching first quote...")

    def stop(self, quiet=False):
        if self.worker is not None:
            self.worker.stop()
            self.worker = None
        self.next_update_ts = None
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        if not quiet:
            self.v_status.set("Stopped.")

    # ---------- queue / updates ----------
    def _poll_queue(self):
        try:
            while True:
                result = self.q.get_nowait()
                self._handle_result(result)
        except queue.Empty:
            pass
        self.after(200, self._poll_queue)

    def _handle_result(self, result):
        if not result.get("ok"):
            self.v_status.set("Data error: " + str(result.get("error")))
            return
        p = self.params
        if not p:
            return

        last = result["last"]
        series = result["series"]
        asof = result["asof"]

        total, per_share, pct = compute_pnl(p["direction"], p["entry"], last, p["shares"])
        zone = compute_zone(p["direction"], last, p["target"], p["stop"])

        # readouts
        self.r_last.config(text=f"${last:,.2f}")
        chg_color = "#1a7f37" if (total >= 0) else "#b3261e"
        self.r_change.config(text=f"{per_share:+.2f}  ({pct:+.2f}%)", foreground=chg_color)
        self.r_pnl.config(text=f"${total:+,.2f}", foreground=chg_color)
        self.r_target.config(
            text=(f"${last - p['target']:+,.2f}" if p["target"] is not None else "--"))
        self.r_stop.config(
            text=(f"${last - p['stop']:+,.2f}" if p["stop"] is not None else "--"))

        # chart
        self._redraw_chart(series, p, last)

        # timing
        self.next_update_ts = time.time() + self.interval_s
        asof_str = ""
        try:
            asof_str = pd.Timestamp(asof).strftime("%H:%M %Z")
        except Exception:
            asof_str = str(asof)
        zone_note = {"TARGET": "  🎯 AT/THROUGH TARGET",
                     "STOP": "  🛑 AT/THROUGH STOP",
                     "NEUTRAL": ""}[zone]
        self.v_status.set(
            f"{p['ticker']} last ${last:,.2f} (data asof {asof_str}). "
            f"P&L ${total:+,.2f}.{zone_note}")

        # audio
        if self.v_ding.get():
            if zone in ("TARGET", "STOP") and zone != self.last_zone:
                ding("alert")
            else:
                ding("update")
        self.last_zone = zone

    def _fetch_terrain(self, ticker):
        """One-shot gamma terrain fetch (background thread). OI is
        prior-session data refreshed each morning — once per Start is the
        honest cadence; restarting the watch refreshes it."""
        try:
            spot, expiries = fetch_expiries_yf(ticker)
            self.terrain = compute_terrain(spot, expiries)
            reg = (self.terrain.get("regime") or "").split(" — ")[0]
            flip = self.terrain.get("flip_price")
            extra = f", flip ${flip:.2f}" if flip else ""
            self.q.put({"ok": False,
                        "error": f"[gamma] terrain loaded: {reg}{extra} "
                                 f"(prior-session OI)"})
        except Exception as e:
            self.terrain = None
            self.q.put({"ok": False, "error": f"[gamma] unavailable: {e}"})

    def _redraw_chart(self, series, p, last):
        ax = self.ax
        ax.clear()

        times = series.index
        prices = series.values
        ax.plot(times, prices, linewidth=1.6, color="#1f77b4", label="Price")

        entry = p["entry"]
        target = p["target"]
        stop = p["stop"]

        # y-range including levels, with padding
        candidates = list(prices) + [entry]
        if target is not None:
            candidates.append(target)
        if stop is not None:
            candidates.append(stop)
        # gamma levels join the y-range only when near the action (±12%),
        # so a far-away wall can't flatten the price series
        gamma_levels = []
        if self.terrain:
            for key, lab, col in (("flip_price", "Γ flip", "#e07b00"),
                                  ("call_wall", "Call wall", "#1a7f37"),
                                  ("put_wall", "Put wall", "#b3261e")):
                v = self.terrain.get(key)
                if v:
                    gamma_levels.append((v, lab, col))
                    if abs(v - last) / last <= 0.12:
                        candidates.append(v)
        lo, hi = min(candidates), max(candidates)
        pad = (hi - lo) * 0.08 or (hi * 0.01 or 1.0)
        ymin, ymax = lo - pad, hi + pad
        ax.set_ylim(ymin, ymax)

        # profit/loss shading relative to entry
        if p["direction"] == "Short":
            ax.axhspan(entry, ymax, color="#b3261e", alpha=0.06)  # up = loss
            ax.axhspan(ymin, entry, color="#1a7f37", alpha=0.06)  # down = profit
        else:
            ax.axhspan(entry, ymax, color="#1a7f37", alpha=0.06)
            ax.axhspan(ymin, entry, color="#b3261e", alpha=0.06)

        # reference lines
        ax.axhline(entry, color="#111", linewidth=1.2, linestyle="-",
                   label=f"Entry {entry:.2f}")
        if target is not None:
            ax.axhline(target, color="#1a7f37", linewidth=1.1, linestyle="--",
                       label=f"Target {target:.2f}")
        if stop is not None:
            ax.axhline(stop, color="#b3261e", linewidth=1.1, linestyle="--",
                       label=f"Stop {stop:.2f}")

        # gamma terrain lines (dotted, only if inside the visible range)
        for v, lab, col in gamma_levels:
            if ymin <= v <= ymax:
                ax.axhline(v, color=col, linewidth=1.0, linestyle=":",
                           alpha=0.9, label=f"{lab} {v:.2f}")

        # last price marker
        ax.scatter([times[-1]], [last], color="#1f77b4", zorder=5, s=28)

        ax.set_title(f"{p['ticker']} — {p['direction']}  (last ${last:,.2f})")
        ax.grid(True, alpha=0.25)
        try:
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
            self.fig.autofmt_xdate(rotation=0)
        except Exception:
            pass
        ax.legend(loc="upper left", fontsize=8, framealpha=0.6)
        self.fig.tight_layout()
        self.canvas.draw()

    def _tick_countdown(self):
        if self.next_update_ts is not None:
            remaining = int(max(0, self.next_update_ts - time.time()))
            self.r_next.config(text=f"{remaining}s")
        else:
            self.r_next.config(text="--")
        self.after(1000, self._tick_countdown)

    # ---------- config persistence ----------
    def _save_config(self):
        try:
            data = dict(
                ticker=self.v_ticker.get(), direction=self.v_dir.get(),
                entry=self.v_entry.get(), shares=self.v_shares.get(),
                target=self.v_target.get(), stop=self.v_stop.get(),
                interval=self.v_interval.get(), ding=self.v_ding.get(),
            )
            CONFIG_PATH.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _load_config(self):
        try:
            if CONFIG_PATH.exists():
                data = json.loads(CONFIG_PATH.read_text())
                self.v_ticker.set(data.get("ticker", ""))
                self.v_dir.set(data.get("direction", "Short"))
                self.v_entry.set(data.get("entry", ""))
                self.v_shares.set(data.get("shares", ""))
                self.v_target.set(data.get("target", ""))
                self.v_stop.set(data.get("stop", ""))
                self.v_interval.set(data.get("interval", "120"))
                self.v_ding.set(bool(data.get("ding", True)))
        except Exception:
            pass

    def _on_close(self):
        self.stop(quiet=True)
        self._save_config()
        self.destroy()


def main():
    app = SqueezeWatcher()
    app.mainloop()


if __name__ == "__main__":
    main()
