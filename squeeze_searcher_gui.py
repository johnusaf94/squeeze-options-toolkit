"""
squeeze_searcher_gui.py
========================
Standalone squeeze searcher — scans tiered universe for squeeze candidates.
Uses Keith Gill and Chamath Palihapitiya frameworks.

Requires in same folder:
  shared_utils.py, squeeze_analyzers.py, squeeze_universe.py,
  data_validator.py, ticker_resolver.py
"""

# ── GLOBAL yfinance RATE LIMITER ────────────────────────────────
# Must be imported BEFORE anything that uses yfinance. Monkey-
# patches yfinance.Ticker with token-bucket rate limiting + caching.
import yfinance_throttle  # noqa: F401  # installs global throttle


import tkinter as tk
from tkinter import scrolledtext
import threading
from shared_utils import *

PORTFOLIO_FILE = "portfolio.xlsx"


class SqueezeSearcherApp:
    def __init__(self, root):
        self.root = root
        self.root.title("🎯 Squeeze Searcher")
        self.root.geometry("1280x860")
        self.root.configure(bg=BG)
        self._sq_running = False
        self._sq_stop    = False
        self._sq_results = []
        self.portfolio_ctx = load_portfolio_context(PORTFOLIO_FILE)

        # Top bar
        top = tk.Frame(root, bg=BG2, pady=8)
        top.pack(fill="x")
        tk.Label(top, text="🎯 SQUEEZE SEARCHER", font=FONT_HD,
                 bg=BG2, fg=ACCENT).pack(side="left", padx=16)
        tk.Label(top, text="Scan universe for short squeeze candidates",
                 font=FONT_SM, bg=BG2, fg=FG_DIM).pack(side="left", padx=8)

        self.conn_lbl = tk.Label(top, text="⏳ checking...", font=FONT_SM, bg=BG2, fg=FG_DIM)
        self.conn_lbl.pack(side="right", padx=4)
        tk.Frame(top, bg=BORDER, width=1).pack(side="right", fill="y", padx=6, pady=4)
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

        self.tab_squeeze = tk.Frame(root, bg=BG)
        self.tab_squeeze.pack(fill="both", expand=True)
        self._build_squeeze_tab()

        # Ask Claude bar
        tk.Frame(root, bg=BORDER, height=1).pack(fill="x")
        qa = tk.Frame(root, bg=BG2, pady=6)
        qa.pack(fill="x")
        tk.Label(qa, text="Ask Claude:", font=FONT, bg=BG2, fg=FG_DIM).pack(side="left", padx=(12,4))
        self._sq_qa_var = tk.StringVar()
        self._sq_qa_entry = tk.Entry(qa, textvariable=self._sq_qa_var, font=FONT,
                                      bg=BG3, fg=FG_DIM, insertbackground=FG,
                                      relief="flat", bd=6, state="disabled")
        self._sq_qa_entry.pack(side="left", fill="x", expand=True, padx=6)
        self._sq_qa_entry.bind("<Return>", lambda e: self._sq_ask_claude())
        self._sq_qa_btn = tk.Button(qa, text="💬 Ask", font=("Consolas",10,"bold"),
                                     bg=BG3, fg=FG_DIM, relief="flat", cursor="hand2",
                                     padx=10, pady=4, state="disabled",
                                     command=self._sq_ask_claude)
        self._sq_qa_btn.pack(side="left", padx=(4,12))

        threading.Thread(target=self._sq_check_backend, daemon=True).start()

    def _build_squeeze_tab(self):
        """Squeeze Searcher — scans S&P500 from smallest to largest market cap."""
        parent = self.tab_squeeze
        self._sq_running   = False
        self._sq_stop      = False
        self._sq_results   = []   # list of (combined_score, gill, chamath, ticker)

        # ── LEFT: Controls (fixed panel — no scroll) ───────────────────
        ctrl = tk.Frame(parent, bg=BG2, width=285)
        ctrl.pack(side="left", fill="y")
        ctrl.pack_propagate(False)

        tk.Label(ctrl, text="SQUEEZE SEARCHER", font=FONT_LG,
                 bg=BG2, fg=ACCENT).pack(pady=(14,2), padx=14, anchor="w")
        tk.Label(ctrl, text="Tiered universe — high SI names first",
                 font=FONT_SM, bg=BG2, fg=FG_DIM).pack(padx=14, anchor="w")

        tk.Frame(ctrl, bg=BORDER, height=1).pack(fill="x", padx=10, pady=8)

        # ── UNIVERSE TIER SELECTOR ──
        tk.Label(ctrl, text="SEARCH UNIVERSE", font=FONT_SM,
                 bg=BG2, fg=FG_DIM).pack(padx=14, anchor="w", pady=(0,4))

        TIER_INFO = [
            ("T1: Chronic high-SI  (~270)",  "Meme/EV/biotech — known squeeze names"),
            ("T2: + Russell 2000   (~725)",  "Small caps — primary squeeze ground"),
            ("T3: + Mid cap growth (~903)",  "Growth stocks with short crowding"),
            ("T4: Full universe  (~1,100)",  "Complete coverage"),
        ]
        self._sq_tier_var = tk.IntVar(value=2)
        self._sq_tier_lbl = tk.Label(ctrl, text=TIER_INFO[1][1],
                                      font=("Consolas",7), bg=BG2, fg=YELLOW,
                                      wraplength=245, anchor="w")
        self._sq_tier_lbl.pack(padx=14, anchor="w", pady=(0,4))

        def _update_tier(*_):
            self._sq_tier_lbl.config(text=TIER_INFO[self._sq_tier_var.get()-1][1])

        tier_f = tk.Frame(ctrl, bg=BG2)
        tier_f.pack(fill="x", padx=10, pady=(0,4))
        for i, (label, _) in enumerate(TIER_INFO, 1):
            tk.Radiobutton(tier_f, text=label, variable=self._sq_tier_var, value=i,
                           font=("Consolas",8), bg=BG2, fg=FG, selectcolor=BG3,
                           activebackground=BG2, relief="flat",
                           command=_update_tier).pack(anchor="w", pady=1)

        tk.Frame(ctrl, bg=BORDER, height=1).pack(fill="x", padx=10, pady=6)

        def labeled_sq(label, default):
            f = tk.Frame(ctrl, bg=BG2)
            f.pack(fill="x", padx=14, pady=2)
            tk.Label(f, text=label, font=FONT_SM, bg=BG2, fg=FG_DIM,
                     width=16, anchor="w").pack(side="left")
            var = tk.StringVar(value=str(default))
            tk.Entry(f, textvariable=var, font=FONT_SM, bg=BG3, fg=FG,
                     insertbackground=FG, relief="flat", bd=4, width=8).pack(side="left")
            return var

        self._sq_max_stocks  = labeled_sq("Limit (0=all)", "0")
        self._sq_top_results = labeled_sq("Show top N", "25")

        tk.Frame(ctrl, bg=BORDER, height=1).pack(fill="x", padx=10, pady=8)

        # Squeeze thresholds
        tk.Label(ctrl, text="MINIMUM THRESHOLDS", font=FONT_SM,
                 bg=BG2, fg=FG_DIM).pack(padx=14, anchor="w", pady=(0,4))

        self._sq_min_si  = labeled_sq("Min Short Int %", "5")
        self._sq_min_dtc = labeled_sq("Min Days to Cover", "1")
        self._sq_min_score = labeled_sq("Min Combined Score", "30")

        tk.Frame(ctrl, bg=BORDER, height=1).pack(fill="x", padx=10, pady=8)

        # Agent selector
        tk.Label(ctrl, text="SQUEEZE AGENTS", font=FONT_SM,
                 bg=BG2, fg=FG_DIM).pack(padx=14, anchor="w", pady=(0,4))

        self._sq_use_gill    = tk.BooleanVar(value=True)
        self._sq_use_chamath = tk.BooleanVar(value=True)
        af = tk.Frame(ctrl, bg=BG2)
        af.pack(fill="x", padx=10)
        tk.Checkbutton(af, text="🎮 Keith Gill (DFV)",
                       variable=self._sq_use_gill,
                       font=FONT_SM, bg=BG2, fg=FG, selectcolor=BG3,
                       activebackground=BG2, relief="flat").pack(anchor="w")
        tk.Checkbutton(af, text="💰 Chamath Palihapitiya",
                       variable=self._sq_use_chamath,
                       font=FONT_SM, bg=BG2, fg=FG, selectcolor=BG3,
                       activebackground=BG2, relief="flat").pack(anchor="w")

        tk.Frame(ctrl, bg=BORDER, height=1).pack(fill="x", padx=10, pady=8)

        # Sort by
        tk.Label(ctrl, text="SORT RESULTS BY", font=FONT_SM,
                 bg=BG2, fg=FG_DIM).pack(padx=14, anchor="w", pady=(0,4))
        self._sq_sort_var = tk.StringVar(value="combined")
        sort_opts = [
            ("Combined Score",   "combined"),
            ("Gill Score",       "gill"),
            ("Chamath Score",    "chamath"),
            ("Short Interest %", "si"),
            ("Days to Cover",    "dtc"),
        ]
        for label, val in sort_opts:
            tk.Radiobutton(ctrl, text=label, variable=self._sq_sort_var, value=val,
                           font=FONT_SM, bg=BG2, fg=FG, selectcolor=BG3,
                           activebackground=BG2, relief="flat").pack(anchor="w", padx=14)

        tk.Frame(ctrl, bg=BORDER, height=1).pack(fill="x", padx=10, pady=8)

        # Run button
        self._sq_run_btn = tk.Button(ctrl, text="🎯  Start Squeeze Search",
                                      font=("Consolas",11,"bold"),
                                      bg=ACCENT, fg="#000000",
                                      relief="flat", cursor="hand2",
                                      padx=14, pady=6,
                                      command=self._sq_toggle)
        self._sq_run_btn.pack(fill="x", padx=10, pady=4)

        self._sq_export_btn = tk.Button(ctrl, text="📄  Export CSV for Analysis",
                                         font=("Consolas",10,"bold"),
                                         bg=BG3, fg=FG_DIM,
                                         relief="flat", cursor="hand2",
                                         padx=14, pady=5, state="disabled",
                                         command=self._sq_export_csv)
        self._sq_export_btn.pack(fill="x", padx=10, pady=(0,4))

        self._sq_status = tk.Label(ctrl, text="Ready — select tier & scan",
                                    font=FONT_SM, bg=BG2, fg=FG_DIM, wraplength=240)
        self._sq_status.pack(padx=14, pady=4, anchor="w")

        # ── RIGHT: Results ──────────────────────────────────────────────
        right = tk.Frame(parent, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        # Progress bar at top
        prog_frame = tk.Frame(right, bg=BG2, pady=6)
        prog_frame.pack(fill="x")

        self._sq_prog_lbl = tk.Label(prog_frame, text="",
                                      font=FONT_SM, bg=BG2, fg=FG_DIM)
        self._sq_prog_lbl.pack(side="left", padx=12)

        self._sq_prog_bar_frame = tk.Frame(prog_frame, bg=BG3, height=6)
        self._sq_prog_bar_frame.pack(side="left", fill="x", expand=True, padx=8, pady=4)
        self._sq_prog_fill = tk.Frame(self._sq_prog_bar_frame, bg=ACCENT, height=6, width=0)
        self._sq_prog_fill.place(x=0, y=0, relheight=1.0)

        tk.Frame(right, bg=BORDER, height=1).pack(fill="x")

        # Results log
        self._sq_log = scrolledtext.ScrolledText(
            right, wrap="word", font=FONT_SM, bg=BG, fg=FG,
            insertbackground=FG, relief="flat", borderwidth=0,
            state="disabled", padx=16, pady=10,
        )
        self._sq_log.pack(fill="both", expand=True)

        # Tags
        for tag, cfg in [
            ("header",   {"font": FONT_LG,                    "foreground": ACCENT}),
            ("dim",      {                                      "foreground": FG_DIM}),
            ("green",    {                                      "foreground": GREEN}),
            ("red",      {                                      "foreground": RED}),
            ("yellow",   {                                      "foreground": YELLOW}),
            ("blue",     {                                      "foreground": BLUE}),
            ("strong",   {"font": ("Consolas",10,"bold"),      "foreground": GREEN}),
            ("watch",    {"font": ("Consolas",10,"bold"),      "foreground": YELLOW}),
            ("pass_tag", {"font": ("Consolas",9),              "foreground": FG_DIM}),
        ]:
            self._sq_log.tag_config(tag, **cfg)

    # ── SQUEEZE SEARCH HELPERS ───────────────────────────────────────────


    def _on_backend_change(self, selection):
        import shared_utils as su
        backend, model = self._model_options.get(selection, ("local", "local-model"))
        su._ACTIVE_BACKEND = backend
        su._ACTIVE_ONLINE_MODEL = model
        threading.Thread(target=self._sq_check_backend, daemon=True).start()

    def _sq_check_backend(self):
        ok, msg = check_backend_status()
        self.conn_lbl.config(text=msg, fg=GREEN if ok else YELLOW)

    def _sq_ask_claude(self):
        if self._sq_running or not self._sq_results:
            return
        q = self._sq_qa_var.get().strip()
        if not q:
            self._sq_qa_entry.focus()
            return
        self._sq_running = True
        self._sq_qa_btn.config(state="disabled", text="⏳")
        self._sq_qa_entry.config(state="disabled")
        threading.Thread(target=self._sq_claude_thread, args=(q,), daemon=True).start()

    def _sq_claude_thread(self, question):
        try:
            # Build context from top candidates
            lines = ["SQUEEZE SCAN RESULTS — top candidates:"]
            for i, c in enumerate(self._sq_results[:10], 1):
                lines.append(
                    f"{i}. {c['ticker']} — FINAL {c.get('final_score', c.get('combined',0)):.0f} "
                    f"(combined {c.get('combined',0):.0f} x conv {c.get('conviction_mult',1):.2f}) "
                    f"SI {c.get('si',0):.0%} DTC {c.get('dtc',0):.1f} "
                    f"{c.get('conviction_state','')}"
                )
            context = "\n".join(lines)
            self._sq_write(f"\n  {'─'*60}\n", "dim")
            self._sq_write(f"  Q: {question}\n\n", "blue")
            self._sq_write("  ⏳ Thinking...\n", "dim")
            answer = ask_lm_studio(question, context, self.portfolio_ctx)
            def _show(ans=answer):
                self._sq_log.config(state="normal")
                pos = self._sq_log.search("  ⏳ Thinking...", "1.0", "end")
                if pos:
                    self._sq_log.delete(pos, f"{pos} lineend+1c")
                self._sq_log.insert("end", f"  {ans}\n\n", "green")
                self._sq_log.see("end")
                self._sq_log.config(state="disabled")
            self.root.after(0, _show)
        except Exception as e:
            self._sq_write(f"  ❌ Error: {e}\n", "red")
        finally:
            self._sq_running = False
            self.root.after(0, lambda: [
                self._sq_qa_btn.config(state="normal", text="💬 Ask",
                                        bg="#238636", fg="#FFFFFF"),
                self._sq_qa_entry.config(state="normal", fg=FG),
                self._sq_qa_var.set(""),
            ])

    def _sq_export_csv(self):
        """Export current scan results to an analysis-ready CSV.
        This is the file the user brings to Claude for analyst-mode review."""
        if not self._sq_results:
            self._sq_write("\n  ⚠️  No results to export — run a scan first.\n", "yellow")
            return
        try:
            import csv as _csv
            from datetime import datetime as _dt
            from tkinter import filedialog

            default_name = f"squeeze_analysis_{_dt.now().strftime('%Y%m%d_%H%M')}.csv"
            path = filedialog.asksaveasfilename(
                title="Export scan results for Claude analysis",
                defaultextension=".csv",
                initialfile=default_name,
                filetypes=[("CSV files", "*.csv")],
            )
            if not path:
                return

            # Analysis-ready columns — everything Claude needs for analyst mode
            cols = [
                "rank", "ticker", "company", "sector",
                "composite_pct", "catalyst_window", "catalyst_type", "catalyst_score",
                "catalyst_mult", "days_to_earnings",
                "final_score", "combined", "conviction_mult", "conviction_state",
                "deep_verdict", "probability", "imminence", "magnitude",
                "gill", "chamath",
                "si_pct", "dtc", "ctb",
                "ctb_trend", "dtc_trend", "si_trend",
                "otm_call_ratio", "call_put_oi_ratio", "convexity_skew_1w",
                "implied_move_pct", "gex_net_musd", "gex_regime",
                "calibrated_prob",
                "svr_recent", "svr_trend",
                "ftd_trend", "ftd_pct_float_accum", "ftd_closeout_date",
                "price_at_scan", "market_cap",
                "scan_time",
            ]

            scan_time = _dt.now().isoformat(timespec="seconds")
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = _csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                for rank, c in enumerate(self._sq_results, 1):
                    deep = c.get("deep")
                    w.writerow({
                        "rank":               rank,
                        "ticker":             c.get("ticker", ""),
                        "company":            (c.get("company", "") or "")[:50],
                        "sector":             c.get("sector", ""),
                        "final_score":        round(c.get("final_score", c.get("combined", 0)), 2),
                        "combined":           round(c.get("combined", 0), 2),
                        "conviction_mult":    round(c.get("conviction_mult", 1.0), 3),
                        "composite_pct":      round(c.get("composite_pct", 0), 1),
                        "catalyst_window":    c.get("catalyst_window", ""),
                        "catalyst_type":      c.get("catalyst_type", ""),
                        "catalyst_score":     round(c.get("catalyst_score", 0), 1),
                        "catalyst_mult":      round(c.get("catalyst_mult", 1.0), 3),
                        "days_to_earnings":   c.get("days_to_earnings", ""),
                        "conviction_state":   c.get("conviction_state", ""),
                        "deep_verdict":       c.get("deep_verdict", ""),
                        "probability":        round(c.get("probability", 0), 2),
                        "imminence":          round(c.get("imminence", 0), 2),
                        "magnitude":          round(c.get("magnitude", 0), 2),
                        "gill":               round(c.get("gill", 0), 2),
                        "chamath":            round(c.get("chamath", 0), 2),
                        "si_pct":             round(c.get("si", 0), 4),
                        "dtc":                round(c.get("dtc", 0), 2),
                        "ctb":                round(c.get("ctb", 0) or 0, 2),
                        "ctb_trend":          getattr(deep, "ctb_trend", "") if deep else "",
                        "dtc_trend":          getattr(deep, "dtc_trend", "") if deep else "",
                        "si_trend":           getattr(deep, "si_trend", "") if deep else "",
                        # None exports as BLANK, not 0 — "options data
                        # unavailable" and "zero call skew" are different
                        # facts; conflating them made the June 9 outage
                        # look like 156 names with no options interest.
                        "otm_call_ratio":     (round(deep.otm_call_ratio, 4)
                                               if deep and deep.otm_call_ratio is not None else ""),
                        "call_put_oi_ratio":  (round(deep.call_put_oi_ratio, 3)
                                               if deep and deep.call_put_oi_ratio is not None else ""),
                        "convexity_skew_1w":  (round(deep.convexity_skew_1w, 4)
                                               if deep and getattr(deep, "convexity_skew_1w", None) is not None else ""),
                        "implied_move_pct":   (round(deep.implied_move_pct, 4)
                                               if deep and deep.implied_move_pct is not None else ""),
                        "calibrated_prob":    (deep.calibrated_prob
                                               if deep and getattr(deep, "calibrated_prob", None) is not None else ""),
                        "gex_net_musd":       (deep.gex_net_musd
                                               if deep and deep.gex_net_musd is not None else ""),
                        "gex_regime":         (deep.gex_regime.split(" — ")[0]
                                               if deep and deep.gex_regime else ""),
                        "svr_recent":         (deep.svr_recent
                                               if deep and getattr(deep, "svr_available", False) else ""),
                        "svr_trend":          (deep.svr_trend
                                               if deep and getattr(deep, "svr_available", False) else ""),
                        "ftd_closeout_date":  getattr(deep, "ftd_closeout_date", "") if deep else "",
                        "ftd_trend":          getattr(deep, "ftd_trend", "") if deep else "",
                        "ftd_pct_float_accum":round(getattr(deep, "ftd_pct_float_accum", 0) or 0, 5) if deep else "",
                        "price_at_scan":      round(c.get("price", 0) or 0, 4),
                        "market_cap":         c.get("mktcap", 0) or 0,
                        "scan_time":          scan_time,
                    })

            self._sq_write(f"\n  📄 Exported {len(self._sq_results)} candidates\n", "green")
            self._sq_write(f"     → {path}\n", "dim")
            self._sq_write(f"     Bring this file to Claude for analyst-mode review\n", "dim")
            self._sq_status.config(text=f"Exported {len(self._sq_results)} to CSV")
        except Exception as e:
            self._sq_write(f"\n  ❌ Export failed: {e}\n", "red")

    def _sq_write(self, text, tag=None):
        self._sq_log.config(state="normal")
        self._sq_log.insert("end", text, tag) if tag else self._sq_log.insert("end", text)
        self._sq_log.see("end")
        self._sq_log.config(state="disabled")
        self.root.update_idletasks()


    def _sq_toggle(self):
        if self._sq_running:
            self._sq_stop = True
            self._sq_run_btn.config(text="⏹ Stopping...", bg=RED, state="disabled")
        else:
            self._sq_start()


    def _sq_start(self):
        try:
            max_stocks  = int(self._sq_max_stocks.get())
            top_n       = int(self._sq_top_results.get())
            min_si      = float(self._sq_min_si.get()) / 100.0
            min_dtc     = float(self._sq_min_dtc.get())
            min_score   = float(self._sq_min_score.get())
        except ValueError:
            self._sq_write("❌ Invalid inputs.\n", "red")
            return

        tier        = getattr(self, "_sq_tier_var", tk.IntVar(value=2)).get()
        use_gill    = self._sq_use_gill.get()
        use_chamath = self._sq_use_chamath.get()
        sort_by     = self._sq_sort_var.get()

        if not use_gill and not use_chamath:
            self._sq_write("❌ Select at least one squeeze agent.\n", "red")
            return

        self._sq_running = True
        self._sq_stop    = False
        self._sq_results = []
        self._sq_run_btn.config(bg=RED, fg="#000000", text="⏹ Stop")

        self._sq_log.config(state="normal")
        self._sq_log.delete("1.0", "end")
        self._sq_log.config(state="disabled")

        threading.Thread(
            target=self._sq_thread,
            args=(max_stocks, top_n, min_si, min_dtc, min_score,
                  use_gill, use_chamath, sort_by, tier),
            daemon=True,
        ).start()


    def _sq_thread(self, max_stocks, top_n, min_si, min_dtc, min_score,
                   use_gill, use_chamath, sort_by, tier=2):
        try:
            from squeeze_analyzers import (
                run_gill_analysis, run_chamath_analysis,
                fetch_squeeze_metrics
            )

            self._sq_write("\n")
            self._sq_write("  🎯 SQUEEZE SEARCHER\n", "header")

            # ── Load universe from squeeze_universe.py ──
            self._sq_status.config(text="Loading ticker universe...")
            try:
                # keep the universe current without manual runs:
                # no-op if universe_live.json is <5 days old; otherwise
                # refreshes in a background thread (this scan uses the
                # existing universe; the next one gets the new names)
                try:
                    from universe_refresh import auto_refresh_if_stale
                    _u_msg = auto_refresh_if_stale(background=True)
                    self._sq_write(f"  🌐 {_u_msg}\n", "dim")
                except Exception:
                    pass
                from squeeze_universe import get_universe
                universe = get_universe(
                    tier_max=tier,
                    limit=max_stocks if max_stocks > 0 else None
                )
                tier_names = {1:"Chronic+High-SI", 2:"+Russell2000",
                              3:"+MidCap", 4:"+S&P500(full)"}
                self._sq_write(
                    f"  Tier {tier} ({tier_names.get(tier,'')}) | "
                    f"{len(universe):,} tickers\n"
                    f"  Min SI: {min_si:.0%} | DTC ≥ {min_dtc:.1f}d | "
                    f"Score ≥ {min_score:.0f}\n"
                    f"  Chronic high-SI names scanned first.\n\n", "dim"
                )
            except ImportError:
                self._sq_write(
                    "  ⚠️  squeeze_universe.py not found — using fallback\n\n",
                    "yellow"
                )
                universe = self._sq_get_broad_universe(max_stocks or 500)

            total = len(universe)
            if total == 0:
                self._sq_write("❌ Empty universe. Check squeeze_universe.py.\n", "red")
                return

            candidates = []

            for i, ticker in enumerate(universe):
                if self._sq_stop:
                    self._sq_write("\n  ⏹ Stopped.\n", "dim")
                    break

                # Update progress bar
                pct = (i + 1) / total
                self._sq_status.config(text=f"[{i+1}/{total}] {ticker}")
                self._sq_prog_lbl.config(text=f"{i+1}/{total}")
                try:
                    bar_w = self._sq_prog_bar_frame.winfo_width()
                    self._sq_prog_fill.place(x=0, y=0,
                                              width=int(bar_w * pct),
                                              relheight=1.0)
                except Exception:
                    pass

                # Quick pre-filter: fetch raw metrics first
                try:
                    metrics = fetch_squeeze_metrics(ticker)
                except Exception as e:
                    self._sq_write(f"  ⚠️  {ticker}: fetch error — {e}\n", "dim")
                    continue

                # Pre-filter on minimum thresholds to skip obvious non-candidates fast
                si = metrics.short_interest_pct or 0
                dtc = metrics.days_to_cover or 0
                if si < min_si or dtc < min_dtc:
                    self._sq_write(
                        f"  ○ {ticker:<6}  SI:{si:.0%}  DTC:{dtc:.1f}d  — below threshold\n",
                        "pass_tag"
                    )
                    continue

                # Run full squeeze analyses
                gill_result    = None
                chamath_result = None
                gill_score     = 0
                chamath_score  = 0

                if use_gill:
                    try:
                        gill_result = run_gill_analysis(ticker)
                        gill_score  = gill_result.total_score
                    except Exception:
                        pass

                if use_chamath:
                    try:
                        chamath_result = run_chamath_analysis(ticker)
                        chamath_score  = chamath_result.total_score
                    except Exception:
                        pass

                combined = (gill_score + chamath_score) / (
                    (1 if use_gill else 0) + (1 if use_chamath else 0)
                )

                if combined < min_score:
                    self._sq_write(
                        f"  ○ {ticker:<6}  Score:{combined:.0f}  SI:{si:.0%}  DTC:{dtc:.1f}d  — score too low\n",
                        "pass_tag"
                    )
                    continue

                # Passed all filters — this is a candidate
                candidates.append({
                    "ticker":   ticker,
                    "company":  metrics.company_name,
                    "sector":   metrics.sector,
                    "gill":     gill_score,
                    "chamath":  chamath_score,
                    "combined": combined,
                    "si":       si,
                    "dtc":      dtc,
                    "ctb":      metrics.ctb_proxy,
                    "price":    metrics.current_price,
                    "mktcap":   metrics.market_cap,
                    "gill_obj": gill_result,
                    "ch_obj":   chamath_result,
                    "metrics_obj": metrics,
                    "verdict":  gill_result.verdict if gill_result else (chamath_result.verdict if chamath_result else ""),
                })

                verdict_tag = "strong" if combined >= 60 else "watch"
                self._sq_write(
                    f"  ✅ {ticker:<6}  Combined:{combined:.0f}  "
                    f"Gill:{gill_score:.0f}  Chamath:{chamath_score:.0f}  "
                    f"SI:{si:.0%}  DTC:{dtc:.1f}d\n",
                    verdict_tag
                )

            # ── Sort and display final results ──
            sort_key = {
                "combined": lambda x: x["combined"],
                "gill":     lambda x: x["gill"],
                "chamath":  lambda x: x["chamath"],
                "si":       lambda x: x["si"],
                "dtc":      lambda x: x["dtc"],
            }.get(sort_by, lambda x: x["combined"])

            candidates.sort(key=sort_key, reverse=True)

            # ══════════════════════════════════════════════════════════════
            # STAGE 2: DEEP ANALYSIS on top 25 finalists
            # Options convexity + CTB velocity + FTD accumulation.
            # Re-ranks by deep_score (Probability → Imminence → Magnitude).
            # ══════════════════════════════════════════════════════════════
            DEEP_N = 25
            finalists = candidates[:DEEP_N]
            # ── severity override: extreme fuel skips the rank gate ──
            # Stage-1 is saturated (an 8-pt band across the top names),
            # so rank alone decides deep dives almost randomly among
            # leaders — and a GRPN-class monster that stumbles at
            # stage-1 would never reach the ACTIVE SQUEEZE detector.
            # SI>=40% or CTB>=60 forces inclusion regardless of rank.
            try:
                from squeeze_deep import force_deep_dive
                _ids = {id(c) for c in finalists}
                forced = [c for c in candidates[DEEP_N:]
                          if force_deep_dive(
                              c.get("si_pct"),
                              c.get("ctb") or c.get("ctb_now"))]
                if forced:
                    finalists = finalists + forced
                    _ids |= {id(c) for c in forced}
                    self._sq_write(
                        f"  🔥 severity override: "
                        f"{', '.join(c.get('ticker','?') for c in forced)} "
                        f"forced into deep analysis (extreme SI/CTB "
                        f"levels)\n", "yellow")
            except ImportError:
                _ids = {id(c) for c in finalists}
            if finalists and not self._sq_stop:
                self._sq_write("\n")
                self._sq_write(f"  {'─'*70}\n", "dim")
                self._sq_write(f"  🔬 STAGE 2: DEEP ANALYSIS — Top {len(finalists)} finalists\n", "header")
                self._sq_write(f"  Options convexity + CTB velocity + FTD accumulation\n", "dim")
                self._sq_write(f"  Snapshots stored for week-over-week velocity tracking\n\n", "dim")
                try:
                    from squeeze_deep import run_deep_analysis
                    for di, c in enumerate(finalists):
                        if self._sq_stop:
                            break
                        self._sq_status.config(
                            text=f"Deep analysis {di+1}/{len(finalists)}: {c['ticker']}"
                        )
                        self._sq_prog_lbl.config(
                            text=f"Deep: {di+1}/{len(finalists)} — {c['ticker']}"
                        )
                        try:
                            deep = run_deep_analysis(
                                c['ticker'],
                                stage1_score=c['combined'],
                                metrics=c.get('metrics_obj')
                            )
                            c['deep']            = deep
                            c['deep_score']      = deep.deep_score
                            c['deep_verdict']    = deep.deep_verdict
                            c['probability']     = deep.probability_score
                            c['imminence']       = deep.imminence_score
                            c['magnitude']       = deep.magnitude_score
                            c['conviction_mult'] = deep.conviction_mult
                            c['conviction_state']= deep.conviction_state
                            c['catalyst_window'] = deep.catalyst_window
                            c['catalyst_type']   = getattr(deep, 'catalyst_type', '')
                            c['ftd_closeout_date'] = getattr(deep, 'ftd_closeout_date', '')
                            c['ftd_impact_factor'] = getattr(deep, 'ftd_impact_factor', None)
                            c['effective_float']   = getattr(deep, 'effective_float', None)
                            c['float_tightness']   = getattr(deep, 'float_tightness', None)
                            c['ftd_pct_float']     = getattr(deep, 'ftd_pct_float_accum', None)
                            c['ftd_pct_eff_float'] = getattr(deep, 'ftd_pct_eff_float_accum', None)
                            c['ftd_closeout_adv_days'] = getattr(deep, 'ftd_closeout_adv_days', None)
                            c['inst_shares_over_float'] = getattr(
                                c.get('metrics_obj'), 'institutional_shares_over_float', None)
                            c['scoring_version']   = getattr(deep, 'scoring_version', 1)
                            c['deep_score_v1']     = getattr(deep, 'deep_score_v1', None)
                            c['probability_v1']    = getattr(deep, 'probability_score_v1', None)
                            c['magnitude_v1']      = getattr(deep, 'magnitude_score_v1', None)
                            c['ftd_score_v1']      = getattr(deep, 'ftd_score_v1', None)
                            c['ftd_impact_factor_v1'] = getattr(deep, 'ftd_impact_factor_v1', None)
                            c['dtc_exchange']      = getattr(deep, 'dtc_exchange', None)
                            c['dtc_robust']        = getattr(deep, 'dtc_robust', None)
                            c['dtc_60d']           = getattr(deep, 'dtc_60d', None)
                            c['dtc_spike_ratio']   = getattr(deep, 'dtc_spike_ratio', None)
                            c['si_change_settlement'] = getattr(deep, 'si_change_settlement', None)
                            c['si_trend_source']   = getattr(deep, 'si_trend_source', '')
                            c['si_trend_v1']       = getattr(deep, 'si_trend_v1', '')
                            c['dtc_trend_v1']      = getattr(deep, 'dtc_trend_v1', '')
                            c['settlement_date']   = getattr(deep, 'settlement_date', '')
                            c['settlement_age_days'] = getattr(deep, 'settlement_age_days', None)
                            c['implied_move_pct'] = getattr(deep, 'implied_move_pct', None)
                            c['gex_net_musd']    = getattr(deep, 'gex_net_musd', None)
                            c['gex_regime']      = getattr(deep, 'gex_regime', '')
                            c['svr_recent']      = (getattr(deep, 'svr_recent', None)
                                                    if getattr(deep, 'svr_available', False) else None)
                            c['svr_trend']       = (getattr(deep, 'svr_trend', '')
                                                    if getattr(deep, 'svr_available', False) else '')
                            c['catalyst_score']  = deep.catalyst_score
                            c['catalyst_mult']   = deep.catalyst_mult
                            c['days_to_earnings']= deep.days_to_earnings
                            c['final_score']     = deep.final_score
                        except Exception as de:
                            c['deep_score']      = 0.0
                            c['deep_verdict']    = f"deep failed: {de}"
                            c['probability']     = 0.0
                            c['imminence']       = 0.0
                            c['magnitude']       = 0.0
                            c['conviction_mult'] = 1.0
                            c['conviction_state']= ""
                            c['catalyst_window'] = ""
                            c['catalyst_type']   = ""
                            c['catalyst_score']  = 0.0
                            c['catalyst_mult']   = 1.0
                            c['days_to_earnings']= None
                            c['final_score']     = c['combined']
                except ImportError:
                    self._sq_write("  ⚠️  squeeze_deep.py not found — skipping Stage 2\n\n", "yellow")
                    for c in finalists:
                        c['deep_score'] = c['combined']
                        c['final_score'] = c['combined']
                        c['conviction_mult'] = 1.0

                # ── CROSS-SECTIONAL PERCENTILE RANKING ──
                # Regime-adaptive: score each signal by where it falls
                # WITHIN this batch, not vs fixed thresholds. In a weak
                # field nothing scores high — that's the tool telling you
                # to stand down rather than forcing a trade.
                try:
                    # ── SHARED SETTLEMENT-DATE DAMPENER ──
                    # When 3+ finalists project the SAME FTD close-out date,
                    # that's a market-wide SEC settlement boundary, not a
                    # per-name catalyst (June 12 scan: five names all at
                    # 2026-06-19, inflating CORZ to FINAL 150). Each shared
                    # name keeps the window but loses 15% of its catalyst
                    # multiplier, and FINAL is recomputed honestly.
                    from collections import Counter as _Ctr
                    _ftd_dates = _Ctr(
                        c.get("ftd_closeout_date", "")
                        for c in finalists
                        if (c.get("catalyst_type") == "FTD_CLOSEOUT"
                            and c.get("ftd_closeout_date")))
                    _shared = {d for d, n in _ftd_dates.items() if n >= 3}
                    for c in finalists:
                        if (c.get("catalyst_type") == "FTD_CLOSEOUT"
                                and c.get("ftd_closeout_date") in _shared):
                            _old_m = c.get("catalyst_mult", 1.0) or 1.0
                            c["catalyst_mult"] = round(_old_m * 0.85, 3)
                            c["final_score"] = round(
                                (c.get("combined", 0) or 0)
                                * (c.get("conviction_mult", 1.0) or 1.0)
                                * c["catalyst_mult"], 2)
                            c["conviction_state"] = (
                                (c.get("conviction_state", "") or "")
                                + " | shared FTD date — de-weighted")

                    from squeeze_catalyst import apply_percentile_ranks
                    apply_percentile_ranks(finalists)
                    self._sq_write(
                        "  📊 Cross-sectional percentile ranking applied "
                        "(regime-adaptive)\n", "dim")
                except ImportError:
                    for c in finalists:
                        c['composite_pct'] = c.get('final_score', 0)

                # Re-rank by cross-sectional composite, then final_score.
                # Catalyst timing is weighted heaviest inside composite_pct
                # because it is the user's empirically proven edge.
                finalists.sort(
                    # FINAL leads the sort: it is the all-in number
                    # (combined x conviction x catalyst). Sorting by
                    # composite_pct first buried the multipliers — where
                    # ALL the differentiation now lives — and ranked
                    # JACK(72) above RH(107).
                    key=lambda x: (x.get('final_score', 0),
                                   x.get('composite_pct', 0),
                                   x.get('probability', 0),
                                   x.get('imminence', 0)),
                    reverse=True
                )
                candidates = finalists + [c for c in candidates[DEEP_N:]
                                          if id(c) not in _ids]

            self._sq_results = candidates

            self._sq_write("\n")
            self._sq_write(f"  {'─'*70}\n", "dim")
            self._sq_write(f"  TOP SQUEEZE CANDIDATES (ranked by FINAL — deep-adjusted)\n", "header")
            self._sq_write(f"  Stage-1 sort: {sort_by}  |  Stage-2: Probability→Imminence→Magnitude\n", "dim")
            self._sq_write(f"  Found {len(candidates)} candidates from {i+1} scanned\n\n", "dim")

            if not candidates:
                self._sq_write("  No candidates found matching your criteria.\n", "yellow")
                self._sq_write("  Try lowering Min Short Interest % or Min Score.\n", "dim")
            else:
                # Header — PCT% (cross-sectional) leads, catalyst window shown
                self._sq_write(
                    f"  {'Rank':<5} {'Ticker':<7} {'Company':<14} "
                    f"{'PCT%':>5}  {'FINAL':>6}  {'CatX':>4}  "
                    f"{'Conv':>5}  {'Event':>8}  Catalyst Window\n", "blue"
                )
                self._sq_write(f"  {'─'*108}\n", "dim")

                for rank, c in enumerate(candidates[:top_n], 1):
                    pct_s   = c.get("composite_pct", 0)
                    final_s = c.get("final_score", c.get("combined", 0))
                    conv_m  = c.get("conviction_mult", 1.0)
                    cat_m   = c.get("catalyst_mult", 1.0)
                    cwin    = c.get("catalyst_window", "") or "—"
                    dte     = c.get("days_to_earnings")
                    # Event column: WHICH catalyst is coming + when.
                    # E=earnings, FDA, RDT=readout, FTD=T+35 close-out,
                    # CNF=conference, LCK=lockup, LGL=legal, MAC=macro
                    _abbr = {"EARNINGS": "E", "FDA": "FDA", "READOUT": "RDT",
                             "FTD_CLOSEOUT": "FTD", "CONFERENCE": "CNF",
                             "LOCKUP": "LCK", "LEGAL": "LGL", "MACRO": "MAC"}
                    ctype = (c.get("catalyst_type") or "").upper()
                    tabbr = _abbr.get(ctype, ctype[:3] if ctype else "")
                    if dte is not None and tabbr and tabbr != "NON":
                        earn_s = f"{tabbr}{dte:+d}d"
                    elif dte is not None:
                        earn_s = f"{dte:+d}d"
                    else:
                        earn_s = "—"
                    # Window label
                    wlabel = {
                        "SWEET_SPOT": "🎯 SWEET SPOT (position now)",
                        "IMMINENT":   "⏰ IMMINENT (late)",
                        "PASSED":     "⚠️ PASSED (fuel spent)",
                        "TOO_FAR":    "⏳ TOO FAR",
                        "NONE":       "— no catalyst",
                    }.get(cwin, cwin)
                    # Color: sweet-spot timing is the prize
                    if cwin == "SWEET_SPOT":
                        tag = "strong"
                    elif cwin == "PASSED":
                        tag = "red"
                    elif pct_s >= 70:
                        tag = "watch"
                    else:
                        tag = "dim"

                    self._sq_write(
                        f"  #{rank:<4} {c['ticker']:<7} {c['company'][:14]:<14} "
                        f"{pct_s:>5.0f}  {final_s:>6.0f}  {cat_m:>3.2f}x  "
                        f"{conv_m:>4.2f}x  {earn_s:>8}  {wlabel}\n",
                        tag
                    )

                # Highlight TOP 5 — by FINAL score (combined × conviction × catalyst)
                # GATED (June 2026): "ACTIONABLE" requires a live catalyst
                # (SWEET_SPOT/IMMINENT) or a BUILDING/IGNITING verdict. When
                # the catalyst + conviction layers were data-dead (June 9
                # scan) FINAL collapsed to stage-1 statics and this block
                # crowned CRKN/SBNY-style DORMANT names "actionable" — the
                # exact failure the two-stage design exists to prevent.
                # An honest empty answer beats a fake top-5.
                self._sq_write(f"\n  {'─'*70}\n", "dim")
                self._sq_write(f"  ⭐ TOP 5 — HIGHEST CONVICTION ACTIONABLE\n\n", "header")

                def _is_actionable(c):
                    window = (c.get("catalyst_window") or "")
                    verdict = (c.get("deep_verdict") or "")
                    conv = (c.get("conviction_state") or "")
                    # ACTIVE SQUEEZE is actionable BY DEFINITION — it is a
                    # squeeze in progress. Without this, GRPN (SI 67%,
                    # CTB 109, +50% run, final 103) was grayed out while
                    # lower-final names made the list, because its window
                    # was TOO_FAR and its trend-driven verdict read
                    # DORMANT. The verdict floor in squeeze_deep also
                    # fixes this; this line makes the searcher robust on
                    # its own.
                    return (window in ("SWEET_SPOT", "IMMINENT")
                            or "BUILDING" in verdict or "IGNITING" in verdict
                            or "ACTIVE SQUEEZE" in conv)

                actionable = [c for c in candidates[:top_n] if _is_actionable(c)]
                top5_by_final = sorted(
                    actionable,
                    key=lambda c: c.get("final_score", 0),
                    reverse=True,
                )[:5]
                if not top5_by_final:
                    self._sq_write(
                        "  ⚠ No actionable names this scan — no live catalysts "
                        "(SWEET_SPOT/IMMINENT)\n    and no BUILDING/IGNITING "
                        "verdicts. Static metrics alone don't qualify.\n"
                        "    This is a stand-down read, not a stock-picking read.\n",
                        "watch")
                for rank, c in enumerate(top5_by_final, 1):
                    final_s = c.get("final_score", 0)
                    _ct = (c.get("catalyst_type") or "").replace("_", " ").title()
                    _dte = c.get("days_to_earnings")
                    _ev = (f"{_ct} {_dte:+d}d" if _ct and _dte is not None
                           else (c.get("catalyst_window", "") or "—"))
                    self._sq_write(
                        f"  #{rank}  {c['ticker']:<6} FINAL {final_s:.0f}  "
                        f"({c.get('combined',0):.0f} × {c.get('conviction_mult',1):.2f} × "
                        f"{c.get('catalyst_mult',1):.2f})  "
                        f"{_ev} | {c.get('catalyst_window','')} | "
                        f"{c.get('conviction_state','')}\n",
                        "strong" if final_s >= 100 else "watch"
                    )

                # Detailed breakdown — TOP 3 FROM THE ACTIONABLE CHART above
                # (#1-#3 here = #1-#3 there). Previously this showed the top 3
                # of the base table sort, so the breakdown could analyze names
                # the actionable chart didn't even list. Falls back to the
                # setup-sorted top 3 only when nothing is actionable, clearly
                # labeled so a stand-down scan can't masquerade as picks.
                breakdown_pool = top5_by_final[:3] if top5_by_final else candidates[:3]
                self._sq_write(f"\n  {'─'*70}\n", "dim")
                if top5_by_final:
                    self._sq_write(f"  DETAILED BREAKDOWN — TOP 3 ACTIONABLE "
                                   f"(#1-#3 from the ⭐ chart above)\n\n", "header")
                else:
                    self._sq_write(f"  DETAILED BREAKDOWN — TOP 3 BY SETUP "
                                   f"(reference only — NOTHING actionable this scan)\n\n",
                                   "header")

                from squeeze_analyzers import format_gill_display, format_chamath_display
                try:
                    from squeeze_deep import format_deep_display
                except ImportError:
                    format_deep_display = None

                for _bidx, c in enumerate(breakdown_pool, 1):
                    self._sq_write(f"  {'='*68}\n", "dim")
                    self._sq_write(f"  #{_bidx}  {c['ticker']} — {c['company']}\n", "strong")
                    self._sq_write(f"  Deep Score: {c.get('deep_score',0):.0f}  |  "
                                   f"{c.get('deep_verdict','—')}  |  "
                                   f"Stage-1: {c['combined']:.0f}\n\n", "watch")
                    # Deep analysis first — it's the decision driver
                    if c.get("deep") and format_deep_display:
                        self._sq_write(format_deep_display(c["deep"]) + "\n\n", "dim")
                    if c.get("gill_obj") and use_gill:
                        self._sq_write(f"  🎮 KEITH GILL\n", "blue")
                        self._sq_write(format_gill_display(c["gill_obj"]), "dim")
                    if c.get("ch_obj") and use_chamath:
                        self._sq_write(f"  💰 CHAMATH\n", "blue")
                        self._sq_write(format_chamath_display(c["ch_obj"]), "dim")

            # ── Log this scan for outcome tracking ──
            try:
                from squeeze_logger import log_scan, log_summary
                scan_id, ok = log_scan(candidates, tier=tier, top_n=25)
                if ok:
                    self._sq_write(f"\n  📝 Logged scan {scan_id} → squeeze_log.csv\n", "dim")
                    self._sq_write(f"     {log_summary()}\n", "dim")
                    self._sq_write(f"     Run review_outcomes.py weekly to grade results\n", "dim")
                else:
                    self._sq_write(f"\n  ⚠️  Log skipped: {scan_id}\n", "yellow")
            except ImportError:
                self._sq_write("\n  ⚠️  squeeze_logger.py not found — scan not logged\n", "yellow")
            except Exception as le:
                self._sq_write(f"\n  ⚠️  Logging error: {le}\n", "yellow")

            self._sq_prog_lbl.config(text=f"Done — {len(candidates)} candidates")
            self._sq_status.config(text=f"Done — {len(candidates)} squeeze candidates found")
            # Enable Ask Claude now that we have results
            self.root.after(0, lambda: [
                self._sq_qa_entry.config(state="normal", fg=FG),
                self._sq_qa_btn.config(state="normal", bg="#238636", fg="#FFFFFF"),
                self._sq_export_btn.config(state="normal", bg="#1f6feb", fg="#FFFFFF"),
            ])

        except Exception as e:
            import traceback
            self._sq_write(f"\n❌ Error: {e}\n", "red")
            self._sq_write(traceback.format_exc(), "dim")
            self._sq_status.config(text="Error")
        finally:
            self._sq_running = False
            self._sq_stop    = False
            self._sq_run_btn.config(state="normal", text="🎯  Start Squeeze Search",
                                     bg=ACCENT, fg="#000000")


    def _sq_get_sp500_by_marketcap(self, ascending=True) -> list:
        """
        Fetch S&P500 tickers sorted by market cap.
        ascending=True → smallest first (more squeeze candidates at small cap).
        Returns list of ticker strings.
        """
        import yfinance as yf

        tickers = []

        # Method 1: Wikipedia via requests + html.parser (no lxml needed)
        try:
            import pandas as pd
            import requests
            resp = requests.get(
                "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15
            )
            sp500 = pd.read_html(resp.text, attrs={"id": "constituents"})[0]
            sym_col = "Symbol" if "Symbol" in sp500.columns else sp500.columns[0]
            tickers = sp500[sym_col].str.replace(".", "-", regex=False).tolist()
            self._sq_write(f"  📋 Wikipedia: {len(tickers)} S&P500 tickers\n", "dim")
        except Exception as e:
            self._sq_write(f"  ⚠️  Wikipedia method failed: {e}\n", "dim")

        # Method 2: yfinance screener for US large/mid cap equities
        if not tickers:
            try:
                import yfinance as yf
                screener = yf.Screener()
                screener.set_body({
                    "offset": 0, "size": 500,
                    "sortField": "intradaymarketcap", "sortType": "DESC",
                    "quoteType": "EQUITY",
                    "query": {
                        "operator": "and",
                        "operands": [
                            {"operator": "eq",  "operands": ["region", "us"]},
                            {"operator": "gt",  "operands": ["intradaymarketcap", 2_000_000_000]},
                        ]
                    },
                    "userId": "", "userIdType": "guest"
                })
                quotes = screener.response.get("quotes", [])
                tickers = [q["symbol"] for q in quotes if q.get("symbol")]
                self._sq_write(f"  📋 Screener: {len(tickers)} large/mid cap tickers\n", "dim")
            except Exception as e:
                self._sq_write(f"  ⚠️  Screener method failed: {e}\n", "dim")

        # Method 3: Hardcoded S&P500-representative list as last resort
        if not tickers:
            self._sq_write("  ⚠️  Using built-in representative ticker list\n", "yellow")
            tickers = [
                # Small/micro cap (most squeeze candidates)
                "BBAI","MARA","RIOT","CLSK","CIFR","HUT","BTBT","ARBK",
                "SRRK","NKLA","RIDE","WKHS","GOEV","MULN","FFIE","CENN",
                "SPCE","JOBY","LILM","ACHR","EVTL","EHGO","GREE","HIVE",
                # Mid cap
                "GME","AMC","BBBY","KOSS","EXPR","NAKD","SNDL","CLOV",
                "WISH","WOOF","PAYO","OPEN","OFSG","PRCH","SKLZ","DKNG",
                # Large cap (lower squeeze probability but included)
                "NVDA","AMD","TSLA","META","GOOGL","MSFT","AAPL","AMZN",
                "JPM","BAC","WFC","GS","MS","C","USB","PNC","TFC","FITB",
                "XOM","CVX","COP","SLB","HAL","DVN","EOG","PXD","MPC","VLO",
                "LLY","UNH","JNJ","ABBV","MRK","ABT","TMO","DHR","ISRG","REGN",
            ]

        if not tickers:
            self._sq_write("  ❌ Could not build ticker list from any source.\n", "red")
            return []

        self._sq_write(f"  📋 Got {len(tickers)} S&P500 tickers — fetching market caps...\n", "dim")

        # Fetch market caps in bulk using yfinance download
        # Use info for a batch — this is slow but necessary for sorting
        # Speed optimization: use fast_info which is cached
        ticker_caps = {}
        batch_size = 50

        for i in range(0, min(len(tickers), 500), batch_size):
            batch = tickers[i:i+batch_size]
            if self._sq_stop:
                break
            try:
                # Download to get tickers validated, then pull fast_info
                for t in batch:
                    try:
                        fast = yf.Ticker(t).fast_info
                        mc = getattr(fast, "market_cap", None)
                        ticker_caps[t] = mc if mc else 0
                    except Exception:
                        ticker_caps[t] = 0
            except Exception:
                for t in batch:
                    ticker_caps[t] = 0

            self._sq_status.config(text=f"Sorting by market cap... {i+batch_size}/{len(tickers)}")

        # Sort by market cap
        sorted_tickers = sorted(
            ticker_caps.keys(),
            key=lambda t: ticker_caps.get(t, 0),
            reverse=not ascending
        )

        cap_order = "smallest → largest" if ascending else "largest → smallest"
        self._sq_write(f"  ✅ Sorted {len(sorted_tickers)} tickers by market cap ({cap_order})\n\n", "dim")
        return sorted_tickers


if __name__ == "__main__":
    root = tk.Tk()
    app = SqueezeSearcherApp(root)
    root.mainloop()
