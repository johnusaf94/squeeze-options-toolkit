"""
learning_engine.py
===================
Closes the loop. The system already records its own signals
(squeeze_logger.py -> squeeze_log.csv) and grades them against reality
(review_outcomes.py fills 5/10/20-day returns). This module is the part
that was missing: it LEARNS from the graded history and APPLIES the
result, automatically.

The loop:
  scan -> auto-log signals -> [time passes] -> review_outcomes grades
  -> learning_engine derives bounded adjustments -> learned_params.json
  -> next scan reads the file -> scoring shifts toward what actually worked

What it learns (squeeze side):
  - catalyst window scales: does SWEET_SPOT really outperform? By how much?
  - conviction effect scale: do high-conviction-multiplier names deliver?

What it learns (stock side, once stock_log.csv accumulates):
  - per-framework weight scales from correlation with 20-day forward returns

SAFETY RAILS — the difference between learning and overfitting:
  1. SAMPLE GATE: no adjustments until >= 30 resolved rows. Below that
     the file is written with "active": false and scanners ignore it.
  2. SHRINKAGE: adjustment strength scales with evidence. 30 samples ->
     25% of the measured edge is applied; 100+ -> 50%. Never 100%.
  3. HARD BOUNDS: every learned scale lives in [0.75, 1.25]. The system
     can tilt its own parameters by up to a quarter; it cannot rewrite them.
  4. CROSS-SECTIONAL GRADING: a bucket earns adjustment only by beating
     the AVERAGE of all logged candidates over the same windows — not by
     absolute return. A bull market lifts everything; that's not skill.
  5. FULL AUDIT TRAIL: learned_params.json records n, the measured edges,
     the baselines, and the applied scales. Nothing silent.

Run weekly (one command, after review_outcomes.py or via its auto-call):
    python learning_engine.py update      # grade stock log + learn + write
    python learning_engine.py report      # show current learned state
    python learning_engine.py reset       # back to baselines
"""

import csv
import json
import os
from datetime import datetime, timedelta

SQUEEZE_LOG = "squeeze_log.csv"
STOCK_LOG = "stock_log.csv"
PARAMS_FILE = "learned_params.json"

MIN_SAMPLES = 30          # below this: observe, don't adjust
FULL_TRUST_SAMPLES = 100  # at/above this: maximum blend
MIN_BLEND = 0.25
MAX_BLEND = 0.50
SCALE_LO, SCALE_HI = 0.75, 1.25   # hard bounds on every learned scale

# Stock log columns (mirrors squeeze_logger's design: immutable, outcomes
# filled in place later)
STOCK_LOG_COLUMNS = [
    "scan_timestamp", "scan_id", "ticker", "company", "sector",
    "composite", "signal", "coverage_pct", "data_quality",
    "buffett_moat_raw", "buffett_valuation_raw", "weiss_yield_raw",
    "weiss_quality_raw", "bogle_timing_raw", "dalio_debt_raw",
    "dalio_bubble_raw", "lynch_peg_raw", "druck_raw",
    "moat_direction", "price_at_scan",
    "price_20d", "return_20d", "price_60d", "return_60d",
    "outcome_checked",
]

# Components whose weights the stock side can learn (must match
# composite_score.WEIGHTS keys)
STOCK_COMPONENTS = {
    "buffett_moat":      "buffett_moat_raw",
    "buffett_valuation": "buffett_valuation_raw",
    "weiss_yield":       "weiss_yield_raw",
    "weiss_quality":     "weiss_quality_raw",
    "bogle_timing":      "bogle_timing_raw",
    "dalio_debt":        "dalio_debt_raw",
    "dalio_bubble":      "dalio_bubble_raw",
    "lynch_peg":         "lynch_peg_raw",
    "druckenmiller":     "druck_raw",
}


# ─────────────────────────────────────────────
# SHARED HELPERS
# ─────────────────────────────────────────────

def _f(v, default=None):
    try:
        x = float(v)
        return x
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────
# RETURN-UNIT NORMALIZATION
# ─────────────────────────────────────────────
# THE critical integrity layer. This engine's math (excess/100, the +15%
# winner target) assumes returns are in PERCENT (4.0 = 4%). If the grader
# wrote FRACTIONS (0.04 = 4%), every measured edge collapses ~100x, all
# scales silently converge to 1.0, and calibration never finds a winner —
# the system runs but learns nothing. Rather than trusting any writer,
# normalize at READ time, in memory only (the CSV is never mutated):
#   1) GOLD: if the row carries entry+outcome prices, recompute the return
#      from prices — immune to unit ambiguity entirely.
#   2) FALLBACK: distribution test on the remaining values. Squeeze-name
#      10-day returns in percent routinely exceed |1.0|; a graded set
#      where ~all |values| <= 1.5 is fraction-coded beyond reasonable
#      doubt. Convert x100.

_PRICE_PAIRS_10D = (("price_at_entry", "price_10d"),
                    ("entry_price", "price_10d"),
                    ("price_at_scan", "price_10d"),
                    ("price_at_log", "price_10d"))


def _normalize_return_units(rows: list, ret_col: str = "return_10d") -> dict:
    """Normalize rows[*][ret_col] to PERCENT in place (in memory).
    Returns a note dict describing what was done, for evidence/report."""
    note = {"col": ret_col, "n": 0, "price_recomputed": 0,
            "converted_x100": 0, "verdict": "no graded rows"}
    graded = [r for r in rows if _f(r.get(ret_col)) is not None]
    note["n"] = len(graded)
    if not graded:
        return note

    # Pass 1 — gold standard: recompute from prices where available
    remaining = []
    for r in graded:
        done = False
        for c0, c1 in _PRICE_PAIRS_10D:
            p0, p1 = _f(r.get(c0)), _f(r.get(c1))
            if p0 and p1 and p0 > 0:
                r[ret_col] = round((p1 / p0 - 1.0) * 100.0, 3)
                note["price_recomputed"] += 1
                done = True
                break
        if not done:
            remaining.append(r)

    # Pass 2 — distribution test on whatever prices couldn't settle
    if len(remaining) >= 10:
        vals = sorted(abs(_f(r.get(ret_col), 0.0)) for r in remaining)
        p95 = vals[int(0.95 * (len(vals) - 1))]
        if p95 <= 1.5:      # ~all under 1.5 "percent" -> fraction-coded
            for r in remaining:
                v = _f(r.get(ret_col))
                if v is not None:
                    r[ret_col] = round(v * 100.0, 3)
            note["converted_x100"] = len(remaining)
    elif remaining:
        note["undetermined"] = len(remaining)   # too few to test; left as-is

    if note["price_recomputed"] and note["converted_x100"]:
        note["verdict"] = "mixed: prices recomputed + fraction block converted"
    elif note["converted_x100"]:
        note["verdict"] = "FRACTION-coded log detected -> converted x100"
    elif note["price_recomputed"] == note["n"]:
        note["verdict"] = "all returns recomputed from prices (unit-proof)"
    else:
        note["verdict"] = "percent units confirmed / left unchanged"
    return note


