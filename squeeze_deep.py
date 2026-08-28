"""
squeeze_deep.py
================
Stage-2 deep squeeze analysis. Runs ONLY on the top finalists from the
Stage-1 composite scan. Measures whether a squeeze is actively igniting
RIGHT NOW (velocity) rather than just whether static metrics look good.

Three free-data signals:
  1. Options Chain Convexity  — OTM call OI concentration + 1-week skew shift
  2. CTB Velocity             — 1-week delta in cost-to-borrow proxy (own snapshots)
  3. FTD Accumulation         — rolling sum + trend of SEC fails-to-deliver

Final deep score is ranked:  Probability → Imminence → Magnitude.

Snapshots are stored in  squeeze_snapshots.json  in the working directory.
No paid APIs. yfinance + SEC + local history only.
"""

import json
import os
import math
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

import yfinance as yf
import numpy as np

SNAPSHOT_FILE = "squeeze_snapshots.json"

# ─────────────────────────────────────────────
# SCORING CONFIG (v2 — effective float + volume)
# ─────────────────────────────────────────────
# Changing a scoring gate makes every previously graded row a description of
# a scorer that no longer exists. Two things keep that honest here:
#
#   1. `scoring_version` is stamped on every logged row, so the grader can
#      separate v1 rows from v2 rows instead of averaging two different
#      scorers into one meaningless number.
#   2. The v1 FTD score and impact factor are computed ALONGSIDE v2 and
#      logged too. That turns the question "is v2 better" into an A/B on
#      identical rows, which is real evidence — far stronger than comparing
#      old rows to new rows, where the market also changed underneath.
#
# Flip `use_effective_float` to false in scoring_config.json to run pure v1.

SCORING_CONFIG_FILE = "scoring_config.json"

_SCORING_DEFAULTS = {
    "scoring_version":     2,
    "use_effective_float": True,
    # Days-of-average-volume ramp for the FTD close-out. Below the floor the
    # forced buying disappears into normal turnover; at/above full it is a
    # day's worth of demand that has to be sourced from somewhere.
    "ftd_vol_floor_days":  0.10,
    "ftd_vol_full_days":   1.00,
    # Read SI/DTC trends off the exchange settlement series instead of 7-day
    # local snapshots. See analyze_conviction_matrix for the measurement that
    # motivated this.
    "use_settlement_trends": True,
    # Effective-float buckets for the magnitude pillar (shares).
    "eff_float_buckets":   [10e6, 25e6, 50e6, 150e6],
}

_SCORING_CFG = None


def _scoring_cfg() -> dict:
    """Scoring knobs, cached. Missing/broken file = defaults, never a crash."""
    global _SCORING_CFG
    if _SCORING_CFG is None:
        cfg = dict(_SCORING_DEFAULTS)
        try:
            import json as _j
            import os as _os
            _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                               SCORING_CONFIG_FILE)
            with open(_p, encoding="utf-8") as f:
                cfg.update({k: v for k, v in _j.load(f).items()
                            if k in _SCORING_DEFAULTS})
        except (OSError, ValueError):
            pass
        _SCORING_CFG = cfg
    return _SCORING_CFG


# ─────────────────────────────────────────────
# RESULT STRUCTURE
# ─────────────────────────────────────────────

@dataclass
class DeepSqueezeResult:
    ticker:              str = ""
    current_price:       Optional[float] = None

    # ── Options convexity ──
    otm_call_oi:         Optional[int]   = None   # total OI on OTM calls 10-30% above
    otm_call_ratio:      Optional[float] = None   # OTM call OI / total call OI
    call_put_oi_ratio:   Optional[float] = None   # total call OI / total put OI
    options_data_available: bool = False          # chains actually fetched

    # ── Implied move (market-priced catalyst impact) ──
    implied_move_pct:    Optional[float] = None   # ATM straddle / spot
    implied_move_expiry: str = ""                 # expiry used

    # ── Dealer gamma exposure (amplification variable) ──
    gex_net_musd:        Optional[float] = None   # net $GEX per 1% move ($M)
    gex_wall_strike:     Optional[float] = None   # largest gamma strike above spot
    gex_regime:          str = ""                 # AMPLIFYING / DAMPENING / label

    # ── Gamma flip / zero-gamma level (ANALYZER-ONLY: display + CSV) ──
    # The hypothetical underlying price where net dealer GEX changes sign
    # — short-gamma below (hedging amplifies moves), long-gamma above
    # (hedging dampens). Computed on demand ONLY when run_deep_analysis is
    # called with_gamma_flip=True (the analyzer does; the bulk searcher
    # does not), so these stay None on the searcher path and the scored
    # output is unchanged. No scoring impact — context only.
    gamma_flip_price:    Optional[float] = None   # interpolated zero-gamma price
    gamma_flip_pct:      Optional[float] = None   # signed % from spot to flip
    gamma_flip_regime:   str = ""                 # SHORT-GAMMA NOW / LONG-GAMMA NOW + note

    # ── FINRA daily short-volume nowcast (SI freshness fix) ──
    svr_recent:          Optional[float] = None   # 3-day avg short-vol ratio
    svr_baseline:        Optional[float] = None   # ~10-day baseline ratio
    svr_trend:           str = ""                 # PRESSING / STEADY / COVERING
    svr_score:           float = 0.0              # 0-100 nowcast signal
    svr_available:       bool = False

    # ── T+35 FTD close-out projection (mechanical catalyst) ──
    ftd_closeout_date:   str = ""                 # projected forced-buy window
    ftd_closeout_days:   Optional[int] = None
    ftd_impact_factor:   float = 0.0   # 0..1 — closeout weight by %float     # signed days from today
    catalyst_type:       str = ""                 # WHICH event drives the window:
                                                  # EARNINGS/FDA/READOUT/FTD_CLOSEOUT/...
    # Calibrated probability (Phase 5.1) — None until the model earns
    # activation at 150+ graded rows. When set: P(return_10d > +15%).
    calibrated_prob:     Optional[float] = None
    convexity_strike:    Optional[float] = None   # strike with max OTM OI concentration
    convexity_skew_1w:   Optional[float] = None   # change in OTM call ratio vs snapshot
    convexity_score:     float = 0.0              # 0-100

    # ── CTB velocity ──
    ctb_now:             Optional[float] = None
    ctb_1w_ago:          Optional[float] = None
    ctb_velocity:        Optional[float] = None   # (now - 1w) absolute delta
    ctb_velocity_pct:    Optional[float] = None   # relative change
    ctb_velocity_score:  float = 0.0              # 0-100

    # ── FTD accumulation ──
    ftd_total_recent:    Optional[int]   = None   # sum FTD over last N cycles
    ftd_trend:           str = ""                  # RISING / FLAT / FALLING
    ftd_pct_float_accum: Optional[float] = None
    ftd_score:           float = 0.0              # 0-100

    # ── Float / effective float (REPORTED, NOT SCORED) ──
    # float_shares was previously attached ad hoc by run_deep_analysis and only
    # on the path where stage-1 metrics were passed in; declaring it here means
    # the magnitude pillar's float bucket sees a value on both paths.
    float_shares:            Optional[int]   = None
    effective_float:         Optional[float] = None  # float less locked stock
    float_tightness:         Optional[float] = None  # float / effective float
    avg_daily_volume:        Optional[float] = None
    ftd_pct_eff_float_accum: Optional[float] = None  # fails vs tradeable float
    ftd_closeout_adv_days:   Optional[float] = None  # fails / avg daily volume
    ftd_closeout_verdict:    str = ""                # NOISE/NOTABLE/HEAVY/EXTREME

    # ── A/B: v1 scores kept alongside v2 so the change is testable ──
    scoring_version:        int = 1
    ftd_score_v1:           float = 0.0
    ftd_impact_factor_v1:   float = 0.0
    magnitude_score_v1:     float = 0.0
    probability_score_v1:   float = 0.0
    deep_score_v1:          float = 0.0

    # ── CONVICTION MATRIX (CTB vel + DTC trend + SI trend) ──
    dtc_now:             Optional[float] = None
    dtc_1w_ago:          Optional[float] = None
    dtc_trend:           str = ""                  # TIGHTENING / LOOSENING / FLAT
    si_now:              Optional[float] = None
    si_1w_ago:           Optional[float] = None
    si_trend:            str = ""                  # ADDING / COVERING / FLAT

    # ── DTC by named volume window + settlement-cadence trends ──
    dtc_exchange:        Optional[float] = None    # 10-session mean (official)
    dtc_robust:          Optional[float] = None    # 10-session MEDIAN
    dtc_60d:             Optional[float] = None    # long horizon
    dtc_spread_low:      Optional[float] = None
    dtc_spread_high:     Optional[float] = None
    dtc_spike_ratio:     Optional[float] = None    # 10d mean / 10d median
    si_change_settlement: Optional[float] = None
    settlement_date:     str = ""
    settlement_age_days: Optional[int] = None
    settlement_consecutive: int = 0
    si_trend_source:     str = ""                  # settlement | snapshot_7d
    si_trend_v1:         str = ""                  # what the 7d snapshot said
    dtc_trend_v1:        str = ""
    ctb_trend:           str = ""                  # RISING / FALLING / FLAT
    conviction_state:    str = ""                  # the matrix readout
    conviction_mult:     float = 1.0               # multiplier applied to combined

    # ── CATALYST TIMING (the proven-edge layer) ──
    catalyst_window:     str = ""                  # SWEET_SPOT / PASSED / etc
    catalyst_score:      float = 0.0               # 0-100 timing quality
    catalyst_note:       str = ""
    days_to_earnings:    Optional[int] = None
    catalyst_mult:       float = 1.0               # timing multiplier on final

    final_score:         float = 0.0               # combined × conviction × catalyst

    # ── Composite ──
    probability_score:   float = 0.0   # how confident the setup is
    imminence_score:     float = 0.0   # how soon it could fire
    magnitude_score:     float = 0.0   # how big it could be
    deep_score:          float = 0.0   # final blended 0-100
    deep_verdict:        str = ""       # IGNITING / BUILDING / DORMANT / TRAP

    # price-action (the system was blind to a +50% run before these)
    ret_5d:              Optional[float] = None
    ret_20d:             Optional[float] = None
    rel_volume:          Optional[float] = None
    momentum_score:      float = 0.0    # 0-100 thrust detector
    momentum_available:  bool = False

    flags:               list = field(default_factory=list)
    warnings:            list = field(default_factory=list)


def _squeeze_severity(si_pct, ctb_pct) -> float:
    """0..1 fuel-severity from LEVELS (not trends): how extreme are short
    interest and cost-to-borrow right now? 0 below SI 25% / CTB 30%;
    saturates at SI 70% / CTB 120%. This is what lets the matrix tell
    'FALLING from 109%' apart from 'FALLING from 8%'."""
    s, n = 0.0, 0
    if si_pct is not None:
        s += min(max((si_pct - 0.25) / 0.45, 0.0), 1.0)
        n += 1
    if ctb_pct is not None:
        s += min(max((float(ctb_pct) - 30.0) / 90.0, 0.0), 1.0)
        n += 1
    return round(s / n, 3) if n else 0.0


# ── SEVERITY-FORCED DEEP DIVE (improvement #2) ──
# Stage-1 is saturated (8-pt band across the top 12), so the deep-dive
# gate in the searcher is nearly rank-random among leaders — and 372/397
# names never reach the ACTIVE SQUEEZE detector at all. Any name with
# extreme fuel LEVELS must be force-included in stage 2 regardless of
# stage-1 rank. The searcher calls this per candidate:
#     from squeeze_deep import force_deep_dive
#     if r["combined"] >= cutoff or force_deep_dive(r.get("si_pct"),
#                                                   r.get("ctb")): ...
FORCE_DEEP_SI = 0.40      # 40%+ short interest always deep-dives
FORCE_DEEP_CTB = 60.0     # 60%+ cost-to-borrow always deep-dives


