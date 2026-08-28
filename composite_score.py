"""
composite_score.py
==================
Pure Python composite scoring engine.
Aggregates all framework scores into a single 0-100 score.
Zero LLM involvement — entirely deterministic and reproducible.
"""

from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────
# WEIGHTS (must sum to 100)
# ─────────────────────────────────────────────
WEIGHTS = {
    "buffett_moat":           22,
    "buffett_valuation":      16,
    "weiss_yield":             9,
    "weiss_quality":           9,
    "bogle_timing":           10,
    "dalio_debt":              8,
    "dalio_bubble":            8,
    "lynch_peg":               7,
    "druckenmiller":          11,
}
assert sum(WEIGHTS.values()) == 100, "Weights must sum to 100"

# Score thresholds
STRONG_BUY  = 75
BUY         = 60
WATCHLIST   = 45
# below 45 = AVOID

# Account fit thresholds
ROTH_MIN_SCORE    = 55   # higher risk acceptable in Roth
TAXABLE_MIN_SCORE = 60   # need more stability in taxable


# ─────────────────────────────────────────────
# INDIVIDUAL COMPONENT SCORERS
# Each returns a (raw_score 0-1, display, detail) tuple
# ─────────────────────────────────────────────

def score_buffett_moat(moat_score) -> tuple:
    """
    Buffett moat: 0-4 criteria passing, ADJUSTED by the moat-trend
    multiplier so a narrowing moat scores lower than a widening one.

    Uses moat_score.adjusted_score (raw criteria × trend multiplier,
    clamped 0-4) rather than the raw score. This keeps the composite
    consistent with the searcher's moat ranking — previously the two
    disagreed because the composite read raw .score while the ranking
    read .adjusted_score. A 4/4 STRONG·narrowing moat now contributes
    proportionally less than a 4/4 STRONG·widening one.

    Returns None (excluded + weight redistributed) when there's no data,
    consistent with the other "no data → None" scorers.
    """
    if moat_score is None:
        return None, "N/A", "No data"

    # Prefer trend-adjusted score; fall back to raw for older MoatScore
    # objects that predate the adjusted_score field.
    adjusted = getattr(moat_score, "adjusted_score", None)
    if adjusted is None:
        adjusted = moat_score.score
    score = max(0.0, min(4.0, adjusted)) / 4.0

    rating = moat_score.rating
    flags = " | ".join(moat_score.flags) if moat_score.flags else "No flags"
    # Show both raw criteria count and adjusted so the breakdown is honest
    raw_n = moat_score.score
    if abs(adjusted - raw_n) > 0.01:
        display = f"{raw_n}/4 raw → {adjusted:.2f}/4 adj — {rating}"
    else:
        display = f"{raw_n}/4 — {rating}"
    return score, display, flags


