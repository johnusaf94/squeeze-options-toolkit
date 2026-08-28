"""
squeeze_universe.py
====================
Comprehensive hardcoded universe of US equities for squeeze scanning.

Organized in tiers by squeeze probability (scan Tier 1 first):

TIER 1 — Chronic squeeze candidates (~200 tickers)
  Small/micro cap with known persistent short interest.
  Biotech, EV, crypto-adjacent, speculative tech.
  These are historically the most frequent squeeze setups.

TIER 2 — Russell 2000 small caps (~800 tickers)
  Full small cap universe. Most squeezes originate here.
  Lower market cap = smaller float = easier to squeeze.

TIER 3 — Mid cap momentum names (~500 tickers)
  Growth stocks with institutional interest and potential
  for short crowding when narratives shift.

TIER 4 — S&P500 large caps (~503 tickers)
  Rare squeezes but included for completeness.
  Occasionally heavily shorted defensively.

Usage:
    from squeeze_universe import get_universe
    tickers = get_universe(tier_max=2, limit=500)
"""

from typing import List

# ─────────────────────────────────────────────────────────────────────────────
# TIER 1 — Chronic squeeze candidates (scan first)
# Known for persistent elevated short interest, retail attention,
# or structural characteristics that create squeeze conditions.
# ─────────────────────────────────────────────────────────────────────────────
TIER_1 = [
    # Active watchlist / live trade names (added May 2026)
    # These are surfaced by T2+ scans but kept in T1 for reliable
    # catalyst tracking without triggering yfinance throttle.
    "ASAN","BRZE","CNXC","CRMD","SOUN","TGTX","EVGO","RUN","NVAX",
    "UPST","CADL","ANAB","APLD","CELC",

    # Crypto / blockchain adjacent
    "MARA","RIOT","CLSK","HUT","BTBT","CIFR","GREE","HIVE","MSTR","COIN",
    "CANO","ARBK","BITF","SOS","BTCM","DMGI","SDIG","CORZ","IREN","WULF",

    # EV / mobility — high short interest sector
    "NKLA","RIVN","LCID","GOEV","WKHS","FSR","SOLO","ARVL","AYRO","BLNK",
    "CHPT","EVGO","VLCN","RIDE","MULN","FFIE","CENN","PTRA","XPEV","LI",
    "NIO","HYZN","HYLN","IDEX","SUNL","SOLO","EVTV","KNDI","TANH","AYRO",

    # Biotech / pharma — highest SI sector
    "SAVA","ACAD","NVAX","OCGN","AGEN","OTIC","ATNX","MNKD","SRNE","INVA",
    "CLOV","VNET","SNDL","TLRY","APHA","CGC","ACB","CRON","HEXO","OGI",
    "CRBP","BNGO","NKTR","AMRN","INFU","MRSN","XBIO","TGTX","ALDX","AGIO",
    "PRTA","SRRK","VKTX","RETA","BPMC","ABCL","VCEL","IMVT","ARQT","DCPH",
    "DNLI","PMVP","KYMR","NRIX","PRAX","ELVN","ARCT","BLUE","FATE","BEAM",
    "NTLA","CRSP","EDIT","VERV","GRPH","RCUS","ALEC","TVTX","INBX","HALO",

    # Meme / retail favorites
    "GME","AMC","BBBY","KOSS","EXPR","CLOV","WISH","SPCE","DKNG","PLBY",
    "NAKD","SNDL","BB","NOK","CTRM","SENS","CIDM","CTXR","ONVO","ZSAN",

    # SPAC / recent IPO high-SI
    "JOBY","ACHR","LILM","EVTL","BARK","BODY","BFLY","OPAD","DKNG","PSFE",
    "GOCO","MVST","XOS","ELMS","REE","GFAI","OUST","AEYE","MVIS","LIDR",

    # Short-attacked / contested companies
    "HIMS","OPEN","OFSG","PRCH","SKLZ","PAYO","AFRM","UPST","LMND","ROOT",
    "METC","JMIA","WISH","RDFN","CVNA","OPENDOOR","FIGS","BROS","DNUT",
    "BBAI","SOUN","TALK","GFAI","AEAC","BRPM","GTLB","ALCC","ZING","BZFD",

    # High-beta tech with short crowding
    "PLTR","HOOD","SOFI","COUR","DUOL","BIRD","MAPS","TPVG","QNST","EOSE",
    "OZON","GRAB","SE","MNDY","DDOG","NET","SNOW","ZS","MDB","ESTC",
]

