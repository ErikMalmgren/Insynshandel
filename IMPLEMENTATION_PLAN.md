# Insynshandel — Implementation Plan

Rewrite of a collection of ad-hoc scripts into a pipeline, a static JSON export
and a read-only API, fed from Finansinspektionen's insider register.

**This file is the index.** The plan is split by phase so an implementing agent
loads only what the current task needs — roughly 2–6k tokens instead of 30k.
Start with `CLAUDE.md`, then open the one phase file you are working on.

Everything marked **[VERIFIED]** was tested against the live source on
2026-09-07. Trust those facts; do not re-derive them.

---

## Build order

Phases are ordered so a later decision cannot invalidate earlier work.
**Do not start a phase until the previous phase's acceptance checks pass.**

| # | Phase | File | ~tokens |
| --- | --- | --- | --- |
| 1 | **Phase 0** — repo hygiene, git, `uv`, the 249 MB file | [`plan/00-hygiene.md`](plan/00-hygiene.md) | 1.9k |
| 2 | **Phase 1** — fetch → raw store | [`plan/01-ingest.md`](plan/01-ingest.md) | 6.1k |
| 3 | **Phase 2** — normalize | [`plan/02-normalize.md`](plan/02-normalize.md) | 1.7k |
| 4 | **Phase 3** — classify & aggregate | [`plan/03-aggregate.md`](plan/03-aggregate.md) | 6.1k |
| 4b | **Phase 5** — reference data (OpenFIGI, FX, market caps) | [`plan/05-refdata.md`](plan/05-refdata.md) | 3.6k |
| 5 | **Phase 6** — static JSON export | [`plan/06-static.md`](plan/06-static.md) | 0.5k |
| 6 | **Phase 4** — API | [`plan/04-api.md`](plan/04-api.md) | 1.7k |
| 7 | **Phase 7** — the `insyn.malmgren.dev` frontend | [`plan/07-frontend.md`](plan/07-frontend.md) | 2.6k |
| — | Scheduling & deployment | [`plan/deploy.md`](plan/deploy.md) | 3.0k |
| — | Testing and `insyn doctor` | [`plan/testing.md`](plan/testing.md) | 0.4k |
| — | Rationale, known bugs, rejected options, README spec | [`plan/background.md`](plan/background.md) | 4.5k |

Phase 5 is a separate pipeline with no dependency on 1–3, but `insyn aggregate`
needs its FX rates and market caps, so build it alongside phase 3.

## Where a `§N.M` reference lives

Section numbers are **stable identifiers** and unchanged by the split.

| Reference | File |
| --- | --- |
| §1 architecture | `plan/background.md` |
| §2 repo layout · §3 phase 0 | `plan/00-hygiene.md` |
| §4 ingest · §11.1 · §11.2.2 | `plan/01-ingest.md` |
| §5 normalize | `plan/02-normalize.md` |
| §6 classify · §11.2 · §11.2.1 · §11.2.3 · §11.3 | `plan/03-aggregate.md` |
| §7 reference data · §11.2.4 | `plan/05-refdata.md` |
| §8 API | `plan/04-api.md` |
| §9 static export | `plan/06-static.md` |
| §10 deployment | `plan/deploy.md` |
| §11 intro · §11.4 unit tests | `plan/testing.md` |
| §12 bugs · §13 considerations · §14 decision log · §15 README | `plan/background.md` |
| §16 frontend | `plan/07-frontend.md` |
| §0 decisions · §0.1 rejected alternatives | this file, below |

## Invariants

The rules that fail silently live in [`CLAUDE.md`](CLAUDE.md), which Claude Code
loads on every turn. Read it before touching anything.

---

## 0. Decisions

These are settled. Do not re-litigate them during implementation.

| Question | Decision |
| --- | --- |
| Language | **Python 3.14**, pinned (§2.1). All existing code is Python; `pandas`/`yfinance` already in use; nothing here is CPU-bound. |
| Database | **SQLite**, single file, WAL mode. ~180k rows total (§4.6) — trivial for SQLite. No server process to host or pay for. |
| DB access | **Plain `sqlite3` + hand-written SQL** in `.sql` migration files. No ORM. (`peewee` in the current venv is a `yfinance` transitive dep, not a choice.) |
| API | **FastAPI + uvicorn**. Pydantic response models double as the schema for the static JSON export. |
| Packaging | **`uv`** — also pins the interpreter, so all five environments match (§2.1). Not installed yet: `curl -LsSf https://astral.sh/uv/install.sh \| sh`. |
| Frontend hosting | **GitHub Pages**, but on this repo at `insyn.malmgren.dev`, not in the `www.malmgren.dev` repo. Amended by §16.0; the reasoning in §1.2 still holds. |
| Aggregation key | **Transaktionsdatum** (economic date), *not* Publiceringsdatum (which is only used to window the fetch). |
| Currency | **Everything converted to SEK** at the transaction-date Riksbank rate. One currency everywhere; no per-currency buckets. See §6.3. |
| Counted transactions | **`Förvärv` (+1) and `Avyttring` (−1) only.** Everything else excluded with a recorded reason. One definition, no profile switch. See §6.1. |
| Outlier rule | **`gross_value_sek > issuer market cap`** only. No market cap means **unverifiable**, reported as such — never silently passed. See §6.4. |
| Person data | **Company-level aggregates only.** Names appear on a company's transaction list; there is no person index, search, or profile. See §8.1. |
| Build order | **0 → 1 → 2 → 3 → 6 → 4.** Static JSON ships before the API. See §1.1. |
| Ingest cadence | FI hourly on weekdays + nightly 90-day re-scan; OpenFIGI on-demand for unknown ISINs only; FX and market caps daily. See §10.3. |

### 0.1 Rejected alternatives (for the record)

- **Postgres** — nothing in the workload needs concurrent writers, and it adds a
  service to host. Revisit only if you add user accounts or write traffic.
- **Go / TypeScript backend** — would be fine, but throws away working Python and
  the `yfinance` integration for no benefit at this data size.
- **Serverless (Vercel / Lambda) for the API** — SQLite needs a persistent
  filesystem. Ephemeral FS makes this a poor fit. Use a small VM or Fly.io with
  a volume.
- **A message broker (RabbitMQ / Redis / Celery)** — there is no queue-shaped
  problem here. Ingest is a scheduled batch job over a known date range, not a
  stream of independently-arriving work items, and the whole backfill is ~330
  sequential requests deliberately rate-limited to one every three seconds. A
  broker would add a service to run, a failure mode to handle, and no capability
  the scheduler does not already provide. The retry-and-backoff behaviour a
  queue would give us already exists where it is actually needed, as
  `figi_lookup.attempts` + `looked_up_at` (§7.1.1) — durable across restarts and
  inspectable with SQL.
- **Kubernetes** — the application is one process and one SQLite file on one
  disk. SQLite cannot be scaled horizontally; replicas would each need their own
  copy and there is no consistency story. Orchestrating a single stateful pod is
  strictly more complexity than `systemd` or `docker compose` for identical
  behaviour. Docker itself **is** in scope (§10.4) because it solves a real
  problem here — identical behaviour on a VPS and a home server.

---

The architecture these decisions produce, the bugs in the current code they
fix, and the full decision log are in
[`plan/background.md`](plan/background.md).
