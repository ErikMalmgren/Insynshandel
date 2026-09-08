<!-- Part of IMPLEMENTATION_PLAN.md. Section numbers (§N.M) are stable
     identifiers across all plan files — see the map in the index. -->

# Background — read once, then move on
## 1. Target architecture

```
                    ┌──────────────────────────────────────────┐
                    │  marknadssok.fi.se  (CSV export, UTF-16) │
                    └────────────────────┬─────────────────────┘
                                         │ date-windowed fetch, bisect on cap
                                         ▼
  ┌────────────┐   ┌──────────────────────────────────────────────────┐
  │  OpenFIGI  │   │  raw_transaction   (append-only, source-faithful) │  phase 1
  │  Riksbank  │   └────────────────────┬─────────────────────────────┘
  │  Yahoo     │                        ▼
  └─────┬──────┘   ┌──────────────────────────────────────────────────┐
        │          │  transaction_norm  (typed, classified)            │  phase 2-3
        ▼          └────────────────────┬─────────────────────────────┘
  ┌────────────┐                        ▼
  │  company   │   ┌──────────────────────────────────────────────────┐
  │ market_cap │──▶│  agg_* tables / views                            │  phase 3
  └────────────┘   └────────────────────┬─────────────────────────────┘
      phase 5                           │
                          ┌─────────────┴──────────────┐
                          ▼                            ▼
                  ┌───────────────┐          ┌──────────────────┐
                  │ FastAPI       │          │ static JSON dump │  phase 4 / 6
                  │ (live queries)│          │ (committed to    │
                  └───────┬───────┘          │  Pages repo)     │
                          │                  └────────┬─────────┘
                          └──────────┬────────────────┘
                                     ▼
                          GitHub Pages frontend
```

### 1.1 The two read paths — build both

The API and the static JSON dump **share the same serializers**. `export_static.py`
calls the same functions the API routes call and writes their output to files.
This guarantees identical response shapes and costs almost nothing.

- **Static JSON** is the default path. Zero hosting cost, zero latency, no CORS,
  no uptime concerns. Good enough for the leaderboard and per-company pages.
- **Live API** is for what static files can't do: arbitrary date windows,
  free-text search, pagination over 180k transactions.

**Build order is `0 → 1 → 2 → 3 → 6 → 4`.** Phase 6 (static export) ships
*before* phase 4 (API). After phase 6 you have a working public site with
nothing to host and nothing to pay for; the API is then an upgrade, not a
prerequisite. The frontend switches by changing one base URL:

```js
const API_BASE = './data';                        // static JSON
// const API_BASE = 'https://api.example.com/api/v1';   // live API
```

### 1.2 Hosting — answer to question 1

