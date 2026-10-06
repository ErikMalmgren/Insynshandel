-- The leaderboard's query builder aggregates in the browser, over any nature,
-- date range, instrument and role group — not just the counted Förvärv and
-- Avyttring rows `is_counted` covers.

-- The ordered filter with its nature step skipped: NULL means the row has a
-- trustworthy SEK value, whatever its nature. For a counted nature it equals
-- `exclude_reason`; for every other nature it is what the chain stopped short
-- of evaluating. Written by `insyn aggregate`, like `exclude_reason`.
ALTER TABLE transaction_norm ADD COLUMN value_exclude_reason TEXT;

-- Which way a nature moves the holding, for every nature: `sign` is the
-- default board's (0 for every uncounted nature), `direction` is what the
-- query builder uses when one is picked. +1 / -1, or 0 for no direction at
-- all (a pledge, a loan) — such a row is a transaction but neither a buy nor a
-- sell.
ALTER TABLE nature_map ADD COLUMN direction INTEGER NOT NULL DEFAULT 0;

-- Befattning → role group (seed: data/seed/position_group.csv). Ordered: per
-- comma-separated part of the text, the first matching pattern wins.
CREATE TABLE position_group (
  ord     INTEGER PRIMARY KEY,
  pattern TEXT NOT NULL,       -- Python regex, re.search over normalised text
  grp     TEXT NOT NULL,       -- one of config.POSITION_GROUPS
  note    TEXT
);