def score_buffett_valuation(valuation) -> tuple:
    """
    Buffett valuation — recalibrated three-check method.

    Replaces the prior FEY-vs-Treasury implementation that structurally
    capped quality-stock scores in the low 70s by requiring sub-15x P/E
    to score full marks. The new method asks "is the price reasonable
    for a quality business" rather than "is it statistically cheap vs
    bonds" — which matches Buffett's actual practice better.

    Check 1 — Forward P/E (33% of valuation score)
        < 15x → 1.0   (cheap)
        < 22x → 0.85  (fair-to-attractive for a quality compounder)
        < 30x → 0.60  (premium but justifiable for high-ROIC names)
        < 40x → 0.30  (expensive — needs special story)
        >=40x → 0.10  (rich)
        Note: 22x is the meaningful threshold — Buffett owns AAPL at
        25-30x because the moat justifies it. Anchoring "full credit"
        at sub-15x systematically misprices quality.

    Check 2 — Price to FCF (33% of valuation score)
        < 20x → 1.0   (cash-generative bargain)
        < 30x → 0.70  (fair)
        < 50x → 0.40  (premium)
        >=50x → 0.10  (rich)
        FCF check provides a different lens than P/E — catches accounting
        differences. If FCF unavailable, this check is excluded and the
        other two re-normalize to fill the gap (no penalty for data gap).

    Check 3 — DCF margin of safety (33% of valuation score)
        > +25% → 1.0   (significant margin of safety)
        > 0%   → 0.70  (at or above intrinsic)
        > -30% → 0.40  (modestly overvalued)
        <= -30% → 0.10 (significantly overvalued)
        Uses the two-stage DCF with linear growth fade we built — already
        a meaningfully more honest intrinsic value than the old single-
        stage 25%-growth-for-10-years model.

    Final score = average of available checks (excluded checks redistributed).
    Returns None for the entire component if NO valuation data is available.
    """
    # Need at least the valuation object itself; individual fields below
    # are checked separately so partial data still produces a usable score.
    if valuation is None:
        return None, "N/A", "No data"

    checks = []      # list of (label, score 0-1, detail string)
    details = []

    # ── Check 1: Forward P/E ──
    fpe = getattr(valuation, "forward_pe", None) or valuation.pe_ratio
    pe_label = "Fwd P/E" if getattr(valuation, "forward_pe", None) else "P/E"
    if fpe and fpe > 0:
        if fpe < 15:
            s = 1.00
            tag = "cheap ✅"
        elif fpe < 22:
            s = 0.85
            tag = "fair-to-attractive ✅"
        elif fpe < 30:
            s = 0.60
            tag = "premium but justifiable ⚠️"
        elif fpe < 40:
            s = 0.30
            tag = "expensive ⚠️"
        else:
            s = 0.10
            tag = "rich ❌"
        checks.append(s)
        details.append(f"{pe_label} {fpe:.1f}x — {tag}")
    # else: check excluded (no P/E available)

    # ── Check 2: Price to FCF ──
    fcf_ps = getattr(valuation, "fcf_per_share", None)
    price  = valuation.current_price
    if fcf_ps and price and fcf_ps > 0:
        p_fcf = price / fcf_ps
        if p_fcf < 20:
            s = 1.00
            tag = "cash bargain ✅"
        elif p_fcf < 30:
            s = 0.70
            tag = "fair ⚠️"
        elif p_fcf < 50:
            s = 0.40
            tag = "premium ⚠️"
        else:
            s = 0.10
            tag = "rich ❌"
        checks.append(s)
        details.append(f"P/FCF {p_fcf:.1f}x — {tag}")
    # else: check excluded if no FCF data

    # ── Check 3: DCF margin of safety ──
    dcf_up = getattr(valuation, "dcf_upside_pct", None)
    if dcf_up is not None:
        if dcf_up > 0.25:
            s = 1.00
            tag = f"DCF upside {dcf_up:+.0%} — strong margin of safety ✅"
        elif dcf_up > 0:
            s = 0.70
            tag = f"DCF upside {dcf_up:+.0%} — at or above intrinsic ⚠️"
        elif dcf_up > -0.30:
            s = 0.40
            tag = f"DCF downside {dcf_up:+.0%} — modestly overvalued ⚠️"
        else:
            s = 0.10
            tag = f"DCF downside {dcf_up:+.0%} — significantly overvalued ❌"
        checks.append(s)
        details.append(tag)

    # ── Combine: average of available checks ──
    # If all 3 are gone, fall back to None (excluded entirely from composite).
    if not checks:
        return None, "N/A — no valuation metrics", ""

    score = sum(checks) / len(checks)
    n = len(checks)
    label = f"{score:.0%} valuation ({n}/3 checks)"
    return score, label, " | ".join(details)