# ─────────────────────────────────────────────────────────────────────────────
# TIER 2 — Russell 2000 small caps (primary squeeze hunting ground)
# Market cap roughly $300M - $2B. Small float = squeeze amplification.
# ─────────────────────────────────────────────────────────────────────────────
TIER_2 = [
    # Small cap biotech / healthcare
    "ACRS","ADMA","ADTX","AEZS","AFMD","AGFS","AGMH","AHCO","AILE","AKBA",
    "AKRO","AKTS","ALBT","ALCO","ALEC","ALEX","ALGT","ALIM","ALKS","ALLK",
    "ALNY","ALRM","ALRS","ALSA","ALTM","ALVO","AMAG","AMBO","AMCX","AMGN",
    "AMKR","AMPH","AMRK","AMRN","AMSC","AMTB","AMTX","AMWL","ANAB","ANIK",
    "ANNA","ANPC","ANSS","ANTX","ANVS","AORT","APDN","APLD","APLT","APOG",
    "APRE","APVO","APWC","AQMS","AQNB","AQUA","ARAV","ARCO","ARCT","ARDC",
    "ARDX","AREC","ARGX","AROW","ARPO","ARQQ","ARRY","ARTE","ARTL","ARTNA",
    "ARTW","ARWR","ASAI","ASIX","ASND","ASNS","ASPS","ASRT","ASST","ASTE",
    "ATAX","ATCX","ATEC","ATEN","ATEX","ATHA","ATHX","ATIF","ATKR","ATLO",
    "ATMC","ATNF","ATNI","ATRC","ATRI","ATSG","ATXS","ATYR","AUBN","AULT",

    # Small cap tech / software
    "AVDL","AVEO","AVGR","AVID","AVIR","AVNS","AVNW","AVRO","AVTE","AVXL",
    "AWRE","AXDX","AXGN","AXLA","AXNX","AXSM","AXTL","AXTI","AYTU","AZEK",
    "AZPN","AZRE","AZTA","AZUL","BAND","BANF","BANR","BARK","BCEL","BCLI",
    "BCML","BCOV","BCPC","BCRX","BCSA","BCTG","BCYC","BDNX","BDSX","BDTX",
    "BEEM","BFAC","BFAM","BFLY","BFRI","BGNE","BGRY","BGSX","BGXX","BHIL",
    "BHTG","BIKE","BIMI","BIOF","BIOR","BIOS","BIOX","BJRI","BKBK","BKNG",
    "BKSC","BKSY","BKTI","BKYI","BLBD","BLBX","BLCM","BLCO","BLCT","BLDE",
    "BLDP","BLDR","BLEN","BLFS","BLFY","BLGX","BLIN","BLKB","BLNK","BLPH",
    "BLRX","BLSA","BLTE","BLTS","BLUA","BLUN","BLYQ","BMBL","BMEA","BMIX",
    "BMRA","BMRN","BMTC","BNAI","BNCO","BNGO","BNIX","BNRG","BNSO","BNTC",

    # Small cap industrials / energy
    "BNTX","BOCH","BODY","BOLT","BOMN","BOOM","BORR","BOTJ","BOWX","BPMC",
    "BPTH","BPYP","BRBR","BRBS","BRCN","BRDS","BREZ","BRID","BRKL","BRLIR",
    "BROG","BRTX","BRWM","BRZE","BSFC","BSRR","BSVN","BTAI","BTCS","BTEK",
    "BTER","BTHM","BTMD","BTOG","BTRN","BTRS","BTTX","BTUS","BTWN","BULD",
    "BURL","BURP","BUSE","BVFL","BVNK","BWAY","BWEN","BWFG","BWMX","BWXT",
    "BYFC","BYND","BYRN","BYSI","BZFD","BZUN","CAAS","CABA","CABO","CACC",
    "CACH","CACO","CADE","CADL","CAKE","CALA","CALB","CALM","CALT","CALX",
    "CAMP","CAMT","CANF","CANO","CAPL","CAPR","CARA","CARE","CARG","CARM",
    "CARO","CARV","CASM","CASI","CASS","CATB","CATC","CATX","CATO","CATS",
    "CAUD","CAVA","CBAT","CBAY","CBFV","CBIO","CBLI","CBMG","CBNK","CBOE",

    # More small caps
    "CBPO","CBRL","CBSH","CBTX","CCAP","CCIX","CCLD","CCLP","CCNC","CCNE",
    "CCOI","CCRD","CCRN","CCSI","CCTS","CDMO","CDNA","CDNS","CDRE","CDRO",
    "CDTG","CDXC","CDXS","CDZI","CELC","CELH","CELL","CELU","CELZ","CEMI",
    "CENT","CERE","CERS","CERT","CEVA","CFBK","CFFI","CFFE","CFFN","CFFS",
    "CFLT","CFNB","CGEM","CGNT","CGNX","CGON","CGRN","CGRO","CGTX","CHCI",
    "CHCO","CHCT","CHDN","CHEF","CHEK","CHGG","CHKP","CHMG","CHNG","CHPT",
    "CHRS","CHTR","CHUY","CHWY","CIAN","CIFR","CIGI","CIGP","CIIG","CILK",
    "CINC","CINT","CIVB","CIVC","CIVF","CIZN","CJET","CKPT","CKTX","CLAR",
    "CLBT","CLBZ","CLFD","CLGN","CLII","CLIR","CLLS","CLMT","CLNC","CLNE",
    "CLNN","CLOA","CLPS","CLPT","CLRB","CLRO","CLSD","CLSN","CLST","CLVS",

    # Additional Russell 2000 names
    "CLWT","CLXT","CMBT","CMCL","CMCO","CMCT","CMGE","CMLS","CMND","CMPO",
    "CMPR","CMPS","CMRA","CMRE","CMRX","CMTG","CMTL","CNCE","CNCG","CNDB",
    "CNDT","CNFINANCE","CNGL","CNHI","CNMD","CNNE","CNOB","CNOB","CNPX",
    "CNSL","CNSP","CNTB","CNTG","CNTY","CNVS","CNXC","CNXN","COCP","CODX",
    "COEP","COHR","COHN","COHU","COKE","COMS","CONN","CONX","COOK","COOP",
    "COPX","CORR","CORS","COSM","COST","COUR","COVS","CPAA","CPBI","CPIX",
    "CPLP","CPNG","CPOP","CPRX","CPSS","CPTN","CPUH","CRAI","CRBP","CRBU",
    "CRCT","CREV","CREX","CRGE","CRGX","CRGY","CRIS","CRIX","CRKN","CRMD",
    "CRMT","CRNT","CRNX","CROC","CRON","CROP","CROS","CROX","CRSP","CRSS",
    "CRTO","CRTX","CRUS","CRVO","CRWD","CRWS","CSBR","CSCW","CSGP","CSGS",

    # Small cap finance / real estate
    "CSII","CSIQ","CSLM","CSLT","CSOD","CSSE","CSTE","CSTM","CSTR","CTBI",
    "CTCX","CTDH","CTGO","CTIB","CTLT","CTMX","CTNM","CTOS","CTRE","CTRI",
    "CTRL","CTRM","CTRN","CTSH","CTSO","CTVA","CTXR","CTXS","CTYR","CUBE",
    "CUDA","CULD","CULP","CURO","CUTR","CUVA","CVAC","CVBF","CVCO","CVCY",
    "CVEO","CVGW","CVKD","CVLB","CVLT","CVLY","CVNX","CVOR","CVRS","CVSA",
]

