"""
political_engine.py
===================
Storage, incremental scanning, grading and publishing for the Copy Trading
tool. No GUI imports — everything here runs from a terminal, the same split
value_engine.py uses.

    python political_engine.py backfill 30     # first run, one month back
    python political_engine.py scan            # one incremental pass
    python political_engine.py grade           # fill forward returns
    python political_engine.py report          # what the record says so far
    python political_engine.py leaderboard     # ranked filers, with p-values
    python political_engine.py publish         # portable snapshot for sharing

WHAT THE WATERMARK BUYS
-----------------------
Every source records where it last got to. A scan asks the cheap index what
is new, and only then pays for the expensive document. The House index is a
60KB zip behind a conditional request; in the steady state a poll costs
nothing and finds about one and a half new filings a day. Polling faster than
that does not make a 30-to-45-day-old disclosure fresher, so the loop is
paced to the filings rather than to the button.

WHY THE GRADER HAS TWO ANCHORS
------------------------------
A disclosure has two dates that matter and they are not the same event:

    trade_date    what the filer got. Nobody else could have acted on it.
    notify_date   when it became public. The only tradeable one.

Grading from trade_date and reporting the result as though it were available
is the single most common way this data is oversold. Both are computed here,
side by side, and the gap between them is the number this tool exists to
show.

WHY RANKINGS CARRY A P-VALUE
----------------------------
With 535 members of Congress, the best-performing one is best by chance
before it is best by skill. `hypotheses.py` in this repo exists because the
same trap was found here first: 35 buckets searched, best hit 26.2% against
a 16.4% base, and a permutation test reached that in 32.3% of random trials.
So every leaderboard this module prints reports the probability that a
population of pure noise, shuffled the same way, would have produced a
leader that good. A leaderboard without that number is a random number
generator with names attached.
"""

import os
import csv
import json
import gzip
import math
import random
import sqlite3
import threading
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, Tuple

import political_feeds as pf


_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(_DIR, "political.db")
LOG_FILE = os.path.join(_DIR, "political_log.csv")
SNAPSHOT_FILE = os.path.join(_DIR, "political_snapshot.json.gz")

HORIZONS = (10, 20, 60)
ANCHORS = ("notify", "trade")

# Cadence per source family, in seconds. Sized to how fast the underlying
# filing actually moves, not to how fast a loop can spin.
TIER_SECONDS = {
    "congress": 30 * 60,          # House index + Senate search
    "form4": 24 * 60 * 60,        # daily index, once after the close
    "f13": 24 * 60 * 60,          # cheap check; real work only in the window
    "influence": 7 * 24 * 60 * 60,
}

SOURCES = ("house_ptr", "senate_ptr", "form4", "f13")
# The influence overlay is not a disclosure source — it annotates tickers
# that the sources above have already surfaced — so it is opted into
# separately rather than being part of the default set.
ALL_FEEDS = SOURCES + ("influence",)


# ─────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────

_GRADE_COLS = []
for _a in ANCHORS:
    _GRADE_COLS.append("px_{}".format(_a))
    for _h in HORIZONS:
        _GRADE_COLS.append("ret_{}d_{}".format(_h, _a))

