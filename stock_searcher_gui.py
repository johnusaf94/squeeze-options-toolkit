"""
stock_searcher_gui.py
======================
Quality-investing universe scanner. Runs the stock-analysis frameworks
(Buffett, Weiss, Bogle, Dalio, Druckenmiller) across a tiered universe
of quality candidates, then surfaces the top 5 per framework + the top
5 composite.

Distinct from squeeze_searcher_gui.py — that one targets high-SI squeeze
candidates. This one targets durable-moat compounders and quality value.

Requires in same folder:
  shared_utils.py, ticker_resolver.py, composite_score.py,
  buffett_analyzer.py, weiss_analyzer.py, bogle_analyzer.py,
  dalio_analyzer.py, druckenmiller_analyzer.py, stock_universe.py,
  yfinance_throttle.py
"""

# ── GLOBAL yfinance RATE LIMITER ────────────────────────────────
# Must be imported BEFORE anything that uses yfinance. Monkey-
# patches yfinance.Ticker with token-bucket rate limiting + caching.
import yfinance_throttle  # noqa: F401

import tkinter as tk
from tkinter import scrolledtext, filedialog
import threading
import csv as _csv
from datetime import datetime as _dt

from shared_utils import *


# ─────────────────────────────────────────────
# MAIN APP
# ─────────────────────────────────────────────

