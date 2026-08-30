"""
borrow_availability.py
======================
Shares available to borrow, utilization, and how close the borrow is to zero.

WHY THIS IS THE GAP THAT MATTERED MOST
--------------------------------------
A squeeze needs shorts that CANNOT cover cheaply. The scanner has never
measured that directly. What it has is `ctb_proxy`, and measured over 2,430
graded rows:

    corr(short_interest, ctb_proxy) = +0.875

because calc_ctb_proxy computes cost-to-borrow FROM short interest. It is
short interest wearing a different hat. "CTB velocity rising" — one of the
headline signals — is arithmetically "short interest rising".

The variable that actually gates a squeeze is UTILIZATION: what fraction of
the lendable pool is already lent. At 100% there are no more shares to
borrow, so new shorts cannot press and existing shorts cannot roll. That is
the precondition. Nothing in this repo could see it.

WHY IT IS NOT WIRED TO A LIVE FEED
----------------------------------
Checked directly, 2026-08-30:

    iborrowdesk.com/api/ticker/...      connection refused
    ftp3.interactivebrokers.com/usa.txt timeout
    ftp.interactivebrokers.com/usa.txt  timeout
    shortstock.interactivebrokers.com   DNS does not resolve
    interactivebrokers.com public pages marketing only, no data XHR

IBKR's own page says it plainly: quantity available, number of lenders and
indicative borrow rate live inside Client Portal or TWS, behind a login. The
"prepared lists grouped by country" is the FTP mirror, which is not reachable
from here.

So this module ships with the ARITHMETIC and no source. Point it at one and
it starts working; until then it reports `available=False` and changes
nothing. Three ways in:

  1. Drop a CSV at borrow_data/<TICKER>.csv or borrow_data/latest.csv with
     columns symbol,available[,rate][,lenders]. A Client Portal export or a
     bulk shortable-securities download both fit.
  2. Set BORROW_SOURCE to a callable in your own module.
  3. Any paid feed (ORTEX, S3, Fintel) — write a six-line adapter.

WHAT WE DO ALREADY HAVE, AND IT IS NOT NOTHING
----------------------------------------------
Reg SHO threshold status is the DOWNSTREAM SYMPTOM of this exact condition.
A security lands on that list because deliveries fail, and deliveries fail
because shares could not be located to borrow. reg_sho.py is therefore a
lagging, binary, officially-adjudicated proxy for "availability hit zero" —
worse than the real number in timeliness and resolution, but real, free, and
already wired in. Utilization would lead it by days to weeks.
"""

import csv
import json
import os
from typing import Optional, Callable

_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_DIR, "borrow_data")
SNAPSHOT_FILE = os.path.join(_DIR, "borrow_snapshots.json")

# Set this to a callable taking a ticker and returning
# {'available': int, 'rate': float|None, 'lenders': int|None} — or leave None
# and drop CSVs in borrow_data/ instead.
BORROW_SOURCE: Optional[Callable[[str], Optional[dict]]] = None

# ── THRESHOLDS ──
# Utilization at or above this is "no borrow left". Widely cited as the
# squeeze precondition; NOT validated against this repo's outcomes, because
# there is no data to validate it against yet.
UTIL_EXTREME = 0.95
UTIL_HIGH = 0.85
# Shares available below this share of float is a functionally empty borrow.
AVAIL_FLOOR_PCT_FLOAT = 0.001      # 0.1% of float
# Session-over-session collapse in availability that counts as "draining".
DRAIN_FRACTION = 0.50              # lost half the available pool


def _read_csv_source(ticker: str) -> Optional[dict]:
    """Look for borrow_data/<TICKER>.csv, then borrow_data/latest.csv.

    Accepts any column naming that contains the words below, so an export
    does not have to be reshaped by hand.
    """
    tk = (ticker or "").upper().strip()
    if not tk or not os.path.isdir(DATA_DIR):
        return None

    def _match(header, *words):
        for h in header:
            hl = (h or "").lower().replace("_", " ")
            if any(w in hl for w in words):
                return h
        return None

    for fname in (f"{tk}.csv", "latest.csv", "usa.csv", "shortable.csv"):
        path = os.path.join(DATA_DIR, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, newline="", encoding="utf-8", errors="replace") as f:
                rd = csv.DictReader(f)
                hdr = rd.fieldnames or []
                c_sym = _match(hdr, "symbol", "ticker")
                c_av = _match(hdr, "available", "quantity", "shares")
                c_rt = _match(hdr, "rate", "fee", "cost")
                c_ln = _match(hdr, "lender")
                if not c_av:
                    continue
                for row in rd:
                    if c_sym and (row.get(c_sym) or "").upper().strip() != tk:
                        continue
                    raw = (row.get(c_av) or "").replace(",", "").strip()
                    if raw.lower() in ("", "none", "n/a"):
                        continue
                    try:
                        av = float(raw)
                    except ValueError:
                        continue
                    out = {"available": av, "source": fname}
                    for key, col in (("rate", c_rt), ("lenders", c_ln)):
                        if col:
                            try:
                                out[key] = float((row.get(col) or "")
                                                 .replace("%", "").replace(",", ""))
                            except ValueError:
                                pass
                    return out
        except OSError:
            continue
    return None


def _load_snaps() -> dict:
    try:
        with open(SNAPSHOT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_snap(ticker: str, available: float):
    """One reading per ticker per day, last 30 kept."""
    from datetime import date
    snaps = _load_snaps()
    row = snaps.setdefault(ticker.upper(), [])
    today = date.today().isoformat()
    row = [r for r in row if r.get("date") != today]
    row.append({"date": today, "available": available})
    snaps[ticker.upper()] = row[-30:]
    try:
        tmp = SNAPSHOT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snaps, f)
        os.replace(tmp, SNAPSHOT_FILE)
    except OSError:
        pass