SCHEMA = """
CREATE TABLE IF NOT EXISTS disclosures (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    filer_name   TEXT,
    filer_id     TEXT,
    chamber      TEXT,
    party        TEXT,
    state        TEXT,
    committees   TEXT,
    ticker       TEXT,
    asset_name   TEXT,
    asset_type   TEXT,
    txn_type     TEXT,
    owner        TEXT,
    trade_date   TEXT,
    notify_date  TEXT,
    file_date    TEXT,
    filer_notified TEXT,
    amount_lo    REAL,
    amount_hi    REAL,
    shares       REAL,
    price        REAL,
    option_type  TEXT,
    strike       REAL,
    expiry       TEXT,
    doc_id       TEXT,
    doc_url      TEXT,
    fetched_at   TEXT,
    raw          TEXT,
    parse_flags  TEXT,
    graded_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_disc_ticker  ON disclosures(ticker);
CREATE INDEX IF NOT EXISTS ix_disc_notify  ON disclosures(notify_date);
CREATE INDEX IF NOT EXISTS ix_disc_filer   ON disclosures(filer_id);
CREATE INDEX IF NOT EXISTS ix_disc_source  ON disclosures(source);

CREATE TABLE IF NOT EXISTS scan_state (
    source     TEXT PRIMARY KEY,
    watermark  TEXT,
    last_run   TEXT,
    last_note  TEXT
);

CREATE TABLE IF NOT EXISTS seen_docs (
    source TEXT, doc_id TEXT, seen_at TEXT, rows INTEGER, error TEXT,
    PRIMARY KEY (source, doc_id)
);

CREATE TABLE IF NOT EXISTS f13_positions (
    manager_cik TEXT, report_date TEXT, cusip TEXT,
    issuer TEXT, shares REAL, value REAL, filing_date TEXT,
    PRIMARY KEY (manager_cik, report_date, cusip)
);

CREATE TABLE IF NOT EXISTS lobbying (
    filing_uuid TEXT PRIMARY KEY, year INTEGER, period TEXT, posted TEXT,
    income REAL, expenses REAL, client TEXT, registrant TEXT, issues TEXT
);

CREATE TABLE IF NOT EXISTS influence (
    ticker TEXT, year INTEGER, matched_name TEXT,
    lobby_spend REAL, lobby_filings INTEGER,
    contract_total REAL, contract_count INTEGER,
    updated_at TEXT,
    PRIMARY KEY (ticker, year)
);

CREATE TABLE IF NOT EXISTS sectors (
    ticker TEXT PRIMARY KEY, sector TEXT, industry TEXT, fetched_at TEXT
);
-- market_cap lives here too; added by ALTER for stores created earlier.

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# The cluster view gets unreadable past a couple of hundred nodes, and with
# seventeen managers tracked the buy side alone runs into the thousands. A
# sector is the cut that keeps a screenful legible without hiding anything
# arbitrarily — unlike a row cap, which silently drops whatever sorted last.
#
# The keys are what the rail shows; the values are the strings yfinance
# actually returns, verified against live tickers rather than assumed.
# Financials, consumer and utilities are listed even though they were not
# asked for, because a filter that omits them would make those names
# invisible with no way to reach them.
SECTOR_GROUPS = {
    "tech": ("Technology", "Communication Services"),
    "medical": ("Healthcare",),
    "real estate": ("Real Estate",),
    "materials": ("Basic Materials",),
    "industrials": ("Industrials",),
    "energy": ("Energy", "Utilities"),
    "financials": ("Financial Services",),
    "consumer": ("Consumer Cyclical", "Consumer Defensive"),
}
SECTOR_ORDER = ("all", "tech", "medical", "industrials", "energy",
                "materials", "real estate", "financials", "consumer")


def connect(path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for c in _GRADE_COLS:
        try:
            conn.execute("ALTER TABLE disclosures ADD COLUMN {} REAL".format(c))
        except sqlite3.OperationalError:
            pass                                 # already present
    try:
        conn.execute("ALTER TABLE disclosures ADD COLUMN filer_notified TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE sectors ADD COLUMN market_cap REAL")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    _migrate_house_notify(conn)
    return conn


def _migrate_house_notify(conn):
    """Move House rows onto the filing date as their public anchor.

    Rows written before 2026-09-21 stored the House form's "Notification
    Date" in notify_date. That column is when the FILER was told, not when
    the document became public, and it sits 13.4 days earlier on average.
    Every forward return anchored on it was measured from a day on which
    nobody outside the filer's household could have acted.

    The correction is exact rather than a re-parse, because file_date was
    already stored correctly on every row. Grades computed from the old
    anchor are cleared so they are recomputed rather than left to describe
    a date the column no longer holds.
    """
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM disclosures WHERE source='house_ptr' "
            "AND filer_notified IS NULL AND notify_date IS NOT NULL "
            "AND file_date IS NOT NULL AND notify_date != file_date"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return
    if not n:
        return
    conn.execute(
        "UPDATE disclosures SET filer_notified = notify_date, "
        "notify_date = file_date WHERE source='house_ptr' "
        "AND filer_notified IS NULL AND file_date IS NOT NULL")
    clears = ", ".join("{}=NULL".format(c) for c in _GRADE_COLS
                       if c.endswith("_notify") or c == "px_notify")
    conn.execute(
        "UPDATE disclosures SET {}, graded_at=NULL "
        "WHERE source='house_ptr'".format(clears))
    conn.commit()
    print("[political_engine] migrated {} House rows onto the filing date "
          "as their public anchor; notify-side grades cleared for "
          "recompute".format(n))


# ─────────────────────────────────────────────
# WRITE PATH
# ─────────────────────────────────────────────

_INSERT_COLS = [c for c in pf.ROW_FIELDS]


def upsert_rows(conn, rows: List[dict], log: bool = True) -> int:
    """Insert rows that are new. Existing ids are left alone.

    A filed disclosure never changes — an amendment is a new document with
    its own id — so a row already in the table is authoritative and is not
    overwritten. That also means a parser improvement does not silently
    rewrite history; re-parsing is a deliberate act.

    Which rows are new is determined before the insert rather than read off
    executemany's rowcount, which is not reliable for INSERT OR IGNORE. The
    genuinely new ones are what gets appended to the immutable log, so that
    file records each disclosure once, at the moment it was first seen.
    """
    if not rows:
        return 0
    ids = [r.get("id") for r in rows if r.get("id")]
    existing = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        existing |= {r[0] for r in conn.execute(
            "SELECT id FROM disclosures WHERE id IN ({})".format(
                ",".join("?" * len(chunk))), chunk)}
    fresh = [r for r in rows if r.get("id") and r["id"] not in existing]
    if not fresh:
        return 0

    payload = []
    for r in fresh:
        d = dict(r)
        fl = d.get("parse_flags")
        d["parse_flags"] = json.dumps(fl) if isinstance(fl, list) else (fl or "[]")
        payload.append(tuple(d.get(c) for c in _INSERT_COLS))
    conn.executemany(
        "INSERT OR IGNORE INTO disclosures ({}) VALUES ({})".format(
            ",".join(_INSERT_COLS), ",".join("?" * len(_INSERT_COLS))),
        payload)
    conn.commit()
    if log:
        log_rows(fresh)
    return len(fresh)


def get_state(conn, source: str) -> dict:
    r = conn.execute("SELECT * FROM scan_state WHERE source=?",
                     (source,)).fetchone()
    return dict(r) if r else {"source": source, "watermark": None,
                              "last_run": None, "last_note": None}


def set_state(conn, source: str, watermark: Optional[str] = None,
              note: str = ""):
    prev = get_state(conn, source)
    conn.execute(
        "INSERT INTO scan_state (source, watermark, last_run, last_note) "
        "VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET "
        "watermark=excluded.watermark, last_run=excluded.last_run, "
        "last_note=excluded.last_note",
        (source, watermark if watermark is not None else prev.get("watermark"),
         datetime.now().isoformat(timespec="seconds"), note))
    conn.commit()


def _seen(conn, source: str, doc_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM seen_docs WHERE source=? AND doc_id=?",
        (source, str(doc_id))).fetchone() is not None


def _mark_seen(conn, source: str, doc_id: str, nrows: int,
               error: Optional[str]):
    conn.execute(
        "INSERT OR REPLACE INTO seen_docs (source, doc_id, seen_at, rows, error)"
        " VALUES (?,?,?,?,?)",
        (source, str(doc_id), datetime.now().isoformat(timespec="seconds"),
         nrows, error))


# ─────────────────────────────────────────────
# SCANS
# ─────────────────────────────────────────────

class ScanResult(dict):
    """new rows, documents read, documents skipped, and why."""

    def __init__(self, **kw):
        super().__init__(new_rows=0, docs=0, skipped=0, errors={}, **kw)

    def fail(self, reason: str):
        self["skipped"] += 1
        self["errors"][reason] = self["errors"].get(reason, 0) + 1

    def merge(self, other: "ScanResult"):
        self["new_rows"] += other["new_rows"]
        self["docs"] += other["docs"]
        self["skipped"] += other["skipped"]
        for k, v in other["errors"].items():
            self["errors"][k] = self["errors"].get(k, 0) + v
        return self


def scan_house(conn, since: date, roster=None, progress=None) -> ScanResult:
    res = ScanResult()
    roster = roster or pf.Roster()
    # Every year in the span, not just the endpoints. `{since.year,
    # today.year}` silently skipped 2025 on a two-year backfill — a set of
    # two elements looks like a range and is not one.
    years = list(range(since.year, date.today().year + 1))
    for year in years:
        try:
            idx = pf.house_index(year)
        except Exception as e:                         # noqa: BLE001
            res.fail("index: {}".format(e))
            continue
        ptrs = [f for f in idx if f.get("filing_type") == pf.HOUSE_PTR_TYPE]
        for f in ptrs:
            fd = pf._mdy(f.get("filing_date"))
            if fd and fd < since.isoformat():
                continue
            if _seen(conn, "house_ptr", f["doc_id"]):
                continue
            rows, err = pf.house_ptr_rows(f, roster)
            _mark_seen(conn, "house_ptr", f["doc_id"], len(rows), err)
            res["docs"] += 1
            if err:
                res.fail(err)
            else:
                res["new_rows"] += upsert_rows(conn, rows)
            if progress:
                progress("house", f.get("last", ""), res)
        conn.commit()
    set_state(conn, "house_ptr", date.today().isoformat(),
              "{} docs, {} rows".format(res["docs"], res["new_rows"]))
    return res


def scan_senate(conn, since: date, roster=None, progress=None) -> ScanResult:
    res = ScanResult()
    roster = roster or pf.Roster()
    sess = pf.senate_session()
    if sess is None:
        res.fail("could not establish eFD session")
        set_state(conn, "senate_ptr", None, "session failed")
        return res
    try:
        filings = pf.senate_search(sess, since)
    except Exception as e:                             # noqa: BLE001
        res.fail("search: {}".format(e))
        return res
    for f in filings:
        doc_id = pf._senate_doc_id(f.get("url"))
        if not doc_id or _seen(conn, "senate_ptr", doc_id):
            continue
        rows, err = pf.senate_ptr_rows(sess, f, roster)
        _mark_seen(conn, "senate_ptr", doc_id, len(rows), err)
        res["docs"] += 1
        if err:
            res.fail(err)
        else:
            res["new_rows"] += upsert_rows(conn, rows)
        if progress:
            progress("senate", f.get("last", ""), res)
    conn.commit()
    set_state(conn, "senate_ptr", date.today().isoformat(),
              "{} docs, {} rows".format(res["docs"], res["new_rows"]))
    return res


def sp500_tickers() -> List[str]:
    """The S&P 500, cached on disk.

    This is the issuer universe for the insider feed. Tracking "every major
    CEO" by name is the wrong shape of problem — executives come and go, the
    list is never right, and a name is not an identifier. Tracking every
    Form 4 filed AT a defined set of companies captures every CEO, CFO and
    director at those companies automatically and stays correct when they
    change.
    """
    def fetch():
        r = pf._get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                    headers={"User-Agent": "Mozilla/5.0"})
        return r.content if r is not None and r.status_code == 200 else None

    raw = pf._cached_bytes("universe/sp500.html", fetch, 7.0)
    if not raw:
        return []
    # Parsed with BeautifulSoup rather than pandas.read_html, which needs
    # lxml — a dependency this repo does not carry and does not need for one
    # table. bs4 is already here for the Senate eFD pages.
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw.decode("utf-8", errors="replace"),
                             "html.parser")
        table = soup.find("table", {"id": "constituents"}) or soup.find("table")
        if table is None:
            return []
        out = []
        for tr in table.find_all("tr")[1:]:
            td = tr.find_all("td")
            if not td:
                continue
            sym = td[0].get_text(strip=True).replace(".", "-").upper()
            if sym and len(sym) <= 8:
                out.append(sym)
        return out
    except Exception as e:                             # noqa: BLE001
        print("[political_engine] S&P 500 fetch failed: {}".format(e))
        return []


def watchlist_from_db(conn, limit: int = 600, include_sp500: bool = True
                      ) -> List[str]:
    """Tickers the insider feed watches.

    The names Congress and the tracked funds have disclosed, plus the
    S&P 500. The first half answers "what are insiders doing in the names
    politicians trade"; the second makes the feed cover the large-cap
    executives anyone would actually want to follow, without needing a list
    of people that would be stale the week it was written.
    """
    seen = [r["ticker"] for r in conn.execute(
        "SELECT ticker, COUNT(*) c FROM disclosures WHERE ticker IS NOT NULL "
        "AND ticker != '' AND source IN ('house_ptr','senate_ptr','f13') "
        "GROUP BY ticker ORDER BY c DESC LIMIT ?", (limit,))]
    if not include_sp500:
        return seen
    out, have = list(seen), set(seen)
    for t in sp500_tickers():
        if t not in have:
            have.add(t)
            out.append(t)
    return out


def scan_form4(conn, since: date, watchlist: Optional[List[str]] = None,
               progress=None, max_days: int = 45) -> ScanResult:
    """Insider transactions, narrowed to a ticker watchlist.

    A full day of Form 4s is roughly 1,500 filings. Reading a fixed slice of
    them is worse than reading none: EDGAR's daily index is ordered by
    company name, so a per-day budget quietly means "every insider whose
    employer starts with A". That is a sampling rule masquerading as a feed,
    and any statistic computed downstream would inherit the alphabet.

    So the default watchlist is the set of tickers Congress and the tracked
    funds have actually disclosed. That makes this feed answer a question
    worth asking — what are insiders doing in the names politicians are
    trading — instead of an arbitrary one. An explicit watchlist overrides
    it; only an empty database falls back to a bounded scan, and that case
    is flagged.
    """
    res = ScanResult()
    if watchlist is None:
        watchlist = watchlist_from_db(conn)
        if not watchlist:
            res["errors"]["no watchlist yet — bounded alphabetical sample"] = 1
    wl = {t.upper() for t in (watchlist or [])}
    cikmap = sec_cik_map() if wl else {}
    want_ciks = {cikmap[t] for t in wl if t in cikmap}
    day = max(since, date.today() - timedelta(days=max_days))
    today = date.today()
    while day <= today:
        if day.weekday() < 5:
            try:
                entries = pf.form4_index(day)
            except Exception as e:                     # noqa: BLE001
                entries = []
                res.fail("index {}: {}".format(day, e))
            if want_ciks:
                # The CIK on a Form 4 index line is the ISSUER's for filings
                # indexed under the company and the reporting person's for
                # the mirrored copy, so both are kept and the ticker check
                # after parsing still has the final say.
                entries = [e for e in entries
                           if str(e.get("cik") or "").lstrip("0") in want_ciks]
            budget = len(entries) if want_ciks else 120
            for e in entries[:budget]:
                key = e.get("path") or ""
                if _seen(conn, "form4", key):
                    continue
                rows, err = pf.form4_rows(e)
                if wl:
                    rows = [r for r in rows
                            if (r.get("ticker") or "").upper() in wl]
                _mark_seen(conn, "form4", key, len(rows), err)
                res["docs"] += 1
                if err and not rows:
                    res.fail(err)
                else:
                    res["new_rows"] += upsert_rows(conn, rows)
                if progress:
                    progress("form4", e.get("company", ""), res)
            conn.commit()
        day += timedelta(days=1)
    set_state(conn, "form4", today.isoformat(),
              "{} docs, {} rows".format(res["docs"], res["new_rows"]))
    return res


def scan_f13(conn, managers=None, progress=None) -> ScanResult:
    """13F holdings, diffed quarter over quarter into buys and sells.

    A 13F is a snapshot, not a transaction. The trade is the difference
    between two consecutive snapshots, and it has no date — only the quarter
    it happened inside. So trade_date is set to the report date, which is the
    LATEST the position could have been taken, and every such row is flagged
    so nothing downstream mistakes a quarter-end stamp for an execution time.
    """
    res = ScanResult()
    managers = managers or pf.WHALES
    tmap = sec_ticker_map()
    for name, cik in managers:
        try:
            filings = pf.f13_filings(cik, limit=4)
        except Exception as e:                         # noqa: BLE001
            res.fail("{}: {}".format(name, e))
            continue
        # Store every snapshot FIRST, then diff. EDGAR returns filings
        # newest first, so diffing as each one lands means the newest is
        # compared against a predecessor that has not been fetched yet and
        # produces nothing — the symptom is a scan that reads forty 13Fs
        # and emits zero trades.
        stored = []
        for f in filings:
            acc = f.get("accession")
            rd = f.get("report_date")
            if _seen(conn, "f13", acc):
                if rd:
                    stored.append(f)
                continue
            holdings, err = pf.f13_holdings(f)
            _mark_seen(conn, "f13", acc, len(holdings), err)
            res["docs"] += 1
            if err:
                res.fail(err)
                continue
            conn.executemany(
                "INSERT OR REPLACE INTO f13_positions "
                "(manager_cik, report_date, cusip, issuer, shares, value, "
                "filing_date) VALUES (?,?,?,?,?,?,?)",
                [(str(cik).lstrip("0"), rd, h["cusip"], h["issuer"],
                  h["shares"], h["value"], f.get("filing_date"))
                 for h in holdings if h.get("cusip")])
            conn.commit()
            if rd:
                stored.append(f)
            if progress:
                progress("f13", name, res)

        for f in sorted(stored, key=lambda x: x.get("report_date") or ""):
            rows = _f13_diff_rows(conn, name, str(cik).lstrip("0"),
                                  f.get("report_date"),
                                  f.get("filing_date"), tmap)
            res["new_rows"] += upsert_rows(conn, rows)
    set_state(conn, "f13", date.today().isoformat(),
              "{} filings, {} rows".format(res["docs"], res["new_rows"]))
    return res


def _f13_diff_rows(conn, name: str, cik: str, report_date: Optional[str],
                   filing_date: Optional[str], tmap: dict) -> List[dict]:
    if not report_date:
        return []
    prev = conn.execute(
        "SELECT DISTINCT report_date FROM f13_positions WHERE manager_cik=? "
        "AND report_date < ? ORDER BY report_date DESC LIMIT 1",
        (cik, report_date)).fetchone()
    if not prev:
        return []                      # first snapshot: nothing to diff against
    prev_date = prev["report_date"]
    now = {r["cusip"]: dict(r) for r in conn.execute(
        "SELECT * FROM f13_positions WHERE manager_cik=? AND report_date=?",
        (cik, report_date))}
    old = {r["cusip"]: dict(r) for r in conn.execute(
        "SELECT * FROM f13_positions WHERE manager_cik=? AND report_date=?",
        (cik, prev_date))}

    rows = []
    for cusip in set(now) | set(old):
        n = (now.get(cusip) or {})
        o = (old.get(cusip) or {})
        n_sh = float(n["shares"] or 0) if n else 0.0
        o_sh = float(o["shares"] or 0) if o else 0.0
        delta = n_sh - o_sh
        if abs(delta) < 1:
            continue
        issuer = (n.get("issuer") if n else None) or o.get("issuer")

        # A dollar figure for the position change, so the graph can size a
        # 13F move against a Form 4 or a PTR bucket. The information table
        # reports value in whole dollars alongside the share count, and the
        # ratio is a real price — median 77.18 across 5,064 positions here,
        # 10th to 90th percentile 10.25 to 330.11, which is what stock
        # prices look like. It is the QUARTER-END price though, not what the
        # manager paid, so the row is flagged as an estimate rather than
        # presented as a disclosed amount like the other three sources.
        px = None
        for side in (n, o):
            if side and side.get("shares") and side.get("value"):
                try:
                    px = float(side["value"]) / float(side["shares"])
                    break
                except (TypeError, ZeroDivisionError):
                    pass

        row = pf.blank_row()
        row["id"] = "f13:{}:{}:{}".format(cik, report_date, cusip)
        row["source"] = "f13"
        row["filer_name"] = name
        row["filer_id"] = cik
        row["chamber"] = "fund"
        row["asset_name"] = issuer
        row["asset_type"] = "ST"
        row["txn_type"] = "P" if delta > 0 else "S"
        row["owner"] = "self"
        row["shares"] = abs(delta)
        row["trade_date"] = report_date
        row["notify_date"] = filing_date
        row["file_date"] = filing_date
        row["doc_id"] = "{}:{}".format(cik, report_date)
        row["doc_url"] = "https://www.sec.gov/cgi-bin/browse-edgar?action=" \
                         "getcompany&CIK={}&type=13F-HR".format(cik)
        row["committees"] = json.dumps([])
        row["fetched_at"] = datetime.utcnow().isoformat(timespec="seconds")
        row["price"] = px
        if px:
            row["amount_lo"] = row["amount_hi"] = round(abs(delta) * px, 2)
        row["ticker"], by_name = _resolve_issuer(issuer, tmap)
        row["parse_flags"] = ["f13_quarter_stamp"]
        if px:
            row["parse_flags"].append("amount_estimated")
        if by_name:
            row["parse_flags"].append("ticker_by_name")
        if not row["ticker"]:
            row["parse_flags"].append("no_ticker")
        row["raw"] = "{} {} {} -> {} shares ({} to {})".format(
            name, issuer, o_sh, n_sh, prev_date, report_date)[:400]
        rows.append(row)
    return rows


# ─────────────────────────────────────────────
# CUSIP HAS NO FREE TICKER MAP — so match on name
# ─────────────────────────────────────────────

_TICKER_MAP_CACHE = {}
_CIK_MAP_CACHE = {}
_NAME_BY_TICKER = {}


def sec_cik_map() -> dict:
    """ticker -> CIK, as the EDGAR daily index spells it (no zero padding).

    This is what makes the Form 4 watchlist affordable. The index line
    carries a CIK but not a ticker, so without this map the only way to
    know whether a filing is on the watchlist is to download and parse it
    — which means fetching all 1,500 of a day's filings to keep the twenty
    that matter. Resolving the watchlist to CIKs first turns a thirty-day
    backfill from roughly twelve thousand requests into a few hundred.
    """
    if _CIK_MAP_CACHE:
        return _CIK_MAP_CACHE
    sec_ticker_map()                      # shares the same source document
    return _CIK_MAP_CACHE


def sec_ticker_map() -> dict:
    """Normalised issuer name -> ticker, from SEC's own company list.

    13F information tables identify a holding by CUSIP, and there is no free
    CUSIP-to-ticker service — CUSIP is licensed. So the only route left is
    the issuer name, which is entered by the filer and is not canonical.
    Matches made this way are flagged `ticker_by_name` on the row, because a
    name match is a guess with a good hit rate, not an identifier.
    """
    if _TICKER_MAP_CACHE:
        return _TICKER_MAP_CACHE

    def fetch():
        r = pf._get("https://www.sec.gov/files/company_tickers.json")
        return r.content if r is not None and r.status_code == 200 else None

    raw = pf._cached_bytes("sec/company_tickers.json", fetch, 7.0)
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    for v in data.values():
        t = (v.get("ticker") or "").upper()
        n = _norm_issuer(v.get("title"))
        if t and n and n not in _TICKER_MAP_CACHE:
            _TICKER_MAP_CACHE[n] = t
        cik = v.get("cik_str")
        if t and cik is not None:
            _CIK_MAP_CACHE[t] = str(int(cik))
        if t and v.get("title") and t not in _NAME_BY_TICKER:
            _NAME_BY_TICKER[t] = v["title"]
    return _TICKER_MAP_CACHE


_ISSUER_NOISE = (
    " inc", " corp", " corporation", " co", " company", " ltd", " limited",
    " plc", " lp", " llc", " holdings", " holding", " group", " sa", " nv",
    " ag", " the", " trust", " class a", " class b", " com", " cl a", " cl b",
    " new", " del", " usa", " intl", " international",
)


def _norm_issuer(s: Optional[str]) -> str:
    t = " " + (s or "").lower().replace("&", " and ").replace(".", " ")
    t = "".join(ch if ch.isalnum() or ch == " " else " " for ch in t)
    t = " ".join(t.split())
    changed = True
    while changed:
        changed = False
        for n in _ISSUER_NOISE:
            if t.endswith(n):
                t = t[: -len(n)].strip()
                changed = True
    return t


def _resolve_issuer(issuer: Optional[str], tmap: dict) -> Tuple[Optional[str], bool]:
    if not issuer or not tmap:
        return None, False
    key = _norm_issuer(issuer)
    if key in tmap:
        return tmap[key], True
    head = " ".join(key.split()[:2])
    if head and head in tmap:
        return tmap[head], True
    return None, False


# ─────────────────────────────────────────────
# SECTORS
# ─────────────────────────────────────────────

def fetch_sectors(conn, tickers=None, progress=None, limit=None) -> dict:
    """Classify tickers by sector, once, and keep it.

    A sector almost never changes, so this is fetched once per ticker and
    stored. A ticker that cannot be classified is recorded with a NULL
    sector rather than skipped, so the next run does not try it again
    forever — a delisted or mis-resolved symbol should cost one lookup, not
    one per scan.
    """
    import yfinance_throttle    # noqa: F401  installs the global limiter
    import yfinance as yf

    if tickers is None:
        tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT d.ticker FROM disclosures d "
            "LEFT JOIN sectors s ON s.ticker = d.ticker "
            "WHERE d.ticker IS NOT NULL AND d.ticker != '' "
            "AND s.ticker IS NULL")]
    if limit:
        tickers = tickers[:limit]
    stats = {"asked": len(tickers), "ok": 0, "unknown": 0}
    now = datetime.now().isoformat(timespec="seconds")
    for i, t in enumerate(tickers, 1):
        sec = ind = cap = None
        try:
            info = yf.Ticker(t).info or {}
            sec, ind = info.get("sector"), info.get("industry")
            cap = info.get("marketCap")
        except Exception:                              # noqa: BLE001
            pass
        conn.execute("INSERT OR REPLACE INTO sectors "
                     "(ticker, sector, industry, market_cap, fetched_at) "
                     "VALUES (?,?,?,?,?)", (t, sec, ind, cap, now))
        if sec:
            stats["ok"] += 1
        else:
            stats["unknown"] += 1
        if i % 25 == 0:
            conn.commit()
        if progress:
            progress(i, len(tickers), t, sec or "unknown")
    conn.commit()
    return stats


def market_caps(conn) -> dict:
    """ticker -> market cap. The only honest way to size a company orb.

    Disclosed amount was standing in for this and should not have been: it
    says how much somebody happened to trade, not how big the company is,
    so a small name one fund bought heavily drew larger than Apple.
    """
    return {r[0]: r[1] for r in conn.execute(
        "SELECT ticker, market_cap FROM sectors WHERE market_cap IS NOT NULL")}


def fund_values(conn) -> dict:
    """filer -> total 13F portfolio value, for sizing the institution ring."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT filer_name, SUM(amount_lo) FROM disclosures "
        "WHERE source='f13' AND amount_lo IS NOT NULL GROUP BY filer_name")}


