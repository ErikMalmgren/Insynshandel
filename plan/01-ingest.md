<!-- Part of IMPLEMENTATION_PLAN.md. Section numbers (§N.M) are stable
     identifiers across all plan files — see the map in the index. -->

# Phase 1 — fetch to raw store
## 4. Phase 1 — fetch → raw store

The single most important phase. It must land and pass its checks before
anything else begins. Everything downstream is a re-runnable query over this
table; a classification bug becomes a `UPDATE` + re-aggregate, never a
re-download.

### 4.1 The source — verified facts

**[VERIFIED]** URL template (all params required, `Page` is present but ignored):

```
https://marknadssok.fi.se/Publiceringsklient/sv-SE/Search/Search
  ?SearchFunctionType=Insyn
  &Utgivare=
  &PersonILedandeStallningNamn=
  &Transaktionsdatum.From=
  &Transaktionsdatum.To=
  &Publiceringsdatum.From={YYYY-MM-DD}
  &Publiceringsdatum.To={YYYY-MM-DD}
  &button=export
  &Page=1
```

Response: `Content-Type: text/csv`,
`Content-Disposition: attachment; filename=Insyn{today}.csv`.

| Property | Value |
| --- | --- |
| Encoding | **UTF-16LE, no BOM** — decode with `.decode('utf-16-le')`, not `utf-16` |
| Delimiter | `;` |
| Quoting | **RFC 4180 quoting is used.** Fields may be `"`-wrapped, contain embedded `;`, and escape inner quotes as `""`. **Use `csv.reader(f, delimiter=';')` — never `line.split(';')`.** See §4.1.1. |
| Embedded newlines | None observed across 6,500 rows (physical line count == record count), but let `csv.reader` handle the file object anyway rather than iterating lines. |
| Trailing field | Every row ends with `;` → 23 fields, last is empty. 22 real columns. |
| Line ending | `\r\n` |
| Row cap | **Exactly 1000 rows, hard.** |
| `Page` param | **Ignored by the export.** `Page=2` and `Page=3` return byte-identical payloads to `Page=1`. Pagination is not available. |
| Sort order | Publiceringsdatum **descending**. A capped window silently drops the *oldest* rows. |
| Earliest data | **2016-07** (MAR entry into force). Queries for 2015 return 0 rows. |
| Rate limiting | Real. A year-wide query got `ConnectionResetError`. **Sleep ≥ 3 s between requests** — that spacing was stable across ~20 sequential requests. |

Column order (0-indexed), exactly as in the header row:

```
0  Publiceringsdatum              11 Karaktär
1  Emittent                       12 Instrumenttyp
2  LEI-kod                        13 Instrumentnamn
3  Anmälningsskyldig              14 ISIN
4  Person i ledande ställning     15 Transaktionsdatum
5  Befattning                     16 Volym
6  Närstående                     17 Volymsenhet
7  Korrigering                    18 Pris
8  Beskrivning av korrigering     19 Valuta
9  Är förstagångsrapportering     20 Handelsplats
10 Är kopplad till aktieprogram   21 Status
```

Numbers use a **comma decimal separator** (`2600000,0`, `105,6997`).
Dates are `YYYY-MM-DD HH:MM:SS`.

#### 4.1.1 Quoting is real — this is a crash, not a theoretical risk

**[VERIFIED]** Two columns carry free text and *are* quoted when they need to be:
`Beskrivning av korrigering` (col 8) and `Instrumentnamn` (col 13). Real rows
from the register:

```
...;Ja;"Wrong transaction date reported; unintentionally checked ""closely associated"" box, which was no correct";;;Förvärv;...
...;;;Förvärv;Aktie;"Vertiseit AB B-aktie ""VERT""";SE0012481133;...
...;Ja;"efter klargörande från FI skulle ""Kopplad till aktieoptionsprogram"" ej fyllas i!";;;Lösen minskning;...
```

The first row contains a **semicolon inside a quoted field**. A naive
`line.split(';')` yields 24 fields instead of 23 and shifts every column past
index 8:

```
c[16] (Volym)  → '2020-03-13 00:00:00'   → float() raises ValueError
c[18] (Pris)   → 'Antal'
c[19] (Valuta) → '100,9'
c[21] (Status) → 'NASDAQ STOCKHOLM AB'
```

