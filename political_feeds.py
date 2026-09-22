"""
political_feeds.py
==================
Fetchers and parsers for public disclosure filings. One function per source,
each returning normalised rows. No storage, no state, no GUI — everything
here is a pure read of a public endpoint plus a parse.

SOURCES AND THEIR LAG
---------------------
    house_ptr    House periodic transaction reports     30-45 day lag
    senate_ptr   Senate periodic transaction reports    30-45 day lag
    form4        SEC insider transactions               2 business days
    f13          SEC 13F institutional holdings         quarterly, 45 day lag
    lobbying     Senate LDA quarterly filings           quarterly
    contracts    USAspending awards                     rolling

None of these need an API key. SEC asks for a contact address in the
User-Agent; set SEC_USER_AGENT, following the same convention value_engine.py
already uses.

WHAT A ROW LOOKS LIKE
---------------------
Every parser emits the same dict. Fields a source cannot supply are None
rather than a default, because a defaulted strike price is indistinguishable
from a real one three months later. See ROW_FIELDS.

THE AMOUNT IS A RANGE
---------------------
Congressional filings disclose a bucket, not a figure: "$15,001 - $50,000".
amount_lo and amount_hi carry the bucket edges and there is deliberately no
amount_mid. A midpoint is an invention, and every downstream consumer that
wants one should have to write it itself and own the choice.

LEGAL NOTE
----------
Congressional disclosure reports carry a statutory use restriction — Ethics
in Government Act Title 1, quoted on the Senate eFD agreement as
5 U.S.C. app. section 105(c). Non-commercial personal use and free sharing
are outside it. See COPY_TRADING_PLAN.md. The SEC, LDA and USAspending
sources carry no equivalent restriction.
"""

import os
import re
import io
import csv
import json
import time
import gzip
import zipfile
import threading
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any, Tuple

import requests


_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_DIR, "political_cache")

CONTACT = os.environ.get("SEC_USER_AGENT", "squeeze-toolkit copy-trading")
UA = "squeeze-toolkit/1.0 ({})".format(CONTACT)

HOUSE_BASE = "https://disclosures-clerk.house.gov/public_disc"
SENATE_BASE = "https://efdsearch.senate.gov"
SEC_BASE = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"
LDA_BASE = "https://lda.senate.gov/api/v1"
USASPENDING = "https://api.usaspending.gov/api/v2"
LEGISLATORS = "https://unitedstates.github.io/congress-legislators"


# ─────────────────────────────────────────────
# ROW SCHEMA
# ─────────────────────────────────────────────

ROW_FIELDS = (
    "id", "source", "filer_name", "filer_id", "chamber", "party", "state",
    "committees", "ticker", "asset_name", "asset_type", "txn_type", "owner",
    "trade_date", "notify_date", "file_date", "filer_notified",
    "amount_lo", "amount_hi",
    "shares", "price", "option_type", "strike", "expiry",
    "doc_id", "doc_url", "fetched_at", "raw", "parse_flags",
)

# THE THREE DATES, AND WHY THERE ARE THREE
# ----------------------------------------
#   trade_date      when the transaction happened
#   notify_date     when the PUBLIC could first see it — the only date
#                   anyone else could have acted on, and the only one a
#                   claim about a tradeable edge may be measured from
#   filer_notified  House PTRs only: the "Notification Date" column ON the
#                   filing, which is when the FILER was told. For a managed
#                   account or a trust that is weeks before they filed.
#
# Reading the House form's Notification Date as the public date understates
# the disclosure lag by 13.4 days on average, measured across the 329
# House rows in the 2026-09-20 store: 17.1 days against a true 30.5. Nancy
# Pelosi's 2026-07-24 purchases carry a Notification Date of 2026-07-24 and
# were filed on 2026-08-21 — twenty-eight days during which the trade was
# not public and the "10-day return from disclosure" was being measured
# from a day nobody could trade on.
#
# Senate, Form 4 and 13F rows have no such column; for those, notify_date
# and file_date are the same filing date and always were.


def blank_row() -> dict:
    """A row with every field present and set to None.

    Parsers fill what they can. A field a source does not carry stays None,
    which is a different thing from zero and has to survive the round trip
    to storage as a different thing.
    """
    r = {k: None for k in ROW_FIELDS}
    r["parse_flags"] = []
    return r


# Owner codes as they appear on House PTRs and Senate eFD.
OWNER_MAP = {
    "SP": "spouse", "JT": "joint", "DC": "child",
    "Self": "self", "Joint": "joint", "Spouse": "spouse",
    "Dependent Child": "child",
}

# Transaction codes. The House writes "S (partial)"; the Senate spells out
# "Sale (Partial)". Both land on the same normalised value.
TXN_MAP = {
    "P": "P", "S": "S", "S (partial)": "S_partial", "E": "E",
    "Purchase": "P", "Sale": "S", "Sale (Full)": "S",
    "Sale (Partial)": "S_partial", "Exchange": "E",
}


# ─────────────────────────────────────────────
# HTTP — one polite token bucket per host
# ─────────────────────────────────────────────

class _TokenBucket:
    """Same shape as the limiter in yfinance_throttle.py.

    Rate is requests per second, burst is how many may go at once. SEC
    publishes a 10 req/sec ceiling; every default here sits far below the
    host's stated or implied limit, because none of these feeds is urgent
    enough to be worth an IP block.
    """

    def __init__(self, rate: float, burst: int):
        self.rate = float(rate)
        self.capacity = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity,
                    self._tokens + (now - self._last) * self.rate,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(min(wait, 1.0))


_BUCKETS = {
    "disclosures-clerk.house.gov": _TokenBucket(2.0, 4),
    "efdsearch.senate.gov": _TokenBucket(1.0, 2),
    "www.sec.gov": _TokenBucket(5.0, 8),
    "data.sec.gov": _TokenBucket(5.0, 8),
    "lda.senate.gov": _TokenBucket(2.0, 4),
    "api.usaspending.gov": _TokenBucket(2.0, 4),
    "unitedstates.github.io": _TokenBucket(5.0, 8),
}
_DEFAULT_BUCKET = _TokenBucket(1.0, 2)


def _bucket_for(url: str) -> _TokenBucket:
    m = re.match(r"https?://([^/]+)", url)
    return _BUCKETS.get(m.group(1), _DEFAULT_BUCKET) if m else _DEFAULT_BUCKET


def _request(method: str, url: str, session=None, tries: int = 3, **kw):
    """Throttled request with a bounded retry. Returns the response or None.

    Never raises. A feed that cannot reach its host has to degrade to "no
    rows this pass" rather than take down a scan loop that is also serving
    five other sources.
    """
    sess = session or requests
    kw.setdefault("timeout", 45)
    headers = dict(kw.pop("headers", {}) or {})
    headers.setdefault("User-Agent", UA)
    headers.setdefault("Accept-Encoding", "gzip, deflate")
    last = None
    for attempt in range(tries):
        _bucket_for(url).acquire()
        try:
            r = sess.request(method, url, headers=headers, **kw)
            if r.status_code in (429, 503):
                time.sleep(2.0 * (attempt + 1))
                last = r
                continue
            return r
        except Exception as e:                      # noqa: BLE001
            last = e
            time.sleep(1.0 * (attempt + 1))
    if isinstance(last, requests.Response):
        return last
    return None


def _get(url, **kw):
    return _request("GET", url, **kw)


def _post(url, **kw):
    return _request("POST", url, **kw)