# ─────────────────────────────────────────────────────────────────────────────
# TIER 3 — Mid cap growth / momentum names
# $2B-$20B market cap. Institutional short crowding happens here.
# ─────────────────────────────────────────────────────────────────────────────
TIER_3 = [
    # Fintech / payments
    "AFRM","UPST","SOFI","HOOD","LMND","ROOT","CLOV","OPEN","PTON","BYND",
    "DASH","LYFT","UBER","ABNB","RDFN","CVNA","OPAD","FLUT","DAVE","MOGO",
    "RELY","FICO","SMAR","BRZE","GTLB","DDOG","NET","ZS","CRWD","OKTA",

    # E-commerce / consumer
    "ETSY","CHWY","W","RH","RVLV","XMTR","FIGS","BIRD","SSYS","DDD","NKLA",
    "PUBM","MGNI","TTD","IAS","HUBS","SPRK","BLND","BFAM","PRTS","REAL",
    "EVER","LPSN","HIMS","NTRA","TDOC","AMWL","ONEM","ACCD","PHR","TALK",

    # Cloud / SaaS
    "BOX","FSLY","PUBM","NCNO","ENFN","SMAR","JAMF","ALTR","AZUL","BIGC",
    "WEBR","CDAY","PDFS","TNET","COUP","PING","SPSC","PAYC","PCTY","MNTV",
    "VNET","WIX","MNDY","DOCN","TASK","AMPL","RENT","RAMP","KPLT","ASAN",
    "PD","BILL","FIVN","QTWO","PEGA","AVLR","NCNO","APPN","YEXT","ALRM",

    # Healthcare tech
    "ACCD","NTRA","GDRX","ONEM","HIMS","PHR","AMWL","TDOC","LVGO","OMCL",
    "INSP","TNDM","PODD","DXCM","ITGR","INMD","NVCR","NVST","HCAT","MDRX",

    # Energy transition
    "PLUG","BE","FCEL","BLDP","EVGO","CHPT","BLNK","NKLA","WKHS","FSR",
    "SPWR","ENPH","SEDG","ARRY","RUN","NOVA","SHLS","STEM","NRDY","GEVI",
    "AMRC","FTCI","CWEN","NEP","HASI","AMPS","GEVO","AMTX","REX","BTAI",

    # Semiconductors / hardware
    "WOLF","MVIS","AMBA","FORM","SMTC","COHU","ACLS","ONTO","ICHR","KLIC",
    "CEVA","POWI","DIOD","SITM","NVEC","SIMO","PSEC","IIPR","GNLN","GRWG",
    "AEHR","OLED","ALGM","XPEL","OSIS","TTMI","VIAV","CRUS","MTSI","ENVA",

    # Retail / restaurants
    "JACK","RRGB","CBRL","DNUT","BROS","BJ","PLAY","EAT","CAKE","TXRH",
    "WING","SHAK","DINE","FAT","LOCO","KRUS","PTON","XPOF","PLNT","SFIX",
    "FIVE","PRTY","BOOT","CROX","YETI","VGT","BIRD","ON","LULU","NKE",

    # Real estate / REITs with shorts
    "OPEN","RDFN","COMP","HOUS","DOMA","BETR","SMRT","LADR","GPMT","BXMT",
    "TRTX","LOAN","KREF","ACR","SACH","AOMR","NREF","RC","STWD","BFAM",

    # Industrials with momentum
    "ACHR","JOBY","LILM","EVTL","WKHS","BLBD","ERII","GATO","SLCA","NVRI",
    "CLF","STLD","CMC","RS","ZEUS","TS","TX","VALE","MP","FWRD","SAIA",
]

