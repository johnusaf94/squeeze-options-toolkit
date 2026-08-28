"""
options_golden.py
=================
The safety net for changing options_ev.py's math.

WHY THIS EXISTS
---------------
The pricing fixes queued for the options layer (exit-cost modeling, forward
pricing via put-call parity, a continuous scenario distribution, smile roll)
all change numbers that are currently unverifiable. A live chain moves every
second, so "run it before and after and eyeball it" cannot distinguish a fix
from a quote tick, let alone from a bug the fix introduced somewhere else.

So: freeze a real chain to disk, compute the FULL matrix from it, and store
that as the baseline. Every later run replays the SAME frozen chain through
the CURRENT code and diffs cell by cell. Then a pricing change produces an
exact, itemized answer to the only question that matters — what moved, by how
much, and did anything move that shouldn't have.

DETERMINISM
-----------
Replay pins three things that would otherwise drift:
  * the chain itself (frozen JSON — zero network calls on replay)
  * the spot price (stored, not re-fetched)
  * "today" (stored as as_of and threaded into options_ev's time math, so a
    chain captured on the 20th still prices at its original days-to-expiry
    when replayed in December)
Without the third, every diff would be dominated by time decay and the tool
would be useless.

USAGE
-----
    python options_golden.py capture CRSP            # freeze a chain + baseline
    python options_golden.py capture CRSP "40:+15, 35:0, 25:-10"
    python options_golden.py check                   # replay ALL baselines
    python options_golden.py check golden/CRSP_20260820.json
    python options_golden.py list

WORKFLOW FOR A PRICING CHANGE
-----------------------------
    1. capture (once, on a live chain — ideally a few tickers with different
       liquidity profiles: one tight mega-cap chain, one wide squeeze chain)
    2. make the change
    3. check  ->  read the itemized diff
    4. if the diff matches what the change was supposed to do, re-baseline
       with --accept; if anything else moved, the change has a bug

A baseline is evidence, not decoration. Re-accept deliberately, never
reflexively.
"""

import json
import os
import sys
from datetime import date, datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
GOLDEN_DIR = os.path.join(_DIR, "golden")

# Per-row numeric fields compared on replay. Anything the pricing math
# produces belongs here — a field NOT listed is a field a bug can change
# without this harness noticing.
COMPARE_FIELDS = [
    "ask", "bid", "spread_pct", "iv", "breakeven", "be_move", "entry_cost",
    "sc_ev", "best_mult", "sc_p_be", "mkt_p_be", "disagree", "mkt_ev",
    "p_itm_mkt", "sc_ev_exit", "kelly", "val_edge",
    # pre-fix companions: these must stay frozen forever. A change here
    # means a "fix" leaked into the baseline column and the side-by-side
    # comparison has quietly stopped comparing anything.
    "sc_ev_gross", "sc_ev_exit_gross",
    "kelly_ev",
]

# Below this absolute change a field counts as unchanged. Floating-point
# reassociation (a fix that reorders arithmetic without altering it) must not
# register as a difference, or every refactor drowns the signal.
EPS = 1e-9


# ─────────────────────────────────────────────
# CAPTURE
# ─────────────────────────────────────────────