def score_weiss_yield(yield_signal) -> tuple:
    """
    Weiss yield: BUY=100%, WATCH_BUY=75%, HOLD=50%, WATCH_SELL=25%, SELL/NO_DIV=0%
    Non-dividend stocks get 50% (neutral — not penalized)
    """
    if yield_signal is None:
        return None, "N/A", "No yield data — excluded"

    signal_map = {
        "BUY":               (1.00, "✅ In buy zone"),
        "WATCH — BUY ZONE":  (0.75, "👀 Approaching buy zone"),
        "HOLD":              (0.50, "⚪ Mid-range — neutral"),
        "WATCH — SELL ZONE": (0.25, "⚠️ Approaching sell zone"),
        "SELL":              (0.00, "🔴 In sell zone — overvalued by yield"),
        "NON-DIVIDEND STOCK":(0.50, "— No dividend (neutral for growth stock)"),
        "NO DIVIDEND":       (0.50, "— No dividend (neutral for growth stock)"),
        "NO PRICE DATA":     (0.50, "No price data"),
        "INSUFFICIENT DATA": (0.50, "Insufficient history"),
        "ERROR":             (0.50, "Data error"),
    }
    # Data-gap signals → exclude from composite (None) rather than
    # contributing a neutral 0.5. Non-dividend stocks are already
    # auto-skipped upstream in build_composite, so reaching here with
    # a no-data signal means a genuine gap.
    NO_DATA_SIGNALS = {"NO PRICE DATA", "INSUFFICIENT DATA", "ERROR"}
    if yield_signal.signal in NO_DATA_SIGNALS:
        return None, "N/A", f"{yield_signal.signal} — excluded"

    score, label = signal_map.get(yield_signal.signal, (0.50, yield_signal.signal))
    detail = yield_signal.reasoning[:100] if yield_signal.reasoning else ""
    return score, label, detail


def score_weiss_quality(blue_chip) -> tuple:
    """
    Weiss 7 blue chip criteria — calibrated sliding scale (not linear),
    with denominator-aware scoring for missing data.

    Now scores against `measurable` (the criteria with available data),
    not the full 7. A stock passing 5/5 measurable criteria scores the
    SAME as a stock passing 5/7 with all data available — because the
    same proportion of verifiable quality criteria were satisfied.

    Structural fails (no dividend history, insufficient earnings track
    record) still count against — those are real Weiss disqualifications.
    Data gaps (P/E unavailable, S&P rating not provided by yfinance) are
    excluded from both numerator and denominator.

    Curve calibrated to the realistic distribution of modern blue chip
    outcomes (so 5 out of 7 isn't penalized as 71% — modern quality
    compounders rarely pass legacy criteria like 12yr dividend history).
    """
    if blue_chip is None or not blue_chip.criteria:
        return None, "N/A", "No quality data"

    measurable = getattr(blue_chip, "measurable", None)
    if measurable is None:
        measurable = 7   # backward-compat fallback for old BlueChipScore objects
    passed = int(blue_chip.score)

    if measurable == 0:
        return None, "N/A — no criteria measurable", \
               "All 7 criteria had data gaps"

    # Compute proportion, then map through a calibrated curve.
    # The curve uses the same shape as the old 7-point table but applies
    # to whatever number of measurable criteria exist.
    pct = passed / measurable

    # Calibrated curve — same intent as the per-pass table, expressed as
    # a smooth function of the proportion passing:
    #   0%  → 0%
    #   14% → 12%   (just one — likely noise)
    #   28% → 25%
    #   43% → 45%
    #   57% → 65%
    #   71% → 80%   (modern quality compounder ceiling — was 5/7)
    #   86% → 92%
    #   100% → 100%
    CURVE = [
        (0.000, 0.00), (0.143, 0.12), (0.286, 0.25), (0.429, 0.45),
        (0.571, 0.65), (0.714, 0.80), (0.857, 0.92), (1.000, 1.00),
    ]
    # Linear interpolation between curve points
    score = 0.0
    for i in range(len(CURVE) - 1):
        x0, y0 = CURVE[i]
        x1, y1 = CURVE[i + 1]
        if x0 <= pct <= x1:
            if x1 == x0:
                score = y0
            else:
                score = y0 + (y1 - y0) * (pct - x0) / (x1 - x0)
            break
    else:
        score = 1.0 if pct >= 1.0 else 0.0

    display = f"{passed}/{measurable} measurable — {blue_chip.rating}"
    return score, display, ""


def score_bogle_timing(reversion) -> tuple:
    """
    Bogle reversion timing: 0-10 score, normalized to 0-1.
    """
    if reversion is None or reversion.timing_score is None:
        return None, "N/A", "No timing data — excluded"
    score = reversion.timing_score / 10.0
    return score, f"{reversion.timing_score}/10 — {reversion.timing_signal}", reversion.timing_reasoning[:80]


