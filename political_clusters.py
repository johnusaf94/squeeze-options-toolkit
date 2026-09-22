"""
political_clusters.py
=====================
Which names several INDEPENDENT filers bought at about the same time, and a
visible score for sorting them. No GUI imports — runs from a terminal.

    python political_clusters.py                # current clusters
    python political_clusters.py movers 90      # most-bought / most-sold
    python political_clusters.py filer Pelosi
    python political_clusters.py ticker NVDA

WHY CLUSTERS AND NOT RANKINGS
-----------------------------
One politician buying a stock is one person's opinion arriving five weeks
late. `political_engine` measures what that is worth and the answer is
-1.23% at ten days. Ranking filers by return does not fix that; the
permutation test in the same module says the leader of any such ranking is
noise at this sample size.

Several unrelated people buying the same name inside the same window is a
different observation, and it is the one thing in this dataset that gets
stronger as more sources are added rather than weaker. So the tool ranks
NAMES by agreement, not PEOPLE by return.

THE COUNTING TRAP THIS MODULE EXISTS TO AVOID
---------------------------------------------
Rank tickers by row count and the board is:

    DELL   792 rows   0 buys / 792 sells   11 filers
    UTHR   238 rows   1 buy  / 237 sells    4 filers
    STX    192 rows   0 buys / 192 sells    7 filers

Every one of those is automated selling — vesting, 10b5-1 plans, a secondary.
It will top a row-count board every week forever and it means nothing. So
nothing here counts rows. Buys and sells are separate boards, the unit is
the DISTINCT FILER, and a filer who appears twenty times in one name counts
once.

THE SCORE IS A SORT KEY, NOT A PREDICTION
-----------------------------------------
Same convention as the value meter in `value_analysis_gui.py`: the score is
made of named parts, each part's weight is printed next to it, and the
weights are judgement. Nothing in it has been tested against what prices did
next, and `political_hypotheses.py` is where a claim about that would have
to be registered before it could be believed. A high score means "several
independent people bought this recently", which is a fact about the filings.
It does not mean the stock goes up.
"""

import json
import math
from datetime import date, datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

import political_engine as pe


# ─────────────────────────────────────────────
# SCORE COMPONENTS
# ─────────────────────────────────────────────
# Each returns 0-100. The weights below are judgement and are printed beside
# every score the tool shows, so a reader can disagree with a specific number
# rather than with the whole thing.

WEIGHTS = {
    "agreement": 0.40,     # how many independent filers
    "diversity": 0.25,     # how many different KINDS of filer
    "recency":   0.20,     # how fresh the newest disclosure is
    "size":      0.10,     # how much money the largest one moved
    "instrument": 0.05,    # dated options say more than shares
}

COMPONENT_NOTE = {
    "agreement": "filers, shrunk by how many names each bought",
    "diversity": "how many source families agree",
    "recency":   "age of the newest disclosure",
    "size":      "largest disclosed amount",
    "instrument": "any dated options",
}


def _agreement(n_filers: float) -> float:
    """Saturating, because the eighth buyer adds less than the second.

    Two independent buyers is the whole difference between an anecdote and
    a pattern, so the curve is steep at the start and flat after about six.
    """
    if n_filers <= 1:
        return 0.0
    return min(100.0, 100.0 * (1.0 - 0.72 ** (n_filers - 1)))


def _selectivity(breadth: int) -> float:
    """How much one filer's buy is worth, given how many names they bought.

    A fund holding 1,227 positions adding one more is not an opinion about
    that company; it is what an index-like book does every quarter. An
    insider who bought exactly one stock in the window made a choice. Both
    arrive as a row and counting them the same is what makes a cluster of
    large funds look like agreement.

    So each filer is shrunk by 1/sqrt(breadth): one name keeps full weight,
    ten names is worth 0.32, a hundred is 0.10, and Bridgewater's 1,227 is
    0.03. The curve is the standard root-n shrinkage rather than anything
    fitted — there is no forward-return evidence behind it and picking a
    steeper one to make a favourite name score higher would be exactly the
    thing this module is supposed to refuse.
    """
    return 1.0 / math.sqrt(max(int(breadth or 1), 1))


