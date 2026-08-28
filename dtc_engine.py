"""
dtc_engine.py
=============
Days-to-cover measured against an explicit, named volume denominator — and
short-interest trends measured on the cadence short interest actually has.

WHY THIS EXISTS
---------------
Two separate defects, both found by measurement rather than reasoning.

DEFECT 1 — DTC IS A VOLUME STATISTIC WEARING A SHORT-CROWDING COSTUME
Days to cover is short interest over average daily volume. The numerator moves
twice a month. The denominator is a ten-day mean of a heavy-tailed
distribution, so it can double on one spike day. Measured on the exchange's own
published numbers:

    HTZ   settlement   short interest   exchange ADV   exchange DTC
          2026-06-15      54,395,315      5,236,672       10.39
          2026-08-14     110,061,505     88,658,772        1.24

Short interest DOUBLED and DTC fell by a factor of eight. Every bit of that
move came from the denominator. A "DTC collapsing" read on that name is a
volume-surge detector, and volume surges are what squeezes look like — so the
metric points backwards at the exact moment it matters most.

We also established, by replication, what the exchange's denominator IS. Our
ten-trading-day mean ending at the settlement date reproduces the published
`averageDailyVolume` to within ~1% on every settlement tested, and to five
significant figures on GME. That is worth knowing: it means a disagreement
with any other data vendor is a disagreement about WINDOW CHOICE, not about
facts, and the fix is to name the window rather than to hunt for a bug.

    GME 2026-08-14   exchange (10d mean)  DTC  5.31
                     10d MEDIAN           DTC  8.49
                     60d median           DTC 11.94

All three are arithmetically correct. They answer different questions. A
vendor showing ~12 for GME is not wrong and neither is the exchange — the
vendor is using a longer horizon. This module reports the family and names
each member, so a comparison is apples to apples instead of a mystery.

DEFECT 2 — "SHORT COVERING" WAS BEING READ OFF STALE SNAPSHOTS
The conviction matrix derived its SI trend by diffing today's short interest
against a snapshot from seven days ago. Short interest is published twice a
month with a reporting lag, so most of those pairs are the SAME settlement:

    consecutive SI snapshot pairs   1026
    identical (no new settlement)    758   73.9%

Three quarters of the comparisons were structurally incapable of showing a
change. The remaining quarter fired on the day a new settlement landed — and
then attributed up to fifteen days of position change to a "one week" trend.
RH held one SI value to fourteen decimal places across seven consecutive
snapshots; JACK stepped -5.4 points in a single day.

The exchange publishes ~26 settlements of history per symbol. Trends belong on
that series, where both ends are real observations of the same quantity, and
"covering" means what a data vendor means by it: this settlement versus the
last one.

WHAT THIS DOES NOT FIX
----------------------
Short interest is still bi-monthly and still lagged. Nothing here makes it
fresh. It makes the ratio honest about its own denominator and the trend
honest about its own cadence.
"""

from typing import Optional
import statistics as _st

# Volume windows offered, in trading days. 10 reproduces the exchange; 20 is a
# month; 60 is the long horizon where the statistic stops being dominated by
# single sessions.
WINDOWS = (10, 20, 60)

# mean/median ratio above which a window is called spike-contaminated. At 1.25
# the mean is being carried 25% above the typical session by outliers, which
# is enough to move DTC by a full point on most names.
SPIKE_RATIO_FLAG = 1.25

# Settlement-over-settlement short interest change treated as a real move
# rather than noise.
COVER_THRESHOLD = 0.05      # +/-5% of the prior settlement's short interest


def _clean(vals) -> list:
    out = []
    for v in vals:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            out.append(f)
    return out


