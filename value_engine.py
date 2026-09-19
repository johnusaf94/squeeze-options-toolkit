"""
value_engine.py
===============
Earnings-based valuation series — the data and the math behind the
Value Analysis tab. No GUI in this file, so the numbers can be checked
from a REPL without opening a window.

WHAT THIS IS
------------
An implementation of the standard earnings-overlay valuation technique:
plot split-adjusted price against a line drawn at a fixed multiple of the
company's own reported earnings, so "expensive" and "cheap" get read
against the earnings stream rather than against a chart pattern.

Two reference lines are drawn:

  * BENCHMARK line — earnings x 15. Fifteen times earnings is roughly the
    long-run average multiple of the US market, and the multiple Graham
    and Dodd used as a dividing line for a defensive purchase. For
    companies compounding faster than 15%/yr the multiple is raised to
    the growth rate and capped at 30x, which is the usual PEG convention.

  * NORMAL line — earnings x the multiple THIS stock actually traded at
    over the selected window (median of the monthly P/E). A stock that
    has spent fifteen years at 25x is not "40% overvalued" at 22x merely
    because the market average is 15x.

Neither line is a forecast. Both are descriptions — one of a market
convention, one of this stock's own history. What they add is a fixed,
checkable reference, so a move in price can be separated from a move in
earnings.

DATA SOURCES AND THEIR SEAMS
----------------------------
1. ANNUAL FUNDAMENTALS come from SEC XBRL companyfacts (data.sec.gov):
   free, authoritative, and reaching back to roughly 2009 when XBRL was
   phased in. This is the whole reason the tool does not just use
   yfinance — yfinance returns FOUR years of annual income statement,
   which is not enough to compute anything resembling a normal multiple.

2. SPLIT BASIS. XBRL stores EPS AS FILED. A company that split after
   filing leaves a pre-split EPS sitting in the record, so the raw series
   has a step discontinuity at every split. Apple's FY2011 EPS is filed
   as 27.68 and its FY2012 EPS as 6.31 — same company, two different
   share bases, a 78% "decline" that never happened. Every per-share
   value is therefore divided by the product of the splits that occurred
   AFTER the filing date that reported it. Share counts are multiplied by
   the same factor.

   Price comes from Yahoo's `Close` with auto_adjust=False, which is
   split-adjusted but NOT dividend-adjusted. That is the correct series
   to pair with EPS: the dividend-adjusted series would push historical
   prices down and quietly understate every historical P/E.

3. THE GAAP / ADJUSTED SEAM. Filed EPS is GAAP. Analyst consensus is
   almost always ADJUSTED. Splicing a consensus number onto the end of a
   GAAP history draws a step that is an accounting-definition change, not
   a change in the business. So the forecast is built by default from
   consensus GROWTH RATES applied to the last GAAP actual, and the size
   of the level gap is measured and reported rather than papered over.
   Absolute-consensus mode is available and labelled as such.

USAGE
-----
    import value_engine as ve
    a = ve.analyze("MSFT", metric="eps", window_years=15)
    print(a.summary_text())
"""

import json
import math
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────

_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_DIR, "cache", "sec")

# SEC asks for a descriptive User-Agent with a contact. Set the env var
# SEC_USER_AGENT to your own address; the default follows the convention
# already used by verify_short_data.py and ships no real mailbox.
SEC_UA = os.environ.get("SEC_USER_AGENT", "squeeze-toolkit value-analysis")

FACTS_TTL = timedelta(hours=24)         # companyfacts is ~4 MB; cache hard
TICKERMAP_TTL = timedelta(days=7)

# The classic defensive multiple. Not a magic number — the long-run
# average market P/E, and the level Graham used for a no-growth buy.
BENCHMARK_PE = 15.0
BENCHMARK_PE_CAP = 30.0     # PEG multiple ceiling for fast compounders

# Filings whose numbers are the primary record. Restatements arriving in
# an 8-K are used only when nothing better covers the period.
PRIMARY_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}

# Annual periods in XBRL are not exactly 365 days (52/53-week fiscal
# calendars, transition periods). This window accepts a real fiscal year
# and rejects the 9-month and 2-year aggregates that also appear.
ANNUAL_MIN_DAYS = 330
ANNUAL_MAX_DAYS = 400

# Some filers — Berkshire is the well-known one — never tag a full-year
# duration for EPS at all, only the four quarters. Those get summed.
QUARTER_MIN_DAYS = 80
QUARTER_MAX_DAYS = 100

# A price-to-metric multiple outside these ranges is not a valuation, it is
# a units mismatch: per-share figures filed on a different share class
# than the ticker being priced (BRK.A vs BRK.B), or an ADR ratio. The range
# depends on the metric. A car maker at 0.3x sales is ordinary, and a floor
# built for P/E would silently throw away its entire history.
SANE_MULTIPLES = {
    "eps":       (0.5, 500.0),
    "ocf":       (0.3, 500.0),
    "revenue":   (0.02, 200.0),
    "dividends": (2.0, 5000.0),
}
SANE_MULTIPLE = SANE_MULTIPLES["eps"]


def sane_range(metric: str) -> Tuple[float, float]:
    return SANE_MULTIPLES.get(metric, SANE_MULTIPLE)

# What a unit key has to look like for each kind of figure. Matching by
# KIND rather than against a literal "USD" is what lets a foreign filer
# work at all: Sony's EPS is tagged in JPY/shares, and a hardcoded USD unit
# finds nothing in a filing that is otherwise complete.
PER_SHARE, MONEY, SHARE_COUNT = "per_share", "money", "shares"

# XBRL tag preference per metric, most specific first. Both taxonomies are
# listed: US filers report under us-gaap, foreign issuers filing a 20-F
# under ifrs-full, and a company that switched — Sony did, in 2022 — has
# its history split across the two. The lists are MERGED rather than raced;
# see _annual_facts.
TAGS = {
    "eps": [
        ("us-gaap", "EarningsPerShareDiluted", PER_SHARE),
        ("ifrs-full", "DilutedEarningsLossPerShare", PER_SHARE),
        ("us-gaap", "EarningsPerShareBasicAndDiluted", PER_SHARE),
        ("us-gaap", "IncomeLossFromContinuingOperationsPerDilutedShare", PER_SHARE),
        ("us-gaap", "EarningsPerShareBasic", PER_SHARE),
        ("ifrs-full", "BasicEarningsLossPerShare", PER_SHARE),
    ],
    "dividends": [
        ("us-gaap", "CommonStockDividendsPerShareDeclared", PER_SHARE),
        ("us-gaap", "CommonStockDividendsPerShareCashPaid", PER_SHARE),
        ("ifrs-full", "DividendsPaidOrdinarySharesPerShare", PER_SHARE),
        ("ifrs-full", "DividendsRecognisedAsDistributionsToOwnersPerShare", PER_SHARE),
    ],
    "ocf": [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities", MONEY),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", MONEY),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities", MONEY),
    ],
    "revenue": [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", MONEY),
        ("us-gaap", "Revenues", MONEY),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax", MONEY),
        ("us-gaap", "SalesRevenueNet", MONEY),
        ("us-gaap", "SalesRevenueGoodsNet", MONEY),
        ("ifrs-full", "Revenue", MONEY),
        ("ifrs-full", "RevenueFromContractsWithCustomers", MONEY),
    ],
    "shares": [
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", SHARE_COUNT),
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstandingBasicAndDiluted", SHARE_COUNT),
        ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic", SHARE_COUNT),
        ("ifrs-full", "WeightedAverageNumberOfDilutedOrdinarySharesOutstanding", SHARE_COUNT),
        ("ifrs-full", "WeightedAverageNumberOfOrdinarySharesOutstandingBasic", SHARE_COUNT),
    ],
    # Capital spending, reported as a positive outflow. Free cash flow is
    # operating cash flow after it — the money actually left over to pay a
    # dividend with.
    "capex": [
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment", MONEY),
        ("us-gaap", "PaymentsToAcquireProductiveAssets", MONEY),
        ("us-gaap", "PaymentsForCapitalImprovements", MONEY),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", MONEY),
    ],
    # Last resort for EPS. Companies that tag earnings per share only with
    # a share-class dimension (Berkshire) publish nothing under the plain
    # EPS concepts, but their net income and share count are both there.
    "net_income": [
        ("us-gaap", "NetIncomeLoss", MONEY),
        ("us-gaap", "ProfitLoss", MONEY),
        ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic", MONEY),
        ("ifrs-full", "ProfitLossAttributableToOwnersOfParent", MONEY),
        ("ifrs-full", "ProfitLoss", MONEY),
    ],
}

METRIC_LABELS = {
    "eps":       "Diluted EPS",
    "ocf":       "Operating cash flow / share",
    "dividends": "Dividends / share",
    "revenue":   "Revenue / share",
}

# The same metrics as they read mid-sentence, with EPS kept in capitals.
METRIC_PHRASE = {
    "eps":       "diluted EPS",
    "ocf":       "operating cash flow per share",
    "dividends": "dividends per share",
    "revenue":   "revenue per share",
}

# Metrics already per-share in the filing; the rest are absolute dollars
# and get divided by the diluted share count.
PER_SHARE_NATIVE = {"eps", "dividends"}

# Metrics that read better upside down. Nobody quotes a stock at "22x its
# dividend" — they quote a 4.6% yield, and those are the same number. The
# chart still works in multiples throughout (every line is still dividend
# x N); only the labels change, to what a reader would actually say.
YIELD_METRICS = {"dividends"}


def is_yield_metric(metric: str) -> bool:
    return metric in YIELD_METRICS


def as_yield(multiple: Optional[float]) -> Optional[float]:
    """The percentage yield a multiple is the reciprocal of."""
    if not multiple or multiple <= 0:
        return None
    return 100.0 / multiple


def mult_text(metric: str, multiple: Optional[float]) -> str:
    """One multiple, written the way this metric is normally quoted."""
    if multiple is None or multiple <= 0:
        return "—"
    if is_yield_metric(metric):
        y = 100.0 / multiple
        return f"{y:.2f}%" if y < 1.0 else f"{y:.1f}%"
    return f"{multiple:.1f}×"


# ─────────────────────────────────────────────
# HTTP + DISK CACHE
# ─────────────────────────────────────────────

_ssl_ctx = None


def _ctx():
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import certifi
            _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ssl_ctx = False
    return _ssl_ctx or None


def _http_json(url: str, timeout: int = 40):
    req = urllib.request.Request(
        url, headers={"User-Agent": SEC_UA, "Accept": "application/json",
                      "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", "replace"))


