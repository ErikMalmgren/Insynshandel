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

- **Phases 0–6 done** (see git log). `insyn build` = normalize→refdata→aggregate;
  `insyn export-static --out dist/` dumps the read API to JSON; `insyn serve`
  runs the FastAPI wrapper. `api/schemas.py` (Pydantic) + `api/reads.py` are the
  **shared layer** — both the export and the routes call the same function, and
  `tests/test_api.py` diffs every route against its `dist/` file so they can't
  drift. 129 hermetic tests.
- **Phase 4 notes:** routes are `api/app.py` (`create_app`, CORS from
  `config.API_CORS_ORIGINS`, never `*`) + `api/routes.py`. Query params are
  `extra="forbid"` Pydantic models, so `/leaderboard?limit=` and
  `/transactions?pdmr=` are 422, not silent-ignore (§8.1/§8.2). `/leaderboard`
  is `?period=` (one of `AGG_PERIODS`) — **not** the plan header's `?from=&to=`;
  arbitrary windows would need a live-aggregation read with no static twin,
  breaking parity. `/companies?q=` is a filter only (no `limit`/`offset`).
  `/transactions` server-sorts on the `reads._TX_ORDER` allow-list with `raw_id`
  as tiebreaker; its rows carry **no** `pdmr`/`position` (that grant is
  company-scoped, §8.1). No frontend code lives here — the JSON shape is the
  wiring (plan §1.1).
- **Next: deployment** (`plan/deploy.md`) — Docker/compose, reverse proxy,
  Litestream, cron. Dockerfile CMD is `uvicorn insynshandel.api.app:app`.
- **Not run yet:**
  - `insyn ingest backfill` (~330 req / 30-45 min) — the user runs it
    **overnight after the final phase**; remind them, don't start it. Resumable.
  - `refdata figi` / `marketcaps` (yfinance, can hang) — built + unit-tested,
    run at scale post-backfill. `build --no-figi --no-marketcaps` skips them.
    FX backfill (4 req) is already done on the working DB.
  - §11.3 parity vs `python script.py` — last gate before deleting the 4 legacy
    root scripts (their inputs are in `data/cache/legacy/`).
- Gotchas:
  - **Invariants 9 & 12 overstate LEI coverage** (`2016-07` 79% no-LEI, modern
    0%). `aggregate` excludes blank-`lei` rows as `exclude_reason='no_lei'`
    (filter rule 0). Post-backfill follow-up: `issuer_name→lei` seed map +
    re-aggregate (⚠ box in `plan/03-aggregate.md` §6.5).
  - Invariant 10 (`.strip()`) is phase 2+ only (phase 1 stores verbatim —
    stripping changes `row_hash`).
  - `raw_live` / `market_cap_current` are `SELECT`-based views — a migration
    altering `raw_transaction` must `DROP`+recreate `raw_live`.
  - §4.6's `2026-03-01..07` count drifts down as FI withdraws reports; `doctor`
    splits FROZEN (exact) / RECENT (`<=`).
  - `data/cache/legacy/` is gitignored & absent from a fresh clone.
  - `ticker_override.csv`: `symbol='?'` = worklist (loader skips); `provider`
    PK is `''` not `NULL`.
  - `.strip()` from `data/seed/*.csv` comment lines (`#`) — `load_seeds` skips them.
  - Bash tool runs zsh; `uv` at `~/.local/bin/uv`; zsh aborts a command on a
    non-matching glob (use `find … -delete`, not `rm glob*`).

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
