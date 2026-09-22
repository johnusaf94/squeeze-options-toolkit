"""
political_views.py
==================
The four views that turn the disclosure store into something you can ask a
question of: Clusters, Ticker, Filer and Movers. Each is a class that builds
itself into a frame, so `political_gui.py` stays a shell that wires them
together rather than a thousand lines of widget code.

WHAT EACH ONE IS FOR
--------------------
    Clusters   which names several INDEPENDENT people bought at once,
               drawn as the bipartite network it actually is
    Ticker     one stock's whole political and insider footprint
    Filer      one person or fund's record, with n attached and no rank
    Movers     most bought and most sold, counted by distinct filers

WHAT THEY REFUSE TO DO
----------------------
None of them ranks people by forward return. `political_engine`'s own
permutation test says the leader of any such ranking is noise at this
sample size, and the Filer view therefore shows a filer's returns with
their n and no comparison to anybody else — a number next to a rank reads
as a recommendation no matter what the caption says.

Every score shown is broken into its named parts with the weight printed
beside each one, the same convention `value_analysis_gui.py` uses for the
value meter, and for the same reason: a single number nobody can take apart
gets trusted more than it has earned.
"""

import json
import webbrowser
import tkinter as tk
from tkinter import ttk
from datetime import date, datetime, timedelta

import political_engine as pe
import political_clusters as pc
import political_graph as pgraph
import political_cards as cards

BG = "#05070C"
BG2 = "#0C1119"
BG3 = "#141B26"
BG4 = "#1B2432"
FG = "#CDD6F4"
FG_DIM = "#6C7086"
ACCENT = "#F4C430"
GREEN = "#A6E3A1"
RED = "#F38BA8"
YELLOW = "#F9E2AF"
BLUE = "#89B4FA"
TEAL = "#94E2D5"
VIOLET = "#A78BFA"
ORANGE = "#F5A97F"
BORDER = "#222B3A"

F_XS = ("Consolas", 8)
F_SM = ("Consolas", 9)
F = ("Consolas", 10)
F_MD = ("Consolas", 11, "bold")
F_LG = ("Consolas", 14, "bold")
F_XL = ("Consolas", 22, "bold")
F_HUGE = ("Consolas", 34, "bold")

SRC_COLOR = {"house_ptr": ACCENT, "senate_ptr": ORANGE,
             "form4": TEAL, "f13": VIOLET}
SRC_LABEL = {"house_ptr": "House", "senate_ptr": "Senate",
             "form4": "Form 4", "f13": "13F"}


def money(v):
    if not v:
        return "--"
    if v >= 1e9:
        return "${:.2f}B".format(v / 1e9)
    if v >= 1e6:
        return "${:.1f}M".format(v / 1e6)
    if v >= 1e3:
        return "${:,.0f}K".format(v / 1e3)
    return "${:,.0f}".format(v)


def bucket(lo, hi):
    if lo is None:
        return "--"
    if hi is None:
        return "over " + money(lo)
    return "{} – {}".format(money(lo), money(hi))


def pct(v, nd=2):
    return "--" if v is None else "{:+.{}f}%".format(100.0 * v, nd)


def ago(d):
    try:
        n = (date.today() - date.fromisoformat(str(d)[:10])).days
        return "today" if n <= 0 else "{}d ago".format(n)
    except Exception:
        return ""


def btn(parent, text, cmd, fg=FG, bg=BG3, font=F_SM, **kw):
    return tk.Button(parent, text=text, command=cmd, font=font, bg=bg, fg=fg,
                     activebackground=BG4, activeforeground=ACCENT,
                     relief="flat", cursor="hand2", padx=12, pady=5, bd=0, **kw)


def head(parent, text, color=ACCENT, font=F_SM, **kw):
    return tk.Label(parent, text=text, font=font, bg=parent["bg"],
                    fg=color, anchor="w", **kw)


def fixed_map(style, option, base="Treeview"):
    """Strip the state specs that kill Treeview tag colours.

    Tk 8.6.9 ships a default map containing ('!disabled', '!selected',
    'black'). A three-element entry like that matches ordinary rows and
    overrides whatever `tag_configure` set, so every per-row colour in the
    app silently did nothing — the combined view's buys and sells rendered
    identically because neither tag was ever painted, not because the tags
    were wrong.

    Keeping only the one-element state specs restores tag colours and leaves
    selection highlighting intact. This is the long-standing workaround for
    Tk bug 1733220.
    """
    return [e for e in style.map(base, query_opt=option) if len(e[:-1]) != 2]


def style_tree(name="View.Treeview"):
    st = ttk.Style()
    st.configure(name, background=BG2, fieldbackground=BG2, foreground=FG,
                 rowheight=22, font=F_SM, borderwidth=0)
    st.configure(name + ".Heading", background=BG3, foreground=ACCENT,
                 font=F_XS, relief="flat")
    st.map(name,
           background=fixed_map(st, "background") + [("selected", BG4)],
           foreground=fixed_map(st, "foreground") + [("selected", ACCENT)])
    return name


class _Base:
    def __init__(self, parent, conn, navigate=None):
        self.parent = parent
        self.conn = conn
        self.navigate = navigate or (lambda tab, arg=None: None)
        self.frame = tk.Frame(parent, bg=BG)
        self.frame.pack(fill="both", expand=True)

    def _scroll_tree(self, parent, cols, widths, anchors=None, height=None):
        wrap = tk.Frame(parent, bg=BG)
        style_tree()
        t = ttk.Treeview(wrap, columns=[c for c, _ in cols], show="headings",
                         style="View.Treeview", selectmode="browse",
                         **({"height": height} if height else {}))
        for (c, lab), w in zip(cols, widths):
            t.heading(c, text=lab)
            t.column(c, width=w, anchor=(anchors or {}).get(c, "w"),
                     stretch=(c in ("asset", "name", "asset_name")))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        t.tag_configure("buy", foreground=GREEN)
        t.tag_configure("sell", foreground=RED)
        t.tag_configure("opt", foreground=YELLOW)
        t.tag_configure("dim", foreground=FG_DIM)
        return wrap, t


# ═════════════════════════════════════════════
# CLUSTERS
# ═════════════════════════════════════════════

