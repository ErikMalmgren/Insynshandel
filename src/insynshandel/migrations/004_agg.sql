-- Phase 3 — aggregates (§6.5). Keyed on `lei` (invariant 12). Rebuilt in full by
-- `insyn aggregate` after the classify pass writes transaction_norm's derived
-- columns. Precomputed windows only; the API computes arbitrary ranges live.
CREATE TABLE agg_company_period (
  lei             TEXT NOT NULL,
  period          TEXT NOT NULL,   -- '30d' | '90d' | '365d' | 'ytd' | 'all'
  period_start    TEXT NOT NULL,   -- transaction_date window (inclusive)
  period_end      TEXT NOT NULL,
  buy_value_sek   REAL NOT NULL,
  sell_value_sek  REAL NOT NULL,
  net_value_sek   REAL NOT NULL,   -- buy - sell
  tx_count        INTEGER NOT NULL, -- is_counted = 1 only
  buyer_count     INTEGER NOT NULL, -- DISTINCT pdmr with sign = +1
  seller_count    INTEGER NOT NULL, -- DISTINCT pdmr with sign = -1
  n_unverifiable  INTEGER NOT NULL, -- counted rows with no market cap (§6.4)
  computed_at     TEXT NOT NULL,
  PRIMARY KEY (lei, period)
);

CREATE INDEX ix_agg_period_net ON agg_company_period(period, net_value_sek);