def check(ticker: str, shares_short: Optional[float] = None,
          float_shares: Optional[float] = None) -> dict:
    """Borrow availability and utilization for one ticker.

    `available=False` means no source is configured — distinct from a source
    that reports zero shares, which is the loudest possible signal.

    UTILIZATION, AND WHY IT IS AN APPROXIMATION
    -------------------------------------------
    True utilization is on-loan / total-lendable. A broker feed publishes the
    quantity still AVAILABLE, not the total pool, so the denominator has to
    be reconstructed. Short interest is a reasonable stand-in for shares on
    loan, giving:

        utilization ~= shares_short / (shares_short + shares_available)

    Of the borrowable pool visible to us, how much is already taken. Both
    inputs are imperfect — short interest is bi-monthly and lags, and one
    broker's availability is not the whole street's — so this is a floor on
    the real number rather than the number itself. It is still the closest
    thing to the actual precondition that free data allows.
    """
    out = {
        "ticker": (ticker or "").upper().strip(),
        "available": False,          # is a SOURCE configured
        "shares_available": None,
        "borrow_rate": None,
        "lenders": None,
        "utilization": None,
        "avail_pct_float": None,
        "prev_available": None,
        "drain_pct": None,
        "state": "",
        "near_zero": False,
        "draining": False,
        "notes": [],
    }
    tk = out["ticker"]
    if not tk:
        return out

    data = None
    if BORROW_SOURCE is not None:
        try:
            data = BORROW_SOURCE(tk)
        except Exception:
            data = None
    if data is None:
        data = _read_csv_source(tk)
    if not data or data.get("available") is None:
        out["notes"].append(
            "no borrow source configured — see module docstring "
            "(drop a CSV in borrow_data/, or set BORROW_SOURCE)")
        return out

    av = float(data["available"])
    out["available"] = True
    out["shares_available"] = av
    out["borrow_rate"] = data.get("rate")
    out["lenders"] = data.get("lenders")

    if shares_short and shares_short > 0:
        denom = shares_short + max(av, 0.0)
        if denom > 0:
            out["utilization"] = shares_short / denom
    if float_shares and float_shares > 0:
        out["avail_pct_float"] = av / float_shares

    # ── trend: is the pool draining ──
    snaps = _load_snaps().get(tk, [])
    prior = [s for s in snaps if s.get("available") is not None]
    if prior:
        prev = prior[-1]["available"]
        out["prev_available"] = prev
        if prev > 0:
            out["drain_pct"] = av / prev - 1.0
            out["draining"] = (av / prev) <= (1.0 - DRAIN_FRACTION)
    _save_snap(tk, av)

    # ── state ──
    util = out["utilization"]
    apf = out["avail_pct_float"]
    out["near_zero"] = bool(
        (util is not None and util >= UTIL_EXTREME)
        or (apf is not None and apf <= AVAIL_FLOOR_PCT_FLOAT)
        or av <= 0)

    if av <= 0:
        out["state"] = "NO BORROW — zero shares available"
        out["notes"].append(
            "zero available: new shorts cannot be opened and existing ones "
            "cannot roll. This is the precondition, not the squeeze")
    elif out["near_zero"]:
        out["state"] = "BORROW EXHAUSTED"
        bits = []
        if util is not None:
            bits.append(f"utilization {util:.1%}")
        if apf is not None:
            bits.append(f"{apf:.3%} of float still lendable")
        out["notes"].append("; ".join(bits))
    elif util is not None and util >= UTIL_HIGH:
        out["state"] = "BORROW TIGHT"
        out["notes"].append(f"utilization {util:.1%}")
    else:
        out["state"] = "BORROW AVAILABLE"

    if out["draining"]:
        out["notes"].append(
            f"available pool fell {out['drain_pct']:.0%} since the last "
            f"reading ({out['prev_available']:,.0f} -> {av:,.0f}) — "
            f"the direction matters more than the level")
    return out


def format_block(r: dict, indent: str = "  ") -> str:
    L = [f"{indent}BORROW AVAILABILITY"]
    if not r.get("available"):
        L.append(f"{indent}   not measured — {'; '.join(r.get('notes', []))}")
        return "\n".join(L) + "\n"
    L.append(f"{indent}   Shares available:   {r['shares_available']:,.0f}")
    if r.get("utilization") is not None:
        L.append(f"{indent}   Utilization:        {r['utilization']:.1%}")
    if r.get("avail_pct_float") is not None:
        L.append(f"{indent}   Available / float:  {r['avail_pct_float']:.3%}")
    if r.get("borrow_rate") is not None:
        L.append(f"{indent}   Borrow rate:        {r['borrow_rate']:.1f}%"
                 f"   (REAL, not the SI-derived proxy)")
    if r.get("lenders") is not None:
        L.append(f"{indent}   Lenders:            {r['lenders']:.0f}")
    if r.get("prev_available") is not None:
        L.append(f"{indent}   Previous reading:   {r['prev_available']:,.0f}")
    L.append(f"{indent}   State:              {r['state']}")
    for n in r.get("notes", []):
        L.append(f"{indent}   . {n}")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    import sys
    print(__doc__.split("WHY THIS IS")[0].strip())
    print(f"source dir: {DATA_DIR}"
          f"  ({'exists' if os.path.isdir(DATA_DIR) else 'NOT PRESENT'})")
    print(f"BORROW_SOURCE: {BORROW_SOURCE}")
    for tk in (sys.argv[1:] or ["GME", "TASK"]):
        print()
        print(format_block(check(tk, shares_short=10_000_000,
                                 float_shares=50_000_000)))
