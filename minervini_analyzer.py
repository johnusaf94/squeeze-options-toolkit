"""
minervini_analyzer.py
======================
Mark Minervini's SEPA (Specific Entry Point Analysis) Trend Template.

Five criteria — ALL must pass for a SEPA SETUP:
1. Price above MA50, MA150, MA200
2. MA50 > MA150 > MA200 (fanning out = accelerating trend)
3. MA200 trending upward for at least 1 month
4. Relative Strength >= 70th percentile (top 30% of market)
5. Price within 25% of 52-week high
"""

import yfinance as yf
import numpy as np
import math
from dataclasses import dataclass, field
from typing import Optional, Tuple, List
from datetime import datetime


# ─────────────────────────────────────────────
# THRESHOLDS
# ─────────────────────────────────────────────
RS_MIN_PERCENTILE     = 70     # top 30% = RS >= 70th percentile
PROXIMITY_TO_HIGH_MAX = 0.25   # within 25% of 52wk high
MA200_TREND_LOOKBACK  = 21     # ~1 month of trading days


# ─────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────

@dataclass
class TrendCheck:
    name:        str  = ""
    passed:      bool = False
    value:       str  = ""
    requirement: str  = ""
    note:        str  = ""


@dataclass
class MinerviniAnalysis:
    ticker:              str = ""
    company_name:        str = ""
    sector:              str = ""
    analysis_date:       str = ""

    current_price:       Optional[float] = None
    ma_50d:              Optional[float] = None
    ma_150d:             Optional[float] = None
    ma_200d:             Optional[float] = None
    ma_200d_month_ago:   Optional[float] = None
    high_52wk:           Optional[float] = None
    low_52wk:            Optional[float] = None
    rs_raw:              Optional[float] = None
    rs_percentile:       Optional[float] = None
    pct_from_52wk_high:  Optional[float] = None
    ma200_trending_up:   bool = False
    adr_pct:             Optional[float] = None

    checks:              List[TrendCheck] = field(default_factory=list)
    criteria_passed:     int   = 0
    criteria_total:      int   = 5
    sepa_score:          float = 0.0
    verdict:             str   = ""
    thesis:              str   = ""
    errors:              list  = field(default_factory=list)


# ─────────────────────────────────────────────
# RELATIVE STRENGTH
# ─────────────────────────────────────────────

def calc_rs_percentile(ticker_return_1yr: float) -> float:
    """Estimate RS percentile vs market using SPY as benchmark."""
    try:
        spy_hist = yf.Ticker("SPY").history(period="13mo", interval="1d")["Close"]
        if len(spy_hist) >= 252:
            spy_return = float((spy_hist.iloc[-1] / spy_hist.iloc[-252]) - 1)
        else:
            spy_return = 0.10
        market_std = 0.35
        z = (ticker_return_1yr - spy_return) / market_std
        percentile = 0.5 * (1 + math.erf(z / math.sqrt(2))) * 100
        return max(1.0, min(99.0, percentile))
    except Exception:
        if ticker_return_1yr > 0.20:   return 80.0
        elif ticker_return_1yr > 0.10: return 65.0
        elif ticker_return_1yr > 0:    return 52.0
        else:                          return 35.0


# ─────────────────────────────────────────────
# MAIN ANALYZER
# ─────────────────────────────────────────────

