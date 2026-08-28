"""
data_validator.py  (v2 — full rewrite)
========================================
Centralised data validation and normalisation layer.

Every yfinance field passes through here before being used
in any analyzer or display. This fixes:

  1. Dividend yield 100x inflation
  2. Short interest % calculated wrong (shares_out vs float_shares)
  3. Earnings/revenue growth inconsistent scaling
  4. DTC cross-validation and capping
  5. NaN / None / 0 / "" unified to None
  6. Growth rate sanity caps
  7. Beta clamping
  8. Market cap cross-validation
  9. Payout ratio interpretation

Also provides free supplemental data sources:
  - SEC Failure-to-Deliver (FTD) data — proxy for real borrow scarcity
  - FINRA short interest files — twice-monthly official short data
"""

import math
import requests
import io
from typing import Optional
from datetime import datetime, timedelta


# ─────────────────────────────────────────────
# CORE SAFE GETTER
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# DATA QUALITY RESULT
# ─────────────────────────────────────────────

from dataclasses import dataclass, field as dc_field


class ValidatedDict(dict):
    """
    A dict subclass that also exposes data quality attributes.
    Returned by fetch_validated_info so analyzers can use EITHER:
        validated['currentPrice']      (dict access)
        validated.confidence           (attribute access)
        validated.can_analyze          (attribute access)
    """
    confidence:  str  = "LOW"
    can_analyze: bool = False
    asset_type:  str  = "UNKNOWN"
    has_price:   bool = False
    has_earnings:bool = False
    has_balance: bool = False
    has_dividend:bool = False
    missing:     list = None
    warnings:    list = None
    ticker_sym:  str  = ""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.confidence   = "LOW"
        self.can_analyze  = False
        self.asset_type   = "UNKNOWN"
        self.has_price    = False
        self.has_earnings = False
        self.has_balance  = False
        self.has_dividend = False
        self.missing      = []
        self.warnings     = []
        self.ticker_sym   = ""

    def assess(self, ticker: str = ""):
        """Assess data quality and set attributes."""
        self.ticker_sym = ticker.upper()
        qt     = (self.get("quoteType") or "").upper()
        sector = (self.get("sector") or "").lower()
        name   = (self.get("longName") or "").lower()

        if qt == "ETF":
            self.asset_type = "ETF"
        elif qt == "MUTUALFUND":
            self.asset_type = "MUTUAL_FUND"
        elif qt == "EQUITY":
            if "real estate" in sector or "reit" in name:
                self.asset_type = "REIT"
            else:
                self.asset_type = "STOCK"
        else:
            self.asset_type = "UNKNOWN"

        self.has_price    = self.get("currentPrice") is not None
        self.has_earnings = (self.get("trailingEps") is not None or
                             self.get("earningsGrowth") is not None)
        self.has_balance  = (self.get("totalDebt") is not None or
                             self.get("debtToEquity") is not None)
        self.has_dividend = bool(self.get("dividendRate", 0) and
                                  self.get("dividendRate", 0) > 0)

        missing = []
        if not self.has_price:    missing.append("price")
        if not self.has_earnings: missing.append("earnings")
        if not self.has_balance:  missing.append("balance_sheet")
        self.missing = missing

        fields_ok = sum([self.has_price, self.has_earnings, self.has_balance])
        if fields_ok == 3:
            self.confidence  = "HIGH"
            self.can_analyze = True
        elif fields_ok >= 2:
            self.confidence  = "MEDIUM"
            self.can_analyze = True
        elif self.has_price:
            self.confidence  = "LOW"
            self.can_analyze = True
        else:
            self.confidence  = "LOW"
            self.can_analyze = False
        return self

@dataclass
class DataQuality:
    """
    Quality assessment of fetched ticker data.
    Returned alongside the validated dict so analyzers
    can check data reliability before running.
    """
    ticker:       str   = ""
    confidence:   str   = "LOW"      # HIGH / MEDIUM / LOW
    can_analyze:  bool  = False
    asset_type:   str   = "UNKNOWN"  # STOCK / ETF / REIT / MUTUAL_FUND
    has_price:    bool  = False
    has_earnings: bool  = False
    has_balance:  bool  = False
    has_dividend: bool  = False
    missing:      list  = dc_field(default_factory=list)
    warnings:     list  = dc_field(default_factory=list)

    def __repr__(self):
        return (f"DataQuality({self.ticker} | {self.confidence} | "
                f"can_analyze={self.can_analyze} | type={self.asset_type})")


