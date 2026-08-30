"""
squeeze_analyzers.py  (v2 — data_validator powered)
=====================================================
All data now flows through data_validator.fetch_validated_info()
which fixes:
  - Dividend yield 100x bug
  - Short interest % calculated from float (not shares outstanding)
  - Growth rate scaling inconsistencies
  - DTC cross-validated against raw components
  - CTB proxy enhanced with SEC FTD data
  - NaN/None/empty unified
"""

import yfinance as yf
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional, List
from datetime import datetime


# ─────────────────────────────────────────────
# THRESHOLDS
# ─────────────────────────────────────────────
GILL_SHORT_INTEREST_MIN  = 0.20
GILL_DTC_MIN             = 5.0
GILL_CTB_PROXY_MIN       = 10.0
GILL_PE_MAX              = 30.0
GILL_REVENUE_GROWTH_MIN  = 0.0

CHAMATH_SHORT_INTEREST_MIN = 0.15
CHAMATH_MOMENTUM_MIN       = 0.05
CHAMATH_INSIDER_THRESHOLD  = 0.05


# ─────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class SqueezeMetrics:
    ticker:                  str = ""
    company_name:            str = ""
    sector:                  str = ""

    short_interest_pct:      Optional[float] = None
    shares_short:            Optional[int]   = None
    float_shares:            Optional[int]   = None
    avg_daily_volume:        Optional[float] = None
    days_to_cover:           Optional[float] = None
    ctb_proxy:               Optional[float] = None
    si_data_quality:         str = ""

    # Days to cover by named volume window. days_to_cover above stays the
    # exchange figure; these are the same settlement measured against
    # different denominators, because the denominator is what the
    # disagreements are actually about.
    dtc_exchange:            Optional[float] = None
    dtc_robust:              Optional[float] = None   # 10-session MEDIAN
    dtc_20d:                 Optional[float] = None
    dtc_60d:                 Optional[float] = None
    dtc_preferred:           Optional[float] = None
    dtc_preferred_basis:     str = ""
    dtc_spread_low:          Optional[float] = None
    dtc_spread_high:         Optional[float] = None
    dtc_spike_ratio:         Optional[float] = None
    dtc_spike_contaminated:  bool = False

    # Trends on the settlement cadence
    si_trend_settlement:     str = ""
    si_change_settlement:    Optional[float] = None
    dtc_trend_settlement:    str = ""
    settlement_date:         str = ""
    settlement_age_days:     Optional[int] = None
    settlement_count:        int = 0
    settlement_consecutive:  int = 0
    dtc_move_is_liquidity:   bool = False
    settlement_vol_change:   Optional[float] = None

    # FTD data
    ftd_shares:              Optional[int]   = None
    ftd_pct_float:           Optional[float] = None
    ftd_report_date:         str = ""

    # Effective float — reported float less locked institutional stock.
    # Reported alongside float, never in place of it: the scoring pillars
    # below all still read float_shares, so history stays comparable.
    effective_float:         Optional[float] = None
    effective_float_quality: str = ""
    float_tightness:         Optional[float] = None   # float / effective float
    ftd_pct_eff_float:       Optional[float] = None
    # 13F institutional shares as a multiple of float. >1.0 is only reachable
    # through share lending — the lender and the short's counterparty both
    # report the same share.
    institutional_shares_over_float: Optional[float] = None
    # True when 13F holdings exceeded the float and effective float fell back
    # to a fixed fraction — carries no name-specific information once set.
    inst_capped:             bool = False

    current_price:           Optional[float] = None
    price_change_1m:         Optional[float] = None
    price_change_3m:         Optional[float] = None
    rsi_14:                  Optional[float] = None
    volume_surge:            Optional[float] = None

    pe_ratio:                Optional[float] = None
    revenue_growth:          Optional[float] = None
    free_cash_flow:          Optional[float] = None
    debt_to_equity:          Optional[float] = None
    market_cap:              Optional[float] = None

    insider_ownership:       Optional[float] = None
    institutional_ownership: Optional[float] = None
    short_change_pct:        Optional[float] = None

    fetch_errors:            list = field(default_factory=list)

    # Liveness. `alive` False means the tape has no liquidity to enter or
    # exit and every metric below is arithmetic on a corpse — the scanner
    # should drop the name, not rank it. `zombie` means it trades but
    # days-to-cover has pinned the 60 cap (0 winners in 21 graded episodes).
    alive:                   bool = True
    zombie:                  bool = False
    liveness_reasons:        list = field(default_factory=list)
    # STOCK / REIT / ETF / MUTUAL_FUND / UNKNOWN. data_validator has always
    # classified this and the squeeze path has never read it, so SPY and TQQQ
    # ran the full analysis — float, insider ownership and earnings timing all
    # absent — and scored 58, mid-pack among real candidates.
    asset_type:              str = "UNKNOWN"
    price_source:            str = ""
    price_stale_ratio:       Optional[float] = None

    # Borrow availability — the squeeze precondition. borrow_available
    # False means no source is configured, NOT that shares are unavailable.
    borrow_available:        bool = False
    shares_available:        Optional[float] = None
    borrow_utilization:      Optional[float] = None
    borrow_avail_pct_float:  Optional[float] = None
    borrow_rate_real:        Optional[float] = None
    borrow_state:            str = ""
    borrow_near_zero:        bool = False
    borrow_draining:         bool = False

    # Reg SHO threshold list — the official determination, not our estimate
    reg_sho_on_list:         bool = False
    reg_sho_days:            int = 0
    reg_sho_mandatory:       bool = False
    reg_sho_days_to_mandatory: Optional[int] = None
    reg_sho_available:       bool = False

    # Months of cash before a raise is forced. inf = cash-generative.
    cash_runway_months:      Optional[float] = None
    cash_burn_annual:        Optional[float] = None


