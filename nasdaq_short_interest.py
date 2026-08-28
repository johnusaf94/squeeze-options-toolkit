"""
nasdaq_short_interest.py
========================
Official bi-monthly short interest, straight from the exchange.

WHY THIS EXISTS
---------------
Days-to-cover was being computed as:

    sharesShort (a settlement snapshot, up to 3 weeks old)
    ------------------------------------------------------
    averageVolume10days (a rolling window, updated daily)

That ratio mixes vintages. The numerator is frozen between settlements while
the denominator moves every session, so DTC falls purely because volume rose —
which is precisely what happens when a squeeze starts. Low DTC was therefore
partly a recent-volume-surge detector wearing a short-crowding costume, and
days-to-cover is the single strongest correlate with forward returns in the
graded log (-0.34), pointing the opposite way to how the scanner scores it.

NASDAQ publishes, for every settlement date, the short interest AND the average
daily volume measured over that same settlement period, AND the resulting days
to cover. Contemporaneous numerator and denominator, computed by the exchange.
Free, no key, ~25 settlements of history per symbol.

Measured on TASK (2026-08-26):

    settlement    interest    avgDailyVolume   NASDAQ DTC
    08/14/2026   2,286,112        966,022         2.37
    07/31/2026   4,036,547      1,026,996         3.93
    07/15/2026   1,992,205        516,263         3.86

The local computation gave 4.27 for the same day, from the current settlement's
interest over a 10-day volume average — a denominator roughly half NASDAQ's.

WHAT THIS DOES NOT FIX
----------------------
Short interest is still only published twice a month with a reporting lag. This
makes the ratio internally consistent; it does not make it fresh. A settlement
dated the 14th published on the 26th describes the 14th, and nothing here can
change that.
"""

import json
import os
import ssl
import time
import urllib.request
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_DIR, "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "nasdaq_short_interest.json")

# Short interest changes twice a month. A day of caching is generous and still
# never serves a stale settlement, while keeping this off the network entirely
# during a bulk scan of 150 tickers.
CACHE_TTL_S = 24 * 3600
TIMEOUT_S = 20
MIN_INTERVAL_S = 0.4          # be a polite client

_last_call = [0.0]
_ssl_ctx = None


def _ctx():
    """certifi-backed SSL when available — this Python's bundled CA store has
    an expired root, which is what silently killed the FINRA feed."""
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import certifi
            _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ssl_ctx = False
    return _ssl_ctx or None


def _load_cache() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_cache(c: dict):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def _num(s):
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


FINRA_URL = ("https://api.finra.org/data/group/otcMarket/name/"
             "consolidatedShortInterest")


def _fetch_finra(ticker: str) -> list:
    """FINRA consolidated short interest — the same official settlements, but
    for EVERY listing rather than Nasdaq only.

    The NASDAQ endpoint refuses NYSE names outright ("only supported for
    Nasdaq Listed stocks"), which left about a quarter of this scanner's
    universe on the mixed-vintage fallback. FINRA covers all of them and
    agrees with NASDAQ exactly where both report, so it is tried first."""
    import json as _json
    from datetime import timedelta as _td
    # The API rejects sort directives and returns rows ASCENDING by date, so a
    # wide range plus a row limit silently yields the OLDEST settlements. A
    # request spanning 2024 onwards came back with June 2026 as its newest row
    # while August settlements existed. Anchor the window to the recent past
    # instead, and sort locally.
    _today = datetime.now().date()
    body = _json.dumps({
        "compareFilters": [{"fieldName": "symbolCode",
                            "fieldValue": ticker, "compareType": "EQUAL"}],
        "dateRangeFilters": [{"fieldName": "settlementDate",
                              "startDate": (_today - _td(days=400)).isoformat(),
                              "endDate": (_today + _td(days=30)).isoformat()}],
        "limit": 60,
    }).encode()
    req = urllib.request.Request(FINRA_URL, data=body, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json",
        "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_ctx()) as r:
        rows = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for row in rows or []:
        try:
            d = datetime.strptime(row["settlementDate"], "%Y-%m-%d").date()
        except (KeyError, ValueError, TypeError):
            continue
        interest = _num(row.get("currentShortPositionQuantity"))
        vol = _num(row.get("averageDailyVolumeQuantity"))
        dtc = _num(row.get("daysToCoverQuantity"))
        if interest is None:
            continue
        if dtc is None and vol:
            dtc = interest / vol if vol > 0 else None
        out.append({"settlement": d, "interest": interest,
                    "avg_volume": vol, "dtc": dtc})
    out.sort(key=lambda x: x["settlement"], reverse=True)
    return out


def fetch(ticker: str, use_cache: bool = True) -> list:
    """-> [{settlement (date), interest, avg_volume, dtc}] newest first, or [].

    Never raises: a short-interest lookup failing must not take down an
    analysis that can still run on yfinance's fields."""
    ticker = (ticker or "").upper().strip()
    if not ticker:
        return []
    cache = _load_cache()
    hit = cache.get(ticker)
    if use_cache and hit and (time.time() - hit.get("t", 0)) < CACHE_TTL_S:
        return _from_cache(hit)

    wait = MIN_INTERVAL_S - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()

    # FINRA first (all listings), NASDAQ second (Nasdaq only, same numbers).
    try:
        parsed = _fetch_finra(ticker)
        if parsed:
            cache[ticker] = {"t": time.time(), "parsed":
                             [{**p, "settlement": p["settlement"].isoformat()}
                              for p in parsed]}
            _save_cache(cache)
            return parsed
    except Exception:
        pass

    url = (f"https://api.nasdaq.com/api/quote/{ticker}/short-interest"
           f"?assetClass=stocks")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S,
                                    context=_ctx()) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        # Serve a stale cache rather than nothing — an old settlement is
        # still the right settlement for its own date.
        return _from_cache(hit) if hit else []

    rows = (((payload.get("data") or {}).get("shortInterestTable") or {})
            .get("rows")) or []
    if rows:
        cache[ticker] = {"t": time.time(), "rows": rows}
        _save_cache(cache)
    return _parse(rows)