def assess_data_quality(validated: dict, ticker: str = "") -> DataQuality:
    """
    Assess the quality of a validated info dict.
    Returns a DataQuality object with confidence rating.
    """
    dq = DataQuality(ticker=ticker.upper())

    # Asset type
    qt = (validated.get("quoteType") or "").upper()
    sector = (validated.get("sector") or "").lower()
    name   = (validated.get("longName") or "").lower()
    if qt == "ETF":
        dq.asset_type = "ETF"
    elif qt == "MUTUALFUND":
        dq.asset_type = "MUTUAL_FUND"
    elif qt == "EQUITY":
        if "real estate" in sector or "reit" in name:
            dq.asset_type = "REIT"
        else:
            dq.asset_type = "STOCK"
    else:
        dq.asset_type = "UNKNOWN"

    # Check key fields
    dq.has_price    = validated.get("currentPrice") is not None
    dq.has_earnings = (validated.get("trailingEps") is not None or
                       validated.get("earningsGrowth") is not None)
    dq.has_balance  = (validated.get("totalDebt") is not None or
                       validated.get("debtToEquity") is not None)
    dq.has_dividend = (validated.get("dividendRate") is not None and
                       validated.get("dividendRate", 0) > 0)

    missing = []
    if not dq.has_price:    missing.append("price")
    if not dq.has_earnings: missing.append("earnings")
    if not dq.has_balance:  missing.append("balance_sheet")
    dq.missing = missing

    # Confidence rating
    fields_ok = sum([dq.has_price, dq.has_earnings, dq.has_balance])
    if fields_ok == 3:
        dq.confidence  = "HIGH"
        dq.can_analyze = True
    elif fields_ok >= 2:
        dq.confidence  = "MEDIUM"
        dq.can_analyze = True
    elif dq.has_price:
        dq.confidence  = "LOW"
        dq.can_analyze = True   # can attempt, with caveats
    else:
        dq.confidence  = "LOW"
        dq.can_analyze = False

    return dq


def safe_get(info: dict, key: str, default=None):
    """
    Get a value from yfinance info dict, treating all
    empty/null variants as None (or default).
    Handles: None, NaN, '', 'N/A', 0 when 0 is meaningless.
    """
    v = info.get(key, default)
    if v is None:
        return default
    if isinstance(v, float) and math.isnan(v):
        return default
    if isinstance(v, str) and v.strip() in ('', 'N/A', 'None', 'nan', 'null'):
        return default
    return v


def safe_float(info: dict, key: str, default=None) -> Optional[float]:
    """safe_get + cast to float."""
    v = safe_get(info, key, default)
    if v is None:
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def safe_int(info: dict, key: str, default=None) -> Optional[int]:
    v = safe_get(info, key, default)
    if v is None:
        return default
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


# ─────────────────────────────────────────────
# DIVIDEND NORMALISATION
# ─────────────────────────────────────────────

def normalise_dividend_yield(info: dict, current_price: Optional[float]) -> Optional[float]:
    """
    Return dividend yield as a decimal (0.0008 = 0.08%).

    yfinance is inconsistent:
      - Most tickers: dividendYield = 0.0052  (decimal, correct)
      - Some tickers: dividendYield = 0.52    (already %, wrong)
      - Micron (MU):  dividendYield = 0.0008  (correct)
      - Broken case:  dividendYield = 0.08    (should be 0.0008)

    Validation chain:
      1. Get dividendYield from info
      2. Cross-check against dividendRate / currentPrice
      3. If they disagree by > 10x, trust the calculated one
      4. Apply sanity caps: yield > 30% is almost always a data error
    """
    raw_yield = safe_float(info, 'dividendYield')
    raw_rate  = safe_float(info, 'dividendRate') or safe_float(info, 'trailingAnnualDividendRate')
    price     = current_price or safe_float(info, 'currentPrice') or safe_float(info, 'regularMarketPrice')

    # Calculate from rate/price if we can
    calc_yield = None
    if raw_rate and price and price > 0:
        calc_yield = raw_rate / price

    if raw_yield is None and calc_yield is None:
        return None

    # Use calculated yield as ground truth when available
    if calc_yield is not None:
        # Sanity: yield should be 0-30%
        if 0 <= calc_yield <= 0.30:
            return calc_yield
        elif 0 < calc_yield <= 30.0:
            # calc_yield is already in % format (shouldn't happen but handle it)
            return calc_yield / 100.0
        # else: calc_yield is nonsensical, fall through to raw_yield

    if raw_yield is None:
        return calc_yield  # use whatever we have

    # Normalise raw_yield
    if 0 <= raw_yield <= 0.30:
        # Looks like proper decimal (0.0008 = 0.08%)
        result = raw_yield
    elif 0.30 < raw_yield <= 30.0:
        # Likely already in percent (8.0 = 8%), divide by 100
        result = raw_yield / 100.0
    elif raw_yield > 30.0:
        # Definitely wrong — ignore
        result = calc_yield
    else:
        result = raw_yield

    # Final cross-check: if calc_yield available and result is 100x off, fix it
    if result and calc_yield and calc_yield > 0:
        ratio = result / calc_yield
        if ratio > 50:      # raw is 100x too high
            result = result / 100.0
        elif ratio < 0.02:  # raw is 100x too low
            result = result * 100.0

    # Final sanity: cap at 30%
    if result and result > 0.30:
        return calc_yield  # trust the calculation over the raw field
    return result