@dataclass
class GillAnalysis:
    ticker:               str = ""
    metrics:              SqueezeMetrics = field(default_factory=SqueezeMetrics)
    squeeze_setup_score:  float = 0.0
    fundamental_score:    float = 0.0
    catalyst_score:       float = 0.0
    total_score:          float = 0.0
    verdict:              str = ""
    conviction:           str = ""
    thesis:               str = ""
    red_flags:            list = field(default_factory=list)
    green_flags:          list = field(default_factory=list)


@dataclass
class ChamathAnalysis:
    ticker:                  str = ""
    metrics:                 SqueezeMetrics = field(default_factory=SqueezeMetrics)
    macro_setup_score:       float = 0.0
    squeeze_pressure_score:  float = 0.0
    catalyst_momentum_score: float = 0.0
    total_score:             float = 0.0
    verdict:                 str = ""
    narrative:               str = ""
    thesis:                  str = ""
    red_flags:               list = field(default_factory=list)
    green_flags:             list = field(default_factory=list)


# ─────────────────────────────────────────────
# SHARED DATA FETCHER — uses data_validator
# ─────────────────────────────────────────────

def fetch_squeeze_metrics(ticker: str, enrich: bool = True) -> SqueezeMetrics:
    """
    Fetch all squeeze-relevant data using data_validator for accuracy.
    All fields are validated and normalised before use.

    enrich=False runs the cheap pass: yfinance info and history only, no SEC
    fails, no FINRA settlements, no Reg SHO list, no effective float. Costs
    about an eighth as much and carries everything the SI/DTC pre-filter
    needs. See fetch_validated_info for the measurements.
    """
    from data_validator import fetch_validated_info

    m = SqueezeMetrics(ticker=ticker.upper())

    try:
        v = fetch_validated_info(ticker, enrich=enrich)

        if v.get('_fetch_error'):
            m.fetch_errors.append(v['_fetch_error'])
            return m

        m.asset_type        = getattr(v, 'asset_type', 'UNKNOWN') or 'UNKNOWN'
        m.alive             = bool(getattr(v, 'alive', True))
        m.zombie            = bool(getattr(v, 'zombie', False))
        m.liveness_reasons  = list(getattr(v, 'liveness_reasons', []) or [])
        m.price_source      = v.get('priceSource', '') or ''
        m.price_stale_ratio = v.get('priceStaleRatio')
        m.borrow_available       = bool(v.get('borrowAvailable'))
        m.shares_available       = v.get('sharesAvailable')
        m.borrow_utilization     = v.get('borrowUtilization')
        m.borrow_avail_pct_float = v.get('borrowAvailPctFloat')
        m.borrow_rate_real       = v.get('borrowRateReal')
        m.borrow_state           = v.get('borrowState', '') or ''
        m.borrow_near_zero       = bool(v.get('borrowNearZero'))
        m.borrow_draining        = bool(v.get('borrowDraining'))
        m.reg_sho_on_list   = bool(v.get('regShoOnList'))
        m.reg_sho_days      = v.get('regShoDays', 0) or 0
        m.reg_sho_mandatory = bool(v.get('regShoMandatory'))
        m.reg_sho_days_to_mandatory = v.get('regShoDaysToMandatory')
        m.reg_sho_available = bool(v.get('regShoAvailable'))
        m.cash_runway_months = v.get('cashRunwayMonths')
        m.cash_burn_annual   = v.get('cashBurnAnnual')
        if not m.alive:
            m.fetch_errors.append(
                "not tradeable: " + "; ".join(m.liveness_reasons))

        m.company_name           = v.get('longName', ticker)
        m.sector                 = v.get('sector', 'Unknown') or 'Unknown'
        m.current_price          = v.get('currentPrice')
        m.market_cap             = v.get('marketCap')
        m.pe_ratio               = v.get('trailingPE')
        m.revenue_growth         = v.get('revenueGrowth')   # already normalised
        m.free_cash_flow         = v.get('freeCashflow')
        m.debt_to_equity         = v.get('debtToEquity')
        m.insider_ownership      = v.get('heldPercentInsiders')
        m.institutional_ownership= v.get('heldPercentInstitutions')

        # Short interest — recalculated correctly by data_validator
        m.short_interest_pct     = v.get('shortPercentOfFloat')
        m.shares_short           = v.get('sharesShort')
        m.float_shares           = v.get('floatShares')
        m.days_to_cover          = v.get('shortRatio')
        m.short_change_pct       = v.get('shortChangePercent')
        m.si_data_quality        = v.get('si_data_quality', '')

        # DTC family + settlement-cadence trends
        m.dtc_exchange           = v.get('dtcExchange')
        m.dtc_robust             = v.get('dtcRobust')
        m.dtc_20d                = v.get('dtc20d')
        m.dtc_60d                = v.get('dtc60d')
        m.dtc_preferred          = v.get('dtcPreferred')
        m.dtc_preferred_basis    = v.get('dtcPreferredBasis', '') or ''
        m.dtc_spread_low         = v.get('dtcSpreadLow')
        m.dtc_spread_high        = v.get('dtcSpreadHigh')
        m.dtc_spike_ratio        = v.get('dtcSpikeRatio')
        m.dtc_spike_contaminated = bool(v.get('dtcSpikeContaminated'))
        m.si_trend_settlement    = v.get('siTrendSettlement', '') or ''
        m.si_change_settlement   = v.get('siChangeSettlement')
        m.dtc_trend_settlement   = v.get('dtcTrendSettlement', '') or ''
        m.settlement_date        = v.get('settlementDate', '') or ''
        m.settlement_age_days    = v.get('settlementAgeDays')
        m.settlement_count       = v.get('settlementCount', 0) or 0
        m.settlement_consecutive = v.get('settlementConsecutive', 0) or 0
        m.dtc_move_is_liquidity  = bool(v.get('dtcMoveIsLiquidity'))
        m.settlement_vol_change  = v.get('settlementVolChange')
        m.avg_daily_volume       = v.get('averageVolume10days') or v.get('averageVolume')

        # FTD data from SEC
        m.ftd_shares             = v.get('ftdShares')
        m.ftd_pct_float          = v.get('ftdPctFloat')
        m.ftd_report_date        = v.get('ftdReportDate', '')

        # Effective float
        m.effective_float         = v.get('effectiveFloat')
        m.effective_float_quality = v.get('effectiveFloatQuality', '') or ''
        m.float_tightness         = v.get('floatTightness')
        m.ftd_pct_eff_float       = v.get('ftdPctEffFloat')
        m.institutional_shares_over_float = v.get('instSharesOverFloat')
        m.inst_capped            = bool(v.get('instCapped'))

        # CTB proxy (enhanced with FTD)
        m.ctb_proxy              = v.get('ctbProxy')

        # Price action from history
        hist = v.get('_history')
        if hist is not None and not hist.empty:
            prices  = hist["Close"]
            volumes = hist["Volume"]
            n = len(prices)

            if n >= 21:
                m.price_change_1m = float((prices.iloc[-1] / prices.iloc[-21]) - 1)
            if n >= 65:
                m.price_change_3m = float((prices.iloc[-1] / prices.iloc[-65]) - 1)

            # RSI-14
            delta = prices.diff()
            gain  = delta.clip(lower=0).rolling(14).mean()
            loss  = (-delta.clip(upper=0)).rolling(14).mean()
            rs    = gain / loss
            rsi   = 100 - (100 / (1 + rs))
            if not rsi.empty:
                m.rsi_14 = float(rsi.iloc[-1])

            # Volume surge: recent 5d vs 20d average
            if n >= 20:
                recent_vol = float(volumes.iloc[-5:].mean())
                avg_vol    = float(volumes.iloc[-20:].mean())
                if avg_vol > 0:
                    m.volume_surge = recent_vol / avg_vol

    except Exception as e:
        m.fetch_errors.append(str(e))

    return m