def force_deep_dive(si_pct, ctb) -> bool:
    """True if fuel levels alone demand a deep dive (stage-1 rank be
    damned). Cheap, pure, no network."""
    try:
        if si_pct is not None and float(si_pct) >= FORCE_DEEP_SI:
            return True
        if ctb is not None and float(ctb) >= FORCE_DEEP_CTB:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _price_momentum(result) -> None:
    """Improvement #4: the one signal the system never had — price
    action. Computes 5d/20d returns + relative volume from a 1-month
    history (throttle-cached, VOLATILE TTL) and scores THRUST 0-100:
      * 5d return ramps 0→50 pts across 0%→+15%  (spike ignition)
      * 20d return ramps 0→30 pts across 0%→+35% (sustained run —
        a GRPN-style 3-week grind must register, not just spikes)
      * relative volume (last 5d vs month) ramps 0→20 pts, 1x→2.5x
    One-sided by design — it detects ignition, it does not penalize
    quiet names (downside pressure is other signals' job). Failure is
    a logged warning, never a silent pass."""
    tk = getattr(result, "ticker", None)
    if not tk:
        return
    try:
        import yfinance as yf
        h = yf.Ticker(tk).history(period="1mo")
        if h is None or len(h) < 8:
            result.warnings.append("price history unavailable — momentum "
                                   "signal skipped")
            return
        closes = [float(x) for x in h["Close"]]
        vols = [float(x) for x in h["Volume"]]
        result.ret_5d = closes[-1] / closes[-6] - 1.0
        result.ret_20d = closes[-1] / closes[0] - 1.0
        avg_vol = sum(vols) / len(vols)
        if avg_vol > 0:
            result.rel_volume = (sum(vols[-5:]) / 5.0) / avg_vol
        pts = 0.0
        pts += min(max(result.ret_5d / 0.15, 0.0), 1.0) * 50.0
        pts += min(max(result.ret_20d / 0.35, 0.0), 1.0) * 30.0
        if result.rel_volume is not None:
            pts += min(max((result.rel_volume - 1.0) / 1.5, 0.0), 1.0) * 20.0
        result.momentum_score = round(pts, 1)
        result.momentum_available = True
        if pts >= 55:
            result.flags.append(
                f"🚀 PRICE THRUST — 5d {result.ret_5d:+.0%}, 20d "
                f"{result.ret_20d:+.0%}, rel-vol "
                f"{(result.rel_volume or 0):.1f}x (momentum "
                f"{result.momentum_score:.0f}/100)")
    except Exception as e:
        result.warnings.append(f"momentum fetch failed: {e}")


# ─────────────────────────────────────────────
# SNAPSHOT PERSISTENCE
# ─────────────────────────────────────────────

def _load_snapshots() -> dict:
    if not os.path.exists(SNAPSHOT_FILE):
        return {}
    try:
        with open(SNAPSHOT_FILE, "r") as f:
            snaps = json.load(f)
    except Exception:
        return {}
    if not isinstance(snaps, dict):
        return {}
    # ── SELF-HEALING MIGRATION (June 2026) ──
    # Older files contain multiple PARTIAL entries per ticker per day
    # (the append bug: options/ctb/conviction each stacked their own
    # fragment). Merge same-date fragments on load so existing history
    # immediately becomes usable by the 1-week trend lookup instead of
    # waiting 30 days for the bad entries to age out.
    for ticker, entries in snaps.items():
        if not isinstance(entries, list) or len(entries) < 2:
            continue
        by_date = {}
        order = []
        for e in entries:
            d = e.get("date")
            if d is None:
                continue
            if d not in by_date:
                by_date[d] = dict(e)
                order.append(d)
            else:
                # later fragments fill gaps; None never overwrites real data
                for k, v in e.items():
                    if v is not None:
                        by_date[d][k] = v
        snaps[ticker] = [by_date[d] for d in order]
    return snaps


def _save_snapshot(ticker: str, data: dict):
    """Merge `data` into TODAY's snapshot for this ticker.

    CRITICAL FIX (June 2026): the old version APPENDED a new entry on
    every call. One scan calls this 3+ times per ticker (options saves
    only OTM fields, CTB velocity saves only ctb, conviction saves
    ctb/dtc/si) — so each scan stacked 3 PARTIAL entries. The 1-week
    lookup then grabbed whichever single entry was closest to 7 days
    old — often an options-only entry with NO ctb/dtc/si keys — so the
    conviction matrix read None for every trend, classified everything
    FLAT, and the entire velocity layer silently flatlined at 1.0x.

    Now: one merged entry per ticker per day. None values never
    overwrite real data (an options outage can't erase a good reading
    saved earlier the same day). Write is atomic (temp + fsync +
    os.replace) so an interrupted scan can't corrupt history.
    """
    snaps = _load_snapshots()
    if ticker not in snaps:
        snaps[ticker] = []
    today = datetime.now().strftime("%Y-%m-%d")
    clean = {k: v for k, v in data.items() if v is not None}

    entry = None
    for s in snaps[ticker]:
        if s.get("date") == today:
            entry = s
            break
    if entry is not None:
        entry.update(clean)
    else:
        snaps[ticker].append({"date": today, **clean})

    # Keep only last 30 snapshots per ticker
    snaps[ticker] = snaps[ticker][-30:]
    try:
        tmp = f"{SNAPSHOT_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w") as f:
            json.dump(snaps, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SNAPSHOT_FILE)
    except Exception:
        pass


def _get_snapshot_n_days_ago(ticker: str, target_days: int = 7,
                              max_age: int = None) -> Optional[dict]:
    """Find the snapshot closest to `target_days` ago.

    Bounded: only snapshots between 3 days and `max_age` (default 3x the
    target) qualify. Previously there was NO upper bound — a 30-day-old
    snapshot would silently serve as the "1-week-ago" baseline, producing
    garbage trends with no warning. The returned dict now includes
    "_age_days" so callers can surface how fresh the comparison really is.
    """
    if max_age is None:
        max_age = target_days * 3
    snaps = _load_snapshots().get(ticker, [])
    if not snaps:
        return None
    today = datetime.now()
    best = None
    best_diff = 999
    for s in snaps:
        try:
            d = datetime.strptime(s["date"], "%Y-%m-%d")
            age = (today - d).days
            diff = abs(age - target_days)
            # accept only a real delta (>=3d) that is not stale (<=max_age)
            if 3 <= age <= max_age and diff < best_diff:
                best_diff = diff
                best = dict(s)
                best["_age_days"] = age
        except Exception:
            continue
    return best


# ─────────────────────────────────────────────
# PURE-MATH HELPERS (no network — unit-testable)
# ─────────────────────────────────────────────
import math as _math