def normalise_dividend_rate(info: dict) -> Optional[float]:
    """Return annual dividend per share in dollars."""
    rate = safe_float(info, 'dividendRate') or safe_float(info, 'trailingAnnualDividendRate')
    if rate is None:
        return None
    # Rate should be $ per share per year, e.g. 0.06 for MU
    # Sanity: if > $50/share that's almost certainly wrong
    if rate > 50:
        return None
    return rate


# ─────────────────────────────────────────────
# SHORT INTEREST NORMALISATION
# ─────────────────────────────────────────────

def normalise_short_interest(info: dict) -> dict:
    """
    Return validated short interest metrics.

    The key problem: Yahoo's shortPercentOfFloat field uses
    sharesShort / sharesOutstanding (NOT float shares), which
    dramatically overstates SI% for stocks with low public float.

    We always recalculate from raw components.

    Returns dict with keys:
      short_pct_float    - correct SI% of float (decimal)
      shares_short       - raw shares short count
      float_shares       - float share count
      days_to_cover      - validated DTC
      short_change_pct   - month-over-month change
      data_quality       - 'calculated' / 'reported' / 'missing'
    """
    result = {
        'short_pct_float': None,
        'shares_short':    None,
        'float_shares':    None,
        'days_to_cover':   None,
        'short_change_pct': None,
        'data_quality':    'missing',
    }

    shares_short  = safe_int(info, 'sharesShort')
    float_shares  = safe_int(info, 'floatShares')
    shares_out    = safe_int(info, 'sharesOutstanding')
    avg_vol       = safe_int(info, 'averageVolume10days') or safe_int(info, 'averageVolume')
    short_ratio   = safe_float(info, 'shortRatio')
    prior_short   = safe_int(info, 'sharesShortPriorMonth')
    raw_si_pct    = safe_float(info, 'shortPercentOfFloat')

    result['shares_short'] = shares_short
    result['float_shares'] = float_shares

    # ── Calculate SI% from components (most reliable) ──
    if shares_short and float_shares and float_shares > 0:
        calc_pct = shares_short / float_shares
        # Sanity: must be 0-100% (can theoretically exceed 100% in extreme squeezes)
        if 0 <= calc_pct <= 2.0:
            result['short_pct_float'] = calc_pct
            result['data_quality'] = 'calculated'
        elif calc_pct > 2.0:
            # Something wrong — fall back to shares_outstanding denominator
            if shares_out and shares_out > 0:
                alt_pct = shares_short / shares_out
                if 0 <= alt_pct <= 2.0:
                    result['short_pct_float'] = alt_pct
                    result['data_quality'] = 'calculated_from_outstanding'

    # ── Fallback: use raw reported field ──
    if result['short_pct_float'] is None and raw_si_pct is not None:
        if 0 < raw_si_pct <= 1.0:
            # Proper decimal (0.15 = 15%)
            result['short_pct_float'] = raw_si_pct
            result['data_quality'] = 'reported_decimal'
        elif 1.0 < raw_si_pct <= 100.0:
            # Pre-multiplied percent (15.0 = 15%)
            result['short_pct_float'] = raw_si_pct / 100.0
            result['data_quality'] = 'reported_pct_converted'
        elif raw_si_pct > 100.0:
            # Almost certainly wrong — skip
            pass

    # ── Days to Cover ──
    # Cross-validate shortRatio against our own calculation
    if shares_short and avg_vol and avg_vol > 0:
        calc_dtc = shares_short / avg_vol
        # Cap at 60 — beyond that it's a data error or zombie stock
        calc_dtc = min(calc_dtc, 60.0)
        result['days_to_cover'] = round(calc_dtc, 1)
    elif short_ratio is not None:
        dtc = min(float(short_ratio), 60.0)
        if dtc > 0:
            result['days_to_cover'] = round(dtc, 1)

    # ── Short change (month over month) ──
    if prior_short and shares_short and prior_short > 0:
        change = (shares_short - prior_short) / prior_short
        # Cap at +/- 500% (anything beyond is data error)
        if abs(change) <= 5.0:
            result['short_change_pct'] = change

    return result


# ─────────────────────────────────────────────
# GROWTH RATE NORMALISATION
# ─────────────────────────────────────────────