# ─────────────────────────────────────────────
# KEITH GILL ANALYZER
# ─────────────────────────────────────────────

def run_gill_analysis(ticker: str) -> GillAnalysis:
    g = GillAnalysis(ticker=ticker.upper())
    g.metrics = fetch_squeeze_metrics(ticker)
    m = g.metrics
    green, red = [], []

    # ── PILLAR 1: Squeeze Setup (40 pts) ──
    squeeze = 0.0
    si = m.short_interest_pct

    if si is not None:
        q = f"[{m.si_data_quality}]" if m.si_data_quality else ""
        if si >= 0.50:
            squeeze += 16
            green.append(f"Short interest {si:.1%} {q} — EXTREME. Wall Street is ALL IN on the short.")
        elif si >= 0.30:
            squeeze += 12
            green.append(f"Short interest {si:.1%} {q} — very high. Significant forced-covering risk.")
        elif si >= GILL_SHORT_INTEREST_MIN:
            squeeze += 8
            green.append(f"Short interest {si:.1%} {q} — above Gill's 20% threshold.")
        elif si >= 0.10:
            squeeze += 3
            red.append(f"Short interest {si:.1%} {q} — below 20% threshold. Mild pressure only.")
        else:
            red.append(f"Short interest {si:.1%} {q} — very low. No meaningful squeeze setup.")
    else:
        red.append("Short interest data unavailable.")

    dtc = m.days_to_cover
    # ── Volume-surge distortion guard (F9) ──
    # DTC = shares short / avg volume. A volume SURGE (rewarded below as a
    # crowd-forming catalyst) mechanically CRUSHES DTC — the same event
    # was earning points in pillar 3 and losing them here. When volume is
    # surging ≥2x, evaluate DTC bands on a partially-restored value so the
    # trap measurement reflects normal-volume conditions.
    vs_guard = m.volume_surge
    dtc_eval = dtc
    if dtc is not None and vs_guard is not None and vs_guard >= 2.0:
        dtc_eval = dtc * min(vs_guard, 3.0) / 1.5
    if dtc is not None:
        if dtc_eval != dtc:
            green.append(f"DTC {dtc:.1f}d evaluated as {dtc_eval:.1f}d "
                         f"(volume surge {vs_guard:.1f}x deflates raw DTC)")
        if dtc_eval >= 20:
            squeeze += 13
            green.append(f"DTC {dtc:.1f} days — CRITICAL. Shorts are completely trapped.")
        elif dtc_eval >= 10:
            squeeze += 10
            green.append(f"DTC {dtc:.1f} days — very dangerous for shorts.")
        elif dtc_eval >= GILL_DTC_MIN:
            squeeze += 6
            green.append(f"DTC {dtc:.1f} days — above 5-day threshold. Exit door is narrow.")
        elif dtc_eval >= 2:
            squeeze += 2
            red.append(f"DTC {dtc:.1f} days — shorts can exit relatively easily.")
        else:
            red.append(f"DTC {dtc:.1f} days — shorts can cover quickly. No trap.")
    else:
        red.append("Days to cover unavailable.")

    ctb = m.ctb_proxy
    if ctb is not None:
        if ctb >= 50:
            squeeze += 7
            green.append(f"CTB proxy {ctb:.0f}% — borrow is extremely scarce.")
        elif ctb >= GILL_CTB_PROXY_MIN:
            squeeze += 4
            green.append(f"CTB proxy {ctb:.0f}% — meaningful borrow cost.")
        else:
            squeeze += 1
            red.append(f"CTB proxy {ctb:.0f}% — borrow appears available.")

    # FTD bonus — real borrow scarcity signal
    if m.ftd_pct_float and m.ftd_pct_float > 0.001:
        ftd_pct = m.ftd_pct_float
        if ftd_pct > 0.02:
            squeeze += 4
            green.append(f"FTD {ftd_pct:.2%} of float [{m.ftd_report_date}] — confirmed borrow scarcity.")
        elif ftd_pct > 0.005:
            squeeze += 2
            green.append(f"FTD {ftd_pct:.2%} of float — elevated failures to deliver.")

    # Max now genuinely 40: SI 16 + DTC 13 + CTB 7 + FTD 4 = 40.
    # (Was capped at 44 vs a documented 40 — Gill totals reached 104 while
    # Chamath capped at 100, structurally skewing `combined` toward
    # Gill-heavy high-SI/DTC names.)
    g.squeeze_setup_score = min(squeeze, 40)

    # ── PILLAR 2: Fundamental Quality (35 pts) ──
    fund = 0.0
    pe = m.pe_ratio
    if pe is not None and pe > 0:
        if pe <= 10:
            fund += 10
            green.append(f"P/E {pe:.1f}x — deeply cheap. Shorts may be wrong on valuation.")
        elif pe <= 20:
            fund += 7
            green.append(f"P/E {pe:.1f}x — reasonable valuation.")
        elif pe <= GILL_PE_MAX:
            fund += 4
            red.append(f"P/E {pe:.1f}x — fair to slightly elevated.")
        else:
            red.append(f"P/E {pe:.1f}x — expensive. Shorts may have valuation right.")
    elif pe is None:
        # No charity points for missing data (was +3, which made an
        # unprofitable company outscore an expensive profitable one in
        # a pillar literally named "fundamental quality").
        red.append("P/E unavailable — may be unprofitable.")

    rev = m.revenue_growth
    if rev is not None:
        if rev >= 0.20:
            fund += 10
            green.append(f"Revenue growing {rev:.0%} — company growing into the story.")
        elif rev >= GILL_REVENUE_GROWTH_MIN:
            fund += 6
            green.append(f"Revenue growth {rev:.0%} — positive trajectory.")
        else:
            fund += 1
            red.append(f"Revenue declining {rev:.0%} — shorts may have fundamental thesis right.")
    else:
        red.append("Revenue growth data unavailable.")

    fcf = m.free_cash_flow
    if fcf is not None:
        if fcf > 0:
            fund += 8
            green.append(f"Positive FCF ${fcf/1e6:.0f}M — real business, not burning cash.")
        else:
            fund += 1
            red.append(f"Negative FCF ${fcf/1e6:.0f}M — cash burn is a concern.")

    dte = m.debt_to_equity
    if dte is not None:
        if dte < 0.5:
            fund += 7
            green.append(f"Debt/equity {dte:.2f} — clean balance sheet.")
        elif dte < 1.5:
            fund += 4
            green.append(f"Debt/equity {dte:.2f} — manageable.")
        else:
            red.append(f"Debt/equity {dte:.2f} — heavy debt limits runway.")

    g.fundamental_score = min(fund, 35)

    # ── PILLAR 3: Catalyst (25 pts) ──
    cat = 0.0
    vs = m.volume_surge
    if vs is not None:
        if vs >= 3.0:
            cat += 12
            green.append(f"Volume surge {vs:.1f}x normal — someone is loading up.")
        elif vs >= 2.0:
            cat += 8
            green.append(f"Volume surge {vs:.1f}x normal — elevated interest.")
        elif vs >= 1.3:
            cat += 4
            green.append(f"Volume slightly elevated ({vs:.1f}x) — early signal.")
        else:
            red.append(f"Volume normal ({vs:.1f}x) — no crowd forming yet.")

    sc = m.short_change_pct
    if sc is not None:
        if sc > 0.10:
            cat += 7
            green.append(f"Short interest grew {sc:.0%} last month — shorts adding conviction. FUEL for squeeze.")
        elif sc < -0.10:
            # Shorts covering = squeeze fuel LEAVING. Zero, not charity (was +2).
            red.append(f"Short interest decreased {sc:.0%} — shorts already covering. Fuel draining.")
        else:
            cat += 4
            green.append("Short interest stable — no capitulation yet.")

    rsi = m.rsi_14
    if rsi is not None:
        # Coil beats knife: 40-60 = consolidating with shorts trapped (best
        # pre-ignition state). Deep oversold (<30) means the stock is in
        # freefall — shorts are WINNING and averaging down profitably; that
        # earned the same +6 as a healthy coil and juiced falling knives
        # (the CRKN class). Now graduated.
        if 40 <= rsi <= 60:
            cat += 6
            green.append(f"RSI {rsi:.0f} — coiled, not overbought. Ideal pre-ignition state.")
        elif 30 <= rsi < 40:
            cat += 4
            green.append(f"RSI {rsi:.0f} — washed out but stabilizing.")
        elif rsi < 30:
            cat += 2
            red.append(f"RSI {rsi:.0f} — freefall. Shorts are winning; knife risk.")
        elif rsi <= 75:
            cat += 3
            red.append(f"RSI {rsi:.0f} — momentum building but watch for overextension.")
        else:
            red.append(f"RSI {rsi:.0f} — OVERBOUGHT. Squeeze may have begun or nearly over.")

    g.catalyst_score = min(cat, 25)

    g.total_score = g.squeeze_setup_score + g.fundamental_score + g.catalyst_score
    g.green_flags = green
    g.red_flags   = red

    if g.total_score >= 75:
        g.verdict, g.conviction = "SQUEEZE CANDIDATE", "YOLO"
    elif g.total_score >= 55:
        g.verdict, g.conviction = "SQUEEZE CANDIDATE", "HIGH"
    elif g.total_score >= 38:
        g.verdict, g.conviction = "WATCH", "MODERATE"
    else:
        g.verdict, g.conviction = "PASS", "LOW"

    si_str  = f"{si:.1%}" if si else "unknown"
    dtc_str = f"{dtc:.1f}" if dtc else "unknown"
    g.thesis = (
        f"{ticker.upper()} has {si_str} of float short with {dtc_str} days to cover. "
        f"Squeeze: {g.squeeze_setup_score:.0f}/44 | "
        f"Fundamental: {g.fundamental_score:.0f}/35 | "
        f"Catalyst: {g.catalyst_score:.0f}/25. "
        f"Verdict: {g.verdict} ({g.conviction} conviction)."
    )
    return g


