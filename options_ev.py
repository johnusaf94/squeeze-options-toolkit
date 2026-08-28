"""
options_ev.py
=============
Strike matrix for expressing a squeeze thesis in CALLS: every liquid
strike/expiry ranked by expected value per dollar of premium under
  (a) the MARKET's own distribution (lognormal from each strike's IV), and
  (b) YOUR scenario distribution ("40:+15, 35:0, 25:-10" = 40% chance of
      +15%, 35% flat, 25% down 10% by expiry) — read as a MIXTURE OF
      LOGNORMALS, not three exact prices: each branch is a distribution
      centred on its scenario, widened to absorb whatever variance the
      market's ATM IV prices that your scenarios do not already explain,
spread-adjusted (you buy at the ASK), with breakeven, catalyst-window
coverage, and a disagreement column showing where your thesis diverges
from what's priced.

Sits in the DECISION layer: consumes the analyzer's DeepSqueezeResult
(implied move, FTD close-out date, calibrated_prob) — never feeds the
squeeze score. Called by squeeze_analyzer_gui after deep analysis, or
standalone:  python options_ev.py CRSP "40:+15, 35:0, 25:-10"

HONESTY, BUILT IN:
  * Scenario EV is only as good as YOUR probabilities. The tool labels it
    "under YOUR scenarios" everywhere. Garbage in, confident garbage out.
  * Market EV/$ uses each strike's own IV — by construction it hovers at
    ~zero minus the spread. Its job is to be the baseline you must
    disagree with, not a signal.
  * Costs are the FULL ROUND TRIP: you buy at the ask and pay commission;
    exits give up the dollar half-spread plus commission. Exit costs were
    previously ignored entirely, which flattered every contract on a wide
    chain — worth 9-18% of premium on the chains this tool is aimed at. The
    pre-cost figure prints beside the costed one so the size of that
    correction stays visible rather than being quietly absorbed.
  * calibrated_prob socket: when the learning loop's calibration
    activates (150+ graded rows), the system's own P(+15%/10d) prints
    beside your scenario's P — until then the footer says so plainly.
  * Scenario probabilities are CONTINUOUS. Read literally, three point
    masses gave P(above breakeven) exactly two possible values across a
    423-row chain — 0% or 40% — and assigned probability zero to every move
    larger than the best scenario, valuing tail contracts at nothing. The
    mixture fixes both; sigma=0 reproduces the old point-mass numbers
    exactly, which is how the change was verified.
  * Kelly is EXACT (maximizes E[log(1+f*r)]) over that distribution, with a
    disclosed 1% total-loss probability standing in for gaps, halts, stale
    quotes and model error. It is a ranking score, NOT a position size —
    full Kelly on a single option is far too aggressive to trade.
  * This ranks structures. It does not pick your trade or your size.
"""

import math
import sys
from bisect import bisect_left as _bisect_left
from datetime import datetime, date, timedelta
from typing import Optional

# One fetcher for the whole toolkit (defined in gamma_terrain, which
# imports the GEX math from squeeze_deep — single source of truth).
try:
    from gamma_terrain import fetch_expiries_yf
except Exception:                                   # pragma: no cover
    fetch_expiries_yf = None

MIN_OI = 0             # default: show even untraded strikes (flagged THIN)
THIN_OI = 10           # below this OI the row is flagged
STRIKE_BAND = (0.70, 1.80)   # consider strikes in this ×spot band
MAX_ROWS_PER_EXPIRY = 12     # display cap; heatmap uses ALL rows
WIDE_SPREAD = 0.20     # (ask-bid)/mid above this flags the quote
DEFAULT_HORIZON_DAYS = 14   # scenario moves are "by the catalyst" (or by
                            # ~10 trading days when no catalyst) — expiries
                            # SHORTER than this get sqrt-time-scaled moves
IV_CRUSH_MULT = 0.85   # post-catalyst IV haircut assumption for exit-EV
                       # (events deflate implied vol; 15% cut is a modest
                       # default — a disclosed assumption, not a fact)

# ── ROUND-TRIP COSTS ──
# Entry was always costed honestly (you pay the ASK). The EXIT was not: it
# valued the option at theoretical fair value and charged nothing to sell —
# no spread crossed, no commission. That is a systematic optimism, and it is
# largest exactly where this tool ranks contracts highest, because wide
# chains are where the fair-value-to-bid gap is widest.
#
# Exit is modeled as: give up the dollar half-spread (capped at a share of
# the option's value), then pay commission. See the note below on why dollars
# and not percent — that distinction is the difference between a working model
# and a broken one here.
COMMISSION_PER_CONTRACT = 0.65   # $/contract each way (Fidelity retail)
EXIT_CROSS = 1.0                 # 1.0 = you sell at the bid (you do)
EXIT_SPREAD_CAP = 0.25           # never give up more than this share of value

# WHY THE SPREAD IS CHARGED IN DOLLARS, NOT PERCENT
# -------------------------------------------------
# The obvious model — proceeds = value * (1 - spread_pct/2) — is wrong here,
# and the golden harness caught it on its first run. Deep-OTM chains quote
# bid 0.01 / ask 0.20, which is a spread_pct of 181%; that formula then hands
# back a NEGATIVE exit value and reported a -1042% "correction" on AAPL.
#
# Two errors compounded. First, spread_pct > 2 breaks the formula outright.
# Second, and worse, it applies the spread the option has TODAY (while it is a
# worthless lottery ticket) to its value AFTER the thesis lands. Market makers
# quote a roughly stable width in DOLLARS; an option that goes from $0.10 to
# $3.00 does not keep a 181% spread. Charging the dollar width — capped at a
# share of value, since nobody crosses a spread wider than the option is worth
# — is both closer to reality and immune to the degenerate case.


def _commission_ps() -> float:
    """Commission per SHARE — every price in this module is per share, and a
    contract is 100 of them. Mixing the two units silently mis-prices penny
    options by 100x, so the conversion lives in exactly one place."""
    return COMMISSION_PER_CONTRACT / 100.0


def entry_cost_for(ask: float) -> float:
    """What one share of the contract actually costs to get on."""
    return ask + _commission_ps()


def exit_proceeds_for(value: float, half_spread: float,
                      cross: float = EXIT_CROSS,
                      cap_frac: float = EXIT_SPREAD_CAP) -> float:
    """What a theoretical value of `value` actually converts to in cash.

    half_spread is the DOLLAR half-width at entry, (ask-bid)/2.

    An option worth less than the commission is ABANDONED, not sold — you do
    not pay $0.65 to collect $0.30. Proceeds floor at zero rather than going
    negative, which is what actually happens."""
    if value <= 0:
        return 0.0
    give_up = min(cross * max(half_spread, 0.0), cap_frac * value)
    return max(value - give_up - _commission_ps(), 0.0)


# ─────────────────────────────────────────────
# SCENARIOS
# ─────────────────────────────────────────────

def parse_scenarios(text: str) -> list:
    """'40:+15, 35:0, 25:-10' -> [(0.40, +0.15), (0.35, 0.0), (0.25, -0.10)]
    Probabilities are normalized to sum to 1 (a note is worth printing if
    they were far off). Raises ValueError on junk."""
    parts = [p.strip() for p in text.replace(";", ",").split(",") if p.strip()]
    if not parts:
        raise ValueError("no scenarios given")
    out = []
    for p in parts:
        if ":" not in p:
            raise ValueError(f"bad scenario '{p}' (want prob:move%)")
        a, b = p.split(":", 1)
        prob = float(a.strip().rstrip("%"))
        move = float(b.strip().rstrip("%"))
        out.append((prob, move / 100.0))
    tot = sum(p for p, _ in out)
    if tot <= 0:
        raise ValueError("scenario probabilities sum to zero")
    return [(p / tot, m) for p, m in out]


def scenario_stats(scenarios: list) -> dict:
    exp_move = sum(p * m for p, m in scenarios)
    p_up15 = sum(p for p, m in scenarios if m >= 0.15)
    return {"expected_move": exp_move, "p_up15": p_up15}


# ─────────────────────────────────────────────
# MARKET-IMPLIED MATH (lognormal, r=0)
# ─────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def market_p_above(spot: float, level: float, T: float, iv: float) -> Optional[float]:
    """Risk-neutral P(S_T > level) = N(d2), r=0."""
    if spot <= 0 or level <= 0 or T <= 0 or not iv or iv <= 0:
        return None
    d2 = ((math.log(spot / level) - 0.5 * iv * iv * T)
          / (iv * math.sqrt(T)))
    return _norm_cdf(d2)


def bs_call_ev(spot: float, K: float, T: float, iv: float) -> Optional[float]:
    """E[max(S_T-K,0)] under lognormal (undiscounted BS, r=0).

    Pass the FORWARD rather than spot to price in carry: with r=0 this is
    exactly Black-76, F*N(d1) - K*N(d2). Same formula, different centre."""
    if spot <= 0 or K <= 0 or T <= 0 or not iv or iv <= 0:
        return None
    sq = iv * math.sqrt(T)
    d1 = (math.log(spot / K) + 0.5 * iv * iv * T) / sq
    return spot * _norm_cdf(d1) - K * _norm_cdf(d1 - sq)


def learned() -> dict:
    """learned_params.json, read fresh each analysis (it is rewritten nightly).
    Empty dict when absent — every consumer must degrade gracefully."""
    try:
        import json as _json
        import os as _os
        _p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                           "learned_params.json")
        with open(_p, encoding="utf-8") as _f:
            return _json.load(_f)
    except Exception:
        return {}


# ─────────────────────────────────────────────
# SCENARIO DISTRIBUTION (mixture of lognormals)
# ─────────────────────────────────────────────
# WHAT WAS WRONG WITH THREE POINTS
# --------------------------------
# "40:+15, 35:0, 25:-10" was taken literally: the stock lands on exactly one
# of three prices. Nothing in between, nothing beyond. That produces:
#   * a STEP FUNCTION for P(above breakeven) — it can only ever read 0%, 25%,
#     40%, 60%, 65%, 75% or 100%, so two strikes a dollar apart show the same
#     probability until one crosses a scenario price and it jumps
#   * NO TAIL. A +40% move has probability zero by construction, so any
#     contract that only pays in the tail is valued at exactly nothing
#   * a Kelly denominator built from three samples, which is why the original
#     needed `L = max(L, 0.25)` — an arbitrary floor invented to stop
#     near-riskless-looking strikes reporting f*=1.0
#
# Each branch is now a LOGNORMAL centred on its scenario price rather than a
# spike at it. The scenario still says what you think happens; the mixture
# admits you do not know it to the penny.
#
# WHERE THE BRANCH WIDTH COMES FROM
# ---------------------------------
# Not invented — decomposed from the market's own ATM implied variance:
#     Var_total (market)  =  Var_between (your scenarios)  +  Var_within
# The spread BETWEEN your scenarios already explains part of what the market
# prices; the branches get what is left over. This is self-correcting: state
# wide scenarios and the branches tighten, state timid ones and they widen to
# fill the market's variance. A floor keeps them from collapsing to spikes
# when your scenarios are wider than the market's whole distribution.

MIX_MIN_WITHIN = 0.35    # branch sigma floor, as a fraction of ATM IV
MIX_NODES = 97           # quadrature nodes per branch (odd: lands on centre)
MIX_SPAN = 4.0           # +/- this many sigma sampled per branch