def _diversity(n_sources: int) -> float:
    """A congressman and a fund manager agreeing is rarer than two funds."""
    return {0: 0.0, 1: 0.0, 2: 60.0, 3: 88.0, 4: 100.0}.get(
        min(n_sources, 4), 0.0)


def _recency(newest: Optional[str], window_days: int) -> float:
    if not newest:
        return 0.0
    try:
        age = (date.today() - date.fromisoformat(newest[:10])).days
    except ValueError:
        return 0.0
    if age <= 0:
        return 100.0
    return max(0.0, 100.0 * (1.0 - float(age) / max(window_days, 1)))


def _size(amount_lo: Optional[float]) -> float:
    """Log scale over the disclosed bucket floors.

    The buckets run $1,001 / $15,001 / $50,001 / $100,001 / $250,001 / $1M+,
    so they are already roughly logarithmic and this just maps them onto a
    line. A Form 4 carries an exact figure rather than a bucket and lands on
    the same scale.
    """
    if not amount_lo or amount_lo <= 0:
        return 0.0
    import math
    lo = math.log10(max(amount_lo, 1000.0))
    return max(0.0, min(100.0, (lo - 3.0) / 3.0 * 100.0))


def _instrument(has_option: bool) -> float:
    return 100.0 if has_option else 0.0


# ─────────────────────────────────────────────
# CLUSTERS
# ─────────────────────────────────────────────

_BUY = "P"


def _is_buy(t: Optional[str]) -> bool:
    return (t or "") == _BUY


def _is_sell(t: Optional[str]) -> bool:
    return (t or "").startswith("S")


