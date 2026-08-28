"""
stock_analysis_gui.py
======================
Standalone Stock Analysis tool.
Runs: Buffett, Weiss, Bogle, Dalio, Druckenmiller, Minervini

Requires in same folder:
  shared_utils.py, ticker_resolver.py, composite_score.py,
  buffett_analyzer.py, weiss_analyzer.py, bogle_analyzer.py,
  dalio_analyzer.py, druckenmiller_analyzer.py, minervini_analyzer.py (optional)
"""

# ── GLOBAL yfinance RATE LIMITER ────────────────────────────────
# Must be imported BEFORE anything that uses yfinance. Monkey-
# patches yfinance.Ticker with token-bucket rate limiting + caching.
import yfinance_throttle  # noqa: F401  # installs global throttle


import tkinter as tk
from tkinter import scrolledtext, filedialog
import threading
import traceback
from shared_utils import *

PORTFOLIO_FILE = "portfolio.xlsx"

# ─────────────────────────────────────────────
# ANALYSIS PIPELINE
# ─────────────────────────────────────────────

def run_full_analysis(ticker, portfolio_path=PORTFOLIO_FILE):
    import yfinance as yf
    from ticker_resolver import resolve_ticker, fetch_live_data
    from composite_score import build_composite
    from buffett_analyzer import run_buffett_analysis, fetch_buffett_indicator
    from weiss_analyzer import run_weiss_analysis
    from bogle_analyzer import run_bogle_analysis
    from dalio_analyzer import run_dalio_analysis
    from druckenmiller_analyzer import run_druckenmiller_analysis

    resolved, company_name, ok = resolve_ticker(ticker)
    if not ok:
        return None, f"Could not resolve '{ticker}'"

    results = {"ticker": resolved, "company_name": company_name}
    live_data = fetch_live_data(resolved)
    results["live_data"] = live_data

    try:
        asset_class = detect_asset_class(yf.Ticker(resolved).info)
    except Exception:
        asset_class = "UNKNOWN"

    routing = ASSET_ANALYZERS.get(asset_class, ASSET_ANALYZERS["UNKNOWN"])
    results["asset_class"] = asset_class
    results["asset_note"]  = routing["note"]
    skip = set(routing["skip"])

    all_analyzers = [
        ("buffett",       run_buffett_analysis,       (resolved,)),
        ("weiss",         run_weiss_analysis,         (resolved,)),
        ("bogle",         run_bogle_analysis,         (resolved, portfolio_path)),
        ("dalio",         run_dalio_analysis,         (resolved, portfolio_path)),
        ("druckenmiller", run_druckenmiller_analysis, (resolved,)),
    ]
    try:
        from minervini_analyzer import run_minervini_analysis
        all_analyzers.append(("minervini", run_minervini_analysis, (resolved,)))
    except ImportError:
        pass

    for name, fn, args in all_analyzers:
        base = name.split("_")[0]
        if base in skip or name in skip:
            results[name] = None
            results[f"{name}_skipped"] = True
            continue
        try:
            results[name] = fn(*args)
        except Exception as e:
            results[name] = None
            results[f"{name}_error"] = str(e)
            import traceback
            results[f"{name}_traceback"] = traceback.format_exc()

    try:
        bi = fetch_buffett_indicator()
        market_ctx = f"Buffett Indicator {bi.ratio*100:.0f}% — {bi.signal}" if bi.ratio else ""
    except Exception:
        market_ctx = ""

    active_skipped = set(routing.get("skip", []))
    if live_data and (not live_data.dividend_rate or live_data.dividend_rate == 0):
        active_skipped.add("weiss_yield")

    composite = build_composite(
        ticker=resolved,
        company_name=company_name,
        buffett_analysis=results.get("buffett"),
        weiss_analysis=results.get("weiss"),
        bogle_analysis=results.get("bogle"),
        dalio_analysis=results.get("dalio"),
        druckenmiller_analysis=results.get("druckenmiller"),
        live_data=live_data,
        market_context_str=market_ctx,
        skipped=active_skipped,
    )
    results["composite"] = composite
    return results, None


# ─────────────────────────────────────────────
# GUI
# ─────────────────────────────────────────────