`csv.reader` parses all 6,500 sampled rows to exactly 23 fields, including
these. Assert `len(row) == 23` on every record and reject anything else as a
format change.

> **Note on `Anmälningsskyldig` vs `Person i ledande ställning`:** they are not
> the same. `Anmälningsskyldig` is the party with the reporting obligation —
> often a company owned by the insider (e.g. `Marknadspotential Aktiebolag`).
> `Person i ledande ställning` is the PDMR. Any "person" view must key on
> `Person i ledande ställning`.

### 4.2 The 1000-row cap and bisection

**[VERIFIED]** the cap is per-query, and narrowing the date window escapes it:

| Window | Rows |
| --- | --- |
| `2020-03-01 .. 2020-03-31` | 1000 (capped) |
| `2020-03-01 .. 2020-03-15` | 1000 (still capped) |
| `2020-03-16 .. 2020-03-31` | 1000 (still capped) |
| `2020-03-01 .. 2020-03-04` | 393 |
| `2020-03-05 .. 2020-03-08` | 410 |
| `2020-03-09 .. 2020-03-11` | 292 |
| `2020-03-12 .. 2020-03-15` | 463 |

March 2020 (COVID crash) is the busiest month in the register's history and
resolves at **3–4 day windows**, i.e. recursion depth ~3 from a monthly start.
Busiest observed single day: **217 rows** (2020-03-13). Day granularity is a
safe floor.

Algorithm:

```python
def fetch_window(from_date, to_date, depth=0):
    rows = http_get_csv(from_date, to_date)      # sleep 3s before each call
    if len(rows) >= CAP:                          # CAP = 1000
        if from_date == to_date:
            log.error("single day %s still capped — data loss", from_date)
            yield from_date, to_date, rows, True  # truncated=True, store anyway
            return
        mid = from_date + (to_date - from_date) // 2
        yield from fetch_window(from_date, mid, depth + 1)
        yield from fetch_window(mid + 1day, to_date, depth + 1)
    else:
        yield from_date, to_date, rows, False
```

**Only leaf windows get inserted.** A capped window's rows are discarded
without touching the DB — this keeps the insert path free of partial data.

A window returning exactly 1000 genuine rows would be bisected unnecessarily.
That costs two extra HTTP calls and is otherwise harmless. Accept it.

#### 4.2.1 Seed window size — use 14 days, not one month

**[VERIFIED]** monthly windows are the wrong starting granularity. **Every
sampled May from 2017 to 2026 caps at 1000** — AGM and share-program season
guarantees it. Measured seed-window sizes:

| Seed size | Observed range | Caps? |
| --- | --- | --- |
| 1 month | 581 (2017-01) … 1000+ (every May, 2020-03) | **Frequently** |
| 14 days | 932 (2026-05) … 1000 (2021-06, 2024-05) | Occasionally, in May–June |
| 7 days | 150 (2024-01) … ~755 (peak COVID week) | **Never observed** |

Use **14-day seed windows**. Roughly 266 windows cover 2016-07 → today; the
~30 that cap bisect once. 7-day seeds never cap but need ~530 requests for the
same result.

#### 4.2.2 Backfill cost — plan for ~30–45 minutes

**~330 HTTP requests** (266 seed windows + ~60 from bisections), at 3 s spacing
plus 2–5 s response time. Budget **30–45 minutes** for a full backfill. Run it
once; `incremental` thereafter is a handful of requests per day.

Make the backfill **resumable**: record each completed leaf window in
`fetch_batch` and skip windows already covered by a successful batch, so a
network failure at minute 30 doesn't restart from 2016.

### 4.3 Two commands, one code path

Because the write path is a **diff** (§4.5), re-fetching a window you already
have writes nothing. That collapses what would otherwise be three ingest modes
into one parameterized operation:

```
insyn ingest recent --days N     # fetch [today-N, today], diff into the DB
insyn ingest backfill            # fetch [2016-07-01, today] in 14-day windows
```

`backfill` is `recent` with a longer range and resumability. **There is no
semantic difference in how rows are written** — same function, same guarantees.
Do not write a second code path for it.

Cadence (§10.3): `--days 7` hourly, `--days 90` nightly.

#### 4.3.1 Why the nightly 90-day re-scan is not optional

