"""
cache_doctor.py
===============
Integrity check for the yfinance cache.

THE BUG THIS EXISTS TO PREVENT
------------------------------
yfinance_throttle decided what was safe to cache with:

    json.dumps(obj, default=str)

`default=str` is the opposite of a safety check — it makes EVERY object look
serializable by stringifying whatever JSON cannot encode. So:

  * an option chain (a namedtuple of DataFrames) cached as a JSON ARRAY of
    truncated DataFrame repr strings
  * price history cached as the STRING "Empty DataFrame\\nColumns: [...]"

and on the next call the cache handed back a `list` or a `str` where the
caller expected an object. The visible symptom was one line in the analyzer:

    Options analysis failed: 'list' object has no attribute 'calls'

The invisible symptoms were worse and lasted months: implied_move_pct present
in only 59% of graded rows, price history unavailable for tickers that had a
poisoned entry, and a calibration model fit on the wreckage.

Nothing raised. Nothing logged. The cache simply returned a different TYPE
than the live call, and every caller's broad `except` turned that into
"no data available".

WHAT THIS CHECKS
----------------
  1. The serializability guard actually rejects unserializable objects.
  2. option_chain is never cached (it cannot survive JSON, and it is live
     quotes regardless).
  3. No entry currently on disk holds a stringified object where a structured
     value belongs.

Run it after touching yfinance_throttle, and nightly as a standing guard.

    python cache_doctor.py            # report
    python cache_doctor.py --fix      # report, then purge what it found
"""

import json
import os
import sys

_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(_DIR, "cache", "yfinance_cache.json")

# Attributes whose genuine return value is a DataFrame/Series/namedtuple.
# A cached str or list for any of these is a stringification casualty.
FRAME_ATTRS = {
    "history", "financials", "balance_sheet", "balancesheet", "cashflow",
    "income_stmt", "quarterly_income_stmt", "quarterly_balance_sheet",
    "quarterly_cashflow", "dividends", "splits", "actions", "earnings",
    "quarterly_earnings", "earnings_dates", "get_earnings_dates", "calendar",
    "recommendations", "recommendations_summary", "institutional_holders",
    "major_holders", "mutualfund_holders", "insider_transactions",
    "insider_purchases", "insider_roster_holders", "shares", "option_chain",
}


def check_guard() -> list:
    """The serializability guard must REJECT things JSON cannot represent."""
    problems = []
    try:
        import yfinance_throttle as yt
    except Exception as e:
        return [f"cannot import yfinance_throttle: {e}"]

    class _Opaque:
        def __init__(self):
            self.x = object()

    cases = [
        ({"a": 1, "b": "two"}, True,  "plain dict"),
        ([1, 2, 3],            True,  "list of numbers"),
        ("text",               True,  "string"),
        (_Opaque(),            False, "object with no JSON form"),
        ({"d": _Opaque()},     False, "dict containing an opaque object"),
    ]
    for obj, expected, label in cases:
        got = yt._is_serializable(obj)
        if got != expected:
            problems.append(
                f"_is_serializable({label}) returned {got}, expected "
                f"{expected} — the guard is not guarding")

    if yt._ttl_for("option_chain") is not None:
        problems.append("option_chain has a cache TTL — it cannot survive "
                        "JSON and must never be cached")
    return problems


def check_disk() -> dict:
    """Find entries whose stored value is the wrong SHAPE for their attr."""
    out = {"entries": 0, "corrupt": [], "by_attr": {}}
    if not os.path.exists(CACHE_FILE):
        return out
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        out["unreadable"] = str(e)
        return out
    out["entries"] = len(d)
    for k, e in d.items():
        parts = k.split("::")
        attr = parts[1] if len(parts) > 1 else ""
        v = e.get("v") if isinstance(e, dict) else e
        if attr in FRAME_ATTRS and isinstance(v, (str, list)):
            out["corrupt"].append(k)
            out["by_attr"][attr] = out["by_attr"].get(attr, 0) + 1
    return out


def purge(keys: list) -> int:
    if not keys or not os.path.exists(CACHE_FILE):
        return 0
    with open(CACHE_FILE, encoding="utf-8") as f:
        d = json.load(f)
    n = 0
    for k in keys:
        if k in d:
            del d[k]
            n += 1
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CACHE_FILE)
    return n


def run(fix: bool = False, verbose: bool = True) -> dict:
    guard = check_guard()
    disk = check_disk()
    healthy = not guard and not disk["corrupt"]
    if verbose:
        print("=" * 60)
        print("  CACHE DOCTOR")
        print("=" * 60)
        if guard:
            print("  GUARD FAILURES — the cache can corrupt data again:")
            for g in guard:
                print(f"    - {g}")
        else:
            print("  guard: OK (unserializable objects are rejected, "
                  "option_chain is never cached)")
        n = len(disk["corrupt"])
        if n:
            print(f"  disk: {n} corrupt entr(ies) of {disk['entries']}")
            for a, c in sorted(disk["by_attr"].items(),
                               key=lambda kv: -kv[1]):
                print(f"    {a:<24}{c:>6}")
            if not fix:
                print("  run with --fix to purge them")
        else:
            print(f"  disk: OK ({disk['entries']} entries, none corrupt)")
    if fix and disk["corrupt"]:
        removed = purge(disk["corrupt"])
        if verbose:
            print(f"  purged {removed} entries")
        disk["purged"] = removed
    if verbose:
        print("=" * 60)
    return {"healthy": healthy, "guard_problems": guard,
            "corrupt": len(disk["corrupt"]), "entries": disk["entries"],
            "by_attr": disk["by_attr"]}


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    res = run(fix="--fix" in sys.argv)
    sys.exit(0 if res["healthy"] or "--fix" in sys.argv else 1)