def clusters(conn=None, days: int = 90, min_filers: int = 2,
             sources: Optional[Tuple[str, ...]] = None,
             direction: str = "P", limit: int = 80,
             sector: Optional[str] = None) -> List[dict]:
    """Names with `min_filers` or more distinct filers on one side.

    `direction` is "P" for buying or "S" for selling. The sell side is
    computed the same way and is worth looking at, but read it knowing that
    most insider selling is a calendar rather than a view.
    """
    own = conn is None
    conn = conn or pe.connect()
    try:
        since = (date.today() - timedelta(days=days)).isoformat()
        sql = ("SELECT id, source, filer_name, filer_id, chamber, party, "
               "committees, ticker, asset_name, asset_type, txn_type, owner, "
               "trade_date, notify_date, amount_lo, amount_hi, shares, price, "
               "option_type, strike, expiry, doc_url, parse_flags "
               "FROM disclosures WHERE ticker IS NOT NULL AND ticker != '' "
               "AND notify_date >= ?")
        params = [since]
        if sources:
            sql += " AND source IN ({})".format(",".join("?" * len(sources)))
            params += list(sources)
        rows = [dict(r) for r in conn.execute(sql, params)]
        # Sector is applied to the ROWS, before anything is counted, so a
        # cluster's filer count means "people who bought this name in this
        # sector" rather than a total quietly filtered afterwards.
        keep_t = pe.sector_tickers(conn, sector)
        if keep_t is not None:
            rows = [r for r in rows if r["ticker"] in keep_t]
    finally:
        if own:
            conn.close()

    # "BOTH" keeps each side separate inside one cluster rather than
    # merging them, because a name five people bought and five people sold
    # is a different object from one five people bought — and a mode that
    # adds them up would render those two identically.
    if direction == "BOTH":
        keep = lambda t: _is_buy(t) or _is_sell(t)          # noqa: E731
    else:
        keep = _is_buy if direction == "P" else _is_sell
    by_ticker: Dict[str, List[dict]] = {}
    breadth: Dict[str, set] = {}
    for r in rows:
        if keep(r.get("txn_type")):
            by_ticker.setdefault(r["ticker"], []).append(r)
            breadth.setdefault(r.get("filer_name") or "?", set()).add(
                r["ticker"])
    # How many DIFFERENT names each filer touched in this window. A buy from
    # someone who bought four things is evidence; the same row from someone
    # who bought four hundred is a rebalance.
    n_breadth = {k: len(v) for k, v in breadth.items()}

    out = []
    for ticker, rs in by_ticker.items():
        # A filer who appears twenty times in one name is still one filer.
        # Without this DELL's eleven sellers and its 792 rows would look the
        # same as eleven separate decisions.
        filers = {}
        for r in rs:
            f = r.get("filer_name") or "?"
            cur = filers.get(f)
            if cur is None or (r.get("notify_date") or "") > (cur.get("notify_date") or ""):
                filers[f] = r
        if len(filers) < min_filers:
            continue

        # Display rows. In the combined view someone who bought AND sold the
        # same name is two facts, not one, and keying only by name kept
        # whichever happened to be more recent — so a filer who sold in July
        # and bought in September showed up as a buyer and their sale
        # vanished. `filers` still keys by name because that is what a
        # "distinct filer" count has to mean; `entries` is what gets drawn.
        if direction == "BOTH":
            ent = {}
            for r in rs:
                k = (r.get("filer_name") or "?",
                     "P" if _is_buy(r.get("txn_type")) else "S")
                cur = ent.get(k)
                if cur is None or (r.get("notify_date") or "") > (
                        cur.get("notify_date") or ""):
                    ent[k] = r
            entries = list(ent.values())
        else:
            entries = list(filers.values())

        eff = sum(_selectivity(n_breadth.get(f, 1)) for f in filers)

        # Both sides, counted separately and weighted the same way, so the
        # lean is a comparison of like with like.
        buyers = {r["filer_name"] for r in rs if _is_buy(r.get("txn_type"))}
        sellers = {r["filer_name"] for r in rs if _is_sell(r.get("txn_type"))}
        eff_b = sum(_selectivity(n_breadth.get(f, 1)) for f in buyers)
        eff_s = sum(_selectivity(n_breadth.get(f, 1)) for f in sellers)
        # +1 everyone bought, -1 everyone sold, 0 evenly contested.
        lean = ((eff_b - eff_s) / (eff_b + eff_s)) if (eff_b + eff_s) else 0.0
        srcs = sorted({r["source"] for r in rs})
        newest = max((r.get("notify_date") or "") for r in rs)
        oldest = min((r.get("notify_date") or "") for r in rs)
        max_lo = max([r.get("amount_lo") or 0 for r in rs] or [0])
        has_opt = any(r.get("option_type") or r.get("strike") for r in rs)

        comp = {
            "agreement": _agreement(eff),
            "diversity": _diversity(len(srcs)),
            "recency": _recency(newest, days),
            "size": _size(max_lo),
            "instrument": _instrument(has_opt),
        }
        score = sum(comp[k] * WEIGHTS[k] for k in WEIGHTS)

        out.append({
            "ticker": ticker,
            "asset_name": (rs[0].get("asset_name") or "")[:90],
            "rows": rs,
            "filers": sorted(entries,
                             key=lambda r: r.get("notify_date") or "",
                             reverse=True),
            "filer_names": sorted(filers),
            "n_filers": len(filers),
            "n_effective": eff,
            "n_buyers": len(buyers), "n_sellers": len(sellers),
            "eff_buy": eff_b, "eff_sell": eff_s, "lean": lean,
            "contested": bool(buyers and sellers),
            "breadth": {f: n_breadth.get(f, 1) for f in filers},
            "n_rows": len(rs),
            "sources": srcs,
            "n_sources": len(srcs),
            "newest": newest, "oldest": oldest,
            "max_amount": max_lo,
            "has_option": has_opt,
            "components": comp,
            "score": score,
            "direction": direction,
        })

    out.sort(key=lambda d: -d["score"])
    return out[:limit]


