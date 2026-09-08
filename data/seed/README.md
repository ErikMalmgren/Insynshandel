# `data/seed/` — committed reference data

Small, hand-maintained CSVs that the pipeline reads but never writes. Each is
loaded into a table during `insyn build` / `insyn db migrate`.

| File | Loaded into | Added in | Plan |
| --- | --- | --- | --- |
| `ticker_override.csv` | `ticker_override` | Phase 0 (seeded), used Phase 5 | §7.3 |
| `nature_map.csv` | `nature_map` | Phase 3 | §6.1 |
| `market_cap_manual.csv` | ManualProvider input | Phase 5 | §7.4 |
| `issuer_alias.csv` | `issuer_alias` | Phase 3/5 | §7.2.3 |

## `ticker_override.csv`

Columns: `lei,provider,symbol,note`.

- `provider` — `yahoo`, `borsdata`, … or **blank** = applies to every provider.
- `symbol` — one of:
  - a **real ticker** (`ABC-B.ST`) → loaded as a `yahoo`/all-provider override;
  - **`?`** → unresolved worklist item; **the loader skips it** (stays here as a
    to-do, contributes nothing to the DB);
  - **`''` (empty)** with a `note` → "checked, this company genuinely has no
    listed symbol" (§7.4.2) — loaded, stops anyone re-investigating.
- `note` — free text.

### Seeding note

Seeded from `data/cache/legacy/failed_isins.csv` — 61 LEIs whose ISIN OpenFIGI's
XSTO lookup could not resolve as of the legacy run, all marked `symbol = ?`.
**"OpenFIGI failed" is not "this company has no symbol"** — most are real listed
companies the lookup just missed. `db.load_seeds()` skips every `?` row.

Re-measure against the real backfill before hand-filling: after `insyn ingest
backfill` + `insyn refdata figi`, the set of genuinely unresolvable LEIs may be
much smaller than 61. Fill a real ticker in, or set `symbol` empty with a note
if the company truly has none.
