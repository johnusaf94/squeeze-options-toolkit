"""
squeeze_logger.py
==================
Write-only scan logger. Every squeeze scan appends its top-25 candidates
(with every score component separately) to squeeze_log.csv.

The log is IMMUTABLE history — never read or modified by the scanner.
A separate review_outcomes.py grades it later.

One row = one candidate from one scan.
"""

import csv
import os
from datetime import datetime

LOG_FILE = "squeeze_log.csv"

# Every signal stored separately so regression can isolate what predicts.
# Fields whose presence decides whether a logged row is usable evidence.
# A row is not "bad" for missing some — but a training set that cannot tell a
# complete row from a gutted one will average the two and learn from neither.
QUALITY_FIELDS = ["si_pct", "dtc", "ctb", "implied_move_pct",
                  "gex_net_musd", "svr_recent", "combined", "final_score"]


def _num(v, nd):
    """Round a numeric to nd places, or blank. Blank beats a fabricated 0 —
    _completeness treats a scrubbed-to-zero field as missing for the same
    reason."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return ""
    if v != v or v in (float("inf"), float("-inf")):
        return ""
    return round(v, nd) if nd else int(v)


def _completeness(row: dict) -> float:
    """Share of QUALITY_FIELDS that carry a real value in this row.

    Exists because two silent outages — a cache bug that destroyed option
    chains and an expired CA root that killed the FINRA feed — left
    implied_move_pct present in 59% of graded rows and svr_recent in 18%,
    with nothing anywhere recording that the rows were different. Stamping it
    at write time means a later refit can separate the healthy rows from the
    damaged ones instead of guessing."""
    have = 0
    for f in QUALITY_FIELDS:
        v = row.get(f)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        try:
            if float(v) == 0.0:
                continue          # a scrubbed-to-zero field is a missing one
        except (TypeError, ValueError):
            pass
        have += 1
    return round(have / len(QUALITY_FIELDS), 3)


LOG_COLUMNS = [
    "scan_timestamp",      # ISO datetime of the scan
    "scan_id",             # YYYYMMDD-HHMMSS — groups one scan together
    "feature_completeness",  # 0..1 share of QUALITY_FIELDS actually present
    "rank",                # position in the re-ranked top list
    "source",              # bulk_scan | single_analysis — which funnel
    "ticker",
    "company",
    "sector",
    "tier",                # which universe tier was scanned
    # ── Stage-1 composite ──
    "combined",
    "gill",
    "chamath",
    # ── Stage-2 deep ──
    "deep_score",
    "probability",
    "imminence",
    "magnitude",
    "conviction_mult",
    "conviction_state",
    "composite_pct",
    "catalyst_window",
    "catalyst_score",
    "catalyst_mult",
    "days_to_earnings",
    "deep_verdict",
    "final_score",         # combined x conviction — the headline number
    # ── Raw squeeze metrics at scan time ──
    "si_pct",
    "dtc",
    "ctb",
    "catalyst_type",
    "implied_move_pct",
    "gex_net_musd",
    "gex_regime",
    "svr_recent",
    "svr_trend",
    "ftd_closeout_date",
    "ftd_impact_factor",
    # ── Effective float (logged from day one, scored by nothing) ──
    # Logged so the grader can eventually answer the question this repo
    # cannot answer today: does the close-out measured against TRADEABLE
    # float predict returns better than the reported-float version the
    # score currently uses? Until enough graded rows exist to say, these
    # are evidence being collected, not inputs being trusted.
    "effective_float",
    "float_tightness",
    "ftd_pct_float",
    "ftd_pct_eff_float",
    "ftd_closeout_adv_days",
    "inst_shares_over_float",
    # ── A/B: which scorer produced this row, and what v1 would have said ──
    # Without scoring_version the grader averages two different scorers into
    # one number and learns from neither. The paired v1 columns make the
    # comparison an A/B on IDENTICAL rows rather than old-rows-vs-new-rows,
    # where the market moved underneath as well as the code.
    "scoring_version",
    "deep_score_v1",
    "probability_v1",
    "magnitude_v1",
    "ftd_score_v1",
    "ftd_impact_factor_v1",
    "ftd_mult",
    "final_score_v1",
    # ── Signals added after the log audit ──
    # Reg SHO is the official close-out clock; exhaustion records how much of
    # the thrust score was a move that had already happened; runway is the
    # dilution mechanism that turns a squeeze into a permanent loss.
    "reg_sho_days",
    "reg_sho_mult",
    "exhaustion_factor",
    "momentum_raw",
    "cash_runway_months",
    # ── DTC by named volume window + settlement-cadence trend ──
    # `dtc` above is the exchange figure and stays that. These record which
    # denominator was used and what the alternatives said, so a future refit
    # can test whether a spike-robust or long-horizon DTC predicts better —
    # and so a disagreement with any vendor is resolvable from the log alone.
    # ── THE FIVE DEEP SIGNALS ──
    # These are what stage 2 actually computes, and every one of them feeds
    # probability_score. None were logged, so every calibration run to date
    # has been asking whether short interest and days-to-cover predict
    # returns — never whether the scanner's own analysis does. The deep layer
    # has never been evaluated because the grader could not see it.
    # Undamped conviction, so the authority setting is itself testable:
    # conviction_mult / conviction_mult_raw recovers the scale that was
    # applied, and a refit can ask whether damping helped.
    # Borrow availability — blank until a source is configured, which is
    # honest: the column records that the precondition was unmeasured, not
    # that borrow was plentiful.
    "borrow_utilization",
    "shares_available",
    "borrow_rate_real",
    "borrow_mult",
    "borrow_state",
    "conviction_mult_raw",
    "convexity_score",
    "ctb_velocity_score",
    "ftd_score",
    "svr_score",
    "momentum_score",
    "ret_5d",
    "ret_20d",
    "rel_volume",
    "float_shares",
    "ctb_trend",
    "dtc_trend",
    "si_trend",
    "calibrated_prob",
    "dtc_exchange",
    "dtc_robust",
    "dtc_60d",
    "dtc_spike_ratio",
    "si_change_settlement",
    "si_trend_source",
    "si_trend_v1",
    "dtc_trend_v1",
    "settlement_date",
    "settlement_age_days",
    "price_at_scan",
    "market_cap",
    # ── Outcome columns (filled later by review_outcomes.py) ──
    "price_5d",
    "return_5d",
    "price_10d",
    "return_10d",
    "price_20d",
    "return_20d",
    "outcome_checked",     # ISO date when outcomes were last filled
]


def log_scan(candidates: list, tier: int, top_n: int = 25,
            source: str = "bulk_scan"):
    """
    Append the top-N candidates of a completed scan to squeeze_log.csv.
    Called once per scan, right after re-ranking. Never raises — logging
    failure must not break the scanner.

    source: "bulk_scan" (default — the searcher's normal call) or
    "single_analysis" (the analyzer logging one deep-dive ticker), so the
    grader can eventually compare whether the two funnels perform
    differently.
    """
    _lock = LOG_FILE + ".lock"
    _got = False
    try:
        import time as _t
        for _ in range(40):     # ~10s max wait; logging must not hang scans
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

        now = datetime.now()
        scan_id = now.strftime("%Y%m%d-%H%M%S")
        ts      = now.isoformat(timespec="seconds")

        file_exists = os.path.exists(LOG_FILE)

        # ── HEADER MIGRATION ──
        # If the log predates newer feature columns, rewrite it once with
        # the current schema (old rows padded blank) so appends never
        # corrupt the CSV. The learning engine needs these features logged
        # from day one of their existence.
        if file_exists:
            try:
                with open(LOG_FILE, "r", newline="", encoding="utf-8") as f:
                    first = f.readline().strip()
                if first and first.split(",") != LOG_COLUMNS:
                    with open(LOG_FILE, newline="", encoding="utf-8") as f:
                        _old_rows = list(csv.DictReader(f))
                    tmp = f"{LOG_FILE}.migrate.tmp"
                    with open(tmp, "w", newline="", encoding="utf-8") as f:
                        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS,
                                           extrasaction="ignore")
                        w.writeheader()
                        for r in _old_rows:
                            row_out = {k: r.get(k, "") for k in LOG_COLUMNS}
                            # every row logged before "source" existed came
                            # from the bulk scanner — the analyzer's logging
                            # call is new as of this change
                            if not row_out.get("source"):
                                row_out["source"] = "bulk_scan"
                            w.writerow(row_out)
                    os.replace(tmp, LOG_FILE)
            except Exception:
                pass   # best-effort; never block logging

        # Append mode — file only ever grows
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
            if not file_exists:
                writer.writeheader()

            for rank, c in enumerate(candidates[:top_n], 1):
                writer.writerow({
                    "scan_timestamp":   ts,
                    "scan_id":          scan_id,
                    "feature_completeness": _completeness(c),
                    "rank":             rank,
                    "source":           source,
                    "ticker":           c.get("ticker", ""),
                    "company":          (c.get("company", "") or "")[:40],
                    "sector":           c.get("sector", ""),
                    "tier":             tier,
                    "combined":         round(c.get("combined", 0), 2),
                    "gill":             round(c.get("gill", 0), 2),
                    "chamath":          round(c.get("chamath", 0), 2),
                    "deep_score":       round(c.get("deep_score", 0), 2),
                    "probability":      round(c.get("probability", 0), 2),
                    "imminence":        round(c.get("imminence", 0), 2),
                    "magnitude":        round(c.get("magnitude", 0), 2),
                    "conviction_mult":  round(c.get("conviction_mult", 1.0), 3),
                    "conviction_state": c.get("conviction_state", ""),
                    "composite_pct":    (round(c["composite_pct"], 1)
                                         if isinstance(c.get("composite_pct"), (int, float))
                                         else ""),
                    "catalyst_window":  c.get("catalyst_window", ""),
                    "catalyst_score":   round(c.get("catalyst_score", 0), 1),
                    "catalyst_mult":    round(c.get("catalyst_mult", 1.0), 3),
                    "days_to_earnings": c.get("days_to_earnings", ""),
                    "deep_verdict":     c.get("deep_verdict", ""),
                    "final_score":      round(c.get("final_score",
                                                     c.get("combined", 0)), 2),
                    # None-tolerant: analyzer rows can carry None for
                    # thin names; round(None) would kill the whole log call.
                    # Blank is honest; a fabricated 0 is not.
                    "si_pct":           (round(c["si"], 4)
                                         if isinstance(c.get("si"), (int, float)) else ""),
                    "dtc":              (round(c["dtc"], 2)
                                         if isinstance(c.get("dtc"), (int, float)) else ""),
                    "ctb":              round(c.get("ctb", 0) or 0, 2),
                    "catalyst_type":    c.get("catalyst_type", ""),
                    "implied_move_pct": (round(c["implied_move_pct"], 4)
                                         if c.get("implied_move_pct") is not None else ""),
                    "gex_net_musd":     (c.get("gex_net_musd")
                                         if c.get("gex_net_musd") is not None else ""),
                    "gex_regime":       ((c.get("gex_regime") or "").split(" — ")[0]),
                    "svr_recent":       (c.get("svr_recent")
                                         if c.get("svr_recent") is not None else ""),
                    "svr_trend":        c.get("svr_trend", ""),
                    "ftd_closeout_date": c.get("ftd_closeout_date", ""),
                    "ftd_impact_factor": c.get("ftd_impact_factor", ""),
                    "effective_float":   (round(c["effective_float"])
                                          if isinstance(c.get("effective_float"),
                                                        (int, float)) else ""),
                    "float_tightness":   (round(c["float_tightness"], 3)
                                          if isinstance(c.get("float_tightness"),
                                                        (int, float)) else ""),
                    "ftd_pct_float":     (round(c["ftd_pct_float"], 6)
                                          if isinstance(c.get("ftd_pct_float"),
                                                        (int, float)) else ""),
                    "ftd_pct_eff_float": (round(c["ftd_pct_eff_float"], 6)
                                          if isinstance(c.get("ftd_pct_eff_float"),
                                                        (int, float)) else ""),
                    "ftd_closeout_adv_days": (round(c["ftd_closeout_adv_days"], 3)
                                              if isinstance(c.get("ftd_closeout_adv_days"),
                                                            (int, float)) else ""),
                    "inst_shares_over_float": (round(c["inst_shares_over_float"], 3)
                                               if isinstance(c.get("inst_shares_over_float"),
                                                             (int, float)) else ""),
                    "scoring_version":  c.get("scoring_version", ""),
                    "deep_score_v1":    (round(c["deep_score_v1"], 2)
                                         if isinstance(c.get("deep_score_v1"),
                                                       (int, float)) else ""),
                    "probability_v1":   (round(c["probability_v1"], 2)
                                         if isinstance(c.get("probability_v1"),
                                                       (int, float)) else ""),
                    "magnitude_v1":     (round(c["magnitude_v1"], 2)
                                         if isinstance(c.get("magnitude_v1"),
                                                       (int, float)) else ""),
                    "ftd_score_v1":     (round(c["ftd_score_v1"], 2)
                                         if isinstance(c.get("ftd_score_v1"),
                                                       (int, float)) else ""),
                    "ftd_impact_factor_v1": (round(c["ftd_impact_factor_v1"], 3)
                                             if isinstance(c.get("ftd_impact_factor_v1"),
                                                           (int, float)) else ""),
                    "reg_sho_days":     c.get("reg_sho_days", ""),
                    "reg_sho_mult":     (round(c["reg_sho_mult"], 4)
                                         if isinstance(c.get("reg_sho_mult"),
                                                       (int, float)) else ""),
                    "exhaustion_factor": (round(c["exhaustion_factor"], 3)
                                          if isinstance(c.get("exhaustion_factor"),
                                                        (int, float)) else ""),
                    "momentum_raw":     (round(c["momentum_raw"], 2)
                                         if isinstance(c.get("momentum_raw"),
                                                       (int, float)) else ""),
                    "cash_runway_months": (round(c["cash_runway_months"], 1)
                                           if isinstance(c.get("cash_runway_months"),
                                                         (int, float))
                                           and c["cash_runway_months"] != float("inf")
                                           else ""),
                    "ftd_mult":         (round(c["ftd_mult"], 4)
                                         if isinstance(c.get("ftd_mult"),
                                                       (int, float)) else ""),
                    "final_score_v1":   (round(c["final_score_v1"], 2)
                                         if isinstance(c.get("final_score_v1"),
                                                       (int, float)) else ""),
                    "borrow_utilization": _num(c.get("borrow_utilization"), 4),
                    "shares_available":  _num(c.get("shares_available"), 0),
                    "borrow_rate_real":  _num(c.get("borrow_rate_real"), 2),
                    "borrow_mult":       _num(c.get("borrow_mult"), 4),
                    "borrow_state":      c.get("borrow_state", ""),
                    "conviction_mult_raw": _num(c.get("conviction_mult_raw"), 4),
                    "convexity_score":  _num(c.get("convexity_score"), 2),
                    "ctb_velocity_score": _num(c.get("ctb_velocity_score"), 2),
                    "ftd_score":        _num(c.get("ftd_score"), 2),
                    "svr_score":        _num(c.get("svr_score"), 2),
                    "momentum_score":   _num(c.get("momentum_score"), 2),
                    "ret_5d":           _num(c.get("ret_5d"), 5),
                    "ret_20d":          _num(c.get("ret_20d"), 5),
                    "rel_volume":       _num(c.get("rel_volume"), 3),
                    "float_shares":     _num(c.get("float_shares"), 0),
                    "ctb_trend":        c.get("ctb_trend", ""),
                    "dtc_trend":        c.get("dtc_trend", ""),
                    "si_trend":         c.get("si_trend", ""),
                    "calibrated_prob":  _num(c.get("calibrated_prob"), 4),
                    "dtc_exchange":     (round(c["dtc_exchange"], 3)
                                         if isinstance(c.get("dtc_exchange"),
                                                       (int, float)) else ""),
                    "dtc_robust":       (round(c["dtc_robust"], 3)
                                         if isinstance(c.get("dtc_robust"),
                                                       (int, float)) else ""),
                    "dtc_60d":          (round(c["dtc_60d"], 3)
                                         if isinstance(c.get("dtc_60d"),
                                                       (int, float)) else ""),
                    "dtc_spike_ratio":  (round(c["dtc_spike_ratio"], 3)
                                         if isinstance(c.get("dtc_spike_ratio"),
                                                       (int, float)) else ""),
                    "si_change_settlement": (round(c["si_change_settlement"], 5)
                                             if isinstance(c.get("si_change_settlement"),
                                                           (int, float)) else ""),
                    "si_trend_source":  c.get("si_trend_source", ""),
                    "si_trend_v1":      c.get("si_trend_v1", ""),
                    "dtc_trend_v1":     c.get("dtc_trend_v1", ""),
                    "settlement_date":  c.get("settlement_date", ""),
                    "settlement_age_days": (c.get("settlement_age_days")
                                            if c.get("settlement_age_days") is not None
                                            else ""),
                    # Blank, not 0. A zero price is a missing price, and a
                    # zero market cap is a missing market cap — writing them
                    # as 0 makes an absent field indistinguishable from a
                    # real measurement, and _completeness() already treats a
                    # scrubbed-to-zero field as missing for exactly this
                    # reason. 9 rows carried price 0 and 18 carried mktcap 0.
                    "price_at_scan":    (round(c["price"], 4)
                                         if isinstance(c.get("price"), (int, float))
                                         and c["price"] > 0 else ""),
                    "market_cap":       (c["mktcap"]
                                         if isinstance(c.get("mktcap"), (int, float))
                                         and c["mktcap"] > 0 else ""),
                    # outcomes blank until review_outcomes.py runs
                    "price_5d":         "",
                    "return_5d":        "",
                    "price_10d":        "",
                    "return_10d":       "",
                    "price_20d":        "",
                    "return_20d":       "",
                    "outcome_checked":  "",
                })
        return scan_id, True
    except Exception as e:
        # Logging must never break the scanner
        return f"log_error: {e}", False
    finally:
        if _got:
            try:
                os.remove(_lock)
            except OSError:
                pass


def log_summary() -> str:
    """Quick stats about the accumulated log (for display)."""
    if not os.path.exists(LOG_FILE):
        return "No scan log yet."
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return "Log is empty."
        scans = len({r["scan_id"] for r in rows})
        checked = sum(1 for r in rows if r.get("outcome_checked"))
        return (f"Log: {len(rows)} candidates across {scans} scans "
                f"| {checked} have outcomes")
    except Exception:
        return "Log exists (stats unavailable)."
