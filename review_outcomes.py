"""
review_outcomes.py
===================
Standalone. Run whenever you want (weekly is ideal).

Reads squeeze_log.csv, finds every candidate whose scan is now old enough
to have 5/10/20-trading-day outcomes, fetches actual prices, and fills in
the return columns. Then prints a performance report — your scanner's
real hit rate broken down by signal.

Run:  python review_outcomes.py

This is the file whose output you bring to Claude for re-weighting.
"""

import csv
import os
import sys
from datetime import datetime, timedelta

LOG_FILE = "squeeze_log.csv"

try:
    import yfinance as yf
except ImportError:
    print("yfinance required:  pip install yfinance")
    sys.exit(1)


# ─────────────────────────────────────────────
# PRICE LOOKUP
# ─────────────────────────────────────────────

def _trading_days_after(start: datetime, n: int) -> datetime:
    """Approximate calendar date n trading days after start (n*1.4 cal days)."""
    return start + timedelta(days=int(n * 1.4) + 2)


# How far past the target date a bar may sit and still be accepted as that
# date's price. A holiday or a thin tape can push the next print out a few
# sessions; a week means the security stopped trading, and the last tick
# before it stopped is not an outcome.
MAX_PRICE_GAP_DAYS = 7

# Below this scan-date price, an outcome is arithmetic rather than a result.
# CRKN was scanned at $0.0003 and printed $0.0179 five sessions later — a
# genuine, correctly measured +5850%, and completely undominated by anything
# else in 2,900 rows. One such row rewrites any regression that reads the
# column. The names this excludes are the same ones liveness.py now keeps out
# of the universe, so this only matters for history already on disk.
MIN_GRADABLE_PRICE = 0.10


def _price_on_or_after(ticker: str, target: datetime, hist) -> float:
    """Closest closing price on/after target date, or None if the tape ended.

    The old version fell back to `hist["Close"].iloc[-1]` whenever the target
    ran past the end of history — silently grading a delisted or halted name
    against its final tick no matter how stale. CRKN, SBNY and MDRX were
    graded that way for months and produced returns to -99.9%, which then
    dominated every statistic computed from this log: removing those three
    tickers moves the base rate from -0.84% to +1.08% and accounts for 14 of
    the 20 worst episodes.

    A missing outcome is honest. A fabricated one is not.
    """
    try:
        if hist is None or hist.empty:
            return None
        for idx, row in hist.iterrows():
            d = idx.to_pydatetime().replace(tzinfo=None)
            if d >= target:
                if (d - target).days > MAX_PRICE_GAP_DAYS:
                    return None      # gap in the tape — refuse to guess
                c = float(row["Close"])
                # A bar can exist with no print. NaN propagates silently
                # through the return arithmetic and lands in the log as the
                # literal string "nan", which then poisons every statistic
                # and crashes the report. Treat it as no price.
                return c if c == c and c > 0 else None
        # Target is past the end of the tape. Only accept if the tape ended
        # within the tolerance; otherwise the security stopped trading and
        # there is no price for this date.
        last_d = hist.index[-1].to_pydatetime().replace(tzinfo=None)
        if (target - last_d).days <= MAX_PRICE_GAP_DAYS:
            c = float(hist["Close"].iloc[-1])
            return c if c == c and c > 0 else None
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────
# OUTCOME FILLING
# ─────────────────────────────────────────────