class StockSearcherApp:
    def __init__(self, root):
        self.root = root
        self.root.title("📊 Stock Searcher — Quality Investment Scanner")
        self.root.geometry("1280x860")
        self.root.configure(bg=BG)

        self._running = False
        self._stop = False
        self._results = []          # list of per-ticker dicts
        self.portfolio_ctx = ""

        # Top bar with model selector + LM Studio status
        top = tk.Frame(root, bg=BG2, pady=8)
        top.pack(fill="x")
        tk.Label(top, text="📊 STOCK SEARCHER", font=FONT_HD,
                 bg=BG2, fg=BLUE).pack(side="left", padx=16)
        tk.Label(top, text="Scan universe for quality investments",
                 font=FONT_SM, bg=BG2, fg=FG_DIM).pack(side="left", padx=8)

        self.conn_lbl = tk.Label(top, text="⏳ checking...",
                                  font=FONT_SM, bg=BG2, fg=FG_DIM)
        self.conn_lbl.pack(side="right", padx=4)
        tk.Frame(top, bg=BORDER, width=1).pack(side="right", fill="y",
                                                padx=6, pady=4)

        # Backend model selector
        self._model_options = {"🖥  Local (LM Studio)": ("local", "local-model")}
        for n, mid in GROQ_MODELS.items():
            self._model_options[f"⚡ Groq — {n}"] = ("groq", mid)
        for n, mid in TOGETHER_MODELS.items():
            self._model_options[f"☁  Together — {n}"] = ("together", mid)
        self._backend_var = tk.StringVar(value="🖥  Local (LM Studio)")
        menu = tk.OptionMenu(top, self._backend_var, *self._model_options.keys(),
                              command=self._on_backend_change)
        menu.config(font=FONT_SM, bg=BG3, fg=FG, relief="flat",
                    highlightthickness=0, bd=0)
        menu["menu"].config(bg=BG3, fg=FG, font=FONT_SM)
        menu.pack(side="right", padx=2)
        tk.Label(top, text="Model:", font=FONT_SM,
                 bg=BG2, fg=FG_DIM).pack(side="right", padx=(8, 2))

        # Main content
        self.tab = tk.Frame(root, bg=BG)
        self.tab.pack(fill="both", expand=True)
        self._build_main_tab()

        # Ask Claude bar at bottom
        tk.Frame(root, bg=BORDER, height=1).pack(fill="x")
        qa = tk.Frame(root, bg=BG2, pady=6)
        qa.pack(fill="x")
        tk.Label(qa, text="Ask Claude:", font=FONT,
                 bg=BG2, fg=FG_DIM).pack(side="left", padx=(12, 4))
        self._qa_var = tk.StringVar()
        self._qa_entry = tk.Entry(qa, textvariable=self._qa_var, font=FONT,
                                   bg=BG3, fg=FG_DIM, insertbackground=FG,
                                   relief="flat", bd=6, state="disabled")
        self._qa_entry.pack(side="left", fill="x", expand=True, padx=6)
        self._qa_entry.bind("<Return>", lambda e: self._ask_claude())
        self._qa_btn = tk.Button(qa, text="💬 Ask", font=("Consolas", 10, "bold"),
                                  bg=BG3, fg=FG_DIM, relief="flat",
                                  cursor="hand2", padx=10, pady=4,
                                  state="disabled", command=self._ask_claude)
        self._qa_btn.pack(side="left", padx=(4, 12))

        threading.Thread(target=self._check_backend, daemon=True).start()

    # ── BACKEND ──────────────────────────────────
    def _on_backend_change(self, selection):
        import shared_utils as su
        backend, model = self._model_options.get(selection,
                                                  ("local", "local-model"))
        su._ACTIVE_BACKEND = backend
        su._ACTIVE_ONLINE_MODEL = model
        threading.Thread(target=self._check_backend, daemon=True).start()

    def _check_backend(self):
        ok, msg = check_backend_status()
        self.conn_lbl.config(text=msg, fg=GREEN if ok else YELLOW)

    # ── MAIN TAB UI ─────────────────────────────
    def _build_main_tab(self):
        parent = self.tab

        # LEFT sidebar — fixed panel
        ctrl = tk.Frame(parent, bg=BG2, width=295)
        ctrl.pack(side="left", fill="y")
        ctrl.pack_propagate(False)

        tk.Label(ctrl, text="STOCK SEARCHER", font=FONT_LG,
                 bg=BG2, fg=BLUE).pack(pady=(14, 2), padx=14, anchor="w")
        tk.Label(ctrl, text="Quality-investing universe scan",
                 font=FONT_SM, bg=BG2, fg=FG_DIM).pack(padx=14, anchor="w")

        tk.Frame(ctrl, bg=BORDER, height=1).pack(fill="x", padx=10, pady=8)

        # ── UNIVERSE TIER SELECTOR ──
        tk.Label(ctrl, text="SEARCH UNIVERSE", font=FONT_SM,
                 bg=BG2, fg=FG_DIM).pack(padx=14, anchor="w", pady=(0, 4))

        # Get actual sizes
        try:
            from stock_universe import get_universe_size
            sizes = get_universe_size()
        except Exception:
            sizes = {1: 125, 2: 230, 3: 480, 4: 650}

        TIER_INFO = [
            (f"T1: Compounders        (~{sizes[1]})",
             "Durable-moat names with proven ROIC"),
            (f"T2: + S&P 100 large caps  (~{sizes[2]})",
             "Large caps with multi-year track record"),
            (f"T3: + S&P 500 broader    (~{sizes[3]})",
             "Full S&P 500 quality coverage"),
            (f"T4: + dividend & growth  (~{sizes[4]})",
             "Aristocrats, ADRs, growth franchises"),
        ]
        self._tier_var = tk.IntVar(value=1)
        self._tier_lbl = tk.Label(ctrl, text=TIER_INFO[0][1],
                                   font=("Consolas", 7), bg=BG2, fg=YELLOW,
                                   wraplength=255, anchor="w")
        self._tier_lbl.pack(padx=14, anchor="w", pady=(0, 4))

        def _update_tier(*_):
            self._tier_lbl.config(text=TIER_INFO[self._tier_var.get() - 1][1])

        tier_f = tk.Frame(ctrl, bg=BG2)
        tier_f.pack(fill="x", padx=10, pady=(0, 4))
        for i, (label, _) in enumerate(TIER_INFO, 1):
            tk.Radiobutton(tier_f, text=label, variable=self._tier_var,
                           value=i, font=("Consolas", 8), bg=BG2, fg=FG,
                           selectcolor=BG3, activebackground=BG2,
                           relief="flat",
                           command=_update_tier).pack(anchor="w", pady=1)

        tk.Frame(ctrl, bg=BORDER, height=1).pack(fill="x", padx=10, pady=6)

        # ── PARAMETERS ──
        def labeled(label, default):
            f = tk.Frame(ctrl, bg=BG2)
            f.pack(fill="x", padx=14, pady=2)
            tk.Label(f, text=label, font=FONT_SM, bg=BG2, fg=FG_DIM,
                     width=18, anchor="w").pack(side="left")
            var = tk.StringVar(value=str(default))
            tk.Entry(f, textvariable=var, font=FONT_SM, bg=BG3, fg=FG,
                     insertbackground=FG, relief="flat",
                     bd=4, width=8).pack(side="left")
            return var

        self._max_stocks = labeled("Limit (0=all)", "0")
        self._min_score = labeled("Min composite", "50")
        self._top_n = labeled("Show top per group", "5")

        tk.Frame(ctrl, bg=BORDER, height=1).pack(fill="x", padx=10, pady=6)

        # ── RUN/STOP BUTTON ──
        self._run_btn = tk.Button(ctrl, text="📊  Start Stock Search",
                                   font=("Consolas", 11, "bold"),
                                   bg=BLUE, fg="#000000",
                                   relief="flat", cursor="hand2",
                                   padx=14, pady=6, command=self._toggle)
        self._run_btn.pack(fill="x", padx=10, pady=4)

        self._export_btn = tk.Button(ctrl, text="📄  Export CSV for Analysis",
                                      font=("Consolas", 10, "bold"),
                                      bg=BG3, fg=FG_DIM, relief="flat",
                                      cursor="hand2", padx=14, pady=5,
                                      state="disabled",
                                      command=self._export_csv)
        self._export_btn.pack(fill="x", padx=10, pady=(0, 4))

        self._status = tk.Label(ctrl, text="Ready — select tier & scan",
                                 font=FONT_SM, bg=BG2, fg=FG_DIM,
                                 wraplength=250)
        self._status.pack(padx=14, pady=4, anchor="w")

        prog_frame = tk.Frame(ctrl, bg=BG2)
        prog_frame.pack(fill="x", padx=10, pady=4)
        self._prog_lbl = tk.Label(prog_frame, text="",
                                   font=("Consolas", 9), bg=BG2, fg=BLUE)
        self._prog_lbl.pack(side="left", padx=4)

        # Notice — yfinance throttle status
        tk.Frame(ctrl, bg=BORDER, height=1).pack(fill="x", padx=10, pady=8)
        tk.Label(ctrl, text="⚙ INFO", font=FONT_SM,
                 bg=BG2, fg=FG_DIM).pack(padx=14, anchor="w")
        tk.Label(ctrl,
                 text="Each ticker = ~10 yfinance calls.\n"
                      "Cache: 24h on stable data.\n"
                      "Rate: 1.5 calls/sec (global).\n"
                      "First T2 scan: ~25-30 min.\n"
                      "Repeat scans: much faster.",
                 font=("Consolas", 7), bg=BG2, fg=FG_DIM,
                 justify="left", wraplength=255).pack(padx=14, anchor="w",
                                                       pady=(2, 0))

        # RIGHT — log output
        right = tk.Frame(parent, bg=BG)
        right.pack(side="right", fill="both", expand=True)

        self._log = scrolledtext.ScrolledText(
            right, font=("Consolas", 10), bg=BG, fg=FG,
            insertbackground=FG, relief="flat", padx=10, pady=10,
            wrap="word",
        )
        self._log.pack(fill="both", expand=True)

        TAGS = {
            "header": {"foreground": BLUE, "font": ("Consolas", 12, "bold")},
            "strong": {"foreground": GREEN, "font": ("Consolas", 10, "bold")},
            "watch":  {"foreground": YELLOW},
            "red":    {"foreground": RED},
            "dim":    {"foreground": FG_DIM},
            "blue":   {"foreground": BLUE},
            "green":  {"foreground": GREEN},
            "framework": {"foreground": "#F4C430",
                          "font": ("Consolas", 11, "bold")},
        }
        for tag, cfg in TAGS.items():
            self._log.tag_config(tag, **cfg)
        self._log.config(state="disabled")

    # ── LOGGING ─────────────────────────────────
    def _write(self, text, tag=None):
        self._log.config(state="normal")
        if tag:
            self._log.insert("end", text, tag)
        else:
            self._log.insert("end", text)
        self._log.see("end")
        self._log.config(state="disabled")

    # ── RUN CONTROL ─────────────────────────────
    def _toggle(self):
        if self._running:
            self._stop = True
            self._status.config(text="Stopping...")
            self._run_btn.config(text="⏳ Stopping")
            return
        self._start()

    def _start(self):
        try:
            max_stocks = int(self._max_stocks.get())
            min_score = float(self._min_score.get())
            top_n = int(self._top_n.get())
        except ValueError:
            self._write("❌ Invalid parameter values.\n", "red")
            return

        tier = self._tier_var.get()
        self._running = True
        self._stop = False
        self._results = []
        self._run_btn.config(bg=RED, fg="#000000", text="⏹ Stop")

        self._log.config(state="normal")
        self._log.delete("1.0", "end")
        self._log.config(state="disabled")

        threading.Thread(
            target=self._thread,
            args=(tier, max_stocks, min_score, top_n),
            daemon=True,
        ).start()

    # ── MAIN SCAN LOOP ──────────────────────────
    def _thread(self, tier, max_stocks, min_score, top_n):
        try:
            self._write("\n")
            self._write("  📊 STOCK SEARCHER — quality-investing scan\n", "header")

            # Load universe
            self._status.config(text="Loading universe...")
            try:
                from stock_universe import get_universe
                universe = get_universe(
                    tier_max=tier,
                    limit=max_stocks if max_stocks > 0 else None
                )
                tier_names = {1: "Compounders", 2: "+S&P100", 3: "+S&P500",
                              4: "Full Quality"}
                self._write(
                    f"  Tier {tier} ({tier_names.get(tier, '')}) | "
                    f"{len(universe):,} tickers\n"
                    f"  Min composite: {min_score:.0f} | top-{top_n} per framework\n\n",
                    "dim"
                )
            except ImportError as e:
                self._write(f"❌ stock_universe.py not found: {e}\n", "red")
                return

            total = len(universe)
            results = []

            # Lazy import the heavy analyzer pipeline
            from composite_score import build_composite

            t_start = _dt.now()
            for i, ticker in enumerate(universe):
                if self._stop:
                    self._write(f"\n  ⏹ Stopped at {i}/{total}\n", "yellow")
                    break

                # Progress
                if i % 5 == 0 or i == total - 1:
                    elapsed = (_dt.now() - t_start).total_seconds()
                    rate = (i + 1) / max(elapsed, 0.1)
                    eta_s = (total - i - 1) / max(rate, 0.01)
                    eta_m = int(eta_s / 60)
                    self._prog_lbl.config(
                        text=f"{i+1}/{total} | "
                             f"{rate:.1f}/s | ETA ~{eta_m}m"
                    )
                    self._status.config(text=f"Analyzing {ticker} "
                                              f"({i+1}/{total})")

                # Run all analyzers for this ticker
                row = None
                try:
                    analyses = self._analyze_ticker(ticker)
                    if analyses is None:
                        continue

                    live = analyses.get("live")
                    company_name = (getattr(live, "company_name", "") or ticker)

                    # Build composite from the analyses.
                    # NOTE: company_name is REQUIRED — passing it was the bug
                    # that caused every ticker to silently throw and the
                    # whole scan to return zero candidates.
                    composite = build_composite(
                        ticker=ticker,
                        company_name=company_name,
                        buffett_analysis=analyses.get("buffett"),
                        weiss_analysis=analyses.get("weiss"),
                        bogle_analysis=analyses.get("bogle"),
                        dalio_analysis=analyses.get("dalio"),
                        druckenmiller_analysis=analyses.get("druckenmiller"),
                        live_data=live,
                    )

                    # Stream per-ticker result so the user can see progress
                    score = composite.total_score
                    sig = composite.signal or ""
                    # Color tag based on composite tier
                    if score >= 75:
                        tag = "strong"
                    elif score >= 60:
                        tag = "watch"
                    elif score >= 45:
                        tag = "dim"
                    else:
                        tag = "red"
                    threshold_marker = "✓" if score >= min_score else " "
                    self._write(
                        f"  {threshold_marker} {ticker:<6} {company_name[:24]:<24} "
                        f"score={score:>5.1f}  {sig}\n",
                        tag,
                    )

                    if score < min_score:
                        continue

                    # Capture all the per-framework component scores
                    row = self._extract_row(ticker, analyses, composite)
                    results.append(row)
                except Exception as e:
                    # Show errors so the user sees what's failing instead of
                    # the entire scan silently producing zero results
                    self._write(
                        f"  ⚠ {ticker:<6} error: {str(e)[:60]}\n",
                        "red",
                    )
                    continue

            # ── DISPLAY RESULTS ──
            if not results:
                self._write("\n  ❌ No candidates found above min composite "
                            f"({min_score}).\n", "red")
                return

            self._results = results

            # ── AUTO-LOG to stock_log.csv (learning engine dataset) ──
            # Write-only history; outcomes graded later by
            # `python learning_engine.py update`. This is the stock-side
            # half of the self-learning loop.
            try:
                from learning_engine import log_stock_scan
                _sid, _ok = log_stock_scan(results, top_n=40)
                if _ok:
                    self._write(f"\n  📚 Logged top 40 to stock_log.csv "
                                f"(scan {_sid}) for outcome learning\n", "dim")
            except ImportError:
                pass
            except Exception:
                pass

            self._write(f"\n  ✓ Scan complete — {len(results)} candidates "
                        f"above {min_score:.0f}\n", "green")
            self._write(f"  {'─' * 70}\n\n", "dim")

            self._display_rankings(results, top_n)

            self._status.config(text=f"Done — {len(results)} candidates")
            self._prog_lbl.config(text=f"Complete — {len(results)} results")

            # Enable Ask Claude + Export
            self.root.after(0, lambda: [
                self._qa_entry.config(state="normal", fg=FG),
                self._qa_btn.config(state="normal",
                                     bg="#238636", fg="#FFFFFF"),
                self._export_btn.config(state="normal",
                                         bg="#1f6feb", fg="#FFFFFF"),
            ])
        except Exception as e:
            import traceback
            self._write(f"\n❌ Error: {e}\n{traceback.format_exc()}\n", "red")
        finally:
            self._running = False
            self._run_btn.config(bg=BLUE, fg="#000000",
                                  text="📊  Start Stock Search")

    # ── PER-TICKER ANALYSIS ─────────────────────
    def _analyze_ticker(self, ticker: str):
        """Run all 5 analyzers for one ticker. Returns dict of analyses or None."""
        analyses = {}
        try:
            from buffett_analyzer import run_buffett_analysis
            analyses["buffett"] = run_buffett_analysis(ticker)
        except Exception:
            analyses["buffett"] = None
        try:
            from weiss_analyzer import run_weiss_analysis
            analyses["weiss"] = run_weiss_analysis(ticker)
        except Exception:
            analyses["weiss"] = None
        try:
            from bogle_analyzer import run_bogle_analysis
            analyses["bogle"] = run_bogle_analysis(ticker)
        except Exception:
            analyses["bogle"] = None
        try:
            from dalio_analyzer import run_dalio_analysis
            analyses["dalio"] = run_dalio_analysis(ticker)
        except Exception:
            analyses["dalio"] = None
        try:
            from druckenmiller_analyzer import run_druckenmiller_analysis
            analyses["druckenmiller"] = run_druckenmiller_analysis(ticker)
        except Exception:
            analyses["druckenmiller"] = None

        # Lightweight live data — use the proper LiveTickerData fetcher
        # from ticker_resolver. The previous hand-rolled stub class was
        # missing attributes (dividend_rate, peg_ratio, beta, etc.) that
        # composite_score.py expects, causing every ticker to throw.
        try:
            from ticker_resolver import fetch_live_data
            analyses["live"] = fetch_live_data(ticker)
        except Exception:
            analyses["live"] = None

        return analyses

    # ── EXTRACT FRAMEWORK SCORES FROM COMPOSITE ─
    def _extract_row(self, ticker, analyses, composite):
        """Build a single result row capturing every score we care about."""
        row = {
            "ticker": ticker,
            "company": getattr(analyses.get("live"), "company_name", ticker),
            "sector": getattr(analyses.get("live"), "sector", "Unknown"),
            "price": getattr(analyses.get("live"), "current_price", None),
            "composite": composite.total_score,
            "signal": composite.signal,
            "account_fit": getattr(composite, "account_fit", ""),
            "skipped": list(getattr(composite, "skipped", [])),
        }

        # Per-component raw scores (0-1) from composite
        per_component = {}
        for c in getattr(composite, "components", []):
            # ComponentScore typically has .key, .raw_score, .name
            key = getattr(c, "key", None) or getattr(c, "name", "")
            raw = getattr(c, "raw_score", None)
            if key and raw is not None:
                per_component[key] = raw * 100   # scale to 0-100
        row["per_component"] = per_component

        # Also capture some specific high-level scores for ranking by framework
        # Buffett — moat score (4 criteria) and FEY valuation
        b = analyses.get("buffett")
        if b is not None:
            ms = getattr(b, "moat_score", None)
            if ms is not None:
                # Use ADJUSTED score (raw criteria × trend multiplier) so a
                # 4/4 STRONG moat that's rapidly narrowing shows as a much
                # lower numeric than a 4/4 STRONG · widening moat.
                adj = getattr(ms, "adjusted_score", None)
                if adj is None:
                    adj = ms.score  # backward-compat
                row["buffett_moat_raw"] = (adj / 4.0) * 100
                row["buffett_moat_raw_unadjusted"] = (ms.score / 4.0) * 100
                row["buffett_moat_rating"] = getattr(ms, "rating", "")
                row["moat_direction"] = getattr(b.moat, "moat_direction", "") if hasattr(b, "moat") else ""
                row["moat_trend_delta"] = getattr(b.moat, "moat_trend_delta", 0.0) if hasattr(b, "moat") else 0.0

        # Weiss — blue chip rating
        w = analyses.get("weiss")
        if w is not None:
            bc = getattr(w, "blue_chip", None)
            if bc is not None:
                measurable = getattr(bc, "measurable", 7) or 7
                row["weiss_quality_raw"] = (bc.score / max(measurable, 1)) * 100
                row["weiss_rating"] = getattr(bc, "rating", "")

        # Bogle — timing score
        # Bug fix (May 29 2026): `rv.score` doesn't exist on ReversionSignal.
        # The actual numeric score field is `timing_score` (0-10 int).
        bg = analyses.get("bogle")
        if bg is not None:
            rv = getattr(bg, "reversion", None)
            if rv is not None:
                ts = getattr(rv, "timing_score", None)
                if ts is not None:
                    row["bogle_timing_raw"] = float(ts) * 10  # 0-10 → 0-100

        # Dalio — debt cycle and bubble filter
        # Bug fix (May 29 2026): fields are `debt_cycle` and `bubble`, not
        # `debt_analysis`/`bubble_analysis`. Each contains a FilterResult
        # with `passed: bool` (not `score: float`). We map pass=100, fail=0.
        # Also expose the overall multi-filter score via filters_passed.
        dl = analyses.get("dalio")
        if dl is not None:
            db = getattr(dl, "debt_cycle", None)
            bb = getattr(dl, "bubble", None)
            if db is not None and getattr(db, "result", None) is not None:
                row["dalio_debt_raw"] = 100.0 if db.result.passed else 0.0
            if bb is not None and getattr(bb, "result", None) is not None:
                row["dalio_bubble_raw"] = 100.0 if bb.result.passed else 0.0
            # Also surface the overall Dalio pass rate (4 filters)
            fp = getattr(dl, "filters_passed", None)
            ft = getattr(dl, "filters_total", 4)
            if fp is not None and ft:
                row["dalio_overall_raw"] = (fp / ft) * 100
                row["dalio_signal"] = getattr(dl, "overall_signal", "")

        # Druckenmiller — composite score
        # Bug fix (May 29 2026): field is `overall_signal`, not `signal`.
        dr = analyses.get("druckenmiller")
        if dr is not None:
            row["druck_raw"] = getattr(dr, "overall_score", 0)
            row["druck_signal"] = getattr(dr, "overall_signal", "")
            row["druck_conviction"] = getattr(dr, "conviction", "")

        return row

    # ── RANKED OUTPUT BY FRAMEWORK ──────────────
    def _display_rankings(self, results, top_n):
        """Show top-N by each framework, plus top-N composite."""

        def fmt_row(rank, r, score_key, score_label, extra=""):
            score = r.get(score_key, 0) or 0
            comp = r.get("composite", 0)
            name = r.get("company", "")[:22]
            return (f"  #{rank:<2} {r['ticker']:<6} {name:<22} "
                    f"{score_label}={score:>5.0f}  composite={comp:>5.0f}  {extra}")

        # ── BUFFETT MOAT QUALITY ──
        self._write("\n  🏰 TOP 5 — BUFFETT MOAT QUALITY\n", "framework")
        self._write(f"  {'─' * 70}\n", "dim")
        ranked = sorted(
            [r for r in results if r.get("buffett_moat_raw") is not None],
            key=lambda r: r["buffett_moat_raw"], reverse=True
        )[:top_n]
        for i, r in enumerate(ranked, 1):
            direction = r.get("moat_direction", "")
            delta = r.get("moat_trend_delta", 0.0) or 0.0
            # Show graduated label + actual multiplier so the user sees
            # exactly how much the trend shifted the moat score.
            if direction and direction != "N/A":
                mult_str = f"x{1+delta:.2f}"
                dir_tag = f" · {direction.lower()} ({mult_str})"
            else:
                dir_tag = ""
            extra = f"{r.get('buffett_moat_rating', '')[:10]}{dir_tag}"
            self._write(fmt_row(i, r, "buffett_moat_raw", "moat", extra) + "\n",
                        "strong" if r["buffett_moat_raw"] >= 75 else None)

        # ── WEISS BLUE CHIP ──
        self._write("\n  💎 TOP 5 — WEISS BLUE CHIP QUALITY\n", "framework")
        self._write(f"  {'─' * 70}\n", "dim")
        ranked = sorted(
            [r for r in results if r.get("weiss_quality_raw") is not None],
            key=lambda r: r["weiss_quality_raw"], reverse=True
        )[:top_n]
        for i, r in enumerate(ranked, 1):
            # Show only the rating CATEGORY (before the em-dash), not the
            # full descriptive string — the tail ("strong but not full
            # qualification (5/7 measurable)") was being truncated mid-word
            # to "strong but not ful". The category alone is what matters here.
            full_rating = (r.get("weiss_rating", "") or "")
            category = full_rating.split("—")[0].strip() if "—" in full_rating else full_rating[:20]
            self._write(fmt_row(i, r, "weiss_quality_raw", "weiss", category) + "\n",
                        "strong" if r["weiss_quality_raw"] >= 75 else None)

        # ── BOGLE MEAN-REVERSION TIMING ──
        self._write("\n  📈 TOP 5 — BOGLE TIMING (oversold quality)\n", "framework")
        self._write(f"  {'─' * 70}\n", "dim")
        ranked = sorted(
            [r for r in results if r.get("bogle_timing_raw") is not None],
            key=lambda r: r["bogle_timing_raw"], reverse=True
        )[:top_n]
        for i, r in enumerate(ranked, 1):
            self._write(fmt_row(i, r, "bogle_timing_raw", "timing") + "\n",
                        "strong" if r["bogle_timing_raw"] >= 75 else None)

        # ── DALIO LOW-DEBT BALANCE SHEETS ──
        self._write("\n  🏛 TOP 5 — DALIO BALANCE-SHEET STRENGTH\n", "framework")
        self._write(f"  {'─' * 70}\n", "dim")
        ranked = sorted(
            [r for r in results if r.get("dalio_debt_raw") is not None],
            key=lambda r: r["dalio_debt_raw"], reverse=True
        )[:top_n]
        for i, r in enumerate(ranked, 1):
            self._write(fmt_row(i, r, "dalio_debt_raw", "debt") + "\n",
                        "strong" if r["dalio_debt_raw"] >= 75 else None)

        # ── DRUCKENMILLER TREND ACCELERATION ──
        self._write("\n  🚀 TOP 5 — DRUCKENMILLER MOMENTUM/RoC\n", "framework")
        self._write(f"  {'─' * 70}\n", "dim")
        ranked = sorted(
            [r for r in results if r.get("druck_raw") is not None],
            key=lambda r: r["druck_raw"], reverse=True
        )[:top_n]
        for i, r in enumerate(ranked, 1):
            extra = (r.get("druck_signal", "") or "")[:20]
            self._write(fmt_row(i, r, "druck_raw", "druck", extra) + "\n",
                        "strong" if r["druck_raw"] >= 75 else None)

        # ── COMPOSITE OVERALL ──
        self._write("\n  ⭐ TOP 5 — COMPOSITE OVERALL SCORE\n", "framework")
        self._write(f"  {'─' * 70}\n", "dim")
        ranked = sorted(results, key=lambda r: r.get("composite", 0),
                         reverse=True)[:top_n]
        for i, r in enumerate(ranked, 1):
            extra = (r.get("account_fit", "") or "")[:30]
            self._write(fmt_row(i, r, "composite", "score", extra) + "\n",
                        "strong" if r["composite"] >= 75 else "watch")

        self._write(f"\n  {'─' * 70}\n", "dim")
        self._write(
            f"  Use Export CSV to bring full per-framework breakdown to Claude.\n",
            "dim"
        )

    # ── CSV EXPORT ──────────────────────────────
    def _export_csv(self):
        if not self._results:
            self._write("\n  ⚠️  No results to export.\n", "yellow")
            return
        try:
            default_name = f"stock_analysis_{_dt.now().strftime('%Y%m%d_%H%M')}.csv"
            path = filedialog.asksaveasfilename(
                title="Export scan results for Claude analysis",
                defaultextension=".csv",
                initialfile=default_name,
                filetypes=[("CSV files", "*.csv")],
            )
            if not path:
                return

            cols = [
                "ticker", "company", "sector", "price",
                "composite", "signal", "account_fit",
                "coverage_pct", "data_quality",
                "buffett_moat_raw", "buffett_moat_raw_unadjusted",
                "buffett_moat_rating", "moat_direction", "moat_trend_delta",
                "weiss_quality_raw", "weiss_rating",
                "bogle_timing_raw",
                "dalio_debt_raw", "dalio_bubble_raw",
                "dalio_overall_raw", "dalio_signal",
                "druck_raw", "druck_signal", "druck_conviction",
                "skipped",
                "scan_time",
            ]
            scan_time = _dt.now().isoformat(timespec="seconds")
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = _csv.DictWriter(f, fieldnames=cols)
                w.writeheader()
                for r in self._results:
                    row = {k: r.get(k, "") for k in cols if k != "scan_time"}
                    row["scan_time"] = scan_time
                    # Skipped is a list — flatten
                    if isinstance(row.get("skipped"), list):
                        row["skipped"] = ";".join(row["skipped"])
                    # Round floats
                    for k, v in list(row.items()):
                        if isinstance(v, float):
                            row[k] = round(v, 2)
                    w.writerow(row)

            self._write(f"\n  📄 Exported {len(self._results)} candidates\n", "green")
            self._write(f"     → {path}\n", "dim")
            self._write(f"     Bring this to Claude for full analyst review\n", "dim")
            self._status.config(text=f"Exported {len(self._results)} to CSV")
        except Exception as e:
            self._write(f"\n  ❌ Export failed: {e}\n", "red")

    # ── ASK CLAUDE Q&A ──────────────────────────
    def _ask_claude(self):
        if self._running or not self._results:
            return
        q = self._qa_var.get().strip()
        if not q:
            self._qa_entry.focus()
            return
        self._running = True
        self._qa_btn.config(state="disabled", text="⏳")
        self._qa_entry.config(state="disabled")
        threading.Thread(target=self._claude_thread, args=(q,),
                          daemon=True).start()

    def _claude_thread(self, question):
        try:
            lines = ["STOCK SEARCH RESULTS — top candidates:"]
            # Sort by composite
            ranked = sorted(self._results,
                             key=lambda r: r.get("composite", 0),
                             reverse=True)
            for i, r in enumerate(ranked[:10], 1):
                lines.append(
                    f"{i}. {r['ticker']} — composite {r.get('composite',0):.0f}  "
                    f"(moat {r.get('buffett_moat_raw',0):.0f}, "
                    f"weiss {r.get('weiss_quality_raw',0):.0f}, "
                    f"bogle {r.get('bogle_timing_raw',0):.0f}, "
                    f"dalio_debt {r.get('dalio_debt_raw',0):.0f}, "
                    f"druck {r.get('druck_raw',0):.0f})  "
                    f"{r.get('signal','')}"
                )
            context = "\n".join(lines)
            self._write(f"\n  {'─'*60}\n", "dim")
            self._write(f"  Q: {question}\n\n", "blue")
            self._write("  ⏳ Thinking...\n", "dim")
            answer = ask_lm_studio(question, context, self.portfolio_ctx)
            def _show(ans=answer):
                self._log.config(state="normal")
                pos = self._log.search("  ⏳ Thinking...", "1.0", "end")
                if pos:
                    self._log.delete(pos, f"{pos} lineend+1c")
                self._log.insert("end", f"  {ans}\n\n", "green")
                self._log.see("end")
                self._log.config(state="disabled")
            self.root.after(0, _show)
        except Exception as e:
            self._write(f"  ❌ Error: {e}\n", "red")
        finally:
            self._running = False
            self.root.after(0, lambda: [
                self._qa_btn.config(state="normal", text="💬 Ask",
                                     bg="#238636", fg="#FFFFFF"),
                self._qa_entry.config(state="normal", fg=FG),
                self._qa_var.set(""),
            ])


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app = StockSearcherApp(root)
    root.mainloop()
