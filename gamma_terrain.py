"""
gamma_terrain.py
================
Dealer-gamma terrain for a single name: the full net-GEX-vs-price profile,
the gamma flip (regime boundary), and BOTH hedging walls.

Reuses squeeze_deep's exact math (_bs_gamma, _net_gex_at_price,
_gamma_flip_from_rows) so there is ONE implementation of the GEX model in
the toolkit — no drift between scanner and terrain.

What this adds on top of squeeze_deep:
  * PUT WALL  — largest put dollar-gamma strike at/below spot (support side;
                squeeze_deep only tracked the call wall above spot)
  * PROFILE   — the net-GEX curve across ±span, surfaced instead of discarded
  * REPORT    — terminal-friendly terrain summary
  * FETCHER   — yfinance chain -> squeeze_deep row format (kept separate from
                the math so everything below fetch is testable offline)

HONEST LIMITS (read once, remember always):
  * Open interest from yfinance is PRIOR-SESSION data, refreshed each
    morning. Terrain is morning-fresh, stale intraday. Do not treat walls
    as tick-precise.
  * The +call/-put dealer convention is the classic SqueezeMetrics
    assumption. In speculative call-buying frenzies it INVERTS (dealers are
    short the calls customers bought). squeeze_deep's CALL-SKEW OVERRIDE
    exists for exactly this; the report repeats the caveat.
  * These levels are widely watched (SpotGamma et al.). The value is not
    secrecy — it's that hedging flows around them are real. This is
    decision support for exits/risk, not a money printer.

Usage:
    python gamma_terrain.py CRSP            # terminal report
    from gamma_terrain import fetch_expiries_yf, compute_terrain
"""

from typing import Optional

from squeeze_deep import (          # single source of truth for the math
    _bs_gamma,
    _net_gex_at_price,
    _gamma_flip_from_rows,
)

LOCALITY = 0.25   # same ±25% strike-locality gate squeeze_deep uses


# ─────────────────────────────────────────────
# FETCH (yfinance -> squeeze_deep row format)
# ─────────────────────────────────────────────

# Expiries dropped during the most recent fetch, as ["2026-09-18: HTTPError"].
# Read by the options layer so a partially-fetched chain is reported rather
# than quietly presented as the whole chain.
LAST_FETCH_ISSUES = []


def fetch_expiries_yf(ticker: str, n_expiries: int = 4):
    """Fetch the nearest N option expiries via yfinance.

    Returns (spot, expiries) where expiries = [(T_years, call_rows, put_rows)]
    with rows as plain dicts carrying strike/openInterest/impliedVolatility —
    the exact shape squeeze_deep's GEX helpers consume.
    Raises RuntimeError with a plain message on any fetch problem.

    Partial failures are recorded in LAST_FETCH_ISSUES rather than hidden.
    """
    import time
    import yfinance as yf
    from datetime import datetime

    tk = yf.Ticker(ticker)
    spot = None
    try:
        h = tk.history(period="1d")
        if h is not None and not h.empty:
            spot = float(h["Close"].iloc[-1])
    except Exception:
        pass
    if not spot:
        fi = getattr(tk, "fast_info", None)
        spot = float(getattr(fi, "last_price", 0) or 0) if fi else 0
    if not spot:
        raise RuntimeError(f"No spot price for {ticker}")

    dates = list(tk.options or [])[:n_expiries]
    if not dates:
        raise RuntimeError(f"No listed options for {ticker}")

    expiries = []
    now = datetime.now()
    # Per-expiry failures used to `continue` in total silence, so a chain that
    # lost 8 of its 10 expiries to transient throttling looked identical to a
    # ticker that genuinely lists two. Record what was dropped so callers can
    # say so out loud, and retry once before giving up on an expiry.
    LAST_FETCH_ISSUES.clear()
    for ds in dates:
        try:
            try:
                chain = tk.option_chain(ds)
            except Exception:
                time.sleep(1.0)                  # one transient-failure retry
                chain = tk.option_chain(ds)
            T = max((datetime.strptime(ds, "%Y-%m-%d") - now).days, 0) / 365.0
            cols = ["strike", "openInterest", "impliedVolatility",
                    "bid", "ask", "lastPrice", "volume"]
            ccols = [c for c in cols if c in chain.calls.columns]
            pcols = [c for c in cols if c in chain.puts.columns]
            # yfinance chains carry NaN in openInterest/bid/ask/IV (very
            # common on big ETF chains). NaN is truthy and survives `or 0`
            # guards, then blows up int() casts downstream — scrub here.
            calls = chain.calls[ccols].fillna(0.0).to_dict("records")
            puts = chain.puts[pcols].fillna(0.0).to_dict("records")
            for r in calls:
                r["expiry"] = ds     # extra keys are ignored by the GEX math
            for r in puts:
                r["expiry"] = ds
            expiries.append((T, calls, puts))
        except Exception as e:
            LAST_FETCH_ISSUES.append(f"{ds}: {type(e).__name__}")
            continue
    if not expiries:
        raise RuntimeError(
            f"Option chains unavailable for {ticker} — all {len(dates)} "
            f"expiries failed to fetch ({'; '.join(LAST_FETCH_ISSUES[:3])}). "
            f"Usually transient rate limiting; retry in a minute.")
    return spot, expiries


# ─────────────────────────────────────────────
# TERRAIN
# ─────────────────────────────────────────────