# ─────────────────────────────────────────────────────────────────────────────
# TIER 4 — S&P500 large caps
# Occasional high-SI situations (defensive shorts, activist targets)
# ─────────────────────────────────────────────────────────────────────────────
TIER_4 = [
    # Technology
    "AAPL","MSFT","NVDA","GOOGL","META","AMZN","TSLA","AVGO","ORCL","ADBE",
    "AMD","QCOM","TXN","INTC","MU","AMAT","LRCX","KLAC","MRVL","SNPS",
    "CDNS","FTNT","PANW","CRWD","CSGP","ANSS","EPAM","PAYC","PCTY","INTU",
    "CRM","NOW","WDAY","VEEV","TEAM","DDOG","SNOW","MDB","ESTC","ZS",

    # Healthcare
    "LLY","UNH","JNJ","ABBV","MRK","ABT","TMO","DHR","ISRG","REGN",
    "VRTX","AMGN","BSX","EW","DXCM","IDXX","MRNA","BIO","BIIB","HUM",
    "CI","CVS","MCK","ABC","CAH","GEHC","HOLX","PODD","INSP","NVST",

    # Financials
    "BRK-B","JPM","V","MA","BAC","GS","MS","BLK","SCHW","AXP",
    "SPGI","ICE","CME","COF","USB","TFC","FITB","WFC","C","PNC",
    "MTB","CFG","HBAN","KEY","RF","PBCT","SIVB","SBNY","WAL","FRC",

    # Consumer
    "AMZN","TSLA","HD","MCD","NKE","SBUX","LOW","TGT","BKNG","CMG",
    "DHI","LEN","PHM","GM","F","ABNB","DKNG","DASH","LYFT","UBER",
    "PG","KO","PEP","COST","WMT","PM","MO","MDLZ","CL","KMB",

    # Energy
    "XOM","CVX","COP","EOG","SLB","PXD","MPC","VLO","PSX","OXY",
    "DVN","FANG","HES","APA","HAL","BKR","NOV","WMB","KMI","OKE",

    # Industrials
    "GE","CAT","HON","UPS","RTX","LMT","BA","DE","MMM","EMR",
    "ETN","PH","ROK","IR","AME","FTV","GD","NOC","HII","TDG",

    # Materials / utilities
    "LIN","APD","SHW","ECL","NEM","FCX","NUE","VMC","MLM","ALB",
    "NEE","SO","DUK","SRE","AEP","XEL","PCG","EXC","D","ES",

    # Comm services
    "GOOGL","META","NFLX","DIS","CMCSA","T","VZ","TMUS","CHTR","PARA",
    "WBD","FOXA","OMC","IPG","ZM","SNAP","PINS","SPOT","TTD","MTCH",

    # Real estate
    "AMT","PLD","EQIX","CCI","SPG","O","VICI","PSA","EXR","WELL",
    "ARE","BXP","KIM","REG","FRT","NNN","WPC","STAG","COLD","ELS",
]