class ClustersView(_Base):
    """The network. Filers on the outside, tickers in the middle.

    Two tickers drift together exactly when the same people bought both.
    That adjacency is the entire reason this is a picture and not a table,
    and it is the only view here that shows a relationship between two
    different names rather than a fact about one.
    """

    def __init__(self, parent, conn, navigate=None):
        _Base.__init__(self, parent, conn, navigate)
        self.v_days = tk.StringVar(value="90")
        self.v_min = tk.StringVar(value="2")
        self.v_dir = tk.StringVar(value="P")
        self.v_sector = tk.StringVar(value="all")
        self.v_find = tk.StringVar(value="")
        self.v_src = {k: tk.BooleanVar(value=True) for k in SRC_LABEL}
        self.clusters = []
        self._build()
        self.reload()

    def _build(self):
        rail = tk.Frame(self.frame, bg=BG2, width=224)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        self._build_rail(rail)
        tk.Frame(self.frame, bg=BORDER, width=1).pack(side="left", fill="y")

        right = tk.Frame(self.frame, bg=BG2, width=452)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self._build_detail(right)
        tk.Frame(self.frame, bg=BORDER, width=1).pack(side="right", fill="y")

        mid = tk.Frame(self.frame, bg=BG)
        mid.pack(side="left", fill="both", expand=True)

        bar = tk.Frame(mid, bg=BG, height=34)
        bar.pack(fill="x", padx=14, pady=(8, 0))
        bar.pack_propagate(False)
        self.title = tk.Label(bar, text="CLUSTERS", font=F_LG, bg=BG,
                              fg=FG, anchor="w")
        self.title.pack(side="left")
        self.sub = tk.Label(bar, text="", font=F_SM, bg=BG, fg=FG_DIM,
                            anchor="w")
        self.sub.pack(side="left", padx=14)
        self.legend = []
        for lab, k in (("Form 4", "form4"), ("13F", "f13"),
                       ("Senate", "senate_ptr"), ("House", "house_ptr")):
            w = tk.Label(bar, text="●  " + lab, font=F_XS, bg=BG,
                         fg=SRC_COLOR[k])
            w.pack(side="right", padx=7)
            self.legend.append(w)
        self.legend2 = []
        for lab, col in (("net sold", RED), ("contested", ACCENT),
                         ("net bought", GREEN)):
            w = tk.Label(bar, text="●  " + lab, font=F_XS, bg=BG, fg=col)
            self.legend2.append(w)

        self.canvas = pgraph.NetworkCanvas(
            mid, on_select=self._on_select, on_hover=self._on_hover,
            on_open=self.open_card, bg=BG)
        self.canvas.pack(fill="both", expand=True, padx=14, pady=8)

        strip = tk.Frame(mid, bg=BG2, height=186)
        strip.pack(fill="x", padx=14, pady=(0, 10))
        strip.pack_propagate(False)
        _, self.top = self._scroll_tree(
            strip,
            [("rank", "#"), ("ticker", "ticker"), ("score", "score"),
             ("filers", "filers"), ("eff", "effective"),
             ("srcs", "sources"), ("newest", "newest"), ("name", "asset")],
            [34, 74, 60, 56, 68, 150, 96, 420],
            {"rank": "e", "score": "e", "filers": "e", "eff": "e"})
        self.top.master.pack(fill="both", expand=True)
        self.top.bind("<<TreeviewSelect>>", self._on_pick_row)
        self.top.bind("<Double-1>", lambda e: self._open_ticker())

        self.status = tk.Label(mid, text="", font=F_XS, bg=BG, fg=FG_DIM,
                               anchor="w")
        self.status.pack(fill="x", padx=16, pady=(0, 6))

    def _build_rail(self, p):
        head(p, "WINDOW", font=F_SM).pack(fill="x", padx=14, pady=(14, 2))
        for lab, val in (("30 days", "30"), ("90 days", "90"),
                         ("180 days", "180"), ("1 year", "365")):
            tk.Radiobutton(p, text=lab, variable=self.v_days, value=val,
                           command=self.reload, font=F_SM, bg=BG2, fg=FG,
                           selectcolor=BG4, activebackground=BG2,
                           activeforeground=ACCENT, anchor="w", bd=0,
                           highlightthickness=0).pack(fill="x", padx=18)

        head(p, "MINIMUM FILERS", font=F_SM).pack(fill="x", padx=14,
                                                  pady=(14, 2))
        for lab in ("2", "3", "4", "5"):
            tk.Radiobutton(p, text=lab + "+ independent", variable=self.v_min,
                           value=lab, command=self.reload, font=F_SM, bg=BG2,
                           fg=FG, selectcolor=BG4, activebackground=BG2,
                           activeforeground=ACCENT, anchor="w", bd=0,
                           highlightthickness=0).pack(fill="x", padx=18)

        head(p, "SIDE", font=F_SM).pack(fill="x", padx=14, pady=(14, 2))
        for lab, val in (("buying", "P"), ("selling", "S"),
                         ("both — net lean", "BOTH")):
            tk.Radiobutton(p, text=lab, variable=self.v_dir, value=val,
                           command=self.reload, font=F_SM, bg=BG2, fg=FG,
                           selectcolor=BG4, activebackground=BG2,
                           activeforeground=ACCENT, anchor="w", bd=0,
                           highlightthickness=0).pack(fill="x", padx=18)
        tk.Label(p, text="most insider selling is a\ncalendar, not a view",
                 font=F_XS, bg=BG2, fg=FG_DIM, anchor="w", wraplength=182,
                 justify="left").pack(fill="x", padx=18, pady=(2, 0))

        head(p, "FIND", font=F_SM).pack(fill="x", padx=14, pady=(12, 2))
        e = tk.Entry(p, textvariable=self.v_find, font=F_MD, bg=BG3, fg=FG,
                     insertbackground=ACCENT, relief="flat", bd=5)
        e.pack(fill="x", padx=16)
        e.bind("<Return>", lambda ev: self.find())
        e.bind("<KeyRelease>", self._find_hint)
        self.find_msg = tk.Label(p, text="ticker, then Enter", font=F_XS,
                                 bg=BG2, fg=FG_DIM, anchor="w",
                                 wraplength=190, justify="left")
        self.find_msg.pack(fill="x", padx=18, pady=(2, 0))

        head(p, "INDUSTRY", font=F_SM).pack(fill="x", padx=14, pady=(14, 2))
        for name in pe.SECTOR_ORDER:
            tk.Radiobutton(p, text=name, variable=self.v_sector, value=name,
                           command=self.reload, font=F_SM, bg=BG2, fg=FG,
                           selectcolor=BG4, activebackground=BG2,
                           activeforeground=ACCENT, anchor="w", bd=0,
                           highlightthickness=0).pack(fill="x", padx=18)

        head(p, "SOURCES", font=F_SM).pack(fill="x", padx=14, pady=(14, 2))
        for k, lab in SRC_LABEL.items():
            tk.Checkbutton(p, text=lab, variable=self.v_src[k],
                           command=self.reload, font=F_SM, bg=BG2,
                           fg=SRC_COLOR[k], selectcolor=BG4,
                           activebackground=BG2, activeforeground=ACCENT,
                           anchor="w", bd=0,
                           highlightthickness=0).pack(fill="x", padx=18)

        head(p, "VIEW", font=F_SM).pack(fill="x", padx=14, pady=(16, 2))
        head(p, "LAYOUT", font=F_SM).pack(fill="x", padx=14, pady=(14, 2))
        self.v_layout = tk.StringVar(value="radial")
        for lab, val in (("rings by role", "radial"), ("free force", "force")):
            tk.Radiobutton(p, text=lab, variable=self.v_layout, value=val,
                           command=self._apply_layout, font=F_SM, bg=BG2,
                           fg=FG, selectcolor=BG4, activebackground=BG2,
                           activeforeground=ACCENT, anchor="w", bd=0,
                           highlightthickness=0).pack(fill="x", padx=18)
        tk.Label(p, text="centre out: mega cap,\nfirms, sectors, people",
                 font=F_XS, bg=BG2, fg=FG_DIM, anchor="w", wraplength=182,
                 justify="left").pack(fill="x", padx=18, pady=(2, 0))

        self.v_isolate = tk.BooleanVar(value=True)
        self.v_logos = tk.BooleanVar(value=True)
        tk.Checkbutton(p, text="isolate on click", variable=self.v_isolate,
                       command=self._apply_view, font=F_SM, bg=BG2, fg=FG,
                       selectcolor=BG4, activebackground=BG2,
                       activeforeground=ACCENT, anchor="w", bd=0,
                       highlightthickness=0).pack(fill="x", padx=18)
        self.v_lite = tk.BooleanVar(value=False)
        tk.Checkbutton(p, text="lite (low memory)", variable=self.v_lite,
                       command=self._apply_view, font=F_SM, bg=BG2, fg=YELLOW,
                       selectcolor=BG4, activebackground=BG2,
                       activeforeground=ACCENT, anchor="w", bd=0,
                       highlightthickness=0).pack(fill="x", padx=18)
        tk.Checkbutton(p, text="company logos", variable=self.v_logos,
                       command=self._apply_view, font=F_SM, bg=BG2, fg=FG,
                       selectcolor=BG4, activebackground=BG2,
                       activeforeground=ACCENT, anchor="w", bd=0,
                       highlightthickness=0).pack(fill="x", padx=18)
        btn(p, "⟳  reheat layout",
            lambda: self.canvas.reheat(0.95)).pack(fill="x", padx=16, pady=2)
        btn(p, "⊙  fit to window",
            self.canvas_reset).pack(fill="x", padx=16, pady=2)
        btn(p, "↻  rebuild", self.reload).pack(fill="x", padx=16, pady=2)

        tk.Label(p, text="drag a node to pin it\ndrag the field to pan\n"
                         "scroll to zoom\ndouble-click to reset",
                 font=F_XS, bg=BG2, fg=FG_DIM, anchor="w",
                 justify="left").pack(fill="x", padx=18, pady=(14, 0))

    def _apply_layout(self):
        self.canvas.set_layout(self.v_layout.get())

    def _apply_view(self):
        self.canvas.isolate = self.v_isolate.get()
        self.canvas.logos = self.v_logos.get()
        self.canvas.set_lite(self.v_lite.get())
        if self.canvas.logos:
            self.canvas._prefetch_logos()
        self.canvas._schedule_quality()

    def canvas_reset(self):
        self.canvas.reset_view()

    def _build_detail(self, p):
        self.d_ticker = tk.Label(p, text="", font=F_HUGE, bg=BG2, fg=FG,
                                 anchor="w")
        self.d_ticker.pack(fill="x", padx=16, pady=(16, 0))
        self.d_name = tk.Label(p, text="select a node", font=F_SM, bg=BG2,
                               fg=FG_DIM, anchor="w", wraplength=414,
                               justify="left")
        self.d_name.pack(fill="x", padx=16, pady=(0, 10))

        self.d_score = tk.Canvas(p, bg=BG2, height=62, highlightthickness=0)
        self.d_score.pack(fill="x", padx=16, pady=(0, 6))

        self.d_text = tk.Text(p, bg=BG2, fg=FG_DIM, font=F_XS, relief="flat",
                              wrap="word", padx=14, pady=8, height=17,
                              insertbackground=ACCENT)
        self.d_text.pack(fill="x", padx=4)
        for tag, col in (("hd", ACCENT), ("good", GREEN), ("bad", RED),
                         ("dim", FG_DIM), ("warn", YELLOW), ("fg", FG)):
            self.d_text.tag_configure(tag, foreground=col)

        self.who_head = head(p, "WHO", font=F_SM)
        self.who_head.pack(fill="x", padx=16, pady=(6, 2))
        wrap, self.d_who = self._scroll_tree(
            p, [("who", "filer"), ("src", "src"), ("txn", "txn"),
                ("br", "names"), ("amt", "amount"), ("when", "disclosed")],
            [140, 52, 44, 44, 82, 88], {"br": "e", "amt": "e"}, height=10)
        wrap.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.d_who.bind("<Double-1>", lambda e: self._open_filer())

        row = tk.Frame(p, bg=BG2)
        row.pack(fill="x", padx=12, pady=(0, 12))
        btn(row, "deep dive", self._deep_dive, fg="#000000",
            bg=ACCENT).pack(side="left")
        btn(row, "open ticker →", self._open_ticker).pack(side="left", padx=6)
        btn(row, "filing", self._open_doc).pack(side="left", padx=6)

    # ── data ─────────────────────────────
    def reload(self):
        srcs = tuple(k for k, v in self.v_src.items() if v.get())
        if not srcs:
            self.sub.config(text="no sources selected")
            return
        self.status.config(text="building…")
        self.frame.update_idletasks()
        self.clusters = pc.clusters(
            self.conn, days=int(self.v_days.get()),
            min_filers=int(self.v_min.get()), sources=srcs,
            direction=self.v_dir.get(), limit=90,
            sector=self.v_sector.get())
        if not getattr(self, "_caps", None):
            self._caps = pe.market_caps(self.conn)
            self._books = pe.fund_values(self.conn)
        g = pc.graph(self.clusters, caps=self._caps,
                     fund_values=self._books)
        self.canvas.by_direction = (self.v_dir.get() == "BOTH")
        if not getattr(self, "_sectors", None):
            import political_layout as playout
            self._sectors = playout.sector_map(self.conn)
        self.canvas.set_graph(g["nodes"], g["edges"], sectors=self._sectors)
        self.sub.config(text="{} names · {} nodes · {} links · {} · {}".format(
            len(self.clusters), len(g["nodes"]), len(g["edges"]),
            {"P": "buying", "S": "selling"}.get(self.v_dir.get(),
                                                "buying and selling"),
            self.v_sector.get()))

        both = self.v_dir.get() == "BOTH"
        for w in self.legend:
            w.pack_forget() if both else w.pack(side="right", padx=7)
        for w in self.legend2:
            w.pack(side="right", padx=7) if both else w.pack_forget()

        self.top.delete(*self.top.get_children())
        for i, c in enumerate(self.clusters, 1):
            self.top.insert(
                "", "end", iid=c["ticker"],
                values=(i, c["ticker"], "{:.0f}".format(c["score"]),
                        c["n_filers"],
                        "{:.1f}".format(c.get("n_effective", c["n_filers"])),
                        " ".join(SRC_LABEL.get(s, s) for s in c["sources"]),
                        "{}  ({})".format(c["newest"], ago(c["newest"])),
                        c["asset_name"]),
                tags=("buy" if self.v_dir.get() == "P" else "sell",))
        n_multi = sum(1 for c in self.clusters if c["n_sources"] >= 3)
        self.status.config(
            text="{} names with {}+ independent {}.  {} of them have three or "
                 "more source families agreeing.  Score is a sort key made of "
                 "named parts — it is not a prediction.".format(
                     len(self.clusters), self.v_min.get(),
                     "buyers" if self.v_dir.get() == "P" else "sellers",
                     n_multi))
        if self.clusters:
            self.show(self.clusters[0])

    # ── find ─────────────────────────────
    def _find_hint(self, _e=None):
        """Live count of what the typed prefix would match on screen."""
        q = self.v_find.get().strip().upper()
        if not q:
            self.find_msg.config(text="ticker, then Enter", fg=FG_DIM)
            return
        hits = [c["ticker"] for c in self.clusters
                if c["ticker"].startswith(q)]
        if hits:
            self.find_msg.config(text="{}  ({} on screen)".format(
                "  ".join(hits[:6]), len(hits)), fg=GREEN)
        else:
            self.find_msg.config(text="not in this view — Enter to look "
                                      "for it anyway", fg=YELLOW)

    def find(self):
        """Jump to a ticker, and say why it is missing when it is.

        A graph you cannot search is a graph you can only browse. When the
        name is on screen this selects and centres it; when it is not, the
        answer is never a silent no — the filters that are hiding it are
        named, because "no such company" and "you are looking at medical
        stocks from the last thirty days" are different answers.
        """
        q = self.v_find.get().strip().upper()
        if not q:
            return
        hit = next((c for c in self.clusters if c["ticker"] == q), None)             or next((c for c in self.clusters
                     if c["ticker"].startswith(q)), None)
        if hit:
            self.show(hit)
            self.canvas.focus_node("T:" + hit["ticker"])
            if self.top.exists(hit["ticker"]):
                self.top.selection_set(hit["ticker"])
                self.top.see(hit["ticker"])
            self.find_msg.config(text="{} — centred".format(hit["ticker"]),
                                 fg=GREEN)
            return

        # Not on screen. Work out whether it exists at all, and under which
        # filter it would appear.
        row = self.conn.execute(
            "SELECT COUNT(*) n, MAX(notify_date) last, COUNT(DISTINCT "
            "filer_name) f FROM disclosures WHERE ticker = ?", (q,)).fetchone()
        if not row or not row["n"]:
            self.find_msg.config(
                text="{} — nobody tracked has disclosed it".format(q),
                fg=RED)
            return
        sec = self.conn.execute(
            "SELECT sector FROM sectors WHERE ticker = ?", (q,)).fetchone()
        why = []
        if sec and sec["sector"]:
            grp = next((g for g, names in pe.SECTOR_GROUPS.items()
                        if sec["sector"] in names), None)
            if grp and self.v_sector.get() not in ("all", grp):
                why.append("it is {}".format(grp))
        try:
            if row["last"] and row["last"] < (
                    date.today() - timedelta(days=int(self.v_days.get()))
            ).isoformat():
                why.append("last disclosed {}".format(row["last"]))
        except ValueError:
            pass
        if row["f"] < int(self.v_min.get()):
            why.append("only {} filer{}".format(
                row["f"], "" if row["f"] == 1 else "s"))
        self.find_msg.config(
            text="{}: {} rows, hidden because {}".format(
                q, row["n"], "; ".join(why) or "of the current filters"),
            fg=YELLOW)
        self._pending_find = q
        cards.open_ticker(self.frame.winfo_toplevel(), self.conn, q,
                          self.navigate)

    def _cluster_for(self, ticker):
        return next((c for c in self.clusters if c["ticker"] == ticker), None)

    def _on_select(self, node):
        """Single click fills the side rail; the rail is the summary.

        A click on a FILER opens the floating card straight away, because
        the rail has nowhere useful to put a person — it is laid out for a
        company — and a person is the thing worth going deep on.
        """
        if node is None:
            return
        if node["kind"] == "ticker":
            self.show(node.get("cluster"))
            if self.top.exists(node["label"]):
                self.top.selection_set(node["label"])
                self.top.see(node["label"])
        else:
            self._show_filer_node(node)
            self.open_card(node)

    def open_card(self, node):
        if node is None:
            return
        try:
            if node["kind"] == "ticker":
                cards.open_ticker(self.frame.winfo_toplevel(), self.conn,
                                  node["label"], self.navigate)
            else:
                cards.open_filer(self.frame.winfo_toplevel(), self.conn,
                                 node["label"], self.navigate)
        except Exception as e:                          # noqa: BLE001
            print("card failed:", e)

    def _on_hover(self, node):
        pass

    def _on_pick_row(self, _e):
        sel = self.top.selection()
        if sel:
            self.show(self._cluster_for(sel[0]))
            self.canvas.focus_node("T:" + sel[0])

    def show(self, cl):
        if not cl:
            return
        self.current = cl
        self.d_ticker.config(text=cl["ticker"], fg=pgraph._hex(
            pgraph.heat(cl["score"])))
        self.d_name.config(text=cl["asset_name"] or "")
        self._paint_score(cl)

        t = self.d_text
        t.delete("1.0", "end")
        side = {"P": "buyers", "S": "sellers"}.get(cl["direction"], "filers")
        t.insert("end", "{} independent {} · {} of 4 source families\n".format(
            cl["n_filers"], side, cl["n_sources"]), "fg")
        if cl.get("direction") == "BOTH":
            t.insert("end",
                     "{} buying, {} selling   ({:.1f} vs {:.1f} effective)\n"
                     .format(cl.get("n_buyers", 0), cl.get("n_sellers", 0),
                             cl.get("eff_buy", 0), cl.get("eff_sell", 0)),
                     "good" if (cl.get("lean") or 0) > 0 else "bad")
            lean = cl.get("lean") or 0
            if cl.get("contested") and abs(lean) < 0.34:
                verdict = "contested — both sides active in size"
            else:
                verdict = "net bought" if lean > 0 else "net sold"
            t.insert("end", "lean {:+.2f} — {}\n".format(lean, verdict),
                     "warn")
        t.insert("end", "{:.1f} effective — each filer is shrunk by how many "
                        "different names they bought, so a fund adding one "
                        "more of 1,200 positions counts for almost nothing\n"
                 .format(cl.get("n_effective", cl["n_filers"])), "warn")
        t.insert("end", "{} disclosures between {} and {}\n".format(
            cl["n_rows"], cl["oldest"], cl["newest"]), "dim")
        t.insert("end", "largest disclosed {}\n".format(
            money(cl["max_amount"])), "dim")
        if cl["has_option"]:
            t.insert("end", "includes dated options — strike and expiry are "
                            "in the filing\n", "warn")
        inf = pe.influence_for(self.conn, cl["ticker"])
        if inf:
            line = "lobbying {} in {}".format(
                money(inf.get("lobby_spend")), inf.get("year"))
            if inf.get("lobby_growth") is not None:
                line += " ({:+.0%} yoy)".format(inf["lobby_growth"])
            if inf.get("contract_total"):
                line += " · federal awards {}".format(
                    money(inf["contract_total"]))
            t.insert("end", line + "\n", "dim")
        t.insert("end", "\n")
        for k in ("agreement", "diversity", "recency", "size", "instrument"):
            c = cl["components"][k]
            t.insert("end", "  {:<11}{:>4.0f} × {:.2f} = {:>5.1f}   {}\n".format(
                k, c, pc.WEIGHTS[k], c * pc.WEIGHTS[k], pc.COMPONENT_NOTE[k]),
                "dim")
        t.insert("end", "\nweights are judgement, printed so you can disagree "
                        "with one of them. nothing here has been tested "
                        "against what prices did next.\n", "dim")

        if cl.get("direction") == "BOTH":
            self.who_head.config(
                text="WHO   ({} buying, {} selling)".format(
                    cl.get("n_buyers", 0), cl.get("n_sellers", 0)))
        else:
            self.who_head.config(text="WHO")
        self.d_who.delete(*self.d_who.get_children())
        for r in cl["filers"]:
            br = (cl.get("breadth") or {}).get(r.get("filer_name"), 1)
            t = r.get("txn_type") or ""
            bought = t == "P"
            # Colour by the ROW, not by the cluster. Reading the cluster's
            # direction meant that in combined mode — where it is "BOTH" and
            # matches neither branch — every line fell through to red, so a
            # name nine people were buying rendered as nine sells.
            tag = "opt" if (r.get("option_type") or r.get("strike")) else (
                "buy" if bought else "sell")
            self.d_who.insert(
                "", "end", iid="{}:{}".format(r["id"], t),
                values=((r.get("filer_name") or "")[:18],
                        SRC_LABEL.get(r.get("source"), ""),
                        "buy" if bought else "sell", br,
                        money(r.get("amount_lo")),
                        r.get("notify_date")),
                tags=(tag,))

    def _show_filer_node(self, node):
        self.d_ticker.config(text="", fg=FG)
        self.d_name.config(text=node["label"])
        self.d_score.delete("all")
        t = self.d_text
        t.delete("1.0", "end")
        t.insert("end", "{}\n".format(node["label"]), "hd")
        t.insert("end", "{} · appears in {} of the clusters on screen\n".format(
            SRC_LABEL.get(node.get("source"), node.get("source") or ""),
            node.get("weight", 0)), "dim")
        t.insert("end", "\ndouble-click in the list below, or open the Filer "
                        "tab, for their whole record.\n", "dim")

    def _paint_score(self, cl):
        c = self.d_score
        c.delete("all")
        w = max(c.winfo_width(), 280)
        x = 8
        total = 0.0
        for k in ("agreement", "diversity", "recency", "size", "instrument"):
            part = cl["components"][k] * pc.WEIGHTS[k]
            total += part
            seg = (part / 100.0) * (w - 16)
            if seg > 0.6:
                c.create_rectangle(x, 30, x + seg, 44, fill=pgraph._hex(
                    pgraph.heat(cl["components"][k])), outline="")
            x += seg
        c.create_rectangle(8, 30, w - 8, 44, outline=BORDER)
        c.create_text(8, 16, text="{:.0f}".format(total), anchor="w",
                      font=F_XL, fill=pgraph._hex(pgraph.heat(total)))
        c.create_text(68, 20, text="/ 100  sort key, not a forecast",
                      anchor="w", font=F_XS, fill=FG_DIM)

    # ── actions ──────────────────────────
    def _open_ticker(self):
        sel = self.top.selection()
        t = sel[0] if sel else getattr(self, "current", {}).get("ticker")
        if t:
            self.navigate("ticker", t)

    def _deep_dive(self):
        cl = getattr(self, "current", None)
        if cl:
            cards.open_ticker(self.frame.winfo_toplevel(), self.conn,
                              cl["ticker"], self.navigate)

    def _open_filer(self):
        sel = self.d_who.selection()
        if not sel:
            return
        cl = getattr(self, "current", None)
        if not cl:
            return
        r = next((x for x in cl["filers"]
                  if sel[0].startswith(x["id"])), None)
        if r and r.get("filer_name"):
            cards.open_filer(self.frame.winfo_toplevel(), self.conn,
                             r["filer_name"], self.navigate)

    def _open_doc(self):
        sel = self.d_who.selection()
        cl = getattr(self, "current", None)
        if not sel or not cl:
            return
        r = next((x for x in cl["filers"]
                  if sel[0].startswith(x["id"])), None)
        if r and r.get("doc_url"):
            webbrowser.open(r["doc_url"])

    def stop(self):
        self.canvas.stop()


