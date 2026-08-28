"""
ticker_resolver.py  (v2 — data_validator powered)
===================================================
Resolves ticker symbols and fetches validated live data.
All raw yfinance fields pass through data_validator before use,
fixing dividend yield inflation, short interest calculation errors,
growth rate scaling issues, and other known yfinance inconsistencies.
"""

import yfinance as yf
from dataclasses import dataclass, field
from typing import Optional, Tuple
from data_validator import (
    fetch_validated_info,
    normalise_dividend_yield,
    normalise_short_interest,
    safe_float, safe_get
)


# ─────────────────────────────────────────────
# LIVE TICKER DATA STRUCTURE
# ─────────────────────────────────────────────

@dataclass
class LiveTickerData:
    # Identity
    ticker:           str = ""
    company_name:     str = ""
    sector:           str = ""
    industry:         str = ""
    quote_type:       str = ""

    # Price
    current_price:    Optional[float] = None
    market_cap:       Optional[float] = None
    shares_outstanding: Optional[float] = None

    # Dividend — validated (no 100x bug)
    dividend_yield:   Optional[float] = None   # decimal e.g. 0.0008 for 0.08%
    dividend_rate:    Optional[float] = None   # $ per share annually
    payout_ratio:     Optional[float] = None
    yield_5yr_high:   Optional[float] = None
    yield_5yr_low:    Optional[float] = None

    # Short interest — recalculated from components
    short_pct_float:  Optional[float] = None   # shares_short / float_shares
    shares_short:     Optional[int]   = None
    float_shares:     Optional[int]   = None
    days_to_cover:    Optional[float] = None
    short_change_pct: Optional[float] = None
    si_data_quality:  str = ""

    # Valuation
    pe_ratio:         Optional[float] = None
    forward_pe:       Optional[float] = None
    peg_ratio:        Optional[float] = None
    price_to_book:    Optional[float] = None
    trailing_eps:     Optional[float] = None
    forward_eps:      Optional[float] = None

    # Growth — normalised (no scaling inconsistency)
    earnings_growth:  Optional[float] = None
    revenue_growth:   Optional[float] = None

    # Margins
    gross_margin:     Optional[float] = None
    operating_margin: Optional[float] = None
    profit_margin:    Optional[float] = None

    # Balance sheet
    total_debt:       Optional[float] = None
    free_cash_flow:   Optional[float] = None
    ebitda:           Optional[float] = None
    debt_to_equity:   Optional[float] = None
    current_ratio:    Optional[float] = None

    # Ownership
    insider_ownership:      Optional[float] = None
    institutional_ownership:Optional[float] = None

    # Technical
    beta:             Optional[float] = None
    fifty_two_wk_high:Optional[float] = None
    fifty_two_wk_low: Optional[float] = None
    avg_volume:       Optional[float] = None
    pct_from_52wk_high: Optional[float] = None

    # Data quality
    fetch_errors:     list = field(default_factory=list)
    _validated:       dict = field(default_factory=dict)  # full validated dict


# ─────────────────────────────────────────────
# TICKER RESOLVER
# ─────────────────────────────────────────────

def resolve_ticker(query: str) -> Tuple[str, str, bool]:
    """
    Resolve a user query to a valid ticker symbol.
    Returns (resolved_ticker, company_name, success).
    """
    query = query.strip().upper()
    if not query:
        return "", "", False

    # Try direct lookup first
    try:
        t = yf.Ticker(query)
        info = t.info or {}
        name = info.get("longName") or info.get("shortName") or ""
        qt   = info.get("quoteType", "")
        if name and qt in ("EQUITY", "ETF", "MUTUALFUND", "CURRENCY", "INDEX"):
            return query, name, True
        if name:
            return query, name, True
    except Exception:
        pass

    # Try as company name search via yfinance search
    try:
        results = yf.Search(query, max_results=5)
        quotes = getattr(results, 'quotes', [])
        if quotes:
            best = quotes[0]
            ticker = best.get("symbol", "")
            name   = best.get("longname") or best.get("shortname") or ticker
            if ticker:
                return ticker.upper(), name, True
    except Exception:
        pass

    # Return query as-is with warning
    return query, query, True   # let the caller discover bad tickers


