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
- **House Clerk / Senate eFD** — congressional periodic transaction reports,
  read by the copy trading tool. No key. The House side needs `pypdf`.
- **SEC EDGAR** — Form 4 insider transactions and 13F fund holdings, same tool.
- **Senate LDA / USAspending** — lobbying spend and federal contract awards,
  used as context on a ticker rather than as a screen.

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
| `political_feeds.py` | Disclosure fetchers and parsers — House PTR, Senate eFD, Form 4, 13F, lobbying, contracts |
| `political_engine.py` | Copy trading store, incremental scan, forward grading, permutation tests, publish |
| `political_clusters.py` | Cluster detection, selectivity weighting, the visible component score |
| `political_layout.py` | The radial layout — rings by role, wedges by sector |
| `political_graph.py` | The cluster canvas: sprites, rendering, interaction |
| `political_views.py` | The four views: Clusters, Ticker, Filer, Movers |
| `political_cards.py` | Floating deep-dive cards for a filer or a company |
| `political_gui.py` | The Copy Trading tab — shell, scanner controls, the record |
| `political_hypotheses.py` | Pre-registered predictions about disclosure data |

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
linear, can say "this is 6× sales". References are ordered by value rather
than by name, because a company the market has always distrusted trades under
15x for decades and calling normal the ceiling would paint that stock
backwards.

The `Reference` control decides what the corridor is measured against:

- **Outside** — the stock's own *normal* multiple (median monthly over the
  window) against a yardstick from outside the company: 15x, or the growth
  rate capped at 30x for faster compounders, or the 10-year Treasury on a
  dividend chart. This is the only setting that can call a whole stock
  expensive, which is what catches a company whose own history was a bubble.
- **Own history** — the stock's own 25th and 75th percentile, with the median
  dashed between them. Every zone is one the price has actually reached,
  because the stock built them.
- **Auto** (the default) — the outside yardstick when the stock has traded
  anywhere near it, its own percentiles when it has not. Fifteen times *sales*
  is not a standard anyone uses: on Papa John's, whose revenue multiple has
  lived between 0.7x and 1.9x for fifteen years, a corridor drawn to 15x
  painted 92% of the panel one amber block the price had no way of reaching.
  The rejected yardstick is still reported in the panel, with the distance
  from today's price.

When the corridor comes from the stock's own record the meter drops its
benchmark component rather than scoring it: distance from your own 75th
percentile is the history rank again, and counting it twice would double the
weight of one fact.

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
distance from the normal multiple, its distance from the outside benchmark
(where one is in use), and the
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

**Scenarios** are written out in full beside the meter — bear, base and bull
at the end of the forecast. They are *not* drawn on the chart unless the
`scenario cone` toggle is on: fifteen years of history should not have to
share the panel with a fan of guesses about the next five. Both axes
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

## Copy trading

A disclosure feed for notable figures — members of Congress and their
spouses, corporate insiders, and a list of named fund managers — with the
forward record those disclosures have actually earned.

    python political_engine.py backfill 30     # first run, one month back
    python political_engine.py scan            # one incremental pass
    python political_engine.py grade           # fill forward returns
    python political_engine.py report
    python political_engine.py leaderboard filer_name 20
    python political_engine.py publish         # portable snapshot to share

The Run button in the GUI starts a background scanner that stays up until it
is stopped. It is paced to the filings rather than to the button: a
congressional disclosure is 30 to 45 days old before it is public, so the
House index is polled every thirty minutes as a 60KB conditional request
that usually transfers nothing, and the expensive PDF fetch happens only for
a document that has not been seen. Between polls the loop is one thread
waiting on an event.

**Coverage is measured, not assumed.** Across all 395 House PTRs filed in
2026, 349 parse — 88.4%. Every one of the remaining 46 is a paper filing
scanned without a text layer, and they belong to ten named members
(Harshbarger, Khanna, Rogers, McCaul, Fleischmann, Wied, Cole, Malliotakis,
Mann, Self). Those members are invisible to this tool until someone adds
OCR. Electronic filings carry a DocID of the form `2XXXXXXX` and paper ones
a seven-digit ID; the split is checked before fetching, so a scan does not
download 46 documents it cannot read.

### What the four views answer

    Clusters   which names several INDEPENDENT filers bought at once,
               drawn as the bipartite filer-to-ticker network it is
    Ticker     one stock's whole political and insider footprint
    Filer      one person or fund's record, with n and no rank
    Movers     most bought and most sold, by distinct filers