When FI revises a report it publishes a *new* row (`Status=Aktuell`,
`Korrigering=Ja`) **and flips the original row to `Status=Reviderad` while
leaving that row's original Publiceringsdatum untouched.** Observed pair:

```
2026-04-21 10:20:42 ... Stefan Wänstedt ... 49,34633 ... Reviderad
2026-04-21 10:25:05 ... Stefan Wänstedt ... 49,34633 ... Aktuell   (Korrigering=Ja)
```

A revision landing months after the original changes the status of a row sitting
in an *old* publication-date window. A 7-day-only ingest never looks there
again and keeps serving that superseded row as `Aktuell` forever.

The 90-day window is ~7 requests and — thanks to the diff — writes **zero rows**
on a night when nothing was revised. It is cheap enough to run daily, so run it
daily. Widen it to 365 days once a month if you want more assurance; it costs
~26 requests and still writes nothing when nothing changed.

### 4.4 Schema — `migrations/001_raw.sql`

```sql
PRAGMA journal_mode = WAL;

CREATE TABLE fetch_batch (
  id            INTEGER PRIMARY KEY,
  mode          TEXT    NOT NULL,          -- backfill | recent
  window_from   TEXT    NOT NULL,          -- YYYY-MM-DD (Publiceringsdatum)
  window_to     TEXT    NOT NULL,
  started_at    TEXT    NOT NULL,
  finished_at   TEXT,
  row_count       INTEGER,          -- rows FI returned
  rows_inserted   INTEGER,          -- rows new to us (see §4.5)
  rows_superseded INTEGER,          -- rows FI stopped returning
  truncated     INTEGER NOT NULL DEFAULT 0,  -- 1 = hit cap at day granularity
  http_status   INTEGER,
  error         TEXT
);

CREATE TABLE raw_transaction (
  id            INTEGER PRIMARY KEY,
  row_hash      TEXT    NOT NULL,   -- sha256 of all 22 fields joined by \x1f
  ordinal       INTEGER NOT NULL,   -- 0-based index among identical row_hash
  pub_date      TEXT    NOT NULL,   -- substr(publiceringsdatum,1,10), stored not computed
  first_seen_batch INTEGER NOT NULL REFERENCES fetch_batch(id),
  last_seen_batch  INTEGER NOT NULL REFERENCES fetch_batch(id),
  superseded_at TEXT,               -- NULL = live

  publiceringsdatum            TEXT,
  emittent                     TEXT,
  lei_kod                      TEXT,
  anmalningsskyldig            TEXT,
  person_i_ledande_stallning   TEXT,
  befattning                   TEXT,
  narstaende                   TEXT,
  korrigering                  TEXT,
  beskrivning_av_korrigering   TEXT,
  ar_forstagangsrapportering   TEXT,
  ar_kopplad_till_aktieprogram TEXT,
  karaktar                     TEXT,
  instrumenttyp                TEXT,
  instrumentnamn               TEXT,
  isin                         TEXT,
  transaktionsdatum            TEXT,
  volym                        TEXT,
  volymsenhet                  TEXT,
  pris                         TEXT,
  valuta                       TEXT,
  handelsplats                 TEXT,
  status                       TEXT
);

-- One row per calendar day we have successfully fetched. This is what makes
-- "which days are we missing?" a single query instead of interval arithmetic
-- over fetch_batch windows. See §4.7.
CREATE TABLE coverage_day (
  pub_date         TEXT PRIMARY KEY,   -- YYYY-MM-DD
  first_fetched_at TEXT NOT NULL,
  last_fetched_at  TEXT NOT NULL,
  fetch_count      INTEGER NOT NULL DEFAULT 1,
  row_count        INTEGER NOT NULL    -- 0 is a valid, meaningful value
);

-- THE duplicate-prevention guarantee. The database itself refuses to hold two
-- live copies of the same source row. Everything in §4.5 depends on this index.
CREATE UNIQUE INDEX ux_raw_live_key
    ON raw_transaction(row_hash, ordinal)
 WHERE superseded_at IS NULL;

CREATE INDEX ix_raw_live_window ON raw_transaction(pub_date)
 WHERE superseded_at IS NULL;

CREATE VIEW raw_live AS
  SELECT * FROM raw_transaction WHERE superseded_at IS NULL;
```

