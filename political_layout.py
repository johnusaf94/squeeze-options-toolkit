"""
political_layout.py
===================
A structured radial layout for the cluster network — rings by what a node
IS, wedges by sector, and relaxation that can only slide a node along its
ring.

WHY NOT FREE FORCE-DIRECTED
---------------------------
A force-directed graph puts the highest-degree node in the middle. On this
data that is Bridgewater, which holds a thousand names, so the picture put
one asset manager at the centre of the market and drew everything else
orbiting it. That is a fact about the algorithm, not about capital — and a
reader has no way to tell the difference. Every other layout artefact has
the same problem: position looked meaningful and was not.

Here position means exactly one thing, and it is fixed:

    ring     what the node is        centre → edge
    ─────────────────────────────────────────────
    0        the Mag 7               the names everything else is measured against
    1        institutions            13F filers
    2        every other stock       banded into sector wedges
    3        people                  members of Congress and corporate insiders

    angle    which sector it belongs to

So "near the middle" means mega-cap, "outer edge" means an individual
person, and "same direction" means same sector. None of those can be an
accident of how the springs settled.

WHAT THE FORCES STILL DO
------------------------
Relaxation runs, but it is constrained to the angular axis: a node may
slide around its ring to stop overlapping its neighbours, and may not
change radius. The structure is hard-coded; the physics only tidies it.
"""

import math
from typing import Dict, List, Optional, Tuple

import numpy as np


# Eight wedges. Chosen from what the store actually holds rather than from
# a standard taxonomy — Communication Services folds into tech because on
# this data it is six names that behave like tech, and Utilities folds into
# energy for the same reason. Real Estate stays separate despite being small
# because it behaves nothing like the rest.
SECTOR_GROUPS: Dict[str, Tuple[str, ...]] = {
    "tech": ("Technology", "Communication Services"),
    "healthcare": ("Healthcare",),
    "financials": ("Financial Services",),
    "consumer": ("Consumer Cyclical", "Consumer Defensive"),
    "industrials": ("Industrials",),
    "energy": ("Energy", "Utilities"),
    "materials": ("Basic Materials",),
    "real estate": ("Real Estate",),
}
# Clockwise from the top. Ordered so related sectors sit beside each other
# and the two biggest are not adjacent, which keeps the wedges legible.
SECTOR_ORDER = ("tech", "healthcare", "financials", "consumer",
                "industrials", "energy", "materials", "real estate",
                "other")