# ─────────────────────────────────────────────────────────────────────────────
# KNOWN CHRONIC HIGH SHORT-INTEREST (always scan these regardless of tier)
# These appear on FINRA short reports consistently above 15% SI
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# TIER 4 ADDITIONS — surfaced by short-squeeze research sweep (Jul 2026)
# WSB / squeeze-tracker / high-SI-screener names not already in the
# universe. Placed in Tier 4 so they are SCORED, not endorsed — the
# pipeline decides if any deserve attention. See build_live_universe()
# below to refresh high-SI names automatically instead of by hand.
# ─────────────────────────────────────────────────────────────────────────────
TIER_4_RESEARCH_ADDS = [
    "AI", "ALLR", "APPS", "ASTS", "ATVI", "CAR", "DJT", "DM", "DNA", "DWAC",
    "ENVX", "FLNC", "FUBO", "GENI", "GLTO", "GPS", "GRPN", "GSAT", "H", "HTZ",
    "INVZ", "IONQ", "IONS", "KSS", "LAC", "LAZR", "LKNCY", "LTHM", "LUNR", "M",
    "MAXN", "ME", "MVST", "MTTR", "NNOX", "OATLY", "PATH", "PENN", "PHUN", "PLL",
    "QBTS", "QMCO", "QS", "RDW", "RGTI", "RKLB", "RL", "RNA", "RSI", "RUM",
    "RXRX", "SABR", "SANA", "SDC", "SDGR", "SGML", "SMCI", "SMMT", "VFC", "VLDR",
    "WOOF", "WVE",
]