# Populated on each _graded_squeeze_rows() call; surfaced in params + report
_LAST_UNITS_NOTE = {}


# ─────────────────────────────────────────────
# ADVISORY FILE LOCK (stock_log writers)
# ─────────────────────────────────────────────
# log_stock_scan APPENDS during scans; fill_stock_outcomes REWRITES the
# whole file during grading. If those overlap, appended rows are lost in
# the rewrite (read-modify-write race). A tiny lockfile makes writers
# take turns; stale locks (>10 min, e.g. a crashed run) are broken.

_LOCK_PATH = STOCK_LOG + ".lock"
_LOCK_STALE_S = 600


def _acquire_log_lock(timeout_s: float = 20.0) -> bool:
    import time as _t
    deadline = _t.time() + timeout_s
    while True:
        try:
            fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if _t.time() - os.path.getmtime(_LOCK_PATH) > _LOCK_STALE_S:
                    os.remove(_LOCK_PATH)   # break stale lock
                    continue
            except OSError:
                pass
            if _t.time() >= deadline:
                return False
            _t.sleep(0.25)
        except OSError:
            return False


def _release_log_lock():
    try:
        os.remove(_LOCK_PATH)
    except OSError:
        pass


def _blend_for(n: int) -> float:
    """Evidence-scaled shrinkage factor."""
    if n < MIN_SAMPLES:
        return 0.0
    if n >= FULL_TRUST_SAMPLES:
        return MAX_BLEND
    span = FULL_TRUST_SAMPLES - MIN_SAMPLES
    return MIN_BLEND + (MAX_BLEND - MIN_BLEND) * (n - MIN_SAMPLES) / span


def _bounded_scale(edge_frac: float, blend: float) -> float:
    """Convert a measured relative edge (fraction, e.g. +0.04 = 4 points of
    excess return) into a bounded, shrunk multiplier scale.

    Sensitivity 2.5: a +4% mean excess maps to a raw 1.10 scale; shrinkage
    then takes 25-50% of that. Hard-clamped to [0.75, 1.25] regardless."""
    raw = 1.0 + max(-0.25, min(0.25, edge_frac * 2.5))
    scaled = 1.0 + blend * (raw - 1.0)
    return round(max(SCALE_LO, min(SCALE_HI, scaled)), 4)


def load_params() -> dict:
    """Safe load — scanners call this. Returns {} when absent/invalid."""
    if not os.path.exists(PARAMS_FILE):
        return {}
    try:
        with open(PARAMS_FILE, "r", encoding="utf-8") as f:
            p = json.load(f)
        return p if isinstance(p, dict) else {}
    except Exception:
        return {}


def _save_params(p: dict):
    tmp = f"{PARAMS_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PARAMS_FILE)


# ─────────────────────────────────────────────
# SQUEEZE-SIDE LEARNING
# ─────────────────────────────────────────────