def sector_tickers(conn, group: str):
    """Tickers in one of the SECTOR_GROUPS buckets, or None for everything."""
    if not group or group == "all":
        return None
    names = SECTOR_GROUPS.get(group)
    if not names:
        return None
    return {r[0] for r in conn.execute(
        "SELECT ticker FROM sectors WHERE sector IN ({})".format(
            ",".join("?" * len(names))), tuple(names))}


def sector_counts(conn) -> dict:
    out = {}
    for r in conn.execute(
            "SELECT s.sector, COUNT(DISTINCT d.ticker) c FROM disclosures d "
            "JOIN sectors s ON s.ticker = d.ticker WHERE s.sector IS NOT NULL "
            "GROUP BY s.sector"):
        out[r[0]] = r[1]
    return out


# ─────────────────────────────────────────────
# INFLUENCE OVERLAY — lobbying spend and federal awards
# ─────────────────────────────────────────────

def _lobby_search_name(ticker: str) -> Optional[str]:
    """A search string for a ticker, from SEC's own registrant title.

    The lobbying register and the federal award system both key on a
    company's legal name, and neither carries a ticker. So the join runs
    through SEC's registrant title, cut to its first two words — "NVIDIA
    CORPORATION" searches as "NVIDIA", which the LDA API matches as a
    prefix. The name actually used is stored on the row, because a join
    made on a string is a claim somebody should be able to check.
    """
    sec_ticker_map()
    title = _NAME_BY_TICKER.get((ticker or "").upper())
    if not title:
        return None
    words = _norm_issuer(title).split()
    return " ".join(words[:2]) if words else None