def capture(ticker: str, scenario_text: str = "40:+15, 35:0, 25:-10",
            n_expiries: int = 10, max_dte=None, out_path: str = "",
            catalyst_days: int = 10) -> str:
    """Freeze a live chain plus the matrix it currently produces.

    catalyst_days is NOT optional in spirit: with no catalyst date the
    exit-at-catalyst branch returns None on every row, so sc_ev_exit, the
    Kelly exit path, and the crush simulator are all silently untested. The
    first version of this harness made exactly that mistake and produced a
    baseline that could not see the change it existed to verify."""
    import options_ev as oe
    from gamma_terrain import fetch_expiries_yf

    ticker = ticker.upper()
    scenarios = oe.parse_scenarios(scenario_text)
    spot, expiries = fetch_expiries_yf(ticker, n_expiries)
    if not expiries:
        raise RuntimeError(f"no chain returned for {ticker} — market closed "
                           f"with quotes cleared, or bad ticker")
    as_of = date.today()

    # expiries is [(T, calls, puts)]; calls/puts are lists of dicts already,
    # so it serializes as-is. Puts are captured even though today's matrix
    # ignores them — the forward-pricing fix will need them, and a baseline
    # missing them would have to be re-captured on a different day's chain.
    frozen = [{"T": T, "calls": calls, "puts": puts}
              for T, calls, puts in expiries]

    catalyst_iso = date.fromordinal(
        as_of.toordinal() + max(int(catalyst_days), 0)).isoformat()
    crush = oe.estimate_iv_crush(spot, expiries, catalyst_iso, "")
    blocks = oe.build_matrix(spot, expiries, scenarios, catalyst_iso,
                             max_dte=max_dte, iv_crush=crush["mult"],
                             as_of=as_of)
    payload = {
        "schema": 1,
        "ticker": ticker,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "catalyst_iso": catalyst_iso,
        "spot": spot,
        "scenario_text": scenario_text,
        "max_dte": max_dte,
        "n_expiries": n_expiries,
        "iv_crush": crush["mult"],
        "crush_method": crush["method"],
        "chain": frozen,
        "baseline": _snapshot(blocks),
    }
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    path = out_path or os.path.join(
        GOLDEN_DIR, f"{ticker}_{as_of:%Y%m%d}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    n_rows = sum(len(b["rows"]) for b in payload["baseline"])
    print(f"captured {ticker} @ ${spot:,.2f} — {len(frozen)} expiries, "
          f"{n_rows} rows -> {os.path.relpath(path, _DIR)}")
    return path


def _snapshot(blocks: list) -> list:
    """Blocks reduced to the numbers worth diffing."""
    out = []
    for b in blocks:
        rows = []
        for r in b["rows"]:
            rows.append({"strike": r["strike"],
                         **{k: r.get(k) for k in COMPARE_FIELDS}})
        out.append({"expiry": b["expiry"], "days": b["days"], "rows": rows})
    return out


# ─────────────────────────────────────────────
# REPLAY + DIFF
# ─────────────────────────────────────────────

def replay(path: str) -> dict:
    """Recompute from the frozen chain with the CURRENT code."""
    import options_ev as oe
    with open(path, encoding="utf-8") as f:
        g = json.load(f)
    scenarios = oe.parse_scenarios(g["scenario_text"])
    expiries = [(e["T"], e["calls"], e["puts"]) for e in g["chain"]]
    as_of = datetime.strptime(g["as_of"], "%Y-%m-%d").date()
    blocks = oe.build_matrix(g["spot"], expiries, scenarios,
                             g.get("catalyst_iso", ""),
                             max_dte=g.get("max_dte"),
                             iv_crush=g["iv_crush"], as_of=as_of)
    return {"golden": g, "current": _snapshot(blocks)}


def diff(path: str, verbose: bool = True) -> dict:
    r = replay(path)
    g, cur = r["golden"], r["current"]
    base = g["baseline"]

    def _index(snap):
        return {(b["expiry"], row["strike"]): row
                for b in snap for row in b["rows"]}

    bi, ci = _index(base), _index(cur)
    added = sorted(set(ci) - set(bi))
    removed = sorted(set(bi) - set(ci))
    changes = {}          # field -> list of (expiry, strike, old, new)
    for key in sorted(set(bi) & set(ci)):
        for fld in COMPARE_FIELDS:
            o, n = bi[key].get(fld), ci[key].get(fld)
            if o is None and n is None:
                continue
            if o is None or n is None:
                changes.setdefault(fld, []).append((key[0], key[1], o, n))
                continue
            if abs(float(n) - float(o)) > EPS:
                changes.setdefault(fld, []).append((key[0], key[1], o, n))

    n_rows = len(bi)
    # INVARIANT: whatever Kelly integrated must have the same mean as the
    # exit-EV column. These are two consumers of one model; if they disagree,
    # the ranking and the number on screen describe different trades.
    broken = []
    for b in cur:
        for row in b["rows"]:
            ke, ev = row.get("kelly_ev"), row.get("sc_ev_exit")
            if ke is not None and ev is not None and abs(ke - ev) > 1e-6:
                broken.append((b["expiry"], row["strike"], ke, ev))
    result = {"ticker": g["ticker"], "path": path, "rows": n_rows,
              "added": added, "removed": removed, "changes": changes,
              "invariant_broken": broken,
              "clean": not (added or removed or changes)}
    if verbose:
        _report(result)
    return result


def _report(res: dict):
    print("=" * 68)
    print(f"  GOLDEN CHECK — {res['ticker']}  "
          f"({os.path.basename(res['path'])}, {res['rows']} rows)")
    print("=" * 68)
    if res.get("invariant_broken"):
        bad = res["invariant_broken"]
        print(f"  *** INVARIANT VIOLATED on {len(bad)} row(s): Kelly and "
              f"sc_ev_exit are integrating DIFFERENT models ***")
        for exp, K, ke, ev in bad[:3]:
            print(f"      {exp} ${K:g}: kelly_ev {ke:+.6g} vs "
                  f"sc_ev_exit {ev:+.6g}")
    if res["clean"]:
        print("  CLEAN — every field identical to the baseline.")
        return
    if res["added"]:
        print(f"  ROWS APPEARED ({len(res['added'])}): "
              f"{res['added'][:6]}{' ...' if len(res['added']) > 6 else ''}")
    if res["removed"]:
        print(f"  ROWS VANISHED ({len(res['removed'])}): "
              f"{res['removed'][:6]}{' ...' if len(res['removed']) > 6 else ''}")
    for fld, items in res["changes"].items():
        deltas = [abs(float(n) - float(o)) for _, _, o, n in items
                  if o is not None and n is not None]
        avg_signed = ([(float(n) - float(o)) for _, _, o, n in items
                       if o is not None and n is not None] or [0.0])
        mean_signed = sum(avg_signed) / len(avg_signed)
        print(f"\n  {fld}: {len(items)}/{res['rows']} rows changed"
              + (f"   max |delta| {max(deltas):.6g}"
                 f"   mean delta {mean_signed:+.6g}" if deltas else ""))
        # the three largest movers, so a change is inspectable not just counted
        worst = sorted(items,
                       key=lambda t: (abs(float(t[3]) - float(t[2]))
                                      if (t[2] is not None and t[3] is not None)
                                      else float("inf")),
                       reverse=True)[:3]
        for exp, K, o, n in worst:
            os_ = f"{o:.6g}" if isinstance(o, (int, float)) else str(o)
            ns_ = f"{n:.6g}" if isinstance(n, (int, float)) else str(n)
            print(f"      {exp} ${K:g}:  {os_}  ->  {ns_}")
    print("\n  Verify every line above is a change you INTENDED. Then "
          "re-baseline:\n      python options_golden.py check --accept")
    print("=" * 68)


def accept(path: str):
    """Overwrite the baseline with current output, keeping the frozen chain."""
    r = replay(path)
    g = r["golden"]
    g["baseline"] = r["current"]
    g["rebaselined_at"] = datetime.now().isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(g, f, indent=1)
    print(f"  re-baselined {os.path.basename(path)}")


def _all_goldens() -> list:
    if not os.path.isdir(GOLDEN_DIR):
        return []
    return sorted(os.path.join(GOLDEN_DIR, fn)
                  for fn in os.listdir(GOLDEN_DIR) if fn.endswith(".json"))


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = (args[0].lower() if args else "check")

    if cmd == "capture":
        if len(args) < 2:
            print('usage: python options_golden.py capture TICKER '
                  '["40:+15, 35:0, 25:-10"]')
            sys.exit(1)
        scn = args[2] if len(args) > 2 else "40:+15, 35:0, 25:-10"
        capture(args[1], scn)

    elif cmd == "list":
        paths = _all_goldens()
        if not paths:
            print("no baselines yet — python options_golden.py capture TICKER")
        for p in paths:
            with open(p, encoding="utf-8") as f:
                g = json.load(f)
            n = sum(len(b["rows"]) for b in g["baseline"])
            print(f"  {os.path.basename(p):<28} {g['ticker']:<6} "
                  f"as_of {g['as_of']}  spot ${g['spot']:,.2f}  {n} rows")

    else:                                    # check
        accept_flag = "--accept" in args
        targets = [a for a in args[1:] if not a.startswith("--")]
        paths = targets or _all_goldens()
        if not paths:
            print("no baselines to check — "
                  "python options_golden.py capture TICKER")
            sys.exit(1)
        dirty = 0
        for p in paths:
            res = diff(p, verbose=True)
            if not res["clean"]:
                dirty += 1
                if accept_flag:
                    accept(p)
            print()
        sys.exit(1 if (dirty and not accept_flag) else 0)