def fill_outcomes():
    if not os.path.exists(LOG_FILE):
        print(f"No {LOG_FILE} found. Run a scan first.")
        return

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fieldnames = rows[0].keys() if rows else []

    if not rows:
        print("Log is empty.")
        return

    now = datetime.now()

    # ── REPAIR PASS ──
    # The old age-gate graded windows before they completed (calendar vs
    # trading days), stamping today's price as the outcome. Re-blank any
    # filled window whose target date hasn't actually arrived yet; they
    # will regrade honestly once mature.
    repaired = 0
    for r in rows:
        try:
            scan_dt = datetime.fromisoformat(r["scan_timestamp"])
        except Exception:
            continue
        row_repaired = False
        for n in (5, 10, 20):
            if r.get(f"return_{n}d") and now < _trading_days_after(scan_dt, n):
                r[f"return_{n}d"] = ""
                r[f"price_{n}d"] = ""
                repaired += 1
                row_repaired = True
        if row_repaired and not any(r.get(f"return_{k}d") for k in (5, 10, 20)):
            r["outcome_checked"] = ""
    if repaired:
        print(f"🔧 repaired {repaired} prematurely-graded window(s) — "
              f"they will regrade once their windows complete\n")

    # Group rows needing outcomes by ticker (one yfinance call per ticker)
    to_fill = {}
    for r in rows:
        if r.get("return_20d"):       # fully graded already
            continue
        try:
            scan_dt = datetime.fromisoformat(r["scan_timestamp"])
        except Exception:
            continue
        age_days = (now - scan_dt).days
        if age_days < 5:              # too fresh for any window
            continue
        to_fill.setdefault(r["ticker"], []).append((r, scan_dt, age_days))

    if not to_fill:
        print("Nothing new to grade — all candidates either too fresh or done.")
        _print_report(rows)
        return

    print(f"Grading {sum(len(v) for v in to_fill.values())} candidates "
          f"across {len(to_fill)} tickers...\n")

    for ticker, entries in to_fill.items():
        try:
            earliest = min(e[1] for e in entries)
            hist = yf.Ticker(ticker).history(
                start=(earliest - timedelta(days=2)).strftime("%Y-%m-%d"),
                end=now.strftime("%Y-%m-%d"),
                interval="1d",
            )
            if hist.empty:
                print(f"  {ticker}: no price history")
                continue

            for r, scan_dt, age in entries:
                # ── NUMERATOR AND DENOMINATOR MUST SHARE AN ADJUSTMENT ──
                # `price_at_scan` is the raw quote logged at scan time.
                # `hist` is split- and dividend-ADJUSTED and is rewritten
                # retroactively by every corporate action. Dividing one by
                # the other means any split inside the window produces a
                # return that is pure arithmetic artifact.
                #
                # The scan-date price is therefore taken from the SAME
                # adjusted frame the exit price comes from. price_at_scan
                # survives only as a last resort, flagged, for rows whose
                # history could not be fetched at all.
                p0 = _price_on_or_after(ticker, scan_dt, hist)
                if not p0 or p0 <= 0:
                    # NO unadjusted fallback. Reaching for price_at_scan here
                    # would put a raw quote over an adjusted exit price — the
                    # exact unit mismatch this block exists to prevent — and
                    # it would do so precisely on the names where history is
                    # patchy, which are the names most likely to have had a
                    # corporate action. If the tape cannot price the scan
                    # date, the row does not get an outcome.
                    continue
                # A frozen quote is not a price. When the logged scan price
                # and the adjusted history disagree by more than 5x, one of
                # them describes a security that stopped trading; grading
                # either way invents an outcome.
                try:
                    _raw = float(r["price_at_scan"])
                except (ValueError, TypeError):
                    _raw = None
                if (_raw and _raw > 0
                        and (p0 / _raw > 5.0 or _raw / p0 > 5.0)):
                    print(f"  {ticker}: scan price {_raw:g} vs adjusted "
                          f"{p0:g} ({scan_dt.date()}) — corporate action or "
                          f"dead tape, not graded")
                    continue
                if p0 < MIN_GRADABLE_PRICE:
                    print(f"  {ticker}: scan price {p0:g} below "
                          f"${MIN_GRADABLE_PRICE:g} ({scan_dt.date()}) — "
                          f"sub-penny moves are arithmetic, not outcomes; "
                          f"not graded")
                    continue

                any_filled = False
                for n in (5, 10, 20):
                    if not r.get(f"return_{n}d"):
                        tgt = _trading_days_after(scan_dt, n)
                        # CRITICAL GATE: the window must have COMPLETED.
                        # The old check (age >= n) compared calendar days
                        # to trading days, so young rows were graded with
                        # today's price as if the window had finished —
                        # truncated outcomes poisoning the training data.
                        if now < tgt:
                            continue
                        pn = _price_on_or_after(ticker, tgt, hist)
                        if pn:
                            r[f"price_{n}d"]  = round(pn, 4)
                            # NOTE: returns stored as FRACTIONS (0.15 = 15%).
                            # learning_engine normalizes at read (and
                            # recomputes from the price columns), so do NOT
                            # change this convention casually.
                            r[f"return_{n}d"] = round((pn - p0) / p0, 4)
                            any_filled = True
                if any_filled or r.get("return_10d"):
                    r["outcome_checked"] = now.strftime("%Y-%m-%d")
            print(f"  {ticker}: graded {len(entries)} entries")
        except Exception as e:
            print(f"  {ticker}: error — {e}")

    # Write back (under the shared lock: a scan appending mid-rewrite
    # would otherwise be lost when this stale copy replaces the file)
    _lock = LOG_FILE + ".lock"
    import time as _t
    _got = False
    for _ in range(80):
        try:
            _fd = os.open(_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(_fd)
            _got = True
            break
        except FileExistsError:
            try:
                if _t.time() - os.path.getmtime(_lock) > 600:
                    os.remove(_lock)
                    continue
            except OSError:
                pass
            _t.sleep(0.25)
    try:
        tmp = f"{LOG_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(fieldnames))
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, LOG_FILE)
    finally:
        if _got:
            try:
                os.remove(_lock)
            except OSError:
                pass

    print(f"\n✅ Outcomes written to {LOG_FILE}\n")
    _print_report(rows)


