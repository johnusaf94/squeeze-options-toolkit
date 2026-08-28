"""
trading_config.py
=================
Your capital and risk limits, in one place, so the tool can answer in
CONTRACTS and DOLLARS instead of percentages.

WHY THIS MATTERS MORE THAN IT LOOKS
-----------------------------------
Without a capital figure the structure ranker was answering a question nobody
asked. On a real AAPL chain it crowned a deep-ITM call at an $85.01 debit —
$8,501 for one contract. Against a small account that is not a recommendation,
it is a rounding error in the wrong direction. Every structure it ranked above
the affordable ones was noise to the only reader who mattered.

With capital declared, the ranker can do the thing that actually helps: throw
away everything you cannot buy, and rank what is left.

SIZING
------
Kelly is computed on the scenario distribution, then quartered. Quarter-Kelly
is the conventional haircut for model error, and this model has plenty: no
closed trades, a calibration that fails its own gate, and a path shape resting
on eight episodes per bin. The result is then capped by your own limits, and
the cap is expected to bind most of the time — it should.

Edit the values below, or trading_config.json if you prefer to keep them out
of source.
"""

import json
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_DIR, "trading_config.json")

DEFAULTS = {
    # Placeholder. Set your own in trading_config.json (gitignored) — the
    # sizing below is meaningless until this matches a real account.
    "capital": 10000.0,         # total account equity
    "typical_position_pct": 0.10,   # a normal position, as a share of capital
    "max_position_pct": 0.25,       # absolute ceiling at full conviction
    "kelly_fraction": 0.25,         # quarter-Kelly
    "min_contracts": 1,             # below one contract there is no trade
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg: dict) -> None:
    keep = {k: cfg[k] for k in DEFAULTS if k in cfg}
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(keep, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, CONFIG_FILE)


def size_position(debit_per_share: float, kelly=None, cfg: dict = None) -> dict:
    """Turn a per-share debit and a Kelly number into an actual order.

    Returns contracts, dollars, share of capital, and WHICH limit bound —
    naming the binding constraint matters, because "Kelly wanted more but your
    ceiling said no" and "there was no edge" are different situations that
    look identical if you only print a number."""
    cfg = cfg or load()
    cap = float(cfg["capital"])
    per_contract = max(float(debit_per_share), 0.0) * 100.0
    out = {"per_contract": per_contract, "capital": cap,
           "contracts": 0, "dollars": 0.0, "pct": 0.0,
           "bound_by": "", "affordable": False}
    if per_contract <= 0:
        out["bound_by"] = "no price"
        return out

    k = kelly if kelly is not None else 0.0
    ceiling = cap * float(cfg["max_position_pct"])
    if k <= 0:
        out["bound_by"] = "no edge — model sees none"
        out["affordable"] = per_contract <= ceiling
        return out

    want = cap * k * float(cfg["kelly_fraction"])
    budget = min(want, ceiling)
    n = int(budget // per_contract)
    out["affordable"] = per_contract <= ceiling
    if n < int(cfg["min_contracts"]):
        out["bound_by"] = ("one contract exceeds your ceiling"
                           if per_contract > ceiling
                           else "sizing rounds below one contract")
        # what it would take
        out["need_capital_for_one"] = per_contract
        return out
    out["contracts"] = n
    out["dollars"] = n * per_contract
    out["pct"] = out["dollars"] / cap if cap else 0.0
    out["bound_by"] = ("your %.0f%% ceiling" % (cfg["max_position_pct"] * 100)
                       if budget == ceiling else
                       "quarter-Kelly (%.0f%% of capital)" % (k * 25))
    return out
