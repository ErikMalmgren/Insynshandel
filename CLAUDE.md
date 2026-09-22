# Insynshandel

Swedish insider-trading data (Finansinspektionen's *insynsregistret*) ingested
into SQLite, aggregated per company, and published as static JSON plus a
read-only API.

**Python 3.14, pinned. Use `uv`, never `pip`.** Run everything as
`uv run insyn <cmd>`.

## Working on this project

The implementation plan is split by phase so only the relevant part needs
loading. **Read the one file for the phase you are on — not the whole plan.**

| Phase | File | Build order |
| --- | --- | --- |
| 0 · repo hygiene, toolchain | `plan/00-hygiene.md` | 1st |
| 1 · fetch → raw store | `plan/01-ingest.md` | 2nd |
| 2 · normalize | `plan/02-normalize.md` | 3rd |
| 3 · classify & aggregate | `plan/03-aggregate.md` | 4th |
| 5 · reference data | `plan/05-refdata.md` | with phase 3 |
| 6 · static JSON export | `plan/06-static.md` | 5th |
| 4 · API | `plan/04-api.md` | 6th |
| 7 · frontend (`insyn.malmgren.dev`) | `plan/07-frontend.md` | 7th |
| — deployment | `plan/deploy.md` | last |
| — testing, `insyn doctor` | `plan/testing.md` | throughout |
| — rationale, rejected options | `plan/background.md` | read once |

`IMPLEMENTATION_PLAN.md` is the index and maps every `§N.M` reference to its
file. Section numbers are stable across the split — `§4.5` means the same thing
everywhere.

**Do not start a phase until the previous phase's acceptance checks pass.**
Each phase file ends with its own checks. State lives in the database and the
repo, not in conversation history, so a fresh session picks up by running
`uv run insyn doctor` and reading one phase file.

## Invariants

These hold in every phase. Each one, if violated, fails **silently** — the site
keeps working while the data goes wrong.

1. **Parse the FI CSV with `csv.reader(f, delimiter=';')`, never
   `line.split(';')`.** The export is UTF-16LE without BOM and uses RFC 4180
   quoting; fields contain embedded semicolons. Assert `len(row) == 23`.

2. **Scope the ingest diff to the LEAF window, never the parent.** When a
   window caps and bisects, diffing against the parent range supersedes rows the
   current fetch never covered — silently deleting good data. (§4.5.3)

3. **Never supersede rows for a truncated window.** `truncated = 1` means FI
   withheld rows because of the 1000-row cap, not because they were withdrawn.
   Insert only. (§4.5.4)

4. **Re-running an ingest with no new data must write nothing.** `rows_inserted`
   and `rows_superseded` both 0. This is what makes the hourly cron sustainable.
   (§4.5)

5. **`normalize` is a full rebuild** — `DELETE FROM transaction_norm`, then
   re-derive. Never incremental. (§5.0)

6. **Never run `normalize` without `aggregate`.** Use `insyn build`. Alone,
   `normalize` leaves every classification column `NULL`. (§5.0)

7. **An unmapped `Karaktär` value fails the build.** Never `else: continue`.
   Add a row to `data/seed/nature_map.csv` and re-run. (§6.1)

8. **Only `Förvärv` (+1) and `Avyttring` (−1) count.** Everything else is
   excluded with a recorded `exclude_reason`. No row is ever uncounted without
   a reason. (§6.1)

9. **There is no GLEIF step.** The FI export carries `LEI-kod` on 100% of rows
   and `ISIN` on 95% — the issuer↔instrument link is already in the data. Never
   add a GLEIF fetcher. (§3.1)

10. **`.strip()` every field, ISIN especially.** The export contains the same
    ISIN with and without a trailing space. (§5.2)

11. **Read market caps only through the `market_cap_current` view.** A bare
    `FROM market_cap` bypasses the guard that turns the `0` sentinel into SQL
    `NULL`. Missing market cap serializes as `null`, never `0`. (§6.4)

12. **Aggregate on `lei`, never on `isin`.** One company has many ISINs — share
    classes, BTAs, historical ones. Keying on ISIN splits a company across the
    leaderboard. Display name = most frequent within the last 12 months, because
    companies rename and the LEI persists. (§6.5, §7.2)

13. **All money is SEK**, converted at the row's own **transaction-date**
    Riksbank rate, forward-filled across weekends. No per-currency buckets.
    (§6.3)

14. **Aggregate on `transaction_date`, never `published_date`.** Publication
    date only windows the fetch. (§6.5)

15. **No person index, ever** — no `/persons` route, no `?pdmr=` filter, no
    client-side name search. Names appear only on a company's transaction list.
    (§8.1)

16. **A market-cap provider failure writes no row at all** — not a `0` row. The
    previous snapshot stays latest. A degraded provider never aborts
    `insyn build`. (§7.4.3)

17. **Keep the database on local disk.** SQLite WAL corrupts on NFS/SMB.
    (§10.2.2)

## Conventions

- Plain `sqlite3` with hand-written SQL in `src/insynshandel/migrations/*.sql`.
  No ORM.
- The API opens SQLite **read-only**; only the ingest writes.
- Be a polite client: 3 s between requests to marknadssok.fi.se, descriptive
  `User-Agent`, backoff on 5xx and connection resets.
- Swedish domain terms stay in Swedish in the data layer (`karaktar`,
  `emittent`, `anmalningsskyldig`); code and comments are in English.
- Money is a plain number in JSON. Never format it server-side.

## Phase status

- **Whole plan implemented** (see git log). Pipeline: `insyn build` =
  normalize→refdata→aggregate. Read surface: `api/reads.py` (+ `api/schemas.py`
  Pydantic) is the **shared layer** — `export_static.py` and the FastAPI routes
  (`api/app.py`, `api/routes.py`, `insyn serve`) call the same functions;
  `tests/test_api.py` diffs every route against its `dist/` file. 148 hermetic
  tests.
- **Phase 4 deviations from the plan header** (deliberate, also in the commit):
  `/leaderboard` is `?period=` not `?from=&to=` (arbitrary windows have no
  static twin → break parity); `/companies?q=` is a filter, no `limit`/`offset`
  (§8.2); the global `/transactions` feed omits `pdmr`/`position` (that §8.1
  grant is company-scoped). Query params are `extra="forbid"` models, so an
  unknown/off-list param is a 422, not a silent ignore.
- **Deployment** (`plan/deploy.md`): `Dockerfile` (+`.dockerignore`),
  `docker-compose.yml`, `.env.example`, `deploy/` (Caddyfile, systemd
  unit+timer, `ingest.sh`, runbook), `.github/workflows/` — `ci.yml` (beyond
  the plan) and `ingest.yml`. **`ingest.yml` runs on its cron and succeeds**
  (~10 min; restores + re-uploads the `db-latest` release asset), so the
  *cloud* DB is current even when the local one is stale — a local doctor
  "N missing days" FAIL is just an idle laptop. Its `deploy` job publishes
  `site/` (§16.2) to Pages → `insyn.malmgren.dev` (2026-09-22; live once Pages
  + Cloudflare DNS are set up by hand — `deploy/README.md`). Docker uses
  `INSYN_DATA_DIR=/data`, **not** the plan sketch's `INSYN_DB`. Docker build
  **unverified** (no docker in this env); the API path is not deployed.
- **Phase 7 frontend built** (2026-09-09, `frontend/`) — the nine files of
  §16.8, no deps. As-built notes + how it was verified: `plan/07-frontend.md`
  §16.9.
- **Next: nothing left in the plan** — only the follow-ups below.
- **`insyn ingest backfill` is DONE** (2026-09-09): 167,477 live rows,
  2016-07-04..2026-09-08, 3722 coverage days — inside doctor's 160k-200k band.
- **Follow-ups, worst first** (2026-09-09):
  1. **LEI collisions** — the only one that makes the site *wrong* rather than
     incomplete (see the two gotchas below). `issuer_alias` is wired but
     **empty**, and keyed `alias_lei`→`canonical_lei` it can only fold many
     LEIs into one — it cannot split one bad LEI across 253 issuers or recover
     a blank one from a name. Needs an `issuer_name`-keyed table + a join
     change: schema work, not a seed-file edit. ⚠ box in
     `plan/03-aggregate.md` §6.5.
  2. §11.3 parity vs `python script.py`, then delete the 4 legacy root scripts
     (inputs in `data/cache/legacy/`). Runnable now.
  3. `refdata marketcaps` — 419 caps over 534 addressable, so 115 missing.
     Snapshots are dated + append-only, so a re-run is cheap.
  4. ~~Wire the frontend deploy~~ — workflow side done 2026-09-22; the Pages +
     DNS clicks are the user's (checklist in `deploy/README.md`).
  - ~~`refdata figi`~~ / ~~`refdata fx --backfill`~~ **both done** — see
    `plan/05-refdata.md` §7.5 for what they returned and the ⚠ 90-day retry
    freeze on figi's 5222 NULL tickers.
- Gotchas:
  - **Invariants 9 & 12 overstate LEI coverage** (`2016-07` 79% no-LEI, modern
    0%). `aggregate` excludes blank-`lei` rows as `exclude_reason='no_lei'`
    (filter rule 0). Post-backfill follow-up: `issuer_name→lei` seed map +
    re-aggregate (⚠ box in `plan/03-aggregate.md` §6.5).
  - **A wrong LEI is worse than a blank one, and FI ships plenty.**
    `549300O897ZC5H7CY412` is on 992 `raw_live` rows across **253 unrelated
    issuers** (Tobii, Mekonomen, Kjell Group, Hifab …), each with its own
    correct ISIN; 594 LEIs carry >5 distinct `emittent` names, and Tobii alone
    has 4 LEI values. Verbatim from FI, not a normalize bug — but invariant 12
    keys on `lei`, so those 253 collapse into one leaderboard row. Same
    follow-up as above: the seed map has to *override* bad LEIs, not only fill
    blanks. (Unrelated to the 218 outliers — checked, 87% vs an 81% baseline.)
  - Invariant 10 (`.strip()`) is phase 2+ only (phase 1 stores verbatim —
    stripping changes `row_hash`).
  - **`implausible_unit_price` (§6.2.1) is what keeps the leaderboard sane.**
    FI writes a total into `Pris` on ~181 rows; unguarded they carried 99.99%
    of the counted SEK total. It needs no market cap, which matters because
    only ~20% of issuers have one — the outlier rule protects nobody else.
    Two arms (equity unit price > 10k SEK; `volume == price` above 1 bn), both
    required. `EQUITY_INSTRUMENT_TYPES` is a hand-derived list: a type FI adds
    later is never flagged until someone adds it.
  - Two FX exclude reasons, and the split is load-bearing: `no_fx_series`
    (currency in `config.FX_NO_SERIES` — no SWEA series exists) is tolerated by
    `doctor`; `no_fx_rate` (just not fetched) is a hard failure. A currency in
    neither tuple falls to `no_fx_rate` **on purpose**, so a new one surfaces.
  - `raw_live` / `market_cap_current` are `SELECT`-based views — a migration
    altering `raw_transaction` must `DROP`+recreate `raw_live`.
  - §4.6's `2026-03-01..07` count drifts down as FI withdraws reports; `doctor`
    splits FROZEN (exact) / RECENT (`<=`).
  - A **negative `pct_of_mcap` is normal** (net seller) — the §9 guard that
    flagged it was dropped 2026-09-09. What remains is `market_cap == 0`, which
    can only mean a query bypassed `market_cap_current` (inv 11). The sentinel
    is live: `marketcaps` writes `max(mc_sek, 0.0)` when FX lookup fails.
  - `refdata fx`/`figi`/`marketcaps` print a stderr progress meter (`_Progress`
    in `cli.py`); `--quiet` silences it, and piping through `tail` swallows it.
    yfinance's own chatter scrolls it away.
  - `data/cache/legacy/` is gitignored & absent from a fresh clone.
  - `ticker_override.csv`: `symbol='?'` = worklist (loader skips); `provider`
    PK is `''` not `NULL`.
  - `.strip()` from `data/seed/*.csv` comment lines (`#`) — `load_seeds` skips them.
  - Bash tool runs zsh; `uv` at `~/.local/bin/uv`; zsh aborts a command on a
    non-matching glob (use `find … -delete`, not `rm glob*`). An **unquoted**
    heredoc delimiter expands `$…` and backticks inside — always `<<'EOF'`.
  - **`pct_of_mcap == 0` is normal** (bought exactly as much as sold) — the
    invariant-11 sentinel is about `market_cap == 0` only. `mcapCell` warns,
    `pctCell` deliberately does not. 151 of 251 leaderboard rows show `—`.

## Commands

```
uv run insyn db migrate
uv run insyn ingest backfill              # once, ~330 requests, 30-45 min
uv run insyn ingest recent --days 7       # hourly
uv run insyn ingest gaps --dry-run        # heal an outage
uv run insyn build                        # normalize -> refdata -> aggregate
uv run insyn export-static --out dist/
uv run insyn serve
uv run insyn doctor                       # acceptance checks
```

## Maintaining this file

Append what you learn as the code appears — real module paths, commands that
turned out to matter, gotchas discovered in practice. Do **not** move plan
detail here: this file is loaded on every turn, so it stays short. If it grows
past roughly 150 lines, something belongs in a phase file instead.
