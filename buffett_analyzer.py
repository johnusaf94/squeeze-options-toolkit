"""
buffett_analyzer.py
====================
Concrete Buffett financial analysis pipeline.
Pulls real data via yfinance, calculates metrics deterministically,
then passes a structured fact sheet to the LLM for interpretation.

Requires: pip install yfinance requests
"""

import yfinance as yf
import requests
import json
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime

# ─────────────────────────────────────────────
# BUFFETT THRESHOLDS (source: documented philosophy)
# ─────────────────────────────────────────────
ROIC_MIN            = 0.15   # > 15%
GROSS_MARGIN_MIN    = 0.40   # > 40%
DEBT_TO_EQUITY_MAX  = 0.50   # < 0.5
MARGIN_OF_SAFETY    = 0.25   # wants 25% discount to intrinsic value
DCF_HIGH_GROWTH_YEARS = 5      # stage 1: high growth period (years 1-5)
DCF_FADE_YEARS        = 5      # stage 2: linear fade from high → terminal (years 6-10)
DCF_GROWTH_YEARS      = DCF_HIGH_GROWTH_YEARS + DCF_FADE_YEARS  # 10 total
TERMINAL_GROWTH       = 0.03   # 3% perpetual growth after year 10 (~ GDP)
DISCOUNT_RATE         = 0.09   # Buffett's ~9% hurdle rate
HIGH_GROWTH_CAP       = 0.20   # cap stage-1 growth at 20% — even great companies fade

# Buffett Indicator thresholds (total market cap / GDP)
BUFFETT_IND_FAIR       = 1.00   # 100% = fairly valued
BUFFETT_IND_OVERVALUED = 1.20   # 120% = overvalued
BUFFETT_IND_EXTREME    = 1.50   # 150% = extreme bubble territory


# ─────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────
@dataclass
class MoatMetrics:
    roic:               Optional[float] = None   # Return on Invested Capital
    gross_margin:       Optional[float] = None   # Gross profit margin
    debt_to_equity:     Optional[float] = None   # D/E ratio
    net_income:         Optional[float] = None   # TTM net income
    free_cash_flow:     Optional[float] = None   # TTM FCF
    fcf_to_net_income:  Optional[float] = None   # FCF quality ratio
    roe:                Optional[float] = None   # Return on equity (supplemental)
    operating_margin:   Optional[float] = None   # Operating margin

    # ── MOAT TREND (Buffett's "moat report card") ──
    # Multi-year history of the two metrics most diagnostic of moat health.
    # A widening moat shows improving or stable ROIC + margins over time.
    # A narrowing moat shows declining values — competition is winning.
    roic_history:        list = field(default_factory=list)   # [oldest, ..., newest]
    gross_margin_history:list = field(default_factory=list)
    roic_trend:          str = ""        # graduated label e.g. "SLOWLY WIDENING"
    margin_trend:        str = ""        # graduated label e.g. "RAPIDLY NARROWING"
    roic_trend_delta:    float = 0.0     # numeric -0.5 to +0.5 from ROIC trend
    margin_trend_delta:  float = 0.0     # numeric -0.5 to +0.5 from margin trend
    moat_direction:      str = ""        # combined verdict label
    moat_trend_delta:    float = 0.0     # combined multiplier delta applied to score
    moat_trend_note:     str = ""        # human-readable explanation

@dataclass
class ValuationMetrics:
    current_price:          Optional[float] = None
    eps_ttm:                Optional[float] = None
    eps_forward:            Optional[float] = None   # NTM (next twelve months) EPS estimate
    earnings_yield:         Optional[float] = None   # 1 / trailing P/E
    forward_earnings_yield: Optional[float] = None   # NTM EPS / Price  ← PRIMARY signal
    treasury_10yr:          Optional[float] = None   # current 10yr yield
    margin_vs_treasury:     Optional[float] = None   # trailing earnings yield - treasury
    fey_vs_treasury:        Optional[float] = None   # forward earnings yield - treasury (new)
    intrinsic_value_fey:    Optional[float] = None   # Price implied at fair yield
    fey_upside_pct:         Optional[float] = None   # % upside/downside using FEY method
    fey_verdict:            str = ""                  # UNDERVALUED / FAIR / OVERVALUED
    fcf_per_share:          Optional[float] = None
    shares_outstanding:     Optional[float] = None
    dcf_intrinsic_value:    Optional[float] = None   # 10yr FCF DCF (secondary)
    dcf_upside_pct:         Optional[float] = None   # % upside/downside to DCF
    dcf_growth_assumed:     Optional[float] = None
    dcf_growth_source:      str = ""
    pe_ratio:               Optional[float] = None
    forward_pe:             Optional[float] = None
    market_cap:             Optional[float] = None

@dataclass
class BuffettIndicator:
    total_market_cap_usd:   Optional[float] = None   # Wilshire 5000 proxy
    gdp_usd:                Optional[float] = None   # US GDP (annualized)
    ratio:                  Optional[float] = None   # market cap / GDP
    signal:                 str = "UNKNOWN"          # FAIR / CAUTION / OVERVALUED / EXTREME

@dataclass
class MoatScore:
    roic_pass:          bool = False
    gross_margin_pass:  bool = False
    debt_pass:          bool = False
    fcf_quality_pass:   bool = False
    score:              int = 0          # 0-4 raw criteria passing
    adjusted_score:     float = 0.0      # 0-4 after trend multiplier — drives rating
    rating:             str = "WEAK"    # WEAK / MODERATE / STRONG (from adjusted_score)
    flags:              list = field(default_factory=list)

@dataclass
class BuffettAnalysis:
    ticker:             str = ""
    company_name:       str = ""
    sector:             str = ""
    industry:           str = ""
    analysis_date:      str = ""
    moat:               MoatMetrics = field(default_factory=MoatMetrics)
    valuation:          ValuationMetrics = field(default_factory=ValuationMetrics)
    buffett_indicator:  BuffettIndicator = field(default_factory=BuffettIndicator)
    moat_score:         MoatScore = field(default_factory=MoatScore)
    errors:             list = field(default_factory=list)