**No, this does not change your GitHub Pages hosting.** A static site is
perfectly capable of calling a JSON API on another origin. The only new
requirement is one HTTP header on the API side:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://<your-user>.github.io", "https://<your-domain>"],
    allow_methods=["GET"],
    allow_headers=["*"],
)
```

No proxy, no migration off Pages, no server-side rendering. If you go
static-JSON-only, you don't even need that — the JSON lives in the Pages repo
and is same-origin.

**Where the API runs** (when you want it): a €4–6/month VM, Fly.io, or Railway,
with a persistent volume holding `insynshandel.db`. Add
[Litestream](https://litestream.io/) to stream the SQLite file to object storage
for backup. Do not put SQLite on a platform with an ephemeral filesystem.

---
## 12. Bugs in the current code that this plan fixes

### 12.1 `script.py:44` — the `00` prefix

```python
if karak in ("Förvärv", "00Teckning", "00Tilldelning"):
```

**This was deliberate, not a typo.** The `00` prefix was a way to disable those
two values while their meaning was unresolved — `Förvärv` and `Avyttring` map
cleanly to bought/sold, `Teckning` and `Tilldelning` did not.

The instinct was right, and the data confirms it. The two are not even
interchangeable with each other: `Teckning` is 96% cash-priced and 96%
unconnected to a share program, while `Tilldelning` is 54% share-program and
45% priced at zero (§6.1.1). Lumping them together would have been wrong.

**The `00` decision is upheld, not overturned.** Neither counts — `Tilldelning`
because it is mostly free compensation, `Teckning` because 139 of its 238 rows
sit on BTA/BTU interim paper that later converts, risking a double count. The
counted set is `Förvärv` and `Avyttring`: bought and sold, no interpretation
needed.

What *is* a structural problem is the mechanism. An in-line string match means
a disabled value and an unknown value are indistinguishable — both fall into
`else: continue` and neither is reported. §6.1 replaces it with a seed table
where every value carries an explicit `counted` flag and a reason, and an
unmapped value **fails the build** instead of vanishing. Sampling just 2020
turned up two Karaktär values (`Koncernintern överföring …`) that were absent
from the 2026 file entirely; with the old mechanism they would have been
silently dropped forever.

### 12.2 `script.py:34` — ISINs collected before the status filter

`isin.add(cur_line[14])` runs before `if status != "Aktuell": continue`, so
ISINs from cancelled and superseded rows pollute `isin.csv`, which then drives
the OpenFIGI lookups. In the new design ISINs come from a query over
`raw_live`, so the filter is explicit.

### 12.3 `script.py:22` — `isin.csv` is read, mutated, and rewritten in place

An ever-growing file that is both input and output of the same run, with no
history. Replaced by `raw_transaction` as the sole source of truth.

### 12.4 `script.py:27` — naive `;` splitting crashes on real data

```python
cur_line = line.strip().split(";")
```

This is not a latent risk; it is a **live crash**. The export uses RFC 4180
quoting, and `Beskrivning av korrigering` can contain a semicolon. A real row
from 2020-03-18 (BillerudKorsnäs / Michael Kaufmann) splits into 24 fields,
shifting every column past index 8, and `float(cur_line[16])` raises
`ValueError: could not convert string to float: '2020-03-13 00:00:00'`.

The current script survives only because the manually-downloaded window happened
to contain no such row. **A backfill over March 2020 would abort on it.** Any
row that shifted columns without crashing would be worse — silently wrong
currency, price and status.

Fix: `csv.reader(f, delimiter=';')` plus a hard `len(row) == 23` assertion
(§4.1.1).

### 12.5 The 1000-row cap was invisible

`Insyn2026-04-21.csv` has 985 rows — right up against the cap. The manual
workflow was one busy week away from silently losing data with no signal.

### 12.6 Market cap has no as-of date

`company_map.csv` holds one mutable `MarketCap` scalar per company. There is no
record of when it was fetched, so "andel av mcap" silently compares a historical
flow against whatever number was last written. Fixed by `market_cap(lei, as_of)`
plus an explicit definition (§6.6).

---
## 13. Things worth deciding that weren't in the brief

Ordered by how much they'll cost you if ignored.

1. **The repo cannot currently be pushed.** 249 MB file, no git. §3 — do it
   first.

2. **Revisions mutate old rows.** The single most likely way this system goes
   quietly wrong is a 7-day-only ingest serving `Reviderad` rows as current
   forever. The nightly 90-day re-scan (§4.3.1) is the fix, and it is the
   thing most likely to get dropped as "redundant" because it usually finds
   nothing. It finding nothing is the normal case, not evidence it is useless.

3. **`yfinance` is unofficial and legally grey for redistribution.** It scrapes
   Yahoo, breaks without warning, and Yahoo's ToS restricts redistributing their
   data on a public site. It's fine for a personal project; if the site grows,
   consider Nasdaq Nordic's own data or a paid feed. At minimum, don't build the
   UI so that a `yfinance` outage empties the page — market cap must be an
   optional enrichment, never a join that drops rows.

4. **You have no stable row ID.** The CSV export doesn't expose one, which is
   why dedup needs a content hash. The **HTML** result pages *do*:
   `/Rapportsammanställning/Index/A004C215-1` — sequential, with what looks like
   a `-N` revision suffix. If you ever need reliable per-report identity
   (permalinks to FI, precise revision chains), that's the path — one extra
   HTML fetch per listing page. Not needed now; worth knowing it exists.

5. **Terms of use for the FI data.** The insider register is public data and
   FI publishes an open-data page, but check the terms before putting it behind
   a public API and confirm attribution requirements. Add a source credit and a
   "data from Finansinspektionen, updated {date}" line to the site regardless.

6. **Be a polite client.** 3 s between requests, a descriptive `User-Agent`
   with contact info, exponential backoff on 5xx/connection-reset, and a hard
   cap on requests per run. Backfill is **~330 requests over 30–45 minutes**
   (§4.2.2) — run it once, off-hours, and make it resumable so a failure
   doesn't mean doing it again. Daily incremental is ~2 requests.

7. **Person-level identity is unreliable — a second reason not to expose it.**
   `Person i ledande ställning` is a free-text name. The same person at two
   companies, spelling variants, and namesakes are all indistinguishable. Even
   setting aside the GDPR scope decision in §8.1, a "top insider buyers by
   person" feature built on this column would be quietly wrong. If it is ever
   built, it needs an explicit entity-resolution step and a visible confidence
   caveat.

8. **`Befattning` is free text and the vocabulary drifted badly.**
   **[VERIFIED]** ~250 distinct values across the 2016–2026 samples. Recent
   years use a controlled list of ~10 (`Styrelseledamot`,
   `Verkställande direktör (VD)`, …); older years are uncontrolled free text
   mixing Swedish and English (`Board member/Board Deputy`, `CEO`, `Director`),
   comma-joined multi-roles (`Vice VD, Styrelseledamot`), trailing whitespace,
   inconsistent case (`vd`, `VD`, `Vd`), and outright typos
   (`Styresleordförande`, `Chief Opeerating Officer`, `Styrelsseledamot`).
   **Do not build a position filter over the raw column.** If you want one, add
   a `position_map` seed table (same pattern as `nature_map`) collapsing values
   into a handful of buckets — CEO / CFO / Chair / Board / Other executive —
   with an explicit `unmapped` bucket. Store the raw string regardless.

9. **What does "net" mean to a reader?** SEK-weighted net flow is dominated by
   large caps. A leaderboard sorted by raw net value will always be Ericsson,
   Investor, Volvo. `pct_of_mcap` (§6.6) is the more interesting ranking and
   should probably be the default sort. Consider also surfacing
   `buyer_count`/`seller_count` — five directors buying is a different signal
   from one director buying five times.

10. **Timezone.** All FI timestamps are Europe/Stockholm local, with no offset in
   the data. Store them verbatim as text; do not naively `datetime.utcnow()`
   anywhere near them. Only `fetched_at` / `computed_at` should be UTC, and
   label them as such.

11. **Backups.** Once the backfill exists, losing the DB means ~330 requests to
    rebuild — annoying but survivable. Once you add anything not reconstructible
    from FI (manual ticker overrides, accumulated market-cap history), it isn't.
    `ticker_override.csv` is committed for exactly this reason; back up the DB
    once `market_cap` has real history.

---
## 14. Open questions — decide these, don't guess

Everything above is settled. These are not. **If you are the implementing
model: pick the stated default, implement it, and leave a `# OPEN QUESTION §14.n`
comment at the site of the decision. Do not silently choose differently, and do
not stall waiting for an answer.**