def branch_sigma(scenarios: list, atm_iv: Optional[float], T: float,
                 scale: float = 1.0) -> float:
    """Within-branch volatility from total-variance decomposition.
    Returns an annualized sigma. Falls back to a fraction of ATM IV when the
    scenarios already account for more variance than the market prices."""
    if not atm_iv or atm_iv <= 0 or T <= 0:
        return 0.0
    mean_l = sum(p * math.log(max(1.0 + m * scale, 1e-6)) for p, m in scenarios)
    var_between = sum(p * (math.log(max(1.0 + m * scale, 1e-6)) - mean_l) ** 2
                      for p, m in scenarios)
    var_total = atm_iv * atm_iv * T
    floor = (MIX_MIN_WITHIN * atm_iv) ** 2 * T
    var_within = max(var_total - var_between, floor)
    return math.sqrt(var_within / T)


def mixture_call_ev(spot: float, K: float, T: float, scenarios: list,
                    sigma: float, scale: float = 1.0) -> float:
    """E[max(S_T-K,0)] under the mixture. Each branch is priced with the
    SAME Black machinery as everything else — a lognormal whose mean is the
    scenario price. sigma<=0 degenerates to the original point masses, which
    is what makes this change verifiable against the old baseline."""
    acc = 0.0
    for p, m in scenarios:
        centre = spot * (1.0 + m * scale)
        if sigma <= 0 or T <= 0:
            acc += p * max(centre - K, 0.0)
        else:
            v = bs_call_ev(centre, K, T, sigma)
            acc += p * (v if v is not None else max(centre - K, 0.0))
    return acc


def mixture_p_above(spot: float, level: float, T: float, scenarios: list,
                    sigma: float, scale: float = 1.0) -> float:
    """P(S_T > level) under the mixture — continuous in `level`, which is
    the entire point. The step function is gone."""
    acc = 0.0
    for p, m in scenarios:
        centre = spot * (1.0 + m * scale)
        if sigma <= 0 or T <= 0:
            acc += p * (1.0 if centre > level else 0.0)
        else:
            q = market_p_above(centre, level, T, sigma)
            acc += p * (q if q is not None else
                        (1.0 if centre > level else 0.0))
    return acc


def mixture_nodes(spot: float, T: float, scenarios: list, sigma: float,
                  scale: float = 1.0) -> list:
    """[(weight, price)] discretization of the mixture, for the integrals
    that have no closed form (Kelly, exit values). Weights sum to 1."""
    if sigma <= 0 or T <= 0:
        return [(p, spot * (1.0 + m * scale)) for p, m in scenarios]
    sq = sigma * math.sqrt(T)
    out = []
    for p, m in scenarios:
        centre = spot * (1.0 + m * scale)
        if centre <= 0:
            out.append((p, 0.0))
            continue
        # lognormal with mean `centre`: median = centre*exp(-sq^2/2)
        mu = math.log(centre) - 0.5 * sq * sq
        step = 2.0 * MIX_SPAN / (MIX_NODES - 1)
        raw = []
        for i in range(MIX_NODES):
            z = -MIX_SPAN + i * step
            dens = math.exp(-0.5 * z * z)
            raw.append((dens, math.exp(mu + sq * z)))
        tot = sum(d for d, _ in raw)
        for d, s in raw:
            out.append((p * d / tot, s))
    return out


MIX_RUIN = 0.01     # disclosed probability of a total loss the model cannot see

# ── HOW IV BEHAVES AFTER THE MOVE ──
# Two refinements to exit valuation, both previously assumed away:
#
# 1. SMILE ROLL (sticky-delta). The old code valued the remaining life at the
#    strike's OWN current IV, no matter how far the stock had moved. But after
#    a +15% move a strike that was 20% OTM is now near the money, and near-the-
#    money IV is not what a 20%-OTM strike quotes today. Sticky-delta says
#    volatility attaches to MONEYNESS, not to the strike: IV(K, S2) is read off
#    today's smile at K*S/S2. Matters most on the OTM strikes this tool ranks
#    highest, which is exactly where it was most wrong.
#
# 2. CRUSH DEPENDS ON THE OUTCOME. One multiplier for every branch says a
#    squeeze that rips and a catalyst that flops deflate implied vol equally.
#    They do not: a violent up-move feeds realized vol back into implied and
#    partially offsets the event crush, while a failed catalyst removes the
#    reason anyone was paying for vol at all. Scaled continuously off the
#    realized move rather than by branch, so it stays smooth under the mixture.
CRUSH_MOVE_SENS = 1.0        # d(relative crush) / d(move)
CRUSH_REL_CLAMP = (0.85, 1.25)
CRUSH_ABS_CLAMP = (0.30, 1.10)


def crush_for_move(base_crush: float, move: float) -> float:
    """Post-event IV multiplier given the realized move. base_crush is the
    term-structure (or default) estimate; a rip crushes less, a flop more."""
    rel = 1.0 + CRUSH_MOVE_SENS * move
    rel = min(max(rel, CRUSH_REL_CLAMP[0]), CRUSH_REL_CLAMP[1])
    return min(max(base_crush * rel, CRUSH_ABS_CLAMP[0]), CRUSH_ABS_CLAMP[1])


def rolled_iv(smile: list, K: float, spot: float, s2: float,
              fallback: float) -> float:
    """Sticky-delta IV lookup: the vol for strike K once spot has moved to s2
    is today's vol at the strike with the SAME moneyness, K*spot/s2."""
    if not smile or s2 <= 0 or spot <= 0:
        return fallback
    v = _interp_iv(smile, K * spot / s2)
    return v if (v and v > 0) else fallback


def kelly_fraction(nodes: list, ruin: float = MIX_RUIN) -> Optional[float]:
    """Exact Kelly: the f maximizing E[log(1 + f*r)] over the distribution.

    This replaces the binary approximation (p/L - q/b) and the arbitrary
    `L = max(L, 0.25)` tail floor it needed.

    WHY A RUIN TERM IS STILL REQUIRED
    ---------------------------------
    The mixture is truncated at +/-MIX_SPAN sigma, so it asserts a hard floor
    under every contract. A 1-day deep-ITM AAPL call came out "unable to lose
    more than 34%", and exact Kelly correctly answered: borrow and bet more
    than the bankroll. Ten AAPL rows pinned at f*=1.0, destroying the ranking.
    The Kelly math was right; the distribution was lying about its own tail.

    Options gap, underlyings halt (routine in this universe), quotes are stale,
    and the model itself can be wrong. So a small explicit probability of TOTAL
    loss is mixed in. Unlike the floor it replaces, this is a statement about
    the world with an honest name and a number you can argue with, and it is
    what mathematically bounds f* below 1 for every contract.

    Returns the optimal fraction in [0,1] when the bet is favorable, or the
    (negative) mean return when it is not, so the heat map keeps its gradient
    instead of flattening every bad contract to the same value.

    NOTE: this is FULL Kelly — a ranking score, not a position size. Full
    Kelly on a single option is far too aggressive to trade."""
    if not nodes:
        return None
    if ruin > 0:
        nodes = [(w * (1.0 - ruin), r) for w, r in nodes] + [(ruin, -1.0)]
    mean_r = sum(w * r for w, r in nodes)
    if mean_r <= 0:
        return max(min(mean_r, 0.0), -1.0)
    # g'(f) = E[r / (1 + f*r)] is strictly decreasing; bisect it.
    def _gp(f):
        return sum(w * r / (1.0 + f * r) for w, r in nodes if 1.0 + f * r > 1e-12)
    lo, hi = 0.0, 0.999
    if _gp(hi) > 0:
        return 1.0
    # Bisect to a tolerance instead of a fixed 60 iterations. This was 48% of
    # the whole matrix build — 20,801 evaluations of a 291-node sum on one
    # chain — and 60 halvings of a unit interval resolve f to 1e-18, which is
    # meaningless for a number displayed as a percentage. 1e-10 is still far
    # tighter than the harness's 1e-9 comparison epsilon, so results are
    # unchanged; it just stops after ~34 halvings instead of 60.
    for _ in range(60):
        if hi - lo < 1e-10:
            break
        mid = (lo + hi) / 2.0
        if _gp(mid) > 0:
            lo = mid
        else:
            hi = mid
    return max(min((lo + hi) / 2.0, 1.0), 0.0)


# ─────────────────────────────────────────────
# IMPLIED FORWARD (put-call parity)
# ─────────────────────────────────────────────
# WHY THIS MATTERS MORE HERE THAN ANYWHERE ELSE
# ---------------------------------------------
# Everything above priced off SPOT with r=0 and no borrow cost. For a normal
# stock that is a rounding error. For the names this tool hunts it is not:
# a squeeze candidate costs 20-150%/yr to borrow, and hard borrow drags the
# forward BELOW spot. Pricing calls off spot therefore OVERVALUES them —
# systematically, and worst exactly where the borrow is hardest, which is
# precisely the setup the scanner is looking for.
#
# The market already prices this and will tell us if asked. Put-call parity
# at r=0 says C - P = F - K, so every strike with two-sided quotes on BOTH
# legs implies its own forward. The chain's own opinion, no assumption of
# ours involved. Puts were already being fetched and thrown away.

FWD_BAND = (0.50, 1.50)     # implied forward must land within this x spot
FWD_MIN_STRIKES = 4         # below this, the estimate is one bad quote
FWD_ATM_WINDOW = 0.25       # only strikes within +/-25% of spot are usable
FWD_MIN_T = 5.0 / 365.0     # under a week, parity is all spread and no signal
FWD_MAD_MULT = 1.0          # signal must exceed the scatter that produced it

# THE NOISE FLOOR — why the guards above exist
# --------------------------------------------
# First run of this extractor on real chains:
#   AAPL  10 expiries, 21-39 strikes each, F/S rising smoothly +0.16% -> +0.75%
#         with tenor, carry converging to ~+5%/yr. Textbook: that is the
#         risk-free rate the old r=0 assumption was discarding.
#   HTZ   2 strikes per expiry, F/S oscillating +1.35%, -0.13%, +1.10%, -1.37%
#         with the SIGN flipping between adjacent expiries, carry ranging
#         +69.7%/yr to -3.4%/yr. That is not a borrow cost. That is two wide
#         quotes and a coarse strike grid.
# Unguarded, that junk fed exp(carry * t_rem) in the exit valuation and moved
# sc_ev_exit by a mean of +19% on AAPL alone — a large, confident, entirely
# fabricated correction.
#
# So an estimate must now clear three bars: enough strikes, enough time for
# the signal to exceed the spread, and an offset from spot larger than the
# scatter of the individual per-strike estimates (median absolute deviation).
# The third is the real test — if the strikes disagree with each other by more
# than they collectively disagree with spot, there is no measurement here.
# Failing any bar returns spot with the reason attached. Falling back to spot
# is not a defeat; it is the old behavior, which was at least not inventing
# numbers.


