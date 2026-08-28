"""
verify_short_data.py
====================
Adjudicate short-interest disagreements between this toolkit and any website,
by putting every independent source side by side.

WHY
---
Short interest looks like one number and is actually four decisions: which
settlement date, whose share count, whose float, and which volume window. Two
sites can both be "right" and differ by 3x. Rather than argue, print the
sources.

    python verify_short_data.py TASK

Sources, all free and independent:
  FINRA  consolidated short interest — the regulatory filing, all listings
  NASDAQ short interest table        — same data, Nasdaq-listed only
  yfinance info fields               — what the toolkit used to rely on
  SEC EDGAR EntityPublicFloat        — the company's own filed public float,
                                       in dollars, from the 10-K cover page

A worked example, TASK on 2026-08-26, where a website showed 21.51% short
interest against this toolkit's 8.0%:

  shares short   2,286,112 (FINRA + NASDAQ, settlement 08-14) vs the site's
                 4,036,547, which is the 07-31 settlement — one period stale
  float          28.7M (yfinance) corroborated by SEC public float implying
                 26.5M; the site's implied float was 18.8M, 29% lower
  the two errors compound: 1.77 x 1.53 = 2.7, and 21.51 / 7.96 = 2.70

Neither number was invented. One was simply older and measured against a
smaller denominator.
"""

import json
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta

SEC_UA = "squeeze-toolkit short-interest verification"
_ctx_cache = None


def _ctx():
    global _ctx_cache
    if _ctx_cache is None:
        try:
            import certifi
            _ctx_cache = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ctx_cache = False
    return _ctx_cache or None