def _from_cache(hit: dict) -> list:
    """Cache entries hold either raw NASDAQ rows or already-parsed FINRA
    records; both must come back in the same shape."""
    if hit.get("parsed"):
        out = []
        for p in hit["parsed"]:
            try:
                out.append({**p, "settlement":
                            datetime.strptime(p["settlement"], "%Y-%m-%d").date()})
            except (ValueError, TypeError):
                continue
        return out
    return _parse(hit.get("rows") or [])


def _parse(rows: list) -> list:
    out = []
    for r in rows:
        try:
            d = datetime.strptime(r["settlementDate"], "%m/%d/%Y").date()
        except (KeyError, ValueError, TypeError):
            continue
        interest = _num(r.get("interest"))
        vol = _num(r.get("avgDailyShareVolume"))
        dtc = _num(r.get("daysToCover"))
        if interest is None:
            continue
        # Trust the exchange's own ratio; recompute only if it is absent.
        if dtc is None and vol:
            dtc = interest / vol if vol > 0 else None
        out.append({"settlement": d, "interest": interest,
                    "avg_volume": vol, "dtc": dtc})
    out.sort(key=lambda x: x["settlement"], reverse=True)
    return out


def latest(ticker: str) -> dict:
    """Most recent settlement, plus how old it is and the change since the
    prior one. Empty dict when unavailable."""
    rows = fetch(ticker)
    if not rows:
        return {}
    cur = dict(rows[0])
    cur["age_days"] = (datetime.now().date() - cur["settlement"]).days
    if len(rows) > 1:
        prev = rows[1]
        cur["prev_interest"] = prev["interest"]
        cur["prev_settlement"] = prev["settlement"]
        if prev["interest"]:
            cur["interest_change_pct"] = (cur["interest"] / prev["interest"]
                                          - 1.0)
        cur["prev_dtc"] = prev["dtc"]
    cur["history"] = rows
    return cur


if __name__ == "__main__":
    import sys
    for tk in (sys.argv[1:] or ["TASK", "GME"]):
        info = latest(tk)
        if not info:
            print(f"{tk}: no NASDAQ short-interest data")
            continue
        print(f"\n{tk.upper()}  settlement {info['settlement']} "
              f"({info['age_days']}d old)")
        print(f"  short interest   {info['interest']:>14,.0f}"
              + (f"   ({info['interest_change_pct']:+.1%} vs "
                 f"{info['prev_settlement']})"
                 if info.get("interest_change_pct") is not None else ""))
        print(f"  avg daily volume {info['avg_volume']:>14,.0f}"
              if info.get("avg_volume") else "")
        print(f"  days to cover    {info['dtc']:>14.2f}   (exchange-computed,"
              f" contemporaneous volume)")
        print(f"  settlements held {len(info['history'])}")
