"""
options_structures.py
=====================
Ranks MULTI-LEG expressions of a thesis, not just long calls.

WHY THIS EXISTS
---------------
The strike matrix could only ever recommend buying a call. That is one
expression out of many, and on this universe it is close to the worst one:
measured across 384 graded episodes, realized 10-day moves ran 0.87x the
implied move and exceeded it only 36% of the time. Implied volatility on
these names is systematically too expensive, and a long call is a pure bet
that it is too cheap.

A structure that SELLS some of that expensive premium — a vertical spread, a
calendar — keeps the directional view while handing back the part of the
trade the data says you were losing on. The tool could not see those, so it
could not recommend them, so every answer it gave was "buy a call" even when
the chain was screaming otherwise.

WHAT IT SUPPORTS
----------------
    long_call          buy 1 call                     (the old default)
    long_put           buy 1 put                      (the downside thesis)
    call_debit         buy K1 call, sell K2>K1 call   (sells the upper wing)
    put_debit          buy K1 put, sell K2<K1 put     (downside, financed)
    call_calendar      sell near expiry, buy far      (sells event premium)
LEAPS are not a separate type — they are long-dated singles and verticals,
and they appear automatically once the expiry filter allows them through.

HOW IT VALUES THEM
------------------
Every structure is evaluated on the SAME machinery the strike matrix uses:
the lognormal scenario mixture, the put-call-parity forward, the rolled
smile, move-dependent IV crush, and full round-trip costs. Nothing here has
its own private model — a structure and a single call are scored by one set
of assumptions, which is the only way their EVs can be compared at all.

Costs are asymmetric and that matters more for spreads than singles: you BUY
at the ask and SELL at the bid, and you pay commission per leg, both ways. A
two-leg structure crosses four spreads over its life. Ignoring that would
make every spread look better than it is, which is exactly the error this
module exists to avoid making in the other direction.
"""

import math
from typing import Optional

import options_ev as oe

# Screening runs over thousands of candidate structures, so the quadrature is
# coarser than the strike matrix's; finalists are re-scored at full resolution.
SCREEN_NODES = 33
FINAL_NODES = 97

MAX_WIDTH_STRIKES = 8      # widest vertical considered, in strike steps
MIN_CREDIT_RATIO = 0.15    # a short leg must fund at least this share of cost
MIN_LEG_PRICE = 0.05       # ignore legs quoted in dust
MIN_LEG_OI = 1             # a leg nobody holds cannot be traded out of
INTRINSIC_TOL = 0.95       # ask below this share of intrinsic = stale quote
MIN_DEBIT = 0.10           # below this the trade is all friction
COST_COVER = 1.5           # degenerate-case guard only — see note below

# WHY COST_COVER IS 1.5 AND NOT 4.0
# ---------------------------------
# It was 4.0, and it was double-counting. structure_exit() already charges the
# half-spread on every leg and commission both ways, so transaction costs are
# priced INTO the expected value; filtering on the same costs again penalized
# spreads twice and left thin chains showing nothing but single calls.
#
# Measured attrition before the change:
#     AAPL   2478/3434 multi-leg survived   (thick chain, 42 strikes/expiry)
#     ARQQ     26/197  multi-leg survived   (thin chain, 7 strikes/expiry)
#     HTZ      62/142  multi-leg survived
# The rejects were overwhelmingly "friction" — on exactly the small-cap chains
# this toolkit exists to trade.
#
# The filter now catches only the degenerate case where costs exceed two-thirds
# of the debit, which is a structure with no room to be right. Everything else
# is left to the EV and Kelly math, which already knows what it costs.

# WHY THESE FILTERS ARE NOT OPTIONAL
# ----------------------------------
# An optimizer searching thousands of combinations will find the errors in the
# data before it finds the opportunities in the market. Two showed up on the
# very first run against a real AAPL chain:
#
#   * A $500/$440 PUT debit spread priced at $9.51 with the stock at $316.
#     Both legs are deep in the money and the spread is worth $60 at expiry by
#     definition. No such trade exists — those are stale quotes on strikes
#     nobody trades. It ranked first, at "+531% EV, 100% win".
#
#   * Call spreads at a $0.03 debit showing -383% worst case. That one is
#     arithmetically REAL: two legs, each with a ~$0.05 half-spread, cost more
#     to unwind than the entire debit. It is a true number about a trade no
#     one should take.
#
# So: reject quotes that violate arbitrage (an option cannot trade below its
# intrinsic value), reject legs with no open interest, and require the debit
# to be large enough that transaction costs are not the whole position.


