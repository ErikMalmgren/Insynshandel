<!-- Part of IMPLEMENTATION_PLAN.md. Section numbers (§N.M) are stable
     identifiers across all plan files — see the map in the index. -->

# Phase 3 — classify and aggregate
## 6. Phase 3 — classify & aggregate

### 6.1 `nature_map` — the classification table

This is **the** correctness-critical piece, and it is a data table, not an
`if/elif` chain. Three outcomes, not two: `+1`, `-1`, and `0 = excluded, with a
recorded reason`.

**Exactly two values count: `Förvärv` (+1) and `Avyttring` (−1).** They map
directly onto "bought" and "sold" with no interpretation required. Everything
else is excluded with a recorded reason. The evidence is in §6.1.1 — read it
before changing anything here.

`data/seed/nature_map.csv`, loaded into a table by `insyn db migrate`:

```csv
karaktar,sign,counted,category,note
Förvärv,1,1,market,Open-market or negotiated acquisition
Avyttring,-1,1,market,Disposal
Teckning,0,0,subscription,Rights-issue subscription — excluded; mostly BTA/BTU interim paper (§6.1.1)
Tilldelning,0,0,compensation,54% aktieprogram and 45% priced at 0 — mostly free awards (§6.1.1)
Interntransaktion – Förvärv,0,0,internal,Transfer between own/related accounts
Interntransaktion – Avyttring,0,0,internal,Transfer between own/related accounts
Koncernintern överföring förvärv,0,0,internal,Intra-group transfer (older wording)
Koncernintern överföring avyttring,0,0,internal,Intra-group transfer (older wording)
Lösen ökning,0,0,paired_leg,Option exercise — paired with Lösen minskning
Lösen minskning,0,0,paired_leg,Option exercise — paired with Lösen ökning
Utbyte ökning,0,0,paired_leg,Exchange — paired
Utbyte minskning,0,0,paired_leg,Exchange — paired
Konvertering ökning,0,0,paired_leg,Conversion — paired
Konvertering minskning,0,0,paired_leg,Conversion — paired
Fusion ökning,0,0,corporate,Merger mechanics — not a discretionary purchase
Fusion minskning,0,0,corporate,Merger mechanics
Utdelning mottagen,0,0,non_market,Dividend in kind received
Utdelning lämnad,0,0,non_market,Dividend in kind given
Gåva mottagen,0,0,non_market,Gift received
Gåva lämnad,0,0,non_market,Gift given
Arv mottagen,0,0,non_market,Inheritance
Pantsättning,0,0,pledge,Pledge — no change of beneficial ownership
Pantsättning åter,0,0,pledge,Pledge released
Lån mottaget,0,0,lending,Securities borrowing
Lån utlåning,0,0,lending,Securities lending
Lån återgång ökning,0,0,lending,Loan return
Lån återgång minskning,0,0,lending,Loan return
Inlösen egenutfärdat instrument,0,0,corporate,Redemption of own issued instrument
Utfärdande av instrument,0,0,derivative,Writing an instrument
```

**There is no profile switch and no second definition.** One counted set, one
number. If the definition needs to change, change `counted` in this file and
re-run `insyn aggregate` — that is the only lever, and it lives in one place.

#### 6.1.1 Why neither Teckning nor Tilldelning counts

**[VERIFIED]** over 4,334 distinct sampled rows:

| | `Teckning` | `Tilldelning` |
| --- | --- | --- |
| rows | 238 | 162 |
| linked to `aktieprogram` | 9 (4%) | **87 (54%)** |
| priced at 0 | 10 (4%) | **73 (45%)** |
| dominant instrument | **BTA/BTU (139) — interim paper** | Aktie (100), Option (29) |

**`Tilldelning` is majority free compensation.** Counting it would rank every
company with a generous LTI programme as a heavy insider buyer each vesting
season — a calendar artifact, not a signal.

**`Teckning` is cash, but it lands on the wrong instrument.** 139 of 238 rows
are on **BTA/BTU** — *betald tecknad aktie/unit*, the interim paper issued
during a rights issue that later converts into the ordinary share. Counting the
subscription risks counting the same money twice when that conversion is also
reported, and untangling it would require tracing each rights issue end to end.

Excluding both removes that entire class of ambiguity. What remains —
`Förvärv` and `Avyttring` — needs no interpretation: bought and sold.

