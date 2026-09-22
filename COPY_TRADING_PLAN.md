# Copy Trading — build plan

A standalone dashboard tool that watches public disclosure filings for trades
by notable figures, grades what those trades actually did next, and publishes
a portable snapshot other people can read without scraping anything
themselves.

**Status: built 2026-09-20. All seven phases shipped.** What the build found
that the plan did not anticipate is recorded at the bottom, under *What
changed*.

## What this is not

It is not a backtest leaderboard. Quiver Quantitative publishes 31 strategies
ranked by total return, best at 849%, with no correction for the fact that 31
strategies were searched. `hypotheses.py` in this repo exists because that
exact failure mode was found here first: 35 buckets screened, best hit 26.2%
against a 16.4% base, and a permutation test reached 26.2% in 32.3% of random
trials.

With 535 members of Congress the problem is worse, not better. Someone in that
population will show a spectacular record by chance alone. A tool that ranks
them and shows the top of the list is a tool that finds noise every single
time it is run.

So every ranking this tool produces carries its sample size and a permutation
p-value, and the pre-registration discipline in `hypotheses.py` applies: a
predicate about political trades is registered before the forward data exists,
or it is not evidence.

## Legal position

Congressional financial disclosure reports are governed by Title 1 of the
Ethics in Government Act, quoted on the Senate eFD agreement as
`5 U.S.C. app. § 105(c)` and recodified in 2022. It prohibits obtaining or
using a report "for any commercial purpose, other than by news and
communications media for dissemination to the general public", or for the
solicitation of money, with a civil penalty up to $10,000.

This tool is personal and free, shared without charge. That sits outside the
restriction. **If that ever changes, this section is where the analysis starts
over.** The restriction covers the congressional reports only — USAspending,
Senate LDA lobbying, SEC Form 4 and 13F carry no equivalent use limit.

Government works are public domain under 17 U.S.C. §105, so redistributing the
published snapshot raises no copyright question. §105(c) is a use restriction,
separate from copyright, and both were considered.

## Data sources — all verified reachable 2026-09-20

| Source | Endpoint | Key | Lag | Verified |
|---|---|---|---|---|
| House PTR index | `disclosures-clerk.house.gov/public_disc/financial-pdfs/{yr}FD.zip` | none | 30-45d | 200 · 1,664 filings in 2026, 395 of them PTRs |
| House PTR detail | `/public_disc/ptr-pdfs/{yr}/{DocID}.pdf` | none | — | 200 · real text layer, fields extracted from 3 samples |
| Senate PTR search | `efdsearch.senate.gov/search/report/data/` | none | 30-45d | 200 · CSRF plus agreement POST, 130 filings since Jan 2026 |
| Senate PTR detail | `/search/view/ptr/{uuid}/` | none | — | 200 · clean HTML table, carries option strike and expiry |
| Form 4 insiders | SEC EDGAR daily index | none | **2 business days** | 200 · 744KB/day |
| 13F holdings | `data.sec.gov/submissions/CIK*.json` | none | quarterly, 45d | 200 · Scion, Pershing, Gates Trust, Oaktree all resolve |
| Lobbying | `lda.senate.gov/api/v1/filings/` | none | quarterly | 200 · 56,685 filings for 2026 |
| Contracts | `api.usaspending.gov/api/v2/search/spending_by_award/` | none | rolling | 200 |
| Roster and committees | `unitedstates.github.io/congress-legislators` | none | — | 200 |

Not used: Congress.gov (403, needs key), FEC (needs key), PatentsView (needs
key), DOL WARN (403), Google Trends (unofficial and fragile).

## Modules

Three new files, matching the `value_engine.py` / `value_analysis_gui.py`
split already used here — math and fetching carry no GUI imports and run from
a terminal.

| File | Role |
|---|---|
| `political_feeds.py` | Fetchers and parsers, one function per source. Returns normalized rows. No storage, no state. |
| `political_engine.py` | SQLite store, dedupe, watermarks, scan loop, grading, publish, CLI. |
| `political_gui.py` | The Copy Trading tab. Run button, toggles, live table. |

Registered in `dashboard.py` as a seventh entry in `APPS`, launched as its own
process like every other tool.

One new dependency: `pypdf`, for the House PTR text layer. Nothing else.

## Storage

SQLite (`political.db`) as the working store — incremental append, indexed
lookups from the GUI, survives an interrupted scan. One normalized table:

    disclosures
      id           TEXT PRIMARY KEY   -- "{source}:{doc_id}:{line_no}"
      source       TEXT               -- house_ptr | senate_ptr | form4 | f13
      filer_name   TEXT
      filer_id     TEXT               -- bioguide id or CIK
      chamber      TEXT               -- house | senate | corporate | fund
      party, state TEXT
      committees   TEXT               -- json array
      ticker       TEXT
      asset_name   TEXT
      asset_type   TEXT               -- ST | OP | GS | OT
      txn_type     TEXT               -- P | S | S_partial | E
      owner        TEXT               -- self | spouse | joint | child
      trade_date   DATE
      notify_date  DATE
      file_date    DATE
      amount_lo    REAL               -- bucket floor
      amount_hi    REAL               -- bucket ceiling
      shares       REAL               -- form4 and 13f only
      price        REAL               -- form4 only
      option_type  TEXT               -- call | put
      strike       REAL
      expiry       DATE
      doc_id, doc_url TEXT
      fetched_at   TIMESTAMP
      raw          TEXT               -- source line, kept for audit

