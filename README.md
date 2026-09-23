# Insynshandel

Every insider transaction reported to Sweden's financial regulator
(Finansinspektionen's *insynsregistret*, under MAR article 19) since July 2016,
converted to SEK and aggregated per company. A Python pipeline turns the
regulator's CSV export into one SQLite file, exports it as static JSON, and a
small static frontend on GitHub Pages renders it.

**Live:** [insyn.malmgren.dev](https://insyn.malmgren.dev) ·
**Methodology:** [about page](https://insyn.malmgren.dev/about.html)

Not investment advice, and not an official publication. The authoritative data
is Finansinspektionen's.

## How it works

```
FI marknadssok ──► ingest ──► raw_transaction   (append-only, source-faithful)
                                   │
                              normalize ──► transaction_norm
                                   │
   Riksbank SWEA ──► refdata fx    │
   OpenFIGI      ──► refdata figi  │
   yfinance/CSV  ──► refdata marketcaps
                                   │
                              aggregate ──► agg_company_period
                                   │
                           export-static ──► dist/*.json ──► frontend/ (Pages)
```

| Stage | What it does |
| --- | --- |
| `ingest` | Fetches the FI export by publication-date window into `raw_transaction`. Rows FI stops returning are marked superseded, never deleted. |
| `normalize` | Rebuilds `transaction_norm` from the live raw rows: parses dates, numbers and booleans. |
| `refdata` | FX rates from the Riksbank, ISIN → ticker from OpenFIGI, market caps from Yahoo (with a manual CSV fallback). |
| `aggregate` | Classifies every row (counted, or excluded with a reason), then sums buys and sells per company for the `30d`, `90d`, `365d`, `ytd` and `all` windows. |
| `export-static` | Writes the JSON the frontend reads. |

`insyn build` runs normalize → refdata → aggregate in that order.

## The data problems, and what the pipeline does about them

This is most of the work. The register is public but awkward.

- **The export silently truncates.** Any query returns at most 1000 rows,
  newest first, with no error and no indication that older rows were dropped.
  The ingest fetches in 14-day windows and **bisects** any window that hits the
  cap until every slice fits. A window that still caps at a single day is
  flagged, never silently accepted.
- **It is UTF-16LE without a BOM, with RFC 4180 quoting** and fields that
  contain embedded semicolons and newlines. It is parsed with a real CSV reader,
  and every row is asserted to have 23 fields.
- **Reports get revised and withdrawn.** FI keeps returning `Reviderad` and
  `Makulerad` rows, so only the `Aktuell` version of each report is counted.
- **`Karaktär` (transaction nature) has dozens of values.** Only `Förvärv`
  (+1) and `Avyttring` (−1) move the totals. Everything else (subscriptions,
  allotments, pledges, gifts, lending, …) is excluded **for a recorded reason**
  via [`data/seed/nature_map.csv`](data/seed/nature_map.csv). An unmapped value
  fails the build rather than falling through.
- **Totals sometimes land in the price column.** FI occasionally writes a total
  amount into `Pris`, so volume × price squares it (one 2018 row reads as 6.7
  quadrillion SEK). An equity unit price above 10,000 SEK, or the same amount
  above 1 bn SEK written into both columns, is excluded as
  `implausible_unit_price`.
- **Every amount is converted to SEK** at the Riksbank rate for the
  transaction's own date, forward-filled across weekends and holidays. A few
  currencies have no Riksbank series at all. Rows in those are excluded as
  `no_fx_series` rather than guessed at.
- **Companies are keyed on LEI, not name or ISIN.** Names change, and one
  company has many ISINs (share classes, rights, historical listings). The
  displayed name is the one used most often in the last 12 months.
- **Market caps are a sanity check, not a filter.** A transaction worth more
  than its issuer's whole market cap is excluded as `outlier`. A company with
  no known market cap is marked *unverifiable* and still counted.

Exclusions are evaluated in a fixed order, and the first one that matches is
recorded:

```
no_lei → not_current → nature_not_counted → volume_unit → unparseable_number
       → no_fx_series / no_fx_rate → implausible_unit_price → outlier → counted
```

### Known limitations

- FI's LEI column is not clean. Many rows before 2018 carry no LEI and are
  excluded as `no_lei`. Some LEIs are attached to many unrelated issuers, which
  merges them into one company here.
- Market caps are missing for most issuers, so most totals are *unverifiable*.

## What this deliberately does not have

**No person index.** The names in the register belong to private individuals,
and republishing them in a more searchable form than the regulator does is a
different act under GDPR. Names appear only on a company's own transaction
list. There is no person page, no name search, and no way to sort or filter by
person. `companies.json` carries no names of people at all.

## Running it locally

Requires Python 3.14 and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync
```

**Fast path: download the current database.** The ingest workflow publishes
the database as the rolling `db-latest` release asset:

```bash
gh release download db-latest --pattern insynshandel.db --dir data --clobber
```

**Slow path: build it from scratch.**

```bash
uv run insyn db migrate
uv run insyn ingest backfill      # full history from 2016-07-01, ~30–45 min
uv run insyn build                # normalize → refdata → aggregate
```

The backfill is rate-limited out of politeness to FI (3 s between requests,
600 requests per run). If it reports that the request budget was hit, run it
again to continue. Unauthenticated OpenFIGI and yfinance lookups also take a
while on the first `build`. Pass `--no-figi` / `--no-marketcaps` to skip them.

To bring a local copy up to date, download `db-latest` again. To fetch the
new data yourself instead:

```bash
uv run insyn ingest gaps          # any days never fetched
uv run insyn ingest recent        # re-scan the last 7 days for revisions
uv run insyn build
```

### Previewing the site

The frontend reads its JSON from `./data` next to the pages. Assemble the site
the same way the workflow does:

```bash
uv run insyn export-static --out dist
mkdir -p site/data
cp -r frontend/. site/
cp -r dist/. site/data/
python -m http.server -d site 8000
```

Then open <http://localhost:8000>.

> **Never point `--out` at `frontend/` or any directory you care about.**
> `export-static` deletes its output directory before writing.

The export contains:

```
meta.json                        coverage span, missing days, row counts, last ingest
data-quality.json                excluded outliers, ISINs filed under >1 LEI
leaderboard-{30d,90d,365d,all}.json
companies.json                   lei, name, ticker
company/{lei}.json               per-company totals + recent transactions
```

## CLI

All commands run as `uv run insyn …`.

| Command | Purpose |
| --- | --- |
| `db migrate` | Create or upgrade the SQLite schema. |
| `ingest backfill [--from YYYY-MM-DD] [--to YYYY-MM-DD]` | One-off full-history fetch. |
| `ingest recent [--days N]` | Incremental fetch of the last N publication days (default 7). |
| `ingest gaps [--dry-run] [--max-days N]` | Find and re-fetch days that were never fetched. |
| `normalize` | Rebuild `transaction_norm` from raw rows. |
| `refdata {fx,figi,marketcaps}… [--backfill] [--limit N] [--quiet]` | Fetch reference data. `--backfill` fetches FX from 2016-07-01; `--limit` caps OpenFIGI lookups. |
| `aggregate` | Classify rows and rebuild the per-company aggregates. |
| `build [--fx-backfill] [--figi-limit N] [--no-figi] [--no-marketcaps] [--quiet]` | `normalize` → `refdata` → `aggregate`. |
| `export-static [--out DIR]` | Write the static JSON (default `dist/`). |
| `doctor [--network]` | Acceptance checks. Exits non-zero on failure. `--network` also re-fetches a few frozen FI windows and checks the exact row counts. |

## Configuration

All optional.

| Variable | Default | Purpose |
| --- | --- | --- |
| `INSYN_DATA_DIR` | `data/` | Where the database lives. |
| `INSYN_DB_PATH` | `$INSYN_DATA_DIR/insynshandel.db` | Database file. Keep it on local disk: SQLite WAL mode is unsafe on NFS/SMB. |
| `INSYN_REQUEST_SPACING_S` | `3.0` | Delay between FI requests. |
| `INSYN_REQUEST_TIMEOUT_S` | `60` | FI request timeout. |
| `INSYN_MAX_REQUESTS_PER_RUN` | `600` | FI request budget per run. |
| `INSYN_USER_AGENT` | `Insynshandel/0.1 (…)` | User agent sent to FI. |
| `INSYN_RIKSBANK_SPACING_S` | `25` | Delay between Riksbank requests. |
| `INSYN_MARKETCAP_PROVIDERS` | `yahoo,manual` | Market-cap providers, tried in order. |
| `OPENFIGI_API_KEY` | – | Raises the OpenFIGI rate limit (2.4 s → 0.3 s spacing). |

Hand-maintained reference data (ticker overrides, the `Karaktär` map, manual
market caps, issuer aliases) lives in [`data/seed/`](data/seed/README.md).

## Deployment

There is no server. Everything runs in GitHub Actions and is served from
GitHub Pages.

- [`ci.yml`](.github/workflows/ci.yml) runs on every push and PR, with no
  network access: `ruff`, `pytest`, and `doctor` against a freshly migrated
  empty database.
- [`ingest.yml`](.github/workflows/ingest.yml) runs hourly 06:00–20:00 CET on
  weekdays (`ingest recent --days 7`) and nightly (`ingest gaps` plus a 90-day
  re-scan). Each run restores the database from the `db-latest` release, then
  runs `build`, `export-static` and `doctor`. It then re-uploads the database
  and deploys the site to Pages. A failing `doctor` stops the deploy.

To set it up on a fork, set **Settings → Pages → Source** to *GitHub Actions*
and configure the custom domain there. Add `OPENFIGI_API_KEY` as a repository
secret if you have one. Seed the database by backfilling locally and
uploading the result:

```bash
gh release create db-latest data/insynshandel.db \
  --title "Latest database snapshot" --notes "Rolling. Updated by the ingest workflow."
```

The workflow restores from that release on every run. It can start from an
empty database, but it does not run a backfill, and `doctor` fails a database
that spans the full history with missing days in between.

## Repository layout

```
src/insynshandel/
  cli.py              `insyn` entry point
  config.py           paths, constants, env vars
  db.py               connection, migrations, seed loading
  migrations/         SQL schema (raw → norm → reference → agg)
  sources/            FI, Riksbank, OpenFIGI and market-cap clients
  pipeline/           ingest, normalize, fx, reference, aggregate
  reads.py            read layer used by the export
  schemas.py          Pydantic models for the exported JSON
  export_static.py    writes dist/
  doctor.py           acceptance checks
frontend/             static HTML/CSS/JS site
data/seed/            committed reference CSVs
tests/                pytest suite (offline, uses fixtures)
```

## Development

```bash
uv run ruff check .
uv run pytest
```

## Attribution

Transaction data from [Finansinspektionen](https://www.fi.se/)
([marknadssok.fi.se](https://marknadssok.fi.se/)). FX rates from
[Sveriges Riksbank](https://www.riksbank.se/). Instrument identifiers from
[OpenFIGI](https://www.openfigi.com/).
