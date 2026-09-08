-- Phase 1 — raw store. Source-faithful, append-only. See plan/01-ingest.md §4.4.
PRAGMA journal_mode = WAL;

CREATE TABLE fetch_batch (
  id            INTEGER PRIMARY KEY,
  mode          TEXT    NOT NULL,          -- backfill | recent | gaps
  window_from   TEXT    NOT NULL,          -- YYYY-MM-DD (Publiceringsdatum)
  window_to     TEXT    NOT NULL,
  started_at    TEXT    NOT NULL,
  finished_at   TEXT,
  row_count       INTEGER,                 -- rows FI returned
  rows_inserted   INTEGER,                 -- rows new to us (see §4.5)
  rows_superseded INTEGER,                 -- rows FI stopped returning
  truncated     INTEGER NOT NULL DEFAULT 0,  -- 1 = hit cap at day granularity
  http_status   INTEGER,
  error         TEXT
);

CREATE TABLE raw_transaction (
  id            INTEGER PRIMARY KEY,
  row_hash      TEXT    NOT NULL,   -- sha256 of all 22 fields joined by \x1f
  ordinal       INTEGER NOT NULL,   -- 0-based index among identical row_hash
  pub_date      TEXT    NOT NULL,   -- substr(publiceringsdatum,1,10), stored not computed
  first_seen_batch INTEGER NOT NULL REFERENCES fetch_batch(id),
  last_seen_batch  INTEGER NOT NULL REFERENCES fetch_batch(id),
  superseded_at TEXT,               -- NULL = live

  publiceringsdatum            TEXT,
  emittent                     TEXT,
  lei_kod                      TEXT,
  anmalningsskyldig            TEXT,
  person_i_ledande_stallning   TEXT,
  befattning                   TEXT,
  narstaende                   TEXT,
  korrigering                  TEXT,
  beskrivning_av_korrigering   TEXT,
  ar_forstagangsrapportering   TEXT,
  ar_kopplad_till_aktieprogram TEXT,
  karaktar                     TEXT,
  instrumenttyp                TEXT,
  instrumentnamn               TEXT,
  isin                         TEXT,
  transaktionsdatum            TEXT,
  volym                        TEXT,
  volymsenhet                  TEXT,
  pris                         TEXT,
  valuta                       TEXT,
  handelsplats                 TEXT,
  status                       TEXT
);

-- One row per calendar day we have successfully fetched. Makes "which days are
-- we missing?" a single anti-join instead of interval arithmetic. See §4.7.
CREATE TABLE coverage_day (
  pub_date         TEXT PRIMARY KEY,   -- YYYY-MM-DD
  first_fetched_at TEXT NOT NULL,
  last_fetched_at  TEXT NOT NULL,
  fetch_count      INTEGER NOT NULL DEFAULT 1,
  row_count        INTEGER NOT NULL    -- 0 is a valid, meaningful value
);

-- THE duplicate-prevention guarantee: the DB itself refuses two live copies of
-- the same source row. Everything in §4.5 depends on this index.
CREATE UNIQUE INDEX ux_raw_live_key
    ON raw_transaction(row_hash, ordinal)
 WHERE superseded_at IS NULL;

CREATE INDEX ix_raw_live_window ON raw_transaction(pub_date)
 WHERE superseded_at IS NULL;

CREATE VIEW raw_live AS
  SELECT * FROM raw_transaction WHERE superseded_at IS NULL;
