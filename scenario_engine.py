"""
scenario_engine.py
==================
Automatic scenario generation for the options layer — ZERO human input.

The engine answers "what is the probability distribution of this stock's
move by the catalyst?" using a GRADUATED TRUST LADDER. It works on day
one with no training data, and hands itself over to learned parameters
as graded history accumulates — automatically, with no rewiring.

  TIER 0 — heuristic (active immediately, no history needed)
    Magnitudes come from the MARKET's implied move (already computed by
    the analyzer); probabilities start market-neutral and are tilted
    modestly by the squeeze score. Deterministic, documented, clamped.
    A heuristic — and labeled as one in every output.

  TIER 1 — empirical blend (>=30 self-graded scenario rows)
    The engine grades its own past scenario sets against realized
    prices, then shrinks Tier-0 parameters toward what actually
    happened (win-rate tilt, magnitude ratio). Same shrinkage-and-clamp
    philosophy as learning_engine.

  TIER 2 — calibrated (learning_engine calibration active, 150+ rows)
    The squeeze system's own calibrated P(+15%/10d) becomes the
    probability source for the up scenario, per-candidate.

WHAT THIS ENGINE WILL NEVER DO: invent training data. Every parameter
either comes from live market data, from the documented heuristic, or
from graded REAL outcomes. The tier and provenance are printed with
every generated scenario set so you always know what you're trusting.

Files it owns (created next to this module):
  scenario_log.csv     — every generated scenario set (for self-grading)
  scenario_params.json — learned tilt parameters + evidence
"""

import csv
import json
import math
import os
from datetime import datetime, date
from typing import Optional

_DIR = os.path.dirname(os.path.abspath(__file__))
SCENARIO_LOG = os.path.join(_DIR, "scenario_log.csv")
PARAMS_FILE = os.path.join(_DIR, "scenario_params.json")

LOG_COLUMNS = ["logged_at", "ticker", "spot", "horizon_days",
               "p_up", "up_move", "p_flat", "down_move", "p_down",
               "implied_move", "final_score", "tier",
               "outcome_checked", "realized_ret", "realized_bucket"]

# ── Tier-0 heuristic constants (documented, clamped) ──
BASE_P_UP = 0.30          # market-neutral-ish starting up-probability
SCORE_TILT_MAX = 0.15     # a 140-score adds at most +15 pts of up-prob
SCORE_REF = 140.0         # score at which the full tilt applies
UP_MAG_MULT = 1.20        # squeeze upside runs past the implied move...
DOWN_MAG_MULT = 0.80      # ...and failed setups fade rather than crash
P_UP_CLAMP = (0.15, 0.55)  # heuristic may never claim near-certainty
MAG_CLAMP = (0.03, 0.60)   # scenario moves stay in a sane band
MIN_GRADED_T1 = 30        # rows before self-graded history gets weight
SHRINK_K = 40.0           # weight = n / (n + K), mirrors learning_engine


# ─────────────────────────────────────────────
# PARAMS (learned tilt, evidence-gated)
# ─────────────────────────────────────────────

