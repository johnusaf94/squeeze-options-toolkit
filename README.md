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

`value_analysis_gui.py` plots price over coloured bands whose edges are all
multiples of the company's own earnings — amber between two reference
multiples, green below them and red above, each step deepening in colour the
further it sits from that corridor. Thin lines at round multiples (2×, 4×, 8×
…) are marked on the right edge as a ruler, because no price axis, log or
linear, can say "this is 6× sales". The references are the *normal* multiple (what
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

A value meter scores where today's price sits, 0 (expensive) to 100 (cheap),
from four visible parts: the current multiple's rank in its own history, its
distance from the normal multiple, its distance from the benchmark, and the
return consensus growth alone would deliver at today's multiple. The weights
are judgement, printed beside each part, and nothing in the meter has been
tested against what prices did next. A confidence reading — docked for erratic
earnings, loss years, a short filing history, a re-rating, a dividend cut, or
an impossible break in the series — pulls the score toward 50, so a chart that
cannot be trusted cannot produce a loud number.

Consensus is matched to the metric: revenue per share grows at the revenue
consensus, EPS at the earnings consensus. Cash flow and dividends have no
published consensus, so their forecast extends the metric's own historical
rate, is labelled an extrapolation rather than a forecast, and is left out of
the meter.

**Dividends are shown as yield**, because 22× the dividend and a 4.6% yield
are the same number and only one of them is how anyone quotes it. The grid,
the reference lines and the lower panel all speak in percent, and the outside
reference is the **10-year Treasury** rather than 15× earnings — above that
line the dividend beats government debt, below it you are paid less than cash
for holding equity. Today's yield is priced against the last twelve months of
actual payments, so a cut shows up immediately instead of waiting a year for
the next annual report.

**Scenarios** put bear, base and bull at the end of the forecast. Both axes
are measured rather than assumed: the exit multiple is the stock's own 25th,
50th and 75th percentile, and the earnings are the low, average and high of
published consensus applied as a ratio to the base path. The odds are the
percentile definition, not a forecast. The scenarios are clamped to bracket
today's multiple — an unclamped "bear" case had PayPal doubling, because even
the 25th percentile of its history is twice where it trades now — and each row
reports the re-rating it assumes, alongside the return growth alone would give.

**Dividend cover** is a toggle: earnings cover and free cash flow cover, drawn
on the lower panel against a line at 1.0×. Cash cover is the one that decides,
because a dividend is paid out of cash and not out of accounting profit — in
2025 Coca-Cola earned 1.49× its dividend and generated 0.60× of it.

The chart takes drag to pan, scroll to zoom and double-click to reset, and the
`Fit` control decides whether the vertical scale has to contain the forecast.
It does not by default on a linear axis, where a fast grower's fan flattens a
decade of price into an unreadable strip.

The math has no GUI imports and can be checked from a terminal:

```bash
python value_engine.py MSFT eps 15
```

## Runtime data

The scanner writes logs, caches, and snapshots into the working directory.
None of it is committed — see `.gitignore`, which is an allowlist for exactly
that reason.