def _get(url, headers=None, data=None, timeout=25):
    req = urllib.request.Request(
        url, data=data,
        headers=headers or {"User-Agent": "Mozilla/5.0",
                            "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def finra(ticker):
    """Official settlements, all listings. Includes revisionFlag — FINRA does
    restate, and a restated period explains a lot of otherwise baffling jumps."""
    today = datetime.now().date()
    body = json.dumps({
        "compareFilters": [{"fieldName": "symbolCode",
                            "fieldValue": ticker, "compareType": "EQUAL"}],
        "dateRangeFilters": [{"fieldName": "settlementDate",
                              "startDate": (today - timedelta(days=200)).isoformat(),
                              "endDate": (today + timedelta(days=30)).isoformat()}],
        "limit": 30}).encode()
    try:
        rows = _get("https://api.finra.org/data/group/otcMarket/name/"
                    "consolidatedShortInterest", data=body,
                    headers={"User-Agent": "Mozilla/5.0",
                             "Accept": "application/json",
                             "Content-Type": "application/json"})
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    rows.sort(key=lambda r: r.get("settlementDate", ""), reverse=True)
    return {"rows": rows[:4]}


def nasdaq(ticker):
    try:
        d = _get(f"https://api.nasdaq.com/api/quote/{ticker}/short-interest"
                 f"?assetClass=stocks")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if not d.get("data"):
        return {"error": d.get("message") or "no data"}
    return {"rows": (d["data"].get("shortInterestTable") or {}).get("rows", [])[:4]}


def yahoo(ticker):
    try:
        import yfinance_throttle  # noqa: F401
        import yfinance as yf
        i = yf.Ticker(ticker).info
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return {k: i.get(k) for k in
            ("sharesShort", "sharesShortPriorMonth", "dateShortInterest",
             "floatShares", "sharesOutstanding", "averageVolume10days",
             "averageVolume", "shortRatio", "shortPercentOfFloat")}


def sec_float(ticker):
    """The company's OWN public float, filed on the 10-K cover, in dollars.
    Converted to shares at the price on the measurement date. This is the only
    float figure here that comes from the issuer rather than a vendor."""
    try:
        tk = _get("https://www.sec.gov/files/company_tickers.json",
                  headers={"User-Agent": SEC_UA, "Accept": "application/json"})
        cik = next((v["cik_str"] for v in tk.values()
                    if v["ticker"].upper() == ticker.upper()), None)
        if cik is None:
            return {"error": "ticker not in SEC index"}
        facts = _get(f"https://data.sec.gov/api/xbrl/companyfacts/"
                     f"CIK{cik:010d}.json",
                     headers={"User-Agent": SEC_UA, "Accept": "application/json"})
        pf = facts["facts"]["dei"]["EntityPublicFloat"]["units"]["USD"]
        pf.sort(key=lambda v: v.get("end", ""), reverse=True)
        newest = pf[0]
        out = {"as_of": newest["end"], "usd": newest["val"],
               "form": newest.get("form"), "cik": cik}
        try:
            import yfinance as yf
            h = yf.Ticker(ticker).history(start=newest["end"], period="1mo")
            if h is not None and not h.empty:
                px = float(h["Close"].iloc[0])
                out["price"] = px
                out["implied_shares"] = newest["val"] / px if px else None
        except Exception:
            pass
        return out
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _n(v, w=14):
    return f"{v:>{w},.0f}" if isinstance(v, (int, float)) else f"{'—':>{w}}"


def report(ticker):
    ticker = ticker.upper()
    print("=" * 74)
    print(f"  SHORT-INTEREST AUDIT — {ticker}   {datetime.now():%Y-%m-%d %H:%M}")
    print("=" * 74)

    f = finra(ticker)
    print("\nFINRA consolidated short interest (regulatory filing, all listings)")
    if f.get("error"):
        print("   unavailable:", f["error"])
    else:
        print(f"   {'settlement':<12}{'short interest':>16}{'avg volume':>14}"
              f"{'DTC':>8}  revised")
        for r in f["rows"]:
            print(f"   {r.get('settlementDate',''):<12}"
                  f"{_n(r.get('currentShortPositionQuantity'),16)}"
                  f"{_n(r.get('averageDailyVolumeQuantity'),14)}"
                  f"{(r.get('daysToCoverQuantity') or 0):>8.2f}"
                  f"  {r.get('revisionFlag') or '-'}")

    n = nasdaq(ticker)
    print("\nNASDAQ short interest table (Nasdaq-listed only)")
    if n.get("error"):
        print("   unavailable:", n["error"])
    else:
        for r in n["rows"]:
            print(f"   {r.get('settlementDate',''):<12}"
                  f"{r.get('interest',''):>16}"
                  f"{r.get('avgDailyShareVolume',''):>14}"
                  f"{float(r.get('daysToCover') or 0):>8.2f}")

    y = yahoo(ticker)
    print("\nyfinance fields (what the toolkit reads for float and volume)")
    if y.get("error"):
        print("   unavailable:", y["error"])
    else:
        ds = y.get("dateShortInterest")
        if isinstance(ds, (int, float)):
            ds = datetime.utcfromtimestamp(ds).strftime("%Y-%m-%d")
        print(f"   sharesShort         {_n(y.get('sharesShort'))}   "
              f"(settlement {ds})")
        print(f"   floatShares         {_n(y.get('floatShares'))}")
        print(f"   sharesOutstanding   {_n(y.get('sharesOutstanding'))}")
        print(f"   avgVolume10days     {_n(y.get('averageVolume10days'))}")
        print(f"   avgVolume (3mo)     {_n(y.get('averageVolume'))}")
        print(f"   shortPercentOfFloat {str(y.get('shortPercentOfFloat')):>14}"
              f"   <- Yahoo's own, often stale")

    s = sec_float(ticker)
    print("\nSEC EDGAR — the issuer's own filed public float")
    if s.get("error"):
        print("   unavailable:", s["error"])
    else:
        print(f"   as of {s['as_of']} ({s.get('form')})   "
              f"${s['usd']:,.0f}")
        if s.get("implied_shares"):
            print(f"   at ${s['price']:.2f} that implies "
                  f"{s['implied_shares']:,.0f} float shares")

    # ── the adjudication ──
    print("\n" + "-" * 74)
    if not f.get("error") and f.get("rows"):
        cur = f["rows"][0]
        si = cur.get("currentShortPositionQuantity")
        settle = cur.get("settlementDate")
        age = ""
        try:
            age = (f" ({(datetime.now().date() - datetime.strptime(settle, '%Y-%m-%d').date()).days}"
                   f" days old)")
        except Exception:
            pass
        print(f"  CURRENT short interest: {si:,.0f} as of {settle}{age}")
        print(f"  Days to cover: {cur.get('daysToCoverQuantity')} — the exchange's"
              f" own, using that period's average volume")
        floats = []
        if not y.get("error") and y.get("floatShares"):
            floats.append(("yfinance", y["floatShares"]))
        if s.get("implied_shares"):
            floats.append((f"SEC {s['as_of']}", s["implied_shares"]))
        for label, fl in floats:
            print(f"  SI% of float using {label:<18} {si/fl:>7.2%}"
                  f"   (float {fl:,.0f})")
        if len(f["rows"]) > 1:
            prev = f["rows"][1]
            print(f"\n  If a site shows {prev.get('currentShortPositionQuantity'):,.0f} "
                  f"shares, it is reporting the {prev.get('settlementDate')} "
                  f"settlement — one period behind.")
    print("-" * 74)
    print("  A site differing on SI% is usually differing on FLOAT, not on")
    print("  short interest. Compare its implied float against the SEC line.")


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    for t in (sys.argv[1:] or ["TASK"]):
        report(t)
        print()