**All 22 columns are `TEXT`, stored verbatim.** No number parsing, no filtering,
no aggregation in this phase. If FI adds a column or changes a value, this table
still holds the truth.

Notes on the key columns:

- **`row_hash` covers all 22 fields, including `status`.** That is deliberate: a
  status flip `Aktuell → Reviderad` changes the hash, so the revision shows up
  as "old row superseded, new row inserted" rather than a silent in-place edit.
  You get the audit trail for free.
- **`row_hash` is globally unique, not per-window.** It includes
  `publiceringsdatum` down to the second, so two rows with the same hash are by
  construction in the same publication-date window. No window qualifier needed
  in the index.
- **`ordinal`** distinguishes the source's genuine exact duplicates (§4.5).
- **`pub_date` is a stored column, not `substr()` in the index.** SQLite will
  not use an expression index for a `BETWEEN` range scan as reliably, and the
  diff query in §4.5 runs on every cron tick.

### 4.5 The write path is a diff, not a replace

**This section exists because the cron runs hourly.** A "supersede the window,
re-insert everything" write path would rewrite every row in the trailing window
on every tick — at 14 ticks/day over a 7-day window that is ~50,000 dead rows
per day, and the audit history becomes noise. The write path must be a **diff**:
compute what changed, write only that.

#### 4.5.1 Exact duplicates in the source are real

Verified in the sample: `SinterCast / Ian Kershaw / 1000 / 7,0685 GBP` appears
**3×**, byte-identical; a Zaplox row appears 3×. It is not possible to tell from
the CSV whether these are genuine separate transactions or a source-side
duplication artifact. **Preserve multiplicity; flag for review.**

That is what `ordinal` is for. Within a fetched window, group rows by
`row_hash` and number each group `0, 1, 2…` in the order returned. A plain
content hash as the key would silently collapse three real rows into one.

#### 4.5.2 The algorithm

For each **leaf** window `[f, t]`:

```python
fetched   = [(row_hash, ordinal, *fields), ...]        # from the CSV
live      = SELECT row_hash, ordinal, id
              FROM raw_transaction
             WHERE superseded_at IS NULL AND pub_date BETWEEN f AND t

to_insert    = keys(fetched) - keys(live)
to_supersede = keys(live)    - keys(fetched)
unchanged    = keys(fetched) & keys(live)              # touch last_seen only
```

```sql
BEGIN IMMEDIATE;

-- 1. rows FI no longer returns for this window (revised, cancelled, withdrawn)
UPDATE raw_transaction SET superseded_at = :now
 WHERE id IN (:to_supersede_ids);

-- 2. genuinely new rows. The partial unique index makes this safe to retry.
INSERT INTO raw_transaction
       (row_hash, ordinal, pub_date, first_seen_batch, last_seen_batch, ...)
VALUES (...)
ON CONFLICT(row_hash, ordinal) WHERE superseded_at IS NULL
DO UPDATE SET last_seen_batch = excluded.last_seen_batch;

-- 3. freshness bookkeeping for rows that did not change
UPDATE raw_transaction SET last_seen_batch = :batch_id
 WHERE id IN (:unchanged_ids);

UPDATE fetch_batch
   SET finished_at = :now, row_count = :n,
       rows_inserted = :i, rows_superseded = :s
 WHERE id = :batch_id;
COMMIT;
```

**On a quiet hour, steps 1–3 write nothing but `last_seen_batch`.** That is the
property that makes an hourly (or 15-minute) cron sustainable.

The `ON CONFLICT … DO UPDATE` targeting the partial unique index means the
insert is safe to retry after a crash. Combined with `BEGIN IMMEDIATE`, two
overlapping cron runs cannot corrupt the table — the second blocks, then finds
nothing to do.

#### 4.5.3 Leaf scoping — the easiest thing to get wrong

> **`:f` and `:t` are the LEAF window's bounds, never the parent's.** When
> `2020-03-01..15` caps and bisects into `03-01..08` and `03-09..15`, processing
> the first leaf must diff against only `03-01..08`. Diffing against the parent
> range would compute `to_supersede` over rows in `03-09..15` that the current
> fetch never covered, and mark a week of good data dead. Bind the diff, the
> supersede and the insert to the same leaf, in one transaction.

#### 4.5.4 Failure modes — why no defensive cleanup is needed