def score_bogle_diversification(diversification) -> tuple:
    """
    Bogle diversification — displayed for context but NEUTRAL score always.
    Correlation to personal portfolio should not penalize a stock's quality rating.
    The display label still shows the actual impact so it's visible in the breakdown.
    """
    if diversification is None:
        return 0.5, "N/A", "No portfolio data"
    # Map labels for display only — score is always 0.5 (neutral)
    impact_labels = {
        "IMPROVES":             "✅ Reduces portfolio concentration",
        "NEUTRAL":              "⚪ No meaningful diversification change",
        "HURTS":                "⚠️ Increases portfolio concentration",
        "HURTS_SIGNIFICANTLY":  "❌ Significantly increases concentration",
    }
    label = impact_labels.get(diversification.diversification_impact, "Unknown")
    reasoning = diversification.impact_reasoning[:80] if hasattr(diversification, "impact_reasoning") else ""
    # Always return neutral 0.5 — correlation info is for context, not rating
    return 0.5, f"{label} [not scored]", reasoning


def score_dalio_debt(debt_cycle) -> tuple:
    """
    Dalio debt: pass/fail with gradient.
    Below 3x=100%, 3-5x=40%, above 5x=0%
    """
    if debt_cycle is None or debt_cycle.debt_to_ebitda is None:
        return None, "N/A", "No debt data — excluded"
    d = debt_cycle.debt_to_ebitda
    if d == 0:
        return 1.0, "✅ No debt", "Zero leverage"
    elif d < 1.5:
        return 1.0, f"✅ {d:.1f}x — very low", "Conservative balance sheet"
    elif d < 3.0:
        return 0.75, f"✅ {d:.1f}x — manageable", "Within Dalio threshold"
    elif d < 5.0:
        return 0.35, f"⚠️ {d:.1f}x — elevated", "Above 3x threshold"
    else:
        return 0.0, f"❌ {d:.1f}x — dangerous", "High debt in late cycle"


def score_dalio_bubble(bubble) -> tuple:
    """
    Dalio bubble: 3 checks, each worth 1/3.
    """
    if bubble is None:
        return None, "N/A", "No bubble data — excluded"
    score = bubble.checks_passed / 3.0
    return score, f"{bubble.checks_passed}/3 checks — {bubble.result.note[:50]}", ""


def score_lynch_peg(live_data) -> tuple:
    """
    Lynch PEG ratio:
    <0.5=100% (screaming bargain), 0.5-1.0=85%, 1.0-1.5=65%,
    1.5-2.0=40%, 2.0-2.5=20%, >2.5=0%
    No dividend/no PEG = 50% neutral
    """
    if live_data is None or live_data.peg_ratio is None:
        return None, "N/A (no PEG data)", "Excluded — no growth estimate available"
    peg = live_data.peg_ratio
    if peg <= 0:
        # Negative PEG = unprofitable or no growth. This is real information
        # (not a data gap), but PEG is genuinely unreliable here — exclude
        # rather than guess. Profitability shows up in other frameworks.
        return None, f"PEG {peg:.2f} (negative — unprofitable)", "Excluded — PEG unreliable for unprofitable co"
    elif peg < 0.5:
        return 1.00, f"PEG {peg:.2f} — screaming bargain", "Lynch loves this"
    elif peg < 1.0:
        return 0.85, f"PEG {peg:.2f} — attractive", "Below Lynch's target of 1.0"
    elif peg < 1.5:
        return 0.65, f"PEG {peg:.2f} — fair value", "Reasonable but not cheap"
    elif peg < 2.0:
        return 0.40, f"PEG {peg:.2f} — stretched", "Paying up for growth"
    elif peg < 2.5:
        return 0.20, f"PEG {peg:.2f} — expensive", "Lynch would pass"
    else:
        return 0.00, f"PEG {peg:.2f} — overpriced", "Market pricing in perfection"


# ─────────────────────────────────────────────
# ACCOUNT FIT
# ─────────────────────────────────────────────

