-- Phase 2 — normalize. transaction_norm is a materialized view of raw_live:
-- DELETEd and rebuilt in full on every `insyn normalize`. It holds no
-- state of its own. The derived columns (sign … gross_value_sek) are left NULL
-- here and written by `insyn aggregate`.
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
  sign              INTEGER,  -- +1 / -1 / 0 from nature_map
  is_counted        INTEGER,  -- 1 = included in net aggregation
  verification      TEXT,     -- ok | outlier | unverifiable
  exclude_reason    TEXT,     -- which exclusion rule fired; NULL when is_counted = 1
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