def implied_forward(calls: list, puts: list, spot: float,
                    T: float) -> dict:
    """-> {fwd, carry, n, method, detail}. carry is the continuously
    compounded annual rate implied by F = S*exp(carry*T); it is NEGATIVE
    for hard-to-borrow names, which is the whole point.

    Robustness over precision: near-ATM strikes only (parity is swamped by
    spread noise far from the money), both legs must be two-sided, and the
    MEDIAN is taken rather than the mean so a single stale quote cannot
    move it. Falls back to spot — never to a guess — when the chain cannot
    support an estimate, and says so."""
    out = {"fwd": spot, "carry": 0.0, "n": 0, "method": "spot (no parity)",
           "detail": ""}
    if spot <= 0 or T <= 0 or not calls or not puts:
        return out
    pmap = {}
    for p in puts:
        K = _num(p.get("strike"))
        pb, pa = _num(p.get("bid")), _num(p.get("ask"))
        if K > 0 and pb > 0 and pa > 0:
            pmap[round(K, 4)] = (pb + pa) / 2.0
    fwds = []
    for c in calls:
        K = _num(c.get("strike"))
        cb, ca = _num(c.get("bid")), _num(c.get("ask"))
        if K <= 0 or cb <= 0 or ca <= 0:
            continue
        if abs(K - spot) / spot > FWD_ATM_WINDOW:
            continue
        pm = pmap.get(round(K, 4))
        if pm is None:
            continue
        fwds.append(K + (cb + ca) / 2.0 - pm)      # F = K + C - P
    if len(fwds) < FWD_MIN_STRIKES:
        out["detail"] = (f"only {len(fwds)} strike(s) with two-sided quotes "
                         f"on both legs near the money "
                         f"(need {FWD_MIN_STRIKES})")
        return out
    if T < FWD_MIN_T:
        out["detail"] = (f"{T * 365:.0f}d to expiry — too short for parity to "
                         f"say anything the spread does not drown")
        return out
    fwds.sort()
    F = fwds[len(fwds) // 2]
    if not (spot * FWD_BAND[0] <= F <= spot * FWD_BAND[1]):
        out["detail"] = (f"parity implied ${F:,.2f} vs spot ${spot:,.2f} — "
                         f"outside sanity band, quotes are junk")
        return out
    # Median absolute deviation: how much do the individual strikes disagree
    # with each other? If that scatter is as large as the offset from spot,
    # the offset is an artifact of the scatter and means nothing.
    devs = sorted(abs(f - F) for f in fwds)
    mad = devs[len(devs) // 2]
    if abs(F - spot) <= FWD_MAD_MULT * mad:
        out["detail"] = (f"forward ${F:,.2f} is within the strike-to-strike "
                         f"scatter (+/-${mad:,.2f}) of spot ${spot:,.2f} — "
                         f"no measurable carry")
        return out
    carry = math.log(F / spot) / T if (F > 0 and T > 0) else 0.0
    return {"fwd": F, "carry": carry, "n": len(fwds),
            "method": "put-call parity",
            "detail": (f"forward ${F:,.2f} vs spot ${spot:,.2f} "
                       f"({carry * 100:+.1f}%/yr implied carry, "
                       f"{len(fwds)} strikes, scatter +/-${mad:,.2f})")}


# ─────────────────────────────────────────────
# PER-STRIKE ANALYSIS
# ─────────────────────────────────────────────

def _num(v) -> float:
    """NaN-safe numeric coercion. NaN is truthy in Python, so `x or 0`
    guards do NOT catch it — it then passes every comparison as False and
    detonates at int() ('cannot convert float NaN to integer')."""
    try:
        f = float(v)
        return f if f == f else 0.0     # NaN != NaN
    except (TypeError, ValueError):
        return 0.0


CRUSH_LEVELS = (1.00, 0.85, 0.70, 0.50)   # post-event IV as fraction of quoted


def exit_ev_for(spot: float, K: float, ask: float, iv: float, T: float,
                catalyst_T: Optional[float], scenarios: list,
                iv_crush: float = IV_CRUSH_MULT,
                half_spread: float = 0.0,
                costs: bool = True,
                carry: float = 0.0,
                sigma: float = 0.0,
                smile: Optional[list] = None,
                spot_ref: Optional[float] = None) -> Optional[float]:
    """EV per premium dollar, selling ON the catalyst date after the
    scenario move, remaining time valued at iv*iv_crush. The ONE
    implementation of exit-EV — the table metric, the heatmap, and the
    crush simulator all call this, so they can never disagree.

    costs=True (default) charges the real round trip: commission on the way
    in, and on the way out the half-spread plus commission. costs=False
    reproduces the original cost-free math exactly, which is what the
    side-by-side 'gross' column and the golden-file diff need."""
    if catalyst_T is None or not (0 <= catalyst_T <= T) or ask <= 0:
        return None
    cost = entry_cost_for(ask) if costs else ask
    t_rem = T - catalyst_T
    acc = 0.0
    # sigma>0 integrates the scenario mixture across prices at the catalyst;
    # sigma=0 collapses to the original three point masses, which is how the
    # frozen pre-fix comparison column stays reproducible.
    for w, s2 in mixture_nodes(spot, max(catalyst_T, 1e-6), scenarios,
                               sigma, 1.0):
        if t_rem <= 1.0 / 365.0 or not iv:
            v = max(s2 - K, 0.0)
        else:
            # The scenario move lands on the STOCK; the option's remaining
            # life must then be priced off the forward from that new spot,
            # not the spot itself. On a hard-to-borrow name that forward is
            # below the stock and the remaining time value is worth less.
            f2 = s2 * math.exp(carry * t_rem) if carry else s2
            _sref = spot_ref if spot_ref else spot
            _iv2 = rolled_iv(smile, K, _sref, s2, iv) if smile else iv
            _cr = crush_for_move(iv_crush, s2 / _sref - 1.0) if _sref else iv_crush
            v = bs_call_ev(f2, K, t_rem, _iv2 * _cr)
            if v is None:
                v = max(s2 - K, 0.0)
        acc += w * (exit_proceeds_for(v, half_spread) if costs else v)
    return acc / cost - 1.0


def _time_scale(T: float, horizon_T: Optional[float]) -> float:
    """sqrt-time factor for scenario moves on expiries SHORTER than the
    horizon. A '+15% by catalyst' thesis cannot be fully credited to an
    expiry that dies in 2 days — diffusion scales with sqrt(time). This
    is what stopped penny weeklies on mega-caps showing +17,000% EV.
    Expiries at/beyond the horizon keep the full move (factor 1.0)."""
    if not horizon_T or horizon_T <= 0 or T >= horizon_T:
        return 1.0
    return (max(T, 0.0) / horizon_T) ** 0.5


def analyze_strike(spot: float, row: dict, T: float, scenarios: list,
                   min_oi: int = MIN_OI,
                   band: tuple = STRIKE_BAND,
                   catalyst_T: Optional[float] = None,
                   iv_crush: float = IV_CRUSH_MULT,
                   horizon_T: Optional[float] = None,
                   fwd: Optional[float] = None,
                   carry: float = 0.0,
                   atm_iv: Optional[float] = None,
                   smile: Optional[list] = None) -> Optional[dict]:
    K = _num(row.get("strike"))
    bid = _num(row.get("bid"))
    ask = _num(row.get("ask"))
    last = _num(row.get("lastPrice"))
    oi = _num(row.get("openInterest"))
    iv = _num(row.get("impliedVolatility"))
    # Mark whether the strike's own IV is real or will need interpolation
    iv_is_real = iv > 0.001
    # Scenario EV is an AT-EXPIRY calculation, so a missing bid doesn't
    # invalidate it — it means exiting early is ugly. Show, don't hide:
    # zero-bid and thin-OI strikes are admitted and FLAGGED.
    #
    # AFTER-HOURS MODE: yfinance clears bid/ask quotes overnight (0/NaN
    # -> scrubbed to 0), which used to blank the entire chain outside
    # market hours. When there is no ask but a LAST TRADE exists, cost
    # the analysis off the last print and FLAG the row stale — an honest
    # overnight estimate beats an empty screen, but it is yesterday's
    # price, not a live offer.
    stale_quote = False
    if ask <= 0.009:
        if last > 0.009:
            ask = last
            stale_quote = True
        else:
            return None                   # no ask, no last = nothing to price
    if K <= 0 or oi < min_oi:
        return None
    if not (band[0] <= K / spot <= band[1]):
        return None
    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid if (mid > 0 and not stale_quote) else 1.0
    # Cost basis is the ask PLUS commission. Breakeven moves with it: the
    # stock has to cover the fee too, and on a $0.30 contract that fee is
    # 2% of the position.
    entry = entry_cost_for(ask)
    breakeven = K + entry
    be_move = breakeven / spot - 1.0

    # Scenario side. Moves are sqrt-time-scaled for expiries shorter than
    # the horizon. NOTE: no exit spread is charged here — this branch values
    # the option AT EXPIRY, where an ITM call settles at intrinsic rather
    # than being sold across a spread. Exit costs belong to sc_ev_exit,
    # which models actually selling the thing before it expires.
    _f = _time_scale(T, horizon_T)
    sigma = branch_sigma(scenarios, atm_iv, T, _f)
    _exp_payoff = mixture_call_ev(spot, K, T, scenarios, sigma, _f)
    sc_ev = _exp_payoff / entry - 1.0
    sc_p_be = mixture_p_above(spot, breakeven, T, scenarios, sigma, _f)
    # best_mult stays a POINT-MASS figure on purpose: it answers "how many
    # times my money back if the best scenario lands", and under a mixture
    # the best case is unbounded, which is not a useful number.
    payoffs = [max(spot * (1.0 + m * _f) - K, 0.0) for _, m in scenarios]
    best_mult = max(po / entry for po in payoffs)
    # frozen pre-fix reference: point masses, no costs
    sc_ev_gross = (sum(p * po for (p, _), po in zip(scenarios, payoffs))
                   / ask - 1.0)

    # Market side (this strike's own IV), centred on the FORWARD. The
    # market's risk-neutral distribution is centred there, not on spot —
    # that is what "risk-neutral" means once carry is non-zero.
    F = fwd if (fwd and fwd > 0) else spot
    mkt_p_be = market_p_above(F, breakeven, T, iv)
    p_itm_mkt = market_p_above(F, K, T, iv)
    ev = bs_call_ev(F, K, T, iv)
    mkt_ev = (ev / ask - 1.0) if ev is not None else None

    # EXIT-AT-CATALYST EV (theta-aware): value the option ON the catalyst
    # date, after the scenario move, with its REMAINING time and crushed
    # IV — models selling into the event (the actual trading pattern)
    # rather than holding to expiry. None if the option expires before
    # the catalyst (scenario moves are "by catalyst"; applying them to an
    # earlier expiry would be dishonest).
    half_spread = max(ask - bid, 0.0) / 2.0
    sigma_cat = (branch_sigma(scenarios, atm_iv, max(catalyst_T, 1e-6), 1.0)
                 if catalyst_T else 0.0)
    sc_ev_exit = exit_ev_for(spot, K, ask, iv, T, catalyst_T,
                             scenarios, iv_crush,
                             half_spread=half_spread, costs=True,
                             carry=carry, sigma=sigma_cat,
                             smile=smile, spot_ref=spot)
    sc_ev_exit_gross = exit_ev_for(spot, K, ask, iv, T, catalyst_T,
                                   scenarios, iv_crush, costs=False)

    # ── KELLY SCORE: the "best bet" metric ──
    # f* = p/L - (1-p)/b  (binary Kelly, asymmetric loss)
    #   p = scenario prob of finishing profitable, b = avg win multiple,
    #   L = avg loss fraction. Uses EXIT values when a catalyst exists
    #   (sell-into-the-event, crush applied), else expiry payoffs.
    # Kelly penalizes lotto strikes (low p) AND deep ITM (low b) — it
    # peaks at the efficient contract, which pure EV cannot do.
    # Kelly integrates the SAME distribution, costs, forward, rolled smile and
    # outcome-dependent crush that sc_ev_exit uses. Any divergence here means
    # the ranking recommends one contract while the EV column shows another.
    _f2 = _time_scale(T, horizon_T)
    _hs = max(ask - bid, 0.0) / 2.0
    if catalyst_T is not None and 0 <= catalyst_T <= T:
        t_rem = T - catalyst_T
        rets = []
        for w, s2 in mixture_nodes(spot, max(catalyst_T, 1e-6), scenarios,
                                   sigma_cat, 1.0):
            f2b = s2 * math.exp(carry * t_rem) if carry else s2
            _ivk = rolled_iv(smile, K, spot, s2, iv) if smile else iv
            _crk = crush_for_move(iv_crush, s2 / spot - 1.0)
            v = (max(s2 - K, 0.0) if (t_rem <= 1.0 / 365.0 or not iv)
                 else (bs_call_ev(f2b, K, t_rem, _ivk * _crk)
                       or max(s2 - K, 0.0)))
            rets.append((w, exit_proceeds_for(v, _hs) / entry - 1.0))
    else:
        rets = [(w, max(sT - K, 0.0) / entry - 1.0)
                for w, sT in mixture_nodes(spot, T, scenarios, sigma, _f2)]
    kelly = kelly_fraction(rets)
    # The mean of exactly what Kelly integrated. For exit-path rows this MUST
    # equal sc_ev_exit; the golden harness asserts it every run. A silent
    # patch failure once left Kelly ranking off point masses while the EV
    # column showed the mixture — this invariant makes that impossible to
    # miss again.
    kelly_ev = sum(w * r for w, r in rets) if rets else None

    return {"strike": K, "bid": bid, "ask": ask, "spread_pct": spread_pct,
            "entry_cost": entry, "half_spread": half_spread, "fwd": F,
            "branch_sigma": sigma, "branch_sigma_cat": sigma_cat,
            "sc_ev_gross": sc_ev_gross, "sc_ev_exit_gross": sc_ev_exit_gross,
            "oi": int(oi), "iv": iv, "breakeven": breakeven,
            "be_move": be_move, "sc_ev": sc_ev, "best_mult": best_mult,
            "sc_p_be": sc_p_be, "mkt_p_be": mkt_p_be,
            "disagree": (sc_p_be - mkt_p_be) if mkt_p_be is not None else None,
            "mkt_ev": mkt_ev, "p_itm_mkt": p_itm_mkt,
            "sc_ev_exit": sc_ev_exit,
            "no_bid": bid <= 0 and not stale_quote,
            "thin": oi < THIN_OI,
            "stale": stale_quote, "iv_is_real": iv_is_real,
            "kelly": kelly, "kelly_ev": kelly_ev,
            "wide": spread_pct > WIDE_SPREAD or bid <= 0}


def _coverage(expiry_str: str, catalyst_iso: str) -> str:
    """Does this expiry cover the catalyst date?"""
    try:
        exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        cat = datetime.strptime(catalyst_iso[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return ""
    d = (exp - cat).days
    if d >= 0:
        return f"COVERS catalyst (+{d}d buffer)"
    return f"⚠ EXPIRES {-d}d BEFORE catalyst"


def build_matrix(spot: float, expiries: list, scenarios: list,
                 catalyst_iso: str = "", max_dte: Optional[int] = None,
                 min_oi: int = MIN_OI, band: tuple = STRIKE_BAND,
                 iv_crush: float = IV_CRUSH_MULT,
                 as_of: Optional[date] = None,
                 borrow_pct: Optional[float] = None) -> list:
    """-> list of expiry blocks: {expiry, days, coverage, rows}. Rows are
    UNCAPPED here (the heatmap wants everything; the text formatter caps
    its own display). max_dte drops expiries further out than N days —
    squeeze expressions are short-dated by nature. Calls only.

    as_of overrides "today" for every time calculation. Production never
    passes it; the golden-file harness does, so a chain captured on one day
    replays with the SAME days-to-expiry on any later day. Without it, every
    replay differs from its baseline purely because time passed, and the
    diff — the whole point of a golden file — is drowned in noise."""
    blocks = []
    today = as_of or date.today()
    catalyst_T = None
    if catalyst_iso:
        try:
            _cd = (datetime.strptime(catalyst_iso[:10], "%Y-%m-%d").date()
                   - today).days
            if _cd >= 0:
                catalyst_T = _cd / 365.0
        except (ValueError, TypeError):
            catalyst_T = None
    for T, calls, puts in expiries:
        if not calls:
            continue
        exp_str = calls[0].get("expiry", "")
        try:
            days = (datetime.strptime(exp_str, "%Y-%m-%d").date() - today).days
        except (ValueError, TypeError):
            days = round(T * 365)
        if max_dte is not None and days > max_dte:
            continue
        rows = []
        _horizon_T = (catalyst_T if catalyst_T and catalyst_T > 0
                      else DEFAULT_HORIZON_DAYS / 365.0)
        _fw = implied_forward(calls, puts, spot, max(T, 1.0 / 365.0))
        # PARITY FAILS ON EXACTLY THE NAMES THAT NEED IT MOST. Measured on
        # real chains: AAPL resolved 8/10 expiries, HTZ resolved 0/10 — one
        # or two two-sided put quotes per expiry is not enough. But a hard-to-
        # borrow name is precisely where ignoring carry overvalues calls. The
        # deep layer already measures cost-to-borrow (ctb_now), so when the
        # chain cannot speak, use the borrow rate directly: carry = -ctb.
        if (_fw["method"] != "put-call parity" and borrow_pct
                and borrow_pct > 0):
            _c = -float(borrow_pct) / 100.0
            _Te = max(T, 1.0 / 365.0)
            _fw = {"fwd": spot * math.exp(_c * _Te), "carry": _c, "n": 0,
                   "method": "borrow rate (CTB)",
                   "detail": (f"chain too thin for parity; carry from "
                              f"measured cost-to-borrow {borrow_pct:.0f}%/yr")}
        _aiv = _atm_iv(spot, calls)
        # Raw smile straight off the chain (not off analyzed rows, which do
        # not exist yet): [(strike, iv)] for every strike quoting a real IV.
        _smile = sorted((_num(c.get("strike")), _num(c.get("impliedVolatility")))
                        for c in calls
                        if _num(c.get("strike")) > 0
                        and _num(c.get("impliedVolatility")) > 0.001)
        for r in calls:
            a = analyze_strike(spot, r, max(T, 1.0 / 365.0), scenarios,
                               min_oi=min_oi, band=band,
                               catalyst_T=catalyst_T,
                               iv_crush=iv_crush,
                               horizon_T=_horizon_T,
                               fwd=_fw["fwd"], carry=_fw["carry"],
                               atm_iv=_aiv, smile=_smile)
            if a:
                rows.append(a)
        if not rows:
            continue
        # ── FILL MISSING IVs from neighbors (the NaN-scrub-to-0 fix) ──
        # yfinance returns NaN IV on some strikes (especially OTM,
        # after-hours). Our NaN-hardening scrubs NaN→0, which prevents
        # crashes but makes P(ITM) binary (0% or 100%) and val_edge
        # degenerate. Fix: interpolate IV from neighboring strikes.
        _T_eff = max(T, 1.0 / 365.0)
        _smile_pts = sorted((r["strike"], r["iv"]) for r in rows
                            if r.get("iv_is_real") and r["iv"] > 0)
        if _smile_pts:
            for r in rows:
                if not r.get("iv_is_real") and r["iv"] <= 0.001:
                    interp = _interp_iv(_smile_pts, r["strike"])
                    if interp and interp > 0:
                        r["iv"] = interp
                        # recompute market-derived fields with real IV
                        r["mkt_p_be"] = market_p_above(_fw["fwd"],
                                                       r["breakeven"],
                                                       _T_eff, interp)
                        r["p_itm_mkt"] = market_p_above(_fw["fwd"],
                                                        r["strike"],
                                                        _T_eff, interp)
                        r["mkt_ev"] = ((bs_call_ev(_fw["fwd"], r["strike"],
                                                   _T_eff, interp) or 0)
                                       / r["entry_cost"] - 1.0) if r["ask"] > 0 else None

        # ── VALUE vs MARKET (no scenario) ──
        for r in rows:
            others = sorted((x["strike"], x["iv"]) for x in rows
                            if x is not r and x.get("iv") and x["iv"] > 0)
            r["val_edge"] = None
            if len(others) >= 2 and r["ask"] > 0:
                iv_loo = _interp_iv(others, r["strike"])
                fair = (bs_call_ev(_fw["fwd"], r["strike"], _T_eff, iv_loo)
                        if iv_loo else None)
                if fair is not None:
                    # Measured against what you actually pay to get on, so
                    # the "value" ruler and the EV columns share a cost basis.
                    r["val_edge"] = fair / r["entry_cost"] - 1.0
        rows.sort(key=lambda x: x["sc_ev"], reverse=True)
        blocks.append({"expiry": exp_str, "days": days,
                       "fwd": _fw["fwd"], "carry": _fw["carry"],
                       "fwd_method": _fw["method"], "fwd_n": _fw["n"],
                       "fwd_detail": _fw["detail"], "atm_iv": _aiv,
                       "smile": _smile,
                       # Puts need their OWN smile. Reading a put's vol off
                       # the CALL smile means looking it up at a deep-ITM call
                       # strike, where quoted IVs are unreliable — that valued
                       # a $225 put at $1.64 with the stock at $341, off a
                       # rolled vol near 166%.
                       "put_smile": sorted(
                           (_num(p.get("strike")),
                            _num(p.get("impliedVolatility")))
                           for p in puts
                           if _num(p.get("strike")) > 0
                           and _num(p.get("impliedVolatility")) > 0.001),
                       # Puts, normalized to the same shape as call rows.
                       # The matrix itself still ranks calls only, but the
                       # structure search needs the put side to express a
                       # downside thesis or finance a spread — and the chain
                       # was already fetched, so discarding it was waste.
                       "put_rows": [
                           {"strike": _num(p.get("strike")),
                            "bid": _num(p.get("bid")),
                            "ask": _num(p.get("ask")),
                            "iv": _num(p.get("impliedVolatility")),
                            "oi": int(_num(p.get("openInterest")))}
                           for p in puts
                           if _num(p.get("strike")) > 0
                           and band[0] <= _num(p.get("strike")) / spot <= band[1]],
                       "coverage": _coverage(exp_str, catalyst_iso) if catalyst_iso else "",
                       "rows": rows})
    return blocks


# ─────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────
# Row color grading (GUI maps tags to colors; CLI strips them):
#   good: EV/$ scn >= +30%   mid: 0..+30%   bad: negative
EV_GOOD = 0.30


def _row_tag(r: dict) -> str:
    if r["sc_ev"] >= EV_GOOD:
        return "good"
    if r["sc_ev"] >= 0:
        return "mid"
    return "bad"


def matrix_lines(ticker: str, spot: float, blocks: list, scenarios: list,
                 implied_move_pct: Optional[float] = None,
                 calibrated_prob: Optional[float] = None,
                 catalyst_label: str = "",
                 catalyst_iso: str = "",
                 iv_crush: float = IV_CRUSH_MULT,
                 crush_method: str = "",
                 as_of: Optional[date] = None,
                 deep_verdict: str = "",
                 structures: Optional[dict] = None) -> list:
    """The report as [(line_text, tag)] so the GUI can colorize.
    Tags: hdr / dim / good / mid / bad / warn / note."""
    st = scenario_stats(scenarios)
    L = []
    L.append((f"  {ticker.upper()} @ ${spot:,.2f} — CALL STRIKE MATRIX "
              f"(cost = ask + ${COMMISSION_PER_CONTRACT:.2f}/contract; "
              f"exits pay the spread)", "hdr"))
    scn_str = ", ".join(f"{p:.0%}:{m:+.0%}" for p, m in scenarios)
    L.append((f"  Your scenarios: {scn_str}   "
              f"(E[move] {st['expected_move']:+.1%}, P(≥+15%) {st['p_up15']:.0%})",
              "dim"))
    if implied_move_pct:
        gap = st["expected_move"] - implied_move_pct
        inside = gap <= 0
        L.append((f"  Market implied move (ATM straddle): ±{implied_move_pct:.1%} "
                  f"— your thesis {'is inside' if inside else 'exceeds'} it "
                  f"by {abs(gap):.1%}"
                  + ("  (you're paying for vol you don't forecast)" if inside else ""),
                  "warn" if inside else "dim"))
    _fwd_ok = [b for b in blocks
               if b.get("fwd_method", "").startswith(("put-call", "borrow"))]
    if _fwd_ok:
        _carries = sorted(b["carry"] for b in _fwd_ok)
        _med = _carries[len(_carries) // 2]
        _srcs = sorted({b["fwd_method"] for b in _fwd_ok})
        L.append((f"  Forward: {len(_fwd_ok)}/{len(blocks)} expiries priced "
                  f"off a carry-adjusted forward via {' + '.join(_srcs)} "
                  f"(median carry {_med * 100:+.1f}%/yr). Negative carry = "
                  f"hard borrow, and it makes calls worth LESS than the "
                  f"spot-based price.", "dim"))
    elif blocks:
        L.append(("  Forward: no expiry had enough two-sided put quotes to "
                  "measure carry — priced off SPOT, as before. On a "
                  "hard-to-borrow name that overstates call value; this "
                  "chain simply cannot say by how much.", "warn"))
    # ── WHAT YOUR OWN GRADED HISTORY SAYS ABOUT BUYING PREMIUM HERE ──
    _lp = learned()
    _vp = _lp.get("vol_premium") or {}
    if _vp.get("ratio"):
        _ratio, _n = _vp["ratio"], _vp.get("n", 0)
        _exc = _vp.get("exceed_rate")
        _tag = "warn" if _ratio < 1.0 else "dim"
        L.append((f"  Vol premium: across {_n} graded episodes of your own "
                  f"candidates, realized 10-day moves ran {_ratio:.2f}x the "
                  f"implied move"
                  + (f" and exceeded it only {_exc:.0%} of the time"
                     if _exc is not None else "")
                  + ".", _tag))
        if _ratio < 1.0:
            L.append((f"  -> Long premium has been the LOSING side on this "
                      f"universe by ~{(1 - _ratio):.0%} before spreads and "
                      f"commission. A long call needs your forecast to beat "
                      f"implied by more than that gap; otherwise the honest "
                      f"expressions are a debit SPREAD (which sells the "
                      f"expensive wing) or no trade.", "warn"))
        if implied_move_pct:
            _exp_abs = sum(p * abs(m) for p, m in scenarios)
            _need = implied_move_pct * _ratio
            if _exp_abs <= _need:
                L.append((f"  -> GATE: your scenarios expect E|move| "
                          f"{_exp_abs:.1%}, under the {_need:.1%} that "
                          f"history-discounted implied vol already prices. "
                          f"On these numbers you are buying movement you do "
                          f"not forecast.", "warn"))
    # ── VERDICT FAMILIES THAT HAVE LOST MONEY ──
    _vs = _lp.get("verdicts") or {}
    _fam = (deep_verdict or "").split("—")[0].split("(")[0]
    _fam = "".join(c for c in _fam if c.isalpha() or c.isspace()).strip().upper()
    if _fam and _fam in (_vs.get("families") or {}):
        _st = _vs["families"][_fam]
        _bad = _fam in (_vs.get("avoid") or [])
        L.append((f"  Verdict '{_fam}': in {_st['n']} graded episodes this "
                  f"family averaged {_st['mean_10d']:+.1f}% over 10 days "
                  f"with a {_st['hit_15']:.0%} hit rate"
                  + ("  — an AVOID signal in your own data, whatever the "
                     "score says." if _bad else "."),
                  "warn" if _bad else "dim"))
    if catalyst_label:
        L.append((f"  Catalyst: {catalyst_label}", "dim"))
    if calibrated_prob is not None:
        L.append((f"  📊 System calibrated P(+15%/10d): {calibrated_prob:.0%} "
                  f"(learned from graded history) vs your P(≥+15%) "
                  f"{st['p_up15']:.0%}", "hdr"))
    else:
        L.append(("  Calibration inactive (needs 150+ graded rows) — "
                  "probabilities are market-implied + your scenarios only.",
                  "dim"))
    L.append(("", "dim"))

    hdr = (f"  {'K':>7} {'bid/ask':>13} {'sprd':>5} {'BE(move)':>15} "
           f"{'profit%':>9} {'p%@cat':>7} {'(pre-cost)':>11} "
           f"{'best×':>6} {'P>BE scn':>9} "
           f"{'P>BE mkt':>9} {'Δview':>6} {'p% mkt':>9}")
    any_positive = False
    for b in blocks:
        cov_tag = ("bad" if "BEFORE" in (b["coverage"] or "")
                   else ("good" if b["coverage"] else "dim"))
        cov = f"   {b['coverage']}" if b["coverage"] else ""
        _fwd_tag = ""
        if b.get("fwd_method", "").startswith(("put-call", "borrow")):
            _fwd_tag = (f"   [fwd {b['fwd'] / spot - 1:+.2%}, "
                        f"carry {b['carry'] * 100:+.1f}%/yr]")
        L.append((f"  ── {b['expiry']}  ({b['days']}d){cov}{_fwd_tag}",
                  cov_tag))
        L.append((hdr, "dim"))
        shown = b["rows"][:MAX_ROWS_PER_EXPIRY]
        for r in shown:
            if r["sc_ev"] >= 0:
                any_positive = True
            wide = "!" if r["wide"] else " "
            L.append((
                f"  {r['strike']:>7.2f} "
                f"{r['bid']:>6.2f}/{r['ask']:<6.2f}{wide}"
                f"{r['spread_pct']:>4.0%} "
                f"{r['breakeven']:>7.2f}({r['be_move']:>+5.1%}) "
                f"{r['sc_ev']:>+8.0%} "
                + (f"{r['sc_ev_exit']:>+6.0%} " if r.get("sc_ev_exit") is not None
                   else f"{'—':>6} ")
                + (f"{r['sc_ev_exit_gross']:>+11.0%} "
                   if r.get("sc_ev_exit_gross") is not None
                   else f"{'—':>11} ")
                + f"{r['best_mult']:>5.1f}x "
                f"{r['sc_p_be']:>8.0%} "
                + (f"{r['mkt_p_be']:>8.0%} " if r["mkt_p_be"] is not None else f"{'—':>8} ")
                + (f"{r['disagree']:>+5.0%} " if r["disagree"] is not None else f"{'—':>5} ")
                + (f"{r['mkt_ev']:>+8.0%}" if r["mkt_ev"] is not None else f"{'—':>8}")
                + ("  LAST-PX" if r.get("stale") else
                   ("  NO-BID" if r.get("no_bid") else
                    ("  thin" if r.get("thin") else ""))),
                _row_tag(r)))
        hidden = len(b["rows"]) - len(shown)
        if hidden > 0:
            L.append((f"      … {hidden} more strikes ranked lower "
                      f"(all appear in the heatmap)", "dim"))
        L.append(("", "dim"))

    if not any_positive:
        L.append(("  ⚠ EVERY strike is negative-EV under your scenarios: this "
                  "chain offers no attractive call expression of that thesis.",
                  "warn"))
        L.append(("    Honest options: equity instead, a different thesis "
                  "(edit the Scenarios box), or no trade.", "warn"))
        L.append(("", "dim"))

    # ── IV-CRUSH SENSITIVITY (the simulator) ──
    sens = crush_sensitivity(blocks, spot, scenarios, catalyst_iso or "",
                             as_of=as_of)
    if sens:
        L.append(("  💨 IV-CRUSH SENSITIVITY — top strikes by avg profit "
                  "at catalyst exit, re-priced across post-event IV "
                  "assumptions:", "hdr"))
        L.append((f"  {'contract':>16} {'ask':>7}   "
                  + "  ".join(f"{lv:>7.0%}" for lv in CRUSH_LEVELS)
                  + "   (post-event IV as % of quoted)", "dim"))
        for c in sens:
            name = f"{c['expiry'][5:]} ${c['strike']:g}C"
            vals = "  ".join(
                (f"{c['evs'][lv]:>+7.0%}" if c["evs"][lv] is not None
                 else f"{'—':>7}") for lv in CRUSH_LEVELS)
            tag = ("good" if all(v is not None and v > 0
                                 for v in c["evs"].values())
                   else ("bad" if (c["evs"].get(0.85) or 0) <= 0 else "mid"))
            L.append((f"  {name:>16} {c['ask']:>7.2f}   {vals}", tag))
        L.append(("     green = positive at EVERY crush level (robust); "
                  "yellow = depends on the assumption; red = negative even "
                  "at the default 85%.", "note"))
        L.append(("", "dim"))

    # ── STRUCTURE MENU ──
    if not structures and blocks:
        _stale = any(r.get("stale") or (r.get("bid") or 0) <= 0
                     for b in blocks for r in b["rows"])
        L.append(("  🧰 STRUCTURE MENU: none available — "
                  + ("every leg is quoted with NO BID (market closed, or a "
                     "chain too thin to quote two-sided). A spread requires "
                     "selling a leg, and you cannot sell into a bid that does "
                     "not exist. Re-run during the session."
                     if _stale else
                     "no combination cleared the liquidity and cost floors on "
                     "this chain."), "warn"))
        L.append(("", "dim"))
    if structures:
        L.append(("  🧰 STRUCTURE MENU — best expression of this thesis "
                  "in each family, all scored on the SAME distribution, "
                  "forward, crush and round-trip costs:", "hdr"))
        L.append((f"  {'family':<13}{'structure':<40}{'debit':>8}"
                  f"{'EV':>8}{'Kelly':>7}{'P(win)':>8}", "dim"))
        for _k, _r in sorted(structures.items(),
                             key=lambda kv: -(kv[1].get("kelly") or -9)):
            _tag = ("good" if (_r.get("kelly") or 0) >= 0.25
                    else ("mid" if (_r.get("kelly") or 0) > 0 else "bad"))
            L.append((f"  {_k:<13}{_r['name']:<40}{_r['debit']:>8.2f}"
                      f"{_r['ev']:>+7.0%}{(_r.get('kelly') or 0):>+7.2f}"
                      f"{_r['p_profit']:>8.0%}", _tag))
        L.append(("     Ranked by Kelly, which rewards win probability over "
                  "raw return — so a deep-ITM call often outranks a spread "
                  "with far higher EV. Read the whole row, not the order.",
                  "note"))
        L.append(("     A SPREAD sells the wing that this universe's data says "
                  "is overpriced; a single long call buys it. That is the "
                  "trade-off the menu exists to show.", "note"))
        L.append(("", "dim"))

    L.append(("  READ ME: 'profit%' is the AVERAGE profit per dollar of "
              "premium UNDER YOUR SCENARIOS (an expectation across many "
              "repeats, not a prediction) — only as honest as they are.", "note"))
    L.append((f"  Scenario moves are 'by the catalyst' (or {DEFAULT_HORIZON_DAYS}d "
              f"if none): expiries shorter than that get sqrt-time-scaled "
              f"moves, so penny weeklies can't claim the full thesis.", "note"))
    L.append(("  'p% mkt' uses each strike's own IV and sits near zero-minus-"
              "spread by construction: the baseline you must disagree with.", "note"))
    L.append(("  Δview = your P(profit) minus the market's. '!' = spread "
              ">20% of mid: slippage is a first-class cost at these sizes.", "note"))
    L.append(("  'P>BE scn' comes from a mixture of lognormals around your "
              "scenarios, not from the three prices read literally — so it "
              "moves smoothly with strike and does not price tail moves at "
              "zero. Branch width is whatever ATM implied variance your "
              "scenarios leave unexplained.", "note"))
    L.append(("  'Kelly' (heatmap) is FULL Kelly on that distribution, "
              "including a 1% total-loss allowance for gaps and halts. A "
              "ranking score for picking between contracts — never a size.",
              "note"))
    L.append(("  '(pre-cost)' = the same exit EV with NO exit spread and no "
              "commission — what this tool reported before round-trip costs "
              "were modeled. The gap is the cost of trading, not a forecast "
              "change.", "note"))
    L.append((f"  'p%@cat' = sell ON the catalyst date after your scenario "
              f"move, remaining time valued at {iv_crush:.0%} of current IV"
              + (f" — AUTO via {crush_method}" if crush_method else
                 " (post-event crush assumption)")
              + ". Theta lives here, not in expiry EV.", "note"))
    if any(r.get("stale") for b in blocks for r in b["rows"]):
        L.append(("  ⏰ LAST-PX rows: no live ask (market likely closed) — "
                  "priced from the LAST TRADE. Overnight estimate only; "
                  "re-run during the session before acting.", "warn"))
    L.append(("  Quotes/OI are delayed & prior-session on thin names. This "
              "ranks structures under stated assumptions; trade selection "
              "and sizing are yours.", "note"))
    return L


def format_matrix(ticker: str, spot: float, blocks: list, scenarios: list,
                  implied_move_pct: Optional[float] = None,
                  calibrated_prob: Optional[float] = None,
                  catalyst_label: str = "") -> str:
    lines = matrix_lines(ticker, spot, blocks, scenarios,
                         implied_move_pct, calibrated_prob, catalyst_label)
    return "\n".join(t for t, _ in lines) + "\n"
# note: format_matrix has no catalyst_iso -> sensitivity omitted there;
# the GUI path (run_strike_matrix_data) supplies it.


def ev_grid(blocks: list, metric: str = "sc_ev"):
    """(strikes_desc, expiries, grid) for heat-mapping: grid[i][j] =
    row[metric] for strikes[i] at expiries[j], None where absent.
    metric: sc_ev | sc_ev_exit | p_itm_mkt (any per-strike field works)."""
    strikes = sorted({r["strike"] for b in blocks for r in b["rows"]},
                     reverse=True)
    expiries = [b["expiry"] for b in blocks]
    idx = {(r["strike"], b["expiry"]): r.get(metric)
           for b in blocks for r in b["rows"]}
    grid = [[idx.get((k, e)) for e in expiries] for k in strikes]
    return strikes, expiries, grid


# ─────────────────────────────────────────────
# ONE-CALL ENTRY POINT (GUI + CLI)
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# SURFACE ESTIMATION (fill the gray cells, honestly marked)
# ─────────────────────────────────────────────
# Two kinds of gray cell:
#   * listed strike, no ask  -> estimate is a fair-value LIMIT-ORDER anchor
#   * strike not listed here -> the contract DOES NOT EXIST; the estimate
#     is a reading of the vol surface, not a tradable opportunity.
# Both are computed by interpolating the expiry's own IV smile, pricing
# with the same BS machinery, and penalizing by that expiry's median
# half-spread (even tradable estimates wouldn't fill at fair value).
# The GUI renders estimates faded with a '~' prefix — they must never
# look as solid as a real quote.

def _expiry_smile(rows: list) -> list:
    """[(K, iv)] sorted by strike, from rows with usable IV."""
    pts = sorted((r["strike"], r["iv"]) for r in rows
                 if r.get("iv") and r["iv"] > 0)
    return pts


def _interp_iv(smile: list, K: float) -> Optional[float]:
    """Linear IV interpolation in strike; flat beyond the known range.

    Binary search, not a scan. The smile roll calls this once per quadrature
    node per strike — 140,685 times on a single 423-row chain — and the
    original walked the strike list from the start every time. Tuples compare
    on their first element, so bisecting the (strike, iv) pairs directly finds
    the bracket without rebuilding a key list. Same arithmetic, same result."""
    if not smile:
        return None
    i = _bisect_left(smile, (K,))
    if i == 0:
        return smile[0][1]
    if i >= len(smile):
        return smile[-1][1]
    k0, v0 = smile[i - 1]
    k1, v1 = smile[i]
    w = (K - k0) / (k1 - k0) if k1 > k0 else 0.0
    return v0 + w * (v1 - v0)


def estimated_grid(blocks: list, spot: float, scenarios: list,
                   metric: str = "sc_ev",
                   catalyst_iso: str = "",
                   iv_crush: float = IV_CRUSH_MULT,
                   as_of: Optional[date] = None) -> list:
    """Same shape as ev_grid's grid, holding ESTIMATED values only where
    the real grid is None (and None everywhere a real quote exists).
    Estimates: smile-interpolated IV -> BS fair value -> cost penalized by
    the expiry's median half-spread. Cells whose theoretical value is dust
    (<$0.01) stay None — an EV on a half-cent estimate is noise."""
    strikes = sorted({r["strike"] for b in blocks for r in b["rows"]},
                     reverse=True)
    real = {(r["strike"], b["expiry"]) for b in blocks for r in b["rows"]}

    catalyst_T = None
    if catalyst_iso:
        try:
            _cd = (datetime.strptime(catalyst_iso[:10], "%Y-%m-%d").date()
                   - (as_of or date.today())).days
            if _cd >= 0:
                catalyst_T = _cd / 365.0
        except (ValueError, TypeError):
            catalyst_T = None

    grid = []
    per_exp = {}
    for b in blocks:
        smile = _expiry_smile(b["rows"])
        spreads = sorted(r["spread_pct"] for r in b["rows"]
                         if not r.get("no_bid"))
        med_half = (spreads[len(spreads) // 2] / 2.0) if spreads else 0.15
        T = max(b["days"], 1) / 365.0
        per_exp[b["expiry"]] = (smile, med_half, T)

    for K in strikes:
        row = []
        for b in blocks:
            e = b["expiry"]
            if (K, e) in real:
                row.append(None)              # real quote exists — no estimate
                continue
            if metric == "val_edge":
                row.append(None)          # no honest estimate for the ruler
                continue
            smile, med_half, T = per_exp[e]
            iv = _interp_iv(smile, K)
            theo = bs_call_ev(spot, K, T, iv) if iv else None
            if theo is None or theo < 0.01:
                row.append(None)              # dust — leave gray
                continue
            cost = max(theo * (1.0 + med_half), 0.01)
            if metric == "p_itm_mkt":
                row.append(market_p_above(spot, K, T, iv))
            elif metric == "sc_ev_exit":
                if catalyst_T is None or not (0 <= catalyst_T <= T):
                    row.append(None)
                else:
                    t_rem = T - catalyst_T
                    acc = 0.0
                    for p, m in scenarios:
                        s2 = spot * (1.0 + m)
                        v = (max(s2 - K, 0.0) if t_rem <= 1.0 / 365.0 else
                             (bs_call_ev(s2, K, t_rem, iv * iv_crush)
                              or max(s2 - K, 0.0)))
                        acc += p * v
                    row.append(acc / cost - 1.0)
            else:                              # sc_ev at expiry
                _hz = (catalyst_T if catalyst_T and catalyst_T > 0
                       else DEFAULT_HORIZON_DAYS / 365.0)
                _f = _time_scale(T, _hz)
                ev = sum(p * max(spot * (1.0 + m * _f) - K, 0.0)
                         for p, m in scenarios)
                row.append(ev / cost - 1.0)
        grid.append(row)
    return grid


CRUSH_TYPE_DEFAULTS = {          # fallback when term structure can't speak
    "EARNINGS": 0.75,            # earnings deflate event premium hard
    "FDA": 0.65, "PDUFA": 0.65,  # binary bio events crush hardest
    "FTD_CLOSEOUT": 0.95,        # mechanical flows barely dent vol
}


def _atm_iv(spot: float, rows: list) -> Optional[float]:
    """IV of the strike nearest spot (within 15%) with a usable IV."""
    best, bd = None, 1e9
    for r in rows:
        K = _num(r.get("strike")); iv = _num(r.get("impliedVolatility"))
        if K > 0 and iv > 0:
            d = abs(K - spot) / spot
            if d < 0.15 and d < bd:
                best, bd = iv, d
    return best


def estimate_iv_crush(spot: float, expiries: list, catalyst_iso: str = "",
                      catalyst_type: str = "") -> dict:
    """AUTO crush estimate -> {mult, method, detail}.

    Primary: TERM-STRUCTURE FORWARD VOL — the market's own implied
    post-event volatility. With front ATM IV s1 (first expiry covering
    the catalyst, T1) and a later expiry s2 (T2):
        fwd² = (s2²·T2 − s1²·T1) / (T2 − T1)
    and crush = fwd / s1, clamped to [0.50, 1.00]. This is the market
    pricing its own crush — no opinion of ours involved.

    Fallbacks (in order): catalyst-type default table; the 0.85 constant.
    The method used is always returned and displayed."""
    fallback_mult = CRUSH_TYPE_DEFAULTS.get(
        (catalyst_type or "").upper().split()[0] if catalyst_type else "",
        IV_CRUSH_MULT)
    fb_name = ("type default" if (catalyst_type or "").upper().split()[:1]
               and (catalyst_type or "").upper().split()[0] in CRUSH_TYPE_DEFAULTS
               else "default")
    try:
        cat = (datetime.strptime(catalyst_iso[:10], "%Y-%m-%d").date()
               if catalyst_iso else None)
    except (ValueError, TypeError):
        cat = None
    if cat is None or not expiries:
        return {"mult": fallback_mult, "method": fb_name,
                "detail": "no catalyst date / no chain"}

    # ATM IV per expiry, with real dates
    pts = []
    for T, calls, _p in expiries:
        if not calls:
            continue
        iv = _atm_iv(spot, calls)
        exp = calls[0].get("expiry", "")
        try:
            ed = datetime.strptime(exp, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if iv:
            pts.append((ed, max(T, 1.0 / 365.0), iv))
    pts.sort()
    front = next(((d, T, iv) for d, T, iv in pts if d >= cat), None)
    if front is None:
        return {"mult": fallback_mult, "method": fb_name,
                "detail": "no expiry covers catalyst"}
    d1, T1, s1 = front
    later = next(((d, T, iv) for d, T, iv in pts
                  if T > T1 + 5.0 / 365.0), None)
    if later is None:
        return {"mult": fallback_mult, "method": fb_name,
                "detail": "no later expiry for term structure"}
    _d2, T2, s2 = later
    var_f = (s2 * s2 * T2 - s1 * s1 * T1) / (T2 - T1)
    if var_f <= 0:
        return {"mult": 0.50, "method": "term structure (floor)",
                "detail": f"IV {s1:.0%}->fwd ~0 — extreme event premium"}
    fwd = math.sqrt(var_f)
    mult = min(max(fwd / s1, 0.50), 1.00)
    return {"mult": round(mult, 3), "method": "term structure",
            "detail": f"front IV {s1:.0%} -> mkt-implied post-event "
                      f"{fwd:.0%}"}


def _catalyst_T_from_iso(catalyst_iso: str,
                         as_of: Optional[date] = None) -> Optional[float]:
    if not catalyst_iso:
        return None
    try:
        d = (datetime.strptime(catalyst_iso[:10], "%Y-%m-%d").date()
             - (as_of or date.today())).days
        return d / 365.0 if d >= 0 else None
    except (ValueError, TypeError):
        return None


def crush_grid(blocks: list, spot: float, scenarios: list,
               catalyst_iso: str, iv_crush: float,
               as_of: Optional[date] = None) -> list:
    """ev_grid-shaped grid of exit-EV recomputed at an ARBITRARY crush
    level (the heatmap's crush selector)."""
    cat_T = _catalyst_T_from_iso(catalyst_iso, as_of)
    strikes = sorted({r["strike"] for b in blocks for r in b["rows"]},
                     reverse=True)
    idx = {}
    for b in blocks:
        T = max(b["days"], 1) / 365.0
        for r in b["rows"]:
            idx[(r["strike"], b["expiry"])] = exit_ev_for(
                spot, r["strike"], r["ask"], r["iv"], T, cat_T,
                scenarios, iv_crush,
                half_spread=max(r["ask"] - r["bid"], 0.0) / 2.0, costs=True,
                carry=b.get("carry", 0.0),
                sigma=r.get("branch_sigma_cat", 0.0),
                smile=b.get("smile"), spot_ref=spot)
    return [[idx.get((k, b["expiry"])) for b in blocks] for k in strikes]


def crush_sensitivity(blocks: list, spot: float, scenarios: list,
                      catalyst_iso: str, top_n: int = 5,
                      levels: tuple = CRUSH_LEVELS,
                      as_of: Optional[date] = None) -> list:
    """Top strikes by base exit-EV, each with EV across crush levels.
    -> [{expiry, strike, ask, evs: {level: ev}}], best first."""
    cat_T = _catalyst_T_from_iso(catalyst_iso, as_of)
    if cat_T is None:
        return []
    cands = []
    for b in blocks:
        T = max(b["days"], 1) / 365.0
        for r in b["rows"]:
            base = r.get("sc_ev_exit")
            if base is None:
                continue
            evs = {lv: exit_ev_for(spot, r["strike"], r["ask"], r["iv"],
                                   T, cat_T, scenarios, lv,
                                   half_spread=max(r["ask"] - r["bid"],
                                                   0.0) / 2.0,
                                   costs=True, carry=b.get("carry", 0.0),
                                   sigma=r.get("branch_sigma_cat", 0.0),
                                   smile=b.get("smile"), spot_ref=spot)
                   for lv in levels}
            cands.append({"expiry": b["expiry"], "strike": r["strike"],
                          "ask": r["ask"], "base": base, "evs": evs})
    cands.sort(key=lambda c: c["base"], reverse=True)
    return cands[:top_n]


# ─────────────────────────────────────────────
# CONTRACT P/L MATRIX (one contract through price x time)
# ─────────────────────────────────────────────

def contract_matrix(spot: float, strike: float, expiry_days: int,
                    iv: float, entry_cost: float,
                    up_move: float = 0.15,
                    scenarios: Optional[list] = None,
                    catalyst_days: Optional[int] = None,
                    iv_crush: float = IV_CRUSH_MULT,
                    n_prices: int = 26, max_cols: int = 16,
                    carry: float = 0.0,
                    smile: Optional[list] = None,
                    half_spread: float = 0.0,
                    commission: bool = True) -> dict:
    """OptionsProfitCalculator-style grid for ONE contract, plus a squeeze
    scenario path. Returns {dates_d, prices, pnl (rows=prices desc,
    cols=dates, % of entry), path: [(day, price, pnl_pct)], exit_day,
    crush_from_day}.

    HONESTY NOTES baked into the math:
      * IV is CRUSHED (x iv_crush) for all dates AFTER the catalyst — the
        decay cliff flat-IV calculators can't show.
      * The path is a SCENARIO, not a forecast: price ramps sqrt-time into
        the catalyst to spot*(1+up_move) (same diffusion convention as the
        strike matrix), holds ~2 days of momentum, then gives back half
        the move over the next 5 — 'ride until momentum dies' drawn as a
        curve. Exit marker = catalyst+2d (sell into strength).
      * Expiry column is pure intrinsic (no time value left, no model).
    """
    expiry_days = max(int(expiry_days), 1)
    # Cost basis matches the strike matrix: the ask PLUS commission. Passing
    # a bare ask here while the table charged commission was a second way the
    # two screens disagreed.
    entry_cost = max(float(entry_cost), 0.01)
    if commission:
        entry_cost = entry_cost + _commission_ps()
    # yfinance after-hours chains report IV as a ~1e-5 PLACEHOLDER (not
    # NaN) — an IV that small makes BS collapse to intrinsic and every
    # column reads identical (no theta, no decay: a broken chart). Any
    # IV below 5% on these names is missing data, not a real quote.
    iv_fallback = not (iv and iv > 0.05)
    iv = iv if not iv_fallback else 0.8

    # price rows: cover the squeeze peak with headroom, and downside
    hi = spot * (1.0 + max(up_move, 0.05) * 1.6)
    lo = spot * (1.0 - max(up_move, 0.05) * 1.0)
    lo = min(lo, strike * 0.92, spot * 0.85)
    step = (hi - lo) / (n_prices - 1)
    prices = [round(hi - i * step, 4) for i in range(n_prices)]   # desc

    # date columns: day 0 .. expiry, thinned to max_cols
    stride = max(1, math.ceil((expiry_days + 1) / max_cols))
    dates_d = list(range(0, expiry_days, stride)) + [expiry_days]

    def _val(S, d):
        """Theoretical value of the contract at price S on day d.

        Now shares the strike matrix's corrections rather than running its own
        older math. This grid is the screen the decision actually gets made
        on, and it was the ONE surface still pricing off spot with no borrow,
        a single flat crush for every outcome, and the strike's own IV no
        matter how far the stock had moved — while the table beside it used
        all four fixes. The two disagreed, and the more optimistic one was in
        front of you."""
        t_rem = (expiry_days - d) / 365.0
        if t_rem <= 1e-9:
            return max(S - strike, 0.0)
        # forward, not spot: carry over the REMAINING life
        F = S * math.exp(carry * t_rem) if carry else S
        # crush scales with the realized move, and only after the catalyst
        base = (iv_crush if (catalyst_days is not None
                             and d > catalyst_days) else 1.0)
        eff_crush = (crush_for_move(base, S / spot - 1.0)
                     if (catalyst_days is not None and d > catalyst_days)
                     else base)
        # sticky-delta: vol attaches to moneyness, not to the strike
        eff_iv = rolled_iv(smile, strike, spot, S, iv) if smile else iv
        v = bs_call_ev(F, strike, t_rem, eff_iv * eff_crush)
        return v if v is not None else max(S - strike, 0.0)

    def _net(S, d):
        """What the position is actually WORTH TO YOU on day d: theoretical
        value less the cost of getting out of it. At expiry an ITM call
        settles at intrinsic and no spread is crossed."""
        v = _val(S, d)
        if d >= expiry_days or half_spread <= 0:
            return v
        return exit_proceeds_for(v, half_spread)

    pnl = [[round((_net(S, d) - entry_cost) / entry_cost * 100.0, 1)
            for d in dates_d] for S in prices]

    # scenario paths: same ramp/hold/fade SHAPE for every outcome, each
    # with its own magnitude. Stylized (disclosed), not learned.
    cat = (catalyst_days if catalyst_days and catalyst_days > 0
           else min(10, expiry_days))
    cat = min(cat, expiry_days)
    hold, fade_len, fade_frac = 2, 5, 0.5

    # ── LEARNED PATH SHAPE (preferred over the stylized one below) ──
    # Fractions of the 10-day move realized by day 5/10/20, measured from
    # logged prices on real episodes. Where this exists it REPLACES the
    # hand-invented ramp/hold/fade, which the same data contradicts: winners
    # hold and extend through day 20 rather than fading half the move, and
    # losers partially recover rather than drifting lower.
    # ── LEARNED PATH SHAPE, IN EVENT TIME ──
    # An earlier version of this averaged returns by days from the SCAN. Every
    # episode's catalyst lands on a different day, so that smeared the event
    # move across the window and produced two nearly MIRROR-IMAGE curves for
    # up and down. A symmetric picture cannot show risk against reward, which
    # is the whole reason to draw the paths at all.
    #
    # Aligned to each episode's own event date, the sides stop resembling each
    # other. Measured on this log:
    #     UP    peaks BEFORE the event (x1.15 at day -2) then eases to x0.84
    #     DOWN  drifts shallowly (x0.40 at -8), CLIFFS at the event, bottoms
    #           just after (x1.05 at +4), then recovers to x0.79
    # Those are different anatomies, and they are measured rather than drawn.
    #
    # A side is only used when its bins carry enough samples; FLAT on this log
    # is visibly noise (x0.38 at day -2, x1.94 at +8) and is left to the
    # stylized fallback below. The fallback is asymmetric too — the structure
    # it encodes is what the event-time data confirms.
    _ev_shape = (learned().get("path_shape_event") or {}).get("sides") or {}
    _MIN_BIN = 8

    def _ev_curve(bucket):
        """[(rel_day, fraction)] for a side, or None if under-sampled."""
        sd = _ev_shape.get(bucket)
        if not sd:
            return None
        pts = []
        for k, v in (sd.get("frac") or {}).items():
            try:
                if (sd.get("n") or {}).get(k, 0) >= _MIN_BIN:
                    pts.append((float(k), float(v)))
            except (TypeError, ValueError):
                continue
        pts.sort()
        return pts if len(pts) >= 3 else None

    def _S_learned(m, d):
        """Measured path in event time, scaled to the scenario magnitude.

        d is days from today; the curve is indexed by days from the EVENT, so
        the two are related by the catalyst offset. Returns None whenever the
        side lacks evidence, which sends the caller to the stylized anatomy."""
        bucket = "UP" if m >= 0.15 else ("DOWN" if m <= -0.05 else "FLAT")
        pts = _ev_curve(bucket)
        if not pts:
            return None
        rel = d - cat
        if rel <= pts[0][0]:
            f = pts[0][1] * max(0.0, (d / max(cat, 1)))   # ramp from flat
        elif rel >= pts[-1][0]:
            f = pts[-1][1]
        else:
            f = pts[-1][1]
            for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
                if d0 <= rel <= d1:
                    w = (rel - d0) / (d1 - d0) if d1 > d0 else 0.0
                    f = v0 + w * (v1 - v0)
                    break
        return spot * (1.0 + m * f)

    def _S_at(m, d):
        """Price along a scenario path at (possibly fractional) day d.

        Uses the LEARNED shape when learned_params.json carries one; the
        stylized anatomy below is the fallback for a system with no graded
        history yet.

        ASYMMETRIC ANATOMY — each outcome has its own honest shape:
          UP (squeeze fires):  anticipation ramp (sqrt-time) into the
              catalyst, ~2d momentum hold at the peak, then fades half
              the move — enter early, sell into strength.
          DOWN (squeeze fails): the stock HOLDS/drifts up slightly on
              hope into the event (shorts don't cover early), then
              CLIFFS to the down level when the catalyst disappoints,
              and drifts a further ~20% of the move lower. No recovery.
          FLAT: the documented run-up-and-give-back — a modest
              anticipation drift (~30% of the up-move) that returns to
              spot after the event.
        Stylized and disclosed — shapes, not forecasts."""
        _s = _S_learned(m, d)
        if _s is not None:
            return _s
        if m > 0.02:                       # squeeze fires
            peak = spot * (1.0 + m)
            if d <= cat:
                return spot * (1.0 + m * math.sqrt(d / cat)) if cat else spot
            if d <= cat + hold:
                return peak
            gone = min((d - cat - hold) / fade_len, 1.0)
            return peak - (peak - spot * (1.0 + m * (1 - fade_frac))) * gone
        if m < -0.02:                      # squeeze fails
            hope = 0.3 * abs(up_move)      # pre-event hope drift
            if d <= cat:
                return spot * (1.0 + hope * math.sqrt(d / cat)) if cat else spot
            floor_px = spot * (1.0 + m)
            drift = min((d - cat) / fade_len, 1.0) * 0.2 * abs(m)
            return floor_px * (1.0 - drift)
        # flat: run-up and give-back
        bump = 0.3 * abs(up_move)
        if d <= cat:
            return spot * (1.0 + bump * math.sqrt(d / cat)) if cat else spot
        back = min((d - cat) / 2.0, 1.0)
        return spot * (1.0 + bump * (1.0 - back))

    def _pnl(S, d):
        return round((_net(S, d) - entry_cost) / entry_cost * 100.0, 1)

    def _path_for(m, step=1.0):
        n = int(expiry_days / step)
        ds = [round(i * step, 3) for i in range(n + 1)]
        if ds[-1] < expiry_days:
            ds.append(float(expiry_days))
        return [(d, round(_S_at(m, d), 4), _pnl(_S_at(m, d), d))
                for d in ds]

    exit_day = min(cat + hold, expiry_days)
    out = {"dates_d": dates_d, "prices": prices, "pnl": pnl,
           "exit_day": exit_day,
           "crush_from_day": (cat if catalyst_days else None),
           "entry_cost": entry_cost, "iv_used": iv,
           "iv_fallback": iv_fallback}

    if scenarios:
        tot = sum(p for p, _ in scenarios) or 1.0
        scn = [(p / tot, m) for p, m in scenarios]
        per = [(p, m, _path_for(m)) for p, m in scn]          # daily
        m_best = max(m for _, m in scn)
        m_worst = min(m for _, m in scn)

        def _exp_path(step=1.0):
            n = int(expiry_days / step)
            ds = [round(i * step, 3) for i in range(n + 1)]
            if ds[-1] < expiry_days:
                ds.append(float(expiry_days))
            return [(d, round(sum(p * _S_at(m, d) for p, m in scn), 4),
                     _pnl(sum(p * _S_at(m, d) for p, m in scn), d))
                    for d in ds]

        out["path"] = _exp_path(1.0)                          # daily (logic)
        # DENSE versions (0.2d) purely for smooth drawing
        out["path_dense"] = _exp_path(0.2)
        out["paths"] = {
            "scenarios": per,
            "best": _path_for(m_best), "worst": _path_for(m_worst),
            "best_dense": _path_for(m_best, 0.2),
            "worst_dense": _path_for(m_worst, 0.2)}
        # TRUE expected P/L at exit: probability-weighted average of the
        # per-scenario P/Ls (options are convex — pnl at the expected
        # price is NOT the expected pnl; this is the honest number).
        out["exit_pnls"] = {
            "best": out["paths"]["best"][exit_day][2],
            "worst": out["paths"]["worst"][exit_day][2],
            "expected": round(sum(p * pth[exit_day][2]
                                  for p, _, pth in per), 1)}
    else:
        out["path"] = _path_for(up_move)
        out["path_dense"] = _path_for(up_move, 0.2)
        out["paths"] = None
        out["exit_pnls"] = None
    return out


def run_strike_matrix_data(ticker: str, scenario_text: str, deep=None,
                           n_expiries: int = 10,
                           max_dte: Optional[int] = None) -> dict:
    """Fetch + build, return everything the GUI needs:
    {ticker, spot, blocks, scenarios, lines, implied_move_pct,
     calibrated_prob, catalyst_label}. Raises on fetch/parse problems."""
    if fetch_expiries_yf is None:
        raise RuntimeError("gamma_terrain.fetch_expiries_yf unavailable "
                           "(gamma_terrain.py + squeeze_deep.py required)")
    scenarios = parse_scenarios(scenario_text)
    spot, expiries = fetch_expiries_yf(ticker, n_expiries)
    # A chain that lost expiries to transient failures is NOT the same chain
    # as one that never listed them, and the difference changes which
    # contracts you are choosing between. Say so rather than presenting a
    # partial fetch as the whole market.
    try:
        import gamma_terrain as _gt
        _fetch_issues = list(getattr(_gt, "LAST_FETCH_ISSUES", []) or [])
    except Exception:
        _fetch_issues = []
    if deep is not None and getattr(deep, "current_price", None):
        spot = deep.current_price      # keep one spot across the analysis

    catalyst_iso = ""
    catalyst_label = ""
    if deep is not None:
        catalyst_iso = getattr(deep, "ftd_closeout_date", "") or ""
        ctype = getattr(deep, "catalyst_type", "") or ""
        if catalyst_iso:
            catalyst_label = f"{ctype or 'FTD close-out'} ~{catalyst_iso}"
        elif ctype:
            catalyst_label = ctype

    _crush_est = estimate_iv_crush(spot, expiries, catalyst_iso,
                                   (getattr(deep, "catalyst_type", "")
                                    if deep else ""))
    _borrow = getattr(deep, "ctb_now", None) if deep is not None else None
    blocks = build_matrix(spot, expiries, scenarios, catalyst_iso,
                          max_dte=max_dte, iv_crush=_crush_est["mult"],
                          borrow_pct=_borrow)
    # Structure menu: the best expression of this thesis in each family.
    # Screened at coarse resolution so it costs a couple of seconds, not a
    # minute; never allowed to break the analysis if it fails.
    _structures = None
    if blocks:
        try:
            import options_structures as _ost
            _catT = _catalyst_T_from_iso(catalyst_iso)
            _structures = _ost.rank_by_type(blocks, spot, scenarios, _catT,
                                            _crush_est["mult"]) or None
        except Exception:
            _structures = None
    # Earnings date, so the heatmap can mark it alongside the FTD catalyst.
    # These are DIFFERENT events on different dates and the chart has only
    # ever shown one of them; an expiry can cover a close-out and still miss
    # the earnings print that actually moves the stock.
    _earn_iso = ""
    _de = getattr(deep, "days_to_earnings", None) if deep is not None else None
    try:
        if _de is not None and 0 <= float(_de) <= 400:
            _earn_iso = (date.today()
                         + timedelta(days=int(float(_de)))).isoformat()
    except (TypeError, ValueError):
        _earn_iso = ""
    imp = getattr(deep, "implied_move_pct", None) if deep else None
    cal = getattr(deep, "calibrated_prob", None) if deep else None
    lines = (matrix_lines(ticker, spot, blocks, scenarios, imp, cal,
                          catalyst_label, catalyst_iso,
                          iv_crush=_crush_est["mult"],
                          crush_method=_crush_est["method"],
                          deep_verdict=(getattr(deep, "deep_verdict", "")
                                        if deep is not None else ""),
                          structures=_structures) if blocks else
             [(f"  No call strikes found for {ticker.upper()} within "
               f"{STRIKE_BAND[0]:.0%}–{STRIKE_BAND[1]:.0%} of spot"
               + (f" and ≤{max_dte}d to expiry — this chain may not list "
                  f"short-dated expiries (raise Max DTE)" if max_dte else "")
               + ". If running OUTSIDE market hours, quotes may be cleared "
                 "and last-trade prices unavailable — retry during the "
                 "session.", "warn")])
    if _fetch_issues:
        lines.insert(0, (f"  ⚠ {len(_fetch_issues)} expiry/expiries failed to "
                         f"download and are MISSING from this table "
                         f"({', '.join(_fetch_issues[:3])}"
                         + (" ..." if len(_fetch_issues) > 3 else "")
                         + "). Usually transient rate limiting — re-run to "
                           "see the full chain.", "warn"))
    return {"ticker": ticker.upper(), "spot": spot, "blocks": blocks,
            "scenarios": scenarios, "lines": lines,
            "fetch_issues": _fetch_issues,
            "implied_move_pct": imp, "calibrated_prob": cal,
            "catalyst_label": catalyst_label,
            "raw_expiries": expiries, "catalyst_iso": catalyst_iso,
            "iv_crush": _crush_est["mult"],
            "crush_method": _crush_est["method"],
            "crush_detail": _crush_est["detail"],
            "structures": _structures,
            "earnings_iso": _earn_iso}


def run_strike_matrix(ticker: str, scenario_text: str, deep=None,
                      n_expiries: int = 10,
                      max_dte: Optional[int] = None) -> str:
    """String version (CLI / plain rendering)."""
    d = run_strike_matrix_data(ticker, scenario_text, deep, n_expiries,
                               max_dte=max_dte)
    return "\n".join(t for t, _ in d["lines"]) + "\n"


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('usage: python options_ev.py TICKER ["40:+15, 35:0, 25:-10"]')
        sys.exit(1)
    scn = sys.argv[2] if len(sys.argv) > 2 else "40:+15, 35:0, 25:-10"
    dte = int(sys.argv[3]) if len(sys.argv) > 3 else None
    print(run_strike_matrix(sys.argv[1].upper(), scn, max_dte=dte))
