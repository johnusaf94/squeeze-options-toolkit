"""
stock_universe.py
==================
Tiered universe of QUALITY-INVESTING candidates for the Stock Searcher.

This is a DIFFERENT universe from squeeze_universe.py — that one targets
high-short-interest names. This one targets durable-moat compounders,
quality dividend payers, and proven growth franchises — the opposite
profile.

Four tiers in increasing breadth:
  T1: "The compounders" — ~50 names with proven durable moats
       (the Buffett/Munger universe of high-ROIC, wide-moat businesses)
  T2: + S&P 100 large caps                          → ~150 names total
  T3: + S&P 500 broader                             → ~500 names total
  T4: + dividend aristocrats + key growth franchises → ~650 names total

Selection criteria favor names with:
  - Multi-year financial history (so trend analysis works)
  - Reliable yfinance data coverage (high-attention names)
  - Real fundamental anchors (revenue, FCF, ROIC, margins)
  - Established business models (not pre-revenue speculation)
"""

# ─────────────────────────────────────────────
# T1: THE COMPOUNDERS — proven durable moats
# These are the names Buffett-style investors anchor portfolios around:
# consistently high ROIC, wide moats, decades of compounding.
# ─────────────────────────────────────────────
TIER_1 = [
    # Consumer staples — recession-resistant moats
    "KO","PEP","PG","CL","KMB","CHD","MKC","CLX","COST","WMT",
    "PM","MO","DEO","BUD",

    # Consumer discretionary moats
    "HD","LOW","NKE","SBUX","MCD","CMG","TJX","ROST","ORLY","AZO",
    "DPZ","YUM","ULTA","LULU",

    # Healthcare — pricing power + repeat revenue
    "JNJ","ABBV","PFE","MRK","LLY","UNH","TMO","DHR","ABT","BMY",
    "GILD","AMGN","ISRG","VRTX","REGN","ELV","CI","HUM","CVS",
    "MDT","SYK","BSX","EW","IDXX","ZTS","BDX","BAX","RMD",

    # Financials — durable franchises
    "BRK-B","JPM","BAC","V","MA","AXP","SPGI","MCO","ICE","CME",
    "MMC","AON","TRV","ALL","CB","PGR","HIG","BLK","SCHW","MS",

    # Industrials & defense — embedded moats
    "BA","LMT","RTX","NOC","GD","CAT","DE","HON","MMM","ITW",
    "EMR","ETN","ROK","PH","ROP","FTV","OTIS","CARR","JCI","LIN",
    "APD","SHW","ECL","NEM",

    # Technology — modern moats (high switching cost, network effects)
    "MSFT","AAPL","GOOG","GOOGL","META","ADBE","CRM","ORCL","INTU","NOW",
    "ACN","CSCO","TXN","AVGO","QCOM","AMAT","ASML","LRCX","KLAC",
    "ANET","FTNT","PANW","CRWD","SNOW","DDOG","CDNS","SNPS","MSCI",
    "NVDA","AMD",

    # Real estate (REITs with durable demand)
    "AMT","PLD","EQIX","O","SPG","WELL","PSA","CCI","DLR","VTR",

    # Energy majors (cash-generative durables)
    "XOM","CVX","COP","EOG","SLB","PSX","MPC","VLO",

    # Communication services
    "DIS","NFLX","CMCSA","T","VZ","TMUS",

    # Other recognized compounders / specialty names
    "TSCO","FAST","ADP","PAYX","ECL","WM","RSG","WCN","UNP","NSC",
    "CSX","FDX","UPS","ABBV","BIIB",
]

# ─────────────────────────────────────────────
# T2: + S&P 100 quality names (large-cap broad)
# Names not yet in T1 but reliably tracked and large enough that
# yfinance data quality is high.
# ─────────────────────────────────────────────
TIER_2_EXTRA = [
    "WFC","C","GS","USB","PNC","TFC","COF","DFS","BK","STT",
    "AIG","MET","PRU","AFL","TROW","BEN","NTRS","IVZ",
    "CAH","MCK","ABC","HCA","CNC","UHS","DGX","LH","ALGN","CTLT",
    "PXD","OXY","HAL","BKR","KMI","OKE","WMB","ENB","TRP","SU",
    "DOW","DD","PPG","NUE","STLD","X","FCX","RIO","BHP",
    "PCAR","CMI","GE","HII","TXT","LHX","TDG","BR","FIS","FISV",
    "ETSY","EBAY","BKNG","ABNB","UBER","LYFT","DASH","CHWY","W",
    "EXPE","TRIP","MAR","HLT","H","RCL","CCL","NCLH","WYNN","MGM","LVS",
    "GIS","HSY","K","SJM","CAG","CPB","TSN","HRL","KHC",
    "EL","COTY","BBY","KSS","M","JWN","TGT","DLTR","DG","FIVE",
    "AAP","AN","KMX","CVNA","DRI","CAKE","TXRH","WING","SHAK",
    "F","GM","STLA","TM","HMC","NSANY","TSLA","RIVN","LCID","NIO","XPEV",
]