def scan_influence(conn, tickers: Optional[List[str]] = None,
                   years: Optional[List[int]] = None,
                   progress=None) -> ScanResult:
    """Lobbying spend and contract awards for the tickers already tracked.

    This is an overlay, not a feed. It answers "is this company spending
    more in Washington than it was" for names that already appear in the
    disclosure table, and it deliberately does not screen the whole market
    — the point is context on a trade somebody actually made, not another
    ranking to search through.
    """
    res = ScanResult()
    tickers = tickers or watchlist_from_db(conn, limit=250)
    years = years or [date.today().year, date.today().year - 1]
    for t in tickers:
        name = _lobby_search_name(t)
        if not name:
            res.fail("no registrant name for ticker")
            continue
        for y in years:
            try:
                fils = pf.lobbying_filings_for(name, y)
            except Exception as e:                     # noqa: BLE001
                res.fail("lobbying: {}".format(e))
                fils = []
            spend = sum((f.get("income") or 0) + (f.get("expenses") or 0)
                        for f in fils)
            ctotal, ccount = 0.0, 0
            if y == date.today().year:
                try:
                    awards = pf.contracts(
                        name, date(y - 1, 10, 1), date.today(), limit=100)
                    ctotal = sum(a.get("Award Amount") or 0 for a in awards)
                    ccount = len(awards)
                except Exception as e:                 # noqa: BLE001
                    res.fail("contracts: {}".format(e))
            conn.execute(
                "INSERT OR REPLACE INTO influence (ticker, year, matched_name,"
                " lobby_spend, lobby_filings, contract_total, contract_count,"
                " updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (t, y, name, spend, len(fils), ctotal, ccount,
                 datetime.now().isoformat(timespec="seconds")))
            res["docs"] += 1
        conn.commit()
        if progress:
            progress("influence", t, res)
    set_state(conn, "influence", date.today().isoformat(),
              "{} ticker-years".format(res["docs"]))
    return res


def influence_for(conn, ticker: str) -> dict:
    """Lobbying and contract context for one ticker, with growth."""
    rows = {r["year"]: dict(r) for r in conn.execute(
        "SELECT * FROM influence WHERE ticker=? ORDER BY year", (ticker,))}
    if not rows:
        return {}
    ys = sorted(rows)
    cur = rows[ys[-1]]
    out = dict(cur)
    if len(ys) >= 2:
        prev = rows[ys[-2]]
        p = prev.get("lobby_spend") or 0
        c = cur.get("lobby_spend") or 0
        # Growth is only meaningful against a non-trivial base. A company
        # that spent $5,000 last year and $50,000 this year is up 900% and
        # has told you nothing.
        out["lobby_growth"] = ((c - p) / p) if p >= 50000 else None
        out["lobby_prev"] = p
    return out


# ─────────────────────────────────────────────
# ORCHESTRATION
# ─────────────────────────────────────────────

def backfill_all_congress(conn=None, since_year: int = 2024,
                          progress=None) -> dict:
    """Read EVERY congressional transaction report back to `since_year`.

    There is no roster to add here. The House bulk index and the Senate eFD
    search each enumerate all of their filers, so the scanner has always
    covered all 539 members — what limited the store was the WINDOW, not a
    missing list.

    The honest ceiling is well under 539 and worth stating plainly: 107
    members filed a periodic transaction report in the whole of 2026, 108 in
    2025, 98 in 2024. The rest hold funds, or nothing, or do not trade. A
    member who never files a transaction cannot be copy-traded, so "every
    member of Congress" in practice means the ~120 who actually trade.
    """
    own = conn is None
    conn = conn or connect()
    since = date(since_year, 1, 1)
    out = {}
    roster = pf.Roster()
    try:
        out["house_ptr"] = scan_house(conn, since, roster, progress)
        out["senate_ptr"] = scan_senate(conn, since, roster, progress)
    finally:
        if own:
            conn.close()
    return out


