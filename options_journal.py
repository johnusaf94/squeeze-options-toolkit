"""
options_journal.py
==================
The ledger that turns predictions into evidence.

WHY THIS EXISTS
---------------
Everything upstream of this file produces NUMBERS: expected value per premium
dollar, probability of finishing above breakeven, a Kelly ranking. None of it
has ever been checked against what a contract actually did. The squeeze side
of the toolkit had that problem too and it took 2,575 ungraded rows before it
got fixed; this file exists so the options side never accumulates the same
debt.

A journal entry freezes, at the moment of the trade:
  * what you actually paid (your fill, not the ask — the gap is slippage, and
    slippage is data)
  * every number the model predicted for that exact contract
  * WHICH MODEL said it (model_version)

That last field is the one people leave out and regret. The pricing math
changed materially — round-trip costs, forward/borrow pricing, a mixture
distribution replacing three point masses. Predictions made before and after
those changes are not comparable, and averaged together they answer nothing.
Stamped, they answer the question that matters: is the corrected model
actually better than the one it replaced?

WHAT IT WILL NOT DO
-------------------
It will not tell you the trade was good because it made money. One resolved
option is a coin flip with a story attached. The report deals in aggregates,
prints its own sample size, and stays quiet about edge until there is enough
of it to say anything — same discipline as the learning engine's gates.

USAGE
-----
    python options_journal.py list                 # open positions + P/L
    python options_journal.py update               # refresh marks from market
    python options_journal.py close J20260820-001 2.35
    python options_journal.py report               # predicted vs realized
    python options_journal.py import-fidelity FILE.csv
"""

import csv
import json
import math
import os
import sys
from datetime import date, datetime

# Installs the shared rate limiter before any yfinance call.
try:
    import yfinance_throttle  # noqa: F401
except Exception:                                   # pragma: no cover
    pass

_DIR = os.path.dirname(os.path.abspath(__file__))
JOURNAL_FILE = os.path.join(_DIR, "options_journal.csv")
LOCK_FILE = JOURNAL_FILE + ".lock"

# Bump this WHENEVER the pricing math changes in a way that moves predictions.
# Entries carry it forever, so "did the fix help?" stays answerable.
#   2026.08.20-a : round-trip costs, put-call-parity forward + CTB carry,
#                  lognormal mixture, exact Kelly, smile roll, per-move crush
MODEL_VERSION = "2026.08.20-a"

COLUMNS = [
    # ── identity ──
    "journal_id", "status", "opened_at", "model_version",
    "ticker", "expiry", "strike", "right", "occ_symbol",
    # ── the trade ──
    "contracts", "fill_price", "fees", "cost_basis",
    # ── market state at entry ──
    "spot_at_entry", "bid_at_entry", "ask_at_entry", "iv_at_entry",
    "oi_at_entry", "spread_pct_at_entry", "dte_at_entry",
    # ── what the model PREDICTED for this exact contract ──
    "pred_sc_ev", "pred_sc_ev_exit", "pred_kelly", "pred_val_edge",
    "pred_sc_p_be", "pred_mkt_p_be", "pred_breakeven",
    "scenario_text", "scenario_tier", "iv_crush", "carry", "fwd_method",
    "catalyst_iso", "catalyst_type",
    # ── live marks (rewritten on every update) ──
    "last_mark", "last_bid", "last_checked", "mark_stale",
    "unreal_pnl", "unreal_pct", "peak_pct", "trough_pct",
    # ── exit ──
    "closed_at", "exit_price", "exit_reason", "proceeds",
    "realized_pnl", "realized_pct", "days_held", "auto_closed",
    "notes",
]

CONTRACT_MULT = 100          # shares per contract
DEFAULT_FEE_PER_CONTRACT = 0.65


# ─────────────────────────────────────────────
# SMALL HELPERS
# ─────────────────────────────────────────────

def _f(v, default=None):
    try:
        x = float(v)
        return x if x == x else default          # NaN guard
    except (TypeError, ValueError):
        return default