# ═════════════════════════════════════════════
# TICKER
# ═════════════════════════════════════════════

class TickerView(_Base):

    def __init__(self, parent, conn, navigate=None):
        _Base.__init__(self, parent, conn, navigate)
        self.v_t = tk.StringVar(value="")
        self.v_days = tk.StringVar(value="365")
        self.profile = {}
        self._build()

    def _build(self):
        bar = tk.Frame(self.frame, bg=BG2, height=52)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(bar, text="TICKER", font=F_SM, bg=BG2,
                 fg=ACCENT).pack(side="left", padx=(16, 8))
        e = tk.Entry(bar, textvariable=self.v_t, font=F_MD, bg=BG3, fg=FG,
                     insertbackground=ACCENT, relief="flat", bd=6, width=12)
        e.pack(side="left", pady=10)
        e.bind("<Return>", lambda ev: self.load())
        btn(bar, "look up", self.load, fg="#000000",
            bg=ACCENT).pack(side="left", padx=8)
        for lab, v in (("90d", "90"), ("1y", "365"), ("all", "3650")):
            tk.Radiobutton(bar, text=lab, variable=self.v_days, value=v,
                           command=self.load, font=F_XS, bg=BG2, fg=FG_DIM,
                           selectcolor=BG4, activebackground=BG2,
                           bd=0, highlightthickness=0).pack(side="left",
                                                            padx=3)
        self.hdr = tk.Label(bar, text="", font=F_SM, bg=BG2, fg=FG_DIM)
        self.hdr.pack(side="right", padx=16)

        body = tk.Frame(self.frame, bg=BG)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(12, 6), pady=10)
        self.big = tk.Label(left, text="", font=F_HUGE, bg=BG, fg=FG,
                            anchor="w")
        self.big.pack(fill="x")
        self.bigsub = tk.Label(left, text="type a ticker above", font=F_SM,
                               bg=BG, fg=FG_DIM, anchor="w")
        self.bigsub.pack(fill="x", pady=(0, 8))
        wrap, self.tree = self._scroll_tree(
            left,
            [("when", "disclosed"), ("lag", "lag"), ("who", "filer"),
             ("src", "source"), ("txn", "txn"), ("own", "owner"),
             ("amt", "amount"), ("opt", "option"), ("r10", "10d"),
             ("r20", "20d")],
            [96, 46, 210, 74, 76, 62, 132, 140, 68, 68],
            {"lag": "e", "r10": "e", "r20": "e", "amt": "e"})
        wrap.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self._open_doc())

        right = tk.Frame(body, bg=BG2, width=392)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self.panel = tk.Text(right, bg=BG2, fg=FG_DIM, font=F_SM,
                             relief="flat", wrap="word", padx=16, pady=14,
                             insertbackground=ACCENT)
        self.panel.pack(fill="both", expand=True)
        for tag, col in (("hd", ACCENT), ("good", GREEN), ("bad", RED),
                         ("dim", FG_DIM), ("warn", YELLOW), ("fg", FG),
                         ("teal", TEAL), ("violet", VIOLET)):
            self.panel.tag_configure(tag, foreground=col)
        self.panel.tag_configure("big", font=F_MD, foreground=FG)

    def load(self, ticker=None):
        if ticker:
            self.v_t.set(ticker.upper())
        t = self.v_t.get().strip().upper()
        if not t:
            return
        p = pc.ticker_profile(self.conn, t, days=int(self.v_days.get()))
        self.profile = p
        self.big.config(text=t)
        if not p.get("rows"):
            self.bigsub.config(text="no disclosures in this window")
            self.tree.delete(*self.tree.get_children())
            self.panel.delete("1.0", "end")
            self.panel.insert("end", "Nothing filed on {} in the last {} "
                                     "days.\n\nThat is a real answer: no "
                                     "politician, insider or tracked fund "
                                     "disclosed a trade in it.".format(
                                         t, self.v_days.get()), "dim")
            return
        self.bigsub.config(text="{} · {} disclosures, {} buys / {} sells, "
                                "{} .. {}".format(
                                    p.get("asset_name") or "", p["n"],
                                    p["n_buys"], p["n_sells"],
                                    p["range"][0], p["range"][1]))
        self.hdr.config(text="{} buyers · {} sellers".format(
            len(p["buyers"]), len(p["sellers"])))

        self.tree.delete(*self.tree.get_children())
        for r in p["rows"]:
            lag = ""
            try:
                lag = str((date.fromisoformat(r["notify_date"])
                           - date.fromisoformat(r["trade_date"])).days)
            except Exception:
                pass
            opt = ""
            if r.get("option_type") or r.get("strike"):
                opt = "{} {} {}".format(
                    r.get("option_type") or "",
                    "${:g}".format(r["strike"]) if r.get("strike") else "",
                    r.get("expiry") or "").strip()
            buy = (r.get("txn_type") or "") == "P"
            self.tree.insert(
                "", "end", iid=r["id"],
                values=(r.get("notify_date"), lag,
                        (r.get("filer_name") or "")[:28],
                        SRC_LABEL.get(r.get("source"), r.get("source")),
                        r.get("txn_type"), r.get("owner"),
                        bucket(r.get("amount_lo"), r.get("amount_hi")),
                        opt, pct(r.get("ret_10d_notify"), 1),
                        pct(r.get("ret_20d_notify"), 1)),
                tags=("opt" if opt else ("buy" if buy else "sell"),))
        self._panel(p)

    def _panel(self, p):
        t = self.panel
        t.delete("1.0", "end")
        t.insert("end", "WHO IS IN IT\n", "hd")
        for s, d in sorted(p["by_source"].items()):
            t.insert("end", "  {:<9} {} buys / {} sells across {} filers\n"
                     .format(SRC_LABEL.get(s, s), d["buys"], d["sells"],
                             d["filers"]),
                     {"house_ptr": "warn", "senate_ptr": "warn",
                      "form4": "teal", "f13": "violet"}.get(s, "dim"))
        if p["buyers"]:
            t.insert("end", "\n  buyers  ", "dim")
            t.insert("end", ", ".join(p["buyers"][:12]) + "\n", "good")
        if p["sellers"]:
            t.insert("end", "  sellers ", "dim")
            t.insert("end", ", ".join(p["sellers"][:12]) + "\n", "bad")

        if p["options"]:
            t.insert("end", "\nDATED OPTIONS\n", "hd")
            t.insert("end", "  what they actually bought, which most trackers "
                            "flatten away\n", "dim")
            for r in p["options"][:8]:
                t.insert("end", "  {:<22} {} {} exp {}  {}\n".format(
                    (r.get("filer_name") or "")[:22],
                    r.get("option_type") or "?",
                    "${:g}".format(r["strike"]) if r.get("strike") else "?",
                    r.get("expiry") or "?", r.get("notify_date")), "warn")

        inf = p.get("influence") or {}
        if inf:
            t.insert("end", "\nWASHINGTON FOOTPRINT\n", "hd")
            t.insert("end", "  lobbying {} in {}".format(
                money(inf.get("lobby_spend")), inf.get("year")), "fg")
            if inf.get("lobby_growth") is not None:
                t.insert("end", "  ({:+.0%} on last year)".format(
                    inf["lobby_growth"]),
                    "good" if inf["lobby_growth"] > 0 else "bad")
            t.insert("end", "\n")
            if inf.get("contract_total"):
                t.insert("end", "  federal awards {} across {} contracts\n"
                         .format(money(inf["contract_total"]),
                                 inf.get("contract_count") or 0), "fg")
            t.insert("end", "  matched on the name '{}' — a join on a string, "
                            "not an identifier\n".format(
                                inf.get("matched_name")), "dim")

        if p.get("perf"):
            t.insert("end", "\nWHAT THE BUYS DID NEXT\n", "hd")
            t.insert("end", "  from the disclosure date, the only one anyone "
                            "else could act on\n", "dim")
            for k, v in sorted(p["perf"].items(),
                               key=lambda kv: int(kv[0][:-1])):
                t.insert("end", "  {:<5} n={:<4} mean {}\n".format(
                    k, v["n"], pct(v["mean"])),
                    "good" if v["mean"] > 0 else "bad")
            t.insert("end", "  n is small. one name is not a sample, and this "
                            "carries no correction for the fact that you "
                            "chose this ticker.\n", "dim")

    def _open_doc(self):
        sel = self.tree.selection()
        if not sel:
            return
        r = next((x for x in self.profile.get("rows", [])
                  if x["id"] == sel[0]), None)
        if r and r.get("doc_url"):
            webbrowser.open(r["doc_url"])