Two cases an implementer will worry about. Both are already safe; **do not add
a `DELETE` or a reconciliation pass to "fix" them.**

**A leaf fails mid-bisection.** Parent `[f,t]` caps and bisects into `L1` and
`L2`. `L1` commits; `L2`'s request fails. Because each leaf diffs only its own
range (§4.5.3), `L1`'s transaction never touched `L2`'s rows — they are still
live and unchanged. The next run re-fetches the parent, bisects again, and
processes `L2` normally. **A failed leaf leaves the previous data live and
self-heals on the next run.** Nothing is lost and nothing needs cleaning up.

**A window is truncated by the row cap.** If a single-day window still returns
1000 rows (`truncated = 1`, §4.2), FI withheld rows because of the cap — not
because they were withdrawn. Computing `to_supersede` there would kill live
rows on the basis of an incomplete answer.

> **Rule: when `truncated = 1`, insert only. Never supersede.** Skip step 1 of
> §4.5.2 entirely for that window and log it. The result is a window that may
> hold stale rows, which is strictly better than one missing real ones — and
> `truncated = 1` is already an acceptance-check failure (§4.6), so it will not
> pass unnoticed.

#### 4.5.5 Retention of superseded rows

Keep them. With a diff write path, a row is superseded only when FI actually
changed something — a few dozen rows a week, not 50,000 a day. The history is
small, and it is the only record of what the register said before a revision.

Do **not** add a pruning job. If the table ever does grow unexpectedly, that is
a signal the diff is broken, not that you need a cleaner.

### 4.6 Acceptance checks — phase 1

Implement these as `insyn doctor` assertions. They are measured facts, not
guesses:

Single-window fetches (these windows do not cap, so the counts are exact):

```
fetch(2016-07-01, 2016-07-31)  == 606
fetch(2017-01-01, 2017-01-31)  == 581
fetch(2026-03-01, 2026-03-07)  == 339
fetch(2024-01-08, 2024-01-14)  == 150
fetch(2024-05-15, 2024-05-21)  == 579
fetch(2020-03-13, 2020-03-13)  == 217     (busiest single day observed)
fetch(2015-01-01, 2015-01-31)  == 0       (register starts 2016-07)
```

Bisection recovery — **assert on the union, not on a sum of leaves.** Your
bisector picks its own split points, so per-leaf counts are implementation
detail; the recovered set is not:

```
ingest_window(2020-03-01, 2020-03-15)
  → SELECT COUNT(*) FROM raw_live
     WHERE substr(publiceringsdatum,1,10) BETWEEN '2020-03-01' AND '2020-03-15'
    must be 1558, and must exceed the 1000 a single query returns.
```

Plus:

- `SELECT COUNT(*) FROM fetch_batch WHERE truncated = 1` → **must be 0**.
- Re-running `insyn ingest recent --days 7` twice does not change
  `SELECT COUNT(*) FROM raw_live`.
- Full backfill lands **roughly 180,000 live rows** across 2016-07 → today
  (estimated from 22 sampled weekly windows: ~245/week in 2017 rising to
  ~360/week from 2021 onward). Treat 160k–200k as the plausible band; anything
  outside it means bisection is losing or duplicating data. Print the actual
  number and per-year breakdown at the end of the backfill.
- Every record parses to exactly 23 `csv.reader` fields (22 real + trailing
  empty). Anything else means the format changed — fail loudly, don't coerce.
- The 2020-03-18 BillerudKorsnäs row (§4.1.1) round-trips with
  `beskrivning_av_korrigering` containing a `;` and `status = 'Aktuell'`.
- `insyn ingest gaps --dry-run` reports **zero missing days** after a
  successful backfill (§4.7).

### 4.7 Coverage tracking — surviving an outage

**Yes, track fetched days explicitly.** It is the difference between "probably
fine" and "provably complete", and the cost is one small table.

Why `--days 7` is not enough on its own: it heals a one-week outage and nothing
longer. If the machine is off for a fortnight, or a holiday, or the cron
silently fails for a month, the hourly job's rolling window slides straight past
the hole and never looks back. Nothing in the system would notice — the site
keeps working, just missing three weeks of history.

#### Why a table and not derived from `fetch_batch`