def score_block(cluster: dict, indent: str = "  ") -> str:
    """The score, taken apart, with its weights showing."""
    c = cluster["components"]
    L = ["{}score {:.0f} / 100   (weights are judgement, untested against "
         "forward returns)".format(indent, cluster["score"])]
    for k in ("agreement", "diversity", "recency", "size", "instrument"):
        L.append("{}  {:<11} {:>5.0f} x {:.2f} = {:>5.1f}   {}".format(
            indent, k, c[k], WEIGHTS[k], c[k] * WEIGHTS[k],
            COMPONENT_NOTE[k]))
    return "\n".join(L)


# ─────────────────────────────────────────────
# GRAPH — the bipartite filer/ticker network
# ─────────────────────────────────────────────

SOURCE_COLOR = {
    "house_ptr": "#F4C430",     # amber
    "senate_ptr": "#F5A97F",    # orange
    "form4": "#94E2D5",         # teal
    "f13": "#A78BFA",           # violet
}


def graph(cluster_list: List[dict], max_nodes: int = 300,
          caps: Optional[dict] = None, fund_values: Optional[dict] = None
          ) -> dict:
    """Nodes and edges for the cluster view.

    The data is genuinely bipartite — filers on one side, tickers on the
    other, a disclosure as the edge between them — so the network is not a
    decoration over a table. Two tickers sit near each other exactly when
    the same people bought both, which is the thing worth seeing and the
    thing a table cannot show.
    """
    caps = caps or {}
    fund_values = fund_values or {}
    nodes: Dict[str, dict] = {}
    edges: List[dict] = []

    for cl in cluster_list:
        tid = "T:" + cl["ticker"]
        # Money on the ticker is the sum of what was actually disclosed into
        # it, so a name three people put $40M behind draws bigger than one
        # three people put $4,000 behind even though the filer count is the
        # same. Rows with no figure at all are counted separately rather
        # than as zero — see `amount_known` below.
        amt = sum(r.get("amount_lo") or 0 for r in cl["rows"])
        known = sum(1 for r in cl["rows"] if r.get("amount_lo"))
        if tid not in nodes:
            nodes[tid] = {
                "id": tid, "kind": "ticker", "label": cl["ticker"],
                "score": cl["score"], "n_filers": cl["n_filers"],
                "n_effective": cl.get("n_effective", cl["n_filers"]),
                "sources": cl["sources"], "cluster": cl,
                "lean": cl.get("lean"), "contested": cl.get("contested"),
                "direction": cl.get("direction"),
                "weight": cl["n_filers"], "amount": amt,
                # How big the COMPANY is, which is what an orb should show.
                # `amount` is how much happened to be traded in it and is
                # kept for the panel, not for the radius.
                "market_cap": caps.get(cl["ticker"].upper()),
                "amount_known": known, "amount_rows": len(cl["rows"]),
            }
        for r in cl["filers"]:
            fname = r.get("filer_name") or "?"
            fid = "F:" + fname
            if fid not in nodes:
                nodes[fid] = {
                    "id": fid, "kind": "filer", "label": fname,
                    "source": r.get("source"), "chamber": r.get("chamber"),
                    "party": r.get("party"), "weight": 0, "cluster": None,
                    "amount": 0.0, "amount_known": 0,
                    # An institution's size is the book it runs, not what it
                    # happened to move in the names on screen.
                    "book": fund_values.get(fname),
                }
            nodes[fid]["weight"] += 1
            ra = r.get("amount_lo") or 0
            nodes[fid]["amount"] += ra
            nodes[fid]["amount_known"] += 1 if ra else 0
            edges.append({
                "a": fid, "b": tid, "source": r.get("source"),
                "amount": ra,
                "notify": r.get("notify_date"),
                "option": bool(r.get("option_type") or r.get("strike")),
                "buy": _is_buy(r.get("txn_type")),
                "estimated": "amount_estimated" in (
                    r.get("parse_flags") if isinstance(r.get("parse_flags"), list)
                    else json.loads(r.get("parse_flags") or "[]")),
                "row": r,
            })

    if len(nodes) > max_nodes:
        # Trim the least connected filers first, then the lowest clusters.
        # A node budget is about frame rate; dropping the periphery keeps
        # the shape of the thing that matters.
        filers = sorted([n for n in nodes.values() if n["kind"] == "filer"],
                        key=lambda n: n["weight"])
        drop = set()
        over = len(nodes) - max_nodes
        for n in filers:
            if over <= 0:
                break
            if n["weight"] <= 1:
                drop.add(n["id"])
                over -= 1
        nodes = {k: v for k, v in nodes.items() if k not in drop}
        edges = [e for e in edges if e["a"] in nodes and e["b"] in nodes]

    return {"nodes": list(nodes.values()), "edges": edges,
            "trimmed": max(0, len(cluster_list) and 0)}


