"""
liveness.py
===========
Is this security actually trading, or is the data provider still quoting a
corpse?

WHY THIS EXISTS
---------------
Three tickers — CRKN, SBNY, MDRX — sat in the scan universe with a price that
never moved. CRKN was logged at exactly $0.2500 across 35 separate scans over
two months; SBNY at $0.95 across 18; MDRX at $4.74 across 24. Those are not
prices, they are the last quote before the tape stopped, served forever.

They were 3.6% of graded episodes and they inverted every statistic computed
from the log:

    all episodes            n=702   mean -0.84%   14.8% win   20 disasters
    excluding these three   n=677   mean +1.08%   15.4% win    6 disasters

Two findings that looked solid at p=0.000 and p=0.005 (an imminence gradient
and a catalyst-proximity edge) fell to p=0.072 and p=0.184 once they were
removed. The calibration layer's AUC of 0.423 — *below* a coin flip, i.e.
actively backwards — is consistent with the same cause: a dead ticker shows
extreme short interest, days-to-cover and borrow cost precisely BECAUSE it
stopped trading, so it scores highly, then books a fabricated -99% and
teaches the model that high scores lose.

The cheapest fix in the codebase is to not analyse them in the first place.

WHAT WAS ACTUALLY WRONG (the first diagnosis was half right)
------------------------------------------------------------
The frozen prices did NOT come from frozen tapes. Checked 2026-08-28, all
three still print daily. They came from yfinance's `info` block going stale
while `history` stayed correct:

    SBNY   info currentPrice 0.95    history last close 0.16    5.9x apart
    MDRX   info currentPrice 4.74    history last close 4.81
    CRKN   info currentPrice 0.01785 history last close 0.01785 (since caught up)

fetch_validated_info consulted history only when info was MISSING, never when
it was WRONG, so the stale quote propagated into every downstream number. That
root cause is fixed in data_validator by cross-validating the quote against
the tape; this module handles the separate question of whether the security is
worth analysing at all.

TWO DIFFERENT VERDICTS
----------------------
`alive` is a DATA question: does this tape have prints? A dead tape means
every downstream number is fiction, so nothing should be computed at all.
CRKN qualifies on volume — its last five sessions are 0, 711, 0, 0, 0.

`zombie` is a QUALITY question: the tape is live but days-to-cover has pinned
the 60-day cap, which normalise_short_interest itself describes as "a data
error or zombie stock". Measured over 702 episodes:

    DTC at cap (>=59.9)   n=21    mean -54.30%    0.0% win   11 of 20 disasters
    DTC below cap         n=681   mean  +0.81%   15.3% win

Zero winners in twenty-one episodes. The scanner currently hands that same 60
to the scorer, where it earns MAXIMUM pressure and magnitude points. These are
kept separate because they want different treatment: a dead tape cannot be
analysed, while a zombie can be analysed and should be ranked last.
"""

from typing import Optional

# Sessions inspected when deciding whether a tape is frozen.
FLAT_WINDOW = 10
# Sessions inspected for volume. The test is on the MEDIAN, not the sum: a
# dead name still prints the odd block trade (CRKN's last five sessions are
# 0, 711, 0, 0, 0 — a sum test passes it, a median test does not).
VOLUME_WINDOW = 10
# Median sessions traded below this and there is no tape to exit into.
MIN_MEDIAN_VOLUME = 1000
# Quote/tape divergence beyond this means one of the two is fiction.
MAX_QUOTE_DIVERGENCE = 1.15
# A tape whose newest bar is older than this is not trading. Generous enough
# for a long holiday weekend plus a data-feed hiccup.
STALE_TAPE_DAYS = 7
# Days-to-cover at or above this is the cap set by normalise_short_interest,
# not a measurement.
DTC_CAP = 59.9


