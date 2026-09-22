"""
political_gui.py
================
Copy Trading — the disclosure feed, its filters, and the record it has
earned so far.

Launched from the dashboard as its own OS process, like every other tool
here, so a hung House Clerk fetch cannot take the launcher with it.

WHAT THE RUN BUTTON DOES
------------------------
Starts a background scanner that stays active until it is stopped. It does
not spin: each source carries its own cadence and the loop waits on an
event rather than sleeping, so between polls this costs one blocked thread
and Stop takes effect at once instead of at the end of a nap.

The cadence is paced to the filings, not to the button. A congressional
disclosure is 30 to 45 days old before it is public, so polling the House
index every thirty minutes and polling it every thirty seconds produce the
same data and differ only in how rude they are to a government server. The
index poll is a 60KB conditional request that usually transfers nothing;
the expensive PDF fetch happens only for a document that has not been seen.

WHAT THE RECORD TAB REFUSES TO DO
---------------------------------
It will not show a ranked list of politicians without the probability that
pure noise would have produced a leader that good. With 535 members
somebody finishes first every time, and a leaderboard that does not say so
is a random number generator with names on it. See hypotheses.py, which
exists because the same trap was found in this repo's own squeeze data
first.
"""

import os
import json
import queue
import threading
import webbrowser
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import datetime, date, timedelta

import political_engine as pe
import political_feeds as pf
import political_views as pv
import political_cards as cards

BG = "#0A0E14"
BG2 = "#12171F"
BG3 = "#1A2030"
FG = "#CDD6F4"
FG_DIM = "#6C7086"
ACCENT = "#F4C430"
GREEN = "#A6E3A1"
RED = "#F38BA8"
YELLOW = "#F9E2AF"
BLUE = "#89B4FA"
TEAL = "#94E2D5"
BORDER = "#313244"

FONT = ("Consolas", 10)
FONT_SM = ("Consolas", 9)
FONT_LG = ("Consolas", 12, "bold")
FONT_HD = ("Consolas", 14, "bold")

OWNERS = ("self", "spouse", "joint", "child")
TXNS = (("P", "purchase"), ("S", "sale"), ("S_partial", "partial sale"),
        ("E", "exchange"))
ASSETS = (("ST", "stock"), ("OP", "option"), ("GS", "gov/muni"),
          ("CS", "corp bond"), ("EF", "ETF"), ("MF", "fund"),
          ("OT", "other"))
SOURCE_LABELS = (("house_ptr", "House"), ("senate_ptr", "Senate"),
                 ("form4", "Form 4 insiders"), ("f13", "13F funds"))
# Not a disclosure source — it annotates tickers the sources above surfaced,
# so it is scanned but never filtered on.
OVERLAY = "influence"
AMOUNTS = (("0", "any"), ("15001", "$15k+"), ("50001", "$50k+"),
           ("100001", "$100k+"), ("250001", "$250k+"), ("1000001", "$1M+"))