# ─────────────────────────────────────────────
# MOVERS — most bought / most sold, counted correctly
# ─────────────────────────────────────────────

def movers(conn=None, days: int = 90, direction: str = "P",
           sources: Optional[Tuple[str, ...]] = None,
           limit: int = 40, sector: Optional[str] = None) -> List[dict]:
    """Ranked by distinct filers, never by row count.

    Also reports the other side, because a name with nine buyers and forty
    sellers is not the same story as one with nine buyers and none, and a
    one-sided board hides that completely.
    """
    own = conn is None
    conn = conn or pe.connect()
    try:
        since = (date.today() - timedelta(days=days)).isoformat()
        sql = ("SELECT ticker, source, filer_name, txn_type, amount_lo, "
               "notify_date, option_type, strike, asset_name "
               "FROM disclosures WHERE ticker IS NOT NULL AND ticker != '' "
               "AND notify_date >= ?")
        params = [since]
        if sources:
            sql += " AND source IN ({})".format(",".join("?" * len(sources)))
            params += list(sources)
        rows = [dict(r) for r in conn.execute(sql, params)]

        keep_t = pe.sector_tickers(conn, sector)
        if keep_t is not None:
            rows = [r for r in rows if r["ticker"] in keep_t]

        first_seen = {}
        for r in conn.execute(
                "SELECT ticker, MIN(notify_date) f FROM disclosures "
                "WHERE ticker IS NOT NULL GROUP BY ticker"):
            first_seen[r["ticker"]] = r["f"]
    finally:
        if own:
            conn.close()

    breadth: Dict[str, set] = {}
    for r in rows:
        if _is_buy(r["txn_type"]) or _is_sell(r["txn_type"]):
            breadth.setdefault(r["filer_name"] or "?", set()).add(r["ticker"])
    n_breadth = {k: len(v) for k, v in breadth.items()}

    agg: Dict[str, dict] = {}
    for r in rows:
        t = r["ticker"]
        a = agg.setdefault(t, {
            "ticker": t, "asset_name": (r.get("asset_name") or "")[:70],
            "buyers": set(), "sellers": set(), "buy_rows": 0, "sell_rows": 0,
            "sources": set(), "max_amount": 0.0, "newest": "",
            "has_option": False,
        })
        if _is_buy(r["txn_type"]):
            a["buyers"].add(r["filer_name"])
            a["buy_rows"] += 1
        elif _is_sell(r["txn_type"]):
            a["sellers"].add(r["filer_name"])
            a["sell_rows"] += 1
        else:
            continue
        a["sources"].add(r["source"])
        a["max_amount"] = max(a["max_amount"], r.get("amount_lo") or 0)
        a["newest"] = max(a["newest"], r.get("notify_date") or "")
        if r.get("option_type") or r.get("strike"):
            a["has_option"] = True

    out = []
    for t, a in agg.items():
        side = a["buyers"] if direction == "P" else a["sellers"]
        if not side:
            continue
        eff = sum(_selectivity(n_breadth.get(f, 1)) for f in side)
        comp = {
            "agreement": _agreement(eff),
            "diversity": _diversity(len(a["sources"])),
            "recency": _recency(a["newest"], days),
            "size": _size(a["max_amount"]),
            "instrument": _instrument(a["has_option"]),
        }
        fs = first_seen.get(t)
        out.append({
            "ticker": t, "asset_name": a["asset_name"],
            "n_buyers": len(a["buyers"]), "n_sellers": len(a["sellers"]),
            "n_effective": eff,
            "buy_rows": a["buy_rows"], "sell_rows": a["sell_rows"],
            "sources": sorted(a["sources"]), "newest": a["newest"],
            "max_amount": a["max_amount"], "has_option": a["has_option"],
            "components": comp,
            "score": sum(comp[k] * WEIGHTS[k] for k in WEIGHTS),
            "is_new": bool(fs and fs >= (date.today()
                                         - timedelta(days=days)).isoformat()),
            "direction": direction,
        })
    out.sort(key=lambda d: (-(d["n_buyers"] if direction == "P"
                              else d["n_sellers"]), -d["score"]))
    return out[:limit]


