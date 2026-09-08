<!-- Part of IMPLEMENTATION_PLAN.md. Section numbers (§N.M) are stable
     identifiers across all plan files — see the map in the index. -->

# Phase 5 — reference data
## 7. Phase 5 — reference data (independent pipeline)

Runs on its own schedule; a failure here must not block ingest.

### 7.1 `migrations/003_reference.sql`

```sql
-- Provider-NEUTRAL identity only. No column here is in any one vendor's
-- symbol format; each MarketCapProvider derives its own symbol (§7.4.2).
CREATE TABLE company (
  lei           TEXT PRIMARY KEY,
  display_name  TEXT,          -- canonical, see §7.2
  primary_isin  TEXT,          -- derived from FI's own rows, see §7.2.1
  raw_ticker    TEXT,          -- OpenFIGI verbatim, e.g. 'INVE B' — NOT 'INVE-B.ST'
  mic_code      TEXT,          -- e.g. 'XSTO'
  exch_code     TEXT,          -- e.g. 'SS'
  ticker_source TEXT,          -- openfigi | manual | NULL
  updated_at    TEXT
);

-- Manual symbol overrides. provider IS NULL means "applies to every provider".
CREATE TABLE ticker_override (
  lei      TEXT NOT NULL,
  provider TEXT,               -- 'yahoo', 'borsdata', … or NULL for all
  symbol   TEXT,               -- '' = checked, this company has no symbol
  note     TEXT,
  PRIMARY KEY (lei, COALESCE(provider, ''))
);

CREATE TABLE company_name_variant (
  lei        TEXT NOT NULL,
  name       TEXT NOT NULL,
  first_seen TEXT,
  last_seen  TEXT,
  n_rows     INTEGER,
  PRIMARY KEY (lei, name)
);

CREATE TABLE market_cap (
  lei        TEXT NOT NULL,
  as_of      TEXT NOT NULL,    -- YYYY-MM-DD
  market_cap REAL,
  currency   TEXT NOT NULL,    -- from yfinance fast_info['currency'] — NOT assumed SEK
  market_cap_sek REAL NOT NULL  -- converted via fx_rate; 0 = unknown (§6.4)
             CHECK (market_cap_sek >= 0),
  source     TEXT,             -- 'yfinance'
  PRIMARY KEY (lei, as_of)
);

-- Riksbank SWEA daily rates. Gaps on weekends/holidays are NOT filled here —
-- store exactly what the API returned and forward-fill at read time (§6.3.1).
CREATE TABLE fx_rate (
  currency    TEXT NOT NULL,        -- 'USD', 'EUR', 'GBP', 'CAD', ...
  rate_date   TEXT NOT NULL,        -- YYYY-MM-DD, business days only
  sek_per_unit REAL NOT NULL,
  source      TEXT NOT NULL DEFAULT 'riksbank',
  PRIMARY KEY (currency, rate_date)
);

-- Every OpenFIGI answer we have ever received, including the negative ones.
CREATE TABLE figi_lookup (
  isin         TEXT PRIMARY KEY,
  ticker       TEXT,            -- NULL = asked, no usable answer
  raw_ticker   TEXT,            -- pre-formatting, for debugging _format_ticker
  name         TEXT,
  exch_code    TEXT,
  mic_code     TEXT,
  looked_up_at TEXT NOT NULL,
  attempts     INTEGER NOT NULL DEFAULT 1,
  last_error   TEXT
);
```

`market_cap` is keyed by `(lei, as_of)` and **appended**, never overwritten.
Backfilling history is out of scope, but from day one you accumulate a real
series for free. Aggregates join to `MAX(as_of)`.

#### 7.1.1 Refresh cadences and the "only ask for what's missing" rule

| Source | Cadence | Rule |
| --- | --- | --- |
| OpenFIGI `figi_lookup` | **On demand only** | Query **only** ISINs with no row in `figi_lookup`. Never re-query a resolved one. |
| `market_cap` | **Daily** | Append a new `as_of` row per company with a ticker. |

The OpenFIGI query set is exactly:

```sql
SELECT DISTINCT r.isin
  FROM raw_live r
  LEFT JOIN figi_lookup f ON f.isin = r.isin
 WHERE r.isin <> ''
   AND f.isin IS NULL                      -- never asked before
   AND r.isin NOT IN (SELECT isin FROM ticker_override_isin);
```

After a full backfill this returns a few thousand ISINs once, then **near zero
forever** — typically a handful of new listings per week.

