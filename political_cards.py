"""
political_cards.py
==================
Floating detail cards — the deep dive you get by clicking a node.

Each card is a borderless Toplevel you can drag anywhere, and several can
be open at once, so two filers or a filer and a company can sit side by side
and be compared. Escape or the × closes one; the graph stays put underneath.

WHAT A CARD CAN AND CANNOT SAY
------------------------------
It reports **total disclosed value** — the sum of what a filer has actually
disclosed in the window — and never calls that net worth, because it is not.
Net worth lives in the annual Schedule A financial disclosure, a different
document this tool does not parse: of the 1,664 House filings in 2026, 395
are the periodic transaction reports read here and 807 are annual reports
that are not. Those carry asset RANGES rather than figures anyway, so even
parsed they would give a band, not a number.

Everything on a card comes from a filing. Where a number is derived rather
than disclosed — a 13F dollar value inferred from the quarter-end price, a
ticker matched to a company by name — the card says so.
"""

import json
import webbrowser
import tkinter as tk
from tkinter import ttk
from datetime import date, datetime, timedelta

import political_engine as pe
import political_clusters as pc
import political_reference as pref

BG = "#0B111B"
BG2 = "#121A27"
BG3 = "#1A2433"
FG = "#D6DEF5"
FG_DIM = "#7A849E"
ACCENT = "#F4C430"
GREEN = "#A6E3A1"
RED = "#F38BA8"
YELLOW = "#F9E2AF"
TEAL = "#94E2D5"
VIOLET = "#A78BFA"
ORANGE = "#F5A97F"
BORDER = "#26314A"

F_XS = ("Consolas", 8)
F_SM = ("Consolas", 9)
F_MD = ("Consolas", 10, "bold")
F_LG = ("Consolas", 13, "bold")
F_XL = ("Consolas", 19, "bold")

SRC_LABEL = {"house_ptr": "House", "senate_ptr": "Senate",
             "form4": "Form 4", "f13": "13F"}
SRC_COLOR = {"house_ptr": ACCENT, "senate_ptr": ORANGE,
             "form4": TEAL, "f13": VIOLET}

_OPEN: list = []
_CASCADE = [0]


def money(v):
    if not v:
        return "--"
    for cut, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= cut:
            return "${:,.2f}{}".format(v / cut, suf).replace(".00", "")
    return "${:,.0f}".format(v)


def pct(v, nd=1):
    return "--" if v is None else "{:+.{}f}%".format(100.0 * v, nd)


def close_all():
    for c in list(_OPEN):
        try:
            c.close()
        except Exception:
            pass


