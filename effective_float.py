"""
effective_float.py
==================
Effective (tradeable) float, and what an FTD close-out is worth against it.

WHY THIS EXISTS
---------------
"FTD balance is 0.4% of float" is a weak sentence, because "float" is not the
share count a forced buyer has to compete for. Reported float is everything not
locked up by insiders and affiliates. It still contains the index funds, the
pensions and the long-only mandates that will not sell into a two-week
close-out window at any price a short is willing to pay. Net those out and the
same fail balance can be several times larger against the shares actually
available.

THREE ARITHMETIC TRAPS THIS MODULE IS BUILT AROUND
--------------------------------------------------
1. `heldPercentInsiders` and `heldPercentInstitutions` are fractions of SHARES
   OUTSTANDING, not of float. Multiplying them by float overstates both.

2. Reported float ALREADY excludes insider and closely-held stock. Subtracting
   insiders again is a double-count that can halve the answer. This module
   checks whether float is consistent with shares_out x (1 - insiders) and only
   subtracts insiders when the reported float clearly still contains them.

3. 13F institutional totals can exceed the entire float, and routinely exceed
   100% of shares outstanding on heavily shorted names. That is not an error in
   the data -- a lent share is reported by the lender AND by whoever bought it
   from the short. It does mean the institutional number cannot be subtracted
   literally; it is capped at the float here, and the condition is flagged,
   because the same double-count is itself a squeeze tell.

THE ASSUMPTION, STATED PLAINLY
------------------------------
`locked_frac` is the share of institutional holdings assumed unavailable during
a close-out window. It is an ASSUMPTION, not a measurement. Nothing in this
repository has validated it against an outcome. 0.70 is the default because the
passive/index share of institutional ownership in US small caps is broadly in
that neighbourhood, but the honest output is the band, not the point estimate,
so every caller gets the 0.50 / 0.70 / 0.90 spread alongside the headline.

NOT WIRED INTO SCORING
----------------------
These numbers are reported, not scored. The FTD score gates in squeeze_deep.py
still fire on percent-of-REPORTED-float, so every row already in
squeeze_log.csv stays comparable with every row logged after this module
landed. Promote it into the score only when graded outcomes say the
effective-float version predicts better -- that is a question for
review_outcomes.py, not for a default.
"""

from typing import Optional

# Share of institutional holdings assumed not for sale inside a close-out
# window. Assumption, not measurement -- see module docstring.
DEFAULT_LOCKED_FRAC = 0.70

# The band reported alongside every point estimate, so nobody reads the
# headline as precise.
LOCKED_BAND = (0.50, 0.70, 0.90)

# Effective float is never reported below this share of reported float. Without
# a floor, a name with 95% institutional ownership divides by ~zero and every
# ratio downstream becomes an infinity that looks like a signal.
MIN_EFF_FRAC = 0.02


def _as_fraction(pct) -> Optional[float]:
    """Normalise an ownership figure to a decimal fraction.

    Accepts 0.65 and 65.0 alike. Values above 100% survive on purpose:
    institutional ownership genuinely exceeds 100% of shares outstanding on
    heavily lent names, and silently discarding that would delete the loudest
    datapoint on the page.
    """
    if pct is None:
        return None
    try:
        v = float(pct)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    if v > 2.0:                 # reported as 65.0 rather than 0.65
        v = v / 100.0
    if v > 1.5:                 # >150% of shares out -- unusable either way
        return None
    return v


