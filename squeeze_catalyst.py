"""
squeeze_catalyst.py
====================
Catalyst timing overlay. The single most important upgrade based on graded
outcomes: the setups that worked were surfaced WITH a catalyst a few days
out. The scanner previously had no concept of WHEN a catalyst was — so it
surfaced the same good names both at the perfect moment and the worst
moment with equal confidence.

This module answers: "is there a known catalyst within striking distance?"

Free data only:
  - Earnings dates       : yfinance .calendar / .get_earnings_dates()
  - SEC 13F window        : quarterly, deterministic (45 days after qtr end)
  - (FDA PDUFA dates are not in a free structured feed — flagged as a
     known gap; user can hand-enter known biotech dates if desired.)

A catalyst INSIDE the actionable window (default 2-15 trading days out)
is the sweet spot: enough time to position, close enough to ignite.
Too far = dead money. Already passed = fuel spent (the exact failure
mode that wrecked the last scan).
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, date
from typing import Optional
import os
import json
import time

# yfinance imported lazily inside _fetch_next_earnings — the manual-catalyst
# CLI (add/list/remove) and cache paths work without it installed, and a
# broken yfinance install can't take down the whole module.
yf = None


def _get_yf():
    global yf
    if yf is None:
        import yfinance as _yf
        yf = _yf
    return yf

# ── PERSISTENT CACHE (restored June 2026 — was lost to version skew) ──
# Earnings dates don't change minute-to-minute. 24h disk cache prevents
# yfinance rate-limiting; negative results cached too.
_CATALYST_CACHE_FILE = "catalyst_cache.json"
_CATALYST_CACHE_TTL_HOURS = 24
_catalyst_cache = None


def _load_catalyst_cache() -> dict:
    global _catalyst_cache
    if _catalyst_cache is not None:
        return _catalyst_cache
    if not os.path.exists(_CATALYST_CACHE_FILE):
        _catalyst_cache = {}
        return _catalyst_cache
    try:
        with open(_CATALYST_CACHE_FILE, "r", encoding="utf-8") as f:
            _catalyst_cache = json.load(f)
        if not isinstance(_catalyst_cache, dict):
            _catalyst_cache = {}
    except Exception:
        _catalyst_cache = {}
    return _catalyst_cache


def _save_catalyst_cache():
    if _catalyst_cache is None:
        return
    try:
        tmp = f"{_CATALYST_CACHE_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_catalyst_cache, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _CATALYST_CACHE_FILE)
    except Exception:
        pass


def _cache_lookup(ticker: str):
    cache = _load_catalyst_cache()
    entry = cache.get(ticker.upper())
    if not entry:
        return None
    try:
        cached_at = datetime.fromisoformat(entry["cached_at"])
        if datetime.now() - cached_at > timedelta(hours=_CATALYST_CACHE_TTL_HOURS):
            return None
        iso = entry.get("iso")
        if iso is None:
            return (None, None, entry.get("confirmed", False))
        d = datetime.strptime(iso, "%Y-%m-%d")
        # Calendar-day difference (not timedelta.days truncation)
        return (iso, (d.date() - datetime.now().date()).days,
                entry.get("confirmed", False))
    except Exception:
        return None


def _cache_store(ticker: str, iso, days_out, confirmed):
    cache = _load_catalyst_cache()
    cache[ticker.upper()] = {
        "iso": iso,
        "confirmed": bool(confirmed),
        "cached_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_catalyst_cache()


# ── MANUAL CATALYST FILE ──
# Free data covers ONLY earnings. FDA PDUFA dates, Phase 2/3 readouts,
# conference presentations, lockups etc. have no free structured feed —
# they live in press releases. This file lets the user hand-enter known
# events; the scanner merges them with earnings and the BEST catalyst of
# any type drives the timing window. Format:
#   { "TGTX": [ {"date": "2026-07-15", "type": "READOUT",
#                "note": "ENHANCE Phase 3 topline"} ] }
# Types: FDA / READOUT / CONFERENCE / LOCKUP / LEGAL / MACRO / OTHER
MANUAL_CATALYST_FILE = "manual_catalysts.json"


def _load_manual_catalysts() -> dict:
    """User-entered events. Returns {} on any problem — a malformed
    file must never break a scan."""
    if not os.path.exists(MANUAL_CATALYST_FILE):
        return {}
    try:
        with open(MANUAL_CATALYST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_manual_catalysts(data: dict):
    tmp = f"{MANUAL_CATALYST_FILE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, MANUAL_CATALYST_FILE)


def _manual_events_for(ticker: str) -> list:
    """Validated events: list of (iso, days_out_signed, type, note)."""
    out = []
    today = datetime.now().date()
    for e in _load_manual_catalysts().get(ticker.upper(), []):
        try:
            d = datetime.strptime(str(e.get("date", "")), "%Y-%m-%d").date()
            out.append((d.strftime("%Y-%m-%d"), (d - today).days,
                        str(e.get("type", "OTHER")).upper(),
                        str(e.get("note", ""))))
        except Exception:
            continue
    return out


# ─────────────────────────────────────────────
# RESULT
# ─────────────────────────────────────────────

@dataclass
class CatalystInfo:
    ticker:              str = ""
    next_earnings:       Optional[str] = None      # ISO date string
    days_to_earnings:    Optional[int] = None      # calendar days (signed)
    earnings_confirmed:  bool = False              # vs estimated
    in_13f_window:       bool = False              # 13F filing season active
    catalyst_type:       str = ""                  # EARNINGS / 13F / NONE
    catalyst_window:     str = ""                  # SWEET_SPOT / TOO_FAR /
                                                   # PASSED / IMMINENT / NONE
    catalyst_score:      float = 0.0               # 0-100 timing quality
    catalyst_note:       str = ""
    catalyst_type_hint:  str = "EARNINGS"          # EARNINGS/FDA/READOUT/...
    catalyst_note_extra: str = ""                  # manual entry note
    warnings:            list = field(default_factory=list)


# ─────────────────────────────────────────────
# 13F WINDOW (deterministic — no fetch needed)
# ─────────────────────────────────────────────

def _in_13f_window(today: date = None) -> bool:
    """
    13F filings are due 45 days after each calendar quarter end.
    The ~2 weeks BEFORE that deadline is when institutional position
    changes get disclosed — a known catalyst window for squeeze names
    (a big new 13F holder can spark momentum).

    Quarter ends: Mar 31, Jun 30, Sep 30, Dec 31
    Deadlines:    ~May 15, Aug 14, Nov 14, Feb 14
    Window: the 18 days leading up to and including each deadline.
    """
    if today is None:
        today = datetime.now().date()
    deadlines = [
        date(today.year, 5, 15),
        date(today.year, 8, 14),
        date(today.year, 11, 14),
        date(today.year, 2, 14),
    ]
    for dl in deadlines:
        window_start = dl - timedelta(days=18)
        if window_start <= today <= dl:
            return True
    return False


# ─────────────────────────────────────────────
# EARNINGS DATE (free via yfinance)
# ─────────────────────────────────────────────

def _fetch_next_earnings(ticker: str):
    """
    Returns (iso_date_str, days_out_signed, confirmed_bool) or (None,None,False).
    days_out is signed: positive = future, negative = already happened.
    """
    # Cache first — no network, no throttle risk
    cached = _cache_lookup(ticker)
    if cached is not None:
        return cached
    time.sleep(0.15)   # polite delay reduces throttle risk

    try:
        t = _get_yf().Ticker(ticker)

        # Preferred: explicit earnings dates frame
        try:
            edf = t.get_earnings_dates(limit=8)
            if edf is not None and not edf.empty:
                now = datetime.now()
                # Index is timestamps; find the nearest FUTURE one,
                # else the most recent past one (to detect "just passed")
                future = [idx for idx in edf.index
                          if idx.to_pydatetime().replace(tzinfo=None) >= now]
                past = [idx for idx in edf.index
                        if idx.to_pydatetime().replace(tzinfo=None) < now]
                if future:
                    nxt = min(future)
                    d = nxt.to_pydatetime().replace(tzinfo=None)
                    iso = d.strftime("%Y-%m-%d")
                    days = (d.date() - now.date()).days   # calendar days
                    _cache_store(ticker, iso, days, True)
                    return (iso, days, True)
                elif past:
                    nxt = max(past)
                    d = nxt.to_pydatetime().replace(tzinfo=None)
                    iso = d.strftime("%Y-%m-%d")
                    days = (d.date() - now.date()).days
                    _cache_store(ticker, iso, days, True)
                    return (iso, days, True)   # negative days
        except Exception:
            pass

        # Fallback: .calendar dict
        try:
            cal = t.calendar
            if cal:
                ed = None
                if isinstance(cal, dict):
                    ed = cal.get("Earnings Date")
                    if isinstance(ed, (list, tuple)) and ed:
                        ed = ed[0]
                if ed is not None:
                    if hasattr(ed, "to_pydatetime"):
                        ed = ed.to_pydatetime()
                    ed = datetime(ed.year, ed.month, ed.day)
                    now = datetime.now()
                    iso = ed.strftime("%Y-%m-%d")
                    days = (ed.date() - now.date()).days
                    _cache_store(ticker, iso, days, False)
                    return (iso, days, False)
        except Exception:
            pass

    except Exception:
        pass
    # Cache the negative result too — stops re-hammering dead tickers
    _cache_store(ticker, None, None, False)
    return (None, None, False)


# ─────────────────────────────────────────────
# CATALYST SCORING
# ─────────────────────────────────────────────

# Mechanical/soft events earn less timing score than binary prints:
# an FTD close-out is real forced buying but not an information event;
# a conference is a stage, not a verdict. 1.0 = full weight.
CATALYST_TYPE_SCORE_WEIGHT = {
    "FTD_CLOSEOUT": 0.80,
    "CONFERENCE":   0.70,
    "MACRO":        0.75,
}

CATALYST_TYPE_LABELS = {
    "EARNINGS":     "Earnings",
    "FDA":          "FDA decision",
    "READOUT":      "Readout",
    "CONFERENCE":   "Conference",
    "LOCKUP":       "Lockup expiry",
    "FTD_CLOSEOUT": "FTD T+35 close-out",
    "LEGAL":        "Legal ruling",
    "MACRO":        "Macro event",
}


def analyze_catalyst(ticker: str,
                       actionable_lo: int = 2,
                       actionable_hi: int = 15,
                       extra_events: list = None) -> CatalystInfo:
    """
    Score the timing quality of the nearest known catalyst.

    The sweet spot (default 2-15 calendar days out) is where the user's
    actual wins lived: enough time to position, close enough to ignite.

      PASSED   (catalyst < 2 days ago to ~ -10): fuel likely spent → heavy penalty
      IMMINENT (0-2 days):  too late to position well, but live
      SWEET_SPOT (2-15d):   optimal — the window the wins came from
      TOO_FAR  (>15d):      dead money risk, mild positive only
      NONE:                 no known scheduled catalyst
    """
    ci = CatalystInfo(ticker=ticker.upper())

    iso, days_out, confirmed = _fetch_next_earnings(ticker)
    ci.in_13f_window = _in_13f_window()

    if iso is not None:
        ci.next_earnings      = iso
        ci.days_to_earnings   = days_out
        ci.earnings_confirmed = confirmed

    # ── MERGE MANUAL CATALYSTS (FDA / readouts / conferences / ...) ──
    # Every known catalyst is ranked by timing quality and the BEST one
    # drives the window: an FDA decision 8 days out beats earnings 60
    # days out; sweet-spot earnings beat a readout that fired last week.
    # Other recently-passed catalysts surface as fuel-spent warnings.
    candidates = []           # (days, iso, type, note)
    if days_out is not None:
        candidates.append((days_out, iso, "EARNINGS", "scheduled earnings"))
    for m_iso, m_days, m_type, m_note in _manual_events_for(ticker):
        if m_days < -12:
            continue          # long past — irrelevant
        candidates.append((m_days, m_iso, m_type, m_note))
    # Programmatic events injected by the deep layer (e.g. FTD T+35
    # close-out projections) join the same pool on equal footing.
    for ev in (extra_events or []):
        try:
            e_iso, e_days, e_type, e_note = ev
            if e_days is not None and e_days >= -12:
                candidates.append((e_days, e_iso,
                                   str(e_type).upper(), str(e_note)))
        except Exception:
            continue

    if candidates:
        def _window_rank(d):
            # SWEET_SPOT > IMMINENT > TOO_FAR (nearer better) > PASSED
            if actionable_lo <= d <= actionable_hi: return (3, -abs(d - 8))
            if 0 <= d < actionable_lo:              return (2, -d)
            if d > actionable_hi:                   return (1, -d)
            return (0, d)
        candidates.sort(key=lambda c: _window_rank(c[0]), reverse=True)
        best = candidates[0]
        for c in candidates[1:]:
            if -12 <= c[0] < 0:
                ci.warnings.append(
                    f"{c[2]} catalyst passed {abs(c[0])}d ago ({c[3]}) — "
                    f"some fuel may be spent")
        days_out = best[0]
        ci.catalyst_type_hint  = best[2]
        ci.catalyst_note_extra = best[3]
        if best[2] != "EARNINGS":
            # Window display tracks the chosen catalyst, not earnings
            ci.days_to_earnings = days_out

    score = 0.0

    if days_out is not None:
        # Use the chosen catalyst's TYPE in classification fields
        chosen_type = ci.catalyst_type_hint or "EARNINGS"
        if days_out < -12:
            # Long past — irrelevant, treat as no catalyst
            ci.catalyst_type   = "NONE"
            ci.catalyst_window = "NONE"
            ci.catalyst_note   = f"Last earnings {abs(days_out)}d ago — stale"
        elif -12 <= days_out < 0:
            # Recently passed — THE failure mode. Fuel spent.
            # NOTE: strictly negative only. The old condition used
            # `< actionable_lo` which swallowed days_out 0 and +1
            # (earnings TODAY/TOMORROW — the future!) and made the
            # IMMINENT branch below unreachable. RH reporting in +1d
            # was tagged PASSED (0.55x penalty) instead of IMMINENT
            # (1.10x) — a ~2x scoring error at the most time-critical
            # moment in the system.
            ci.catalyst_type   = chosen_type
            ci.catalyst_window = "PASSED"
            ci.catalyst_score  = 5.0
            ci.catalyst_note   = (f"⚠️ Earnings {abs(days_out)}d AGO — "
                                  f"catalyst fuel likely spent")
            ci.warnings.append("Catalyst already fired — squeeze fuel spent")
            return ci
        elif actionable_lo <= days_out <= actionable_hi:
            # SWEET SPOT — the window the wins came from
            ci.catalyst_type   = chosen_type
            ci.catalyst_window = "SWEET_SPOT"
            # Peak score in the middle of the window (~7d out)
            mid = (actionable_lo + actionable_hi) / 2
            closeness = 1.0 - abs(days_out - mid) / mid
            score = 70 + 30 * max(0.0, closeness)
            score *= CATALYST_TYPE_SCORE_WEIGHT.get(chosen_type, 1.0)
            ci.catalyst_score = min(score, 100.0)
            label = CATALYST_TYPE_LABELS.get(ci.catalyst_type_hint,
                                              ci.catalyst_type_hint.title())
            extra = f" [{ci.catalyst_note_extra}]" if ci.catalyst_type_hint != "EARNINGS" and ci.catalyst_note_extra else ""
            ci.catalyst_note  = (f"🎯 {label} in {days_out}d — "
                                 f"SWEET SPOT (position-able pre-catalyst){extra}")
        elif 0 <= days_out < actionable_lo:
            ci.catalyst_type   = chosen_type
            ci.catalyst_window = "IMMINENT"
            ci.catalyst_score  = 45.0
            ci.catalyst_note   = (f"Earnings in {days_out}d — IMMINENT "
                                  f"(live but late to position)")
        else:  # days_out > actionable_hi
            ci.catalyst_type   = chosen_type
            ci.catalyst_window = "TOO_FAR"
            # Mild positive that decays with distance
            ci.catalyst_score  = max(5.0, 25.0 - (days_out - actionable_hi))
            ci.catalyst_note   = (f"Earnings in {days_out}d — too far "
                                  f"(dead-money risk until then)")
    else:
        ci.catalyst_type   = "NONE"
        ci.catalyst_window = "NONE"
        ci.catalyst_note   = "No scheduled earnings found"

    # 13F window is a secondary additive catalyst
    if ci.in_13f_window:
        ci.catalyst_score = min(100.0, ci.catalyst_score + 15.0)
        if ci.catalyst_type == "NONE":
            ci.catalyst_type = "13F"
        ci.catalyst_note += "  |  13F filing window active"

    return ci


# ─────────────────────────────────────────────
# CROSS-SECTIONAL PERCENTILE RANKING
# (operates on the whole batch — regime-adaptive)
# ─────────────────────────────────────────────

def apply_percentile_ranks(candidates: list):
    """
    Cross-sectional ranking: instead of absolute thresholds, score each
    signal by where it falls WITHIN today's scan. Regime-adaptive — in a
    weak field nothing scores high; in a hot field the bar rises.

    Adds to each candidate dict:
      pct_<signal>   : 0-100 percentile within this batch
      composite_pct  : blended cross-sectional percentile score

    Mutates candidates in place. Returns the list for chaining.
    """
    if not candidates:
        return candidates

    # Signals where HIGHER = more squeeze-relevant. Getters return None
    # for genuinely MISSING data (not zero) so absent signals are
    # excluded from both the percentile pool and the blend — a name with
    # no CTB data must not collect the ~50th-percentile tie-mass for free.
    signals = {
        "si":          lambda c: c.get("si") if c.get("si") else None,
        "dtc":         lambda c: c.get("dtc") if c.get("dtc") else None,
        "ctb":         lambda c: c.get("ctb") if c.get("ctb") is not None
                                  and c.get("ctb") > 0 else None,
        "combined":    lambda c: c.get("combined", 0) or 0,
        "probability": lambda c: c.get("probability") if c.get("probability")
                                  else None,
        "imminence":   lambda c: c.get("imminence") if c.get("imminence")
                                  else None,
    }

    for sig, getter in signals.items():
        pool = sorted(v for v in (getter(c) for c in candidates)
                      if v is not None)
        np = len(pool)
        for c in candidates:
            v = getter(c)
            if v is None or np == 0:
                c[f"pct_{sig}"] = None
            else:
                below = sum(1 for x in pool if x <= v)
                c[f"pct_{sig}"] = round(100.0 * below / np, 1)

    # ── BLEND REWORK (double-count fix) ──
    # catalyst_score and conviction_mult are REMOVED from this blend:
    # both already enter FINAL as multipliers (combined × conviction ×
    # catalyst). Counting them here too made the table re-rank a second,
    # catalyst-heavier version of FINAL instead of an independent view.
    #
    # composite_pct now answers ONE question — "how strong is this SETUP
    # cross-sectionally, today?" (statics + deep read). FINAL answers
    # "setup × velocity × timing". Two scores, two meanings, no overlap.
    # probability (the deep layer's headline) finally enters the blend.
    w = {
        "pct_probability": 0.20,
        "pct_imminence":   0.20,
        "pct_si":          0.20,
        "pct_ctb":         0.15,
        "pct_combined":    0.15,
        "pct_dtc":         0.10,
    }
    for c in candidates:
        avail = {k: wt for k, wt in w.items() if c.get(k) is not None}
        tot = sum(avail.values())
        if tot > 0:
            c["composite_pct"] = round(
                sum(c[k] * wt for k, wt in avail.items()) / tot, 1)
        else:
            c["composite_pct"] = 0.0
    return candidates


def _cli():
    """Manual catalyst management + single-ticker test.

    Usage:
      python squeeze_catalyst.py TICKER                       analyze one name
      python squeeze_catalyst.py add TICKER DATE TYPE "note"  add manual event
      python squeeze_catalyst.py list                         show all manual events
      python squeeze_catalyst.py remove TICKER [N]            remove Nth (or all)
    Types: FDA / READOUT / CONFERENCE / LOCKUP / LEGAL / MACRO / OTHER
    """
    import sys
    args = sys.argv[1:]

    if args and args[0].lower() == "add":
        if len(args) < 4:
            print('Usage: add TICKER YYYY-MM-DD TYPE "note"')
            return
        ticker, date_s, typ = args[1].upper(), args[2], args[3].upper()
        note = args[4] if len(args) > 4 else ""
        try:
            datetime.strptime(date_s, "%Y-%m-%d")
        except ValueError:
            print(f"❌ Bad date '{date_s}' — use YYYY-MM-DD")
            return
        data = _load_manual_catalysts()
        data.setdefault(ticker, []).append(
            {"date": date_s, "type": typ, "note": note})
        _save_manual_catalysts(data)
        d = datetime.strptime(date_s, "%Y-%m-%d").date()
        days = (d - datetime.now().date()).days
        print(f"✅ {ticker}: {typ} on {date_s} ({days:+d}d) — {note}")
        return

    if args and args[0].lower() == "list":
        data = _load_manual_catalysts()
        if not data:
            print("No manual catalysts. Add with: add TICKER DATE TYPE \"note\"")
            return
        today = datetime.now().date()
        for tk, events in sorted(data.items()):
            for i, e in enumerate(events):
                try:
                    d = datetime.strptime(e["date"], "%Y-%m-%d").date()
                    days = f"{(d - today).days:+d}d"
                except Exception:
                    days = "??"
                stale = "  ⚠ PAST" if days != "??" and int(days[:-1]) < -12 else ""
                print(f"  {tk:<6} [{i}] {e['date']} ({days:>5}) "
                      f"{e.get('type','OTHER'):<10} {e.get('note','')}{stale}")
        return

    if args and args[0].lower() == "remove":
        if len(args) < 2:
            print("Usage: remove TICKER [index]")
            return
        ticker = args[1].upper()
        data = _load_manual_catalysts()
        if ticker not in data:
            print(f"No entries for {ticker}")
            return
        if len(args) > 2:
            idx = int(args[2])
            if 0 <= idx < len(data[ticker]):
                gone = data[ticker].pop(idx)
                if not data[ticker]:
                    del data[ticker]
                print(f"✅ Removed {ticker}[{idx}]: {gone}")
            else:
                print(f"❌ Index {idx} out of range")
        else:
            del data[ticker]
            print(f"✅ Removed all {ticker} entries")
        _save_manual_catalysts(data)
        return

    tk = args[0].upper() if args else "AAPL"
    ci = analyze_catalyst(tk)
    print(f"Catalyst analysis — {tk}")
    print(f"  Chosen catalyst: {ci.catalyst_type_hint}  {ci.catalyst_note_extra}")
    print(f"  Days out:       {ci.days_to_earnings}")
    print(f"  Window:         {ci.catalyst_window}")
    print(f"  Score:          {ci.catalyst_score:.0f}/100")
    print(f"  13F window:     {ci.in_13f_window}")
    print(f"  Note:           {ci.catalyst_note}")
    for w in ci.warnings:
        print(f"  ⚠ {w}")


if __name__ == "__main__":
    _cli()
