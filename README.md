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
- **SEC XBRL company facts** — filed annual earnings, cash flow, revenue,
  dividends and share counts, used by the value analysis tool. SEC asks for a
  contact in the User-Agent; set `SEC_USER_AGENT` to your own address.

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
| `value_engine.py` | Earnings-overlay valuation: filed EPS history, split-normalised, against price |
| `value_analysis_gui.py` | The Value Analysis tab — chart, multiples, total-return scenarios |

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

## Value analysis

`value_analysis_gui.py` plots price over three coloured bands, each bounded by
a multiple of the company's own earnings — green under both references, amber
between them, red over both. The references are the *normal* multiple (what
this stock actually traded at over the window, median monthly) and the
*benchmark* (15x, or the growth rate capped at 30x for faster compounders).
They are ordered by value rather than by name, because a company the market has
always distrusted trades under 15x for decades and calling normal the ceiling
would paint that stock backwards.

Because the bands are multiples of earnings, they move as earnings move. That
is the whole point: a band rising under a flat price means the company grew
into its valuation, while a flat band under a rising price means the market
repriced it. Neither line is a forecast.

Three seams in the data are handled explicitly rather than smoothed over,
because each of them silently corrupts this chart if it is not:

- **Split basis.** XBRL stores EPS as filed, so a company that split later
  leaves a pre-split figure in the record. Apple's FY2011 EPS is filed as
  27.68 and FY2012 as 6.31 — a 78% "decline" that never happened. Every
  per-share value is divided by the splits that came after the filing that
  reported it.
- **Dividend adjustment.** Price is Yahoo's split-adjusted close, not the
  dividend-adjusted one, which would depress every historical multiple.
- **GAAP versus adjusted.** Actuals are GAAP; consensus is adjusted. The
  forecast is drawn from consensus *growth rates* applied to the last GAAP
  actual, and the level gap between the two bases is reported.

The math has no GUI imports and can be checked from a terminal:

```bash
python value_engine.py MSFT eps 15
```

## Runtime data

The scanner writes logs, caches, and snapshots into the working directory.
None of it is committed — see `.gitignore`, which is an allowlist for exactly
that reason.