def backfill(days: int = 30, sources=SOURCES, conn=None,
             progress=None) -> dict:
    """First run. Reads `days` back, then hands over to incremental scans."""
    own = conn is None
    conn = conn or connect()
    since = date.today() - timedelta(days=days)
    out = {}
    roster = pf.Roster()
    try:
        # Order matters on a first run: the congressional and 13F feeds
        # populate the ticker set that the Form 4 watchlist is derived from,
        # so Form 4 runs last or it has nothing to narrow to.
        if "house_ptr" in sources:
            out["house_ptr"] = scan_house(conn, since, roster, progress)
        if "senate_ptr" in sources:
            out["senate_ptr"] = scan_senate(conn, since, roster, progress)
        if "f13" in sources:
            out["f13"] = scan_f13(conn, progress=progress)
        if "form4" in sources:
            out["form4"] = scan_form4(conn, since, progress=progress)
        if "influence" in sources:
            out["influence"] = scan_influence(conn, progress=progress)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key,value) VALUES ('backfilled',?)",
            (datetime.now().isoformat(timespec="seconds"),))
        conn.commit()
    finally:
        if own:
            conn.close()
    return out


def scan_once(sources=SOURCES, conn=None, progress=None,
              lookback_days: int = 10) -> dict:
    """One incremental pass. Cheap when nothing new has been filed."""
    own = conn is None
    conn = conn or connect()
    out = {}
    roster = pf.Roster()
    try:
        since = date.today() - timedelta(days=lookback_days)
        if "house_ptr" in sources:
            out["house_ptr"] = scan_house(conn, since, roster, progress)
        if "senate_ptr" in sources:
            out["senate_ptr"] = scan_senate(conn, since, roster, progress)
        if "f13" in sources:
            out["f13"] = scan_f13(conn, progress=progress)
        if "form4" in sources:
            out["form4"] = scan_form4(conn, date.today() - timedelta(days=3),
                                      progress=progress)
        if "influence" in sources:
            out["influence"] = scan_influence(conn, progress=progress)
    finally:
        if own:
            conn.close()
    return out


class ScanWorker(threading.Thread):
    """Background scanner with per-source cadence.

    Waits on an Event rather than sleeping, so Stop takes effect immediately
    instead of at the end of a half-hour nap. Each source carries its own
    next-due time; the loop wakes at the nearest one, does only what is due,
    and goes back to waiting. Between polls this costs one blocked thread.

    Any exception inside a source is caught and recorded. One dead endpoint
    must not take down a loop that is also serving three live ones.
    """

    def __init__(self, sources=SOURCES, on_event=None, tiers=None,
                 lookback_days: int = 10):
        super().__init__(daemon=True)
        self.sources = list(sources)
        self.on_event = on_event or (lambda *a, **k: None)
        self.tiers = dict(TIER_SECONDS)
        if tiers:
            self.tiers.update(tiers)
        self.lookback_days = lookback_days
        # NOT self._stop — threading.Thread has its own _stop()
        # that join() calls internally, and shadowing it with an
        # Event makes every join() raise 'Event object is not
        # callable' the moment the thread finishes.
        self._halt = threading.Event()
        self._due = {}
        self.stats = {"passes": 0, "new_rows": 0, "errors": 0,
                      "started": None, "last_pass": None}

    def stop(self):
        self._halt.set()

    def _tier_of(self, source: str) -> str:
        return {"house_ptr": "congress", "senate_ptr": "congress",
                "form4": "form4", "f13": "f13",
                "influence": "influence"}.get(source, "congress")

    def run(self):
        self.stats["started"] = datetime.now().isoformat(timespec="seconds")
        conn = connect()
        roster = pf.Roster()
        now = datetime.now().timestamp()
        for s in self.sources:
            self._due[s] = now                 # everything due on first pass
        self.on_event("start", "scanner started", self.stats)
        try:
            while not self._halt.is_set():
                now = datetime.now().timestamp()
                ran = False
                for s in list(self.sources):
                    if self._halt.is_set():
                        break
                    if self._due.get(s, 0) > now:
                        continue
                    ran = True
                    try:
                        res = self._run_source(conn, s, roster)
                        self.stats["new_rows"] += res["new_rows"]
                        self.stats["errors"] += res["skipped"]
                        self.on_event("source", s, res)
                    except Exception as e:             # noqa: BLE001
                        self.stats["errors"] += 1
                        self.on_event("error", "{}: {}".format(s, e), None)
                    self._due[s] = datetime.now().timestamp() + \
                        self.tiers[self._tier_of(s)]
                if ran:
                    self.stats["passes"] += 1
                    self.stats["last_pass"] = \
                        datetime.now().isoformat(timespec="seconds")
                    self.on_event("pass", "pass complete", self.stats)
                nxt = min(self._due.values()) if self._due else now + 60
                self._halt.wait(max(5.0, min(60.0, nxt - datetime.now().timestamp())))
        finally:
            try:
                conn.close()
            except Exception:
                pass
            self.on_event("stop", "scanner stopped", self.stats)

    def _run_source(self, conn, source: str, roster) -> ScanResult:
        since = date.today() - timedelta(days=self.lookback_days)
        if source == "house_ptr":
            return scan_house(conn, since, roster)
        if source == "senate_ptr":
            return scan_senate(conn, since, roster)
        if source == "form4":
            return scan_form4(conn, date.today() - timedelta(days=3))
        if source == "f13":
            return scan_f13(conn)
        if source == "influence":
            return scan_influence(conn)
        return ScanResult()


# ─────────────────────────────────────────────
# GRADING
# ─────────────────────────────────────────────

def grade(conn=None, limit_tickers: Optional[int] = None,
          progress=None) -> dict:
    """Fill forward returns from both anchors.

    The anchor price and the forward price come out of the SAME yfinance
    frame, so both carry the same split and dividend adjustment. That is the
    trap review_outcomes.py documents: dividing a raw quote logged at scan
    time by an adjusted series rewrites the return every time a corporate
    action lands. Here there is no separately logged price to divide by, so
    the only requirement is that nothing ever mixes the two.

    A window that has not matured is left empty rather than graded against
    today. A ticker whose tape has gapped is left empty too — see
    review_outcomes._price_on_or_after, which refuses rather than guessing.
    """
    own = conn is None
    conn = conn or connect()
    try:
        import yfinance as yf
        from review_outcomes import _trading_days_after, _price_on_or_after
    except ImportError as e:                           # noqa: BLE001
        if own:
            conn.close()
        return {"error": "missing dependency: {}".format(e)}

    now = datetime.now()
    need_cols = ", ".join(_GRADE_COLS)
    rows = conn.execute(
        "SELECT id, ticker, trade_date, notify_date, {} FROM disclosures "
        "WHERE ticker IS NOT NULL AND ticker != '' "
        "AND (graded_at IS NULL OR ret_60d_notify IS NULL)".format(need_cols)
    ).fetchall()

    by_ticker = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(dict(r))
    tickers = list(by_ticker)
    if limit_tickers:
        tickers = tickers[:limit_tickers]

    stats = {"tickers": len(tickers), "rows": 0, "graded": 0,
             "no_history": 0, "immature": 0, "gapped": 0}

    for n, tkr in enumerate(tickers, 1):
        entries = by_ticker[tkr]
        anchors = [e[a + "_date"] for e in entries for a in ANCHORS
                   if e.get(a + "_date")]
        if not anchors:
            continue
        earliest = min(anchors)
        try:
            hist = yf.Ticker(tkr).history(
                start=(datetime.fromisoformat(earliest)
                       - timedelta(days=5)).strftime("%Y-%m-%d"),
                end=now.strftime("%Y-%m-%d"), interval="1d")
        except Exception:                              # noqa: BLE001
            hist = None
        if hist is None or hist.empty:
            stats["no_history"] += len(entries)
            if progress:
                progress(n, len(tickers), tkr, "no history")
            continue

        for e in entries:
            stats["rows"] += 1
            updates = {}
            for a in ANCHORS:
                ad = e.get(a + "_date")
                if not ad:
                    continue
                try:
                    a_dt = datetime.fromisoformat(ad)
                except ValueError:
                    continue
                px0 = _price_on_or_after(tkr, a_dt, hist)
                if not px0:
                    stats["gapped"] += 1
                    continue
                updates["px_" + a] = px0
                for h in HORIZONS:
                    target = _trading_days_after(a_dt, h)
                    if now < target:
                        stats["immature"] += 1
                        continue
                    px1 = _price_on_or_after(tkr, target, hist)
                    if not px1:
                        continue
                    updates["ret_{}d_{}".format(h, a)] = (px1 / px0) - 1.0
            if updates:
                sets = ", ".join("{}=?".format(k) for k in updates)
                conn.execute(
                    "UPDATE disclosures SET {}, graded_at=? WHERE id=?".format(sets),
                    tuple(updates.values()) +
                    (now.isoformat(timespec="seconds"), e["id"]))
                stats["graded"] += 1
        conn.commit()
        if progress:
            progress(n, len(tickers), tkr, "{} rows".format(len(entries)))
    if own:
        conn.close()
    return stats