# ─────────────────────────────────────────────
# CHAMATH ANALYZER
# ─────────────────────────────────────────────

def run_chamath_analysis(ticker: str) -> ChamathAnalysis:
    c = ChamathAnalysis(ticker=ticker.upper())
    c.metrics = fetch_squeeze_metrics(ticker)
    m = c.metrics
    green, red = [], []

    # ── PILLAR 1: Macro Setup (30 pts) ──
    macro = 0.0
    mc = m.market_cap
    if mc is not None:
        if 500e6 <= mc <= 10e9:
            macro += 12
            green.append(f"Market cap ${mc/1e9:.1f}B — ideal squeeze size.")
        elif 10e9 < mc <= 50e9:
            macro += 7
            green.append(f"Market cap ${mc/1e9:.1f}B — larger cap, needs bigger catalyst.")
        elif mc < 500e6:
            macro += 4
            red.append(f"Market cap ${mc/1e6:.0f}M — micro cap, limited liquidity.")
        else:
            red.append(f"Market cap ${mc/1e9:.0f}B — mega cap, squeeze mathematically harder.")

    ins = m.insider_ownership
    if ins is not None:
        if ins >= CHAMATH_INSIDER_THRESHOLD:
            macro += 10
            green.append(f"Insider ownership {ins:.1%} — management has skin in the game.")
        elif ins >= 0.02:
            macro += 5
            green.append(f"Insider ownership {ins:.1%} — some alignment.")
        else:
            red.append(f"Insider ownership {ins:.1%} — management not aligned.")

    inst = m.institutional_ownership
    if inst is not None:
        # The 13F-exceeds-float condition is a STRONGER and more specific tell
        # than a high ownership percentage on its own. Institutions holding
        # 85% of a company is ordinary; institutions reporting MORE shares
        # than the entire float can only happen through lending — the lender
        # and the buyer who bought from the short both report the same share.
        # That overlap is direct evidence the borrow is being used, which a
        # bare percentage cannot distinguish from a boring index-heavy name.
        _overlap = (m.institutional_shares_over_float
                    if getattr(m, 'institutional_shares_over_float', None)
                    else None)
        if _overlap and _overlap > 1.0:
            macro += 8
            green.append(
                f"Institutional holdings {_overlap:.2f}x the float — 13F "
                f"reports more shares than exist to trade, which only happens "
                f"through lending. Direct evidence the borrow is in use.")
        elif 0.40 <= inst <= 0.80:
            macro += 8
            green.append(f"Institutional ownership {inst:.1%} — institutional consensus can flip rapidly.")
        elif inst > 0.80:
            macro += 4
            red.append(f"Institutional ownership {inst:.1%} — institutions ARE the short.")
        else:
            macro += 5
            green.append(f"Institutional ownership {inst:.1%} — retail can drive narrative.")

    c.macro_setup_score = min(macro, 30)

    # ── PILLAR 2: Squeeze Pressure (35 pts) ──
    pressure = 0.0
    si = m.short_interest_pct
    dtc = m.days_to_cover

    if si is not None:
        q = f"[{m.si_data_quality}]" if m.si_data_quality else ""
        if si >= 0.40:
            pressure += 18
            green.append(f"Short interest {si:.1%} {q} — maximum squeeze pressure.")
        elif si >= CHAMATH_SHORT_INTEREST_MIN:
            pressure += 12
            green.append(f"Short interest {si:.1%} {q} — meaningful. Narrative flip could cascade.")
        elif si >= 0.08:
            pressure += 5
            red.append(f"Short interest {si:.1%} {q} — modest. Needs strong catalyst.")
        else:
            red.append(f"Short interest {si:.1%} {q} — insufficient pressure.")

    if dtc is not None:
        # Volume-surge distortion guard — see Gill pillar 1 for rationale.
        vs_g = m.volume_surge
        dtc_eval = dtc * min(vs_g, 3.0) / 1.5 if (vs_g and vs_g >= 2.0) else dtc
        if dtc_eval >= 15:
            pressure += 12
            green.append(f"DTC {dtc:.1f} — catastrophically trapped.")
        elif dtc_eval >= GILL_DTC_MIN:
            pressure += 8
            green.append(f"DTC {dtc:.1f} — tight exit. When they run, they all run at once.")
        elif dtc_eval >= 2:
            pressure += 3
            red.append(f"DTC {dtc:.1f} — shorts can exit.")

    ctb = m.ctb_proxy
    if ctb and ctb >= GILL_CTB_PROXY_MIN:
        pressure += 5
        green.append(f"CTB proxy {ctb:.0f}% — expensive to short.")

    if m.ftd_pct_float and m.ftd_pct_float > 0.001:
        pressure += 3
        green.append(f"FTD {m.ftd_pct_float:.2%} of float — confirmed borrow scarcity.")

    c.squeeze_pressure_score = min(pressure, 35)

    # ── PILLAR 3: Catalyst Momentum (35 pts) ──
    catalyst = 0.0
    p1m = m.price_change_1m
    p3m = m.price_change_3m

    if p1m is not None:
        # Recalibrated for PRE-IGNITION entry (the system's actual goal).
        # The old bands gave maximum points to +30%+ months — by which
        # point the squeeze is underway and the asymmetry is gone. The
        # sweet spot is EARLY ignition (5-30%): shorts newly underwater,
        # covering not yet cascaded. Big runs now score as chasing.
        if p1m >= 0.50:
            catalyst += 2
            red.append(f"1-month return {p1m:.0%} — squeeze likely already RAN. Chasing risk.")
        elif p1m >= 0.30:
            catalyst += 7
            green.append(f"1-month return {p1m:.0%} — strong move; mid-squeeze, size accordingly.")
        elif p1m >= CHAMATH_MOMENTUM_MIN:
            catalyst += 12
            green.append(f"1-month return {p1m:.0%} — EARLY IGNITION. Shorts newly underwater.")
        elif p1m >= -0.10:
            catalyst += 5
            green.append(f"1-month return {p1m:.0%} — coiled flat. Loaded spring if catalyst lands.")
        else:
            red.append(f"1-month return {p1m:.0%} — declining. Shorts still winning.")

    if p3m is not None:
        if p3m >= 0.50:
            catalyst += 3
            red.append(f"3-month return {p3m:.0%} — big move already behind. Late.")
        elif p3m >= 0.15:
            catalyst += 6
            green.append(f"3-month return {p3m:.0%} — trend turning, not exhausted.")
        elif p3m >= -0.20:
            catalyst += 3
        else:
            red.append(f"3-month return {p3m:.0%} — weak trend.")

    vs = m.volume_surge
    if vs is not None and vs >= 2.0:
        catalyst += 8
        green.append(f"Volume surge {vs:.1f}x — crowd forming.")

    sc = m.short_change_pct
    if sc is not None:
        if sc >= 0.20:
            catalyst += 7
            green.append(f"Short interest surged {sc:.0%} — shorts doubling down = more fuel.")
        elif sc <= -0.15:
            red.append(f"Short interest dropped {sc:.0%} — best entry may have passed.")
        else:
            catalyst += 3

    c.catalyst_momentum_score = min(catalyst, 35)

    c.total_score = (c.macro_setup_score +
                     c.squeeze_pressure_score +
                     c.catalyst_momentum_score)
    c.green_flags = green
    c.red_flags   = red

    if c.total_score >= 70:
        c.verdict = "SQUEEZE CANDIDATE"
    elif c.total_score >= 50:
        c.verdict = "WATCH — Building Setup"
    else:
        c.verdict = "PASS"

    si_str  = f"{si:.1%}" if si else "N/A"
    dtc_str = f"{dtc:.1f}d" if dtc else "N/A"
    mc_str  = f"${mc/1e9:.1f}B" if mc else "N/A"
    c.narrative = (
        f"Macro: {c.macro_setup_score:.0f}/30 | "
        f"Pressure: {c.squeeze_pressure_score:.0f}/35 | "
        f"Catalyst: {c.catalyst_momentum_score:.0f}/35"
    )
    c.thesis = (
        f"{ticker.upper()} ({mc_str}): SI {si_str}, DTC {dtc_str}. "
        f"Total: {c.total_score:.0f}/100 — {c.verdict}."
    )
    return c