def compute_effective_float(float_shares: Optional[float],
                            shares_outstanding: Optional[float],
                            pct_insiders=None,
                            pct_institutions=None,
                            locked_frac: float = DEFAULT_LOCKED_FRAC) -> dict:
    """Reported float minus the shares that will not trade.

    Returns a dict that is always safe to read: on missing inputs the effective
    float falls back to the reported float and `quality` says so, so callers
    never have to branch on None before formatting.
    """
    out = {
        'float_shares':         None,
        'shares_outstanding':   None,
        'insider_shares':       None,
        'institutional_shares': None,
        'inst_pct_of_float':    None,
        'effective_float':      None,
        'effective_float_band': {},
        'locked_frac':          locked_frac,
        'tightness':            None,     # reported float / effective float
        'floored':              False,
        # True when 13F holdings exceeded the float and the cap bound. That
        # matters downstream: once capped, effective float is exactly
        # float x (1 - locked_frac) — a CONSTANT multiple of the reported
        # float, carrying no name-specific information. Anything that scores
        # off effective float must check this flag, or it will read the same
        # constant on every heavily-lent name and mistake it for a signal.
        'inst_capped':          False,
        'quality':              'unavailable',
        'notes':                [],
    }

    try:
        flt = float(float_shares) if float_shares else None
        so = float(shares_outstanding) if shares_outstanding else None
    except (TypeError, ValueError):
        flt = so = None

    if not flt or flt <= 0:
        out['notes'].append("No float -- effective float cannot be computed")
        return out

    out['float_shares'] = flt
    out['shares_outstanding'] = so

    ins = _as_fraction(pct_insiders)
    inst = _as_fraction(pct_institutions)

    if not so or so <= 0:
        # Without shares outstanding the ownership percentages have no
        # denominator. Applying them to float instead would understate the
        # effective float, which is the direction that flatters the thesis --
        # so refuse rather than guess.
        out['effective_float'] = flt
        out['effective_float_band'] = {f: flt for f in LOCKED_BAND}
        out['tightness'] = 1.0
        out['quality'] = 'float_only'
        out['notes'].append(
            "No shares-outstanding figure -- ownership percentages have no "
            "denominator; falling back to reported float")
        return out

    insider_shares = ins * so if ins is not None else None
    inst_shares = inst * so if inst is not None else None
    out['insider_shares'] = insider_shares
    out['institutional_shares'] = inst_shares

    # -- Trap 2: is insider stock already out of the reported float? --
    base = flt
    if insider_shares:
        implied_ex_insiders = so * (1.0 - ins)
        if flt > implied_ex_insiders * 1.05:
            base = max(flt - insider_shares, 0.0)
            out['notes'].append(
                f"Reported float ({flt:,.0f}) exceeds shares outstanding net "
                f"of insiders ({implied_ex_insiders:,.0f}) -- insider stock "
                f"appears NOT to have been excluded, so it was subtracted here")
        else:
            out['notes'].append(
                f"Insider stock ({insider_shares:,.0f} sh, {ins:.1%} of shares "
                f"out) already excluded from reported float -- not subtracted "
                f"again")

    if inst_shares is None:
        out['effective_float'] = base
        out['effective_float_band'] = {f: base for f in LOCKED_BAND}
        out['tightness'] = flt / base if base > 0 else None
        out['quality'] = 'no_institutional'
        out['notes'].append(
            "No institutional ownership figure -- effective float equals "
            "reported float; treat the close-out percentages as an upper bound "
            "on available supply, not a squeeze read")
        return out

    out['inst_pct_of_float'] = inst_shares / base if base > 0 else None

    # -- Trap 3: 13F totals can exceed the float --
    inst_in_float = min(inst_shares, base)
    out['inst_capped'] = bool(inst_shares > base)
    if inst_shares > base and base > 0:
        out['notes'].append(
            f"13F institutional holdings ({inst_shares:,.0f} sh) EXCEED the "
            f"float ({base:,.0f} sh) -- {inst_shares / base:.2f}x. Each lent "
            f"share is reported twice (lender, and the buyer who bought it from "
            f"the short), so this is itself evidence of heavy lending. Capped "
            f"at the float for the arithmetic below")

    def _eff(lf: float) -> float:
        raw = base - lf * inst_in_float
        return max(raw, base * MIN_EFF_FRAC)

    band = {lf: _eff(lf) for lf in LOCKED_BAND}
    eff = _eff(locked_frac)

    out['effective_float'] = eff
    out['effective_float_band'] = band
    out['floored'] = eff <= base * MIN_EFF_FRAC * 1.0001
    out['tightness'] = flt / eff if eff > 0 else None
    out['quality'] = 'full'
    if out['floored']:
        out['notes'].append(
            f"Effective float hit the {MIN_EFF_FRAC:.0%}-of-float floor -- "
            f"ownership leaves almost nothing unlocked on paper. Read the "
            f"ratios as 'off the scale', not as their literal values")
    return out


def closeout_read(ftd_shares: Optional[float],
                  float_shares: Optional[float],
                  effective_float: Optional[float],
                  avg_daily_volume: Optional[float] = None) -> dict:
    """How substantial is this fail balance, measured three ways.

    Percent of float is the conventional number. Percent of EFFECTIVE float is
    the same fails against the shares a forced buyer can actually reach. Days of
    average volume is the one that survives contact with reality: a close-out is
    forced BUYING, and buying that takes a fraction of one session's volume does
    not move a price no matter how it reads as a percentage.
    """
    out = {'ftd_shares': None, 'pct_float': None, 'pct_eff_float': None,
           'adv_days': None, 'verdict': 'NO DATA', 'basis': ''}
    try:
        f = float(ftd_shares) if ftd_shares else None
    except (TypeError, ValueError):
        f = None
    if not f or f <= 0:
        return out
    out['ftd_shares'] = f

    if float_shares:
        out['pct_float'] = f / float(float_shares)
    if effective_float:
        out['pct_eff_float'] = f / float(effective_float)
    if avg_daily_volume:
        try:
            adv = float(avg_daily_volume)
            if adv > 0:
                out['adv_days'] = f / adv
        except (TypeError, ValueError):
            pass

    # Verdict leans on effective float where available, reported float
    # otherwise, and is DEMOTED when the volume read says the forced buying is
    # small against normal turnover.
    p = out['pct_eff_float']
    out['basis'] = 'effective float'
    if p is None:
        p = out['pct_float']
        out['basis'] = 'reported float'
    if p is None:
        return out

    if p >= 0.05:
        v = 'EXTREME'
    elif p >= 0.02:
        v = 'HEAVY'
    elif p >= 0.005:
        v = 'NOTABLE'
    else:
        v = 'NOISE'

    d = out['adv_days']
    if d is not None:
        if d < 0.10 and v in ('EXTREME', 'HEAVY'):
            v += ' (but <0.1 day of volume -- absorbable)'
        elif d >= 1.0 and v == 'NOTABLE':
            v = 'HEAVY (>1 full day of volume to close)'
    out['verdict'] = v
    return out