def volume_windows(volumes, asof=None) -> dict:
    """Mean AND median daily volume over each window ending at `asof`.

    Both statistics are returned for every window because their disagreement
    is itself the diagnostic: where they diverge, the mean is being set by
    sessions that are not representative of the liquidity a coverer would
    actually find.

    `volumes` is an iterable of (date, volume) pairs or a pandas Series with a
    date-like index. `asof` (a date) truncates to sessions at or before it;
    None means use everything.
    """
    out = {'asof': asof, 'n_sessions': 0}

    pairs = []
    try:
        items = volumes.items()          # pandas Series
    except AttributeError:
        items = volumes
    for d, v in items:
        try:
            dd = d.date() if hasattr(d, 'date') else d
        except Exception:
            dd = d
        pairs.append((dd, v))

    if asof is not None:
        pairs = [(d, v) for d, v in pairs if d is not None and d <= asof]
    series = _clean(v for _, v in pairs)
    out['n_sessions'] = len(series)
    if not series:
        return out

    for w in WINDOWS:
        chunk = series[-w:]
        if len(chunk) < max(3, w // 2):
            # Too few sessions to call it a w-day average. Say nothing rather
            # than quietly average five days and label it sixty.
            out[f'mean{w}'] = None
            out[f'median{w}'] = None
            out[f'n{w}'] = len(chunk)
            continue
        out[f'mean{w}'] = _st.mean(chunk)
        out[f'median{w}'] = _st.median(chunk)
        out[f'n{w}'] = len(chunk)

    m10, md10 = out.get('mean10'), out.get('median10')
    out['spike_ratio'] = (m10 / md10) if (m10 and md10) else None
    out['spike_contaminated'] = bool(out['spike_ratio']
                                     and out['spike_ratio'] >= SPIKE_RATIO_FLAG)
    return out


def dtc_panel(short_interest: Optional[float], volumes,
              settlement=None, exchange_dtc: Optional[float] = None,
              exchange_adv: Optional[float] = None) -> dict:
    """The family of days-to-cover figures, each with its denominator named.

    There is no single correct DTC. There is a correct DTC *for a stated
    volume window*, and this returns them side by side so a disagreement with
    any other source can be resolved by matching windows instead of guessing.
    """
    out = {
        'short_interest':  short_interest,
        'settlement':      settlement,
        'exchange':        exchange_dtc,
        'exchange_adv':    exchange_adv,
        'replica_10d_mean': None,   # our reproduction of the exchange figure
        'replica_error':   None,    # how closely we match it
        'robust_10d_median': None,
        'horizon_20d_median': None,
        'horizon_60d_median': None,
        'spread_low':      None,
        'spread_high':     None,
        'spike_ratio':     None,
        'spike_contaminated': False,
        'preferred':       None,
        'preferred_basis': '',
        'notes':           [],
    }
    if not short_interest or short_interest <= 0:
        out['notes'].append("No short interest — no days-to-cover")
        return out

    vw = volume_windows(volumes, asof=settlement)
    out['volume_windows'] = vw
    out['spike_ratio'] = vw.get('spike_ratio')
    out['spike_contaminated'] = vw.get('spike_contaminated', False)

    def _d(v):
        return (short_interest / v) if v else None

    out['replica_10d_mean']    = _d(vw.get('mean10'))
    out['robust_10d_median']   = _d(vw.get('median10'))
    out['horizon_20d_median']  = _d(vw.get('median20'))
    out['horizon_60d_median']  = _d(vw.get('median60'))

    # Replication check against the exchange's published ADV. This is the
    # line that turns "we disagree with a vendor" from a mystery into a
    # known window difference — if we can reproduce the exchange exactly,
    # our arithmetic is not the problem.
    if exchange_adv and vw.get('mean10'):
        out['replica_error'] = vw['mean10'] / exchange_adv - 1.0
        if abs(out['replica_error']) <= 0.05:
            out['notes'].append(
                f"Reproduces the exchange ADV to {out['replica_error']:+.1%} "
                f"— the exchange denominator is a 10-session mean, confirmed")
        else:
            out['notes'].append(
                f"10-session mean is {out['replica_error']:+.1%} vs the "
                f"exchange ADV — window or session set differs")

    fam = [v for v in (out['exchange'], out['robust_10d_median'],
                       out['horizon_20d_median'], out['horizon_60d_median'])
           if v]
    if fam:
        out['spread_low'], out['spread_high'] = min(fam), max(fam)

    # ── Which one to lead with ──
    # The exchange figure is authoritative for reconciliation and is what the
    # settlement series is built from, so it stays the headline UNLESS its
    # own window is spike-contaminated, in which case the same-window median
    # answers the same question without the outlier sessions setting the
    # answer.
    if out['spike_contaminated'] and out['robust_10d_median']:
        out['preferred'] = out['robust_10d_median']
        out['preferred_basis'] = '10-session median (exchange window, spike-robust)'
        out['notes'].append(
            f"Volume is spike-contaminated: the 10-session mean sits "
            f"{out['spike_ratio']:.2f}x its median, so the exchange DTC "
            f"understates how long covering would take at a typical "
            f"session's liquidity")
    elif out['exchange']:
        out['preferred'] = out['exchange']
        out['preferred_basis'] = 'exchange (10-session mean, contemporaneous)'
    elif out['robust_10d_median']:
        out['preferred'] = out['robust_10d_median']
        out['preferred_basis'] = '10-session median'
    elif out['horizon_20d_median']:
        out['preferred'] = out['horizon_20d_median']
        out['preferred_basis'] = '20-session median'

    if out['spread_low'] and out['spread_high'] and out['spread_low'] > 0:
        if out['spread_high'] / out['spread_low'] >= 2.0:
            out['notes'].append(
                f"DTC ranges {out['spread_low']:.1f}–{out['spread_high']:.1f} "
                f"depending only on the volume window. Any single figure "
                f"quoted without its denominator is arbitrary within that "
                f"band — match windows before calling a vendor wrong")
    return out


def settlement_trends(history: list) -> dict:
    """Short-interest and DTC trends from the exchange settlement series.

    `history` is the list from nasdaq_short_interest.fetch(): newest first,
    each {settlement, interest, avg_volume, dtc}.

    This is what a data vendor means by "short covering": this settlement
    against the previous one. Both ends are real observations of the same
    published quantity, so a flat reading means the position genuinely did not
    move — not that the script sampled the same stale number twice.
    """
    out = {
        'si_trend':        '',
        'si_change_pct':   None,
        'dtc_trend':       '',
        'dtc_now':         None,
        'dtc_prev':        None,
        'settlement':      None,
        'prev_settlement': None,
        'age_days':        None,
        'n_settlements':   0,
        'consecutive':     0,      # runs of same-direction settlements
        'available':       False,
        # True when DTC moved while short interest did NOT — the move came
        # from the volume denominator, so it says nothing about positioning.
        'dtc_move_is_liquidity': False,
        'volume_change_pct': None,
        'notes':           [],
    }
    rows = [r for r in (history or []) if r.get('interest')]
    out['n_settlements'] = len(rows)
    if len(rows) < 2:
        out['notes'].append(
            "Fewer than two settlements — no settlement-over-settlement trend")
        return out

    cur, prev = rows[0], rows[1]
    out['available'] = True
    out['settlement'] = cur.get('settlement')
    out['prev_settlement'] = prev.get('settlement')
    try:
        from datetime import datetime as _dt
        out['age_days'] = (_dt.now().date() - cur['settlement']).days
    except Exception:
        pass

    if prev['interest']:
        chg = cur['interest'] / prev['interest'] - 1.0
        out['si_change_pct'] = chg
        if chg >= COVER_THRESHOLD:
            out['si_trend'] = 'ADDING'
        elif chg <= -COVER_THRESHOLD:
            out['si_trend'] = 'COVERING'
        else:
            out['si_trend'] = 'FLAT'

    out['dtc_now'], out['dtc_prev'] = cur.get('dtc'), prev.get('dtc')
    if out['dtc_now'] and out['dtc_prev']:
        d = out['dtc_now'] - out['dtc_prev']
        if d >= 0.5:
            out['dtc_trend'] = 'TIGHTENING'
        elif d <= -0.5:
            out['dtc_trend'] = 'LOOSENING'
        else:
            out['dtc_trend'] = 'FLAT'
        # DTC moved but short interest did not: the move is the denominator.
        # Saying so out loud prevents a volume surge being read as shorts
        # getting trapped or escaping.
        if (out['si_change_pct'] is not None
                and abs(out['si_change_pct']) < COVER_THRESHOLD
                and out['dtc_trend'] != 'FLAT'
                and cur.get('avg_volume') and prev.get('avg_volume')):
            vchg = cur['avg_volume'] / prev['avg_volume'] - 1.0
            out['volume_change_pct'] = vchg
            if abs(vchg) >= 0.25:
                out['dtc_move_is_liquidity'] = True
                out['notes'].append(
                    f"DTC {out['dtc_trend']} on a {vchg:+.0%} volume change "
                    f"while short interest moved {out['si_change_pct']:+.1%} "
                    f"— this is a liquidity move, not a positioning move")

    # Run length: three settlements of covering is a different animal from one.
    if out['si_trend'] in ('ADDING', 'COVERING'):
        want = out['si_trend']
        run = 0
        for a, b in zip(rows, rows[1:]):
            if not b['interest']:
                break
            c = a['interest'] / b['interest'] - 1.0
            d = ('ADDING' if c >= COVER_THRESHOLD
                 else 'COVERING' if c <= -COVER_THRESHOLD else 'FLAT')
            if d != want:
                break
            run += 1
        out['consecutive'] = run
        if run >= 3:
            out['notes'].append(
                f"{run} consecutive settlements of {want.lower()} — sustained, "
                f"not a single-period wobble")
    return out


def for_ticker(ticker: str, history=None, volumes=None) -> dict:
    """Full read for one ticker: DTC family + settlement trends.

    `history` (settlement rows) and `volumes` (daily bars) are accepted so
    callers that already fetched them do not pay for a second round trip.
    """
    out = {'ticker': (ticker or '').upper(), 'panel': {}, 'trends': {}}

    rows = history
    if rows is None:
        try:
            import nasdaq_short_interest as _nsi
            rows = _nsi.fetch(ticker)
        except Exception:
            rows = []
    out['trends'] = settlement_trends(rows)

    if volumes is None:
        try:
            import yfinance as yf
            h = yf.Ticker(ticker).history(period="14mo", interval="1d")
            volumes = h["Volume"] if h is not None and not h.empty else None
        except Exception:
            volumes = None

    if volumes is not None and rows:
        cur = rows[0]
        out['panel'] = dtc_panel(cur.get('interest'), volumes,
                                 settlement=cur.get('settlement'),
                                 exchange_dtc=cur.get('dtc'),
                                 exchange_adv=cur.get('avg_volume'))
    return out


def format_block(res: dict, indent: str = "  ") -> str:
    """Human-readable DTC panel + trend block."""
    L = []
    p, t = res.get('panel') or {}, res.get('trends') or {}

    L.append(f"{indent}DAYS TO COVER — by volume window")
    if not p or not p.get('short_interest'):
        L.append(f"{indent}   no short interest available")
    else:
        L.append(f"{indent}   Short interest:      "
                 f"{p['short_interest']:,.0f}"
                 + (f"  (settlement {p['settlement']})" if p.get('settlement') else ""))

        def _row(label, v, extra=""):
            if v:
                L.append(f"{indent}   {label:<22}{v:>6.2f}{extra}")

        _row("exchange 10d mean", p.get('exchange'),
             f"   ADV {p['exchange_adv']:,.0f}" if p.get('exchange_adv') else "")
        _row("our 10d mean", p.get('replica_10d_mean'),
             f"   ({p['replica_error']:+.1%} vs exchange)"
             if p.get('replica_error') is not None else "")
        _row("10d MEDIAN", p.get('robust_10d_median'), "   spike-robust")
        _row("20d median", p.get('horizon_20d_median'))
        _row("60d median", p.get('horizon_60d_median'), "   long horizon")
        if p.get('spread_low') and p.get('spread_high'):
            L.append(f"{indent}   {'spread':<22}"
                     f"{p['spread_low']:.2f}–{p['spread_high']:.2f}")
        if p.get('spike_ratio'):
            L.append(f"{indent}   {'spike ratio':<22}{p['spike_ratio']:>6.2f}"
                     f"   (10d mean / 10d median)")
        if p.get('preferred'):
            L.append(f"{indent}   -> using {p['preferred']:.2f} on "
                     f"{p['preferred_basis']}")
        for n in p.get('notes', []):
            L.append(f"{indent}   . {n}")

    L.append("")
    L.append(f"{indent}SHORT INTEREST TREND — settlement over settlement")
    if not t.get('available'):
        L.append(f"{indent}   unavailable ({t.get('n_settlements', 0)} settlements)")
    else:
        L.append(f"{indent}   {t['prev_settlement']} -> {t['settlement']}"
                 + (f"  ({t['age_days']}d old)" if t.get('age_days') is not None else ""))
        if t.get('si_change_pct') is not None:
            L.append(f"{indent}   Short interest:      "
                     f"{t['si_change_pct']:+.1%}   {t['si_trend']}")
        if t.get('dtc_now') and t.get('dtc_prev'):
            L.append(f"{indent}   Days to cover:       "
                     f"{t['dtc_prev']:.2f} -> {t['dtc_now']:.2f}   "
                     f"{t['dtc_trend']}")
        if t.get('consecutive', 0) > 1:
            L.append(f"{indent}   Consecutive:         "
                     f"{t['consecutive']} settlements")
        L.append(f"{indent}   History:             {t['n_settlements']} settlements")
        for n in t.get('notes', []):
            L.append(f"{indent}   . {n}")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    import sys
    for tk in (sys.argv[1:] or ["TASK", "HTZ", "GME"]):
        print("\n" + "=" * 64 + f"\n{tk.upper()}\n" + "=" * 64)
        try:
            print(format_block(for_ticker(tk)))
        except Exception as e:
            print(f"  failed: {e}")
