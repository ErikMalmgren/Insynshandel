<!-- Part of IMPLEMENTATION_PLAN.md. Section numbers (§N.M) are stable
     identifiers across all plan files — see the map in the index. -->

# Phase 0 — repo hygiene and toolchain
## 2. Repo layout

```
Insynshandel/
├── pyproject.toml
├── uv.lock                         (committed — §2.1)
├── .python-version                 (committed — 3.14, §2.1)
├── README.md                       (§15)
├── IMPLEMENTATION_PLAN.md          ← this file
├── .gitignore
├── .github/workflows/ingest.yml
├── data/
│   ├── insynshandel.db             (gitignored)
│   ├── cache/                      (gitignored — downloaded zips/CSVs)
│   └── seed/
│       ├── nature_map.csv          (committed — Karaktär → sign, §6.1)
│       ├── ticker_override.csv     (committed — manual LEI→ticker fixes, §7.3)
│       ├── market_cap_manual.csv   (committed — ManualProvider input, §7.4)
│       └── issuer_alias.csv       (committed — confirmed LEI merges, §7.2.3)
├── src/insynshandel/
│   ├── config.py                   paths, constants, env
│   ├── db.py                       connect(), migrate(), helpers
│   ├── migrations/
│   │   ├── 001_raw.sql
│   │   ├── 002_norm.sql
│   │   ├── 003_reference.sql
│   │   └── 004_agg.sql
│   ├── sources/
│   │   ├── fi.py                   FI export client + bisection
│   │   ├── openfigi.py             ISIN→ticker
│   │   ├── riksbank.py             FX rates → SEK
│   │   └── marketcap/              pluggable providers, §7.4
│   │       ├── base.py             Protocol + registry
│   │       ├── yahoo.py            yfinance (default)
│   │       └── manual.py           seed-file fallback
│   ├── pipeline/
│   │   ├── ingest.py               phase 1 (fetch + bisect + diff write)
│   │   ├── normalize.py            phase 2
│   │   ├── aggregate.py            phase 3 (classify, outliers, aggregates)
│   │   └── reference.py            phase 5 (figi / fx / marketcaps)
│   ├── reads.py                    read functions behind every JSON file
│   ├── schemas.py                  pydantic models (the JSON contract)
│   ├── export_static.py            phase 6
│   └── cli.py
└── tests/
    ├── fixtures/insyn_sample.csv   (UTF-16LE, ~50 rows, committed)
    ├── test_fi_client.py
    ├── test_normalize.py
    ├── test_aggregate.py
    └── test_export.py
```

### 2.1 Toolchain — `uv`, and why the interpreter is pinned

Use **`uv`**, not `pip` + `venv`. The reason is specific to this project, not
general enthusiasm: **every environment must agree on the same dependency set
and the same Python version.**

```
your laptop  ·  GitHub Actions
```

`pip freeze` produces a flat list of what happened to be installed on one
machine, for one Python version, one OS. `uv.lock` is a resolved, hashed,
**cross-platform** lockfile — `uv sync --frozen` installs a byte-identical
dependency tree everywhere. Given that `yfinance` is the flakiest thing in this
system (§7.4), removing "it resolved differently in CI" from the list of
possible causes is worth real money in debugging time. (The list once also
held a home server, a VPS and a Docker image — the API path, removed
2026-09-23.)

The second reason matters just as much: **`uv` manages the interpreter too.**
`pip` cannot. That closes the gap this plan had until now — your machine runs
**Python 3.14.7** while the Dockerfile originally said `3.12-slim`, so code
developed against 3.14 would have been deployed onto 3.12. With `uv` the
version is declared once and enforced everywhere:

```
.python-version        →  3.14
pyproject.toml         →  requires-python = ">=3.14"
```