def determine_account_fit(composite: float, live_data, bogle_div=None) -> tuple:
    """
    Given composite score and stock characteristics, recommend account placement.
    Returns (account, reasoning).
    bogle_div param kept for compatibility but no longer used.
    """
    is_dividend = live_data and live_data.dividend_rate and live_data.dividend_rate > 0
    dividend_yield = live_data.dividend_yield if live_data else None
    beta = live_data.beta if live_data else None

    high_yield = dividend_yield and dividend_yield > 0.04   # >4% yield
    high_beta  = beta and beta > 1.4

    if composite >= STRONG_BUY:
        if high_beta and not high_yield:
            return "Roth 401k", "High growth, higher volatility — tax-free compounding maximizes return"
        elif high_yield:
            return "Either", "Strong score with meaningful yield — works in both accounts"
        else:
            return "Roth 401k", "Strong compounder — let it grow tax-free over 30 years"
    elif composite >= BUY:
        if high_yield and not high_beta:
            return "Taxable Brokerage", "Solid yield, lower volatility — fits income + stability goal"
        else:
            return "Roth 401k", "Growth profile — better in tax-free account"
    elif composite >= WATCHLIST:
        if high_yield:
            return "Watchlist — Taxable", "Watch for better entry; yield supports taxable placement if bought"
        else:
            return "Watchlist — Roth", "Not a buy yet; monitor for improvement in valuation/timing"
    else:
        return "Avoid", "Score too low across multiple frameworks"


# ─────────────────────────────────────────────
# MAIN COMPOSITE SCORER
# ─────────────────────────────────────────────

@dataclass
class ComponentScore:
    name:         str = ""
    weight:       int = 0
    raw:          float = 0.0    # 0.0 to 1.0
    weighted:     float = 0.0    # raw * weight
    display:      str = ""
    detail:       str = ""
    bar_filled:   int = 0        # 0-20 blocks for display


@dataclass
class CompositeResult:
    ticker:           str = ""
    company_name:     str = ""
    date:             str = ""
    components:       list = field(default_factory=list)
    total_score:      float = 0.0    # 0-100
    signal:           str = ""       # STRONG BUY / BUY / WATCHLIST / AVOID
    account_fit:      str = ""
    account_reason:   str = ""
    market_context:   str = ""       # Buffett Indicator reading
    data_quality:     str = ""       # HIGH / MEDIUM / LOW
    coverage_pct:     float = 100.0  # % of base framework weight that had data
    missing_data:     list = field(default_factory=list)
    skipped:          list = field(default_factory=list)


def score_gill(gill_analysis) -> tuple:
    """
    Keith Gill squeeze score: 0-1 normalized from 0-100.
    Returns neutral 0.5 if short interest is below threshold
    (squeeze analysis only meaningful for heavily shorted stocks).
    """
    if gill_analysis is None:
        return 0.5, "N/A", "No Gill analysis"
    g = gill_analysis
    m = g.metrics

    # If barely shorted, this metric is neutral — don't penalize normal stocks
    if m.short_interest_pct is None or m.short_interest_pct < 0.10:
        return 0.5, f"SI {m.short_interest_pct:.0%} — below squeeze threshold (neutral)" if m.short_interest_pct else "No short data (neutral)", ""

    score = g.total_score / 100.0
    display = f"{g.verdict} | Score {g.total_score:.0f}/100 | SI {m.short_interest_pct:.0%} | DTC {m.days_to_cover:.1f}d" if m.days_to_cover else f"{g.verdict} | {g.total_score:.0f}/100"
    return score, display, g.thesis[:80]


def score_chamath(chamath_analysis) -> tuple:
    """
    Chamath squeeze score: 0-1 normalized.
    Also neutral for low short-interest stocks.
    """
    if chamath_analysis is None:
        return 0.5, "N/A", "No Chamath analysis"
    c = chamath_analysis
    m = c.metrics

    if m.short_interest_pct is None or m.short_interest_pct < 0.08:
        return 0.5, f"SI {m.short_interest_pct:.0%} — below squeeze threshold (neutral)" if m.short_interest_pct else "No short data (neutral)", ""

    score = c.total_score / 100.0
    display = f"{c.verdict} | Score {c.total_score:.0f}/100 | {c.narrative}"
    return score, display, c.thesis[:80]


