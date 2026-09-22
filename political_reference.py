"""
political_reference.py
======================
Hand-maintained reference data about the tracked institutions.

**NOTHING HERE COMES FROM A FILING.** Every other number in this toolkit is
read from a document and can be traced back to it; this file is typed in by
hand, which makes it the one place that can quietly go stale without
anything breaking. It is kept separate for exactly that reason — so it is
obvious which facts are sourced and which are not.

`principal` is the founder or the figure the firm is publicly identified
with, not necessarily today's chief executive. That is a deliberate choice:
a founder's name is stable for decades, a CEO's is not, and a stale CEO is
worse than no CEO. Berkshire is the clearest case — Buffett founded and ran
it for sixty years while the chief executive role has a named successor, so
"principal: Warren Buffett" stays true in the sense a reader means it.

`short` is the name as it appears ON A CARD. The legal filer name that
EDGAR returns — "Pershing Square Capital Management" — is far wider than a
card on the institution ring, and truncating it at render time produced
"Persh…", which identifies nothing. A hand-written short form is the only
way to keep the card legible without making the ring bigger than the chart.

`logo_ticker` is only set where the manager itself, or a vehicle carrying
its name, is publicly listed and therefore has a logo to fetch. Most private
partnerships have none, and those fall back to a monogram.
"""

REVIEWED = "2026-09-22"

FUNDS = {
    "Berkshire Hathaway": {
        "short": "Berkshire",
        "principal": "Warren Buffett",
        "style": "value, permanent capital",
        "logo_ticker": "BRK.B",
    },
    "Baupost Group": {
        "short": "Baupost",
        "principal": "Seth Klarman",
        "style": "value, special situations",
    },
    "Oaktree Capital Management": {
        "short": "Oaktree",
        "principal": "Howard Marks",
        "style": "credit, distressed",
        "logo_ticker": "OAK",
    },
    "Scion Asset Management": {
        "short": "Scion",
        "principal": "Michael Burry",
        "style": "contrarian value",
    },
    "Pershing Square Capital Management": {
        "short": "Pershing Sq.",
        "principal": "Bill Ackman",
        "style": "concentrated activist",
        "logo_ticker": "PSH",
    },
    "Pershing Square Inc": {
        "short": "Pershing Sq.",
        "principal": "Bill Ackman",
        "style": "concentrated activist",
        "logo_ticker": "PSH",
    },
    "Third Point": {
        "short": "Third Point",
        "principal": "Dan Loeb",
        "style": "activist, event-driven",
    },
    "TCI Fund Management": {
        "short": "TCI",
        "principal": "Chris Hohn",
        "style": "concentrated activist",
    },
    "Elliott Investment Management": {
        "short": "Elliott",
        "principal": "Paul Singer",
        "style": "activist, distressed",
    },
    "ValueAct Holdings": {
        "short": "ValueAct",
        "principal": "Mason Morfit",
        "style": "constructivist activist",
    },
    "Tiger Global Management": {
        "short": "Tiger Global",
        "principal": "Chase Coleman",
        "style": "growth, Tiger lineage",
    },
    "Coatue Management": {
        "short": "Coatue",
        "principal": "Philippe Laffont",
        "style": "growth, technology",
    },
    "Lone Pine Capital": {
        "short": "Lone Pine",
        "principal": "Stephen Mandel",
        "style": "growth, Tiger lineage",
    },
    "Bridgewater Associates": {
        "short": "Bridgewater",
        # Founder. He stepped back from the CIO role in 2022, which is why
        # this field says principal rather than CEO.
        "principal": "Ray Dalio",
        "style": "systematic macro",
    },
    "Appaloosa": {
        "short": "Appaloosa",
        "principal": "David Tepper",
        "style": "distressed, opportunistic",
    },
    "Duquesne Family Office": {
        "short": "Duquesne",
        "principal": "Stanley Druckenmiller",
        "style": "macro, concentrated",
    },
    "Bill & Melinda Gates Foundation Trust": {
        "short": "Gates Trust",
        "principal": "endowment",
        "style": "foundation trust",
    },
}


def about(name: str) -> dict:
    """Reference entry for a fund, matched loosely on the filer name.

    The filer name comes from EDGAR and does not always match the key
    exactly — "Appaloosa LP" against "Appaloosa" — so a prefix match is
    tried before giving up. An unknown fund returns an empty dict and the
    card simply shows less, which is the right failure: absent rather than
    invented.
    """
    if not name:
        return {}
    hit = FUNDS.get(name)
    if hit:
        return hit
    low = name.lower()
    for k, v in FUNDS.items():
        kl = k.lower()
        if low.startswith(kl) or kl.startswith(low):
            return v
    return {}


# What the number on a card actually is. A 13F covers US-listed LONG equity
# positions over $100m and nothing else — no bonds, no cash, no shorts, no
# non-US listings, no private holdings. Bridgewater's 13F is about $37bn
# against an AUM several times that. Calling it AUM would be wrong, so the
# card says "13F equity" and this constant is where that wording lives.
BOOK_LABEL = "13F EQUITY"
BOOK_NOTE = ("US-listed long equity from the latest 13F. Not AUM — it "
             "excludes bonds, cash, shorts, non-US and private holdings.")