# ═════════════════════════════════════════════
# FILER
# ═════════════════════════════════════════════

class FilerView(_Base):

    def __init__(self, parent, conn, navigate=None):
        _Base.__init__(self, parent, conn, navigate)
        self.v_q = tk.StringVar(value="")
        self.v_min = tk.StringVar(value="5")
        self.profile = {}
        self.all = []
        self._build()
        self.refresh_list()

    def _build(self):
        left = tk.Frame(self.frame, bg=BG2, width=310)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        sb = tk.Frame(left, bg=BG2)
        sb.pack(fill="x", padx=12, pady=(14, 6))
        e = tk.Entry(sb, textvariable=self.v_q, font=F_SM, bg=BG3, fg=FG,
                     insertbackground=ACCENT, relief="flat", bd=5)
        e.pack(fill="x")
        e.bind("<KeyRelease>", lambda ev: self.refresh_list())
        row = tk.Frame(left, bg=BG2)
        row.pack(fill="x", padx=12)
        tk.Label(row, text="min rows", font=F_XS, bg=BG2,
                 fg=FG_DIM).pack(side="left")
        for v in ("1", "5", "10", "25"):
            tk.Radiobutton(row, text=v, variable=self.v_min, value=v,
                           command=self.refresh_list, font=F_XS, bg=BG2,
                           fg=FG_DIM, selectcolor=BG4, activebackground=BG2,
                           bd=0, highlightthickness=0).pack(side="left")

        wrap, self.list = self._scroll_tree(
            left, [("name", "filer"), ("src", "src"), ("n", "n")],
            [186, 54, 40], {"n": "e"})
        wrap.pack(fill="both", expand=True, padx=8, pady=8)
        self.list.bind("<<TreeviewSelect>>", self._pick)

        tk.Frame(self.frame, bg=BORDER, width=1).pack(side="left", fill="y")

        right = tk.Frame(self.frame, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        hdr = tk.Frame(right, bg=BG)
        hdr.pack(fill="x", padx=16, pady=(14, 4))
        self.name = tk.Label(hdr, text="", font=F_XL, bg=BG, fg=FG,
                             anchor="w")
        self.name.pack(fill="x")
        self.meta = tk.Label(hdr, text="pick a filer on the left", font=F_SM,
                             bg=BG, fg=FG_DIM, anchor="w", wraplength=1100,
                             justify="left")
        self.meta.pack(fill="x")

        split = tk.Frame(right, bg=BG)
        split.pack(fill="both", expand=True, padx=12, pady=8)

        lcol = tk.Frame(split, bg=BG)
        lcol.pack(side="left", fill="both", expand=True)
        head(lcol, "EVERY DISCLOSURE", font=F_SM).pack(fill="x", pady=(0, 3))
        wrap2, self.tree = self._scroll_tree(
            lcol,
            [("when", "disclosed"), ("lag", "lag"), ("tk", "ticker"),
             ("txn", "txn"), ("own", "owner"), ("amt", "amount"),
             ("opt", "option"), ("r10", "10d"), ("r20", "20d")],
            [96, 44, 68, 74, 60, 130, 132, 64, 64],
            {"lag": "e", "r10": "e", "r20": "e"})
        wrap2.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self._open_ticker())

        rcol = tk.Frame(split, bg=BG2, width=380)
        rcol.pack(side="right", fill="y", padx=(10, 0))
        rcol.pack_propagate(False)
        self.panel = tk.Text(rcol, bg=BG2, fg=FG_DIM, font=F_SM,
                             relief="flat", wrap="word", padx=16, pady=14)
        self.panel.pack(fill="both", expand=True)
        for tag, col in (("hd", ACCENT), ("good", GREEN), ("bad", RED),
                         ("dim", FG_DIM), ("warn", YELLOW), ("fg", FG)):
            self.panel.tag_configure(tag, foreground=col)

    def refresh_list(self):
        q = self.v_q.get().strip().lower()
        self.all = pc.filer_list(self.conn, min_rows=int(self.v_min.get()))
        self.list.delete(*self.list.get_children())
        for f in self.all:
            nm = f["filer_name"] or ""
            if q and q not in nm.lower():
                continue
            self.list.insert("", "end", iid=nm,
                             values=(nm[:26],
                                     SRC_LABEL.get(f["source"], ""), f["n"]),
                             tags=("dim" if f["n"] < 5 else "",))

    def _pick(self, _e):
        sel = self.list.selection()
        if sel:
            self.load(sel[0])

    def load(self, name=None):
        if name:
            p = pc.filer_profile(self.conn, name)
        else:
            return
        self.profile = p
        if not p:
            self.meta.config(text="no rows")
            return
        self.name.config(text=" / ".join(p["names"])[:60])
        bits = ["{} disclosures".format(p["n"]),
                "{} buys · {} sells".format(p["n_buys"], p["n_sells"]),
                "{} .. {}".format(p["range"][0], p["range"][1])]
        if p.get("party"):
            bits.insert(0, "{}-{}".format(p["party"], p.get("state") or ""))
        if p.get("mean_lag") is not None:
            bits.append("discloses {:.0f} days after trading".format(
                p["mean_lag"]))
        if p.get("options"):
            bits.append("{} with dated options".format(p["options"]))
        self.meta.config(text="   ·   ".join(bits))

        self.tree.delete(*self.tree.get_children())
        for r in p["rows"]:
            lag = ""
            try:
                lag = str((date.fromisoformat(r["notify_date"])
                           - date.fromisoformat(r["trade_date"])).days)
            except Exception:
                pass
            opt = ""
            if r.get("option_type") or r.get("strike"):
                opt = "{} {} {}".format(
                    r.get("option_type") or "",
                    "${:g}".format(r["strike"]) if r.get("strike") else "",
                    r.get("expiry") or "").strip()
            buy = (r.get("txn_type") or "") == "P"
            self.tree.insert(
                "", "end", iid=r["id"],
                values=(r.get("notify_date"), lag, r.get("ticker") or "--",
                        r.get("txn_type"), r.get("owner"),
                        bucket(r.get("amount_lo"), r.get("amount_hi")), opt,
                        pct(r.get("ret_10d_notify"), 1),
                        pct(r.get("ret_20d_notify"), 1)),
                tags=("opt" if opt else ("buy" if buy else "sell"),))

        t = self.panel
        t.delete("1.0", "end")
        if p.get("committees"):
            t.insert("end", "COMMITTEES\n", "hd")
            for c in p["committees"][:8]:
                t.insert("end", "  {}\n".format(c[:46]), "dim")
            t.insert("end", "\n")
        t.insert("end", "MOST TRADED\n", "hd")
        for d in p["tickers"][:14]:
            t.insert("end", "  {:<7} {:>2}B / {:>2}S   {}\n".format(
                d["ticker"], d["buys"], d["sells"], d["newest"]),
                "good" if d["buys"] > d["sells"] else "bad")

        if p.get("perf"):
            t.insert("end", "\nWHAT THEIR BUYS DID NEXT\n", "hd")
            for k in sorted(p["perf"], key=lambda k: (k.split("_")[1],
                                                      int(k.split("d")[0]))):
                v = p["perf"][k]
                lab = k.replace("_notify", " from disclosure").replace(
                    "_trade", " from the trade")
                t.insert("end", "  {:<24} n={:<4} mean {}  hit {:.0%}\n".format(
                    lab, v["n"], pct(v["mean"]), v["hit"]),
                    "good" if v["mean"] > 0 else "bad")
            t.insert("end", "\nThere is deliberately no rank here. The "
                            "permutation test in the Record tab says the "
                            "leader of any per-filer ranking is noise at this "
                            "sample size, and a number printed next to a "
                            "position reads as a recommendation however it is "
                            "captioned.\n", "dim")

    def _open_ticker(self):
        sel = self.tree.selection()
        if not sel:
            return
        r = next((x for x in self.profile.get("rows", [])
                  if x["id"] == sel[0]), None)
        if r and r.get("ticker"):
            self.navigate("ticker", r["ticker"])