# ─────────────────────────────────────────────
# ONE FILER
# ─────────────────────────────────────────────

def filer_profile(conn=None, name: str = "", days: Optional[int] = None
                  ) -> dict:
    """Everything one person or fund has disclosed, and what it did next.

    The forward returns here carry n and nothing else — no rank, no
    comparison to other filers. A per-filer number IS the thing the
    permutation test says is noise at this sample size, so presenting it
    beside a leaderboard position would be presenting it as the opposite of
    what it is.
    """
    own = conn is None
    conn = conn or pe.connect()
    try:
        sql = ("SELECT * FROM disclosures WHERE filer_name LIKE ?")
        params = ["%{}%".format(name)]
        if days:
            sql += " AND notify_date >= ?"
            params.append((date.today() - timedelta(days=days)).isoformat())
        sql += " ORDER BY notify_date DESC"
        rows = [dict(r) for r in conn.execute(sql, params)]
    finally:
        if own:
            conn.close()
    if not rows:
        return {}

    names = sorted({r["filer_name"] for r in rows if r.get("filer_name")})
    buys = [r for r in rows if _is_buy(r["txn_type"])]
    sells = [r for r in rows if _is_sell(r["txn_type"])]
    lags = []
    for r in rows:
        try:
            lags.append((date.fromisoformat(r["notify_date"])
                         - date.fromisoformat(r["trade_date"])).days)
        except Exception:
            pass
    lags = [l for l in lags if l >= 0]

    tick = {}
    for r in rows:
        t = r.get("ticker")
        if not t:
            continue
        d = tick.setdefault(t, {"ticker": t, "buys": 0, "sells": 0,
                                "newest": "", "max_amount": 0.0})
        if _is_buy(r["txn_type"]):
            d["buys"] += 1
        elif _is_sell(r["txn_type"]):
            d["sells"] += 1
        d["newest"] = max(d["newest"], r.get("notify_date") or "")
        d["max_amount"] = max(d["max_amount"], r.get("amount_lo") or 0)

    perf = {}
    for h in pe.HORIZONS:
        for a in pe.ANCHORS:
            col = "ret_{}d_{}".format(h, a)
            vals = [r[col] for r in buys if r.get(col) is not None]
            if vals:
                perf["{}d_{}".format(h, a)] = {
                    "n": len(vals), "mean": sum(vals) / len(vals),
                    "hit": sum(1 for v in vals if v > 0) / float(len(vals)),
                }

    r0 = rows[0]
    return {
        "names": names, "rows": rows, "n": len(rows),
        "n_buys": len(buys), "n_sells": len(sells),
        "source": r0.get("source"), "chamber": r0.get("chamber"),
        "party": r0.get("party"), "state": r0.get("state"),
        "committees": json.loads(r0.get("committees") or "[]"),
        "mean_lag": (sum(lags) / float(len(lags))) if lags else None,
        "median_lag": sorted(lags)[len(lags) // 2] if lags else None,
        "options": sum(1 for r in rows if r.get("option_type") or r.get("strike")),
        "tickers": sorted(tick.values(),
                          key=lambda d: (-(d["buys"] + d["sells"]),
                                         d["ticker"])),
        "range": (rows[-1].get("notify_date"), rows[0].get("notify_date")),
        "perf": perf,
    }


def filer_list(conn=None, min_rows: int = 5) -> List[dict]:
    own = conn is None
    conn = conn or pe.connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT filer_name, source, chamber, party, state, COUNT(*) n, "
            "SUM(txn_type='P') buys, SUM(txn_type LIKE 'S%') sells, "
            "MAX(notify_date) newest FROM disclosures "
            "WHERE filer_name IS NOT NULL GROUP BY filer_name "
            "HAVING n >= ? ORDER BY n DESC", (min_rows,))]
    finally:
        if own:
            conn.close()