**What this costs, stated plainly:** an insider subscribing 20 MSEK of new
equity in a rights issue is invisible to the leaderboard. That is a real
conviction signal being dropped in exchange for a definition with no edge
cases. It is a deliberate trade, not an oversight — revisit it by setting
`counted = 1` for `Teckning` in `nature_map.csv` and running the BTA
double-count query kept in §14.2.

#### 6.1.2 Why the ökning/minskning families are excluded

**[VERIFIED]** they are **two rows describing one event**, not two events.
26 of 39 sampled `Lösen ökning` groups also contain a `Lösen minskning` for the
same `(LEI, person, transaction date)`, at the **same price**:

```
Lösen minskning   Teckningsoption   vol=50 000,0   pris=58,97
Lösen ökning      Aktie             vol=50 500,0   pris=58,97
```

The option is consumed; shares appear. Counting both double-counts; counting
one records an acquisition at a pre-agreed strike, which is compensation
vesting rather than a market view. Same structure for `Utbyte`, `Konvertering`
and `Interntransaktion` (15 of which co-occur as matched pairs).

Excluding both legs is the only option that is neither double-counted nor
arbitrary. `category = 'paired_leg'` marks them so this reasoning is
discoverable from the data.

**This seed list is incomplete by construction, and that is the point.** It was
built from ~6,500 sampled rows out of ~160,000. The register's vocabulary
drifted over ten years: sampling 2020 alone surfaced
`Koncernintern överföring förvärv` / `…avyttring`, an older wording for what is
now `Interntransaktion – …`. Assume the full backfill reveals more.

**The ingester must therefore count and report unmapped `karaktar` values.**
This is the structural fix for the bug described in §12.1 — an unknown value
must be a loud, counted, reported condition, never a silent `else: continue`.

```sql
SELECT nature, COUNT(*) FROM transaction_norm
 WHERE nature NOT IN (SELECT karaktar FROM nature_map)
 GROUP BY nature ORDER BY 2 DESC;
```

`insyn aggregate` prints this and **exits non-zero if it is non-empty**.
Adding a row to `nature_map.csv` and re-running is then the whole fix.

### 6.2 Row filter

A row is counted (`is_counted = 1`) when **all** hold:

1. `status = 'Aktuell'` — excludes `Reviderad` (superseded) and `Makulerad`
   (cancelled).
2. `nature_map.counted = 1` for its `nature`.
3. `volume_unit = 'Antal'`.
4. `volume` and `price` both parsed successfully.
5. `gross_value_sek` is not NULL — i.e. an FX rate was found (§6.3.2).
6. `verification <> 'outlier'` (§6.4). `unverifiable` **is** counted.

Otherwise `is_counted = 0` and `exclude_reason` records which rule fired.
Every row keeps a reason — "why isn't company X in the list" must be answerable
by a single query.

There is no profile parameter. See §6.1.

### 6.3 Currency — convert everything to SEK

The current code discards every non-SEK row. In the 985-row sample that is 58
rows: 27 CAD, 23 GBP, 5 EUR, 3 USD. SinterCast reports in GBP and vanishes
entirely. 20 issuers — Hexagon, Epiroc, Swedish Match, Monivent among them —
report in more than one currency.

**Rule: convert every row to SEK at ingest-derivation time and aggregate in SEK
only.** There are no per-currency buckets anywhere in the schema or the API.
This is the single biggest simplification in the plan: one currency, one
number, no per-currency array, no currency dimension on the aggregate
key, and no cross-currency ratio hazard in `pct_of_mcap`.

The original currency and amount are still stored on `transaction_norm`
(`currency`, `price`, `volume`) so nothing is lost and the conversion is always
auditable.

#### 6.3.1 The rate source — Riksbank SWEA

**[VERIFIED]** free, no auth, no key:

```
GET https://api.riksbank.se/swea/v1/Observations/{series}/{from}/{to}
Accept: application/json

→ [{"date":"2026-09-04","value":9.55128}, ...]      # value = SEK per 1 unit
```

Series IDs follow `SEK<CCY>PMI`. Verified working: `SEKUSDPMI`, `SEKEURPMI`,
`SEKGBPPMI`, `SEKCADPMI`. History goes back to at least **2016-07-01**, which
covers the entire register.

Two properties that will bite if ignored:

- **[VERIFIED] The series has gaps.** Weekends and Swedish public holidays
  return no observation at all — a request for `2026-09-03..2026-09-07` returns
  only the 3rd and 4th. **Forward-fill from the most recent earlier business
  day.** Never interpolate, never skip the row.
- **[VERIFIED] Rate limit is roughly 3 requests/minute** and returns
  `{"statusCode": 429, "message": "Rate limit is exceeded. Try again in 49
  seconds."}`. Fetch **one full date range per currency** — 4–6 requests total
  for the whole backfill — not one request per day. If the API rejects a
  decade-long range, chunk by year and sleep 25 s between calls.

#### 6.3.2 Which date's rate — the transaction date

Convert at the rate for the row's **`transaction_date`**, not today's rate.

> **This is a deliberate deviation from "current exchange rate".** Riksbank
> serves the historical series from the same endpoint at the same cost, so
> transaction-date conversion is the same amount of work and strictly more
> correct. For the 30/90/365-day windows the site actually shows, the two are
> indistinguishable. For the all-time view they are not: GBP/SEK moved from
> ~11.3 in 2016 to ~12.9 in 2026, and USD/SEK ranged 8–11 over the decade.
> Converting a 2016 trade at 2026 rates would misstate it by up to 30%.

Fall back to the latest available rate only when `transaction_date` is in the
future relative to the series (shouldn't happen) or the currency has no series
at all — and in that case set `gross_value_sek = NULL` and an exclude reason
rather than guessing. **Which** reason is the point:

| Reason | Meaning | `doctor` |
| --- | --- | --- |
| `no_fx_series` | the currency is in `config.FX_NO_SERIES` — the Riksbank publishes no SWEA series for it, verified by hand | tolerated, reported as INFO |
| `no_fx_rate` | we simply have not fetched the rate | **hard failure** |

A currency in neither `FX_CURRENCIES` nor `FX_NO_SERIES` falls to `no_fx_rate`
and fails the build. That asymmetry is deliberate: the day FI publishes a
transaction in a currency nobody has classified, it must surface loudly instead
of being forgiven by a catch-all and silently uncounted.

#### 6.3.2.1 This applies to the backfill too

**Every historical row is converted at its own transaction-date rate**, not at
whatever the rate was on the day you ran the backfill. There is no separate
"historical" code path — `gross_value_sek` is computed the same way for a 2016
row and a row filed this morning, because the rate is looked up per row.

That requires the FX series to be loaded **before** the first `aggregate` run:

```
insyn refdata fx --backfill      # 2016-07-01 → today, one call per currency
```

Roughly 4–6 requests total (one per currency over the whole range). If Riksbank
rejects a decade-long range, chunk by year and sleep 25 s between calls — at
~3 requests/minute that is ~50 requests and under 20 minutes, once.

**Order matters in the first build:**

```
insyn ingest backfill        # raw rows,  ~30-45 min
insyn refdata fx --backfill  # FX series, before anything is valued
insyn normalize
insyn refdata figi marketcaps
insyn aggregate              # needs BOTH fx_rate and market_cap populated
```

Rows whose currency has no rate on file come out `NULL` with
`exclude_reason = 'no_fx_rate'` — a visible, countable failure rather than a
silent zero. Check that count is 0 after the backfill. Currencies the Riksbank
does not publish at all get `no_fx_series` instead (§6.3.2); that count is
expected to be non-zero and small.

#### 6.2.1 Implausible unit price — the market-cap-independent guard

FI sometimes writes a **total amount into the `Pris` column**, so `volume *
price` squares it. Peab 2018-03-23 reads `27,993,250 x 240,741,950 SEK` = 6.7
quadrillion. Left alone these dominate every aggregate: 161 rows carried 99.99%
of the counted SEK total across the 2016-2026 corpus.

The outlier rule (§6.4) cannot catch them, because it needs a market cap and
only ~20% of issuers have one. This rule needs none, so it protects the other
80%. Two arms, **both required**:

1. `instrument_type` in `config.EQUITY_INSTRUMENT_TYPES` **and** the
   transaction-date SEK unit price exceeds `MAX_EQUITY_UNIT_PRICE_SEK`
   (10,000). Debt is excluded from the gate on purpose: a bond legitimately
   prices at nominal, `Kapitalandelsbevis` and `Företagscertifikat` at 10k+.
2. `volume == price` **and** `gross_value_sek` exceeds
   `AMOUNT_IN_BOTH_COLUMNS_FLOOR_SEK` (1 bn). Arm 1 misses a bond with its
   nominal in both columns; arm 2 catches it. The magnitude floor is what makes
   the structural test safe — 100 shares at 100 SEK satisfies `volume == price`
   honestly, and 24 such rows in the corpus stay counted.

Neither threshold is delicate: the highest legitimate equity unit price in the
corpus is Mangold at ~5,750 SEK and the lowest bad one is ~12,568.

> **Calibration is a judgment call, so record it.** The type list was derived
> from the 25-value `instrument_type` vocabulary on 2026-09-09. A type FI adds
> later is never flagged by arm 1 until someone adds it — the same
> fail-loud-not-silently trade-off as `config.FX_NO_SERIES`.

#### 6.3.3 Proportionality

Non-SEK rows are ~6% of the sample and likely 1–2% of the corpus. This is a
correctness fix, not a headline feature — build it simply and move on.

### 6.4 Outliers — the source contains uncorrected filing errors

**[VERIFIED]** these rows are live in the register with `Status = Aktuell`:

| Issuer | Published | Volume | Price | Gross | Karaktär |
| --- | --- | --- | --- | --- | --- |
| Swedbank AB | 2026-09-03 | 100 000 000 | 100 000 000 | **10 000 000 000 000 000 kr** | Teckning |
| Swedbank AB | 2026-03-25 | 50 000 000 | 50 000 000 | 2 500 000 000 000 000 kr | Teckning |
| Cell impact AB | 2020-03-30 | 20 070 | 271 190 | 5 442 783 300 kr | Avyttring |

Someone filed the same number into both the volume and the price field. Without
a filter, `SUM(volume * price)` puts Swedbank at the top of the leaderboard with
a number roughly 200 000× Sweden's GDP, permanently.

#### The rule — one test, deliberately

```
verification = 'outlier'  WHEN  market_cap_sek IS NOT NULL   -- via market_cap_current
                          AND  gross_value_sek > market_cap_sek
```

**A single insider transaction cannot exceed the entire issuer's market
capitalisation.** This is a structural impossibility, not a statistical guess,
which is why it is the only automatic test used.

Explicitly **rejected** alternatives, because each can be legitimately true:

- ~~price deviates >20× from the ISIN's median~~ — thin small caps, share-class
  differences and post-reverse-split prices all produce this honestly.
- ~~`volume == price`~~ — a genuine coincidence on round numbers.
- ~~winsorising at a percentile~~ — silently truncates real large trades, so the
  published figure stops being the real figure.

Verification: Swedbank's market cap is ~369 bn SEK; the bad row is 10¹⁶ →
caught. EQT's 19.9 bn SEK `Teckning` (58 722 850 × 339,10) is ~6% of a ~350 bn
market cap → correctly **kept**.

#### The coverage gap — and why there is no fallback threshold

**[VERIFIED]** only **507 of 682 LEIs (74%)** currently have a market cap.
For the other 175 the rule cannot fire, and those are exactly the small caps
where sloppy filings are most likely.

**There is deliberately no absolute-value fallback.** An invented ceiling
(50 bn, 10 bn, any number) would let every row beneath it pass the filter and
*appear verified* when nothing was actually checked. A number that looks
validated but wasn't is worse than an obvious blank.

Instead, verification is a **three-state** field, never a boolean:

```sql
verification TEXT NOT NULL CHECK (verification IN ('ok','outlier','unverifiable'))
```

| State | Condition | Counted? | Shown as |
| --- | --- | --- | --- |
| `ok` | market cap known **and** `gross_value_sek <= market_cap_sek` | yes | normal |
| `outlier` | market cap known **and** `gross_value_sek > market_cap_sek` | **no** | on the QA page |
| `unverifiable` | **no market cap** (`market_cap_sek = 0`) | yes | flagged "ej verifierad" |

#### The sentinel: `0`, and the guard that makes it safe

A missing market cap is stored as **`0`, meaning "we have none"**:

```sql
market_cap_sek REAL NOT NULL CHECK (market_cap_sek >= 0)
```

> **`0` is the safer sentinel, and deliberately so.** Both `0` and `-1` are
> unmistakable to a human reading the table, but they fail differently if the
> guard below is ever bypassed: `net / 0` raises `ZeroDivisionError` or yields
> infinity — loud, immediate, impossible to miss — while `net / -1` would
> silently produce a plausible negative percentage that renders happily on the
> page and is wrong. **Pick the sentinel whose bypass is noisy.**

**All reads go through a view. Nothing queries `market_cap` directly.**

```sql
CREATE VIEW market_cap_current AS
SELECT lei, as_of, currency, source,
       CASE WHEN market_cap_sek > 0 THEN market_cap_sek END AS market_cap_sek
  FROM market_cap m
 WHERE as_of = (SELECT MAX(as_of) FROM market_cap x WHERE x.lei = m.lei);
```

The `CASE` has no `ELSE`, so the sentinel becomes SQL `NULL`. From there the
language does the work for you: `net_value_sek / NULL` is `NULL`, `SUM()` skips
it, and a comparison against it is never true. The wrong number becomes
impossible rather than merely discouraged — and if someone does bypass the view,
they get a division error rather than a plausible-looking wrong answer.

Serialization follows automatically:

```
market_cap_sek IS NULL  →  API:  market_cap: null
                                 pct_of_mcap: null
                                 verification: "unverifiable"
                           UI:   "–" / "saknas"   (never "0 kr", never blank)
```

**Enforce it:** grep for `FROM market_cap` outside the view definition and the
writer in `reference.py`. Any other occurrence is a bug. Since percent of market
cap is one of the main reasons this project exists, this guard protects the
headline number — treat a bare `market_cap` read as a failing check, not a
style issue.

**What this costs, stated plainly:** a Swedbank-style filing error in a company
with no ticker **will** reach the leaderboard, marked `unverifiable`. That is
the accepted trade: a visibly-unchecked number rather than a
falsely-validated one. The mitigation is to resolve more tickers — every entry
added to `ticker_override.csv` (§7.3) converts an `unverifiable` company into a
checkable one.

Track the ratio and put it on the meta endpoint:

```sql
SELECT verification, COUNT(*) FROM transaction_norm GROUP BY verification;
```

If `unverifiable` stops shrinking over time, ticker resolution has stalled and
that is the thing to fix — not the filter.

#### Excluded, never deleted

Outliers stay in `raw_transaction` and `transaction_norm` with
`verification = 'outlier', exclude_reason = 'outlier_gross_exceeds_mcap'`. They are
surfaced at `GET /api/v1/data-quality` and on a public QA page, each linking to
FI's own record. Nothing is hidden; it is just not summed.

#### Phase dependency — do not miss this

This rule needs market caps, which come from the phase 5 reference pipeline.
That is the one place the clean phase ordering bends. Handle it explicitly:

- `insyn aggregate` runs the outlier pass **after** joining reference data.
- If no market caps exist yet (first run, or a `yfinance` outage), only the
  absolute fallback fires. **Log a loud warning with the count of rows that
  could not be tested** — do not fail, and do not silently pass everything.
- Re-running `insyn refdata marketcaps` must be followed by
  `insyn aggregate` to re-evaluate. The cron in §10.3 already orders them that
  way.

### 6.5 Aggregates — `migrations/004_agg.sql`

Aggregate on **`transaction_date`**, not `published_date`. Publication date is a
fetch-windowing concern only; conflating the two makes "net insider trading in
March" mean two different things in two different places.

> **⚠ LEI coverage is not 100% — invariants 9 & 12 are wrong for history.**
> Measured against the live export 2026-09-08:
>
> | window | rows | no LEI | no LEI *and* no ISIN |
> | --- | --- | --- | --- |
> | 2016-07 | 606 | 477 (78.7%) | 130 |
> | 2017-01 | 581 | 358 (61.6%) | 66 |
> | 2024-05 | 579 | 0 | 0 |
>
> Modern data is clean; pre-2018 is not. `PRIMARY KEY (lei, …)` and a bare
> `GROUP BY lei` silently collapse every LEI-less row into one bogus `''`
> bucket, or (if excluded) drop ~two thirds of 2016–2017. **Decide the key
> before writing `004_agg.sql`.** Options, cheapest first:
> 1. **Exclude** LEI-less rows with `exclude_reason = 'no_lei'`. Simple, honest,
>    loses early history. `is_counted = 0`.
> 2. **Recover** LEI from `issuer_name` via a seed map (`data/seed/issuer_alias.csv`
>    already exists for LEI↔LEI; extend or add `name→lei`). Most 2016 issuers
>    (Skanska, Sectra, …) have a modern LEI on later rows — derive the map from
>    the backfill, review by hand (§7.2's "re-measure after backfill").
> 3. **Composite key** `COALESCE(NULLIF(lei,''), 'name:'||issuer_name)` so
>    LEI-less issuers still aggregate, keyed on their (messy, rename-prone) name.
>
> `normalize` already carries `lei` verbatim (`''` when absent) and reports the
> count; `insyn doctor` prints the per-year breakdown.

```sql
CREATE TABLE agg_company_period (
  lei             TEXT NOT NULL,
  period_start    TEXT NOT NULL,    -- transaction_date window
  period_end      TEXT NOT NULL,
  buy_value_sek   REAL NOT NULL,    -- all values are SEK; see §6.3
  sell_value_sek  REAL NOT NULL,
  net_value_sek   REAL NOT NULL,    -- buy - sell
  tx_count        INTEGER NOT NULL,
  buyer_count     INTEGER NOT NULL, -- DISTINCT pdmr with sign=+1
  seller_count    INTEGER NOT NULL,
  n_unverifiable  INTEGER NOT NULL, -- counted rows with no market cap (§6.4)
  computed_at     TEXT NOT NULL,
  PRIMARY KEY (lei, period_start, period_end)
);
```

Precompute the windows the frontend uses: `30d`, `90d`, `365d`, `ytd`, `all`.
Arbitrary windows are computed on the fly by the API from `transaction_norm`
(fast — it's an indexed scan over ~180k rows).

`net_value = SUM(sign * volume * price)`.

**`tx_count` counts only rows with `is_counted = 1`** — the same population that
produced `net_value`. Rows excluded by §6.2 (pledges, lending, `Belopp` units,
`Reviderad`) are not in it. Otherwise the two columns describe different sets
and the leaderboard becomes unreadable.

Within that population, `price = 0` rows (64 of 985 in the sample — free
allotments and gifts) **do** count: they contribute 0 to `net_value` and 1 to
`tx_count`. That is correct and worth surfacing in the UI — "8 transactions,
0 kr" is meaningful information, not a bug.

### 6.6 Percent of market cap

Definition, to be **stated in the API response and on the site**, not left
implicit:

> `pct_of_mcap` = net insider flow over the selected transaction-date window,
> divided by the **most recent** market cap snapshot for that company.

It is a ratio of a period flow to a point-in-time stock. That is defensible and
is what the current code already does — but it must be labelled rather than
assumed. A historically-correct version needs market cap as of each transaction
date, which requires a price-history backfill. Out of scope; note it in the
README as a known limitation.

Return `null` (not `0`) when no market cap is known — **[VERIFIED]** 175 of 682
LEIs (26%) currently lack one. Never render a missing value as zero.

#### The currency trap — half solved, half still live

The numerator is handled: every transaction is converted to SEK (§6.3), so
`net_value_sek` is unambiguous.

**The denominator is not automatic.** `yfinance` returns `marketCap` in the
*listing's* currency, not SEK. **[VERIFIED]** `company_map.csv` holds 554 `.ST`
tickers (SEK) but also 2 with no exchange suffix and one `.Sg` — those market
caps are not SEK.

Rule: convert the market cap too, using the same `fx_rate` table.

```
market_cap_sek = market_cap * fx_rate(market_cap.currency, as_of)
pct_of_mcap    = net_value_sek / market_cap_sek     -- read via market_cap_current,
                                                    -- so NULL propagates on its own
```

Always read `fast_info['currency']` rather than assuming SEK. A company whose
market cap currency has no `fx_rate` series gets `market_cap_sek = 0` and is
therefore `unverifiable` (§6.4) — which is the correct outcome, not a bug.

---
---

## Acceptance checks
### 11.2 Classification

- Zero unmapped `karaktar` values across the full backfill. Any new value must
  fail the build, not be silently skipped.
- `SELECT DISTINCT exclude_reason FROM transaction_norm WHERE is_counted = 0`
  returns only reasons defined in §6.2 — never `NULL`. An uncounted row with no
  reason means a filter fired without recording why.
- Every row is either counted or has a reason:
  `SELECT COUNT(*) FROM transaction_norm WHERE is_counted = 0 AND exclude_reason IS NULL` → 0.
### 11.2.1 Outliers

- The Swedbank row (`2026-09-03`, volume = price = 100 000 000) has
  `verification = 'outlier'` and does not appear in any `agg_company_period`
  row.
- A large but legitimate `Förvärv` — say 6% of the issuer's market cap — has
  `verification = 'ok'`. **A rule that catches that one is too aggressive.**
- Every row of a company with `market_cap_sek = 0` has `verification = 'unverifiable'`
  and **is** included in `net_value_sek`. Assert this — the common mistake is
  silently dropping them.
- Outliers **not explained by §6.2.1** are few — but count them as
  `exclude_reason = 'outlier'`, never as "verification = 'outlier' minus
  implausible_unit_price". `exclude_reason` holds the FIRST hit of the ordered
  §6.2 chain and `outlier` is its second-to-last rule, so a row excluded earlier
  (`nature_not_counted`, `not_current`, `volume_unit`) keeps
  `verification = 'outlier'` while the cap never judged it.
- Measured over the full 2016-2026 backfill on 2026-09-09: 218 rows carry
  `verification = 'outlier'`; 20 are tagged `implausible_unit_price`, 189 were
  excluded by an earlier rule, and **9** actually reached the cap comparison and
  failed it. The plan originally predicted "single digits" — for this number,
  that was right. (An earlier revision of this bullet claimed "196 encoding
  errors / 22 genuine"; that came from the minus-implausible_unit_price count
  and was wrong.)
- The register really does contain impossible source rows — a Swedbank trade at
  9e16 SEK against a 428 bn cap, ÅF at 3.6e13 — but 78% of them are excluded
  before the cap is consulted, so they are FI's error, not a join failure.
- Some of the 9 are large-but-real trades measured against a market cap from
  a different year (§6.4 compares a historical transaction to the *current*
  cap). Revisit once `market_cap` has accumulated dated snapshots.
### 11.2.3 Currency (§6.3)

- `SELECT COUNT(*) FROM transaction_norm WHERE exclude_reason = 'no_fx_rate'`
  → **must be 0** after a successful `refdata fx --backfill`. If it is not, a
  currency is missing from `config.FX_CURRENCIES` — add it (SWEA has a series)
  or to `config.FX_NO_SERIES` (it does not), then re-run `insyn aggregate`.
- `exclude_reason = 'no_fx_series'` is expected to be non-zero: as of 2026-09-09
  the register carries 13 currencies, of which SCR and BWP have no SWEA series
  (6 rows over the whole backfill). `doctor` prints these as INFO, not FAIL.
- A GBP row from 2016 uses a 2016 rate, not today's. Check one directly:
  `fx_rate_date` must be within a few days of `transaction_date`, never near
  `date('now')`.
- A transaction dated on a weekend has `fx_rate_date` on the **preceding**
  business day — forward-filled, never interpolated, never skipped.
- `SELECT DISTINCT currency FROM transaction_norm` is fully covered by
  `SELECT DISTINCT currency FROM fx_rate` plus `SEK`.
### 11.3 Parity with the current script (run once, then delete the old code)

Point the new pipeline at the same window `Insyn2026-04-21.csv` covers
(`2026-03-13 .. 2026-04-21`), currency SEK, and compare the
top 20 by net value against `python script.py`.

**This is a directional check, not an equality check.** That file has 985 rows
against a 1000-row cap (§12.5) — it may itself be truncated, so a row-count
mismatch against a fresh fetch of the same window is *expected*, not a bug. Run
the comparison against the old CSV file, not a fresh fetch, and look for the
same companies in roughly the same order.

Expect these **known, intentional differences** — verify each is explained,
don't chase them to zero:

- **`Teckning` (90) and `Tilldelning` (56) still do not count** — same outcome
  as the `00`-prefixed original, now for a recorded reason (§6.1.1) rather than
  by a string that happened not to match. **The top-20 should be very close to
  the old output.** A large discrepancy here is a bug, not an expected
  difference.
- **Non-SEK rows now appear, converted to SEK** at the transaction-date rate
  (§6.3), instead of vanishing. SinterCast (GBP) is the visible example.
- **`Interntransaktion – …` (31 rows) is explicitly excluded** rather than
  falling through an unlabelled `else`. No numeric change; the reason is now
  recorded.
- **Aggregation is by transaction date, not publication date** — companies near
  the window edges will shift.
- **Outlier rows are excluded** (§6.4). None fall in this particular window, so
  this should produce no difference here — but check rather than assume.
