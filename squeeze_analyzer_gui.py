"""
squeeze_analyzer_gui.py
========================
Standalone single-stock squeeze analyzer.
Runs Keith Gill (DFV) and Chamath Palihapitiya squeeze frameworks.

Requires in same folder:
  shared_utils.py, squeeze_analyzers.py, data_validator.py, ticker_resolver.py
"""

# ── GLOBAL yfinance RATE LIMITER ────────────────────────────────
# Must be imported BEFORE anything that uses yfinance. Monkey-
# patches yfinance.Ticker with token-bucket rate limiting + caching.
import yfinance_throttle  # noqa: F401  # installs global throttle


import tkinter as tk
from datetime import datetime
from tkinter import scrolledtext
import threading
from shared_utils import *

PORTFOLIO_FILE = "portfolio.xlsx"


class SqueezeAnalyzerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("🔬 Squeeze Analyzer")
        self.root.geometry("1280x820")
        self.root.configure(bg=BG)
        self._sa_running = False
        self._sa_stop    = False
        self._sa_results = None
        self.portfolio_ctx = load_portfolio_context(PORTFOLIO_FILE)

        self._build_top_bar()

        self.tab_squeeze_single = tk.Frame(root, bg=BG)
        self.tab_squeeze_single.pack(fill="both", expand=True)
        self._build_squeeze_analyzer_tab()

        threading.Thread(target=self._check_backend, daemon=True).start()


    def _build_top_bar(self):
        top = tk.Frame(self.root, bg=BG2, pady=8)
        top.pack(fill="x")
        tk.Label(top, text="🔬 SQUEEZE ANALYZER", font=FONT_HD,
                 bg=BG2, fg=ACCENT).pack(side="left", padx=16)
        tk.Label(top, text="Keith Gill + Chamath Palihapitiya",
                 font=FONT_SM, bg=BG2, fg=FG_DIM).pack(side="left", padx=8)

        # Backend status (right side)
        self.conn_lbl = tk.Label(top, text="⏳ checking...", font=FONT_SM, bg=BG2, fg=FG_DIM)
        self.conn_lbl.pack(side="right", padx=4)
        tk.Frame(top, bg=BORDER, width=1).pack(side="right", fill="y", padx=6, pady=4)

        # Model selector dropdown
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

    def _build_squeeze_analyzer_tab(self):
        parent = self.tab_squeeze_single
        self._sa_running = False
        self._sa_stop    = False
        self._sa_results = None
        main = tk.Frame(parent, bg=BG)
        main.pack(fill="both", expand=True)
        left = tk.Frame(main, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        self._sa_chat = scrolledtext.ScrolledText(
            left, wrap="word", font=FONT, bg=BG, fg=FG,
            insertbackground=FG, selectbackground=BORDER,
            relief="flat", borderwidth=0, state="disabled", padx=20, pady=16)
        self._sa_chat.pack(fill="both", expand=True)
        for tag, cfg in [
            ("header",       {"font": FONT_LG, "foreground": ACCENT}),
            ("dim",          {"foreground": FG_DIM}),
            ("green",        {"foreground": GREEN}),
            ("red",          {"foreground": RED}),
            ("yellow",       {"foreground": YELLOW}),
            ("blue",         {"foreground": BLUE}),
            ("score_strong", {"font": ("Consolas",12,"bold"), "foreground": GREEN}),
            ("score_watch",  {"font": ("Consolas",11,"bold"), "foreground": YELLOW}),
            ("score_pass",   {"font": ("Consolas",11,"bold"), "foreground": RED}),
            ("claude",       {"font": ("Consolas",10), "foreground": TEAL}),
        ]:
            self._sa_chat.tag_config(tag, **cfg)
        sb = tk.Frame(main, bg=BG2, width=250)
        sb.pack(side="right", fill="y")
        sb.pack_propagate(False)
        tk.Label(sb, text="SQUEEZE SCORE", font=FONT_SM, bg=BG2, fg=FG_DIM).pack(pady=(14,2), padx=10, anchor="w")
        box = tk.Frame(sb, bg=BG3)
        box.pack(fill="x", padx=6, pady=2)
        self._sa_lbl_ticker  = tk.Label(box, text="—", font=FONT_LG, bg=BG3, fg=ACCENT)
        self._sa_lbl_ticker.pack(pady=(8,0))
        self._sa_lbl_gill    = tk.Label(box, text="Gill: —", font=("Consolas",10,"bold"), bg=BG3, fg=FG_DIM)
        self._sa_lbl_gill.pack()
        self._sa_lbl_chamath = tk.Label(box, text="Chamath: —", font=("Consolas",10,"bold"), bg=BG3, fg=FG_DIM)
        self._sa_lbl_chamath.pack()
        self._sa_lbl_combined = tk.Label(box, text="—", font=("Consolas",28,"bold"), bg=BG3, fg=FG_DIM)
        self._sa_lbl_combined.pack(pady=(4,0))
        self._sa_lbl_verdict = tk.Label(box, text="—", font=("Consolas",9,"bold"), bg=BG3, fg=FG_DIM, wraplength=220)
        self._sa_lbl_verdict.pack(pady=(0,8), padx=6)
        tk.Frame(sb, bg=BORDER, height=1).pack(fill="x", padx=6, pady=6)
        tk.Label(sb, text="KEY METRICS", font=FONT_SM, bg=BG2, fg=FG_DIM).pack(anchor="w", padx=10, pady=(0,4))
        self._sa_metrics_frame = tk.Frame(sb, bg=BG2)
        self._sa_metrics_frame.pack(fill="x", padx=6)
        tk.Frame(sb, bg=BORDER, height=1).pack(fill="x", padx=6, pady=6)
        tk.Label(sb, text="AGENTS", font=FONT_SM, bg=BG2, fg=FG_DIM).pack(anchor="w", padx=10, pady=(0,4))
        self._sa_use_gill    = tk.BooleanVar(value=True)
        self._sa_use_chamath = tk.BooleanVar(value=True)
        af2 = tk.Frame(sb, bg=BG2)
        af2.pack(fill="x", padx=8)
        for text, var in [("🎮 Keith Gill (DFV)", self._sa_use_gill),
                           ("💰 Chamath Palihapitiya", self._sa_use_chamath)]:
            tk.Checkbutton(af2, text=text, variable=var, font=FONT_SM,
                           bg=BG2, fg=FG, selectcolor=BG3,
                           activebackground=BG2, relief="flat").pack(anchor="w")
        tk.Frame(sb, bg=BORDER, height=1).pack(fill="x", padx=6, pady=6)
        tk.Button(sb, text="Clear", font=FONT_SM, bg=BG3, fg=FG_DIM,
                  relief="flat", cursor="hand2", command=self._sa_clear).pack(fill="x", padx=6, pady=2)
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x")
        bot = tk.Frame(parent, bg=BG2, pady=8)
        bot.pack(fill="x")
        r1 = tk.Frame(bot, bg=BG2)
        r1.pack(fill="x", padx=12, pady=(0,4))
        tk.Label(r1, text="Ticker:", font=FONT, bg=BG2, fg=FG_DIM).pack(side="left")
        self._sa_ticker_var = tk.StringVar()
        self._sa_ticker_entry = tk.Entry(r1, textvariable=self._sa_ticker_var, font=FONT,
                                          bg=BG3, fg=FG, insertbackground=FG, relief="flat", bd=6, width=12)
        self._sa_ticker_entry.pack(side="left", padx=6)
        self._sa_ticker_entry.bind("<Return>", lambda e: self._sa_toggle())
        self._sa_run_btn = tk.Button(r1, text="▶  Analyze", font=("Consolas",11,"bold"),
                                      bg=ACCENT, fg="#000000", relief="flat",
                                      cursor="hand2", padx=14, pady=4, command=self._sa_toggle)
        self._sa_run_btn.pack(side="left", padx=4)
        self._sa_export_btn = tk.Button(r1, text="💾 Export CSV", font=FONT_SM,
                                         bg=BG2, fg=FG_DIM, relief="flat",
                                         cursor="hand2", padx=10, pady=4,
                                         state="disabled", command=self._sa_export)
        self._sa_export_btn.pack(side="left", padx=4)
        self._sa_heat_btn = tk.Button(r1, text="🔥 EV Heatmap", font=FONT_SM,
                                       bg=BG2, fg=FG_DIM, relief="flat",
                                       cursor="hand2", padx=10, pady=4,
                                       state="disabled",
                                       command=self._sa_show_heatmap)
        self._sa_heat_btn.pack(side="left", padx=4)
        self._sa_pl_btn = tk.Button(r1, text="📈 Contract P/L",
                                    font=FONT_SM, bg=BG2, fg=FG_DIM,
                                    relief="flat", cursor="hand2",
                                    padx=10, pady=4, state="disabled",
                                    command=self._sa_show_contract_pl)
        self._sa_pl_btn.pack(side="left", padx=4)
        self._sa_thesis_btn = tk.Button(r1, text="📄 Thesis",
                                        font=FONT_SM, bg=BG2, fg=FG_DIM,
                                        relief="flat", cursor="hand2",
                                        padx=10, pady=4, state="disabled",
                                        command=self._sa_show_thesis)
        self._sa_thesis_btn.pack(side="left", padx=4)
        self._sa_status_lbl = tk.Label(r1, text="Ready", font=FONT_SM, bg=BG2, fg=FG_DIM)
        self._sa_status_lbl.pack(side="left", padx=10)
        # Options-EV scenario input: "prob:move%, ..." — probabilities are
        # normalized; drives the strike matrix section after deep analysis
        tk.Label(r1, text="Scenarios p:%", font=FONT_SM, bg=BG2,
                 fg=FG_DIM).pack(side="left", padx=(14, 2))
        self._sa_scn_var = tk.StringVar(value="auto")
        tk.Entry(r1, textvariable=self._sa_scn_var, font=FONT_SM,
                 bg=BG3, fg=FG, insertbackground=FG, relief="flat",
                 bd=4, width=24).pack(side="left")
        tk.Label(r1, text="Max DTE", font=FONT_SM, bg=BG2,
                 fg=FG_DIM).pack(side="left", padx=(10, 2))
        self._sa_dte_var = tk.StringVar(value="35")
        tk.Entry(r1, textvariable=self._sa_dte_var, font=FONT_SM,
                 bg=BG3, fg=FG, insertbackground=FG, relief="flat",
                 bd=4, width=5).pack(side="left")
        tk.Frame(bot, bg=BORDER, height=1).pack(fill="x")
        r2 = tk.Frame(bot, bg=BG, pady=6)
        r2.pack(fill="x", padx=12)
        tk.Label(r2, text="Ask Claude:", font=FONT, bg=BG, fg=FG_DIM).pack(side="left")
        self._sa_qa_var = tk.StringVar()
        self._sa_qa_entry = tk.Entry(r2, textvariable=self._sa_qa_var, font=FONT,
                                      bg=BG3, fg=FG_DIM, insertbackground=FG, relief="flat", bd=6, state="disabled")
        self._sa_qa_entry.pack(side="left", fill="x", expand=True, padx=6)
        self._sa_qa_entry.bind("<Return>", lambda e: self._sa_ask_claude())
        self._sa_qa_btn = tk.Button(r2, text="💬 Ask", font=("Consolas",10,"bold"),
                                     bg=BG3, fg=FG_DIM, relief="flat", cursor="hand2",
                                     padx=10, pady=4, state="disabled", command=self._sa_ask_claude)
        self._sa_qa_btn.pack(side="left", padx=4)


    @staticmethod
    def _sa_build_csv_row(res):
        """One CSV row for the analyzed stock, matching the SEARCHER's
        export schema so prompts work identically on both files.
        Cross-sectional fields (rank, composite_pct) are blank — a single
        stock has no scan to be ranked within."""
        d = res.get("deep")
        m = res.get("metrics")
        gv = lambda o, a, dflt="": (getattr(o, a, dflt) if o is not None else dflt)
        def nz(v, nd=None):
            if v is None or v == "":
                return ""
            return round(v, nd) if nd is not None else v
        return {
            "rank": "", "ticker": res.get("ticker", ""),
            "company": gv(m, "company_name", "") or res.get("ticker", ""),
            "sector": gv(m, "sector", ""),
            "composite_pct": "",
            "catalyst_window":  gv(d, "catalyst_window", ""),
            "catalyst_type":    gv(d, "catalyst_type", ""),
            "catalyst_score":   nz(gv(d, "catalyst_score", None), 1),
            "catalyst_mult":    nz(gv(d, "catalyst_mult", None), 3),
            "days_to_earnings": gv(d, "days_to_earnings", ""),
            "final_score":      nz(gv(d, "final_score", None), 2),
            "combined":         nz(res.get("combined"), 2),
            "conviction_mult":  nz(gv(d, "conviction_mult", None), 3),
            "conviction_state": gv(d, "conviction_state", ""),
            "deep_verdict":     gv(d, "deep_verdict", ""),
            "probability":      nz(gv(d, "probability_score", None), 1),
            "imminence":        nz(gv(d, "imminence_score", None), 1),
            "magnitude":        nz(gv(d, "magnitude_score", None), 1),
            "gill":  nz((res.get("gill").total_score if res.get("gill") else None), 1),
            "chamath": nz((res.get("chamath").total_score if res.get("chamath") else None), 1),
            "si_pct": nz(gv(m, "short_interest_pct", None), 4),
            "dtc":    nz(gv(m, "days_to_cover", None), 2),
            "ctb":    nz(gv(m, "ctb_proxy", None), 1),
            "ctb_trend": gv(d, "ctb_trend", ""),
            "dtc_trend": gv(d, "dtc_trend", ""),
            "si_trend":  gv(d, "si_trend", ""),
            "otm_call_ratio":    nz(gv(d, "otm_call_ratio", None), 4),
            "call_put_oi_ratio": nz(gv(d, "call_put_oi_ratio", None), 3),
            "convexity_skew_1w": nz(gv(d, "convexity_skew_1w", None), 4),
            "implied_move_pct":  nz(gv(d, "implied_move_pct", None), 4),
            "gex_net_musd":      nz(gv(d, "gex_net_musd", None)),
            "gex_regime":        (gv(d, "gex_regime", "") or "").split(" — ")[0],
            "svr_recent": (nz(gv(d, "svr_recent", None), 4)
                           if gv(d, "svr_available", False) else ""),
            "svr_trend":  (gv(d, "svr_trend", "")
                           if gv(d, "svr_available", False) else ""),
            "ftd_trend":           gv(d, "ftd_trend", ""),
            "ftd_pct_float_accum": nz(gv(d, "ftd_pct_float_accum", None), 5),
            "ftd_closeout_date":   gv(d, "ftd_closeout_date", ""),
            "price_at_scan": nz(gv(m, "current_price", None), 4),
            "market_cap":    gv(m, "market_cap", ""),
            "scan_time": datetime.now().isoformat(timespec="seconds"),
            # ── Gamma flip zone (analyzer-only) — appended AFTER the shared
            # searcher columns so every shared field keeps its position;
            # the searcher CSV simply won't have these three. ──
            "gamma_flip_price":  nz(gv(d, "gamma_flip_price", None), 2),
            "gamma_flip_pct":    nz(gv(d, "gamma_flip_pct", None), 4),
            "gamma_flip_regime": (gv(d, "gamma_flip_regime", "") or "").split(" — ")[0],
        }

    def _sa_show_heatmap(self):
        """Strike x expiry map of the analyzed chain.

        TWO MODES, deliberately separated:
          VALUE (default) — 'val_edge': each strike priced against the IV
            smile implied by its NEIGHBORS (leave-one-out), vs its ask.
            Green = cheap relative to its own chain. Predicts NOTHING; it
            is a ruler for picking the efficient strike once your thesis
            exists. Needs no scenarios, no crush, no calibration.
          SIMULATION — the scenario metrics (profit @ expiry / @ catalyst
            exit, P(ITM)). These DO forecast, are only as honest as the
            scenarios, and keep feeding the learning database for later.

        View defaults to 3D. Window shrink-wraps to the active mode and
        the chrome is measured (not guessed) so nothing clips.
        """
        data = self._sa_guard_opt_data()
        if not data:
            self._sa_w("  ⚠ No options data for this ticker yet — "
                       "re-run the analysis (the options layer may have "
                       "failed on the last attempt).\n", "yellow")
            return
        try:
            import matplotlib
            matplotlib.use("TkAgg")
            import numpy as np
            import matplotlib.colors            # noqa: F401  (mask colormap)
            import matplotlib.patheffects as pe
            from matplotlib.figure import Figure
            from matplotlib.transforms import (
                blended_transform_factory as _blend)
            from matplotlib.backends.backend_tkagg import (
                FigureCanvasTkAgg, NavigationToolbar2Tk)
            from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
            from options_ev import (ev_grid, estimated_grid, scenario_stats,
                                     crush_grid, IV_CRUSH_MULT)

            VALUE_METRIC = "Value vs market (no scenario)"
            SIM_METRICS = {
                "Best contract (Kelly score)":             ("kelly",      (-0.5, 0.5)),
                "Avg profit % @ expiry (your scenarios)":  ("sc_ev",      (-1.0, 1.0)),
                "Avg profit % @ catalyst exit":            ("sc_ev_exit", (-1.0, 1.0)),
                "P(ITM) market-implied":                   ("p_itm_mkt",  (0.0, 1.0)),
            }
            METRICS = dict(SIM_METRICS)
            METRICS[VALUE_METRIC] = ("val_edge", (-0.50, 0.50))

            blocks = data["blocks"]
            strikes_all, expiries_all, _ = ev_grid(blocks)
            n_k, n_e = len(strikes_all), len(expiries_all)
            if not n_k:
                return

            win = tk.Toplevel(self.root)
            win.title(f"EV Heatmap — {data['ticker']}")
            win.configure(bg=BG)
            win.minsize(720, 560)
            # Force dark background on all frames so no white bleeds
            # through during resize transitions or fullscreen
            for _w in (win,):
                _w.option_add("*Background", BG)
                # option_add sets the DEFAULT for every child created after
                # it. Setting only the background made the matplotlib
                # navigation toolbar render near-black text and icons on a
                # near-black ground — effectively invisible. A default
                # background needs a matching default foreground.
                _w.option_add("*Foreground", FG)
                _w.option_add("*activeBackground", BG3)
                _w.option_add("*activeForeground", FG)

            _state = {"ax3d": None, "zoom": 1.0, "resizing": False}

            CONE_SIGMA = 1.5     # how wide "reachable" is, in sigma

            def _cone_bounds(expiries_l, days_map):
                """Price range the scenario distribution can actually reach by
                each expiry: the extreme scenario centres, widened by the
                within-branch volatility over that expiry's own life.

                Returns (lo_list, hi_list) in PRICE, or (None, None) when the
                inputs cannot support it."""
                try:
                    from options_ev import branch_sigma
                    scen = data.get("scenarios") or []
                    if not scen:
                        return None, None
                    spot_l = data["spot"]
                    aiv = next((b.get("atm_iv") for b in blocks
                                if b.get("atm_iv")), None)
                    lo_out, hi_out = [], []
                    for e in expiries_l:
                        T = max(days_map.get(e, 1), 1) / 365.0
                        sig = branch_sigma(scen, aiv, T, 1.0)
                        w = (sig * (T ** 0.5)) if sig else 0.0
                        centres = [spot_l * (1.0 + m) for _, m in scen]
                        lo_out.append(min(centres)
                                      * (1.0 - CONE_SIGMA * w))
                        hi_out.append(max(centres)
                                      * (1.0 + CONE_SIGMA * w))
                    return lo_out, hi_out
                except Exception:
                    return None, None

            def _strike_pos(strikes_l, price):
                """Fractional ROW index for a price on the strike axis, which
                runs high-to-low and is not evenly spaced."""
                n = len(strikes_l)
                if n == 0:
                    return 0.0
                if price >= strikes_l[0]:
                    return -0.5
                if price <= strikes_l[-1]:
                    return n - 0.5
                for i in range(n - 1):
                    hi, lo = strikes_l[i], strikes_l[i + 1]
                    if lo <= price <= hi:
                        f = (hi - price) / (hi - lo) if hi > lo else 0.0
                        return i + f
                return n - 0.5

            # ── control row 1: view + mode ──
            ctrl1 = tk.Frame(win, bg=BG2)
            ctrl1.pack(fill="x", side="top")
            three_var = tk.BooleanVar(value=True)      # 3D IS THE DEFAULT
            tk.Checkbutton(ctrl1, text="3D surface", variable=three_var,
                           command=lambda: _redraw(), font=FONT_SM, bg=BG2,
                           fg=FG, selectcolor=BG3, activebackground=BG2,
                           activeforeground=FG).pack(side="left",
                                                     padx=(10, 6), pady=5)
            sim_var = tk.BooleanVar(value=False)       # VALUE MAP IS DEFAULT
            tk.Checkbutton(ctrl1, text="Simulation mode (scenario forecasts)",
                           variable=sim_var, command=lambda: _on_mode(),
                           font=FONT_SM, bg=BG2, fg=FG, selectcolor=BG3,
                           activebackground=BG2,
                           activeforeground=FG).pack(side="left", padx=6)
            mode_lbl = tk.Label(ctrl1, text="", font=FONT_SM, bg=BG2,
                                fg=FG_DIM)
            mode_lbl.pack(side="left", padx=10)
            cone_var = tk.BooleanVar(value=False)
            tk.Checkbutton(ctrl1, text="Zoom to cone",
                           variable=cone_var, command=lambda: _redraw(),
                           font=FONT_SM, bg=BG2, fg=FG, selectcolor=BG3,
                           activebackground=BG2,
                           activeforeground=FG).pack(side="left", padx=6)

            def _open_best():
                """Argmax the CURRENT metric over real cells and open that
                exact contract in the Contract P/L view — the heatmap picks
                the contract, the P/L grid shows its life."""
                sh = _shared()
                strikes_b, exps_b, arr_b = sh[4], sh[5], sh[6]
                if np.all(np.isnan(arr_b)):
                    return
                i, j = np.unravel_index(np.nanargmax(arr_b), arr_b.shape)
                self._sa_show_contract_pl(
                    preselect=(exps_b[j], strikes_b[i]))

            tk.Button(ctrl1, text="\u2192 Contract P/L (best)",
                      font=FONT_SM, bg="#2da44e", fg="#FFFFFF",
                      relief="flat", cursor="hand2", padx=8, pady=2,
                      command=_open_best).pack(side="right", padx=10)

            # ── control row 2: metric + crush ──
            ctrl2 = tk.Frame(win, bg=BG2)
            ctrl2.pack(fill="x", side="top")
            tk.Label(ctrl2, text="Metric:", font=FONT_SM, bg=BG2,
                     fg=FG_DIM).pack(side="left", padx=(10, 2), pady=5)
            metric_var = tk.StringVar(value=VALUE_METRIC)
            metric_menu = tk.OptionMenu(ctrl2, metric_var, VALUE_METRIC)
            metric_menu.config(font=FONT_SM, bg=BG3, fg=FG, relief="flat",
                               highlightthickness=0, activebackground=BG3,
                               width=32, anchor="w")
            metric_menu["menu"].config(bg=BG3, fg=FG, font=FONT_SM)
            metric_menu.pack(side="left")

            crush_frame = tk.Frame(ctrl2, bg=BG2)
            tk.Label(crush_frame, text="Crush:", font=FONT_SM, bg=BG2,
                     fg=FG_DIM).pack(side="left", padx=(12, 2))
            _auto_mult = data.get("iv_crush", IV_CRUSH_MULT)
            _auto_lbl = (f"Auto {_auto_mult:.0%} "
                         f"({data.get('crush_method', 'default')})")
            CRUSH_OPTS = {_auto_lbl: _auto_mult, "100% (none)": 1.00,
                          "85%": 0.85, "70% (heavy)": 0.70,
                          "50% (severe)": 0.50}
            crush_var = tk.StringVar(value=_auto_lbl)
            cm = tk.OptionMenu(crush_frame, crush_var, *CRUSH_OPTS.keys(),
                               command=lambda *_: _redraw())
            cm.config(font=FONT_SM, bg=BG3, fg=FG, relief="flat",
                      highlightthickness=0, activebackground=BG3)
            cm["menu"].config(bg=BG3, fg=FG, font=FONT_SM)
            cm.pack(side="left")

            note_lbl = tk.Label(ctrl2, text="", font=FONT_SM, bg=BG2,
                                fg=FG_DIM)
            note_lbl.pack(side="left", padx=10)

            # ── rotation row (3D only) ──
            rot = tk.Frame(win, bg=BG2)
            rot.pack(fill="x", side="top")
            tk.Label(rot, text="Rotate:", font=FONT_SM, bg=BG2,
                     fg=FG_DIM).pack(side="left", padx=(10, 4))

            def _set_azim(v):
                if _state["ax3d"] is not None:
                    _state["ax3d"].view_init(elev=elev_scale.get(),
                                             azim=float(v))
                    canvas.draw_idle()

            azim_scale = tk.Scale(rot, from_=0, to=360, orient="horizontal",
                                  command=_set_azim, bg=BG2, fg=FG,
                                  troughcolor=BG3, highlightthickness=0,
                                  length=300, showvalue=True, font=FONT_SM)
            azim_scale.set(235)
            azim_scale.pack(side="left")
            tk.Label(rot, text="Tilt:", font=FONT_SM, bg=BG2,
                     fg=FG_DIM).pack(side="left", padx=(12, 4))
            elev_scale = tk.Scale(rot, from_=5, to=80, orient="horizontal",
                                  command=lambda v: _set_azim(azim_scale.get()),
                                  bg=BG2, fg=FG, troughcolor=BG3,
                                  highlightthickness=0, length=130,
                                  showvalue=True, font=FONT_SM)
            elev_scale.set(25)
            elev_scale.pack(side="left")

            # ── caption (packed BOTTOM first so it never gets squeezed out) ──
            caption = tk.Label(win, text="", font=FONT_SM, bg=BG, fg=FG_DIM,
                               wraplength=900, justify="left")
            caption.pack(side="bottom", fill="x", padx=8, pady=(2, 6))

            fig = Figure(figsize=(8.5, 6.5), dpi=100, facecolor=BG)
            canvas = FigureCanvasTkAgg(fig, master=win)
            # CRITICAL: set the Tk widget's background to match the figure.
            # Without this, any space the figure doesn't fill renders as
            # Tk's default white — the white bar at the bottom and the
            # fullscreen whiteout both come from this.
            canvas.get_tk_widget().config(bg=BG)
            toolbar = NavigationToolbar2Tk(canvas, win, pack_toolbar=False)
            toolbar.config(bg=BG2)
            # The toolbar builds its own buttons and labels and styles none of
            # them, so they inherit the dark default and disappear. Repaint
            # every child explicitly; widget types differ (Button, Label,
            # Frame, Checkbutton) and not all accept every option.
            def _style_toolbar(tb):
                for _c in tb.winfo_children():
                    for _opt in ({"bg": BG2, "fg": FG},
                                 {"highlightbackground": BG2},
                                 {"activebackground": BG3,
                                  "activeforeground": FG},
                                 {"relief": "flat", "borderwidth": 0}):
                        try:
                            _c.config(**_opt)
                        except tk.TclError:
                            pass
                    if _c.winfo_children():
                        _style_toolbar(_c)
            toolbar.update()
            _style_toolbar(toolbar)
            toolbar.pack(fill="x", side="top")
            canvas.get_tk_widget().pack(fill="both", expand=True,
                                        side="top")

            CAP_VALUE = ("VALUE MAP — each strike priced against the IV smile "
                         "of its NEIGHBOURS, vs its ask. Green = cheap "
                         "relative to its own chain; ~0% = fairly priced "
                         "(you pay the spread); red = rich. It forecasts "
                         "NOTHING — it picks the efficient strike once your "
                         "squeeze thesis exists. A strike beside a mispriced "
                         "outlier reads distorted (the outlier bends its "
                         "reference smile). Gray = too few neighbours to "
                         "judge.")
            CAP_SIM = ("SIMULATION — forecasts under YOUR scenarios; only as "
                       "honest as they are. Solid = real quotes, faded '~' = "
                       "surface estimates (not tradable where unlisted). "
                       "3D fills gaps to form a surface. These runs keep "
                       "feeding the learning database.")

            def _rebuild_metric_menu():
                menu = metric_menu["menu"]
                menu.delete(0, "end")
                opts = ([VALUE_METRIC] + list(SIM_METRICS)
                        if sim_var.get() else [VALUE_METRIC])
                for o in opts:
                    menu.add_command(
                        label=o,
                        command=lambda v=o: (metric_var.set(v), _redraw()))
                if metric_var.get() not in opts:
                    metric_var.set(VALUE_METRIC)

            def _on_mode():
                _rebuild_metric_menu()
                if sim_var.get():
                    crush_frame.pack(side="left")
                    mode_lbl.config(text="forecasting — scenarios apply",
                                    fg=YELLOW)
                    # auto-switch to the first sim metric instead of
                    # staying on the value map
                    metric_var.set(list(SIM_METRICS.keys())[0])
                else:
                    crush_frame.pack_forget()
                    metric_var.set(VALUE_METRIC)
                    mode_lbl.config(text="ruler — no forecast, no tuning",
                                    fg=GREEN)
                _redraw()

            # ── scroll zoom (3D) ──
            def _apply_zoom(ax3d):
                lims = _state.get("base_lims")
                if not lims:
                    return
                z = _state["zoom"]
                for setter, (lo, hi) in zip(
                        (ax3d.set_xlim, ax3d.set_ylim, ax3d.set_zlim), lims):
                    c = (lo + hi) / 2.0
                    half = (hi - lo) / 2.0 * z
                    setter(c - half, c + half)

            def _on_scroll(event):
                ax3d = _state.get("ax3d")
                if ax3d is None:
                    return
                step = getattr(event, "step", 0) or (
                    1 if event.button == "up" else -1)
                _state["zoom"] = min(max(_state["zoom"] * (0.9 ** step),
                                         0.25), 4.0)
                _apply_zoom(ax3d)
                canvas.draw_idle()
            canvas.mpl_connect("scroll_event", _on_scroll)

            def _on_click(event):
                """RIGHT-CLICK A CELL -> journal that exact contract.
                The 2D heat map draws strikes on the y axis and expiries on
                the x axis at integer positions, so the data coordinates round
                straight to grid indices. Ignored on the 3D view, where a
                click does not identify one cell unambiguously."""
                if getattr(event, "button", None) != 3:
                    return
                if _state.get("ax3d") is not None:
                    return
                if event.xdata is None or event.ydata is None:
                    return
                try:
                    sh = _shared()
                    strikes_b, exps_b = sh[4], sh[5]
                    j = int(round(event.xdata))
                    i = int(round(event.ydata))
                    if not (0 <= i < len(strikes_b) and 0 <= j < len(exps_b)):
                        return
                    K, exp = strikes_b[i], exps_b[j]
                    blk = next(b for b in blocks if b["expiry"] == exp)
                    r = next(x for x in blk["rows"]
                             if abs(x["strike"] - K) < 1e-9)
                except Exception:
                    return          # estimated/empty cell: nothing to journal
                try:
                    from options_journal_gui import (journal_dialog,
                                                     open_journal_window)
                except Exception:
                    return
                ctx = {"spot": data["spot"],
                       "scenario_text": ", ".join(
                           f"{p_ * 100:.0f}:{m_ * 100:+.1f}"
                           for p_, m_ in (data.get("scenarios") or [])),
                       "iv_crush": data.get("iv_crush", ""),
                       "carry": blk.get("carry", ""),
                       "fwd_method": blk.get("fwd_method", ""),
                       "catalyst_iso": data.get("catalyst_iso", ""),
                       "catalyst_type": ""}
                jid = journal_dialog(win, data["ticker"], exp, K,
                                     row=r, ctx=ctx)
                if jid:
                    open_journal_window(win, highlight=jid)

            canvas.mpl_connect("button_press_event", _on_click)

            def _on_resize(event):
                if _state.get("resizing"):
                    return
                try:
                    w = max(event.width / fig.get_dpi(), 3.0)
                    h = max(event.height / fig.get_dpi(), 3.0)
                    fig.set_size_inches(w, h, forward=False)
                    # tight_layout silently fails on 3D projections —
                    # use subplots_adjust which works for both
                    if _state.get("ax3d"):
                        fig.subplots_adjust(left=0.08, right=0.88,
                                            bottom=0.08, top=0.90)
                    else:
                        try:
                            fig.tight_layout()
                        except Exception:
                            fig.subplots_adjust(left=0.10, right=0.92,
                                                bottom=0.10, top=0.90)
                    canvas.draw_idle()
                except Exception:
                    pass
            canvas.get_tk_widget().bind("<Configure>", _on_resize)

            def _fit_window(is_3d):
                """Size figure to the mode, then wrap the window around it
                using MEASURED chrome height (guessing it clipped the top
                bar). Runs once per mode change."""
                if is_3d:
                    fw = min(max(7.0, 1.4 + 0.75 * n_e + 0.10 * n_k), 13)
                    fh = min(max(6.0, 1.3 + 0.45 * n_k), 10.5)
                else:
                    fw = min(max(7.5, 1.6 + 1.05 * n_e), 15)
                    fh = min(max(4.8, 1.4 + 0.30 * n_k), 10.5)
                _state["resizing"] = True
                try:
                    fig.set_size_inches(fw, fh, forward=False)
                    win.update_idletasks()
                    chrome = (ctrl1.winfo_reqheight() + ctrl2.winfo_reqheight()
                              + toolbar.winfo_reqheight()
                              + caption.winfo_reqheight() + 16)
                    if is_3d:
                        chrome += rot.winfo_reqheight()
                    px = int(fw * fig.get_dpi())
                    py = int(fh * fig.get_dpi()) + chrome
                    sh = win.winfo_screenheight() - 80
                    win.geometry(f"{px}x{min(py, sh)}")
                finally:
                    # Release after Tk finishes processing — 300ms handles
                    # Windows fullscreen transitions which fire multiple
                    # Configure events (140ms was too short, causing the
                    # guard to block the final resize)
                    win.after(300, lambda: _state.update(resizing=False))

            def _shared():
                mname = metric_var.get()
                metric, (vlo, vhi) = METRICS[mname]
                _crush = CRUSH_OPTS.get(crush_var.get(),
                                        data.get("iv_crush", IV_CRUSH_MULT))
                strikes, expiries, grid = ev_grid(blocks, metric=metric)
                if (metric == "sc_ev_exit"
                        and abs(_crush - data.get("iv_crush",
                                                  IV_CRUSH_MULT)) > 1e-9):
                    grid = crush_grid(blocks, data["spot"], data["scenarios"],
                                      data.get("catalyst_iso", ""), _crush)
                arr = np.array([[np.nan if v is None else v for v in row]
                                for row in grid], dtype=float)
                try:
                    est = estimated_grid(blocks, data["spot"],
                                         data["scenarios"], metric=metric,
                                         catalyst_iso=data.get("catalyst_iso", ""),
                                         iv_crush=_crush)
                    est_arr = np.array([[np.nan if v is None else v
                                         for v in row] for row in est],
                                       dtype=float)
                except Exception:
                    est_arr = np.full_like(arr, np.nan)
                days_by_exp = {b["expiry"]: b["days"] for b in blocks}
                from datetime import datetime as _dtm
                _eds = []
                for e in expiries:
                    try:
                        _eds.append(_dtm.strptime(e, "%Y-%m-%d").date())
                    except ValueError:
                        _eds.append(None)

                def _boundary(iso):
                    """Index of the first expiry that COVERS this date.
                    len(expiries) means no shown expiry reaches it."""
                    if not iso:
                        return None
                    try:
                        d0 = _dtm.strptime(iso[:10], "%Y-%m-%d").date()
                    except (ValueError, TypeError):
                        return None
                    return next((j for j, d in enumerate(_eds)
                                 if d and d >= d0), len(expiries))

                cat_iso = (data.get("catalyst_iso") or "")[:10]
                earn_iso = (data.get("earnings_iso") or "")[:10]
                cat_j = _boundary(cat_iso)
                earn_j = _boundary(earn_iso)
                # EVENTS is what the drawing code consumes: every known date
                # that matters, each with its own colour and label. Earnings
                # and an FTD close-out are different events on different
                # dates, and the chart previously showed only one of them.
                events = []
                if cat_j is not None:
                    events.append({"j": cat_j, "iso": cat_iso, "color": RED,
                                   "label": (data.get("catalyst_label")
                                             or f"catalyst {cat_iso}")})
                if earn_j is not None and earn_iso != cat_iso:
                    # Two events landing on the SAME expiry boundary would
                    # draw two identical lines on top of each other. Merge
                    # them into one marker naming both.
                    _same = next((e for e in events if e["j"] == earn_j), None)
                    if _same is not None:
                        _same["label"] += f" + earnings {earn_iso}"
                    else:
                        events.append({"j": earn_j, "iso": earn_iso,
                                       "color": "#F4C430",
                                       "label": f"earnings {earn_iso}"})
                if metric == "val_edge":
                    title = (f"{data['ticker']} @ ${data['spot']:,.2f} — "
                             f"value vs its own IV surface\n"
                             f"green = cheap relative to neighbouring "
                             f"strikes  ·  no forecast")
                else:
                    st = scenario_stats(data["scenarios"])
                    scn = ", ".join(f"{p:.0%}:{m:+.0%}"
                                    for p, m in data["scenarios"])
                    extra = ""
                    if metric == "sc_ev_exit":
                        _cat_past = False
                        if cat_iso:
                            try:
                                from datetime import datetime as _dtm2
                                _cat_past = (_dtm2.strptime(
                                    cat_iso, "%Y-%m-%d").date()
                                    < _dtm2.now().date())
                            except (ValueError, TypeError):
                                pass
                        if _cat_past:
                            extra = ("  |  \\u26a0 CATALYST PASSED — "
                                     "use expiry or value map")
                        else:
                            _is_auto = abs(_crush - data.get("iv_crush",
                                                             IV_CRUSH_MULT)) < 1e-9
                            extra = (f"  |  exit IV {_crush:.0%} "
                                     + (f"(AUTO: {data.get('crush_method','default')})"
                                        if _is_auto else "(SIMULATED)"))
                    title = (f"{data['ticker']} @ ${data['spot']:,.2f} — "
                             f"{mname}{extra}\nscenarios: {scn}  "
                             f"(E[move] {st['expected_move']:+.1%})")
                return (mname, metric, vlo, vhi, strikes, expiries, arr,
                        est_arr, days_by_exp, cat_iso, cat_j, title, events)

            def _norm_for(metric, vlo, vhi):
                """EV metrics routinely exceed +100%; a linear scale clips
                them into one flat green blob (the 'every map looks the
                same' bug). Asinh compresses the tails so +120% and +400%
                are visibly different, while 0 stays centered. Cell
                NUMBERS are always the true values."""
                from matplotlib import colors as _mc
                if metric in ("sc_ev", "sc_ev_exit"):
                    try:
                        return _mc.AsinhNorm(linear_width=0.5,
                                             vmin=-1.0, vmax=5.0)
                    except AttributeError:      # matplotlib < 3.6
                        return _mc.Normalize(vmin=vlo, vmax=vhi)
                return _mc.Normalize(vmin=vlo, vmax=vhi)

            def _fmt(v, vlo, metric):
                if metric == "p_itm_mkt":
                    return f"{v:.0%}"
                return f"{v:+.0%}"

            def _draw2d(sh):
                (mname, metric, vlo, vhi, strikes, expiries, arr, est_arr,
                 days_by_exp, cat_iso, cat_j, title, events) = sh
                xlabels = []
                for j, e in enumerate(expiries):
                    mark = ("\u2717 " if (cat_j is not None and j < cat_j)
                            else ("\u2713 " if cat_j is not None else ""))
                    xlabels.append(f"{mark}{e[5:]}\n({days_by_exp.get(e,'?')}d)")
                n_cells = len(strikes) * len(expiries)
                annotate = n_cells <= 400
                tick_fs = (10 if len(strikes) <= 6 else
                           (6 if len(strikes) > 25 else
                            (7 if len(strikes) > 14 else 8)))
                ax = fig.add_subplot(111)
                ax.set_facecolor(BG2)
                cmap = matplotlib.cm.get_cmap("RdYlGn").copy()
                cmap.set_bad(color="#2a2f3a")
                _nrm = _norm_for(metric, vlo, vhi)
                im = ax.imshow(np.ma.masked_invalid(arr), cmap=cmap,
                               norm=_nrm, aspect="auto")
                if not np.all(np.isnan(est_arr)):
                    ax.imshow(np.ma.masked_invalid(est_arr), cmap=cmap,
                              norm=_norm_for(metric, vlo, vhi),
                              aspect="auto", alpha=0.40, zorder=1)

                # ── CONE OF RETURNS ──
                # Every cell is drawn at full saturation, including strikes
                # the stock has essentially no chance of reaching by that
                # expiry. Those cells carry real numbers but no decision
                # value, and at full colour they compete for attention with
                # the strikes that matter. Dim what lies outside the range
                # the scenario distribution can actually produce, and draw
                # the boundary so the reachable region is explicit.
                cone_lo, cone_hi = _cone_bounds(expiries, days_by_exp)
                if cone_lo is not None:
                    _mask = np.zeros(arr.shape, dtype=float)
                    for _j in range(len(expiries)):
                        for _i, _k in enumerate(strikes):
                            if _k < cone_lo[_j] or _k > cone_hi[_j]:
                                _mask[_i, _j] = 1.0
                    if _mask.any():
                        ax.imshow(np.ma.masked_where(_mask < 0.5, _mask),
                                  cmap=matplotlib.colors.ListedColormap([BG]),
                                  aspect="auto", alpha=0.62, zorder=3,
                                  interpolation="nearest")
                    _xs = np.arange(len(expiries))
                    for _bound, _lbl in ((cone_lo, None), (cone_hi, "cone")):
                        _idx = [_strike_pos(strikes, v) for v in _bound]
                        ax.plot(_xs, _idx, color=BLUE, linewidth=1.6,
                                alpha=0.85, zorder=6,
                                linestyle=(0, (4, 2)), label=_lbl)
                    ax.text(len(expiries) - 0.6,
                            _strike_pos(strikes, cone_hi[-1]),
                            " reachable range", color=BLUE, fontsize=7.5,
                            va="bottom", ha="right", zorder=7,
                            path_effects=[pe.withStroke(linewidth=3,
                                                        foreground=BG)])
                if cone_var.get() and cone_lo is not None:
                    _top = min(_strike_pos(strikes, max(cone_hi)) - 0.5,
                               len(strikes) - 0.5)
                    _bot = max(_strike_pos(strikes, min(cone_lo)) + 0.5, -0.5)
                    if _bot > _top:
                        ax.set_ylim(_bot, _top)
                ax.set_xticks(range(len(expiries)))
                ax.set_xticklabels(xlabels, fontsize=tick_fs, color=FG)
                if cat_j is not None:
                    for j, lbl in enumerate(ax.get_xticklabels()):
                        lbl.set_color(RED if j < cat_j else GREEN)
                # Draw EVERY known event, not just the catalyst, stacking the
                # labels so an earnings date a week from a close-out does not
                # overprint it.
                for _ei, _ev in enumerate(events):
                    _x = min(_ev["j"], len(expiries)) - 0.5
                    _c = _ev["color"]
                    ax.axvline(_x, color=_c, linestyle=(0, (3, 2)),
                               linewidth=4.0, alpha=0.95, zorder=5,
                               path_effects=[pe.withStroke(linewidth=11,
                                                           foreground=_c,
                                                           alpha=0.30),
                                             pe.withStroke(linewidth=7,
                                                           foreground=_c,
                                                           alpha=0.45)])
                    _cl = _ev["label"]
                    if _ev["j"] == 0:
                        _txt = f"all shown expiries cover: {_cl}"
                    elif _ev["j"] >= len(expiries):
                        _txt = f"\u26a0 NO shown expiry covers {_cl}"
                    else:
                        _txt = f"\u25c0 before {_cl}  |  covers \u25b6"
                    ax.text(_x, 1.01 + 0.05 * _ei, _txt, color=_c, fontsize=8,
                            ha="center", va="bottom", clip_on=False,
                            fontweight="bold",
                            transform=_blend(ax.transData, ax.transAxes),
                            path_effects=[pe.withStroke(linewidth=3,
                                                        foreground=BG)])
                ax.set_yticks(range(len(strikes)))
                ax.set_yticklabels([f"${k:g}" for k in strikes],
                                   fontsize=tick_fs, color=FG)
                _spot = data["spot"]
                if _spot >= strikes[0]:
                    _sy = -0.5
                elif _spot <= strikes[-1]:
                    _sy = len(strikes) - 0.5
                else:
                    _sy = 0.0
                    for _i in range(len(strikes) - 1):
                        if strikes[_i] >= _spot >= strikes[_i + 1]:
                            _sp = strikes[_i] - strikes[_i + 1]
                            _sy = _i + ((strikes[_i] - _spot) / _sp
                                        if _sp else 0.5)
                            break
                ax.axhline(_sy, color=GREEN, linestyle=(0, (5, 2)),
                           linewidth=3.2, zorder=6,
                           path_effects=[pe.withStroke(linewidth=10,
                                                       foreground=GREEN,
                                                       alpha=0.30),
                                         pe.withStroke(linewidth=6,
                                                       foreground=GREEN,
                                                       alpha=0.45)])
                ax.text(len(expiries) - 0.45, _sy - 0.15,
                        f"spot ${_spot:,.2f}", color=GREEN, fontsize=8,
                        ha="right", va="bottom", fontweight="bold", zorder=7,
                        path_effects=[pe.withStroke(linewidth=3,
                                                    foreground=BG)])
                ax.set_title(title, fontsize=9, color=FG, pad=22)
                if annotate:
                    cell_fs = (12 if n_cells <= 12 else
                               (9 if n_cells <= 60 else
                                (8 if n_cells <= 200 else 6)))
                    for i in range(len(strikes)):
                        for j in range(len(expiries)):
                            v = arr[i][j]
                            if not np.isnan(v):
                                mid = (abs(v) < abs(vhi) * 0.55 if vlo < 0
                                       else 0.2 < v < 0.8)
                                ax.text(j, i, _fmt(v, vlo, metric),
                                        ha="center", va="center",
                                        fontsize=cell_fs,
                                        color="#000000" if mid else "#FFFFFF")
                                continue
                            ve = est_arr[i][j]
                            if not np.isnan(ve):
                                ax.text(j, i, "~" + _fmt(ve, vlo, metric),
                                        ha="center", va="center",
                                        fontsize=cell_fs, fontstyle="italic",
                                        color="#B8BCC8")
                _n = []
                if not annotate:
                    _n.append("cells unlabeled >400 — zoom with toolbar")
                if len(expiries) <= 2:
                    _n.append(f"only {len(expiries)} expiry(ies) within Max "
                              f"DTE — raise it to widen the map")
                note_lbl.config(text="  |  ".join(_n))
                cb = fig.colorbar(im, ax=ax, fraction=0.04, pad=0.02)
                cb.ax.tick_params(labelsize=7, colors=FG)

            def _draw3d(sh):
                (mname, metric, vlo, vhi, strikes, expiries, arr, est_arr,
                 days_by_exp, cat_iso, cat_j, title, events) = sh
                comb = np.where(np.isnan(arr), est_arr, arr)
                keep = []
                for j in range(comb.shape[1]):
                    col = comb[:, j]
                    ok = ~np.isnan(col)
                    if ok.sum() == 0:
                        continue
                    if ok.sum() == 1:
                        col[:] = col[ok][0]
                    elif ok.sum() < len(col):
                        idx = np.arange(len(col))
                        col[~ok] = np.interp(idx[~ok], idx[ok], col[ok])
                    comb[:, j] = col
                    keep.append(j)
                if len(keep) < 1 or len(strikes) < 2:
                    note_lbl.config(text="not enough data for a surface — "
                                         "showing 2D")
                    _draw2d(sh)
                    return
                Z = comb[:, keep]
                exps_k = [expiries[j] for j in keep]
                Xg, Yg = np.meshgrid(np.arange(len(keep)), np.array(strikes))
                if Z.shape[1] == 1:
                    Z = np.hstack([Z, Z])
                    Xg, Yg = np.meshgrid([0, 0.4], np.array(strikes))

                ax = fig.add_subplot(111, projection="3d")
                _state["ax3d"] = ax
                ax.set_facecolor(BG)
                for pane in (ax.xaxis, ax.yaxis, ax.zaxis):
                    pane.set_pane_color((0.07, 0.09, 0.12, 1.0))
                    pane.label.set_color(FG)
                ax.tick_params(colors=FG, labelsize=7)
                surf = ax.plot_surface(Xg, Yg, Z, cmap="RdYlGn",
                                       norm=_norm_for(metric, vlo, vhi),
                                       edgecolor="#000000", linewidth=0.15,
                                       antialiased=True, alpha=0.95)
                ax.set_xticks(range(len(exps_k)))
                ax.set_xticklabels([e[5:] for e in exps_k], fontsize=7,
                                   color=FG)
                ax.set_xlabel("Expiry", fontsize=8, color=FG, labelpad=8)
                ax.set_ylabel("Strike ($)", fontsize=8, color=FG, labelpad=8)
                ax.set_zlabel(("Value vs market" if metric == "val_edge"
                               else mname.split(" @")[0]),
                              fontsize=8, color=FG, labelpad=6)
                from matplotlib.ticker import FuncFormatter as _FF
                _zmin, _zmax = float(np.nanmin(Z)), float(np.nanmax(Z))
                _zpad = max((_zmax - _zmin) * 0.06, 0.05)
                ax.set_zlim(_zmin - _zpad, _zmax + _zpad)
                ax.zaxis.set_major_formatter(
                    _FF(lambda v, _p: (f"{v:.0%}" if metric == "p_itm_mkt"
                                       else f"{v:+.0%}")))
                _spot = data["spot"]
                zfloor = float(np.nanmin(Z))
                xs = np.linspace(0, max(len(exps_k) - 1, 0.4), 20)
                ax.plot(xs, [_spot] * len(xs), zs=zfloor, color=GREEN,
                        linestyle=(0, (5, 2)), linewidth=2.5, alpha=0.9)
                if cat_j is not None and 0 < cat_j < len(exps_k):
                    ys = np.linspace(min(strikes), max(strikes), 20)
                    ax.plot([cat_j - 0.5] * len(ys), ys, zs=zfloor, color=RED,
                            linestyle=(0, (3, 2)), linewidth=2.5, alpha=0.9)
                ax.view_init(elev=elev_scale.get(), azim=azim_scale.get())
                _state["base_lims"] = (ax.get_xlim(), ax.get_ylim(),
                                       ax.get_zlim())
                if _state.get("zoom", 1.0) != 1.0:
                    _apply_zoom(ax)
                ax.set_title(title, fontsize=9, color=FG, pad=12)
                cb = fig.colorbar(surf, ax=ax, fraction=0.035, pad=0.06)
                cb.ax.tick_params(labelsize=7, colors=FG)
                note_lbl.config(text="drag to rotate · scroll to zoom · "
                                     "sliders for exact angle")

            def _redraw():
                is_3d = three_var.get()
                caption.config(text=(CAP_SIM if sim_var.get() else CAP_VALUE))
                if is_3d:
                    rot.pack(fill="x", side="top", before=toolbar)
                    azim_scale.config(state="normal")
                    elev_scale.config(state="normal")
                else:
                    rot.pack_forget()
                _fit_window(is_3d)
                sh = _shared()
                fig.clf()
                _state["ax3d"] = None
                if is_3d:
                    _draw3d(sh)
                else:
                    _draw2d(sh)
                # tight_layout fails silently on 3D projections — use
                # subplots_adjust which works for both view modes
                if is_3d:
                    fig.subplots_adjust(left=0.05, right=0.88,
                                        bottom=0.05, top=0.92)
                else:
                    try:
                        fig.tight_layout()
                    except Exception:
                        fig.subplots_adjust(left=0.10, right=0.92,
                                            bottom=0.10, top=0.90)
                canvas.draw()

            _rebuild_metric_menu()
            _on_mode()          # sets value-mode chrome, then draws
        except Exception as e:
            self._sa_w(f"  \u26a0\ufe0f Heatmap error: {e}\n", "yellow")

    @staticmethod
    def _path_provenance() -> str:
        """One line saying whether the overlay's path shape was MEASURED and
        from how many episodes, or is the stylized fallback.

        Worth stating on the chart because an earlier version drew up and down
        as mirror images — which looked precise and was useless, since a
        symmetric overlay cannot express risk against reward."""
        try:
            from options_ev import learned as _learned
            ps = (_learned().get("path_shape_event") or {})
            sides = ps.get("sides") or {}
            if not sides:
                return ("Path shape: stylized fallback — not enough graded "
                        "episodes with an event date to measure it yet.")
            return (f"Path shape MEASURED from {ps.get('n_episodes', 0)} graded "
                    f"episodes, aligned to each one's own event date: up runs "
                    f"up and peaks BEFORE the event then eases; down drifts, "
                    f"steps down AT the event, and keeps bleeding. The two "
                    f"sides differ because the data says they do "
                    f"({', '.join(sorted(sides))} measured).")
        except Exception:
            return ""

    def _sa_show_thesis(self):
        """Build the one-page thesis for the analyzed ticker and open it.

        Everything else in this app is an instrument answering one narrow
        question. This assembles them into an argument with a verdict, the
        clock, and the case AGAINST given equal billing — which on this
        universe is usually the stronger side."""
        data = self._sa_guard_opt_data() or {}
        res = getattr(self, "_sa_results", None) or {}
        ticker = (res.get("ticker")
                  or (self._sa_ticker_var.get() or "").strip().upper())
        if not ticker:
            self._sa_w("  ⚠ Run an analysis first.\n", "yellow")
            return
        try:
            from thesis_page import build_thesis
            path = build_thesis(ticker, deep=res.get("deep"), opt=data,
                                open_after=True)
            self._sa_w(f"  📄 thesis written: {path}\n", "opt_hdr")
        except Exception as e:
            self._sa_w(f"  ⚠ Thesis unavailable: "
                       f"{type(e).__name__}: {e}\n", "yellow")

    def _sa_guard_opt_data(self):
        """Return the options data ONLY if it belongs to the ticker currently
        in the box. Belt to the reset's braces: if these ever disagree, the
        screen would be showing one stock's contracts under another's name,
        which is worse than showing nothing."""
        data = getattr(self, "_sa_opt_data", None)
        if not data or not data.get("blocks"):
            return None
        want = (self._sa_ticker_var.get() or "").strip().upper()
        if want and data.get("ticker", "").upper() != want:
            return None
        return data


    def _sa_show_contract_pl(self, preselect=None):
        """One contract through price x time — the OptionsProfitCalculator
        view, upgraded: IV crush applies after the catalyst (the decay
        cliff flat-IV sites can't show), and the analyzer's own squeeze
        scenario is drawn ON the grid with P/L annotated along it. The
        path is a SCENARIO ('if the squeeze beats expectations by the
        engine's up-move'), not a forecast."""
        data = self._sa_guard_opt_data()
        if not data:
            self._sa_w("  ⚠ No options data for this ticker yet — "
                       "re-run the analysis (the options layer may have "
                       "failed on the last attempt).\n", "yellow")
            return
        try:
            import matplotlib
            matplotlib.use("TkAgg")
            import numpy as np
            import matplotlib.patheffects as pe
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_tkagg import (
                FigureCanvasTkAgg, NavigationToolbar2Tk)
            from options_ev import contract_matrix, IV_CRUSH_MULT

            blocks = data["blocks"]
            spot = data["spot"]
            up_move = data["scenarios"][0][1] if data.get("scenarios") else 0.15
            cat_days = None
            ci = (data.get("catalyst_iso") or "")[:10]
            if ci:
                try:
                    from datetime import datetime as _dtm, date as _date
                    _cd = (_dtm.strptime(ci, "%Y-%m-%d").date()
                           - _date.today()).days
                    if _cd > 0:
                        cat_days = _cd
                except (ValueError, TypeError):
                    pass

            # ── AUTO-PICK: no preselect -> scan EVERY contract and open
            # on the highest squeeze-scenario exit return. This is a
            # LEVERAGE ranking (what pays most if the squeeze lands), so
            # it skews OTM; the heatmap's Kelly button is the
            # risk-adjusted alternative. Strikes under $0.05 excluded —
            # penny asks make return figures meaningless.
            # fallback IV: median of real IVs anywhere in the chain —
            # after-hours, whole expiries carry placeholder ~0 IVs
            _real_ivs = sorted(r["iv"] for b in blocks for r in b["rows"]
                               if r.get("iv") and r["iv"] > 0.05)
            fb_iv = (_real_ivs[len(_real_ivs) // 2] if _real_ivs else None)

            def _iv_for(r):
                if r.get("iv") and r["iv"] > 0.05:
                    return r["iv"], False
                return (fb_iv if fb_iv else 0.8), True

            auto_note = ""
            if preselect is None:
                # AUTO-PICK BY KELLY, NOT BY BEST CASE.
                # This used to open on whichever contract paid most IF the
                # squeeze landed, which structurally selects the cheapest
                # far-OTM strike on the board every time — maximum leverage,
                # maximum chance of expiring worthless. Kelly already
                # integrates the same distribution, round-trip costs, forward
                # and crush the table uses, and answers the question actually
                # being asked: which contract is the best BET. Best-case
                # return is still one click away in the heatmap.
                best_score, best_pick = None, None
                for b in blocks:
                    for r in b["rows"]:
                        if (r.get("ask") or 0) < 0.05:
                            continue
                        k = r.get("kelly")
                        if k is None:
                            continue
                        if best_score is None or k > best_score:
                            best_score = k
                            best_pick = (b["expiry"], r["strike"])
                if best_pick is not None:
                    preselect = best_pick
                    auto_note = (f"auto-picked: best risk-adjusted bet "
                                 f"(Kelly {best_score:+.2f})")

            win = tk.Toplevel(self.root)
            win.title(f"Contract P/L — {data['ticker']}")
            win.configure(bg=BG)
            win.option_add("*Background", BG)

            ctrl = tk.Frame(win, bg=BG2)
            ctrl.pack(fill="x")
            tk.Label(ctrl, text="Expiry:", font=FONT_SM, bg=BG2,
                     fg=FG_DIM).pack(side="left", padx=(10, 2), pady=5)
            _pre_exp = (preselect[0] if preselect and any(
                b["expiry"] == preselect[0] for b in blocks) else None)
            exp_var = tk.StringVar(value=_pre_exp or blocks[0]["expiry"])
            em = tk.OptionMenu(ctrl, exp_var, *[b["expiry"] for b in blocks],
                               command=lambda *_: _fill_strikes())
            em.config(font=FONT_SM, bg=BG3, fg=FG, relief="flat",
                      highlightthickness=0, activebackground=BG3)
            em["menu"].config(bg=BG3, fg=FG, font=FONT_SM)
            em.pack(side="left")
            tk.Label(ctrl, text="Strike:", font=FONT_SM, bg=BG2,
                     fg=FG_DIM).pack(side="left", padx=(12, 2))
            k_var = tk.StringVar(value="")
            km = tk.OptionMenu(ctrl, k_var, "")
            km.config(font=FONT_SM, bg=BG3, fg=FG, relief="flat",
                      highlightthickness=0, activebackground=BG3, width=18)
            km["menu"].config(bg=BG3, fg=FG, font=FONT_SM)
            km.pack(side="left")
            info_lbl = tk.Label(ctrl, text="", font=FONT_SM, bg=BG2,
                                fg=FG_DIM)
            info_lbl.pack(side="left", padx=10)

            # ── STRUCTURE SELECTOR ──
            # The window used to draw exactly one thing: a single long call.
            # The structure engine ranks spreads, calendars and puts on the
            # same distribution, so the grid now draws whichever of them you
            # pick. "Single contract" keeps the original expiry/strike
            # behaviour; anything else draws that structure's payoff and the
            # strike selector stops applying.
            _structs = data.get("structures") or {}
            # ORDER THE MENU BY WHAT YOU CAN ACTUALLY TRADE.
            # Affordable, positive-EV structures first (best Kelly at the
            # top), then the rest. Sorting purely by Kelly put an unaffordable
            # contract at the top of the list and buried the spread that was
            # the real answer.
            def _rankkey(kv):
                _, v = kv
                sz = v.get("sizing") or {}
                tradeable = (sz.get("contracts", 0) >= 1
                             and (v.get("ev") or -1) > 0)
                return (0 if tradeable else 1, -(v.get("kelly") or -9))
            _sorted_structs = sorted(_structs.items(), key=_rankkey)
            def _slabel(k, v):
                sz = v.get("sizing") or {}
                if sz.get("contracts", 0) >= 1:
                    return (f"{k}  —  {v['name']}   [{sz['contracts']}x "
                            f"${sz['dollars']:,.0f}]")
                return f"{k}  —  {v['name']}   [not sized]"
            _sopts = [_slabel(k, v) for k, v in _sorted_structs] +                      ["Single contract"]
            ctrl_s = tk.Frame(win, bg=BG2)
            ctrl_s.pack(fill="x")
            tk.Label(ctrl_s, text="Structure:", font=FONT_SM, bg=BG2,
                     fg=FG_DIM).pack(side="left", padx=(10, 2), pady=4)
            # DEFAULT TO THE RECOMMENDATION, NOT TO A BLANK SLATE.
            # This opened on "Single contract" every time, so the multi-leg
            # work was invisible unless you already knew to go looking for it.
            # If a structure is affordable and positive-EV, that is what the
            # window should be showing when it opens.
            _default = _sopts[0]
            if preselect is not None or not _sorted_structs:
                _default = "Single contract"
            else:
                _top = _sorted_structs[0][1]
                if not ((_top.get("sizing") or {}).get("contracts", 0) >= 1
                        and (_top.get("ev") or -1) > 0):
                    _default = "Single contract"
            struct_var = tk.StringVar(value=_default)
            sm_ = tk.OptionMenu(ctrl_s, struct_var, *_sopts,
                                command=lambda *_: _redraw())
            sm_.config(font=FONT_SM, bg=BG3, fg=FG, relief="flat",
                       highlightthickness=0, activebackground=BG3, width=52,
                       anchor="w")
            sm_["menu"].config(bg=BG3, fg=FG, font=FONT_SM)
            sm_.pack(side="left")
            struct_lbl = tk.Label(ctrl_s, text="", font=FONT_SM, bg=BG2,
                                  fg=TEAL)
            struct_lbl.pack(side="left", padx=10)

            def _selected_structure():
                sel = struct_var.get()
                if sel.startswith("Single"):
                    return None
                key = sel.split("  —  ")[0]
                return _structs.get(key)

            def _journal_this():
                """Record the CURRENTLY SELECTED contract, together with every
                number the model predicted for it. The predictions are frozen
                at entry so the trade can be graded against them later — that
                is the whole point of the journal."""
                try:
                    from options_journal_gui import (journal_dialog,
                                                     open_journal_window)
                except Exception as ie:
                    info_lbl.config(text=f"journal unavailable: {ie}")
                    return
                try:
                    b = _block()
                    K = float(k_var.get().split()[0].lstrip("$"))
                    r = next(x for x in b["rows"]
                             if abs(x["strike"] - K) < 1e-9)
                except (ValueError, IndexError, StopIteration):
                    info_lbl.config(text="select a strike first")
                    return
                ctx = {"spot": spot,
                       "scenario_text": ", ".join(
                           f"{p_ * 100:.0f}:{m_ * 100:+.1f}"
                           for p_, m_ in (data.get("scenarios") or [])),
                       "iv_crush": data.get("iv_crush", ""),
                       "carry": b.get("carry", ""),
                       "fwd_method": b.get("fwd_method", ""),
                       "catalyst_iso": data.get("catalyst_iso", ""),
                       "catalyst_type": ""}
                jid = journal_dialog(win, data["ticker"], b["expiry"],
                                     r["strike"], row=r, ctx=ctx)
                if jid:
                    info_lbl.config(text=f"journaled {jid}")
                    open_journal_window(win, highlight=jid)

            tk.Button(ctrl, text="📓 Journal this contract",
                      font=FONT_SM, bg="#8957e5", fg="#FFFFFF", relief="flat",
                      cursor="hand2", padx=10, pady=2,
                      command=_journal_this).pack(side="right", padx=10)

            fig = Figure(figsize=(11.5, 7.2), dpi=100, facecolor=BG)
            canvas = FigureCanvasTkAgg(fig, master=win)
            canvas.get_tk_widget().config(bg=BG)
            toolbar = NavigationToolbar2Tk(canvas, win, pack_toolbar=False)
            toolbar.config(bg=BG2)
            toolbar.update()
            toolbar.pack(fill="x")
            canvas.get_tk_widget().pack(fill="both", expand=True)
            tk.Label(win, text="Cells: contract P/L (% of premium) if the "
                     "stock is at that price on that day — IV crushed after "
                     "the catalyst. White line: the analyzer's squeeze "
                     "scenario with P/L marked along it. \u2691 = suggested "
                     "exit. A scenario, not a forecast.\n"
                     + self._path_provenance(),
                     font=FONT_SM, bg=BG, fg=FG_DIM, wraplength=1050,
                     justify="left").pack(fill="x", padx=8, pady=(2, 6))

            def _block():
                return next(b for b in blocks
                            if b["expiry"] == exp_var.get())

            def _fill_strikes(*_):
                b = _block()
                menu = km["menu"]
                menu.delete(0, "end")
                rows = sorted(b["rows"], key=lambda r: r["strike"])
                for r in rows:
                    lab = (f"${r['strike']:g}  (ask {r['ask']:.2f}"
                           + (" LAST-PX" if r.get("stale") else "") + ")")
                    menu.add_command(
                        label=lab,
                        command=lambda v=lab: (k_var.set(v), _redraw()))
                # default: preselected strike (pipeline from heatmap),
                # else nearest OTM above spot, else nearest to spot
                pick = None
                if preselect is not None:
                    for r in rows:
                        if abs(r["strike"] - preselect[1]) < 1e-9:
                            pick = r
                            break
                if pick is None:
                    otm = [r for r in rows if r["strike"] >= spot]
                    pick = (otm[0] if otm else rows[-1])
                k_var.set(f"${pick['strike']:g}  (ask {pick['ask']:.2f}"
                          + (" LAST-PX" if pick.get("stale") else "") + ")")
                _redraw()

            def _redraw():
                b = _block()
                try:
                    kstr = k_var.get().split()[0].lstrip("$")
                    K = float(kstr)
                except (ValueError, IndexError):
                    return
                r = next(x for x in b["rows"] if abs(x["strike"] - K) < 1e-9)
                _iv_e, _iv_est = _iv_for(r)
                _sres = _selected_structure()
                if _sres is not None:
                    try:
                        import options_structures as _ost
                        _sb = next(x for x in blocks
                                   if x["expiry"] == _sres["legs"][0]["expiry"])
                        _sm = (_sb.get("put_smile")
                               if _sres["legs"][0]["right"] == "P"
                               else _sb.get("smile"))
                        m = _ost.structure_matrix(
                            _sres, spot, data.get("scenarios"), cat_days,
                            data.get("iv_crush", IV_CRUSH_MULT), _sm,
                            _sb.get("carry", 0.0))
                        _sz = _sres.get("sizing") or {}
                        _ord = (f"   ORDER {_sz['contracts']}x = "
                                f"${_sz['dollars']:,.0f} "
                                f"({_sz['pct']:.0%} of capital)"
                                if _sz.get("contracts", 0) >= 1 else
                                f"   NOT SIZED ({_sz.get('bound_by','')})")
                        _be = m.get("breakevens") or []
                        _bes = (" · BE " + " / ".join(f"${x:,.2f}" for x in _be[:2])
                                if _be else "")
                        struct_lbl.config(
                            text=f"{_sres['name']}   debit ${_sres['debit']:.2f}"
                                 f"   EV {_sres['ev']:+.0%}"
                                 f"   Kelly {(_sres.get('kelly') or 0):+.2f}"
                                 f"   max +{m.get('max_profit', 0):.2f}/"
                                 f"{m.get('max_loss', 0):.2f}{_bes}{_ord}")
                    except Exception as _se:
                        _sres = None
                        struct_lbl.config(text=f"structure unavailable: {_se}")
                if _sres is None:
                    struct_lbl.config(text="")
                    m = contract_matrix(spot, K, max(b["days"], 1),
                                        carry=b.get("carry", 0.0),
                                        smile=b.get("smile"),
                                        half_spread=max(r["ask"] - r["bid"],
                                                        0.0) / 2.0,
                                        iv=_iv_e, entry_cost=r["ask"],
                                        up_move=up_move,
                                        scenarios=data.get("scenarios"),
                                        catalyst_days=cat_days,
                                        iv_crush=data.get("iv_crush",
                                                          IV_CRUSH_MULT))
                arr = np.array(m["pnl"], dtype=float)
                fig.clf()
                ax = fig.add_subplot(111)
                ax.set_facecolor(BG2)
                # color clip +/-100 so the gradient reads; numbers are true
                im = ax.imshow(np.clip(arr, -100, 100), cmap="RdYlGn",
                               vmin=-100, vmax=100, aspect="auto")
                # cell borders: minor-tick grid on half-integer boundaries
                ax.set_xticks(range(len(m["dates_d"])))
                ax.set_xticklabels([f"d{d}" for d in m["dates_d"]],
                                   fontsize=9, color=FG)
                ax.set_yticks(range(len(m["prices"])))
                ax.set_yticklabels([f"${p:.2f}" for p in m["prices"]],
                                   fontsize=9, color=FG)
                n_cells = arr.size
                if n_cells <= 500:
                    fs = 7 if n_cells > 250 else 8
                    for i in range(arr.shape[0]):
                        for j in range(arr.shape[1]):
                            v = arr[i][j]
                            ax.text(j, i, f"{v:+.0f}%", ha="center",
                                    va="center", fontsize=fs,
                                    color=("#000000" if abs(v) < 55
                                           else "#FFFFFF"))
                # catalyst column line + crush shading note
                if cat_days is not None and m["crush_from_day"]:
                    xs = [j for j, d in enumerate(m["dates_d"])
                          if d > m["crush_from_day"]]
                    if xs:
                        ax.axvline(xs[0] - 0.5, color=RED,
                                   linestyle=(0, (3, 2)), linewidth=3.0,
                                   path_effects=[pe.withStroke(
                                       linewidth=8, foreground=RED,
                                       alpha=0.35)])
                        ax.text(xs[0] - 0.5, -0.7,
                                f"catalyst d{m['crush_from_day']} — IV "
                                f"crush {data.get('iv_crush', 0.85):.0%} "
                                f"after", color=RED, fontsize=8,
                                ha="center", fontweight="bold",
                                clip_on=False,
                                path_effects=[pe.withStroke(
                                    linewidth=3, foreground=BG)])
                # spot row line
                i_spot = min(range(len(m["prices"])),
                             key=lambda i: abs(m["prices"][i] - spot))
                ax.axhline(i_spot, color=GREEN, linestyle=(0, (5, 2)),
                           linewidth=2.5, alpha=0.85)
                # squeeze path overlay: map (day, price) -> (col, row)
                def _col(d):
                    ds = m["dates_d"]
                    for j in range(len(ds) - 1):
                        if ds[j] <= d <= ds[j + 1]:
                            span = ds[j + 1] - ds[j]
                            return j + ((d - ds[j]) / span if span else 0)
                    return len(ds) - 1
                def _row(S):
                    ps = m["prices"]           # descending
                    if S >= ps[0]:
                        return 0
                    if S <= ps[-1]:
                        return len(ps) - 1
                    for i in range(len(ps) - 1):
                        if ps[i] >= S >= ps[i + 1]:
                            span = ps[i] - ps[i + 1]
                            return i + ((ps[i] - S) / span if span else 0)
                    return len(ps) - 1
                # ── SCENARIO BAND: translucent spread from worst-case
                # to best-case path (dotted edges) around the expected-
                # price line. Three discrete outcomes -> the band IS the
                # model's full spread (no invented percentiles).
                if m.get("paths"):
                    bd = m["paths"].get("best_dense", m["paths"]["best"])
                    wd = m["paths"].get("worst_dense", m["paths"]["worst"])
                    px_b = [_col(d) for d, S, _ in bd]
                    py_b = [_row(S) for _, S, _ in bd]
                    py_w = [_row(S) for _, S, _ in wd]
                    ax.fill_between(px_b, py_b, py_w, color="#FFFFFF",
                                    alpha=0.10, zorder=5)
                    # bolder dashed edges WITH dark halos — over red-on-red
                    # a thin colored line vanishes, so stroke each edge
                    # with a black outline first
                    ax.plot(px_b, py_b, color=GREEN, linewidth=2.6,
                            linestyle=(0, (4, 2)), zorder=7, alpha=1.0,
                            path_effects=[pe.withStroke(linewidth=5,
                                                        foreground="#000000")])
                    ax.plot(px_b, py_w, color=RED, linewidth=2.6,
                            linestyle=(0, (4, 2)), zorder=7, alpha=1.0,
                            path_effects=[pe.withStroke(linewidth=5,
                                                        foreground="#000000")])
                _pd = m.get("path_dense", m["path"])
                px = [_col(d) for d, S, _ in _pd]
                py = [_row(S) for _, S, _ in _pd]
                ax.plot(px, py, color="#FFFFFF", linewidth=3.0, zorder=6,
                        path_effects=[pe.withStroke(linewidth=7,
                                                    foreground="#FFFFFF",
                                                    alpha=0.30)])
                # annotate: entry + catalyst on the expected line; exit
                # shows the full best/expected/worst spread
                ed = m["exit_day"]
                if m.get("paths"):
                    _ep = m["exit_pnls"]
                    exit_txt = (f"\u2691 exit d{ed}\n"
                                f"best {_ep['best']:+.0f}% / "
                                f"E[P/L] {_ep['expected']:+.0f}% / "
                                f"worst {_ep['worst']:+.0f}%")
                else:
                    exit_txt = f"\u2691 exit\n{m['path'][ed][2]:+.0f}%"
                marks = [(0, f"entry\n{m['path'][0][2]:+.0f}%")]
                if cat_days:
                    cd = min(cat_days, m["path"][-1][0])
                    if cd not in (0, ed):
                        marks.append(
                            (cd, f"catalyst\n{m['path'][cd][2]:+.0f}%"))
                marks.append((ed, exit_txt))
                _last_col = len(m["dates_d"]) - 1
                for d, txt in marks:
                    dd, S, _ = m["path"][min(d, len(m["path"]) - 1)]
                    cx = _col(dd)
                    # near the right edge, place the label to the LEFT of
                    # its point and right-align it, so it stays on-axes
                    # (the colorbar used to absorb this; it's gone now)
                    near_right = cx >= _last_col - 0.5
                    dx = -0.4 if near_right else 0.3
                    ha = "right" if near_right else "left"
                    # zorder 10 puts the labels + arrows ABOVE the white
                    # path and the dashed band edges (z6-7); a dark backing
                    # box makes text legible wherever it lands
                    ax.annotate(txt, xy=(cx, _row(S)),
                                xytext=(cx + dx, _row(S) - 1.8),
                                color="#FFFFFF", fontsize=9,
                                fontweight="bold", ha=ha, zorder=10,
                                bbox=dict(boxstyle="round,pad=0.3",
                                          facecolor=BG, edgecolor="#FFFFFF",
                                          linewidth=0.8, alpha=0.92),
                                path_effects=[pe.withStroke(
                                    linewidth=3, foreground=BG)],
                                arrowprops=dict(arrowstyle="->",
                                                color="#FFFFFF", lw=1.8,
                                                zorder=10))
                ax.set_title(
                    f"{data['ticker']} ${K:g}C {b['expiry']} — entry "
                    f"{r['ask']:.2f}"
                    + (" (LAST-PX, stale)" if r.get("stale") else "")
                    + (f"  ·  IV~{_iv_e:.0%} EST (quote IV missing)"
                       if _iv_est else "")
                    + f"  ·  squeeze path +{up_move:.0%} into catalyst\n"
                    f"cells = P/L% of premium at (price, day); color "
                    f"saturates ±100%", fontsize=9, color=FG, pad=14)
                if m.get("paths"):
                    _e = m["exit_pnls"]
                    info_lbl.config(
                        text=(f"exit d{m['exit_day']}: best "
                              f"{_e['best']:+.0f}% / "
                              f"E[P/L] {_e['expected']:+.0f}% / "
                              f"worst "
                              f"{_e['worst']:+.0f}%"
                              + (f"   ·   {auto_note}" if auto_note else "")))
                else:
                    info_lbl.config(
                        text=f"path exit d{m['exit_day']}: "
                             f"{m['path'][m['exit_day']][2]:+.0f}%")
                # no colorbar — every cell is labeled with its exact P/L,
                # so the bar is redundant and was covering the exit marker
                try:
                    fig.tight_layout()
                except Exception:
                    pass
                canvas.draw()

            _fill_strikes()
        except Exception as e:
            self._sa_w(f"  \u26a0\ufe0f Contract P/L error: {e}\n", "yellow")

    def _sa_export(self):
        """Export the analyzed stock to a one-row CSV (searcher schema)."""
        res = self._sa_results
        if not res:
            return
        try:
            import csv as _csv
            row = self._sa_build_csv_row(res)
            fname = (f"squeeze_{res['ticker']}_"
                     f"{datetime.now().strftime('%Y%m%d_%H%M')}.csv")
            with open(fname, "w", newline="", encoding="utf-8") as f:
                w = _csv.DictWriter(f, fieldnames=list(row.keys()))
                w.writeheader()
                w.writerow(row)
            self._sa_status_lbl.config(text=f"Exported → {fname}")
            self._sa_w(f"\n  💾 Exported to {fname}\n", "green")
        except Exception as e:
            self._sa_w(f"\n  ❌ Export failed: {e}\n", "red")

    def _sa_w(self, text, tag=None):
        def _do():
            self._sa_chat.config(state="normal")
            if tag: self._sa_chat.insert("end", text, tag)
            else:   self._sa_chat.insert("end", text)
            self._sa_chat.see("end")
            self._sa_chat.config(state="disabled")
        self.root.after(0, _do)


    def _sa_rule(self, label=""):
        pad = max(0, (60 - len(label) - 2) // 2) if label else 0
        self._sa_w(f"\n{'─'*pad} {label} {'─'*pad}\n\n" if label else f"\n{'─'*60}\n\n", "dim")


    def _sa_clear(self):
        self._sa_chat.config(state="normal")
        self._sa_chat.delete("1.0", "end")
        self._sa_chat.config(state="disabled")
        self._sa_results = None
        self._sa_lbl_ticker.config(text="—", fg=ACCENT)
        self._sa_lbl_gill.config(text="Gill: —", fg=FG_DIM)
        self._sa_lbl_chamath.config(text="Chamath: —", fg=FG_DIM)
        self._sa_lbl_combined.config(text="—", fg=FG_DIM)
        self._sa_lbl_verdict.config(text="—", fg=FG_DIM)
        for w in self._sa_metrics_frame.winfo_children(): w.destroy()
        self._sa_qa_entry.config(state="disabled", fg=FG_DIM)
        self._sa_qa_btn.config(state="disabled", bg=BG3, fg=FG_DIM)


    def _sa_toggle(self):
        if self._sa_running:
            self._sa_stop = True
            self._sa_run_btn.config(text="⏹ Stopping...", bg=RED, state="disabled")
        else:
            ticker = self._sa_ticker_var.get().strip().upper()
            if not ticker: self._sa_ticker_entry.focus(); return
            self._sa_start(ticker)


    def _sa_start(self, ticker):
        self._sa_running = True
        self._sa_stop    = False
        self._sa_results = None
        # Options state belongs to ONE ticker. Clear it before anything can
        # fail, so a failed options layer can never leave the previous
        # ticker's chain reachable behind the P/L and heatmap buttons.
        self._sa_opt_data = None
        try:
            self._sa_heat_btn.config(state="disabled", bg=BG3, fg=FG_DIM)
            self._sa_pl_btn.config(state="disabled", bg=BG3, fg=FG_DIM)
            self._sa_thesis_btn.config(state="disabled", bg=BG3, fg=FG_DIM)
        except Exception:
            pass
        self._sa_run_btn.config(bg=RED, fg="#000000", text="⏹ Stop")
        self._sa_ticker_entry.config(state="disabled")
        self._sa_qa_entry.config(state="disabled", fg=FG_DIM)
        self._sa_qa_btn.config(state="disabled", bg=BG3, fg=FG_DIM)
        self._sa_status_lbl.config(text=f"Analyzing {ticker}...")
        self._sa_chat.config(state="normal")
        self._sa_chat.delete("1.0", "end")
        self._sa_chat.config(state="disabled")
        threading.Thread(target=self._sa_thread, args=(ticker,), daemon=True).start()


    def _sa_thread(self, ticker):
        try:
            from squeeze_analyzers import (run_gill_analysis, run_chamath_analysis,
                                            format_gill_display, format_chamath_display,
                                            fetch_squeeze_metrics)
            use_gill    = self._sa_use_gill.get()
            use_chamath = self._sa_use_chamath.get()
            self._sa_w(f"\n🔬  SQUEEZE ANALYSIS — {ticker}\n", "header")
            self._sa_rule()
            self.root.after(0, lambda: self._sa_status_lbl.config(text="Fetching data..."))
            metrics = fetch_squeeze_metrics(ticker)
            if metrics.fetch_errors:
                self._sa_w(f"  ⚠️  {'; '.join(metrics.fetch_errors[:2])}\n", "yellow")

            # ── LIVENESS ──
            # The bulk searcher gates on this; the single-ticker path did not,
            # so a deep dive on a name with no tape ran the full analysis and
            # printed scores computed from nothing. Warn loudly and continue
            # rather than refuse: the user asked for THIS ticker specifically,
            # and the honest response is to say the numbers are not real, not
            # to silently show a blank pane.
            if not getattr(metrics, "alive", True):
                self._sa_w(
                    f"  ⛔ NOT TRADEABLE — "
                    f"{'; '.join(getattr(metrics, 'liveness_reasons', []))}\n"
                    f"     Every metric below is arithmetic on a dead tape.\n",
                    "red")
            elif getattr(metrics, "zombie", False):
                self._sa_w(
                    "  ⚠️  ZOMBIE — days-to-cover is pinned at the 60 cap, "
                    "which is a data error rather than a measurement "
                    "(0 winners in 21 graded episodes).\n", "yellow")
            if getattr(metrics, "price_source", "") == "history_info_stale":
                self._sa_w(
                    f"  ⚠️  quote was stale by "
                    f"{getattr(metrics, 'price_stale_ratio', 0):.1f}x — "
                    f"corrected from the tape to "
                    f"${metrics.current_price:g}\n", "yellow")
            self._sa_rule("📊 Short Interest Data")
            self._sa_w(f"  Company:  {metrics.company_name}\n", "blue")
            self._sa_w(f"  Sector:   {metrics.sector}\n", "dim")
            if metrics.current_price: self._sa_w(f"  Price:    ${metrics.current_price:.2f}\n", "dim")
            if metrics.market_cap:    self._sa_w(f"  Mkt Cap:  ${metrics.market_cap/1e9:.2f}B\n", "dim")
            self._sa_w("\n")
            si_note = f" [{metrics.si_data_quality}]" if metrics.si_data_quality else ""
            self._sa_w(f"  {'Metric':<28} {'Value':<16} Threshold\n", "header")
            self._sa_w(f"  {'─'*60}\n", "dim")
            def mrow(lbl, val, ctx, thresh=None, raw=None):
                tag = "green" if (thresh and raw and raw >= thresh) else ("yellow" if thresh else "dim")
                self._sa_w(f"  {lbl:<28} {val:<16} {ctx}\n", tag)
            mrow("Short Interest % Float", f"{metrics.short_interest_pct:.1%}{si_note}" if metrics.short_interest_pct else "N/A", "> 20% (Gill)", 0.20, metrics.short_interest_pct)
            mrow("Days to Cover (DTC)", f"{metrics.days_to_cover:.1f}d" if metrics.days_to_cover else "N/A", "> 5 days", 5.0, metrics.days_to_cover)
            mrow("CTB Proxy", f"{metrics.ctb_proxy:.1f}%" if metrics.ctb_proxy else "N/A", "> 10%", 10.0, metrics.ctb_proxy)
            mrow("Shares Short", f"{metrics.shares_short:,}" if metrics.shares_short else "N/A", "")
            mrow("Float Shares", f"{metrics.float_shares:,}" if metrics.float_shares else "N/A", "")
            mrow("Short Change", f"{metrics.short_change_pct:+.1%}" if metrics.short_change_pct is not None else "N/A", "+ = adding")
            mrow("FTD % Float", f"{metrics.ftd_pct_float:.3%}" if metrics.ftd_pct_float else "N/A", "SEC data")
            mrow("Volume Surge", f"{metrics.volume_surge:.2f}x" if metrics.volume_surge else "N/A", "vs 20d avg")
            mrow("RSI (14)", f"{metrics.rsi_14:.0f}" if metrics.rsi_14 else "N/A", "< 70 preferred")
            mrow("1-Month Return", f"{metrics.price_change_1m:+.1%}" if metrics.price_change_1m is not None else "N/A", "")
            mrow("3-Month Return", f"{metrics.price_change_3m:+.1%}" if metrics.price_change_3m is not None else "N/A", "")
            self._sa_w("\n")
            if self._sa_stop: self._sa_w("  ⏹ Stopped.\n", "dim"); return
            gill_score = chamath_score = 0.0
            gill_result = chamath_result = None
            if use_gill:
                self.root.after(0, lambda: self._sa_status_lbl.config(text="Running Gill..."))
                self._sa_rule("🎮 Keith Gill — DeepFuckingValue")
                gill_result = run_gill_analysis(ticker)
                gill_score  = gill_result.total_score
                self._sa_w(format_gill_display(gill_result), "dim")
            if not self._sa_stop and use_chamath:
                self.root.after(0, lambda: self._sa_status_lbl.config(text="Running Chamath..."))
                self._sa_rule("💰 Chamath Palihapitiya")
                chamath_result = run_chamath_analysis(ticker)
                chamath_score  = chamath_result.total_score
                self._sa_w(format_chamath_display(chamath_result), "dim")
            active   = sum([use_gill, use_chamath])
            combined = (gill_score + chamath_score) / max(active, 1)
            self._sa_rule("📋 Combined Verdict")
            self._sa_w(f"  {'Agent':<22} {'Score':>8}  Verdict\n", "blue")
            self._sa_w(f"  {'─'*55}\n", "dim")
            if use_gill and gill_result:
                t = "green" if gill_score >= 55 else ("yellow" if gill_score >= 38 else "dim")
                self._sa_w(f"  {'Keith Gill':<22} {gill_score:>7.0f}  {gill_result.verdict} ({gill_result.conviction})\n", t)
            if use_chamath and chamath_result:
                t = "green" if chamath_score >= 70 else ("yellow" if chamath_score >= 50 else "dim")
                self._sa_w(f"  {'Chamath':<22} {chamath_score:>7.0f}  {chamath_result.verdict}\n", t)
            self._sa_w(f"  {'─'*55}\n", "dim")
            comb_tag = "score_strong" if combined >= 65 else ("score_watch" if combined >= 45 else "score_pass")
            self._sa_w(f"  {'COMBINED':<22} {combined:>7.0f}\n", comb_tag)
            overall = ("SQUEEZE CANDIDATE 🔥" if combined >= 65 else
                       ("WATCH — Setup Building ⚠️" if combined >= 45 else "PASS — No Squeeze Setup"))
            self._sa_w(f"\n  {overall}\n\n", comb_tag)

            # ── STAGE 2: the SAME deep engine the searcher runs ──
            # conviction matrix, catalyst timing (incl. FTD T+35), implied
            # move, dealer gamma, FINRA nowcast — identical output, so the
            # analyzer and searcher can never disagree about a stock again.
            deep = None   # defined even if the deep layer fails below
            if not self._sa_stop:
                self.root.after(0, lambda: self._sa_status_lbl.config(
                    text="Deep analysis (conviction × catalyst × structure)..."))
                self._sa_rule("🔬 Deep Analysis — searcher-grade Stage 2")
                try:
                    from squeeze_deep import run_deep_analysis, format_deep_display
                    deep = run_deep_analysis(ticker, stage1_score=combined,
                                              metrics=metrics, with_gamma_flip=True)
                    self._sa_w(format_deep_display(deep), "dim")
                    _f = deep.final_score
                    ftag = ("score_strong" if _f >= 80 else
                            ("score_watch" if _f >= 50 else "score_pass"))
                    self._sa_w(
                        f"\n  FINAL: {_f:.0f}  =  {combined:.0f} setup × "
                        f"{deep.conviction_mult:.2f} velocity × "
                        f"{deep.catalyst_mult:.2f} timing\n", ftag)
                    _ct = f" ({deep.catalyst_type})" if deep.catalyst_type else ""
                    self._sa_w(f"  Verdict: {deep.deep_verdict}   "
                               f"Window: {deep.catalyst_window or '—'}{_ct}\n\n",
                               ftag)
                    if deep.warnings:
                        self._sa_w("  ⚠️ Data warnings:\n", "yellow")
                        for wmsg in deep.warnings[:6]:
                            self._sa_w(f"     • {wmsg}\n", "dim")

                    # ── LOG THIS RUN into the SAME learning pipeline the ──
                    # bulk searcher feeds. Every analyzer run gets logged
                    # (per your call — you're policing signal-to-noise on
                    # what you choose to analyze), tagged source=
                    # "single_analysis" so the grader can eventually compare
                    # the two funnels. review_outcomes.py grades it exactly
                    # like a bulk-scan row once its window matures.
                    try:
                        from squeeze_logger import log_scan
                        # metrics is an OBJECT (fetch_squeeze_metrics), not
                        # a dict; gill_score/chamath_score are the locals
                        # computed above. Attribute access is guarded so a
                        # partial metrics object can't kill the log call.
                        _cand = {
                            "ticker": ticker,
                            "company": getattr(metrics, "company_name", "") or "",
                            "sector": getattr(metrics, "sector", "") or "",
                            "combined": combined,
                            "gill": gill_score,
                            "chamath": chamath_score,
                            "deep_score": deep.deep_score,
                            "probability": deep.probability_score,
                            "imminence": deep.imminence_score,
                            "magnitude": deep.magnitude_score,
                            "conviction_mult": deep.conviction_mult,
                            "conviction_state": deep.conviction_state,
                            # composite_pct is a CROSS-SECTIONAL percentile
                            # in bulk rows — a single run has no cross-
                            # section, so blank is the honest value
                            "composite_pct": "",
                            "catalyst_window": deep.catalyst_window,
                            "catalyst_score": deep.catalyst_score,
                            "catalyst_mult": deep.catalyst_mult,
                            "days_to_earnings": deep.days_to_earnings,
                            "deep_verdict": deep.deep_verdict,
                            "final_score": deep.final_score,
                            "si": deep.si_now,
                            "dtc": deep.dtc_now,
                            "ctb": deep.ctb_now,
                            "catalyst_type": deep.catalyst_type,
                            "implied_move_pct": deep.implied_move_pct,
                            "gex_net_musd": deep.gex_net_musd,
                            "gex_regime": deep.gex_regime,
                            "svr_recent": deep.svr_recent,
                            "svr_trend": deep.svr_trend,
                            "ftd_closeout_date": deep.ftd_closeout_date,
                            "ftd_impact_factor": deep.ftd_impact_factor,
                            "effective_float": deep.effective_float,
                            "float_tightness": deep.float_tightness,
                            "ftd_pct_float": deep.ftd_pct_float_accum,
                            "ftd_pct_eff_float": deep.ftd_pct_eff_float_accum,
                            "ftd_closeout_adv_days": deep.ftd_closeout_adv_days,
                            "inst_shares_over_float": getattr(
                                metrics, "institutional_shares_over_float", None),
                            "scoring_version": deep.scoring_version,
                            "deep_score_v1": deep.deep_score_v1,
                            "probability_v1": deep.probability_score_v1,
                            "magnitude_v1": deep.magnitude_score_v1,
                            "ftd_score_v1": deep.ftd_score_v1,
                            "ftd_impact_factor_v1": deep.ftd_impact_factor_v1,
                            "borrow_utilization": deep.borrow_utilization,
                            "shares_available": deep.shares_available,
                            "borrow_rate_real": deep.borrow_rate_real,
                            "borrow_mult": deep.borrow_mult,
                            "borrow_state": deep.borrow_state,
                            "conviction_mult_raw": deep.conviction_mult_raw,
                            "convexity_score": deep.convexity_score,
                            "ctb_velocity_score": deep.ctb_velocity_score,
                            "ftd_score": deep.ftd_score,
                            "svr_score": (deep.svr_score if deep.svr_available else None),
                            "momentum_score": (deep.momentum_score
                                               if deep.momentum_available else None),
                            "ret_5d": deep.ret_5d,
                            "ret_20d": deep.ret_20d,
                            "rel_volume": deep.rel_volume,
                            "float_shares": deep.float_shares,
                            "ctb_trend": deep.ctb_trend,
                            "dtc_trend": deep.dtc_trend,
                            "si_trend": deep.si_trend,
                            "calibrated_prob": deep.calibrated_prob,
                            "ftd_mult": deep.ftd_mult,
                            "reg_sho_days": deep.reg_sho_days,
                            "reg_sho_mult": deep.reg_sho_mult,
                            "exhaustion_factor": deep.exhaustion_factor,
                            "momentum_raw": deep.momentum_score_raw,
                            "cash_runway_months": deep.cash_runway_months,
                            "final_score_v1": deep.final_score_v1,
                            "dtc_exchange": deep.dtc_exchange,
                            "dtc_robust": deep.dtc_robust,
                            "dtc_60d": deep.dtc_60d,
                            "dtc_spike_ratio": deep.dtc_spike_ratio,
                            "si_change_settlement": deep.si_change_settlement,
                            "si_trend_source": deep.si_trend_source,
                            "si_trend_v1": deep.si_trend_v1,
                            "dtc_trend_v1": deep.dtc_trend_v1,
                            "settlement_date": deep.settlement_date,
                            "settlement_age_days": deep.settlement_age_days,
                            "price": deep.current_price,
                            "mktcap": getattr(metrics, "market_cap", 0) or 0,
                        }
                        _sid, _logok = log_scan([_cand], tier=0, top_n=1,
                                                source="single_analysis")
                        if _logok:
                            self._sa_w(f"  📝 logged to squeeze_log.csv "
                                       f"(source=single_analysis, id {_sid})\n\n",
                                       "dim")
                        else:
                            self._sa_w(f"  ⚠️ logging skipped: {_sid}\n\n",
                                       "yellow")
                    except Exception as le:
                        self._sa_w(f"  ⚠️ logging unavailable: {le}\n\n",
                                   "yellow")
                except Exception as de:
                    self._sa_w(f"  ⚠️ Deep layer unavailable: {de}\n", "yellow")

            # ── Options expression layer (decision support) ──
            # Consumes the deep result (implied move, close-out date,
            # calibrated_prob socket); NEVER feeds the squeeze score.
            if not self._sa_stop:
                self.root.after(0, lambda: self._sa_status_lbl.config(
                    text="Options strike matrix..."))
                self._sa_rule("🎯 Options Expression — strike matrix (decision support)")
                try:
                    from options_ev import run_strike_matrix_data
                    _scn = (self._sa_scn_var.get() or "").strip() or "auto"
                    _prov = None
                    if _scn.lower() == "auto":
                        # ZERO-INPUT MODE: the scenario engine derives the
                        # distribution from the analyzer's own outputs and
                        # logs it for self-grading. Typing scenarios in
                        # the box still overrides.
                        from scenario_engine import auto_scenarios, log_generated
                        _cd = getattr(deep, "ftd_closeout_days", None) if deep else None
                        _gen = auto_scenarios(
                            getattr(deep, "current_price", 0) if deep else 0,
                            implied_move_pct=getattr(deep, "implied_move_pct", None) if deep else None,
                            final_score=getattr(deep, "final_score", None) if deep else None,
                            catalyst_days=_cd if (_cd and _cd > 0) else None,
                            calibrated_prob=getattr(deep, "calibrated_prob", None) if deep else None)
                        _scn = _gen["text"]
                        _prov = (f"AUTO scenarios (tier {_gen['tier']}): "
                                 f"{_gen['provenance']}")
                        try:
                            log_generated(ticker,
                                          getattr(deep, "current_price", 0) if deep else 0,
                                          _gen,
                                          implied_move_pct=getattr(deep, "implied_move_pct", None) if deep else None,
                                          final_score=getattr(deep, "final_score", None) if deep else None)
                        except Exception:
                            pass
                    try:
                        _dte = int(float(self._sa_dte_var.get()))
                        if _dte <= 0:
                            _dte = None
                    except (ValueError, TypeError):
                        _dte = None      # blank/junk = no expiry cap
                    if _prov:
                        self._sa_w(f"  🤖 {_prov}\n\n", "opt_hdr")
                    _opt = run_strike_matrix_data(ticker, _scn, deep=deep,
                                                  max_dte=_dte)
                    self._sa_opt_data = _opt
                    # color tags (idempotent to re-configure)
                    def _cfg_tags():
                        c = self._sa_chat
                        c.tag_configure("opt_good", foreground=GREEN)
                        c.tag_configure("opt_mid",  foreground=YELLOW)
                        c.tag_configure("opt_bad",  foreground=RED)
                        c.tag_configure("opt_warn", foreground=ACCENT)
                        c.tag_configure("opt_hdr",  foreground=TEAL)
                    self.root.after(0, _cfg_tags)
                    _tagmap = {"good": "opt_good", "mid": "opt_mid",
                               "bad": "opt_bad", "warn": "opt_warn",
                               "hdr": "opt_hdr", "note": "dim", "dim": "dim"}
                    for _line, _tag in _opt["lines"]:
                        self._sa_w(_line + "\n", _tagmap.get(_tag, "dim"))
                    if _opt["blocks"]:
                        self.root.after(0, lambda: self._sa_heat_btn.config(
                            state="normal", bg="#1f6feb", fg="#FFFFFF"))
                        self.root.after(0, lambda: self._sa_pl_btn.config(
                            state="normal", bg="#2da44e", fg="#FFFFFF"))
                        self.root.after(0, lambda: self._sa_thesis_btn.config(
                            state="normal", bg="#8957e5", fg="#FFFFFF"))
                except Exception as oe:
                    # Name the ticker and the failure type. "Options layer
                    # unavailable" alone gave no way to tell a dead ticker
                    # from a rate limit from a bug — and left the reader
                    # assuming the numbers on screen belonged to this stock.
                    self._sa_w(f"  ⚠️ Options layer failed for {ticker}: "
                               f"{type(oe).__name__}: {oe}\n", "yellow")
                    self._sa_w(f"     The strike matrix, heatmap and Contract "
                               f"P/L are DISABLED for {ticker} — they will "
                               f"not show stale data from a previous ticker. "
                               f"Chain fetches fail transiently under rate "
                               f"limiting; re-run in a minute.\n", "dim")

            self._sa_rule()
            self._sa_results = {"ticker": ticker, "metrics": metrics,
                                 "gill": gill_result, "chamath": chamath_result,
                                 "combined": combined, "overall": overall,
                                 "deep": deep}
            def _update_sb():
                cc = GREEN if combined >= 65 else (YELLOW if combined >= 45 else RED)
                self._sa_lbl_ticker.config(text=ticker)
                self._sa_lbl_combined.config(text=f"{combined:.0f}", fg=cc)
                self._sa_lbl_verdict.config(text=overall, fg=cc)
                if use_gill:
                    gc = GREEN if gill_score >= 55 else (YELLOW if gill_score >= 38 else RED)
                    self._sa_lbl_gill.config(text=f"Gill:    {gill_score:.0f}/100", fg=gc)
                if use_chamath:
                    cc2 = GREEN if chamath_score >= 70 else (YELLOW if chamath_score >= 50 else RED)
                    self._sa_lbl_chamath.config(text=f"Chamath: {chamath_score:.0f}/100", fg=cc2)
                for w in self._sa_metrics_frame.winfo_children(): w.destroy()
                for lbl, val, col in [
                    ("SI% Float", f"{metrics.short_interest_pct:.1%}" if metrics.short_interest_pct else "N/A", GREEN if (metrics.short_interest_pct or 0) >= 0.20 else YELLOW),
                    ("DTC",       f"{metrics.days_to_cover:.1f}d" if metrics.days_to_cover else "N/A", GREEN if (metrics.days_to_cover or 0) >= 5 else YELLOW),
                    ("CTB",       f"{metrics.ctb_proxy:.0f}%" if metrics.ctb_proxy else "N/A", GREEN if (metrics.ctb_proxy or 0) >= 10 else YELLOW),
                    ("FTD",       f"{metrics.ftd_pct_float:.3%}" if metrics.ftd_pct_float else "None", FG_DIM),
                    ("RSI",       f"{metrics.rsi_14:.0f}" if metrics.rsi_14 else "N/A", FG_DIM),
                    ("Vol Surge", f"{metrics.volume_surge:.1f}x" if metrics.volume_surge else "N/A", GREEN if (metrics.volume_surge or 0) >= 2.0 else FG_DIM),
                ]:
                    rw = tk.Frame(self._sa_metrics_frame, bg=BG2)
                    rw.pack(fill="x", pady=1)
                    tk.Label(rw, text=lbl, font=FONT_SM, bg=BG2, fg=FG_DIM, width=10, anchor="w").pack(side="left")
                    tk.Label(rw, text=val, font=FONT_SM, bg=BG2, fg=col, anchor="e").pack(side="right")
                self._sa_export_btn.config(state="normal", bg="#1f6feb", fg="#FFFFFF")
                self._sa_qa_entry.config(state="normal", fg=FG)
                self._sa_qa_btn.config(state="normal", bg="#238636", fg="#FFFFFF")
            self.root.after(0, _update_sb)
            self._sa_w("  💬 Ask Claude about this analysis below\n", "dim")
            self.root.after(0, lambda: self._sa_status_lbl.config(text=f"Done — {combined:.0f}  {overall}"))
        except Exception as e:
            import traceback
            self._sa_w(f"\n❌ Error: {e}\n", "red")
            self._sa_w(traceback.format_exc() + "\n", "dim")
            self.root.after(0, lambda: self._sa_status_lbl.config(text="Error"))
        finally:
            self._sa_running = False
            self._sa_stop    = False
            self.root.after(0, lambda: [
                self._sa_run_btn.config(state="normal", text="▶  Analyze", bg=ACCENT, fg="#000000"),
                self._sa_ticker_entry.config(state="normal"),
                self._sa_ticker_var.set(""),
                self._sa_ticker_entry.focus(),
            ])


    def _sa_ask_claude(self):
        if self._sa_running or not self._sa_results: return
        question = self._sa_qa_var.get().strip()
        if not question: self._sa_qa_entry.focus(); return
        self._sa_running = True
        self._sa_qa_btn.config(state="disabled", text="⏳")
        self._sa_qa_entry.config(state="disabled")
        self._sa_run_btn.config(state="disabled")
        threading.Thread(target=self._sa_claude_thread, args=(question,), daemon=True).start()


    def _sa_claude_thread(self, question):
        try:
            r = self._sa_results
            m = r["metrics"]
            lines = [f"SQUEEZE ANALYSIS — {r['ticker']}", f"Combined: {r['combined']:.0f}/100 — {r['overall']}",
                     f"SI: {m.short_interest_pct:.1%}" if m.short_interest_pct else "SI: N/A",
                     f"DTC: {m.days_to_cover:.1f}d" if m.days_to_cover else "DTC: N/A"]
            if r.get("gill"):
                lines += [f"GILL: {r['gill'].total_score:.0f}/100 — {r['gill'].verdict}",
                          "Green: " + "; ".join(r['gill'].green_flags[:3])]
            if r.get("chamath"):
                lines.append(f"CHAMATH: {r['chamath'].total_score:.0f}/100 — {r['chamath'].verdict}")
            context = "\n".join(l for l in lines if l)
            self._sa_rule("Claude Q&A")
            self._sa_w(f"  Q: {question}\n\n", "blue")
            self._sa_w("  ⏳ Thinking...\n", "dim")
            # Fetch answer in background (we already are in background thread)
            answer = ask_lm_studio(question, context, self.portfolio_ctx)
            # Replace thinking placeholder and write answer — all via root.after()
            def _show_answer(ans=answer):
                self._sa_chat.config(state="normal")
                pos = self._sa_chat.search("  ⏳ Thinking...", "1.0", "end")
                if pos:
                    self._sa_chat.delete(pos, f"{pos} lineend+1c")
                self._sa_chat.insert("end", f"  {ans}\n\n", "claude")
                self._sa_chat.see("end")
                self._sa_chat.config(state="disabled")
            self.root.after(0, _show_answer)
        except Exception as e:
            self._sa_w(f"  ❌ Error: {e}\n", "red")
        finally:
            self._sa_running = False
            self.root.after(0, lambda: [
                self._sa_qa_btn.config(state="normal", text="💬 Ask", bg="#238636", fg="#FFFFFF"),
                self._sa_qa_entry.config(state="normal", fg=FG),
                self._sa_run_btn.config(state="normal"),
                self._sa_qa_var.set(""),
                self._sa_qa_entry.focus(),
            ])


if __name__ == "__main__":
    root = tk.Tk()
    app = SqueezeAnalyzerApp(root)
    root.mainloop()