# ─────────────────────────────────────────────
# LEG VALUATION
# ─────────────────────────────────────────────

def bs_put_ev(fwd: float, K: float, T: float, iv: float) -> Optional[float]:
    """European put by parity off the same Black machinery: P = C - F + K."""
    c = oe.bs_call_ev(fwd, K, T, iv)
    if c is None:
        return None
    return c - fwd + K


def leg_value(leg: dict, S: float, t_rem: float, spot: float,
              smile: Optional[list], carry: float,
              crush: float) -> float:
    """Value of ONE contract of this leg with the underlying at S.

    t_rem <= 0 is expiry: pure intrinsic, no model. Otherwise the leg is
    priced off the forward from S, at the smile-rolled IV for its own strike,
    with the crush that corresponds to the realized move."""
    K, right = leg["strike"], leg["right"]
    if t_rem <= 1.0 / 365.0:
        return max(S - K, 0.0) if right == "C" else max(K - S, 0.0)
    F = S * math.exp(carry * t_rem) if carry else S
    quoted = leg.get("iv") or 0.0
    iv = quoted
    if smile:
        rolled = oe.rolled_iv(smile, K, spot, S, quoted)
        # THE ROLL ADJUSTS VOL; IT DOES NOT REPLACE IT.
        # Interpolating a smile far from the money lands in the region where
        # quoted IVs are least reliable, and an unclamped lookup returned ~166%
        # for a strike quoting 39% — which priced a near-worthless put at $1.64
        # and made deep-OTM puts look like the best trades on the board.
        # Outside a factor of two from the strike's own quote, trust the quote.
        if quoted > 0.01:
            rolled = min(max(rolled, 0.5 * quoted), 2.0 * quoted)
        iv = rolled
    iv = min(max(iv * crush, 1e-4), 5.0)
    v = (oe.bs_call_ev(F, K, t_rem, iv) if right == "C"
         else bs_put_ev(F, K, t_rem, iv))
    if v is None:
        return max(S - K, 0.0) if right == "C" else max(K - S, 0.0)
    return max(v, 0.0)


def structure_cost(legs: list) -> float:
    """Net debit per share, INCLUDING commission on every leg.

    Buying lifts the ask, selling hits the bid — the wrong way round on both
    sides. Commission is charged per leg because the broker does."""
    total = 0.0
    for lg in legs:
        q = lg["qty"]
        px = lg["ask"] if q > 0 else lg["bid"]
        total += q * px + abs(q) * oe._commission_ps()
    return total


def structure_exit(legs: list, S: float, ref_day: float, spot: float,
                   smile: Optional[list], carry: float,
                   crush: float) -> float:
    """Proceeds from unwinding the whole structure at underlying S.

    Each leg is closed against the side that hurts: longs are sold at their
    bid, shorts are bought back at their ask. Legs already expired settle at
    intrinsic and cost nothing to close."""
    total = 0.0
    for lg in legs:
        t_rem = max(lg["T"] - ref_day / 365.0, 0.0)
        v = leg_value(lg, S, t_rem, spot, smile, carry, crush)
        half = max(lg["ask"] - lg["bid"], 0.0) / 2.0
        if t_rem <= 1.0 / 365.0:
            total += lg["qty"] * v            # settles, no spread crossed
        elif lg["qty"] > 0:
            total += lg["qty"] * oe.exit_proceeds_for(v, half)
        else:
            # buying back a short: pay the value PLUS the half-spread and fee
            total += lg["qty"] * (v + half) - abs(lg["qty"]) * oe._commission_ps()
    return total


# ─────────────────────────────────────────────
# EVALUATION
# ─────────────────────────────────────────────

