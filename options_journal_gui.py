"""
options_journal_gui.py
======================
The screens for options_journal: the "journal this contract" dialog and the
live position monitor.

Kept OUT of squeeze_analyzer_gui.py deliberately — that file is already 90KB,
and the journal is usable without an analysis running (you want to check open
positions on a Tuesday without re-scanning anything). It is imported lazily by
the analyzer and can also be launched standalone from the dashboard:

    python options_journal_gui.py

TWO HONESTY RULES BAKED INTO THE DISPLAY
----------------------------------------
1. MARK vs BID are shown as separate columns and never blended. The mark is
   the midpoint — what the position is "worth". The bid is what you would
   actually receive if you sold right now. On the chains this toolkit hunts
   those are very different numbers, and only one of them is money.
2. STALE marks are labelled. yfinance clears bid/ask outside market hours; the
   last trade is used instead and flagged, rather than being painted as a live
   quote on a screen that looks live.
"""

import tkinter as tk
from datetime import date, datetime

import options_journal as oj

BG     = "#0A0E14"
BG2    = "#12171F"
BG3    = "#1A2030"
FG     = "#CDD6F4"
FG_DIM = "#6C7086"
ACCENT = "#F4C430"
GREEN  = "#A6E3A1"
RED    = "#F38BA8"
BLUE   = "#89B4FA"
TEAL   = "#94E2D5"
BORDER = "#313244"

FONT_HD = ("Consolas", 13, "bold")
FONT    = ("Consolas", 10)
FONT_SM = ("Consolas", 9)

REFRESH_SECONDS = 60      # quotes are 15-min delayed; faster is pointless


def _f(v, d=None):
    return oj._f(v, d)


# ─────────────────────────────────────────────
# ENTRY DIALOG
# ─────────────────────────────────────────────