# ─────────────────────────────────────────────
# T3: + broader S&P 500 quality
# Smaller large caps + mid-caps with multi-year operating history.
# ─────────────────────────────────────────────
TIER_3_EXTRA = [
    "ZBH","HOLX","ILMN","MTD","WAT","WST","TFX","COO","STE","PKI",
    "INCY","BIO","CRL","IQV","A","DXCM","BMRN","ALNY","SGEN","TECH",
    "ROL","MAS","FBHS","WHR","PHM","DHI","LEN","TOL","KBH","LGIH",
    "NVR","MTH","TMHC","BLD","BLDR","ADT","ALLE","MLM","VMC","EXP",
    "USG","SUM","SAM","STZ","TAP","BF-B","FIZZ","CELH","MNST","KDP",
    "WLK","EMN","FMC","CE","ASH","OLN","CTVA","SMG","CF","MOS","NTR",
    "BOH","CMA","CFR","FHN","SNV","HBAN","CFG","RF","FNB","EWBC","WAL",
    "KEY","ZION","FITB","MTB","HWC","UMBF","UCBI","FFIN","TCBI","BANR",
    "PLD","EQR","ESS","AVB","MAA","UDR","AIV","INVH","SUI","ELS","REG",
    "FRT","ROIC","BRX","KIM","KRG","SLG","BXP","HIW","CUZ","DEI","JBGS",
    "EXR","CUBE","NSA","LSI","SBAC","SUI","DRE","STAG","FR","REXR",
    "BRO","WLTW","RGA","RNR","ESGR","ATH","AMBC","MTG","RDN","ESNT",
    "JEF","RJF","LPLA","HLI","MC","EVR","PJT","LAZ","COWN","SF",
    "GPN","FLT","WU","WEX","SQ","PYPL","AFRM","SOFI","UPST","HOOD",
    "ROK","FAST","WPP","PWR","ACM","J","MTZ","FLR","KBR","DY","PRIM",
    "WAB","PCAR","ALK","DAL","UAL","AAL","JBLU","LUV","SAVE","ALGT",
    "EXPD","CHRW","ODFL","XPO","JBHT","ARCB","SAIA","KNX","HUBG",
    "ACI","KR","BG","ADM","INGR","AGCO","DE","CNHI","LNN",
    "WSM","RH","DECK","SKX","CROX","BIRK","CRI","CHS","BURL","SCVL",
    "EW","ETSY","FND","BBWI","BJ","SFM","WMK","CASY","FRSH","ALKT",
    "TYL","JKHY","SSNC","WDAY","TEAM","ZS","NET","OKTA","TWLO","DOCU",
    "ZM","RBLX","U","PATH","AI","BILL","COIN","HUBS","VEEV","PCTY",
    "WK","ESTC","CFLT","MDB","FSLY","DT","NEWR","DOMO","FROG","BAND",
]

# ─────────────────────────────────────────────
# T4: + dividend aristocrats + growth franchises
# Names that round out the universe with strong income or strong growth
# stories not captured above.
# ─────────────────────────────────────────────
TIER_4_EXTRA = [
    # Dividend kings/aristocrats not already included
    "GPC","SWK","LEG","CINF","FRT","NWN","TGT","SYY","ATO","NJR",
    "BANF","CWT","GWW","SON","UVV","FMCB","HRL","NDSN","DOV","BEN",
    "VFC","CAH","BDX","ABM","ROP","XYL","WTRG","ESS","O","SPG",
    # International ADRs (large, liquid)
    "TSM","BABA","JD","PDD","NIO","XPEV","LI","BIDU","TME","BILI",
    "SE","MELI","NU","STNE","PAGS","VALE","ITUB","BBD","VIST","GGB",
    "SAP","SHOP","SPOT","NVO","NVS","RHHBY","AZN","GSK","BTI","RIO",
    "BHP","NTES","WIT","INFY","HDB","IBN","RDY","SHG","KB","WBK",
    # Specialty growth franchises
    "ASML","SMCI","NVDA","AMD","ARM","MRVL","ENPH","FSLR","RUN","STEM",
    "PLUG","CHPT","BLNK","BE","FCEL","ALB","LTHM","SQM","MP","REE",
    "PLTR","NET","CFLT","MDB","DDOG","S","ZS","CRWD","PANW","FTNT",
    "BILL","HUBS","VEEV","PCTY","INTU","TYL","ANET","NTAP","JNPR",
]


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

def _dedupe(seq):
    """Remove duplicates while preserving order."""
    seen = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def get_universe(tier_max: int = 2, limit: int = None) -> list:
    """
    Return the universe of tickers up to and including `tier_max`.

      tier_max=1  → ~125 compounders
      tier_max=2  → ~230 large caps
      tier_max=3  → ~480 broader S&P
      tier_max=4  → ~650 full quality universe

    `limit` truncates the result (useful for testing/quick scans).
    """
    tier_max = max(1, min(4, int(tier_max)))
    out = list(TIER_1)
    if tier_max >= 2:
        out += TIER_2_EXTRA
    if tier_max >= 3:
        out += TIER_3_EXTRA
    if tier_max >= 4:
        out += TIER_4_EXTRA
    out = _dedupe(out)
    if limit and limit > 0:
        out = out[:limit]
    return out


def get_universe_size() -> dict:
    """Return the actual size of each tier (after dedup) for UI display."""
    return {
        1: len(_dedupe(TIER_1)),
        2: len(_dedupe(TIER_1 + TIER_2_EXTRA)),
        3: len(_dedupe(TIER_1 + TIER_2_EXTRA + TIER_3_EXTRA)),
        4: len(_dedupe(TIER_1 + TIER_2_EXTRA + TIER_3_EXTRA + TIER_4_EXTRA)),
    }


if __name__ == "__main__":
    sizes = get_universe_size()
    print("Stock universe sizes by tier:")
    for tier, n in sizes.items():
        print(f"  T{tier}: {n} tickers")
