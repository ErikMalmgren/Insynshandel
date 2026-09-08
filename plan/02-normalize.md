<!-- Part of IMPLEMENTATION_PLAN.md. Section numbers (§N.M) are stable
     identifiers across all plan files — see the map in the index. -->

# Phase 2 — normalize
## 5. Phase 2 — normalize

Derived from `raw_live`. Fully rebuildable, never edited by hand.

### 5.0 What "full rebuild" means, and why

**Think of `transaction_norm` as a materialized view, not a table you
maintain.** It holds no information of its own. It is a pure function of three
inputs:

```
transaction_norm  =  f( raw_live , nature_map , parsing rules in code )
```

Every run throws the whole thing away and recomputes it:

```sql
BEGIN IMMEDIATE;
DELETE FROM transaction_norm;                  -- yes, all of it, every time
INSERT INTO transaction_norm (...)
SELECT ... FROM raw_live;                      -- re-parse, re-type, re-derive
COMMIT;
```

Nothing is preserved across runs, because nothing needs to be. If the process
dies halfway, the transaction rolls back and the previous contents are intact.

#### Why not incremental

Four separate things can change the correct value of an already-processed row.
An incremental normalize would have to detect all four; a full rebuild handles
them for free:

| What changed | Effect on rows already normalized |
| --- | --- |
| A row was **revised** — FI flipped it to `Reviderad` | It leaves `raw_live`, so it must **leave** `transaction_norm` and stop being counted |
| A row was **cancelled** — `Makulerad` | Same: must stop being counted |
| You edited **`nature_map.csv`** | Every row with that Karaktär must be reclassified, across all ten years |
| You fixed a **parsing bug** | Every affected row must be re-parsed retroactively |

Only the first two are visible in `raw_transaction`. The last two are changes to
*code and config* — there is no row-level signal to key an incremental update
off. You would end up rebuilding anyway, just with more machinery and a subtle
staleness bug when someone forgets to force it.

#### It is cheap

180,000 rows of straightforward parsing. Seconds, not minutes. There is no
performance argument for the incremental version, and §14.5 says explicitly not
to hand-roll one until the full rebuild actually hurts.

#### The consequence you must not miss

`normalize` writes the **parsed** columns and leaves the **derived** columns
(`sign`, `is_counted`, `verification`, `exclude_reason`, `gross_value_sek`)
`NULL`. Those are written by `insyn aggregate`, which needs FX rates and market
caps from the reference pipeline.

> **Running `normalize` alone leaves the database in an intermediate state**
> where nothing is classified and every aggregate is empty. That is not a bug,
> but it means the two commands are one logical step. Use the combined command
> in every automated context:
>
> ```
> insyn build     # = normalize → refdata → aggregate → (export-static)
> ```
>
> The individual commands stay available for debugging. The cron (§10.3) and
> the CI workflow (§10.1) call `insyn build`, never `normalize` on its own.

#### What it must never do

- **Never edit `transaction_norm` by hand.** Any manual fix is erased on the
  next run. Fix the input — `raw_transaction`, `nature_map.csv`, or the parser —
  and rebuild.
- **Never write back into `raw_transaction` from this phase.** Raw is
  append-and-supersede only (§4.5); normalize reads it and nothing else.

### 5.1 `migrations/002_norm.sql`

```sql
CREATE TABLE transaction_norm (
  raw_id            INTEGER PRIMARY KEY REFERENCES raw_transaction(id),
  published_at      TEXT,     -- 'YYYY-MM-DD HH:MM:SS'
  published_date    TEXT,     -- 'YYYY-MM-DD'
  transaction_date  TEXT,     -- 'YYYY-MM-DD'
  lei               TEXT,
  issuer_name       TEXT,
  notifier          TEXT,     -- Anmälningsskyldig
  pdmr              TEXT,     -- Person i ledande ställning
  position          TEXT,     -- Befattning
  is_closely_assoc  INTEGER,  -- Närstående = 'Ja'
  is_correction     INTEGER,  -- Korrigering = 'Ja'
  correction_note   TEXT,
  is_initial_report INTEGER,
  is_share_program  INTEGER,  -- Är kopplad till aktieprogram = 'Ja'
  nature            TEXT,     -- Karaktär, verbatim
  instrument_type   TEXT,
  instrument_name   TEXT,
  isin              TEXT,
  volume            REAL,
  volume_unit       TEXT,
  price             REAL,
  currency          TEXT,
  venue             TEXT,
  status            TEXT,     -- Aktuell | Reviderad | Makulerad
  -- Written by `insyn aggregate`, NOT by `insyn normalize`.
  -- `insyn aggregate` must always run after `insyn normalize` in the same
  -- invocation chain (§10.1 step 5), because `verification` needs market caps
  -- from the reference pipeline. normalize leaves these NULL.
  sign              INTEGER,  -- +1 / -1 / 0 from nature_map
  is_counted        INTEGER,  -- 1 = included in net aggregation
  verification      TEXT,     -- ok | outlier | unverifiable  (§6.4)
  exclude_reason    TEXT,     -- which §6.2 rule fired; NULL when is_counted = 1
  gross_value       REAL,     -- volume * price, in the ORIGINAL `currency`
  fx_rate_sek       REAL,     -- SEK per 1 unit of `currency` on transaction_date
  fx_rate_date      TEXT,     -- the date the rate was taken from (forward-filled)
  gross_value_sek   REAL      -- gross_value * fx_rate_sek  <- everything uses this
);

CREATE INDEX ix_norm_lei_txdate ON transaction_norm(lei, transaction_date);
CREATE INDEX ix_norm_txdate     ON transaction_norm(transaction_date);
CREATE INDEX ix_norm_isin       ON transaction_norm(isin);
CREATE INDEX ix_norm_pdmr       ON transaction_norm(pdmr);
CREATE INDEX ix_norm_counted    ON transaction_norm(is_counted, transaction_date);
```

### 5.2 Parsing rules

- **Numbers:** `float(s.replace('\xa0','').replace(' ','').replace(',','.'))`.
  On failure store `NULL` and set `exclude_reason='unparseable_number'`. Never
  crash the batch.
- **Booleans:** `'Ja' → 1`, empty → `0`. Anything else → log and treat as `0`.
- **Dates:** split on space; keep date and time separately for
  Publiceringsdatum. Transaktionsdatum's time component is always `00:00:00` —
  discard it.
- **Strip whitespace from every text field, ISIN especially.** **[VERIFIED]**
  the export contains both `'SE0006425815'` and `'SE0006425815 '` for the same
  PowerCell instrument. Un-stripped, that is two instruments, two ticker
  lookups, and a split company. Apply `.strip()` to all 22 columns.
- **Empty ISIN is legal** — observed on some `Teckningsoption` rows. Do not
  reject.
- **`volume_unit`:** **[VERIFIED]** two values exist — `Antal` (6,482 sampled
  rows) and **`Belopp`** (19 rows), the latter on bonds and convertibles where
  the volume is a nominal amount, not a share count. For those,
  `volume * price` is **not** a monetary value. Set `is_counted = 0`,
  `exclude_reason = 'volume_unit'` for anything that is not `Antal`, and log the
  distinct set seen after backfill in case a third value appears.

### 5.3 Acceptance

- `COUNT(transaction_norm) == COUNT(raw_live)` — normalization drops nothing.
- No `NULL` in `published_date`, `transaction_date`, `lei`, `nature`, `status`.
- `SELECT DISTINCT volume_unit`, `DISTINCT currency`, `DISTINCT status` logged
  for review.

---