# ─────────────────────────────────────────────
# LIVE DATA FETCH
# ─────────────────────────────────────────────

def fetch_live_data(ticker: str) -> LiveTickerData:
    """
    Fetch validated live data for a ticker.
    All fields pass through data_validator — no raw yfinance bugs.
    """
    data = LiveTickerData(ticker=ticker.upper())

    try:
        v = fetch_validated_info(ticker)
        data._validated = v

        if v.get('_fetch_error'):
            data.fetch_errors.append(v['_fetch_error'])
            return data

        # Identity
        data.company_name   = v.get('longName', ticker) or ticker
        data.sector         = v.get('sector', 'Unknown') or 'Unknown'
        data.industry       = v.get('industry', '') or ''
        data.quote_type     = v.get('quoteType', '') or ''

        # Price
        data.current_price     = v.get('currentPrice')
        data.market_cap        = v.get('marketCap')
        data.shares_outstanding= v.get('sharesOutstanding')

        # Dividend — VALIDATED (fixes 100x bug)
        data.dividend_yield = v.get('dividendYield')   # decimal, e.g. 0.0008
        data.dividend_rate  = v.get('dividendRate')    # $ per share
        data.payout_ratio   = v.get('payoutRatio')

        # Calculate 5yr yield range from history
        data.yield_5yr_high, data.yield_5yr_low = _calc_yield_range(
            ticker, data.dividend_rate, v.get('_history')
        )

        # Short interest — RECALCULATED correctly
        data.short_pct_float  = v.get('shortPercentOfFloat')
        data.shares_short     = v.get('sharesShort')
        data.float_shares     = v.get('floatShares')
        data.days_to_cover    = v.get('shortRatio')
        data.short_change_pct = v.get('shortChangePercent')
        data.si_data_quality  = v.get('si_data_quality', '')

        # Valuation
        data.pe_ratio    = v.get('trailingPE')
        data.forward_pe  = v.get('forwardPE')
        data.peg_ratio   = v.get('pegRatio')
        data.price_to_book = v.get('priceToBook')
        data.trailing_eps  = v.get('trailingEps')
        data.forward_eps   = v.get('forwardEps')

        # Growth — NORMALISED
        data.earnings_growth = v.get('earningsGrowth')
        data.revenue_growth  = v.get('revenueGrowth')

        # Margins
        data.gross_margin    = v.get('grossMargins')
        data.operating_margin= v.get('operatingMargins')
        data.profit_margin   = v.get('profitMargins')

        # Balance sheet
        data.total_debt     = v.get('totalDebt')
        data.free_cash_flow = v.get('freeCashflow')
        data.ebitda         = v.get('ebitda')
        data.debt_to_equity = v.get('debtToEquity')
        data.current_ratio  = v.get('currentRatio')

        # Ownership
        data.insider_ownership      = v.get('heldPercentInsiders')
        data.institutional_ownership= v.get('heldPercentInstitutions')

        # Technical
        data.beta             = v.get('beta')
        data.fifty_two_wk_high= v.get('fiftyTwoWeekHigh')
        data.fifty_two_wk_low = v.get('fiftyTwoWeekLow')
        data.avg_volume       = v.get('averageVolume10days') or v.get('averageVolume')

        if data.current_price and data.fifty_two_wk_high:
            data.pct_from_52wk_high = (
                (data.current_price - data.fifty_two_wk_high) / data.fifty_two_wk_high
            )

    except Exception as e:
        data.fetch_errors.append(str(e))

    return data


def _calc_yield_range(ticker: str, dividend_rate: Optional[float],
                       hist=None) -> Tuple[Optional[float], Optional[float]]:
    """
    Calculate 5-year yield high and low from price history.
    Used by Weiss yield signal analysis.
    """
    if not dividend_rate or dividend_rate <= 0:
        return None, None
    if hist is None or hist.empty:
        return None, None

    try:
        prices = hist["Close"]
        # Use up to 5 years
        prices_5yr = prices.iloc[-min(len(prices), 252*5):]
        if len(prices_5yr) < 50:
            return None, None

        # Rolling annual yield: dividend_rate / price
        yields = dividend_rate / prices_5yr
        return float(yields.max()), float(yields.min())
    except Exception:
        return None, None