def occ_symbol(ticker: str, expiry: str, right: str, strike: float) -> str:
    """OCC 21-character option symbol, e.g. AAPL260918C00320000.
    This is how a specific contract is looked up later — the journal must be
    able to find the exact thing you bought, not 'a call around that strike'."""
    d = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
    return (f"{ticker.upper()}{d:%y%m%d}{right.upper()[0]}"
            f"{int(round(strike * 1000)):08d}")


def suggested_size_pct(kelly) -> str:
    """Quarter-Kelly as a share of capital, as TEXT — no account size is
    stored anywhere by design. Full Kelly on a single option is far too
    aggressive to trade; quarter-Kelly is the conventional haircut for model
    error, and even that assumes the model is right about the distribution."""
    k = _f(kelly)
    if k is None or k <= 0:
        return "no position (model sees no edge)"
    return f"~{k / 4.0:.1%} of capital (quarter-Kelly; full Kelly {k:.0%})"


# ─────────────────────────────────────────────
# STORAGE (same lock/atomic discipline as squeeze_logger)
# ─────────────────────────────────────────────

def _acquire(timeout_s: float = 20.0) -> bool:
    import time
    deadline = time.time() + timeout_s
    while True:
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK_FILE) > 600:
                    os.remove(LOCK_FILE)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return False
            time.sleep(0.5)
        except OSError:
            return False


def _release():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def load_rows() -> list:
    if not os.path.exists(JOURNAL_FILE):
        return []
    with open(JOURNAL_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_rows(rows: list):
    tmp = JOURNAL_FILE + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in COLUMNS})
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, JOURNAL_FILE)


def _next_id(rows: list) -> str:
    today = f"J{date.today():%Y%m%d}"
    n = sum(1 for r in rows if (r.get("journal_id") or "").startswith(today))
    return f"{today}-{n + 1:03d}"


# ─────────────────────────────────────────────
# ADDING A TRADE
# ─────────────────────────────────────────────

def add_entry(ticker: str, expiry: str, strike: float, contracts: int,
              fill_price: float, row: dict = None, ctx: dict = None,
              right: str = "C", fees: float = None,
              notes: str = "") -> str:
    """Record a trade. `row` is the analyzed strike dict straight out of
    options_ev (all the predictions), `ctx` the surrounding run context
    (spot, scenarios, catalyst, crush). Both optional — a trade entered by
    hand is still worth journaling, it just grades against less.

    fill_price is what YOU paid per share, defaulting to the ask only because
    something has to. Type the real fill: the difference between it and the
    ask is your slippage, and slippage is one of the few things here you can
    actually control."""
    row = row or {}
    ctx = ctx or {}
    rows = load_rows()
    if fees is None:
        fees = DEFAULT_FEE_PER_CONTRACT * max(int(contracts), 0)
    cost = fill_price * CONTRACT_MULT * int(contracts) + fees
    try:
        dte = (datetime.strptime(expiry[:10], "%Y-%m-%d").date()
               - date.today()).days
    except (ValueError, TypeError):
        dte = ""
    entry = {c: "" for c in COLUMNS}
    entry.update({
        "journal_id": _next_id(rows),
        "status": "OPEN",
        "opened_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "model_version": MODEL_VERSION,
        "ticker": ticker.upper(), "expiry": expiry[:10],
        "strike": strike, "right": right.upper()[0],
        "occ_symbol": occ_symbol(ticker, expiry, right, strike),
        "contracts": int(contracts), "fill_price": round(float(fill_price), 4),
        "fees": round(float(fees), 2), "cost_basis": round(cost, 2),
        "spot_at_entry": ctx.get("spot", ""),
        "bid_at_entry": row.get("bid", ""), "ask_at_entry": row.get("ask", ""),
        "iv_at_entry": row.get("iv", ""), "oi_at_entry": row.get("oi", ""),
        "spread_pct_at_entry": row.get("spread_pct", ""),
        "dte_at_entry": dte,
        "pred_sc_ev": row.get("sc_ev", ""),
        "pred_sc_ev_exit": row.get("sc_ev_exit", ""),
        "pred_kelly": row.get("kelly", ""),
        "pred_val_edge": row.get("val_edge", ""),
        "pred_sc_p_be": row.get("sc_p_be", ""),
        "pred_mkt_p_be": row.get("mkt_p_be", ""),
        "pred_breakeven": row.get("breakeven", ""),
        "scenario_text": ctx.get("scenario_text", ""),
        "scenario_tier": ctx.get("scenario_tier", ""),
        "iv_crush": ctx.get("iv_crush", ""),
        "carry": ctx.get("carry", ""),
        "fwd_method": ctx.get("fwd_method", ""),
        "catalyst_iso": ctx.get("catalyst_iso", ""),
        "catalyst_type": ctx.get("catalyst_type", ""),
        "peak_pct": "", "trough_pct": "",
        "notes": notes,
    })
    if not _acquire():
        raise RuntimeError("journal is locked by another process")
    try:
        rows = load_rows()                       # re-read inside the lock
        entry["journal_id"] = _next_id(rows)
        rows.append(entry)
        save_rows(rows)
    finally:
        _release()
    return entry["journal_id"]