# ─────────────────────────────────────────────
# DISK CACHE
# ─────────────────────────────────────────────

def _cache_path(*parts) -> str:
    p = os.path.join(CACHE_DIR, *parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def _cached_bytes(key: str, fetch, ttl_days: Optional[float] = None):
    """Fetch-once-keep-forever cache for immutable documents.

    A filed PTR never changes; an amended one is a new DocID. So documents
    are cached with no expiry by default and ttl_days is only for the index
    files that genuinely do change.
    """
    p = _cache_path(key)
    if os.path.exists(p):
        if ttl_days is None:
            return open(p, "rb").read()
        age = (time.time() - os.path.getmtime(p)) / 86400.0
        if age < ttl_days:
            return open(p, "rb").read()
    data = fetch()
    if data:
        tmp = p + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p)
    elif os.path.exists(p):
        return open(p, "rb").read()      # stale beats nothing
    return data


# ─────────────────────────────────────────────
# ROSTER — who is in Congress, and on what
# ─────────────────────────────────────────────

def load_legislators(ttl_days: float = 7.0) -> List[dict]:
    """Current members with bioguide id, party, state, district, chamber."""
    def fetch():
        r = _get(LEGISLATORS + "/legislators-current.json")
        return r.content if r is not None and r.status_code == 200 else None

    raw = _cached_bytes("roster/legislators-current.json", fetch, ttl_days)
    if not raw:
        return []
    try:
        people = json.loads(raw.decode("utf-8"))
    except Exception:
        return []
    out = []
    for p in people:
        name = p.get("name", {}) or {}
        terms = p.get("terms") or []
        if not terms:
            continue
        t = terms[-1]
        out.append({
            "bioguide": (p.get("id", {}) or {}).get("bioguide"),
            "first": name.get("first"),
            "last": name.get("last"),
            "official_full": name.get("official_full"),
            "chamber": "house" if t.get("type") == "rep" else "senate",
            "party": (t.get("party") or "")[:1] or None,
            "state": t.get("state"),
            "district": t.get("district"),
        })
    return out


def load_committee_membership(ttl_days: float = 7.0) -> Dict[str, List[str]]:
    """bioguide id -> list of committee names they sit on.

    Committee membership is the axis Quiver builds its committee strategies
    on. It is also the axis most likely to produce a spurious result, since
    there are ~20 standing committees and testing all of them is twenty
    chances to find a pattern. The engine's ranking layer applies the
    permutation correction; this function only supplies the mapping.
    """
    def fetch_m():
        r = _get(LEGISLATORS + "/committee-membership-current.json")
        return r.content if r is not None and r.status_code == 200 else None

    def fetch_c():
        r = _get(LEGISLATORS + "/committees-current.json")
        return r.content if r is not None and r.status_code == 200 else None

    mraw = _cached_bytes("roster/committee-membership.json", fetch_m, ttl_days)
    craw = _cached_bytes("roster/committees.json", fetch_c, ttl_days)
    if not mraw:
        return {}
    try:
        members = json.loads(mraw.decode("utf-8"))
        committees = json.loads(craw.decode("utf-8")) if craw else []
    except Exception:
        return {}

    names = {}
    for c in committees:
        names[c.get("thomas_id")] = c.get("name")
        for sub in c.get("subcommittees", []) or []:
            names[(c.get("thomas_id") or "") + (sub.get("thomas_id") or "")] = \
                "{} - {}".format(c.get("name"), sub.get("name"))

    out = {}
    for code, people in members.items():
        label = names.get(code, code)
        for p in people:
            bg = p.get("bioguide")
            if bg:
                out.setdefault(bg, []).append(label)
    return out


class Roster:
    """Name -> bioguide resolution, with the match quality recorded.

    House filings give last name, first name and state/district. Senate
    filings give first and last. Neither gives a bioguide id, so every match
    is a join on a human-entered string and some of them will be wrong.
    A row whose filer could not be matched keeps filer_id None and carries
    an 'unmatched_filer' flag rather than being dropped, because a trade by
    someone this roster does not recognise is still a trade.
    """

    def __init__(self):
        self.people = load_legislators()
        self.committees = load_committee_membership()
        self._by_key = {}
        for p in self.people:
            if not p["bioguide"]:
                continue
            last = _norm_name(p["last"])
            self._by_key.setdefault(("last", last), []).append(p)
            if p["chamber"] == "house" and p["state"] and p["district"] is not None:
                sd = "{}{:02d}".format(p["state"], int(p["district"]))
                self._by_key[("sd", sd, last)] = [p]
                self._by_key[("sd", sd)] = [p]

    def match(self, last: str, first: str = "",
              state_dst: str = "", chamber: str = "") -> Tuple[Optional[dict], str]:
        """Return (person, quality) where quality is exact | name | none."""
        last_n = _norm_name(last)
        sd = (state_dst or "").strip().upper()
        if sd:
            hit = self._by_key.get(("sd", sd, last_n))
            if hit:
                return hit[0], "exact"
            # Compound surnames are split differently by the two sources:
            # the House index files "Delaney, April McClain" where the
            # roster carries the surname "McClain Delaney". A district has
            # exactly one member, so the district alone identifies them —
            # but only accept it when the two surnames share a word, or a
            # filing by a departed member would be attributed to whoever
            # holds the seat now.
            seat = self._by_key.get(("sd", sd))
            if seat:
                mine = set(last_n.split())
                theirs = set(_norm_name(seat[0]["last"]).split())
                if mine & theirs:
                    return seat[0], "exact"
        cands = self._by_key.get(("last", last_n), [])
        if chamber:
            cands = [c for c in cands if c["chamber"] == chamber] or cands
        if len(cands) == 1:
            return cands[0], "name"
        if len(cands) > 1 and first:
            f = _norm_name(first).split()[0] if first.strip() else ""
            tight = [c for c in cands
                     if _norm_name(c["first"] or "").startswith(f)]
            if len(tight) == 1:
                return tight[0], "name"
        return None, "none"

    def decorate(self, row: dict, last: str, first: str = "",
                 state_dst: str = "", chamber: str = "") -> dict:
        person, quality = self.match(last, first, state_dst, chamber)
        if person:
            row["filer_id"] = person["bioguide"]
            row["party"] = person["party"]
            row["state"] = person["state"]
            row["chamber"] = person["chamber"]
            row["committees"] = json.dumps(
                self.committees.get(person["bioguide"], []))
            if quality != "exact":
                row["parse_flags"].append("filer_matched_by_name")
        else:
            row["parse_flags"].append("unmatched_filer")
            row["committees"] = json.dumps([])
            if chamber:
                row["chamber"] = chamber
        return row


