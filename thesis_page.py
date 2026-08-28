"""
thesis_page.py
==============
The whole squeeze thesis on one page, as an argument with a verdict.

WHY THIS EXISTS
---------------
Everything else in this toolkit is an INSTRUMENT: a strike matrix, a heat map,
a P/L grid, a structure menu. Each answers one narrow question well, and none
of them answers the only question that matters — do I take this trade, in
what, and what would make me wrong. The synthesis was left to the human, and
the synthesis is the part where money is lost.

This page performs the synthesis, and it is built to be READ IN ORDER:

    VERDICT     the answer first, with the single most decisive reason
    CLOCK       today, earnings, close-out, and which expiries cover them
    FOR         the mechanical case, each claim with its number
    AGAINST     the case to walk away, given EQUAL billing
    EXPRESSION  the best structure of each family, if any survive
    FALSIFIERS  what would have to change for the verdict to flip

WHAT MAKES IT DIFFERENT FROM A DASHBOARD
----------------------------------------
Every claim carries its own evidence and SAMPLE SIZE, drawn from this system's
graded record of its own past predictions. A retail options screen can tell
you the short interest; it cannot tell you that candidates with this verdict
family have averaged -7.7% over ten days across 99 graded episodes, because it
never wrote down what it said last time.

That record is also why the AGAINST column is not a disclaimer. On this
universe the base rate is 13% and implied volatility has run 15% richer than
realized, so most honest verdicts are NO TRADE. A tool that cannot say that
is a tool that sells you something every time you open it.

USAGE
-----
    from thesis_page import build_thesis
    path = build_thesis(ticker, deep=deep, opt=opt_data)   # -> html file path

Opens in the default browser when open_after=True.
"""

import html
import json
import os
import webbrowser
from datetime import date, datetime, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_DIR, "thesis")


# ─────────────────────────────────────────────
# SMALL HELPERS
# ─────────────────────────────────────────────

def _f(v, default=None):
    try:
        x = float(v)
        return x if x == x else default
    except (TypeError, ValueError):
        return default


def _g(obj, name, default=None):
    v = getattr(obj, name, default) if obj is not None else default
    return default if v is None else v


def _pct(v, digits=0, blank="—"):
    x = _f(v)
    return blank if x is None else f"{x:.{digits}%}"


def _num(v, digits=1, blank="—"):
    x = _f(v)
    return blank if x is None else f"{x:,.{digits}f}"


def _esc(s):
    return html.escape(str(s if s is not None else ""))