# ─────────────────────────────────────────────
# MARKS
# ─────────────────────────────────────────────

def _chain_quote(ticker: str, expiry: str, strike: float, right: str = "C"):
    """-> {bid, ask, last, iv, stale} for one contract, or None.

    Marks at the MID but also returns the bid, because those are different
    numbers and only one of them is what you would actually receive. After
    hours yfinance clears bid/ask to zero; then the last trade is used and the
    row is flagged stale rather than silently shown as a live quote."""
    try:
        import yfinance as yf
        ch = yf.Ticker(ticker).option_chain(expiry[:10])
        df = ch.calls if right.upper().startswith("C") else ch.puts
        m = df[abs(df["strike"] - float(strike)) < 1e-6]
        if m.empty:
            return None
        r = m.iloc[0]
        bid, ask = _f(r.get("bid"), 0.0), _f(r.get("ask"), 0.0)
        last = _f(r.get("lastPrice"), 0.0)
        iv = _f(r.get("impliedVolatility"), 0.0)
        if bid > 0 and ask > 0:
            return {"bid": bid, "ask": ask, "mark": (bid + ask) / 2.0,
                    "last": last, "iv": iv, "stale": False}
        if last > 0:
            return {"bid": bid, "ask": ask, "mark": last, "last": last,
                    "iv": iv, "stale": True}
        return None
    except Exception:
        return None