# ─────────────────────────────────────────────
# DATA FETCHERS
# ─────────────────────────────────────────────
def fetch_ticker_data(ticker: str) -> dict:
    """Pull all relevant yfinance data for a ticker."""
    t = yf.Ticker(ticker)
    info = t.info

    # Cash flow statement — annual, most recent year
    cf = t.cashflow
    fcf = None
    try:
        # FCF = Operating Cash Flow - Capital Expenditures
        op_cf = cf.loc["Operating Cash Flow"].iloc[0] if "Operating Cash Flow" in cf.index else None
        capex = cf.loc["Capital Expenditure"].iloc[0] if "Capital Expenditure" in cf.index else 0
        if op_cf is not None:
            fcf = float(op_cf) + float(capex)   # capex is negative in yfinance
    except Exception:
        fcf = info.get("freeCashflow")

    # ROIC calculation: Net Income / (Total Equity + Total Debt)
    roic = None
    try:
        bs = t.balance_sheet
        net_income = info.get("netIncomeToCommon") or info.get("netIncome")
        total_equity = bs.loc["Stockholders Equity"].iloc[0] if "Stockholders Equity" in bs.index else None
        total_debt_raw = bs.loc["Total Debt"].iloc[0] if "Total Debt" in bs.index else None
        if net_income and total_equity and total_debt_raw:
            invested_capital = float(total_equity) + float(total_debt_raw)
            if invested_capital > 0:
                roic = float(net_income) / invested_capital
    except Exception:
        pass

    # ── HISTORICAL DATA for moat trend analysis ──
    # yfinance returns up to 4 annual reports — perfect for trend slopes.
    # We extract ROIC and gross margin for each year so we can see the
    # moat report card: is it widening or narrowing?
    roic_history = []
    margin_history = []
    try:
        bs   = t.balance_sheet
        fin  = t.financials
        # Both frames have columns = report dates, newest first.
        # Iterate columns oldest → newest so the lists read chronologically.
        if not bs.empty and not fin.empty:
            common_dates = sorted(set(bs.columns) & set(fin.columns))
            for col in common_dates:
                # ROIC for this year = Net Income / (Equity + Debt)
                try:
                    ni  = float(fin.loc["Net Income", col])
                    eq  = float(bs.loc["Stockholders Equity", col]) if "Stockholders Equity" in bs.index else None
                    dbt = float(bs.loc["Total Debt", col]) if "Total Debt" in bs.index else None
                    if eq is not None and dbt is not None and (eq + dbt) > 0:
                        roic_history.append(ni / (eq + dbt))
                except Exception:
                    pass
                # Gross margin = Gross Profit / Total Revenue
                try:
                    gp  = float(fin.loc["Gross Profit", col]) if "Gross Profit" in fin.index else None
                    rev = float(fin.loc["Total Revenue", col]) if "Total Revenue" in fin.index else None
                    if gp is not None and rev is not None and rev > 0:
                        margin_history.append(gp / rev)
                except Exception:
                    pass
    except Exception:
        pass

    return {
        "info":           info,
        "fcf":            fcf,
        "roic":           roic,
        "roic_history":   roic_history,    # oldest → newest, ~4 years
        "margin_history": margin_history,
    }


