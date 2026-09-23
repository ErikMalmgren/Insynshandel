-- Phase 5 — reference data. Independent pipeline: a failure here must
-- never block ingest. All identity is provider-NEUTRAL; each MarketCapProvider
-- derives its own symbol format.

-- Provider-neutral company entity, keyed on LEI.
CREATE TABLE company (
  lei           TEXT PRIMARY KEY,
  display_name  TEXT,          -- canonical, recency-then-frequency
  primary_isin  TEXT,          -- derived from FI's own 'Aktie' rows
  raw_ticker    TEXT,          -- OpenFIGI verbatim, e.g. 'INVE B' — NOT 'INVE-B.ST'
  mic_code      TEXT,          -- e.g. 'XSTO'
  exch_code     TEXT,          -- e.g. 'SS'
  ticker_source TEXT,          -- openfigi | manual | NULL
  updated_at    TEXT
);

-- Manual symbol overrides. provider = '' means "applies to every provider"
-- (`COALESCE(provider,'')` in the PK would be an expression, which SQLite
-- rejects — an empty-string default is the same thing without the expr).
-- symbol '' (with a note) = "checked, this company has no symbol".
CREATE TABLE ticker_override (
  lei      TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT '',
  symbol   TEXT,
  note     TEXT,
  PRIMARY KEY (lei, provider)
);

CREATE TABLE company_name_variant (
  lei        TEXT NOT NULL,
  name       TEXT NOT NULL,
  first_seen TEXT,
  last_seen  TEXT,
  n_rows     INTEGER,
  PRIMARY KEY (lei, name)
);

-- Appended, never overwritten. Aggregates join to MAX(as_of). 0 = unknown.
CREATE TABLE market_cap (
  lei        TEXT NOT NULL,
  as_of      TEXT NOT NULL,      -- YYYY-MM-DD
  market_cap REAL,
  currency   TEXT NOT NULL,      -- from the provider — NEVER assumed SEK
  market_cap_sek REAL NOT NULL CHECK (market_cap_sek >= 0),
  source     TEXT,
  PRIMARY KEY (lei, as_of)
);

-- Riksbank SWEA daily rates. Business days only — gaps on weekends/holidays are
-- NOT filled here; forward-fill at read time.
CREATE TABLE fx_rate (
  currency     TEXT NOT NULL,     -- 'USD', 'EUR', 'GBP', 'CAD', ...
  rate_date    TEXT NOT NULL,     -- YYYY-MM-DD
  sek_per_unit REAL NOT NULL,
  source       TEXT NOT NULL DEFAULT 'riksbank',
  PRIMARY KEY (currency, rate_date)
);

-- Every OpenFIGI answer we have ever received, including the negatives.
CREATE TABLE figi_lookup (
  isin         TEXT PRIMARY KEY,
  ticker       TEXT,              -- NULL = asked, no usable answer
  raw_ticker   TEXT,
  name         TEXT,
  exch_code    TEXT,
  mic_code     TEXT,
  looked_up_at TEXT NOT NULL,
  attempts     INTEGER NOT NULL DEFAULT 1,
  last_error   TEXT
);

-- ── Seed-file tables (reloaded from data/seed/*.csv on every `db migrate` and
--    every `insyn aggregate` — editing the CSV + re-running is the only lever) ──

-- Karaktär → sign / counted. An unmapped value fails the build.
CREATE TABLE nature_map (
  karaktar TEXT PRIMARY KEY,
  sign     INTEGER NOT NULL,
  counted  INTEGER NOT NULL,
  category TEXT,
  note     TEXT
);

-- Confirmed LEI merges. alias_lei folds into canonical_lei at
-- aggregation time. Populated by hand from backfill evidence, never a ratio.
CREATE TABLE issuer_alias (
  alias_lei     TEXT PRIMARY KEY,
  canonical_lei TEXT NOT NULL,
  note          TEXT
);

-- ── The market-cap guard. NOTHING reads `market_cap` directly. ─────────
-- The CASE has no ELSE, so the 0 sentinel becomes SQL NULL before it can reach
-- arithmetic: net / NULL is NULL, SUM() skips it, comparisons are never true.
CREATE VIEW market_cap_current AS
SELECT m.lei, m.as_of, m.currency, m.source,
       CASE WHEN m.market_cap_sek > 0 THEN m.market_cap_sek END AS market_cap_sek
  FROM market_cap m
 WHERE m.as_of = (SELECT MAX(x.as_of) FROM market_cap x WHERE x.lei = m.lei);
