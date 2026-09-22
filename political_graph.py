"""
political_graph.py
==================
A force-directed network canvas for the cluster view, rendered through
Pillow rather than through Tk canvas primitives.

WHY A NETWORK AND NOT A TABLE
-----------------------------
The data is genuinely bipartite: filers on one side, tickers on the other, a
disclosure as the edge between them. Two tickers end up near each other
exactly when the same people bought both, and that adjacency is the one
thing a sorted table cannot show. The picture is not decoration over a list
— it is the only view in which "these three names are being bought by the
same cluster of people" is visible at a glance.

WHY PILLOW AND NOT THE CANVAS
-----------------------------
Tk's canvas has no alpha channel, no anti-aliasing, no gradients, no blur
and no shadows. A circle drawn on it is a hard-edged solid, a "glow" can
only be a stack of flat rings, and no amount of tuning changes that — the
result reads as three low-resolution blobs in slightly different colours.

So the whole scene is drawn into a PIL image and blitted to the canvas as a
single bitmap. That buys true radial-gradient falloff, per-pixel alpha,
anti-aliased strokes and real additive bloom.

This is only affordable because the layout freezes. Measured at 1900x1060:
a plain pass is 22 ms, a 2x supersampled pass with a bloom stage is about
230 ms. Animating at 230 ms a frame would be unusable; rendering one
finished frame and leaving it there is not. So there are two tiers —

    fast      1x, no bloom, ~22 ms   while panning, zooming or hovering
    quality   2x + bloom, ~230 ms    once, a moment after you stop

and the quality pass is cancelled and rescheduled on every interaction, so
it only ever runs when the view has come to rest.

WHY THE SIMULATION STOPS
------------------------
The settling animation carries no information; only the final arrangement
does. The old version annealed to a floor and redrew every 28 ms forever,
which measured 25 ms of work per frame at 300 nodes — most of a core spent
repainting a picture that had stopped changing. Now the layout is solved in
a tight loop before the first paint, and a settled graph issues no timers at
all unless the idle pulse is on.
"""

import math
import tkinter as tk
from typing import Optional, List, Dict, Any, Tuple

import numpy as np

import political_layout as playout
import political_reference as pref

try:
    from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops, ImageTk
    HAVE_PIL = True
except ImportError:                                    # pragma: no cover
    HAVE_PIL = False


BG = "#05070C"
GRID = "#0C1018"
FG = "#CDD6F4"
FG_DIM = "#6C7086"
ACCENT = "#F4C430"

# Heat ramp for cluster score: cold violet through teal, amber, to hot coral.
HEAT = ((0.00, (0x4C, 0x3A, 0x8C)),
        (0.35, (0x3E, 0x8E, 0xA8)),
        (0.60, (0x94, 0xE2, 0xD5)),
        (0.80, (0xF4, 0xC4, 0x30)),
        (1.00, (0xFF, 0x7A, 0x6E)))

SOURCE_COLOR = {
    "house_ptr": (0xF4, 0xC4, 0x30),
    "senate_ptr": (0xF5, 0xA9, 0x7F),
    "form4": (0x94, 0xE2, 0xD5),
    "f13": (0xA7, 0x8B, 0xFA),
}
DEFAULT_SRC = (0x89, 0xB4, 0xFA)

_FONT_PATHS = (r"C:\Windows\Fonts\consolab.ttf",
               r"C:\Windows\Fonts\consola.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")


def _hex(rgb) -> str:
    return "#{:02X}{:02X}{:02X}".format(
        max(0, min(255, int(rgb[0]))),
        max(0, min(255, int(rgb[1]))),
        max(0, min(255, int(rgb[2]))))


def _rgb(h: str):
    h = h.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _blend(c1, c2, t: float):
    t = max(0.0, min(1.0, t))
    return tuple(c1[i] + (c2[i] - c1[i]) * t for i in range(3))


# Diverging ramp for the combined mode: everyone selling through contested
# to everyone buying. Amber in the middle is deliberate — a name both sides
# are active in is the interesting one, so it should not read as "neutral".
LEAN = ((-1.00, (0xF3, 0x8B, 0xA8)),
        (-0.35, (0xF5, 0xA9, 0x7F)),
        ( 0.00, (0xF4, 0xC4, 0x30)),
        ( 0.35, (0x94, 0xE2, 0xD5)),
        ( 1.00, (0xA6, 0xE3, 0xA1)))


def lean_color(lean: float):
    x = max(-1.0, min(1.0, float(lean)))
    for i in range(len(LEAN) - 1):
        a, ca = LEAN[i]
        b, cb = LEAN[i + 1]
        if x <= b:
            t = 0.0 if b == a else (x - a) / (b - a)
            return _blend(ca, cb, t)
    return LEAN[-1][1]


def heat(score: float):
    x = max(0.0, min(1.0, score / 100.0))
    for i in range(len(HEAT) - 1):
        a, ca = HEAT[i]
        b, cb = HEAT[i + 1]
        if x <= b:
            t = 0.0 if b == a else (x - a) / (b - a)
            return _blend(ca, cb, t)
    return HEAT[-1][1]


# ─────────────────────────────────────────────
# SPRITES — the thing Tk could not do
# ─────────────────────────────────────────────

_GLOW_MASK = None
_SPHERE_MASK = None
_SPRITE_CACHE: Dict[tuple, Any] = {}
GLOW_MAX_PX = 192       # cached glow size; larger ones are scaled at draw
BALL_MAX_PX = 160       # same for the solid orbs


def _trim(cache: dict, budget_mb: float):
    """Evict by BYTES, not by entry count.

    An entry cap is meaningless when entries differ by three orders of
    magnitude — a 4 px sprite and a 290 px glow both counted as one, so a
    cache "limited" to 4,000 could hold anything from 2 MB to a gigabyte.
    Clearing on a pixel budget bounds it for real.
    """
    tot = 0
    for v in cache.values():
        if hasattr(v, "size"):
            tot += v.size[0] * v.size[1] * len(v.getbands())
    if tot > budget_mb * 1048576:
        cache.clear()


def _glow_mask(size: int = 192):
    """A radial alpha falloff, built once and scaled per node.

    Computing a gradient per node per frame is far too slow; one mask
    resized with a bilinear filter is not. The curve is alpha ~ (1-r)^2.4,
    which reads as a soft halo rather than a flat disc with a hard edge.
    """
    global _GLOW_MASK
    if _GLOW_MASK is not None:
        return _GLOW_MASK
    y, x = np.mgrid[0:size, 0:size]
    c = (size - 1) / 2.0
    r = np.sqrt((x - c) ** 2 + (y - c) ** 2) / c
    a = np.clip(1.0 - r, 0.0, 1.0) ** 2.4
    _GLOW_MASK = Image.fromarray((a * 255).astype(np.uint8), "L")
    return _GLOW_MASK


def _sphere_mask(size: int = 192):
    """Alpha disc plus a lit top-left, so a node reads as a sphere."""
    global _SPHERE_MASK
    if _SPHERE_MASK is not None:
        return _SPHERE_MASK
    y, x = np.mgrid[0:size, 0:size]
    c = (size - 1) / 2.0
    nx, ny = (x - c) / c, (y - c) / c
    r = np.sqrt(nx ** 2 + ny ** 2)
    disc = np.clip((1.0 - r) * c * 0.9, 0.0, 1.0)      # anti-aliased edge
    lit = np.clip(1.0 - np.sqrt((nx + 0.36) ** 2 + (ny + 0.36) ** 2) / 1.15,
                  0.0, 1.0) ** 1.8
    _SPHERE_MASK = (Image.fromarray((disc * 255).astype(np.uint8), "L"),
                    Image.fromarray((lit * 255).astype(np.uint8), "L"))
    return _SPHERE_MASK


_FIGURE_MASK: Dict[str, Any] = {}


def _figure_mask(kind: str, size: int = 192):
    """Silhouettes, so the two halves of the graph are different SHAPES.

    Colour alone was carrying the distinction between a company and the
    people trading it, and at these sizes a teal dot and a violet dot read
    as the same object. A figure and an orb do not.

    Three marks, matching what the filer actually is:
        person   a member of Congress or a corporate insider
        fund     an institution filing a 13F — not a person, so not a figure
        ball     the company itself
    """
    hit = _FIGURE_MASK.get(kind)
    if hit is not None:
        return hit
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    s = size / 192.0
    if kind == "person":
        d.ellipse([74 * s, 16 * s, 118 * s, 60 * s], fill=255)      # head
        d.rounded_rectangle([56 * s, 70 * s, 136 * s, 176 * s],
                            radius=int(36 * s), fill=255)           # torso
        d.rectangle([56 * s, 150 * s, 136 * s, 176 * s], fill=255)
    else:                                                # fund / institution
        w, h = 44 * s, 52 * s
        cx, cy = 96 * s, 98 * s
        pts = [(cx + w * math.cos(math.radians(a)),
                cy + h * math.sin(math.radians(a)))
               for a in range(-90, 271, 60)]
        d.polygon(pts, fill=255)
    _FIGURE_MASK[kind] = m
    return m