def run_minervini_analysis(ticker: str) -> MinerviniAnalysis:
    analysis = MinerviniAnalysis(
        ticker=ticker.upper(),
        analysis_date=datetime.now().strftime("%Y-%m-%d")
    )

    try:
        t    = yf.Ticker(ticker)
        info = t.info or {}
        analysis.company_name = info.get("longName", ticker)
        analysis.sector       = info.get("sector", "Unknown") or "Unknown"
    except Exception as e:
        analysis.errors.append(f"Info: {e}")
        info = {}

    # Fetch 14 months of daily history
    try:
        hist = yf.Ticker(ticker).history(period="14mo", interval="1d")
        if hist.empty or len(hist) < 200:
            analysis.errors.append("Need 200+ days of price history")
            analysis.verdict = "INSUFFICIENT DATA"
            return analysis

        prices  = hist["Close"]
        n       = len(prices)

        analysis.current_price     = float(prices.iloc[-1])
        analysis.ma_50d            = float(prices.rolling(50).mean().iloc[-1])  if n >= 50  else None
        analysis.ma_150d           = float(prices.rolling(150).mean().iloc[-1]) if n >= 150 else None
        analysis.ma_200d           = float(prices.rolling(200).mean().iloc[-1]) if n >= 200 else None

        if n >= 200 + MA200_TREND_LOOKBACK:
            analysis.ma_200d_month_ago = float(
                prices.rolling(200).mean().iloc[-(MA200_TREND_LOOKBACK + 1)]
            )

        yr_slice = prices.iloc[-252:] if n >= 252 else prices
        analysis.high_52wk = float(yr_slice.max())
        analysis.low_52wk  = float(yr_slice.min())

        if n >= 252:
            analysis.rs_raw = float((prices.iloc[-1] / prices.iloc[-252]) - 1)
        elif n >= 100:
            analysis.rs_raw = float((prices.iloc[-1] / prices.iloc[0]) - 1) * (252 / n)

        if n >= 20:
            h = hist["High"].iloc[-20:]
            l = hist["Low"].iloc[-20:]
            analysis.adr_pct = float(((h - l) / l).mean() * 100)

    except Exception as e:
        analysis.errors.append(f"History: {e}")
        analysis.verdict = "DATA ERROR"
        return analysis

    p      = analysis.current_price
    ma50   = analysis.ma_50d
    ma150  = analysis.ma_150d
    ma200  = analysis.ma_200d
    checks = []

    # ── Criterion 1: Price above all three MAs ──
    c1 = TrendCheck(
        name="Price Above MA50 / MA150 / MA200",
        requirement="Price > MA50 AND > MA150 AND > MA200"
    )
    if ma50 and ma150 and ma200:
        a50, a150, a200 = p > ma50, p > ma150, p > ma200
        c1.passed = a50 and a150 and a200
        c1.value  = (
            f"Price ${p:.2f} | "
            f"MA50 ${ma50:.2f} ({'✅' if a50 else '❌'}) | "
            f"MA150 ${ma150:.2f} ({'✅' if a150 else '❌'}) | "
            f"MA200 ${ma200:.2f} ({'✅' if a200 else '❌'})"
        )
        if c1.passed:
            c1.note = "Price leading all moving averages — uptrend aligned."
        else:
            fails = []
            if not a50:  fails.append(f"below MA50 by {(p-ma50)/ma50:.1%}")
            if not a150: fails.append(f"below MA150 by {(p-ma150)/ma150:.1%}")
            if not a200: fails.append(f"below MA200 by {(p-ma200)/ma200:.1%}")
            c1.note = "Trend not established: " + " | ".join(fails)
    else:
        c1.value = "Insufficient history"
        c1.note  = "Need 200+ days."
    checks.append(c1)

    # ── Criterion 2: MA hierarchy (50 > 150 > 200) ──
    c2 = TrendCheck(
        name="MA Hierarchy: MA50 > MA150 > MA200",
        requirement="MA50 > MA150 AND MA150 > MA200"
    )
    if ma50 and ma150 and ma200:
        h1, h2 = ma50 > ma150, ma150 > ma200
        c2.passed = h1 and h2
        g1 = (ma50 - ma150) / ma150 * 100
        g2 = (ma150 - ma200) / ma200 * 100
        c2.value = (
            f"MA50-MA150: {g1:+.2f}% ({'✅' if h1 else '❌'}) | "
            f"MA150-MA200: {g2:+.2f}% ({'✅' if h2 else '❌'})"
        )
        c2.note = ("MAs fanning out — stage 2 uptrend signature." if c2.passed
                   else "MAs compressed or inverted — not a stage 2 uptrend.")
    else:
        c2.value = "Insufficient MA data"
    checks.append(c2)

    # ── Criterion 3: MA200 trending up ≥ 1 month ──
    c3 = TrendCheck(
        name="MA200 Rising ≥ 1 Month",
        requirement=f"MA200 today > MA200 {MA200_TREND_LOOKBACK} trading days ago"
    )
    ma200_ago = analysis.ma_200d_month_ago
    if ma200 and ma200_ago:
        change = (ma200 - ma200_ago) / ma200_ago * 100
        analysis.ma200_trending_up = ma200 > ma200_ago
        c3.passed = analysis.ma200_trending_up
        c3.value  = f"Now ${ma200:.2f} | 1mo ago ${ma200_ago:.2f} | Change {change:+.2f}%"
        c3.note   = ("MA200 rising — long-term trend intact." if c3.passed
                     else f"MA200 declining {change:.2f}% — no stage 2 uptrend without rising 200d MA.")
    else:
        c3.value = "Need 14+ months of history"
    checks.append(c3)

    # ── Criterion 4: RS ≥ 70th percentile ──
    c4 = TrendCheck(
        name=f"Relative Strength ≥ {RS_MIN_PERCENTILE}th Pct",
        requirement=f"Top 30% of market (RS percentile ≥ {RS_MIN_PERCENTILE})"
    )
    if analysis.rs_raw is not None:
        analysis.rs_percentile = calc_rs_percentile(analysis.rs_raw)
        c4.passed = analysis.rs_percentile >= RS_MIN_PERCENTILE
        c4.value  = (
            f"1yr return: {analysis.rs_raw:.1%} | "
            f"RS ~{analysis.rs_percentile:.0f}th pct "
            f"({'✅ Top 30%' if c4.passed else '❌ Not top 30%'})"
        )
        c4.note = (
            f"Outperforming ~{analysis.rs_percentile:.0f}% of market." if c4.passed
            else f"RS ~{analysis.rs_percentile:.0f}th pct — Minervini: 'I only buy the best horses.'"
        )
    else:
        c4.value = "Need 12+ months of history"
    checks.append(c4)

    # ── Criterion 5: Within 25% of 52-week high ──
    c5 = TrendCheck(
        name="Within 25% of 52-Week High",
        requirement="Price ≥ 75% of 52-week high"
    )
    if analysis.high_52wk and p:
        pct_from_high = (p - analysis.high_52wk) / analysis.high_52wk
        analysis.pct_from_52wk_high = pct_from_high
        c5.passed = pct_from_high >= -PROXIMITY_TO_HIGH_MAX
        c5.value  = (
            f"Price ${p:.2f} | 52wk High ${analysis.high_52wk:.2f} | "
            f"{pct_from_high:.1%} from high"
        )
        c5.note = (
            f"Only {abs(pct_from_high):.1%} below 52wk high — near highs for a reason." if c5.passed
            else f"{abs(pct_from_high):.1%} below 52wk high — too far. Wait for recovery to within 25%."
        )
    else:
        c5.value = "52-week high unavailable"
    checks.append(c5)

    # ── Aggregate ──
    analysis.checks          = checks
    analysis.criteria_passed = sum(1 for c in checks if c.passed)
    analysis.sepa_score      = analysis.criteria_passed / 5 * 100

    if analysis.criteria_passed == 5:
        analysis.verdict = "SEPA SETUP ✅"
        analysis.thesis  = (
            f"{ticker.upper()} passes ALL 5 SEPA criteria. "
            f"Price ${p:.2f} above all MAs, MAs fanning, MA200 rising, "
            f"RS ~{analysis.rs_percentile:.0f}th pct, "
            f"{abs(analysis.pct_from_52wk_high or 0):.1%} from 52wk high. "
            f"Minervini would consider a breakout entry."
        )
    elif analysis.criteria_passed >= 4:
        failed = [c.name.split(":")[0].split("(")[0].strip() for c in checks if not c.passed]
        analysis.verdict = "NEAR SETUP ⚠️"
        analysis.thesis  = f"{ticker.upper()} passes {analysis.criteria_passed}/5. Failing: {', '.join(failed)}."
    elif analysis.criteria_passed >= 3:
        analysis.verdict = "DEVELOPING"
        analysis.thesis  = f"{ticker.upper()} {analysis.criteria_passed}/5 criteria met. Not ready for entry."
    else:
        analysis.verdict = "NOT READY"
        analysis.thesis  = f"{ticker.upper()} only {analysis.criteria_passed}/5 criteria. Not in stage 2 uptrend."

    return analysis


# ─────────────────────────────────────────────
# DISPLAY
# ─────────────────────────────────────────────

def format_minervini_display(analysis: MinerviniAnalysis) -> str:
    lines = [
        "",
        f"  ── SEPA TREND TEMPLATE ({analysis.criteria_passed}/{analysis.criteria_total} criteria) ──────────────",
    ]
    for i, c in enumerate(analysis.checks, 1):
        sym = "✅" if c.passed else "❌"
        lines.append(f"  {sym} [{i}] {c.name}")
        if c.value:
            lines.append(f"       {c.value}")
        if c.note:
            lines.append(f"       {c.note}")
        lines.append("")

    lines += [
        f"  ── MINERVINI VERDICT ───────────────────────────────────────────",
        f"  SEPA Score:  {analysis.sepa_score:.0f}/100",
        f"  Verdict:     {analysis.verdict}",
        f"  {analysis.thesis[:120]}",
    ]
    if analysis.errors:
        lines.append(f"  ⚠️  {' | '.join(analysis.errors[:2])}")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    ticker = sys.argv[1] if len(sys.argv) > 1 else "NVDA"
    result = run_minervini_analysis(ticker)
    print(format_minervini_display(result))