# ─────────────────────────────────────────────
# ONE TICKER
# ─────────────────────────────────────────────

def ticker_profile(conn=None, ticker: str = "", days: int = 365) -> dict:
    """A stock's whole political and insider footprint on one object."""
    own = conn is None
    conn = conn or pe.connect()
    try:
        t = (ticker or "").upper()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM disclosures WHERE ticker = ? AND notify_date >= ? "
            "ORDER BY notify_date DESC",
            (t, (date.today() - timedelta(days=days)).isoformat()))]
        inf = pe.influence_for(conn, t)
    finally:
        if own:
            conn.close()
    if not rows:
        return {"ticker": ticker.upper(), "rows": [], "influence": inf}

    buys = [r for r in rows if _is_buy(r["txn_type"])]
    sells = [r for r in rows if _is_sell(r["txn_type"])]
    by_source = {}
    for r in rows:
        s = by_source.setdefault(r["source"], {"buys": 0, "sells": 0,
                                               "filers": set()})
        if _is_buy(r["txn_type"]):
            s["buys"] += 1
        elif _is_sell(r["txn_type"]):
            s["sells"] += 1
        s["filers"].add(r.get("filer_name"))
    for s in by_source.values():
        s["filers"] = len(s["filers"])

    perf = {}
    for h in pe.HORIZONS:
        col = "ret_{}d_notify".format(h)
        vals = [r[col] for r in buys if r.get(col) is not None]
        if vals:
            perf["{}d".format(h)] = {"n": len(vals),
                                     "mean": sum(vals) / len(vals)}

    return {
        "ticker": (ticker or "").upper(),
        "asset_name": rows[0].get("asset_name"),
        "rows": rows, "n": len(rows),
        "buyers": sorted({r["filer_name"] for r in buys if r.get("filer_name")}),
        "sellers": sorted({r["filer_name"] for r in sells if r.get("filer_name")}),
        "n_buys": len(buys), "n_sells": len(sells),
        "by_source": by_source,
        "options": [r for r in rows if r.get("option_type") or r.get("strike")],
        "influence": inf,
        "perf": perf,
        "range": (rows[-1].get("notify_date"), rows[0].get("notify_date")),
    }


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def _amt(v):
    return "--" if not v else "${:,.0f}+".format(v)


