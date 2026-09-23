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

# The DB must live on local disk — SQLite WAL corrupts on NFS/SMB.
DATA_DIR = Path(os.environ.get("INSYN_DATA_DIR", REPO_ROOT / "data"))
DB_PATH = Path(os.environ.get("INSYN_DB_PATH", DATA_DIR / "insynshandel.db"))

# ── FI export source ─────────────────────────────────────────────────────────
FI_SEARCH_URL = "https://marknadssok.fi.se/Publiceringsklient/sv-SE/Search/Search"
FI_ENCODING = "utf-16-le"          # no BOM — never "utf-16"
FI_DELIMITER = ";"
FI_FIELD_COUNT = 23                # 22 real columns + trailing empty
FI_ROW_CAP = 1000                  # hard, per query
FI_EARLIEST = "2016-07-01"         # MAR entry into force; earlier queries return 0

# ── Time ────────────────────────────────────────────────────────────────────
# All FI timestamps are Europe/Stockholm local, no offset. "Today", for choosing
# which publication-date windows to fetch, means today *there*.
FI_TZ = zoneinfo.ZoneInfo("Europe/Stockholm")


def today() -> date:
    return datetime.now(FI_TZ).date()


def now_local_iso() -> str:
    """Wall-clock timestamp for *_fetched_at / *_at bookkeeping columns."""
    return datetime.now(FI_TZ).isoformat(timespec="seconds")


# ── Politeness ────────────────────────────────────────────
REQUEST_SPACING_S = float(os.environ.get("INSYN_REQUEST_SPACING_S", "3.0"))
REQUEST_TIMEOUT_S = float(os.environ.get("INSYN_REQUEST_TIMEOUT_S", "60"))
MAX_REQUESTS_PER_RUN = int(os.environ.get("INSYN_MAX_REQUESTS_PER_RUN", "600"))
HTTP_MAX_RETRIES = 4              # exponential backoff on 5xx / connection reset
USER_AGENT = os.environ.get(
    "INSYN_USER_AGENT",
    "Insynshandel/0.1 (+https://github.com/ErikMalmgren/Insynshandel; erik@malmgren.dev)",
)

# ── Windowing ───────────────────────────────────────────────────────────────
SEED_WINDOW_DAYS = 14             # 7-day seeds never cap but cost ~2x requests
BISECT_FLOOR_DAYS = 1            # a single day still capped => data loss, flagged

# ── Incremental cadence ─────────────────────────────────────────────────────
RECENT_DAYS_DEFAULT = 7
RESCAN_DAYS = 90

# ── Reference data ──────────────────────────────────────────────────────────
# Tried in order, first hit wins. Swapping providers is editing this list.
MARKETCAP_PROVIDERS = [
    p.strip() for p in os.environ.get("INSYN_MARKETCAP_PROVIDERS", "yahoo,manual").split(",")
    if p.strip()
]
# Every currency in the register that the Riksbank SWEA API publishes a series
# for. Must stay in sync with `sources.riksbank.SERIES`.
FX_CURRENCIES = ("USD", "EUR", "GBP", "CAD", "CHF", "NOK", "DKK", "RUB")
# Currencies that appear in the register and have **no** SWEA series at all —
# verified against https://api.riksbank.se/swea/v1/Series (117 series) on
# 2026-09-09. Rows in these convert to nothing and are excluded as
# `no_fx_series`: a documented impossibility, not a fetch gap.
#
# A currency missing from BOTH tuples yields `no_fx_rate`, which fails `doctor`.
# That asymmetry is the point — a new currency must surface loudly rather than
# be forgiven by a catch-all. Add it to FX_CURRENCIES if a series exists, or
# here (with the date you checked) if one does not.
FX_NO_SERIES = ("BND", "BSD", "BWP", "SCR", "SVC")
MARKETCAP_DEGRADED_FLOOR = 0.5                 # success ratio below which a provider is 'degraded'

# ── Implausible-price guard ─────────────────────────────────────────────────
# FI sometimes writes a TOTAL amount into the `Pris` column, so volume*price
# squares it. Peab 2018-03-23 reads 27,993,250 x 240,741,950 SEK = 6.7
# quadrillion. Unlike the outlier rule this needs no market cap, so it protects
# the ~80% of companies that have none.
#
# Instrument types whose `Pris` is genuinely a PER-UNIT price. Debt is absent on
# purpose: a bond legitimately prices at nominal (1,000,000/unit),
# Kapitalandelsbevis and Företagscertifikat at 10k+. Derived from the 25-value
# instrument_type vocabulary of the 2016-2026 corpus on 2026-09-09 — a type not
# listed here is never flagged by the unit-price arm, so add new ones as FI
# introduces them.
EQUITY_INSTRUMENT_TYPES = (
    "Aktie", "", "BTA (betald tecknad aktie)", "BTU (betald tecknad unit)",
    "Teckningsrätt", "Teckningsrätt/Uniträtt", "Interimsaktie", "Depåbevis",
    "Inlösenaktie", "Inlösenrätt",
)
# No Swedish share has traded near this. The highest legitimate unit price in
# the corpus is Mangold at ~5,750 SEK; the lowest bad one is ~12,568. The gap is
# wide, so the exact value is not delicate.
MAX_EQUITY_UNIT_PRICE_SEK = 10_000.0
# `volume == price` means FI put the same amount in both columns. That is a
# structural tell, but 100 shares at 100 SEK satisfies it honestly — the
# magnitude floor is what makes the test safe. 24 small rows legitimately match
# and stay counted; exactly one row in the corpus exceeds the floor.
AMOUNT_IN_BOTH_COLUMNS_FLOOR_SEK = 1_000_000_000.0

# ── Aggregate windows — anchored to config.today(), not MAX(tx_date) ─────────
AGG_PERIODS = ("30d", "90d", "365d", "ytd", "all")

# ── Static export ───────────────────────────────────────────────────────────
EXPORT_PERIODS = ("30d", "90d", "365d", "all")   # no ytd file
COMPANY_TX_LIMIT = 50                             # recent transactions per company detail