Plus `filers` (roster, party, committees) and `scan_state` (per-source
watermark, so a restart resumes rather than refetches).

`raw` is kept deliberately. A parser change six months from now has to be
checkable against what the filing actually said, and re-fetching 400 PDFs to
answer that question is not a plan.

Publishing emits `political_snapshot.json.gz` — the same rows, portable,
readable with stdlib alone, small enough to commit. The SQLite file never
leaves the machine, because git stores each version of a binary whole and a
weekly commit of a growing database would bloat the repo for no gain.

## The scan loop

Disclosure delay is 30 to 45 days. Polling every five minutes instead of every
hour improves freshness by roughly a tenth of a percent, so it is not done.

| Tier | What | Interval | Cost |
|---|---|---|---|
| A | House bulk index, Senate eFD search | 30 min | `If-Modified-Since` on a 59KB zip — usually zero bytes |
| B | New PTR PDFs | only on an unseen DocID | about 1.5/day steady state |
| C | Form 4 daily index | once, after the close | 744KB |
| D | 13F | daily inside the 45-day window, weekly outside | reuses the window logic at `squeeze_catalyst.py:184` |
| E | Lobbying, contracts | weekly | — |

The worker runs on `threading.Event().wait(interval)` rather than a sleep
loop, so Stop is immediate and the GUI never blocks. Idle cost is one sleeping
thread. A shared token bucket modelled on `yfinance_throttle.py` fronts every
host; SEC publishes a 10 requests/second ceiling and this stays far below it.

First run backfills one month — roughly 35 House PTRs and 15 Senate filings,
one to two minutes. A seed snapshot ships alongside so even that is skipped.
Every run afterwards is incremental from the watermark.

## Toggles

    Chamber      House · Senate · Both
    Party        D · R · I
    Committee    from congress-legislators, multi-select
    Filer        individual members, multi-select, searchable
    Owner        self · spouse · joint · dependent child
    Asset        stock · option · bond · other
    Txn          purchase · sale · partial sale · exchange
    Amount       bucket floor
    Other feeds  13F whales (list) · Form 4 insiders (watchlist)

## Grading

Reuses `review_outcomes._price_on_or_after`, which refuses to grade when the
tape has gapped. That function exists because fabricated outcomes on CRKN,
SBNY and MDRX moved this repo's base rate from -0.84% to +1.08% and accounted
for 14 of the 20 worst episodes. The same trap is waiting here.

Every disclosure is graded at 10, 20 and 60 days from two anchors:

- **notify_date** — the day the trade became public. The only tradeable one,
  and the only honest headline number.
- **trade_date** — what the filer got. Not available to anyone else.

The gap between those two is the number this tool exists to report, and
nothing found so far publishes it plainly.

Three things the output must always carry:

1. **n, beside every rate.** A member with four trades has no record.
2. **A permutation p-value on any ranking.** Best-of-535 is not a finding.
3. **Amount as a range.** Filings disclose `$15,001 - $50,000`. Any position
   size derived from that is a range, and collapsing it to a point estimate
   invents precision that does not exist.

Results land in `political_log.csv`, write-only and never read back by the
scanner, matching `squeeze_logger.py`.

## Phases

1. `political_feeds.py` — House and Senate congress path only. Parse to
   normalized rows, print to terminal. No storage, no GUI.
2. `political_engine.py` — SQLite store, watermarks, one-month backfill,
   incremental scan, CLI.
3. `political_gui.py` plus the `dashboard.py` entry — Run button, toggles,
   live table, background worker.
4. Grading and the honest-statistics layer. Forward returns from both anchors,
   n and p-values everywhere.
5. Form 4 and 13F feeds behind their toggles.
6. `publish()` — gzipped snapshot, and a read-only mode in the GUI that loads
   a snapshot instead of scanning.
7. Lobbying and contracts overlay. Optional, last, and possibly never.

## Known risks

- **PTR parsing is sampled, not solved.** Three PDFs from one filer parsed
  cleanly. Amended reports, multi-page transaction tables, scanned paper
  filings and unusual asset descriptions have not been seen yet. Phase 1 ends
  when a parse runs across all 395 of 2026's House PTRs with an explicit count
  of what failed, not when the first file works.
- **Tickers are not always present.** The Senate sample showed a stock sale
  with `--` in the ticker column and the symbol only inside the asset name.
  Name-to-ticker resolution will be imperfect and has to record its own
  confidence rather than guess silently.
- **Senate eFD needs a session.** CSRF plus an agreement POST, and the session
  can expire mid-scan. Re-establishing it has to be automatic.
- **Options detail exists only on the Senate side.** House PTRs describe
  options in free text. Strike and expiry will be sparse and must be nullable
  rather than defaulted.