def score_druckenmiller(druck_analysis) -> tuple:
    """
    Druckenmiller triple alignment: pig philosophy score normalized to 0-1.
    MAX conviction + SIZE UP signal = 100%
    EXIT signal = 0% regardless of other scores
    """
    if druck_analysis is None:
        return 0.5, "N/A", "No Druckenmiller data"

    # Hard exit overrides everything
    mf = druck_analysis.mental_flexibility
    if mf and mf.exit_signal == "EXIT":
        return 0.0, f"❌ EXIT — Stop triggered ({mf.stop_note[:60]})", "Druckenmiller exits immediately when price breaks key MA"

    pp = druck_analysis.pig_philosophy
    if pp.triple_align_score is None:
        return 0.5, "N/A", "No alignment data"

    score = pp.triple_align_score
    display = f"{score:.0%} alignment — {pp.conviction_level} conviction — {pp.signal}"
    detail = pp.reasoning[:90]
    return score, display, detail


def build_composite(
    ticker: str,
    company_name: str,
    buffett_analysis=None,
    weiss_analysis=None,
    bogle_analysis=None,
    dalio_analysis=None,
    druckenmiller_analysis=None,
    live_data=None,
    market_context_str: str = "",
    skipped: set = None,
) -> CompositeResult:
    """
    Build the composite score from all framework analyses.

    Key behaviour:
    - skipped: set of analyzer names that were intentionally excluded for this
               asset class (e.g. {"buffett", "weiss"} for ETFs).
               Skipped components are REMOVED and their weights redistributed
               proportionally across remaining components so the total = 100.
    - None analysis (data error): component included at 0% and flagged as missing.
    """
    from datetime import datetime
    if skipped is None:
        skipped = set()

    result = CompositeResult(
        ticker=ticker.upper(),
        company_name=company_name,
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        market_context=market_context_str,
    )

    # ── Also auto-skip Weiss yield if stock pays no dividend ──
    if live_data and (not live_data.dividend_rate or live_data.dividend_rate == 0):
        skipped = skipped | {"weiss_yield"}

    components_raw = []   # (key, name, base_weight, raw, display, detail)
    missing = []

    # Helper: don't add component if its framework key is in skipped
    def maybe_add(key, name, base_weight, raw_fn, *args):
        # Check if this component's framework is skipped
        framework = key.split("_")[0]   # "buffett_moat" -> "buffett"
        if key in skipped or framework in skipped:
            return   # exclude entirely — weight redistributed to active components
        try:
            raw, display, detail = raw_fn(*args)
        except Exception as e:
            # Scorer crashed — treat as missing (exclude + redistribute),
            # NOT as 0.0 (which would silently punish the composite score).
            # Record the error in missing_data for transparency.
            missing.append(f"{name} (error: {e})")
            return
        # None return = the scorer explicitly signals "no data available".
        # Exclude from composite entirely rather than contributing a zero.
        # The weight redistribution in the normalisation step handles this.
        if raw is None:
            missing.append(f"{name} (no data)")
            return
        components_raw.append((key, name, base_weight, raw, display, detail))

    def flag_missing(key, label, analysis_obj):
        if analysis_obj is None:
            missing.append(label)

    # ── Build candidate components ──
    flag_missing("buffett", "Buffett Moat",        buffett_analysis)
    flag_missing("buffett", "Buffett Valuation",   buffett_analysis)
    flag_missing("weiss",   "Weiss Yield",         weiss_analysis)
    flag_missing("weiss",   "Weiss Quality",       weiss_analysis)
    flag_missing("bogle",   "Bogle Timing",        bogle_analysis)
    flag_missing("dalio",   "Dalio Debt",          dalio_analysis)
    flag_missing("dalio",   "Dalio Bubble",        dalio_analysis)
    flag_missing("druck",   "Druckenmiller",       druckenmiller_analysis)

    moat_obj = buffett_analysis.moat_score if buffett_analysis else None
    val_obj  = buffett_analysis.valuation  if buffett_analysis else None
    ys_obj   = weiss_analysis.yield_signal  if weiss_analysis  else None
    bc_obj   = weiss_analysis.blue_chip     if weiss_analysis  else None
    rev_obj  = bogle_analysis.reversion     if bogle_analysis  else None
    debt_obj = dalio_analysis.debt_cycle    if dalio_analysis  else None
    bub_obj  = dalio_analysis.bubble        if dalio_analysis  else None

    maybe_add("buffett_moat",          "Buffett — Moat Quality",      WEIGHTS["buffett_moat"],          score_buffett_moat,      moat_obj)
    maybe_add("buffett_valuation",     "Buffett — Valuation/FEY",     WEIGHTS["buffett_valuation"],     score_buffett_valuation, val_obj)
    maybe_add("weiss_yield",           "Weiss — Yield Signal",        WEIGHTS["weiss_yield"],           score_weiss_yield,       ys_obj)
    maybe_add("weiss_quality",         "Weiss — Blue Chip Quality",   WEIGHTS["weiss_quality"],         score_weiss_quality,     bc_obj)
    maybe_add("bogle_timing",          "Bogle — Buy Timing",          WEIGHTS["bogle_timing"],          score_bogle_timing,      rev_obj)
    maybe_add("dalio_debt",            "Dalio — Debt Cycle",          WEIGHTS["dalio_debt"],            score_dalio_debt,        debt_obj)
    maybe_add("dalio_bubble",          "Dalio — Bubble Risk",         WEIGHTS["dalio_bubble"],          score_dalio_bubble,      bub_obj)
    maybe_add("lynch_peg",             "Lynch — PEG Ratio",           WEIGHTS["lynch_peg"],             score_lynch_peg,         live_data)
    maybe_add("druckenmiller",         "Druckenmiller — Triple Align",WEIGHTS["druckenmiller"],         score_druckenmiller,     druckenmiller_analysis)

    # ── Redistribute weights so active components always sum to 100 ──
    total_base_weight = sum(row[2] for row in components_raw)
    # Track how much of the FULL 100-point base weight survived. If a lot
    # was excluded (data-poor stock), surviving components get amplified —
    # so we flag low coverage to prevent a STRONG BUY on thin evidence.
    coverage_pct = total_base_weight   # base weights sum to 100, so this IS the %
    components = []
    for key, name, base_w, raw, display, detail in components_raw:
        if total_base_weight > 0:
            adjusted_w = round(base_w / total_base_weight * 100, 1)
        else:
            adjusted_w = 0
        components.append(ComponentScore(
            name=name,
            weight=adjusted_w,
            raw=raw,
            weighted=raw * adjusted_w,
            display=display,
            detail=detail,
        ))

    # ── Fill bar blocks (0-20) ──
    for c in components:
        c.bar_filled = round(c.raw * 20)

    result.components = components
    result.total_score = sum(c.weighted for c in components)
    result.missing_data = missing
    result.skipped = list(skipped)

    # ── Signal ──
    if result.total_score >= STRONG_BUY:
        result.signal = "STRONG BUY"
    elif result.total_score >= BUY:
        result.signal = "BUY"
    elif result.total_score >= WATCHLIST:
        result.signal = "WATCHLIST"
    else:
        result.signal = "AVOID"

    result.coverage_pct = round(coverage_pct, 1)

    # ── Data quality — now coverage-based, not just a missing-item count ──
    # coverage_pct is the share of the full 100-point framework weight that
    # actually had data. Intentional skips (ETFs) and data gaps both reduce
    # it; what matters for confidence is how much evidence the score rests on.
    if coverage_pct >= 85:
        result.data_quality = "HIGH"
    elif coverage_pct >= 65:
        result.data_quality = "MEDIUM"
    else:
        result.data_quality = "LOW"

    # ── Thin-coverage signal guard ──
    # Fix #4 means excluded components no longer contribute free 0.5s — good,
    # but it also amplifies surviving components. A STRONG BUY resting on
    # <65% framework coverage is sitting on thin evidence. Cap the *signal*
    # (not the numeric score) so a data-poor stock can't present as a
    # high-confidence buy. The number stays visible; the label gets honest.
    if coverage_pct < 65 and result.signal == "STRONG BUY":
        result.signal = "BUY"   # demote one notch — thin evidence
    if coverage_pct < 50 and result.signal == "BUY":
        result.signal = "WATCHLIST"   # very thin — don't imply conviction

    # ── Account fit ──
    result.account_fit, result.account_reason = determine_account_fit(
        result.total_score, live_data, None
    )

    return result