def update_marks(verbose: bool = True) -> dict:
    """Refresh every OPEN position. One chain fetch per (ticker, expiry), so
    several positions on the same expiry cost one call."""
    rows = load_rows()
    open_rows = [r for r in rows if (r.get("status") or "") == "OPEN"]
    if not open_rows:
        if verbose:
            print("  no open positions")
        return {"updated": 0, "failed": 0}
    cache, updated, failed = {}, 0, 0
    for r in open_rows:
        key = (r["ticker"], r["expiry"], float(r["strike"]),
               r.get("right", "C"))
        if key not in cache:
            cache[key] = _chain_quote(*key)
        q = cache[key]
        if not q:
            failed += 1
            continue
        n = int(_f(r["contracts"], 0) or 0)
        cost = _f(r["cost_basis"], 0.0) or 0.0
        value = q["mark"] * CONTRACT_MULT * n
        pnl = value - cost
        pct = (pnl / cost) if cost else 0.0
        r["last_mark"] = round(q["mark"], 4)
        r["last_bid"] = round(q["bid"], 4)
        r["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        r["mark_stale"] = "1" if q["stale"] else ""
        r["unreal_pnl"] = round(pnl, 2)
        r["unreal_pct"] = round(pct, 4)
        peak = _f(r.get("peak_pct"))
        trough = _f(r.get("trough_pct"))
        r["peak_pct"] = round(max(pct, peak if peak is not None else pct), 4)
        r["trough_pct"] = round(min(pct, trough if trough is not None else pct), 4)
        updated += 1
    if not _acquire():
        raise RuntimeError("journal is locked by another process")
    try:
        save_rows(rows)
    finally:
        _release()
    if verbose:
        print(f"  marks updated: {updated}, unavailable: {failed}")
    return {"updated": updated, "failed": failed}


# ─────────────────────────────────────────────
# CLOSING
# ─────────────────────────────────────────────

def close_position(journal_id: str, exit_price: float, reason: str = "",
                   fees: float = None, auto: bool = False) -> bool:
    rows = load_rows()
    hit = next((r for r in rows if r.get("journal_id") == journal_id), None)
    if hit is None or hit.get("status") == "CLOSED":
        return False
    n = int(_f(hit["contracts"], 0) or 0)
    if fees is None:
        fees = DEFAULT_FEE_PER_CONTRACT * n
    proceeds = float(exit_price) * CONTRACT_MULT * n - fees
    cost = _f(hit["cost_basis"], 0.0) or 0.0
    try:
        d0 = datetime.strptime(hit["opened_at"][:10], "%Y-%m-%d").date()
        held = (date.today() - d0).days
    except (ValueError, TypeError):
        held = ""
    hit.update({
        "status": "CLOSED",
        "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "exit_price": round(float(exit_price), 4),
        "exit_reason": reason,
        "proceeds": round(proceeds, 2),
        "realized_pnl": round(proceeds - cost, 2),
        "realized_pct": round((proceeds - cost) / cost, 4) if cost else "",
        "days_held": held,
        "auto_closed": "1" if auto else "",
    })
    if not _acquire():
        raise RuntimeError("journal is locked by another process")
    try:
        save_rows(rows)
    finally:
        _release()
    return True


def auto_close_expired(verbose: bool = True) -> int:
    """Settle anything past expiry at intrinsic value, flagged auto_closed so
    it is never confused with a real recorded exit. Without this, forgotten
    positions sit OPEN forever and quietly poison every aggregate."""
    rows = load_rows()
    today = date.today()
    todo = []
    for r in rows:
        if (r.get("status") or "") != "OPEN":
            continue
        try:
            exp = datetime.strptime(r["expiry"][:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if exp < today:
            todo.append((r, exp))
    n = 0
    for r, exp in todo:
        px = _last_close(r["ticker"], exp)
        if px is None:
            continue
        intrinsic = max(px - float(r["strike"]), 0.0)
        # An expiring-worthless option is abandoned, not sold: no exit fee.
        close_position(r["journal_id"], intrinsic,
                       reason=f"expired {exp:%Y-%m-%d} (auto, intrinsic on "
                              f"${px:,.2f} close)",
                       fees=0.0 if intrinsic <= 0 else None, auto=True)
        n += 1
    if verbose and n:
        print(f"  auto-closed {n} expired position(s) at intrinsic")
    return n


def _last_close(ticker: str, on: date):
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="3mo")
        if h is None or h.empty:
            return None
        for idx, row in h.iterrows():
            if idx.strftime("%Y-%m-%d") >= on.isoformat():
                return float(row["Close"])
        return float(h["Close"].iloc[-1])
    except Exception:
        return None


# ─────────────────────────────────────────────
# REPORT — predicted vs realized
# ─────────────────────────────────────────────

MIN_CLOSED_FOR_EDGE = 20     # below this, aggregates are anecdotes


def report(verbose: bool = True) -> dict:
    rows = load_rows()
    closed = [r for r in rows if r.get("status") == "CLOSED"
              and _f(r.get("realized_pct")) is not None]
    open_ = [r for r in rows if r.get("status") == "OPEN"]
    out = {"n_closed": len(closed), "n_open": len(open_)}
    if verbose:
        print("=" * 64)
        print(f"  OPTIONS JOURNAL — {len(open_)} open, {len(closed)} closed")
        print("=" * 64)
    if open_:
        tot_cost = sum(_f(r.get("cost_basis"), 0.0) or 0.0 for r in open_)
        tot_pnl = sum(_f(r.get("unreal_pnl"), 0.0) or 0.0 for r in open_)
        out["open_cost"], out["open_unreal"] = tot_cost, tot_pnl
        if verbose:
            print(f"  open at risk ${tot_cost:,.2f}   "
                  f"unrealized ${tot_pnl:+,.2f} "
                  f"({tot_pnl / tot_cost:+.1%})" if tot_cost else "")
    if not closed:
        if verbose:
            print("\n  Nothing closed yet — no realized results to grade.")
            print("  Predictions are recorded and waiting.")
        return out

    realized = [_f(r["realized_pct"]) for r in closed]
    wins = [x for x in realized if x > 0]
    out["hit_rate"] = len(wins) / len(realized)
    out["mean_realized"] = sum(realized) / len(realized)
    preds = [(_f(r.get("pred_sc_ev_exit")), _f(r.get("realized_pct")))
             for r in closed]
    preds = [(p, a) for p, a in preds if p is not None and a is not None]
    out["n_with_pred"] = len(preds)
    if verbose:
        print(f"\n  realized: hit rate {out['hit_rate']:.0%}, "
              f"mean {out['mean_realized']:+.1%} per trade "
              f"(n={len(realized)})")
    if preds:
        mp = sum(p for p, _ in preds) / len(preds)
        ma = sum(a for _, a in preds) / len(preds)
        out["mean_predicted"], out["mean_actual"] = mp, ma
        if verbose:
            print(f"  predicted mean EV {mp:+.1%}  vs  realized {ma:+.1%}"
                  f"   gap {ma - mp:+.1%}  (n={len(preds)})")
    # per model_version — the comparison the version stamp exists for
    byv = {}
    for r in closed:
        byv.setdefault(r.get("model_version", "?"), []).append(
            _f(r["realized_pct"]))
    if len(byv) > 1 and verbose:
        print("\n  by model version:")
        for v, xs in sorted(byv.items()):
            print(f"    {v:<18} n={len(xs):>3}  mean {sum(xs)/len(xs):+.1%}  "
                  f"hit {sum(1 for x in xs if x > 0)/len(xs):.0%}")
    out["by_version"] = {v: {"n": len(xs), "mean": sum(xs) / len(xs)}
                         for v, xs in byv.items()}
    if len(closed) < MIN_CLOSED_FOR_EDGE and verbose:
        print(f"\n  NOTE: {len(closed)} closed trades is not evidence of edge. "
              f"At least {MIN_CLOSED_FOR_EDGE} before these aggregates mean\n"
              f"  anything, and options returns are skewed enough that even "
              f"that is thin.")
    if verbose:
        print("=" * 64)
    return out


def print_open():
    rows = [r for r in load_rows() if r.get("status") == "OPEN"]
    if not rows:
        print("  no open positions")
        return
    print(f"  {'id':<16}{'contract':<22}{'n':>3}{'cost':>10}{'mark':>8}"
          f"{'bid':>8}{'P/L':>11}{'%':>8}  flags")
    for r in rows:
        mark = _f(r.get("last_mark"))
        bid = _f(r.get("last_bid"))
        pnl = _f(r.get("unreal_pnl"))
        pct = _f(r.get("unreal_pct"))
        name = f"{r['ticker']} {r['expiry'][5:]} ${_f(r['strike']):g}{r['right']}"
        flags = "STALE" if r.get("mark_stale") else ""
        try:
            dte = (datetime.strptime(r["expiry"][:10], "%Y-%m-%d").date()
                   - date.today()).days
            flags += (" " if flags else "") + f"{dte}d"
        except (ValueError, TypeError):
            pass
        print(f"  {r['journal_id']:<16}{name:<22}{r['contracts']:>3}"
              f"{_f(r['cost_basis'], 0):>10,.2f}"
              f"{(f'{mark:.2f}' if mark is not None else '—'):>8}"
              f"{(f'{bid:.2f}' if bid is not None else '—'):>8}"
              f"{(f'{pnl:+,.2f}' if pnl is not None else '—'):>11}"
              f"{(f'{pct:+.1%}' if pct is not None else '—'):>8}  {flags}")


# ─────────────────────────────────────────────
# FIDELITY IMPORT
# ─────────────────────────────────────────────

def import_fidelity(path: str, verbose: bool = True) -> int:
    """Import option trades from a Fidelity activity CSV export.

    Fidelity's export format is not a stable contract and has changed before,
    so this parses defensively: it looks for the columns it needs by fuzzy
    header match, skips anything it cannot confidently read, and reports what
    it skipped instead of guessing. Rows already present (same symbol, date
    and quantity) are not duplicated."""
    if not os.path.exists(path):
        print(f"  no such file: {path}")
        return 0
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    # find the header line — Fidelity prefixes exports with banner rows
    lines = text.splitlines()
    start = 0
    for i, ln in enumerate(lines[:40]):
        low = ln.lower()
        if "symbol" in low and ("quantity" in low or "amount" in low):
            start = i
            break
    reader = csv.DictReader(lines[start:])
    existing = {(r["occ_symbol"], r["opened_at"][:10], r["contracts"])
                for r in load_rows()}

    def _col(row, *names):
        for k in row:
            if k and any(n in k.strip().lower() for n in names):
                return row[k]
        return ""

    added = skipped = 0
    for row in reader:
        sym = (_col(row, "symbol") or "").strip()
        # OCC-style option symbols carry a 6-digit date + C/P + 8-digit strike
        if not sym or len(sym.replace("-", "").replace(" ", "")) < 15:
            skipped += 1
            continue
        raw = sym.replace("-", "").replace(" ", "").upper().lstrip("+")
        try:
            body = raw[-15:]
            root = raw[:-15]
            yy, mm, dd = body[0:2], body[2:4], body[4:6]
            right = body[6]
            strike = int(body[7:15]) / 1000.0
            expiry = f"20{yy}-{mm}-{dd}"
            qty = abs(int(float(_col(row, "quantity") or 0)))
            price = abs(_f(_col(row, "price"), None) or 0.0)
            when = (_col(row, "run date", "date") or "").strip()[:10]
        except (ValueError, IndexError):
            skipped += 1
            continue
        if qty <= 0 or price <= 0 or right not in ("C", "P"):
            skipped += 1
            continue
        occ = f"{root}{body}"
        if (occ, when, str(qty)) in existing:
            continue
        jid = add_entry(root, expiry, strike, qty, price, right=right,
                        notes=f"imported from Fidelity export {os.path.basename(path)}")
        # preserve the real trade date rather than the import date
        rows = load_rows()
        for r in rows:
            if r["journal_id"] == jid and when:
                r["opened_at"] = when + " 00:00"
                r["model_version"] = "imported (no model prediction)"
        save_rows(rows)
        added += 1
    if verbose:
        print(f"  imported {added} option trade(s); skipped {skipped} "
              f"non-option or unreadable row(s)")
        if added:
            print("  NOTE: imported rows carry no model prediction — they "
                  "grade P/L only, not forecast accuracy.")
    return added


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    cmd = (sys.argv[1].lower() if len(sys.argv) > 1 else "list")
    if cmd == "update":
        auto_close_expired()
        update_marks()
        print_open()
    elif cmd == "close":
        if len(sys.argv) < 4:
            print("usage: python options_journal.py close JOURNAL_ID EXIT_PRICE "
                  '["reason"]')
            sys.exit(1)
        reason = sys.argv[4] if len(sys.argv) > 4 else "manual close"
        ok = close_position(sys.argv[2], float(sys.argv[3]), reason)
        print("  closed" if ok else "  no such open position")
    elif cmd == "report":
        report()
    elif cmd in ("import-fidelity", "import"):
        if len(sys.argv) < 3:
            print("usage: python options_journal.py import-fidelity FILE.csv")
            sys.exit(1)
        import_fidelity(sys.argv[2])
    else:
        print_open()