def _bs_gamma(S: float, K: float, T: float, sigma: float,
              r: float = 0.04) -> float:
    """Black-Scholes gamma. T in years."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    try:
        d1 = ((_math.log(S / K) + (r + 0.5 * sigma * sigma) * T)
              / (sigma * _math.sqrt(T)))
        pdf = _math.exp(-0.5 * d1 * d1) / _math.sqrt(2 * _math.pi)
        return pdf / (S * sigma * _math.sqrt(T))
    except (ValueError, ZeroDivisionError, OverflowError):
        return 0.0


def _mid_price(row: dict) -> Optional[float]:
    """Best-effort option mid from bid/ask, falling back to last."""
    bid, ask = row.get("bid"), row.get("ask")
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    last = row.get("lastPrice")
    return last if last and last > 0 else None


def _implied_move_from_rows(spot: float, call_rows: list,
                             put_rows: list) -> Optional[float]:
    """ATM straddle / spot from plain row dicts (strike/bid/ask/lastPrice).
    The market's own forecast of the move through this expiry — i.e. the
    priced CATALYST IMPACT."""
    if not spot or not call_rows or not put_rows:
        return None
    atm_c = min(call_rows, key=lambda r: abs(r.get("strike", 1e12) - spot))
    atm_p = min(put_rows,  key=lambda r: abs(r.get("strike", 1e12) - spot))
    # Strikes should bracket spot reasonably; reject junk chains
    if (abs(atm_c.get("strike", 0) - spot) / spot > 0.15
            or abs(atm_p.get("strike", 0) - spot) / spot > 0.15):
        return None
    cm, pm = _mid_price(atm_c), _mid_price(atm_p)
    if cm is None or pm is None:
        return None
    return (cm + pm) / spot


def _gex_from_rows(spot: float, expiries: list) -> tuple:
    """Net dealer gamma exposure from plain rows.

    expiries: list of (T_years, call_rows, put_rows); rows are dicts with
    strike, openInterest, impliedVolatility (+bid/ask unused here).

    Convention (SqueezeMetrics-style): calls contribute POSITIVE gamma,
    puts NEGATIVE — i.e. the classic assumption that dealers are long
    customer-sold calls and short customer-bought puts. Returns
    (net_dollar_gex_per_1pct_in_$M, wall_strike_above_spot).
    CAVEAT handled by caller: in extreme call-skew names that assumption
    inverts (speculative call BUYING puts dealers SHORT gamma)."""
    if not spot or not expiries:
        return None, None
    net = 0.0
    wall_val, wall_strike = 0.0, None
    for T, calls, puts in expiries:
        if T <= 0:
            T = 1.0 / 365.0
        for rows, sign in ((calls, +1.0), (puts, -1.0)):
            for r in rows:
                K = r.get("strike") or 0
                oi = r.get("openInterest") or 0
                iv = r.get("impliedVolatility") or 0
                if not K or not oi or not iv:
                    continue
                if abs(K - spot) / spot > 0.25:
                    continue            # gamma is local; far strikes ~0
                g = _bs_gamma(spot, K, T, iv)
                dollar = g * oi * 100 * spot * spot * 0.01  # per 1% move
                net += sign * dollar
                if sign > 0 and K >= spot and dollar > wall_val:
                    wall_val, wall_strike = dollar, K
    return net / 1e6, wall_strike


def _net_gex_at_price(S: float, expiries: list) -> float:
    """Net dealer $GEX (per 1% move, in $M) evaluated at a HYPOTHETICAL
    underlying price S. Identical machinery to _gex_from_rows — same
    +call / -put convention, same per-strike dollar-gamma formula, same
    ±25% locality window (re-centred on S, since the relevant strikes
    shift as the hypothetical spot moves). Used to trace the gamma
    profile across price and locate the flip. No `wall` bookkeeping here;
    we only need the signed net."""
    if S <= 0 or not expiries:
        return 0.0
    net = 0.0
    for T, calls, puts in expiries:
        if T <= 0:
            T = 1.0 / 365.0
        for rows, sign in ((calls, +1.0), (puts, -1.0)):
            for r in rows:
                K = r.get("strike") or 0
                oi = r.get("openInterest") or 0
                iv = r.get("impliedVolatility") or 0
                if not K or not oi or not iv:
                    continue
                if abs(K - S) / S > 0.25:        # gamma is local — same gate as _gex_from_rows
                    continue
                g = _bs_gamma(S, K, T, iv)
                net += sign * g * oi * 100 * S * S * 0.01   # per 1% move
    return net / 1e6


def _gamma_flip_from_rows(spot: float, expiries: list,
                           span: float = 0.30, steps: int = 61) -> Optional[float]:
    """Locate the gamma flip (zero-gamma) underlying price: the level
    where net dealer GEX changes sign. Scans a grid of ±`span` around
    spot (default ±30%, 61 nodes ≈ 1% resolution), evaluates
    _net_gex_at_price at each node, and linearly interpolates the zero
    crossing of the first sign change. If the profile crosses more than
    once (choppy chains), returns the crossing NEAREST spot — that's the
    regime boundary that governs hedging from here. Returns None if the
    profile never crosses zero inside the scanned band (i.e. one regime
    dominates ±span), which is itself meaningful and left to the caller
    to surface or ignore."""
    if not spot or spot <= 0 or not expiries:
        return None
    lo, hi = spot * (1.0 - span), spot * (1.0 + span)
    grid = [lo + (hi - lo) * i / (steps - 1) for i in range(steps)]
    prev_S = prev_g = None
    crossings = []
    for S in grid:
        g = _net_gex_at_price(S, expiries)
        if prev_g is not None and (prev_g < 0) != (g < 0):
            # opposite signs bracket a zero — interpolate it
            denom = (prev_g - g)
            frac = prev_g / denom if denom else 0.0
            crossings.append(prev_S + frac * (S - prev_S))
        prev_S, prev_g = S, g
    if not crossings:
        return None
    return min(crossings, key=lambda p: abs(p - spot))


def _score_svr(daily: list) -> tuple:
    """Score FINRA short-volume-ratio series.
    daily: chronological list of (date_str, short_vol, total_vol).
    Returns (recent3, baseline, trend_label, score)."""
    ratios = [(s / t) for _, s, t in daily if t and t > 0]
    if len(ratios) < 5:
        return None, None, "", 0.0
    recent = sum(ratios[-3:]) / 3
    base_pool = ratios[:-3] if len(ratios) > 6 else ratios[:max(3, len(ratios)//2)]
    baseline = sum(base_pool) / len(base_pool)
    delta = recent - baseline
    if recent >= 0.60 and delta >= 0.05:
        return recent, baseline, "PRESSING HARD", 90.0
    if delta >= 0.03:
        return recent, baseline, "PRESSING", 72.0
    if delta <= -0.05:
        return recent, baseline, "COVERING", 15.0
    if delta <= -0.02:
        return recent, baseline, "EASING", 30.0
    if recent >= 0.55:
        return recent, baseline, "ELEVATED-STEADY", 60.0
    return recent, baseline, "STEADY", 45.0


def _ftd_period_end(tag: str):
    """'202605a' -> 2026-05-15, '202605b' -> 2026-05-31 (month end)."""
    try:
        yr, mo, half = int(tag[:4]), int(tag[4:6]), tag[6]
        if half == "a":
            return datetime(yr, mo, 15).date()
        if mo == 12:
            return datetime(yr, 12, 31).date()
        return (datetime(yr, mo + 1, 1) - timedelta(days=1)).date()
    except Exception:
        return None


def _nyse_holidays(year: int) -> set:
    """Observed NYSE full-day holidays for a year (algorithmic, no deps)."""
    from datetime import date

    def observed(d):
        if d.weekday() == 5:
            return d - timedelta(days=1)   # Sat -> Fri
        if d.weekday() == 6:
            return d + timedelta(days=1)   # Sun -> Mon
        return d

    def nth_weekday(y, month, weekday, n):
        d = date(y, month, 1)
        return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))

    def last_weekday(y, month, weekday):
        d = (date(y, 12, 31) if month == 12
             else date(y, month + 1, 1) - timedelta(days=1))
        return d - timedelta(days=(d.weekday() - weekday) % 7)

    def easter(y):
        # Anonymous Gregorian computus
        a, b, c = y % 19, y // 100, y % 100
        d0, e = b // 4, b % 4
        f = (b + 8) // 25
        g = (b - f + 1) // 3
        h = (19 * a + b - d0 - g + 15) % 30
        i, k = c // 4, c % 4
        l = (32 + 2 * e + 2 * i - h - k) % 7
        m = (a + 11 * h + 22 * l) // 451
        month = (h + l - 7 * m + 114) // 31
        day = ((h + l - 7 * m + 114) % 31) + 1
        return date(y, month, day)

    return {
        observed(date(year, 1, 1)),          # New Year's Day
        nth_weekday(year, 1, 0, 3),          # MLK Day
        nth_weekday(year, 2, 0, 3),          # Washington's Birthday
        easter(year) - timedelta(days=2),    # Good Friday
        last_weekday(year, 5, 0),            # Memorial Day
        observed(date(year, 6, 19)),         # Juneteenth
        observed(date(year, 7, 4)),          # Independence Day
        nth_weekday(year, 9, 0, 1),          # Labor Day
        nth_weekday(year, 11, 3, 4),         # Thanksgiving
        observed(date(year, 12, 25)),        # Christmas
    }


def _next_trading_day(d):
    """Roll a date forward to the next NYSE trading day (d itself if open)."""
    hols = _nyse_holidays(d.year) | _nyse_holidays(d.year + 1)
    while d.weekday() >= 5 or d in hols:
        d += timedelta(days=1)
    return d


def _project_ftd_closeout(ftd_series: list) -> tuple:
    """From the SEC FTD series, project the T+35 forced close-out window of
    the LARGEST recent fail cluster. Returns (iso_date, signed_days).

    Accepts BOTH series shapes:
      old: (period_tag, shares)
      new: (period_tag, settlement_date_YYYYMMDD, shares)  <- data_validator fix

    Anchor priority: the REAL settlement date of the peak fail balance;
    falls back to the period-end estimate only if no settlement date exists.
    The raw anchor+35 calendar days is rolled FORWARD to the next NYSE
    trading day — an obligation landing on a weekend/holiday executes the
    next session (this was the Juneteenth/NVAX blind spot)."""
    if not ftd_series:
        return None, None
    peak = max(ftd_series, key=lambda p: p[-1])   # shares are last in both shapes
    tag = peak[0]
    sdate = peak[1] if len(peak) >= 3 else None
    anchor = None
    if sdate:
        try:
            anchor = datetime.strptime(str(sdate), "%Y%m%d").date()
        except (ValueError, TypeError):
            anchor = None
    if anchor is None:
        anchor = _ftd_period_end(tag)
    if anchor is None:
        return None, None
    closeout = _next_trading_day(anchor + timedelta(days=35))
    days = (closeout - datetime.now().date()).days
    return closeout.strftime("%Y-%m-%d"), days


# ─────────────────────────────────────────────
# SIGNAL 1: OPTIONS CHAIN CONVEXITY
# ─────────────────────────────────────────────

def analyze_options_convexity(ticker: str, current_price: float,
                                result: DeepSqueezeResult,
                                with_gamma_flip: bool = False):
    """
    Measure gamma-squeeze fuel: OTM call open interest concentration.
    A surge in OI at strikes 10-30% above spot is the dealer-hedging
    feedback loop that powers reflexive squeezes (GME/AMC fingerprint).
    """
    try:
        t = yf.Ticker(ticker)
        expiries = t.options
        if not expiries:
            result.warnings.append("No options chain available")
            return

        # Use the nearest 3 expiries (front-month gamma matters most)
        near_expiries = expiries[:3]

        total_call_oi = 0
        total_put_oi  = 0
        otm_call_oi   = 0
        strike_oi_map = {}   # strike -> OI for OTM calls

        otm_low  = current_price * 1.10   # 10% above spot
        otm_high = current_price * 1.30   # 30% above spot

        chain_rows = []   # (T_years, call_row_dicts, put_row_dicts, expiry_str)
        _now = datetime.now()
        for exp in near_expiries:
            try:
                chain = t.option_chain(exp)
            except Exception:
                continue

            calls = chain.calls
            puts  = chain.puts

            # Normalize to plain dicts for the GEX / implied-move helpers
            try:
                T_years = max(
                    (datetime.strptime(exp, "%Y-%m-%d") - _now).days, 1
                ) / 365.0
                c_rows = (calls[["strike", "openInterest",
                                 "impliedVolatility", "bid", "ask",
                                 "lastPrice"]]
                          .fillna(0).to_dict("records")
                          if calls is not None and not calls.empty else [])
                p_rows = (puts[["strike", "openInterest",
                                "impliedVolatility", "bid", "ask",
                                "lastPrice"]]
                          .fillna(0).to_dict("records")
                          if puts is not None and not puts.empty else [])
                chain_rows.append((T_years, c_rows, p_rows, exp))
            except Exception:
                pass

            if calls is not None and not calls.empty:
                total_call_oi += int(calls["openInterest"].fillna(0).sum())
                otm = calls[(calls["strike"] >= otm_low) &
                            (calls["strike"] <= otm_high)]
                if not otm.empty:
                    otm_call_oi += int(otm["openInterest"].fillna(0).sum())
                    for _, row in otm.iterrows():
                        k = float(row["strike"])
                        strike_oi_map[k] = strike_oi_map.get(k, 0) + int(
                            row["openInterest"] if not math.isnan(row["openInterest"]) else 0
                        )

            if puts is not None and not puts.empty:
                total_put_oi += int(puts["openInterest"].fillna(0).sum())

        result.otm_call_oi = otm_call_oi

        if total_call_oi > 0:
            result.otm_call_ratio = otm_call_oi / total_call_oi
        if total_put_oi > 0:
            result.call_put_oi_ratio = total_call_oi / total_put_oi

        # ── IMPLIED MOVE: the market's own forecast of catalyst impact ──
        # ATM straddle on the front expiry ≥2 days out (the expiry that
        # prices the imminent event). This is the "determine the catalyst
        # and its impact" number — what the options market expects.
        im_chain = next((c for c in chain_rows
                         if c[0] * 365 >= 2 and c[1] and c[2]),
                        chain_rows[0] if chain_rows else None)
        if im_chain and current_price:
            im = _implied_move_from_rows(current_price, im_chain[1], im_chain[2])
            if im is not None and 0 < im < 1.0:
                result.implied_move_pct = im
                result.implied_move_expiry = im_chain[3]
                result.flags.append(
                    f"Implied move ±{im:.1%} through {im_chain[3]} "
                    f"(ATM straddle — market-priced catalyst impact)")

        # ── DEALER GAMMA EXPOSURE: the amplification variable ──
        if chain_rows and current_price:
            gex, wall = _gex_from_rows(
                current_price, [(c[0], c[1], c[2]) for c in chain_rows[:2]])
            if gex is not None:
                result.gex_net_musd = round(gex, 3)
                result.gex_wall_strike = wall
                cp = result.call_put_oi_ratio
                if gex < 0:
                    result.gex_regime = "SHORT-GAMMA — dealer hedging AMPLIFIES moves"
                elif cp is not None and cp >= 3.0:
                    # The classic +call/−put convention assumes covered-call
                    # supply. A 3x+ call-skewed chain is speculative call
                    # BUYING → dealers likely SHORT those calls → the naive
                    # positive sign inverts. Flag honestly.
                    result.gex_regime = ("CALL-SKEW OVERRIDE — naive GEX positive "
                                          "but extreme call buying implies dealers "
                                          "short gamma: treat as AMPLIFYING")
                elif gex > 0.05:
                    result.gex_regime = "LONG-GAMMA — dealers dampen moves"
                else:
                    # Net within ±0.05$M of zero: calls and puts cancel.
                    # Say so — a blank regime reads as missing data.
                    result.gex_regime = "BALANCED — negligible net dealer gamma"
                if wall:
                    result.flags.append(
                        f"GEX {result.gex_net_musd:+.1f}$M/1% | gamma wall ~${wall:.0f} "
                        f"| {result.gex_regime.split(' — ')[0]}")

            # ── GAMMA FLIP ZONE (analyzer-only; display + CSV, no scoring) ──
            # Same chain, same +call/-put convention as the GEX above —
            # just traced across price to find where net dealer gamma
            # crosses zero. Reuses the already-fetched chain_rows (no extra
            # network call) and runs ONLY when the caller asks for it, so
            # the bulk searcher path is byte-identical with the flag off.
            if with_gamma_flip:
                flip = _gamma_flip_from_rows(
                    current_price, [(c[0], c[1], c[2]) for c in chain_rows[:2]])
                if flip is not None:
                    result.gamma_flip_price = round(flip, 2)
                    result.gamma_flip_pct = (flip - current_price) / current_price
                    # Posture "now" is derived from the SAME signed net $GEX
                    # printed in the GEX block above (incl. its call-skew
                    # override), not from a spot-vs-flip syllogism — so the
                    # two lines can never contradict on a choppy or
                    # call-skewed chain. The flip PRICE is a zero of the
                    # gamma profile and is location-invariant to the sign
                    # convention (flipping every sign doesn't move the zero);
                    # only the short/long *side* depends on convention, which
                    # is exactly what gex_regime already adjudicated.
                    g = result.gex_net_musd
                    if result.gex_regime.startswith("CALL-SKEW OVERRIDE"):
                        result.gamma_flip_regime = (
                            "SHORT-GAMMA NOW — extreme call-skew implies dealers "
                            "short gamma; hedging amplifies moves")
                    elif g is not None and g < 0:
                        result.gamma_flip_regime = (
                            "SHORT-GAMMA NOW — dealer hedging amplifies moves")
                    elif g is not None and g <= 0.05:
                        result.gamma_flip_regime = (
                            "BALANCED NOW — net dealer gamma ~flat at spot")
                    else:
                        result.gamma_flip_regime = (
                            "LONG-GAMMA NOW — dealer hedging dampens moves")

        # Explicit availability flag: distinguishes "options data fetched
        # and shows no skew" from "yfinance returned empty chains" (the
        # June 9 scan failure where 156/160 names had zeroed options and
        # every probability collapsed → mass DORMANT verdicts).
        result.options_data_available = total_call_oi > 0

        # Strike with the most OTM call OI = the gamma magnet
        if strike_oi_map:
            result.convexity_strike = max(strike_oi_map, key=strike_oi_map.get)

        # ── 1-week skew shift ──
        prev = _get_snapshot_n_days_ago(ticker, 7)
        if prev and prev.get("otm_call_ratio") is not None and result.otm_call_ratio is not None:
            result.convexity_skew_1w = result.otm_call_ratio - prev["otm_call_ratio"]

        # ── SCORE: 0-100 ──
        score = 0.0

        # OTM call ratio: how much of call OI is in the squeeze zone
        if result.otm_call_ratio is not None:
            if result.otm_call_ratio >= 0.40:
                score += 35
                result.flags.append(f"Heavy OTM call OI ({result.otm_call_ratio:.0%} of calls)")
            elif result.otm_call_ratio >= 0.25:
                score += 22
            elif result.otm_call_ratio >= 0.15:
                score += 12

        # Call/put OI imbalance: bullish positioning
        if result.call_put_oi_ratio is not None:
            if result.call_put_oi_ratio >= 3.0:
                score += 25
                result.flags.append(f"Call/Put OI {result.call_put_oi_ratio:.1f}x — heavily call-skewed")
            elif result.call_put_oi_ratio >= 2.0:
                score += 16
            elif result.call_put_oi_ratio >= 1.3:
                score += 8

        # Skew velocity: is OTM call buying ACCELERATING this week?
        if result.convexity_skew_1w is not None:
            if result.convexity_skew_1w >= 0.08:
                score += 40
                result.flags.append(f"OTM call skew surging +{result.convexity_skew_1w:.0%} in 1wk 🔥")
            elif result.convexity_skew_1w >= 0.03:
                score += 24
            elif result.convexity_skew_1w > 0:
                score += 10
            elif result.convexity_skew_1w < -0.05:
                result.warnings.append("OTM call interest fading")

        result.convexity_score = min(score, 100.0)

        # Save snapshot for next run's velocity calc
        _save_snapshot(ticker, {
            "otm_call_ratio":    result.otm_call_ratio,
            "call_put_oi_ratio": result.call_put_oi_ratio,
            "otm_call_oi":       result.otm_call_oi,
        })

    except Exception as e:
        result.warnings.append(f"Options analysis failed: {e}")


# ─────────────────────────────────────────────
# SIGNAL 1.5: FINRA DAILY SHORT-VOLUME NOWCAST
# ─────────────────────────────────────────────
# Exchange short interest is biweekly with a ~9-day publication lag —
# the scanner's "39% SI" can be three weeks stale, and squeezes live and
# die inside that window. FINRA publishes DAILY short-sale volume (free,
# no key): short volume / total volume per ticker. The RATIO TREND is a
# nowcast of whether shorts are pressing or covering RIGHT NOW, between
# official SI prints. Whole-market files (~4MB/day) are cached to disk
# once per date and parsed into memory once per scan.

FINRA_CACHE_DIR = "finra_cache"

_SSL_CTX = None


def _ssl_ctx():
    """SSL context backed by certifi's CA bundle when available.

    Built once and reused. Returns None if certifi is missing, which makes
    urlopen fall back to the system store — the same behavior as before, so a
    missing certifi degrades rather than breaks."""
    global _SSL_CTX
    if _SSL_CTX is None:
        try:
            import ssl
            import certifi
            _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _SSL_CTX = False        # sentinel: tried, unavailable
    return _SSL_CTX or None
_FINRA_DAY_CACHE = {}     # date_str -> {SYM: (short, total)} | None=failed
_FINRA_LAST_ERR = ""      # last fetch failure reason (for honest warnings)


def _finra_trading_dates(n: int = 12) -> list:
    """Last n weekdays (newest last), excluding today (file posts ~6pm)."""
    out, d = [], datetime.now().date() - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return list(reversed(out))


def _finra_load_day(date_str: str):
    """Load one day's consolidated NMS short-volume file → {SYM:(s,t)}.
    Disk-cached; returns None on any failure (holiday 404s included)."""
    if date_str in _FINRA_DAY_CACHE:
        return _FINRA_DAY_CACHE[date_str]
    os.makedirs(FINRA_CACHE_DIR, exist_ok=True)
    path = os.path.join(FINRA_CACHE_DIR, f"CNMSshvol{date_str}.txt")
    text = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except Exception:
            text = None
    if text is None:
        try:
            import urllib.request
            url = (f"https://cdn.finra.org/equity/regsho/daily/"
                   f"CNMSshvol{date_str}.txt")
            req = urllib.request.Request(
                url, headers={"User-Agent": "squeeze-scanner/1.0"})
            # This Python's bundled CA store has an EXPIRED root, so FINRA
            # fails verification with "certificate has expired" and the
            # short-volume nowcast silently goes dark (0/5 sessions). certifi
            # ships a current bundle; use it when present. Never disable
            # verification — a broken trust store is a reason to fix trust,
            # not to stop checking.
            with urllib.request.urlopen(req, timeout=20,
                                        context=_ssl_ctx()) as resp:
                text = resp.read().decode("utf-8", errors="replace")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            global _FINRA_LAST_ERR
            _FINRA_LAST_ERR = f"{type(e).__name__}: {e}"
            _FINRA_DAY_CACHE[date_str] = None
            return None
    day_map = {}
    for line in text.splitlines()[1:]:
        parts = line.split("|")
        if len(parts) >= 5:
            try:
                # FINRA files carry FRACTIONAL share volumes (e.g. 309572.067225).
                # int() on those raises ValueError and silently drops the row —
                # that bug wiped ~93% of tickers and nulled the SVR nowcast.
                day_map[parts[1]] = (int(float(parts[2])), int(float(parts[4])))
            except (ValueError, IndexError):
                continue
    _FINRA_DAY_CACHE[date_str] = day_map
    return day_map


def analyze_short_volume_nowcast(ticker: str, result: DeepSqueezeResult):
    """SVR trend over the last ~12 sessions → PRESSING / COVERING read."""
    try:
        tk = ticker.upper()
        daily = []
        for ds in _finra_trading_dates(12):
            dm = _finra_load_day(ds)
            if dm is None:
                continue
            row = dm.get(tk)
            if row and row[1] > 0:
                daily.append((ds, row[0], row[1]))
        if len(daily) < 5:
            reason = f" — last error: {_FINRA_LAST_ERR}" if _FINRA_LAST_ERR else ""
            result.warnings.append(
                f"FINRA short-volume nowcast unavailable "
                f"({len(daily)}/5 sessions minimum){reason}")
            return
        recent, base, trend, score = _score_svr(daily)
        if recent is None:
            return
        result.svr_recent    = round(recent, 4)
        result.svr_baseline  = round(base, 4)
        result.svr_trend     = trend
        result.svr_score     = score
        result.svr_available = True
        result.flags.append(
            f"FINRA nowcast: short-vol ratio {recent:.0%} vs {base:.0%} "
            f"baseline → {trend}")
        _save_snapshot(ticker, {"svr": round(recent, 4)})
    except Exception as e:
        result.warnings.append(f"SVR nowcast failed: {e}")


# ─────────────────────────────────────────────
# SIGNAL 2: CTB VELOCITY
# ─────────────────────────────────────────────

def analyze_ctb_velocity(ticker: str, ctb_now: Optional[float],
                           result: DeepSqueezeResult):
    """
    Cost-to-borrow velocity. A static high CTB says hard-to-borrow now;
    the *velocity* says the squeeze is actively tightening. We snapshot
    the CTB proxy each run and compute the 1-week delta from our own history.
    """
    result.ctb_now = ctb_now
    if ctb_now is None:
        result.warnings.append("No CTB data")
        return

    prev = _get_snapshot_n_days_ago(ticker, 7)
    if prev and prev.get("ctb") is not None:
        result.ctb_1w_ago = prev["ctb"]
        result.ctb_velocity = ctb_now - prev["ctb"]
        if prev["ctb"] > 0:
            result.ctb_velocity_pct = result.ctb_velocity / prev["ctb"]

        # ── SCORE ──
        score = 0.0
        v = result.ctb_velocity
        vp = result.ctb_velocity_pct or 0

        if v >= 10.0:          # CTB jumped 10+ percentage points
            score += 50
            result.flags.append(f"CTB spiking +{v:.0f}pts in 1wk — borrow tightening fast 🔥")
        elif v >= 5.0:
            score += 35
            result.flags.append(f"CTB rising +{v:.0f}pts in 1wk")
        elif v >= 2.0:
            score += 20
        elif v > 0:
            score += 8
        elif v < -3.0:
            result.warnings.append(f"CTB falling {v:.0f}pts — borrow loosening")

        # Relative acceleration bonus
        if vp >= 0.50:
            score += 30
        elif vp >= 0.25:
            score += 18
        elif vp >= 0.10:
            score += 8

        # Absolute level still matters (high + rising = best)
        if ctb_now >= 50:
            score += 20
        elif ctb_now >= 25:
            score += 12
        elif ctb_now >= 10:
            score += 5

        result.ctb_velocity_score = min(score, 100.0)
    else:
        # No history yet — score on absolute level only, flag as first observation
        result.warnings.append("No CTB history — first snapshot (velocity next run)")
        if ctb_now >= 50:
            result.ctb_velocity_score = 30
        elif ctb_now >= 25:
            result.ctb_velocity_score = 18
        elif ctb_now >= 10:
            result.ctb_velocity_score = 8

    _save_snapshot(ticker, {"ctb": ctb_now})


# ─────────────────────────────────────────────
# SIGNAL 3: FTD ACCUMULATION
# ─────────────────────────────────────────────

def analyze_ftd_accumulation(ticker: str, ftd_shares: Optional[int],
                               ftd_pct_float: Optional[float],
                               float_shares: Optional[int],
                               result: DeepSqueezeResult,
                               effective_float: Optional[float] = None,
                               avg_daily_volume: Optional[float] = None):
    """
    Rolling FTD accumulation. A single FTD reading is noise; the trend
    of fails-to-deliver over multiple settlement cycles is the signal.
    Persistent rising FTDs = chronic settlement pressure = squeeze fuel.

    `effective_float` and `avg_daily_volume` size the close-out for the
    reader — the same fail balance against the shares that will actually
    trade, and against a day of normal turnover. Both are REPORTED ONLY:
    every gate and every score below still reads percent-of-reported-float,
    so rows logged after this change stay comparable with the graded history.
    """
    # ── PRIMARY: real SEC rolling FTD history (works on FIRST run) ──
    sec_ftd = None
    try:
        from data_validator import fetch_sec_ftd
        sec_ftd = fetch_sec_ftd(ticker, n_periods=6)
    except Exception as e:
        result.warnings.append(f"SEC FTD fetch failed: {e}")

    if sec_ftd and sec_ftd.get("data_quality") in ("live", "partial"):
        # Official multi-period data — no snapshot warm-up needed
        # ftd_total_recent (summed balances) is DEPRECATED — SEC FTD rows are
        # outstanding-balance snapshots; summing double-counts persistent
        # fails. Use the PEAK outstanding balance across recent periods as
        # the pressure measure (a real, non-double-counted quantity).
        series_all = sec_ftd.get("ftd_series") or []
        peak_bal = max((p[-1] for p in series_all), default=None)
        if peak_bal is None:
            peak_bal = sec_ftd.get("ftd_shares")
        result.ftd_total_recent = peak_bal   # semantics now: peak fail balance
        result.ftd_trend        = sec_ftd.get("ftd_trend", "")
        n_periods               = sec_ftd.get("ftd_periods", 0)
        if float_shares and float_shares > 0 and peak_bal:
            result.ftd_pct_float_accum = peak_bal / float_shares
        result.flags.append(
            f"SEC FTD: {n_periods} periods, trend {result.ftd_trend} "
            f"({sec_ftd.get('data_quality')})"
        )
        # ── T+35 FORCED CLOSE-OUT PROJECTION (mechanical catalyst) ──
        # Reg SHO requires fails to be closed out; a large fail cluster
        # creates a PREDICTABLE forced-buying window ~35 calendar days
        # after the settlement period. Unlike earnings, this catalyst is
        # mechanical — the buying must occur. Project only when the fail
        # load is significant (rising trend or ≥1.5% of float accumulated).
        series = sec_ftd.get("ftd_series") or []
        # Gate recalibrated: measure is now PEAK BALANCE %float (correct),
        # not the old inflated sum. ≥0.5% outstanding is Reg SHO
        # threshold-security territory — already significant.
        # ── FTD IMPACT FACTOR (fair weighting by float impact) ──
        # The closeout is forced buying, but forced buying of HOW MUCH?
        # Closing 0.3% of float over a 35-day window is noise against
        # daily volume; closing 2%+ is real demand. The old gate fired
        # on "RISING trend" alone, so 0.02%->0.05% drifts produced
        # full-strength catalysts that never moved a price.
        #   impact = 0.0  below 0.25% of float (noise floor — no catalyst)
        #   impact ramps linearly 0.25% -> 1.5% of float
        #   impact = 1.0  at 1.5%+ (Reg-SHO-heavy territory)
        # A RISING trend can nudge impact up slightly (+0.15) but can
        # never conjure a catalyst below the noise floor.
        _accum = result.ftd_pct_float_accum or 0.0
        NOISE_FLOOR, FULL_IMPACT = 0.0025, 0.015
        if _accum >= NOISE_FLOOR:
            fi = min((_accum - NOISE_FLOOR) / (FULL_IMPACT - NOISE_FLOOR),
                     1.0)
            if result.ftd_trend == "RISING":
                fi = min(fi + 0.15, 1.0)
        else:
            fi = 0.0
        result.ftd_impact_factor_v1 = round(fi, 3)

        # ── v2: SIZE THE BUYING AGAINST NORMAL TURNOVER ──
        # %float alone cannot answer "will this move a price". Two names can
        # both fail 2% of float; if one turns over 20% of its float a day and
        # the other 0.5%, the same close-out is noise in the first and a
        # week's demand in the second. Days-of-average-volume is that missing
        # dimension, and unlike effective float it is not a rescaling of the
        # number already in the gate — it is independent information.
        #
        # Deliberately NOT scored on percent-of-effective-float. Where 13F
        # holdings exceed the float the cap binds and effective float becomes
        # exactly float x (1 - locked_frac) — a constant multiple. Gating on
        # that would inflate every heavily-lent name by the same factor and
        # call the constant a discovery. Effective float earns its place in
        # the magnitude pillar (a supply-ceiling question) and in the display;
        # here the honest new variable is volume.
        #
        # Combination is the geometric mean, which is conservative on purpose:
        # the documented failure mode of v1 was full-strength catalysts that
        # never moved a price, so a large %float with trivial volume impact
        # SHOULD be demoted rather than waved through.
        _cfg = _scoring_cfg()
        _adv = avg_daily_volume or result.avg_daily_volume
        _adv_days = None
        if _adv and _adv > 0 and peak_bal:
            _adv_days = peak_bal / float(_adv)

        if _cfg["use_effective_float"] and _adv_days is not None and fi > 0:
            _vfloor = _cfg["ftd_vol_floor_days"]
            _vfull = _cfg["ftd_vol_full_days"]
            fi_vol = min(max((_adv_days - _vfloor) / (_vfull - _vfloor), 0.0),
                         1.0)
            fi_v2 = (fi * fi_vol) ** 0.5
            if fi_vol <= 0.0:
                result.warnings.append(
                    f"FTD close-out is {_adv_days:.2f} days of average volume "
                    f"— below the {_vfloor:.2f}-day floor. Fails are "
                    f"{_accum:.2%} of float but absorbable in normal "
                    f"turnover; impact cut from {fi:.0%} to 0%")
            elif fi_v2 < fi * 0.9:
                result.warnings.append(
                    f"FTD impact cut {fi:.0%} -> {fi_v2:.0%}: {_accum:.2%} of "
                    f"float is only {_adv_days:.2f} days of average volume")
            elif fi_v2 > fi:
                result.flags.append(
                    f"FTD close-out is {_adv_days:.2f} days of average volume "
                    f"— thin turnover makes {_accum:.2%} of float bite harder")
            fi = fi_v2
        elif _cfg["use_effective_float"] and fi > 0:
            result.warnings.append(
                "No average-volume figure — FTD impact left on the v1 "
                "percent-of-float gate alone (cannot size against turnover)")

        result.ftd_impact_factor = round(fi, 3)
        if series and fi > 0:
            co_date, co_days = _project_ftd_closeout(series)
            if co_date is not None and -5 <= co_days <= 45:
                result.ftd_closeout_date = co_date
                result.ftd_closeout_days = co_days
                result.flags.append(
                    f"FTD T+35 close-out window ~{co_date} "
                    f"({co_days:+d}d) — forced-buy pressure, impact "
                    f"{fi:.0%} ({_accum:.2%} of float)")
        elif series and _accum > 0:
            result.flags.append(
                f"FTD balance {_accum:.2%} of float — below noise floor "
                f"(0.25%), closeout NOT treated as a catalyst")
        # Still snapshot for cross-checking + future second-derivative work
        _save_snapshot(ticker, {
            "ftd_shares":    sec_ftd.get("ftd_shares"),
            "ftd_pct_float": ftd_pct_float,
        })
    else:
        # FALLBACK: old snapshot-based path if SEC unavailable
        if ftd_shares is None:
            result.warnings.append("No FTD data (SEC + snapshot both empty)")
            return
        _save_snapshot(ticker, {
            "ftd_shares":    ftd_shares,
            "ftd_pct_float": ftd_pct_float,
        })
        snaps = _load_snapshots().get(ticker, [])
        ftd_series = [s["ftd_shares"] for s in snaps
                      if s.get("ftd_shares") is not None][-6:]
        if len(ftd_series) >= 2:
            # Snapshots store outstanding balances — track the PEAK, never sum
            result.ftd_total_recent = int(max(ftd_series))
            x = np.arange(len(ftd_series))
            y = np.array(ftd_series, dtype=float)
            if y.std() > 0:
                slope = np.polyfit(x, y, 1)[0]
                avg = y.mean()
                if avg > 0 and slope > avg * 0.10:
                    result.ftd_trend = "RISING"
                elif avg > 0 and slope < -avg * 0.10:
                    result.ftd_trend = "FALLING"
                else:
                    result.ftd_trend = "FLAT"
            else:
                result.ftd_trend = "FLAT"
            if float_shares and float_shares > 0:
                result.ftd_pct_float_accum = result.ftd_total_recent / float_shares
        else:
            result.ftd_total_recent = ftd_shares
            result.ftd_trend = "INSUFFICIENT HISTORY"
            if float_shares and float_shares > 0:
                result.ftd_pct_float_accum = ftd_shares / float_shares

    # ── SIZE THE CLOSE-OUT (reported, never scored) ──
    # Percent of reported float answers "how big are the fails". Percent of
    # effective float answers "how big against what a forced buyer can reach",
    # and days-of-volume answers the only question that decides whether a
    # price moves: how much buying, relative to a normal session.
    peak_fails = result.ftd_total_recent or ftd_shares
    if peak_fails:
        try:
            from effective_float import closeout_read
            co = closeout_read(peak_fails, float_shares,
                               effective_float or result.effective_float,
                               avg_daily_volume or result.avg_daily_volume)
            result.ftd_pct_eff_float_accum = co.get('pct_eff_float')
            result.ftd_closeout_adv_days   = co.get('adv_days')
            result.ftd_closeout_verdict    = co.get('verdict', '')
            if (result.ftd_pct_eff_float_accum is not None
                    and result.ftd_pct_float_accum
                    and result.ftd_pct_eff_float_accum
                    > result.ftd_pct_float_accum * 1.5):
                result.flags.append(
                    f"Fails are {result.ftd_pct_eff_float_accum:.2%} of "
                    f"EFFECTIVE float vs {result.ftd_pct_float_accum:.2%} of "
                    f"reported float — locked institutional stock makes the "
                    f"close-out {result.ftd_pct_eff_float_accum / result.ftd_pct_float_accum:.1f}x "
                    f"heavier than the headline (not scored)")
        except Exception as e:
            result.warnings.append(f"Effective-float read failed: {e}")

    # ── SCORE ──
    score = 0.0
    accum = result.ftd_pct_float_accum or 0

    # Thresholds RECALIBRATED for the corrected balance-based measure.
    # The old 5%/2%/0.5% bands were tuned to summed balances (inflated
    # roughly 3-7x by double-counting). Peak outstanding fails ≥0.5% of
    # float is Reg SHO threshold-security territory — already significant.
    # These bands are a principled starting point; let the outcome grader
    # refine them as graded history accumulates on clean data.
    if accum >= 0.015:       # peak fails ≥ 1.5% of float — extreme
        score += 45
        result.flags.append(f"FTD peak balance {accum:.2%} of float — chronic settlement pressure")
    elif accum >= 0.005:     # threshold-security territory
        score += 30
    elif accum >= 0.001:
        score += 15
    elif accum > 0:
        score += 5

    if result.ftd_trend == "RISING":
        score += 40
        result.flags.append("FTDs trending UP across cycles 🔥")
    elif result.ftd_trend == "FLAT" and accum >= 0.02:
        score += 15
    elif result.ftd_trend == "FALLING":
        result.warnings.append("FTDs declining — pressure easing")

    result.ftd_score_v1 = min(score, 100.0)

    # ── v2: scale by how much turnover the close-out actually represents ──
    # Bounded 0.50x–1.15x on purpose. Fails existing at all is information,
    # so a thin-volume-impact name is DEMOTED, never zeroed; and a name whose
    # close-out exceeds a day of volume gets a modest lift rather than a
    # second full helping of the same signal.
    cfg = _scoring_cfg()
    if cfg["use_effective_float"] and result.ftd_closeout_adv_days is not None:
        _vf, _vfl = cfg["ftd_vol_floor_days"], cfg["ftd_vol_full_days"]
        _fiv = min(max((result.ftd_closeout_adv_days - _vf) / (_vfl - _vf),
                       0.0), 1.0)
        vol_mult = 0.50 + 0.65 * _fiv
        result.ftd_score = min(score * vol_mult, 100.0)
        result.scoring_version = int(cfg["scoring_version"])
    else:
        result.ftd_score = result.ftd_score_v1
        result.scoring_version = (int(cfg["scoring_version"])
                                  if cfg["use_effective_float"] else 1)



# ─────────────────────────────────────────────
# CONVICTION MATRIX: CTB velocity + DTC trend + SI trend
# ─────────────────────────────────────────────

def analyze_conviction_matrix(ticker: str,
                                ctb_now: Optional[float],
                                dtc_now: Optional[float],
                                si_now: Optional[float],
                                result: DeepSqueezeResult):
    """
    Three lenses on the same trade:
      CTB trend  — cost lens     (how expensive to stay short)
      DTC trend  — liquidity lens (how trapped shorts are)
      SI  trend  — positioning   (shorts adding or covering NOW)

    Produces a conviction multiplier applied to the Stage-1 combined score.
    Multiplier ranges roughly 0.6x (unwinding) to 1.8x (all confirming).
    """
    result.ctb_now = ctb_now
    result.dtc_now = dtc_now
    result.si_now  = si_now

    prev = _get_snapshot_n_days_ago(ticker, 7)
    # Distinguish "never snapshotted" from "snapshots exist but all stale" —
    # previously both silently produced STABLE/NO-HISTORY 1.0x and a dead
    # velocity layer was indistinguishable from a calm market.
    has_any_history = bool(_load_snapshots().get(ticker))
    prev_age = prev.get("_age_days") if prev else None

    # ── Determine each trend vs 1-week-ago snapshot ──
    ctb_dir = dtc_dir = si_dir = "FLAT"

    if prev:
        # CTB trend
        if ctb_now is not None and prev.get("ctb") is not None:
            result.ctb_1w_ago = prev["ctb"]
            d = ctb_now - prev["ctb"]
            if d >= 3.0:
                ctb_dir = "RISING"
            elif d <= -3.0:
                ctb_dir = "FALLING"
        # DTC trend
        if dtc_now is not None and prev.get("dtc") is not None:
            result.dtc_1w_ago = prev["dtc"]
            d = dtc_now - prev["dtc"]
            # tightening = DTC rising (harder to cover)
            if d >= 0.5:
                dtc_dir = "TIGHTENING"
            elif d <= -0.5:
                dtc_dir = "LOOSENING"
        # SI trend
        if si_now is not None and prev.get("si") is not None:
            result.si_1w_ago = prev["si"]
            d = si_now - prev["si"]
            if d >= 0.01:           # +1pp short interest
                si_dir = "ADDING"
            elif d <= -0.01:
                si_dir = "COVERING"

    # ── PREFER SETTLEMENT-OVER-SETTLEMENT FOR SI AND DTC ──
    # The snapshot path above diffs today against ~7 days ago. Short interest
    # is published twice a month, so 74% of consecutive snapshot pairs carry
    # the IDENTICAL value (measured over 1,026 pairs in squeeze_snapshots.json)
    # — the comparison is structurally incapable of showing a change most of
    # the time, and when it does fire it attributes up to fifteen days of
    # position change to a "one week" trend.
    #
    # The exchange publishes ~26 settlements per symbol with contemporaneous
    # short interest AND volume. Both ends of that comparison are real
    # observations of the same quantity, and it is what a data vendor means by
    # "covering". CTB has no settlement series and keeps the snapshot path.
    result.si_trend_v1  = si_dir
    result.dtc_trend_v1 = dtc_dir
    _st_cfg = _scoring_cfg()
    st_si = getattr(result, "_settlement_si_trend", "") or ""
    st_dtc = getattr(result, "_settlement_dtc_trend", "") or ""
    if _st_cfg.get("use_settlement_trends", True) and st_si:
        if st_si != si_dir:
            result.flags.append(
                f"SI trend {si_dir or 'FLAT'} (7d snapshot) -> {st_si} "
                f"(settlement over settlement) — snapshot pairs are the same "
                f"settlement 74% of the time")
        si_dir = st_si
        result.si_trend_source = "settlement"
        if st_dtc:
            dtc_dir = st_dtc
        # ── A DTC MOVE WITH NO POSITION MOVE IS NOT A POSITION SIGNAL ──
        # GME, settlement 2026-08-14: DTC fell 17.06 -> 5.31 while short
        # interest moved +0.6%, because average volume rose 223%. Read as a
        # trend that is "shorts escaping, pressure fading" and it cut the
        # conviction multiplier to 0.80x. Nothing about the short position
        # changed. The liquidity lens is only a liquidity lens when the
        # numerator holds still; when it does not, this branch says so and
        # declines to score the denominator as positioning.
        if getattr(result, "_dtc_move_is_liquidity", False) and dtc_dir != "FLAT":
            _vc = getattr(result, "_settlement_vol_change", None)
            result.warnings.append(
                f"DTC {dtc_dir} is a VOLUME move, not a positioning move: "
                f"average volume {_vc:+.0%} while short interest moved "
                f"{(result.si_change_settlement or 0):+.1%}. DTC trend "
                f"neutralised for conviction."
                if _vc is not None else
                f"DTC {dtc_dir} came from the volume denominator, not the "
                f"short position — neutralised for conviction.")
            dtc_dir = "FLAT"
    else:
        result.si_trend_source = "snapshot_7d"

    result.ctb_trend = ctb_dir
    result.dtc_trend = dtc_dir if dtc_dir != "FLAT" else "FLAT"
    result.si_trend  = si_dir

    # ── CONVICTION MATRIX ──
    # Map the (CTB, DTC, SI) combination to a state + multiplier.
    ctb_up  = ctb_dir == "RISING"
    ctb_dn  = ctb_dir == "FALLING"
    dtc_up  = dtc_dir == "TIGHTENING"
    dtc_dn  = dtc_dir == "LOOSENING"
    si_up   = si_dir  == "ADDING"
    si_dn   = si_dir  == "COVERING"

    # ── ACTIVE SQUEEZE DETECTION (the level-aware branch) ──
    # Every branch below reads trend DIRECTION only — which made the
    # system structurally blind to a squeeze already FIRING. During
    # forced covering the signature INVERTS: SI falls (that's what a
    # squeeze is) and CTB falls as borrow demand drains. A GRPN-class
    # name (SI 67%, CTB 109%, +50% off the low) read as 'early/DORMANT'
    # at 1.1x. If fuel LEVELS are extreme AND the covering signature is
    # present, that is not the absence of a squeeze — it is one, live.
    # Severity-scaled so 'extreme' is graded, not a binary gate.
    _sev = _squeeze_severity(si_now, getattr(result, "ctb_now", None))
    if _sev >= 0.45 and (si_dn or ctb_dn):
        state = (f"🔥 ACTIVE SQUEEZE — covering into extreme fuel "
                 f"(severity {_sev:.0%}: SI/CTB levels remain massive)")
        mult  = round(1.40 + 0.35 * _sev, 3)      # 1.56 → 1.75 by severity
    elif ctb_up and dtc_up and si_up:
        state = "🔥 MAXIMUM CONVICTION — all three confirming squeeze tightening"
        mult  = 1.80
    elif ctb_up and dtc_up and si_dn:
        state = "⚡ COVERING INTO TIGHTENING — imminent, timing critical"
        mult  = 1.65
    elif ctb_up and dtc_up:
        state = "🔥 BORROW SEIZING — cost + liquidity both tightening"
        mult  = 1.50
    elif ctb_up and si_up:
        state = "⚠️ COST SQUEEZE + shorts adding — building"
        mult  = 1.30
    elif dtc_up and si_up:
        state = "⚠️ LIQUIDITY TRAP forming — shorts adding, exit narrowing"
        mult  = 1.30
    elif ctb_up:
        state = "📈 COST SQUEEZE only — borrow expensive, watch"
        mult  = 1.15
    elif dtc_up:
        state = "📈 LIQUIDITY TIGHTENING only — early"
        mult  = 1.10
    elif ctb_dn and dtc_dn and si_dn:
        state = "❌ UNWINDING — all three loosening, exit/avoid"
        mult  = 0.60
    elif ctb_dn or dtc_dn:
        state = "⬇️ EASING — borrow loosening, pressure fading"
        mult  = 0.80
    elif si_dn:
        state = "⬇️ SHORTS COVERING — fuel leaving quietly"
        mult  = 0.85
    elif not prev and has_any_history:
        state = "⚠️ STALE HISTORY — last snapshot too old for 1-wk trend, conviction neutral (rescan within a week to rebuild)"
        mult  = 1.00
    elif not prev:
        state = "⏳ NO HISTORY — first snapshot, conviction next run"
        mult  = 1.00
    else:
        state = "➡️ STABLE — no meaningful trend change"
        if prev_age and prev_age > 10:
            state += f" (vs {prev_age}d-old snapshot)"
        mult  = 1.00

    result.conviction_state = state
    # Learned conviction-effect scaling: if graded history shows the
    # conviction matrix over/under-delivers, scale its DEVIATION from
    # neutral (1.0 stays 1.0; a 1.8 with scale 0.8 becomes 1.64).
    _cs = _learned_squeeze().get("conviction_effect_scale", 1.0)
    if _cs and abs(_cs - 1.0) > 0.005 and abs(mult - 1.0) > 0.001:
        mult = round(1.0 + (mult - 1.0) * _cs, 4)
    result.conviction_mult  = mult

    if mult >= 1.5:
        result.flags.append(f"Conviction {mult:.2f}x — {state.split('—')[0].strip()}")
    elif mult < 0.9:
        result.warnings.append(f"Conviction {mult:.2f}x — {state.split('—')[0].strip()}")

    # Snapshot all three for next run's trend calc
    _save_snapshot(ticker, {
        "ctb": ctb_now,
        "dtc": dtc_now,
        "si":  si_now,
    })


# ─────────────────────────────────────────────
# COMPOSITE: PROBABILITY → IMMINENCE → MAGNITUDE
# ─────────────────────────────────────────────

def _compute_composite(result: DeepSqueezeResult, stage1_score: float,
                         si_pct: Optional[float], dtc: Optional[float]):
    """
    Blend the three deep signals into Probability / Imminence / Magnitude,
    then a final deep_score ranked Probability → Imminence → Magnitude.
    """
    cvx = result.convexity_score
    ctb = result.ctb_velocity_score
    ftd = result.ftd_score

    # ── PROBABILITY (highest weight) ──
    # How confident is the setup? Multiple confirming signals + strong stage-1.
    #
    # MISSING DATA ≠ BEARISH SIGNAL (same principle as the stock-side fix):
    # a signal only participates if its data was actually fetched. Scoring
    # an empty options chain as cvx=0 dragged every probability down and
    # produced the all-DORMANT June 9 scan. Unavailable signals are excluded
    # and the confluence denominator + signal weights renormalize over what
    # was measurable.
    sig_list = []
    if getattr(result, "options_data_available", False):
        sig_list.append(("options", cvx))
    else:
        result.warnings.append(
            "Options chain unavailable — probability computed without convexity")
    if result.ctb_now is not None:
        sig_list.append(("ctb", ctb))
    if result.ftd_trend:
        sig_list.append(("ftd", ftd))
    if result.svr_available:
        sig_list.append(("svr", result.svr_score))
    _price_momentum(result)
    if result.momentum_available:
        sig_list.append(("momentum", result.momentum_score))

    def _prob_from(sigs) -> float:
        """Probability over a signal list. Factored out so the v1 variant is
        the SAME arithmetic with the v1 FTD score substituted, rather than an
        approximation that could drift from the real one."""
        n = len(sigs)
        vs = [v for _, v in sigs]
        firing = sum(1 for v in vs if v >= 40)
        return (stage1_score * 0.35
                + (firing / n) * 100 * 0.35
                + (sum(vs) / n) * 0.30)

    if sig_list:
        n_avail = len(sig_list)
        signals_firing = sum(1 for _, v in sig_list if v >= 40)
        prob = _prob_from(sig_list)
        # Same signals, v1 FTD score — the only deep signal v2 changes.
        prob_v1 = _prob_from([(nm, result.ftd_score_v1 if nm == "ftd" else v)
                              for nm, v in sig_list])
        result.probability_score_v1 = min(prob_v1, 100.0)
        if n_avail < 3:
            result.warnings.append(
                f"Probability from {n_avail}/3 deep signals — data partially unavailable")
    else:
        # No deep signals at all — probability rests on stage-1 alone,
        # flagged loudly so a data-outage scan can't masquerade as a read.
        signals_firing = 0
        n_avail = 0
        prob = stage1_score * 0.35
        result.probability_score_v1 = min(prob, 100.0)
        result.warnings.append(
            "NO deep signals available — verdict unreliable (data outage?)")
    result.probability_score = min(prob, 100.0)
    result._n_signals_available = n_avail

    # ── IMMINENCE ──
    # How soon could it fire? Velocity signals dominate (CTB + skew shift).
    # Renormalize over available components — an options outage must not
    # read as "less imminent" (same missing-data principle as probability).
    opts_ok = getattr(result, "options_data_available", False)
    ctb_ok = result.ctb_now is not None
    if opts_ok and ctb_ok:
        imm = ctb * 0.45 + cvx * 0.35
    elif ctb_ok:
        imm = ctb * 0.80          # cvx weight folded into ctb
    elif opts_ok:
        imm = cvx * 0.80
    else:
        imm = 0.0
    if result.convexity_skew_1w and result.convexity_skew_1w >= 0.05:
        imm += 20
    if result.ctb_velocity and result.ctb_velocity >= 5:
        imm += 20
    # ── Catalyst proximity (F12 fix) ──
    # A scheduled catalyst inside the actionable window IS imminence —
    # the single most reliable "when" signal the system has, and it was
    # invisible to this score. Up to +20 as the date approaches (linear
    # from 15d out). Note: this feeds deep_score/verdict gating only;
    # catalyst timing's effect on FINAL stays solely in catalyst_mult,
    # so this does NOT re-create the double-count we just removed.
    d2c = getattr(result, "days_to_earnings", None)
    if d2c is not None and 0 <= d2c <= 15:
        imm += (15 - d2c) / 15.0 * 20
    # ACTIVE SQUEEZE + price thrust = the covering is happening NOW
    if (result.momentum_score >= 55
            and "ACTIVE SQUEEZE" in (result.conviction_state or "")):
        imm += 15
        result.flags.append("⏱ price thrust CONFIRMS live covering — "
                            "imminence boosted")
    result.imminence_score = min(imm, 100.0)

    # ── MAGNITUDE ──
    # How big could it move? Static squeeze potential: SI%, DTC, FTD accum.
    # Renormalize over available components — missing FTD data should not
    # cap magnitude at 70.
    #
    # REWEIGHTED (F11): SI and DTC are already ~55% of stage-1; giving
    # them 70% of magnitude re-asked the same question a third time.
    # Float tightness added — the actual ceiling variable: a 25M-float
    # name at 40% SI can triple; a 900M-float name at 40% SI grinds.
    # Parts carry a NAME as well as value and weight. Two of them (FTD and
    # float) share weight 25, so any attempt to rebuild the v1 variant by
    # matching on (value, weight) would eventually swap the wrong one.
    mag_parts = []          # (name, v2_value, weight)
    if si_pct:
        mag_parts.append(("si", min(si_pct / 0.50, 1.0) * 100, 30))  # 50%+ = max
    if dtc:
        mag_parts.append(("dtc", min(dtc / 10.0, 1.0) * 100, 20))    # 10+ = max
    if result.ftd_trend:
        mag_parts.append(("ftd", ftd, 25))
    # Implied move — the market-priced catalyst impact IS a magnitude read
    if result.implied_move_pct is not None:
        im = result.implied_move_pct
        if   im >= 0.15: im_sc = 100.0
        elif im >= 0.10: im_sc = 75.0
        elif im >= 0.06: im_sc = 50.0
        elif im >= 0.03: im_sc = 25.0
        else:            im_sc = 10.0
        mag_parts.append(("implied_move", im_sc, 20))
    # GEX regime — amplification multiplies realized magnitude
    if result.gex_regime:
        if "AMPLIF" in result.gex_regime:
            mag_parts.append(("gex", 85.0, 15))
        elif "LONG-GAMMA" in result.gex_regime:
            mag_parts.append(("gex", 20.0, 15))
    # ── Float ceiling ──
    # Magnitude is the one pillar where effective float belongs: "how big
    # could it move" IS a question about how many shares a buyer must source,
    # and index/long-only stock is not for sale at any price a short will pay.
    # The v2 buckets are set on their own merits for TRADEABLE float, not by
    # rescaling the reported-float buckets — a 10M tradeable float is
    # explosive whatever the headline float says. v1 buckets stay the
    # fallback wherever effective float is unavailable, and v1's score is
    # kept alongside so the two can be compared on identical rows.
    flt = getattr(result, "float_shares", None)
    eff = getattr(result, "effective_float", None)
    cfg_m = _scoring_cfg()

    def _bucket(v, b):
        if v <= b[0]:   return 100.0    # tiny float — explosive ceiling
        elif v <= b[1]: return 70.0
        elif v <= b[2]: return 45.0
        elif v <= b[3]: return 25.0
        return 10.0

    V1_BUCKETS = [30e6, 75e6, 150e6, 400e6]
    f_sc_v1 = _bucket(flt, V1_BUCKETS) if flt else None
    if cfg_m["use_effective_float"] and eff:
        f_sc = _bucket(eff, cfg_m["eff_float_buckets"])
    else:
        f_sc = f_sc_v1
    if f_sc is not None:
        mag_parts.append(("float", f_sc, 25))

    # v1 differs from v2 in exactly two named parts: the FTD score and the
    # float bucket. Everything else is shared, so both totals come from one
    # part list and no value-matching is involved.
    v1_override = {"ftd": result.ftd_score_v1}
    if f_sc_v1 is not None:
        v1_override["float"] = f_sc_v1

    def _weighted(parts, override=None):
        override = override or {}
        tw = sum(w for _, _, w in parts)
        if not tw:
            return 0.0
        return min(sum(override.get(n, v) * w for n, v, w in parts) / tw,
                   100.0)

    result.magnitude_score = _weighted(mag_parts)
    result.magnitude_score_v1 = _weighted(mag_parts, v1_override)

    # ── FINAL: Probability → Imminence → Magnitude ──
    result.deep_score = (
        result.probability_score * 0.50 +
        result.imminence_score   * 0.30 +
        result.magnitude_score   * 0.20
    )
    # Same blend on the v1 pillars. Imminence is untouched by v2, so it is
    # shared — the v1/v2 gap is entirely probability and magnitude.
    result.deep_score_v1 = (
        result.probability_score_v1 * 0.50 +
        result.imminence_score      * 0.30 +
        result.magnitude_score_v1   * 0.20
    )

    # Verdict
    n_avail = getattr(result, "_n_signals_available", 3)
    if n_avail == 0:
        # No deep data at all — an honest "can't tell" beats a fake DORMANT.
        result.deep_verdict = "UNRELIABLE — deep data unavailable"
    elif result.deep_score >= 65 and signals_firing >= 2:
        result.deep_verdict = "IGNITING 🔥"
    elif result.deep_score >= 45:
        result.deep_verdict = "BUILDING ⚠️"
    elif stage1_score >= 55 and signals_firing == 0:
        # TRAP only means something when the signals were MEASURED and
        # showed nothing — not when they were missing. AND it must respect
        # the conviction matrix: calling a name "no velocity" while its own
        # conviction column shows 1.1x tightening is a contradiction (the
        # June 12 scan labeled PRCH exactly that way). Conviction IS
        # velocity — just on a weekly clock instead of the deep signals'.
        _conv = result.conviction_mult or 1.0
        if _conv > 1.05:
            result.deep_verdict = "DORMANT"   # quiet deep layer, but weekly
                                              # pressure tightening — not a trap
        elif _conv < 0.95:
            result.deep_verdict = "TRAP — statics strong but pressure EASING"
        else:
            result.deep_verdict = "TRAP — static metrics only, velocity unconfirmed"
    else:
        result.deep_verdict = "DORMANT"

    # ── VERDICT COHERENCE (the GRPN contradiction) ──
    # The conviction matrix can now say ACTIVE SQUEEZE while the trend-
    # driven probability still reads DORMANT — the same one-layer-up
    # blindness we fixed in the matrix. A live squeeze is by definition
    # not dormant: floor the verdict at BUILDING.
    if ("ACTIVE SQUEEZE" in (result.conviction_state or "")
            and ("DORMANT" in result.deep_verdict
                 or "TRAP" in result.deep_verdict)):
        result.deep_verdict = "BUILDING ⚠️ (floored: ACTIVE SQUEEZE)"
        result.flags.append("verdict floored to BUILDING — conviction "
                            "matrix reports a live squeeze")


_LEARNED_CACHE = None


def _learned_squeeze() -> dict:
    """Cached squeeze-side learned params. {} when absent/inactive —
    scanners run on baselines until the outcome log earns adjustments."""
    global _LEARNED_CACHE
    if _LEARNED_CACHE is None:
        try:
            from learning_engine import load_params
            p = load_params().get("squeeze", {})
            _LEARNED_CACHE = p if p.get("active") else {}
        except Exception:
            _LEARNED_CACHE = {}
    return _LEARNED_CACHE


# ─────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────

def run_deep_analysis(ticker: str, stage1_score: float = 0.0,
                       metrics=None, with_gamma_flip: bool = False) -> DeepSqueezeResult:
    """
    Run full deep analysis on a single finalist.

    `metrics` is the SqueezeMetrics object from stage-1 (so we reuse
    already-fetched data instead of re-pulling). If None, fetches fresh.

    `with_gamma_flip` computes the zero-gamma (gamma flip) price from the
    same options chain used for GEX. Display + CSV only, no scoring impact.
    The single-stock analyzer passes True; the bulk searcher leaves it
    False, so its scored output is unchanged.
    """
    result = DeepSqueezeResult(ticker=ticker.upper())

    # Reuse stage-1 metrics where possible
    if metrics is not None:
        m = metrics
    else:
        try:
            from squeeze_analyzers import fetch_squeeze_metrics
            m = fetch_squeeze_metrics(ticker)
        except Exception as e:
            result.warnings.append(f"Metrics fetch failed: {e}")
            return result

    result.current_price = m.current_price
    result.float_shares  = getattr(m, "float_shares", None)
    result.effective_float  = getattr(m, "effective_float", None)
    result.float_tightness  = getattr(m, "float_tightness", None)
    result.avg_daily_volume = getattr(m, "avg_daily_volume", None)
    # DTC family + settlement-cadence trends. The private attributes are read
    # by analyze_conviction_matrix, which runs after this.
    result.dtc_exchange     = getattr(m, "dtc_exchange", None)
    result.dtc_robust       = getattr(m, "dtc_robust", None)
    result.dtc_60d          = getattr(m, "dtc_60d", None)
    result.dtc_spread_low   = getattr(m, "dtc_spread_low", None)
    result.dtc_spread_high  = getattr(m, "dtc_spread_high", None)
    result.dtc_spike_ratio  = getattr(m, "dtc_spike_ratio", None)
    result.si_change_settlement = getattr(m, "si_change_settlement", None)
    result.settlement_date  = getattr(m, "settlement_date", "") or ""
    result.settlement_age_days = getattr(m, "settlement_age_days", None)
    result.settlement_consecutive = getattr(m, "settlement_consecutive", 0) or 0
    result._settlement_si_trend = getattr(m, "si_trend_settlement", "") or ""
    result._settlement_dtc_trend = getattr(m, "dtc_trend_settlement", "") or ""
    result._dtc_move_is_liquidity = getattr(m, "dtc_move_is_liquidity", False)
    result._settlement_vol_change = getattr(m, "settlement_vol_change", None)
    ctb_now      = m.ctb_proxy
    si_pct       = m.short_interest_pct
    dtc          = m.days_to_cover
    ftd_shares   = getattr(m, "ftd_shares", None)
    ftd_pct      = m.ftd_pct_float
    float_shares = m.float_shares

    if not result.current_price:
        result.warnings.append("No price — cannot run deep analysis")
        return result

    analyze_options_convexity(ticker, result.current_price, result,
                              with_gamma_flip=with_gamma_flip)
    analyze_ctb_velocity(ticker, ctb_now, result)
    analyze_short_volume_nowcast(ticker, result)
    analyze_ftd_accumulation(ticker, ftd_shares, ftd_pct, float_shares, result,
                             effective_float=result.effective_float,
                             avg_daily_volume=result.avg_daily_volume)
    analyze_conviction_matrix(ticker, ctb_now, dtc, si_pct, result)

    # ── CATALYST TIMING — the proven-edge layer ──
    # The setups that worked were surfaced WITH a catalyst days out, not on
    # squeeze fuel alone. This makes that timing requirement systematic.
    try:
        from squeeze_catalyst import analyze_catalyst
        # FTD close-out joins the catalyst pool as a mechanical event —
        # the merge logic picks the best-timed catalyst of ANY type.
        _extra = []
        if result.ftd_closeout_date and result.ftd_closeout_days is not None:
            _extra.append((result.ftd_closeout_date,
                           result.ftd_closeout_days,
                           "FTD_CLOSEOUT",
                           f"T+35 close-out of "
                           f"{(result.ftd_total_recent or 0):,} fails"))
        cat = analyze_catalyst(ticker, extra_events=_extra or None)
        result.catalyst_window  = cat.catalyst_window
        result.catalyst_type    = getattr(cat, "catalyst_type", "") or ""
        result.catalyst_score   = cat.catalyst_score
        result.catalyst_note    = cat.catalyst_note
        result.days_to_earnings = cat.days_to_earnings
        # Catalyst MULTIPLIER: rewards sweet-spot timing, punishes
        # the "already fired" failure mode that wrecked the last scan.
        if cat.catalyst_window == "SWEET_SPOT":
            _cm = 1.0 + (cat.catalyst_score / 100.0) * 0.5   # up to 1.5x
            if (result.catalyst_type or "").upper() == "FTD_CLOSEOUT":
                # scale the BOOST by float impact: mult = 1 + (mult-1)*fi
                _fi = getattr(result, "ftd_impact_factor", 0.0) or 0.0
                _cm = 1.0 + (_cm - 1.0) * _fi
                if _fi < 1.0:
                    result.flags.append(
                        f"FTD catalyst multiplier scaled x{_fi:.2f} by "
                        f"float impact -> {_cm:.2f}")
            result.catalyst_mult = round(_cm, 4)
            result.flags.append(cat.catalyst_note)
        elif cat.catalyst_window == "IMMINENT":
            result.catalyst_mult = 1.10
        elif cat.catalyst_window == "PASSED":
            result.catalyst_mult = 0.55   # heavy penalty — fuel spent
            result.warnings.append(cat.catalyst_note)
        elif cat.catalyst_window == "TOO_FAR":
            # was 0.90 — which perversely scored a KNOWN catalyst 20d out
            # BELOW a name with no catalyst at all (1.0). Information can't
            # be worth less than ignorance; neutral, with the dead-money
            # note kept as text.
            result.catalyst_mult = 1.00
        else:
            result.catalyst_mult = 1.0

        # ── LEARNED ADJUSTMENT (learning_engine) ──
        # If the graded outcome log shows a window performing above or
        # below its baseline, scale the multiplier — bounded ±25%,
        # shrunk by sample size, fully audited in learned_params.json.
        _lscale = _learned_squeeze().get("catalyst_window_scale", {}).get(
            cat.catalyst_window)
        if _lscale and abs(_lscale - 1.0) > 0.005:
            result.catalyst_mult = round(result.catalyst_mult * _lscale, 4)
            result.flags.append(
                f"📚 learned: {cat.catalyst_window} ×{_lscale} "
                f"(from graded history)")
    except ImportError:
        result.warnings.append("squeeze_catalyst.py not found — no timing layer")
        result.catalyst_mult = 1.0
    except Exception as e:
        result.warnings.append(f"Catalyst analysis failed: {e}")
        result.catalyst_mult = 1.0

    _compute_composite(result, stage1_score, si_pct, dtc)

    # ── CALIBRATED PROBABILITY (Phase 5.1) ──
    # When the graded log has earned a fitted model (150+ rows, 20+ each
    # class), translate this candidate's features into an actual
    # probability with a base rate for comparison. Silent until then.
    try:
        from learning_engine import load_params, calibrated_probability
        _cal = load_params().get("calibration", {})
        if _cal.get("active"):
            _p = calibrated_probability({
                "si_pct": si_pct,
                "dtc": dtc,
                "ctb": ctb_now,
                "conviction_mult": result.conviction_mult,
                "sweet_spot": 1.0 if result.catalyst_window == "SWEET_SPOT" else 0.0,
                "implied_move": result.implied_move_pct,
                "combined": stage1_score,
            }, _cal)
            if _p is not None:
                result.calibrated_prob = _p
                result.flags.append(
                    f"📊 CALIBRATED: {_p:.0%} chance of +15%/10d "
                    f"(base rate {_cal.get('base_rate', 0):.0%}, "
                    f"n={_cal.get('n')})")
    except Exception as _e:
        # Calibration is optional, but its failures should be VISIBLE —
        # a silent pass here is how invalid weight/return-unit mismatches
        # go unnoticed for months.
        result.warnings.append(f"Calibration layer error: {_e}")

    # ── FINAL SCORE: combined × conviction × catalyst timing ──
    # Three multiplicative layers. Scores >100 expected and meaningful.
    # A perfect setup at the perfect time compounds; a stale one decays.
    result.final_score = (stage1_score
                          * result.conviction_mult
                          * result.catalyst_mult)

    return result


# ─────────────────────────────────────────────
# DISPLAY
# ─────────────────────────────────────────────

def format_deep_display(r: DeepSqueezeResult) -> str:
    def bar(score, width=20):
        filled = int(score / 100 * width)
        return "█" * filled + "░" * (width - filled)

    lines = [
        f"  ── DEEP SQUEEZE ANALYSIS: {r.ticker} ──────────────────────────",
        f"",
        f"  Deep Score:  {r.deep_score:.0f}/100   {r.deep_verdict}",
        f"  {'─'*58}",
        f"  Probability  {r.probability_score:>5.0f}  {bar(r.probability_score)}",
        f"  Imminence    {r.imminence_score:>5.0f}  {bar(r.imminence_score)}",
        f"  Magnitude    {r.magnitude_score:>5.0f}  {bar(r.magnitude_score)}",
        f"  {'─'*58}",
    ]
    if r.scoring_version >= 2 and abs(r.deep_score - r.deep_score_v1) > 0.5:
        _d = r.deep_score - r.deep_score_v1
        lines.append(
            f"  scoring v{r.scoring_version} · v1 would have scored "
            f"{r.deep_score_v1:.0f} ({_d:+.0f}) — prob "
            f"{r.probability_score_v1:.0f}, mag {r.magnitude_score_v1:.0f}")
        lines.append(f"  {'─'*58}")
    lines += [
        f"",
        f"  📊 OPTIONS CONVEXITY: {r.convexity_score:.0f}/100",
    ]
    if r.otm_call_ratio is not None:
        lines.append(f"     OTM call OI ratio:  {r.otm_call_ratio:.0%}  (calls 10-30% above spot)")
    if r.call_put_oi_ratio is not None:
        lines.append(f"     Call/Put OI ratio:  {r.call_put_oi_ratio:.1f}x")
    if r.convexity_skew_1w is not None:
        arrow = "▲" if r.convexity_skew_1w > 0 else "▼"
        lines.append(f"     1wk skew shift:     {arrow} {r.convexity_skew_1w:+.1%}")
    if r.convexity_strike:
        lines.append(f"     Gamma magnet strike: ${r.convexity_strike:.2f}")

    # ── Implied move: market-priced catalyst impact ──
    if r.implied_move_pct is not None:
        lines.append(f"")
        lines.append(f"  🎯 IMPLIED MOVE (catalyst impact, market-priced)")
        lines.append(f"     ATM straddle:       ±{r.implied_move_pct:.1%} "
                     f"through {r.implied_move_expiry}")
        if r.current_price:
            mv = r.current_price * r.implied_move_pct
            lines.append(f"     Dollar move:        ±${mv:.2f} on "
                         f"${r.current_price:.2f}")

    # ── Dealer gamma exposure: the amplification variable ──
    if r.gex_net_musd is not None:
        lines.append(f"")
        lines.append(f"  ⚡ DEALER GAMMA (GEX)")
        lines.append(f"     Net $GEX:           {r.gex_net_musd:+.1f}M per 1% move")
        if r.gex_wall_strike:
            lines.append(f"     Gamma wall:         ~${r.gex_wall_strike:.0f} "
                         f"(largest call-gamma strike above spot)")
        if r.gex_regime:
            lines.append(f"     Regime:             {r.gex_regime}")

    # ── Gamma flip zone: where net dealer gamma crosses zero ──
    # Populated only on the analyzer path (with_gamma_flip=True); stays
    # blank — and so invisible — on the searcher path. Display only.
    if r.gamma_flip_price is not None:
        lines.append(f"")
        lines.append(f"  🧲 GAMMA FLIP ZONE (zero-gamma level)")
        _side = "above" if (r.gamma_flip_pct or 0) > 0 else "below"
        lines.append(f"     Flip price:         ${r.gamma_flip_price:.2f}  "
                     f"({_side} spot, {r.gamma_flip_pct:+.1%})")
        if r.gamma_flip_regime:
            lines.append(f"     Now:                {r.gamma_flip_regime}")

    # ── FINRA short-volume nowcast: live SI direction ──
    if r.svr_available:
        lines.append(f"")
        lines.append(f"  📡 FINRA SHORT-VOLUME NOWCAST (daily, fills the "
                     f"biweekly SI gap)")
        lines.append(f"     Short-vol ratio:    {r.svr_recent:.0%} recent-3d "
                     f"vs {r.svr_baseline:.0%} baseline")
        lines.append(f"     Read:               {r.svr_trend}  "
                     f"(score {r.svr_score:.0f}/100)")

    # ── FTD T+35 close-out: mechanical catalyst ──
    if r.calibrated_prob is not None:
        lines.append(f"")
        lines.append(f"  📊 CALIBRATED PROBABILITY (fitted on graded history)")
        lines.append(f"     P(+15% in 10 days): {r.calibrated_prob:.0%}")

    if r.ftd_closeout_date:
        lines.append(f"")
        lines.append(f"  ⏰ FTD T+35 CLOSE-OUT (mechanical forced-buying)")
        lines.append(f"     Window:             ~{r.ftd_closeout_date} "
                     f"({r.ftd_closeout_days:+d}d)")
        lines.append(f"     Fails outstanding:  "
                     f"{(r.ftd_total_recent or 0):,} shares")

    lines.append(f"")
    lines.append(f"  💰 CTB VELOCITY: {r.ctb_velocity_score:.0f}/100")
    if r.ctb_now is not None:
        lines.append(f"     CTB now:            {r.ctb_now:.0f}%")
    if r.ctb_1w_ago is not None:
        lines.append(f"     CTB 1wk ago:        {r.ctb_1w_ago:.0f}%")
    if r.ctb_velocity is not None:
        arrow = "▲" if r.ctb_velocity > 0 else "▼"
        lines.append(f"     Velocity:           {arrow} {r.ctb_velocity:+.0f}pts ({r.ctb_velocity_pct:+.0%})")

    lines.append(f"")
    lines.append(f"  🔴 FTD ACCUMULATION: {r.ftd_score:.0f}/100")
    if r.ftd_total_recent is not None:
        lines.append(f"     Accumulated FTDs:   {r.ftd_total_recent:,}")
    if r.ftd_pct_float_accum is not None:
        lines.append(f"     % of float:         {r.ftd_pct_float_accum:.2%}")
    if r.ftd_pct_eff_float_accum is not None:
        lines.append(f"     % of EFF float:     {r.ftd_pct_eff_float_accum:.2%}"
                     f"  (tradeable float, not scored)")
    if r.ftd_closeout_adv_days is not None:
        lines.append(f"     Days of avg volume: {r.ftd_closeout_adv_days:.2f}")
    if r.ftd_closeout_verdict:
        lines.append(f"     Close-out read:     {r.ftd_closeout_verdict}")
    if r.scoring_version >= 2 and abs(r.ftd_score - r.ftd_score_v1) > 0.5:
        lines.append(f"     v1 would have said: {r.ftd_score_v1:.0f}/100 "
                     f"(impact {r.ftd_impact_factor_v1:.0%} vs "
                     f"{r.ftd_impact_factor:.0%})")
    if r.ftd_trend:
        lines.append(f"     Trend:              {r.ftd_trend}")

    if r.effective_float:
        lines.append(f"")
        lines.append(f"  📉 EFFECTIVE FLOAT (institutional stock netted out)")
        if r.float_shares:
            lines.append(f"     Reported float:     {r.float_shares:,.0f}")
        lines.append(f"     Effective float:    {r.effective_float:,.0f}")
        if r.float_tightness and r.float_tightness > 1.05:
            lines.append(f"     Tightness:          {r.float_tightness:.1f}x "
                         f"smaller than reported")

    lines.append(f"")
    lines.append(f"  ⚖️  CONVICTION MATRIX (CTB · DTC · SI trends)")
    lines.append(f"     CTB trend:  {r.ctb_trend or 'N/A':<12} ({r.ctb_1w_ago:.0f}% → {r.ctb_now:.0f}%)" if r.ctb_now is not None and r.ctb_1w_ago is not None else f"     CTB trend:  {r.ctb_trend or 'NO HISTORY'}")
    # The parenthetical pair must come from the SAME source as the label.
    # Showing a settlement-derived trend next to a 7-day snapshot pair reads
    # as a contradiction ("FLAT (9.7 -> 5.3d)") when it is really two
    # different measurements sitting on one line.
    _from_settlement = r.si_trend_source == "settlement"
    if not _from_settlement and r.dtc_now is not None and r.dtc_1w_ago is not None:
        lines.append(f"     DTC trend:  {r.dtc_trend or 'N/A':<12} "
                     f"({r.dtc_1w_ago:.1f} → {r.dtc_now:.1f}d)")
    else:
        lines.append(f"     DTC trend:  {r.dtc_trend or 'NO HISTORY'}")
    if not _from_settlement and r.si_now is not None and r.si_1w_ago is not None:
        lines.append(f"     SI  trend:  {r.si_trend or 'N/A':<12} "
                     f"({r.si_1w_ago:.0%} → {r.si_now:.0%})")
    else:
        lines.append(f"     SI  trend:  {r.si_trend or 'NO HISTORY'}")
    if r.si_trend_source:
        _src = ("settlement over settlement" if r.si_trend_source == "settlement"
                else "7-day snapshot")
        _chg = (f"  ({r.si_change_settlement:+.1%})"
                if r.si_change_settlement is not None else "")
        lines.append(f"     SI source:  {_src}{_chg}"
                     + (f"  settlement {r.settlement_date}"
                        f" [{r.settlement_age_days}d old]"
                        if r.settlement_date else ""))
        if r.si_trend_v1 and r.si_trend_v1 != r.si_trend:
            lines.append(f"     7d snapshot would have said: {r.si_trend_v1}")
        if r.settlement_consecutive > 1:
            lines.append(f"     Consecutive:{r.settlement_consecutive} "
                         f"settlements same direction")
    lines.append(f"     State:      {r.conviction_state}")
    lines.append(f"     Multiplier: {r.conviction_mult:.2f}x")

    if r.dtc_exchange or r.dtc_robust:
        lines.append(f"")
        lines.append(f"  📏 DAYS TO COVER by volume window")
        if r.dtc_exchange:
            lines.append(f"     exchange 10d mean:  {r.dtc_exchange:.2f}")
        if r.dtc_robust:
            lines.append(f"     10d MEDIAN:         {r.dtc_robust:.2f}"
                         f"   spike-robust")
        if r.dtc_60d:
            lines.append(f"     60d median:         {r.dtc_60d:.2f}"
                         f"   long horizon")
        if r.dtc_spread_low and r.dtc_spread_high:
            lines.append(f"     spread:             "
                         f"{r.dtc_spread_low:.2f}–{r.dtc_spread_high:.2f}"
                         f"   (denominator choice alone)")
        if r.dtc_spike_ratio:
            lines.append(f"     spike ratio:        {r.dtc_spike_ratio:.2f}"
                         + ("   volume spike-contaminated"
                            if r.dtc_spike_ratio >= 1.25 else ""))
    lines.append(f"")
    lines.append(f"  📅 CATALYST TIMING")
    if r.catalyst_window:
        wlabel = {
            "SWEET_SPOT": "🎯 SWEET SPOT — position-able pre-catalyst",
            "IMMINENT":   "⏰ IMMINENT — live but late",
            "PASSED":     "⚠️ PASSED — fuel likely spent",
            "TOO_FAR":    "⏳ TOO FAR — dead-money risk",
            "NONE":       "— no scheduled catalyst",
        }.get(r.catalyst_window, r.catalyst_window)
        lines.append(f"     Window:     {wlabel}")
        if r.days_to_earnings is not None:
            lines.append(f"     Earnings:   {r.days_to_earnings:+d} days out")
        lines.append(f"     Cat. score: {r.catalyst_score:.0f}/100")
        lines.append(f"     Cat. mult:  {r.catalyst_mult:.2f}x")
    else:
        lines.append(f"     (catalyst layer unavailable)")
    lines.append(f"")
    lines.append(f"     FINAL SCORE: {r.final_score:.0f}  "
                 f"(combined × {r.conviction_mult:.2f} conv × "
                 f"{r.catalyst_mult:.2f} cat)")

    if r.flags:
        lines.append(f"")
        lines.append(f"  🟢 SIGNALS FIRING:")
        for f in r.flags:
            lines.append(f"     • {f}")

    if r.warnings:
        lines.append(f"")
        lines.append(f"  ⚠️  NOTES:")
        for w in r.warnings[:4]:
            lines.append(f"     • {w}")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    tk = sys.argv[1] if len(sys.argv) > 1 else "GME"
    print(f"Running deep analysis on {tk}...\n")
    res = run_deep_analysis(tk, stage1_score=70.0)
    print(format_deep_display(res))