# ─────────────────────────────────────────────
# DISPLAY FORMATTERS
# ─────────────────────────────────────────────

SIGNAL_COLORS = {
    "STRONG BUY": "\033[92m",   # bright green
    "BUY":        "\033[32m",   # green
    "WATCHLIST":  "\033[93m",   # yellow
    "AVOID":      "\033[91m",   # red
}
RESET = "\033[0m"


def format_composite_terminal(result: CompositeResult) -> str:
    """Plain text table for terminal display (used in tkinter chat window)."""
    lines = []
    lines.append("")
    lines.append(f"  ╔══════════════════════════════════════════════════════════╗")
    lines.append(f"  ║  COMPOSITE SCORE — {result.ticker:<10} {result.company_name[:28]:<28}  ║")
    lines.append(f"  ╠══════════════════════════════════════════════════════════╣")
    lines.append(f"  ║  {'Framework':<28} {'Score':>6}  {'Contribution':>5}  Bar               ║")
    lines.append(f"  ╠══════════════════════════════════════════════════════════╣")

    for c in result.components:
        bar = "█" * c.bar_filled + "░" * (20 - c.bar_filled)
        pct = f"{c.raw:.0%}"
        contrib = f"+{c.weighted:.1f}"
        lines.append(f"  ║  {c.name:<28} {pct:>6}  {contrib:>5}  {bar}  ║")

    lines.append(f"  ╠══════════════════════════════════════════════════════════╣")

    # Score bar
    score_bar_filled = round(result.total_score / 5)
    score_bar = "█" * score_bar_filled + "░" * (20 - score_bar_filled)
    lines.append(f"  ║  {'TOTAL SCORE':<28} {result.total_score:>5.1f}  {'':>5}  {score_bar}  ║")
    lines.append(f"  ╠══════════════════════════════════════════════════════════╣")
    lines.append(f"  ║  Signal:      {result.signal:<44}  ║")
    lines.append(f"  ║  Account fit: {result.account_fit:<44}  ║")
    lines.append(f"  ║  Data quality:{result.data_quality:<44}  ║")
    if result.market_context:
        mc = result.market_context[:44]
        lines.append(f"  ║  Market:      {mc:<44}  ║")
    lines.append(f"  ╚══════════════════════════════════════════════════════════╝")

    # Component details
    lines.append("")
    lines.append("  BREAKDOWN:")
    for c in result.components:
        if c.display and c.display != "N/A":
            lines.append(f"  • {c.name}: {c.display}")
            if c.detail:
                lines.append(f"    {c.detail[:90]}")

    if result.missing_data:
        lines.append(f"\n  ⚠️  Excluded (no data — weight redistributed): {', '.join(result.missing_data)}")

    lines.append(f"\n  Account reasoning: {result.account_reason}")
    lines.append("")
    return "\n".join(lines)


def format_composite_for_claude(result: CompositeResult) -> str:
    """Structured fact sheet for Claude API Q&A — contains all scores as context."""
    lines = [
        f"COMPOSITE ANALYSIS — {result.ticker} ({result.company_name})",
        f"Date: {result.date}",
        f"Overall Score: {result.total_score:.1f}/100 — {result.signal}",
        f"Account Fit: {result.account_fit}",
        f"Data Quality: {result.data_quality}",
        f"Market Context: {result.market_context}",
        "",
        "COMPONENT SCORES:",
    ]
    for c in result.components:
        lines.append(f"  {c.name} (weight {c.weight}%): {c.raw:.0%} → +{c.weighted:.1f}pts | {c.display}")
        if c.detail:
            lines.append(f"    Detail: {c.detail}")
    if result.missing_data:
        lines.append(f"\nExcluded components (no data — weight redistributed): {', '.join(result.missing_data)}")
    lines.append(f"\nAccount recommendation: {result.account_fit} — {result.account_reason}")
    return "\n".join(lines)