def _graded_squeeze_rows() -> list:
    global _LAST_UNITS_NOTE
    if not os.path.exists(SQUEEZE_LOG):
        return []
    with open(SQUEEZE_LOG, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    _LAST_UNITS_NOTE = _normalize_return_units(rows, "return_10d")
    out = []
    for r in rows:
        if not r.get("outcome_checked") or _f(r.get("return_10d")) is None:
            continue
        # A ROW FROM A CRASHED ANALYSIS IS NOT AN OBSERVATION.
        # When the deep layer raises, the scanner still logs the candidate
        # with deep_score/probability/imminence/magnitude all set to 0.0 and
        # the verdict set to "deep failed: <error>". Those zeros are not
        # measurements of anything, but to a fitter they are indistinguishable
        # from a genuinely weak candidate. Ten such rows entered the log on
        # 2026-07-03 alone, from one schema migration.
        if (r.get("deep_verdict") or "").startswith("deep failed"):
            continue
        out.append(r)
    return out


def analyze_squeeze(rows: list) -> dict:
    """Cross-sectional bucket stats on 10-day returns."""
    rets = [_f(r["return_10d"]) for r in rows]
    overall = sum(rets) / len(rets) if rets else 0.0

    def bucket(pred):
        sub = [_f(r["return_10d"]) for r in rows if pred(r)]
        if len(sub) < 5:           # too thin to mean anything
            return None
        return {"n": len(sub),
                "mean": round(sum(sub) / len(sub), 3),
                "excess": round(sum(sub) / len(sub) - overall, 3)}

    windows = {}
    for w in ("SWEET_SPOT", "IMMINENT", "PASSED", "TOO_FAR", "NONE"):
        b = bucket(lambda r, _w=w: r.get("catalyst_window") == _w)
        if b:
            windows[w] = b

    conviction = {
        "high": bucket(lambda r: (_f(r.get("conviction_mult"), 1.0) or 1.0) > 1.05),
        "neutral": bucket(lambda r: abs((_f(r.get("conviction_mult"), 1.0) or 1.0) - 1.0) <= 0.05),
        "low": bucket(lambda r: (_f(r.get("conviction_mult"), 1.0) or 1.0) < 0.95),
    }
    return {"n": len(rows), "overall_mean_10d": round(overall, 3),
            "windows": windows, "conviction": conviction}


def derive_squeeze_params(stats: dict) -> dict:
    n = stats["n"]
    blend = _blend_for(n)
    out = {"n_samples": n, "blend": round(blend, 3),
           "active": blend > 0,
           "catalyst_window_scale": {},
           "conviction_effect_scale": 1.0,
           "evidence": stats}
    if blend == 0:
        return out

    for w, b in stats["windows"].items():
        # edge as fraction: +4.0 excess points -> 0.04
        out["catalyst_window_scale"][w] = _bounded_scale(b["excess"] / 100.0, blend)

    hi = stats["conviction"].get("high")
    if hi:
        out["conviction_effect_scale"] = _bounded_scale(hi["excess"] / 100.0, blend)
    return out


# ─────────────────────────────────────────────
# STOCK-SIDE LOGGING + GRADING + LEARNING
# ─────────────────────────────────────────────

def log_stock_scan(result_rows: list, top_n: int = 40) -> tuple:
    """Append top candidates from a stock-searcher scan. Called by
    stock_searcher_gui automatically. Write-only; outcomes filled later."""
    scan_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    ts = datetime.now().isoformat(timespec="seconds")
    exists = os.path.exists(STOCK_LOG)
    got_lock = _acquire_log_lock()
    try:
        ranked = sorted(result_rows,
                        key=lambda r: r.get("composite") or 0, reverse=True)[:top_n]
        with open(STOCK_LOG, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=STOCK_LOG_COLUMNS)
            if not exists:
                w.writeheader()
            for r in ranked:
                w.writerow({
                    "scan_timestamp": ts,
                    "scan_id": scan_id,
                    "ticker": r.get("ticker", ""),
                    "company": r.get("company", ""),
                    "sector": r.get("sector", ""),
                    "composite": r.get("composite", ""),
                    "signal": r.get("signal", ""),
                    "coverage_pct": r.get("coverage_pct", ""),
                    "data_quality": r.get("data_quality", ""),
                    "buffett_moat_raw": r.get("buffett_moat_raw", ""),
                    "buffett_valuation_raw": r.get("buffett_valuation_raw", ""),
                    "weiss_yield_raw": r.get("weiss_yield_raw", ""),
                    "weiss_quality_raw": r.get("weiss_quality_raw", ""),
                    "bogle_timing_raw": r.get("bogle_timing_raw", ""),
                    "dalio_debt_raw": r.get("dalio_debt_raw", ""),
                    "dalio_bubble_raw": r.get("dalio_bubble_raw", ""),
                    "lynch_peg_raw": r.get("lynch_peg_raw", ""),
                    "druck_raw": r.get("druck_raw", ""),
                    "moat_direction": r.get("moat_direction", ""),
                    "price_at_scan": r.get("price", ""),
                    "price_20d": "", "return_20d": "",
                    "price_60d": "", "return_60d": "",
                    "outcome_checked": "",
                })
        return scan_id, True
    except Exception:
        return scan_id, False
    finally:
        if got_lock:
            _release_log_lock()


def fill_stock_outcomes() -> int:
    """Fill 20/60-trading-day returns for stock_log rows old enough.
    Lazy yfinance; one history fetch per ticker."""
    if not os.path.exists(STOCK_LOG):
        return 0
    import yfinance as yf

    with open(STOCK_LOG, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    now = datetime.now()
    hist_cache = {}
    updated = 0

    def px_on_or_after(tk, target):
        if tk not in hist_cache:
            try:
                # 2y (not 1y): rows older than a year could never grade and
                # silently rotted in the backlog with only a 1y window
                hist_cache[tk] = yf.Ticker(tk).history(period="2y")
            except Exception:
                hist_cache[tk] = None
        h = hist_cache[tk]
        if h is None or h.empty:
            return None
        for idx, row in h.iterrows():
            d = idx.to_pydatetime().replace(tzinfo=None)
            if d >= target:
                return float(row["Close"])
        return None

    for r in rows:
        if r.get("outcome_checked") and r.get("return_60d"):
            continue
        try:
            scan_d = datetime.fromisoformat(r["scan_timestamp"])
        except Exception:
            continue
        p0 = _f(r.get("price_at_scan"))
        tk = r.get("ticker", "")
        if not tk:
            continue
        changed = False
        # ~20 trading days ≈ 28 calendar; 60 ≈ 84
        for col, cal_days in (("20d", 28), ("60d", 84)):
            if r.get(f"return_{col}"):
                continue
            target = scan_d + timedelta(days=cal_days)
            if now < target:
                continue
            px = px_on_or_after(tk, target)
            if px is None:
                continue
            if p0 is None:
                p0 = px_on_or_after(tk, scan_d)
                if p0:
                    r["price_at_scan"] = round(p0, 4)
            if p0:
                r[f"price_{col}"] = round(px, 4)
                r[f"return_{col}"] = round((px / p0 - 1) * 100, 3)
                changed = True
        if changed:
            r["outcome_checked"] = now.strftime("%Y-%m-%d")
            updated += 1

    if updated:
        got_lock = _acquire_log_lock()
        try:
            tmp = f"{STOCK_LOG}.{os.getpid()}.tmp"
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=STOCK_LOG_COLUMNS)
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, "") for k in STOCK_LOG_COLUMNS})
            os.replace(tmp, STOCK_LOG)
        finally:
            if got_lock:
                _release_log_lock()
    return updated


def _graded_stock_rows() -> list:
    if not os.path.exists(STOCK_LOG):
        return []
    with open(STOCK_LOG, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if _f(r.get("return_20d")) is not None]


def analyze_stock(rows: list) -> dict:
    """Correlation of each framework's score with 20-day forward return."""
    out = {"n": len(rows), "component_corr": {}}
    if len(rows) < 5:
        return out
    rets = [_f(r["return_20d"]) for r in rows]

    def corr(xs, ys):
        pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
        if len(pairs) < 10:
            return None
        xs2, ys2 = zip(*pairs)
        n = len(pairs)
        mx, my = sum(xs2) / n, sum(ys2) / n
        cov = sum((a - mx) * (b - my) for a, b in pairs)
        vx = sum((a - mx) ** 2 for a in xs2) ** 0.5
        vy = sum((b - my) ** 2 for b in ys2) ** 0.5
        if vx == 0 or vy == 0:
            return None
        return cov / (vx * vy)

    for comp, col in STOCK_COMPONENTS.items():
        c = corr([_f(r.get(col)) for r in rows], rets)
        if c is not None:
            out["component_corr"][comp] = round(c, 4)
    return out


def derive_stock_params(stats: dict) -> dict:
    n = stats["n"]
    blend = _blend_for(n)
    out = {"n_samples": n, "blend": round(blend, 3),
           "active": blend > 0 and bool(stats.get("component_corr")),
           "weight_scale": {}, "evidence": stats}
    if not out["active"]:
        return out
    for comp, c in stats["component_corr"].items():
        # correlation -> edge fraction (corr 0.20 ≈ strong for noisy returns
        # -> treat as 5% edge before shrinkage)
        out["weight_scale"][comp] = _bounded_scale(c * 0.25, blend)
    return out


# ─────────────────────────────────────────────
# CALIBRATION LAYER (Phase 5.1) — score → probability
# ─────────────────────────────────────────────
# The endgame: stop saying "FINAL 128" and start saying "31% chance of a
# +15% move within 10 days, vs a 9% base rate." A pure-python logistic
# regression on the graded log — no sklearn dependency, no black box:
# every weight is inspectable in learned_params.json.
#
# HARD GATES (stricter than the bounded-adjustment stage):
#   - >= 150 graded rows
#   - >= 20 winners AND >= 20 losers (no degenerate fits)
# Until both hold, "calibration": {"active": false} and the scanners
# show nothing. A probability fitted on 40 rows is a lie with decimals.

CALIB_MIN_ROWS = 150
CALIB_MIN_CLASS = 20
CALIB_TARGET_RET = 15.0      # "winner" = return_10d > +15%