class CopyTradingApp:

    def __init__(self, root):
        self.root = root
        root.title("Copy Trading — disclosure feed")
        # This tool is a wall of relationships; give it the monitor.
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("{}x{}+0+0".format(int(sw * 0.98), int(sh * 0.94)))
        try:
            root.state("zoomed")
        except tk.TclError:
            pass
        root.configure(bg=BG)
        root.minsize(1180, 700)

        self.conn = pe.connect()
        self.worker = None
        self.events = queue.Queue()
        self._rows_cache = []

        self.v_source = {k: tk.BooleanVar(value=True) for k, _ in SOURCE_LABELS}
        self.v_overlay = tk.BooleanVar(value=False)
        self.v_owner = {k: tk.BooleanVar(value=True) for k in OWNERS}
        self.v_txn = {k: tk.BooleanVar(value=(k == "P")) for k, _ in TXNS}
        self.v_asset = {k: tk.BooleanVar(value=(k in ("ST", "OP")))
                        for k, _ in ASSETS}
        self.v_party = {k: tk.BooleanVar(value=True) for k in ("D", "R", "I")}
        self.v_chamber = {k: tk.BooleanVar(value=True)
                          for k in ("house", "senate", "corporate", "fund")}
        self.v_amount = tk.StringVar(value="0")
        self.v_filer = tk.StringVar(value="")
        self.v_ticker = tk.StringVar(value="")
        self.v_committee = tk.StringVar(value="(any)")
        self.v_optonly = tk.BooleanVar(value=False)
        self.v_days = tk.StringVar(value="30")

        self._style()
        self._build_top()
        self._build_body()
        self._build_status()
        self.root.after(200, self._pump)
        self.refresh()
        self._greet()

    # ── chrome ───────────────────────────────
    def _style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Treeview", background=BG2, fieldbackground=BG2,
                     foreground=FG, rowheight=21, font=FONT_SM,
                     borderwidth=0)
        st.configure("Treeview.Heading", background=BG3, foreground=ACCENT,
                     font=FONT_SM, relief="flat")
        # Tk 8.6.9's default map carries ('!disabled', '!selected', ...),
        # which overrides tag_configure and made every coloured row in the
        # Feed table render the same. See political_views.fixed_map.
        from political_views import fixed_map
        st.map("Treeview",
               background=fixed_map(st, "background") + [("selected", BG3)],
               foreground=fixed_map(st, "foreground") + [("selected", ACCENT)])
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=BG2, foreground=FG_DIM,
                     padding=(16, 7), font=FONT_SM)
        st.map("TNotebook.Tab", background=[("selected", BG3)],
               foreground=[("selected", ACCENT)])
        st.configure("Vertical.TScrollbar", background=BG3, troughcolor=BG,
                     borderwidth=0, arrowcolor=FG_DIM)

    def _btn(self, parent, text, cmd, fg=FG, bg=BG3, **kw):
        b = tk.Button(parent, text=text, command=cmd, font=FONT_SM,
                      bg=bg, fg=fg, activebackground=BORDER,
                      activeforeground=FG, relief="flat", cursor="hand2",
                      padx=12, pady=5, bd=0, **kw)
        return b

    def _build_top(self):
        bar = tk.Frame(self.root, bg=BG2, height=56)
        bar.pack(fill="x", side="top")
        bar.pack_propagate(False)

        tk.Label(bar, text="◆", font=("Consolas", 15), bg=BG2,
                 fg=ACCENT).pack(side="left", padx=(16, 6))
        tk.Label(bar, text="COPY TRADING", font=FONT_LG, bg=BG2,
                 fg=FG).pack(side="left", padx=(0, 18))

        self.run_btn = self._btn(bar, "▶  Run", self._toggle_run,
                                 fg="#000000", bg=GREEN)
        self.run_btn.pack(side="left", padx=3, pady=11)
        self._btn(bar, "⤓  Backfill 30d", self._backfill).pack(
            side="left", padx=3, pady=11)
        self._btn(bar, "◷  Grade", self._grade).pack(
            side="left", padx=3, pady=11)
        self._btn(bar, "⇪  Publish", self._publish).pack(
            side="left", padx=3, pady=11)
        self._btn(bar, "⇩  Load snapshot", self._load_snapshot).pack(
            side="left", padx=3, pady=11)

        self.mode_lbl = tk.Label(bar, text="live", font=FONT_SM, bg=BG2,
                                 fg=FG_DIM)
        self.mode_lbl.pack(side="right", padx=16)
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

    def _build_body(self):
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)

        nb = ttk.Notebook(body)
        nb.pack(fill="both", expand=True)
        self.nb = nb
        self._tab_index = {}

        def add(key, label, builder):
            fr = tk.Frame(nb, bg=BG)
            nb.add(fr, text="  {}  ".format(label))
            self._tab_index[key] = nb.index("end") - 1
            return builder(fr)

        # The four views that answer a question come first; the raw table
        # stays underneath them because sometimes the answer is "show me the
        # rows", but it should not be what opens.
        self.view_clusters = add("clusters", "Clusters", lambda f: pv.ClustersView(
            f, self.conn, navigate=self.navigate))
        self.view_ticker = add("ticker", "Ticker", lambda f: pv.TickerView(
            f, self.conn, navigate=self.navigate))
        self.view_filer = add("filer", "Filer", lambda f: pv.FilerView(
            f, self.conn, navigate=self.navigate))
        self.view_movers = add("movers", "Movers", lambda f: pv.MoversView(
            f, self.conn, navigate=self.navigate))

        # Feed keeps its own filter rail — those controls describe the table
        # beside them and mean nothing on the views above.
        def _feed(f):
            side = tk.Frame(f, bg=BG2, width=248)
            side.pack(side="left", fill="y")
            side.pack_propagate(False)
            self._build_filters(side)
            tk.Frame(f, bg=BORDER, width=1).pack(side="left", fill="y")
            inner = tk.Frame(f, bg=BG)
            inner.pack(side="left", fill="both", expand=True)
            self._build_feed(inner)

        add("feed", "Feed", _feed)
        add("record", "Record", self._build_record)
        add("scanner", "Scanner", self._build_log)

    def navigate(self, tab: str, arg=None):
        """Cross-view jumps: a ticker on one board opens on another."""
        i = self._tab_index.get(tab)
        if i is None:
            return
        self.nb.select(i)
        if tab == "ticker" and arg:
            self.view_ticker.load(arg)
        elif tab == "filer" and arg:
            if self.view_filer.list.exists(arg):
                self.view_filer.list.selection_set(arg)
                self.view_filer.list.see(arg)
            self.view_filer.load(arg)

    # ── filters ──────────────────────────────
    def _section(self, parent, title):
        tk.Label(parent, text=title, font=FONT_SM, bg=BG2, fg=ACCENT,
                 anchor="w").pack(fill="x", padx=12, pady=(12, 2))

    def _check(self, parent, var, text):
        c = tk.Checkbutton(parent, text=text, variable=var, font=FONT_SM,
                           bg=BG2, fg=FG, selectcolor=BG3, activebackground=BG2,
                           activeforeground=ACCENT, anchor="w", bd=0,
                           highlightthickness=0, command=self.refresh)
        c.pack(fill="x", padx=16)
        return c

    def _build_filters(self, p):
        cv = tk.Canvas(p, bg=BG2, highlightthickness=0, width=246)
        sb = ttk.Scrollbar(p, orient="vertical", command=cv.yview)
        inner = tk.Frame(cv, bg=BG2)
        inner.bind("<Configure>",
                   lambda e: cv.configure(scrollregion=cv.bbox("all")))
        cv.create_window((0, 0), window=inner, anchor="nw", width=228)
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self._section(inner, "SOURCE")
        for k, lab in SOURCE_LABELS:
            self._check(inner, self.v_source[k], lab)
        c = tk.Checkbutton(inner, text="lobbying / contracts overlay",
                           variable=self.v_overlay, font=FONT_SM, bg=BG2,
                           fg=FG_DIM, selectcolor=BG3, activebackground=BG2,
                           activeforeground=ACCENT, anchor="w", bd=0,
                           highlightthickness=0, wraplength=200,
                           justify="left")
        c.pack(fill="x", padx=16)

        self._section(inner, "CHAMBER")
        for k in ("house", "senate", "corporate", "fund"):
            self._check(inner, self.v_chamber[k], k)

        self._section(inner, "PARTY")
        for k in ("D", "R", "I"):
            self._check(inner, self.v_party[k], k)

        self._section(inner, "OWNER")
        for k in OWNERS:
            self._check(inner, self.v_owner[k], k)

        self._section(inner, "TRANSACTION")
        for k, lab in TXNS:
            self._check(inner, self.v_txn[k], lab)

        self._section(inner, "ASSET")
        for k, lab in ASSETS:
            self._check(inner, self.v_asset[k], lab)
        self._check(inner, self.v_optonly, "only rows with strike/expiry")

        self._section(inner, "AMOUNT AT LEAST")
        om = tk.OptionMenu(inner, self.v_amount, *[a for a, _ in AMOUNTS],
                           command=lambda *_: self.refresh())
        om.configure(bg=BG3, fg=FG, font=FONT_SM, relief="flat", bd=0,
                     highlightthickness=0, activebackground=BORDER,
                     width=18, anchor="w")
        om["menu"].configure(bg=BG3, fg=FG, font=FONT_SM)
        om.pack(fill="x", padx=16, pady=2)

        self._section(inner, "COMMITTEE")
        self.cmb_committee = ttk.Combobox(
            inner, textvariable=self.v_committee, font=FONT_SM, width=26,
            state="readonly", values=["(any)"])
        self.cmb_committee.pack(fill="x", padx=16, pady=2)
        self.cmb_committee.bind("<<ComboboxSelected>>",
                                lambda e: self.refresh())

        self._section(inner, "FILER CONTAINS")
        e1 = tk.Entry(inner, textvariable=self.v_filer, font=FONT_SM, bg=BG3,
                      fg=FG, insertbackground=ACCENT, relief="flat", bd=4)
        e1.pack(fill="x", padx=16, pady=2)
        e1.bind("<Return>", lambda e: self.refresh())

        self._section(inner, "TICKER")
        e2 = tk.Entry(inner, textvariable=self.v_ticker, font=FONT_SM, bg=BG3,
                      fg=FG, insertbackground=ACCENT, relief="flat", bd=4)
        e2.pack(fill="x", padx=16, pady=2)
        e2.bind("<Return>", lambda e: self.refresh())

        self._section(inner, "DISCLOSED WITHIN (DAYS)")
        e3 = tk.Entry(inner, textvariable=self.v_days, font=FONT_SM, bg=BG3,
                      fg=FG, insertbackground=ACCENT, relief="flat", bd=4)
        e3.pack(fill="x", padx=16, pady=2)
        e3.bind("<Return>", lambda e: self.refresh())

        tk.Frame(inner, bg=BG2, height=8).pack()
        self._btn(inner, "Apply", self.refresh, fg="#000000",
                  bg=ACCENT).pack(fill="x", padx=16, pady=(4, 2))
        self._btn(inner, "Reset", self._reset_filters).pack(
            fill="x", padx=16, pady=(0, 14))

    def _reset_filters(self):
        for d, on in ((self.v_source, True), (self.v_owner, True),
                      (self.v_chamber, True), (self.v_party, True)):
            for v in d.values():
                v.set(on)
        for k, v in self.v_txn.items():
            v.set(k == "P")
        for k, v in self.v_asset.items():
            v.set(k in ("ST", "OP"))
        self.v_amount.set("0")
        self.v_filer.set("")
        self.v_ticker.set("")
        self.v_committee.set("(any)")
        self.v_optonly.set(False)
        self.v_days.set("30")
        self.refresh()

    # ── feed table ───────────────────────────
    COLS = (("notify_date", "disclosed", 92),
            ("trade_date", "traded", 92),
            ("lag", "lag", 46),
            ("filer_name", "filer", 188),
            ("party", "", 26),
            ("chamber", "chamber", 74),
            ("ticker", "ticker", 66),
            ("txn_type", "txn", 78),
            ("owner", "owner", 62),
            ("amount", "amount", 126),
            ("option", "option", 150),
            ("asset_name", "asset", 300),
            ("flags", "flags", 150))

    def _build_feed(self, p):
        head = tk.Frame(p, bg=BG)
        head.pack(fill="x", padx=10, pady=(8, 4))
        self.count_lbl = tk.Label(head, text="", font=FONT_SM, bg=BG,
                                  fg=FG_DIM, anchor="w")
        self.count_lbl.pack(side="left")
        self._btn(head, "open filing", self._open_doc).pack(side="right")
        self._btn(head, "export csv", self._export).pack(side="right", padx=6)

        wrap = tk.Frame(p, bg=BG)
        wrap.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        cols = [c for c, _, _ in self.COLS]
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                 selectmode="browse")
        for c, lab, w in self.COLS:
            self.tree.heading(c, text=lab,
                              command=lambda cc=c: self._sort(cc))
            self.tree.column(c, width=w, anchor="w", stretch=(c == "asset_name"))
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.tag_configure("buy", foreground=GREEN)
        self.tree.tag_configure("sell", foreground=RED)
        self.tree.tag_configure("flagged", foreground=YELLOW)
        self.tree.bind("<Double-1>", lambda e: self._open_doc())
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self._sort_col, self._sort_desc = "notify_date", True

        # Detail strip. The table shows a trimmed asset name; this shows the
        # whole one, the text it was parsed out of, and whatever the
        # influence overlay knows about the ticker — so a row can be checked
        # against its source without leaving the window.
        det = tk.Frame(p, bg=BG2, height=112)
        det.pack(fill="x", side="bottom", padx=10, pady=(0, 8))
        det.pack_propagate(False)
        self.detail = tk.Text(det, bg=BG2, fg=FG_DIM, font=FONT_SM,
                              relief="flat", wrap="word", padx=12, pady=8,
                              height=6)
        self.detail.pack(fill="both", expand=True)
        for tag, col in (("hd", ACCENT), ("dim", FG_DIM), ("warn", YELLOW),
                         ("blue", BLUE)):
            self.detail.tag_configure(tag, foreground=col)

    def _on_select(self, _evt=None):
        sel = self.tree.selection()
        self.detail.delete("1.0", "end")
        if not sel:
            return
        r = next((x for x in self._rows_cache if x["id"] == sel[0]), None)
        if not r:
            return
        self.detail.insert("end", (r.get("asset_name") or "") + "\n", "hd")
        flags = json.loads(r.get("parse_flags") or "[]")
        if flags:
            self.detail.insert("end", "flags: {}\n".format(", ".join(flags)),
                               "warn")
        if r.get("raw"):
            self.detail.insert("end", "source text: {}\n".format(
                r["raw"][:300]), "dim")
        inf = {}
        if r.get("ticker"):
            try:
                inf = pe.influence_for(self.conn, r["ticker"])
            except Exception:
                inf = {}
        if inf:
            line = "{}: lobbying ${:,.0f} in {}".format(
                r["ticker"], inf.get("lobby_spend") or 0, inf.get("year"))
            if inf.get("lobby_growth") is not None:
                line += " ({:+.0%} on last year)".format(inf["lobby_growth"])
            if inf.get("contract_total"):
                line += " · federal awards ${:,.0f} across {}".format(
                    inf["contract_total"], inf.get("contract_count") or 0)
            line += "  [matched as '{}']".format(inf.get("matched_name"))
            self.detail.insert("end", line + "\n", "blue")

    def _sort(self, col):
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, True
        self._paint(self._rows_cache)

    # ── record tab ───────────────────────────
    def _build_record(self, p):
        bar = tk.Frame(p, bg=BG)
        bar.pack(fill="x", padx=10, pady=(8, 4))
        self.v_by = tk.StringVar(value="filer_name")
        self.v_h = tk.StringVar(value="20")
        self.v_dir = tk.StringVar(value="P")
        for label, var, opts in (
                ("group by", self.v_by,
                 ["filer_name", "committees", "party", "chamber", "source"]),
                ("horizon", self.v_h, ["10", "20", "60"]),
                ("side", self.v_dir, ["P", "S"])):
            tk.Label(bar, text=label, font=FONT_SM, bg=BG,
                     fg=FG_DIM).pack(side="left", padx=(8, 3))
            om = tk.OptionMenu(bar, var, *opts)
            om.configure(bg=BG3, fg=FG, font=FONT_SM, relief="flat", bd=0,
                         highlightthickness=0, activebackground=BORDER)
            om["menu"].configure(bg=BG3, fg=FG, font=FONT_SM)
            om.pack(side="left")
        self._btn(bar, "compute", self._record, fg="#000000",
                  bg=ACCENT).pack(side="left", padx=12)

        self.rec_txt = tk.Text(p, bg=BG2, fg=FG, font=FONT_SM, relief="flat",
                               wrap="word", padx=14, pady=12,
                               insertbackground=ACCENT)
        self.rec_txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        for tag, col in (("hd", ACCENT), ("good", GREEN), ("bad", RED),
                         ("dim", FG_DIM), ("warn", YELLOW), ("blue", BLUE)):
            self.rec_txt.tag_configure(tag, foreground=col)
        self.rec_txt.tag_configure("bold", font=("Consolas", 9, "bold"))

    def _build_log(self, p):
        self.log_txt = tk.Text(p, bg=BG2, fg=FG_DIM, font=FONT_SM,
                               relief="flat", wrap="word", padx=14, pady=12)
        self.log_txt.pack(fill="both", expand=True, padx=10, pady=10)
        for tag, col in (("ok", GREEN), ("err", RED), ("hd", ACCENT)):
            self.log_txt.tag_configure(tag, foreground=col)

    def _build_status(self):
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x", side="bottom")
        bar = tk.Frame(self.root, bg=BG2, height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status = tk.Label(bar, text="ready", font=FONT_SM, bg=BG2,
                               fg=FG_DIM, anchor="w")
        self.status.pack(side="left", padx=14)
        self.scan_lbl = tk.Label(bar, text="scanner stopped", font=FONT_SM,
                                 bg=BG2, fg=FG_DIM)
        self.scan_lbl.pack(side="right", padx=14)

    def _set_status(self, text, color=FG_DIM):
        self.status.config(text=text, fg=color)

    def _log(self, text, tag=None):
        self.log_txt.insert("end", "{}  {}\n".format(
            datetime.now().strftime("%H:%M:%S"), text), tag or ())
        self.log_txt.see("end")

    def _greet(self):
        s = pe.summary(self.conn)
        if not s["total"]:
            self._set_status(
                "Empty store. Load snapshot for an instant start, or "
                "Backfill 30d to read the filings directly.", ACCENT)
        else:
            self._set_status("{} disclosures, {} graded, {} .. {}".format(
                s["total"], s["graded"], s["range"].get("lo"),
                s["range"].get("hi")))

    # ── query ────────────────────────────────
    def _where(self):
        w, p = [], []
        srcs = [k for k, _ in SOURCE_LABELS if self.v_source[k].get()]
        if srcs and len(srcs) < len(SOURCE_LABELS):
            w.append("source IN ({})".format(",".join("?" * len(srcs))))
            p += srcs
        elif not srcs:
            return "1=0", []

        for var, col in ((self.v_owner, "owner"), (self.v_txn, "txn_type"),
                         (self.v_asset, "asset_type"),
                         (self.v_chamber, "chamber")):
            on = [k for k, v in var.items() if v.get()]
            if on and len(on) < len(var):
                w.append("{} IN ({})".format(col, ",".join("?" * len(on))))
                p += on
            elif not on:
                return "1=0", []

        parties = [k for k, v in self.v_party.items() if v.get()]
        if parties and len(parties) < 3:
            # A Form 4 or 13F row has no party. Filtering on party would
            # drop them silently, so they are kept explicitly.
            w.append("(party IN ({}) OR party IS NULL)".format(
                ",".join("?" * len(parties))))
            p += parties

        amt = self.v_amount.get()
        if amt and amt != "0":
            w.append("amount_lo >= ?")
            p.append(float(amt))

        if self.v_optonly.get():
            w.append("(strike IS NOT NULL OR expiry IS NOT NULL)")

        f = self.v_filer.get().strip()
        if f:
            w.append("filer_name LIKE ?")
            p.append("%{}%".format(f))
        t = self.v_ticker.get().strip().upper()
        if t:
            w.append("ticker = ?")
            p.append(t)
        c = self.v_committee.get()
        if c and c != "(any)":
            w.append("committees LIKE ?")
            p.append("%{}%".format(c))
        try:
            d = int(self.v_days.get())
            if d > 0:
                w.append("notify_date >= ?")
                p.append((date.today() - timedelta(days=d)).isoformat())
        except ValueError:
            pass
        return (" AND ".join(w) if w else "1=1"), p

    def refresh(self, *_):
        where, params = self._where()
        try:
            rows = [dict(r) for r in self.conn.execute(
                "SELECT * FROM disclosures WHERE {} ORDER BY notify_date DESC,"
                " filer_name LIMIT 4000".format(where), params)]
        except Exception as e:                          # noqa: BLE001
            self._set_status("query failed: {}".format(e), RED)
            return
        self._rows_cache = rows
        self._paint(rows)
        self._refresh_committees()

    def _refresh_committees(self):
        try:
            seen = set()
            for r in self.conn.execute(
                    "SELECT DISTINCT committees FROM disclosures "
                    "WHERE committees IS NOT NULL AND committees != '[]' "
                    "LIMIT 3000"):
                for c in json.loads(r["committees"] or "[]"):
                    seen.add(c)
            vals = ["(any)"] + sorted(seen)
            if list(self.cmb_committee["values"]) != vals:
                self.cmb_committee["values"] = vals
        except Exception:
            pass

    @staticmethod
    def _amount_text(lo, hi):
        if lo is None:
            return "--"
        if hi is None:
            return "over ${:,.0f}".format(lo)
        return "${:,.0f} - ${:,.0f}".format(lo, hi)

    def _paint(self, rows):
        col = self._sort_col

        def key(r):
            if col == "lag":
                return self._lag(r) if self._lag(r) is not None else -1
            if col == "amount":
                return r.get("amount_lo") or 0
            if col == "option":
                return r.get("strike") or 0
            if col == "flags":
                return len(json.loads(r.get("parse_flags") or "[]"))
            return str(r.get(col) or "")

        try:
            rows = sorted(rows, key=key, reverse=self._sort_desc)
        except Exception:
            pass

        self.tree.delete(*self.tree.get_children())
        for r in rows:
            flags = json.loads(r.get("parse_flags") or "[]")
            lag = self._lag(r)
            opt = ""
            if r.get("option_type") or r.get("strike"):
                opt = "{} {} {}".format(
                    r.get("option_type") or "",
                    "${:g}".format(r["strike"]) if r.get("strike") else "",
                    r.get("expiry") or "").strip()
            vals = (r.get("notify_date") or "", r.get("trade_date") or "",
                    "" if lag is None else str(lag),
                    r.get("filer_name") or "", r.get("party") or "",
                    r.get("chamber") or "", r.get("ticker") or "--",
                    r.get("txn_type") or "", r.get("owner") or "",
                    self._amount_text(r.get("amount_lo"), r.get("amount_hi")),
                    opt, (r.get("asset_name") or "")[:120],
                    ",".join(flags))
            tag = "flagged" if flags else (
                "buy" if r.get("txn_type") == "P" else "sell")
            self.tree.insert("", "end", iid=r["id"], values=vals, tags=(tag,))
        self.count_lbl.config(
            text="{} rows{}".format(len(rows),
                                    "  (capped at 4000)" if len(rows) >= 4000
                                    else ""))

    @staticmethod
    def _lag(r):
        try:
            a = datetime.fromisoformat(r["trade_date"])
            b = datetime.fromisoformat(r["notify_date"])
            return (b - a).days
        except Exception:
            return None

    def _open_doc(self):
        sel = self.tree.selection()
        if not sel:
            return
        row = next((r for r in self._rows_cache if r["id"] == sel[0]), None)
        if row and row.get("doc_url"):
            webbrowser.open(row["doc_url"])

    def _export(self):
        if not self._rows_cache:
            return
        p = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
            initialfile="copy_trading_{}.csv".format(
                date.today().isoformat()))
        if not p:
            return
        import csv as _csv
        cols = [c for c in pf.ROW_FIELDS if c != "raw"]
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in self._rows_cache:
                w.writerow(r)
        self._set_status("exported {} rows to {}".format(
            len(self._rows_cache), os.path.basename(p)), GREEN)

    # ── scanner ──────────────────────────────
    def _toggle_run(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop()
            self.run_btn.config(text="▶  Run", bg=GREEN)
            self._set_status("stopping scanner...")
            return
        srcs = [k for k, _ in SOURCE_LABELS if self.v_source[k].get()]
        if not srcs:
            messagebox.showinfo("Copy Trading",
                                "Enable at least one source to scan.")
            return
        if self.v_overlay.get():
            srcs.append(OVERLAY)
        self.worker = pe.ScanWorker(sources=srcs, on_event=self._on_event)
        self.worker.start()
        self.run_btn.config(text="■  Stop", bg=RED)
        self._log("scanner started on: {}".format(", ".join(srcs)), "hd")
        self.nb.select(self._tab_index["scanner"])

    def _on_event(self, kind, msg, payload):
        """Called from the worker thread. Never touches tkinter directly."""
        self.events.put((kind, msg, payload))

    def _pump(self):
        drained = False
        while True:
            try:
                kind, msg, payload = self.events.get_nowait()
            except queue.Empty:
                break
            drained = True
            if kind == "source":
                self._log("{:<12} docs={} new rows={} skipped={}".format(
                    msg, payload["docs"], payload["new_rows"],
                    payload["skipped"]),
                    "ok" if not payload["skipped"] else None)
                for reason, n in sorted(payload["errors"].items(),
                                        key=lambda x: -x[1]):
                    self._log("    {:>4}  {}".format(n, reason), "err")
            elif kind == "pass":
                self._log("pass {} complete — {} rows added since start"
                          .format(payload["passes"], payload["new_rows"]),
                          "hd")
                self.refresh()
            elif kind == "error":
                self._log(msg, "err")
            elif kind == "stop":
                self.run_btn.config(text="▶  Run", bg=GREEN)
                self._log("scanner stopped", "hd")
            else:
                self._log(msg)
        if drained and self.worker and self.worker.is_alive():
            s = self.worker.stats
            self.scan_lbl.config(
                text="scanning · {} passes · {} rows · {} skipped".format(
                    s["passes"], s["new_rows"], s["errors"]), fg=GREEN)
        elif not (self.worker and self.worker.is_alive()):
            self.scan_lbl.config(text="scanner stopped", fg=FG_DIM)
        self.root.after(400, self._pump)

    # ── long jobs, off the UI thread ─────────
    def _job(self, name, fn):
        self._set_status("{} running...".format(name), ACCENT)
        self.nb.select(self._tab_index["scanner"])

        def work():
            try:
                out = fn()
                self.events.put(("log", "{}: {}".format(name, out), None))
            except Exception as e:                       # noqa: BLE001
                self.events.put(("error", "{} failed: {}".format(name, e),
                                 None))
            self.root.after(0, self._after_job, name)

        threading.Thread(target=work, daemon=True).start()

    def _after_job(self, name):
        self._set_status("{} finished".format(name), GREEN)
        self.refresh()
        self.refresh_views()
        self._greet()

    def refresh_views(self):
        """Rebuild the boards after new rows land."""
        for v in (self.view_clusters, self.view_movers):
            try:
                v.reload()
            except Exception as e:                     # noqa: BLE001
                self._log("view refresh failed: {}".format(e), "err")
        try:
            self.view_filer.refresh_list()
        except Exception:
            pass

    def _backfill(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Copy Trading",
                                "Stop the scanner before backfilling.")
            return
        srcs = [k for k, _ in SOURCE_LABELS if self.v_source[k].get()]
        if self.v_overlay.get():
            srcs.append(OVERLAY)

        def run():
            conn = pe.connect()
            try:
                out = pe.backfill(30, sources=srcs, conn=conn,
                                  progress=lambda s, w, r: self.events.put(
                                      ("log", "  [{}] {} rows={}".format(
                                          s, str(w)[:24], r["new_rows"]),
                                       None)))
                return ", ".join("{}={}".format(k, v["new_rows"])
                                 for k, v in out.items())
            finally:
                conn.close()

        self._job("backfill", run)

    def _grade(self):
        def run():
            conn = pe.connect()
            try:
                st = pe.grade(conn, progress=lambda i, n, t, m:
                              self.events.put(("log", "  [{}/{}] {} {}".format(
                                  i, n, t, m), None)))
                return json.dumps(st)
            finally:
                conn.close()

        self._job("grade", run)

    def _publish(self):
        out = pe.publish(conn=self.conn)
        self._set_status(
            "published {} rows to {} ({:,} bytes) — commit this file to "
            "share it".format(out["rows"], os.path.basename(out["path"]),
                              out["bytes"]), GREEN)
        self._log("published {} -> {} rows, {:,} bytes".format(
            out["path"], out["rows"], out["bytes"]), "ok")

    def _load_snapshot(self):
        p = filedialog.askopenfilename(
            filetypes=[("Snapshot", "*.json.gz"), ("All", "*.*")],
            initialfile=os.path.basename(pe.SNAPSHOT_FILE))
        if not p:
            return
        n = pe.import_snapshot(p, self.conn)
        self.mode_lbl.config(text="snapshot loaded", fg=TEAL)
        self._set_status("imported {} rows from {}".format(
            n, os.path.basename(p)), GREEN)
        self.refresh()
        self._greet()

    # ── record ───────────────────────────────
    def _w(self, text, tag=None):
        self.rec_txt.insert("end", text + "\n", tag or ())

    def _record(self):
        self.rec_txt.delete("1.0", "end")
        h = int(self.v_h.get())
        direction = self.v_dir.get()
        by = self.v_by.get()
        s = pe.summary(self.conn)

        if not s["graded"]:
            self._w("NOTHING GRADED YET", "hd")
            self._w("")
            self._w("Forward returns need their window to close before they "
                    "mean anything. Run Grade once disclosures are at least "
                    "10 trading days old.", "dim")
            self._w("")
            self._w("Until then this tool has a dataset and no record, and "
                    "saying otherwise would be the whole problem it was "
                    "built to avoid.", "dim")
            return

        srcs = tuple(k for k, _ in SOURCE_LABELS if self.v_source[k].get())
        self._w("WHAT THE FILER GOT vs WHAT THE PUBLIC COULD GET", "hd")
        self._w("sources: {}   ·   paired rows only".format(
            ", ".join(srcs) or "none"), "dim")
        self._w("")
        g = pe.anchor_gap(self.conn, h, direction, sources=srcs or None)
        t, n = g["trade"], g["notify"]
        for label, d in (("from trade date ", t), ("from notify date", n)):
            if not d["n"]:
                continue
            self._w("  {}  n={:<5}  hit {:<7}  mean {:<9}  median {}".format(
                label, d["n"],
                "--" if d["hit"] is None else "{:.1%}".format(d["hit"]),
                pe._pct(d["mean"]), pe._pct(d["median"])),
                "good" if (d["mean"] or 0) > 0 else "bad")
        if t["mean"] is not None and n["mean"] is not None:
            self._w("")
            self._w("  the {}-day disclosure delay costs {}".format(
                h, pe._pct(n["mean"] - t["mean"])), "warn")
        if g.get("by_source"):
            self._w("")
            for src, d in sorted(g["by_source"].items()):
                if not d["trade"]["n"]:
                    continue
                self._w("    {:<12} n={:<5} trade {:<10} notify {:<10} gap {}"
                        .format(src, d["trade"]["n"],
                                pe._pct(d["trade"]["mean"]),
                                pe._pct(d["notify"]["mean"]),
                                pe._pct((d["notify"]["mean"] or 0)
                                        - (d["trade"]["mean"] or 0))), "dim")
        if g.get("delay_days"):
            self._w("")
            self._w("  mean delay {:.1f} days across {} congressional rows"
                    .format(g["delay_days"], g["delay_n"]), "dim")
        self._w("  A 13F trade date is the quarter END, not an execution "
                "time, so its gap is partly the length of a quarter. The "
                "congressional rows are the ones where both dates mean what "
                "they say.", "dim")

        self._w("")
        self._w("-" * 74, "dim")
        self._w("LEADERBOARD — {} by {}, {}d from notify date".format(
            "purchases" if direction == "P" else "sales", by, h), "hd")
        self._w("")
        lb = pe.leaderboard(self.conn, h, "notify", direction, by, min_n=10,
                            sources=srcs or None)
        base, perm = lb["base"], lb["permutation"]
        if base["n"]:
            self._w("  base rate over everything: n={} hit {:.1%} mean {}"
                    .format(base["n"], base["hit"], pe._pct(base["mean"])),
                    "dim")
            self._w("")
        if not lb["table"]:
            self._w("  Not enough graded rows yet — a group needs 10.", "dim")
            return
        self._w("  {:<40}{:>5}{:>11}{:>8}".format("", "n", "mean", "hit"),
                "dim")
        for row in lb["table"][:20]:
            self._w("  {:<40}{:>5}{:>11}{:>8}".format(
                str(row["key"])[:40], row["n"], pe._pct(row["mean"]),
                "{:.0%}".format(row["hit"])))

        self._w("")
        self._w("-" * 74, "dim")
        for w in lb.get("warnings", []):
            self._w("!  " + w, "warn")
        if lb.get("warnings"):
            self._w("")
        if perm.get("p") is None:
            self._w("  No family-wise test: {}".format(perm.get("reason")),
                    "dim")
            return
        self._w("PERMUTATION TEST — {} groups, {} shuffles".format(
            perm["groups"], perm["iters"]), "hd")
        self._w("  leader         {} (n={})".format(
            str(perm["leader"])[:44], perm["n_leader"]))
        self._w("  observed mean  {}".format(pe._pct(perm["observed"])))
        self._w("  p              {:.3f}".format(perm["p"]),
                "bad" if perm["p"] > 0.10 else
                "warn" if perm["p"] > 0.05 else "good")
        self._w("")
        if perm["p"] > 0.10:
            self._w("READ THIS AS NOISE.", "bad")
            self._w("A pool with no skill in it at all produces a leader this "
                    "good {:.0%} of the time. The name at the top of that "
                    "table is the winner of a lottery that was always going "
                    "to have one.".format(perm["p"]), "dim")
        elif perm["p"] > 0.05:
            self._w("WEAK.", "warn")
            self._w("Survives the correction, but not comfortably, and this "
                    "is still a retrospective screen. Register it in "
                    "hypotheses.py and judge it on rows that arrive after.",
                    "dim")
        else:
            self._w("SURVIVES THE FAMILY-WISE CORRECTION at this sample size.",
                    "good")
            self._w("That is not the same as an edge. The screen still chose "
                    "this horizon, this side and this grouping after seeing "
                    "the data. Register it in hypotheses.py before acting on "
                    "it.", "dim")

    def on_close(self):
        try:
            cards.close_all()      # floating Toplevels outlive the root
        except Exception:
            pass
        try:
            self.view_clusters.stop()          # stop the animation loop first
        except Exception:
            pass
        if self.worker and self.worker.is_alive():
            self.worker.stop()
            self.worker.join(3)
        try:
            self.conn.close()
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    app = CopyTradingApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
