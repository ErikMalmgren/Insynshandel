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

Each file is produced by calling the corresponding route handler function
directly and dumping the Pydantic model. Expect **tens of MB** — 2049 companies
x `COMPANY_TX_LIMIT` (50) transactions is ~34 MB of indented JSON, 92% of it
`recent_transactions`. Indented, not minified, on purpose: text-diffable in git,
and Caddy gzips on the wire. Fine for a Pages repo.

The frontend reads `window.API_BASE`:
- `API_BASE = './data'` → static JSON, no CORS, no server.
- `API_BASE = 'https://api.example.com/api/v1'` → live API.

Same fetch code either way.

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
  is a field it will have to ask the server for.
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