class Card(tk.Toplevel):
    """A draggable, borderless panel. Several may be open at once."""

    W = 470
    H = 720

    def __init__(self, master, title: str):
        super().__init__(master)
        self.overrideredirect(True)
        self.configure(bg=BORDER)
        self.attributes("-topmost", True)
        n = _CASCADE[0] % 6
        _CASCADE[0] += 1
        try:
            x = master.winfo_rootx() + master.winfo_width() - self.W - 60 - n * 26
            y = master.winfo_rooty() + 90 + n * 26
        except Exception:
            x, y = 200, 140
        self.geometry("{}x{}+{}+{}".format(self.W, self.H, max(0, x),
                                           max(0, y)))

        outer = tk.Frame(self, bg=BG, bd=0)
        outer.pack(fill="both", expand=True, padx=1, pady=1)

        self.bar = tk.Frame(outer, bg=BG2, height=30)
        self.bar.pack(fill="x")
        self.bar.pack_propagate(False)
        tk.Label(self.bar, text=title, font=F_XS, bg=BG2,
                 fg=FG_DIM).pack(side="left", padx=12)
        tk.Button(self.bar, text="✕", font=F_SM, bg=BG2, fg=FG_DIM,
                  activebackground=BG3, activeforeground=RED, relief="flat",
                  bd=0, cursor="hand2", command=self.close).pack(side="right",
                                                                 padx=8)
        for w in (self.bar,):
            w.bind("<Button-1>", self._grab)
            w.bind("<B1-Motion>", self._move)
        self.bind("<Escape>", lambda e: self.close())

        # Scrollable body. A filer on six committees with a dozen holdings
        # and a forward record runs well past any fixed height, and a card
        # that silently clips its own content is worse than a short one.
        shell = tk.Frame(outer, bg=BG)
        shell.pack(fill="both", expand=True)
        self._cv = tk.Canvas(shell, bg=BG, highlightthickness=0, bd=0)
        vs = ttk.Scrollbar(shell, orient="vertical", command=self._cv.yview)
        self.body = tk.Frame(self._cv, bg=BG)
        self._win = self._cv.create_window((0, 0), window=self.body,
                                           anchor="nw")
        self._cv.configure(yscrollcommand=vs.set)
        self._cv.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        def _sync(_e=None):
            self._cv.configure(scrollregion=self._cv.bbox("all"))
            self._cv.itemconfig(self._win, width=self._cv.winfo_width())
        self.body.bind("<Configure>", _sync)
        self._cv.bind("<Configure>", _sync)
        for w in (self._cv, self.body):
            w.bind("<MouseWheel>", lambda e: self._cv.yview_scroll(
                -1 if e.delta > 0 else 1, "units"))

        _OPEN.append(self)
        self.lift()

    def _grab(self, e):
        self._ox, self._oy = e.x_root - self.winfo_x(), e.y_root - self.winfo_y()

    def _move(self, e):
        self.geometry("+{}+{}".format(e.x_root - self._ox, e.y_root - self._oy))

    def close(self):
        if self in _OPEN:
            _OPEN.remove(self)
        self.destroy()

    # ── little builders ──────────────────────
    def head(self, parent, text, color=ACCENT):
        tk.Label(parent, text=text, font=F_XS, bg=parent["bg"], fg=color,
                 anchor="w").pack(fill="x", padx=16, pady=(12, 3))

    def kv(self, parent, rows, cols=2):
        grid = tk.Frame(parent, bg=parent["bg"])
        grid.pack(fill="x", padx=16, pady=(0, 2))
        for i, (k, v, col) in enumerate(rows):
            cell = tk.Frame(grid, bg=BG2)
            cell.grid(row=i // cols, column=i % cols, sticky="ew",
                      padx=2, pady=2, ipadx=8, ipady=5)
            grid.columnconfigure(i % cols, weight=1)
            tk.Label(cell, text=k, font=F_XS, bg=BG2, fg=FG_DIM,
                     anchor="w").pack(fill="x")
            tk.Label(cell, text=v, font=F_MD, bg=BG2, fg=col,
                     anchor="w").pack(fill="x")

    def bars(self, parent, series, height=54):
        """Monthly activity, drawn small. Rectangles are enough here."""
        cv = tk.Canvas(parent, bg=BG, height=height, highlightthickness=0)
        cv.pack(fill="x", padx=16, pady=(2, 6))

        def paint(_e=None):
            cv.delete("all")
            w = cv.winfo_width() or (self.W - 32)
            if not series:
                return
            top = max(abs(v) for _, v in series) or 1
            n = len(series)
            # Cap the bar width. With one month of data an uncapped bar
            # spans the whole card and reads as a solid block rather than
            # as a chart of a single observation.
            slot = (w - 4) / float(n)
            bw = max(3.0, min(slot - 3, 46.0))
            x0 = 2 + max(0.0, (w - 4 - n * slot) / 2.0)
            base = height - 13
            cv.create_line(2, base, w - 2, base, fill=BORDER)
            for i, (lab, v) in enumerate(series):
                x = x0 + i * slot + (slot - bw) / 2.0
                h = max(2.0, (abs(v) / top) * (height - 20))
                cv.create_rectangle(x, base - h, x + bw, base,
                                    fill=(GREEN if v >= 0 else RED),
                                    outline="")
                if n <= 14 or i % max(1, n // 8) == 0:
                    cv.create_text(x + bw / 2, height - 5, text=lab,
                                   font=("Consolas", 7), fill=FG_DIM)
        cv.bind("<Configure>", paint)
        self.after(30, paint)

    def table(self, parent, cols, widths, rows, height=7, anchors=None):
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="x", padx=12, pady=(0, 6))
        st = ttk.Style()
        st.configure("Card.Treeview", background=BG2, fieldbackground=BG2,
                     foreground=FG, rowheight=21, font=F_XS, borderwidth=0)
        st.configure("Card.Treeview.Heading", background=BG3,
                     foreground=ACCENT, font=("Consolas", 7), relief="flat")
        # Same Tk 8.6.9 tag-colour bug as political_views.style_tree — the
        # default map's ('!disabled', '!selected', ...) entry overrides
        # tag_configure, so without this every row on a card is one colour.
        from political_views import fixed_map
        st.map("Card.Treeview",
               background=fixed_map(st, "background") + [("selected", BG3)],
               foreground=fixed_map(st, "foreground") + [("selected", ACCENT)])
        t = ttk.Treeview(wrap, columns=[c for c, _ in cols], show="headings",
                         style="Card.Treeview", height=height)
        for (c, lab), w in zip(cols, widths):
            t.heading(c, text=lab)
            t.column(c, width=w, anchor=(anchors or {}).get(c, "w"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.pack(side="left", fill="x", expand=True)
        sb.pack(side="right", fill="y")
        t.tag_configure("buy", foreground=GREEN)
        t.tag_configure("sell", foreground=RED)
        t.tag_configure("dim", foreground=FG_DIM)
        for vals, tag in rows:
            t.insert("", "end", values=vals, tags=(tag,) if tag else ())
        return t

    def note(self, parent, text, color=FG_DIM):
        tk.Label(parent, text=text, font=("Consolas", 7), bg=BG, fg=color,
                 anchor="w", justify="left",
                 wraplength=self.W - 34).pack(fill="x", padx=16, pady=(0, 6))

    def buttons(self, parent, specs):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=14, pady=(4, 12))
        for text, cmd, primary in specs:
            tk.Button(row, text=text, command=cmd, font=F_XS,
                      bg=(ACCENT if primary else BG3),
                      fg=("#000000" if primary else FG),
                      activebackground=BORDER, relief="flat", bd=0,
                      cursor="hand2", padx=12,
                      pady=5).pack(side="left", padx=3)


# ─────────────────────────────────────────────
# FILER
# ─────────────────────────────────────────────

def _role_line(p) -> tuple:
    """What this filer actually is, in one line."""
    src = p.get("source")
    if src in ("house_ptr", "senate_ptr"):
        seat = "Senator" if src == "senate_ptr" else "Representative"
        bits = seat
        if p.get("party") or p.get("state"):
            bits += "  ({}-{})".format(p.get("party") or "?",
                                       p.get("state") or "?")
        return bits, SRC_COLOR[src]
    if src == "form4":
        roles = p.get("committees") or []
        return ("Corporate insider — " + ", ".join(roles[:2])
                if roles else "Corporate insider"), TEAL
    if src == "f13":
        return "Institutional manager (13F filer)", VIOLET
    return (src or "filer"), FG_DIM


def _institution_profile(c, b, conn, name):
    """The reference block for a 13F filer, and the caveat on its number.

    The card on the cluster map carries a name, a figure and a principal,
    which is all a mark that size can hold. The rest belongs here — and so
    does the warning, which has nowhere else to live. A reader who sees
    "$37.4B" beside Bridgewater will read it as what Bridgewater runs. It
    is a fraction of that: a 13F covers US-listed long equity over $100m
    and nothing else. The map has no room to say so, so this card must.
    """
    info = pref.about(name)
    book = pe.fund_values(conn).get(name)
    rows = []
    if info.get("principal"):
        rows.append(("principal", info["principal"], FG))
    if info.get("style"):
        rows.append(("style", info["style"], FG_DIM))
    if book:
        rows.append((pref.BOOK_LABEL.lower(), money(book), VIOLET))
    if not rows:
        return
    c.head(b, "INSTITUTION")
    c.kv(b, rows)
    if book:
        c.note(b, pref.BOOK_NOTE, YELLOW)
    if info:
        # Everything else in this toolkit traces back to a document. This
        # does not, so it says so rather than borrowing their credibility.
        c.note(b, "Principal and style are hand-maintained reference, not "
                  "read from a filing (reviewed {}).".format(pref.REVIEWED))


def open_filer(master, conn, name: str, navigate=None):
    p = pc.filer_profile(conn, name)
    if not p:
        return None
    c = Card(master, "FILER")
    b = c.body

    tk.Label(b, text=(p["names"][0] if p["names"] else name)[:30],
             font=F_XL, bg=BG, fg=FG, anchor="w").pack(fill="x", padx=16,
                                                       pady=(12, 0))
    role, rcol = _role_line(p)
    tk.Label(b, text=role, font=F_SM, bg=BG, fg=rcol, anchor="w",
             wraplength=c.W - 34, justify="left").pack(fill="x", padx=16)

    if p.get("source") == "f13":
        _institution_profile(c, b, conn, name)

    total = sum(r.get("amount_lo") or 0 for r in p["rows"])
    est = sum(1 for r in p["rows"]
              if "amount_estimated" in (json.loads(r.get("parse_flags")
                                                   or "[]")))
    c.head(b, "DISCLOSED IN THIS STORE")
    c.kv(b, [
        ("total disclosed value", money(total), FG),
        ("distinct names", str(len(p["tickers"])), FG),
        ("disclosures", str(p["n"]), FG),
        ("buys / sells", "{} / {}".format(p["n_buys"], p["n_sells"]),
         GREEN if p["n_buys"] >= p["n_sells"] else RED),
        ("discloses after", ("{:.0f} days".format(p["mean_lag"])
                             if p.get("mean_lag") is not None else "--"),
         YELLOW),
        ("with dated options", str(p.get("options") or 0),
         YELLOW if p.get("options") else FG_DIM),
    ])
    c.note(b, "Total disclosed value is the sum of what this filer reported "
              "in this window. It is NOT net worth — that lives in the annual "
              "Schedule A report, which this tool does not parse, and which "
              "discloses ranges rather than figures anyway."
              + ("  {} of these rows carry a 13F value inferred from the "
                 "quarter-end price rather than a disclosed amount."
                 .format(est) if est else ""))

    if p.get("committees"):
        c.head(b, "COMMITTEES")
        tk.Label(b, text="\n".join("· " + x for x in p["committees"][:6]),
                 font=F_XS, bg=BG, fg=FG_DIM, anchor="w", justify="left",
                 wraplength=c.W - 40).pack(fill="x", padx=18)

    # Monthly activity, by disclosed value
    by_month = {}
    for r in p["rows"]:
        d = (r.get("notify_date") or "")[:7]
        if not d:
            continue
        v = (r.get("amount_lo") or 0) * (1 if r.get("txn_type") == "P" else -1)
        by_month[d] = by_month.get(d, 0) + v
    series = [(k[5:], v) for k, v in sorted(by_month.items())][-14:]
    if series:
        c.head(b, "NET DISCLOSED VALUE BY MONTH   (green bought, red sold)")
        c.bars(b, series)

    c.head(b, "MOST TRADED")
    rows = []
    for t in p["tickers"][:40]:
        rows.append(((t["ticker"], t["buys"], t["sells"],
                      money(t["max_amount"]), t["newest"]),
                     "buy" if t["buys"] > t["sells"] else "sell"))
    c.table(b, [("t", "ticker"), ("b", "buys"), ("s", "sells"),
                ("a", "largest"), ("n", "newest")],
            [70, 44, 44, 92, 96], rows, height=6,
            anchors={"b": "e", "s": "e", "a": "e"})

    if p.get("perf"):
        c.head(b, "WHAT THEIR BUYS DID NEXT")
        prows = []
        for k in sorted(p["perf"], key=lambda k: (k.split("_")[1],
                                                  int(k.split("d")[0]))):
            v = p["perf"][k]
            lab = k.replace("_notify", " from disclosure").replace(
                "_trade", " from the trade")
            prows.append(((lab, v["n"], pct(v["mean"]),
                           "{:.0%}".format(v["hit"])),
                          "buy" if v["mean"] > 0 else "sell"))
        c.table(b, [("w", "window"), ("n", "n"), ("m", "mean"), ("h", "hit")],
                [170, 44, 78, 56], prows, height=4,
                anchors={"n": "e", "m": "e", "h": "e"})
        c.note(b, "No rank, deliberately. The permutation test on the Record "
                  "tab says the leader of any per-filer ranking is noise at "
                  "this sample size, and a number printed beside a position "
                  "reads as a recommendation however it is captioned.")

    latest = p["rows"][0] if p["rows"] else {}
    c.buttons(b, [
        ("full profile →",
         lambda: (navigate and navigate("filer", name), c.close()), True),
        ("latest filing",
         lambda: latest.get("doc_url") and webbrowser.open(latest["doc_url"]),
         False),
    ])
    return c


# ─────────────────────────────────────────────
# TICKER
# ─────────────────────────────────────────────

def open_ticker(master, conn, ticker: str, navigate=None):
    p = pc.ticker_profile(conn, ticker, days=400)
    if not p.get("rows"):
        return None
    c = Card(master, "COMPANY")
    b = c.body

    tk.Label(b, text=p["ticker"], font=F_XL, bg=BG, fg=ACCENT,
             anchor="w").pack(fill="x", padx=16, pady=(12, 0))
    tk.Label(b, text=(p.get("asset_name") or "")[:60], font=F_SM, bg=BG,
             fg=FG_DIM, anchor="w", wraplength=c.W - 34,
             justify="left").pack(fill="x", padx=16)

    total = sum(r.get("amount_lo") or 0 for r in p["rows"])
    c.head(b, "WHO IS IN IT")
    c.kv(b, [
        ("total disclosed", money(total), FG),
        ("disclosures", str(p["n"]), FG),
        ("buyers", str(len(p["buyers"])), GREEN),
        ("sellers", str(len(p["sellers"])), RED),
    ])
    srows = []
    for s, d in sorted(p["by_source"].items()):
        srows.append(((SRC_LABEL.get(s, s), d["buys"], d["sells"],
                       d["filers"]),
                      "buy" if d["buys"] >= d["sells"] else "sell"))
    c.table(b, [("s", "source"), ("b", "buys"), ("x", "sells"),
                ("f", "filers")],
            [110, 60, 60, 60], srows, height=4,
            anchors={"b": "e", "x": "e", "f": "e"})

    if p.get("options"):
        c.head(b, "DATED OPTIONS", YELLOW)
        orows = [(((r.get("filer_name") or "")[:22],
                   r.get("option_type") or "?",
                   "${:g}".format(r["strike"]) if r.get("strike") else "?",
                   r.get("expiry") or "?"), None)
                 for r in p["options"][:6]]
        c.table(b, [("w", "filer"), ("t", "type"), ("k", "strike"),
                    ("e", "expiry")], [150, 50, 70, 90], orows, height=3)

    inf = p.get("influence") or {}
    if inf:
        c.head(b, "WASHINGTON FOOTPRINT")
        c.kv(b, [
            ("lobbying {}".format(inf.get("year")),
             money(inf.get("lobby_spend")), FG),
            ("year on year",
             ("--" if inf.get("lobby_growth") is None
              else "{:+.0%}".format(inf["lobby_growth"])),
             GREEN if (inf.get("lobby_growth") or 0) > 0 else RED),
            ("federal awards", money(inf.get("contract_total")), FG),
            ("contracts", str(inf.get("contract_count") or 0), FG),
        ])
        c.note(b, "Matched to the lobbying register and the federal award "
                  "system on the name '{}' — a join on a string, not an "
                  "identifier.".format(inf.get("matched_name")))

    c.head(b, "EVERY DISCLOSURE")
    rows = []
    for r in p["rows"][:60]:
        rows.append(((r.get("notify_date"), (r.get("filer_name") or "")[:20],
                      SRC_LABEL.get(r.get("source"), ""), r.get("txn_type"),
                      money(r.get("amount_lo"))),
                     "buy" if r.get("txn_type") == "P" else "sell"))
    c.table(b, [("d", "disclosed"), ("w", "filer"), ("s", "src"),
                ("t", "txn"), ("a", "amount")],
            [84, 140, 50, 56, 86], rows, height=7, anchors={"a": "e"})

    c.buttons(b, [
        ("full profile →",
         lambda: (navigate and navigate("ticker", p["ticker"]), c.close()),
         True),
        ("latest filing",
         lambda: p["rows"][0].get("doc_url")
         and webbrowser.open(p["rows"][0]["doc_url"]), False),
    ])
    return c