def evaluate(legs: list, spot: float, scenarios: list, atm_iv: Optional[float],
             catalyst_T: Optional[float], base_crush: float,
             smile: Optional[list] = None, carry: float = 0.0,
             nodes: int = FINAL_NODES) -> Optional[dict]:
    """Score a structure over the scenario mixture.

    Returns EV per dollar AT RISK, exact Kelly, probability of profit, and the
    worst outcome across the sampled distribution. None if the structure
    cannot be costed (a net credit, or dust quotes)."""
    debit = structure_cost(legs)
    if debit < MIN_DEBIT:
        return None                      # credit, zero-cost, or all friction
    # Round-trip friction: half-spread + commission on every leg, both ways.
    friction = sum(max(lg["ask"] - lg["bid"], 0.0) / 2.0
                   + 2.0 * oe._commission_ps() for lg in legs)
    if debit < COST_COVER * friction:
        return None                      # costs would dominate the position
    horizon = catalyst_T if (catalyst_T and catalyst_T > 0) else \
        oe.DEFAULT_HORIZON_DAYS / 365.0
    sigma = oe.branch_sigma(scenarios, atm_iv, max(horizon, 1e-6), 1.0)

    _saved = oe.MIX_NODES
    oe.MIX_NODES = nodes
    try:
        pts = oe.mixture_nodes(spot, max(horizon, 1e-6), scenarios, sigma, 1.0)
    finally:
        oe.MIX_NODES = _saved

    exit_day = (catalyst_T * 365.0) if catalyst_T else oe.DEFAULT_HORIZON_DAYS
    rets = []
    for w, S in pts:
        crush = oe.crush_for_move(base_crush, S / spot - 1.0)
        proceeds = structure_exit(legs, S, exit_day, spot, smile, carry, crush)
        rets.append((w, proceeds / debit - 1.0))
    ev = sum(w * r for w, r in rets)
    kelly = oe.kelly_fraction(rets)
    p_profit = sum(w for w, r in rets if r > 0)
    worst = min(r for _, r in rets)
    return {"legs": legs, "debit": debit, "ev": ev, "kelly": kelly,
            "p_profit": p_profit, "worst": worst,
            "friction": friction, "n_legs": len(legs)}


# ─────────────────────────────────────────────
# ENUMERATION
# ─────────────────────────────────────────────

def _rows_for(block: dict, right: str, spot: float) -> list:
    """Tradeable legs from a matrix block, with unusable quotes rejected.
    The matrix only analyses calls, so puts come from the raw chain the block
    carries."""
    out = []
    src = block["rows"] if right == "C" else (block.get("put_rows") or [])
    for r in src:
        bid, ask = r.get("bid") or 0.0, r.get("ask") or 0.0
        K = r["strike"]
        if ask < MIN_LEG_PRICE or bid <= 0:
            continue                       # dust, or one-sided market
        if (r.get("oi") or 0) < MIN_LEG_OI:
            continue                       # nothing outstanding to trade
        intrinsic = max(spot - K, 0.0) if right == "C" else max(K - spot, 0.0)
        if intrinsic > 0 and ask < intrinsic * INTRINSIC_TOL:
            continue                       # below intrinsic = stale quote
        out.append({"strike": K, "right": right,
                    "bid": bid, "ask": ask, "iv": r.get("iv") or 0.0,
                    "T": max(block["days"], 1) / 365.0,
                    "expiry": block["expiry"], "oi": r.get("oi", 0)})
    return sorted(out, key=lambda x: x["strike"])


def enumerate_structures(blocks: list, spot: float,
                         max_per_type: int = 400) -> list:
    """Build the candidate set. Combinatorics are bounded deliberately: every
    (K1,K2) pair on a wide chain is thousands of structures, and a screen that
    takes a minute is a screen nobody runs."""
    cands = []
    for b in blocks:
        calls = _rows_for(b, "C", spot)
        puts = _rows_for(b, "P", spot)

        # singles — the existing behaviour, kept as the baseline to beat
        for c in calls:
            cands.append([dict(c, qty=1)])
        for p in puts:
            cands.append([dict(p, qty=1)])

        # call debit spreads: long lower strike, short higher
        for i, lo in enumerate(calls):
            for hi in calls[i + 1:i + 1 + MAX_WIDTH_STRIKES]:
                if hi["bid"] <= 0:
                    continue
                if hi["bid"] < MIN_CREDIT_RATIO * lo["ask"]:
                    continue          # short leg funds too little to bother
                cands.append([dict(lo, qty=1), dict(hi, qty=-1)])

        # put debit spreads: long higher strike, short lower
        for i, hi in enumerate(puts):
            for lo in puts[max(0, i - MAX_WIDTH_STRIKES):i]:
                if lo["bid"] <= 0:
                    continue
                if lo["bid"] < MIN_CREDIT_RATIO * hi["ask"]:
                    continue
                cands.append([dict(hi, qty=1), dict(lo, qty=-1)])

    # calendars: same strike, sell the near expiry, buy the far one. This is
    # the purest way to sell event premium while staying long the thesis.
    by_exp = {b["expiry"]: b for b in blocks}
    exps = sorted(by_exp, key=lambda e: by_exp[e]["days"])
    for i, near in enumerate(exps):
        for far in exps[i + 1:i + 3]:
            nb, fb = by_exp[near], by_exp[far]
            ncalls = {r["strike"]: r for r in _rows_for(nb, "C", spot)}
            for fc in _rows_for(fb, "C", spot):
                nc = ncalls.get(fc["strike"])
                if not nc or nc["bid"] <= 0:
                    continue
                if nc["bid"] < MIN_CREDIT_RATIO * fc["ask"]:
                    continue
                cands.append([dict(fc, qty=1), dict(nc, qty=-1)])
    return cands[:max_per_type * 12]