**Negative caching, with a bounded retry.** A lookup that returns nothing still
writes a row with `ticker = NULL`. That row stops it being re-queried every
run. Re-ask only when:

```
ticker IS NULL
AND attempts < 5
AND looked_up_at < date('now', '-90 days')
```

Bump `attempts` and `looked_up_at` on each retry. This matters: 62 ISINs
currently fail, and without negative caching that is 62 wasted requests at
2.4 s each on **every** run, forever.

> **Distinguish "no answer" from "request failed".** A `429`, a timeout, or a
> connection reset is **not** a negative result — do not write a `figi_lookup`
> row for it. Record it in `last_error` only if a row already exists, and
> retry on the next run. Writing a NULL row for a transport failure would
> permanently blacklist an ISIN because the network hiccupped once.

### 7.2 Canonical company name — recency wins, not frequency

**[VERIFIED]** 212 of 679 LEIs carry more than one `Emittent` spelling. The old
code picks `sorted(variants)[0]` — alphabetical, which is arbitrary. But
"most frequent" is **also wrong**, and for a reason that only shows up over ten
years of data: **companies rename, and the LEI persists.**

Real cases in the register:

| LEI's names over time | What happened |
| --- | --- |
| `Momentum Group AB` (10 rows) → `Alligo AB` (1) | genuine rename |
| `Mysafety Group AB` (5) → `Empir Group AB` (3) | genuine rename |
| `AHA World AB` (10) → `blick global group ab` (2) | genuine rename |
| `Xavitech AB` (9) → `Frontwalker AB (publ)` (1) | genuine rename |

Frequency picks the **dead** name every time — a leaderboard showing "Momentum
Group AB" for a company now called Alligo is simply wrong, and gets more wrong
the longer the history.

But pure recency is also unsafe, because filers make typos and the most recent
row may carry one. **[VERIFIED]** the `Emittent` field sometimes contains a
person's name outright: `Castellum AB` (15 rows) also appears as
`Martin Bjöörn` (1), and `Genovis AB` as `Susanne Ahlberg` (1).

**Rule — most frequent within a recent window:**

```sql
-- the most common spelling among rows from the last 12 months;
-- fall back to the most recent name overall if the company has been quiet
SELECT issuer_name FROM transaction_norm
 WHERE lei = :lei
   AND transaction_date >= date('now', '-12 months')
 GROUP BY issuer_name
 ORDER BY COUNT(*) DESC, MAX(transaction_date) DESC
 LIMIT 1;
```

Recency handles the rename; frequency-within-the-window absorbs the typo.

Keep **every** variant in `company_name_variant` — they are the aliases a search
box needs, so someone typing "Momentum Group" still finds Alligo.

#### 7.2.1 Choosing a company's primary ISIN

Derived from FI's own rows; no external source needed.

```sql
-- among Instrumenttyp = 'Aktie' rows for this LEI, the ISIN appearing on the
-- most transactions, tie-broken by the most recent transaction_date
SELECT isin FROM transaction_norm
 WHERE lei = :lei AND instrument_type = 'Aktie' AND isin <> ''
 GROUP BY isin
 ORDER BY COUNT(*) DESC, MAX(transaction_date) DESC
 LIMIT 1;
```

**[VERIFIED]** 630 of 679 issuers (92%) resolve this way. Two caveats, both
benign for market-cap purposes:

- **102 issuers have more than one `Aktie` ISIN** — A/B share classes and
  historical ISINs left behind by splits and redenominations. Investor AB has
  four; Fabege has twelve. The rule may pick the A class where B is the liquid
  one. **This does not affect market cap**: providers report a company-level
  figure for either class's symbol. It would matter if you ever wanted price per
  share — revisit then.
- **17 issuers have no ISIN anywhere in the FI data.** They go to
  `ticker_override.csv` (§7.3), which exists for exactly this.

#### 7.2.2 Rows with no ISIN — a non-problem, and where it does bite

**[VERIFIED]** 206 of 4,334 sampled rows (4.8%) carry no ISIN. All 206 still
carry an LEI.

**At row level this changes nothing.** Aggregation keys on `lei`, never on
`isin` (§6.5), so an ISIN-less row is attributed, valued and counted exactly
like any other. ISIN is needed only once per *company*, to find a symbol for the
market-cap lookup. Do not exclude these rows, and do not treat a missing ISIN
as a parse failure — it is a legal, expected value (§5.2).

The instruments involved are mostly derivatives, which explains it: the ISIN
field is often left blank for options and swaps.