def _cache_path(name: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return os.path.join(CACHE_DIR, safe + ".json")


def _cached_json(name: str, url: str, ttl: timedelta):
    """Read-through disk cache. SEC data changes at filing frequency, so a
    24-hour TTL on a 4 MB payload is the difference between a tool that
    opens instantly and one that re-downloads the same file all day."""
    path = _cache_path(name)
    try:
        age = time.time() - os.path.getmtime(path)
        if age < ttl.total_seconds():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass

    data = _http_json(url)
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass
    return data


def cik_for(ticker: str) -> Optional[int]:
    """Map a ticker to its SEC CIK. The index is ~1 MB and changes rarely."""
    try:
        idx = _cached_json("company_tickers",
                           "https://www.sec.gov/files/company_tickers.json",
                           TICKERMAP_TTL)
    except Exception:
        return None
    t = ticker.upper().strip()
    # Class-share tickers appear as BRK-B on Yahoo and BRK.B at the SEC
    wanted = {t, t.replace("-", "."), t.replace(".", "-")}
    for v in idx.values():
        if str(v.get("ticker", "")).upper() in wanted:
            return int(v["cik_str"])
    return None


def company_facts(cik: int) -> dict:
    return _cached_json(f"facts_{cik:010d}",
                        f"https://data.sec.gov/api/xbrl/companyfacts/"
                        f"CIK{cik:010d}.json", FACTS_TTL)


# ─────────────────────────────────────────────
# SPLIT NORMALISATION
# ─────────────────────────────────────────────

def get_splits(ticker: str) -> List[Tuple[date, float]]:
    """[(effective_date, ratio)] — 7.0 means a 7-for-1."""
    try:
        import yfinance_throttle  # noqa: F401  installs the global limiter
        import yfinance as yf
        s = yf.Ticker(ticker).splits
    except Exception:
        return []
    out = []
    try:
        for ts, ratio in s.items():
            r = float(ratio)
            if r > 0 and abs(r - 1.0) > 1e-9:
                out.append((ts.date(), r))
    except Exception:
        return []
    out.sort()
    return out


def _split_factor_after(filed: date, splits: List[Tuple[date, float]]) -> float:
    """Product of every split effective AFTER `filed`. A per-share figure
    reported before those splits must be divided by this to be comparable
    with today's share base; a share COUNT must be multiplied by it."""
    f = 1.0
    for d, ratio in splits:
        if d > filed:
            f *= ratio
    return f


# ─────────────────────────────────────────────
# ANNUAL SERIES EXTRACTION
# ─────────────────────────────────────────────

def _iso(s) -> Optional[date]:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _unit_currency(unit: str, kind: str) -> Optional[str]:
    """The currency of an XBRL unit key, when it is the kind being asked
    for, and None when it is not.

    "USD/shares" and "JPY/shares" are both per-share figures; "USD" and
    "EUR" are both money; "shares" is a count and carries no currency."""
    if kind == SHARE_COUNT:
        return "" if unit == "shares" else None
    if kind == PER_SHARE:
        head, _sep, tail = unit.partition("/")
        if tail == "shares" and len(head) == 3 and head.isalpha():
            return head.upper()
        return None
    if kind == MONEY:
        return unit.upper() if len(unit) == 3 and unit.isalpha() else None
    return None


def _best_by_period(rows, min_days: int, max_days: int,
                    cur: str = "") -> Dict[date, dict]:
    """Collapse every observation of a duration to one value per period.

    For each period the best available observation wins: a primary filing
    (10-K family) beats an 8-K restatement, and within a tier the most
    recently filed value wins — that is the one carrying the company's
    latest restatement and share basis."""
    best: Dict[date, dict] = {}
    for x in rows:
        start, end = _iso(x.get("start")), _iso(x.get("end"))
        filed = _iso(x.get("filed"))
        if not start or not end or not filed:
            continue
        days = (end - start).days
        if days < min_days or days > max_days:
            continue
        val = _num(x.get("val"))
        if val is None:
            continue
        form = x.get("form") or ""
        cand = {"val": val, "filed": filed, "form": form, "cur": cur,
                "fp": x.get("fp") or "",
                "tier": 0 if form in PRIMARY_FORMS else 1}
        prev = best.get(end)
        if (prev is None
                or cand["tier"] < prev["tier"]
                or (cand["tier"] == prev["tier"]
                    and cand["filed"] > prev["filed"])):
            best[end] = cand
    return best


def _merge_periods(dst: Dict[date, dict], src: Dict[date, dict],
                   fill_only: bool = False) -> Dict[date, dict]:
    """Fold one period map into another. `fill_only` keeps what is already
    there and adds only the periods missing from it — how a lower-priority
    tag extends a series without overwriting the preferred one."""
    for end, cand in src.items():
        prev = dst.get(end)
        if prev is None:
            dst[end] = cand
        elif not fill_only and (cand["tier"] < prev["tier"]
                                or (cand["tier"] == prev["tier"]
                                    and cand["filed"] > prev["filed"])):
            dst[end] = cand
    return dst


def _collect_periods(node: dict, kind: str, min_days: int,
                     max_days: int) -> Dict[date, dict]:
    """Every period one concept reports, in whatever currency it reports
    it in."""
    out: Dict[date, dict] = {}
    for unit, rows in (node.get("units") or {}).items():
        cur = _unit_currency(unit, kind)
        if cur is None:
            continue
        _merge_periods(out, _best_by_period(rows, min_days, max_days, cur))
    return out


def _fiscal_year_ends(facts: dict) -> List[date]:
    """The company's fiscal year-end dates, learned from whichever concepts
    DO carry a full-year duration.

    `fp == "FY"` cannot be used for this: every quarter reported inside a
    10-K carries that marker, so it identifies the filing, not the period."""
    tree = facts.get("facts", {})
    ends = set()
    for tag_list in (TAGS["revenue"], TAGS["net_income"], TAGS["ocf"]):
        for taxonomy, tag, kind in tag_list:
            node = tree.get(taxonomy, {}).get(tag)
            if node:
                ends.update(_collect_periods(node, kind, ANNUAL_MIN_DAYS,
                                             ANNUAL_MAX_DAYS))
    return sorted(ends)


def _annual_from_quarters(node: dict, kind: str, splits, per_share: bool,
                          fy_ends: List[date]) -> Dict[date, dict]:
    """Rebuild fiscal years from four quarters, for filers that never tag
    a full-year duration on this concept.

    Quarters inside one fiscal year can straddle a split, so each quarter
    is split-adjusted against its OWN filing date before the sum. The
    resulting record is then stamped with today's date, which makes the
    downstream adjustment a no-op rather than a second, wrong pass."""
    q = _collect_periods(node, kind, QUARTER_MIN_DAYS, QUARTER_MAX_DAYS)
    if not q or not fy_ends:
        return {}
    ends = sorted(q)
    out: Dict[date, dict] = {}
    for fy in fy_ends:
        window = [e for e in ends
                  if fy - timedelta(days=370) < e <= fy]
        if len(window) != 4:
            continue
        if len({q[e]["cur"] for e in window}) != 1:
            continue          # never add up two currencies
        total = 0.0
        for e in window:
            v = q[e]["val"]
            if per_share and splits:
                v /= _split_factor_after(q[e]["filed"], splits)
            total += v
        out[fy] = {"val": total, "filed": date.today(), "cur": q[window[0]]["cur"],
                   "form": "quarterly sum", "fp": "FY", "tier": 0}
    return out


def _annual_facts(facts: dict, tag_list, splits=None,
                  per_share: bool = False) -> Tuple[Dict[date, dict], Optional[str]]:
    """Pull one annual series out of companyfacts.

    Tags are MERGED in preference order rather than raced: the first tag
    that reports a period owns it, and later tags fill only the years it
    does not cover. A company that changed taxonomy or changed tag keeps
    one continuous history instead of being cut off at the switch — Sony's
    EPS runs to 2021 under us-gaap and continues under ifrs-full, and
    picking either one alone loses half the chart."""
    tree = facts.get("facts", {})
    nodes = [(tag, kind, tree.get(taxonomy, {}).get(tag))
             for taxonomy, tag, kind in tag_list]
    nodes = [(t, k, n) for t, k, n in nodes if n]

    series: Dict[date, dict] = {}
    used: List[str] = []
    for tag, kind, node in nodes:
        got = _collect_periods(node, kind, ANNUAL_MIN_DAYS, ANNUAL_MAX_DAYS)
        if any(end not in series for end in got):
            _merge_periods(series, got, fill_only=True)
            used.append(tag)
    if len(series) >= 2:
        return series, " + ".join(used)

    fy_ends = _fiscal_year_ends(facts)
    for tag, kind, node in nodes:
        best = _annual_from_quarters(node, kind, splits, per_share, fy_ends)
        if len(best) >= 2:
            return best, f"{tag} (summed from quarters)"
    return {}, None


def _adjust_per_share(series: Dict[date, dict],
                      splits: List[Tuple[date, float]],
                      invert: bool = False, fx=None) -> Dict[date, float]:
    """Restate every observation onto today's share base, and into the
    currency the stock is priced in. `invert=True` for share COUNTS, which
    move the opposite way from per-share values and have no currency."""
    out = {}
    for end, rec in series.items():
        f = _split_factor_after(rec["filed"], splits)
        if invert:
            out[end] = rec["val"] * f
        else:
            v = rec["val"] / f
            out[end] = v * fx(rec.get("cur", ""), end) if fx else v
    return out


# ─────────────────────────────────────────────
# PRICE
# ─────────────────────────────────────────────

def monthly_prices(ticker: str) -> Tuple[List[date], List[float]]:
    """Split-adjusted, NOT dividend-adjusted, monthly closes. Yahoo's
    `Close` with auto_adjust=False is exactly that; `Adj Close` would
    also strip dividends and depress every historical P/E."""
    import yfinance_throttle  # noqa: F401
    import yfinance as yf
    h = yf.Ticker(ticker).history(period="max", interval="1mo",
                                  auto_adjust=False)
    if h is None or len(h) == 0:
        return [], []
    dates, closes = [], []
    for ts, row in h.iterrows():
        c = _num(row.get("Close"))
        if c is None:
            continue
        dates.append(ts.date())
        closes.append(c)
    return dates, closes


# The 10-year Treasury, quoted by this feed directly in percent: 4.998
# means 4.998%. History runs back to 1985.
TREASURY_TICKER = "^TNX"
_TREASURY: List[Tuple[date, float]] = []
_TREASURY_AT = 0.0


def treasury_series() -> List[Tuple[date, float]]:
    """Monthly 10-year Treasury yield in percent, oldest first.

    Cached for the process: it is the same series for every ticker, and it
    moves in basis points rather than by the minute."""
    global _TREASURY, _TREASURY_AT
    if _TREASURY and time.time() - _TREASURY_AT < 3600:
        return _TREASURY
    out: List[Tuple[date, float]] = []
    try:
        import yfinance_throttle  # noqa: F401
        import yfinance as yf
        h = yf.Ticker(TREASURY_TICKER).history(period="max", interval="1mo",
                                               auto_adjust=False)
        for ts, row in h.iterrows():
            v = _num(row.get("Close"))
            if v and 0 < v < 25:            # a yield, not an index level
                out.append((ts.date(), v))
    except Exception:
        return _TREASURY
    if out:
        _TREASURY, _TREASURY_AT = out, time.time()
    return _TREASURY


def dividend_rates(ticker: str) -> Tuple[Optional[float], Optional[float]]:
    """(trailing twelve months, forward run rate) dividend per share, from
    the quote feed.

    The two differ exactly when a dividend has just been cut or raised, and
    on a dividend chart that difference is the whole story: an annual filing
    can be a year old, and a trailing yield computed from payments that are
    no longer being made is not a yield anyone will receive."""
    try:
        import yfinance_throttle  # noqa: F401
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return (_num(info.get("trailingAnnualDividendRate")),
                _num(info.get("dividendRate")))
    except Exception:
        return None, None


def quote_currency(ticker: str) -> str:
    """The currency the stock is quoted in, which is not always the one its
    accounts are kept in: Sony files in yen and its ADR trades in dollars."""
    try:
        import yfinance_throttle  # noqa: F401
        import yfinance as yf
        return (yf.Ticker(ticker).info.get("currency") or "USD").upper()
    except Exception:
        return "USD"


def fx_series(frm: str, to: str) -> List[Tuple[date, float]]:
    """Monthly exchange rates, oldest first. Tries the inverse pair when
    the direct one is not quoted."""
    try:
        import yfinance_throttle  # noqa: F401
        import yfinance as yf
    except Exception:
        return []
    for pair, invert in ((f"{frm}{to}=X", False), (f"{to}{frm}=X", True)):
        try:
            h = yf.Ticker(pair).history(period="max", interval="1mo",
                                        auto_adjust=False)
        except Exception:
            continue
        if h is None or len(h) == 0:
            continue
        out = []
        for ts, row in h.iterrows():
            c = _num(row.get("Close"))
            if c and c > 0:
                out.append((ts.date(), (1.0 / c) if invert else c))
        if out:
            return out
    return []


def _fx_converter(price_cur: str, notes: List[str], warnings: List[str]):
    """Convert filed figures into the currency the stock is priced in.

    A Japanese filer reports EPS in yen while its ADR trades in dollars,
    and dividing one by the other is a number with no meaning. Each fiscal
    year is converted at the rate on its OWN year end, because that is the
    rate that stood while the market was pricing those earnings; using
    today's rate would rewrite every historical multiple."""
    cache: Dict[str, List[Tuple[date, float]]] = {}
    seen: List[str] = []

    def convert(cur: str, when: date) -> float:
        if not cur:
            return 1.0
        if cur not in seen:
            seen.append(cur)
        if cur == price_cur:
            return 1.0
        series = cache.get(cur)
        if series is None:
            series = fx_series(cur, price_cur)
            cache[cur] = series
            if series:
                notes.append(
                    f"Filed in {cur}, priced in {price_cur}. Every year is "
                    f"converted at the rate on its own fiscal year end.")
            else:
                warnings.append(
                    f"The filings are in {cur} and the price is in "
                    f"{price_cur}, and no {cur}/{price_cur} rate could be "
                    f"fetched. The two cannot be compared, so every multiple "
                    f"here is meaningless.")
        if not series:
            return 1.0
        rate = series[0][1]
        for d, r in series:
            if d > when:
                break
            rate = r
        return rate

    convert.seen = seen
    return convert


def yahoo_name(ticker: str) -> Optional[str]:
    """The name Yahoo has for this ticker, for cross-checking against the
    name the SEC has for the CIK. The fundamentals come from one source and
    the price from the other, so seeing both names side by side is how a
    wrong-company mixup gets caught — a reorganisation or a recycled ticker
    can point the two at different businesses."""
    try:
        import yfinance_throttle  # noqa: F401
        import yfinance as yf
        info = yf.Ticker(ticker).info      # cached by the throttle
        for key in ("longName", "shortName", "displayName"):
            v = info.get(key)
            if v:
                return str(v)
    except Exception:
        pass
    return None


def last_price(ticker: str) -> Optional[float]:
    import yfinance_throttle  # noqa: F401
    import yfinance as yf
    t = yf.Ticker(ticker)
    try:
        info = t.info
        for attr in ("currentPrice", "regularMarketPrice", "previousClose"):
            v = _num(info.get(attr))
            if v:
                return v
    except Exception:
        pass
    try:
        h = t.history(period="5d", auto_adjust=False)
        if h is not None and len(h):
            return _num(h["Close"].iloc[-1])
    except Exception:
        pass
    return None


# ─────────────────────────────────────────────
# FORWARD ESTIMATES
# ─────────────────────────────────────────────

@dataclass
class Estimates:
    """Consensus, as published. `growth` is the field the forecast line is
    actually built from — see the GAAP/adjusted note in the module docstring."""
    cy_eps: Optional[float] = None       # current fiscal year, absolute
    ny_eps: Optional[float] = None       # next fiscal year, absolute
    cy_growth: Optional[float] = None    # fraction, e.g. 0.14
    ny_growth: Optional[float] = None
    # The spread of opinion, whatever the metric: low / average / high of
    # the published estimates. This is the only dispersion in the data that
    # is not invented, and it is what the scenarios are built from.
    cy_low: Optional[float] = None
    cy_avg: Optional[float] = None
    cy_high: Optional[float] = None
    ny_low: Optional[float] = None
    ny_avg: Optional[float] = None
    ny_high: Optional[float] = None
    ltg: Optional[float] = None          # long-term growth, fraction
    cy_year_ago_eps: Optional[float] = None   # the base consensus grew FROM
    analysts_cy: Optional[int] = None
    analysts_ny: Optional[int] = None
    error: Optional[str] = None

    def dispersion(self) -> Optional[Tuple[float, float]]:
        """(low, high) as ratios to the average estimate.

        Prefers the further year, where the disagreement is wider and more
        honest — everyone converges on the quarter they can nearly see."""
        for lo, avg, hi in ((self.ny_low, self.ny_avg, self.ny_high),
                            (self.cy_low, self.cy_avg, self.cy_high)):
            if lo and avg and hi and avg > 0 and lo > 0 and hi >= avg >= lo:
                return lo / avg, hi / avg
        return None


def fetch_estimates(ticker: str, metric: str = "eps") -> Estimates:
    """Consensus growth for the metric being charted, and only that metric.

    Earnings consensus applied to revenue per share is simply wrong. For a
    company growing into profitability EPS growth can run at twice the
    sales growth — Unity's consensus was 41-44%/yr on earnings against
    20%/yr on revenue — and a revenue line drawn at the earnings rate
    overstates the path by that much. Revenue has its own consensus; cash
    flow and dividends have none published, and are given none."""
    e = Estimates()
    if metric not in ("eps", "revenue"):
        return e
    try:
        import yfinance_throttle  # noqa: F401
        import yfinance as yf
        t = yf.Ticker(ticker)
        est = t.earnings_estimate if metric == "eps" else t.revenue_estimate
        if est is not None and len(est):
            if "0y" in est.index:
                r = est.loc["0y"]
                e.cy_growth = _num(r.get("growth"))
                e.analysts_cy = _int(r.get("numberOfAnalysts"))
                e.cy_low = _num(r.get("low"))
                e.cy_avg = _num(r.get("avg"))
                e.cy_high = _num(r.get("high"))
                if metric == "eps":
                    e.cy_eps = e.cy_avg
                    e.cy_year_ago_eps = _num(r.get("yearAgoEps"))
            if "+1y" in est.index:
                r = est.loc["+1y"]
                e.ny_growth = _num(r.get("growth"))
                e.analysts_ny = _int(r.get("numberOfAnalysts"))
                e.ny_low = _num(r.get("low"))
                e.ny_avg = _num(r.get("avg"))
                e.ny_high = _num(r.get("high"))
                if metric == "eps":
                    e.ny_eps = e.ny_avg
        if metric == "eps":
            # Yahoo's long-term growth figure is an earnings rate. It says
            # nothing about sales, so revenue never borrows it.
            try:
                g = t.growth_estimates
                if g is not None and "LTG" in g.index:
                    e.ltg = _num(g.loc["LTG"].get("stockTrend"))
            except Exception:
                pass
    except Exception as exc:
        e.error = f"{type(exc).__name__}: {exc}"
    return e


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    except (TypeError, ValueError):
        return None


def _int(v) -> Optional[int]:
    n = _num(v)
    return None if n is None else int(n)


# ─────────────────────────────────────────────
# MATH
# ─────────────────────────────────────────────

def cagr(first: Optional[float], last: Optional[float],
         years: float) -> Optional[float]:
    """Compound rate. Undefined when either end is not positive — a
    company that went from a loss to a profit has no growth RATE, and
    inventing one is how a turnaround gets sold as a compounder."""
    if years <= 0 or first is None or last is None:
        return None
    if first <= 0 or last <= 0:
        return None
    return (last / first) ** (1.0 / years) - 1.0


def median(xs) -> Optional[float]:
    v = sorted(x for x in xs if x is not None)
    if not v:
        return None
    n = len(v)
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def percentile(xs, q: float) -> Optional[float]:
    """Linear-interpolated percentile. q is a fraction: 0.25 is the value a
    quarter of the sample sits below."""
    v = sorted(x for x in xs if x is not None)
    if not v:
        return None
    if len(v) == 1:
        return v[0]
    pos = q * (len(v) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def mean(xs) -> Optional[float]:
    v = [x for x in xs if x is not None]
    return sum(v) / len(v) if v else None


def worst_jump(dates: List[date], values: List[float],
               limit: float = 10.0) -> Optional[Tuple[date, date, float]]:
    """The largest year-over-year ratio in a series, when it is bigger than
    any business does.

    Revenue per share does not move by a factor of ten in a year, let alone
    a million. A break that size is a filing that changed units, a tag that
    means something different on each side of it, or a merger — and every
    multiple spanning it is unusable. Catching it here is the backstop for
    the scale fixes upstream: those handle the breaks that are understood,
    this one refuses to stay quiet about the rest."""
    worst = None
    for (d0, v0), (d1, v1) in zip(zip(dates, values),
                                  zip(dates[1:], values[1:])):
        if v0 <= 0 or v1 <= 0:
            continue
        ratio = max(v1 / v0, v0 / v1)
        if ratio >= limit and (worst is None or ratio > worst[2]):
            worst = (d0, d1, ratio)
    return worst


def log_fit_r2(years: List[float], values: List[float]) -> Optional[float]:
    """R-squared of ln(value) against time — how closely the metric has
    tracked a constant compounding rate. A high R-squared means the growth
    rate describes what happened; a low one means the CAGR is an average
    over a series that did something else entirely."""
    pts = [(x, math.log(y)) for x, y in zip(years, values) if y and y > 0]
    if len(pts) < 3:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    syy = sum((p[1] - my) ** 2 for p in pts)
    if sxx <= 0 or syy <= 0:
        return None
    return (sxy * sxy) / (sxx * syy)


def interpolate(fiscal_dates: List[date], values: List[float],
                at: List[date]) -> List[Optional[float]]:
    """Straight-line interpolation of an annual series onto a monthly
    grid. A company's earning power does not step once a year on the
    filing date, and a step function would produce a sawtooth P/E that
    says more about the calendar than about the business."""
    out: List[Optional[float]] = []
    n = len(fiscal_dates)
    for d in at:
        if n == 0 or d < fiscal_dates[0] or d > fiscal_dates[-1]:
            out.append(None)
            continue
        i = 0
        while i < n - 1 and fiscal_dates[i + 1] < d:
            i += 1
        if i >= n - 1:
            out.append(values[-1])
            continue
        d0, d1 = fiscal_dates[i], fiscal_dates[i + 1]
        v0, v1 = values[i], values[i + 1]
        span = (d1 - d0).days
        if span <= 0:
            out.append(v1)
            continue
        w = (d - d0).days / span
        out.append(v0 + (v1 - v0) * w)
    return out


# ─────────────────────────────────────────────
# THE ANALYSIS
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# VALUE METER
# ─────────────────────────────────────────────

# What each part of the meter is worth. These are judgement, not fitted
# weights — nothing in the meter has been tested against what prices did
# next — and they are shown beside every score so the judgement stays in
# view rather than hiding inside one number.
METER_WEIGHTS = (
    ("history",   0.40),  # where today's multiple ranks in its own past
    ("normal",    0.30),  # distance from the multiple it usually trades at
    ("benchmark", 0.15),  # distance from the outside yardstick
    ("outlook",   0.15),  # return consensus growth delivers at today's multiple
)

# Score bands, highest floor first.
METER_BANDS = ((75, "Cheap"), (60, "Leaning cheap"), (40, "Fair"),
               (25, "Leaning expensive"), (0, "Expensive"))

# One doubling cheaper than a reference scores 85; one doubling dearer, 15.
_GAP_STRETCH = math.atanh(0.7)

# The consensus-path return that scores a neutral 50 — roughly the long-run
# return on equities, so "pays what stocks pay" reads as fair — and the
# distance either side of it over which the score saturates.
OUTLOOK_NEUTRAL = 0.08
OUTLOOK_SCALE = 0.12

# Fewest monthly multiples the history rank is allowed to rest on.
METER_MIN_MONTHS = 24


def _gap_score(reference: float, current: float) -> float:
    """50 at the reference, rising as the current multiple falls below it."""
    doublings = math.log(reference / current) / math.log(2.0)
    return 50.0 + 50.0 * math.tanh(_GAP_STRETCH * doublings)


def _meter_label(score: float) -> str:
    return next(name for floor, name in METER_BANDS if score >= floor)


def _mult_txt(k: float) -> str:
    """17.5146 -> '17.5', 15.0 -> '15'."""
    return f"{k:.1f}".rstrip("0").rstrip(".")


def _recent_regime(a) -> Optional[Tuple[float, float]]:
    """Median multiple over the last three years, and its ratio to the
    window's normal.

    A stock that has spent three years far from its normal has re-rated.
    "Cheaper than its own history" is then exactly what a broken growth
    story looks like as well as a bargain, and the meter cannot tell which
    — so it says so instead of scoring the old normal as if it will return."""
    if not a.normal_pe or not a.price_dates:
        return None
    lo, hi = sane_range(a.metric)
    cutoff = date.today() - timedelta(days=3 * 365)
    recent = [x for d, x in zip(a.price_dates, a.monthly_pe)
              if x and lo <= x <= hi and d >= cutoff]
    if len(recent) < 12:
        return None
    med = median(recent)
    return med, med / a.normal_pe


def _meter_confidence(a) -> Tuple[float, List[str]]:
    """How far the chart can be trusted to mean what it shows, 0 to 100,
    with a short reason for every point taken off.

    The meter is only as good as the earnings stream under it. A multiple of
    erratic, loss-strewn or thinly-filed earnings produces a confident-looking
    number that describes nothing."""
    conf, why = 100.0, []

    def dock(points, reason):
        nonlocal conf
        conf -= points
        why.append(reason)

    phrase = METRIC_PHRASE.get(a.metric, a.metric)
    r2 = a.growth_r2
    if r2 is None:
        dock(20, f"{phrase} has no steady trend to measure")
    elif r2 < 0.5:
        dock(30, f"{phrase} is erratic (trend fit {r2:.2f})")
    elif r2 < 0.75:
        dock(15, f"{phrase} trends only loosely (fit {r2:.2f})")
    losses = sum(1 for v in a.fiscal_values if v <= 0)
    if losses:
        dock(min(30, 10 * losses), f"{losses} loss year(s) in the window")
    if a.series_jump:
        dock(45, f"the series breaks by {a.series_jump:,.0f}× between two "
                 f"years — a filing artefact, not a business")
    if is_yield_metric(a.metric) and a.coverage:
        last = next((r for r in reversed(a.coverage)
                     if r["fcf_cover"] is not None), None)
        if last and last["fcf_cover"] < 1.0:
            dock(20, f"free cash flow covered the dividend only "
                     f"{last['fcf_cover']:.2f}× in {last['date'].year}")
    if a.dividend_cut:
        ttm, fwd = a.dividend_cut
        dock(30, f"the dividend has been cut from {_money(ttm)} to "
                 f"{_money(fwd)} a year — the trailing yield overstates what "
                 f"a buyer now receives")
    n = len(a.fiscal_dates)
    if n < 5:
        dock(30, f"only {n} years of filings")
    elif n < 8:
        dock(15, f"only {n} years of filings")
    if a.name_mismatch:
        dock(25, "filing and quote names disagree")
    regime = _recent_regime(a)
    if regime and not 0.67 <= regime[1] <= 1.5:
        recent, ratio = regime
        dock(25 if (ratio < 0.5 or ratio > 2.0) else 15,
             f"re-rated: the last 3 years traded near {recent:.1f}× against "
             f"a {a.normal_pe:.1f}× normal that may not come back")
    elif (a.normal_pe and a.normal_pe_mean
            and abs(a.normal_pe_mean - a.normal_pe) / a.normal_pe > 0.25):
        dock(10, "multiple history skewed by a re-rating")
    # Only EPS has an independent vendor figure; for the other metrics the
    # "cross-check" is the same filing and would always agree with itself.
    if (a.metric == "eps" and a.current_pe and a.pe_ttm
            and abs(a.current_pe - a.pe_ttm) / a.pe_ttm > 0.25):
        dock(10, "blended and vendor multiples disagree")
    return max(0.0, conf), why


@dataclass
class ValueAnalysis:
    ticker: str
    metric: str = "eps"
    window_years: int = 15
    basis_divisor: float = 1.0      # share-class translation, e.g. BRK.A -> BRK.B
    cik: Optional[int] = None
    entity_name: Optional[str] = None       # the name on the SEC filings
    quote_name: Optional[str] = None        # the name behind the price quote
    name_mismatch: bool = False             # filer and quote disagree
    price_currency: str = "USD"             # what the stock is quoted in
    filing_currency: Optional[str] = None   # what the accounts are kept in
    stale_days: int = 0             # age of the newest fiscal year in the data
    series_jump: Optional[float] = None     # size of any impossible break
    dividend_cut: Optional[Tuple[float, float]] = None   # (trailing, forward)
    coverage: List[dict] = field(default_factory=list)   # dividend cover by year

    # actuals
    fiscal_dates: List[date] = field(default_factory=list)
    fiscal_values: List[float] = field(default_factory=list)
    dividends: List[Optional[float]] = field(default_factory=list)
    source_tag: Optional[str] = None
    shares_tag: Optional[str] = None

    # forecast
    forecast_dates: List[date] = field(default_factory=list)
    forecast_values: List[float] = field(default_factory=list)
    forecast_basis: str = "growth"       # "growth" | "absolute"
    basis_gap_pct: Optional[float] = None
    forecast_source: Optional[str] = None   # "consensus" | "history" | None

    # price
    price_dates: List[date] = field(default_factory=list)
    price_values: List[float] = field(default_factory=list)
    price_now: Optional[float] = None

    # blended monthly metric aligned to price_dates
    blended: List[Optional[float]] = field(default_factory=list)
    monthly_pe: List[Optional[float]] = field(default_factory=list)

    # multiples
    normal_pe: Optional[float] = None
    normal_pe_mean: Optional[float] = None
    benchmark_pe: Optional[float] = BENCHMARK_PE
    benchmark_name: str = "benchmark"
    benchmark_rule: str = ""
    treasury_pct: Optional[float] = None    # 10-year yield, percent
    treasury_history: List[Tuple[date, float]] = field(default_factory=list)
    current_pe: Optional[float] = None       # price / blended metric today
    pe_ttm: Optional[float] = None           # price / vendor trailing 12m
    ttm_value: Optional[float] = None
    blended_now: Optional[float] = None

    # growth / quality
    growth_rate: Optional[float] = None      # fraction over the window
    growth_r2: Optional[float] = None
    dividend_growth: Optional[float] = None
    payout_ratio: Optional[float] = None
    dividend_yield: Optional[float] = None

    estimates: Estimates = field(default_factory=Estimates)
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: Optional[str] = None

    # ── derived views ────────────────────────
    def metric_at(self, at: Optional[date] = None) -> Optional[float]:
        if at is None:
            return self.fiscal_values[-1] if self.fiscal_values else None
        alld = self.fiscal_dates + self.forecast_dates
        allv = self.fiscal_values + self.forecast_values
        for d, v in zip(alld, allv):
            if d == at:
                return v
        return interpolate(alld, allv, [at])[0]

    def fair_value(self, multiple: Optional[float],
                   at: Optional[date] = None) -> Optional[float]:
        """Metric x multiple at a point in time. `at=None` uses the most
        recent actual; a forecast date uses the forecast metric."""
        m = self.metric_at(at)
        if m is None or multiple is None:
            return None
        return m * multiple

    def upside_to(self, multiple: Optional[float],
                  at: Optional[date] = None) -> Optional[float]:
        fv = self.fair_value(multiple, at)
        if fv is None or not self.price_now:
            return None
        return fv / self.price_now - 1.0

    def total_return(self, multiple: Optional[float],
                     target: Optional[date] = None) -> Optional[dict]:
        """Annualised return if the stock is worth `multiple` x the
        forecast metric on `target`, plus dividends collected along the
        way at the last actual payout ratio.

        The payout assumption is stated rather than buried: it holds the
        most recent payout ratio constant. A company about to cut or to
        start a dividend will not behave this way."""
        if not self.price_now or multiple is None:
            return None
        if target is None:
            if not self.forecast_dates:
                return None
            target = self.forecast_dates[-1]
        m = self.metric_at(target)
        if m is None or m <= 0:
            return None
        years = (target - date.today()).days / 365.25
        if years <= 0.05:
            return None
        exit_price = m * multiple

        div_total = 0.0
        if self.payout_ratio and self.payout_ratio > 0:
            for d, v in zip(self.forecast_dates, self.forecast_values):
                if d <= target and v and v > 0:
                    div_total += v * self.payout_ratio
        gross = (exit_price + div_total) / self.price_now
        annual = gross ** (1.0 / years) - 1.0 if gross > 0 else None
        return {"exit_price": exit_price, "dividends": div_total,
                "years": years, "total": gross - 1.0, "annualised": annual,
                "multiple": multiple, "target": target}

    def references(self):
        """The two reference multiples, ordered by value with their names.
        Normal is not always the higher one — a company the market has
        always distrusted can sit under 15x for its whole history."""
        return sorted((m, n) for m, n in
                      ((self.normal_pe, "normal"),
                       (self.benchmark_pe, self.benchmark_name)) if m)

    def zone(self) -> Optional[dict]:
        """Which of the chart's three bands the price is standing in today,
        and by how much on each side. This is a description of where the
        price is, not a recommendation about what to do."""
        refs = self.references()
        m = self.metric_at(date.today())
        if not refs or not self.price_now or not m or m <= 0:
            return None
        lines = [{"name": n, "multiple": mult, "value": m * mult,
                  "gap": m * mult / self.price_now - 1.0}
                 for mult, n in refs]
        if self.price_now < lines[0]["value"]:
            band = "under both references"
        elif len(lines) > 1 and self.price_now > lines[-1]["value"]:
            band = "over both references"
        else:
            band = "between the two references"
        return {"band": band, "lines": lines, "price": self.price_now,
                "metric": m}

    def window_multiples(self) -> List[Tuple[date, float]]:
        """Every monthly multiple inside the window that is a real multiple:
        positive, and not a units mismatch."""
        win_start = date.today() - timedelta(
            days=int(365.25 * self.window_years))
        lo, hi = sane_range(self.metric)
        return [(d, x) for d, x in zip(self.price_dates, self.monthly_pe)
                if x and lo <= x <= hi and d >= win_start]

    def scenarios(self, target: Optional[date] = None) -> Optional[dict]:
        """Bear, base and bull outcomes at the end of the forecast.

        Both axes come from data rather than from opinion:

          * THE EXIT MULTIPLE is the 25th, 50th and 75th percentile of what
            this stock has actually traded at over the window.
          * THE EARNINGS are the low, average and high of published
            consensus, applied as a RATIO to the base path so the GAAP
            basis of the history is preserved.

        THE ODDS ARE NOT A FORECAST. They are what the percentiles mean:
        over this window the multiple sat below the 25th a quarter of the
        time, between the quartiles half the time, and above the 75th a
        quarter. How often something happened is not how likely it is to
        happen next, and nothing here has been tested against outcomes.

        The pairing is deliberate rather than independent — a low estimate
        is matched with a low multiple — because estimates get cut and
        multiples compress at the same time. That widens the spread against
        treating the two as unrelated, which would be the optimistic error.

        Only drawn on a consensus forecast. Running scenarios off a line
        extrapolated from past growth would dress up an assumption as a
        distribution."""
        if not self.price_now or self.forecast_source != "consensus":
            return None
        if target is None:
            if not self.forecast_dates:
                return None
            target = self.forecast_dates[-1]
        base_metric = self.metric_at(target)
        if not base_metric or base_metric <= 0:
            return None
        hist = [m for _d, m in self.window_multiples()]
        if len(hist) < METER_MIN_MONTHS:
            return None
        years = (target - date.today()).days / 365.25
        if years <= 0.05:
            return None

        spread = self.estimates.dispersion()
        lo_f, hi_f = spread or (1.0, 1.0)
        payout = self.payout_ratio or 0.0
        now_mult = self.current_pe

        # The three multiples must BRACKET today's, or the set is not a set
        # of scenarios. PayPal trades at 9.9x while the 25th percentile of
        # its own history is 20x, so an unclamped "bear" case would have it
        # doubling — a bear case that returns +42%/yr is not a bear case.
        p25, p50, p75 = (percentile(hist, q) for q in (0.25, 0.50, 0.75))
        floored = bool(now_mult and p25 and now_mult < p25)
        capped = bool(now_mult and p75 and now_mult > p75)
        if now_mult:
            p25 = min(p25, now_mult)
            p75 = max(p75, now_mult)

        def outcome(name, prob, mult, factor):
            metric = base_metric * factor
            exit_price = metric * mult
            divs = 0.0
            if payout:
                divs = sum(v * factor * payout
                           for d, v in zip(self.forecast_dates,
                                           self.forecast_values)
                           if d <= target and v > 0)
            total = (exit_price + divs) / self.price_now - 1.0
            return {"name": name, "probability": prob, "multiple": mult,
                    "metric": metric, "exit_price": exit_price,
                    "dividends": divs, "total": total,
                    "rerating": (mult / now_mult) if now_mult else None,
                    "annualised": ((1.0 + total) ** (1.0 / years) - 1.0
                                   if total > -1.0 else None)}

        plan = (("bear", 0.25, p25, lo_f), ("base", 0.50, p50, 1.0),
                ("bull", 0.25, p75, hi_f))
        rows = [outcome(n, p, m, f) for n, p, m, f in plan]
        expected_total = sum(r["probability"] * r["total"] for r in rows)

        # The same three earnings outcomes with the multiple held where it
        # is today. The gap between this and the figure above is the part
        # of the return that depends on the market changing its mind.
        growth_total = None
        if now_mult:
            flat = [outcome(n, p, now_mult, f) for n, p, _m, f in plan]
            growth_total = sum(r["probability"] * r["total"] for r in flat)

        def annualise(tot):
            return ((1.0 + tot) ** (1.0 / years) - 1.0
                    if tot is not None and tot > -1.0 else None)

        return {
            "target": target, "years": years, "rows": rows,
            "expected_total": expected_total,
            "expected": annualise(expected_total),
            "expected_growth": annualise(growth_total),
            "floored": floored, "capped": capped, "spread": spread,
            "analysts": self.estimates.analysts_ny or self.estimates.analysts_cy,
        }

    def value_meter(self) -> dict:
        """Where today's price sits against this stock's own valuation,
        from 0 (expensive) to 100 (cheap), with every input exposed.

        The raw reading is pulled toward 50 in proportion to how little the
        underlying earnings can be trusted, so a shaky chart cannot produce
        a loud score. It summarises the chart. It predicts nothing, and it
        has not been tested against what prices did afterwards."""
        out = {"score": None, "raw": None, "confidence": None,
               "confidence_label": "", "label": "No reading",
               "components": [], "cautions": [], "reason": None}
        phrase = METRIC_PHRASE.get(self.metric, self.metric)
        if self.error:
            out["reason"] = self.error
            return out
        c = self.current_pe
        if not c or c <= 0 or not self.price_now:
            if self.stale_days > 500:
                out["reason"] = (f"The newest filing is {self.stale_days} "
                                 f"days old, so there is no current {phrase} "
                                 f"to price against.")
            elif self.fiscal_values and self.fiscal_values[-1] <= 0:
                out["reason"] = (f"{METRIC_LABELS.get(self.metric, phrase)} "
                                 f"is negative right now, and a multiple of a "
                                 f"loss is not a number. Revenue or cash flow "
                                 f"per share may still score.")
            else:
                out["reason"] = "No current multiple to score."
            return out

        parts = {}
        hist = self.window_multiples()
        if len(hist) >= METER_MIN_MONTHS:
            dearer = sum(1 for _d, x in hist if x > c)
            level = sum(1 for _d, x in hist if x == c)
            share = (dearer + 0.5 * level) / len(hist)
            yrs = max(1, round((hist[-1][0] - hist[0][0]).days / 365.25))
            if is_yield_metric(self.metric) and share >= 0.5:
                detail = (f"a higher yield than in {share:.0%} of months "
                          f"over the last {yrs} years")
            elif is_yield_metric(self.metric):
                detail = (f"a lower yield than in {1 - share:.0%} of months "
                          f"over the last {yrs} years")
            elif share >= 0.5:
                detail = (f"cheaper than {share:.0%} of months in the last "
                          f"{yrs} years")
            else:
                detail = (f"pricier than {1 - share:.0%} of months in the "
                          f"last {yrs} years")
            parts["history"] = ("vs its own history", 100.0 * share, detail)
        if self.normal_pe:
            if is_yield_metric(self.metric):
                title = "vs its normal yield"
                detail = (f"yields {mult_text(self.metric, c)} against its "
                          f"usual {mult_text(self.metric, self.normal_pe)}")
            else:
                title = "vs its normal multiple"
                gap = c / self.normal_pe - 1.0
                detail = (f"{abs(gap):.0%} {'below' if gap < 0 else 'above'} "
                          f"its normal {self.normal_pe:.1f}×")
            parts["normal"] = (title, _gap_score(self.normal_pe, c), detail)
        # The 15x yardstick is an earnings convention: fifteen times sales is
        # not a standard anyone uses, so revenue and cash flow leave this
        # part out. A dividend has a real outside reference — the risk-free
        # rate — and scores against that instead.
        if self.benchmark_pe and is_yield_metric(self.metric):
            parts["benchmark"] = (
                f"vs the {self.benchmark_name}",
                _gap_score(self.benchmark_pe, c),
                f"yields {mult_text(self.metric, c)} against "
                f"{mult_text(self.metric, self.benchmark_pe)} on government "
                f"debt, before any growth")
        elif self.benchmark_pe and self.metric == "eps":
            gap = c / self.benchmark_pe - 1.0
            parts["benchmark"] = (
                "vs the benchmark", _gap_score(self.benchmark_pe, c),
                f"{abs(gap):.0%} {'below' if gap < 0 else 'above'} the "
                f"{_mult_txt(self.benchmark_pe)}× benchmark")
        # Growth alone, at TODAY's multiple. Assuming a return to the normal
        # multiple would count the valuation gap a second time — the history
        # and normal parts already score it — and would promise a re-rating
        # to a stock whose normal may belong to a different era. An
        # extrapolated history is not an outlook, so it scores nothing.
        if self.forecast_source == "consensus":
            r = self.total_return(c)
            if r and r["annualised"] is not None:
                parts["outlook"] = (
                    "consensus outlook",
                    50.0 + 50.0 * math.tanh((r["annualised"] - OUTLOOK_NEUTRAL)
                                            / OUTLOOK_SCALE),
                    f"{r['annualised']:+.0%}/yr to {r['target'].year} from "
                    f"growth, if the multiple holds at {c:.1f}×")
        if len(parts) < 2:
            only = ", ".join(t for t, _s, _d in parts.values()) or "nothing"
            out["reason"] = (
                f"Only one part of the score can be computed here ({only}), "
                f"which is a single comparison rather than a score. The chart "
                f"still shows where the price sits.")
            return out

        total_w = sum(w for key, w in METER_WEIGHTS if key in parts)
        raw = 0.0
        for key, w in METER_WEIGHTS:
            if key not in parts:
                continue
            title, score, detail = parts[key]
            weight = w / total_w
            raw += weight * score
            out["components"].append({"key": key, "title": title,
                                      "score": score, "weight": weight,
                                      "detail": detail})

        conf, why = _meter_confidence(self)
        if "outlook" in parts:
            n_an = self.estimates.analysts_cy or 0
            if n_an and n_an < 4:
                conf = max(0.0, conf - 5)
                why.append(f"outlook rests on {n_an} analyst(s)")
        score = 50.0 + (raw - 50.0) * conf / 100.0
        out.update(score=score, raw=raw, confidence=conf,
                   confidence_label=("solid" if conf >= 80 else
                                     "mixed" if conf >= 50 else "weak"),
                   label=_meter_label(score), cautions=why)
        return out

    def summary_text(self) -> str:
        return _render_summary(self)

    def to_rows(self) -> List[dict]:
        """Year-by-year table — the numbers behind the chart, so an export
        can be checked against the filings line by line."""
        rows = []
        for i, d in enumerate(self.fiscal_dates):
            v = self.fiscal_values[i]
            px = _price_on(self.price_dates, self.price_values, d)
            rows.append({
                "fiscal_end": d.isoformat(),
                "actual_or_estimate": "actual",
                "value": v,
                "dividend": self.dividends[i] if i < len(self.dividends) else None,
                "price_at_fy_end": px,
                "pe_at_fy_end": (px / v) if (px and v and v > 0) else None,
                "normal_line": (v * self.normal_pe) if self.normal_pe else None,
                "benchmark_line": (v * self.benchmark_pe
                                   if self.benchmark_pe else None),
            })
        for d, v in zip(self.forecast_dates, self.forecast_values):
            rows.append({
                "fiscal_end": d.isoformat(),
                "actual_or_estimate": f"estimate ({self.forecast_basis})",
                "value": v,
                "dividend": (v * self.payout_ratio) if self.payout_ratio else None,
                "price_at_fy_end": None,
                "pe_at_fy_end": None,
                "normal_line": (v * self.normal_pe) if self.normal_pe else None,
                "benchmark_line": (v * self.benchmark_pe
                                   if self.benchmark_pe else None),
            })
        return rows


# A share count filed in the wrong unit is out by a power of a thousand,
# never by a little.
SHARE_SCALES = (1e-9, 1e-6, 1e-3, 1e3, 1e6, 1e9)

# How far from an exact power of a thousand a step may fall and still be a
# units error. It has to absorb the REAL change in the share count across
# the same boundary: Berkshire's 2009-to-2010 step is 1,000,000 times a
# genuine 5.4% rise in shares, and a 5% tolerance missed it and left the
# rest of the series divided by a million. It must still be nowhere near a
# basis difference — a Class-A-to-B factor of 1,500 is 50% away from a
# thousand and has to stay untouched.
SCALE_TOLERANCE = 0.30


def _scale_break(ratio: float) -> Optional[float]:
    """The power of a thousand this year-on-year ratio is, if it is one."""
    for scale in SHARE_SCALES:
        if abs(ratio / scale - 1.0) < SCALE_TOLERANCE:
            return scale
    return None


def _normalise_share_counts(shares: Dict[date, float], implied=None,
                            notes=None) -> Dict[date, float]:
    """Put every share count on one scale.

    XBRL share counts are meant to be absolute, and filers get this wrong.
    McDonald's tags 750,100,000 shares for FY2020 and 751.8 for FY2021 —
    same concept, same unit, the second one in millions — so revenue per
    share leaps from $34 to $31 MILLION at that year and the chart falls
    off a cliff.

    The break is found between CONSECUTIVE years rather than against an
    average: a units error is a step of almost exactly a thousand or a
    million from one year to the next, while a real share count moves by a
    few percent. Everything after such a step is put back on the earlier
    scale.

    Where the filing also reports net income and EPS, the correction is
    checked against the share count those two imply, which no units error
    in the share tag can touch. A correction that does not land on that
    figure is refused rather than applied, so a real difference in basis is
    never quietly "fixed" into a smaller, less visible error.

    Berkshire needs this as much as McDonald's: its 2008 and 2009 counts
    are filed as 1,548,960,000,000 against 1,545,751 the year before."""
    live = sorted((d, v) for d, v in shares.items() if v and v > 0)
    if len(live) < 2:
        return shares

    out = dict(shares)
    factor, fixed = 1.0, []
    prev = live[0][1]
    for d, v in live[1:]:
        step = _scale_break(v * factor / prev)
        if step:
            ref = (implied or {}).get(d)
            if ref and abs((v * factor / step) / ref - 1.0) > 0.25:
                step = None
        if step:
            factor /= step
            fixed.append(d)
        prev = v * factor
        out[d] = prev
    if fixed and notes is not None:
        notes.append(
            f"The share count changes scale partway through these filings — "
            f"{', '.join(str(d.year) for d in fixed)} "
            f"{'is' if len(fixed) == 1 else 'are'} tagged in a different unit "
            f"from the years before. Everything after the break is put back "
            f"on one scale; without that every per-share figure past it "
            f"would be out by a factor of a thousand or more.")
    return out


def _implied_shares(facts: dict, splits) -> Dict[date, float]:
    """The share count implied by net income divided by EPS.

    IFRS filers routinely report both of those and never tag a weighted
    average share count — Sony stops tagging one the year it switches
    taxonomy — which silently ends every per-share series that needs a
    denominator. Both figures are as filed and in the same currency, so the
    division recovers exactly the share count the filer used, and only the
    split adjustment is left to apply."""
    ni, _ = _annual_facts(facts, TAGS["net_income"], splits)
    eps, _ = _annual_facts(facts, TAGS["eps"], splits, per_share=True)
    out: Dict[date, float] = {}
    for end, rec in (eps or {}).items():
        n = (ni or {}).get(end)
        if not n or not rec["val"] or rec["cur"] != n["cur"]:
            continue
        count = n["val"] / rec["val"]
        if count > 0:
            out[end] = count * _split_factor_after(rec["filed"], splits)
    return out


def _per_share_from_absolute(facts: dict, tag_list, splits, fx=None,
                             notes=None):
    """Divide an absolute-money annual series by the diluted share count.

    The numerator is money and splits do not touch it; only the share count
    is restated onto today's basis. The currency conversion belongs to the
    numerator, so it is applied here rather than to the ratio."""
    raw, tag = _annual_facts(facts, tag_list, splits)
    if not raw:
        return None, None, None
    shares_raw, stag = _annual_facts(facts, TAGS["shares"], splits)
    shares = (_adjust_per_share(shares_raw, splits, invert=True)
              if shares_raw else {})
    implied = _implied_shares(facts, splits)
    if implied and any(end not in shares for end in raw):
        filled = [end for end in raw if end not in shares and end in implied]
        for end in filled:
            shares[end] = implied[end]
        if filled:
            stag = ((stag + " + " if stag else "")
                    + "implied from net income / EPS")
    if not shares:
        return None, tag, None
    shares = _normalise_share_counts(shares, implied, notes)
    out = {}
    for end, rec in raw.items():
        if shares.get(end):
            rate = fx(rec.get("cur", ""), end) if fx else 1.0
            out[end] = rec["val"] * rate / shares[end]
    return (out or None), tag, stag


def _metric_series(facts: dict, metric: str, splits, fx=None, notes=None):
    """The requested metric as an annual per-share series on today's share
    base. Returns (series, source_tag, shares_tag, error)."""
    if metric in PER_SHARE_NATIVE:
        raw, tag = _annual_facts(facts, TAGS[metric], splits, per_share=True)
        if raw:
            return _adjust_per_share(raw, splits, fx=fx), tag, None, None
        if metric != "eps":
            return None, None, None, (
                f"No annual {METRIC_LABELS[metric]} found in this company's "
                f"XBRL filings.")
        # EPS tagged only per share class — rebuild it from net income
        out, tag, stag = _per_share_from_absolute(
            facts, TAGS["net_income"], splits, fx, notes)
        if out:
            return out, f"{tag} / {stag} (EPS computed)", stag, None
        return None, None, None, (
            "No annual EPS in this company's XBRL filings, and net income "
            "or the share count is missing too — nothing to divide.")

    out, tag, stag = _per_share_from_absolute(facts, TAGS[metric], splits, fx,
                                              notes)
    if out:
        return out, tag, stag, None
    if tag and not stag:
        return None, tag, None, (
            "The diluted share count is missing from the filings, so a "
            "per-share series cannot be built for this metric.")
    return None, None, None, (
        f"No annual {METRIC_LABELS.get(metric, metric)} found in this "
        f"company's XBRL filings.")


# Legal-form suffixes and punctuation carry no identity. "MICROSOFT CORP"
# and "Microsoft Corporation" are the same company; the comparison has to
# survive that without waving through two genuinely different names.
_NAME_NOISE = {"inc", "incorporated", "corp", "corporation", "co", "company",
               "ltd", "limited", "plc", "lp", "llc", "holdings", "holding",
               "group", "the", "sa", "nv", "ag", "class", "common", "stock",
               "new", "cl"}


def _name_tokens(name: str) -> List[str]:
    cleaned = "".join(c if (c.isalnum() or c.isspace()) else " "
                      for c in name.lower())
    return [t for t in cleaned.split() if t and t not in _NAME_NOISE]


def _same_company(sec_name: str, quote_name: str) -> bool:
    """Do these two names plausibly describe one company?

    Deliberately permissive — this exists to catch a wrong-company mixup,
    not to police spelling, and a false alarm on every ticker would train
    the reader to ignore the real one."""
    a, b = _name_tokens(sec_name), _name_tokens(quote_name)
    if not a or not b:
        return True                 # nothing to compare; do not cry wolf
    if a[0] == b[0]:
        return True
    return bool(set(a) & set(b))


def _coverage(facts: dict, splits, fx, dividends: Dict[date, float]) -> List[dict]:
    """How many times over the company covered its dividend, by year.

    Earnings cover is the figure everyone quotes. Cash cover is the honest
    one: a dividend is paid out of cash, not out of accounting profit, and
    free cash flow is operating cash flow after the capital spending the
    business needs just to stay standing. A company can earn a profit and
    still borrow to pay you."""
    if not dividends:
        return []
    eps, _t, _s, _e = _metric_series(facts, "eps", splits, fx)
    ocf, _t2, _s2, _e2 = _metric_series(facts, "ocf", splits, fx)
    capex, _t3, _s3 = _per_share_from_absolute(facts, TAGS["capex"], splits, fx)
    rows = []
    for d in sorted(dividends):
        dps = dividends.get(d)
        if not dps or dps <= 0:
            continue
        e = (eps or {}).get(d)
        o = (ocf or {}).get(d)
        c = (capex or {}).get(d)
        fcf = (o - c) if (o is not None and c is not None) else None
        rows.append({
            "date": d, "dps": dps, "eps": e, "fcf": fcf,
            "eps_cover": (e / dps) if e is not None else None,
            "fcf_cover": (fcf / dps) if fcf is not None else None})
    return rows


def _price_on(dates: List[date], values: List[float],
              target: date) -> Optional[float]:
    """Last close at or before `target`."""
    best = None
    for d, v in zip(dates, values):
        if d <= target:
            best = v
        else:
            break
    return best


def analyze(ticker: str, metric: str = "eps", window_years: int = 15,
            forecast_years: int = 3, forecast_basis: str = "growth",
            basis_divisor: float = 1.0,
            cik: Optional[int] = None) -> ValueAnalysis:
    """Build the full picture for one ticker.

    metric         eps | ocf | dividends | revenue
    window_years   lookback for the normal multiple and the growth rate
    forecast_basis "growth"   consensus growth applied to the last GAAP actual
                   "absolute" consensus EPS as published (adjusted basis)
    basis_divisor  divides the filed per-share figures when the filing's
                   share class is not the one being priced — 1500 turns
                   Berkshire's Class A EPS into a Class B basis
    cik            override the SEC registrant. A reorganisation gives a
                   company a new CIK holding only a few quarters, while
                   the decades of filings stay under the predecessor
    """
    a = ValueAnalysis(ticker=ticker.upper().strip(), metric=metric,
                      window_years=window_years,
                      forecast_basis=forecast_basis,
                      basis_divisor=float(basis_divisor or 1.0))

    # ── 1. fundamentals from the filings ─────
    cik = cik or cik_for(a.ticker)
    if cik is None:
        a.error = (f"{a.ticker} is not in the SEC ticker index. Foreign "
                   f"issuers that file neither a 10-K nor a 20-F, and most "
                   f"ETFs, have no XBRL earnings history to chart.")
        return a
    a.cik = int(cik)
    try:
        facts = company_facts(a.cik)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            a.error = (
                f"The SEC publishes no XBRL company facts for CIK {a.cik}. "
                f"That is what an ETF, a closed-end fund, or a trust looks "
                f"like here — they file holdings, not an income statement, so "
                f"there is no earnings series to value against.")
        else:
            a.error = f"SEC companyfacts fetch failed: HTTP {exc.code}"
        return a
    except Exception as exc:
        a.error = f"SEC companyfacts fetch failed: {type(exc).__name__}: {exc}"
        return a
    a.entity_name = facts.get("entityName")

    splits = get_splits(a.ticker)
    if splits:
        a.notes.append("Split-normalised using " + ", ".join(
            f"{r:g}-for-1 on {d}" for d, r in splits[-4:]))

    a.price_currency = quote_currency(a.ticker)
    fx = _fx_converter(a.price_currency, a.notes, a.warnings)
    adjusted, a.source_tag, a.shares_tag, err = _metric_series(
        facts, metric, splits, fx, a.notes)
    a.filing_currency = next(iter(fx.seen), a.price_currency)
    if err:
        a.error = (f"{err} (CIK {a.cik}, {a.entity_name or '?'}) — if this "
                   f"ticker has been trading for years, a reorganisation may "
                   f"have moved it to a new registrant; the history stays "
                   f"under the predecessor CIK, which the CIK override "
                   f"reaches.")
        return a

    ends = sorted(adjusted)
    cutoff = date.today() - timedelta(days=int(365.25 * window_years) + 200)
    ends = [e for e in ends if e >= cutoff]
    if len(ends) < 3:
        a.error = (
            f"Only {len(ends)} fiscal year(s) of XBRL data for CIK "
            f"{a.cik} ({a.entity_name or '?'}) inside a {window_years}-year "
            f"window. Either the window predates XBRL, which was phased in "
            f"around 2009, or a reorganisation moved this ticker to a new "
            f"registrant and the history stayed behind under the predecessor "
            f"CIK — try the CIK override.")
        return a
    if len(ends) < window_years - 2 and (date.today() - ends[0]).days < 2000:
        a.warnings.append(
            f"CIK {a.cik} ({a.entity_name or '?'}) only starts filing in "
            f"{ends[0].year}, well after the {window_years}-year window "
            f"opens. If this ticker has a longer trading history than that, "
            f"the earlier filings sit under a predecessor CIK and the normal "
            f"multiple below covers only the recent stretch.")
    a.fiscal_dates = ends
    a.fiscal_values = [adjusted[e] for e in ends]

    jump = worst_jump(a.fiscal_dates, a.fiscal_values)
    if jump:
        d0, d1, ratio = jump
        a.series_jump = ratio
        a.warnings.append(
            f"{METRIC_LABELS.get(metric, metric)} moves by a factor of "
            f"{ratio:,.0f} between {d0.year} and {d1.year}. No business does "
            f"that: it is a break in the filings — a change of units, or a "
            f"tag that means something different on each side of it — and "
            f"every multiple spanning it is unusable.")

    # Staleness is decided here, before anything is built on top of the
    # series. Grafting this year's consensus growth onto an actual from a
    # decade ago produces a forecast and a "current" multiple that describe
    # no company at all, so neither is drawn.
    a.stale_days = (date.today() - ends[-1]).days
    if a.stale_days > 500:
        a.warnings.append(
            f"The most recent fiscal year in these filings ends {ends[-1]}, "
            f"{a.stale_days} days ago. Either the annual report is not yet in "
            f"XBRL under this concept, or the company tags it only per share "
            f"class. No forecast and no current multiple are computed on a "
            f"series this stale — the history below is still real, the "
            f"present is simply not in it.")

    # dividends ride along for the payout ratio and the shaded band
    div_raw, _ = _annual_facts(facts, TAGS["dividends"], splits, per_share=True)
    div_adj = _adjust_per_share(div_raw, splits, fx=fx) if div_raw else {}
    a.dividends = [div_adj.get(e) for e in ends]
    paid = [i for i, d in enumerate(a.dividends) if d]
    if paid and (paid[-1] - paid[0] + 1) > len(paid):
        holes = [ends[i].year for i in range(paid[0], paid[-1] + 1)
                 if not a.dividends[i]]
        a.notes.append(
            f"Dividends per share are not tagged in "
            f"{', '.join(str(y) for y in holes)}, between years that are. "
            f"The dividend line breaks over those years rather than drawing "
            f"a straight line across a hole in the filings.")
    if a.basis_divisor != 1.0:
        a.fiscal_values = [v / a.basis_divisor for v in a.fiscal_values]
        a.dividends = [(d / a.basis_divisor) if d else d for d in a.dividends]
        a.notes.append(
            f"Per-share figures divided by {a.basis_divisor:g} to put the "
            f"filed share class onto this ticker's basis.")

    # ── 2. price ─────────────────────────────
    try:
        a.price_dates, a.price_values = monthly_prices(a.ticker)
    except Exception as exc:
        a.warnings.append(f"Price history unavailable: "
                          f"{type(exc).__name__}: {exc}")
    a.price_now = last_price(a.ticker)
    a.quote_name = yahoo_name(a.ticker)
    a.name_mismatch = bool(
        a.entity_name and a.quote_name
        and not _same_company(a.entity_name, a.quote_name))
    if a.name_mismatch:
        a.warnings.append(
            f"The filings are {a.entity_name} (CIK {a.cik}) but the price "
            f"quote is {a.quote_name}. Those may still be the same business "
            f"under two naming conventions — but check, because a recycled "
            f"ticker or a reorganisation would look exactly like this, and "
            f"the chart would be one company's earnings under another "
            f"company's price.")
    if a.price_now is None and a.price_values:
        a.price_now = a.price_values[-1]
        a.warnings.append("Live quote unavailable — using the last monthly "
                          "close, which may be weeks old.")

    # ── 3. growth and how much to trust it ───
    span_years = (a.fiscal_dates[-1] - a.fiscal_dates[0]).days / 365.25
    a.growth_rate = cagr(a.fiscal_values[0], a.fiscal_values[-1], span_years)
    a.growth_r2 = log_fit_r2(
        [(d - a.fiscal_dates[0]).days / 365.25 for d in a.fiscal_dates],
        a.fiscal_values)
    if a.growth_rate is None:
        a.warnings.append(
            "Growth rate undefined — the window starts or ends on a "
            "non-positive value, so there is no compounding rate to quote.")
    if a.growth_r2 is not None and a.growth_r2 < 0.5:
        a.warnings.append(
            f"The series tracks a constant growth rate poorly (R-squared "
            f"{a.growth_r2:.2f}). The growth figure is an average over "
            f"something that is not actually compounding — treat any line "
            f"drawn off it as decoration, not a forecast.")
    neg = [d.year for d, v in zip(a.fiscal_dates, a.fiscal_values) if v <= 0]
    if neg:
        a.warnings.append(
            f"Non-positive {METRIC_LABELS.get(metric, metric)} in "
            f"{', '.join(str(y) for y in neg)}. Those months are excluded "
            f"from the normal multiple — a P/E on a loss is not a number.")

    # ── 4. blended monthly metric, multiple history ──
    if a.price_dates:
        a.blended = interpolate(a.fiscal_dates, a.fiscal_values, a.price_dates)
        a.monthly_pe = [(p / m) if (m and m > 0 and p) else None
                        for p, m in zip(a.price_values, a.blended)]
        win_start = date.today() - timedelta(days=int(365.25 * window_years))
        raw_window = [pe for d, pe in zip(a.price_dates, a.monthly_pe)
                      if pe is not None and pe > 0 and d >= win_start]
        lo, hi = sane_range(metric)
        sane = median(raw_window)
        if sane is not None and not (lo <= sane <= hi):
            msg = (f"The price-to-{METRIC_LABELS.get(metric, metric)} multiple "
                   f"works out at {sane:,.1f}x. That is a units mismatch, not "
                   f"a valuation: the filings report per-share figures on a "
                   f"different share class than the one {a.ticker} prices "
                   f"(BRK.A vs BRK.B), or this is an ADR with a ratio.")
            suggested = sane / 20.0 if sane > hi else None
            if suggested:
                msg += (f" A share-class divisor near {suggested:,.0f} would "
                        f"line the two up.")
            a.warnings.append(msg)
        in_window = [pe for pe in raw_window if lo <= pe <= hi]
        a.normal_pe = median(in_window)
        a.normal_pe_mean = mean(in_window)
        if in_window and len(in_window) < 24:
            a.warnings.append(
                f"The normal multiple rests on only {len(in_window)} monthly "
                f"observations.")
        if (a.normal_pe and a.normal_pe_mean
                and abs(a.normal_pe_mean - a.normal_pe) / a.normal_pe > 0.25):
            a.warnings.append(
                f"Median multiple {a.normal_pe:.1f}x and mean "
                f"{a.normal_pe_mean:.1f}x disagree by more than 25% — the "
                f"multiple history is skewed by a re-rating, and 'normal' is "
                f"doing a lot of work in that word.")

    # ── 5. the benchmark multiple ────────────
    g_pct = (a.growth_rate or 0.0) * 100.0
    if a.growth_rate is None or g_pct <= 15.0:
        a.benchmark_pe = BENCHMARK_PE
        a.benchmark_rule = ("15x — the long-run market average and the "
                            "Graham defensive multiple")
    else:
        a.benchmark_pe = min(g_pct, BENCHMARK_PE_CAP)
        a.benchmark_rule = (
            f"{a.benchmark_pe:.1f}x — PEG convention, multiple set to the "
            f"{g_pct:.1f}% growth rate"
            + (f", capped at {BENCHMARK_PE_CAP:g}x"
               if g_pct > BENCHMARK_PE_CAP else ""))

    if is_yield_metric(metric):
        # 15x earnings says nothing about a dividend. What a dividend
        # competes with is the risk-free rate: government debt pays you to
        # take no risk at all, and a stock yielding less than that is asking
        # to be paid in growth instead.
        a.treasury_history = treasury_series()
        ty = a.treasury_history[-1][1] if a.treasury_history else None
        if ty:
            a.treasury_pct = ty
            a.benchmark_pe = 100.0 / ty
            a.benchmark_name = "10-yr Treasury"
            a.benchmark_rule = (f"the 10-year Treasury at {ty:.1f}% — what "
                                f"cash pays for no risk and no growth")
            a.notes.append(
                f"The outside reference here is the 10-year Treasury, "
                f"{ty:.1f}% today. Above that line the dividend pays more "
                f"than government debt; below it you are accepting less than "
                f"cash for taking equity risk, on the bet that the dividend "
                f"grows into it. The lower panel plots both yields.")
        else:
            a.benchmark_pe = None
            a.benchmark_rule = ""
            a.warnings.append(
                "The 10-year Treasury yield could not be fetched, so this "
                "chart has no outside reference — only the stock's own "
                "normal yield.")

    # ── 6. yield and payout ──────────────────
    a.ttm_value = _ttm_metric(a)
    if a.ttm_value and a.ttm_value > 0 and a.price_now:
        a.pe_ttm = a.price_now / a.ttm_value
    last_div = next((d for d in reversed(a.dividends) if d), None)
    if last_div and a.price_now:
        a.dividend_yield = last_div / a.price_now
    if last_div and a.fiscal_values[-1] > 0:
        a.payout_ratio = last_div / a.fiscal_values[-1]
        if a.payout_ratio > 1.2:
            a.warnings.append(
                f"Payout ratio {a.payout_ratio:.0%} — the dividend is being "
                f"paid out of something other than this year's earnings.")
    a.coverage = _coverage(facts, splits, fx,
                           {d: v for d, v in zip(a.fiscal_dates, a.dividends)
                            if v})
    last_cover = next((r for r in reversed(a.coverage)
                       if r["fcf_cover"] is not None or
                       r["eps_cover"] is not None), None)
    if last_cover and is_yield_metric(metric):
        cover = last_cover["fcf_cover"]
        basis = "free cash flow"
        if cover is None:
            cover, basis = last_cover["eps_cover"], "earnings"
        if cover is not None and cover < 1.0:
            a.warnings.append(
                f"The dividend was not covered in {last_cover['date'].year}: "
                f"{basis} came to {cover:.2f}x the payout. A dividend paid "
                f"out of the balance sheet is a dividend with a clock on it.")

    divs = [(d, v) for d, v in zip(a.fiscal_dates, a.dividends) if v]
    if len(divs) >= 3:
        a.dividend_growth = cagr(divs[0][1], divs[-1][1],
                                 (divs[-1][0] - divs[0][0]).days / 365.25)

    if is_yield_metric(metric) and a.price_now:
        ttm, fwd = dividend_rates(a.ticker)
        if ttm and fwd and fwd < ttm * 0.8:
            a.dividend_cut = (ttm, fwd)
            a.warnings.append(
                f"The dividend has been cut. The last twelve months paid "
                f"{_money(ttm)} a share, a {ttm / a.price_now:.1%} yield, but "
                f"the current rate annualises to {_money(fwd)} — "
                f"{fwd / a.price_now:.1%}. Every yield on this chart is the "
                f"trailing one, and a buyer today receives the lower figure.")

    # ── 7. forecast ──────────────────────────
    a.estimates = fetch_estimates(a.ticker, a.metric)
    if a.stale_days <= 500:
        _build_forecast(a, forecast_years)
    else:
        return a

    # Re-blend now that the forecast exists. The months between the last
    # filing and today sit inside the current fiscal year, and pricing them
    # against last year's figure is exactly what makes the lower panel
    # disagree with the "now" marker drawn above it.
    blend_dates, blend_values = list(a.fiscal_dates), list(a.fiscal_values)
    if is_yield_metric(metric) and a.ttm_value:
        # Walk the recent months from the last filed year to the dividend
        # actually being paid now, so a cut shows up instead of waiting a
        # year for the next 10-K.
        blend_dates.append(date.today())
        blend_values.append(a.ttm_value)
    elif a.forecast_source == "consensus":
        blend_dates += a.forecast_dates
        blend_values += a.forecast_values
    if len(blend_dates) > len(a.fiscal_dates) and a.price_dates:
        a.blended = interpolate(blend_dates, blend_values, a.price_dates)
        a.monthly_pe = [(p / m) if (m and m > 0 and p) else None
                        for p, m in zip(a.price_values, a.blended)]
        win_start = date.today() - timedelta(days=int(365.25 * window_years))
        lo, hi = sane_range(metric)
        in_window = [pe for d, pe in zip(a.price_dates, a.monthly_pe)
                     if pe is not None and lo <= pe <= hi and d >= win_start]
        if in_window:
            a.normal_pe = median(in_window)
            a.normal_pe_mean = mean(in_window)

    # The CURRENT multiple has to sit on the same basis as the normal one,
    # or the two cannot be compared and "fair value at today's multiple"
    # stops equalling today's price. Both use the blended metric — the
    # fiscal series interpolated to a date — not a vendor's trailing EPS.
    if is_yield_metric(metric) and a.ttm_value:
        a.blended_now = a.ttm_value
    elif a.forecast_source == "consensus":
        a.blended_now = a.metric_at(date.today())
    else:
        # An extrapolation is not an estimate. Blending today's price
        # against a line drawn from past growth is how a company that has
        # just cut its dividend gets priced as though it had raised it.
        a.blended_now = a.fiscal_values[-1]
        a.notes.append(
            "No consensus to blend into, so the current figure is the last "
            f"actual year ({a.fiscal_dates[-1]}) rather than a part-elapsed "
            f"one.")
    if a.blended_now and a.blended_now > 0 and a.price_now:
        a.current_pe = a.price_now / a.blended_now
    if a.current_pe and a.pe_ttm and a.pe_ttm > 0:
        drift = abs(a.current_pe - a.pe_ttm) / a.pe_ttm
        if drift > 0.25:
            a.notes.append(
                f"Blended multiple {a.current_pe:.1f}x against "
                f"{a.pe_ttm:.1f}x on Yahoo's trailing EPS. The gap is the "
                f"GAAP-versus-adjusted basis plus the part of the fiscal "
                f"year already elapsed; the chart uses the blended figure "
                f"throughout.")

    return a


def _ttm_metric(a: ValueAnalysis) -> Optional[float]:
    """Trailing twelve months, for the current multiple. Yahoo's trailing
    EPS is the practical source — the only one that updates between annual
    filings. Falls back to the last fiscal year."""
    if a.metric == "eps":
        try:
            import yfinance_throttle  # noqa: F401
            import yfinance as yf
            v = _num(yf.Ticker(a.ticker).info.get("trailingEps"))
            if v:
                return v
        except Exception:
            pass
    if a.metric == "dividends":
        # Actual cash paid over the last four quarters. More current than an
        # annual filing, which can predate a change in the rate by a year.
        ttm, _fwd = dividend_rates(a.ticker)
        if ttm:
            return ttm
    return a.fiscal_values[-1] if a.fiscal_values else None


def _build_forecast(a: ValueAnalysis, years: int):
    """Extend the metric forward.

    Consensus is published on an ADJUSTED basis; the actuals here are GAAP
    as filed. Pasting the two together end to end draws a step that is an
    accounting definition change wearing the costume of growth. So the
    default takes consensus GROWTH and applies it to the last GAAP actual,
    and reports how big the level gap was."""
    e = a.estimates
    if not a.fiscal_values:
        return
    last_actual = a.fiscal_values[-1]
    last_date = a.fiscal_dates[-1]

    # size of the seam, whenever both sides are visible
    if e.cy_year_ago_eps and last_actual > 0:
        a.basis_gap_pct = e.cy_year_ago_eps / last_actual - 1.0
        if abs(a.basis_gap_pct) > 0.03:
            a.notes.append(
                f"Consensus prior-year figure {e.cy_year_ago_eps:.2f} vs GAAP "
                f"filed {last_actual:.2f} — a {a.basis_gap_pct:+.1%} basis gap "
                f"(adjusted vs GAAP). The forecast line uses growth rates, so "
                f"that gap is not drawn as a jump in earnings.")

    growths: List[float] = []
    if e.cy_growth is not None:
        growths.append(e.cy_growth)
    if e.ny_growth is not None:
        growths.append(e.ny_growth)
    from_consensus = bool(growths)
    # third year onward: published long-term growth, else the last explicit
    # consensus year, else this company's own historical rate
    tail = e.ltg
    if tail is None:
        tail = growths[-1] if growths else a.growth_rate
    while len(growths) < years and tail is not None:
        growths.append(tail)

    phrase = METRIC_PHRASE.get(a.metric, a.metric)
    if not growths:
        a.warnings.append(
            f"No consensus for {phrase} and no historical growth rate "
            f"to extend — the chart shows history only, and every valuation "
            f"figure is against trailing numbers.")
        return

    if a.forecast_basis == "absolute":
        seq = [v for v in (e.cy_eps, e.ny_eps) if v]
        if not seq:
            if a.metric == "eps":
                why = "No absolute consensus published"
            else:
                why = (f"Consensus for {phrase} is a company total, "
                       f"not a per-share level")
            a.warnings.append(f"{why} — drawing the growth-rate forecast "
                              f"instead.")
            a.forecast_basis = "growth"
        else:
            cur = seq[-1]
            for i in range(years):
                if i < len(seq):
                    v = seq[i]
                elif tail is not None:
                    cur *= (1.0 + tail)
                    v = cur
                else:
                    break
                a.forecast_dates.append(_add_years(last_date, i + 1))
                a.forecast_values.append(v)
            a.forecast_source = "consensus"
            a.notes.append(
                "Forecast drawn at ABSOLUTE consensus (adjusted basis) — it "
                "will not line up with the GAAP history to its left.")
            _forecast_caveats(a, e, years)
            return

    if last_actual <= 0:
        # A growth rate applied to a loss compounds the loss. Consensus is
        # forecasting a recovery to a positive number, which only the
        # absolute basis can express.
        a.warnings.append(
            f"The last actual year is a loss ({last_actual:.2f}), so a growth "
            f"rate cannot be applied to it — consensus is forecasting a level, "
            f"not a multiple of a negative number. Switch the estimate basis "
            f"to the consensus level to draw a forecast here.")
        return

    cur = last_actual
    for i in range(min(years, len(growths))):
        cur *= (1.0 + growths[i])
        a.forecast_dates.append(_add_years(last_date, i + 1))
        a.forecast_values.append(cur)
    if not a.forecast_dates:
        return
    if from_consensus:
        a.forecast_source = "consensus"
        if a.metric == "revenue":
            a.notes.append(
                "Revenue consensus is a company total. The per-share line "
                "assumes a flat share count, so it runs high for a company "
                "issuing stock and low for one buying it back.")
        _forecast_caveats(a, e, years)
    else:
        a.forecast_source = "history"
        a.notes.append(
            f"Analysts publish no consensus for {phrase}, so the "
            f"forecast extends its own historical growth rate of "
            f"{tail:+.1%}/yr. That is an extrapolation, not an estimate, and "
            f"the value meter leaves it out.")


def _forecast_caveats(a: ValueAnalysis, e: Estimates, years: int):
    n_an = e.analysts_cy or 0
    if n_an and n_an < 4:
        a.warnings.append(
            f"Only {n_an} analyst(s) behind the current-year consensus. A thin "
            f"consensus is one person's model.")
    if e.ltg is None and years > 2:
        a.notes.append(
            "No published long-term growth rate — years beyond the second are "
            "extended at the last consensus year's rate.")


def _add_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:      # 29 February
        return d.replace(year=d.year + n, day=28)


# ─────────────────────────────────────────────
# TEXT REPORT
# ─────────────────────────────────────────────

def _pct(v, digits=1):
    return "—" if v is None else f"{v * 100:.{digits}f}%"


def _x(v):
    return "—" if v is None else f"{v:.1f}x"


def _money(v):
    return "—" if v is None else f"${v:,.2f}"


def _scenario_lines(a: ValueAnalysis) -> List[str]:
    s = a.scenarios()
    if not s:
        return []
    out = ["",
           f"SCENARIOS TO {s['target']}  ({s['years']:.1f} years)",
           "        odds       exit   multiple   re-rate     return"]
    for row in reversed(s["rows"]):               # bull at the top
        ann = row["annualised"]
        ret = "—" if ann is None else f"{ann:+.1%}/yr"
        rr = row["rerating"]
        rr_txt = "—" if rr is None else f"×{rr:.2f}"
        out.append(f"  {row['name']:<6}{row['probability']:>4.0%}"
                   f"{_money(row['exit_price']):>11}"
                   f"{mult_text(a.metric, row['multiple']):>11}"
                   f"{rr_txt:>10}{ret:>11}")
    exp, grow = s["expected"], s["expected_growth"]
    out.append(f"  {'expected':<32}{'':>10}"
               f"{('—' if exp is None else f'{exp:+.1%}/yr'):>11}")
    if grow is not None:
        out.append(f"  {'growth alone, no re-rating':<32}{'':>10}"
                   f"{f'{grow:+.1%}/yr':>11}")

    spread = s["spread"]
    if spread:
        out.append(f"  Earnings span the low-to-high of "
                   f"{s['analysts'] or '?'} analysts ({spread[0]:.0%} to "
                   f"{spread[1]:.0%} of the average); the exit multiple is "
                   f"this stock's own 25th, 50th and 75th percentile.")
    else:
        out.append("  No published high/low estimates, so only the exit "
                   "multiple varies across these three.")
    if s["floored"]:
        out.append("  This stock already trades below the 25th percentile of "
                   "its own window, so the bear case holds the multiple where "
                   "it is — its history offers nothing cheaper to point at.")
    if s["capped"]:
        out.append("  This stock already trades above the 75th percentile of "
                   "its own window, so the bull case holds the multiple where "
                   "it is rather than inventing a further re-rating.")
    out.append("  The odds are the percentile definition, not a forecast, "
               "and a low estimate is paired with a low multiple because the "
               "two move together. Nothing here is tested against outcomes.")
    return out


def _coverage_lines(a: ValueAnalysis) -> List[str]:
    rows = [r for r in a.coverage
            if r["eps_cover"] is not None or r["fcf_cover"] is not None]
    if not rows:
        return []
    last = rows[-1]

    def cov(v):
        return "—" if v is None else f"{v:.2f}×"

    out = ["", f"DIVIDEND COVER ({last['date'].year})",
           f"  earnings         {cov(last['eps_cover'])}   "
           f"({_money(last['eps'])} earned against {_money(last['dps'])} paid)",
           f"  free cash flow   {cov(last['fcf_cover'])}   "
           f"({_money(last['fcf'])} left after capital spending)"]
    span = [r for r in rows if r["fcf_cover"] is not None][-5:]
    if len(span) >= 2:
        out.append("  last five years  " +
                   "  ".join(f"{r['date'].year} {r['fcf_cover']:.1f}×"
                             for r in span))
    out.append("  Cash cover is the one that matters: a dividend is paid out "
               "of cash, not out of profit.")
    return out


def _currency_lines(a: ValueAnalysis) -> List[str]:
    if not a.filing_currency or a.filing_currency == a.price_currency:
        return []
    return [f"Currency                 filed in {a.filing_currency}, priced "
            f"in {a.price_currency} — converted at each fiscal year end"]


def _meter_lines(a: ValueAnalysis) -> List[str]:
    m = a.value_meter()
    if m["score"] is None:
        return [f"VALUE METER              no reading — {m['reason']}", ""]
    head = (f"VALUE METER              {m['score']:.0f} / 100  {m['label']}"
            f"   (confidence {m['confidence']:.0f}, {m['confidence_label']}")
    if abs(m["raw"] - m["score"]) >= 3:
        head += f"; reads {m['raw']:.0f} before it"
    lines = [head + ")"]
    for comp in m["components"]:
        lines.append(f"  {comp['title']:<24}{comp['score']:>4.0f}  "
                     f"({comp['weight']:.0%})  {comp['detail']}")
    lines += [f"  - confidence: {why}" for why in m["cautions"]]
    return lines + [""]


def _render_summary(a: ValueAnalysis) -> str:
    if a.error:
        return f"{a.ticker}: {a.error}"
    label = METRIC_LABELS.get(a.metric, a.metric)
    span = (a.fiscal_dates[-1] - a.fiscal_dates[0]).days / 365.25
    out = [f"{a.ticker} — VALUE ANALYSIS ({label})",
           "=" * 68,
           f"Filings                  {a.entity_name or '?'}  "
           f"(SEC CIK {a.cik})",
           f"Price quote              {a.quote_name or '?'}  (Yahoo)",
           ] + _currency_lines(a) + [
           ""] + _meter_lines(a) + [
           f"Price now                {_money(a.price_now)}",
           f"{'Current yield' if is_yield_metric(a.metric) else 'Current multiple':<25}"
           f"{mult_text(a.metric, a.current_pe)}   "
           f"(against a blended {_money(a.blended_now)})",
           f"  vendor cross-check     {mult_text(a.metric, a.pe_ttm)}   "
           f"(against a trailing 12m {_money(a.ttm_value)})",
           f"{'Normal yield' if is_yield_metric(a.metric) else 'Normal multiple':<25}"
           f"{mult_text(a.metric, a.normal_pe)}   "
           f"(median monthly, {a.window_years}yr window)",
           f"{(a.benchmark_name if is_yield_metric(a.metric) else 'Benchmark multiple'):<25}"
           f"{mult_text(a.metric, a.benchmark_pe)}   {a.benchmark_rule}",
           "",
           f"Growth                   {_pct(a.growth_rate)}/yr over "
           f"{span:.0f} years ({len(a.fiscal_dates)} fiscal years)",
           f"Growth fit (R-squared)   "
           f"{'—' if a.growth_r2 is None else f'{a.growth_r2:.2f}'}",
           # On the dividend chart the yield IS the headline above, and the
           # payout ratio is dividends over dividends. Only growth adds
           # anything here.
           (f"Dividend growth          {_pct(a.dividend_growth)}/yr"
            if is_yield_metric(a.metric) else
            f"Dividend yield           {_pct(a.dividend_yield, 2)}   "
            f"payout {_pct(a.payout_ratio, 0)}   "
            f"growth {_pct(a.dividend_growth)}"),
           ""]

    z = a.zone()
    if z:
        out.append("WHERE PRICE SITS TODAY")
        out.append(f"  The {a.ticker} price of {_money(z['price'])} is "
                   f"{z['band']}.")
        for ln in z["lines"]:
            side = "below" if ln["gap"] > 0 else "above"
            out.append(
                f"    {ln['name']} {mult_text(a.metric, ln['multiple'])} puts "
                f"the line at {_money(ln['value'])} — price is "
                f"{abs(ln['gap']):.0%} {side} it")
        if is_yield_metric(a.metric):
            out.append(f"  Each line is the price at which the blended "
                       f"dividend of {_money(z['metric'])} would yield that.")
        else:
            out.append(f"  Both lines are that multiple times the blended "
                       f"{METRIC_LABELS.get(a.metric, a.metric)} of "
                       f"{_money(z['metric'])}.")
        out.append("")

    out.append(f"FAIR VALUE ON TODAY'S BLENDED {_money(a.blended_now)}")
    out.append("  (at the current multiple this is the price itself, by "
               "definition — that row is left out)")
    today = date.today()
    for name, mult in (("at normal multiple", a.normal_pe),
                       ("at benchmark", a.benchmark_pe)):
        fv, up = a.fair_value(mult, today), a.upside_to(mult, today)
        out.append(f"  {name:<22} {_money(fv):>12}   "
                   f"{'' if up is None else f'{up:+.1%} vs price'}")

    out += _coverage_lines(a)
    out += _scenario_lines(a)

    if a.forecast_dates:
        out += ["",
                f"FORECAST TOTAL RETURN to {a.forecast_dates[-1]} "
                f"({a.forecast_basis} basis)"]
        for name, mult in (("at normal multiple", a.normal_pe),
                           (f"at {a.benchmark_name}", a.benchmark_pe),
                           ("at current multiple", a.current_pe)):
            r = a.total_return(mult)
            if not r or r["annualised"] is None:
                continue
            out.append(f"  {name:<22} exit {_money(r['exit_price']):>10}   "
                       f"{r['annualised'] * 100:+6.1f}%/yr   "
                       f"({r['total'] * 100:+.0f}% total over "
                       f"{r['years']:.1f}y)")

    if a.warnings:
        out += ["", "WARNINGS"] + [f"  ! {w}" for w in a.warnings]
    if a.notes:
        out += ["", "NOTES"] + [f"  - {n}" for n in a.notes]
    out += ["",
            f"Source: SEC XBRL {a.source_tag or '?'}"
            + (f" / shares {a.shares_tag}" if a.shares_tag else "")
            + "; price is the Yahoo split-adjusted close."]
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    tkr = sys.argv[1] if len(sys.argv) > 1 else "MSFT"
    met = sys.argv[2] if len(sys.argv) > 2 else "eps"
    yrs = int(sys.argv[3]) if len(sys.argv) > 3 else 15
    print(analyze(tkr, metric=met, window_years=yrs).summary_text())
