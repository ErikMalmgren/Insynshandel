<!-- Part of IMPLEMENTATION_PLAN.md. Section numbers (§N.M) are stable
     identifiers across all plan files — see the map in the index. -->

# Phase 6 — static export and frontend wiring
## 9. Phase 6 — static export & frontend wiring

```
insyn export-static --out dist/
```

writes:

```
dist/meta.json
dist/leaderboard-30d.json
dist/leaderboard-90d.json
dist/leaderboard-365d.json
dist/leaderboard-all.json
dist/companies.json                 (index: lei, name, ticker — for client search)
dist/company/{lei}.json             (detail + recent transactions)
```

Each file is produced by calling the corresponding `reads.py` function and
dumping its Pydantic model (`schemas.py`). Expect **tens of MB** — 2049 companies
x `COMPANY_TX_LIMIT` (50) transactions is ~34 MB of indented JSON, 92% of it
`recent_transactions`. Indented, not minified, on purpose: text-diffable in git,
and Pages gzips on the wire.

The frontend reads `window.API_BASE`, default `'./data'` — static JSON next to
the pages, same origin, no CORS, no server. (The name is a leftover from the
removed API mode; see the note below.)

## 8. Read-surface rules

> Phase 4 (a FastAPI read API, §8) was built and then **removed on
> 2026-09-23** — the GitHub Actions → Pages path is the only deployment. The
> two rules below outlived it: they govern the static files and the frontend,
> and `doctor.py`, `test_export.py`, the frontend and invariant 15 cite them.

### 8.2 Where sorting lives — the rule is "sort where you paginate"

Client-side sorting is **not** an antipattern. It is the right choice for a
bounded result set and the wrong one for an unbounded set, and the deciding
question is not "who has more CPU" — it is **whether the client holds all the
rows**.

> **The rule: sorting and pagination must live on the same side.**
> Sorting client-side while paginating server-side sorts only the current page.
> That is the classic bug this rule exists to prevent, and it looks correct in
> testing because page 1 is usually right.

The static export paginates nothing, so the client sorts:

| File | Rows | Sorted by | Why |
| --- | --- | --- | --- |
| `leaderboard-{period}.json` | ~2000 | **Client** | Whole set fits in one file. Ship it complete and unsorted. |
| `companies.json` | ~2000 | **Client** | Same. |

Shipping the whole set means re-sorting is instant with no round trip,
filtering by verification state is free, and each file is a single cacheable
object a CDN can serve forever.

Consequences to honour:

- A leaderboard file holds **every** company for the window, with no
  server-side order or limit. Adding either would reintroduce the split-brain
  the rule forbids.
- It must therefore carry everything the client sorts by:
  `net_value_sek`, `buy_value_sek`, `sell_value_sek`, `tx_count`,
  `buyer_count`, `seller_count`, `market_cap`, `pct_of_mcap`, `verification`.
  With no server, a field the client cannot sort on is a field it cannot have.
- Revisit only if the company count grows by an order of magnitude.

### 8.1 Person data — deliberately limited

These are named private individuals, and the register is personal data under
GDPR. FI publishes it, but **republishing it in a more searchable form than the
original is a different act**, and this project deliberately does not do that.

**Build:**

- `pdmr` and `befattning` appear as fields on a company's transaction list,
  exactly as FI shows them on that company's own page.

**Do not build**, in the static export or the frontend:

- a person page, a person index, or a per-person JSON file
- a person filter on any list
- per-person aggregates, totals, or cross-company profiles
- a client-side person search over the exported JSON — a search index shipped
  to the browser is still a person index

This is a scope boundary, not a stylistic preference. An implementer who adds
"top insider buyers by person" because it seems like an obvious feature has
changed what the product is. If you later want it, that is a decision to take
deliberately, with a look at GDPR Art. 6 legitimate interest and the
journalistic exemption — not a side effect of a sprint.

The `pdmr` column is still stored and indexed in `transaction_norm`: it is
needed for `buyer_count` / `seller_count` (§6.5) and for the pair detection in
§6.1.2. Storing it is fine; exposing a search over it is the line.

---
---
### Acceptance checks — static export

The plan's own rule — don't start a phase until the previous one's checks pass —
needs checks for these two as well.

**Phase 6, static export:**

- Every file in `dist/` parses as JSON and is non-empty.
- `dist/leaderboard-30d.json` contains **every** company with activity in the
  window — no `limit`, no truncation (§8.2).
- Each leaderboard entry carries all sortable fields: `net_value_sek`,
  `buy_value_sek`, `sell_value_sek`, `tx_count`, `buyer_count`, `seller_count`,
  `market_cap`, `pct_of_mcap`, `verification`. A field the client cannot sort on
  is a field it cannot have (§8.2).
- No entry has `market_cap: 0` — that means the sentinel escaped
  `market_cap_current` (§6.4), i.e. a query read `market_cap` directly.
  A **negative `pct_of_mcap` is normal** and was wrongly part of this check
  until 2026-09-09: the SQL already requires `market_cap_sek > 0`, so a negative
  pct only ever means `net_value_sek < 0` — insiders were net sellers.
- No company file carries more than `COMPANY_TX_LIMIT` transactions. This is the
  only size assertion. (Checked directly since 2026-09-09 — the old "a few MB,
  not tens" byte proxy predated the real corpus and failed at a legitimate
  34 MB, without ever checking the history it was written to catch.)
- Total size of `dist/` is reported as an `INFO` line, not a gate — a byte
  threshold just re-fires as the register grows. Make it a check again if
  `dist/` is ever committed to a branch (`ingest.yml` 6b), where ~34 MB of
  hourly churn would bloat the repo.
