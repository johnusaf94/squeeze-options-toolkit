"""
regrade.py
==========
Clear outcomes that were graded by the broken price logic, so review_outcomes
can refill them correctly.

WHY
---
Outcomes in squeeze_log.csv were filled by a grader with two defects, both
fixed in review_outcomes.py but not retroactive:

  1. `p0` came from `price_at_scan` (raw quote, logged at scan time) while
     `pn` came from yfinance history (split-adjusted, rewritten by every
     corporate action). Any split inside the window produced a return that
     was pure artifact.

  2. `_price_on_or_after` fell back to the last close in the frame whenever
     the target date ran past the end of the tape, so a delisted or halted
     name was graded against its final tick no matter how stale.

On top of that, yfinance's `info` block goes stale for thin names while
history stays correct, so `price_at_scan` itself was frozen for some tickers
(SBNY logged at 0.95 for 18 consecutive scans while the stock traded at 0.16).

Measured effect of the three worst offenders on the graded log:

    all episodes            n=702   mean -0.84%   14.8% win   20 disasters
    excluding CRKN/SBNY/MDRX n=677  mean +1.08%   15.4% win    6 disasters

Two findings that looked solid at p=0.000 and p=0.005 collapsed to p=0.072
and p=0.184 once they were removed. Everything in learned_params.json rests
on the contaminated version.

WHAT THIS DOES
--------------
Clears the outcome columns (price_*, return_*, outcome_checked) on suspect
rows ONLY. Scan-time columns are never touched — they are the historical
record of what the scanner saw and must not be rewritten. review_outcomes
then refills the cleared cells with the corrected logic, and rows it cannot
grade honestly stay empty.

DEFAULT IS A DRY RUN. Nothing is written without --apply, and --apply always
takes a timestamped backup first.

    python regrade.py                # show what would change
    python regrade.py --apply        # back up, then clear
    python regrade.py --apply --then-grade   # ...and refill immediately
"""

import csv
import os
import shutil
import sys
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(_DIR, "squeeze_log.csv")

OUTCOME_COLS = ["price_5d", "return_5d", "price_10d", "return_10d",
                "price_20d", "return_20d", "outcome_checked"]

# A return this extreme over 10 sessions is far more likely a corporate
# action or a dead tape than a real move. Flagged for regrade rather than
# deleted outright: the refill decides, and refuses when it cannot be sure.
EXTREME_RETURN = 0.60


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def find_suspect(rows) -> dict:
    """Classify rows needing a regrade. Returns {reason: [row_index, ...]}."""
    import liveness

    suspect = {}

    def mark(reason, idx):
        suspect.setdefault(reason, []).append(idx)

    frozen = liveness.frozen_price_tickers(rows)
    for i, r in enumerate(rows):
        if not r.get("outcome_checked"):
            continue                      # never graded; nothing to clear

        if r.get("ticker") in frozen:
            mark("frozen scan price (stale info block)", i)
            continue

        # NaN written as a literal string. A bar can exist with no print;
        # the NaN propagated through the return arithmetic and landed in the
        # log, where it poisons every statistic computed from the column.
        if any("nan" in str(r.get(c, "")).lower() for c in OUTCOME_COLS):
            mark("NaN price (bar with no print)", i)
            continue

        # Screen EVERY horizon, not just 10d. A CRKN row showed +5850% on
        # return_5d (0.0003 -> 0.0179, arithmetically real but quoted in
        # hundredths of a cent) while its return_10d sat inside the old
        # 10d-only screen and survived the last pass.
        hit = None
        for _h in (5, 10, 20):
            _v = _f(r.get(f"return_{_h}d"))
            if _v is not None and abs(_v) >= EXTREME_RETURN:
                hit = _h
                break
        if hit:
            mark(f"|return_{hit}d| >= {EXTREME_RETURN:.0%} "
                 f"(corporate action or dead tape)", i)
            continue

        # A 5d/10d/20d triple that is identical to the cent means the same
        # price was served for all three horizons — the last-close fallback.
        p5, p10, p20 = (_f(r.get("price_5d")), _f(r.get("price_10d")),
                        _f(r.get("price_20d")))
        if p5 and p10 and p20 and p5 == p10 == p20:
            mark("identical 5d/10d/20d price (stale last-close fallback)", i)

    return suspect


