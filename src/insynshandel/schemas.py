"""Pydantic models — the schema of every static JSON file (§9). No database
access here.

Money fields are plain SEK numbers, never formatted (§6.3). Missing market cap is
``None`` → serialized ``null``, never ``0`` (§6.4) — hence ``float | None`` with
no default rather than ``float = 0``.
"""

from __future__ import annotations

from pydantic import BaseModel

Verification = str  # 'ok' | 'unverifiable'  (never 'outlier' at company level, §6.4)


class Meta(BaseModel):
    coverage_first_day: str | None
    coverage_last_day: str | None
    missing_days: int
    longest_gap_days: int
    last_ingest_at: str | None
    raw_live_rows: int
    norm_rows: int
    counted_rows: int
    currencies: list[str]
    unmapped_natures: dict[str, int]
    verification: dict[str, int]
    market_caps_known: int
    computed_at: str


class LeaderboardEntry(BaseModel):
    lei: str
    name: str
    ticker: str | None
    net_value_sek: float
    buy_value_sek: float
    sell_value_sek: float
    tx_count: int
    buyer_count: int
    seller_count: int
    n_unverifiable: int
    market_cap: float | None
    pct_of_mcap: float | None
    verification: Verification


class Leaderboard(BaseModel):
    period: str
    period_start: str
    period_end: str
    count: int
    entries: list[LeaderboardEntry]
    computed_at: str


class CompanyIndexEntry(BaseModel):
    lei: str
    name: str
    ticker: str | None


class CompanyIndex(BaseModel):
    count: int
    companies: list[CompanyIndexEntry]
    computed_at: str


class NameVariant(BaseModel):
    name: str
    first_seen: str | None
    last_seen: str | None
    n_rows: int


class CompanyTransaction(BaseModel):
    transaction_date: str
    published_date: str
    pdmr: str            # a company's own transaction list, as FI shows it (§8.1)
    position: str
    nature: str
    sign: int | None
    is_counted: int
    instrument_type: str
    instrument_name: str
    isin: str
    volume: float | None
    price: float | None
    currency: str
    gross_value_sek: float | None
    verification: str
    exclude_reason: str | None


class CompanyPeriod(BaseModel):
    period: str
    period_start: str
    period_end: str
    buy_value_sek: float
    sell_value_sek: float
    net_value_sek: float
    tx_count: int
    buyer_count: int
    seller_count: int


class CompanyDetail(BaseModel):
    lei: str
    name: str
    ticker: str | None
    primary_isin: str | None
    market_cap: float | None
    market_cap_as_of: str | None
    verification: Verification
    name_variants: list[NameVariant]
    periods: list[CompanyPeriod]
    tx_count_total: int
    recent_transactions: list[CompanyTransaction]
    computed_at: str


class DataQualityEntry(BaseModel):
    lei: str
    name: str
    transaction_date: str
    published_date: str
    nature: str
    volume: float | None
    price: float | None
    currency: str
    gross_value_sek: float | None
    market_cap: float | None
    exclude_reason: str | None
    fi_search_url: str


class DataQuality(BaseModel):
    outlier_count: int
    outliers: list[DataQualityEntry]
    contested_isin_count: int
    computed_at: str