The Clusters network is not decoration over a table. Two tickers sit near
each other exactly when the same people bought both, and no sorted list can
show that. Physics is vectorised numpy, items are created once and only
their coordinates move per frame; it runs at about 165 fps on 125 nodes.

**A filer is weighted by how selective they are.** Bridgewater held 441
different names in a 90-day window; an insider bought exactly one. Counting
those the same is what makes a cluster of index-like funds look like
agreement, so each filer is shrunk by 1/sqrt(names they bought). SBLK's nine
buyers are worth 8.0 effective because eight of them bought one thing each;
PODD's three are worth 1.2 because most of them buy everything. Both numbers
are shown side by side, and the per-filer breadth is in the panel.

Nothing in the score is fitted. The weights are judgement, printed beside
each part, and 1/sqrt is standard shrinkage rather than anything tuned — a
steeper curve chosen to lift a favourite name would be the exact failure
this repo already documents.

### Tracked managers, and the industry cut

Seventeen 13F filers are followed by CIK — value and special situations
(Berkshire, Baupost, Oaktree, Scion), concentrated activists (Pershing
Square, Third Point, TCI, Elliott, ValueAct), the Tiger lineage (Tiger
Global, Coatue, Lone Pine), and macro or event-driven books (Bridgewater,
Appaloosa, Duquesne).

**Every CIK was resolved against EDGAR's actual 13F-HR history rather than
guessed from the name**, because a wrong one fails silently — it returns a
real fund's real holdings under someone else's label and nothing looks
broken. That already happened here: `0001631664` was carried as "Duquesne
Family Office" and is in fact Punch Card Management L.P. Druckenmiller's
office is `0001536411`. Anything attributed to Duquesne before 2026-09-21
was another manager's book wearing his name.

Seventeen managers is more names than a network view can hold, so the
Clusters and Movers boards take an **industry** filter — tech, medical,
industrials, energy, materials, real estate, financials, consumer, or all.
Sectors come from yfinance, are fetched once per ticker and stored, and the
filter is applied to the rows BEFORE anything is counted, so a cluster's
filer count always means "people who bought this name" rather than a total
quietly trimmed afterwards.

    python political_engine.py sectors        # classify anything new
    python political_engine.py sectors list   # what is in each bucket

### The layout means something

Position is fixed by what a node **is**, not by how the springs settled:

    ring      centre to edge
    ────────────────────────────────────────────
    0         the Mag 7
    1         institutions (13F filers)
    2         every other stock, banded into sector wedges
    3         people — members of Congress, corporate insiders

    angle     which sector

This replaced a free force-directed layout, and the reason matters. Force
layouts put the highest-degree node in the middle; on this data that is
Bridgewater, which holds a thousand names. The picture was placing one asset
manager at the centre of the market and drawing everything orbiting it — a
fact about the algorithm that a reader has no way to distinguish from a fact
about capital.

Sector wedges are sized in proportion to how many names each holds. Equal
wedges would give Real Estate's one name the same arc as Technology's
thirty-five, so the dense sectors would overlap while the thin ones sat
empty. Communication Services folds into tech and Utilities into energy,
because on this store they are a handful of names that behave that way.

**Size means size.** A company orb is scaled by market cap, log-scaled from
$1bn to $5tn, and an institution by the 13F book it runs. Both were
previously driven by the disclosed amount — how much somebody happened to
trade — so a small name one fund bought heavily drew larger than Apple, and
every institution came out identical because their books all saturated the
same scale. Agreement still adds a little so a widely-bought small cap does
not vanish, but it no longer sets the size.

**Isolating is not a crop.** A stock sits at r~850 and the people who traded
it at r~1300, spread around their wedge — framing that bounding box gives a
view three screens wide with the connections trailing off the edges. So an
isolated view re-draws the neighbourhood as a compact rosette: the selected
node at the centre, its neighbours ringed closely around it, nothing off
screen and no edge longer than it needs to be. The real positions are
untouched; it is a draw-time substitution.

Forces still run, but constrained to the angular axis: a node may slide
around its ring to stop overlapping a neighbour and may never change radius.
The structure is hard-coded; the physics only tidies it. `free force` in the
rail restores the old behaviour, which is still better for spotting an odd
neighbourhood even though its geometry means nothing.

