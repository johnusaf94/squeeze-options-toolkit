# Squeeze Options Toolkit

Research tooling for short-squeeze candidate screening: a two-stage scanner
(bulk screen, then deep analysis on finalists), an options pricing and
structure ranker, an outcome grader, and a learning layer that refits
parameters against graded history.

**This is research tooling, not investment advice, and not a validated
model.** Several of its measured claims concern the underlying stock rather
than a realised option position, and its probability calibration does not
currently pass its own activation gate. Read the disclosed limits in the
module docstrings before trusting a number.

## Data sources

All free, no API key required for the squeeze path:

- **yfinance** — prices, fundamentals, ownership, options chains
- **SEC** — fails-to-deliver (bi-monthly settlement files)
- **FINRA / NASDAQ** — official short interest, contemporaneous average daily
  volume, and exchange-computed days to cover
- **FINRA** — daily short-volume ratio (short-interest freshness nowcast)

`investor_roundtable_gui.py` optionally uses Groq and Together; both read
`GROQ_API_KEY` / `TOGETHER_API_KEY` from the environment. No keys are stored
in this repository.

## Layout

| Module | Role |
|---|---|
| `squeeze_analyzers.py` | Stage-1 screen (Gill / Chamath pillars) |
| `squeeze_deep.py` | Stage-2: convexity, GEX, CTB velocity, FTD, conviction |
| `data_validator.py` | Single source of truth — every field validated and scaled |
| `dtc_engine.py` | Days-to-cover by named volume window; settlement-cadence trends |
| `effective_float.py` | Tradeable float after institutional stock is netted out |
| `nasdaq_short_interest.py` | Official settlement series (FINRA first, NASDAQ fallback) |
| `options_ev.py` / `options_structures.py` | Pricing and structure ranking |
| `learning_engine.py` | Parameter refits and probability calibration, gated on sample size |
| `review_outcomes.py` / `nightly_grade.py` | Outcome grading |
| `squeeze_logger.py` | Immutable scan log — write-only, never read by the scanner |

## Two conventions worth knowing before changing anything

**Run the golden check around any pricing-math change.** It replays stored
option chains and diffs every field:

```bash
python options_golden.py check
```

The baselines live in `golden/`, which is not committed here — regenerate them
before relying on the check.

**Scoring changes are versioned, not swapped.** `scoring_config.json` carries
the knobs, every logged row is stamped with `scoring_version`, and the
previous scorer's output is logged alongside the current one (`*_v1` columns).
That makes "is the new version better" an A/B on identical rows rather than a
comparison between old rows and new rows, where the market also moved.

## Runtime data

The scanner writes logs, caches, and snapshots into the working directory.
None of it is committed — see `.gitignore`, which is an allowlist for exactly
that reason.