```
73  Aktie          54  Option        50  Teckningsoption
12  Köpoption      12  Swap           2  BTA
```

**Company-level fallback chain**, in order — stop at the first that yields a
symbol:

1. **Another row for the same LEI.** 662 of 679 issuers (97%) have an ISIN
   somewhere in their rows even when individual rows lack one. Prefer
   `Instrumenttyp = 'Aktie'` (§7.2.1).
2. **A non-`Aktie` ISIN for that LEI.** A `Teckningsoption` ISIN will usually
   fail the OpenFIGI equity lookup, but it costs one cached request to find out
   and the negative is remembered (§7.1.1).
3. **`ticker_override.csv`** — the manual escape hatch (§7.3).
4. **Give up cleanly.** `market_cap_sek = 0`, `verification = 'unverifiable'`,
   `pct_of_mcap: null`. The company still appears on the leaderboard with its
   full net flow; only the ratio is missing (§6.4).

> **The "17 issuers with no ISIN at all" figure is a sampling artifact — do not
> design around it.** Those 17 include Tele2, CellaVision, Bonava and Tobii,
> which obviously have ISINs; the 4,334-row sample simply caught them only on
> swaps and options. Over the full ~180k-row backfill nearly all resolve through
> step 1. Re-measure after backfill and expect a much smaller number, mostly
> genuinely unlisted entities.

> **[VERIFIED] Strip whitespace from ISINs.** The FI export contains
> `'SE0006425815'` and `'SE0006425815 '` — the same PowerCell instrument with a
> trailing space — which without `.strip()` become two different companies'
> worth of rows. Normalise in phase 2 (§5.2) and this never reaches here.

#### 7.2.3 One company, one row on the leaderboard

**Multiple ISINs are already handled.** `agg_company_period` is keyed on `lei`
(§6.5), never on ISIN, so Investor AB's four share-class ISINs and Fabege's
twelve already sum into a single row under a single name. Nothing to build.

**The real fragmentation is mis-keyed LEIs**, and it is worth knowing about.

**[VERIFIED]** 47 ISINs in the sample appear under more than one LEI. The
pattern is a filer putting the wrong entity in the issuer field — often the
*closely associated person or their holding company* instead of the issuer:

| ISIN | dominant issuer | also filed under |
| --- | --- | --- |
| `SE0000107401` | Investor AB (19 rows) | **Grace Skaugen** (1) — a board member |
| `SE0007640156` | Scandic Hotels Group (16) | NetEnt AB (1) |
| `SE0007100359` | Pandox AB (8) | **Helene Sundt AS** (4) |
| `SE0009994445` | Seamless Distribution (27) | Clavister Holding AB (7) |
| `SE0013888245` | Collector AB (publ) (29) | Collector AB (3) — duplicate LEI |

Roughly 6% of rows sit on an ISIN whose LEI is contested.

> **Do not merge issuers transitively.** I tried union-find over "LEIs sharing
> an ISIN" and it collapsed **twelve unrelated companies** — Fabege, Storytel,
> Bure, Catella, Prevas, Bergman & Beving among them — into a single group,
> because one serial mis-filer's LEI chains them all together. One bad edge
> poisons the entire graph. This is a trap, not a hypothetical.

**Detect and report; merge only on instruction.** Same stance as outliers
(§6.4): no invented threshold decides that two real companies are one.

```sql
-- ISINs whose 'Aktie' rows disagree about the issuer
SELECT n.isin,
       n.lei                          AS candidate_lei,
       COUNT(*)                       AS rows_for_this_lei,
       (SELECT COUNT(*) FROM transaction_norm x
         WHERE x.isin = n.isin AND x.instrument_type = 'Aktie') AS rows_for_isin
  FROM transaction_norm n
 WHERE n.instrument_type = 'Aktie' AND n.isin <> ''
 GROUP BY n.isin, n.lei
HAVING (SELECT COUNT(DISTINCT lei) FROM transaction_norm y
         WHERE y.isin = n.isin AND y.instrument_type = 'Aktie') > 1
 ORDER BY n.isin, rows_for_this_lei DESC;
```

Surface the count on `/api/v1/data-quality`. Confirmed merges go in a committed
seed file, applied as an alias at aggregation time:

```csv
# data/seed/issuer_alias.csv — canonical_lei wins; alias_lei folds into it
alias_lei,canonical_lei,note
52990026MKVH...,549300VEBQPH...,Grace Skaugen is a PDMR of Investor, not an issuer
529900AGWAKUTYNETM62,529900G9LZILBPCZXA17,duplicate LEI registration for Collector AB
```

