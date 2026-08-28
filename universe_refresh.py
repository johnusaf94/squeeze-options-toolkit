"""
universe_refresh.py
===================
Keep the squeeze universe CURRENT without hand-editing lists.

The problem this solves: the names people discuss online change constantly
(Groupon 46% today, something else next month), and a hardcoded list is
stale the day it's written. Rather than paste snapshots, this pulls the
*current* high-short-interest US equities and writes them to a supplement
file the scanner reads alongside the static tiers.

Two sources, in preference order:
  1. FINRA short-VOLUME nowcast — you ALREADY download these files
     (finra_cache). Names whose recent short-volume ratio is persistently
     elevated are surfaced from data you have, no new dependency.
  2. yfinance short-interest %float — for any candidate list, confirm
     shortPercentOfFloat and liquidity, so only tradable names are kept.

Output: universe_live.json  { "generated": iso, "tickers": [...],
                              "detail": {ticker: {si_pct, price, avg_vol}} }
get_universe() in squeeze_universe.py reads this automatically if present.

INCLUSION BAR is deliberately LOW and MECHANICAL — this is the SEARCH
space, not a buy list. A name qualifies if it's a real, liquid, US-listed
equity with elevated short interest. The scoring pipeline decides merit.

Usage:
    python universe_refresh.py               # refresh from a seed screen
    python universe_refresh.py --from-finra   # mine your finra_cache files
"""

import csv
import glob
import json
import os
from datetime import datetime
from typing import Optional

_DIR = os.path.dirname(os.path.abspath(__file__))
LIVE_FILE = os.path.join(_DIR, "universe_live.json")
FINRA_CACHE = os.path.join(_DIR, "finra_cache")

# Mechanical inclusion filters (search-space bar, not a trade bar)
MIN_PRICE = 1.00
MIN_AVG_VOL = 300_000        # tradable liquidity
MIN_SI_PCT = 0.10            # 10% of float — the conventional "high SI" line
MIN_MKTCAP = 50_000_000      # exclude micro-noise where SI data is unreliable
SVR_ELEVATED = 0.62          # short-volume ratio flagged as persistently high
                             # (raw SVR is noisy; this + multi-day persistence
                             # + the yfinance %float confirm keeps it focused)
MAX_FINRA_CANDIDATES = 400   # cap the pre-confirmation pool; SVR alone is a
                             # weak signal, so don't flood the yfinance step


def _svr_candidates_from_finra(min_files: int = 3,
                               svr_thresh: float = SVR_ELEVATED) -> set:
    """Mine already-downloaded FINRA CNMS short-volume files for tickers
    whose short-volume ratio is elevated across MULTIPLE recent days
    (persistence filters out one-day noise). Zero new network calls."""
    files = sorted(glob.glob(os.path.join(FINRA_CACHE, "CNMSshvol*.txt")))
    if len(files) < min_files:
        return set()
    hits = {}
    for path in files[-min_files - 2:]:      # a few most-recent days
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("|")
                    if len(parts) < 5 or parts[0] == "Date":
                        continue
                    sym = parts[1].strip()
                    try:
                        sv = float(parts[2])
                        tv = float(parts[4])
                    except ValueError:
                        continue
                    if tv <= 0:
                        continue
                    if sv / tv >= svr_thresh:
                        hits[sym] = hits.get(sym, 0) + 1
        except OSError:
            continue
    # require the elevated ratio on at least `min_files` distinct days,
    # then keep the most persistent up to the cap (SVR alone is weak —
    # persistence + the later %float confirm do the real filtering)
    qualifying = [(s, c) for s, c in hits.items() if c >= min_files]
    qualifying.sort(key=lambda x: x[1], reverse=True)
    return {s for s, _ in qualifying[:MAX_FINRA_CANDIDATES]}


# A rotating seed of names that recur on high-SI / WSB screens. NOT the
# universe — just a starting candidate pool to confirm via yfinance when
# FINRA mining isn't available. Edit freely; it's only a seed.
SEED_CANDIDATES = [
    "GRPN", "HTZ", "BETR", "SPCE", "MVIS", "GLTO", "LKNCY", "SMMT", "HIMS",
    "BYND", "CVNA", "UPST", "LMND", "AFRM", "W", "SOUN", "SMCI", "MP", "FUBO",
    "KSS", "M", "GPS", "RL", "VFC", "CAR", "WOOF", "OATLY", "SDC", "ME",
    "DNA", "RXRX", "SANA", "WVE", "RNA", "IONS", "SDGR", "ASTS", "RKLB",
    "LUNR", "RDW", "ACHR", "JOBY", "IONQ", "RGTI", "QBTS", "LAZR", "INVZ",
    "VLDR", "ENVX", "FLNC", "LAC", "SGML", "PLL", "LTHM", "MVST", "MTTR",
    "NNOX", "PATH", "AI", "GSAT", "RUM", "DJT", "PENN", "RSI", "GENI", "QMCO",
]


def _confirm_via_yf(tickers: list) -> dict:
    """For each candidate, pull short %float + price + volume; keep only
    those clearing the mechanical bar. Returns {ticker: detail}."""
    import yfinance as yf
    kept = {}
    for t in sorted(set(tickers)):
        try:
            info = yf.Ticker(t).info
            si = info.get("shortPercentOfFloat")
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            vol = info.get("averageVolume") or info.get("averageVolume10days")
            mc = info.get("marketCap")
            if si is None or price is None or vol is None:
                continue
            if (si >= MIN_SI_PCT and price >= MIN_PRICE
                    and vol >= MIN_AVG_VOL and (mc or 0) >= MIN_MKTCAP):
                kept[t] = {"si_pct": round(si, 4), "price": round(price, 2),
                           "avg_vol": int(vol)}
        except Exception:
            continue
    return kept


def refresh(use_finra: bool = True, extra: Optional[list] = None) -> dict:
    """Build the live supplement. Combines FINRA-mined candidates (if
    available) + the seed pool + any `extra`, confirms each via yfinance,
    writes universe_live.json. Returns the written dict."""
    candidates = set(SEED_CANDIDATES)
    if extra:
        candidates |= {t.upper().strip() for t in extra if t.strip()}
    finra_note = "not used"
    if use_finra:
        fin = _svr_candidates_from_finra()
        if fin:
            candidates |= fin
            finra_note = f"{len(fin)} names from finra_cache short-volume"

    detail = _confirm_via_yf(sorted(candidates))
    out = {"generated": datetime.now().isoformat(timespec="seconds"),
           "source": f"seed+extra ({len(candidates)} screened); {finra_note}",
           "tickers": sorted(detail.keys()),
           "detail": detail}
    tmp = LIVE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, LIVE_FILE)
    return out


def load_live_tickers() -> list:
    """Read the live supplement if present and fresh-ish. Safe to call
    from squeeze_universe.get_universe(); returns [] if missing."""
    try:
        with open(LIVE_FILE, encoding="utf-8") as f:
            return list(json.load(f).get("tickers", []))
    except (OSError, ValueError):
        return []


if __name__ == "__main__":
    import sys
    use_finra = "--no-finra" not in sys.argv
    print(f"Refreshing live universe (finra mining: {use_finra})...")
    result = refresh(use_finra=use_finra)
    print(f"\n✅ {len(result['tickers'])} high-SI names confirmed and written "
          f"to universe_live.json")
    print(f"   source: {result['source']}")
    if result["tickers"]:
        print("   sample:", ", ".join(result["tickers"][:15]),
              ("…" if len(result["tickers"]) > 15 else ""))
    print("\n   squeeze_universe.get_universe() will now include these "
          "automatically.")