Setup, once:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv init --python 3.14          # writes .python-version + pyproject.toml
uv add pydantic yfinance requests
uv add --dev pytest ruff
uv sync                        # creates .venv, installs from uv.lock
```

Day to day, `uv run <cmd>` replaces activating the venv — it resolves and syncs
first, so a stale environment cannot silently persist.

**Pin 3.14, not 3.12.** Your existing `.venv` already runs `pandas` 3.0.1 and
`yfinance` 1.2.0 on 3.14.7, so the dependency tree is known-good there. Pinning
down to 3.12 would be inventing a migration nobody asked for.

> **Commit `uv.lock` and `.python-version`.** They are the reproducibility
> guarantee; a repo with `pyproject.toml` alone is not reproducible. Delete the
> existing `.venv/` — `uv sync` recreates it, and it is already `.gitignore`d.

`pip` is not *wrong* here — it would work. `uv` is faster, but speed is the
least interesting reason: the lockfile and the pinned interpreter are what
actually prevent a class of bug this deployment shape invites.

CLI surface (`uv run insyn <cmd>`):

```
insyn db migrate
insyn ingest backfill  [--from 2016-07-01] [--to TODAY]   # once, ~30-45 min
insyn ingest recent    [--days 7]                         # hourly / nightly
insyn ingest gaps      [--dry-run] [--max-days 400]       # heal an outage, §4.7
insyn refdata figi | fx [--backfill] | marketcaps
insyn build                # normalize -> refdata -> aggregate; USE THIS (§5.0)
insyn normalize            # individual steps, for debugging only
insyn aggregate
insyn export-static --out dist/
insyn doctor               # runs the acceptance checks in §11
```

---
## 3. Phase 0 — repo hygiene (blocking)

Two things currently make this repo unpushable. Fix them first.

### 3.1 `lei-isin-20251108.csv` is 249 MB — delete it, don't replace it

GitHub hard-rejects files over 100 MB, so this file **cannot be committed**.

**It is also unnecessary.** The original scripts used GLEIF to map ISIN → LEI,
but **[VERIFIED]** the FI export already carries `LEI-kod` on **100%** of rows
and `ISIN` on **95%**, in the same row. GLEIF was answering a question the
source data already answers.

Measured over 4,334 distinct sampled rows / 679 issuers:

| | coverage |
| --- | --- |
| rows with an LEI | 4,334 / 4,334 (**100%**) |
| rows with an ISIN | 4,128 / 4,334 (95%) |
| issuers with ≥1 `Instrumenttyp = 'Aktie'` ISIN | 630 / 679 (**92%**) |
| issuers with any ISIN at all | 662 / 679 (97%) |
| issuers with no ISIN anywhere | 17 (2.5%) |

So: **delete the file, do not add a GLEIF fetcher.** The 17 issuers with no ISIN
are handled by `data/seed/ticker_override.csv` (§7.3), which you need anyway.

```bash
rm lei-isin-20251108.csv gleif_lei_isin_SE_old.csv isin.csv
```

This removes an entire external dependency, a 32 MB weekly download, a database
table, and the only thing blocking `git push`.

### 3.2 Not a git repo

```bash
git init
```

`.gitignore`:

```gitignore
.venv/
__pycache__/
*.pyc
data/insynshandel.db*
data/cache/
dist/
.~lock.*#
# large source data — fetched at build time, never committed
lei-isin-*.csv
gleif_lei_isin_SE_old.csv
insyn_*.csv
Insyn*.csv
!tests/fixtures/insyn_sample.csv
.env
```

### 3.3 Toolchain setup

Install `uv` and pin the interpreter before anything else — §2.1 explains why:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
rm -rf .venv                    # the existing venv is pip-managed; uv recreates it
uv init --python 3.14
uv add pydantic yfinance requests
uv add --dev pytest ruff
uv sync
```

Commit `pyproject.toml`, `uv.lock` and `.python-version`. Do **not** commit
`.venv/` — it is already ignored above.

### 3.4 Cleanup

- Delete `.~lock.Insyn2026-04-21.csv#` (stale LibreOffice lock).
- Move `Insyn2026-04-21.csv`, `isin.csv`, `company_map.csv`,
  `failed_isins.csv` out of the repo root into `data/cache/legacy/` — they are
  superseded but useful for cross-checking the migration.
- `failed_isins.csv` (62 rows) becomes the seed for
  `data/seed/ticker_override.csv` — see §7.3.
- Keep `script.py`, `lei_isin.py`, `marketCap.py`, `test_find_unknown.py` until
  phase 3 passes its parity check (§11.3), then delete them.

**Acceptance:** `git add -A && git status` shows no file over 10 MB.

---