STRUCTURE_NAMES = {
    ("C", 1): "long call", ("P", 1): "long put",
}


def describe(legs: list) -> str:
    """Human-readable name, e.g. '09-18 $20/$25 call debit spread'."""
    if len(legs) == 1:
        lg = legs[0]
        kind = "call" if lg["right"] == "C" else "put"
        return f"{lg['expiry'][5:]} ${lg['strike']:g} long {kind}"
    a, b = legs[0], legs[1]
    if a["expiry"] != b["expiry"]:
        return (f"${a['strike']:g} calendar: sell {b['expiry'][5:]} / "
                f"buy {a['expiry'][5:]}")
    kind = "call" if a["right"] == "C" else "put"
    return (f"{a['expiry'][5:]} ${a['strike']:g}/${b['strike']:g} "
            f"{kind} debit spread")


def kind_of(legs: list) -> str:
    """Structure family, for grouping the menu."""
    if len(legs) == 1:
        return "long call" if legs[0]["right"] == "C" else "long put"
    if legs[0]["expiry"] != legs[1]["expiry"]:
        return "calendar"
    return ("call spread" if legs[0]["right"] == "C" else "put spread")


def structure_matrix(res: dict, spot: float, scenarios: list,
                     catalyst_days: Optional[int], base_crush: float,
                     smile: Optional[list], carry: float,
                     n_prices: int = 26, max_cols: int = 16) -> dict:
    """Price x time P/L grid for a STRUCTURE, in contract_matrix's shape.

    The price path itself is borrowed from contract_matrix so the learned
    path shape stays in exactly one place; only the VALUATION differs, because
    a spread's payoff is not a single option's."""
    legs = res["legs"]
    debit = res["debit"]
    long_leg = max(legs, key=lambda l: l["qty"])
    expiry_days = int(round(max(l["T"] for l in legs) * 365))
    expiry_days = max(expiry_days, 1)

    base = oe.contract_matrix(
        spot, long_leg["strike"], expiry_days,
        iv=long_leg.get("iv") or 0.5, entry_cost=max(debit, 0.01),
        up_move=max((m for _, m in scenarios), default=0.15),
        scenarios=scenarios, catalyst_days=catalyst_days,
        iv_crush=base_crush, n_prices=n_prices, max_cols=max_cols,
        carry=carry, smile=smile, half_spread=0.0, commission=False)

    def _pl(S, d):
        crush = oe.crush_for_move(base_crush, S / spot - 1.0)
        proceeds = structure_exit(legs, S, d, spot, smile, carry, crush)
        return round((proceeds - debit) / debit * 100.0, 1)

    out = dict(base)
    out["pnl"] = [[_pl(S, d) for d in base["dates_d"]]
                  for S in base["prices"]]
    out["entry_cost"] = debit
    out["structure"] = res.get("name", describe(legs))

    # ── EXPIRY ANATOMY ──
    # A spread has a ceiling and a floor; a long call has neither. Those are
    # the numbers that make a multi-leg position legible, and the grid alone
    # does not state them. Computed on a fine price scan at expiry so they
    # hold for any leg combination rather than only the textbook shapes.
    lo, hi = min(base["prices"]), max(base["prices"])
    lo, hi = lo * 0.6, hi * 1.4
    steps = 480
    scan = []
    for i in range(steps + 1):
        S = lo + (hi - lo) * i / steps
        val = sum(l["qty"] * (max(S - l["strike"], 0.0) if l["right"] == "C"
                              else max(l["strike"] - S, 0.0)) for l in legs)
        scan.append((S, val - debit))
    out["max_profit"] = max(p for _, p in scan)
    out["max_loss"] = min(p for _, p in scan)
    bes = []
    for (s0, p0), (s1, p1) in zip(scan, scan[1:]):
        if (p0 < 0 <= p1) or (p0 > 0 >= p1):
            w = abs(p0) / max(abs(p0) + abs(p1), 1e-9)
            bes.append(s0 + (s1 - s0) * w)
    out["breakevens"] = bes
    out["sizing"] = res.get("sizing")
    for key in ("path", "path_dense"):
        if base.get(key):
            out[key] = [(d, S, _pl(S, d)) for d, S, _ in base[key]]
    if base.get("paths"):
        p = {}
        for k, v in base["paths"].items():
            if k == "scenarios":
                p[k] = [(pr, m, [(d, S, _pl(S, d)) for d, S, _ in pth])
                        for pr, m, pth in v]
            else:
                p[k] = [(d, S, _pl(S, d)) for d, S, _ in v]
        out["paths"] = p
        ed = base["exit_day"]
        per = p.get("scenarios") or []
        if per:
            out["exit_pnls"] = {
                "best": p["best"][ed][2], "worst": p["worst"][ed][2],
                "expected": round(sum(pr * pth[ed][2] for pr, _, pth in per), 1)}
    return out