# ─────────────────────────────────────────────
# LOGOS
# ─────────────────────────────────────────────

_LOGO: Dict[str, Any] = {}          # ticker -> PIL RGBA, or False for "none"
_LOGO_SPRITES: Dict[tuple, Any] = {}
LOGO_URL = "https://financialmodelingprep.com/image-stock/{}.png"
# Below this screen radius a logo is a smudge and the plain coloured orb
# reads better. Isolating a node re-fits the view onto its neighbourhood,
# which pushes everything on screen well past this, so the marks become
# legible exactly when you have asked to look at something.
LOGO_MIN_RADIUS = 8.5


def _logo_path(ticker: str) -> str:
    import os
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "political_cache", "logos")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "{}.png".format(ticker.upper()))


def load_logo(ticker: str):
    """A company's mark, from disk if it is there and the network if not.

    Returns None when there is no logo, and caches that answer too — a
    missing ticker should cost one request, not one per redraw.
    """
    t = (ticker or "").upper()
    if not t:
        return None
    hit = _LOGO.get(t)
    if hit is not None:
        return hit or None
    import os
    p = _logo_path(t)
    raw = None
    if os.path.exists(p):
        raw = open(p, "rb").read() or None
    else:
        try:
            import requests
            r = requests.get(LOGO_URL.format(t), timeout=15, headers={
                "User-Agent": "squeeze-toolkit/1.0"})
            if r.status_code == 200 and r.content[:4] == b"\x89PNG":
                raw = r.content
            open(p, "wb").write(raw or b"")   # empty file = known-missing
        except Exception:                                  # noqa: BLE001
            pass
    if not raw:
        _LOGO[t] = False
        return None
    try:
        import io
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        # Sources arrive at 250x250 — a megabyte of pixels each, and the
        # orb is composed at 112. Keeping the original would cost 330 MB
        # across a full ticker universe for detail nothing ever draws.
        if max(img.size) > LOGO_SRC_MAX:
            img.thumbnail((LOGO_SRC_MAX, LOGO_SRC_MAX), Image.LANCZOS)
    except Exception:                                      # noqa: BLE001
        _LOGO[t] = False
        return None
    _LOGO[t] = img
    return img


_LOGO_FACES: Dict[tuple, Any] = {}
LOGO_CANON = 112          # the orb is composed once at this size, then scaled
LOGO_SRC_MAX = 128        # sources are downscaled on load; nothing draws bigger


