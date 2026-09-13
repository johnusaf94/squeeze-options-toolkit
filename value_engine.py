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

# A price-to-metric multiple outside this range is not a valuation, it is
# a units mismatch: per-share figures filed on a different share class
# than the ticker being priced (BRK.A vs BRK.B), or an ADR ratio.
SANE_MULTIPLE = (0.5, 500.0)

# XBRL tag preference per metric, most specific first.
TAGS = {
    "eps": [
        ("us-gaap", "EarningsPerShareDiluted", "USD/shares"),
        ("us-gaap", "EarningsPerShareBasicAndDiluted", "USD/shares"),
        ("us-gaap", "IncomeLossFromContinuingOperationsPerDilutedShare", "USD/shares"),
        ("us-gaap", "EarningsPerShareBasic", "USD/shares"),
    ],
    "dividends": [
        ("us-gaap", "CommonStockDividendsPerShareDeclared", "USD/shares"),
        ("us-gaap", "CommonStockDividendsPerShareCashPaid", "USD/shares"),
    ],
    "ocf": [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities", "USD"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", "USD"),
    ],
    "revenue": [
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
        ("us-gaap", "Revenues", "USD"),
        ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax", "USD"),
        ("us-gaap", "SalesRevenueNet", "USD"),
        ("us-gaap", "SalesRevenueGoodsNet", "USD"),
    ],
    "shares": [
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),
        ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstandingBasicAndDiluted", "shares"),
        ("us-gaap", "WeightedAverageNumberOfSharesOutstandingBasic", "shares"),
    ],
    # Last resort for EPS. Companies that tag earnings per share only with
    # a share-class dimension (Berkshire) publish nothing under the plain
    # EPS concepts, but their net income and share count are both there.
    "net_income": [
        ("us-gaap", "NetIncomeLoss", "USD"),
        ("us-gaap", "ProfitLoss", "USD"),
        ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic", "USD"),
    ],
}

METRIC_LABELS = {
    "eps":       "Diluted EPS",
    "ocf":       "Operating cash flow / share",
    "dividends": "Dividends / share",
    "revenue":   "Revenue / share",
}

# Metrics already per-share in the filing; the rest are absolute dollars
# and get divided by the diluted share count.
PER_SHARE_NATIVE = {"eps", "dividends"}


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


def _best_by_period(rows, min_days: int, max_days: int) -> Dict[date, dict]:
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
        cand = {"val": val, "filed": filed, "form": form,
                "fp": x.get("fp") or "",
                "tier": 0 if form in PRIMARY_FORMS else 1}
        prev = best.get(end)
        if (prev is None
                or cand["tier"] < prev["tier"]
                or (cand["tier"] == prev["tier"]
                    and cand["filed"] > prev["filed"])):
            best[end] = cand
    return best


def _fiscal_year_ends(facts: dict) -> List[date]:
    """The company's fiscal year-end dates, learned from whichever concepts
    DO carry a full-year duration.

    `fp == "FY"` cannot be used for this: every quarter reported inside a
    10-K carries that marker, so it identifies the filing, not the period."""
    tree = facts.get("facts", {})
    ends = set()
    for tag_list in (TAGS["revenue"], TAGS["net_income"], TAGS["ocf"]):
        for taxonomy, tag, unit in tag_list:
            rows = (tree.get(taxonomy, {}).get(tag, {})
                        .get("units", {}).get(unit))
            if not rows:
                continue
            ends.update(_best_by_period(rows, ANNUAL_MIN_DAYS,
                                        ANNUAL_MAX_DAYS))
    return sorted(ends)


def _annual_from_quarters(rows, splits, per_share: bool,
                          fy_ends: List[date]) -> Dict[date, dict]:
    """Rebuild fiscal years from four quarters, for filers that never tag
    a full-year duration on this concept.

    Quarters inside one fiscal year can straddle a split, so each quarter
    is split-adjusted against its OWN filing date before the sum. The
    resulting record is then stamped with today's date, which makes the
    downstream adjustment a no-op rather than a second, wrong pass."""
    q = _best_by_period(rows, QUARTER_MIN_DAYS, QUARTER_MAX_DAYS)
    if not q or not fy_ends:
        return {}
    ends = sorted(q)
    out: Dict[date, dict] = {}
    for fy in fy_ends:
        window = [e for e in ends
                  if fy - timedelta(days=370) < e <= fy]
        if len(window) != 4:
            continue
        total = 0.0
        for e in window:
            v = q[e]["val"]
            if per_share and splits:
                v /= _split_factor_after(q[e]["filed"], splits)
            total += v
        out[fy] = {"val": total, "filed": date.today(),
                   "form": "quarterly sum", "fp": "FY", "tier": 0}
    return out


