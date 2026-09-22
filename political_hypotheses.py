"""
political_hypotheses.py
=======================
Declared, dated predictions about disclosure data — registered before the
rows that will judge them exist, and evaluated only on disclosures notified
after the registration date.

WHY THIS IS A SEPARATE REGISTER
-------------------------------
`hypotheses.py` does the same job for the squeeze log and is the reason this
file exists at all. It cannot simply be extended, because the two datasets
disagree about what every column means: the squeeze log keys on
`scan_timestamp` and grades `return_10d` in percent against a 15% win bar,
while a disclosure keys on `notify_date` and grades a fraction against zero.
Bending one register to cover both would make the predicates unreadable and
the win definition ambiguous, which is the opposite of the point.

The contract is identical and deliberately so: an entry is frozen on its
registration date, changing a predicate invalidates the test, and nothing
here is scored, weighted, or fed back into anything.

WHY THE REGISTER MATTERS MORE HERE THAN ANYWHERE ELSE
-----------------------------------------------------
The disclosure store holds 760 distinct filers. Rank them by forward return
and someone finishes first every single time, at every horizon, on both
sides of the book. `political_engine.permutation_best_p` exists to say how
often noise alone would produce a leader that good, and on a first look it
usually says "often".

A permutation test protects a ranking. It does not protect a predicate that
was chosen after looking at the data — for that there is only this file and
the git history that timestamps it.

HOW THESE WERE CHOSEN
---------------------
H1 and H2 are mechanistic. They were written from how the filings work, not
from anything measured on this store, and they carry no in-sample numbers
because none were looked at before registering them.

H3 is not. It was observed on the 2026-09-20 backfill and is registered here
so that it has to survive on rows that arrive afterwards. Its in-sample
figures are recorded as the claim being tested, not as evidence for it —
same treatment `hypotheses.py` gives H1-short-gamma.

    python political_hypotheses.py
"""

from datetime import date


# A disclosure is a "win" if the stock moved the filer's way. Unlike the
# squeeze register there is no magnitude bar: a congressional purchase is
# not trying to catch a 15% move, and imposing one would silently turn a
# question about direction into a question about volatility.
WIN = 0.0

# Which anchor a verdict is computed on. The trade date is what the filer
# got; only the notify date was ever available to anyone else, so it is the
# only one a prediction about a tradeable edge may be judged on.
ANCHOR = "notify"
HORIZON = 20

# Out-of-sample disclosures required before a verdict is offered. Against a
# base rate near 50% a 10-point claimed edge needs roughly this many rows
# before it separates from noise at two standard errors. It is a floor, and
# it is not adjustable downward to get an answer sooner.
MIN_OOS_ROWS = 150


def _ret(row) -> float:
    return row.get("ret")


def _flags(row) -> list:
    import json
    try:
        return json.loads(row.get("parse_flags") or "[]")
    except Exception:
        return []


def _committees(row) -> list:
    import json
    try:
        return json.loads(row.get("committees") or "[]")
    except Exception:
        return []


# Committees with direct jurisdiction over listed companies and markets.
#
# The list is deliberately short. Two wider versions were measured against
# the 2026-09-20 store BEFORE any return was looked at, purely to check that
# the predicate splits the population at all:
#
#     "sits on any committee"                     415/423 rows   98%
#     wide (adds Armed Services, Agriculture,
#           Small Business, Science)               72/81  rows   89%
#     this list                                     4/81  rows    5%
#
# A predicate that selects 89% of the sample is not a test of anything, so
# the narrow list is the only one that can carry the hypothesis. Choosing it
# for balance is legitimate; choosing it for its returns would not be, and
# no return was consulted. Subcommittees are skipped so a member counts once
# rather than once per panel.
#
# At roughly 5% of congressional rows this will take months to reach
# MIN_OOS_ROWS. That is the correct behaviour, not a problem to tune around.
MARKET_COMMITTEES = (
    "financial services", "banking", "energy and commerce",
    "ways and means", "finance",
)


def _on_market_committee(row) -> bool:
    for c in _committees(row):
        if " - " in c:
            continue                    # subcommittee; parent already counted
        low = c.lower()
        if any(k in low for k in MARKET_COMMITTEES):
            return True
    return False


# ─────────────────────────────────────────────
# THE REGISTER
# ─────────────────────────────────────────────

