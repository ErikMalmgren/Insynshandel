# Insynshandel

Every insider transaction reported to Sweden's financial regulator
(Finansinspektionen's *insynsregistret*) since July 2016, aggregated per
company. A pipeline turns the regulator's paginated CSV export into one SQLite
file, then publishes it as static JSON for a site on GitHub Pages.

<!-- TODO after first deploy: screenshot + live link, above the fold. -->
**Live:** _not deployed yet_ · **Plan:** [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)

## Run it

Python 3.14, pinned. Uses [`uv`](https://docs.astral.sh/uv/) — never `pip`.

```bash
uv sync
uv run insyn db migrate
uv run insyn ingest backfill      # full history, ~330 requests, 30–45 min
uv run insyn build                # normalize → reference data → aggregate
uv run insyn export-static --out dist/
```

`insyn ingest recent --days 7` is the incremental run; `insyn doctor` is the
acceptance-check suite. Deployment (GitHub Actions → Pages) is in
[`deploy/`](deploy/).

## The data problems, and what the pipeline does about them

This is most of the work. The register is public but awkward.

- **The export silently truncates.** Any query returns at most 1000 rows, newest
  first, with no error and no indication that older rows were dropped. The
  ingest fetches in date windows and **bisects** any window that hits the cap,
  recursing until every slice fits — so a busy month is fetched as many small
  ranges rather than one truncated one. A window that still caps at a single day
  is flagged, never silently accepted.
- **It is UTF-16LE without a BOM, with RFC 4180 quoting** and fields that
  contain embedded semicolons and newlines. It is parsed with a real CSV reader,
  not `split(';')`, and every row is asserted to have 23 fields.
- **The register contains uncorrected filing errors** — wrong units, misplaced
  decimals, a price entered as a total. A transaction whose value exceeds its
  issuer's entire market capitalisation is **excluded from the totals** and
  listed on a data-quality page instead of quietly skewing a company's number.
  Where no market cap is known the transaction is marked *unverifiable* and
  still counted — it is never silently dropped.
- **`Karaktär` (transaction nature) has 29 distinct values.** Two of them —
  `Förvärv` and `Avyttring` — mean "bought" and "sold". The rest (subscriptions,
  allotments, pledges, gifts, internal transfers, …) are each excluded **for a
  recorded reason**; an unmapped value fails the build rather than falling
  through.
- **Every amount is converted to SEK** at the Riksbank rate for that
  transaction's own date (forward-filled across weekends). No per-currency
  buckets.
- **Companies are keyed on their LEI, not their name or ISIN** — names change,
  one company has many ISINs (share classes, historical listings). The displayed
  name is the most frequent one from the last 12 months.

## What this deliberately does not have

- **No person index.** The names in the register are private individuals, and
  republishing them in a more searchable form than the regulator does is a
  different act under GDPR. Names appear only on a company's own transaction
  list — there is no person search, no per-person profile, no `?pdmr=` filter.
- **No message broker, no Kubernetes.** The ingest is a scheduled batch over a
  known date range — ~330 rate-limited requests — not a stream of work items.
  The runtime is one process and one SQLite file; there is nothing to
  horizontally scale. See §0.1 of the plan.
- **No ORM, no GLEIF fetch** — the FI export already carries the LEI on every
  row and the ISIN on 95%, so the issuer↔instrument link is in the data.

## Attribution

Transaction data from [Finansinspektionen](https://www.fi.se/); FX rates from
[Sveriges Riksbank](https://www.riksbank.se/). `meta.json` carries the
last-updated timestamp and coverage span.