def _annual_facts(facts: dict, tag_list, splits=None,
                  per_share: bool = False) -> Tuple[Dict[date, dict], Optional[str]]:
    """Pull one annual series out of companyfacts, preferring tags in the
    order given. Returns {fiscal_end_date: {val, filed, form}} and the tag
    actually used, so the source is reportable rather than assumed."""
    tree = facts.get("facts", {})
    units = [(tag, unit, (tree.get(taxonomy, {}).get(tag, {})
                              .get("units", {}).get(unit)))
             for taxonomy, tag, unit in tag_list]
    units = [(t, u, r) for t, u, r in units if r]

    for tag, unit, rows in units:
        best = _best_by_period(rows, ANNUAL_MIN_DAYS, ANNUAL_MAX_DAYS)
        if len(best) >= 2:
            return best, f"{tag} ({unit})"
    fy_ends = _fiscal_year_ends(facts)
    for tag, unit, rows in units:
        best = _annual_from_quarters(rows, splits, per_share, fy_ends)
        if len(best) >= 2:
            return best, f"{tag} ({unit}, summed from quarters)"
    return {}, None


def _adjust_per_share(series: Dict[date, dict],
                      splits: List[Tuple[date, float]],
                      invert: bool = False) -> Dict[date, float]:
    """Restate every observation onto today's share base. `invert=True`
    for share COUNTS, which move the opposite way from per-share values."""
    out = {}
    for end, rec in series.items():
        f = _split_factor_after(rec["filed"], splits)
        out[end] = rec["val"] * f if invert else rec["val"] / f
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
    ltg: Optional[float] = None          # long-term growth, fraction
    cy_year_ago_eps: Optional[float] = None   # the base consensus grew FROM
    analysts_cy: Optional[int] = None
    analysts_ny: Optional[int] = None
    error: Optional[str] = None


def fetch_estimates(ticker: str) -> Estimates:
    e = Estimates()
    try:
        import yfinance_throttle  # noqa: F401
        import yfinance as yf
        t = yf.Ticker(ticker)
        est = t.earnings_estimate
        if est is not None and len(est):
            if "0y" in est.index:
                r = est.loc["0y"]
                e.cy_eps = _num(r.get("avg"))
                e.cy_growth = _num(r.get("growth"))
                e.cy_year_ago_eps = _num(r.get("yearAgoEps"))
                e.analysts_cy = _int(r.get("numberOfAnalysts"))
            if "+1y" in est.index:
                r = est.loc["+1y"]
                e.ny_eps = _num(r.get("avg"))
                e.ny_growth = _num(r.get("growth"))
                e.analysts_ny = _int(r.get("numberOfAnalysts"))
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


def mean(xs) -> Optional[float]:
    v = [x for x in xs if x is not None]
    return sum(v) / len(v) if v else None


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
    stale_days: int = 0             # age of the newest fiscal year in the data

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
    benchmark_pe: float = BENCHMARK_PE
    benchmark_rule: str = ""
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
        return sorted((m, n) for m, n in ((self.normal_pe, "normal"),
                                          (self.benchmark_pe, "benchmark"))
                      if m)

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
                "benchmark_line": v * self.benchmark_pe,
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
                "benchmark_line": v * self.benchmark_pe,
            })
        return rows


def _per_share_from_absolute(facts: dict, tag_list, splits):
    """Divide an absolute-dollar annual series by the diluted share count.

    The numerator is dollars and splits do not touch it; only the share
    count is restated onto today's basis."""
    raw, tag = _annual_facts(facts, tag_list, splits)
    if not raw:
        return None, None, None
    shares_raw, stag = _annual_facts(facts, TAGS["shares"], splits)
    if not shares_raw:
        return None, tag, None
    shares = _adjust_per_share(shares_raw, splits, invert=True)
    out = {end: rec["val"] / shares[end] for end, rec in raw.items()
           if shares.get(end)}
    return (out or None), tag, stag