def fetch_treasury_yield() -> Optional[float]:
    """
    Fetch current 10-year US Treasury yield.
    Uses ^TNX ticker from yfinance (CBOE 10yr Treasury Note Yield Index).
    """
    try:
        tnx = yf.Ticker("^TNX")
        hist = tnx.history(period="5d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1]) / 100.0   # convert % to decimal
    except Exception:
        pass
    return None


def fetch_buffett_indicator() -> BuffettIndicator:
    """
    Buffett Indicator = Total US Market Cap / US GDP
    Market cap proxy: Wilshire 5000 Total Market Index (^W5000) — represents total US market cap
    GDP: pulled from World Bank API (quarterly, annualized)
    """
    bi = BuffettIndicator()

    # Market cap via Wilshire 5000 (price * shares is embedded; index level IS the market cap proxy in trillions)
    try:
        w5000 = yf.Ticker("^W5000")
        hist = w5000.history(period="5d")
        if not hist.empty:
            # Wilshire 5000 index level ≈ total US market cap in billions (historical calibration)
            # The index was set so that 1 point ≈ $1 billion of market cap
            level = float(hist["Close"].iloc[-1])
            bi.total_market_cap_usd = level * 1e9   # convert to dollars
    except Exception:
        pass

    # GDP from World Bank (most recent annual US GDP in current USD)
    try:
        url = "https://api.worldbank.org/v2/country/US/indicator/NY.GDP.MKTP.CD?format=json&mrv=2"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data and len(data) > 1 and data[1]:
            for entry in data[1]:
                if entry.get("value"):
                    bi.gdp_usd = float(entry["value"])
                    break
    except Exception:
        # Fallback: use approximate current US GDP
        bi.gdp_usd = 29.0e12   # ~$29 trillion (2024 estimate)

    if bi.total_market_cap_usd and bi.gdp_usd:
        bi.ratio = bi.total_market_cap_usd / bi.gdp_usd
        if bi.ratio < BUFFETT_IND_FAIR:
            bi.signal = "FAIR VALUE"
        elif bi.ratio < BUFFETT_IND_OVERVALUED:
            bi.signal = "CAUTION"
        elif bi.ratio < BUFFETT_IND_EXTREME:
            bi.signal = "OVERVALUED"
        else:
            bi.signal = "EXTREME — BUBBLE TERRITORY"

    return bi


def calculate_dcf(fcf: float, shares: float, growth_rate: float) -> float:
    """
    Professional two-stage DCF with linear growth fade — what real analysts use.

    Stage 1 (years 1-5): Project FCF at the assumed high growth rate.
                         This is the period where above-trend growth is plausible.
    Stage 2 (years 6-10): Linear fade from high growth → terminal rate (3%).
                          No business sustains high growth forever; growth fades.
                          Year 6 = 80% of high rate + 20% of terminal, year 7 = 60%/40%,
                          year 8 = 40%/60%, year 9 = 20%/80%, year 10 = terminal.
    Terminal value:       Year 10 FCF growing at 3% in perpetuity, capitalized at
                          discount rate - terminal growth (Gordon growth model).
    All cash flows discounted to present value at 9% hurdle rate.

    Returns: intrinsic value per share, or None on bad inputs.

    Why this is more honest than single-stage:
      A 20% single-stage rate for 10 years implies 6.2x growth in a decade — only
      the very best businesses (Apple/Google in their primes) ever delivered that.
      The fade respects the empirical reality that competition eventually erodes
      excess returns. Every professional equity research shop models this way.
    """
    if not fcf or not shares or shares == 0:
        return None

    # Defensive: clamp the inputs to keep the model in sane territory
    high_growth  = max(min(growth_rate, HIGH_GROWTH_CAP), -0.50)
    terminal     = TERMINAL_GROWTH

    pv_fcfs = 0.0
    current_fcf = fcf

    # ── Stage 1: High growth (years 1 - DCF_HIGH_GROWTH_YEARS) ──
    for year in range(1, DCF_HIGH_GROWTH_YEARS + 1):
        current_fcf *= (1 + high_growth)
        pv_fcfs += current_fcf / ((1 + DISCOUNT_RATE) ** year)

    # ── Stage 2: Linear fade (years 6 - 10) ──
    # Each year's growth rate eases linearly from high_growth toward terminal.
    for i in range(1, DCF_FADE_YEARS + 1):
        # Fraction of the way through the fade (1/5, 2/5, ... 5/5)
        fade_frac = i / DCF_FADE_YEARS
        year_growth = high_growth + (terminal - high_growth) * fade_frac
        current_fcf *= (1 + year_growth)
        year = DCF_HIGH_GROWTH_YEARS + i
        pv_fcfs += current_fcf / ((1 + DISCOUNT_RATE) ** year)

    # ── Terminal value (Gordon growth model from year-10 FCF) ──
    if DISCOUNT_RATE <= terminal:
        # Sanity guard — would otherwise produce infinite TV
        return None
    terminal_fcf = current_fcf * (1 + terminal)
    terminal_value = terminal_fcf / (DISCOUNT_RATE - terminal)
    pv_terminal = terminal_value / ((1 + DISCOUNT_RATE) ** DCF_GROWTH_YEARS)

    total_pv = pv_fcfs + pv_terminal
    return total_pv / shares


def estimate_growth_rate(info: dict) -> tuple:
    """
    Estimate FCF growth rate for DCF. Returns (rate, source, warning).

    Priority order:
    1. Analyst 5yr EPS growth estimate (forwardEpsGrowth / earningsQuarterlyGrowth) — forward-looking
    2. Revenue growth — more stable than earnings for cyclical companies
    3. Conservative default of 5% — Buffett's baseline for quality companies

    Rules:
    - NEVER use a negative growth rate in a DCF — produces meaningless results
    - If all available data is negative, use 0% (flat) and flag it
    - Hard cap at 25% — Buffett is skeptical of high-growth assumptions
    - Always return what source was used so the display is transparent
    """
    candidates = []

    # 1. Best source: analyst forward EPS growth (5yr estimate)
    fwd = info.get("earningsQuarterlyGrowth")  # QoQ growth — proxy
    if fwd and float(fwd) > 0:
        candidates.append((float(fwd), "analyst quarterly EPS growth"))

    # 2. Revenue growth — more reliable for cyclicals than earnings
    rev = info.get("revenueGrowth")
    if rev and float(rev) > 0:
        candidates.append((float(rev), "revenue growth (YoY)"))

    # 3. Earnings growth — last resort, noisy for cyclicals
    earn = info.get("earningsGrowth")
    if earn and float(earn) > 0:
        candidates.append((float(earn), "earnings growth (YoY)"))

    if candidates:
        # Pick the most conservative positive estimate
        rate, source = min(candidates, key=lambda x: x[0])
        rate = min(rate, HIGH_GROWTH_CAP)  # cap stage-1 at 20%
        warning = None
        if rate > 0.15:
            warning = (f"High stage-1 growth assumed ({rate:.1%}) — "
                       f"fades linearly to 3% terminal by year 10. "
                       f"Even with the fade, this is an optimistic scenario.")
        return rate, source, warning

    # All available data is negative or missing
    # Check if we have any data at all to explain why
    all_data = [info.get("earningsGrowth"), info.get("revenueGrowth"), info.get("earningsQuarterlyGrowth")]
    has_negative = any(v is not None and float(v) < 0 for v in all_data if v is not None)

    if has_negative:
        return 0.0, "0% (all available growth metrics are negative — cyclical or declining earnings)",                "⚠️  Growth data is negative — DCF uses 0% growth floor. Value likely understated for cyclical companies. Do not rely on this DCF for energy/commodity stocks."
    else:
        return 0.05, "5% (conservative default — no growth data available)",                "No growth data from yfinance — using 5% conservative default"


# ─────────────────────────────────────────────
# MOAT TREND ANALYSIS — Buffett's "moat report card"
# ─────────────────────────────────────────────
def _classify_trend(history: list, healthy_level: float = None) -> tuple:
    """
    Classify a metric's trend with magnitude, GATED by absolute level.

    Returns (label, multiplier_delta) where:
      label              : graduated verdict string
      multiplier_delta   : -0.50 to +0.50 — shifts the moat score multiplier.

    THE QUALITY GATE (fix for rebound-off-low-base false positives):
    A positive slope only earns the full "widening" reward if the metric
    is at a HEALTHY ABSOLUTE LEVEL. A company whose ROIC is rising 5%→10%
    is improving, but 10% ROIC is still mediocre — that's a wounded business
    healing, not a moat compounding. Such cases are labeled "RECOVERING"
    with a dampened delta, not "WIDENING".

      healthy_level : the absolute threshold above which "rising" means
                      "moat compounding". For ROIC ~0.15 (15%), for gross
                      margin ~0.40 (40%). If None, no gate is applied
                      (preserves old behavior for un-gated callers).

    Narrowing is NOT gated — a declining moat is a warning at any level.
    We only gate the UPSIDE (the false-positive direction); a high-quality
    company whose metric is falling still earns the narrowing penalty.

    Slope is linear-regression normalized to series mean (scale-invariant).
    Bands (slope as fraction of mean per year):
       > +15%/yr  →  RAPIDLY WIDENING       +0.50   (gated)
       > +8%/yr   →  WIDENING               +0.30   (gated)
       > +2.5%/yr →  SLOWLY WIDENING        +0.12   (gated)
       |x| ≤ 2.5% →  STABLE                  0.00
       < -2.5%/yr →  SLOWLY NARROWING       -0.12
       < -8%/yr   →  NARROWING              -0.30
       < -15%/yr  →  RAPIDLY NARROWING      -0.50
    """
    if not history or len(history) < 3:
        return ("N/A", 0.0)

    n = len(history)
    xs = list(range(n))
    ys = [float(v) for v in history]

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    if mean_y == 0:
        return ("N/A", 0.0)

    numer = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return ("N/A", 0.0)

    slope = numer / denom

    # Current (most recent) level — what the metric IS now
    current_level = ys[-1]
    first_level = ys[0]

    # Normalize slope to the CURRENT level, not the series mean. Normalizing
    # to the mean inflates the signal for low-base series (a stock recovering
    # from a depressed average shows a huge normalized slope) and deflates it
    # for high-base series (an elite compounder's steady rise looks small vs
    # its high mean). Normalizing to current level fixes both.
    denom_level = abs(current_level) if abs(current_level) > 1e-6 else abs(mean_y)
    normalized = slope / denom_level if denom_level > 1e-6 else 0.0

    # ── NARROWING (downside) — never gated ──
    if normalized < -0.15:  return ("RAPIDLY NARROWING",  -0.50)
    if normalized < -0.08:  return ("NARROWING",          -0.30)
    if normalized < -0.025: return ("SLOWLY NARROWING",   -0.12)

    # ── STABLE ──
    if normalized <= 0.025:
        return ("STABLE", 0.00)

    # ── WIDENING (upside) — GATED by absolute level AND rebound detection ──
    if normalized > 0.15:
        raw_label, raw_delta = "RAPIDLY WIDENING", +0.50
    elif normalized > 0.08:
        raw_label, raw_delta = "WIDENING", +0.30
    else:
        raw_label, raw_delta = "SLOWLY WIDENING", +0.12

    # Rebound detection: if the series STARTED below the healthy level, the
    # rise is (at least partly) a recovery off a low base, not pure moat
    # expansion — even if it now ends above the line. A genuine widening moat
    # was already healthy and got better; a rebound climbed UP TO healthy.
    if healthy_level is not None:
        started_unhealthy = first_level < healthy_level
        ends_unhealthy = current_level < healthy_level

        if ends_unhealthy:
            # Still below healthy → recovering, not widening. No score bonus;
            # the business is scored on its raw criteria. The label informs;
            # the multiplier stays neutral until durability is proven.
            return ("RECOVERING (low base)", 0.0)

        if started_unhealthy:
            # Ends healthy but STARTED unhealthy = rebound that crossed the
            # line (the PYPL 7.9%→17.3% case). Honest label, but NO score
            # bonus — a recovery isn't a widening moat until it's durable.
            return ("RECOVERING → healthy", 0.0)

    # Metric was already healthy AND is rising → genuine moat widening.
    # Reward the magnitude fully here.
    return (raw_label, raw_delta)


def _compute_moat_trend(m: MoatMetrics):
    """
    Combined moat-direction verdict and numeric multiplier.

    Sets on m:
      roic_trend, margin_trend            : graduated label strings
      roic_trend_delta, margin_trend_delta : numeric -0.5 to +0.5
      moat_direction                       : combined label (derived from delta)
      moat_trend_delta                     : combined numeric multiplier (averaged)
      moat_trend_note                      : human-readable summary

    Each available metric contributes its own delta; combined delta is
    the AVERAGE of available ones so missing data doesn't bias toward zero.
    The combined LABEL is derived from the combined NUMERIC delta, so the
    displayed direction always agrees with the applied math.
    """
    # ROIC healthy threshold: 15% — the level above which "rising ROIC"
    # signals a compounding moat rather than a wounded business recovering.
    # Gross margin healthy threshold: 40% — real pricing power territory.
    # Below these, a rising metric is labeled RECOVERING (rebound off a low
    # base) and earns little-to-no widening reward — fixes the over-firing
    # where 35% of names showed "RAPIDLY WIDENING" because beaten-down stocks
    # bouncing off depressed bases looked like moat expansion.
    r_label, r_delta = _classify_trend(m.roic_history, healthy_level=0.15)
    g_label, g_delta = _classify_trend(m.gross_margin_history, healthy_level=0.40)

    m.roic_trend = r_label
    m.margin_trend = g_label
    m.roic_trend_delta = r_delta
    m.margin_trend_delta = g_delta

    if r_label == "N/A" and g_label == "N/A":
        m.moat_direction = "N/A"
        m.moat_trend_delta = 0.0
        m.moat_trend_note = "Insufficient history (yfinance returned <3 years of data)"
        return

    # Combine available deltas. Rather than a plain average (which dilutes
    # a strong signal in one metric when the other is flat), weight toward
    # the stronger-magnitude signal: 70% the dominant trend + 30% the
    # average. A sharp ROIC decline with flat margins should still register
    # as a real narrowing, not get halved into noise.
    available = [d for d, lbl in [(r_delta, r_label), (g_delta, g_label)] if lbl != "N/A"]
    if not available:
        m.moat_direction = "N/A"
        m.moat_trend_delta = 0.0
        m.moat_trend_note = "Insufficient data"
        return

    if len(available) == 1:
        combined_delta = available[0]
    else:
        avg = sum(available) / len(available)
        dominant = max(available, key=abs)
        combined_delta = 0.70 * dominant + 0.30 * avg
    m.moat_trend_delta = combined_delta

    # Derive combined label FROM the numeric delta — guarantees the
    # displayed direction matches the applied score multiplier.
    # Special case: if both available metrics are RECOVERING (rising off
    # a low base, gated), say so explicitly rather than mislabeling as
    # STABLE/SLOWLY WIDENING — the user should see it's a rebound, not a moat.
    recovering_labels = {"RECOVERING", "RECOVERING (low base)", "RECOVERING → healthy"}
    contributing = [(d, lbl) for d, lbl in [(r_delta, r_label), (g_delta, g_label)] if lbl != "N/A"]
    all_recovering = contributing and all(lbl in recovering_labels for _, lbl in contributing)
    any_recovering = any(lbl in recovering_labels for _, lbl in contributing)

    # Does any NON-recovering metric show a real (non-zero) trend?
    non_recovering_signal = any(
        lbl not in recovering_labels and abs(d) > 0.07
        for d, lbl in contributing
    )

    if all_recovering:
        m.moat_direction = "RECOVERING (off low base)"
    elif any_recovering and not non_recovering_signal:
        # A metric is recovering and nothing else shows a real trend —
        # label it as recovering (honest) even though its score delta is
        # zero. Prevents a rebound from being silently labeled STABLE.
        m.moat_direction = "RECOVERING → healthy"
    elif combined_delta > +0.40:  m.moat_direction = "RAPIDLY WIDENING"
    elif combined_delta > +0.20:  m.moat_direction = "WIDENING"
    elif combined_delta > +0.07:  m.moat_direction = "SLOWLY WIDENING"
    elif combined_delta < -0.40:  m.moat_direction = "RAPIDLY NARROWING"
    elif combined_delta < -0.20:  m.moat_direction = "NARROWING"
    elif combined_delta < -0.07:  m.moat_direction = "SLOWLY NARROWING"
    else:                         m.moat_direction = "STABLE"

    # Human-readable summary referencing actual numbers
    parts = []
    if m.roic_history and len(m.roic_history) >= 2:
        first, last = m.roic_history[0], m.roic_history[-1]
        parts.append(f"ROIC {first:.1%} → {last:.1%} ({r_label})")
    if m.gross_margin_history and len(m.gross_margin_history) >= 2:
        first, last = m.gross_margin_history[0], m.gross_margin_history[-1]
        parts.append(f"Gross margin {first:.1%} → {last:.1%} ({g_label})")
    m.moat_trend_note = "  |  ".join(parts) if parts else "Trend data computed"



def score_moat(moat: MoatMetrics) -> MoatScore:
    ms = MoatScore()
    flags = []

    # ROIC
    if moat.roic is not None:
        if moat.roic >= ROIC_MIN:
            ms.roic_pass = True
        else:
            flags.append(f"ROIC {moat.roic:.1%} is below Buffett's 15% threshold")

    # Gross Margin
    if moat.gross_margin is not None:
        if moat.gross_margin >= GROSS_MARGIN_MIN:
            ms.gross_margin_pass = True
        else:
            flags.append(f"Gross margin {moat.gross_margin:.1%} below 40% — possible commodity business")

    # Debt
    if moat.debt_to_equity is not None:
        if moat.debt_to_equity <= DEBT_TO_EQUITY_MAX:
            ms.debt_pass = True
        else:
            flags.append(f"Debt/Equity {moat.debt_to_equity:.2f} exceeds 0.5 — Buffett would flag this")

    # FCF Quality (FCF should be ≥ 70% of net income — "creative accounting" flag otherwise)
    if moat.fcf_to_net_income is not None:
        if moat.fcf_to_net_income >= 0.70:
            ms.fcf_quality_pass = True
        else:
            flags.append(
                f"FCF is only {moat.fcf_to_net_income:.0%} of net income — "
                f"Buffett suspects 'creative accounting' when FCF lags earnings"
            )
    elif moat.free_cash_flow is not None:
        # FCF exists but we couldn't compute ratio — still pass
        ms.fcf_quality_pass = True

    ms.score = sum([ms.roic_pass, ms.gross_margin_pass, ms.debt_pass, ms.fcf_quality_pass])

    # ── MOAT TREND INTEGRATION ──
    # The raw 0-4 criteria score stays clean. We compute a graduated
    # multiplier from moat.moat_trend_delta (-0.5 to +0.5) and apply it
    # to produce a trend-adjusted effective score that drives the rating.
    # Composite scoring code reads .score for the raw criteria count and
    # can apply the delta itself; we ALSO expose an adjusted-score helper
    # so downstream code doesn't have to recompute.
    trend_delta = getattr(moat, "moat_trend_delta", 0.0) or 0.0
    # Adjusted 0-4 effective score (used for rating + composite multiplier)
    adjusted = ms.score * (1.0 + trend_delta)
    # Clamp to legal 0-4 range
    adjusted = max(0.0, min(4.0, adjusted))
    ms.adjusted_score = adjusted     # exposed for composite_score.py

    # Rating uses ADJUSTED score so a strong-but-rapidly-narrowing moat
    # demotes to MODERATE/WEAK based on the magnitude of the erosion.
    if   adjusted >= 3.5: ms.rating = "STRONG"
    elif adjusted >= 1.75: ms.rating = "MODERATE"
    else:                  ms.rating = "WEAK"

    # Flag text uses graduated labels. Single source of truth — the
    # rating stays clean (just STRONG/MODERATE/WEAK); direction lives
    # in the flag, not appended to rating. This kills the duplicate-
    # text bug where searcher display appended direction a SECOND time.
    if moat.moat_direction and moat.moat_direction != "N/A":
        direction_emoji = {
            "RAPIDLY WIDENING":  "🟢🟢",
            "WIDENING":          "🟢",
            "SLOWLY WIDENING":   "🟢",
            "STABLE":            "⚪",
            "SLOWLY NARROWING":  "🟡",
            "NARROWING":         "🔴",
            "RAPIDLY NARROWING": "🔴🔴",
        }.get(moat.moat_direction, "⚪")
        flags.insert(
            0,
            f"{direction_emoji} MOAT {moat.moat_direction} "
            f"(score x{1 + trend_delta:.2f}) — {moat.moat_trend_note}"
        )

    ms.flags = flags
    return ms


# ─────────────────────────────────────────────
# MAIN ANALYSIS FUNCTION
# ─────────────────────────────────────────────
def run_buffett_analysis(ticker: str) -> BuffettAnalysis:
    """
    Full pipeline: fetch data → calculate metrics → score moat → return structured fact sheet.
    """
    analysis = BuffettAnalysis(
        ticker=ticker.upper(),
        analysis_date=datetime.now().strftime("%Y-%m-%d")
    )

    # ── FETCH ──
    try:
        raw = fetch_ticker_data(ticker)
        info = raw["info"]
        fcf_raw = raw["fcf"]
        roic_raw = raw["roic"]
    except Exception as e:
        analysis.errors.append(f"Data fetch failed: {e}")
        return analysis

    analysis.company_name = info.get("longName", ticker)
    analysis.sector       = info.get("sector", "Unknown")
    analysis.industry     = info.get("industry", "Unknown")

    # ── DATA VALIDATION GATE ──
    from data_validator import validate, format_validation_header, cannot_conclude_prompt
    dq = validate(ticker, info, "buffett")
    analysis.errors.append(f"VALIDATION:{dq.confidence}:{dq.can_analyze}:{dq.asset_type}")

    if not dq.can_analyze:
        # Store gate info so the caller can use cannot_conclude_prompt
        analysis.errors.append(f"GATE_REASON:{dq.gate_reason}")
        analysis.errors.append(f"DQ_OBJECT:blocked")
        # Still return partial analysis with what we have
        return analysis

    if dq.warnings:
        for w in dq.warnings:
            analysis.errors.append(f"WARNING:{w}")

    # ── MOAT METRICS ──
    m = analysis.moat
    m.roic          = roic_raw
    def safe_pct(val):
        if val is None: return None
        v = float(val)
        return v / 100.0 if v > 1.0 else v

    m.gross_margin     = safe_pct(info.get("grossMargins"))
    m.debt_to_equity   = info.get("debtToEquity")
    if m.debt_to_equity:
        m.debt_to_equity = m.debt_to_equity / 100.0
    m.free_cash_flow   = fcf_raw
    m.net_income       = info.get("netIncomeToCommon") or info.get("netIncome")
    m.roe              = safe_pct(info.get("returnOnEquity"))
    m.operating_margin = safe_pct(info.get("operatingMargins"))

    if m.free_cash_flow and m.net_income and m.net_income != 0:
        m.fcf_to_net_income = m.free_cash_flow / m.net_income

    # ── MOAT TREND ANALYSIS (Buffett's "moat report card") ──
    # The static moat score tells us the company IS high quality today.
    # The trend tells us whether that quality is widening or narrowing —
    # which Buffett cares about more than the snapshot. A 4/4 moat with
    # NARROWING trend is a company being eaten by competition. A 3/4 moat
    # with WIDENING trend is a future 4/4.
    m.roic_history         = raw.get("roic_history",   [])
    m.gross_margin_history = raw.get("margin_history", [])
    _compute_moat_trend(m)

    # ── VALUATION METRICS ──
    v = analysis.valuation
    v.current_price     = info.get("currentPrice") or info.get("regularMarketPrice")
    v.eps_ttm           = info.get("trailingEps")
    v.pe_ratio          = info.get("trailingPE")
    v.forward_pe        = info.get("forwardPE")
    v.market_cap        = info.get("marketCap")
    v.shares_outstanding= info.get("sharesOutstanding")

    v.eps_forward = info.get("forwardEps")

    if v.pe_ratio and v.pe_ratio > 0:
        v.earnings_yield = 1.0 / v.pe_ratio

    # ── FORWARD EARNINGS YIELD (PRIMARY intrinsic value signal) ──
    # FEY = NTM EPS / Current Price
    # Compares directly to 10yr Treasury yield to gauge relative value.
    # FEY > Treasury = stock cheaper than bonds on earnings basis
    if v.eps_forward and v.current_price and v.current_price > 0:
        v.forward_earnings_yield = v.eps_forward / v.current_price

    # 10yr Treasury
    v.treasury_10yr = fetch_treasury_yield()

    if v.earnings_yield and v.treasury_10yr:
        v.margin_vs_treasury = v.earnings_yield - v.treasury_10yr

    # Forward yield vs treasury (new primary spread)
    if v.forward_earnings_yield and v.treasury_10yr:
        v.fey_vs_treasury = v.forward_earnings_yield - v.treasury_10yr

    # ── INTRINSIC VALUE via Forward Earnings Yield ──
    # Fair value = NTM EPS / Required Yield
    # Required yield = 10yr Treasury + equity risk premium (5%)
    # This anchors intrinsic value to bond market + risk premium
    EQUITY_RISK_PREMIUM = 0.05
    if v.eps_forward and v.treasury_10yr:
        required_yield = v.treasury_10yr + EQUITY_RISK_PREMIUM
        if required_yield > 0:
            v.intrinsic_value_fey = v.eps_forward / required_yield
            if v.current_price and v.intrinsic_value_fey:
                v.fey_upside_pct = (v.intrinsic_value_fey - v.current_price) / v.current_price
                spread = v.fey_vs_treasury or 0
                if v.fey_upside_pct > 0.15:
                    v.fey_verdict = "UNDERVALUED"
                elif v.fey_upside_pct < -0.15:
                    v.fey_verdict = "OVERVALUED"
                else:
                    v.fey_verdict = "FAIR VALUE"

    # FCF per share
    if m.free_cash_flow and v.shares_outstanding and v.shares_outstanding > 0:
        v.fcf_per_share = m.free_cash_flow / v.shares_outstanding

    # DCF Intrinsic Value (secondary — FCF based)
    if m.free_cash_flow and v.shares_outstanding:
        growth, growth_source, growth_warning = estimate_growth_rate(info)
        v.dcf_growth_assumed = growth
        v.dcf_growth_source = growth_source if hasattr(v, "dcf_growth_source") else growth_source
        if growth_warning:
            analysis.errors.append(f"DCF_WARNING:{growth_warning}")
        v.dcf_intrinsic_value = calculate_dcf(m.free_cash_flow, v.shares_outstanding, growth)
        if v.dcf_intrinsic_value and v.current_price:
            v.dcf_upside_pct = (v.dcf_intrinsic_value - v.current_price) / v.current_price

    # NOTE: Buffett Indicator is now fetched once at app startup as macro context.
    # It is displayed in the session header, not per stock.

    # ── MOAT SCORE ──
    analysis.moat_score = score_moat(analysis.moat)

    return analysis


# ─────────────────────────────────────────────
# FORMAT FOR LLM — this is what the LLM receives
# ─────────────────────────────────────────────
def format_for_llm(analysis: BuffettAnalysis, portfolio_context: str = "") -> str:
    """
    Convert the analysis into a structured fact sheet for the LLM.
    The LLM's ONLY job is to interpret these numbers through Buffett's lens.
    """
    a = analysis
    m = a.moat
    v = a.valuation
    bi = a.buffett_indicator
    ms = a.moat_score

    def fmt_pct(val, decimals=1):
        if val is None: return "N/A"
        return f"{val:.{decimals}%}"

    def fmt_dollar(val):
        if val is None: return "N/A"
        if abs(val) >= 1e12: return f"${val/1e12:.2f}T"
        if abs(val) >= 1e9:  return f"${val/1e9:.2f}B"
        if abs(val) >= 1e6:  return f"${val/1e6:.2f}M"
        return f"${val:.2f}"

    def fmt_num(val, decimals=2):
        if val is None: return "N/A"
        return f"{val:.{decimals}f}"

    def pass_fail(passed: bool, val_str: str, threshold_str: str):
        symbol = "✅ PASS" if passed else "❌ FAIL"
        return f"{symbol}  |  {val_str}  (threshold: {threshold_str})"

    block = f"""
================================================================================
BUFFETT ANALYSIS FACT SHEET — {a.ticker} ({a.company_name})
Sector: {a.sector} | Industry: {a.industry} | Date: {a.analysis_date}
================================================================================

YOUR ROLE: You are Warren Buffett. Interpret the metrics below through your
documented investment philosophy. Reference the specific numbers. Be direct
and opinionated. Do NOT invent data — only reference what is provided here.
Maximum 250 words. No generic commentary — address the actual numbers.

────────────────────────────────────────────────────────────────────────────────
SECTION 1 — MOAT METRICS (Competitive Advantage Assessment)
────────────────────────────────────────────────────────────────────────────────

ROIC (Return on Invested Capital):
  {pass_fail(ms.roic_pass, fmt_pct(m.roic), "> 15%")}
  Interpretation: Every dollar invested generates {fmt_pct(m.roic)} in profit.

Gross Margin:
  {pass_fail(ms.gross_margin_pass, fmt_pct(m.gross_margin), "> 40%")}
  Interpretation: {"Pricing power evident — commodity businesses can't sustain this." if ms.gross_margin_pass else "Thin margins suggest competitive pressure or commodity-like pricing."}

Debt/Equity Ratio:
  {pass_fail(ms.debt_pass, fmt_num(m.debt_to_equity, 2), "< 0.50")}
  Interpretation: {"Conservative balance sheet — growing on own cash." if ms.debt_pass else "Relies on debt to grow — Buffett would demand explanation."}

FCF Quality (FCF as % of Net Income):
  {pass_fail(ms.fcf_quality_pass, fmt_pct(m.fcf_to_net_income) if m.fcf_to_net_income else "FCF: " + fmt_dollar(m.free_cash_flow), ">= 70%")}
  Net Income: {fmt_dollar(m.net_income)} | Free Cash Flow: {fmt_dollar(m.free_cash_flow)}
  {"FCF closely tracks earnings — accounting appears clean." if ms.fcf_quality_pass else "⚠️  FCF lags net income significantly — Buffett suspects 'creative accounting'."}

Supplemental:
  ROE: {fmt_pct(m.roe)} | Operating Margin: {fmt_pct(m.operating_margin)}

MOAT SCORE: {ms.score}/4 — {ms.rating}
MOAT TREND: {m.moat_direction or "N/A"}{f"   ({m.moat_trend_note})" if m.moat_trend_note else ""}
{"FLAGS: " + " | ".join(ms.flags) if ms.flags else "No flags."}

────────────────────────────────────────────────────────────────────────────────
SECTION 2 — FAIR PRICE / INTRINSIC VALUE
────────────────────────────────────────────────────────────────────────────────

Current Price:       ${v.current_price:.2f} (if available)
P/E (Trailing):      {fmt_num(v.pe_ratio, 1)}x
P/E (Forward):       {fmt_num(v.forward_pe, 1)}x
── FORWARD EARNINGS YIELD (Primary Intrinsic Value Signal) ──
NTM EPS Estimate:    {f"${getattr(v,'eps_forward',None):.2f}" if getattr(v,'eps_forward',None) else "N/A"}  (next twelve months consensus)
Forward Earn. Yield: {fmt_pct(getattr(v,'forward_earnings_yield',None))}  (NTM EPS / Price)
10yr Treasury Yield: {fmt_pct(v.treasury_10yr)}  (risk-free alternative)
FEY vs Treasury:     {fmt_pct(getattr(v,'fey_vs_treasury',None))} spread
FEY Intrinsic Value: {fmt_dollar(getattr(v,'intrinsic_value_fey',None)) if getattr(v,'intrinsic_value_fey',None) else "N/A"}  (NTM EPS / required yield)
FEY Upside:          {fmt_pct(getattr(v,'fey_upside_pct',None))} — {getattr(v,'fey_verdict','N/A')}
  {"✅ Forward yield BEATS treasuries — stock compensates for equity risk on NTM basis." if (getattr(v,"fey_vs_treasury",None) or 0) > 0 else "❌ Forward yield BELOW treasuries — risk not compensated on NTM basis."}

── TRAILING EARNINGS YIELD (Secondary) ──
Trailing Earn. Yield:{fmt_pct(v.earnings_yield)}  (inverse of trailing P/E)
Trailing vs Treasury:{fmt_pct(v.margin_vs_treasury)} spread

── DCF INTRINSIC VALUE (FCF-based, Secondary) ──
DCF Intrinsic Value Estimate:
  FCF/Share: {fmt_num(v.fcf_per_share, 2)}
  Growth rate assumed: {fmt_pct(v.dcf_growth_assumed)} — Source: {v.dcf_growth_source}
  Discount rate: {DISCOUNT_RATE:.0%} | Terminal growth: {TERMINAL_GROWTH:.0%}
  DCF Intrinsic Value: {fmt_dollar(v.dcf_intrinsic_value) if v.dcf_intrinsic_value else "N/A (insufficient FCF data)"}
  Upside / (Downside) to intrinsic value: {fmt_pct(v.dcf_upside_pct) if v.dcf_upside_pct else "N/A"}
  {"✅ Trading at discount — margin of safety present." if (v.dcf_upside_pct or 0) > MARGIN_OF_SAFETY else ("⚠️  Trading near intrinsic value — thin margin of safety." if (v.dcf_upside_pct or 0) > 0 else "❌ Trading above intrinsic value — no margin of safety.")}
  {"⚠️  DCF RELIABILITY NOTE: " + [e.replace("DCF_WARNING:","") for e in a.errors if "DCF_WARNING:" in e][0] if any("DCF_WARNING:" in e for e in a.errors) else ""}

{"────────────────────────────────────────────────────────────────────────────────" if portfolio_context else ""}
{"SECTION 3 — PORTFOLIO IMPACT CONTEXT" if portfolio_context else ""}
{"────────────────────────────────────────────────────────────────────────────────" if portfolio_context else ""}
{portfolio_context if portfolio_context else ""}
NOTE: The Buffett Indicator (market cap/GDP) is shown in the session header above — use it as macro context for your margin of safety assessment.

================================================================================
INSTRUCTION — Warren Buffett, answer ALL of these specifically:

1. MOAT VERDICT: Is the moat real or an illusion? Name the specific metric that proves it.
   If ROIC < 15%, say what that means in plain English for a business owner.

2. PRICE CHECK: Is this a fat pitch or a foul tip? Compare earnings yield to the 10yr treasury
   explicitly. If DCF shows negative upside, say how much overpayment that represents in dollars
   per share, not just a percentage.

3. vs JOHNATHAN'S EXISTING HOLDINGS: Compare this directly to something he already owns.
   Would you swap 10 shares of this for any of his current positions? Which one and why?

4. ALTERNATIVE SUGGESTION: If you would NOT buy this, name one specific stock you would
   prefer instead that fits a similar thesis — with one sentence on why.

5. ONE-LINE VERDICT: Buy at [price], Hold above [price], or Avoid entirely.
================================================================================
"""
    return block


# ─────────────────────────────────────────────
# DISPLAY — human-readable summary for the GUI
# ─────────────────────────────────────────────
def format_display_summary(analysis: BuffettAnalysis) -> str:
    """Compact display string for the chat window header before LLM response."""
    a = analysis
    m = a.moat
    v = a.valuation
    ms = a.moat_score
    bi = a.buffett_indicator

    def p(val, fmt=".1%"):
        return f"{val:{fmt}}" if val is not None else "N/A"

    def d(val):
        if val is None: return "N/A"
        if abs(val) >= 1e9: return f"${val/1e9:.1f}B"
        if abs(val) >= 1e6: return f"${val/1e6:.1f}M"
        return f"${val:.2f}"

    # Forward earnings yield verdict helpers
    fey_verdict = getattr(v, "fey_verdict", "") or ""
    fey_icon = {"UNDERVALUED": "✅", "FAIR VALUE": "🟡", "OVERVALUED": "❌"}.get(fey_verdict, "—")
    fey_upside = getattr(v, "fey_upside_pct", None)
    fey_val    = getattr(v, "intrinsic_value_fey", None)
    fey_spread = getattr(v, "fey_vs_treasury", None)
    eps_fwd    = getattr(v, "eps_forward", None)
    forward_ey = getattr(v, "forward_earnings_yield", None)

    lines = [
        f"  {'Metric':<30} {'Value':>12}  {'Threshold':>12}  {'Pass?':>6}",
        f"  {'─'*65}",
        f"  {'ROIC':<30} {p(m.roic):>12}  {'> 15%':>12}  {'✅' if ms.roic_pass else '❌':>6}",
        f"  {'Gross Margin':<30} {p(m.gross_margin):>12}  {'> 40%':>12}  {'✅' if ms.gross_margin_pass else '❌':>6}",
        f"  {'Debt / Equity':<30} {p(m.debt_to_equity, '.2f') if m.debt_to_equity else 'N/A':>12}  {'< 0.50':>12}  {'✅' if ms.debt_pass else '❌':>6}",
        f"  {'FCF Quality (FCF/NI)':<30} {p(m.fcf_to_net_income) if m.fcf_to_net_income else 'see below':>12}  {'> 70%':>12}  {'✅' if ms.fcf_quality_pass else '❌':>6}",
        f"  {'─'*65}",
        f"  {'MOAT SCORE':<30} {ms.score}/4 — {ms.rating}",
        f"",
        f"  ── VALUATION: FORWARD EARNINGS YIELD (Primary) ──────────────────",
        f"  {'Current Price':<30} {'$'+str(round(v.current_price,2)) if v.current_price else 'N/A':>12}",
        f"  {'NTM EPS Estimate':<30} {'$'+f'{eps_fwd:.2f}' if eps_fwd else 'N/A':>12}  (next 12mo consensus)",
        f"  {'Forward Earnings Yield':<30} {p(forward_ey):>12}  (NTM EPS / Price)",
        f"  {'10yr Treasury Yield':<30} {p(v.treasury_10yr):>12}",
        f"  {'FEY vs Treasury Spread':<30} {p(fey_spread):>12}  (>0 = stock beats bonds)",
        f"  {'FEY Intrinsic Value':<30} {d(fey_val):>12}  (NTM EPS / req. yield)",
        f"  {'Upside to FEY Intrinsic':<30} {p(fey_upside):>12}  {fey_icon} {fey_verdict}",
        f"",
        f"  ── VALUATION: DCF / TRAILING (Secondary) ────────────────────────",
        f"  {'Trailing Earnings Yield':<30} {p(v.earnings_yield):>12}  (1 / trailing P/E)",
        f"  {'Trailing Yield Spread':<30} {p(v.margin_vs_treasury):>12}",
        f"  {'DCF Intrinsic Value':<30} {d(v.dcf_intrinsic_value):>12}  (FCF-based 10yr)",
        f"  {'Upside to DCF':<30} {p(v.dcf_upside_pct):>12}",
        f"  {'DCF Growth Assumed':<30} {p(v.dcf_growth_assumed) if v.dcf_growth_assumed is not None else 'N/A':>12}",
        f"  {'Growth Rate Source':<30} {v.dcf_growth_source[:28] if v.dcf_growth_source else 'N/A':>12}",
    ]
    dcf_warnings = [e.replace("DCF_WARNING:", "") for e in (a.errors if hasattr(a, "errors") else []) if "DCF_WARNING:" in e]
    if dcf_warnings:
        lines.append(f"")
        lines.append(f"  ⚠️  DCF NOTE: {dcf_warnings[0][:80]}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "MSFT"
    print(f"\nRunning Buffett analysis on {ticker}...\n")
    result = run_buffett_analysis(ticker)
    print(format_display_summary(result))
    print()
    print("─" * 80)
    print("LLM PROMPT THAT WOULD BE SENT:")
    print("─" * 80)
    print(format_for_llm(result))
