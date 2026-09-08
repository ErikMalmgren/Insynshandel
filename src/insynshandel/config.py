"""Paths, constants and environment for the Insynshandel pipeline.

Nothing here reads the database or the network; it is import-safe everywhere.
"""

from __future__ import annotations

import os
import zoneinfo
from datetime import date, datetime
from pathlib import Path

# ── Layout ───────────────────────────────────────────────────────────────────
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent.parent
MIGRATIONS_DIR = PACKAGE_DIR / "migrations"
SEED_DIR = REPO_ROOT / "data" / "seed"

# The DB must live on local disk — SQLite WAL corrupts on NFS/SMB (CLAUDE.md #17).
DATA_DIR = Path(os.environ.get("INSYN_DATA_DIR", REPO_ROOT / "data"))
DB_PATH = Path(os.environ.get("INSYN_DB_PATH", DATA_DIR / "insynshandel.db"))
CACHE_DIR = DATA_DIR / "cache"

# ── FI export source (plan/01-ingest.md §4.1) ────────────────────────────────
FI_SEARCH_URL = "https://marknadssok.fi.se/Publiceringsklient/sv-SE/Search/Search"
FI_ENCODING = "utf-16-le"          # no BOM — never "utf-16"
FI_DELIMITER = ";"
FI_FIELD_COUNT = 23                # 22 real columns + trailing empty (§4.1.1)
FI_ROW_CAP = 1000                  # hard, per query (§4.2)
FI_EARLIEST = "2016-07-01"         # MAR entry into force; earlier queries return 0

# ── Time (§13.10) ───────────────────────────────────────────────────────────
# All FI timestamps are Europe/Stockholm local, no offset. "Today", for choosing
# which publication-date windows to fetch, means today *there*.
FI_TZ = zoneinfo.ZoneInfo("Europe/Stockholm")


def today() -> date:
    return datetime.now(FI_TZ).date()


def now_local_iso() -> str:
    """Wall-clock timestamp for *_fetched_at / *_at bookkeeping columns."""
    return datetime.now(FI_TZ).isoformat(timespec="seconds")


# ── Politeness (CLAUDE.md, §13.6) ────────────────────────────────────────────
REQUEST_SPACING_S = float(os.environ.get("INSYN_REQUEST_SPACING_S", "3.0"))
REQUEST_TIMEOUT_S = float(os.environ.get("INSYN_REQUEST_TIMEOUT_S", "60"))
MAX_REQUESTS_PER_RUN = int(os.environ.get("INSYN_MAX_REQUESTS_PER_RUN", "600"))
HTTP_MAX_RETRIES = 4              # exponential backoff on 5xx / connection reset
USER_AGENT = os.environ.get(
    "INSYN_USER_AGENT",
    "Insynshandel/0.1 (+https://github.com/erikm/Insynshandel; e.malmis@gmail.com)",
)

# ── Windowing (§4.2.1) ──────────────────────────────────────────────────────
SEED_WINDOW_DAYS = 14             # 7-day seeds never cap but cost ~2x requests
BISECT_FLOOR_DAYS = 1            # a single day still capped => data loss, flagged

# ── Incremental cadence (§4.3, §10.3) ───────────────────────────────────────
RECENT_DAYS_DEFAULT = 7
RESCAN_DAYS = 90

# ── Reference data (§7.4) ───────────────────────────────────────────────────
# Tried in order, first hit wins. Swapping providers is editing this list.
MARKETCAP_PROVIDERS = [
    p.strip() for p in os.environ.get("INSYN_MARKETCAP_PROVIDERS", "yahoo,manual").split(",")
    if p.strip()
]
FX_CURRENCIES = ("USD", "EUR", "GBP", "CAD")   # SWEA series exist for these
MARKETCAP_DEGRADED_FLOOR = 0.5                 # success ratio below which a provider is 'degraded'

# ── Aggregate windows (§6.5) — anchored to config.today(), not MAX(tx_date) ──
AGG_PERIODS = ("30d", "90d", "365d", "ytd", "all")