# ─────────────────────────────────────────────
# DISPLAY FORMATTERS
# ─────────────────────────────────────────────

def format_gill_display(g: GillAnalysis) -> str:
    m = g.metrics

    def pct(v, d=1): return f"{v:.{d}%}" if v is not None else "N/A"
    def n(v, d=2):   return f"{v:.{d}f}" if v is not None else "N/A"

    # Data quality note for short interest
    si_note = f"  [{m.si_data_quality}]" if m.si_data_quality else ""

    lines = [
        "",
        f"  ── SQUEEZE SETUP ({g.squeeze_setup_score:.0f}/44) ────────────────────────────────",
        f"  Short Interest % Float:  {pct(m.short_interest_pct):<14}{si_note}",
        f"  Days to Cover (DTC):     {n(m.days_to_cover, 1):<14} threshold > 5 days",
        f"  Cost-to-Borrow Proxy:    {n(m.ctb_proxy, 1)}%       threshold > 10%",
        f"  Short Change (1mo):      {pct(m.short_change_pct):<14} (+ = shorts adding)",
        f"  FTD % of Float:          {pct(m.ftd_pct_float, 3):<14} [{m.ftd_report_date or 'no SEC data'}]",
        "",
        f"  ── FUNDAMENTAL QUALITY ({g.fundamental_score:.0f}/35) ─────────────────────────────",
        f"  P/E Ratio:               {n(m.pe_ratio, 1):<14} threshold < 30x",
        f"  Revenue Growth:          {pct(m.revenue_growth):<14} threshold > 0%",
        f"  Free Cash Flow:          {'${:,.0f}M'.format(m.free_cash_flow/1e6) if m.free_cash_flow else 'N/A':<14}",
        f"  Debt / Equity:           {n(m.debt_to_equity):<14}",
        "",
        f"  ── CATALYST SIGNALS ({g.catalyst_score:.0f}/25) ───────────────────────────────────",
        f"  Volume Surge:            {n(m.volume_surge, 1)+'x':<14} (vs 20d avg)",
        f"  RSI (14):                {n(m.rsi_14, 0):<14}",
        f"  1-Month Return:          {pct(m.price_change_1m):<14}",
        "",
        f"  ── GILL VERDICT ───────────────────────────────────────────────",
        f"  Total Score:   {g.total_score:.0f}/100",
        f"  Verdict:       {g.verdict}",
        f"  Conviction:    {g.conviction}",
        "",
        f"  GREEN FLAGS:",
    ]
    for flag in g.green_flags:
        lines.append(f"    ✅ {flag}")
    lines.append(f"  RED FLAGS:")
    for flag in g.red_flags:
        lines.append(f"    ❌ {flag}")
    lines.append("")
    return "\n".join(lines)