- **The edge may not exist.** A trade 30 to 45 days old, of unknown size, may
  carry nothing at all. The grading layer is built so the tool can say that.

## What changed

Written after the build. Each of these was wrong in the plan or invisible
until the code ran against real filings.

**House coverage is 88.4%, and the gap has names.** 349 of 395 PTRs parse.
All 46 failures are paper filings scanned without a text layer, belonging to
ten members: Harshbarger (11), Khanna (8), Rogers (7), McCaul (6),
Fleischmann (5), Wied (5), Cole, Malliotakis, Mann, Self. Electronic filings
carry a DocID of the form `2XXXXXXX` and paper ones a seven-digit ID, and
the split was clean across all 395, so the check happens before the fetch
rather than after a failed parse.

**pypdf renders letter-spaced headings as NUL bytes, not spaces.** The form's
labels arrive as `F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00:`. Every
pattern was written against whitespace, so the metadata stripper matched
nothing, each row's trailing labels were folded into the next row's asset
description, and the `SP`/`JT` owner code at the front of it was eaten. The
visible symptom was that every spouse and joint trade in the file read as the
member's own.

**Four more parser faults, all silent.** The Senate table carries both
"Asset Type" and "Type", and a substring match for "type" found the wrong one
and left every buy and sell unclassified. `_norm_name` stripped accents
instead of folding them, so "Sánchez" became "snchez" and never matched
"Sanchez" — 340 rows. Compound surnames split differently between the two
sources ("Delaney, April McClain" against "McClain Delaney"). The top amount
bucket is one-sided and prefixed with words — "Spouse/DC Over $1,000,000" —
so a regex requiring a range dropped an entire filing.

**Form 4's date format broke the only tradeable anchor.** EDGAR's daily index
writes `20260917`, which `_mdy` did not parse, so all 3,578 insider rows
landed with `notify_date` empty and the entire feed vanished from
notify-anchored grading while looking fine everywhere else.

**A per-day budget on Form 4 is a sampling rule, not a feed.** EDGAR's index
is ordered by company name, so reading the first N filings each day means
"every insider whose employer starts with A". The watchlist is now the
tickers Congress and the tracked funds have actually disclosed, resolved to
CIKs from SEC's own company list so the filter runs on the index line instead
of after downloading. That cut a day from 1,457 documents to 43.

**The two-anchor comparison was measuring itself.** The trade date is always
earlier, so its window closes first, and taking each anchor's graded rows
independently compared 1,662 rows against 1,566 — the trade side silently
collecting every disclosure whose notify window had not matured. A difference
computed across two different samples is not a difference. `anchor_gap` now
uses paired rows only, and splits by source, because a 13F "trade date" is a
quarter end rather than an execution time and its gap is partly just the
length of a quarter.

**The permutation test needed two guards it did not have.** At `min_n=5` the
leaderboard put Nancy Pelosi on top at +20.38% on six trades with p=0.007.
Raising the floor to ten removed her from the table entirely and the leader
became Berkshire at +1.85% with p=0.860 — noise. The pool also mixes
congressional PTRs, Form 4 buys and 13F quarter diffs, which are not
exchangeable, so the null the p-value tests is one nobody believes; that
violation is now named in the output rather than left implicit.

**`self._stop` shadows `threading.Thread._stop`.** `join()` calls it
internally, so naming the worker's stop Event `_stop` made every join raise
`'Event' object is not callable` — which is what closing the window does.

**A shared snapshot has to carry the grades.** Publishing the rows alone
would have left every reader to re-fetch 1,190 price histories, which is the
one cost the sharing model exists to pay once.

**P2 had to be narrowed to test anything.** "Sits on a committee" selects 415
of 423 congressional rows. Even a list including Armed Services, Agriculture,
Small Business and Science selects 89%. The register now names five
money-jurisdiction committees, which selects about 5% and will take months to
reach its floor — the correct behaviour, not a problem to tune around.

**The House form has three dates and only one of them is public.** Found
2026-09-21, after the first build. The PTR's "Notification Date" column is
when the FILER was told — for a managed account or a trust, weeks before
they filed — and the tool was grading its tradeable anchor off it. Pelosi's
2026-07-24 purchases carry a Notification Date of 2026-07-24 and were filed
2026-08-21: twenty-eight days during which the trade was not public and the
"10-day return from disclosure" was being measured from a day nobody could
trade on. Across all 329 House rows the error averaged 13.4 days. Senate,
Form 4 and 13F were already correct — their notify and filing dates are the
same document. `filer_notified` now keeps the form's column for what it is,
`notify_date` is the filing date everywhere, and the House grades were
cleared and recomputed.

**Measured on the first backfill** (8,295 disclosures, 760 filers), after
that correction: mean congressional disclosure delay **37.6 days**. 10-day
paired House purchases returned +4.41% from the trade date and +0.52% from
the filing date, n=53. The sample shrank from 118 because an anchor 13 days
later leaves fewer matured windows, which is the correct behaviour. One
month of filings is one market regime. Registered as P3 rather than reported
as a finding.