> **Re-measure after the backfill before deciding anything.** These ratios come
> from 4,334 rows out of ~180,000. A 19-vs-1 split in the sample may be 400-vs-2
> or 40-vs-38 in the full data, and only the second kind is genuinely ambiguous.
> Run the query once the backfill lands, review the list by hand, and fill in
> `issuer_alias.csv` from evidence rather than from a ratio.

### 7.3 Ticker resolution

Pipeline: `FI row → (LEI, ISIN)` directly, then `ISIN → (OpenFIGI) → ticker`.

**There is no GLEIF step.** **[VERIFIED]** the FI export carries `LEI-kod` on
100% of rows and `ISIN` on 95%, so the issuer↔instrument link comes free with
the transaction data. See §3.1.

Port the working logic from `test_find_unknown.py` — its `_format_ticker`
(SDB handling, share-class `A`/`B` suffixes, the `EXCLUDE_TICKERS` set) is the
good part of the current codebase. Keep it, add tests:

```
'INVE B'  + 'INVESTOR AB SER. B'      → 'INVE-B.ST'
'NCCB'    + 'NCC AB SER. B'           → 'NCC-B.ST'
'ALIV SDB'                            → 'ALIV-SDB.ST'
'ABB'                                 → 'ABB.ST'      (excluded from suffixing)
'SSABB'   + 'SSAB AB SER. B'          → 'SSAB-B.ST'
```

OpenFIGI: rate-limit at 25 req/min unauthenticated (2.4 s spacing). Set
`OPENFIGI_API_KEY` if available to raise it. **Query only unknown ISINs** —
see §7.1.1 for the exact query and the negative-cache rules.

Resolution order when a provider asks for a company's symbol, highest priority
first:

1. `ticker_override` for that **specific** provider — always wins
2. `ticker_override` with `provider IS NULL` — applies to all providers
3. `provider.symbol_for(company)` derived from `company.raw_ticker` +
   `mic_code` (§7.4.2)
4. `None` — no symbol, so no market cap, so `market_cap_sek = 0`,
   `pct_of_mcap` is `null`, and the company is `unverifiable` (§6.4)

**`data/seed/ticker_override.csv` is the documented manual escape hatch** for
the 62 LEIs OpenFIGI can't resolve (seed it from the existing
`failed_isins.csv`). Without it, those companies stay permanently `null` on
market cap with no way for you to fix them.



### 7.4 Market cap — pluggable providers

`yfinance` is unofficial scraping. It breaks without warning, changes its
response shape between releases, and redistributing its values on a public site
is a terms-of-use question. **Treat it as one implementation of an interface,
never as the interface itself.**

#### 7.4.1 The interface

```python
# sources/marketcap/base.py
from typing import Protocol
from dataclasses import dataclass

@dataclass(frozen=True)
class MarketCapQuote:
    lei: str
    market_cap: float          # in `currency`, NOT converted
    currency: str              # e.g. 'SEK' — read from the provider, never assumed
    as_of: str                 # YYYY-MM-DD
    source: str                # provider.name, stored on the row

class MarketCapProvider(Protocol):
    name: str                  # goes into market_cap.source

    def symbol_for(self, company: Company) -> str | None:
        """Map a company to THIS provider's symbol format.
        Return None if this provider cannot address the company."""

    def fetch(self, companies: list[Company]) -> tuple[list[MarketCapQuote],
                                                       list[FetchFailure]]:
        """Never raises. Partial success is normal and expected."""
```

Concrete providers live in `sources/marketcap/` — one file each:

```
sources/marketcap/
├── base.py          Protocol, MarketCapQuote, FetchFailure, registry
├── yahoo.py         YahooProvider     — yfinance, the default
├── borsdata.py      BorsdataProvider  — paid, Nordic-focused, has a real API
└── manual.py        ManualProvider    — reads data/seed/market_cap_manual.csv
```

`ManualProvider` reads a committed seed file — the escape hatch for companies
no automated source can price:

```csv
lei,market_cap,currency,as_of,note
5493002TF62S16D67T17,412000000,SEK,2026-09-01,from H1 2026 report; unlisted
```

Selection is config, not code:

```python
# config.py
MARKETCAP_PROVIDERS = ["yahoo", "manual"]     # tried in order, first hit wins
```