def _load_sc_params() -> dict:
    try:
        with open(PARAMS_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_sc_params(p: dict):
    tmp = PARAMS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(p, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PARAMS_FILE)


# ─────────────────────────────────────────────
# GENERATION
# ─────────────────────────────────────────────

def auto_scenarios(spot: float,
                   implied_move_pct: Optional[float] = None,
                   final_score: Optional[float] = None,
                   catalyst_days: Optional[int] = None,
                   calibrated_prob: Optional[float] = None) -> dict:
    """Generate a 3-scenario distribution with zero human input.

    Returns {scenarios: [(p, move)...], text: '38:+10.2, ...',
             tier: 0|1|2, provenance: str}
    Inputs are the analyzer's own outputs; all optional — the engine
    degrades gracefully (a missing implied move falls back to a default
    magnitude, clearly labeled)."""
    # ---- magnitudes (market-anchored) ----
    im = implied_move_pct if (implied_move_pct and implied_move_pct > 0) else None
    im_src = "ATM implied move" if im else "default (no implied move)"
    base_mag = im if im else 0.10
    up_mag = min(max(base_mag * UP_MAG_MULT, MAG_CLAMP[0]), MAG_CLAMP[1])
    down_mag = -min(max(base_mag * DOWN_MAG_MULT, MAG_CLAMP[0]), MAG_CLAMP[1])

    # ---- Tier-0 probability: neutral base + bounded score tilt ----
    tilt = 0.0
    if final_score is not None and final_score > 0:
        tilt = SCORE_TILT_MAX * min(final_score / SCORE_REF, 1.0)
    p_up = BASE_P_UP + tilt
    tier = 0
    prov = [f"tier0: magnitudes from {im_src} "
            f"(±{base_mag:.1%} -> +{up_mag:.1%}/{down_mag:.1%}), "
            f"up-prob {BASE_P_UP:.0%}+{tilt:.0%} score tilt"]

    # ---- Tier-1: blend with self-graded history (shrinkage) ----
    sp = _load_sc_params()
    n = int(sp.get("n_graded", 0) or 0)
    if n >= MIN_GRADED_T1 and sp.get("emp_p_up") is not None:
        w = n / (n + SHRINK_K)
        p_up = (1 - w) * p_up + w * float(sp["emp_p_up"])
        mr = float(sp.get("emp_up_mag_ratio", 1.0) or 1.0)
        mr = min(max(mr, 0.5), 2.0)           # clamp learned magnitude ratio
        up_mag = min(max(up_mag * mr, MAG_CLAMP[0]), MAG_CLAMP[1])
        tier = 1
        prov.append(f"tier1: blended with {n} self-graded rows "
                    f"(w={w:.0%}, emp up-rate {float(sp['emp_p_up']):.0%}, "
                    f"mag x{mr:.2f})")

    # ---- Tier-2: calibrated per-candidate probability takes over ----
    if calibrated_prob is not None and 0 < calibrated_prob < 1:
        p_up = calibrated_prob
        tier = 2
        prov.append(f"tier2: up-prob = system calibrated P "
                    f"{calibrated_prob:.0%} (learned model, per-candidate)")

    p_up = min(max(p_up, P_UP_CLAMP[0]), P_UP_CLAMP[1])
    p_down = min(max(0.45 * (1.0 - p_up), 0.10, ), 0.45)
    p_flat = max(1.0 - p_up - p_down, 0.05)
    # renormalize exactly
    tot = p_up + p_flat + p_down
    scenarios = [(p_up / tot, up_mag), (p_flat / tot, 0.0),
                 (p_down / tot, down_mag)]
    text = ", ".join(f"{p * 100:.0f}:{m * 100:+.1f}" for p, m in scenarios)
    return {"scenarios": scenarios, "text": text, "tier": tier,
            "provenance": " | ".join(prov),
            "horizon_days": catalyst_days if catalyst_days else 14}


# ─────────────────────────────────────────────
# SELF-GRADING LOOP
# ─────────────────────────────────────────────

def log_generated(ticker: str, spot: float, gen: dict,
                  implied_move_pct=None, final_score=None):
    """Append a generated scenario set to the log so it can be graded
    against reality later. This is how the engine earns Tier 1."""
    (p_up, up_m), (p_flat, _), (p_down, dn_m) = gen["scenarios"]
    row = {"logged_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
           "ticker": ticker.upper(), "spot": round(spot, 4),
           "horizon_days": gen.get("horizon_days", 14),
           "p_up": round(p_up, 4), "up_move": round(up_m, 4),
           "p_flat": round(p_flat, 4), "down_move": round(dn_m, 4),
           "p_down": round(p_down, 4),
           "implied_move": (round(implied_move_pct, 4)
                            if implied_move_pct else ""),
           "final_score": final_score if final_score is not None else "",
           "tier": gen.get("tier", 0),
           "outcome_checked": "", "realized_ret": "",
           "realized_bucket": ""}
    exists = os.path.exists(SCENARIO_LOG)
    with open(SCENARIO_LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
        if not exists:
            w.writeheader()
        w.writerow(row)


def grade_scenarios(fetch_close=None) -> int:
    """Fill realized returns for logged rows whose horizon has passed.
    fetch_close(ticker, iso_date) -> price on/after that date (injectable
    for tests; defaults to a yfinance implementation). Buckets each row:
    UP if realized >= half the up-move, DOWN if <= half the down-move,
    else FLAT. Returns rows graded."""
    if not os.path.exists(SCENARIO_LOG):
        return 0
    if fetch_close is None:
        fetch_close = _yf_close_on_or_after
    with open(SCENARIO_LOG, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    graded = 0
    today = date.today()
    for r in rows:
        if r.get("outcome_checked"):
            continue
        try:
            t0 = datetime.strptime(r["logged_at"][:10], "%Y-%m-%d").date()
            hz = int(float(r["horizon_days"] or 14))
        except (ValueError, TypeError):
            continue
        target = t0.toordinal() + hz
        if today.toordinal() < target:
            continue                      # horizon not reached yet
        tgt_iso = date.fromordinal(target).isoformat()
        px = fetch_close(r["ticker"], tgt_iso)
        spot = float(r["spot"] or 0)
        if not px or not spot:
            continue
        ret = px / spot - 1.0
        up_m, dn_m = float(r["up_move"]), float(r["down_move"])
        bucket = ("UP" if ret >= up_m * 0.5 else
                  ("DOWN" if ret <= dn_m * 0.5 else "FLAT"))
        r["outcome_checked"] = today.isoformat()
        r["realized_ret"] = round(ret, 4)
        r["realized_bucket"] = bucket
        graded += 1
    if graded:
        tmp = SCENARIO_LOG + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in LOG_COLUMNS})
        os.replace(tmp, SCENARIO_LOG)
        update_params()
    return graded


def _yf_close_on_or_after(ticker: str, iso_date: str) -> Optional[float]:
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="6mo")
        if h is None or h.empty:
            return None
        for idx, rowp in h.iterrows():
            if idx.strftime("%Y-%m-%d") >= iso_date:
                return float(rowp["Close"])
        return None
    except Exception:
        return None


def update_params() -> dict:
    """Refit the learned tilt from graded rows. Evidence-gated (30+),
    clamped, and stored with its evidence so it is auditable."""
    if not os.path.exists(SCENARIO_LOG):
        return {}
    with open(SCENARIO_LOG, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("outcome_checked")]
    p = {"n_graded": len(rows), "updated_at":
         datetime.now().isoformat(timespec="seconds")}
    if len(rows) >= MIN_GRADED_T1:
        ups = [r for r in rows if r["realized_bucket"] == "UP"]
        p["emp_p_up"] = round(len(ups) / len(rows), 4)
        # magnitude honesty: how big were UP moves vs what we projected?
        ratios = []
        for r in ups:
            try:
                proj = float(r["up_move"])
                real = float(r["realized_ret"])
                if proj > 0:
                    ratios.append(min(max(real / proj, 0.25), 4.0))
            except (ValueError, TypeError):
                pass
        if ratios:
            ratios.sort()
            p["emp_up_mag_ratio"] = round(ratios[len(ratios) // 2], 4)
        p["active"] = True
    else:
        p["active"] = False
        p["gate"] = (f"need {MIN_GRADED_T1}+ graded scenario rows "
                     f"(have {len(rows)})")
    _save_sc_params(p)
    return p