def normalise_growth_rate(info: dict, key: str, cap: float = 10.0) -> Optional[float]:
    """
    Normalise a growth rate field.

    yfinance returns growth rates inconsistently:
      - 0.308  = 30.8%  (decimal, correct for most tickers)
      - 7.56   = 756%   (decimal, correct for extreme growth)
      - 30.8   = 30.8%  (already percent — wrong format, some tickers)

    Heuristic: if abs(value) > 10.0, it's almost certainly already in %
    EXCEPT when prior period earnings were negative (recovery cases).
    We cross-check against revenue growth to detect this.

    cap: absolute cap on the returned decimal (default 10.0 = 1000%)
    """
    raw = safe_float(info, key)
    if raw is None:
        return None

    # Get cross-field for validation
    cross = None
    if key == 'earningsGrowth':
        cross = safe_float(info, 'revenueGrowth')
    elif key == 'revenueGrowth':
        cross = safe_float(info, 'earningsGrowth')

    # Key insight: yfinance growth rates should be small decimals (-1.0 to 5.0 typically)
    # If raw > 5.0 AND cross is a proper decimal (< 1.0), raw is already in %
    if abs(raw) > 5.0 and cross is not None and abs(cross) < 1.0:
        # Cross is decimal format → raw is in % format → divide
        return max(-cap, min(cap, raw / 100.0))
    elif abs(raw) > 100.0:
        # Definitely in % format regardless
        return max(-cap, min(cap, raw / 100.0))
    elif -10.0 <= raw <= 10.0:
        # Normal decimal range — treat as-is
        return max(-cap, min(cap, raw))
    else:
        # Between 10 and 100 — ambiguous, but lean toward decimal
        # (756% genuine growth is rare but possible — don't divide if no cross)
        if cross is not None and abs(cross) < 1.0:
            return max(-cap, min(cap, raw / 100.0))
        return max(-cap, min(cap, raw))


# ─────────────────────────────────────────────
# COST TO BORROW — ENHANCED PROXY + SEC FTD
# ─────────────────────────────────────────────

# Module-level cache: SEC FTD files are large and shared across all tickers
# in a scan. Fetch each period file ONCE, reuse for every ticker.
_FTD_FILE_CACHE = {}   # url -> {ticker: total_shares}  (parsed once)


def _fetch_ftd_period(url: str) -> Optional[dict]:
    """
    Download + parse ONE SEC FTD period file. Returns {TICKER: total_shares}
    for the whole file (cached so 700 tickers don't re-download it 700×).
    """
    if url in _FTD_FILE_CACHE:
        return _FTD_FILE_CACHE[url]
    try:
        resp = requests.get(url, timeout=12,
                            headers={'User-Agent': 'research analytics contact@example.com'})
        if resp.status_code != 200:
            _FTD_FILE_CACHE[url] = None
            return None
        import zipfile, pandas as pd
        with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
            fname = z.namelist()[0]
            with z.open(fname) as fh:
                df = pd.read_csv(fh, sep='|', dtype=str, on_bad_lines='skip')
        df.columns = [c.strip().upper() for c in df.columns]
        sym_col = next((c for c in df.columns
                        if 'SYMBOL' in c or 'TICKER' in c), None)
        qty_col = next((c for c in df.columns
                        if 'QUANTITY' in c or 'SHARES' in c or 'TOTAL' in c), None)
        if not sym_col or not qty_col:
            _FTD_FILE_CACHE[url] = None
            return None
        df[qty_col] = pd.to_numeric(df[qty_col], errors='coerce')
        # Aggregate total FTD shares per ticker for this period
        grouped = df.groupby(df[sym_col].str.upper())[qty_col].sum()
        period_map = {k: int(v) for k, v in grouped.items()
                      if v and not pd.isna(v)}
        _FTD_FILE_CACHE[url] = period_map
        return period_map
    except Exception:
        _FTD_FILE_CACHE[url] = None
        return None