Swapping providers is then editing one list. Adding one is a new file plus a
registry entry — no change to `reference.py`, the schema, or anything
downstream.

#### 7.4.2 The trap: symbol format is provider-specific

**Do not store "the Yahoo ticker" on `company` and call it the ticker.** The
`_format_ticker` logic ported from `test_find_unknown.py` produces
Yahoo-specific strings — `INVE-B.ST`, `ALIV-SDB.ST`. Another provider wants
`INVE B`, or the ISIN, or an internal id. If the Yahoo format is *the* stored
identity, swapping providers means re-deriving every ticker.

Store the **provider-neutral** facts and let each provider format its own
symbol:

| Field | Example | Lives on | Sourced from |
| --- | --- | --- | --- |
| `lei` | `549300…` | `company` | FI |
| `primary_isin` | `SE0015811963` | `company` | FI, via §7.2.1 |
| `raw_ticker` | `INVE B` | `company` | OpenFIGI, verbatim |
| `mic_code` | `XSTO` | `company` | OpenFIGI |
| `exch_code` | `SS` | `company` | OpenFIGI |

`figi_lookup` (§7.1) is the ISIN-keyed **cache** of OpenFIGI answers; `company`
is the LEI-keyed **entity** a provider addresses. The three OpenFIGI fields are
copied onto `company` during `refdata figi` so a provider never has to join the
cache.

`YahooProvider.symbol_for()` then applies the `.ST` suffixing and share-class
rules (they belong in `yahoo.py`, not in shared code), while a hypothetical
`BorsdataProvider.symbol_for()` uses the ISIN directly. `figi_lookup` already
stores `raw_ticker`, `mic_code` and `exch_code` for exactly this reason (§7.1).

`data/seed/ticker_override.csv` gains a `provider` column so an override can
target one provider; blank means all of them:

```csv
lei,provider,symbol,note
5493001BIDO3E2AOA590,,,unlisted — no symbol expected, checked 2026-09
549300358OIURL35XB35,yahoo,EXAMPLE-B.ST,resolved manually from Nasdaq listing
```

An **empty** `symbol` with a note still means "we checked, there is none" and
stops anyone re-investigating.

#### 7.4.3 Failure handling

Every provider obeys the same three rules:

1. **`fetch()` never raises.** Partial success is the normal case. Return the
   quotes you got and a `FetchFailure` for each one you didn't.
2. **A failure writes nothing.** No row, not even a `0` row. The previous
   snapshot stays the latest, so the site keeps showing a slightly stale market
   cap rather than losing it. `0` means *"this company has no market cap
   source"*, not *"today's fetch failed"* — conflating the two would silently
   turn every outage into a wave of `unverifiable`.
3. **Report a success ratio — but a degraded provider must never abort
   `insyn build`.**

   ```python
   ratio = successes / attempted
   if ratio < DEGRADED_FLOOR:                      # 0.5
       log.error("marketcap provider %s degraded: %d/%d", name, successes, attempted)
       record_provider_health(name, ratio)         # surfaced on /api/v1/meta
       if standalone:                              # `insyn refdata marketcaps`
           sys.exit(1)                             # loud when run by hand
       # inside `insyn build`: warn and continue. See below.
   ```

   > **Why continuing is correct, not lax.** `build` is
   > normalize → refdata → aggregate, and `normalize` has *already* run
   > `DELETE FROM transaction_norm` by the time market caps are fetched.
   > Aborting there would leave the database in exactly the intermediate state
   > §5.0 warns about — every classification column `NULL`, every aggregate
   > empty — triggered by nothing worse than Yahoo having a bad afternoon.
   >
   > There is no need to abort, because rule 2 already makes an outage safe:
   > a failed fetch writes no row, `market_cap_current` reads each company's
   > `MAX(as_of)`, so those companies simply keep **yesterday's** snapshot.
   > `aggregate` runs normally. Some companies value against today's cap and
   > some against yesterday's — a slightly stale denominator, which is
   > enormously better than an empty site.
   >
   > **Nothing becomes `unverifiable` because of an outage.** `0` means
   > "this company has no market cap source at all" (no resolvable ticker),
   > never "today's fetch failed". Keeping those two distinct is what makes
   > continuing safe — do not let a fetch failure write `0`.

   A provider that quietly returns `None` for 90% of tickers is the exact
   `yfinance` failure mode the ratio guards against. Put
   `provider_health` on `/api/v1/meta` so a sustained degradation is visible
   without anyone reading logs.