def format_chamath_display(c: ChamathAnalysis) -> str:
    m = c.metrics

    def pct(v, d=1): return f"{v:.{d}%}" if v is not None else "N/A"
    def n(v, d=2):   return f"{v:.{d}f}" if v is not None else "N/A"

    si_note = f"  [{m.si_data_quality}]" if m.si_data_quality else ""

    lines = [
        "",
        f"  ── MACRO SETUP ({c.macro_setup_score:.0f}/30) ──────────────────────────────────────",
        f"  Market Cap:              {'${:,.1f}B'.format(m.market_cap/1e9) if m.market_cap else 'N/A':<14}",
        f"  Insider Ownership:       {pct(m.insider_ownership):<14} threshold > 5%",
        f"  Institutional Own:       {pct(m.institutional_ownership):<14}",
        "",
        f"  ── SQUEEZE PRESSURE ({c.squeeze_pressure_score:.0f}/35) ──────────────────────────────",
        f"  Short Interest % Float:  {pct(m.short_interest_pct):<14}{si_note}",
        f"  Days to Cover (DTC):     {n(m.days_to_cover, 1):<14} threshold > 5 days",
        f"  Cost-to-Borrow Proxy:    {n(m.ctb_proxy, 1)}%",
        f"  FTD % of Float:          {pct(m.ftd_pct_float, 3):<14}",
        f"  FTD % of EFF Float:      {pct(m.ftd_pct_eff_float, 3):<14}"
        f"{('  [' + m.effective_float_quality + ']') if m.effective_float_quality else ''}",
        f"  Effective Float:         "
        f"{'{:,.0f}'.format(m.effective_float) if m.effective_float else 'N/A':<14}"
        f"{('  ' + format(m.float_tightness, '.1f') + 'x tighter than reported') if m.float_tightness and m.float_tightness > 1.05 else ''}",
        f"  Short Change (1mo):      {pct(m.short_change_pct):<14}",
        "",
        f"  ── CATALYST MOMENTUM ({c.catalyst_momentum_score:.0f}/35) ─────────────────────────────",
        f"  1-Month Return:          {pct(m.price_change_1m):<14}",
        f"  3-Month Return:          {pct(m.price_change_3m):<14}",
        f"  Volume Surge:            {n(m.volume_surge, 1)+'x':<14}",
        f"  RSI (14):                {n(m.rsi_14, 0):<14}",
        "",
        f"  ── CHAMATH VERDICT ─────────────────────────────────────────────",
        f"  {c.narrative}",
        f"  Total Score:   {c.total_score:.0f}/100",
        f"  Verdict:       {c.verdict}",
        "",
        f"  GREEN FLAGS:",
    ]
    for flag in c.green_flags:
        lines.append(f"    ✅ {flag}")
    lines.append(f"  RED FLAGS:")
    for flag in c.red_flags:
        lines.append(f"    ❌ {flag}")
    lines.append("")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# QUICK TEST
# ─────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "GME"
    print(f"\n{'='*60}\nKEITH GILL — {ticker.upper()}\n{'='*60}")
    gill = run_gill_analysis(ticker)
    print(format_gill_display(gill))
    print(f"\n{'='*60}\nCHAMATH — {ticker.upper()}\n{'='*60}")
    chamath = run_chamath_analysis(ticker)
    print(format_chamath_display(chamath))