def journal_dialog(parent, ticker: str, expiry: str, strike: float,
                   row: dict = None, ctx: dict = None, right: str = "C",
                   on_saved=None):
    """Modal: confirm contracts + the fill price you ACTUALLY paid, then
    write the entry. Returns the journal id, or None if cancelled."""
    row = row or {}
    ctx = ctx or {}
    ask = _f(row.get("ask"), 0.0) or 0.0
    bid = _f(row.get("bid"), 0.0) or 0.0

    win = tk.Toplevel(parent)
    win.title(f"Journal — {ticker} {expiry[5:]} ${strike:g}{right}")
    win.configure(bg=BG)
    win.transient(parent)
    win.grab_set()
    win.resizable(False, False)

    result = {"id": None}

    tk.Label(win, text=f"{ticker.upper()}  {expiry}  ${strike:g}{right}",
             font=FONT_HD, bg=BG, fg=TEAL).pack(anchor="w", padx=14,
                                                pady=(12, 2))
    q = f"bid {bid:.2f} / ask {ask:.2f}"
    if row.get("stale"):
        q += "   (LAST-PX — market closed, this is not a live quote)"
    tk.Label(win, text=q, font=FONT_SM, bg=BG,
             fg=ACCENT if row.get("stale") else FG_DIM).pack(anchor="w",
                                                             padx=14)

    # what the model claims — frozen into the entry alongside your fill
    pred = tk.Frame(win, bg=BG2)
    pred.pack(fill="x", padx=12, pady=10)
    def _row(lbl, val, color=FG):
        fr = tk.Frame(pred, bg=BG2)
        fr.pack(fill="x", padx=10, pady=1)
        tk.Label(fr, text=lbl, font=FONT_SM, bg=BG2, fg=FG_DIM,
                 width=22, anchor="w").pack(side="left")
        tk.Label(fr, text=val, font=FONT_SM, bg=BG2, fg=color,
                 anchor="w").pack(side="left")
    def _pct(v):
        x = _f(v)
        return f"{x:+.1%}" if x is not None else "—"
    _row("model says (predictions recorded with this entry):", "", ACCENT)
    _row("  EV @ catalyst exit", _pct(row.get("sc_ev_exit")),
         GREEN if (_f(row.get("sc_ev_exit")) or 0) > 0 else RED)
    _row("  EV @ expiry", _pct(row.get("sc_ev")),
         GREEN if (_f(row.get("sc_ev")) or 0) > 0 else RED)
    _row("  P(above breakeven)", _pct(row.get("sc_p_be")))
    _row("  breakeven", (f"${_f(row.get('breakeven')):,.2f}"
                         if _f(row.get("breakeven")) else "—"))
    _row("  suggested size", oj.suggested_size_pct(row.get("kelly")), BLUE)

    form = tk.Frame(win, bg=BG)
    form.pack(fill="x", padx=14, pady=(2, 6))
    tk.Label(form, text="Contracts:", font=FONT, bg=BG,
             fg=FG).grid(row=0, column=0, sticky="w", pady=4)
    n_var = tk.StringVar(value="1")
    tk.Entry(form, textvariable=n_var, font=FONT, width=10, bg=BG3, fg=FG,
             insertbackground=FG, relief="flat").grid(row=0, column=1,
                                                      sticky="w", padx=8)
    tk.Label(form, text="Fill price / share:", font=FONT, bg=BG,
             fg=FG).grid(row=1, column=0, sticky="w", pady=4)
    px_var = tk.StringVar(value=f"{ask:.2f}" if ask else "")
    tk.Entry(form, textvariable=px_var, font=FONT, width=10, bg=BG3, fg=FG,
             insertbackground=FG, relief="flat").grid(row=1, column=1,
                                                      sticky="w", padx=8)
    tk.Label(form, text="what you ACTUALLY paid — the gap\n"
                        "from the ask is your slippage",
             font=FONT_SM, bg=BG, fg=FG_DIM,
             justify="left").grid(row=1, column=2, sticky="w")
    tk.Label(form, text="Notes:", font=FONT, bg=BG,
             fg=FG).grid(row=2, column=0, sticky="w", pady=4)
    note_var = tk.StringVar(value="")
    tk.Entry(form, textvariable=note_var, font=FONT, width=38, bg=BG3, fg=FG,
             insertbackground=FG, relief="flat").grid(row=2, column=1,
                                                      columnspan=2,
                                                      sticky="we", padx=8)

    cost_lbl = tk.Label(win, text="", font=FONT_SM, bg=BG, fg=FG_DIM)
    cost_lbl.pack(anchor="w", padx=14)

    def _recost(*_):
        try:
            n = int(float(n_var.get()))
            px = float(px_var.get())
            fee = oj.DEFAULT_FEE_PER_CONTRACT * n
            cost_lbl.config(
                text=f"cost basis  {n} x ${px:.2f} x 100 + ${fee:.2f} fees "
                     f"=  ${px * 100 * n + fee:,.2f}")
        except (ValueError, TypeError):
            cost_lbl.config(text="")
    n_var.trace_add("write", _recost)
    px_var.trace_add("write", _recost)
    _recost()

    err = tk.Label(win, text="", font=FONT_SM, bg=BG, fg=RED)
    err.pack(anchor="w", padx=14)

    def _save():
        try:
            n = int(float(n_var.get()))
            px = float(px_var.get())
        except (ValueError, TypeError):
            err.config(text="contracts and fill price must be numbers")
            return
        if n <= 0 or px <= 0:
            err.config(text="contracts and fill price must be positive")
            return
        try:
            jid = oj.add_entry(ticker, expiry, strike, n, px, row=row,
                               ctx=ctx, right=right, notes=note_var.get())
        except Exception as e:
            err.config(text=f"could not save: {e}")
            return
        result["id"] = jid
        win.destroy()
        if on_saved:
            on_saved(jid)

    btns = tk.Frame(win, bg=BG)
    btns.pack(fill="x", padx=14, pady=12)
    tk.Button(btns, text="Cancel", font=FONT_SM, bg=BG3, fg=FG, relief="flat",
              padx=14, pady=4, cursor="hand2",
              command=win.destroy).pack(side="right")
    tk.Button(btns, text="Add to journal", font=FONT_SM, bg="#2da44e",
              fg="#FFFFFF", relief="flat", padx=14, pady=4, cursor="hand2",
              command=_save).pack(side="right", padx=8)

    win.update_idletasks()
    parent.wait_window(win)
    return result["id"]


# ─────────────────────────────────────────────
# LIVE POSITION MONITOR
# ─────────────────────────────────────────────

