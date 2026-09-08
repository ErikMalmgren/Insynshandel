<!-- Part of IMPLEMENTATION_PLAN.md. Section numbers (§N.M) are stable
     identifiers across all plan files — see the map in the index. -->

# Phase 4 — API
## 8. Phase 4 — API

FastAPI in `api/`. All routes `GET`, all read-only, all cacheable.

```
GET /health                                   → {"status":"ok"}
GET /api/v1/meta                              → coverage span, last ingest,
                                                 row counts, unmapped natures,
                                                 verification breakdown
GET /api/v1/leaderboard?from=&to=             → UNSORTED, UNPAGINATED, complete
                                                 (~700 rows — client sorts, §8.2)
GET /api/v1/companies?q=&limit=&offset=       → search by name/ticker/LEI
GET /api/v1/companies/{lei}                   → detail + name variants + mcap
GET /api/v1/companies/{lei}/transactions      → paginated + server-sorted
      ?from=&to=&order=date|value&dir=&limit=&offset=
GET /api/v1/transactions                      → paginated + server-sorted
      ?from=&to=&lei=&isin=&order=&dir=&limit=&offset=
GET /api/v1/data-quality                      → rows marked outlier (§6.4)
```

Conventions:

- Pagination `limit` (default 50, max 500) + `offset`; every paginated response
  is `{"items": [...], "total": N, "limit": L, "offset": O}`.
- `Cache-Control: public, max-age=3600` — data changes hourly at most.
- Dates are `YYYY-MM-DD` and always refer to **transaction date**.
- **All money is SEK** (§6.3). Values are plain numbers. Never format money
  server-side — no thousands separators, no "kr", no rounding.
- A missing market cap serializes as `market_cap: null`, `pct_of_mcap: null`,
  `verification: "unverifiable"` — never `0` (§6.4).
- Errors: RFC 7807-ish `{"detail": "..."}` with a real status code.
- OpenAPI docs come free at `/docs`.

### 8.2 Where sorting lives — the rule is "sort where you paginate"

Client-side sorting is **not** an antipattern. It is the right choice for a
bounded result set and the wrong one for an unbounded set, and the deciding
question is not "who has more CPU" — it is **whether the client holds all the
rows**.

> **The rule: sorting and pagination must live on the same side.**
> Sorting client-side while paginating server-side sorts only the current page.
> That is the classic bug this rule exists to prevent, and it looks correct in
> testing because page 1 is usually right.

Applied here:

| Endpoint | Rows | Sorted by | Why |
| --- | --- | --- | --- |
| `/leaderboard` | ~700 | **Client** | Whole set fits in one response. Ship it complete and unsorted. |
| `/companies` | ~700 | **Client** | Same. |
| `/transactions` | ~180 000 | **Server** | Cannot ship to the browser; must paginate, therefore must sort. |
| `/companies/{lei}/transactions` | up to a few thousand | **Server** | Paginated for consistency with the above. |

**You are right about the leaderboard, and for a better reason than compute.**
At ~700 companies the payload is a few hundred KB — smaller than one photo.
Shipping it whole means re-sorting is instant with no round trip, filtering by
sector or verification state is free, and the endpoint is a single cacheable
object a CDN can serve forever. On the static-JSON path you chose (§1.1) there
is no request-time backend compute at all, so the saving is real but incidental
— the actual win is UX and cache behaviour.

Consequences to honour:

- `/leaderboard` returns **every** company for the window, with **no**
  `order`, `dir`, `limit` or `offset` parameters. Adding them later would
  reintroduce the split-brain the rule forbids.
- It must therefore return everything the client needs to sort by:
  `net_value_sek`, `buy_value_sek`, `sell_value_sek`, `tx_count`,
  `buyer_count`, `seller_count`, `market_cap`, `pct_of_mcap`, `verification`.
  A field the client cannot sort on is a field it will have to ask the server
  for.
- `/transactions` keeps `order` + `dir` on a **closed allow-list** of column
  names mapped to SQL — never string-interpolate a client-supplied column into
  a query.
- Revisit only if the company count grows by an order of magnitude. At ~700,
  and with the register adding a few dozen issuers a year, that is not a
  near-term concern.

Response models live in `api/schemas.py` as Pydantic models and are **the same
objects `export_static.py` serializes**. This is what keeps the two read paths
from drifting.

CORS as in §1.2.

### 8.1 Person data — deliberately limited

These are named private individuals, and the register is personal data under
GDPR. FI publishes it, but **republishing it in a more searchable form than the
original is a different act**, and this project deliberately does not do that.

**Build:**

- `pdmr` and `befattning` appear as fields on a company's transaction list,
  exactly as FI shows them on that company's own page.

**Do not build**, in the API or the static export:

- `GET /persons`, `GET /persons/{name}`, or any person index
- a `?pdmr=` filter on `/transactions`
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
### Acceptance checks — API

**Phase 4, API:**

- Every route returns the **same shape** as its static counterpart. Assert this
  mechanically: serialize the route handler's output and diff it against the
  corresponding `dist/` file. This is the check that keeps the two read paths
  from drifting, and it is worth more than any individual endpoint test.
- `/leaderboard` accepts no `order`, `dir`, `limit` or `offset` parameter —
  passing one is a 4xx, not a silent ignore (§8.2).
- `/transactions` rejects an `order` value outside the allow-list rather than
  interpolating it into SQL.
- No route exposes a person index: there is no `/persons`, and `?pdmr=` is not
  an accepted filter anywhere (§8.1).
- CORS returns the configured origin for a `github.io` preflight and does not
  return `*`.
- The database connection is read-only — an attempted write returns
  `sqlite3.OperationalError`, not a successful mutation (§10.2.3).