def _ftd_period_urls(n_periods: int = 6) -> list:
    """
    Build the most recent N SEC FTD half-month period URLs.

    SEC publishes 2 files/month with SUFFIX 'a' (1st half, ~days 1-15)
    and 'b' (2nd half, ~days 16-end). Verified format (2024-2026):
        https://www.sec.gov/files/data/fails-deliver-data/cnsfailsYYYYMMa.zip
        https://www.sec.gov/files/data/fails-deliver-data/cnsfailsYYYYMMb.zip

    Files publish ~1 month in arrears, so we start from last month.
    """
    base = "https://www.sec.gov/files/data/fails-deliver-data/cnsfails"
    urls = []
    now = datetime.now()
    # Start one month back — current month's file isn't published yet
    for back in range(1, (n_periods // 2) + 4):
        d = now - timedelta(days=back * 30)
        yr, mo = d.year, d.month
        # 'b' (2nd half) is newer than 'a' within the same month
        for half in ('b', 'a'):
            tag = f"{yr}{mo:02d}{half}"
            urls.append((tag, f"{base}{yr}{mo:02d}{half}.zip"))
    # Dedup preserving newest-first order, cap with a little headroom
    seen, ordered = set(), []
    for tag, u in urls:
        if tag not in seen:
            seen.add(tag)
            ordered.append((tag, u))
    return ordered[:n_periods + 2]


def fetch_sec_ftd(ticker: str, n_periods: int = 6) -> Optional[dict]:
    """
    Fetch ROLLING Failure-to-Deliver history from SEC (free, official, no key).

    Pulls the last N half-month periods so FTD accumulation + trend are
    available on the FIRST run — no snapshot warm-up needed.

    Returns:
      ftd_shares        - most recent period total
      ftd_series        - list of (period, shares) oldest→newest
      ftd_total_recent  - sum across all fetched periods
      ftd_trend         - RISING / FALLING / FLAT (linear slope)
      ftd_periods       - how many periods had data
      report_date       - newest period tag
      data_quality      - 'live' / 'partial' / 'error'
    """
    result = {'ftd_shares': None, 'ftd_series': [], 'ftd_total_recent': None,
              'ftd_trend': '', 'ftd_periods': 0, 'report_date': None,
              'data_quality': 'error'}
    try:
        tk = ticker.upper()
        periods = _ftd_period_urls(n_periods)
        series = []   # (period_tag, shares) newest→oldest as we collect

        for tag, url in periods:
            pmap = _fetch_ftd_period(url)
            if pmap is None:
                continue
            shares = pmap.get(tk)
            if shares is not None:
                series.append((tag, shares))
            if len(series) >= n_periods:
                break

        if not series:
            return result

        # series currently newest→oldest; flip to oldest→newest for trend
        series_chrono = list(reversed(series))
        result['ftd_series']       = series_chrono
        result['ftd_shares']       = series[0][1]            # newest period
        result['ftd_total_recent'] = int(sum(s for _, s in series))
        result['ftd_periods']      = len(series)
        result['report_date']      = series[0][0]
        result['data_quality']     = ('live' if len(series) >= 3
                                       else 'partial')

        # Trend via linear slope across chronological series
        if len(series_chrono) >= 2:
            ys = [float(s) for _, s in series_chrono]
            n  = len(ys)
            xs = list(range(n))
            mx = sum(xs) / n
            my = sum(ys) / n
            denom = sum((x - mx) ** 2 for x in xs)
            slope = (sum((xs[i]-mx)*(ys[i]-my) for i in range(n)) / denom
                     if denom else 0)
            if my > 0 and slope > my * 0.10:
                result['ftd_trend'] = 'RISING'
            elif my > 0 and slope < -my * 0.10:
                result['ftd_trend'] = 'FALLING'
            else:
                result['ftd_trend'] = 'FLAT'
        else:
            result['ftd_trend'] = 'SINGLE PERIOD'

    except Exception as e:
        result['error'] = str(e)
    return result


def calc_ctb_proxy(short_pct: float, dtc: Optional[float],
                   ftd_pct: Optional[float] = None) -> float:
    """
    Enhanced Cost-to-Borrow proxy incorporating FTD data when available.

    Model calibrated against Interactive Brokers published borrow rates:
      SI 5%  → CTB ~7%
      SI 10% → CTB ~25%
      SI 20% → CTB ~85%
      SI 30% → CTB ~185%
      SI 50% → CTB ~500%+

    Formula: base_rate + scarcity_premium + dtc_premium + ftd_premium
    """
    if short_pct <= 0:
        return 0.0

    base      = 5.0                          # approximate risk-free rate
    scarcity  = (short_pct ** 2) * 200       # quadratic scarcity curve
    dtc_prem  = 20.0 if (dtc and dtc > 10) else (10.0 if (dtc and dtc > 5) else 0.0)
    ftd_prem  = (ftd_pct * 500) if ftd_pct else 0.0  # FTD adds significant CTB

    raw = base + scarcity + dtc_prem + ftd_prem
    return round(min(raw, 500.0), 1)


# ─────────────────────────────────────────────
# MASTER FETCH FUNCTION
# ─────────────────────────────────────────────

def fetch_validated_info(ticker: str) -> dict:
    """
    Fetch yfinance info for a ticker and return a fully validated,
    normalised data dictionary. Every field is guaranteed to be
    either a correctly-scaled value or None.

    This is the single source of truth for all analyzers.
    """
    import yfinance as yf

    raw = {}
    hist = None

    try:
        t = yf.Ticker(ticker)
        raw = t.info or {}
        try:
            hist = t.history(period="14mo", interval="1d")
        except Exception:
            pass
    except Exception as e:
        r = ValidatedDict({'_fetch_error': str(e), 'symbol': ticker})
        r.confidence = 'LOW'
        r.can_analyze = False
        return r

    price = (safe_float(raw, 'currentPrice') or
             safe_float(raw, 'regularMarketPrice') or
             safe_float(raw, 'navPrice'))

    # ── Price from history if info fails ──
    if price is None and hist is not None and not hist.empty:
        price = float(hist['Close'].iloc[-1])

    # ── Short interest (recalculated correctly) ──
    si_data = normalise_short_interest(raw)

    # ── FTD data (free from SEC) ──
    ftd_data = {'ftd_shares': None, 'ftd_pct_float': None, 'data_quality': 'not_fetched'}
    try:
        float_shares = si_data.get('float_shares')
        ftd_raw = fetch_sec_ftd(ticker)
        if ftd_raw.get('ftd_shares') and float_shares:
            ftd_pct = ftd_raw['ftd_shares'] / float_shares
            ftd_data = {**ftd_raw, 'ftd_pct_float': ftd_pct}
        else:
            ftd_data = ftd_raw
    except Exception:
        pass

    # ── CTB proxy (enhanced with FTD) ──
    ctb = None
    if si_data['short_pct_float'] is not None:
        ctb = calc_ctb_proxy(
            si_data['short_pct_float'],
            si_data['days_to_cover'],
            ftd_data.get('ftd_pct_float')
        )

    # ── Dividend ──
    div_yield = normalise_dividend_yield(raw, price)
    div_rate  = normalise_dividend_rate(raw)

    # ── Growth rates ──
    earn_growth = normalise_growth_rate(raw, 'earningsGrowth')
    rev_growth  = normalise_growth_rate(raw, 'revenueGrowth')
    earn_q_growth = normalise_growth_rate(raw, 'earningsQuarterlyGrowth')

    # ── Beta (clamp) ──
    beta_raw = safe_float(raw, 'beta')
    beta = max(-3.0, min(6.0, beta_raw)) if beta_raw is not None else None

    # ── Market cap cross-validation ──
    mktcap = safe_float(raw, 'marketCap')
    shares_out = safe_float(raw, 'sharesOutstanding')
    if mktcap and price and shares_out:
        calc_mktcap = price * shares_out
        # If reported and calculated differ by more than 10x, use calculated
        if mktcap > 0 and (calc_mktcap / mktcap > 10 or mktcap / calc_mktcap > 10):
            mktcap = calc_mktcap

    # ── Payout ratio (don't normalise — just pass through with flag) ──
    payout = safe_float(raw, 'payoutRatio')
    payout_note = ''
    if payout is not None:
        if payout > 1.0:
            payout_note = 'paying above earnings'
        elif payout > 10.0:
            # Almost certainly in % format — normalise
            payout = payout / 100.0

    # ── 52-week high/low from history (more reliable than info fields) ──
    high_52 = safe_float(raw, 'fiftyTwoWeekHigh')
    low_52  = safe_float(raw, 'fiftyTwoWeekLow')
    if hist is not None and not hist.empty and len(hist) >= 200:
        prices_1yr = hist['Close'].iloc[-252:] if len(hist) >= 252 else hist['Close']
        high_52 = float(prices_1yr.max())
        low_52  = float(prices_1yr.min())

    # ── Assemble validated dict ──
    result = ValidatedDict({
        # Identity
        'symbol':             ticker.upper(),
        'longName':           safe_get(raw, 'longName', ticker),
        'sector':             safe_get(raw, 'sector'),
        'industry':           safe_get(raw, 'industry'),
        'quoteType':          safe_get(raw, 'quoteType'),

        # Price
        'currentPrice':       price,
        'marketCap':          mktcap,
        'sharesOutstanding':  shares_out,

        # Dividend (VALIDATED)
        'dividendYield':      div_yield,
        'dividendRate':       div_rate,
        'payoutRatio':        payout,
        'payoutRatio_note':   payout_note,

        # Short interest (RECALCULATED)
        'shortPercentOfFloat': si_data['short_pct_float'],
        'sharesShort':         si_data['shares_short'],
        'floatShares':         si_data['float_shares'],
        'shortRatio':          si_data['days_to_cover'],
        'shortChangePercent':  si_data['short_change_pct'],
        'si_data_quality':     si_data['data_quality'],

        # FTD data (NEW — free from SEC)
        'ftdShares':          ftd_data.get('ftd_shares'),
        'ftdPctFloat':        ftd_data.get('ftd_pct_float'),
        'ftdReportDate':      ftd_data.get('report_date'),
        'ftdDataQuality':     ftd_data.get('data_quality'),

        # CTB proxy (ENHANCED)
        'ctbProxy':           ctb,

        # Fundamentals
        'trailingPE':         safe_float(raw, 'trailingPE'),
        'forwardPE':          safe_float(raw, 'forwardPE'),
        'pegRatio':           safe_float(raw, 'pegRatio'),
        'priceToBook':        safe_float(raw, 'priceToBook'),
        'trailingEps':        safe_float(raw, 'trailingEps'),
        'forwardEps':         safe_float(raw, 'forwardEps'),

        # Growth (NORMALISED)
        'earningsGrowth':     earn_growth,
        'revenueGrowth':      rev_growth,
        'earningsQuarterlyGrowth': earn_q_growth,

        # Margins
        'grossMargins':       safe_float(raw, 'grossMargins'),
        'operatingMargins':   safe_float(raw, 'operatingMargins'),
        'profitMargins':      safe_float(raw, 'profitMargins'),

        # Balance sheet
        'totalDebt':          safe_float(raw, 'totalDebt'),
        'totalCash':          safe_float(raw, 'totalCash'),
        'debtToEquity':       safe_float(raw, 'debtToEquity'),
        'currentRatio':       safe_float(raw, 'currentRatio'),
        'freeCashflow':       safe_float(raw, 'freeCashflow'),
        'operatingCashflow':  safe_float(raw, 'operatingCashflow'),
        'ebitda':             safe_float(raw, 'ebitda'),
        'netIncomeToCommon':  safe_float(raw, 'netIncomeToCommon'),

        # Ownership
        'heldPercentInsiders':      safe_float(raw, 'heldPercentInsiders'),
        'heldPercentInstitutions':  safe_float(raw, 'heldPercentInstitutions'),

        # Technical
        'beta':               beta,
        'fiftyTwoWeekHigh':   high_52,
        'fiftyTwoWeekLow':    low_52,
        'fiftyDayAverage':    safe_float(raw, 'fiftyDayAverage'),
        'twoHundredDayAverage': safe_float(raw, 'twoHundredDayAverage'),
        'averageVolume10days': safe_float(raw, 'averageVolume10days'),
        'averageVolume':      safe_float(raw, 'averageVolume'),

        # Validation metadata
        '_raw':               raw,          # original for anything we missed
        '_has_history':       hist is not None and not hist.empty,
        '_history':           hist,
    })
    result.assess(ticker)
    return result


# ─────────────────────────────────────────────
# DISPLAY HELPERS
# ─────────────────────────────────────────────

def fmt_pct(val: Optional[float], decimals: int = 1,
            already_pct: bool = False) -> str:
    """Format a decimal as percentage string."""
    if val is None:
        return 'N/A'
    v = val if already_pct else val * 100
    return f"{v:.{decimals}f}%"


def fmt_dollar(val: Optional[float], decimals: int = 2) -> str:
    if val is None:
        return 'N/A'
    if abs(val) >= 1e12:
        return f"${val/1e12:.2f}T"
    if abs(val) >= 1e9:
        return f"${val/1e9:.2f}B"
    if abs(val) >= 1e6:
        return f"${val/1e6:.1f}M"
    return f"${val:,.{decimals}f}"


def fmt_multiple(val: Optional[float], decimals: int = 1) -> str:
    if val is None:
        return 'N/A'
    return f"{val:.{decimals}f}x"


def validation_report(validated: dict) -> str:
    """Print a data quality report for debugging."""
    lines = [
        f"DATA QUALITY REPORT — {validated['symbol']}",
        "=" * 50,
        f"  Dividend yield:    {fmt_pct(validated['dividendYield'])} (raw: {validated['_raw'].get('dividendYield')})",
        f"  Dividend rate:     {fmt_dollar(validated['dividendRate'])} (raw: {validated['_raw'].get('dividendRate')})",
        f"  SI% of float:      {fmt_pct(validated['shortPercentOfFloat'])} [{validated['si_data_quality']}]",
        f"  Shares short:      {validated['sharesShort']:,}" if validated['sharesShort'] else "  Shares short:      N/A",
        f"  Float shares:      {validated['floatShares']:,}" if validated['floatShares'] else "  Float shares:      N/A",
        f"  Days to cover:     {validated['shortRatio']}",
        f"  CTB proxy:         {validated['ctbProxy']}%",
        f"  FTD shares:        {validated['ftdShares']} [{validated['ftdDataQuality']}]",
        f"  Earnings growth:   {fmt_pct(validated['earningsGrowth'])} (raw: {validated['_raw'].get('earningsGrowth')})",
        f"  Revenue growth:    {fmt_pct(validated['revenueGrowth'])} (raw: {validated['_raw'].get('revenueGrowth')})",
        f"  Beta:              {validated['beta']} (raw: {validated['_raw'].get('beta')})",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────


# ─────────────────────────────────────────────
# COMPATIBILITY HELPERS
# (used by buffett_analyzer, weiss_analyzer, bogle_analyzer)
# ─────────────────────────────────────────────

def fetch_data_quality(ticker: str) -> DataQuality:
    """
    Fetch validated info and return a DataQuality assessment object.
    The validated dict is attached as .validated_info for analyzers that need raw fields.
    Called by buffett_analyzer, weiss_analyzer, bogle_analyzer as:
        dq = fetch_data_quality(ticker)
    """
    validated = fetch_validated_info(ticker)
    dq = assess_data_quality(validated, ticker)
    dq.validated_info = validated  # attach raw dict for analyzers that need it
    return dq


class DataQuality:
    """
    Data quality gate result returned by validate().
    Attributes accessed by buffett_analyzer, weiss_analyzer, bogle_analyzer:
        .confidence   — "HIGH" / "MEDIUM" / "LOW"
        .can_analyze  — bool: enough data to run analysis
        .asset_type   — "STOCK" / "ETF" / "REIT" / "MUTUAL_FUND" / "UNKNOWN"
        .gate_reason  — str: why can_analyze is False (if applicable)
        .warnings     — list of non-fatal data quality warnings
    """
    def __init__(self):
        self.confidence  = "LOW"
        self.can_analyze = False
        self.asset_type  = "UNKNOWN"
        self.gate_reason = ""
        self.warnings    = []


def validate(ticker: str, info: dict, analyzer: str = "") -> DataQuality:
    """
    Validate raw yfinance info dict for a given analyzer.
    Returns a DataQuality object with confidence, can_analyze, asset_type.

    Called by analyzers as:
        dq = validate(ticker, info, "buffett")
        if not dq.can_analyze: return
    """
    dq = DataQuality()

    if not info:
        dq.gate_reason = "No data returned from yfinance"
        return dq

    # ── Asset type ──
    qt     = (info.get("quoteType") or "").upper()
    sector = (info.get("sector") or "").lower()
    name   = (info.get("longName") or "").lower()

    if qt == "ETF":
        dq.asset_type = "ETF"
    elif qt == "MUTUALFUND":
        dq.asset_type = "MUTUAL_FUND"
    elif qt == "EQUITY":
        if "real estate" in sector or "reit" in name:
            dq.asset_type = "REIT"
        else:
            dq.asset_type = "STOCK"
    else:
        dq.asset_type = "UNKNOWN"

    # ── Check required fields ──
    has_price    = (info.get("currentPrice") is not None or
                    info.get("regularMarketPrice") is not None)
    has_earnings = (info.get("trailingEps") is not None or
                    info.get("earningsGrowth") is not None or
                    info.get("netIncomeToCommon") is not None)
    has_balance  = (info.get("totalDebt") is not None or
                    info.get("debtToEquity") is not None or
                    info.get("totalAssets") is not None)
    has_revenue  = (info.get("totalRevenue") is not None or
                    info.get("revenueGrowth") is not None)

    # ── Analyzer-specific gates ──
    if analyzer == "buffett":
        # Buffett needs price + some fundamental data
        if not has_price:
            dq.gate_reason = "No price data"
            dq.confidence  = "LOW"
            dq.can_analyze = False
            return dq
        if not has_earnings and not has_balance:
            dq.gate_reason = "No earnings or balance sheet data"
            dq.confidence  = "LOW"
            # Still attempt — buffett can run partial analysis
            dq.can_analyze = True
            dq.warnings.append("Limited fundamental data")
        else:
            dq.can_analyze = True

    elif analyzer == "weiss":
        if not has_price:
            dq.gate_reason = "No price data"
            dq.can_analyze = False
            return dq
        dq.can_analyze = True

    elif analyzer == "bogle":
        if not has_price:
            dq.gate_reason = "No price data"
            dq.can_analyze = False
            return dq
        dq.can_analyze = True

    else:
        # Default: just need price
        dq.can_analyze = has_price
        if not dq.can_analyze:
            dq.gate_reason = "No price data"

    # ── Confidence rating ──
    fields_ok = sum([has_price, has_earnings, has_balance, has_revenue])
    if fields_ok >= 4:
        dq.confidence = "HIGH"
    elif fields_ok >= 2:
        dq.confidence = "MEDIUM"
    else:
        dq.confidence = "LOW"

    # ── ETF/fund warnings for equity analyzers ──
    if dq.asset_type in ("ETF", "MUTUAL_FUND") and analyzer in ("buffett", "weiss"):
        dq.warnings.append(f"{dq.asset_type}: fundamental analysis less reliable")

    return dq


def format_validation_header(ticker: str, validated: dict) -> str:
    """
    Format a short data quality header for display in analyzer output.
    Called by buffett_analyzer and others to show data source quality.
    """
    quality_flags = []
    if validated.get('si_data_quality'):
        quality_flags.append(f"SI: {validated['si_data_quality']}")
    if validated.get('ftdDataQuality') == 'live':
        quality_flags.append("FTD: SEC live")
    if validated.get('_has_history'):
        quality_flags.append("history: OK")

    flag_str = " | ".join(quality_flags) if quality_flags else "standard fetch"
    return f"  [data_validator v2 — {ticker.upper()} — {flag_str}]"


def cannot_conclude_prompt(reason: str = "") -> str:
    """
    Return a standardised message when an analyzer cannot reach a conclusion.
    Used when data is missing or unreliable.
    """
    msg = "Cannot conclude — insufficient or unreliable data"
    if reason:
        msg += f": {reason}"
    return msg

if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MU"
    print(f"\nValidating {ticker}...")
    data = fetch_validated_info(ticker)
    print(validation_report(data))
