-- Ticker resolution v2 (pipeline/reference.py FIGI_RESOLVER_VERSION).
--
-- v1 asked OpenFIGI with a `micCode: XSTO` filter: every First North, Spotlight
-- and NGM share came back empty, and bond ISINs answered with bond "tickers".
-- Rows below the current version are re-asked once and replaced in place, so
-- a half-finished re-run never leaves `company` without its tickers.
ALTER TABLE figi_lookup ADD COLUMN resolver_version INTEGER NOT NULL DEFAULT 1;

-- OpenFIGI's name for the chosen listing ('SAAB AB-B'). It carries the share
-- class the ticker ('SAABB') lacks; the Yahoo fallback formatter reads it.
ALTER TABLE company ADD COLUMN figi_name TEXT;

-- Yahoo's own symbol for an ISIN, from its search endpoint ('SAAB-B.ST').
-- Every answer is kept, including the negatives, exactly like `figi_lookup`.
CREATE TABLE yahoo_symbol (
  isin         TEXT PRIMARY KEY,
  symbol       TEXT,              -- NULL = asked, no Stockholm listing on Yahoo
  looked_up_at TEXT NOT NULL,
  attempts     INTEGER NOT NULL DEFAULT 1,
  last_error   TEXT
);
