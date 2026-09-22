"""
hypotheses.py
=============
Declared, dated predictions — registered BEFORE the data that will judge them
exists, and evaluated only on rows scanned after registration.

WHY THIS EXISTS
---------------
On 2026-08-30 the graded log was screened for anything that timed an entry:
27 numeric features and roughly 35 categorical buckets, against forward
return, on 697 episode-deduplicated rows. Two things looked promising.

    gex_regime = SHORT-GAMMA      10d hit 26.2% vs 16.4% base   (n=80)
                                  20d hit 36.9% vs 26.1% base   (n=65)
    dtc_spike_ratio >= 1.24       10d hit 12.2% vs 16.4% base   (n=139)
                                  20d hit 24.1% vs 26.1% base   (n=133)

Neither is a finding. A permutation test settles it: draw 35 random buckets
of n=80 from the same outcome pool and take the best, and that best reaches
26.2% in 32.3% of trials. On the horizon that was searched, SHORT-GAMMA is
what looking at thirty-five things produces. The 20-day "replication" is not
independent evidence either — the 20-day window CONTAINS the 10-day window,
so the two are one result viewed twice.

That is the whole problem with a retrospective screen: it cannot distinguish
a real edge from the best of thirty-five coin flips, and no amount of further
retrospective analysis on the same rows can. Only new rows can.

So these are registered instead of acted on. A prediction written down before
its data arrives pays no multiple-comparison tax, because there was no search
— there is one predicate, one direction, one horizon, fixed in advance and
diffable in git.

WHAT THIS MODULE WILL AND WILL NOT SAY
--------------------------------------
It will report the out-of-sample record and nothing else until MIN_OOS_EPISODES
of it exist. Before then it prints the count and says "too early". It does not
score, weight, rank, or feed the calibration. If one of these earns its place,
promoting it is a separate, deliberate decision made against this record.

The in-sample numbers are carried alongside purely as the thing being tested.
They are not evidence and are labelled as such wherever they print.

HONEST LIMITS
-------------
  * SHORT-GAMMA is ~11% of scanned episodes, so MIN_OOS_EPISODES = 60 needs
    roughly 550 forward episodes. At the log's recent rate that is months.
    The gate is not adjustable downward to get an answer sooner; that would
    reintroduce exactly the problem it exists to prevent.
  * Dealer gamma is computed from prior-session open interest and cannot be
    reconstructed for a past date, so the pre-registration sample can only
    grow forward. Nothing here is backfillable.
  * Both predicates read columns the scanner already logs. Neither adds a
    fetch, and neither changes a score.

    python hypotheses.py            # current record for every hypothesis
"""

from datetime import date

# Out-of-sample episodes required before a verdict is offered at all.
# 60 gives a hit-rate standard error near 5 points against a ~16% base rate,
# which is the smallest sample where a claimed 10-point edge is separable
# from noise at all. It is a floor, not a target.
MIN_OOS_EPISODES = 60

# "Winner" — same definition the calibration uses, so a hypothesis that earns
# promotion can be evaluated against the same target it would be scored on.
WIN_PCT = 15.0
HORIZON = "return_10d"


def _f(v):
    if isinstance(v, (int, float)):
        return float(v)
    v = (v or "").strip() if isinstance(v, str) else v
    try:
        return float(v) if v not in ("", "None", "nan", None) else None
    except (TypeError, ValueError):
        return None


def _s(r, k):
    v = r.get(k)
    return v.strip() if isinstance(v, str) else ("" if v is None else str(v))


# ─────────────────────────────────────────────
# THE REGISTER
# ─────────────────────────────────────────────
# Each entry is frozen on `registered`. Changing a predicate, direction or
# horizon after that date invalidates the test — register a new hypothesis
# with a new id instead, and leave the old one standing with its record.

HYPOTHESES = [
    {
        "id":         "H1-short-gamma",
        "registered": date(2026, 8, 30),
        "title":      "Short dealer gamma marks amplification, not exhaustion",
        "predicate":  lambda r: _s(r, "gex_regime").startswith("SHORT-GAMMA"),
        "direction":  "+",
        "claim":      ("Episodes scanned while net dealer gamma is negative "
                       "beat the base 10-day hit rate. Mechanism: dealers "
                       "hedge WITH the move rather than against it, so the "
                       "same short-covering flow travels further."),
        "in_sample":  {"n": 80, "hit": 0.262, "base": 0.164,
                       "note": "found by searching ~35 buckets; "
                               "best-of-35 random reaches this 32.3% of the time"},
    },
    {
        "id":         "H2-volume-already-spiked",
        "registered": date(2026, 8, 30),
        "title":      "A spiked volume denominator means the move already happened",
        "predicate":  lambda r: (_f(r.get("dtc_spike_ratio")) or 0) >= 1.24,
        "direction":  "-",
        "claim":      ("Episodes where the 10-session mean volume sits 1.24x "
                       "or more above its median UNDERperform. Mechanism: the "
                       "ratio is high precisely when recent volume was "
                       "concentrated in a few outlier sessions, i.e. the "
                       "repricing is behind you. This is an exclusion, not an "
                       "entry."),
        "in_sample":  {"n": 139, "hit": 0.122, "base": 0.164,
                       "note": "not a gradient — quintiles 1-4 are flat "
                               "(15.8-20.6%), only the top quintile is penalised"},
    },
]


# ─────────────────────────────────────────────
# EVALUATION
# ─────────────────────────────────────────────

