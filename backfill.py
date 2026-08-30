"""
backfill.py
===========
Reconstruct the new feature columns for rows logged before those features
existed — but ONLY where they can be reconstructed as of the scan date.

THE LINE THIS DRAWS
-------------------
A feature is backfillable when its source is date-stamped and queryable by
date. It is NOT backfillable when the only thing available is a snapshot of
today.

    BACKFILLABLE                      SOURCE
    reg_sho_days                      one published file per session
    exhaustion_factor, momentum_raw   daily price bars
    dtc_robust / dtc_60d / spike      daily volume + dated settlements

    NOT BACKFILLABLE                  WHY
    effective_float                   heldPercentInstitutions is a snapshot
    inst_shares_over_float            of today only; 13F moves quarterly
    cash_runway_months                balance sheet as-of is not in the feed
    ftd_pct_eff_float                 derives from effective_float

WHY THE SECOND GROUP IS LEFT EMPTY
----------------------------------
Filling a May row with August ownership data is not an approximation, it is
lookahead. If institutional ownership moved because of what happened between
May and August, the model would learn a relationship that could never have
been traded, and it would learn it as a STRONG one — the fabricated feature
would look more predictive than the honest ones and dominate the fit.

A missing column costs nothing: learning_engine's MIN_FEATURE_PRESENCE floor
drops any feature present in under 70% of rows and reports it as dropped by
name. Empty is self-correcting. Wrong is not.

SCORING VERSION
---------------
Rows written before the v2 scorer are stamped scoring_version=1. That is a
statement of fact, not a backfill: those rows really were produced by v1.
Without it the grader averages two different scorers into one number.

    python backfill.py              # dry run — what would be filled
    python backfill.py --apply      # back up, then fill
    python backfill.py --apply --limit 40   # first N tickers only
"""

import csv
import os
import shutil
import sys
import statistics as st
from datetime import datetime, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(_DIR, "squeeze_log.csv")

# Reconstructable as of the scan date.
BACKFILL_COLS = ["reg_sho_days", "reg_sho_mult", "exhaustion_factor",
                 "momentum_raw", "dtc_robust", "dtc_60d", "dtc_spike_ratio",
                 # momentum_score is momentum_raw x exhaustion_factor, and
                 # ret_5d / ret_20d / rel_volume are the price window that
                 # produced both — all pure functions of dated bars.
                 "momentum_score", "ret_5d", "ret_20d", "rel_volume"]

# Deliberately never written by this tool. See module docstring.
FORWARD_ONLY = ["effective_float", "inst_shares_over_float",
                "cash_runway_months", "ftd_pct_eff_float", "float_tightness",
                "ftd_mult", "ftd_pct_float", "ftd_closeout_adv_days"]


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _scoring_cfg():
    try:
        import squeeze_deep
        return squeeze_deep._scoring_cfg()
    except Exception:
        return {"exhaustion_start": 0.20, "exhaustion_full": 0.60,
                "exhaustion_floor": 0.50, "reg_sho_mult_max": 1.20}


def _thrust(cl, vl):
    """Raw 0-100 thrust from a window ending at the scan date."""
    if len(cl) < 8:
        return None, None, None, None
    ret5 = cl[-1] / cl[-6] - 1.0
    ret20 = cl[-1] / cl[0] - 1.0
    relv = None
    if vl:
        avg = sum(vl) / len(vl)
        if avg > 0:
            relv = (sum(vl[-5:]) / 5.0) / avg
    pts = 0.0
    pts += min(max(ret5 / 0.15, 0.0), 1.0) * 50.0
    pts += min(max(ret20 / 0.35, 0.0), 1.0) * 30.0
    if relv is not None:
        pts += min(max((relv - 1.0) / 1.5, 0.0), 1.0) * 20.0
    return round(pts, 1), ret5, ret20, relv