def _norm_name(s: Optional[str]) -> str:
    """Fold a name to bare lowercase letters, accents included.

    The House index writes "Sanchez"; the roster writes "Sánchez". Stripping
    non-ASCII instead of folding it turns the second into "snchez" and the
    two never meet, which silently drops every trade by that member. So the
    accent is decomposed and its combining mark discarded rather than the
    whole character.
    """
    import unicodedata
    s = unicodedata.normalize("NFKD", (s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ─────────────────────────────────────────────
# HOUSE — bulk index, then one PDF per filing
# ─────────────────────────────────────────────

# Filing types in the bulk index. P is the one that carries transactions;
# the rest are annual reports, amendments, extensions and terminations.
HOUSE_PTR_TYPE = "P"


def house_is_efiled(doc_id: str) -> bool:
    """Whether a DocID belongs to the electronic filing system.

    Measured across all 395 of 2026's House PTRs: every one of the 349
    DocIDs of the form 2XXXXXXX carried an extractable text layer, and every
    one of the 46 seven-digit IDs beginning 8 or 9 was a scan with no text
    at all. The split is the filing system, not the member — the same person
    can appear on both sides across years.

    Checking this before the fetch saves downloading 46 documents that
    cannot be read, and turns "some filings failed" into a coverage
    statement that names exactly who is missing and why.
    """
    d = (doc_id or "").strip()
    return d.isdigit() and len(d) >= 8 and d.startswith("2")


def house_index(year: int, ttl_days: float = 0.02) -> List[dict]:
    """Every filing the House Clerk has published for `year`.

    The zip is about 60KB and is the cheap poll that drives the whole House
    side — the expensive PDF fetch only happens for a DocID this index has
    not shown before. ttl_days defaults to roughly 30 minutes.
    """
    url = "{}/financial-pdfs/{}FD.zip".format(HOUSE_BASE, year)

    def fetch():
        r = _get(url)
        return r.content if r is not None and r.status_code == 200 else None

    raw = _cached_bytes("house/{}FD.zip".format(year), fetch, ttl_days)
    if not raw:
        return []
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        name = next((n for n in zf.namelist() if n.lower().endswith(".txt")),
                    None)
        if not name:
            return []
        text = zf.read(name).decode("utf-8-sig", errors="replace")
    except Exception:
        return []

    out = []
    rows = text.splitlines()
    if not rows:
        return []
    for line in rows[1:]:
        parts = line.split("\t")
        if len(parts) < 9:
            continue
        out.append({
            "prefix": parts[0].strip(), "last": parts[1].strip(),
            "first": parts[2].strip(), "suffix": parts[3].strip(),
            "filing_type": parts[4].strip(), "state_dst": parts[5].strip(),
            "year": parts[6].strip(), "filing_date": parts[7].strip(),
            "doc_id": parts[8].strip(),
        })
    return out


def house_ptr_pdf(doc_id: str, year: int) -> Optional[bytes]:
    url = "{}/ptr-pdfs/{}/{}.pdf".format(HOUSE_BASE, year, doc_id)

    def fetch():
        r = _get(url)
        if r is not None and r.status_code == 200 and \
                r.content[:4] == b"%PDF":
            return r.content
        return None

    return _cached_bytes("house/pdf/{}/{}.pdf".format(year, doc_id), fetch)


def _pdf_text(raw: bytes) -> Optional[str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError(
            "pypdf is required for House PTR parsing: pip install pypdf")
    try:
        rd = PdfReader(io.BytesIO(raw))
        text = "\n".join((p.extract_text() or "") for p in rd.pages)
    except Exception:
        return None
    # The form's headings and field labels are set with wide letter-spacing,
    # and pypdf renders that spacing as NUL bytes rather than spaces:
    # "FILING STATUS:" arrives as "F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00:".
    # Every pattern below is written against whitespace, so leaving the NULs
    # in place makes the label lines unmatchable — which folds a row's
    # trailing metadata into the NEXT row's asset description and eats the
    # SP/JT/DC owner code sitting at the front of it. The visible symptom is
    # that every spouse and joint trade in the file reads as the member's own.
    return text.replace("\x00", " ")


# Page furniture that repeats on every page of a multi-page PTR and has to
# come out before the transaction table can be read as one run of text.
_HOUSE_NOISE = [
    r"Filing ID\s*#\s*\d+",
    r"ID Owner Asset Transaction",
    r"^\s*Type\s*$",
    r"^\s*Date Notification\s*$",
    r"^\s*Date\s*$",
    r"^\s*Amount Cap\.\s*$",
    r"^\s*Gains\s*>\s*$",
    r"^\s*\$200\?\s*$",
    r"^\s*\*\s*For the complete list of asset type abbreviations.*$",
    r"^\s*Clerk of the House of Representatives.*$",
    # Section headings are set in the PDF with wide letter-spacing, which
    # pypdf renders as isolated capitals: "PERIODIC TRANSACTION REPORT"
    # arrives as "P        T           R". Left in place they prepend a
    # stray capital to the next asset description, which then swallows the
    # SP/JT/DC owner code sitting in front of it and every transaction in
    # the filing is silently attributed to the member instead of a spouse.
    r"^\s*[A-Z]\s*$",
    r"^\s*[A-Z](\s{2,}[A-Z]+)+\s*$",
]

# The trailing metadata attached to a transaction. pypdf collapses the PDF's
# letter-spaced labels ("FILING STATUS") into runs like "F      S     :", so
# these are matched on shape rather than on the literal label text.
_HOUSE_META = re.compile(r"^\s*[A-Z][A-Z\s]{0,22}:\s")

# The tail of a transaction row: type, trade date, notification date, amount
# bucket. Everything before it on the run is the asset description.
# The amount is usually a two-sided bucket ("$15,001 - $50,000") but the top
# category is one-sided and prefixed with words instead: Doris Matsui's
# 2025-12-16 filing records four Treasury purchases as "Spouse/DC Over
# $1,000,000". Requiring a range dropped every transaction in that filing.
_HOUSE_TAIL = re.compile(
    r"(?P<txn>S\s*\(partial\)|[PSE])\s+"
    r"(?P<td>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<nd>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<amt>(?:[A-Za-z/]{2,10}\s+){0,3}\$[\d,]+(?:\s*-\s*\$?[\d,]+)?)",
    re.IGNORECASE,
)


def parse_house_ptr(text: str, meta: dict) -> List[dict]:
    """Pull transactions out of one House PTR's extracted text.

    The layout is a table that pypdf flattens into a run where each row is
    [owner?] [asset, possibly wrapped over lines] [type] [date] [date]
    [amount], followed by any of FILING STATUS / SUBHOLDING OF / LOCATION /
    DESCRIPTION lines that belong to the row just read.

    So transactions are found by their tail — the two dates and the dollar
    bucket, which is the only part of the row with a fixed shape — and the
    asset is whatever sits between the previous tail and this one, minus
    the previous row's metadata lines.
    """
    lines = text.splitlines()
    keep = []
    for ln in lines:
        if any(re.search(p, ln, re.IGNORECASE | re.MULTILINE)
               for p in _HOUSE_NOISE):
            continue
        keep.append(ln)
    body = "\n".join(keep)

    # Everything before the first transaction is the filer header; everything
    # after the last is the certification block. The header ends at the last
    # of the filer fields, whichever of them this filing carries.
    start = 0
    for anchor in (r"State/District:\s*\S+", r"Status:\s*\S+",
                   r"Name:\s*\S+"):
        m = re.search(anchor, body)
        if m and m.end() > start:
            start = m.end()
    body = body[start:]
    for stop in ("I CERTIFY that the statements",
                 "Digitally Signed:"):
        i = body.find(stop)
        if i > 0:
            body = body[:i]

    rows = []
    matches = list(_HOUSE_TAIL.finditer(body))
    prev_end = 0
    for m in matches:
        chunk = body[prev_end:m.start()]
        prev_end = m.end()
        rows.append(_house_row(chunk, m, meta))
    return [r for r in rows if r]


def _house_row(chunk: str, m, meta: dict) -> Optional[dict]:
    # Strip the previous transaction's trailing metadata lines.
    asset_lines = []
    for ln in chunk.splitlines():
        if _HOUSE_META.match(ln):
            continue
        if ln.strip():
            asset_lines.append(ln.strip())
    asset = re.sub(r"\s+", " ", " ".join(asset_lines)).strip()

    owner = None
    om = re.match(r"^(SP|JT|DC)\b\s*", asset)
    if om:
        owner = OWNER_MAP.get(om.group(1))
        asset = asset[om.end():].strip()
    else:
        owner = "self"

    if not asset:
        return None

    row = blank_row()
    row["source"] = "house_ptr"
    row["owner"] = owner
    row["asset_name"] = asset
    row["asset_type"] = _bracket_code(asset)
    row["ticker"] = _house_ticker(asset)
    t = re.sub(r"\s+", " ", m.group("txn")).strip().upper()
    row["txn_type"] = "S_partial" if t.startswith("S (") else TXN_MAP.get(t)
    row["trade_date"] = _mdy(m.group("td"))
    # The form's second date column is the FILER's notification, not the
    # public's. See the note beside ROW_FIELDS — notify_date is set from the
    # filing date below, because that is the day the document existed.
    row["filer_notified"] = _mdy(m.group("nd"))
    row["notify_date"] = _mdy(meta.get("filing_date"))
    amt = m.group("amt")
    lo, hi = _amount_range(amt)
    row["amount_lo"], row["amount_hi"] = lo, hi
    if re.search(r"\bover\b", amt, re.IGNORECASE):
        # An open-topped bucket. hi stays None rather than being filled with
        # lo, so nothing downstream can read "over $1,000,000" as exactly a
        # million.
        row["amount_hi"] = None
        row["parse_flags"].append("amount_open_top")
    row["file_date"] = _mdy(meta.get("filing_date"))
    row["doc_id"] = meta.get("doc_id")
    row["doc_url"] = "{}/ptr-pdfs/{}/{}.pdf".format(
        HOUSE_BASE, meta.get("year"), meta.get("doc_id"))
    row["filer_name"] = "{} {}".format(
        meta.get("first", ""), meta.get("last", "")).strip()
    row["chamber"] = "house"
    row["raw"] = re.sub(r"\s+", " ",
                        (chunk + " " + m.group(0)))[-400:].strip()
    _apply_option_detail(row, asset)
    _sanity_flags(row)
    return row


def _bracket_code(asset: str) -> Optional[str]:
    codes = re.findall(r"\[([A-Z]{2})\]", asset)
    return codes[-1] if codes else None


def _house_ticker(asset: str) -> Optional[str]:
    """The ticker is parenthesised, and it is not the only thing that is.

    "Alcon Inc. Ordinary Shares (ALC) [ST]" gives ALC. But municipal bonds
    carry no ticker at all, CUSIPs appear in the same position, and some
    descriptions parenthesise a share class. So this accepts only a short
    all-caps token, rejects anything that looks like a CUSIP, and returns
    None rather than guessing — an unresolved ticker is recoverable later
    from asset_name, a wrong one is not.
    """
    cands = re.findall(r"\(([A-Z0-9.\-]{1,7})\)", asset)
    for c in reversed(cands):
        if c.isdigit():
            continue
        if len(c) >= 6 and any(ch.isdigit() for ch in c):
            continue                      # CUSIP fragment
        if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", c):
            return c
    return None


def _leading_ticker(asset: str) -> Optional[str]:
    """Senate filings sometimes put the symbol in front of the name.

    When the eFD ticker column is "--" the symbol is often still there, as
    a prefix: "EA - Electronic Arts Inc", "SDZNY- Sandoz Group AG ADR". The
    dash is required, which is what keeps "GS Managed Structured Note
    Strategy" — a product name that opens with two capitals and has no
    symbol at all — from being read as a holding in Goldman Sachs.
    """
    m = re.match(r"^([A-Z][A-Z0-9.]{0,6})\s*-\s+\S", asset or "")
    return m.group(1) if m else None


# Option detail appears on Senate filings as labelled fields and on House
# filings, when at all, as free text inside the asset description.
_OPT_TYPE = re.compile(r"Option Type:\s*(Call|Put)", re.IGNORECASE)
_OPT_STRIKE = re.compile(r"Strike price:\s*\$?\s*([\d,]+(?:\.\d+)?)",
                         re.IGNORECASE)
_OPT_EXP = re.compile(
    r"Expires:\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE)


def _apply_option_detail(row: dict, text: str):
    t = _OPT_TYPE.search(text)
    s = _OPT_STRIKE.search(text)
    e = _OPT_EXP.search(text)
    if t:
        row["option_type"] = t.group(1).lower()
    if s:
        row["strike"] = _money(s.group(1))
    if e:
        v = e.group(1)
        row["expiry"] = v if "-" in v else _mdy(v)
    if row["asset_type"] == "OP" and not row["option_type"]:
        row["parse_flags"].append("option_without_detail")


def _mdy(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip()
    # %Y%m%d is how the EDGAR daily index writes a date (20260917). Without
    # it every Form 4 row landed with notify_date None, which silently
    # removed the entire insider feed from the notify-anchored grading —
    # the one anchor that is actually tradeable.
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _money(s: Optional[str]) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _sanity_flags(row: dict):
    """Flag rows whose own dates contradict each other.

    Real example from the 2026 House set: Rep. Cohen's filing records a
    trade date of 12/26/2026 notified on 01/21/2026 — a typo for 2025 in the
    filing itself. Silently accepting it puts a trade eleven months in the
    future into the grader. Flagging it keeps the row and lets the engine
    decide, which is the same treatment squeeze_logger gives an impossible
    field: record it, mark it, do not repair it.
    """
    td, nd = row.get("trade_date"), row.get("notify_date")
    if td and nd and td > nd:
        row["parse_flags"].append("trade_after_notify")
    if td and td > date.today().isoformat():
        row["parse_flags"].append("trade_in_future")
    if row.get("amount_hi") and row.get("amount_lo") and \
            row["amount_hi"] < row["amount_lo"]:
        row["parse_flags"].append("amount_inverted")
    if not row.get("ticker"):
        row["parse_flags"].append("no_ticker")


def house_ptr_rows(filing: dict, roster: Optional[Roster] = None
                   ) -> Tuple[List[dict], Optional[str]]:
    """Fetch and parse one House PTR. Returns (rows, error_or_None)."""
    year = filing.get("year") or str(date.today().year)
    doc_id = filing.get("doc_id")
    if not doc_id:
        return [], "no doc_id"
    if not house_is_efiled(doc_id):
        return [], "paper filing (scanned, needs OCR)"
    raw = house_ptr_pdf(doc_id, int(year))
    if not raw:
        return [], "fetch failed"
    text = _pdf_text(raw)
    if text is None:
        return [], "pdf unreadable"
    if len(text.strip()) < 200:
        return [], "paper filing (scanned, needs OCR)"
    rows = parse_house_ptr(text, filing)
    if not rows:
        return [], "no transactions parsed"
    for i, r in enumerate(rows):
        if roster:
            roster.decorate(r, filing.get("last", ""), filing.get("first", ""),
                            filing.get("state_dst", ""), "house")
        r["id"] = "house_ptr:{}:{}".format(doc_id, i)
        r["fetched_at"] = datetime.utcnow().isoformat(timespec="seconds")
    return rows, None


# ─────────────────────────────────────────────
# SENATE — session, search, then one page per filing
# ─────────────────────────────────────────────

SENATE_REPORT_TYPE_PTR = 11


def senate_session() -> Optional[requests.Session]:
    """Establish an eFD session.

    The site requires an explicit acknowledgement of the Ethics in
    Government Act use restrictions before it will serve search results.
    That acknowledgement is a real legal statement, not a cookie banner —
    see the legal note at the top of this module and COPY_TRADING_PLAN.md.
    The session expires, so the engine re-establishes it rather than
    treating one as good for the life of the process.
    """
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    home = SENATE_BASE + "/search/home/"
    r = _get(home, session=s)
    if r is None or r.status_code != 200:
        return None
    m = re.search(
        r"name=['\"]csrfmiddlewaretoken['\"]\s+value=['\"]([^'\"]+)", r.text)
    if not m:
        return None
    r2 = _post(home, session=s,
               data={"csrfmiddlewaretoken": m.group(1),
                     "prohibition_agreement": "1"},
               headers={"Referer": home})
    if r2 is None or r2.status_code != 200:
        return None
    return s if s.cookies.get("csrftoken") else None


def senate_search(session: requests.Session, start: date,
                  end: Optional[date] = None,
                  report_type: int = SENATE_REPORT_TYPE_PTR,
                  page_size: int = 100) -> List[dict]:
    """Filings submitted between two dates. Paginates until exhausted."""
    if session is None:
        return []
    csrf = session.cookies.get("csrftoken")
    url = SENATE_BASE + "/search/report/data/"
    out, offset = [], 0
    while True:
        payload = {
            "start": str(offset), "length": str(page_size),
            "report_types": "[{}]".format(report_type), "filer_types": "[]",
            "submitted_start_date": start.strftime("%m/%d/%Y 00:00:00"),
            "submitted_end_date": end.strftime("%m/%d/%Y 23:59:59") if end else "",
            "candidate_state": "", "senator_state": "", "office_id": "",
            "first_name": "", "last_name": "",
            "csrfmiddlewaretoken": csrf,
        }
        r = _post(url, session=session, data=payload,
                  headers={"Referer": SENATE_BASE + "/search/",
                           "X-CSRFToken": csrf,
                           "X-Requested-With": "XMLHttpRequest"})
        if r is None or r.status_code != 200:
            break
        try:
            j = r.json()
        except Exception:
            break
        data = j.get("data") or []
        for row in data:
            if len(row) < 5:
                continue
            link = re.search(r'href="([^"]+)"', row[3])
            out.append({
                "first": _strip_tags(row[0]), "last": _strip_tags(row[1]),
                "office": _strip_tags(row[2]),
                "title": _strip_tags(row[3]),
                "filing_date": _mdy(_strip_tags(row[4])),
                "url": (SENATE_BASE + link.group(1)) if link else None,
            })
        offset += len(data)
        if len(data) < page_size or offset >= int(j.get("recordsTotal") or 0):
            break
    return out


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(s))).strip()


def parse_senate_ptr(html: str, meta: dict) -> List[dict]:
    """Read the transaction table out of an electronic Senate PTR.

    The Senate serves electronic filings as an HTML table, which is both
    easier to parse than the House PDFs and richer: it carries the option
    type, strike and expiry as separate fields rather than as free text.
    That detail is the one thing this toolkit can price and nobody else
    publishes, so it is extracted explicitly rather than folded into the
    asset name.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError("beautifulsoup4 is required for Senate parsing")

    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    headers = [th.get_text(" ", strip=True).lower()
               for th in table.find_all("th")]

    def col(*names):
        """Exact header match first, substring only as a fallback.

        The Senate table carries both "Asset Type" and "Type", and a
        substring search for "type" hits "asset type" first — which silently
        reads the asset class into the transaction column and leaves every
        buy and sell unclassified.
        """
        for n in names:
            for i, h in enumerate(headers):
                if h == n:
                    return i
        for n in names:
            for i, h in enumerate(headers):
                if n in h:
                    return i
        return None

    i_date = col("transaction date", "date")
    i_owner = col("owner")
    i_tick = col("ticker")
    i_asset = col("asset name")
    i_atype = col("asset type")
    i_type = col("type")
    i_amt = col("amount")
    i_cmt = col("comment")

    rows = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        cell = [td.get_text(" ", strip=True) for td in tds]

        def g(i):
            return cell[i] if i is not None and i < len(cell) else ""

        asset = g(i_asset)
        if not asset:
            continue
        row = blank_row()
        row["source"] = "senate_ptr"
        row["asset_name"] = re.sub(r"\s+", " ", asset).strip()
        tick = g(i_tick).strip()
        row["ticker"] = None if tick in ("", "--", "-") else tick.upper()
        if row["ticker"] is None:
            row["ticker"] = _leading_ticker(asset) or _house_ticker(asset)
        row["asset_type"] = _senate_asset_code(g(i_atype))
        row["owner"] = OWNER_MAP.get(g(i_owner).strip(), "self")
        row["txn_type"] = TXN_MAP.get(g(i_type).strip())
        if row["txn_type"] is None and g(i_type):
            t = g(i_type).lower()
            row["txn_type"] = ("S_partial" if "partial" in t else
                               "S" if "sale" in t else
                               "P" if "purchase" in t else
                               "E" if "exchange" in t else None)
        row["trade_date"] = _mdy(g(i_date))
        row["notify_date"] = meta.get("filing_date")
        row["file_date"] = meta.get("filing_date")
        lo, hi = _amount_range(g(i_amt))
        row["amount_lo"], row["amount_hi"] = lo, hi
        row["doc_id"] = _senate_doc_id(meta.get("url"))
        row["doc_url"] = meta.get("url")
        row["filer_name"] = "{} {}".format(
            meta.get("first", ""), meta.get("last", "")).strip()
        row["chamber"] = "senate"
        row["raw"] = " | ".join(cell)[:400]
        _apply_option_detail(row, asset + " " + g(i_cmt))
        _sanity_flags(row)
        rows.append(row)
    return rows


_SENATE_ASSET_CODES = {
    "stock": "ST", "stock option": "OP", "corporate bond": "CS",
    "municipal security": "GS", "other securities": "OT",
    "mutual fund": "MF", "exchange traded fund": "EF",
    "non-public stock": "NP",
}


def _senate_asset_code(label: str) -> Optional[str]:
    l = (label or "").strip().lower()
    for k, v in _SENATE_ASSET_CODES.items():
        if k in l:
            return v
    return "OT" if l else None


def _senate_doc_id(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/view/(?:ptr|paper)/([0-9a-f\-]+)", url)
    return m.group(1) if m else url.rstrip("/").rsplit("/", 1)[-1]


def _amount_range(s: str) -> Tuple[Optional[float], Optional[float]]:
    nums = re.findall(r"\$\s*([\d,]+)", s or "")
    if not nums:
        return None, None
    if len(nums) == 1:
        return _money(nums[0]), None
    return _money(nums[0]), _money(nums[1])


def senate_ptr_rows(session, filing: dict, roster: Optional[Roster] = None
                    ) -> Tuple[List[dict], Optional[str]]:
    url = filing.get("url")
    if not url:
        return [], "no url"
    if "/view/paper/" in url:
        # Paper filings are scans with no text layer. They are recorded as
        # a skip rather than silently ignored, so the coverage number stays
        # honest about what was not read.
        return [], "paper filing (scanned)"
    doc_id = _senate_doc_id(url)

    def fetch():
        r = _get(url, session=session)
        return r.content if r is not None and r.status_code == 200 else None

    raw = _cached_bytes("senate/{}.html".format(doc_id), fetch)
    if not raw:
        return [], "fetch failed"
    try:
        rows = parse_senate_ptr(raw.decode("utf-8", errors="replace"), filing)
    except RuntimeError:
        raise
    except Exception as e:                            # noqa: BLE001
        return [], "parse error: {}".format(e)
    if not rows:
        return [], "no transactions parsed"
    for i, r in enumerate(rows):
        if roster:
            roster.decorate(r, filing.get("last", ""), filing.get("first", ""),
                            "", "senate")
        r["id"] = "senate_ptr:{}:{}".format(doc_id, i)
        r["fetched_at"] = datetime.utcnow().isoformat(timespec="seconds")
    return rows, None


# ─────────────────────────────────────────────
# SEC FORM 4 — the freshest feed here
# ─────────────────────────────────────────────

def form4_index(day: date) -> List[dict]:
    """Form 4 filings from one day's EDGAR index.

    Form 4 is due within two business days of the transaction, which makes
    it roughly twenty times fresher than a congressional PTR. It is also the
    feed with no statutory use restriction. If anything in this tool turns
    out to carry a live signal, the prior should be on this one.
    """
    q = (day.month - 1) // 3 + 1
    url = "{}/Archives/edgar/daily-index/{}/QTR{}/form.{}.idx".format(
        SEC_BASE, day.year, q, day.strftime("%Y%m%d"))

    def fetch():
        r = _get(url)
        return r.content if r is not None and r.status_code == 200 else None

    raw = _cached_bytes("sec/idx/form.{}.idx".format(day.strftime("%Y%m%d")),
                        fetch)
    if not raw:
        return []
    out = []
    for line in raw.decode("latin-1").splitlines():
        if not line.startswith("4 "):
            continue
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 5:
            continue
        out.append({"form": parts[0], "company": parts[1], "cik": parts[2],
                    "date_filed": parts[3], "path": parts[-1]})
    return out


def form4_rows(entry: dict) -> Tuple[List[dict], Optional[str]]:
    path = entry.get("path", "")
    if not path:
        return [], "no path"
    url = "{}/Archives/{}".format(SEC_BASE, path.lstrip("/"))
    key = "sec/form4/{}".format(path.strip("/").replace("/", "_"))

    def fetch():
        r = _get(url)
        return r.content if r is not None and r.status_code == 200 else None

    raw = _cached_bytes(key, fetch)
    if not raw:
        return [], "fetch failed"
    text = raw.decode("utf-8", errors="replace")
    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", text,
                  re.DOTALL)
    if not m:
        return [], "no ownership document"
    try:
        rows = parse_form4(m.group(0), entry)
    except Exception as e:                            # noqa: BLE001
        return [], "parse error: {}".format(e)
    return rows, None if rows else "no transactions parsed"


def parse_form4(xml: str, entry: dict) -> List[dict]:
    """Non-derivative and derivative transactions from one Form 4.

    Only open-market purchases and sales are kept. Code A grants, F
    tax-withholding surrenders and M option exercises are mechanical events
    that happen on a vesting calendar rather than on a view, and mixing them
    into a "what did insiders buy" feed is the single easiest way to
    manufacture a signal that is really a payroll schedule.
    """
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)

    def txt(node, path, default=None):
        el = node.find(path)
        if el is None:
            return default
        v = el.find("value")
        s = (v.text if v is not None else el.text) or ""
        return s.strip() or default

    issuer = root.find("issuer")
    ticker = (txt(issuer, "issuerTradingSymbol") if issuer is not None
              else None)
    issuer_name = txt(issuer, "issuerName") if issuer is not None else None

    owner = root.find("reportingOwner")
    owner_name = None
    owner_cik = None
    if owner is not None:
        oid = owner.find("reportingOwnerId")
        if oid is not None:
            owner_name = txt(oid, "rptOwnerName")
            owner_cik = txt(oid, "rptOwnerCik")
        rel = owner.find("reportingOwnerRelationship")
        roles = []
        if rel is not None:
            if txt(rel, "isDirector") in ("1", "true"):
                roles.append("director")
            if txt(rel, "isOfficer") in ("1", "true"):
                roles.append(txt(rel, "officerTitle") or "officer")
            if txt(rel, "isTenPercentOwner") in ("1", "true"):
                roles.append("10% owner")

    rows = []
    for tag, is_deriv in (("nonDerivativeTransaction", False),
                          ("derivativeTransaction", True)):
        for t in root.iter(tag):
            code = txt(t, "transactionCoding/transactionCode")
            if code not in ("P", "S"):
                continue
            amounts = t.find("transactionAmounts")
            shares = _money(txt(amounts, "transactionShares")) \
                if amounts is not None else None
            price = _money(txt(amounts, "transactionPricePerShare")) \
                if amounts is not None else None
            row = blank_row()
            row["source"] = "form4"
            row["filer_name"] = owner_name
            row["filer_id"] = owner_cik
            row["chamber"] = "corporate"
            row["ticker"] = (ticker or "").upper() or None
            row["asset_name"] = issuer_name
            row["asset_type"] = "OP" if is_deriv else "ST"
            row["txn_type"] = code
            row["owner"] = "self"
            row["trade_date"] = _mdy(txt(t, "transactionDate"))
            row["notify_date"] = _mdy(entry.get("date_filed"))
            row["file_date"] = _mdy(entry.get("date_filed"))
            row["shares"] = shares
            row["price"] = price
            if shares is not None and price is not None:
                # Form 4 gives an exact figure, so lo and hi are the same
                # number. That is a real point estimate, unlike a PTR bucket.
                row["amount_lo"] = row["amount_hi"] = round(shares * price, 2)
            if is_deriv:
                row["strike"] = _money(txt(t, "conversionOrExercisePrice"))
                row["expiry"] = _mdy(txt(t, "expirationDate"))
                title = txt(t, "securityTitle") or ""
                if "put" in title.lower():
                    row["option_type"] = "put"
                elif "call" in title.lower() or "option" in title.lower():
                    row["option_type"] = "call"
            row["doc_id"] = entry.get("path")
            row["doc_url"] = "{}/Archives/{}".format(
                SEC_BASE, (entry.get("path") or "").lstrip("/"))
            row["committees"] = json.dumps(roles)
            row["raw"] = "{} {} {} sh @ {}".format(
                code, issuer_name, shares, price)[:400]
            row["fetched_at"] = datetime.utcnow().isoformat(timespec="seconds")
            _sanity_flags(row)
            rows.append(row)
    for i, r in enumerate(rows):
        r["id"] = "form4:{}:{}".format(entry.get("path"), i)
    return rows


# ─────────────────────────────────────────────
# SEC 13F — holdings, which the engine diffs into trades
# ─────────────────────────────────────────────

def f13_filings(cik: str, limit: int = 8) -> List[dict]:
    cik10 = str(cik).lstrip("0").zfill(10)

    def fetch():
        r = _get("{}/submissions/CIK{}.json".format(SEC_DATA, cik10))
        return r.content if r is not None and r.status_code == 200 else None

    raw = _cached_bytes("sec/subs/CIK{}.json".format(cik10), fetch, 1.0)
    if not raw:
        return []
    try:
        j = json.loads(raw.decode("utf-8"))
    except Exception:
        return []
    rec = (j.get("filings") or {}).get("recent") or {}
    forms = rec.get("form") or []
    out = []
    for i, f in enumerate(forms):
        if not str(f).startswith("13F-HR"):
            continue
        out.append({
            "cik": cik10.lstrip("0"), "name": j.get("name"),
            "form": f, "filing_date": rec["filingDate"][i],
            "accession": rec["accessionNumber"][i],
            "report_date": (rec.get("reportDate") or [None] * len(forms))[i],
        })
        if len(out) >= limit:
            break
    return out


def f13_holdings(filing: dict) -> Tuple[List[dict], Optional[str]]:
    """Positions from one 13F information table."""
    cik = str(filing["cik"]).lstrip("0")
    acc = filing["accession"].replace("-", "")
    base = "{}/Archives/edgar/data/{}/{}".format(SEC_BASE, cik, acc)

    def fetch_idx():
        r = _get(base + "/index.json")
        return r.content if r is not None and r.status_code == 200 else None

    idx = _cached_bytes("sec/13f/{}_{}_index.json".format(cik, acc), fetch_idx)
    if not idx:
        return [], "index fetch failed"
    try:
        items = json.loads(idx.decode("utf-8"))["directory"]["item"]
    except Exception:
        return [], "index unreadable"

    name = None
    for it in items:
        n = it.get("name", "")
        if n.lower().endswith(".xml") and "primary_doc" not in n.lower():
            name = n
            break
    if not name:
        return [], "no information table"

    def fetch_tbl():
        r = _get("{}/{}".format(base, name))
        return r.content if r is not None and r.status_code == 200 else None

    raw = _cached_bytes("sec/13f/{}_{}_{}".format(cik, acc, name), fetch_tbl)
    if not raw:
        return [], "table fetch failed"

    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(raw.decode("utf-8", errors="replace"))
    except Exception as e:                            # noqa: BLE001
        return [], "table unreadable: {}".format(e)

    def local(tag):
        return tag.rsplit("}", 1)[-1]

    raw_rows = []
    for info in root.iter():
        if local(info.tag) != "infoTable":
            continue
        d = {}
        for child in info.iter():
            d[local(child.tag)] = (child.text or "").strip()
        raw_rows.append(d)

    # UNITS. The information table's `value` column was reported in
    # THOUSANDS of dollars until the 2023 amendments and some filers still
    # file that way — Baupost and Duquesne both do. Read as dollars, their
    # books come out a thousand times too small: Klarman at $4.8M,
    # Druckenmiller at $9.1M, against Lone Pine's $33.7bn.
    #
    # The tell is the implied price. value/shares is a real share price, so
    # a whole filing whose MEDIAN implied price is under a dollar is not a
    # portfolio of sub-dollar stocks, it is a unit mismatch. Median rather
    # than mean so one odd line cannot flip a correct filing.
    implied = []
    for d in raw_rows:
        sh, val = _money(d.get("sshPrnamt")), _money(d.get("value"))
        if sh and val and sh > 100:
            implied.append(val / sh)
    scale = 1.0
    if len(implied) >= 5:
        implied.sort()
        if implied[len(implied) // 2] < 1.0:
            scale = 1000.0

    out = []
    for d in raw_rows:
        out.append({
            "issuer": d.get("nameOfIssuer"),
            "cusip": d.get("cusip"),
            "value": (_money(d.get("value")) or 0) * scale or None,
            "shares": _money(d.get("sshPrnamt")),
            "value_scaled": scale != 1.0,
            "class": d.get("titleOfClass"),
            "report_date": filing.get("report_date"),
            "filing_date": filing.get("filing_date"),
            "manager": filing.get("name"),
            "manager_cik": cik,
            "accession": filing.get("accession"),
        })
    return out, None if out else "no positions"


# Named managers whose 13Fs are tracked. A list, not a claim that any of
# them carries an edge — that question belongs to political_hypotheses.py.
#
# EVERY CIK HERE WAS RESOLVED AGAINST EDGAR'S ACTUAL 13F-HR HISTORY, not
# guessed from the name, because a wrong one fails silently: it returns a
# real fund's real holdings under someone else's label and nothing about
# the output looks broken.
#
# That already happened once. `0001631664` was carried here as "Duquesne
# Family Office" and is in fact **Punch Card Management L.P.** — 47 13F-HR
# filings, a completely different manager. Druckenmiller's family office is
# `0001536411`. Anything attributed to Duquesne before 2026-09-21 was Punch
# Card's book wearing his name.
#
# Several houses file under more than one entity. Where the filings moved,
# both are listed so coverage does not stop at the handover — Ackman's LP
# last filed 2026-05-15 and Pershing Square Inc picked up from 2026-08-14.
WHALES = [
    # value / special situations
    ("Berkshire Hathaway", "0001067983"),
    ("Baupost Group", "0001061768"),
    ("Oaktree Capital Management", "0000949509"),
    ("Scion Asset Management", "0001649339"),
    # concentrated activists
    ("Pershing Square Capital Management", "0001336528"),
    ("Pershing Square Inc", "0002026053"),
    ("Third Point", "0001040273"),
    ("TCI Fund Management", "0001647251"),
    ("Elliott Investment Management", "0001791786"),
    ("ValueAct Holdings", "0001418814"),
    # growth / Tiger lineage
    ("Tiger Global Management", "0001167483"),
    ("Coatue Management", "0001135730"),
    ("Lone Pine Capital", "0001061165"),
    # macro and event-driven
    ("Bridgewater Associates", "0001350694"),
    ("Appaloosa", "0001656456"),
    ("Duquesne Family Office", "0001536411"),
    # other
    ("Bill & Melinda Gates Foundation Trust", "0001166559"),
]


# ─────────────────────────────────────────────
# INFLUENCE — lobbying and federal contracts
# ─────────────────────────────────────────────

def lobbying_filings(year: int, page: int = 1,
                     page_size: int = 100) -> List[dict]:
    """Senate LDA quarterly lobbying filings."""
    url = "{}/filings/?filing_year={}&page={}&page_size={}".format(
        LDA_BASE, year, page, page_size)
    r = _get(url)
    if r is None or r.status_code != 200:
        return []
    try:
        j = r.json()
    except Exception:
        return []
    out = []
    for f in j.get("results") or []:
        client = f.get("client") or {}
        out.append({
            "filing_uuid": f.get("filing_uuid"),
            "year": f.get("filing_year"),
            "period": f.get("filing_period"),
            "posted": f.get("dt_posted"),
            "income": _money(f.get("income")),
            "expenses": _money(f.get("expenses")),
            "client": client.get("name"),
            "client_id": client.get("id"),
            "registrant": (f.get("registrant") or {}).get("name"),
            "issues": [a.get("general_issue_code")
                       for a in (f.get("lobbying_activities") or [])],
        })
    return out


def lobbying_filings_for(client_name: str, year: int,
                         page_size: int = 100) -> List[dict]:
    """Lobbying filings for one client, by name prefix.

    The register holds 56,685 filings for 2026 alone, so paging the whole
    year to find one company is 567 requests for an answer the API will
    give in one. `client_name` matches as a prefix, which is why the caller
    passes a short form of the name.
    """
    url = "{}/filings/?filing_year={}&page_size={}&client_name={}".format(
        LDA_BASE, year, page_size, requests.utils.quote(client_name))
    r = _get(url)
    if r is None or r.status_code != 200:
        return []
    try:
        j = r.json()
    except Exception:
        return []
    out = []
    for f in j.get("results") or []:
        out.append({
            "filing_uuid": f.get("filing_uuid"),
            "year": f.get("filing_year"),
            "period": f.get("filing_period"),
            "income": _money(f.get("income")),
            "expenses": _money(f.get("expenses")),
            "client": (f.get("client") or {}).get("name"),
            "registrant": (f.get("registrant") or {}).get("name"),
            "issues": [a.get("general_issue_code")
                       for a in (f.get("lobbying_activities") or [])],
        })
    return out


def contracts(recipient: str, start: date, end: date,
              limit: int = 50) -> List[dict]:
    """Federal contract awards to a named recipient."""
    payload = {
        "filters": {
            "award_type_codes": ["A", "B", "C", "D"],
            "time_period": [{"start_date": start.isoformat(),
                             "end_date": end.isoformat()}],
            "recipient_search_text": [recipient],
        },
        "fields": ["Award ID", "Recipient Name", "Award Amount",
                   "Awarding Agency", "Start Date"],
        "limit": limit, "sort": "Award Amount", "order": "desc",
    }
    r = _post(USASPENDING + "/search/spending_by_award/", json=payload)
    if r is None or r.status_code != 200:
        return []
    try:
        return r.json().get("results") or []
    except Exception:
        return []


# ─────────────────────────────────────────────
# SELF TEST — the coverage number this module is judged on
# ─────────────────────────────────────────────

def audit_house(year: int, limit: Optional[int] = None) -> dict:
    """Parse every House PTR for a year and report what failed.

    This exists because "the parser works" is not a claim three sample files
    can support. It prints a per-reason failure count, and that number is
    the honest measure of House coverage.
    """
    roster = Roster()
    idx = [f for f in house_index(year)
           if f.get("filing_type") == HOUSE_PTR_TYPE]
    if limit:
        idx = idx[:limit]
    stats = {"filings": len(idx), "ok": 0, "failed": 0, "rows": 0,
             "reasons": {}, "flags": {}, "no_ticker": 0, "paper_filers": {}}
    for f in idx:
        rows, err = house_ptr_rows(f, roster)
        if err:
            stats["failed"] += 1
            stats["reasons"][err] = stats["reasons"].get(err, 0) + 1
            if "paper" in err:
                who = f.get("last", "?")
                stats["paper_filers"][who] = \
                    stats["paper_filers"].get(who, 0) + 1
            continue
        stats["ok"] += 1
        stats["rows"] += len(rows)
        for r in rows:
            if not r.get("ticker"):
                stats["no_ticker"] += 1
            for fl in r.get("parse_flags") or []:
                stats["flags"][fl] = stats["flags"].get(fl, 0) + 1
    return stats


def audit_senate(days_back: int = 30) -> dict:
    roster = Roster()
    sess = senate_session()
    if sess is None:
        return {"error": "could not establish eFD session"}
    start = date.today() - timedelta(days=days_back)
    filings = senate_search(sess, start)
    stats = {"filings": len(filings), "ok": 0, "failed": 0, "rows": 0,
             "reasons": {}, "flags": {}, "no_ticker": 0, "options": 0}
    for f in filings:
        rows, err = senate_ptr_rows(sess, f, roster)
        if err:
            stats["failed"] += 1
            stats["reasons"][err] = stats["reasons"].get(err, 0) + 1
            continue
        stats["ok"] += 1
        stats["rows"] += len(rows)
        for r in rows:
            if not r.get("ticker"):
                stats["no_ticker"] += 1
            if r.get("option_type"):
                stats["options"] += 1
            for fl in r.get("parse_flags") or []:
                stats["flags"][fl] = stats["flags"].get(fl, 0) + 1
    return stats


def _print_stats(title: str, s: dict):
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)
    if "error" in s:
        print("  ERROR:", s["error"])
        return
    cov = (100.0 * s["ok"] / s["filings"]) if s["filings"] else 0.0
    print("  filings      {}".format(s["filings"]))
    print("  parsed       {}  ({:.1f}%)".format(s["ok"], cov))
    print("  failed       {}".format(s["failed"]))
    print("  transactions {}".format(s["rows"]))
    if s.get("rows"):
        print("  no ticker    {}  ({:.1f}% of rows)".format(
            s["no_ticker"], 100.0 * s["no_ticker"] / s["rows"]))
    if s.get("options"):
        print("  with option detail {}".format(s["options"]))
    if s["reasons"]:
        print("  failure reasons:")
        for k, v in sorted(s["reasons"].items(), key=lambda x: -x[1]):
            print("    {:>5}  {}".format(v, k))
    if s["flags"]:
        print("  row flags:")
        for k, v in sorted(s["flags"].items(), key=lambda x: -x[1]):
            print("    {:>5}  {}".format(v, k))
    if s.get("paper_filers"):
        print("  invisible to this tool — these members file on paper, and")
        print("  their filings are scans with no text to read:")
        for k, v in sorted(s["paper_filers"].items(), key=lambda x: -x[1]):
            print("    {:>5}  {}".format(v, k))


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    cmd = args[0] if args else "audit"

    if cmd == "audit":
        year = int(args[1]) if len(args) > 1 else date.today().year
        _print_stats("HOUSE PTR PARSE AUDIT {}".format(year),
                     audit_house(year))
        _print_stats("SENATE PTR PARSE AUDIT (30 days)", audit_senate(30))

    elif cmd == "house":
        year = int(args[1]) if len(args) > 1 else date.today().year
        n = int(args[2]) if len(args) > 2 else 5
        roster = Roster()
        idx = [f for f in house_index(year) if f["filing_type"] == "P"][:n]
        for f in idx:
            rows, err = house_ptr_rows(f, roster)
            print("\n--- {} {} ({}) doc {} : {} ---".format(
                f["first"], f["last"], f["state_dst"], f["doc_id"],
                err or "{} rows".format(len(rows))))
            for r in rows:
                print("  {:<6} {:<10} {:<8} {} -> {}  ${}-${}  [{}] {}".format(
                    r["ticker"] or "--", r["txn_type"] or "?",
                    r["owner"] or "?", r["trade_date"] or "?",
                    r["notify_date"] or "?",
                    r["amount_lo"], r["amount_hi"], r["asset_type"] or "",
                    ",".join(r["parse_flags"]) or ""))

    elif cmd == "senate":
        days = int(args[1]) if len(args) > 1 else 30
        roster = Roster()
        sess = senate_session()
        for f in senate_search(sess, date.today() - timedelta(days=days))[:10]:
            rows, err = senate_ptr_rows(sess, f, roster)
            print("\n--- {} {} : {} ---".format(
                f["first"], f["last"], err or "{} rows".format(len(rows))))
            for r in rows:
                print("  {:<6} {:<10} {:<8} {}  ${}-${}  {}".format(
                    r["ticker"] or "--", r["txn_type"] or "?",
                    r["owner"] or "?", r["trade_date"] or "?",
                    r["amount_lo"], r["amount_hi"],
                    "{} {} {}".format(r["option_type"] or "", r["strike"] or "",
                                      r["expiry"] or "").strip()))

    elif cmd == "form4":
        d = date.today() - timedelta(days=int(args[1]) if len(args) > 1 else 1)
        entries = form4_index(d)
        print("{} Form 4 filings on {}".format(len(entries), d))
        for e in entries[:5]:
            rows, err = form4_rows(e)
            print("  {:<40} {}".format(
                (e["company"] or "")[:40],
                err or "{} open-market rows".format(len(rows))))
            for r in rows:
                print("      {} {} {} sh @ {}".format(
                    r["txn_type"], r["ticker"], r["shares"], r["price"]))

    else:
        print(__doc__)