def open_journal_window(parent=None, highlight: str = ""):
    """Live view of open positions. Refreshes on a timer while open; the
    nightly job keeps marks current when it is closed."""
    own_root = parent is None
    root = tk.Tk() if own_root else tk.Toplevel(parent)
    root.title("Options Journal")
    root.configure(bg=BG)
    root.minsize(1020, 460)

    state = {"job": None, "busy": False, "closed": False}

    head = tk.Frame(root, bg=BG2)
    head.pack(fill="x")
    tk.Label(head, text="OPTIONS JOURNAL", font=FONT_HD, bg=BG2,
             fg=TEAL).pack(side="left", padx=12, pady=8)
    status = tk.Label(head, text="", font=FONT_SM, bg=BG2, fg=FG_DIM)
    status.pack(side="left", padx=10)
    tk.Button(head, text="Refresh now", font=FONT_SM, bg="#1f6feb",
              fg="#FFFFFF", relief="flat", padx=10, pady=3, cursor="hand2",
              command=lambda: _refresh(force=True)).pack(side="right", padx=10)

    tk.Label(root,
             text="MARK is the midpoint (what it is worth). BID is what you "
                  "would actually receive selling now — on thin chains those "
                  "are very different numbers. Quotes are delayed ~15 min; "
                  "STALE means the market is closed and the last trade is "
                  "being shown instead of a live quote.",
             font=FONT_SM, bg=BG, fg=FG_DIM, wraplength=980,
             justify="left").pack(fill="x", padx=12, pady=(6, 2))

    cols = tk.Frame(root, bg=BG)
    cols.pack(fill="both", expand=True, padx=12, pady=6)
    hdr = (f"{'id':<16}{'contract':<20}{'n':>3}{'cost':>10}{'mark':>8}"
           f"{'bid':>8}{'P/L':>11}{'%':>8}{'vs pred':>9}{'dte':>5}  flags")
    tk.Label(cols, text=hdr, font=FONT_SM, bg=BG, fg=FG_DIM,
             anchor="w").pack(fill="x")
    listbox = tk.Text(cols, bg=BG2, fg=FG, font=FONT_SM, relief="flat",
                      height=16, wrap="none", insertbackground=FG)
    listbox.pack(fill="both", expand=True)
    for tag, col in (("good", GREEN), ("bad", RED), ("dim", FG_DIM),
                     ("warn", ACCENT), ("hi", BLUE)):
        listbox.tag_configure(tag, foreground=col)

    foot = tk.Frame(root, bg=BG2)
    foot.pack(fill="x")
    tk.Label(foot, text="Close a position:", font=FONT_SM, bg=BG2,
             fg=FG_DIM).pack(side="left", padx=(12, 4), pady=6)
    cid = tk.StringVar()
    tk.Entry(foot, textvariable=cid, font=FONT_SM, width=16, bg=BG3, fg=FG,
             insertbackground=FG, relief="flat").pack(side="left")
    tk.Label(foot, text="exit price:", font=FONT_SM, bg=BG2,
             fg=FG_DIM).pack(side="left", padx=(10, 4))
    cpx = tk.StringVar()
    tk.Entry(foot, textvariable=cpx, font=FONT_SM, width=10, bg=BG3, fg=FG,
             insertbackground=FG, relief="flat").pack(side="left")
    cmsg = tk.Label(foot, text="", font=FONT_SM, bg=BG2, fg=FG_DIM)
    cmsg.pack(side="left", padx=10)

    def _do_close():
        try:
            px = float(cpx.get())
        except (ValueError, TypeError):
            cmsg.config(text="exit price must be a number", fg=RED)
            return
        ok = oj.close_position(cid.get().strip(), px, "closed from journal UI")
        cmsg.config(text="closed" if ok else "no such open position",
                    fg=GREEN if ok else RED)
        if ok:
            cid.set("")
            cpx.set("")
            _render()

    tk.Button(foot, text="Close", font=FONT_SM, bg="#8b3a3a", fg="#FFFFFF",
              relief="flat", padx=12, pady=3, cursor="hand2",
              command=_do_close).pack(side="left", padx=8)

    def _render():
        rows = [r for r in oj.load_rows() if r.get("status") == "OPEN"]
        listbox.config(state="normal")
        listbox.delete("1.0", "end")
        if not rows:
            listbox.insert("end",
                           "  No open positions.\n\n"
                           "  Add one from the Contract P/L window in the "
                           "squeeze analyzer:\n"
                           "  the 'Journal this contract' button records the "
                           "trade together with\n"
                           "  every number the model predicted for it.\n",
                           "dim")
            listbox.config(state="disabled")
            return
        tot_cost = tot_pnl = 0.0
        for r in rows:
            mark, bid = _f(r.get("last_mark")), _f(r.get("last_bid"))
            pnl, pct = _f(r.get("unreal_pnl")), _f(r.get("unreal_pct"))
            cost = _f(r.get("cost_basis"), 0.0) or 0.0
            pred = _f(r.get("pred_sc_ev_exit"))
            tot_cost += cost
            tot_pnl += pnl or 0.0
            name = (f"{r['ticker']} {r['expiry'][5:]} "
                    f"${_f(r['strike']):g}{r.get('right', 'C')}")
            try:
                dte = (datetime.strptime(r["expiry"][:10], "%Y-%m-%d").date()
                       - date.today()).days
            except (ValueError, TypeError):
                dte = ""
            flags = []
            if r.get("mark_stale"):
                flags.append("STALE")
            if isinstance(dte, int) and dte <= 5:
                flags.append("EXPIRING")
            cat = (r.get("catalyst_iso") or "")[:10]
            if cat:
                try:
                    cd = (datetime.strptime(cat, "%Y-%m-%d").date()
                          - date.today()).days
                    if cd <= 0:
                        flags.append("CATALYST PASSED")
                except (ValueError, TypeError):
                    pass
            vs = (f"{(pct - pred):+.0%}" if (pct is not None
                                             and pred is not None) else "—")
            line = (f"{r['journal_id']:<16}{name:<20}"
                    f"{int(_f(r['contracts'], 0) or 0):>3}{cost:>10,.0f}"
                    f"{(f'{mark:.2f}' if mark is not None else '—'):>8}"
                    f"{(f'{bid:.2f}' if bid is not None else '—'):>8}"
                    f"{(f'{pnl:+,.0f}' if pnl is not None else '—'):>11}"
                    f"{(f'{pct:+.1%}' if pct is not None else '—'):>8}"
                    f"{vs:>9}{str(dte):>5}  {' '.join(flags)}\n")
            tag = ("hi" if r["journal_id"] == highlight else
                   ("good" if (pct or 0) > 0 else
                    ("bad" if pct is not None else "dim")))
            listbox.insert("end", line, tag)
        listbox.insert("end", "\n")
        listbox.insert("end",
                       f"  {'TOTAL':<36}{tot_cost:>10,.0f}"
                       f"{'':>16}{tot_pnl:>11,.0f}"
                       f"{(tot_pnl / tot_cost if tot_cost else 0):>8.1%}\n",
                       "good" if tot_pnl > 0 else "bad")
        listbox.insert("end",
                       "\n  'vs pred' = realized return so far minus the EV "
                       "the model predicted at entry.\n", "dim")
        listbox.config(state="disabled")

    def _refresh(force=False):
        if state["busy"] or state["closed"]:
            return
        state["busy"] = True
        status.config(text="fetching quotes...", fg=ACCENT)

        def _work():
            try:
                oj.auto_close_expired(verbose=False)
                res = oj.update_marks(verbose=False)
                msg = (f"updated {res['updated']}"
                       + (f", {res['failed']} unavailable"
                          if res["failed"] else "")
                       + f" — {datetime.now():%H:%M:%S}")
            except Exception as e:
                msg = f"refresh failed: {e}"
            if state["closed"]:
                return
            status.config(text=msg, fg=FG_DIM)
            state["busy"] = False
            _render()
            state["job"] = root.after(REFRESH_SECONDS * 1000, _refresh)

        # keep the UI responsive: the fetch is network-bound
        import threading
        threading.Thread(target=lambda: root.after(0, _work),
                         daemon=True).start()

    def _on_close():
        state["closed"] = True
        if state["job"]:
            try:
                root.after_cancel(state["job"])
            except Exception:
                pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    _render()
    root.after(200, _refresh)
    if own_root:
        root.mainloop()
    return root


if __name__ == "__main__":
    open_journal_window()