def main():
    apply = "--apply" in sys.argv
    limit = None
    if "--limit" in sys.argv:
        try:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        except (IndexError, ValueError):
            pass

    import yfinance as yf
    import reg_sho
    import nasdaq_short_interest as nsi
    import dtc_engine

    cfg = _scoring_cfg()

    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        cols = rd.fieldnames
        rows = list(rd)

    missing = [c for c in BACKFILL_COLS if c not in cols]
    if missing:
        print(f"log has no column(s) {missing} — run a scan first")
        return 1

    # ── scoring_version stamp (a fact, not a backfill) ──
    stamped = 0
    for r in rows:
        if not str(r.get("scoring_version", "")).strip():
            r["scoring_version"] = "1"
            stamped += 1

    bytick = {}
    for i, r in enumerate(rows):
        if r.get("ticker") and r.get("scan_timestamp"):
            bytick.setdefault(r["ticker"], []).append(i)
    ticks = sorted(bytick)
    if limit:
        ticks = ticks[:limit]

    filled = {c: 0 for c in BACKFILL_COLS}
    touched = 0
    print(f"reconstructing {len(BACKFILL_COLS)} columns across "
          f"{len(ticks)} tickers...\n")

    for n, tk in enumerate(ticks, 1):
        idxs = bytick[tk]
        try:
            h = yf.Ticker(tk).history(period="2y", interval="1d")
            if h is None or h.empty:
                continue
            bars = [(d.to_pydatetime().replace(tzinfo=None),
                     float(c), float(v))
                    for d, c, v in zip(h.index, h["Close"], h["Volume"])]
            bars = [(d, c, v) for d, c, v in bars if c == c and c > 0 and v == v]
        except Exception:
            continue

        try:
            settlements = nsi.fetch(tk)
        except Exception:
            settlements = []

        for i in idxs:
            r = rows[i]
            try:
                sd = datetime.fromisoformat(r["scan_timestamp"][:19])
            except Exception:
                continue

            upto = [(d, c, v) for d, c, v in bars if d <= sd]
            if len(upto) >= 21:
                win = upto[-21:]
                cl = [c for _, c, _ in win]
                vl = [v for _, _, v in win]
                raw, ret5, ret20, relv = _thrust(cl, vl)
                if raw is not None:
                    if not str(r.get("momentum_raw", "")).strip():
                        r["momentum_raw"] = raw
                        filled["momentum_raw"] += 1
                    lo = float(cfg.get("exhaustion_start", 0.20))
                    hi = float(cfg.get("exhaustion_full", 0.60))
                    fl = float(cfg.get("exhaustion_floor", 0.50))
                    ef = 1.0
                    if ret20 > lo and hi > lo:
                        ef = round(1.0 - (1.0 - fl)
                                   * min((ret20 - lo) / (hi - lo), 1.0), 3)
                    if not str(r.get("exhaustion_factor", "")).strip():
                        r["exhaustion_factor"] = ef
                        filled["exhaustion_factor"] += 1
                    for col, val, nd in (("momentum_score", raw * ef, 2),
                                         ("ret_5d", ret5, 5),
                                         ("ret_20d", ret20, 5),
                                         ("rel_volume", relv, 3)):
                        if val is not None and not str(r.get(col, "")).strip():
                            r[col] = round(val, nd)
                            filled[col] += 1
                    touched += 1

            # ── DTC family, against the settlement current at scan time ──
            if settlements and len(upto) >= 60:
                prior = [s for s in settlements
                         if s.get("settlement") and s["settlement"] <= sd.date()]
                if prior:
                    cur = prior[0]
                    vol_upto = [(d, v) for d, _, v in upto]
                    panel = dtc_engine.dtc_panel(
                        cur.get("interest"), vol_upto,
                        settlement=cur.get("settlement"),
                        exchange_dtc=cur.get("dtc"),
                        exchange_adv=cur.get("avg_volume"))
                    for col, key in (("dtc_robust", "robust_10d_median"),
                                     ("dtc_60d", "horizon_60d_median"),
                                     ("dtc_spike_ratio", "spike_ratio")):
                        v = panel.get(key)
                        if v and not str(r.get(col, "")).strip():
                            r[col] = round(v, 3)
                            filled[col] += 1

            # ── Reg SHO as of the scan date ──
            if not str(r.get("reg_sho_days", "")).strip():
                streak = 0
                ok = True
                d = sd.date()
                misses = 0
                seen = 0
                while seen < 20 and misses < 12:
                    syms = reg_sho._fetch_day(d)
                    if syms is None:
                        misses += 1
                    else:
                        seen += 1
                        if tk in syms:
                            streak += 1
                        else:
                            break
                    d -= timedelta(days=1)
                    if misses >= 12:
                        ok = False
                if ok and seen:
                    r["reg_sho_days"] = streak
                    ceil = float(cfg.get("reg_sho_mult_max", 1.20))
                    r["reg_sho_mult"] = (
                        round(1.0 + (ceil - 1.0) * min(streak / 13.0, 1.0), 4)
                        if streak else 1.0)
                    filled["reg_sho_days"] += 1
                    filled["reg_sho_mult"] += 1

        if n % 20 == 0 or n == len(ticks):
            print(f"  {n}/{len(ticks)} tickers   rows touched {touched}")

    print(f"\nscoring_version=1 stamped on {stamped} rows")
    print("reconstructed:")
    for c in BACKFILL_COLS:
        print(f"   {c:<20}{filled[c]:>6} rows "
              f"({filled[c]/max(len(rows),1)*100:.1f}%)")
    print("\nleft empty on purpose (snapshot-only sources, would be lookahead):")
    print("   " + ", ".join(FORWARD_ONLY))

    if not apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"{LOG_FILE}.pre_backfill_{stamp}"
    shutil.copy2(LOG_FILE, backup)
    print(f"\nbacked up -> {os.path.basename(backup)}")

    tmp = LOG_FILE + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})
    os.replace(tmp, LOG_FILE)
    print(f"wrote {LOG_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
