<!-- Part of IMPLEMENTATION_PLAN.md. Section numbers (§N.M) are stable
     identifiers across all plan files — see the map in the index. -->

# Testing and `insyn doctor`
## 11. Acceptance checks

`insyn doctor` runs all of these and exits non-zero on any failure.
### 11.4 Unit tests
`tests/fixtures/insyn_sample.csv` — a ~50-row UTF-16LE slice of real data,
committed. Must cover, at minimum:

- UTF-16LE-without-BOM decode
- comma decimal separators and the trailing `;`
- **a quoted field containing an embedded `;` and a `""` escape** — copy the
  2020-03-18 BillerudKorsnäs row from §4.1.1 verbatim
- an empty ISIN (`Teckningsoption` rows)
- a `Reviderad` / `Aktuell` revision pair
- an exact-duplicate row pair (`ordinal` 0 and 1)
- a non-SEK row (GBP)
- an unmapped `karaktar` value, asserting `insyn aggregate` exits non-zero
- a `volymsenhet = 'Belopp'` row, asserting `is_counted = 0`
- a `Lösen ökning` / `Lösen minskning` pair, asserting neither is counted
- an outlier row (volume = price), asserting `verification = 'outlier'` when a
  market cap is present, and `verification = 'unverifiable'` (still counted,
  `market_cap` serialized as `null`, never `0`) when it is absent
- a company with `market_cap_sek = 0`, asserting `pct_of_mcap IS NULL` and
  **not** a negative number — the single most likely symptom of a bare
  `market_cap` read bypassing `market_cap_current`
- a GBP row, asserting `gross_value_sek` used the **transaction-date** rate and
  that a weekend transaction date forward-filled from the previous business day

Plus a test for the diff write path with no HTTP at all: feed the same parsed
row set into the ingest function twice and assert the second call inserts 0
rows and supersedes 0 rows.

Mock all HTTP; **no test may hit the network.**

---