`fetch_batch` already stores `window_from`/`window_to`, so coverage is in
principle derivable. In practice that means merging hundreds of overlapping
intervals, in SQL, on every check. A day-grained table makes the question a
single anti-join, and it is trivially cheap: ~3,700 rows for the whole decade.

#### Writing it

After each **leaf** window `[f, t]` commits (§4.5.2), upsert one row per
calendar day in that range — **including days with zero reports**:

```sql
INSERT INTO coverage_day (pub_date, first_fetched_at, last_fetched_at,
                          fetch_count, row_count)
VALUES (:day, :now, :now, 1, :rows_for_that_day)
ON CONFLICT(pub_date) DO UPDATE SET
  last_fetched_at = :now,
  fetch_count     = fetch_count + 1,
  row_count       = excluded.row_count;
```

> **A day with zero reports must still get a row.** That is what separates
> *"fetched, FI published nothing"* — every weekend, every Swedish public
> holiday — from *"never fetched"*. Without it, the gap finder would chase
> hundreds of phantom holes forever.
>
> **Never write `coverage_day` for a truncated window** (`truncated = 1`,
> §4.5.4). A capped window did not see everything, so claiming coverage for it
> would permanently mask real missing rows.

#### Finding and filling gaps

```sql
-- every calendar day from register start to yesterday with no coverage row
WITH RECURSIVE d(day) AS (
  SELECT '2016-07-01'
  UNION ALL SELECT date(day, '+1 day') FROM d
   WHERE day < date('now', '-1 day')
)
SELECT d.day FROM d
LEFT JOIN coverage_day c ON c.pub_date = d.day
 WHERE c.pub_date IS NULL
 ORDER BY d.day;
```

```
insyn ingest gaps [--dry-run] [--max-days 400]
```

Group consecutive missing days into contiguous ranges, then feed each range
through the **same** windowing and bisection code path as `backfill` — no new
fetch logic. `--dry-run` prints the ranges and the request count without
fetching. `--max-days` caps a single run so a long outage doesn't turn into a
surprise 40-minute job.

Run it **nightly, before** the 90-day re-scan. On a healthy system it finds
nothing and costs one query.

#### Also surface it

Put the coverage summary on `/api/v1/meta`: first and last covered day, count
of missing days, and the longest gap. A visitor should be able to tell that the
site is missing last week, and so should you.

---
---

## Acceptance checks
### 11.1 Ingest
The row counts in §4.6, plus `truncated = 0` and idempotent re-run.
### 11.1.1 The diff write path (§4.5) — the check that protects the cron

This is the one to get right, because a broken diff degrades silently: the site
keeps working while the table grows without bound.

```
run `insyn ingest recent --days 7`         → note rows_inserted = A
run it again immediately, no data changed  → rows_inserted MUST be 0
                                             rows_superseded MUST be 0
                                             COUNT(raw_transaction) unchanged
run it 10 more times                       → still 0 / 0 / unchanged
```

Then verify the duplicate-prevention index actually holds by attempting to
insert a known-live `(row_hash, ordinal)` directly — it must raise
`sqlite3.IntegrityError`. If it doesn't, the partial unique index in §4.4 is
missing or wrong, and every guarantee in §4.5 is void.

Also simulate a **mid-bisection failure** (§4.5.4): kill the process after the
first leaf commits, then compare `COUNT(*) FROM raw_live` for the full parent
range against its value before the run. It must be **unchanged, never reduced**
— a drop means the diff is scoped to the parent instead of the leaf.

Ongoing monitoring: `SELECT SUM(rows_inserted) FROM fetch_batch WHERE
started_at > date('now','-1 day')` should be roughly the number of reports FI
actually published that day (order of 50–150 on a business day), not thousands.
### 11.2.2 Coverage (§4.7)

- `insyn ingest gaps --dry-run` reports **zero missing days** after backfill.
- Days FI published nothing (every weekend, every Swedish public holiday) have
  a `coverage_day` row with `row_count = 0` — **not** a missing row. Spot-check
  a known holiday: `SELECT * FROM coverage_day WHERE pub_date = '2026-06-06'`
  (nationaldagen) must return a row.
- No `coverage_day` row exists for any window where `truncated = 1`.
- Simulate an outage: delete a fortnight of `coverage_day` rows, run
  `insyn ingest gaps`, confirm the rows come back and `raw_live` is unchanged
  (the diff means re-fetching the same data writes nothing).