def normalize(rows) -> dict:
    """Bring already-written rows onto the current conventions.

    None of this is a backfill — every value here is derived from data
    already in the same row, so there is no lookahead and no new source.

      * DTC family capped at 60 to match the `dtc` column, which has always
        been capped there as "a data error or zombie stock". Uncapped, the
        newer columns reached 371 days on near-zero-volume tapes.
      * spike ratio capped at 20 (one row held 566).
      * price_at_scan / market_cap of exactly 0 blanked. A zero price is a
        missing price; written as 0 it is indistinguishable from a real
        measurement, and _completeness already treats zero as missing.
      * feature_completeness computed for rows written before that column
        existed. It is a pure function of QUALITY_FIELDS in the same row.
    """
    import squeeze_logger as SL
    counts = {"dtc_capped": 0, "spike_capped": 0, "zeroed_blank": 0,
              "completeness_filled": 0}

    for r in rows:
        for c, cap in (("dtc_robust", 60.0), ("dtc_60d", 60.0),
                       ("dtc_exchange", 60.0)):
            v = _f(r.get(c))
            if v is not None and v > cap:
                r[c] = round(cap, 3)
                counts["dtc_capped"] += 1
        v = _f(r.get("dtc_spike_ratio"))
        if v is not None and v > 20.0:
            r["dtc_spike_ratio"] = 20.0
            counts["spike_capped"] += 1
        for c in ("price_at_scan", "market_cap"):
            v = _f(r.get(c))
            if v is not None and v == 0:
                r[c] = ""
                counts["zeroed_blank"] += 1
        if not str(r.get("feature_completeness", "")).strip():
            r["feature_completeness"] = SL._completeness(r)
            counts["completeness_filled"] += 1
    return counts


def main():
    apply = "--apply" in sys.argv
    then_grade = "--then-grade" in sys.argv
    do_norm = "--normalize" in sys.argv

    if not os.path.exists(LOG_FILE):
        print(f"No {LOG_FILE}")
        return 1

    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        cols = reader.fieldnames
        rows = list(reader)

    graded = sum(1 for r in rows if r.get("outcome_checked"))
    suspect = find_suspect(rows)
    flagged = sorted({i for v in suspect.values() for i in v})

    print(f"rows {len(rows)}   graded {graded}   flagged for regrade "
          f"{len(flagged)} ({len(flagged)/max(graded,1)*100:.1f}% of graded)\n")

    for reason, idxs in sorted(suspect.items(), key=lambda kv: -len(kv[1])):
        tickers = sorted({rows[i]["ticker"] for i in idxs})
        shown = ", ".join(tickers[:8]) + ("..." if len(tickers) > 8 else "")
        print(f"  {len(idxs):>4} rows  {reason}")
        print(f"        {len(tickers)} tickers: {shown}")

    if flagged:
        print(f"\n  sample of what will be cleared:")
        print(f"  {'ticker':<8}{'scan':<12}{'p_scan':>10}{'p_10d':>10}{'ret10d':>10}")
        for i in flagged[:8]:
            r = rows[i]
            print(f"  {r['ticker']:<8}{(r.get('scan_timestamp') or '')[:10]:<12}"
                  f"{_f(r.get('price_at_scan')) or 0:>10.4f}"
                  f"{_f(r.get('price_10d')) or 0:>10.4f}"
                  f"{(_f(r.get('return_10d')) or 0)*100:>9.1f}%")

    _norm_counts = {}
    if do_norm:
        _norm_counts = normalize(rows)
        print("\n  normalize (each value derived from its own row — no new "
              "source, no lookahead):")
        for k, v in _norm_counts.items():
            print(f"    {k:<26}{v:>6}")

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    if not flagged and not any(_norm_counts.values()):
        print("\nNothing to do.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{LOG_FILE}.pre_regrade_{stamp}"
    shutil.copy2(LOG_FILE, backup)
    print(f"\nbacked up -> {os.path.basename(backup)}")

    for i in flagged:
        for c in OUTCOME_COLS:
            if c in rows[i]:
                rows[i][c] = ""

    tmp = LOG_FILE + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})
    os.replace(tmp, LOG_FILE)
    print(f"cleared outcomes on {len(flagged)} rows "
          f"(scan-time columns untouched)")
    if _norm_counts:
        print(f"normalized: {_norm_counts}")

    if then_grade:
        print("\nrefilling with the corrected grader...\n")
        import review_outcomes
        review_outcomes.fill_outcomes()
    else:
        print("\nnow run:  python review_outcomes.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