def _scan_date(r):
    try:
        return date.fromisoformat(_s(r, "scan_timestamp")[:10])
    except ValueError:
        return None


def evaluate(rows, hypothesis: dict) -> dict:
    """Out-of-sample record for one hypothesis.

    `rows` must already be episode-deduplicated and unit-normalized — pass
    learning_engine._episode_dedupe(learning_engine._graded_squeeze_rows()).
    Rows scanned before the registration date are reported separately and
    never counted toward the verdict.
    """
    reg = hypothesis["registered"]
    out = {"id": hypothesis["id"], "registered": reg.isoformat(),
           "oos_n": 0, "oos_hit": None, "oos_base": None, "oos_delta": None,
           "pre_n": 0, "verdict": "", "eligible": 0}

    pre, oos, oos_all = [], [], []
    for r in rows:
        d = _scan_date(r)
        y = _f(r.get(HORIZON))
        if d is None or y is None:
            continue
        if d < reg:
            if hypothesis["predicate"](r):
                pre.append(y)
            continue
        oos_all.append(y)
        if hypothesis["predicate"](r):
            oos.append(y)

    out["pre_n"] = len(pre)
    out["eligible"] = len(oos_all)
    out["oos_n"] = len(oos)
    if not oos or not oos_all:
        out["verdict"] = (f"no out-of-sample episodes yet "
                          f"({len(oos_all)} scanned since {reg})")
        return out

    hit = sum(1 for y in oos if y > WIN_PCT) / len(oos)
    base = sum(1 for y in oos_all if y > WIN_PCT) / len(oos_all)
    out["oos_hit"], out["oos_base"] = hit, base
    out["oos_delta"] = hit - base

    if len(oos) < MIN_OOS_EPISODES:
        out["verdict"] = (f"too early — {len(oos)}/{MIN_OOS_EPISODES} "
                          f"out-of-sample episodes")
        return out

    want = 1 if hypothesis["direction"] == "+" else -1
    moved = (hit - base) * want
    # One standard error on the hypothesis bucket. A delta inside it is not
    # a result in either direction.
    se = (base * (1 - base) / len(oos)) ** 0.5
    # `moved` is signed RELATIVE TO THE PREDICTION: positive means the bucket
    # went the way the hypothesis said, whichever way that was. Spelling that
    # out in the text matters — "no effect (+1.7 SE)" on its own reads like a
    # contradiction when the direction was right and only the size fell short.
    sigma = moved / se
    if sigma > 2:
        out["verdict"] = f"HOLDS out-of-sample (+{sigma:.1f} SE as predicted)"
    elif sigma < -2:
        out["verdict"] = f"REVERSED out-of-sample ({sigma:.1f} SE against prediction)"
    elif sigma >= 0:
        out["verdict"] = (f"inconclusive — moved as predicted but only "
                          f"{sigma:.1f} SE (needs 2.0)")
    else:
        out["verdict"] = (f"inconclusive — moved against the prediction by "
                          f"{-sigma:.1f} SE (needs 2.0 to call it reversed)")
    return out


def evaluate_all(rows=None) -> list:
    if rows is None:
        import learning_engine as le
        rows = le._episode_dedupe(le._graded_squeeze_rows())
    return [evaluate(rows, h) for h in HYPOTHESES]


# ─────────────────────────────────────────────
# PER-CANDIDATE TAGS (for the top-3 writeup)
# ─────────────────────────────────────────────

def tags_for(candidate: dict) -> list:
    """Which registered hypotheses this candidate triggers, right now.

    `candidate` is a squeeze_log-shaped dict or anything with the same keys.
    Returns display strings. These are OBSERVATIONS being recorded, not
    recommendations — the wording says so, because a line in a writeup that
    reads like a call will be used as one.
    """
    out = []
    for h in HYPOTHESES:
        try:
            if h["predicate"](candidate):
                arrow = "bullish" if h["direction"] == "+" else "bearish"
                out.append(f"{h['id']} ({arrow}, unproven) — {h['title']}")
        except Exception:
            continue
    return out


def format_block(rows=None, indent="  ") -> str:
    """The register and its current record, for the scan report."""
    res = evaluate_all(rows)
    L = [f"{indent}DECLARED HYPOTHESES — out-of-sample record",
         f"{indent}  (registered predictions; in-sample numbers are the claim, "
         f"not evidence)"]
    for h, r in zip(HYPOTHESES, res):
        L.append("")
        L.append(f"{indent}  {h['id']}  registered {r['registered']}  "
                 f"[{h['direction']}]")
        L.append(f"{indent}     {h['title']}")
        ins = h["in_sample"]
        L.append(f"{indent}     in-sample   n={ins['n']:<5} hit {ins['hit']:.1%} "
                 f"vs {ins['base']:.1%} base   ({ins['note']})")
        if r["oos_hit"] is None:
            L.append(f"{indent}     out-of-sample  {r['verdict']}")
        else:
            L.append(f"{indent}     out-of-sample  n={r['oos_n']:<5} "
                     f"hit {r['oos_hit']:.1%} vs {r['oos_base']:.1%} base   "
                     f"({r['oos_delta']:+.1%})")
            L.append(f"{indent}     verdict     {r['verdict']}")
    L.append("")
    L.append(f"{indent}  Nothing here is scored or weighted. Promotion is a "
             f"separate decision")
    L.append(f"{indent}  made against this record, not against the in-sample "
             f"numbers above.")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    print()
    print(format_block())