def _walls_from_rows(spot: float, expiries: list) -> tuple:
    """(call_wall, put_wall): the strike with the largest dealer dollar-gamma
    at/above spot on the call side, and at/below spot on the put side —
    evaluated AT spot with the same formula and locality gate as
    squeeze_deep._gex_from_rows."""
    call_val = put_val = 0.0
    call_wall = put_wall = None
    for T, calls, puts in expiries:
        if T <= 0:
            T = 1.0 / 365.0
        for rows, is_call in ((calls, True), (puts, False)):
            for r in rows:
                K = r.get("strike") or 0
                oi = r.get("openInterest") or 0
                iv = r.get("impliedVolatility") or 0
                if not K or not oi or not iv:
                    continue
                if abs(K - spot) / spot > LOCALITY:
                    continue
                dollar = (_bs_gamma(spot, K, T, iv)
                          * oi * 100 * spot * spot * 0.01)
                if is_call and K >= spot and dollar > call_val:
                    call_val, call_wall = dollar, K
                elif (not is_call) and K <= spot and dollar > put_val:
                    put_val, put_wall = dollar, K
    return call_wall, put_wall


def compute_terrain(spot: float, expiries: list,
                    span: float = 0.30, steps: int = 61) -> dict:
    """Full gamma terrain around spot. Pure function — no network.

    Returns dict:
      spot, gex_spot_musd, flip_price, flip_pct,
      call_wall, put_wall, profile [(price, gex_musd)...], regime, note
    """
    out = {"spot": spot, "gex_spot_musd": None, "flip_price": None,
           "flip_pct": None, "call_wall": None, "put_wall": None,
           "profile": [], "regime": "", "note": ""}
    if not spot or spot <= 0 or not expiries:
        out["note"] = "no data"
        return out

    # Profile across the band (same grid the flip finder scans)
    lo, hi = spot * (1 - span), spot * (1 + span)
    grid = [lo + (hi - lo) * i / (steps - 1) for i in range(steps)]
    out["profile"] = [(S, _net_gex_at_price(S, expiries)) for S in grid]

    out["gex_spot_musd"] = _net_gex_at_price(spot, expiries)
    flip = _gamma_flip_from_rows(spot, expiries, span=span, steps=steps)
    # Guard: a genuine flip needs REAL gamma of both signs in the band.
    # A one-sided book decays to ~0 where strikes leave the locality window,
    # and the finder can read that zero-tail as a sign change — a phantom
    # "flip" at the data boundary, not a regime boundary.
    eps = 1e-3  # $M per 1% — below this there is no meaningful dealer gamma
    gvals = [g for _, g in out["profile"]]
    if flip is not None and not (min(gvals) < -eps and max(gvals) > eps):
        flip = None
    if flip is not None:
        out["flip_price"] = round(flip, 2)
        out["flip_pct"] = (flip - spot) / spot
    out["call_wall"], out["put_wall"] = _walls_from_rows(spot, expiries)

    g = out["gex_spot_musd"]
    if g is None:
        out["regime"] = "UNKNOWN"
    elif g < 0:
        out["regime"] = "SHORT-GAMMA — dealer hedging AMPLIFIES moves"
        if flip is not None and flip > spot:
            out["note"] = (f"Amplifying regime persists until ~${flip:.2f} "
                           f"({out['flip_pct']:+.1%}); above that dealers dampen.")
    elif g > 0.05:
        out["regime"] = "LONG-GAMMA — dealers dampen moves"
        if flip is not None and flip < spot:
            out["note"] = (f"Dampening regime holds down to ~${flip:.2f} "
                           f"({out['flip_pct']:+.1%}); below that moves amplify.")
    else:
        out["regime"] = "BALANCED — negligible net dealer gamma"

    if flip is None:
        out["note"] = (out["note"] + " No zero-gamma crossing within "
                       f"±{span:.0%} — one regime dominates the band.").strip()
    return out


# ─────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────

def format_terrain_report(t: dict, ticker: str = "") -> str:
    def d(v, fmt="${:.2f}"):
        return fmt.format(v) if v is not None else "—"
    spot = t.get("spot") or 0
    lines = [
        f"GAMMA TERRAIN — {ticker.upper()}",
        f"  Spot:            {d(spot)}",
        f"  Net GEX @ spot:  "
        + (f"{t['gex_spot_musd']:+.2f} $M per 1% move"
           if t.get("gex_spot_musd") is not None else "—"),
        f"  Regime:          {t.get('regime') or '—'}",
        f"  Gamma flip:      {d(t.get('flip_price'))}"
        + (f"  ({t['flip_pct']:+.1%} from spot)   <- regime boundary"
           if t.get("flip_pct") is not None else ""),
        f"  Call wall:       {d(t.get('call_wall'))}"
        + ("   (dealers sell rallies into this strike in long-gamma)"
           if t.get("call_wall") else ""),
        f"  Put wall:        {d(t.get('put_wall'))}"
        + ("   (heaviest put-gamma support below)"
           if t.get("put_wall") else ""),
    ]
    if t.get("note"):
        lines.append(f"  Note:            {t['note']}")
    lines.append("  Data:            prior-session OI (yfinance) — refreshed "
                 "mornings; stale intraday.")
    lines.append("  Caveat:          +call/-put dealer convention; inverts in "
                 "call-buying frenzies (see CALL-SKEW OVERRIDE in scanner).")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python gamma_terrain.py TICKER [n_expiries]")
        sys.exit(1)
    sym = sys.argv[1].upper()
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    spot, expiries = fetch_expiries_yf(sym, n)
    print(format_terrain_report(compute_terrain(spot, expiries), sym))