def check(validated: dict = None, hist=None,
          days_to_cover: Optional[float] = None,
          asof=None) -> dict:
    """Decide whether a security is trading, and whether it is a zombie.

    Never raises and never blocks on missing data: absence of history is
    unknown, not dead. Only positive evidence of a stopped tape sets
    `alive` False, because refusing to analyse everything with a thin data
    feed would quietly empty the universe.
    """
    out = {'alive': True, 'zombie': False, 'reasons': [], 'checked': False}

    dtc = days_to_cover
    if dtc is None and validated is not None:
        dtc = validated.get('shortRatio')
    try:
        if dtc is not None and float(dtc) >= DTC_CAP:
            out['zombie'] = True
            out['reasons'].append(
                f"days-to-cover {float(dtc):.0f} is the 60 cap, not a "
                f"measurement — 0 winners in 21 graded episodes")
    except (TypeError, ValueError):
        pass

    # ── NO DATA AT ALL is not "unknown", it is absent ──
    # The rule below is "absence of history is unknown, not dead", so a thin
    # feed does not empty the universe. But a symbol with NO price AND NO
    # history is not thin, it is not a symbol: ZZZZZZ returned alive=True with
    # zero fetch errors. It was caught downstream only because its short
    # interest was 0 and failed a threshold — luck, not design.
    if validated is not None:
        try:
            _has_price = validated.get("currentPrice") is not None
            _has_hist = bool(validated.get("_has_history"))
            if not _has_price and not _has_hist:
                out["alive"] = False
                out["reasons"].append(
                    "no price and no history — symbol does not resolve")
                return out
        except Exception:
            pass

    if hist is None:
        return out
    try:
        if hist.empty:
            return out
        closes = [float(c) for c in hist["Close"].tolist() if c == c]
        vols = [float(v) for v in hist["Volume"].tolist() if v == v]
    except Exception:
        return out
    if not closes:
        return out

    out['checked'] = True

    # ── Frozen tape: the quote has not moved at all ──
    win = closes[-FLAT_WINDOW:]
    if len(win) >= 5 and len(set(win)) == 1:
        out['alive'] = False
        out['reasons'].append(
            f"close identical at {win[-1]:g} for {len(win)} sessions — "
            f"quote frozen, security is not trading")

    # ── No liquidity to exit into ──
    vwin = vols[-VOLUME_WINDOW:] if vols else []
    if len(vwin) >= 5:
        import statistics as _st
        med = _st.median(vwin)
        if med < MIN_MEDIAN_VOLUME:
            out['alive'] = False
            out['reasons'].append(
                f"median volume {med:,.0f} over {len(vwin)} sessions — "
                f"no liquidity to enter or exit")

    # ── Quote disagrees with the tape ──
    # Not fatal on its own (data_validator now prefers the tape), but it is
    # the fingerprint of the defect that corrupted the log, so it is always
    # reported.
    if validated is not None:
        try:
            q = validated.get('currentPrice')
            if q and closes[-1] > 0:
                div = max(q / closes[-1], closes[-1] / q)
                if div > MAX_QUOTE_DIVERGENCE:
                    out['reasons'].append(
                        f"quote {q:g} vs last close {closes[-1]:g} "
                        f"({div:.1f}x apart) — stale info block")
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # ── Stale tape: history simply stops ──
    try:
        from datetime import datetime
        last = hist.index[-1].to_pydatetime().replace(tzinfo=None)
        ref = asof or datetime.now()
        gap = (ref - last).days
        if gap > STALE_TAPE_DAYS:
            out['alive'] = False
            out['reasons'].append(
                f"newest bar is {gap} days old ({last.date()}) — "
                f"delisted, halted, or renamed")
    except Exception:
        pass

    return out


def frozen_price_tickers(rows, price_col: str = "price_at_scan",
                         min_scans: int = 5) -> set:
    """Tickers whose logged price never moved across many scans.

    A retrospective version of the same test, for auditing a log that was
    written before this module existed. Used by the regrade path.
    """
    from collections import defaultdict
    seen = defaultdict(set)
    count = defaultdict(int)
    for r in rows:
        t = r.get("ticker")
        if not t:
            continue
        count[t] += 1
        try:
            v = float(r.get(price_col))
        except (TypeError, ValueError):
            continue
        if v > 0:
            seen[t].add(round(v, 4))
    return {t for t, vals in seen.items()
            if len(vals) == 1 and count[t] >= min_scans}


if __name__ == "__main__":
    import sys
    import yfinance as yf
    for tk in (sys.argv[1:] or ["CRKN", "SBNY", "MDRX", "GME", "TASK"]):
        try:
            h = yf.Ticker(tk).history(period="3mo", interval="1d")
            r = check(hist=h)
            state = ("DEAD" if not r['alive'] else
                     "zombie" if r['zombie'] else "alive")
            print(f"{tk:<8} {state:<8} {'; '.join(r['reasons']) or 'trading normally'}")
        except Exception as e:
            print(f"{tk:<8} error: {e}")