Keep the existing `ThreadPoolExecutor(max_workers=5)` and the error taxonomy
from `marketCap.py` — the 404/429/timeout discrimination is genuinely useful and
belongs inside `yahoo.py`.

#### 7.4.4 Reference-data sources considered

Evaluated against the one question that matters: **ISIN → market cap in SEK**,
for Swedish small and mid caps across Nasdaq Stockholm, First North, Spotlight
and NGM.

| Source | Gives | Cost | Verdict |
| --- | --- | --- | --- |
| **GLEIF** | ISIN ↔ LEI | free, 249 MB | **Dropped** — FI already carries both (§3.1) |
| **OpenFIGI → yfinance** | ISIN → ticker → cap | free | **Default.** Free, works, but two hops and a format heuristic |
| **Börsdata** | ISIN → cap, directly | **600 kr/month** | Best data, wrong price — see below |
| **ESMA FIRDS** | ISIN → LEI, CFI, venue MIC, currency | free | Useful, but **no ticker and no price** — see below |
| Nasdaq Nordic | ISIN → ticker for its own venues | free-ish | Bot-protected; covers ~60% of the register |
| FMP / EODHD / Alpha Vantage | ISIN → cap | paid | Generic; weaker Nordic small-cap coverage |

**Börsdata is technically the right answer and the wrong price.** It is
Swedish, keyed by ISIN, and covers precisely this dataset — First North,
Spotlight and NGM included, which is where generic international providers thin
out. Adopting it would delete three moving parts at once: the OpenFIGI lookup,
the Yahoo ticker-format heuristic, and `yfinance`.

**At 600 kr/month (~7,200 kr/year) it is not proportionate to this project.**
That is real money for a portfolio site that serves a leaderboard. **Stay on
OpenFIGI + yfinance, both free.**

This is exactly why §7.4.1 defines a `MarketCapProvider` protocol rather than
calling `yfinance` directly: the day this project has a reason to pay for data —
it becomes commercial, or the Yahoo scraping finally breaks for good — the swap
is a new file in `sources/marketcap/` and one entry in `MARKETCAP_PROVIDERS`.
Build the seam now because it is nearly free; buy the data only when something
justifies it.

**ESMA FIRDS** is the authoritative EU instrument register — the same regulatory
ecosystem that produces FI's insider register. **[VERIFIED]** it is free, needs
no key, and is queryable:

```
https://registers.esma.europa.eu/solr/esma_registers_firds_files/select
    ?q=file_type:FULINS+AND+file_name:FULINS_E*&wt=json&rows=3&sort=publication_date+desc
→ download_link → https://firds.esma.europa.eu/firds/FULINS_E_YYYYMMDD_NNofNN.zip
```

Each `<RefData>` record carries ISIN, `FullNm`, ISO 18774 `ShrtNm`, **CFI
classification code**, notional currency, **issuer LEI**, and the **venue MIC**.

**It does not carry a ticker or a price**, so it cannot replace OpenFIGI or the
market-cap provider. What it *would* add is authoritative instrument
classification — the CFI code says definitively whether an ISIN is an ordinary
share, a warrant, a right or a depositary receipt, and a delisted ISIN simply
stops appearing in the weekly full file. That would resolve the share-class
ambiguity in §7.2.1 properly.

**Not worth building now.** FI's own `Instrumenttyp` column already
distinguishes `Aktie` / `Teckningsoption` / `BTA`, the ambiguity it would fix
does not affect market cap, and the equity files are ~350 MB uncompressed each,
twice weekly. Revisit if you ever need per-share prices rather than company
market cap.

#### 7.4.5 Same pattern, lower priority

The FX and ticker sources have the same swap risk, but neither is urgent:
Riksbank is a central bank with a stable public API, and OpenFIGI is a Bloomberg
open-data service. Give them the same `Protocol` shape if it is free to do so,
but do not build alternative implementations speculatively.

---
---

## Acceptance checks
### 11.2.4 Market-cap providers (§7.4)

- Swapping `MARKETCAP_PROVIDERS` in `config.py` requires **no other code
  change**. Verify by running with `["manual"]` alone and confirming the
  pipeline completes.
- A provider returning nothing writes **no rows at all** — not `0` rows. After
  a simulated total failure, `market_cap_current` still returns yesterday's
  snapshot and no company has newly become `unverifiable`.
- `insyn build` **completes** when a provider is degraded; only standalone
  `insyn refdata marketcaps` exits non-zero (§7.4.3).