CHRONIC_HIGH_SI = [
    "GME","AMC","BBBY","KOSS","EXPR","CLOV","MARA","RIOT","NKLA","LCID",
    "RIVN","FFIE","MULN","CENN","GOEV","WKHS","SAVA","NVAX","OCGN","AGEN",
    "SRNE","MNKD","CRBP","BNGO","TLRY","SNDL","BYND","SPCE","DKNG","HOOD",
    "SOFI","AFRM","UPST","OPEN","LMND","ROOT","CVNA","OPAD","RDFN","WISH",
    "PLTR","BB","BBAI","SOUN","TALK","GFAI","GRAB","SE","JOBY","ACHR",
    "LILM","EVTL","BLNK","CHPT","EVGO","PLUG","BE","FCEL","BLDP","GEVO",
    "AMTX","HIMS","PRCH","SKLZ","PAYO","COIN","MSTR","BTBT","CIFR","CLSK",
]


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ACCESS FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def get_universe(
    tier_max: int = 4,
    limit: int = None,
    chronic_first: bool = True,
) -> List[str]:
    """
    Get the full squeeze scanning universe.

    Args:
        tier_max:      Include tiers 1 through tier_max (1-4).
                       Tier 1 = most likely, Tier 4 = S&P500 large caps.
        limit:         Cap total tickers returned. None = all.
        chronic_first: Put known high-SI names at the front regardless of tier.

    Returns:
        Deduplicated list of tickers in scan priority order.
    """
    # Optional LIVE supplement — current high-SI names refreshed by
    # universe_refresh.py. Folded into Tier 4 (scored, not endorsed).
    # Absent file = no-op, so the static universe always works standalone.
    _live = []
    try:
        from universe_refresh import load_live_tickers
        _live = load_live_tickers()
    except Exception:
        _live = []
    tiers = {1: TIER_1, 2: TIER_2, 3: TIER_3,
             4: TIER_4 + TIER_4_RESEARCH_ADDS + _live}

    seen = set()
    result = []

    # Chronic high-SI names always go first
    if chronic_first:
        for t in CHRONIC_HIGH_SI:
            t = t.upper().strip()
            if t and t not in seen:
                result.append(t)
                seen.add(t)

    # Then add tiers in order
    for tier_num in range(1, tier_max + 1):
        for t in tiers.get(tier_num, []):
            t = t.upper().strip()
            if t and t not in seen:
                result.append(t)
                seen.add(t)

    if limit:
        return result[:limit]
    return result


def get_universe_size(tier_max: int = 4) -> dict:
    """Return count breakdown by tier."""
    sizes = {}
    for i in range(1, tier_max + 1):
        tier = {1: TIER_1, 2: TIER_2, 3: TIER_3,
                4: TIER_4 + TIER_4_RESEARCH_ADDS}.get(i, [])
        sizes[f"Tier {i}"] = len(tier)
    sizes["Chronic High-SI"] = len(CHRONIC_HIGH_SI)
    sizes["Total (deduplicated)"] = len(get_universe(tier_max))
    return sizes


if __name__ == "__main__":
    sizes = get_universe_size(4)
    print("Universe size breakdown:")
    for k, v in sizes.items():
        print(f"  {k}: {v}")
    all_tickers = get_universe(tier_max=4)
    print(f"\nFirst 20: {all_tickers[:20]}")