# ─────────────────────────────────────────────
# VERIFIED DATA BLOCK (for LM Studio Q&A)
# ─────────────────────────────────────────────

def format_verified_data_block(data: LiveTickerData) -> str:
    """
    Format validated data as a text block for LM Studio Q&A context.
    All values are validated — no raw yfinance bugs passed through.
    """
    def p(val, d=1):
        if val is None: return "N/A"
        return f"{val*100:.{d}f}%"

    def n(val, d=2):
        if val is None: return "N/A"
        return f"{val:.{d}f}"

    def dollars(val):
        if val is None: return "N/A"
        if abs(val) >= 1e9: return f"${val/1e9:.2f}B"
        if abs(val) >= 1e6: return f"${val/1e6:.1f}M"
        return f"${val:.2f}"

    si_note = f" [{data.si_data_quality}]" if data.si_data_quality else ""

    block = f"""
VERIFIED DATA — {data.ticker} ({data.company_name})
Sector: {data.sector} | Type: {data.quote_type}
NOTE: All fields validated and normalised. Dividend yield is decimal (0.0008 = 0.08%).

── PRICE ────────────────────────────────────────────────────────────────────────
Current Price:        ${n(data.current_price, 2)}
Market Cap:           {dollars(data.market_cap)}
52wk High:            ${n(data.fifty_two_wk_high, 2)}  ({p(data.pct_from_52wk_high)} from high)
52wk Low:             ${n(data.fifty_two_wk_low, 2)}

── DIVIDEND (VALIDATED — no 100x inflation) ─────────────────────────────────────
Pays Dividend:        {"YES" if data.dividend_rate and data.dividend_rate > 0 else "NO"}
Dividend Yield:       {p(data.dividend_yield, 2)}
Annual Dividend/sh:   ${n(data.dividend_rate, 4) if data.dividend_rate else "0.00"}
Payout Ratio:         {p(data.payout_ratio, 1) if data.payout_ratio else "N/A"}
5yr Yield High:       {p(data.yield_5yr_high, 2) if data.yield_5yr_high else "N/A"}
5yr Yield Low:        {p(data.yield_5yr_low, 2) if data.yield_5yr_low else "N/A"}

── SHORT INTEREST (RECALCULATED from float shares){si_note} ─────────────────────
Short % of Float:     {p(data.short_pct_float, 1)}
Shares Short:         {f"{data.shares_short:,}" if data.shares_short else "N/A"}
Float Shares:         {f"{data.float_shares:,}" if data.float_shares else "N/A"}
Days to Cover:        {n(data.days_to_cover, 1)}

── VALUATION ────────────────────────────────────────────────────────────────────
P/E (Trailing):       {n(data.pe_ratio, 1)}x
P/E (Forward):        {n(data.forward_pe, 1)}x
PEG Ratio:            {n(data.peg_ratio, 2)}
EPS (TTM):            ${n(data.trailing_eps, 2)}
Forward EPS:          ${n(data.forward_eps, 2)}

── GROWTH (NORMALISED — consistent decimal format) ──────────────────────────────
Earnings Growth:      {p(data.earnings_growth, 1)}
Revenue Growth:       {p(data.revenue_growth, 1)}

── FUNDAMENTALS ─────────────────────────────────────────────────────────────────
Gross Margin:         {p(data.gross_margin, 1)}
Operating Margin:     {p(data.operating_margin, 1)}
Free Cash Flow:       {dollars(data.free_cash_flow)}
Total Debt:           {dollars(data.total_debt)}
Debt/Equity:          {n(data.debt_to_equity, 2)}
Beta:                 {n(data.beta, 2)}
Insider Ownership:    {p(data.insider_ownership, 1)}
Institutional Own:    {p(data.institutional_ownership, 1)}
"""
    return block.strip()