# ─────────────────────────────────────────────
# PERFORMANCE REPORT
# ─────────────────────────────────────────────

def _safe_float(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _bucket_stats(rows, window="return_10d"):
    """Hit rate (+15% in window, matching calibration) and avg return by band."""
    graded = [r for r in rows if _safe_float(r.get(window)) is not None]
    if not graded:
        return None

    def stats(subset):
        if not subset:
            return (0, 0.0, 0.0)
        rets = [_safe_float(r[window]) for r in subset]
        hits = sum(1 for x in rets if x >= 0.15)   # matches calibration's +15% winner
        return (len(subset), hits / len(subset), sum(rets) / len(rets))

    report = {"_n_graded": len(graded), "_window": window}

    # By deep verdict
    for verdict_key in ("IGNITING", "BUILDING", "DORMANT", "TRAP"):
        sub = [r for r in graded if verdict_key in (r.get("deep_verdict") or "")]
        if sub:
            n, hr, avg = stats(sub)
            report[f"verdict::{verdict_key}"] = (n, hr, avg)

    # By final_score band
    for lo, hi, lbl in [(0, 50, "0-50"), (50, 70, "50-70"),
                         (70, 100, "70-100"), (100, 999, "100+")]:
        sub = [r for r in graded
               if lo <= (_safe_float(r.get("final_score")) or 0) < hi]
        if sub:
            n, hr, avg = stats(sub)
            report[f"final::{lbl}"] = (n, hr, avg)

    # By SI% band
    for lo, hi, lbl in [(0, 0.15, "<15%"), (0.15, 0.25, "15-25%"),
                        (0.25, 0.40, "25-40%"), (0.40, 1.0, "40%+")]:
        sub = [r for r in graded
               if lo <= (_safe_float(r.get("si_pct")) or 0) < hi]
        if sub:
            n, hr, avg = stats(sub)
            report[f"si::{lbl}"] = (n, hr, avg)

    # Correlation of each signal with return
    import math
    def corr(xs, ys):
        n = len(xs)
        if n < 3:
            return None
        mx, my = sum(xs)/n, sum(ys)/n
        cov = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
        sx  = math.sqrt(sum((x-mx)**2 for x in xs))
        sy  = math.sqrt(sum((y-my)**2 for y in ys))
        if sx == 0 or sy == 0:
            return None
        return cov / (sx * sy)

    for sig in ("combined", "final_score", "deep_score", "probability",
                "imminence", "magnitude", "conviction_mult", "si_pct",
                "dtc", "ctb"):
        pairs = [(_safe_float(r.get(sig)), _safe_float(r.get(window)))
                 for r in graded]
        pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
        if len(pairs) >= 3:
            c = corr([p[0] for p in pairs], [p[1] for p in pairs])
            if c is not None:
                report[f"corr::{sig}"] = c

    return report


def _print_report(rows):
    print("=" * 64)
    print("  SQUEEZE SCANNER PERFORMANCE REPORT")
    print("=" * 64)

    scans = len({r["scan_id"] for r in rows})
    graded = sum(1 for r in rows if r.get("return_10d"))
    print(f"  Total logged: {len(rows)} candidates / {scans} scans")
    print(f"  Graded (10d outcome available): {graded}")

    if graded < 30:
        print(f"\n  ⚠️  Only {graded} graded outcomes. Need ~150+ for")
        print(f"      statistically trustworthy re-weighting.")
        print(f"      Keep scanning weekly — report sharpens over time.")

    for window in ("return_10d", "return_20d"):
        rep = _bucket_stats(rows, window)
        if not rep or rep["_n_graded"] == 0:
            continue
        wl = window.replace("return_", "").upper()
        print(f"\n  ── {wl} OUTCOMES  (n={rep['_n_graded']}) ──")
        print(f"  {'Bucket':<22} {'N':>4} {'Hit%(+15)':>10} {'AvgRet':>9}")
        print(f"  {'-'*48}")
        for key in sorted(k for k in rep if "::" in k and not k.startswith("corr")):
            grp, lbl = key.split("::")
            n, hr, avg = rep[key]
            print(f"  {grp+' '+lbl:<22} {n:>4} {hr:>9.0%} {avg:>+8.1%}")

        # NaN-tolerant: a single unprintable bar used to take the whole
        # report down with "cannot convert float NaN to integer", after the
        # log had already been written — so the run looked like a failure
        # when the data was fine.
        corrs = {k.split("::")[1]: v for k, v in rep.items()
                 if k.startswith("corr::") and v == v}
        if corrs:
            print(f"\n  Signal correlation with {wl} return:")
            for sig, c in sorted(corrs.items(),
                                  key=lambda kv: abs(kv[1]), reverse=True):
                bar = "█" * int(min(abs(c), 1.0) * 20)
                print(f"    {sig:<16} {c:>+.3f}  {bar}")

    print("\n" + "=" * 64)
    print("  Bring squeeze_log.csv to Claude for regression + re-weighting.")
    print("=" * 64)


if __name__ == "__main__":
    fill_outcomes()

    # ── Also grade the scenario engine's own predictions ──
    # scenario_engine logs every auto-generated distribution; without this
    # call it records forever but never learns (stuck at Tier 0). Running
    # it here means ONE weekly command grades both loops. Best-effort:
    # a scenario-grading problem must never block the squeeze report.
    try:
        import scenario_engine
        _n = scenario_engine.grade_scenarios()
        _sp = scenario_engine._load_sc_params()
        _ng = int(_sp.get("n_graded", 0) or 0)
        if _n:
            print(f"\n🎯 scenario engine: graded {_n} new prediction(s); "
                  f"{_ng} total graded.")
        if _sp.get("active"):
            print(f"   scenario tilt ACTIVE — emp up-rate "
                  f"{_sp.get('emp_p_up', 0):.0%}, "
                  f"mag x{_sp.get('emp_up_mag_ratio', 1.0)} "
                  f"(auto scenarios now Tier 1+).")
        elif _ng:
            print(f"   scenario engine at Tier 0 — {_sp.get('gate', '')}")
    except Exception as _e:
        print(f"\n⚠️ scenario grading skipped: {_e}")
