<!-- Part of IMPLEMENTATION_PLAN.md. Section numbers (§N.M) are stable
     identifiers across all plan files — see the map in the index. -->

# Scheduling and deployment
## 10. Scheduling & deployment

### 10.1 Static-only path (recommended start — free)

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

### 10.2 API path — portable by construction

The whole runtime is **one process and one file.** Nothing in this plan
requires a managed service, a cloud API, or a specific provider. At request time
the API talks to exactly one thing: a local SQLite file. Every external source
(FI, OpenFIGI, Riksbank, the market-cap provider) is contacted only
during ingest, as outbound HTTPS.

That is what makes a rented VPS and a machine in your flat the same deployment
with different networking around it.

```
┌─────────────────────────────────────────────────┐
│  host (VPS or home server, identical inside)    │
│                                                 │
│   systemd timer ──▶ insyn ingest / build        │  writer, periodic
│                            │                    │
│                            ▼                    │
│                   insynshandel.db  (WAL)        │  ← one file
│                            │                    │
│   caddy/nginx ──▶ uvicorn ─┘                    │  readers, always on
└─────────────────────────────────────────────────┘
```

#### 10.2.1 What actually differs between a VPS and a home server

Nothing in the application. Everything is in getting requests to it:

| Concern | VPS | Home server |
| --- | --- | --- |
| Inbound reachability | Public IP, just works | NAT port-forward, or a tunnel. **Many Swedish ISPs use CGNAT**, where port-forwarding is impossible and a tunnel is the only option |
| TLS certificate | Let's Encrypt HTTP-01, trivial | HTTP-01 needs inbound :80. Behind CGNAT use DNS-01, or let a tunnel terminate TLS |
| Stable address | Static IP | Dynamic IP → DDNS, or a tunnel |
| Uptime | Provider's problem | Power cuts, reboots, your ISP's maintenance |
| Backup | Snapshot + Litestream | **Must be offsite** — same building as the original is not a backup |

For the home case, a tunnel (Cloudflare Tunnel, Tailscale Funnel) solves
reachability, TLS and dynamic IP in one step and needs no inbound ports at all.
That is the recommended route if you are behind CGNAT.

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

#### 10.2.3 Read-only API connections

The ingest writes; the API only reads. Make that structural rather than
conventional:

```python
sqlite3.connect("file:data/insynshandel.db?mode=ro", uri=True)
```

WAL allows one writer concurrent with many readers, so the API keeps serving
while `insyn build` runs. A read-only connection means an API bug can never
take a write lock and stall the ingest, and it makes multi-worker uvicorn safe
by construction (§10.5).

#### 10.2.4 Splitting ingest from serving

Because the artifact is a single file, the two halves do not have to live on the
same machine:

- **Ingest at home, serve on a VPS** — run `insyn build` on the home box, then
  `rsync` or `litestream replicate` the DB to the VPS. The public surface has no
  outbound dependencies at all.
- **Everything on one host** — simplest, and the right default.

Do not run two writers against the same file from different machines. The
ingest is a single-writer design and there is no coordination for it.

Secrets (`OPENFIGI_API_KEY`) via env or a systemd `EnvironmentFile`. Nothing in
this system needs a secret to *read* FI data — the export endpoint is
unauthenticated.

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

### 10.4 Containerisation

Docker is **in scope**, for a reason specific to this project: §10.2 requires
the same application to run on a rented VPS and on a machine at home. That is
precisely the problem containers solve, and it is the cheapest way to guarantee
the two hosts behave identically.

It also *reduces* dependency risk rather than adding it. `yfinance` and its
transitive tree are the flakiest part of this system (§7.4); pinning the Python
version and the whole dependency set into an image means a `pip` resolution
change on one host cannot break the other.

Keep it minimal — two files, no orchestration:

```dockerfile
# Dockerfile — uv's own image, so the interpreter version is pinned by
# .python-version alone and there is no second place to keep in sync.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev            # cached unless the lockfile changes
COPY src/ ./src/
COPY data/seed/ ./data/seed/
ENV INSYN_DB=/data/insynshandel.db
VOLUME ["/data"]
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "insynshandel.api.app:app", \
     "--host", "0.0.0.0", "--port", "8000"]
```

```yaml
# docker-compose.yml
services:
  api:
    build: .
    restart: unless-stopped
    ports: ["127.0.0.1:8000:8000"]
    volumes: ["./data:/data"]          # bind mount, NOT a named volume
  ingest:
    build: .
    restart: "no"
    volumes: ["./data:/data"]
    entrypoint: ["uv", "run", "insyn", "build"]
```

Three rules that matter more than the files:

1. **The database lives in a bind-mounted volume, never in the image layer.**
   A container rebuild must not lose 180,000 rows and a decade of market-cap
   history. `./data:/data` keeps the file on the host where you can back it up.
2. **The `/data` bind mount must point at local disk**, for the WAL reason in
   §10.2.2. A container does not make a NAS mount safe.
3. **Run the ingest from the host's scheduler, not a container that sleeps.**
   A `systemd` timer calling `docker compose run --rm ingest` is simpler to
   observe and restart than a long-lived container running its own cron, and it
   keeps §10.3's cadence in one place.

Bind the API to `127.0.0.1` and let the host's Caddy or nginx terminate TLS and
proxy to it. Do not expose uvicorn directly.

### 10.5 Python as a backend — what to expect

Python is thoroughly ordinary as a backend language; FastAPI on uvicorn is a
production-grade stack. The mechanics differ from PHP or a Node script, so here
is the model explicitly.

**The stack, top to bottom:**

```
internet → Caddy/nginx      TLS, compression, static files, rate limiting
         → uvicorn          ASGI server — the actual HTTP process
         → FastAPI app      your routes
         → sqlite3 (ro)     the data
```

`uvicorn` is the equivalent of Node's `http` server: a long-running process that
owns the socket. It is **not** CGI — there is no per-request interpreter
startup. You run it under `systemd` (or Docker), and it stays up.

**Will it be fast enough?** Comfortably, and not close. Every request this API
serves is an indexed read from a precomputed table — the leaderboard is ~700
rows from `agg_company_period`. That is single-digit milliseconds, dominated by
JSON serialisation rather than SQLite. On the static-JSON path there is no
Python in the request path at all.

The workload is entirely I/O-bound, so the GIL is irrelevant here. Async
FastAPI on one uvicorn process handles hundreds of concurrent requests fine.

**Worker processes and SQLite.** `uvicorn --workers N` forks N processes sharing
one database file. That is safe **because the API opens read-only** (§10.2.3)
and WAL permits many concurrent readers. Start with `--workers 2`; there is no
reason to go higher at this scale.

> Never run the ingest inside a uvicorn worker. It is the single writer, it runs
> for minutes, and a request-triggered ingest would block a worker and race the
> scheduler. Ingest is always a separate process invoked by the scheduler.

**Things that genuinely bite people, and how this plan avoids them:**

| Common Python-backend problem | Why it does not apply here |
| --- | --- |
| Slow cold starts on serverless | Not serverless — a persistent process (§0.1) |
| Dependency drift between machines | `uv.lock` pins everything; Docker pins the interpreter too |
| Blocking calls stalling the event loop | The only slow I/O is ingest, which never runs in the web process |
| SQLite locking under concurrency | Read-only API connections + WAL + a single writer |
| Long-running requests timing out | No request does real work; everything is precomputed |

---
