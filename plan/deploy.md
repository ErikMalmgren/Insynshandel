<!-- Part of IMPLEMENTATION_PLAN.md. Section numbers (§N.M) are stable
     identifiers across all plan files — see the map in the index. -->

# Scheduling and deployment
## 10. Scheduling & deployment

### 10.1 Static-only path — the deployment

`.github/workflows/ingest.yml`:

1. Restore `insynshandel.db` from the latest GitHub Release asset
   (or `actions/cache`).
2. `insyn ingest gaps` (nightly only — heals any outage, §4.7)
3. `insyn ingest recent --days 7` (or `--days 90` on the nightly run)
4. `insyn build` — normalize → refdata → aggregate, in that order (§5.0)
5. `insyn export-static --out dist/`
6. Push `dist/` to the Pages repo; re-upload the DB as a release asset.

Step 4 is cheap (seconds over 180k rows) — always run the whole chain rather
than trying to detect whether anything changed. **Never call `normalize`
without `aggregate`**: it leaves every classification column `NULL` (§5.0).

Expect the DB around **100–150 MB** (~180k rows across the raw and normalized
tables, plus indexes). That is **over GitHub's 100 MB limit for git-tracked
files**, so committing it is not an option regardless of churn. Release assets
and `actions/cache` have much higher limits (2 GB) and are the right home.
**Only the small JSON gets committed.**

Measure the real size after the first backfill and confirm it fits whichever
mechanism you chose. `VACUUM` after backfill; consider not indexing
`raw_transaction` beyond what §4.4 lists.

Concurrency: `concurrency: { group: ingest, cancel-in-progress: false }` so two
runs never write the DB at once.

### 10.2 ~~API path~~ — removed 2026-09-23

A FastAPI reader behind Docker, Caddy and a systemd timer, on a VPS or a home
server. Built, never deployed, then removed: the static path (§10.1) covers the
whole product. §10.2.1, §10.2.3, §10.2.4, §10.4 and §10.5 went with it. §10.2.2
stays because it applies to any machine that holds the database — a laptop
working copy included (invariant 17).

#### 10.2.2 The one real trap: WAL on a network filesystem

> **Keep `insynshandel.db` on a local disk.** SQLite's WAL mode relies on shared
> memory and POSIX advisory locks that **NFS, SMB/CIFS and most network mounts
> do not implement correctly**. Put the database on a NAS share and you get
> corruption under concurrent access — intermittently, under load, in a way
> that looks like random data loss.
>
> This is the single most likely way a home deployment goes wrong, because
> putting data on the NAS is otherwise the obviously sensible instinct. Keep the
> live DB on the server's own SSD and **back up** to the NAS.

### 10.3 Cadence

| Job | Schedule | Cost per run | Writes when nothing changed |
| --- | --- | --- | --- |
| `ingest recent --days 7` | hourly, 06:00–20:00 CET Mon–Fri | ~1 request | **nothing** (§4.5) |
| `ingest recent --days 90` | nightly 03:00 CET | ~7 requests | **nothing** |
| `ingest recent --days 365` | monthly | ~26 requests | **nothing** |
| `refdata fx` | daily, after 16:00 CET | 4-6 requests (1/currency) | one row per currency per business day |
| `ingest gaps` | nightly, **before** the 90-day re-scan | 1 query when healthy | nothing (§4.7) |
| `refdata figi` | after each ingest | 0 requests once warm | nothing |
| `refdata marketcaps` | daily, after close (~18:00 CET) | ~500 yfinance calls | one row per company |
| `normalize` → `aggregate` → `export-static` | after every ingest | seconds | idempotent |

Notes on the FI cadence:

- **Hourly is fine and useful.** FI publishes continuously during business
  hours, so an hourly poll makes the site meaningfully fresher than a daily
  one. The diff write path (§4.5) is what makes it free.
- **Don't poll faster than every 15 minutes.** It gains nothing — reports
  trickle in — and it is impolite to a public agency's server. Whatever you
  pick, keep the 3 s spacing *within* a run.
- **Skip weekends and nights entirely.** Insider reports are filed on business
  days. An overnight hourly poll is 10 requests that will never find anything.
- `--days 7` rather than `--days 1` so a few days of outage self-heals with no
  intervention.

Only `refdata marketcaps` writes on every run by design — it appends one
`(lei, as_of)` snapshot per company per day, which is the point (§7.1).