### 14.1 ~~The absolute outlier ceiling~~ — RESOLVED

Decided: **no fallback threshold.** A missing market cap yields
`verification = 'unverifiable'`, stored as market cap `0` and read through the
`market_cap_current` view so the sentinel becomes SQL `NULL` before it can
reach arithmetic. See §6.4. Nothing to decide here; kept so the reasoning is
findable.

### 14.2 BTA/BTU double-counting — deferred, not open

`Teckning` is no longer counted (§6.1.1), so the double-count risk cannot
occur. This stays only as the check to run **if** you ever set
`counted = 1` for `Teckning` in `nature_map.csv`:

```sql
-- A counted Teckning on interim paper (BTA/BTU), followed within 90 days by
-- another counted row for the same person and issuer on a DIFFERENT ISIN at a
-- similar volume. Each hit is a candidate double-count.
SELECT t.lei, t.pdmr, t.transaction_date AS teckning_date, t.isin AS bta_isin,
       t.volume AS bta_volume, l.transaction_date AS later_date,
       l.isin AS later_isin, l.volume AS later_volume, l.nature
  FROM transaction_norm t
  JOIN transaction_norm l
    ON l.lei  = t.lei
   AND l.pdmr = t.pdmr
   AND l.isin <> t.isin
   AND l.transaction_date >  t.transaction_date
   AND l.transaction_date <= date(t.transaction_date, '+90 days')
   AND l.is_counted = 1
   AND abs(l.volume - t.volume) <= 0.02 * t.volume
 WHERE t.is_counted = 1
   AND t.nature = 'Teckning'
   AND t.instrument_type LIKE 'BT%'
 ORDER BY t.lei, t.transaction_date;
```

