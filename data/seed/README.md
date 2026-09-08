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
- `symbol` — the manual symbol. **`''` (blank) means "not yet resolved — worklist
  item", NOT the schema's "checked, this company has no symbol".**
- `note` — free text.

### Seeding note (Phase 0)

Seeded from `data/cache/legacy/failed_isins.csv` — 61 LEIs whose ISIN OpenFIGI's
XSTO lookup could not resolve as of the legacy run. **"OpenFIGI failed" is not the
same claim as "this company has no listed symbol."** Most of these are real
listed companies; the lookup just missed.

**Phase 5 loader MUST treat a blank `symbol` here as an unresolved worklist entry
— do not load it as `symbol = ''` into `ticker_override`, or resolution step 2
(§7.3) will permanently short-circuit these companies to "no market cap" with no
signal they were never actually investigated.** Only a non-blank `symbol` (a real
ticker, or a deliberate sentinel decided in Phase 5) should produce a
`ticker_override` row.

Re-measure against the real backfill before hand-filling: after `insyn ingest
backfill` + `insyn refdata figi`, the set of genuinely unresolvable LEIs may be
much smaller than 61.