def _learned():
    try:
        with open(os.path.join(_DIR, "learned_params.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ─────────────────────────────────────────────
# THE ARGUMENT
# ─────────────────────────────────────────────

def build_case(ticker, deep=None, opt=None):
    """Assemble the evidence into FOR / AGAINST / verdict.

    Every entry is (label, value, detail) where detail names the SOURCE and,
    where one exists, the sample size behind it. A claim with no sample size
    is an assumption, and the page says so rather than letting it pass as a
    measurement."""
    lp = _learned()
    opt = opt or {}
    spot = _f(_g(deep, "current_price"), _f(opt.get("spot")))
    fors, againsts, unknowns = [], [], []

    # ── mechanical setup ──
    si = _f(_g(deep, "si_now"))
    # SHORT INTEREST IS STORED AS A FRACTION IN SOME PATHS AND A PERCENT IN
    # OTHERS (the log holds 0.1868 for 18.68%). A threshold written against
    # the wrong unit never fires: the first verdict gate required si >= 20
    # against fractional data and so returned NO on all 694 graded episodes.
    # Normalize before comparing, never after.
    if si is not None and si <= 1.5:
        si *= 100.0
    dtc = _f(_g(deep, "dtc_now"))
    ctb = _f(_g(deep, "ctb_now"))
    if si is not None:
        (fors if si >= 20 else unknowns).append(
            ("Short interest", f"{si:.1f}% of float",
             f"trend {_g(deep, 'si_trend', '?') or '?'}"))
    if ctb is not None:
        (fors if ctb >= 25 else unknowns).append(
            ("Cost to borrow", f"{ctb:.0f}%/yr",
             f"trend {_g(deep, 'ctb_trend', '?') or '?'}"
             + (f", 1w ago {_f(_g(deep,'ctb_1w_ago')):.0f}%"
                if _f(_g(deep, "ctb_1w_ago")) else "")))
    if dtc is not None:
        # Days-to-cover reads as bullish everywhere in this toolkit, but on
        # this system's own graded record it is the single strongest signal
        # and it points the OTHER way (-0.34 with 10-day return). Reported as
        # contested rather than as support.
        againsts.append(
            ("Days to cover", f"{dtc:.1f}d",
             "scored as bullish, but correlates −0.34 with 10-day return "
             "across 2,419 graded rows — the strongest single relationship "
             "in the log, and it opposes the score"))

    ftd_pct = _f(_g(deep, "ftd_pct_float_accum"))
    if ftd_pct:
        (fors if ftd_pct >= 0.005 else unknowns).append(
            ("FTD fail balance", f"{ftd_pct:.2%} of float",
             f"trend {_g(deep, 'ftd_trend', '?') or '?'}"))

    conv = _g(deep, "conviction_state", "")
    if conv:
        unknowns.append(("Conviction", str(conv),
                         f"multiplier ×{_f(_g(deep,'conviction_mult',1)):.2f}"))

    # ── the tool's own track record on this verdict family ──
    verdict = str(_g(deep, "deep_verdict", "") or "")
    fam = verdict.split("—")[0].split("(")[0]
    fam = "".join(c for c in fam if c.isalpha() or c.isspace()).strip().upper()
    vstats = ((lp.get("verdicts") or {}).get("families") or {}).get(fam)
    if vstats:
        entry = ("Verdict family", fam,
                 f"{vstats['n']} graded episodes averaged "
                 f"{vstats['mean_10d']:+.1f}% over 10 days, hit rate "
                 f"{vstats['hit_15']:.0%}")
        (againsts if vstats["mean_10d"] <= -3.0 else fors).append(entry)

    # ── the price of admission ──
    im = _f(_g(deep, "implied_move_pct"), _f(opt.get("implied_move_pct")))
    vp = lp.get("vol_premium") or {}
    ratio = _f(vp.get("ratio"))
    if im:
        detail = "ATM straddle / spot"
        if ratio:
            detail += (f"; across {vp.get('n', 0)} graded episodes realized "
                       f"moves ran {ratio:.2f}× implied")
        againsts.append(("Market already prices", f"±{im:.1%} by expiry", detail))
    if ratio and ratio < 1.0:
        againsts.append(
            ("Volatility premium", f"{(1 - ratio):.0%} overpriced",
             f"buying premium on this universe has been the losing side; "
             f"realized exceeded implied only "
             f"{_pct(vp.get('exceed_rate'))} of the time "
             f"(n={vp.get('n', 0)})"))

    # ── the tool's own predictive power ──
    cal = lp.get("calibration") or {}
    if not cal.get("active"):
        againsts.append(
            ("Model calibration", "OFF",
             f"the system's own winner model does not beat a base-rate "
             f"guess on unseen rows (AUC "
             f"{_num(cal.get('holdout_auc'), 3)}, needs ≥0.55) — no "
             f"per-candidate probability is available"))
    base_rate = _f(cal.get("base_rate"))
    if base_rate:
        againsts.append(
            ("Base rate", f"{base_rate:.0%}",
             f"share of all logged candidates that gained 15%+ in 10 days "
             f"(n={cal.get('n', 0)}) — the prior you are betting against"))

    # ── VERDICT ──
    # Gated on the EXPRESSION, not on the setup score.
    #
    # The first version required two mechanical conditions (short interest,
    # borrow, fail balance) and fired on ZERO of 694 graded episodes — partly
    # from a unit bug, but mostly because those conditions do not discriminate.
    # Repaired and re-measured, they separate outcomes by about half a point
    # of 10-day return, which is noise. The verdict family separates by ten.
    #
    # So the setup metrics are reported as context and the decision rests on
    # two things that are actually decidable: has this verdict family lost
    # money before, and is there a structure you can afford whose EV survives
    # costs and the volatility premium. A tradeable idea you cannot buy is
    # not a trade.
    # An expression must also OUTLIVE the event it is expressing. The clock
    # caught this on the first real page: the verdict recommended an 08-28
    # call while the clock marked 08-28 as expiring before the Sep 08 earnings
    # print. A structure that dies before its own catalyst is not a cheaper
    # way to hold the thesis — it is a different bet, on nothing.
    _events = []
    _ci = (opt.get("catalyst_iso") or _g(deep, "ftd_closeout_date", "") or "")[:10]
    if _ci:
        try:
            _events.append(datetime.strptime(_ci, "%Y-%m-%d").date())
        except ValueError:
            pass
    _de = _f(_g(deep, "days_to_earnings"))
    if _de is not None and 0 <= _de <= 400:
        _events.append(date.today() + timedelta(days=int(_de)))
    _first_event = min(_events) if _events else None

    def _covers(r):
        if _first_event is None:
            return True
        try:
            exp = min(datetime.strptime(l["expiry"], "%Y-%m-%d").date()
                      for l in r["legs"])
        except (KeyError, ValueError, TypeError):
            return True
        return exp >= _first_event

    best = None
    for _k, _r in (opt.get("structures") or {}).items():
        if (_r.get("sizing") or {}).get("contracts", 0) < 1:
            continue
        if (_f(_r.get("ev")) or -1) <= 0:
            continue
        _r = dict(_r, kind=_k, covers_event=_covers(_r))
        if not _r["covers_event"]:
            againsts.append(
                (f"{_k} expires early",
                 _r.get("name", ""),
                 f"positive EV and affordable, but expires before the "
                 f"{_first_event:%b %d} event it is meant to express — "
                 f"excluded from the verdict"))
            continue
        if best is None or (_f(_r.get("kelly")) or -9) > (_f(best.get("kelly")) or -9):
            best = _r

    if not opt.get("blocks"):
        v, vwhy = ("NO TRADE",
                   "No usable option chain — nothing to price, nothing to buy.")
    elif vstats and vstats["mean_10d"] <= -3.0:
        v, vwhy = ("NO TRADE",
                   f"The {fam} verdict family has averaged "
                   f"{vstats['mean_10d']:+.1f}% over 10 days across "
                   f"{vstats['n']} graded episodes — the one relationship in "
                   f"this log that separates outcomes by more than noise. "
                   f"This system has seen this setup and it lost money.")
    elif best is not None:
        sz = best["sizing"]
        v = "TRADEABLE"
        vwhy = (f"{best['name']} is affordable at {sz['contracts']}× "
                f"(${sz['dollars']:,.0f}, {sz['pct']:.0%} of capital, bound by "
                f"{sz['bound_by']}) and carries {_pct(best.get('ev'))} expected "
                f"return after round-trip costs. Size it as a bet, not a "
                f"conviction — nothing here has been validated against a fill.")
    elif opt.get("structures"):
        v, vwhy = ("WAIT",
                   "Structures priced, but none is both affordable at your "
                   "capital and positive-EV after costs. The idea may be "
                   "sound; this expression of it is not.")
    else:
        v, vwhy = ("WAIT",
                   "No structure could be priced — outside market hours every "
                   "bid is cleared, and a spread needs a bid to sell into. "
                   "Re-run during the session.")
    return {"verdict": v, "why": vwhy, "for": fors, "against": againsts,
            "unknown": unknowns, "spot": spot, "implied_move": im,
            "family": fam, "learned": lp, "best": best}


# ─────────────────────────────────────────────
# THE CLOCK
# ─────────────────────────────────────────────

def build_clock(deep=None, opt=None):
    """Today, the events, and which expiries reach them — on one axis.

    This is the piece the other screens could not show. An expiry that covers
    an FTD close-out can still miss the earnings print that actually moves the
    stock, and until now nothing put both dates on the same line."""
    opt = opt or {}
    today = date.today()
    marks = []

    cat_iso = (opt.get("catalyst_iso") or _g(deep, "ftd_closeout_date", "") or "")[:10]
    if cat_iso:
        try:
            d = datetime.strptime(cat_iso, "%Y-%m-%d").date()
            marks.append({"date": d, "kind": "catalyst",
                          "label": _g(deep, "catalyst_type", "") or "FTD close-out"})
        except ValueError:
            pass
    de = _f(_g(deep, "days_to_earnings"))
    if de is not None and 0 <= de <= 400:
        marks.append({"date": today + timedelta(days=int(de)),
                      "kind": "earnings", "label": "Earnings"})

    exps = []
    for b in (opt.get("blocks") or []):
        try:
            exps.append({"date": datetime.strptime(b["expiry"], "%Y-%m-%d").date(),
                         "days": b["days"], "label": b["expiry"]})
        except (ValueError, KeyError):
            continue
    horizon = max([m["date"] for m in marks] + [e["date"] for e in exps]
                  + [today + timedelta(days=30)])
    span = max((horizon - today).days, 1)
    for m in marks:
        m["pct"] = max(0.0, min(1.0, (m["date"] - today).days / span))
    for e in exps:
        e["pct"] = max(0.0, min(1.0, (e["date"] - today).days / span))
        e["covers"] = [m["kind"] for m in marks if e["date"] >= m["date"]]
        e["misses"] = [m["kind"] for m in marks if e["date"] < m["date"]]
    return {"today": today, "horizon": horizon, "span": span,
            "marks": marks, "expiries": exps}


def build_thesis(ticker, deep=None, opt=None, open_after=True,
                 out_path: str = "") -> str:
    """Assemble the case, lay out the clock, render the page. Returns its path."""
    import thesis_render
    case = build_case(ticker, deep, opt)
    case["deep_verdict"] = str(_g(deep, "deep_verdict", "") or "")
    clock = build_clock(deep, opt)
    return thesis_render.render(ticker, case, clock, opt=opt,
                                out_path=out_path, open_after=open_after)


if __name__ == "__main__":
    import sys
    tk = (sys.argv[1] if len(sys.argv) > 1 else "").upper()
    if not tk:
        print("usage: python thesis_page.py TICKER")
        raise SystemExit(1)
    _deep = None
    try:
        import yfinance_throttle  # noqa: F401
        from squeeze_deep import run_deep_analysis
        _deep = run_deep_analysis(tk, stage1_score=50.0)
    except Exception as e:
        print(f"  deep layer unavailable ({e}) — thesis will be thinner")
    _opt = {}
    try:
        from options_ev import run_strike_matrix_data
        _opt = run_strike_matrix_data(tk, "auto" if False else
                                      "40:+15, 35:0, 25:-10", deep=_deep,
                                      max_dte=120)
    except Exception as e:
        print(f"  options layer unavailable ({e})")
    print("wrote:", build_thesis(tk, deep=_deep, opt=_opt, open_after=False))