### What it costs to run

The graph is drawn as a bitmap, so the expensive thing is not the physics —
it is the buffers. At 2x supersample on a 2560-wide display the render
target is 3700x1952, and one RGBA copy of that is 29MB. The first version
made five of them per frame, committed and released each time, which is what
made the machine work rather than the total footprint.

Measured, whole app, seven tabs, 300-node graph, 200 logos cached:

    startup                      108 MB
    after heavy use              145 MB
    drag / hover                  55-60 ms
    quality frame                121 ms      (327 ms isolated, at 2x)

Five things keep it there, each of which was worth more than tuning:

- **The layer composites in place.** `img.paste(layer, (0,0), layer)`
  instead of `alpha_composite(img.convert("RGBA"), layer).convert("RGB")`,
  which was allocating three extra full-canvas buffers per frame.
- **Supersampling is adaptive.** 2x only when 90 or fewer nodes are on
  screen — worth it to finish an isolated neighbourhood, not worth it to
  smooth three hundred dots nobody reads individually.
- **Caches are bounded by BYTES.** An entry cap is meaningless when a 4px
  sprite and a 290px glow both count as one; the sprite cache "limited" to
  4,000 entries was holding 32MB.
- **Sprites are capped and scaled at draw.** A soft radial glow does not
  need to be cached at 290px.
- **Logo sources are downscaled on load** to 128px. Keeping the 250px
  originals would have cost 330MB across a full ticker universe for detail
  nothing ever draws.
- **Hover draws on the cheap tier.** Sweeping the mouse used to fire a full
  repaint per node passed under the cursor.

A `lite (low memory)` toggle in the rail strips glow, bloom, supersampling
and logos: 115MB and a 77ms quality frame. Everything the picture *means* —
positions, colours, sizes, edge weights — survives it. Only the finish goes.

Three things the output always carries, and the reason each one is there:

- **n beside every rate.** A member with four trades has no record.
- **A permutation p-value on any ranking.** With 535 members, somebody
  finishes first every time. The test pools the outcomes, reshuffles them
  across the same group sizes, and reports how often pure noise produces a
  leader as good as the observed one. This is the same correction
  `hypotheses.py` applies, and it exists for the same reason.
- **Amounts as ranges.** Filings disclose `$15,001 - $50,000`, and the top
  bucket is open-ended. There is no `amount_mid` column, because a midpoint
  is an invention and anything that wants one should have to write it and
  own the choice.

Grading runs from two anchors and reports both. `trade_date` is what the
filer got; `notify_date` is the day it became public and the only one anyone
else could have acted on. The gap between them is the number this tool
exists to show. A window that has not matured is left empty rather than
graded against today, and a ticker whose tape has gapped is refused rather
than guessed — `review_outcomes._price_on_or_after` already learned that
lesson on CRKN, SBNY and MDRX.

**There are three dates on a House filing and only one of them is public.**
The form carries a "Notification Date" column, and it is not it — that is
when the *filer* was told, which for a managed account or a trust is weeks
before they filed. The public date is the filing date. Reading the wrong
column understated the disclosure lag by 13.4 days on average across the
329 House rows measured: Pelosi's 2026-07-24 purchases carry a Notification
Date of 2026-07-24 and were filed on 2026-08-21. `filer_notified` keeps the
form's column for what it actually is; `notify_date` is the filing date on
every source. Measured public delay across 423 congressional rows: **37.6
days**.

**Legal.** Congressional disclosure reports carry a statutory use
restriction — Ethics in Government Act Title 1, quoted on the Senate eFD
click-through as `5 U.S.C. app. § 105(c)`. It prohibits use "for any
commercial purpose, other than by news and communications media for
dissemination to the general public" and use in the solicitation of money.
This tool is personal and free and sits outside that. The restriction covers
the congressional reports only; SEC, LDA and USAspending data carry no
equivalent limit. See `COPY_TRADING_PLAN.md`.

**Sharing.** `publish` writes `political_snapshot.json.gz` — the same rows
without the audit text, readable with nothing but the standard library and
small enough to commit. A second copy of the tool loads that instead of
scraping, which keeps the request load on the House and Senate at one
machine's worth however many people are reading.

## Runtime data

The scanner writes logs, caches, and snapshots into the working directory.
None of it is committed — see `.gitignore`, which is an allowlist for exactly
that reason.