A handful of hits is noise. Dozens, clustered around rights issues, means the
conversion leg is also being counted.

### 14.3 ~~Mixed-currency issuers~~ — RESOLVED

Decided: **everything converts to SEK** at the transaction-date Riksbank rate
(§6.3). There are no per-currency buckets, so a multi-currency issuer produces
one row like any other. Nothing to decide here.

### 14.4 Default sort order — moved to the frontend

The backend no longer sorts the leaderboard (§8.2), so this is a UI decision,
not a schema one. The substance still stands and is worth knowing when you
build the table:

- `net_value_sek` descending will rank Ericsson, Investor and Volvo at the top
  every time — they are simply larger.
- `pct_of_mcap` is more informative but is `null` for the 26% of companies
  without a market cap, and is noisy for micro caps.
- `buyer_count` / `seller_count` distinguish "five directors bought" from "one
  director bought five times". Neither value metric captures that.

All four ship in every `/leaderboard` response, so this is a one-line change in
the frontend rather than an API change.

### 14.5 `normalize` must be a full rebuild, not incremental

Not really a question — a constraint that is easy to violate for performance
reasons and expensive to debug afterwards.

`transaction_norm` must be derived from `raw_live` **in full** on every run:

```
DELETE FROM transaction_norm;   -- then re-derive everything
```

A `Makulerad` (cancelled) or `Reviderad` row that was previously counted must
stop being counted, and a `nature_map` edit must take effect retroactively. An
incremental normalize that only processes new `raw_transaction` rows silently
misses both. Over 180k rows a full rebuild takes seconds — there is no
performance argument for the incremental version.

If it ever does get slow, materialize incrementally **keyed on
`raw_transaction.id`** with an explicit invalidation pass for rows whose
`superseded_at` changed since the last run. Do not hand-roll that until the
full rebuild actually hurts.

### 14.6 Distinct-person counting

`buyer_count` / `seller_count` count `DISTINCT pdmr` (Person i ledande
ställning), **not** `anmälningsskyldig`. The latter is often a holding company
(`Marknadspotential Aktiebolag`) and would inflate the count when one person
trades through several vehicles.

Given the free-text problems in §13 item 7, treat these counts as approximate and do
not present them as precise. They are still stored and used internally; §8.1
governs what is exposed.

---
## 15. The README

The plan is written for an implementer. The README is written for a **reader**
— which for this project means a recruiter or an engineer who has thirty
seconds and will not open the source. Write it once the pipeline works, and
keep it short.

Cover, in this order:

1. **What it is, in two sentences.** "Every insider transaction reported to
   Finansinspektionen since July 2016, aggregated per company." Lead with the
   thing, not the stack.
2. **A screenshot or the live link.** Before anything technical.
3. **How to run it.** `uv sync && uv run insyn db migrate && uv run insyn
   ingest backfill`. If it takes more than three commands, the plan has failed
   somewhere.
4. **The data problems, and what you did about them.** This is the most
   valuable section and the one most portfolios lack:
   - the export caps at 1000 rows and silently drops the oldest, so ingestion
     bisects date windows
   - the register contains uncorrected filing errors — one puts Swedbank at
     10 quadrillion SEK — so a transaction exceeding its issuer's market cap is
     excluded and published on a data-quality page
   - `Karaktär` has 29 values of which two mean "bought" and "sold"; the rest
     are excluded for stated reasons
5. **What you deliberately did not build, and why.** No message broker, no
   Kubernetes, no person search (§0.1, §8.1). Explaining a rejected option
   demonstrates more than adopting it does.
6. **Attribution.** Data from Finansinspektionen; FX rates from Sveriges
   Riksbank; a "last updated" timestamp.

> Do not paste this plan into the README. Link to it. A 2,600-line document is
> evidence of thoroughness only if the reader can also find the two-paragraph
> version.