HYPOTHESES = [
    {
        "id": "P1-insider-open-market-buy",
        "registered": date(2026, 9, 20),
        "title": "Open-market insider purchases beat the pooled base rate",
        "sources": ("form4",),
        "predicate": lambda r: (r.get("source") == "form4"
                                and r.get("txn_type") == "P"),
        "direction": "+",
        "claim": ("A corporate officer or director buying their own stock on "
                  "the open market, with their own money, discloses it within "
                  "two business days — roughly twenty times fresher than a "
                  "congressional PTR and with an exact size rather than a "
                  "bucket. Mechanism: unlike a grant or an exercise, an "
                  "open-market purchase is discretionary and costly, so it "
                  "is the one insider event that carries a view. Grants (A), "
                  "tax withholding (F) and exercises (M) are excluded at "
                  "parse time for exactly this reason. Note the scarcity: on "
                  "the 2026-09-20 store only 126 of 3,578 parsed Form 4 rows "
                  "were purchases — insiders sell roughly twenty-seven times "
                  "more often than they buy, which is why this sample builds "
                  "slowly and why the sell side is a different question."),
        "in_sample": None,
    },
    {
        "id": "P2-committee-jurisdiction",
        "registered": date(2026, 9, 20),
        "title": "Market-jurisdiction committee members do no better",
        "sources": ("house_ptr", "senate_ptr"),
        "predicate": _on_market_committee,
        "direction": "0",
        "claim": ("A member sitting on a committee whose jurisdiction touches "
                  "listed companies — Financial Services, Banking, Energy and "
                  "Commerce, Ways and Means, Finance, Armed Services, "
                  "Commerce, Agriculture, Small Business, Science — is not a "
                  "better trader than one who is not. Registered as a NULL "
                  "prediction, which is the honest shape of it: committee "
                  "strategies are the most heavily marketed slice of this "
                  "data, there are about twenty standing committees to test, "
                  "and twenty tests hand you a winner whether or not one "
                  "exists. Predicting no effect means a positive result has "
                  "to argue against the register rather than be read out of "
                  "it. The committee list is fixed here and any change to it "
                  "invalidates the test."),
        "in_sample": None,
    },
    {
        "id": "P3-delay-eats-the-edge",
        "registered": date(2026, 9, 20),
        "title": "The disclosure delay removes the filer's advantage",
        "sources": ("house_ptr", "senate_ptr"),
        "predicate": lambda r: r.get("source") in ("house_ptr", "senate_ptr"),
        "direction": "-",
        "claim": ("Congressional purchases measured from the day they became "
                  "public do worse than the pooled base rate. Mechanism: the "
                  "STOCK Act allows 30 to 45 days, measured here at 27.2 days "
                  "mean, and whatever the trade was worth has had that long "
                  "to be priced. THIS ONE WAS FOUND IN THE DATA, not derived "
                  "from mechanism, so the figures below are the claim and not "
                  "evidence for it."),
        "in_sample": {
            "n": 53, "mean_trade": 0.0441, "mean_notify": 0.0052,
            "note": ("10-day paired House rows, recomputed 2026-09-21. "
                     "The figures first recorded here (n=47, trade +5.47%, "
                     "notify -2.26%) were WRONG: they were anchored on the "
                     "House form's Notification Date, which is when the "
                     "FILER was told, not when the filing became public — "
                     "13.4 days too early on average. The predicate, "
                     "direction and horizon are unchanged, so the "
                     "registration stands; only this description of the "
                     "in-sample data is corrected. n=53 is far too small to "
                     "conclude anything and one month of filings is one "
                     "market regime. The measured public delay is 37.6 days."),
        },
    },
]


# ─────────────────────────────────────────────
# EVALUATION
# ─────────────────────────────────────────────

def _notified(row):
    try:
        return date.fromisoformat((row.get("notify_date") or "")[:10])
    except ValueError:
        return None