# ─────────────────────────────────────────────
# STATISTICS — n, base rates, and a family-wise p-value
# ─────────────────────────────────────────────

def _fetch_graded(conn, horizon: int, anchor: str, where: str = "",
                  params=(), sources: Optional[Tuple[str, ...]] = None
                  ) -> List[dict]:
    col = "ret_{}d_{}".format(horizon, anchor)
    sql = ("SELECT filer_name, filer_id, chamber, party, committees, ticker, "
           "txn_type, owner, source, asset_type, notify_date, trade_date, "
           "{} AS ret FROM disclosures WHERE {} IS NOT NULL".format(col, col))
    params = tuple(params)
    if sources:
        sql += " AND source IN ({})".format(",".join("?" * len(sources)))
        params = params + tuple(sources)
    if where:
        sql += " AND " + where
    return [dict(r) for r in conn.execute(sql, params)]


def base_rate(rows: List[dict], direction: str = "P") -> dict:
    """Hit rate and mean return for one side of the book.

    A purchase is scored on the return itself; a sale is scored on its
    negative, because a sale that avoided a fall is a good sale. Mixing the
    two without that flip produces a number that means nothing.
    """
    vals = []
    for r in rows:
        if r.get("ret") is None:
            continue
        t = r.get("txn_type") or ""
        if direction == "P" and t != "P":
            continue
        if direction == "S" and not t.startswith("S"):
            continue
        v = float(r["ret"])
        vals.append(v if direction == "P" else -v)
    if not vals:
        return {"n": 0, "hit": None, "mean": None, "median": None}
    vals.sort()
    hits = sum(1 for v in vals if v > 0)
    mid = len(vals) // 2
    return {
        "n": len(vals),
        "hit": hits / float(len(vals)),
        "mean": sum(vals) / float(len(vals)),
        "median": vals[mid] if len(vals) % 2 else
                  (vals[mid - 1] + vals[mid]) / 2.0,
    }


def permutation_best_p(groups: Dict[str, List[float]], iters: int = 2000,
                       min_n: int = 5, seed: int = 0) -> dict:
    """How often pure noise produces a leader as good as the observed one.

    The question a leaderboard implicitly asks is "is the best of these real",
    and the answer has to account for how many candidates were looked at.
    So the outcomes are pooled, reshuffled across the same group sizes, and
    the BEST group mean is recorded each time. p is the share of shuffles
    whose best matched or beat the observed best.

    This is the same correction hypotheses.py applies to feature buckets. It
    is what separates "Congressman X returns 40%" from "someone was always
    going to."
    """
    usable = {k: v for k, v in groups.items() if len(v) >= min_n}
    if len(usable) < 2:
        return {"p": None, "leader": None, "observed": None,
                "groups": len(usable), "reason": "too few groups"}
    means = {k: sum(v) / len(v) for k, v in usable.items()}
    leader = max(means, key=means.get)
    observed = means[leader]

    pool = [v for vs in usable.values() for v in vs]
    sizes = [len(v) for v in usable.values()]
    rng = random.Random(seed)
    at_least = 0
    for _ in range(iters):
        rng.shuffle(pool)
        i = 0
        best = None
        for s in sizes:
            m = sum(pool[i:i + s]) / float(s)
            i += s
            if best is None or m > best:
                best = m
        if best is not None and best >= observed:
            at_least += 1
    return {"p": at_least / float(iters), "leader": leader,
            "observed": observed, "groups": len(usable),
            "n_leader": len(usable[leader]), "iters": iters}


def leaderboard(conn=None, horizon: int = 20, anchor: str = "notify",
                direction: str = "P", by: str = "filer_name",
                min_n: int = 10, iters: int = 2000,
                sources: Optional[Tuple[str, ...]] = None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        rows = _fetch_graded(conn, horizon, anchor, sources=sources)
    finally:
        if own:
            conn.close()

    groups = {}
    for r in rows:
        t = r.get("txn_type") or ""
        if direction == "P" and t != "P":
            continue
        if direction == "S" and not t.startswith("S"):
            continue
        val = float(r["ret"]) if direction == "P" else -float(r["ret"])
        if by == "committees":
            # The committees column carries congressional committee names
            # for House and Senate rows and the insider's ROLE for Form 4
            # rows, which is a different thing wearing the same column.
            # Grouping by committee therefore has to exclude everything
            # that is not a member of Congress, or "director" appears in
            # the table as though it were a committee.
            if r.get("source") not in ("house_ptr", "senate_ptr"):
                continue
            for c in json.loads(r.get("committees") or "[]"):
                groups.setdefault(c, []).append(val)
            continue
        key = r.get(by)
        if not key:
            continue
        groups.setdefault(key, []).append(val)

    base = base_rate(
        [r for r in rows
         if by != "committees" or r.get("source") in ("house_ptr", "senate_ptr")],
        direction)
    perm = permutation_best_p(groups, iters=iters, min_n=min_n)
    table = []
    for k, v in groups.items():
        if len(v) < min_n:
            continue
        hits = sum(1 for x in v if x > 0)
        table.append({"key": k, "n": len(v),
                      "mean": sum(v) / float(len(v)),
                      "hit": hits / float(len(v))})
    table.sort(key=lambda d: -d["mean"])

    # A permutation null assumes the pooled outcomes are exchangeable — that
    # any of them could have belonged to any group. Congressional PTRs, Form
    # 4 insider buys and 13F quarter diffs are three different
    # data-generating processes with different horizons and different
    # variance, so shuffling across them tests a null nobody believes. The
    # p-value is still reported, because suppressing it would be worse, but
    # it is reported with the violation named.
    present = sorted({r.get("source") for r in rows if r.get("source")})
    warnings = []
    if len(present) > 1:
        warnings.append(
            "pool mixes {} — a permutation null assumes these are "
            "interchangeable and they are not. Filter to one source before "
            "reading the p-value as a test.".format(", ".join(present)))
    if perm.get("n_leader") and perm["n_leader"] < 20:
        warnings.append(
            "the leader has n={}. At that size one trade moves the mean "
            "several points and the rank is mostly luck, whatever the "
            "p-value says.".format(perm["n_leader"]))
    return {"horizon": horizon, "anchor": anchor, "direction": direction,
            "by": by, "base": base, "permutation": perm, "table": table,
            "min_n": min_n, "sources_present": present,
            "warnings": warnings}


def anchor_gap(conn=None, horizon: int = 20, direction: str = "P",
               sources: Optional[Tuple[str, ...]] = None) -> dict:
    """What the filer got against what the public could get.

    The single number this tool exists to produce. Same horizon, two
    anchors: one starting the day the trade happened, one starting the day
    it was disclosed.

    PAIRED ROWS ONLY. The trade date is always the earlier of the two, so
    its window closes first, and taking each anchor's graded rows
    independently compares a larger sample against a smaller one — the
    trade-date side silently picks up every disclosure whose notify window
    has not matured yet. On this store that inflated the measured cost of
    the delay at 10 days from 1,566 shared rows to 1,662 against 1,566.
    A difference computed across two different samples is not a difference,
    and this function exists specifically to report a difference, so the
    requirement is that both returns come from the same row.
    """
    own = conn is None
    conn = conn or connect()
    try:
        cn = "ret_{}d_notify".format(horizon)
        ct = "ret_{}d_trade".format(horizon)
        where = "{} IS NOT NULL AND {} IS NOT NULL".format(cn, ct)
        params = ()
        if sources:
            where += " AND source IN ({})".format(",".join("?" * len(sources)))
            params = tuple(sources)
        rows = [dict(r) for r in conn.execute(
            "SELECT txn_type, source, {} AS rn, {} AS rt FROM disclosures "
            "WHERE {}".format(cn, ct, where), params)]

        def side(key):
            return base_rate([{"txn_type": r["txn_type"], "ret": r[key]}
                              for r in rows], direction)

        out = {"notify": side("rn"), "trade": side("rt"),
               "paired_rows": len(rows)}

        # Per source, because the two anchors do not mean the same thing
        # everywhere. A 13F trade date is the quarter END — the latest the
        # position could have been taken, not when it was — so its "gap" is
        # partly the length of a quarter and not a disclosure delay at all.
        out["by_source"] = {}
        for s in sorted({r["source"] for r in rows}):
            sub = [r for r in rows if r["source"] == s]
            n = base_rate([{"txn_type": r["txn_type"], "ret": r["rn"]}
                           for r in sub], direction)
            t = base_rate([{"txn_type": r["txn_type"], "ret": r["rt"]}
                           for r in sub], direction)
            out["by_source"][s] = {"notify": n, "trade": t}

        delay = conn.execute(
            "SELECT AVG(julianday(notify_date) - julianday(trade_date)) AS d, "
            "COUNT(*) AS n FROM disclosures WHERE trade_date IS NOT NULL "
            "AND notify_date IS NOT NULL AND source IN ('house_ptr',"
            "'senate_ptr') AND julianday(notify_date) >= julianday(trade_date)"
        ).fetchone()
        out["delay_days"] = delay["d"]
        out["delay_n"] = delay["n"]
    finally:
        if own:
            conn.close()
    return out


# ─────────────────────────────────────────────
# PUBLISH — the portable artefact friends read
# ─────────────────────────────────────────────

PUBLISH_FIELDS = [c for c in pf.ROW_FIELDS if c != "raw"] + _GRADE_COLS


def publish(path: str = SNAPSHOT_FILE, conn=None,
            since_days: Optional[int] = None) -> dict:
    """Write a gzipped JSON snapshot of the table.

    `raw` is left out. It exists so a parser change can be audited against
    the source text locally, and shipping several hundred kilobytes of
    filing fragments to everyone who reads the snapshot serves no one.

    The snapshot is the sharing mechanism: one file, readable with nothing
    but the standard library, small enough to commit. Friends' copies load
    this instead of scraping, which keeps the request load on the House and
    Senate at one machine's worth no matter how many people read it.
    """
    own = conn is None
    conn = conn or connect()
    try:
        where = ""
        params = ()
        if since_days:
            where = " WHERE notify_date >= ?"
            params = ((date.today() - timedelta(days=since_days)).isoformat(),)
        rows = [dict(r) for r in conn.execute(
            "SELECT {} FROM disclosures{} ORDER BY notify_date DESC".format(
                ",".join(PUBLISH_FIELDS), where), params)]
        payload = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "rows": len(rows),
            "fields": PUBLISH_FIELDS,
            "note": ("Public disclosure data, non-commercial use. "
                     "Congressional reports carry a statutory use "
                     "restriction; see COPY_TRADING_PLAN.md."),
            "data": rows,
        }
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, path)
        size = os.path.getsize(path)
    finally:
        if own:
            conn.close()
    return {"path": path, "rows": payload["rows"], "bytes": size}


