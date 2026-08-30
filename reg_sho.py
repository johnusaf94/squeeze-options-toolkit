"""
reg_sho.py
==========
The official Reg SHO Threshold Securities List, and how many consecutive
sessions a name has been on it.

WHY THIS EXISTS
---------------
squeeze_deep already reasons about "Reg SHO threshold-security territory" —
but only as a comment describing a percent-of-float band it computed itself:

    elif accum >= 0.005:     # threshold-security territory

That is an estimate of a determination somebody else publishes. The exchange
decides, daily and by name, which securities have fails large and persistent
enough to qualify, and publishes the answer for free.

Why the official list beats the computed proxy:

  * It is a DETERMINATION, not a threshold guess. A security qualifies on
    fails >= 0.5% of shares outstanding AND >= 10,000 shares, for five
    consecutive settlement days. The scanner's version sees only the newest
    SEC fail file, which lags by about two weeks.

  * The consecutive-day count is the actual mechanical clock. Thirteen
    consecutive settlement days on the list forces mandatory close-out of the
    fail position. That is the hard, dated, non-optional buying event the
    T+35 projection in squeeze_deep is trying to approximate — except the
    exchange publishes exactly where each name stands in the count.

  * It is same-day. SEC fail files are published twice a month with a lag;
    this list updates every session.

COVERAGE
--------
Nasdaq publishes one file covering the securities it reports on. Where a
name is absent the honest answer is "not on the list", which is also the
common case — the list is typically a few hundred names out of thousands.
Absence is information; a fetch failure is not, and the two are kept
distinct in the return value.
"""

import json
import os
import ssl
import time
import urllib.request
from datetime import date, datetime, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_DIR, "cache")
CACHE_FILE = os.path.join(CACHE_DIR, "reg_sho_threshold.json")

BASE_URL = "https://www.nasdaqtrader.com/dynamic/symdir/regsho/nasdaqth{}.txt"

# The list changes once per session; a few hours of caching keeps a bulk scan
# of 150 tickers off the network without ever serving a stale session.
CACHE_TTL_S = 6 * 3600
TIMEOUT_S = 20

# Consecutive settlement days after which close-out is mandatory.
MANDATORY_CLOSEOUT_DAYS = 13
# Sessions of history fetched when counting a streak. Enough to see past the
# mandatory threshold and still be cheap.
LOOKBACK_SESSIONS = 20

_ssl_ctx = None
_MEM = {}


def _ctx():
    """certifi-backed SSL — this Python's bundled CA store has an expired
    root, the same defect that silently killed the FINRA feed."""
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


def _fetch_day(d: date):
    """Symbols on the list for one session. None means the fetch failed or
    that session has no file (weekend/holiday) — NOT an empty list."""
    key = d.strftime("%Y%m%d")
    if key in _MEM:
        return _MEM[key]

    cache = _load_cache()
    hit = cache.get(key)
    # A past session's list never changes, so it is cached forever. Only
    # today's is re-checked on the TTL.
    if hit is not None and (d < date.today()
                            or time.time() - hit.get("t", 0) < CACHE_TTL_S):
        syms = set(hit.get("symbols", []))
        _MEM[key] = syms
        return syms

    try:
        req = urllib.request.Request(
            BASE_URL.format(key), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=_ctx()) as r:
            text = r.read().decode("utf-8", "replace")
    except Exception:
        _MEM[key] = None
        return None

    # ── VALIDATE THE RESPONSE IS THE FILE, NOT AN ERROR PAGE ──
    # Weekends, holidays and not-yet-published sessions return an HTML page
    # with status 200 rather than a 404. Parsed naively, that page yields one
    # junk "symbol" and a list of length 1, which then answers "not on the
    # list" for every real ticker — a false negative that looks exactly like
    # a working feed.
    lines = text.splitlines()
    if not lines:
        _MEM[key] = None
        return None
    header = lines[0].strip()
    if "|" not in header or "Symbol" not in header:
        _MEM[key] = None
        return None

    syms = set()
    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) < 4 or not parts[0].strip():
            continue
        sym = parts[0].strip().upper()
        # The trailer line records file creation time, not a security.
        if sym and not sym.startswith("FILE CREATION") and sym.isascii():
            syms.add(sym)
    if not syms:
        _MEM[key] = None
        return None

    cache[key] = {"t": time.time(), "symbols": sorted(syms)}
    # Keep the cache from growing without bound.
    if len(cache) > 120:
        for k in sorted(cache)[:len(cache) - 120]:
            cache.pop(k, None)
    _save_cache(cache)
    _MEM[key] = syms
    return syms


def check(ticker: str, lookback: int = LOOKBACK_SESSIONS) -> dict:
    """Is this security on the threshold list, and for how many sessions?

    Returns a dict that is always safe to read. `available` False means the
    feed could not be reached — distinct from `on_list` False, which is a
    real answer.
    """
    out = {"ticker": (ticker or "").upper().strip(),
           "on_list": False, "consecutive_days": 0, "sessions_checked": 0,
           "days_to_mandatory": None, "mandatory_closeout": False,
           "latest_session": None, "available": False, "note": ""}
    tk = out["ticker"]
    if not tk:
        return out

    # Walk back from today over calendar days, keeping only sessions that
    # actually produced a file.
    sessions = []
    d = date.today()
    misses = 0
    while len(sessions) < lookback and misses < 12:
        syms = _fetch_day(d)
        if syms is None:
            misses += 1
        else:
            sessions.append((d, tk in syms))
        d -= timedelta(days=1)

    if not sessions:
        out["note"] = "threshold list unreachable — no determination"
        return out

    out["available"] = True
    out["sessions_checked"] = len(sessions)
    out["latest_session"] = sessions[0][0].isoformat()
    out["on_list"] = sessions[0][1]

    streak = 0
    for _, present in sessions:
        if not present:
            break
        streak += 1
    out["consecutive_days"] = streak

    if streak:
        out["days_to_mandatory"] = max(MANDATORY_CLOSEOUT_DAYS - streak, 0)
        out["mandatory_closeout"] = streak >= MANDATORY_CLOSEOUT_DAYS
        if out["mandatory_closeout"]:
            out["note"] = (
                f"{streak} consecutive sessions on the threshold list — past "
                f"the {MANDATORY_CLOSEOUT_DAYS}-day mark, close-out of the "
                f"fail position is mandatory, not projected")
        else:
            out["note"] = (
                f"{streak} consecutive sessions on the threshold list — "
                f"{out['days_to_mandatory']} more forces mandatory close-out")
        if streak == len(sessions):
            out["note"] += (f" (streak may be longer than the {len(sessions)} "
                            f"sessions checked)")
    else:
        out["note"] = "not on the threshold list"
    return out


if __name__ == "__main__":
    import sys
    for tk in (sys.argv[1:] or ["GME", "TASK", "HTZ"]):
        r = check(tk)
        flag = ("MANDATORY" if r["mandatory_closeout"]
                else "ON LIST" if r["on_list"]
                else "-" if r["available"] else "NO DATA")
        print(f"{tk.upper():<8}{flag:<11}streak {r['consecutive_days']:>2}  "
              f"({r['sessions_checked']} sessions)  {r['note']}")
