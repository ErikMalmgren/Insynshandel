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
directly and dumping the Pydantic model. Expect a few MB total — fine for a
Pages repo, and text-diffable in git.

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
- No entry has `market_cap: 0` or a negative `pct_of_mcap` — both mean the
  sentinel escaped `market_cap_current` (§6.4).
- Total size of `dist/` is a few MB, not tens. A sudden jump means a per-company
  file is including full transaction history it shouldn't.