def rank_by_type(blocks: list, spot: float, scenarios: list,
                 catalyst_T: Optional[float], base_crush: float) -> dict:
    """Best surviving structure of EACH family.

    Ranking on Kelly alone always crowns the deep-ITM stock-replacement call:
    it wins most of the time and loses little, which is exactly what Kelly
    rewards, and it says nothing about the spread that offers sixteen times
    the expected return for a fifth of the capital. Showing one per family
    turns the output from a verdict into a menu."""
    out = {}
    by_exp = {b["expiry"]: b for b in blocks}
    for legs in enumerate_structures(blocks, spot):
        b = by_exp.get(legs[0]["expiry"]) or {}
        smile = (b.get("put_smile") if legs[0]["right"] == "P"
                 else b.get("smile"))
        try:
            r = evaluate(legs, spot, scenarios, b.get("atm_iv"), catalyst_T,
                         base_crush, smile=smile, carry=b.get("carry", 0.0),
                         nodes=SCREEN_NODES)
        except Exception:
            continue
        if not r or r["kelly"] is None:
            continue
        k = kind_of(legs)
        # AFFORDABILITY IS A RANKING CRITERION, NOT A FOOTNOTE.
        # Kelly crowned a deep-ITM call at an $85 debit — $8,501 for one
        # contract. Against a small account that is not a recommendation.
        # A structure whose single contract exceeds the position ceiling is
        # dropped: it cannot be traded, so it cannot be the answer.
        try:
            import trading_config as _tc
            r["sizing"] = _tc.size_position(r["debit"], r.get("kelly"))
            if not r["sizing"]["affordable"]:
                continue
        except Exception:
            r["sizing"] = None
        if k not in out or r["kelly"] > out[k]["kelly"]:
            r["name"] = describe(legs)
            r["kind"] = k
            out[k] = r
    return out


def rank(blocks: list, spot: float, scenarios: list,
         catalyst_T: Optional[float], base_crush: float,
         top_n: int = 12) -> list:
    """Screen every structure coarsely, then re-score the finalists exactly.

    Two passes because the full quadrature over thousands of candidates is
    slow enough that nobody would wait for it, and a coarse screen is more
    than accurate enough to decide who deserves the expensive treatment."""
    cands = enumerate_structures(blocks, spot)
    by_exp = {b["expiry"]: b for b in blocks}

    def _ctx(legs):
        b = by_exp.get(legs[0]["expiry"]) or {}
        # Calls read the call smile, puts read the put smile.
        smile = (b.get("put_smile") if legs[0]["right"] == "P"
                 else b.get("smile"))
        return smile, b.get("carry", 0.0), b.get("atm_iv")

    screened = []
    for legs in cands:
        smile, carry, aiv = _ctx(legs)
        try:
            r = evaluate(legs, spot, scenarios, aiv, catalyst_T, base_crush,
                         smile=smile, carry=carry, nodes=SCREEN_NODES)
        except Exception:
            continue
        if r and r["kelly"] is not None:
            screened.append(r)
    screened.sort(key=lambda r: r["kelly"], reverse=True)

    finals = []
    for r in screened[:top_n * 4]:
        smile, carry, aiv = _ctx(r["legs"])
        try:
            f = evaluate(r["legs"], spot, scenarios, aiv, catalyst_T,
                         base_crush, smile=smile, carry=carry,
                         nodes=FINAL_NODES)
        except Exception:
            continue
        if f and f["kelly"] is not None:
            f["name"] = describe(f["legs"])
            finals.append(f)
    finals.sort(key=lambda r: r["kelly"], reverse=True)
    return finals[:top_n]