def main(argv):
    cmd = argv[0] if argv else "clusters"
    conn = pe.connect()
    try:
        if cmd == "clusters":
            days = int(argv[1]) if len(argv) > 1 else 90
            cls = clusters(conn, days=days)
            print("\n{} clusters in the last {} days "
                  "(2+ independent buyers)\n".format(len(cls), days))
            print("  {:<7} {:>5} {:>10} {:>6}  {:<26} {}".format(
                "ticker", "score", "filers/eff", "srcs", "sources", "newest"))
            for c in cls[:25]:
                print("  {:<7} {:>5.0f} {:>4} / {:<5.1f} {:>4}  {:<26} {}".format(
                    c["ticker"], c["score"], c["n_filers"], c["n_effective"],
                    c["n_sources"],
                    ",".join(s.replace("_ptr", "") for s in c["sources"])[:26],
                    c["newest"]))
            if cls:
                print("\n" + score_block(cls[0]))
                print("\n  top cluster {} — who bought:".format(
                    cls[0]["ticker"]))
                for r in cls[0]["filers"][:8]:
                    print("    {:<30} {:<11} {:<11} {}".format(
                        (r.get("filer_name") or "")[:30], r.get("source"),
                        _amt(r.get("amount_lo")), r.get("notify_date")))

        elif cmd == "movers":
            days = int(argv[1]) if len(argv) > 1 else 90
            for d, lab in (("P", "MOST BOUGHT"), ("S", "MOST SOLD")):
                print("\n{} — last {} days, by DISTINCT FILERS".format(
                    lab, days))
                print("  {:<7} {:>7} {:>7} {:>6} {:>6}  {}".format(
                    "ticker", "buyers", "sellers", "brows", "srows", "new?"))
                for m in movers(conn, days=days, direction=d)[:15]:
                    print("  {:<7} {:>7} {:>7} {:>6} {:>6}  {}".format(
                        m["ticker"], m["n_buyers"], m["n_sellers"],
                        m["buy_rows"], m["sell_rows"],
                        "NEW" if m["is_new"] else ""))

        elif cmd == "filer":
            p = filer_profile(conn, " ".join(argv[1:]))
            if not p:
                print("no filer matched")
                return
            print("\n{}".format(" / ".join(p["names"])))
            print("  {} disclosures  ({} buys, {} sells)  {} .. {}".format(
                p["n"], p["n_buys"], p["n_sells"], p["range"][0],
                p["range"][1]))
            if p["mean_lag"] is not None:
                print("  disclosure lag  mean {:.1f}d  median {}d".format(
                    p["mean_lag"], p["median_lag"]))
            if p["committees"]:
                print("  committees: {}".format(
                    "; ".join(p["committees"][:3])))
            print("  most traded:")
            for t in p["tickers"][:10]:
                print("    {:<7} {}B / {}S   {}".format(
                    t["ticker"], t["buys"], t["sells"], t["newest"]))
            if p["perf"]:
                print("  forward returns on BUYS (n only — no rank, see "
                      "the permutation test):")
                for k, v in sorted(p["perf"].items()):
                    print("    {:<14} n={:<4} mean {:+.2%}  hit {:.0%}".format(
                        k, v["n"], v["mean"], v["hit"]))

        elif cmd == "ticker":
            p = ticker_profile(conn, argv[1] if len(argv) > 1 else "")
            if not p.get("rows"):
                print("no disclosures for that ticker")
                return
            print("\n{} — {}".format(p["ticker"], p["asset_name"]))
            print("  {} disclosures  ({} buys / {} sells)  {} .. {}".format(
                p["n"], p["n_buys"], p["n_sells"], p["range"][0],
                p["range"][1]))
            for s, d in sorted(p["by_source"].items()):
                print("    {:<12} {}B / {}S across {} filers".format(
                    s, d["buys"], d["sells"], d["filers"]))
            if p["buyers"]:
                print("  buyers: {}".format(", ".join(p["buyers"][:8])))
            if p["influence"]:
                i = p["influence"]
                print("  lobbying ${:,.0f} in {}{}   federal awards "
                      "${:,.0f}".format(
                          i.get("lobby_spend") or 0, i.get("year"),
                          "" if i.get("lobby_growth") is None else
                          " ({:+.0%} yoy)".format(i["lobby_growth"]),
                          i.get("contract_total") or 0))
        else:
            print(__doc__)
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