def evaluate(rows, hypothesis: dict) -> dict:
    """Out-of-sample record for one hypothesis.

    `rows` are graded disclosure dicts carrying `ret`, as
    political_engine._fetch_graded returns them. Rows notified before the
    registration date are counted separately and never reach the verdict.
    """
    reg = hypothesis["registered"]
    srcs = hypothesis.get("sources")
    out = {"id": hypothesis["id"], "registered": reg.isoformat(),
           "oos_n": 0, "oos_hit": None, "oos_base": None, "oos_delta": None,
           "pre_n": 0, "eligible": 0, "verdict": ""}

    pre, oos, pool = [], [], []
    for r in rows:
        if srcs and r.get("source") not in srcs:
            continue
        if (r.get("txn_type") or "") != "P":
            continue          # the register speaks about purchases only
        y = _ret(r)
        d = _notified(r)
        if y is None or d is None:
            continue
        if d < reg:
            if hypothesis["predicate"](r):
                pre.append(y)
            continue
        pool.append(y)
        if hypothesis["predicate"](r):
            oos.append(y)

    out["pre_n"] = len(pre)
    out["eligible"] = len(pool)
    out["oos_n"] = len(oos)
    if not oos or not pool:
        out["verdict"] = "no out-of-sample rows yet ({} notified since {})" \
            .format(len(pool), reg)
        return out

    hit = sum(1 for y in oos if y > WIN) / float(len(oos))
    base = sum(1 for y in pool if y > WIN) / float(len(pool))
    out["oos_hit"], out["oos_base"] = hit, base
    out["oos_delta"] = hit - base

    if len(oos) < MIN_OOS_ROWS:
        out["verdict"] = "too early — {}/{} out-of-sample rows".format(
            len(oos), MIN_OOS_ROWS)
        return out

    se = (base * (1 - base) / len(oos)) ** 0.5 or 1e-9
    want = {"+": 1, "-": -1, "0": 0}[hypothesis["direction"]]
    sigma = (hit - base) / se

    if want == 0:
        # A null prediction is confirmed by staying inside the band, so the
        # verdict has to be readable in both directions rather than treating
        # "no movement" as a failure to find something.
        if abs(sigma) < 2:
            out["verdict"] = "HOLDS — no effect, as predicted ({:+.1f} SE)" \
                .format(sigma)
        else:
            out["verdict"] = ("REJECTED — a real effect appeared ({:+.1f} SE); "
                              "this is the interesting outcome".format(sigma))
        return out

    moved = sigma * want
    if moved > 2:
        out["verdict"] = "HOLDS out-of-sample ({:+.1f} SE as predicted)" \
            .format(moved)
    elif moved < -2:
        out["verdict"] = "REVERSED out-of-sample ({:.1f} SE against)".format(moved)
    elif moved >= 0:
        out["verdict"] = ("inconclusive — moved as predicted but only "
                          "{:.1f} SE (needs 2.0)".format(moved))
    else:
        out["verdict"] = ("inconclusive — moved against the prediction by "
                          "{:.1f} SE (needs 2.0 to call it reversed)"
                          .format(-moved))
    return out


def evaluate_all(rows=None, conn=None) -> list:
    if rows is None:
        import political_engine as pe
        own = conn is None
        conn = conn or pe.connect()
        try:
            rows = pe._fetch_graded(conn, HORIZON, ANCHOR)
        finally:
            if own:
                conn.close()
    return [evaluate(rows, h) for h in HYPOTHESES]


def format_block(rows=None, conn=None, indent="  ") -> str:
    res = evaluate_all(rows, conn)
    L = ["{}DECLARED HYPOTHESES — {}-day, from the {} date".format(
            indent, HORIZON, ANCHOR),
         "{}  (registered predictions; in-sample numbers are the claim, "
         "not evidence)".format(indent)]
    for h, r in zip(HYPOTHESES, res):
        L.append("")
        L.append("{}  {}  registered {}  [{}]".format(
            indent, h["id"], r["registered"], h["direction"]))
        L.append("{}     {}".format(indent, h["title"]))
        ins = h.get("in_sample")
        if ins:
            L.append("{}     in-sample   n={}  trade {:+.2%}  notify {:+.2%}"
                     .format(indent, ins["n"], ins["mean_trade"],
                             ins["mean_notify"]))
            L.append("{}                 {}".format(indent, ins["note"]))
        else:
            L.append("{}     in-sample   none claimed — registered from "
                     "mechanism".format(indent))
        if r["oos_hit"] is None:
            L.append("{}     out-of-sample  {}".format(indent, r["verdict"]))
        else:
            L.append("{}     out-of-sample  n={}  hit {:.1%} vs {:.1%} base "
                     "({:+.1%})".format(indent, r["oos_n"], r["oos_hit"],
                                        r["oos_base"], r["oos_delta"]))
            L.append("{}     verdict     {}".format(indent, r["verdict"]))
    L.append("")
    L.append("{}  Nothing here is scored, weighted, or fed to anything. "
             "Promotion is".format(indent))
    L.append("{}  a separate decision made against this record.".format(indent))
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    print()
    print(format_block())