MAG7 = ("NVDA", "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "TSLA")

# Ring radii in world units. The gaps are deliberate: a reader should be
# able to see which band a node is in without counting.
# These numbers only ever matter as RATIOS. The view fits the outermost ring
# to the canvas, so `outer * zoom` is a constant — about 459px on a 976px
# tall canvas — and every other ring lands at its own fraction of that, at
# any window size. That is what makes them solvable rather than guessable:
# each band was sized in screen pixels against the marks it has to hold
# (the Mag 7 are the biggest orbs on the chart and need ~120px of ring
# radius for six of them; the stock annulus needs about twice the area of
# the orbs in it), then converted back at the ratio above.
#
# Two staggered rows are used wherever one row cannot hold the marks — but
# only where there is the radial DEPTH to separate them, since nothing can
# slide a node out of the row above it. The Mag 7 tried this and failed:
# 180 units of separation is 38px against orbs 71px across, so the rows sat
# on top of each other. They get one properly sized ring instead.
R_MAG7 = 575.0
# The institution band carries CARDS, not dots — up to 430 world units wide
# each. A dozen of those will not fit on ONE ring at any radius that still
# sits inside the stock ring: twelve plates need about 7,400 units of
# circumference and a 700-radius ring has 4,400. So the band is two
# staggered rows. Alternate neighbours sit at different radii, which halves
# the arc each row must find and lets cards overlap in ANGLE without ever
# touching. Anything less and the relaxation is asked for space that does
# not exist, and simply jams the plates edge to edge.
R_FUND_IN, R_FUND_OUT = 910.0, 1200.0
R_STOCK = (1340.0, 1960.0)     # banded — wedges pack into rows
R_PERSON = (2030.0, 2200.0)

_REV = {}
for _g, _names in SECTOR_GROUPS.items():
    for _n in _names:
        _REV[_n] = _g


def sector_map(conn) -> Dict[str, str]:
    """ticker -> wedge name, from the stored classification."""
    out = {}
    try:
        for t, s in conn.execute(
                "SELECT ticker, sector FROM sectors WHERE sector IS NOT NULL"):
            out[t] = _REV.get(s, "other")
    except Exception:                                      # noqa: BLE001
        pass
    return out


def _wedges(counts: Dict[str, int], min_frac: float = 0.045
            ) -> Dict[str, Tuple[float, float]]:
    """Angular span per sector, proportional to how many names it holds.

    Equal wedges would give Real Estate's single name the same arc as
    Technology's thirty-five, so the dense sectors would overlap while the
    thin ones sat empty. Every present sector still gets a floor, or a
    one-name wedge becomes a line nobody can point at.
    """
    present = [s for s in SECTOR_ORDER if counts.get(s)]
    if not present:
        return {}
    total = float(sum(counts[s] for s in present))
    raw = {s: max(counts[s] / total, min_frac) for s in present}
    scale = 1.0 / sum(raw.values())
    out, a = {}, -math.pi / 2.0            # start at the top
    for s in present:
        span = raw[s] * scale * 2 * math.pi
        out[s] = (a, a + span)
        a += span
    return out


def radial_layout(nodes: List[dict], edges: List[dict],
                  sectors: Optional[Dict[str, str]] = None,
                  relax_steps: int = 220,
                  extent: Optional[np.ndarray] = None
                  ) -> Tuple[np.ndarray, List[str]]:
    """Place every node on its ring. Returns (positions, ring name per node).

    Nothing here consults the edges for POSITION — only for deciding which
    sector a person or a fund belongs beside. That is the point: where a
    node sits is a statement about what it is, not about how many links it
    happens to have.
    """
    sectors = sectors or {}
    n = len(nodes)
    pos = np.zeros((n, 2))
    ring = ["stock"] * n
    idx = {nd["id"]: i for i, nd in enumerate(nodes)}

    mag = set(MAG7)
    tickers = [i for i, nd in enumerate(nodes) if nd["kind"] == "ticker"]
    mag_i = [i for i in tickers if nodes[i]["label"].upper() in mag]
    other_i = [i for i in tickers if i not in set(mag_i)]
    funds_i = [i for i, nd in enumerate(nodes)
               if nd["kind"] == "filer" and nd.get("source") == "f13"]
    people_i = [i for i, nd in enumerate(nodes)
                if nd["kind"] == "filer" and nd.get("source") != "f13"]

    def sec_of(i):
        return sectors.get(nodes[i]["label"].upper(), "other")

    counts: Dict[str, int] = {}
    for i in other_i:
        s = sec_of(i)
        counts[s] = counts.get(s, 0) + 1
    wedge = _wedges(counts)

    # Which sector a filer sits beside: whichever they have the most money
    # in. A fund or a person has no sector of its own, but its book does.
    adj: Dict[int, List[int]] = {i: [] for i in range(n)}
    for e in edges:
        a, b = idx.get(e["a"]), idx.get(e["b"])
        if a is None or b is None:
            continue
        adj[a].append(b)
        adj[b].append(a)

    def filer_sector(i):
        weight: Dict[str, float] = {}
        for j in adj.get(i, ()):
            if nodes[j]["kind"] != "ticker":
                continue
            s = sec_of(j)
            weight[s] = weight.get(s, 0.0) + float(
                nodes[j].get("amount") or 1.0)
        if not weight:
            return "other"
        return max(weight, key=weight.get)

    def place(indices, r_lo, r_hi, ang_lo, ang_hi, per_row=None):
        """Fan a set of nodes across an arc, in rows if they will not fit."""
        k = len(indices)
        if not k:
            return
        span = max(ang_hi - ang_lo, 1e-3)
        if per_row is None:
            # Favour WIDE rows over many rows. Angular relaxation can slide
            # a node along its row to clear a neighbour, but nothing can fix
            # two rows sitting on top of each other — and the view is
            # stretched about 1.9x horizontally, so a wedge has far more
            # room around than it has depth. Extra rows were the single
            # largest source of overlap in the stock band.
            per_row = max(1, int(round(math.sqrt(k) * 2.4)))
        rows = max(1, int(math.ceil(k / float(per_row))))
        for p, i in enumerate(indices):
            row = p // per_row
            col = p % per_row
            in_row = min(per_row, k - row * per_row)
            # centre each row in the wedge, with a half-step offset on
            # alternate rows so columns do not line up into spokes
            t = (col + 0.5) / float(in_row)
            if row % 2:
                t += 0.5 / float(in_row)
            a = ang_lo + span * min(max(t, 0.01), 0.99)
            r = r_lo if rows == 1 else (
                r_lo + (r_hi - r_lo) * row / float(rows - 1))
            pos[i] = (r * math.cos(a), r * math.sin(a))

    # ── ring 0: the Mag 7 ──
    for p, i in enumerate(sorted(mag_i,
                                 key=lambda j: -(nodes[j].get("amount") or 0))):
        a = -math.pi / 2.0 + 2 * math.pi * p / max(len(mag_i), 1)
        pos[i] = (R_MAG7 * math.cos(a), R_MAG7 * math.sin(a))
        ring[i] = "mag7"

    # ── ring 2: every other stock, inside its wedge ──
    by_sector: Dict[str, List[int]] = {}
    for i in other_i:
        by_sector.setdefault(sec_of(i), []).append(i)
    for s, group in by_sector.items():
        lo, hi = wedge.get(s, (-math.pi / 2, math.pi * 1.5))
        group.sort(key=lambda j: -(nodes[j].get("amount") or 0))
        pad = (hi - lo) * 0.06
        place(group, R_STOCK[0], R_STOCK[1], lo + pad, hi - pad)
        for i in group:
            ring[i] = "stock"

    # ── ring 1: institutions, beside the sector they are heaviest in ──
    fund_by_sec: Dict[str, List[int]] = {}
    for i in funds_i:
        fund_by_sec.setdefault(filer_sector(i), []).append(i)
    for s, group in fund_by_sec.items():
        lo, hi = wedge.get(s, (-math.pi / 2, math.pi * 1.5))
        pad = (hi - lo) * 0.10
        place(group, R_FUND_IN, R_FUND_IN, lo + pad, hi - pad,
              per_row=max(1, len(group)))
        for i in group:
            ring[i] = "fund"

    # Stagger the institutions into two rows, by angle, so that adjacent
    # cards are never at the same radius. They relax as two independent
    # rings from here on.
    for p, i in enumerate(sorted(
            funds_i, key=lambda j: math.atan2(pos[j][1], pos[j][0]))):
        a = math.atan2(pos[i][1], pos[i][0])
        rr = R_FUND_IN if p % 2 == 0 else R_FUND_OUT
        pos[i] = (rr * math.cos(a), rr * math.sin(a))
        ring[i] = "fund" if p % 2 == 0 else "fund2"

    # ── ring 3: people, on the outside ──
    ppl_by_sec: Dict[str, List[int]] = {}
    for i in people_i:
        ppl_by_sec.setdefault(filer_sector(i), []).append(i)
    for s, group in ppl_by_sec.items():
        lo, hi = wedge.get(s, (-math.pi / 2, math.pi * 1.5))
        pad = (hi - lo) * 0.03
        place(group, R_PERSON[0], R_PERSON[1], lo + pad, hi - pad)
        for i in group:
            ring[i] = "person"

    _relax(pos, ring, relax_steps, extent)
    return pos, ring


def _relax(pos: np.ndarray, ring: List[str], steps: int,
           extent: Optional[np.ndarray] = None):
    """Let nodes slide along their ring, never across it.

    This is the whole compromise. Unconstrained forces produced a picture
    whose geometry meant nothing; no forces at all produce evenly spaced
    rows that collide wherever two nodes were assigned the same slot. So
    the radius is frozen and only the angle relaxes — overlaps resolve, the
    structure cannot drift.

    `extent` is each node's half-width IN WORLD UNITS, and it matters more
    than it looks. Before it existed the spacing came from one constant per
    ring, which was a world-unit number being asked to keep apart marks
    whose sizes were measured in SCREEN pixels. The two are related by the
    zoom, so the constants were only ever right at one zoom level and one
    dataset — every other time they were guesses, and the Mag 7 piled into
    a blob because seven of the largest orbs on the chart were being spaced
    as though they were dots. Asking each node how wide it actually is
    removes the guess.
    """
    n = len(pos)
    if n < 3 or steps <= 0:
        return
    r = np.sqrt((pos ** 2).sum(-1))
    r[r < 1e-6] = 1e-6
    ang = np.arctan2(pos[:, 1], pos[:, 0])
    if extent is None:
        extent = np.full(n, 30.0)

    # Only nodes at a similar radius can collide, so group by ring and
    # relax each independently — that also keeps it O(k^2) per small group
    # rather than O(n^2) over everything.
    groups: Dict[str, List[int]] = {}
    for i, g in enumerate(ring):
        groups.setdefault(g, []).append(i)

    for g, members in groups.items():
        if len(members) < 3:
            continue
        m = np.array(members)
        ext = extent[m]
        # Required centre-to-centre separation: the two half-widths plus a
        # margin. A ring can be over-subscribed — the demand can exceed the
        # circumference — and when it is, scale the demand down rather than
        # let the push term fight itself forever. The result is a ring that
        # is evenly tight instead of one with a random pile in it.
        need = (ext[:, None] + ext[None, :]) * 1.14
        rr = r[m]
        demand = float(need.sum(1).max() * len(m) / max(len(m) - 1, 1))
        room = 2 * math.pi * float(rr.mean())
        if demand > room > 0:
            need = need * (room / demand)
        want = need / np.maximum(rr[:, None], 1.0)
        for _ in range(steps):
            a = ang[m]
            d = a[:, None] - a[None, :]
            d = (d + math.pi) % (2 * math.pi) - math.pi
            close = (np.abs(d) < want) & (np.abs(d) > 1e-9)
            push = np.where(close, np.sign(d) * (want - np.abs(d)) * 0.35, 0.0)
            ang[m] = a + push.sum(1)
    pos[:, 0] = r * np.cos(ang)
    pos[:, 1] = r * np.sin(ang)


def wedge_labels(nodes: List[dict], sectors: Dict[str, str]
                 ) -> List[Tuple[str, float, float]]:
    """(sector, mid-angle, outer radius) for drawing the wedge names."""
    counts: Dict[str, int] = {}
    mag = set(MAG7)
    for nd in nodes:
        if nd["kind"] != "ticker" or nd["label"].upper() in mag:
            continue
        s = sectors.get(nd["label"].upper(), "other")
        counts[s] = counts.get(s, 0) + 1
    return [(s, (lo + hi) / 2.0, R_PERSON[1])
            for s, (lo, hi) in _wedges(counts).items()]