CALIB_FEATURES = [
    # (name, extractor from a graded squeeze_log row, default)
    ("si_pct",          lambda r: _f(r.get("si_pct")), None),
    ("dtc",             lambda r: min(_f(r.get("dtc"), 0) or 0, 20.0), None),
    ("ctb",             lambda r: min(_f(r.get("ctb"), 0) or 0, 150.0), None),
    ("conviction_mult", lambda r: _f(r.get("conviction_mult"), 1.0), 1.0),
    ("sweet_spot",      lambda r: 1.0 if r.get("catalyst_window") == "SWEET_SPOT" else 0.0, 0.0),
    ("implied_move",    lambda r: _f(r.get("implied_move_pct")), None),
    ("combined",        lambda r: _f(r.get("combined")), None),
    # Effective-float features. Absent from every row logged before they
    # existed, so MIN_FEATURE_PRESENCE drops them automatically and reports
    # them as dropped by name — they switch themselves on only once enough
    # graded rows carry them. Nothing to schedule and nothing to remember.
    # NOTE the `is None else min(...)` shape rather than the `or 0` idiom used
    # by dtc/ctb above. Those columns have existed since the log began, so
    # coalescing them to 0 is harmless. These three do not exist in any row
    # logged before today, and `or 0` would report them as 100% PRESENT with
    # every value fabricated — defeating MIN_FEATURE_PRESENCE and switching
    # the feature off silently, which is precisely the trap documented at
    # _calib_matrix. Missing must stay None so the presence floor can see it.
    ("ftd_pct_eff_float", lambda r: _f(r.get("ftd_pct_eff_float")), None),
    ("ftd_adv_days",    lambda r: (None if _f(r.get("ftd_closeout_adv_days")) is None
                                   else min(_f(r.get("ftd_closeout_adv_days")), 10.0)),
     None),
    ("inst_over_float", lambda r: (None if _f(r.get("inst_shares_over_float")) is None
                                   else min(_f(r.get("inst_shares_over_float")), 5.0)),
     None),
    # Spike-robust days to cover. Added because DTC is already the strongest
    # single correlate with forward returns in the graded log (-0.34) and the
    # `dtc` column measures it against a 10-session MEAN, which a single
    # volume spike can move by 60% (GME 2026-08-14: 5.31 on the mean, 8.49 on
    # the median of the same window). If DTC carries signal, the version not
    # set by outlier sessions should carry more of it.
    #
    # Only this one of the DTC family is registered. dtc_60d and the spike
    # ratio are logged and available, but this fit already fails its own AUC
    # gate on ~694 independent episodes, and adding three correlated columns
    # to a model that cannot yet beat a base rate buys overfitting, not
    # insight. Register them when this one has earned its place.
    ("dtc_robust",      lambda r: (None if _f(r.get("dtc_robust")) is None
                                   else min(_f(r.get("dtc_robust")), 20.0)),
     None),
]


MIN_FEATURE_PRESENCE = 0.70   # a column absent more often than this is not a feature
MIN_ROW_COMPLETENESS = 0.80   # a row missing this much of the retained set is not an example
CLEAN_COHORT_MIN = 0.85       # feature_completeness at/above this = "clean" row
CLEAN_COHORT_MIN_ROWS = 150   # below this the clean cohort cannot be judged
EPISODE_DAYS = 5              # same ticker inside this window = one episode


