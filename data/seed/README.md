# `data/seed/` — committed reference data

Small, hand-maintained CSVs that the pipeline reads but never writes. Each is
loaded into a table during `insyn build` / `insyn db migrate`.

| File | Loaded into |
| --- | --- |
| `ticker_override.csv` | `ticker_override` |
| `nature_map.csv` | `nature_map` |
| `position_group.csv` | `position_group` |
| `market_cap_manual.csv` | ManualProvider input |
| `issuer_alias.csv` | `issuer_alias` |

## `ticker_override.csv`

Columns: `lei,provider,symbol,note`.

- `provider` — `yahoo`, `borsdata`, … or **blank** = applies to every provider.
- `symbol` — one of:
  - a **real ticker** (`ABC-B.ST`) → loaded as a `yahoo`/all-provider override;
  - **`?`** → unresolved worklist item; **the loader skips it** (stays here as a
    to-do, contributes nothing to the DB);
  - **`''` (empty)** with a `note` → "checked, this company genuinely has no
    listed symbol" — loaded, stops anyone re-investigating.
- `note` — free text.

A loaded row is the **first** thing a market-cap provider consults. It wins over
Yahoo's own ISIN search and over the fallback formatter. The symbol is in the
provider's own form, for example `SBB-B.ST` for Yahoo. It is never written into
`company.raw_ticker`, which stays OpenFIGI's verbatim ticker. A blank-provider row
with a symbol sets `company.ticker_source = 'manual'`.

### Seeding note

The worklist was regenerated on 2026-09-28, after the resolver-v2 re-lookup (all
equity listings, with Nasdaq Stockholm, First North, Spotlight or NGM as the home
venue). It is taken from the local DB, whose data ends on 2026-09-08. It lists
every company that traded in the last 12 months and is still unresolved. All
rows are `symbol = ?`, and each note gives the likely reason:

- **75 rows with a blank provider**: no ticker at all. The causes are delisted
  or renamed companies, foreign ISINs (a Stockholm SDB has its own ISIN), and
  issuers with only bond ISINs.
- **18 `yahoo` rows**: OpenFIGI has a ticker, but Yahoo's ISIN search returns no
  `.ST` symbol, so the market cap falls back to the formatter's guess.

The earlier seed held 61 rows from the v1 `micCode: XSTO` lookup. 54 of those now
resolve automatically. **"OpenFIGI failed" is still not "this company has no
symbol"**, so check each row before filling it in. Put in a real symbol, or leave
`symbol` empty with a note when the company truly has none. `db.load_seeds()`
skips every `?` row.

## `nature_map.csv`

Columns: `karaktar,sign,direction,counted,category,note`. An unmapped Karaktär
fails the build.

- `counted` / `sign` — the default board: only `counted = 1` rows reach
  `agg_company_period`, and `sign` is `+1`/`-1` for those and `0` otherwise.
- `direction` — how the nature moves the holding (`+1`, `-1`, or `0` for none,
  e.g. a pledge), for every nature. The leaderboard's query builder uses it when
  an uncounted nature is picked; a `0` row counts in tx but neither bought nor
  sold. For a counted nature it must equal `sign`, which the loader asserts.

## `position_group.csv`

Columns: `pattern,group,note`. Ordered regex rules that fold FI's free-text
Befattning into `config.POSITION_GROUPS`, for the leaderboard's position filter.
How the text is split and matched is in the file's own header. `insyn doctor`
reports how many rows no rule matched (they show as Övrigt).