class StockAnalysisApp:
    def __init__(self, root):
        self.root = root
        self.root.title("📊 Stock Analysis")
        self.root.geometry("1280x820")
        self.root.configure(bg=BG)

        self.is_running      = False
        self.stop_requested  = False
        self._last_results   = None      # populated after each analysis
        self._last_ticker    = None      # populated after each analysis
        self.session_results = None
        self.portfolio_ctx   = load_portfolio_context(PORTFOLIO_FILE)
        self.mode_var        = tk.StringVar(value="composite")

        self._build_top_bar()
        self._build_main()
        self._build_bottom()

        threading.Thread(target=self._check_backend, daemon=True).start()
        threading.Thread(target=self._init_prices,   daemon=True).start()

    # ── TOP BAR ──────────────────────────────
    def _build_top_bar(self):
        top = tk.Frame(self.root, bg=BG2, pady=8)
        top.pack(fill="x")
        tk.Label(top, text="📊 STOCK ANALYSIS", font=FONT_HD, bg=BG2, fg=ACCENT).pack(side="left", padx=16)

        self.port_label = tk.Label(top, text="📋 Loading...", font=FONT_SM, bg=BG2, fg=FG_DIM)
        self.port_label.pack(side="right", padx=8)
        tk.Button(top, text="Reload Portfolio", font=FONT_SM, bg=BG3, fg=FG,
                  relief="flat", cursor="hand2", command=self._reload_portfolio).pack(side="right", padx=4)

        tk.Frame(top, bg=BORDER, width=1).pack(side="right", fill="y", padx=6, pady=4)
        self.conn_lbl = tk.Label(top, text="⏳ checking...", font=FONT_SM, bg=BG2, fg=FG_DIM)
        self.conn_lbl.pack(side="right", padx=4)

        self._model_options = {"🖥  Local (LM Studio)": ("local", "local-model")}
        for n, mid in GROQ_MODELS.items():
            self._model_options[f"⚡ Groq — {n}"] = ("groq", mid)
        for n, mid in TOGETHER_MODELS.items():
            self._model_options[f"☁  Together — {n}"] = ("together", mid)
        self._backend_var = tk.StringVar(value="🖥  Local (LM Studio)")
        menu = tk.OptionMenu(top, self._backend_var, *self._model_options.keys(),
                             command=self._on_backend_change)
        menu.config(font=FONT_SM, bg=BG3, fg=FG, relief="flat", highlightthickness=0, bd=0)
        menu["menu"].config(bg=BG3, fg=FG, font=FONT_SM)
        menu.pack(side="right", padx=2)
        tk.Label(top, text="Model:", font=FONT_SM, bg=BG2, fg=FG_DIM).pack(side="right", padx=(8,2))

    # ── MAIN AREA ─────────────────────────────
    def _build_main(self):
        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True)

        left = tk.Frame(main, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        self.chat = scrolledtext.ScrolledText(
            left, wrap="word", font=FONT, bg=BG, fg=FG,
            insertbackground=FG, selectbackground=BORDER,
            relief="flat", borderwidth=0, state="disabled", padx=20, pady=16)
        self.chat.pack(fill="both", expand=True)

        for tag, cfg in [
            ("header",       {"font": FONT_LG, "foreground": ACCENT}),
            ("dim",          {"foreground": FG_DIM}),
            ("green",        {"foreground": GREEN}),
            ("red",          {"foreground": RED}),
            ("yellow",       {"foreground": YELLOW}),
            ("blue",         {"foreground": BLUE}),
            ("teal",         {"foreground": TEAL}),
            ("score_strong", {"font": ("Consolas",12,"bold"), "foreground": GREEN}),
            ("score_buy",    {"font": ("Consolas",11,"bold"), "foreground": GREEN}),
            ("score_watch",  {"font": ("Consolas",11,"bold"), "foreground": YELLOW}),
            ("score_avoid",  {"font": ("Consolas",11,"bold"), "foreground": RED}),
            ("claude",       {"font": ("Consolas",10),        "foreground": TEAL}),
        ]:
            self.chat.tag_config(tag, **cfg)

        sb = tk.Frame(main, bg=BG2, width=250)
        sb.pack(side="right", fill="y")
        sb.pack_propagate(False)
        tk.Label(sb, text="LAST SCORE", font=FONT_SM, bg=BG2, fg=FG_DIM).pack(pady=(14,2), padx=10, anchor="w")
        box = tk.Frame(sb, bg=BG3)
        box.pack(fill="x", padx=6, pady=2)
        self._lbl_ticker = tk.Label(box, text="—", font=FONT_LG, bg=BG3, fg=ACCENT)
        self._lbl_ticker.pack(pady=(8,0))
        self._lbl_num = tk.Label(box, text="—", font=("Consolas",34,"bold"), bg=BG3, fg=FG_DIM)
        self._lbl_num.pack()
        self._lbl_sig = tk.Label(box, text="—", font=("Consolas",11,"bold"), bg=BG3, fg=FG_DIM)
        self._lbl_sig.pack()
        self._lbl_fit = tk.Label(box, text="", font=FONT_SM, bg=BG3, fg=BLUE, wraplength=210)
        self._lbl_fit.pack(pady=(0,8), padx=6)
        tk.Frame(sb, bg=BORDER, height=1).pack(fill="x", padx=6, pady=6)
        tk.Label(sb, text="COMPONENTS", font=FONT_SM, bg=BG2, fg=FG_DIM).pack(anchor="w", padx=10, pady=(0,4))
        self._comp_frame = tk.Frame(sb, bg=BG2)
        self._comp_frame.pack(fill="x", padx=6)
        tk.Frame(sb, bg=BORDER, height=1).pack(fill="x", padx=6, pady=6)
        tk.Button(sb, text="Clear", font=FONT_SM, bg=BG3, fg=FG_DIM,
                  relief="flat", cursor="hand2", command=self._clear).pack(fill="x", padx=6, pady=2)

    # ── BOTTOM BAR ────────────────────────────
    def _build_bottom(self):
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")
        bot = tk.Frame(self.root, bg=BG2, pady=8)
        bot.pack(fill="x")

        r1 = tk.Frame(bot, bg=BG2)
        r1.pack(fill="x", padx=12, pady=(0,4))
        tk.Label(r1, text="Ticker:", font=FONT, bg=BG2, fg=FG_DIM).pack(side="left")
        self.ticker_var = tk.StringVar()
        self.ticker_entry = tk.Entry(r1, textvariable=self.ticker_var, font=FONT,
                                      bg=BG3, fg=FG, insertbackground=FG, relief="flat", bd=6, width=12)
        self.ticker_entry.pack(side="left", padx=6)
        self.ticker_entry.bind("<Return>", lambda e: self._toggle_run())
        self.run_btn = tk.Button(r1, text="▶  Analyze", font=("Consolas",11,"bold"),
                                  bg=ACCENT, fg="#000000", relief="flat",
                                  cursor="hand2", padx=14, pady=4, command=self._toggle_run)
        self.run_btn.pack(side="left", padx=4)

        # Export CSV — disabled until an analysis completes
        self.export_btn = tk.Button(r1, text="📄 Export CSV",
                                     font=("Consolas",10,"bold"),
                                     bg=BG3, fg=FG_DIM, relief="flat",
                                     cursor="hand2", padx=10, pady=4,
                                     state="disabled",
                                     command=self._export_csv)
        self.export_btn.pack(side="left", padx=4)

        # Score / Deep Dive toggle
        mf = tk.Frame(r1, bg=BG2)
        mf.pack(side="left", padx=12)
        def _toggle_mode():
            self.mode_var.set("long" if self.mode_var.get() == "composite" else "composite")
            lc.config(fg=ACCENT if self.mode_var.get()=="composite" else FG_DIM)
            ll.config(fg=BLUE   if self.mode_var.get()=="long"      else FG_DIM)
            self.run_btn.config(text="▶  Score" if self.mode_var.get()=="composite" else "▶  Deep Dive")
        lc = tk.Label(mf, text="Score",     font=("Consolas",9,"bold"), bg=BG2, fg=ACCENT, cursor="hand2")
        lc.pack(side="left", padx=(0,4))
        lc.bind("<Button-1>", lambda e: [self.mode_var.set("composite"), _toggle_mode()] if self.mode_var.get()!="composite" else None)
        tb = tk.Button(mf, text="  ", width=3, relief="flat", bg="#238636", cursor="hand2", command=_toggle_mode)
        tb.pack(side="left")
        ll = tk.Label(mf, text="Deep Dive", font=FONT_SM, bg=BG2, fg=FG_DIM, cursor="hand2")
        ll.pack(side="left", padx=(4,0))
        ll.bind("<Button-1>", lambda e: [self.mode_var.set("long"), _toggle_mode()] if self.mode_var.get()!="long" else None)

        self.status_lbl = tk.Label(r1, text="Ready", font=FONT_SM, bg=BG2, fg=FG_DIM)
        self.status_lbl.pack(side="left", padx=10)

        tk.Frame(bot, bg=BORDER, height=1).pack(fill="x")
        r2 = tk.Frame(bot, bg=BG, pady=6)
        r2.pack(fill="x", padx=12)
        tk.Label(r2, text="Ask Claude:", font=FONT, bg=BG, fg=FG_DIM).pack(side="left")
        self.qa_var = tk.StringVar()
        self.qa_entry = tk.Entry(r2, textvariable=self.qa_var, font=FONT,
                                  bg=BG3, fg=FG_DIM, insertbackground=FG, relief="flat", bd=6, state="disabled")
        self.qa_entry.pack(side="left", fill="x", expand=True, padx=6)
        self.qa_entry.bind("<Return>", lambda e: self._ask_claude())
        self.qa_btn = tk.Button(r2, text="💬 Ask", font=("Consolas",10,"bold"),
                                 bg=BG3, fg=FG_DIM, relief="flat", cursor="hand2",
                                 padx=10, pady=4, state="disabled", command=self._ask_claude)
        self.qa_btn.pack(side="left", padx=4)

    # ── HELPERS ───────────────────────────────
    def _w(self, text, tag=None):
        self.chat.config(state="normal")
        if tag: self.chat.insert("end", text, tag)
        else:   self.chat.insert("end", text)
        self.chat.see("end")
        self.chat.config(state="disabled")
        self.root.update_idletasks()

    def _rule(self, label=""):
        if label:
            pad = max(0, (62 - len(label) - 2) // 2)
            self._w(f"\n{'─'*pad} {label} {'─'*pad}\n\n", "dim")
        else:
            self._w(f"\n{'─'*62}\n\n", "dim")

    def _clear(self):
        self.chat.config(state="normal")
        self.chat.delete("1.0", "end")
        self.chat.config(state="disabled")
        self.session_results = None
        self._lbl_ticker.config(text="—", fg=ACCENT)
        self._lbl_num.config(text="—", fg=FG_DIM)
        self._lbl_sig.config(text="—", fg=FG_DIM)
        self._lbl_fit.config(text="")
        for w in self._comp_frame.winfo_children(): w.destroy()

    def _on_backend_change(self, selection):
        global _ACTIVE_BACKEND, _ACTIVE_ONLINE_MODEL
        import shared_utils as su
        backend, model = self._model_options.get(selection, ("local", "local-model"))
        su._ACTIVE_BACKEND = backend
        su._ACTIVE_ONLINE_MODEL = model
        globals()["_ACTIVE_BACKEND"] = backend
        globals()["_ACTIVE_ONLINE_MODEL"] = model
        threading.Thread(target=self._check_backend, daemon=True).start()

    def _check_backend(self):
        ok, msg = check_backend_status()
        color = GREEN if ok else YELLOW
        self.conn_lbl.config(text=msg, fg=color)

    def _init_prices(self):
        try:
            from ticker_resolver import fetch_live_data
            self.port_label.config(text="📋 Portfolio loaded", fg=GREEN)
        except Exception:
            self.port_label.config(text="📋 No portfolio", fg=FG_DIM)

    def _reload_portfolio(self):
        path = filedialog.askopenfilename(title="Select portfolio.xlsx",
                                           filetypes=[("Excel files", "*.xlsx")])
        if not path: return
        self.port_label.config(text="📡 Loading...", fg=FG_DIM)
        def _load():
            self.portfolio_ctx = load_portfolio_context(path)
            self.port_label.config(text="📋 Portfolio loaded", fg=GREEN)
        threading.Thread(target=_load, daemon=True).start()

    def _update_sidebar(self, composite):
        sig_color = {"STRONG BUY": GREEN, "BUY": GREEN, "WATCHLIST": YELLOW, "AVOID": RED}.get(composite.signal, FG)
        self._lbl_ticker.config(text=composite.ticker)
        self._lbl_num.config(text=f"{composite.total_score:.0f}", fg=sig_color)
        self._lbl_sig.config(text=composite.signal, fg=sig_color)
        self._lbl_fit.config(text=composite.account_fit)
        for w in self._comp_frame.winfo_children(): w.destroy()
        for c in composite.components:
            row = tk.Frame(self._comp_frame, bg=BG2)
            row.pack(fill="x", pady=1)
            short = c.name.split("—")[1].strip() if "—" in c.name else c.name[:16]
            tk.Label(row, text=short[:18], font=FONT_SM, bg=BG2, fg=FG_DIM, width=16, anchor="w").pack(side="left")
            canvas = tk.Canvas(row, width=56, height=8, bg=BG3, highlightthickness=0)
            canvas.pack(side="left", padx=2)
            fill_w = max(2, int(c.raw * 54))
            bar_color = GREEN if c.raw >= 0.65 else (YELLOW if c.raw >= 0.40 else RED)
            canvas.create_rectangle(0, 0, fill_w, 8, fill=bar_color, outline="")
            tk.Label(row, text=f"{c.raw:.0%}", font=FONT_SM, bg=BG2, fg=FG_DIM, width=4).pack(side="left")

    # ── ANALYSIS ─────────────────────────────
    def _toggle_run(self):
        if self.is_running:
            self.stop_requested = True
            self.run_btn.config(text="⏹ Stopping...", bg=RED, state="disabled")
        else:
            ticker = self.ticker_var.get().strip()
            if not ticker: self.ticker_entry.focus(); return
            self._start_analysis(ticker)

    def _start_analysis(self, ticker):
        self.is_running = True
        self.stop_requested = False
        self.session_results = None
        self._last_results = None      # invalidate prior exportable result
        self._last_ticker = None
        self.run_btn.config(bg=RED, fg="#000000", text="⏹ Stop")
        self.export_btn.config(state="disabled", bg=BG3, fg=FG_DIM)
        self.ticker_entry.config(state="disabled")
        self.qa_entry.config(state="disabled", fg=FG_DIM)
        self.qa_btn.config(state="disabled", bg=BG3, fg=FG_DIM)
        mode = self.mode_var.get()
        threading.Thread(target=self._analysis_thread, args=(ticker, mode), daemon=True).start()

    def _analysis_thread(self, ticker, mode="composite"):
        try:
            self._w(f"\nANALYZING  {ticker.upper()}\n", "header")
            self._rule()
            self.status_lbl.config(text=f"Analyzing {ticker.upper()}...")

            results, err = run_full_analysis(ticker, PORTFOLIO_FILE)
            if err:
                self._w(f"❌ {err}\n", "red"); return
            if self.stop_requested:
                self._w("  ⏹ Stopped.\n", "dim"); return

            self.session_results = results
            composite = results["composite"]
            live = results.get("live_data")

            asset_class = results.get("asset_class", "UNKNOWN")
            asset_note  = results.get("asset_note", "")

            if live:
                price  = f"${live.current_price:.2f}" if live.current_price else "N/A"
                sector = live.sector or "N/A"
                mktcap = f"${live.market_cap/1e9:.1f}B" if live.market_cap else "N/A"
                self._w(f"  {composite.ticker} — {composite.company_name}\n", "blue")
                self._w(f"  Price: {price}  |  Sector: {sector}  |  MCap: {mktcap}\n", "dim")
            if asset_note:
                self._w(f"  ℹ️  {asset_note}\n", "yellow")
            skipped = [k.replace("_skipped","") for k in results if k.endswith("_skipped") and results[k]]
            if skipped:
                self._w(f"  ⏭  Skipped: {', '.join(skipped)}\n", "dim")
            # Show any analyzer errors so we can diagnose
            errors = {k.replace("_error",""): results[k] for k in results if k.endswith("_error")}
            for ename, emsg in errors.items():
                self._w(f"  ⚠️  {ename} error: {emsg}\n", "red")
                tb_key = f"{ename}_traceback"
                if results.get(tb_key):
                    self._w(f"  {results[tb_key]}\n", "dim")
            self._w(f"  {composite.market_context}\n\n", "dim")

            self._write_score_table(composite)
            self._write_framework_details(composite, live, results)
            self._update_sidebar(composite)

            if mode == "long" and not self.stop_requested:
                self._write_long_narratives(results)

            self.qa_entry.config(state="normal", fg=FG)
            self.qa_btn.config(state="normal", bg="#238636", fg="#FFFFFF")
            self._w(f"\n  💬 Ask Claude about this analysis below\n", "dim")
            self._rule()
            self.status_lbl.config(text=f"Done — {composite.total_score:.0f}/100  {composite.signal}")

            # ── ENABLE CSV EXPORT ──
            # Store the full analysis for the Export button to use
            self._last_results = results
            self._last_ticker = results.get("ticker", ticker)
            self.export_btn.config(state="normal",
                                    bg="#1f6feb", fg="#FFFFFF")

        except Exception as e:
            self._w(f"\n❌ Error: {e}\n", "red")
            self._w(traceback.format_exc(), "dim")
            self.status_lbl.config(text="Error")
        finally:
            self.is_running = False
            self.stop_requested = False
            self.run_btn.config(state="normal", text="▶  Analyze", bg=ACCENT, fg="#000000")
            self.ticker_entry.config(state="normal")
            self.ticker_var.set("")
            self.ticker_entry.focus()

    def _export_csv(self):
        """
        Export the most-recent analysis to CSV — same purpose as the
        squeeze_searcher's export, but for Stock Analysis output: one
        row per analyzed ticker with every framework's score and the
        key metrics that fed it. Designed to be brought to Claude for
        analyst-mode review.
        """
        if not self._last_results:
            self._w("\n  ⚠️  No analysis to export — run one first.\n",
                    "watch")
            return

        try:
            from tkinter import filedialog
            import csv as _csv
            from datetime import datetime as _dt

            results = self._last_results
            ticker = self._last_ticker or "analysis"
            composite = results.get("composite")
            if composite is None:
                self._w("\n  ❌ No composite result — cannot export.\n", "red")
                return

            default_name = (f"stock_analysis_{ticker}_"
                            f"{_dt.now().strftime('%Y%m%d_%H%M')}.csv")
            path = filedialog.asksaveasfilename(
                title="Export analysis for Claude review",
                defaultextension=".csv",
                initialfile=default_name,
                filetypes=[("CSV files", "*.csv")],
            )
            if not path:
                return

            # ── BUILD THE ROW ──
            row = {
                "ticker":      results.get("ticker", ticker),
                "company":     results.get("company_name", ""),
                "scan_time":   _dt.now().isoformat(timespec="seconds"),
                "composite":   round(composite.total_score, 2),
                "signal":      composite.signal,
                "account_fit": getattr(composite, "account_fit", ""),
                "account_reason": getattr(composite, "account_reason", ""),
                "narrative":   getattr(composite, "narrative", ""),
                "skipped":     ";".join(getattr(composite, "skipped", [])),
            }

            # Per-component raw scores (0-100)
            for c in getattr(composite, "components", []):
                key = getattr(c, "key", None) or getattr(c, "name", "")
                if not key:
                    continue
                raw = getattr(c, "raw_score", None)
                if raw is not None:
                    # Normalize key to safe CSV column name
                    safe_key = key.lower().replace(" ", "_").replace("/", "_")
                    row[f"score_{safe_key}"] = round(raw * 100, 1)

            # Framework-specific extras worth bringing to analysis
            b = results.get("buffett")
            if b is not None:
                ms = getattr(b, "moat_score", None)
                if ms is not None:
                    row["buffett_moat_rating"] = getattr(ms, "rating", "")
                    row["buffett_moat_score"] = getattr(ms, "score", 0)
                m = getattr(b, "moat", None)
                if m is not None:
                    row["moat_direction"] = getattr(m, "moat_direction", "")
                    row["moat_trend_note"] = getattr(m, "moat_trend_note", "")
                    row["roic"] = (round(m.roic * 100, 1)
                                    if m.roic is not None else "")
                    row["gross_margin"] = (round(m.gross_margin * 100, 1)
                                            if m.gross_margin is not None else "")
                v = getattr(b, "valuation", None)
                if v is not None:
                    row["price"] = getattr(v, "current_price", "")
                    row["pe_ratio"] = getattr(v, "pe_ratio", "")
                    row["forward_pe"] = getattr(v, "forward_pe", "")
                    row["fey"] = getattr(v, "forward_earnings_yield", "")
                    row["fey_spread"] = getattr(v, "fey_spread", "")

            w = results.get("weiss")
            if w is not None:
                bc = getattr(w, "blue_chip", None)
                if bc is not None:
                    row["weiss_rating"] = getattr(bc, "rating", "")
                    row["weiss_passed"] = getattr(bc, "score", 0)
                    row["weiss_measurable"] = getattr(bc, "measurable", 7)

            bg = results.get("bogle")
            if bg is not None:
                rv = getattr(bg, "reversion", None)
                if rv is not None:
                    row["bogle_score"] = getattr(rv, "score", 0)
                    row["bogle_signal"] = getattr(rv, "signal", "")

            dl = results.get("dalio")
            if dl is not None:
                db = getattr(dl, "debt_analysis", None)
                if db is not None:
                    row["dalio_debt_score"] = getattr(db, "score", 0)
                bb = getattr(dl, "bubble_analysis", None)
                if bb is not None:
                    row["dalio_bubble_score"] = getattr(bb, "score", 0)
                    row["dalio_bubble_verdict"] = getattr(bb, "verdict", "")

            dr = results.get("druckenmiller")
            if dr is not None:
                row["druck_score"] = getattr(dr, "overall_score", 0)
                row["druck_signal"] = getattr(dr, "signal", "")
                row["druck_conviction"] = getattr(dr, "conviction", "")

            # Write CSV (one-row file with full column set)
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = _csv.DictWriter(f, fieldnames=list(row.keys()))
                writer.writeheader()
                writer.writerow(row)

            self._w(f"\n  📄 Exported analysis to:\n", "green")
            self._w(f"     {path}\n", "dim")
            self._w(f"     Bring this CSV to Claude for analyst review.\n",
                    "dim")
            self.status_lbl.config(text=f"Exported {ticker} to CSV")
        except Exception as e:
            import traceback
            self._w(f"\n  ❌ Export failed: {e}\n", "red")
            self._w(traceback.format_exc(), "dim")

    def _write_score_table(self, composite):
        sig_tag = {"STRONG BUY": "score_strong", "BUY": "score_buy",
                   "WATCHLIST": "score_watch", "AVOID": "score_avoid"}.get(composite.signal, "dim")
        self._w("  COMPOSITE SCORE\n", "header")
        self._w(f"  {'─'*58}\n", "dim")
        self._w(f"  {'Framework':<30} {'Score':>6}  {'Wt':>3}  Bar\n", "dim")
        self._w(f"  {'─'*58}\n", "dim")
        for c in composite.components:
            bar = "█" * c.bar_filled + "░" * (20 - c.bar_filled)
            bar_tag = "green" if c.raw >= 0.65 else ("yellow" if c.raw >= 0.40 else "red")
            self._w(f"  {c.name:<30} {c.raw:>6.0%}  {c.weight:>3}%  ", "dim")
            self._w(f"{bar}\n", bar_tag)
        self._w(f"  {'─'*58}\n", "dim")
        total_bar = "█" * round(composite.total_score/5) + "░" * (20 - round(composite.total_score/5))
        self._w(f"  {'TOTAL SCORE':<30} {composite.total_score:>6.1f}  100%  ", "dim")
        self._w(f"{total_bar}\n\n", sig_tag)
        self._w(f"  SIGNAL:  ", "dim"); self._w(f"{composite.signal}\n", sig_tag)
        self._w(f"  ACCOUNT: {composite.account_fit}\n", "blue")
        self._w(f"  REASON:  {composite.account_reason}\n", "dim")
        self._w(f"  DATA:    {composite.data_quality} quality", "dim")
        if composite.missing_data:
            self._w(f"  |  Missing: {', '.join(composite.missing_data)}", "dim")
        self._w("\n\n")

    def _write_framework_details(self, composite, live, results):
        for key, label, module, fn_name in [
            ("buffett",       "🎩 Buffett — Moat & Valuation",           "buffett_analyzer",       "format_display_summary"),
            ("weiss",         "📈 Weiss — Yield Signal & Blue Chip",     "weiss_analyzer",         "format_weiss_display"),
            ("bogle",         "📊 Bogle — Timing & Diversification",     "bogle_analyzer",         "format_bogle_display"),
            ("dalio",         "🌊 Dalio — Four Filters",                 "dalio_analyzer",         "format_dalio_display"),
            ("druckenmiller", "📡 Druckenmiller — Five Pillars",         "druckenmiller_analyzer", "format_druckenmiller_display"),
            ("minervini",     "📈 Minervini — SEPA Trend Template",      "minervini_analyzer",     "format_minervini_display"),
        ]:
            obj = results.get(key)
            if obj and not results.get(f"{key}_skipped"):
                self._rule(label)
                try:
                    import importlib
                    mod = importlib.import_module(module)
                    self._w(getattr(mod, fn_name)(obj) + "\n", "dim")
                except Exception as e:
                    self._w(f"  Display error: {e}\n", "red")

        if live:
            self._rule("📋 Key Metrics")
            buffett_obj = results.get("buffett")
            roic = f"{buffett_obj.moat.roic:.1%}" if buffett_obj and buffett_obj.moat.roic else "N/A"
            def fmt(val, style="num"):
                if val is None: return "N/A"
                if style == "pct":  return f"{val:.1%}"
                if style == "pct2": return f"{val:.2%}"
                if style == "x":    return f"{val:.1f}x"
                if style == "bil":  return f"${val/1e9:.2f}B"
                if style == "dol":  return f"${val:.2f}"
                return f"{val:.2f}"
            rows = [
                ("ROIC",           roic,                               ""),
                ("Gross Margin",   fmt(live.gross_margin, "pct"),     "> 40% (Buffett)"),
                ("Debt / Equity",  fmt(live.debt_to_equity),          "< 0.5 (Buffett)"),
                ("Free Cash Flow", fmt(live.free_cash_flow, "bil"),   ""),
                ("P/E (Trailing)", fmt(live.pe_ratio, "x"),           "< 20 (Weiss)"),
                ("PEG Ratio",      fmt(live.peg_ratio),               "< 1.0 best (Lynch)"),
                ("Dividend Yield", fmt(live.dividend_yield, "pct2") if live.dividend_yield else "None", ""),
                ("Payout Ratio",   fmt(live.payout_ratio, "pct") if live.payout_ratio else "N/A", "< 50% (Weiss)"),
                ("Beta",           fmt(live.beta),                    "1.0 = market"),
                ("52wk High",      fmt(live.fifty_two_wk_high, "dol"),""),
                ("52wk Low",       fmt(live.fifty_two_wk_low, "dol"), ""),
                ("Earnings Growth",fmt(live.earnings_growth, "pct") if live.earnings_growth else "N/A", ""),
                ("Revenue Growth", fmt(live.revenue_growth, "pct") if live.revenue_growth else "N/A", ""),
            ]
            self._w(f"  {'Metric':<24} {'Value':>10}   Context\n", "header")
            self._w(f"  {'─'*58}\n", "dim")
            for label, value, ctx in rows:
                self._w(f"  {label:<24} {value:>10}   {ctx}\n", "dim")
            self._w("\n")

    def _write_long_narratives(self, results):
        from composite_score import format_composite_for_claude
        composite = results["composite"]
        ctx = format_composite_for_claude(composite)
        self._rule("Deep Dive — Claude Narratives")
        prompts = [
            ("Buffett — Moat & Valuation",      "Assess the Buffett scores in 3 sentences. Reference actual numbers."),
            ("Weiss — Yield & Blue Chip",        "Assess the Weiss scores in 3 sentences. Is yield actionable?"),
            ("Bogle — Timing",                   "Assess Bogle scores in 3 sentences. Good entry point?"),
            ("Dalio — Debt & Bubble Risk",       "Assess Dalio scores in 3 sentences. Balance sheet safe?"),
            ("Druckenmiller — Triple Alignment", "Assess Druckenmiller scores in 3 sentences. Conviction level?"),
            ("Portfolio Fit",                    f"Final 3-sentence recommendation for Johnathan given score {composite.total_score:.0f}/100. Which account?"),
        ]
        for section, prompt in prompts:
            if self.stop_requested: return
            self._w(f"  {section}\n", "header")
            self._w("  ⏳ Asking Claude...\n", "dim")
            answer = ask_lm_studio(prompt, ctx, self.portfolio_ctx)
            def _show_dive(ans=answer):
                self.chat.config(state="normal")
                pos = self.chat.search("  ⏳ Asking Claude...", "1.0", "end")
                if pos: self.chat.delete(pos, f"{pos} lineend+1c")
                self.chat.insert("end", f"  {ans}\n\n", "claude")
                self.chat.see("end")
                self.chat.config(state="disabled")
            self.root.after(0, _show_dive)

    def _ask_claude(self):
        if self.is_running or not self.session_results: return
        question = self.qa_var.get().strip()
        if not question: self.qa_entry.focus(); return
        self.is_running = True
        self.qa_btn.config(state="disabled", text="⏳")
        self.qa_entry.config(state="disabled")
        self.run_btn.config(state="disabled")
        threading.Thread(target=self._claude_thread, args=(question,), daemon=True).start()

    def _claude_thread(self, question):
        try:
            from composite_score import format_composite_for_claude
            ctx = format_composite_for_claude(self.session_results["composite"])
            self._rule("Claude Q&A")
            self._w(f"  Q: {question}\n\n", "blue")
            self._w("  ⏳ Thinking...\n", "dim")
            answer = ask_lm_studio(question, ctx, self.portfolio_ctx)
            def _show(ans=answer):
                self.chat.config(state="normal")
                pos = self.chat.search("  ⏳ Thinking...", "1.0", "end")
                if pos: self.chat.delete(pos, f"{pos} lineend+1c")
                self.chat.insert("end", f"  {ans}\n\n", "claude")
                self.chat.see("end")
                self.chat.config(state="disabled")
            self.root.after(0, _show)
        finally:
            self.is_running = False
            self.qa_btn.config(state="normal", text="💬 Ask", bg="#238636", fg="#FFFFFF")
            self.qa_entry.config(state="normal", fg=FG)
            self.run_btn.config(state="normal")
            self.qa_var.set("")
            self.qa_entry.focus()


if __name__ == "__main__":
    root = tk.Tk()
    app = StockAnalysisApp(root)
    root.mainloop()