def load_snapshot(path: str = SNAPSHOT_FILE) -> dict:
    """Read a published snapshot. Used by read-only clients."""
    if not os.path.exists(path):
        return {"rows": 0, "data": [], "error": "no snapshot at " + path}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def import_snapshot(path: str = SNAPSHOT_FILE, conn=None) -> int:
    """Merge a snapshot into the local database.

    Used for the seed that ships with the tool, so a first run starts with
    a month of history instead of an empty table and a two-minute wait.
    """
    snap = load_snapshot(path)
    if snap.get("error"):
        return 0
    own = conn is None
    conn = conn or connect()
    try:
        rows, graded = [], []
        for r in snap.get("data", []):
            row = pf.blank_row()
            row.update({k: v for k, v in r.items() if k in pf.ROW_FIELDS})
            rows.append(row)
            g = {k: r[k] for k in _GRADE_COLS
                 if k in r and r[k] is not None}
            if g and row.get("id"):
                graded.append((row["id"], g))
        # Not logged. The log records what THIS machine saw on the wire;
        # rows arriving from someone else's snapshot were already recorded
        # once, wherever they were scanned, and writing them again would
        # turn a provenance record into a copy of the database.
        n = upsert_rows(conn, rows, log=False)

        # Carry the forward returns across too. They are the expensive part
        # — grading this store is 1,190 separate price histories — and the
        # entire point of publishing a snapshot is that one machine pays
        # that cost and everyone else reads the result. Importing the rows
        # without them would leave every reader to re-fetch the same prices.
        applied = 0
        for rid, g in graded:
            sets = ", ".join("{}=?".format(k) for k in g)
            cur = conn.execute(
                "UPDATE disclosures SET {}, graded_at=COALESCE(graded_at,?) "
                "WHERE id=? AND graded_at IS NULL".format(sets),
                tuple(g.values()) + (snap.get("generated_at"), rid))
            applied += cur.rowcount or 0
        conn.commit()
    finally:
        if own:
            conn.close()
    return n


# ─────────────────────────────────────────────
# LOG — write-only, never read back by the scanner
# ─────────────────────────────────────────────

LOG_COLS = ["logged_at", "id", "source", "filer_name", "chamber", "party",
            "ticker", "txn_type", "owner", "trade_date", "notify_date",
            "amount_lo", "amount_hi", "asset_type", "option_type", "strike",
            "expiry", "parse_flags"]


def log_rows(rows: List[dict], path: str = LOG_FILE) -> int:
    """Append newly seen disclosures to an immutable log.

    Same contract as squeeze_logger.py: written once, never read by the
    thing that writes it, and never rewritten. The database is the working
    store and can be rebuilt; this is the record of what was seen and when,
    which cannot.
    """
    if not rows:
        return 0
    new = not os.path.exists(path)
    stamp = datetime.now().isoformat(timespec="seconds")
    try:
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=LOG_COLS, extrasaction="ignore")
            if new:
                w.writeheader()
            for r in rows:
                d = dict(r)
                d["logged_at"] = stamp
                fl = d.get("parse_flags")
                d["parse_flags"] = ",".join(fl) if isinstance(fl, list) else (fl or "")
                w.writerow(d)
    except Exception:                                  # noqa: BLE001
        return 0                    # logging must never break a scan
    return len(rows)


# ─────────────────────────────────────────────
# REPORTS
# ─────────────────────────────────────────────

def summary(conn=None) -> dict:
    own = conn is None
    conn = conn or connect()
    try:
        out = {"total": conn.execute(
            "SELECT COUNT(*) c FROM disclosures").fetchone()["c"]}
        out["by_source"] = {r["source"]: r["c"] for r in conn.execute(
            "SELECT source, COUNT(*) c FROM disclosures GROUP BY source")}
        out["graded"] = conn.execute(
            "SELECT COUNT(*) c FROM disclosures WHERE graded_at IS NOT NULL"
        ).fetchone()["c"]
        out["with_ticker"] = conn.execute(
            "SELECT COUNT(*) c FROM disclosures WHERE ticker IS NOT NULL "
            "AND ticker != ''").fetchone()["c"]
        out["with_option"] = conn.execute(
            "SELECT COUNT(*) c FROM disclosures WHERE option_type IS NOT NULL"
        ).fetchone()["c"]
        out["filers"] = conn.execute(
            "SELECT COUNT(DISTINCT filer_name) c FROM disclosures").fetchone()["c"]
        out["range"] = dict(conn.execute(
            "SELECT MIN(notify_date) lo, MAX(notify_date) hi FROM disclosures"
        ).fetchone())
        out["state"] = {r["source"]: dict(r) for r in conn.execute(
            "SELECT * FROM scan_state")}
        out["flags"] = {}
        for r in conn.execute("SELECT parse_flags FROM disclosures "
                              "WHERE parse_flags IS NOT NULL"):
            try:
                for f in json.loads(r["parse_flags"] or "[]"):
                    out["flags"][f] = out["flags"].get(f, 0) + 1
            except Exception:
                pass
    finally:
        if own:
            conn.close()
    return out


def _pct(v):
    return "--" if v is None else "{:+.2f}%".format(100.0 * v)


def print_report(conn=None):
    s = summary(conn)
    print("\n" + "=" * 70)
    print("COPY TRADING — WHAT IS IN THE STORE")
    print("=" * 70)
    print("  disclosures   {}".format(s["total"]))
    for k, v in sorted(s["by_source"].items()):
        print("      {:<12} {}".format(k, v))
    print("  distinct filers {}".format(s["filers"]))
    print("  with a ticker   {}".format(s["with_ticker"]))
    print("  with option detail {}".format(s["with_option"]))
    print("  graded          {}".format(s["graded"]))
    print("  notify range    {} .. {}".format(
        s["range"].get("lo"), s["range"].get("hi")))
    if s["flags"]:
        print("  row flags:")
        for k, v in sorted(s["flags"].items(), key=lambda x: -x[1]):
            print("      {:>6}  {}".format(v, k))
    if s["state"]:
        print("  last run:")
        for k, v in sorted(s["state"].items()):
            print("      {:<12} {}  {}".format(
                k, v.get("last_run") or "never", v.get("last_note") or ""))

    if not s["graded"]:
        print("\n  Nothing graded yet. Forward returns need the window to")
        print("  close first — run `grade` once disclosures are 10+ trading")
        print("  days old. Until then this tool has a dataset and no record.")
        return

    print("\n" + "=" * 70)
    print("WHAT THE FILER GOT vs WHAT THE PUBLIC COULD GET")
    print("=" * 70)
    print("  Paired rows only — both returns from the same disclosure.\n")
    for h in HORIZONS:
        g = anchor_gap(conn, h, "P")
        t, n = g["trade"], g["notify"]
        if not t["n"]:
            continue
        print("  {}d purchases   (n={} paired)".format(h, t["n"]))
        print("      from trade date   hit {:<7} mean {}".format(
            "--" if t["hit"] is None else "{:.1%}".format(t["hit"]),
            _pct(t["mean"])))
        print("      from notify date  hit {:<7} mean {}".format(
            "--" if n["hit"] is None else "{:.1%}".format(n["hit"]),
            _pct(n["mean"])))
        if t["mean"] is not None and n["mean"] is not None:
            print("      the delay costs   {}".format(
                _pct(n["mean"] - t["mean"])))
        for s, d in sorted(g["by_source"].items()):
            if not d["trade"]["n"]:
                continue
            print("        {:<11} n={:<5} trade {:<9} notify {:<9} gap {}"
                  .format(s, d["trade"]["n"], _pct(d["trade"]["mean"]),
                          _pct(d["notify"]["mean"]),
                          _pct((d["notify"]["mean"] or 0)
                               - (d["trade"]["mean"] or 0))))
    g = anchor_gap(conn, HORIZONS[0], "P")
    if g.get("delay_days"):
        print("\n  mean disclosure delay {:.1f} days over {} congressional "
              "rows".format(g["delay_days"], g["delay_n"]))
    print("  A 13F trade date is the quarter END, not an execution time, so")
    print("  its gap is partly the length of a quarter. Read the congressional")
    print("  rows for the disclosure-delay question; they are the ones where")
    print("  both dates mean what they say.")
    try:
        import political_hypotheses as ph
        print("\n" + "=" * 70)
        print("PRE-REGISTERED PREDICTIONS")
        print("=" * 70)
        print(ph.format_block(conn=conn))
    except Exception as e:                             # noqa: BLE001
        print("\n  (hypothesis register unavailable: {})".format(e))