def _episode_key_list(rows) -> list:
    """Episode key per row: (ticker, scan_date // EPISODE_DAYS).

    The same candidate reappearing in tomorrow's scan is not a second
    independent observation of anything — it is the same setup, still running.
    Treating it as independent inflates every sample count and every
    confidence estimate built on one."""
    from datetime import datetime as _dt
    out = []
    for r in rows:
        try:
            d = _dt.fromisoformat(r["scan_timestamp"]).toordinal()
        except (ValueError, TypeError, KeyError):
            d = 0
        out.append((r.get("ticker", ""), d // EPISODE_DAYS))
    return out


def _episode_dedupe(rows) -> list:
    """One row per (ticker, 5-day window). Every statistic below is computed
    on episodes, not raw rows, because raw rows overcount 3.5x."""
    seen, out = set(), []
    for r, key in zip(rows, _episode_key_list(rows)):
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


VERDICT_MIN_N = 20            # below this an episode bucket says nothing


def derive_vol_premium(rows) -> dict:
    """Did the options market over- or under-price movement on these names?

    The single most decision-relevant number for a tool that BUYS CALLS. If
    implied move systematically exceeds realized move, long premium is the
    losing side of the trade before commissions and spreads are even counted,
    and no amount of strike selection repairs that.

    Compares the ATM straddle's implied move against the realized absolute
    10-day move, on episodes carrying both."""
    eps = _episode_dedupe(rows)
    pairs = []
    for r in eps:
        im = _f(r.get("implied_move_pct"))
        ret = _f(r.get("return_10d"))          # normalized to PERCENT upstream
        if im and im > 0 and ret is not None:
            pairs.append((im, abs(ret) / 100.0))
    out = {"n": len(pairs)}
    if len(pairs) < VERDICT_MIN_N:
        out["gate"] = f"need {VERDICT_MIN_N}+ episodes with both (have {len(pairs)})"
        return out
    mi = sum(a for a, _ in pairs) / len(pairs)
    ma = sum(b for _, b in pairs) / len(pairs)
    exceed = sum(1 for a, b in pairs if b > a) / len(pairs)
    out.update({
        "mean_implied": round(mi, 4),
        "mean_realized_abs": round(ma, 4),
        "ratio": round(ma / mi, 4) if mi else None,
        "exceed_rate": round(exceed, 4),
        "verdict": ("implied move is OVERPRICED — long premium is structurally "
                    "the losing side here"
                    if ma < mi else
                    "implied move is underpriced — long premium is favored"),
    })
    return out


def derive_verdict_stats(rows) -> dict:
    """Per-verdict forward returns, at episode level.

    The verdict FAMILY (the free-text field carries suffixes) separates
    outcomes far better than the headline score does — measured on this log,
    roughly ten points of 10-day return between the best and worst family,
    while final_score quintiles show no monotonic pattern at all. That makes
    this the ranking signal the system already had and was not using."""
    eps = _episode_dedupe(rows)
    fam = {}
    for r in eps:
        v = (r.get("deep_verdict") or "").strip()
        if not v or v.startswith("deep failed"):
            continue
        # collapse "TRAP — statics strong but pressure EASING" -> "TRAP"
        key = v.split("—")[0].split("(")[0].strip()
        key = "".join(ch for ch in key if ch.isalpha() or ch.isspace()).strip()
        if not key:
            continue
        ret = _f(r.get("return_10d"))
        if ret is None:
            continue
        fam.setdefault(key.upper(), []).append(ret)
    out = {"families": {}, "n_episodes": len(eps)}
    for k, xs in sorted(fam.items(), key=lambda kv: -len(kv[1])):
        if len(xs) < VERDICT_MIN_N:
            continue
        out["families"][k] = {
            "n": len(xs),
            "mean_10d": round(sum(xs) / len(xs), 2),
            "hit_15": round(sum(1 for x in xs if x > 15) / len(xs), 4),
        }
    # Families whose measured mean is materially negative are avoid-signals.
    out["avoid"] = sorted(k for k, v in out["families"].items()
                          if v["mean_10d"] <= -3.0)
    return out


PATH_MIN_N = 20


PATH_ANCHORS = (-8, -4, -2, 0, 2, 4, 8, 14)   # days relative to the EVENT
PATH_MIN_BIN = 8                              # samples needed to trust a bin


def derive_path_shape_event(rows) -> dict:
    """Average realized path in EVENT TIME, separately for up and down.

    WHY CALENDAR TIME WAS WRONG
    ---------------------------
    The first version averaged each episode's 5/10/20-day returns by days from
    the SCAN. Every episode's catalyst falls on a different day, so averaging
    that way smears any event-day move across the window and irons it flat.
    It produced two nearly mirror-image curves for up and down, which is both
    wrong and useless: a symmetric picture cannot show risk against reward,
    which is the entire reason to look at the chart.

    Aligned to each episode's own event date instead, the two sides stop
    resembling each other at all:

        UP    peaks BEFORE the event (+30.5% at day -2 vs +26.4% at day 0),
              then eases — the run-up is the trade, not the print
        DOWN  drifts gently, CLIFFS at the event (-11.5% -> -17.8%),
              bottoms just after, then recovers about half by day +10

    Stored as a fraction of the day-0 (event day) move, so the shape can be
    scaled onto any scenario magnitude. Bins below PATH_MIN_BIN samples are
    omitted rather than reported thin, and n is kept for every bin so the
    consumer can see how much evidence is behind the curve it is drawing."""
    eps = _episode_dedupe(rows)
    buckets = {"UP": {}, "DOWN": {}, "FLAT": {}}
    n_used = 0
    for r in eps:
        de = _f(r.get("days_to_earnings"))
        p0 = _f(r.get("price_at_scan"))
        if de is None or not p0 or p0 <= 0 or not (0 <= de <= 30):
            continue
        obs = []
        for d, col in ((5, "price_5d"), (10, "price_10d"), (20, "price_20d")):
            p = _f(r.get(col))
            if p and p > 0:
                obs.append((d, p / p0 - 1.0))
        if len(obs) < 3:
            continue
        n_used += 1
        out10 = dict(obs).get(10, 0.0)
        side = ("UP" if out10 >= 0.15 else
                ("DOWN" if out10 <= -0.05 else "FLAT"))
        for d, ret in obs:
            rel = round(d - de)
            for a in PATH_ANCHORS:
                if abs(rel - a) <= 1:
                    buckets[side].setdefault(a, []).append(ret)
    out = {"n_episodes": n_used, "anchors": list(PATH_ANCHORS), "sides": {}}
    if n_used < PATH_MIN_N:
        out["gate"] = (f"need {PATH_MIN_N}+ episodes with an event date and a "
                       f"full price path (have {n_used})")
        return out
    for side, bins in buckets.items():
        zero = bins.get(0) or []
        if len(zero) < PATH_MIN_BIN:
            continue
        base = sum(zero) / len(zero)
        if abs(base) < 1e-6:
            continue
        curve, counts = {}, {}
        for a in PATH_ANCHORS:
            vals = bins.get(a) or []
            if len(vals) < PATH_MIN_BIN:
                continue
            curve[str(a)] = round((sum(vals) / len(vals)) / base, 3)
            counts[str(a)] = len(vals)
        if len(curve) >= 3:
            curve = _smooth_curve(curve, counts)
            out["sides"][side] = {"frac": curve, "n": counts,
                                  "day0_mean": round(base, 4)}
    return out


def _smooth_curve(curve: dict, counts: dict) -> dict:
    """Sample-weighted smoothing of an event-time curve, NEVER across day 0.

    Thin bins make the raw curve zigzag — a measured wobble that is noise, not
    anatomy, and it reads as a jittery path on the chart. Each anchor is
    blended with its neighbours weighted by sample count, so well-populated
    bins pull harder than sparse ones.

    The event boundary is deliberately excluded from the smoothing window: the
    step at day 0 IS the signal (down moves cliff there), and averaging across
    it would erase exactly the feature that makes the two sides differ."""
    keys = sorted(float(k) for k in curve)
    out = {}
    for i, k in enumerate(keys):
        # neighbours on the SAME side of the event only
        window = [k]
        for j in (i - 1, i + 1):
            if 0 <= j < len(keys):
                nb = keys[j]
                if (nb < 0) == (k < 0) or (nb == 0) == (k == 0):
                    window.append(nb)
        num = den = 0.0
        for w in window:
            wt = float(counts.get(str(int(w)), 1)) * (1.0 if w == k else 0.5)
            num += wt * float(curve[str(int(w))])
            den += wt
        out[str(int(k))] = round(num / den, 3) if den else curve[str(int(k))]
    return out


def derive_path_shape(rows) -> dict:
    """The AVERAGE REALIZED PRICE PATH, by outcome, from logged prices.

    The contract P/L overlay draws a path invented by hand: a square-root ramp
    into the catalyst, two days of momentum, then a fade giving back half the
    move; failures were assumed to drift steadily lower. Nobody had checked
    either against a realized price.

    Measured on episodes carrying a full 0/5/10/20-day price record, the
    stylized shapes are wrong in the tail, and in the direction that matters:

        UP    d5 +16.9%  d10 +26.2%  d20 +28.6%   holds and EXTENDS
        DOWN  d5 -14.2%  d10 -20.2%  d20 -17.1%   partially RECOVERS
        FLAT  d5  +3.1%  d10  +3.8%  d20  +6.8%   drifts UP, not back to spot

    A winner that keeps working through day 20 is a different trade from one
    that gives back half by day 15 — different exit, different expiry choice.
    Shapes are stored as a fraction of the 10-day move so they can be scaled
    onto any catalyst horizon."""
    eps = _episode_dedupe(rows)
    recs = []
    for r in eps:
        p0 = _f(r.get("price_at_scan"))
        p5, p10, p20 = (_f(r.get("price_5d")), _f(r.get("price_10d")),
                        _f(r.get("price_20d")))
        if p0 and p5 and p10 and p20 and p0 > 0:
            recs.append((p5 / p0 - 1.0, p10 / p0 - 1.0, p20 / p0 - 1.0))
    out = {"n_paths": len(recs), "buckets": {}}
    if len(recs) < PATH_MIN_N:
        out["gate"] = f"need {PATH_MIN_N}+ full price paths (have {len(recs)})"
        return out
    defs = (("UP", lambda x: x >= 0.15),
            ("FLAT", lambda x: -0.05 < x < 0.15),
            ("DOWN", lambda x: x <= -0.05))
    for name, sel in defs:
        sub = [x for x in recs if sel(x[1])]
        if len(sub) < PATH_MIN_N:
            continue
        m5 = sum(x[0] for x in sub) / len(sub)
        m10 = sum(x[1] for x in sub) / len(sub)
        m20 = sum(x[2] for x in sub) / len(sub)
        if abs(m10) < 1e-6:
            continue
        out["buckets"][name] = {
            "n": len(sub),
            "mean_5d": round(m5, 4), "mean_10d": round(m10, 4),
            "mean_20d": round(m20, 4),
            # fraction of the 10-day move realized by day d
            "f5": round(m5 / m10, 3), "f10": 1.0, "f20": round(m20 / m10, 3),
        }
    return out


def cohort_comparison(rows) -> dict:
    """Fit the calibration separately on CLEAN rows and report both.

    THE QUESTION THIS EXISTS TO SETTLE
    ----------------------------------
    Two silent outages degraded the inputs for months. When the model then
    scored AUC 0.404 on holdout, there was no way to tell whether the signal
    was genuinely inverted or simply starved. Dropping the worst feature moved
    it only to 0.418, which points at inversion — but the clean test is to fit
    on rows logged with complete features and compare.

    squeeze_logger now stamps feature_completeness on every row, so that test
    becomes possible the moment enough clean rows exist. Until then this
    reports how far off that is, rather than pretending to an answer.

    Returns {} when there is nothing meaningful to say yet."""
    clean = [r for r in rows
             if (_f(r.get("feature_completeness")) or 0.0) >= CLEAN_COHORT_MIN]
    out = {"clean_rows_available": len(clean),
           "clean_rows_needed": CLEAN_COHORT_MIN_ROWS,
           "threshold": CLEAN_COHORT_MIN}
    if len(clean) < CLEAN_COHORT_MIN_ROWS:
        out["status"] = (
            f"{len(clean)}/{CLEAN_COHORT_MIN_ROWS} clean rows — rows logged "
            f"before the data fixes carry no completeness stamp and cannot "
            f"join this cohort. Keep scanning; this answers itself.")
        return out
    sub = derive_calibration(clean)
    out["status"] = "clean cohort evaluated"
    out["clean_fit"] = {k: sub.get(k) for k in
                        ("n", "holdout_auc", "holdout_log_loss",
                         "baseline_log_loss", "beats_baseline", "active")}
    return out


def _calib_matrix(rows):
    """Rows -> (X, y, means, stds, diagnostics).

    WHY THIS GOT STRICTER
    ---------------------
    The original mean-imputed every missing value. After standardization an
    imputed value contributes EXACTLY ZERO, so a feature that is absent 41% of
    the time silently becomes a feature that is switched off for 41% of the
    training set — while still occupying a weight the fit reports as if it
    meant something.

    That was not hypothetical. Measured on the real graded log:

        implied_move_pct   59% present      <- a caching bug destroyed chains
        svr_recent         18% present      <- an expired CA cert killed FINRA
        gex_net_musd       56% present
        si_pct / dtc / ctb 100% present

    The calibration was fit on that and scored AUC 0.404 on holdout, which was
    read as "the signal is inverted". It is at least as likely that two of the
    seven inputs were simply absent. A model cannot be judged on a matrix it
    was never really given.

    So now: a column present in fewer than MIN_FEATURE_PRESENCE of rows is
    DROPPED rather than imputed, and a row missing more than
    MIN_ROW_COMPLETENESS of the retained columns is not a training example.
    Imputation still fills the occasional gap in a column that is otherwise
    healthy — which is what imputation is actually for. Everything dropped is
    reported by name, so a shrinking feature set is visible, not silent."""
    raw, y, src = [], [], []
    for r in rows:
        ret = _f(r.get("return_10d"))
        if ret is None:
            continue
        raw.append([ex(r) for _, ex, _ in CALIB_FEATURES])
        y.append(1.0 if ret > CALIB_TARGET_RET else 0.0)
        src.append(r)
    if not raw:
        return [], [], [], [], {"n_rows": 0}

    names = [f[0] for f in CALIB_FEATURES]
    n = len(raw)
    presence = [sum(1 for row in raw if row[j] is not None) / n
                for j in range(len(names))]
    keep = [j for j in range(len(names)) if presence[j] >= MIN_FEATURE_PRESENCE]
    dropped = {names[j]: round(presence[j], 3)
               for j in range(len(names)) if j not in keep}

    diag = {"n_rows": n,
            "presence": {names[j]: round(presence[j], 3)
                         for j in range(len(names))},
            "features_used": [names[j] for j in keep],
            "features_dropped": dropped}
    if not keep:
        diag["fatal"] = "every feature fell below the presence floor"
        return [], [], [], [], diag

    # Keep only rows that actually carry the retained features.
    rows_kept = []
    for row, yi, r in zip(raw, y, src):
        have = sum(1 for j in keep if row[j] is not None)
        if have / len(keep) >= MIN_ROW_COMPLETENESS:
            rows_kept.append((row, yi, r))
    diag["rows_dropped_incomplete"] = n - len(rows_kept)
    if not rows_kept:
        diag["fatal"] = "no row carries enough of the retained features"
        return [], [], [], [], diag

    raw = [a for a, _, _ in rows_kept]
    y = [b for _, b, _ in rows_kept]
    # Episode ids for the SURVIVING samples, in order — the only way they can
    # stay aligned with X once rows have been filtered out.
    diag["episodes"] = _episode_key_list([r for _, _, r in rows_kept])
    diag["effective_n"] = len(set(diag["episodes"]))
    means, stds = [], []
    for j in keep:
        col = [row[j] for row in raw if row[j] is not None]
        m = sum(col) / len(col) if col else 0.0
        v = (sum((x - m) ** 2 for x in col) / len(col)) ** 0.5 if col else 1.0
        means.append(m)
        stds.append(v if v > 1e-9 else 1.0)
    X = [[((row[j] if row[j] is not None else means[i]) - means[i]) / stds[i]
          for i, j in enumerate(keep)] for row in raw]
    diag["keep_idx"] = keep
    return X, y, means, stds, diag


def _fit_logistic(X, y, iters=400, lr=0.05, l2=0.01):
    """Plain gradient-ascent logistic regression with L2. Returns
    (bias, weights). Small, inspectable, dependency-free."""
    import math as m
    n, k = len(X), len(X[0])
    b, w = 0.0, [0.0] * k
    for _ in range(iters):
        gb, gw = 0.0, [0.0] * k
        for xi, yi in zip(X, y):
            z = b + sum(wi * xij for wi, xij in zip(w, xi))
            z = max(-30.0, min(30.0, z))
            p = 1.0 / (1.0 + m.exp(-z))
            err = yi - p
            gb += err
            for j in range(k):
                gw[j] += err * xi[j]
        b += lr * gb / n
        for j in range(k):
            w[j] += lr * (gw[j] / n - l2 * w[j])
    return b, w


CALIB_MIN_AUC = 0.55         # below this the ranking is a coin flip
CALIB_HOLDOUT_FRAC = 0.30    # last 30% of rows, chronologically, never trained on


def _predict(X, b, w):
    import math as _m
    out = []
    for xi in X:
        z = max(-30.0, min(30.0, b + sum(wi * xij for wi, xij in zip(w, xi))))
        out.append(1.0 / (1.0 + _m.exp(-z)))
    return out


def _auc(y, p):
    """Rank AUC (Mann-Whitney), tie-corrected. 0.5 = no discrimination.

    This — not accuracy — is the right question for an imbalanced target.
    At a 13% base rate a model can rank winners above losers perfectly and
    STILL never emit p>=0.5, scoring identically to 'always predict loser'.
    Accuracy cannot see that; AUC can."""
    n_pos = int(sum(y))
    n_neg = len(y) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    pairs = sorted(zip(p, y))
    rank_sum_pos, i, n = 0.0, 0, len(pairs)
    while i < n:                      # average ranks within tie groups
        j = i
        while j + 1 < n and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            if pairs[k][1] == 1.0:
                rank_sum_pos += avg_rank
        i = j + 1
    return (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _log_loss(y, p):
    """Mean negative log-likelihood — measures whether the PROBABILITIES are
    honest, not just whether the ranking is. A model can rank well and still
    be systematically overconfident; this is what catches that."""
    import math as _m
    eps = 1e-12
    return -sum(yi * _m.log(max(pi, eps)) + (1 - yi) * _m.log(max(1 - pi, eps))
                for yi, pi in zip(y, p)) / len(y)


def derive_calibration(rows) -> dict:
    """Fit the score→probability model if (and only if) the data earns it.

    HOW 'EARNS IT' IS JUDGED — and why this changed
    -----------------------------------------------
    The original check compared in-sample accuracy against the majority-class
    baseline. On the first real run that check fired: n=2313, 312 winners /
    2001 losers, accuracy 0.8651 vs baseline 0.8651 — the model had learned
    to answer 'loser' every time. But the check ALSO wrote active=True, so a
    model with zero discriminative power was handed to scenario_engine's
    Tier 2, where it overrode the up-probability on every generated scenario.
    A gate that detects a problem and then proceeds anyway is not a gate.

    Two things are fixed here:
      1. THE GATE IS ENFORCED. active is now the verdict, not a constant.
      2. THE TEST IS APPROPRIATE. Accuracy-vs-majority is the wrong question
         at a 13% base rate — a genuinely useful model would fail it forever.
         Judged instead on:
           * AUC on a HOLDOUT — can it rank unseen winners above losers?
           * log-loss vs a constant base-rate predictor on the same holdout —
             are the probabilities themselves worth more than 'always say 13%'?
         The holdout is the last 30% of rows CHRONOLOGICALLY. Random splits
         leak on this data: the same ticker recurs across scans, so a random
         split trains and tests on the same episode. Time-ordered does not.

    Every metric is stored whether it passes or fails, so a rejected fit can
    still be inspected instead of silently vanishing."""
    X, y, means, stds, diag = _calib_matrix(rows)
    n = len(y)
    pos = int(sum(y))
    out = {"active": False, "n": n, "winners": pos, "losers": n - pos,
           "target": f"return_10d > +{CALIB_TARGET_RET:.0f}%",
           "data_quality": diag}
    if diag.get("fatal"):
        out["gate"] = f"data quality: {diag['fatal']}"
        return out
    if n < CALIB_MIN_ROWS or pos < CALIB_MIN_CLASS or (n - pos) < CALIB_MIN_CLASS:
        out["gate"] = (f"needs {CALIB_MIN_ROWS}+ rows with {CALIB_MIN_CLASS}+ "
                       f"each side (have {n}: {pos}W/{n-pos}L)")
        return out

    # ── chronological holdout, GROUP-AWARE ──
    # The same ticker recurs across scans — up to 89 times for one name, with
    # 2,419 rows collapsing to ~694 independent (ticker, 5-day) episodes. A
    # plain chronological cut lets one episode straddle the boundary, so the
    # model is tested on rows whose near-duplicates it trained on, and the
    # holdout score reads far more confident than the evidence supports.
    # Chasing an apparent signal inversion through three rounds of analysis
    # traces directly back to that overcounting.
    #
    # So: cut on the episode boundary, and never let one episode land on both
    # sides. Effective sample size is also reported, because "n=2419" invites
    # exactly the overconfidence this is correcting.
    groups = diag.get("episodes") or []
    cut = int(n * (1.0 - CALIB_HOLDOUT_FRAC))
    if groups and len(groups) == n:
        # walk the cut forward until the group changes, so no episode splits
        while cut < n - 1 and groups[cut] == groups[cut - 1]:
            cut += 1
        out["effective_n"] = len(set(groups))
        out["rows_per_episode"] = round(n / max(len(set(groups)), 1), 2)
    Xtr, ytr, Xte, yte = X[:cut], y[:cut], X[cut:], y[cut:]
    if (not yte or sum(yte) < 5 or (len(yte) - sum(yte)) < 5
            or not ytr or sum(ytr) < 5):
        out["gate"] = ("holdout has too few of one class to test honestly "
                       f"(train {int(sum(ytr))}W/{len(ytr) - int(sum(ytr))}L, "
                       f"test {int(sum(yte))}W/{len(yte) - int(sum(yte))}L)")
        return out

    b_tr, w_tr = _fit_logistic(Xtr, ytr)
    p_te = _predict(Xte, b_tr, w_tr)
    auc = _auc(yte, p_te)
    ll = _log_loss(yte, p_te)
    base_rate_tr = sum(ytr) / len(ytr)
    ll_base = _log_loss(yte, [base_rate_tr] * len(yte))

    beats_rank = auc is not None and auc >= CALIB_MIN_AUC
    beats_prob = ll < ll_base
    earned = bool(beats_rank and beats_prob)

    out.update({
        "base_rate": round(pos / n, 4),
        "holdout_n": len(yte),
        "holdout_auc": (round(auc, 4) if auc is not None else None),
        "holdout_log_loss": round(ll, 5),
        "baseline_log_loss": round(ll_base, 5),
        "beats_baseline": earned,
        # ASCII only: this string gets printed to cp1252 consoles and a
        # stray '>=' glyph would raise UnicodeEncodeError mid-report.
        "verdict": (f"AUC {auc:.3f} (need >={CALIB_MIN_AUC}), "
                    f"log-loss {ll:.4f} vs base-rate {ll_base:.4f}"
                    if auc is not None else "AUC unavailable"),
    })
    if not earned:
        out["gate"] = ("fit does not beat a constant base-rate predictor on "
                       "unseen rows — Tier 2 stays OFF and scenarios keep "
                       "using the honest heuristic. " + out["verdict"])
        return out

    # Earned it: refit on ALL rows so the deployed model uses every sample,
    # and keep the holdout metrics above as the evidence that it qualified.
    # (cohort comparison is attached by derive_calibration's caller below)
    b, w = _fit_logistic(X, y)
    used = diag["features_used"]
    out.update({
        "active": True,
        "bias": round(b, 5),
        # Keyed by the features that SURVIVED the presence floor — never by
        # the full CALIB_FEATURES list, or the weights would silently
        # misalign with the columns they were fit on.
        "features_used": used,
        "weights": {used[i]: round(w[i], 5) for i in range(len(w))},
        "means": [round(m, 5) for m in means],
        "stds": [round(s, 5) for s in stds],
    })
    return out


def calibrated_probability(features: dict, calib: dict):
    """Apply a fitted calibration to one candidate. features uses the
    CALIB_FEATURES names; missing values impute to the training mean.
    Returns probability clamped to [0.01, 0.95], or None if inactive."""
    if not calib or not calib.get("active"):
        return None
    import math as m
    try:
        # Must mirror the fit exactly: the retained feature list, in order.
        # Falls back to the full list only for params written before feature
        # gating existed.
        names = calib.get("features_used") or [f[0] for f in CALIB_FEATURES]
        means, stds = calib["means"], calib["stds"]
        z = calib["bias"]
        for j, name in enumerate(names):
            v = features.get(name)
            if v is None:
                v = means[j]
            z += calib["weights"][name] * (v - means[j]) / stds[j]
        z = max(-30.0, min(30.0, z))
        p = 1.0 / (1.0 + m.exp(-z))
        return round(max(0.01, min(0.95, p)), 4)
    except Exception:
        return None


# ─────────────────────────────────────────────
# MAIN UPDATE + CLI
# ─────────────────────────────────────────────

def update_from_logs(grade_stock: bool = True) -> dict:
    """The weekly command. Grades stock log (squeeze grading stays in
    review_outcomes.py), learns from both, writes learned_params.json."""
    if grade_stock:
        try:
            n = fill_stock_outcomes()
            print(f"  stock_log outcomes filled: {n} rows")
        except ImportError:
            print("  (yfinance unavailable — stock grading skipped)")
        except Exception as e:
            print(f"  (stock grading error: {e})")

    sq_rows = _graded_squeeze_rows()
    st_rows = _graded_stock_rows()
    if _LAST_UNITS_NOTE.get("n"):
        print(f"  return units [{_LAST_UNITS_NOTE['col']}]: "
              f"{_LAST_UNITS_NOTE['verdict']} "
              f"(n={_LAST_UNITS_NOTE['n']}, "
              f"price-recomputed={_LAST_UNITS_NOTE['price_recomputed']}, "
              f"x100-converted={_LAST_UNITS_NOTE['converted_x100']})")

    params = {
        "version": 1,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "return_units": _LAST_UNITS_NOTE,
        "squeeze": derive_squeeze_params(analyze_squeeze(sq_rows)) if sq_rows
                   else {"n_samples": 0, "active": False},
        "stock": derive_stock_params(analyze_stock(st_rows)) if st_rows
                 else {"n_samples": 0, "active": False},
        "calibration": derive_calibration(sq_rows) if sq_rows
                       else {"active": False, "n": 0},
        # Clean-cohort comparison: the standing test of whether the signal is
        # inverted or was merely starved of inputs. Populates itself as rows
        # logged after the data fixes accumulate.
        "cohort": cohort_comparison(sq_rows) if sq_rows else {},
        # Two measurements the options layer reads at decision time.
        "vol_premium": derive_vol_premium(sq_rows) if sq_rows else {},
        "verdicts": derive_verdict_stats(sq_rows) if sq_rows else {},
        # Calendar-time shape kept for reference; the EVENT-TIME one is what
        # the overlay draws, because only that preserves the asymmetry.
        "path_shape": derive_path_shape(sq_rows) if sq_rows else {},
        "path_shape_event": derive_path_shape_event(sq_rows) if sq_rows else {},
    }
    _save_params(params)
    return params


def print_report():
    p = load_params()
    if not p:
        print("No learned_params.json yet — run: python learning_engine.py update")
        return
    print("=" * 64)
    print("  LEARNING ENGINE — current state")
    print(f"  generated: {p.get('generated_at', '?')}")
    print("=" * 64)
    sq = p.get("squeeze", {})
    print(f"\n  SQUEEZE  n={sq.get('n_samples', 0)}  "
          f"blend={sq.get('blend', 0)}  active={sq.get('active', False)}")
    for w, s in sq.get("catalyst_window_scale", {}).items():
        ev = sq.get("evidence", {}).get("windows", {}).get(w, {})
        print(f"    {w:<12} scale ×{s}   "
              f"(n={ev.get('n', '?')}, excess {ev.get('excess', '?'):+}% 10d)")
    if sq.get("active"):
        print(f"    conviction effect scale ×{sq.get('conviction_effect_scale', 1.0)}")
    st = p.get("stock", {})
    print(f"\n  STOCK    n={st.get('n_samples', 0)}  "
          f"blend={st.get('blend', 0)}  active={st.get('active', False)}")
    for comp, s in st.get("weight_scale", {}).items():
        c = st.get("evidence", {}).get("component_corr", {}).get(comp, "?")
        print(f"    {comp:<20} weight ×{s}   (corr {c})")
    cal = p.get("calibration", {})
    print(f"\n  CALIBRATION  n={cal.get('n', 0)}  active={cal.get('active', False)}")
    if cal.get("active"):
        print(f"    base rate {cal['base_rate']:.0%} | "
              f"target {cal.get('target')}")
        for k, v in cal.get("weights", {}).items():
            print(f"    {k:<18} {v:+.3f}")
    elif cal.get("gate"):
        print(f"    gated: {cal['gate']}")
    if not sq.get("active") and not st.get("active"):
        print("\n  Gated: adjustments begin at "
              f"{MIN_SAMPLES} graded rows per system. Keep scanning weekly.")


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1].lower() if len(sys.argv) > 1 else "report"
    if cmd == "update":
        p = update_from_logs()
        print_report()
    elif cmd == "reset":
        if os.path.exists(PARAMS_FILE):
            os.remove(PARAMS_FILE)
        print("learned_params.json removed — scanners back to baselines")
    else:
        print_report()