# ═════════════════════════════════════════════
# MOVERS
# ═════════════════════════════════════════════

class MoversView(_Base):
    """Most bought and most sold, counted the only way that means anything.

    The unit is the distinct filer. Ranking by row count puts DELL on top
    with 792 rows, none of which is a buy and all of which are one company's
    vesting schedule — see the note at the top of political_clusters.py.
    """

    def __init__(self, parent, conn, navigate=None):
        _Base.__init__(self, parent, conn, navigate)
        self.v_days = tk.StringVar(value="90")
        self.v_sector = tk.StringVar(value="all")
        self.v_src = {k: tk.BooleanVar(value=True) for k in SRC_LABEL}
        self._build()
        self.reload()

    def _build(self):
        bar = tk.Frame(self.frame, bg=BG2, height=48)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(bar, text="MOVERS", font=F_LG, bg=BG2,
                 fg=FG).pack(side="left", padx=16)
        tk.Label(bar, text="ranked by DISTINCT FILERS, never by row count",
                 font=F_XS, bg=BG2, fg=FG_DIM).pack(side="left", padx=6)
        om = tk.OptionMenu(bar, self.v_sector, *pe.SECTOR_ORDER,
                           command=lambda *_: self.reload())
        om.configure(bg=BG3, fg=FG, font=F_SM, relief="flat", bd=0,
                     highlightthickness=0, activebackground=BG4, width=11,
                     anchor="w")
        om["menu"].configure(bg=BG3, fg=FG, font=F_SM)
        om.pack(side="right", padx=8)
        tk.Label(bar, text="industry", font=F_XS, bg=BG2,
                 fg=FG_DIM).pack(side="right")
        for lab, v in (("30d", "30"), ("90d", "90"), ("1y", "365")):
            tk.Radiobutton(bar, text=lab, variable=self.v_days, value=v,
                           command=self.reload, font=F_SM, bg=BG2, fg=FG,
                           selectcolor=BG4, activebackground=BG2, bd=0,
                           highlightthickness=0).pack(side="right", padx=4)
        for k, lab in SRC_LABEL.items():
            tk.Checkbutton(bar, text=lab, variable=self.v_src[k],
                           command=self.reload, font=F_XS, bg=BG2,
                           fg=SRC_COLOR[k], selectcolor=BG4,
                           activebackground=BG2, bd=0,
                           highlightthickness=0).pack(side="right", padx=4)

        body = tk.Frame(self.frame, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=10)

        cols = [("ticker", "ticker"), ("f", "filers"), ("other", "other side"),
                ("rows", "rows"), ("srcs", "sources"), ("score", "score"),
                ("newest", "newest"), ("flag", "")]
        widths = [76, 58, 76, 56, 148, 58, 150, 56]
        anch = {"f": "e", "other": "e", "rows": "e", "score": "e"}

        lcol = tk.Frame(body, bg=BG)
        lcol.pack(side="left", fill="both", expand=True, padx=(0, 7))
        head(lcol, "MOST BOUGHT", GREEN, F_MD).pack(fill="x", pady=(0, 4))
        w1, self.buy = self._scroll_tree(lcol, cols, widths, anch)
        w1.pack(fill="both", expand=True)
        self.buy.bind("<Double-1>", lambda e: self._open(self.buy))

        rcol = tk.Frame(body, bg=BG)
        rcol.pack(side="left", fill="both", expand=True, padx=(7, 0))
        head(rcol, "MOST SOLD", RED, F_MD).pack(fill="x", pady=(0, 4))
        w2, self.sell = self._scroll_tree(rcol, cols, widths, anch)
        w2.pack(fill="both", expand=True)
        self.sell.bind("<Double-1>", lambda e: self._open(self.sell))

        self.note = tk.Label(
            self.frame,
            text="Double-click a row to open it in the Ticker tab.   "
                 "NEW marks a name whose first-ever disclosure in this store "
                 "falls inside the window.",
            font=F_XS, bg=BG, fg=FG_DIM, anchor="w")
        self.note.pack(fill="x", padx=16, pady=(0, 8))

    def reload(self):
        srcs = tuple(k for k, v in self.v_src.items() if v.get()) or None
        d = int(self.v_days.get())
        for tree, direction in ((self.buy, "P"), (self.sell, "S")):
            tree.delete(*tree.get_children())
            for m in pc.movers(self.conn, days=d, direction=direction,
                               sources=srcs, limit=60,
                               sector=self.v_sector.get()):
                mine = m["n_buyers"] if direction == "P" else m["n_sellers"]
                other = m["n_sellers"] if direction == "P" else m["n_buyers"]
                rows = m["buy_rows"] if direction == "P" else m["sell_rows"]
                tree.insert(
                    "", "end", iid=m["ticker"],
                    values=(m["ticker"], mine, other, rows,
                            " ".join(SRC_LABEL.get(s, s)
                                     for s in m["sources"]),
                            "{:.0f}".format(m["score"]),
                            "{}  ({})".format(m["newest"], ago(m["newest"])),
                            "NEW" if m["is_new"] else ""),
                    tags=("buy" if direction == "P" else "sell",))

    def _open(self, tree):
        sel = tree.selection()
        if sel:
            self.navigate("ticker", sel[0])