def _metric_series(facts: dict, metric: str, splits):
    """The requested metric as an annual per-share series on today's share
    base. Returns (series, source_tag, shares_tag, error)."""
    if metric in PER_SHARE_NATIVE:
        raw, tag = _annual_facts(facts, TAGS[metric], splits, per_share=True)
        if raw:
            return _adjust_per_share(raw, splits), tag, None, None
        if metric != "eps":
            return None, None, None, (
                f"No annual {METRIC_LABELS[metric]} found in this company's "
                f"XBRL filings.")
        # EPS tagged only per share class — rebuild it from net income
        out, tag, stag = _per_share_from_absolute(
            facts, TAGS["net_income"], splits)
        if out:
            return out, f"{tag} / {stag} (EPS computed)", stag, None
        return None, None, None, (
            "No annual EPS in this company's XBRL filings, and net income "
            "or the share count is missing too — nothing to divide.")

    out, tag, stag = _per_share_from_absolute(facts, TAGS[metric], splits)
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

    adjusted, a.source_tag, a.shares_tag, err = _metric_series(
        facts, metric, splits)
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
    div_adj = _adjust_per_share(div_raw, splits) if div_raw else {}
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
        lo, hi = SANE_MULTIPLE
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
    divs = [(d, v) for d, v in zip(a.fiscal_dates, a.dividends) if v]
    if len(divs) >= 3:
        a.dividend_growth = cagr(divs[0][1], divs[-1][1],
                                 (divs[-1][0] - divs[0][0]).days / 365.25)

    # ── 7. forecast ──────────────────────────
    a.estimates = fetch_estimates(a.ticker)
    if a.stale_days <= 500:
        _build_forecast(a, forecast_years)
    else:
        return a

    # The CURRENT multiple has to sit on the same basis as the normal one,
    # or the two cannot be compared and "fair value at today's multiple"
    # stops equalling today's price. Both use the blended metric — the
    # fiscal series interpolated to a date — not a vendor's trailing EPS.
    a.blended_now = a.metric_at(date.today())
    if a.blended_now is None:
        # No forecast to blend into, so today sits past the end of the
        # series. The last actual year is the only honest stand-in.
        a.blended_now = a.fiscal_values[-1]
        a.notes.append(
            "No consensus to blend into, so the current multiple is priced "
            f"against the last actual year ({a.fiscal_dates[-1]}) rather "
            f"than against a part-elapsed one.")
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
    # third year onward: published long-term growth, else the last explicit
    # consensus year, else this company's own historical rate
    tail = e.ltg
    if tail is None:
        tail = growths[-1] if growths else a.growth_rate
    while len(growths) < years and tail is not None:
        growths.append(tail)

    if not growths:
        a.warnings.append(
            "No consensus estimates available — the chart shows history only, "
            "and every valuation figure is against trailing numbers.")
        return

    if a.forecast_basis == "absolute":
        seq = [v for v in (e.cy_eps, e.ny_eps) if v]
        if not seq:
            a.warnings.append("No absolute consensus published — falling back "
                              "to the growth-rate forecast.")
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
    _forecast_caveats(a, e, years)


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
           "",
           f"Price now                {_money(a.price_now)}",
           f"Current multiple         {_x(a.current_pe)}   "
           f"(price / blended {_money(a.blended_now)})",
           f"  vendor cross-check     {_x(a.pe_ttm)}   "
           f"(price / trailing 12m {_money(a.ttm_value)})",
           f"Normal multiple          {_x(a.normal_pe)}   "
           f"(median monthly, {a.window_years}yr window)",
           f"Benchmark multiple       {_x(a.benchmark_pe)}   {a.benchmark_rule}",
           "",
           f"Growth                   {_pct(a.growth_rate)}/yr over "
           f"{span:.0f} years ({len(a.fiscal_dates)} fiscal years)",
           f"Growth fit (R-squared)   "
           f"{'—' if a.growth_r2 is None else f'{a.growth_r2:.2f}'}",
           f"Dividend yield           {_pct(a.dividend_yield, 2)}   "
           f"payout {_pct(a.payout_ratio, 0)}   "
           f"growth {_pct(a.dividend_growth)}",
           ""]

    z = a.zone()
    if z:
        out.append("WHERE PRICE SITS TODAY")
        out.append(f"  The {a.ticker} price of {_money(z['price'])} is "
                   f"{z['band']}.")
        for ln in z["lines"]:
            side = "below" if ln["gap"] > 0 else "above"
            out.append(
                f"    {ln['name']} {ln['multiple']:.1f}× puts the line at "
                f"{_money(ln['value'])} — price is {abs(ln['gap']):.0%} "
                f"{side} it")
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

    if a.forecast_dates:
        out += ["",
                f"FORECAST TOTAL RETURN to {a.forecast_dates[-1]} "
                f"({a.forecast_basis} basis)"]
        for name, mult in (("at normal multiple", a.normal_pe),
                           ("at benchmark", a.benchmark_pe),
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