def print_leaderboard(conn=None, horizon: int = 20, anchor: str = "notify",
                      direction: str = "P", by: str = "filer_name",
                      min_n: int = 10, top: int = 15):
    lb = leaderboard(conn, horizon, anchor, direction, by, min_n)
    base, perm = lb["base"], lb["permutation"]
    print("\n" + "=" * 70)
    print("LEADERBOARD — {} by {}, {}d from {} date".format(
        "purchases" if direction == "P" else "sales", by, horizon, anchor))
    print("=" * 70)
    if base["n"]:
        print("  base rate over everything: n={} hit {:.1%} mean {}".format(
            base["n"], base["hit"], _pct(base["mean"])))
    if not lb["table"]:
        print("  Not enough graded rows yet (need {} per group).".format(min_n))
        return
    print("  {:<38} {:>5} {:>9} {:>8}".format("", "n", "mean", "hit"))
    for row in lb["table"][:top]:
        print("  {:<38} {:>5} {:>9} {:>8}".format(
            str(row["key"])[:38], row["n"], _pct(row["mean"]),
            "{:.0%}".format(row["hit"])))

    print("\n  " + "-" * 66)
    for w in lb.get("warnings", []):
        print("  ! {}".format(w))
    if lb.get("warnings"):
        print("  " + "-" * 66)
    if perm.get("p") is None:
        print("  No family-wise test: {}".format(perm.get("reason")))
        return
    print("  Permutation test over {} groups, {} shuffles:".format(
        perm["groups"], perm["iters"]))
    print("      leader        {} (n={})".format(
        str(perm["leader"])[:40], perm["n_leader"]))
    print("      observed mean {}".format(_pct(perm["observed"])))
    print("      p             {:.3f}".format(perm["p"]))
    if perm["p"] > 0.10:
        print("\n  READ THIS AS NOISE. A pool with no skill in it at all")
        print("  produces a leader this good {:.0%} of the time. The name at"
              .format(perm["p"]))
        print("  the top of this table is the winner of a lottery that was")
        print("  always going to have one.")
    elif perm["p"] > 0.05:
        print("\n  Weak. Survives the correction but not comfortably, and")
        print("  this is still a retrospective screen. Register it in")
        print("  hypotheses.py and judge it on rows that arrive after.")
    else:
        print("\n  Survives the family-wise correction at this sample size.")
        print("  That is not the same as an edge: the screen still chose")
        print("  this horizon and this direction after seeing the data.")
        print("  Register it in hypotheses.py before acting on it.")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def _cli_progress(source, who, res):
    print("    [{}] {:<28} rows={} skipped={}".format(
        source, str(who)[:28], res["new_rows"], res["skipped"]), flush=True)


def main(argv):
    cmd = argv[0] if argv else "report"
    conn = connect()
    try:
        if cmd == "backfill-congress":
            yr = int(argv[1]) if len(argv) > 1 else 2024
            print("Reading every congressional PTR since {} ...".format(yr))
            out = backfill_all_congress(conn, yr, progress=_cli_progress)
            for k, v in out.items():
                print("  {:<12} docs={} rows={} skipped={}".format(
                    k, v["docs"], v["new_rows"], v["skipped"]))
                for reason, n in sorted(v["errors"].items(),
                                        key=lambda x: -x[1]):
                    print("      {:>4}  {}".format(n, reason))
            n = conn.execute(
                "SELECT COUNT(DISTINCT filer_id) FROM disclosures WHERE "
                "source IN ('house_ptr','senate_ptr') AND filer_id IS NOT NULL"
            ).fetchone()[0]
            print()
            print("  distinct members now in the store: {}".format(n))

        elif cmd == "backfill":
            days = int(argv[1]) if len(argv) > 1 else 30
            print("Backfilling {} days into {} ...".format(days, DB_FILE))
            out = backfill(days, conn=conn, progress=_cli_progress)
            for k, v in out.items():
                print("  {:<12} docs={} rows={} skipped={}".format(
                    k, v["docs"], v["new_rows"], v["skipped"]))
                for reason, n in sorted(v["errors"].items(), key=lambda x: -x[1]):
                    print("      {:>4}  {}".format(n, reason))
            print_report(conn)

        elif cmd == "scan":
            out = scan_once(conn=conn, progress=_cli_progress)
            total = sum(v["new_rows"] for v in out.values())
            print("{} new rows".format(total))
            for k, v in out.items():
                if v["errors"]:
                    print("  {}: {}".format(k, dict(v["errors"])))

        elif cmd == "grade":
            lim = int(argv[1]) if len(argv) > 1 else None
            st = grade(conn, lim, progress=lambda i, n, t, m: print(
                "  [{}/{}] {:<8} {}".format(i, n, t, m), flush=True))
            print(json.dumps(st, indent=2))

        elif cmd == "report":
            print_report(conn)

        elif cmd == "leaderboard":
            by = argv[1] if len(argv) > 1 else "filer_name"
            h = int(argv[2]) if len(argv) > 2 else 20
            print_leaderboard(conn, horizon=h, by=by)

        elif cmd == "influence":
            if len(argv) > 1:
                d = influence_for(conn, argv[1].upper())
                print(json.dumps(d, indent=2) if d else "no influence data "
                      "for {} — run `influence` with no argument first"
                      .format(argv[1].upper()))
            else:
                res = scan_influence(conn, progress=_cli_progress)
                print("{} ticker-years, {} skipped".format(
                    res["docs"], res["skipped"]))
                rows = conn.execute(
                    "SELECT ticker, matched_name, lobby_spend, contract_total"
                    " FROM influence WHERE year=? AND lobby_spend > 0"
                    " ORDER BY lobby_spend DESC LIMIT 15",
                    (date.today().year,)).fetchall()
                print("\n  {:<8} {:<26} {:>14} {:>16}".format(
                    "ticker", "matched as", "lobby spend", "contracts"))
                for r in rows:
                    print("  {:<8} {:<26} {:>14,.0f} {:>16,.0f}".format(
                        r["ticker"], (r["matched_name"] or "")[:26],
                        r["lobby_spend"] or 0, r["contract_total"] or 0))

        elif cmd == "sectors":
            if len(argv) > 1 and argv[1] == "list":
                counts = sector_counts(conn)
                tot = conn.execute(
                    "SELECT COUNT(*) FROM sectors WHERE sector IS NULL"
                ).fetchone()[0]
                print()
                print("  {:<26} {:>6}".format("sector", "names"))
                for k, v in sorted(counts.items(), key=lambda kv: -kv[1]):
                    grp = next((g for g, names in SECTOR_GROUPS.items()
                                if k in names), "")
                    print("  {:<26} {:>6}   {}".format(k, v, grp))
                print("  {:<26} {:>6}".format("unclassified", tot))
            else:
                st = fetch_sectors(conn, progress=lambda i, n, t, s: print(
                    "  [{}/{}] {:<7} {}".format(i, n, t, s), flush=True))
                print(json.dumps(st, indent=2))

        elif cmd == "publish":
            out = publish(conn=conn)
            print("wrote {} — {} rows, {:,} bytes".format(
                out["path"], out["rows"], out["bytes"]))

        elif cmd == "import":
            p = argv[1] if len(argv) > 1 else SNAPSHOT_FILE
            print("imported {} rows from {}".format(import_snapshot(p, conn), p))

        elif cmd == "watch":
            secs = int(argv[1]) if len(argv) > 1 else 1800
            w = ScanWorker(tiers={"congress": secs},
                           on_event=lambda k, m, s: print(
                               "  [{}] {}".format(k, m), flush=True))
            w.start()
            print("Scanning every {}s. Ctrl-C to stop.".format(secs))
            try:
                while w.is_alive():
                    w.join(1.0)
            except KeyboardInterrupt:
                w.stop()
                w.join(10)

        else:
            print(__doc__)
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