def _logo_face(ticker: str, ring):
    """The finished orb for one company, built once at a canonical size.

    Composing this per node per frame — thumbnail the 250px source, draw the
    face, composite the sheen, multiply the alpha — measured about 200 ms
    each, so a zoom change rebuilt every visible orb and cost one and a half
    seconds. Building it once and resizing is a single filter call.
    """
    col = tuple(int(c) for c in ring)
    key = (ticker.upper(), col)
    got = _LOGO_FACES.get(key)
    if got is not None:
        return got
    logo = load_logo(ticker)
    if logo is None:
        _LOGO_FACES[key] = False
        return None

    px = LOGO_CANON
    img = Image.new("RGBA", (px, px), col + (255,))
    face = int(px * 0.76)
    inner = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    ImageDraw.Draw(inner).ellipse(
        [(px - face) / 2, (px - face) / 2,
         (px + face) / 2, (px + face) / 2],
        fill=(238, 242, 250, 255))
    img.alpha_composite(inner)

    fit = int(face * 0.74)
    lg = logo.copy()
    lg.thumbnail((fit, fit), Image.LANCZOS)
    img.alpha_composite(lg, ((px - lg.width) // 2, (px - lg.height) // 2))

    # A touch of the sphere's lighting, so it sits in the same scene as the
    # plain orbs rather than looking like a sticker.
    _, lit = _sphere_mask()
    sheen = Image.new("RGBA", (px, px), (255, 255, 255, 40))
    img.alpha_composite(Image.composite(
        sheen, Image.new("RGBA", (px, px), (0, 0, 0, 0)),
        lit.resize((px, px), Image.BILINEAR)))

    disc, _ = _sphere_mask()
    img.putalpha(ImageChops.multiply(img.getchannel("A"),
                                     disc.resize((px, px), Image.BILINEAR)))
    _trim(_LOGO_FACES, 10)
    _LOGO_FACES[key] = img
    return img


def _logo_sprite(ticker: str, px: int, ring, dim: float):
    """A company orb: coloured ring, pale face, logo on top.

    The ring keeps the colour encoding — score, or net lean in the combined
    view — because that is what the picture is actually about. The logo sits
    inside it on a near-white face, since almost every corporate mark is
    drawn for a light background and would vanish on this one.

    Sizes are rounded to multiples of four so that panning and zooming reuse
    sprites instead of rebuilding one per pixel of scale.
    """
    px = max(8, (int(px) + 3) & ~3)
    key = (ticker.upper(), px, tuple(int(c) for c in ring), int(dim * 5))
    got = _LOGO_SPRITES.get(key)
    if got is not None:
        return got or None
    base = _logo_face(ticker, ring)
    if base is None:
        _LOGO_SPRITES[key] = False
        return None
    img = base.resize((px, px), Image.LANCZOS)
    if dim < 0.99:
        img = img.copy()
        img.putalpha(img.getchannel("A").point(lambda v: int(v * dim)))
    _trim(_LOGO_SPRITES, 8)
    _LOGO_SPRITES[key] = img
    return img


# ─────────────────────────────────────────────
# INSTITUTION PLATES
# ─────────────────────────────────────────────

_PLATE_CACHE: Dict[tuple, Any] = {}
PLATE_ASPECT = 0.50           # height as a fraction of width


def _money_short(v) -> str:
    if not v:
        return "--"
    for cut, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(v) >= cut:
            return "${:,.1f}{}".format(v / cut, suf).replace(".0", "")
    return "${:,.0f}".format(v)


def _monogram(name: str) -> str:
    """Initials, for the great majority of managers with no public logo.

    A private partnership has no listed vehicle and therefore no logo to
    fetch. Initials always render, never 404, and read at any size — which
    a missing-image placeholder does not.
    """
    skip = {"the", "of", "and", "group", "capital", "management",
            "associates", "partners", "fund", "llc", "lp", "inc", "trust"}
    words = [w for w in (name or "").replace(",", " ").split()
             if w.lower().strip(".") not in skip]
    if not words:
        words = (name or "?").split() or ["?"]
    if len(words) == 1:
        # One surviving word means a single initial — "B" for Bridgewater
        # Associates, "T" for TCI — which identifies nothing. Two letters
        # of the one word is always readable and always distinct.
        return words[0][:2].upper()
    return "".join(w[0] for w in words[:2]).upper()


_PFONT: Dict[int, Any] = {}


def _plate_font(px: int):
    """A plate-sized font, cached by pixel size.

    The canvas font is 13px bold monospace, which is right for a label
    floating beside an orb and far too wide for text INSIDE a 94px card —
    eleven characters of it measure 86px. Cards therefore pick their own
    size from how much room they actually have, rather than truncating a
    fixed one down to "Brid…".
    """
    px = max(6, min(int(px), 26))
    got = _PFONT.get(px)
    if got is None:
        for path in _FONT_PATHS:
            try:
                got = ImageFont.truetype(path, px)
                break
            except Exception:                              # noqa: BLE001
                continue
        if got is None:
            got = ImageFont.load_default()
        _PFONT[px] = got
    return got


def _plate(node: dict, w: int, color, dim: float, fonts):
    """A small profile card for an institution.

    An orb can carry a size and a colour and nothing else, which is why
    every fund on the ring read as an anonymous dot. These are the handful
    of actors that move the most capital in the picture, so they get a name,
    what they run, who runs it, and a mark — and enough room to be read.
    """
    w = max(60, (int(w) + 3) & ~3)
    h = max(26, int(w * PLATE_ASPECT))
    name = node.get("label") or "?"
    book = node.get("book")
    key = (name, w, tuple(int(c) for c in color), int(dim * 5))
    got = _PLATE_CACHE.get(key)
    if got is not None:
        return got

    f_name, f_small = fonts
    col = tuple(int(c) for c in color)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img, "RGBA")

    r = max(3, h // 5)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=r,
                        fill=(14, 20, 32, 242),
                        outline=col + (210,), width=max(1, w // 90))
    # Accent bar on the left edge, so the ring reads as one family of marks.
    d.rounded_rectangle([0, 0, max(3, w // 36), h - 1], radius=r,
                        fill=col + (255,))

    pad = max(4, w // 26)
    # The mark used to fill the card's full height, which left about 41px
    # of a 94px card for words. It is a supporting detail, not the subject.
    box = max(14, int(h * 0.58))
    bx = pad + max(3, w // 36)
    by = (h - box) // 2
    info = pref.about(name)
    mark = None
    if info.get("logo_ticker"):
        mark = load_logo(info["logo_ticker"])
    if mark is not None:
        m = mark.copy()
        m.thumbnail((box, box), Image.LANCZOS)
        plate_bg = Image.new("RGBA", (box, box), (238, 242, 250, 255))
        ImageDraw.Draw(plate_bg).rectangle([0, 0, box, box],
                                           fill=(238, 242, 250, 255))
        plate_bg.alpha_composite(m, ((box - m.width) // 2,
                                     (box - m.height) // 2))
        img.alpha_composite(plate_bg, (bx, by))
    else:
        # _blend returns floats; PIL's ink parser wants ints.
        tint = tuple(int(v) for v in _blend((14, 20, 32), col, 0.30))
        d.rounded_rectangle([bx, by, bx + box, by + box],
                            radius=max(2, box // 6), fill=tint + (255,))
        d.text((bx + box / 2, by + box / 2), _monogram(name),
               font=_plate_font(int(box * 0.62)), anchor="mm",
               fill=col + (255,))

    tx = bx + box + pad
    avail = w - tx - pad

    # Largest size at which the name still fits whole. Shrinking the type
    # keeps the name readable; truncating it does not.
    display = info.get("short") or name
    f_name = _plate_font(int(h * 0.30))
    for px in range(int(h * 0.30), 5, -1):
        f_name = _plate_font(px)
        if f_name.getlength(display) <= avail:
            break
    f_small = _plate_font(max(6, int(f_name.size * 0.80)))

    def fit(text, font):
        """Clip a line to the card, or return "" if nothing useful fits.

        Every line needs this, not just the name — the book line used to
        run past the right edge and get cut by the image boundary, so a
        card read "$37.4B  1" with the units sliced off mid-word. A hard
        clip is the one thing worse than no text: it can misread as a
        different number.
        """
        if not text:
            return ""
        if font.getlength(text) <= avail:
            return text
        while text and font.getlength(text + "…") > avail:
            text = text[:-1]
        return text + "…" if len(text) >= 3 else ""

    label = fit(display, f_name)
    d.text((tx, pad + h * 0.04), label, font=f_name, anchor="la",
           fill=(232, 238, 250, 255))

    # The label is dropped before the number is: a card showing "$37.4B"
    # with no units beats one showing "13F EQU…" beside a clipped figure.
    money = _money_short(book)
    line = "{}  {}".format(money, pref.BOOK_LABEL)
    if f_small.getlength(line) > avail:
        line = money
    d.text((tx, pad + h * 0.36), fit(line, f_small), font=f_small,
           anchor="la", fill=col + (255,))

    who = fit(info.get("principal"), f_small)
    if who:
        d.text((tx, pad + h * 0.64), who, font=f_small, anchor="la",
               fill=(126, 140, 172, 255))

    if dim < 0.99:
        img.putalpha(img.getchannel("A").point(lambda v: int(v * dim)))
    if len(_PLATE_CACHE) > 400:
        _PLATE_CACHE.clear()
    _PLATE_CACHE[key] = img
    return img


def _sprite(kind: str, px: int, color, alpha: float):
    """Cached, tinted, pre-scaled sprite.

    The key is deliberately COARSE. Every node carries a slightly different
    heat colour, so an exact key made 324 unique sprites on the first frame
    and building each one — resize a 192px mask, composite, set alpha — cost
    about five milliseconds. That alone was two seconds of the first render.
    Rounding the radius to even pixels and the colour to 16 levels per
    channel collapses it to a few dozen sprites, and nothing visible
    changes at these sizes.
    """
    px = max(2, (int(px) + 1) & ~1)
    q = tuple((int(c) >> 4) << 4 for c in color)
    key = (kind, px, q, int(alpha * 8))
    color = q
    hit = _SPRITE_CACHE.get(key)
    if hit is not None:
        return hit
    col = tuple(int(c) for c in color)
    if kind == "glow":
        # A glow for a big orb wants ~290 px, and one cached RGBA sprite at
        # that size is 330 KB. Capping the cached size and letting the draw
        # scale it costs nothing visible — it is a soft radial falloff — and
        # took this cache from 32 MB to a few.
        px = min(px, GLOW_MAX_PX)
        m = _glow_mask().resize((px, px), Image.BILINEAR)
        m = m.point(lambda v: int(v * alpha))
        img = Image.new("RGBA", (px, px), col + (0,))
        img.putalpha(m)
    elif kind in ("person", "fund"):
        # Same lighting as the orbs so the two marks sit in one scene: the
        # silhouette is the alpha, the top-left gradient is the shading.
        fm = _figure_mask(kind).resize((px, px), Image.LANCZOS)
        _, lit = _sphere_mask()
        l = lit.resize((px, px), Image.BILINEAR)
        base = Image.new("RGBA", (px, px), col + (255,))
        hi = Image.new("RGBA", (px, px),
                       tuple(min(255, int(c + (255 - c) * 0.5))
                             for c in col) + (255,))
        img = Image.composite(hi, base, l)
        img.putalpha(fm.point(lambda v: int(v * alpha)))
    else:
        disc, lit = _sphere_mask()
        d = disc.resize((px, px), Image.BILINEAR)
        l = lit.resize((px, px), Image.BILINEAR)
        base = Image.new("RGBA", (px, px), col + (255,))
        hi = Image.new("RGBA", (px, px),
                       tuple(min(255, int(c + (255 - c) * 0.55))
                             for c in col) + (255,))
        img = Image.composite(hi, base, l)
        a = d.point(lambda v: int(v * alpha))
        img.putalpha(a)
    _trim(_SPRITE_CACHE, 12)
    _SPRITE_CACHE[key] = img
    return img


class NetworkCanvas(tk.Canvas):
    """Force-directed bipartite graph, drawn as a bitmap."""

    SETTLE_STEP = 0.45
    SETTLE_FRAMES = 8
    MAX_SETTLE_FRAMES = 340
    QUALITY_DELAY = 240        # ms of stillness before the good render
    SUPERSAMPLE = 2

    def __init__(self, parent, on_select=None, on_hover=None,
                 on_open=None, bg: str = BG, **kw):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, **kw)
        self.bg = bg
        self._bgrgb = _rgb(bg)
        self.on_select = on_select or (lambda n: None)
        self.on_hover = on_hover or (lambda n: None)
        self.on_open = on_open or (lambda n: None)

        self.nodes: List[dict] = []
        self.edges: List[dict] = []
        self._pos = np.zeros((0, 2))
        self._vel = np.zeros((0, 2))
        self._pinned = np.zeros((0,), dtype=bool)
        self._idx: Dict[str, int] = {}
        self._adj: Dict[int, set] = {}
        self._radii = np.zeros((0,))
        self._center_k = np.array([0.0016, 0.0016])

        self._zoom = 1.0
        self._off = np.array([0.0, 0.0])
        self._running = False
        self._after = None
        self._quality_after = None
        self._temp = 1.0
        self._max_step = 1e9
        self._still = 0
        self._settle_frames = 0
        self.motion = False     # a pulse now costs a full re-render; not worth it
        self.by_direction = False   # colour edges buy/sell instead of by source
        self.isolate = True         # selecting hides everything unconnected
        self.logos = True           # draw company marks inside the orbs
        self.lite = False           # strip everything expensive
        # "radial" = structured rings by what a node IS; "force" = the old
        # free layout, kept because it is better for spotting odd
        # neighbourhoods even though its geometry means nothing.
        self.layout_mode = "radial"
        self.sectors = {}           # ticker -> wedge, supplied by the view
        self.ring = []              # per-node ring name, radial mode only
        self._frame = 0
        self._hover = None
        self._selected = None
        self._drag_node = None
        self._drag_moved = False
        self._drag_view = None

        self._photo = None          # must be kept alive or Tk drops it
        self._img_item = None
        self._font = None
        self._font_sm = None

        self._fit_size = (0, 0)
        self.bind("<Configure>", self._on_configure)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Button-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Double-1>", self._on_double)
        self.bind("<MouseWheel>", self._on_wheel)
        self.bind("<Button-4>", lambda e: self._zoom_at(e.x, e.y, 1.1))
        self.bind("<Button-5>", lambda e: self._zoom_at(e.x, e.y, 1 / 1.1))

    # ── fonts ────────────────────────────────
    def _fonts(self, scale: int):
        key = scale
        if getattr(self, "_font_scale", None) == key and self._font:
            return self._font, self._font_sm
        f = fs = None
        for p in _FONT_PATHS:
            try:
                f = ImageFont.truetype(p, 13 * scale)
                fs = ImageFont.truetype(p, 10 * scale)
                break
            except Exception:
                continue
        if f is None:
            f = fs = ImageFont.load_default()
        self._font, self._font_sm, self._font_scale = f, fs, key
        return f, fs

    def _on_configure(self, _e=None):
        """Re-fit when the canvas actually has a size.

        `set_graph` runs while the tab is still being built, before Tk has
        mapped anything, so winfo_width() returns 1 and both the aspect
        stretch and the zoom get computed against a placeholder. Without
        this the graph keeps whatever framing it guessed in the dark — which
        is why it rendered as a small knot in the middle of an empty field
        even though the layout underneath it was correctly spread.
        """
        w, h = self.winfo_width(), self.winfo_height()
        if w < 8 or h < 8:
            return
        ow, oh = self._fit_size
        if abs(w - ow) > 24 or abs(h - oh) > 24:
            self._fit_size = (w, h)
            if len(self._pos) > 3:
                self._fit_aspect()
                self.reset_view()
                return
        self._render(quality=False)

    # ── geometry ─────────────────────────────
    def _center(self):
        return np.array([self.winfo_width() / 2.0,
                         self.winfo_height() / 2.0])

    # How far the world is stretched horizontally. The layout is polar, so
    # it lays out a CIRCLE — and a circle in an 1850x976 canvas leaves
    # nearly half the pixels black down each side while the rings are still
    # too crowded to separate. Pushing the rings further out buys nothing:
    # orbs are sized in screen pixels, so the zoom shrinks to match and the
    # picture is identical. Widening ONE axis is the only change here that
    # is not scale invariant — it adds real room, on the axis that had it
    # spare. Every draw and hit-test path goes through _to_screen, so
    # applying it here keeps rings, spokes, nodes and the mouse in step.
    _aspect = 1.0

    def _ax(self):
        return np.array([self._aspect, 1.0])

    def _to_screen(self, p):
        return (p * self._ax() - self._off) * self._zoom + self._center()

    def _to_world(self, s):
        return ((np.asarray(s, dtype=float) - self._center()) / self._zoom
                + self._off) / self._ax()

    # ── data ─────────────────────────────────
    def set_graph(self, nodes: List[dict], edges: List[dict],
                  sectors: Optional[dict] = None):
        self.stop()
        self.nodes = list(nodes)
        if sectors is not None:
            self.sectors = sectors
        n = len(self.nodes)
        self.ring = ["stock"] * n
        self._idx = {nd["id"]: i for i, nd in enumerate(self.nodes)}
        self.edges = [e for e in edges
                      if e["a"] in self._idx and e["b"] in self._idx]

        self._adj = {i: set() for i in range(n)}
        for e in self.edges:
            a, b = self._idx[e["a"]], self._idx[e["b"]]
            self._adj[a].add(b)
            self._adj[b].add(a)

        # Seed close to the answer. Tickers on a golden-angle spiral, which
        # spreads points evenly over a disc with no clumping, ordered by
        # score so the interesting names start near the middle; each filer
        # just outside whatever it is attached to.
        rng = np.random.default_rng(7)
        self._pos = np.zeros((n, 2))
        tickers = [i for i, nd in enumerate(self.nodes)
                   if nd["kind"] == "ticker"]
        tickers.sort(key=lambda i: -self.nodes[i].get("score", 0))
        ga = math.pi * (3.0 - math.sqrt(5.0))
        for rank, i in enumerate(tickers):
            r = 86.0 * math.sqrt(rank + 0.5)
            a = ga * rank
            self._pos[i] = (r * math.cos(a), r * math.sin(a))
        for i, nd in enumerate(self.nodes):
            if nd["kind"] == "ticker":
                continue
            nb = list(self._adj.get(i, ()))
            base = self._pos[nb].mean(axis=0) if nb else np.zeros(2)
            ang = rng.random() * 2 * math.pi
            self._pos[i] = base + np.array(
                [math.cos(ang), math.sin(ang)]) * (108.0 + rng.normal(0, 14))

        self._vel = np.zeros((n, 2))
        self._pinned = np.zeros((n,), dtype=bool)
        self._temp = 1.0
        self._max_step = 1e9
        self._still = 0
        self._settle_frames = 0
        self._frame = 0
        self._hover = None
        self._selected = None

        self._fit_scales()
        self._radii = np.array([self._radius(nd) for nd in self.nodes])
        self._mass = np.clip(self._radii / 11.0, 0.65, 2.6)

        w = max(self.winfo_width(), 800)
        h = max(self.winfo_height(), 500)
        corr = math.sqrt(max(w / float(h), 1.0))
        self._center_k = np.array([0.0016 / corr, 0.0016 * corr])

        deg = np.array([max(len(self._adj.get(i, ())), 1) for i in range(n)],
                       dtype=float)
        self._deg = deg
        self._rest = np.empty(len(self.edges))
        self._k = np.empty(len(self.edges))
        for k, e in enumerate(self.edges):
            a, b = self._idx[e["a"]], self._idx[e["b"]]
            lo, hi = min(deg[a], deg[b]), max(deg[a], deg[b])
            self._rest[k] = 78.0 + 30.0 * math.sqrt(hi)
            self._k[k] = 0.10 / (lo ** 0.75)

        if self.layout_mode == "radial":
            # Structure first, physics second. Rings come from what each
            # node IS; the relaxation inside the layout only slides nodes
            # along their own ring so they stop overlapping. Nothing here
            # can move a node across a ring, which is what stops the
            # geometry from drifting back into meaning nothing.
            self._pos, self.ring = playout.radial_layout(
                self.nodes, self.edges, self.sectors,
                extent=self._extents())
            self._running = False
        else:
            self._settle_now()
        self.reset_view()
        self._prefetch_logos()
        self._schedule_quality()
        if self.motion:
            self._idle()

    def _prefetch_logos(self):
        """Fetch missing company marks in the background, then repaint once.

        Never on the render path: a logo that is not cached yet would mean a
        network round trip inside a frame. The graph draws immediately with
        plain orbs and quietly upgrades when the images arrive.
        """
        if not self.logos:
            return
        want = [nd["label"] for nd in self.nodes
                if nd["kind"] == "ticker" and nd["label"].upper() not in _LOGO]
        if not want:
            return

        def work():
            for t in want[:400]:
                load_logo(t)
            try:
                self.after(0, self._schedule_quality)
            except Exception:
                pass

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _settle_now(self, budget: int = 320):
        """Arrange the graph before it is ever drawn."""
        for _ in range(budget):
            self._step()
            self._settle_frames += 1
            if self._max_step < self.SETTLE_STEP:
                self._still += 1
                if self._still >= self.SETTLE_FRAMES:
                    break
            else:
                self._still = 0
        self._fit_aspect()
        self._running = False

    def _fit_aspect(self):
        # Never in radial mode. The stretch exists to make a circular
        # force-directed cloud fill a wide window; applied to concentric
        # rings it turns them into ellipses and destroys the one thing the
        # layout is for — that distance from the centre means something.
        if self.layout_mode == "radial":
            return
        """Stretch the settled cloud to the shape of the window.

        Repulsion is isotropic, so the layout always relaxes into a circle
        no matter how the centering force is weighted — measured aspect 1.02
        against a 1.76 canvas. A round cloud in a wide window can only be
        fitted by its height, which is what left half the screen empty and
        made the middle look knotted.

        A single anisotropic scale fixes it. It is a linear map, so relative
        neighbourhoods survive: two tickers that were adjacent stay
        adjacent. The correction is deliberately partial (power 0.75) so the
        result reads as a wide field rather than a smeared one.
        """
        if len(self._pos) < 4:
            return
        w = max(self.winfo_width(), 800)
        h = max(self.winfo_height(), 500)
        lo = np.percentile(self._pos, 5, axis=0)
        hi = np.percentile(self._pos, 95, axis=0)
        span = np.maximum(hi - lo, 1.0)
        target = (w / float(h)) / (span[0] / span[1])
        # Idempotent: this runs again on every resize, and repeatedly
        # stretching an already-stretched cloud would smear it flat. The
        # correction is computed from the CURRENT aspect each time, so once
        # the cloud matches the window the factor is 1 and nothing moves.
        if target > 1.02:
            self._pos[:, 0] *= target ** 0.75
        elif target < 0.98:
            self._pos[:, 1] *= (1.0 / target) ** 0.75

    # ── sizing ───────────────────────────────
    AMT_LO = 1.0e4
    AMT_HI = 5.0e8

    @staticmethod
    def _amt_norm(amount, lo_amt=None, hi_amt=None) -> float:
        if not amount or amount <= 0:
            return 0.0
        lo = math.log10(lo_amt or NetworkCanvas.AMT_LO)
        hi = math.log10(hi_amt or NetworkCanvas.AMT_HI)
        return max(0.0, min(1.0, (math.log10(amount) - lo) / (hi - lo)))

    # Fallbacks. The real endpoints are fitted to whatever is on screen —
    # see _fit_scales — because a FIXED $1bn..$5tn scale wastes most of its
    # range on values no graph contains. Measured on a typical view: the
    # middle half of the names live between $13bn and $255bn, which the
    # fixed scale rendered as radius 16.2 to 28.1, a ratio of 1.73. That is
    # why every orb looked the same size. Scaling everything up 2x does not
    # fix it — it multiplies both ends and leaves every ratio identical.
    CAP_LO, CAP_HI = 1.0e9, 5.0e12
    BOOK_LO, BOOK_HI = 1.0e9, 1.0e11
    CAP_R_LO, CAP_R_HI = 6.0, 68.0

    def _fit_scales(self):
        """Fit the size scales to the values actually present.

        Endpoints come from the 5th and 95th percentile rather than the
        min and max, so one $1bn straggler or one $5tn giant cannot flatten
        everything between them. Values outside that band clamp, which is
        the intended behaviour: NVDA is simply the biggest orb and does not
        need to be forty times the width of the next one to say so.
        """
        caps = [n["market_cap"] for n in self.nodes
                if n["kind"] == "ticker" and n.get("market_cap")]
        if len(caps) >= 8:
            a = np.array(caps, dtype=float)
            lo, hi = np.percentile(a, 5), np.percentile(a, 95)
            if hi > lo * 1.5:
                self.CAP_LO, self.CAP_HI = float(lo), float(hi)
        books = [n["book"] for n in self.nodes
                 if n.get("source") == "f13" and n.get("book")]
        if len(books) >= 4:
            b = np.array(books, dtype=float)
            lo, hi = b.min(), np.percentile(b, 95)
            if hi > lo * 1.5:
                self.BOOK_LO, self.BOOK_HI = float(lo), float(hi)

    # The floor is set so the SMALLEST book still clears the 56px card
    # threshold at default zoom. Below that a plate falls back to a
    # hexagon, and one hexagon in a ring of cards reads as a bug.
    PLATE_MIN_W, PLATE_MAX_W = 285.0, 430.0

    def _extents(self) -> np.ndarray:
        """Each node's half-width in WORLD units, for the layout to space by.

        Sizes are computed in screen pixels, but the layout runs in world
        units and before the view has been fitted, so there is no zoom to
        convert with yet. The zoom is not arbitrary though — the fit puts
        the outermost ring just inside the canvas, so it can be derived
        from the ring geometry that is about to be laid out. That estimate
        is what makes spacing track drawn size instead of a constant.
        """
        w = max(self.winfo_width(), 400)
        h = max(self.winfo_height(), 400)
        world_per_px = (2.0 * playout.R_PERSON[1] * 1.12) / float(min(w, h))
        out = np.empty(len(self.nodes))
        for i, nd in enumerate(self.nodes):
            if nd.get("source") == "f13":
                # A plate is a wide rectangle, so its half-WIDTH is what
                # has to clear its neighbour, not any notion of radius.
                out[i] = self._plate_w(i) * 0.5
            else:
                out[i] = self._radius(nd) * world_per_px
        return out

    def _plate_w(self, i) -> float:
        """Plate width in world units, scaled by the book it represents."""
        nd = self.nodes[i]
        b = self._amt_norm(nd.get("book"), self.BOOK_LO, self.BOOK_HI)
        return self.PLATE_MIN_W + (self.PLATE_MAX_W - self.PLATE_MIN_W) * b

    def _radius(self, nd) -> float:
        """Size means SIZE — market cap for a company, book for a fund.

        This used to be driven by the DISCLOSED AMOUNT, which is how much
        somebody happened to trade rather than how big the thing is. A small
        name one fund bought heavily drew larger than Apple, and every
        institution came out identical because their books all saturated the
        same scale. Agreement still contributes a little, so a widely-bought
        small cap does not disappear, but it no longer sets the size.
        """
        if nd["kind"] == "ticker" and nd.get("market_cap"):
            c = self._amt_norm(nd["market_cap"], self.CAP_LO, self.CAP_HI)
            agree = 0.45 * min(nd.get("n_effective", nd.get("n_filers", 1)), 8)
            return self.CAP_R_LO + agree + (
                self.CAP_R_HI - self.CAP_R_LO) * c
        if nd["kind"] == "filer" and nd.get("source") == "f13" \
                and nd.get("book"):
            b = self._amt_norm(nd["book"], self.BOOK_LO, self.BOOK_HI)
            return 12.0 + 22.0 * b
        a = self._amt_norm(nd.get("amount"))
        if nd["kind"] == "ticker":
            agree = 1.8 * min(nd.get("n_effective", nd.get("n_filers", 1)), 10)
            return 7.0 + agree + 21.0 * a
        # Filers get their own money scale. Inside one cluster the spread is
        # narrow — SBLK's buyers run $57K to $2.1M — and on the ticker scale
        # (which has to reach $17bn) that whole range collapsed into two
        # pixels, so every person on screen was the same dot. Normalising
        # filers over $25K..$50M instead lets the difference show.
        fa = self._amt_norm(nd.get("amount"), 2.5e4, 5.0e7)
        return 5.0 + 1.1 * min(nd.get("weight", 1), 5) + 15.0 * fa

    def _edge_width(self, e):
        w = 0.8 + 5.8 * self._amt_norm(e.get("amount"))
        return max(0.8, w + (1.0 if e.get("option") else 0.0))

    # ── physics ──────────────────────────────
    def _step(self):
        n = len(self.nodes)
        if n < 2:
            return
        p = self._pos
        d = p[:, None, :] - p[None, :, :]
        dist2 = (d ** 2).sum(-1) + 1.0
        dist = np.sqrt(dist2)

        charge = 62000.0 * self._mass[:, None] * self._mass[None, :]
        rep = (d / dist[..., None]) * (charge / dist2)[..., None]
        np.einsum("iij->ij", rep)[...] = 0.0
        force = rep.sum(1)

        # Springs weighted DOWN by how connected each end is: a hub wired to
        # forty tickers must not apply forty full springs and drag them all
        # into one ball.
        for k, e in enumerate(self.edges):
            a, b = self._idx[e["a"]], self._idx[e["b"]]
            delta = p[b] - p[a]
            L = math.hypot(delta[0], delta[1]) + 1e-6
            v = delta / L * ((L - self._rest[k]) * self._k[k])
            force[a] += v
            force[b] -= v

        # Anisotropic centering: pull harder vertically than horizontally so
        # the cloud settles into an ellipse shaped like the canvas. A round
        # layout in a 1.83:1 window can only ever be fitted by its height,
        # which left half the width empty and squeezed the middle into a
        # knot — the spread was real, the framing was wasting it.
        force -= p * self._center_k
        self._vel = (self._vel + force * 0.55) * 0.78
        cap = 30.0
        sp = np.sqrt((self._vel ** 2).sum(-1)) + 1e-9
        over = sp > cap
        if over.any():
            self._vel[over] *= (cap / sp[over])[:, None]
        step = self._vel * self._temp
        step[self._pinned] = 0.0
        self._pos = p + step
        self._temp *= 0.982
        self._max_step = float(np.sqrt((step ** 2).sum(-1)).max())

    # ── render ───────────────────────────────
    def _isolate_positions(self, focus, near):
        """Redraw an isolated neighbourhood as a compact rosette.

        In the radial layout a stock sits at r~850 and the people who traded
        it sit at r~1300, spread right around their sector wedge. Isolating
        that and framing the bounding box gives a view three screens wide
        with one orb in the middle and its connections trailing off the
        edges — so you scroll, and a click on empty space drops the
        selection you were trying to read.

        So an isolated view is not a crop of the big picture. The selected
        node goes to the centre and its neighbours ring it closely, ordered
        by kind so institutions and people stay distinguishable. Nothing is
        off screen and no edge is longer than it needs to be. The real
        positions are untouched — this is a draw-time substitution.
        """
        pos = self._pos.copy()
        others = [i for i in near if i != focus]
        if not others:
            return pos
        pos[focus] = (0.0, 0.0)
        funds = [i for i in others
                 if self.nodes[i].get("source") == "f13"
                 or self.nodes[i]["kind"] == "ticker"]
        people = [i for i in others if i not in set(funds)]
        for group, rad in ((funds, 210.0), (people, 340.0)):
            if not group:
                continue
            group.sort(key=lambda j: -(self.nodes[j].get("amount") or 0))
            # Two rows once a ring would pack tighter than the orbs are wide.
            per = max(6, int(2 * math.pi * rad / 78.0))
            for p, i in enumerate(group):
                row, col = p // per, p % per
                n_row = min(per, len(group) - row * per)
                a = 2 * math.pi * (col + 0.5) / max(n_row, 1) - math.pi / 2
                r = rad + row * 120.0
                pos[i] = (r * math.cos(a), r * math.sin(a))
        return pos

    def _draw_positions(self):
        focus, near = self._focus_sets()
        if (self.isolate and self._selected is not None and near
                and len(near) > 1):
            return self._isolate_positions(focus, near)
        return self._pos

    def _focus_sets(self):
        focus = self._selected if self._selected is not None else self._hover
        near = set()
        if focus is not None:
            near = set(self._adj.get(focus, set())) | {focus}
        return focus, near

    def _visible(self, focus, near):
        """What to draw at all.

        With `isolate` on, a SELECTED node hides everything it is not
        connected to rather than dimming it. Dimming still leaves three
        hundred nodes and five hundred edges to composite, so the view
        stayed as busy and as slow as before; not drawing them makes the
        answer to "who is in this one" the only thing on screen, and cuts
        the frame to a fraction of the work.

        Hovering still only dims, because a hover is a glance and things
        should not vanish under the cursor.
        """
        if not self.isolate or self._selected is None or not near:
            return None
        return near

    def _render(self, quality: bool = False, cheap: bool = False):
        if not HAVE_PIL or not len(self.nodes):
            return
        W, H = self.winfo_width(), self.winfo_height()
        if W < 8 or H < 8:
            return
        ss = self._supersample() if quality else 1
        img = self._compose(W, H, ss, quality, cheap)
        self._photo = ImageTk.PhotoImage(img)
        if self._img_item is None:
            self._img_item = self.create_image(0, 0, anchor="nw",
                                               image=self._photo)
        else:
            self.itemconfig(self._img_item, image=self._photo)
            self.coords(self._img_item, 0, 0)

    def _supersample(self) -> int:
        """Supersample only when there is little on screen.

        A 2x pass on this display allocates a 3700x1952 buffer; every RGBA
        copy of that is 29 MB and the backdrop cache holds one too. That is
        worth paying to make an isolated neighbourhood of eight nodes look
        finished. It is not worth paying to smooth three hundred dots nobody
        is reading individually, and it was the single largest thing making
        the machine work.
        """
        focus, near = self._focus_sets()
        vis = self._visible(focus, near)
        n = len(vis) if vis is not None else len(self.nodes)
        if self.lite:
            return 1
        return self.SUPERSAMPLE if n <= 90 else 1

    def _compose(self, W, H, ss, quality, cheap=False):
        focus, near = self._focus_sets()
        vis = self._visible(focus, near)
        s = self._to_screen(self._draw_positions()) * ss
        r = self._radii * self._zoom * ss

        img = Image.new("RGB", (W * ss, H * ss), self._bgrgb)
        self._paint_backdrop(img, W * ss, H * ss, ss)
        layer = Image.new("RGBA", (W * ss, H * ss), (0, 0, 0, 0))
        dr = ImageDraw.Draw(layer, "RGBA")

        if self.layout_mode == "radial" and not cheap:
            self._paint_rings(dr, ss)

        # Edges. Alpha carries the money; a row with no figure stays a
        # hairline rather than being drawn at some assumed middle.
        for k, e in enumerate(self.edges):
            a, b = self._idx[e["a"]], self._idx[e["b"]]
            if vis is not None and (a not in vis or b not in vis):
                continue
            if focus is not None:
                on = a in near and b in near
                mul = 1.55 if on else 0.20
            else:
                mul = 1.0
            if self.by_direction:
                # In the combined view the question an edge answers is
                # "did this person buy or sell", not "which register are
                # they in" — the filer's own mark still carries the source.
                col = (0xA6, 0xE3, 0xA1) if e.get("buy") else (0xF3, 0x8B, 0xA8)
            else:
                col = SOURCE_COLOR.get(e.get("source"), DEFAULT_SRC)
            al = min(235, int((34 + 150 * self._amt_norm(e.get("amount")))
                              * mul))
            w = max(1, int(round(self._edge_width(e) * ss)))
            dr.line([tuple(s[a]), tuple(s[b])], fill=col + (al,), width=w)

        # Nodes, back to front so the big ones sit on top.
        order = sorted(range(len(self.nodes)), key=lambda i: r[i])
        for i in order:
            if vis is not None and i not in vis:
                continue
            nd = self.nodes[i]
            dim = 1.0 if (focus is None or i in near) else 0.22
            x, y = s[i]
            rad = max(1.5, r[i])
            if nd["kind"] == "ticker":
                if nd.get("direction") == "BOTH" and nd.get("lean") is not None:
                    col = lean_color(nd["lean"])
                else:
                    col = heat(nd.get("score", 0.0))
                gr = 0 if (cheap or self.lite) else int(rad * 6.4)
                if gr > 3:
                    g = _sprite("glow", gr, col, 0.42 * dim)
                    if g.width != gr:
                        g = g.resize((gr, gr), Image.BILINEAR)
                    layer.alpha_composite(g, (int(x - gr / 2),
                                              int(y - gr / 2)))
                sp = None
                if (self.logos and not self.lite
                        and rad >= LOGO_MIN_RADIUS * ss):
                    sp = _logo_sprite(nd["label"], int(rad * 2), col, dim)
                if sp is None:
                    sp = _sprite("ball", min(int(rad * 2), BALL_MAX_PX),
                                 col, dim)
                    if sp.width != int(rad * 2):
                        sp = sp.resize((max(2, int(rad * 2)),) * 2,
                                       Image.BILINEAR)
                layer.alpha_composite(sp, (int(x - rad), int(y - rad)))
                if i == focus:
                    dr.ellipse([x - rad - 3 * ss, y - rad - 3 * ss,
                                x + rad + 3 * ss, y + rad + 3 * ss],
                               outline=(255, 255, 255, 210),
                               width=max(1, ss))
            elif nd.get("source") == "f13":
                col = SOURCE_COLOR["f13"]
                pw = self._plate_w(i) * self._zoom * ss
                if pw >= 56:
                    pl = _plate(nd, int(pw), col, dim, self._fonts(ss))
                    layer.alpha_composite(
                        pl, (int(x - pl.width / 2), int(y - pl.height / 2)))
                    if i == focus:
                        dr.rounded_rectangle(
                            [x - pl.width / 2 - 3 * ss,
                             y - pl.height / 2 - 3 * ss,
                             x + pl.width / 2 + 3 * ss,
                             y + pl.height / 2 + 3 * ss],
                            radius=int(6 * ss),
                            outline=(255, 255, 255, 200), width=max(1, ss))
                    continue
                # Too small to read at this zoom — fall back to the block.
                sp = _sprite("fund", int(rad * 2.6), col, dim)
                layer.alpha_composite(sp, (int(x - rad * 1.3),
                                           int(y - rad * 1.3)))
                continue
            else:
                col = SOURCE_COLOR.get(nd.get("source"), DEFAULT_SRC)
                gr = 0 if (cheap or self.lite) else int(rad * 4.0)
                if gr > 3:
                    g = _sprite("glow", gr, col, 0.26 * dim)
                    if g.width != gr:
                        g = g.resize((gr, gr), Image.BILINEAR)
                    layer.alpha_composite(g, (int(x - gr / 2),
                                              int(y - gr / 2)))
                shape = "fund" if nd.get("source") == "f13" else "person"
                sz = int(rad * 2.6)
                sp = _sprite(shape, sz, col, dim)
                layer.alpha_composite(sp, (int(x - sz / 2), int(y - sz / 2)))

        # Paste the RGBA layer straight onto the RGB base using its own alpha.
        # `alpha_composite(img.convert("RGBA"), layer).convert("RGB")` was
        # allocating three extra full-canvas buffers — at 2x on this display
        # that is 29 MB each, released and recommitted on every frame, which
        # is what made the machine stall rather than merely the total.
        img.paste(layer, (0, 0), layer)
        del layer

        if quality and not self.lite:
            img = self._bloom(img)

        if not cheap:
            self._paint_labels(img, s, r, focus, near, ss, vis)
        if ss > 1:
            img = img.resize((W, H), Image.LANCZOS)
        return img

    RING_LABEL = (("mag7", playout.R_MAG7, "MEGA CAP"),
                  ("fund", playout.R_FUND_OUT, "INSTITUTIONS"),
                  ("stock", playout.R_STOCK[1], "STOCKS BY SECTOR"),
                  ("person", playout.R_PERSON[1], "PEOPLE"))

    def _paint_rings(self, dr, ss):
        """The scaffolding: one circle per band, one label per sector.

        Without these the rings are just a coincidence of spacing. Drawn
        faintly and underneath everything, because they are a reference
        grid rather than data.
        """
        c = self._to_screen(np.zeros((1, 2)))[0] * ss
        z = self._zoom * ss
        ax = self._aspect
        for _, rad, _ in self.RING_LABEL:
            r = rad * z
            if r < 8 or r > 1e5:
                continue
            dr.ellipse([c[0] - r * ax, c[1] - r, c[0] + r * ax, c[1] + r],
                       outline=(60, 74, 104, 90), width=max(1, ss))

        f, fs = self._fonts(ss)
        for s_name, mid, rad in playout.wedge_labels(self.nodes,
                                                     self.sectors):
            rr = (rad + 95.0) * z
            x, y = c[0] + rr * ax * math.cos(mid), c[1] + rr * math.sin(mid)
            dr.text((x, y), s_name.upper(), font=fs, anchor="mm",
                    fill=(120, 136, 172, 190))
            # a faint spoke at each wedge boundary
        for s_name, (lo, hi) in playout._wedges(
                self._sector_counts()).items():
            for a in (lo,):
                r0, r1 = playout.R_STOCK[0] * z * 0.86, (rad + 60.0) * z
                dr.line([c[0] + r0 * ax * math.cos(a),
                         c[1] + r0 * math.sin(a),
                         c[0] + r1 * ax * math.cos(a),
                         c[1] + r1 * math.sin(a)],
                        fill=(52, 64, 92, 70), width=max(1, ss))

    def _sector_counts(self):
        mag = set(playout.MAG7)
        out = {}
        for nd in self.nodes:
            if nd["kind"] != "ticker" or nd["label"].upper() in mag:
                continue
            s = self.sectors.get(nd["label"].upper(), "other")
            out[s] = out.get(s, 0) + 1
        return out

    def _paint_backdrop(self, img, w, h, ss):
        """A vertical wash and a vignette, so the field is not flat black.

        Cached on size. It is a pure function of the canvas dimensions and
        rebuilding it per frame cost 179 ms at 1x and 692 ms at 2x — by far
        the most expensive thing in a render that draws in twelve.
        """
        cache = getattr(self, "_backdrops", None)
        if cache is None:
            cache = self._backdrops = {}
        hit = cache.get((w, h))
        if hit is not None:
            img.paste(hit, (0, 0))
            return
        top = np.array([0x0A, 0x10, 0x1C], dtype=float)
        bot = np.array(self._bgrgb, dtype=float)
        ramp = np.linspace(0.0, 1.0, h)[:, None]
        col = (top[None, :] * (1 - ramp) + bot[None, :] * ramp)
        arr = np.repeat(col[:, None, :], w, axis=1)
        yy, xx = np.mgrid[0:h, 0:w]
        cx, cy = w / 2.0, h / 2.0
        d = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2)
        vig = np.clip(1.0 - 0.34 * np.clip(d - 0.42, 0, None) ** 1.6, 0.4, 1)
        arr *= vig[..., None]
        back = Image.fromarray(arr.astype(np.uint8), "RGB")
        # One full-canvas RGB buffer is 21 MB at 2x. Keeping several
        # sizes around was 29 MB of cache for two entries.
        if len(cache) > 2:
            cache.clear()
        cache[(w, h)] = back
        img.paste(back, (0, 0))

    @staticmethod
    def _bloom(img):
        """Additive bloom, computed at quarter resolution.

        Bloom is a low-frequency effect, so the blur does not need full
        pixels — only the result has to be full size. Thresholding and
        blurring a quarter-scale copy and scaling it back is visually the
        same and turns a 735 ms numpy pass into about forty.
        """
        w, h = img.size
        small = img.resize((max(1, w // 4), max(1, h // 4)), Image.BILINEAR)
        bright = small.point(lambda v: min(255, max(0, v - 118) * 2))
        blur = bright.filter(ImageFilter.GaussianBlur(4))
        return ImageChops.add(img, blur.resize((w, h), Image.BILINEAR))

    def _paint_labels(self, img, s, r, focus, near, ss, vis=None):
        dr = ImageDraw.Draw(img, "RGBA")
        f, fs = self._fonts(ss)
        for i, nd in enumerate(self.nodes):
            if vis is not None and i not in vis:
                continue
            dim = 1.0 if (focus is None or i in near) else 0.30
            x, y = s[i]
            if nd["kind"] == "ticker":
                txt, font = nd["label"], f
                ty = y + r[i] + 7 * ss
                col = (235, 240, 252)
            else:
                if not ((focus is not None and i in near)
                        or self._zoom > 1.9):
                    continue
                txt, font = nd["label"][:24], fs
                ty = y - r[i] - 9 * ss
                col = (150, 160, 185)
            a = int(255 * dim)
            # A dark halo so a label stays readable over an edge or a glow.
            for dx, dy in ((-ss, 0), (ss, 0), (0, -ss), (0, ss)):
                dr.text((x + dx, ty + dy), txt, font=font, anchor="mm",
                        fill=(4, 6, 11, int(a * 0.85)))
            dr.text((x, ty), txt, font=font, anchor="mm", fill=col + (a,))

    # ── loop ─────────────────────────────────
    def _schedule_quality(self):
        """Redraw fast now, redraw properly once the view stops moving."""
        self._render(quality=False)
        if self._quality_after:
            try:
                self.after_cancel(self._quality_after)
            except Exception:
                pass
        self._quality_after = self.after(
            self.QUALITY_DELAY, self._quality_now)

    def _schedule_quality_only(self):
        """Arm the good frame without paying for an immediate one."""
        if self._quality_after:
            try:
                self.after_cancel(self._quality_after)
            except Exception:
                pass
        self._quality_after = self.after(self.QUALITY_DELAY,
                                         self._quality_now)

    def _quality_now(self):
        self._quality_after = None
        if not self._running:
            self._render(quality=True)

    def _tick(self):
        if not self._running:
            return
        self._frame += 1
        for _ in range(4):
            self._step()
            self._settle_frames += 1
            if self._max_step < self.SETTLE_STEP:
                self._still += 1
            else:
                self._still = 0
            if self._still >= self.SETTLE_FRAMES:
                break
        self._render(quality=False)
        if (self._still >= self.SETTLE_FRAMES
                or self._settle_frames >= self.MAX_SETTLE_FRAMES):
            self._running = False
            self._after = None
            self._schedule_quality()
            if self.motion:
                self._idle()
            return
        self._after = self.after(24, self._tick)

    def _idle(self):
        """Frozen. Re-render slowly so hot nodes can breathe.

        Only armed when `motion` is on, because with a bitmap renderer a
        pulse is a whole frame — 22 ms every 140 ms is 16% of a core to
        animate three circles. Off by default; the still image is the point.
        """
        if self._running or not self.motion:
            return
        self._frame += 1
        self._render(quality=False)
        self._after = self.after(140, self._idle)

    def start(self):
        if self._running:
            return
        if self._after:
            try:
                self.after_cancel(self._after)
            except Exception:
                pass
            self._after = None
        self._running = True
        self._still = 0
        self._tick()

    def stop(self):
        self._running = False
        for attr in ("_after", "_quality_after"):
            h = getattr(self, attr, None)
            if h:
                try:
                    self.after_cancel(h)
                except Exception:
                    pass
                setattr(self, attr, None)

    def reheat(self, t: float = 0.9):
        """Re-solve the layout. Anything that disturbs it calls this."""
        if self.layout_mode == "radial":
            self._pos, self.ring = playout.radial_layout(
                self.nodes, self.edges, self.sectors,
                extent=self._extents())
            self._running = False
            self.reset_view()
            self._schedule_quality()
            return
        self._temp = max(self._temp, t)
        self._still = 0
        self._settle_frames = 0
        self.start()

    def set_layout(self, mode: str):
        self.layout_mode = mode
        if self.nodes:
            self.set_graph(self.nodes, self.edges)

    def set_lite(self, on: bool):
        """Strip the expensive parts: no glow, no bloom, no supersample,
        no logos. Everything the picture MEANS survives — positions,
        colours, sizes, edge weights. Only the finish goes.
        """
        self.lite = bool(on)
        if on:
            _SPRITE_CACHE.clear()
            _LOGO_SPRITES.clear()
            _LOGO_FACES.clear()
            self._backdrops = {}
        self._schedule_quality()

    def set_motion(self, on: bool):
        self.motion = bool(on)
        if not self._running:
            if on:
                self._idle()
            elif self._after:
                try:
                    self.after_cancel(self._after)
                except Exception:
                    pass
                self._after = None

    # ── interaction ──────────────────────────
    def _hit(self, x: float, y: float) -> Optional[int]:
        if not len(self.nodes):
            return None
        s = self._to_screen(self._draw_positions())
        d = np.sqrt(((s - np.array([x, y])) ** 2).sum(-1))
        pad = np.maximum(self._radii * self._zoom, 6.0) + 4.0
        # An institution is a card, so its clickable area is the card, not a
        # circle around its centre. Without this the corners of a plate did
        # nothing and the ring felt unresponsive at exactly the places it
        # looked most solid.
        for i, nd in enumerate(self.nodes):
            if nd.get("source") != "f13":
                continue
            pw = self._plate_w(i) * self._zoom
            ph = pw * PLATE_ASPECT
            if abs(x - s[i][0]) <= pw / 2 and abs(y - s[i][1]) <= ph / 2:
                d[i] = 0.0
                pad[i] = 1.0
        hits = np.where(d < pad)[0]
        if not len(hits):
            return None
        tick = [i for i in hits if self.nodes[i]["kind"] == "ticker"]
        pool = tick or list(hits)
        return min(pool, key=lambda i: d[i])

    def _on_motion(self, e):
        i = self._hit(e.x, e.y)
        if i != self._hover:
            self._hover = i
            # Hover only changes which nodes are dimmed, so it draws on the
            # CHEAP tier. Sweeping the mouse across the field used to fire a
            # full repaint per node passed under the cursor, which is what
            # made moving the mouse feel like the app had hung. The good
            # frame still lands a moment after you stop.
            self._render(cheap=True)
            self._schedule_quality_only()
            self.on_hover(self.nodes[i] if i is not None else None)
        self.config(cursor="hand2" if i is not None else "")

    def _on_press(self, e):
        i = self._hit(e.x, e.y)
        if i is not None:
            self._drag_node = i
            self._drag_moved = False
            self._pinned[i] = True
            self._selected = i
            if self.isolate:
                self._fit_isolated(set(self._adj.get(i, ())) | {i})
            self._schedule_quality()
            self.on_select(self.nodes[i])
        else:
            self._drag_view = (e.x, e.y, self._off.copy())
            if self._selected is not None:
                self._selected = None
                if self.isolate:
                    self.reset_view()        # bring the whole field back
                self._schedule_quality()
                self.on_select(None)

    def _on_drag(self, e):
        if self._drag_node is not None:
            if self.isolate and self._selected is not None:
                return          # positions are synthetic here; nothing to drag
            self._drag_moved = True
            self._pos[self._drag_node] = self._to_world((e.x, e.y))
            self._render(cheap=True)
        elif self._drag_view is not None:
            x0, y0, off0 = self._drag_view
            self._off = off0 - np.array([e.x - x0, e.y - y0]) / self._zoom
            self._render(cheap=True)

    def _on_release(self, _e):
        if self._drag_node is not None:
            self._pinned[self._drag_node] = False
            moved = self._drag_moved
            self._drag_node = None
            self._drag_moved = False
            # Only re-run the physics if the node was actually DRAGGED.
            # A plain click sets _drag_node too, so reheating here restarted
            # the whole simulation on every single click — which is why
            # selecting a node made the graph rearrange itself and stall.
            # Nothing has moved, so there is nothing to re-solve.
            if moved:
                self.reheat(0.5)
        self._drag_view = None
        self._schedule_quality()

    def _on_double(self, e):
        """Double-click opens the node under the cursor; empty space refits."""
        i = self._hit(e.x, e.y)
        if i is not None:
            self.on_open(self.nodes[i])
        else:
            self.reset_view()

    def _on_wheel(self, e):
        self._zoom_at(e.x, e.y, 1.12 if e.delta > 0 else 1 / 1.12)

    def _zoom_at(self, x, y, k):
        before = self._to_world((x, y))
        self._zoom = max(0.18, min(5.0, self._zoom * k))
        after = self._to_world((x, y))
        self._off += before - after
        self._schedule_quality()

    def _fit_isolated(self, idx):
        """Frame the rosette. It is centred on the origin by construction."""
        self._selected = next(iter(idx)) if len(idx) == 1 else self._selected
        pos = self._isolate_positions(self._selected, idx)
        pts = pos[sorted(idx)]
        w = max(self.winfo_width(), 400)
        h = max(self.winfo_height(), 300)
        # Fit each axis on its own. The view stretches x, so a span taken
        # as one symmetric number is wrong by exactly that factor — which
        # is the kind of error that crops the far edge of a rosette off
        # the screen and leaves you scrolling to find what you clicked.
        sx = float(np.abs(pts[:, 0]).max()) * 2.0 * self._aspect + 180.0
        sy = float(np.abs(pts[:, 1]).max()) * 2.0 + 180.0
        self._zoom = max(0.12, min(3.0, 0.92 * min(w / sx, h / sy)))
        self._off = np.array([0.0, 0.0])

    def _fit_to(self, idx):
        """Frame a subset of the graph without touching the layout.

        Only the camera moves. Isolating must never nudge a node, or the
        picture you clicked into is not the picture you were looking at.
        """
        if not idx:
            return
        pts = self._pos[sorted(idx)] * self._ax()
        lo, hi = pts.min(0), pts.max(0)
        span = np.maximum(hi - lo, 120.0)
        w = max(self.winfo_width(), 400)
        h = max(self.winfo_height(), 300)
        self._zoom = max(0.18, min(4.0,
                                   0.62 * min(w / span[0], h / span[1])))
        # _off lives in stretched space, the same as _to_screen expects.
        self._off = (lo + hi) / 2.0

    def reset_view(self):
        """Fit the CORE, not the bounding box.

        A couple of weakly-connected nodes drift far out and stretch the
        min/max box, which zooms the dense middle down to a knot in the
        centre of an empty canvas. The 3rd-to-97th percentile keeps the
        outliers visible without letting them set the scale.
        """
        if not len(self.nodes):
            return
        if self.layout_mode == "radial":
            # Fit the WHOLE disc. Percentiles are for a force layout whose
            # outliers are accidents; here the outer ring is the point, and
            # trimming it would crop off every person in the graph.
            w = max(self.winfo_width(), 400)
            h = max(self.winfo_height(), 300)
            # Capped well below the canvas ratio on purpose. The full 1.9
            # does use every pixel, but it flattens the rings into
            # horizontal bands and the bullseye stops reading as one. This
            # keeps most of the extra room while a ring still looks round.
            self._aspect = float(min(max(w / float(h), 1.0), 1.5))
            rx = float(np.abs(self._pos[:, 0]).max()) * self._aspect + 120.0
            ry = float(np.abs(self._pos[:, 1]).max()) + 120.0
            self._zoom = max(0.05, min(3.2, 0.94 * min(w / (2 * rx),
                                                       h / (2 * ry))))
            self._off = np.array([0.0, 0.0])
            self._schedule_quality()
            return
        lo = np.percentile(self._pos, 6, axis=0)
        hi = np.percentile(self._pos, 94, axis=0)
        span = np.maximum(hi - lo, 1.0)
        w = max(self.winfo_width(), 400)
        h = max(self.winfo_height(), 300)
        self._zoom = max(0.18, min(3.2, 0.86 * min(w / span[0], h / span[1])))
        self._off = (lo + hi) / 2.0
        self._schedule_quality()

    def focus_node(self, node_id: str):
        """Select a node by id — from the table, or from the search box.

        When isolation is on this must frame the ROSETTE, not pan to the
        node's position in the big layout. Selecting a row fires this right
        after a click has already framed the rosette, so panning here
        silently undid the fit and pushed half the neighbourhood back off
        the screen.
        """
        i = self._idx.get(node_id)
        if i is None:
            return
        self._selected = i
        if self.isolate:
            self._fit_isolated(set(self._adj.get(i, ())) | {i})
        else:
            self._off = self._pos[i].copy()
            self._zoom = max(self._zoom, 1.3)
        self._schedule_quality()