def format_block(eff: dict, co: Optional[dict] = None, indent: str = "  ") -> str:
    """Human-readable block for the analyzer output."""
    L = []
    if not eff or not eff.get('float_shares'):
        return indent + "Effective float: no float data\n"

    flt = eff['float_shares']
    L.append(f"{indent}EFFECTIVE FLOAT")
    L.append(f"{indent}   Reported float:      {flt:,.0f}")
    if eff.get('insider_shares') is not None:
        L.append(f"{indent}   Insider-held:        {eff['insider_shares']:,.0f}")
    if eff.get('institutional_shares') is not None:
        pctf = eff.get('inst_pct_of_float')
        pctf_s = f" ({pctf:.0%} of float)" if pctf is not None else ""
        L.append(f"{indent}   Institution-held:    "
                 f"{eff['institutional_shares']:,.0f}{pctf_s}")
    if eff.get('effective_float') is not None:
        # Only claim the locked-institutions assumption where it was actually
        # applied. On the degraded paths effective float IS reported float, and
        # printing an assumption that changed nothing reads as a subtraction
        # that never happened.
        assume = (f"  (assumes {eff['locked_frac']:.0%} of institutional "
                  f"holdings locked)" if eff.get('quality') == 'full'
                  else "  (= reported float — nothing subtracted)")
        L.append(f"{indent}   Effective float:     "
                 f"{eff['effective_float']:,.0f}{assume}")
    band = eff.get('effective_float_band') or {}
    if len(set(band.values())) > 1:
        lo = band[max(LOCKED_BAND)]
        hi = band[min(LOCKED_BAND)]
        L.append(f"{indent}   Assumption band:     {lo:,.0f} - {hi:,.0f} "
                 f"(90% - 50% locked)")
    if eff.get('tightness') and eff['tightness'] > 1.05:
        L.append(f"{indent}   Tightness:           "
                 f"{eff['tightness']:.1f}x smaller than reported float")
    L.append(f"{indent}   Data quality:        {eff.get('quality')}")
    for n in eff.get('notes', []):
        L.append(f"{indent}   . {n}")

    if co and co.get('ftd_shares'):
        L.append("")
        L.append(f"{indent}FTD CLOSE-OUT, SIZED")
        L.append(f"{indent}   Fail balance:        {co['ftd_shares']:,.0f} shares")
        if co.get('pct_float') is not None:
            L.append(f"{indent}   % of reported float: {co['pct_float']:.2%}")
        if co.get('pct_eff_float') is not None:
            L.append(f"{indent}   % of EFFECTIVE float:{co['pct_eff_float']:.2%}")
        if co.get('adv_days') is not None:
            L.append(f"{indent}   Days of avg volume:  {co['adv_days']:.2f}")
        L.append(f"{indent}   Read:                {co['verdict']} "
                 f"(on {co['basis']})")
    return "\n".join(L) + "\n"


def for_ticker(ticker: str, locked_frac: float = DEFAULT_LOCKED_FRAC) -> dict:
    """Fetch + compute in one call. Returns {'eff':.., 'closeout':.., 'info':..}."""
    from data_validator import fetch_validated_info
    v = fetch_validated_info(ticker)
    eff = compute_effective_float(
        v.get('floatShares'), v.get('sharesOutstanding'),
        v.get('heldPercentInsiders'), v.get('heldPercentInstitutions'),
        locked_frac=locked_frac)
    co = closeout_read(
        v.get('ftdShares'), v.get('floatShares'), eff.get('effective_float'),
        v.get('averageVolume10days') or v.get('averageVolume'))
    return {'eff': eff, 'closeout': co, 'info': v}


if __name__ == "__main__":
    import sys
    tickers = [t.upper() for t in sys.argv[1:]] or ["GME"]
    for tk in tickers:
        print("\n" + "=" * 60 + f"\n{tk}\n" + "=" * 60)
        try:
            r = for_ticker(tk)
            print(format_block(r['eff'], r['closeout']))
            q = r['info'].get('ftdDataQuality')
            if q and q != 'live':
                print(f"  (FTD data quality: {q})")
        except Exception as e:
            print(f"  failed: {e}")
