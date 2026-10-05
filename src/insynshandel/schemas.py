"""Pydantic models — the schema of every static JSON file. No database
access here.

Money fields are plain SEK numbers, never formatted. Missing market cap is
``None`` → serialized ``null``, never ``0`` — hence ``float | None`` with
no default rather than ``float = 0``.
"""

from __future__ import annotations

from pydantic import BaseModel

Verification = str  # 'ok' | 'unverifiable'  (never 'outlier' at company level)


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
    issuer_name: str     # the name FI filed the row under — one of name_variants
    pdmr: str            # a company's own transaction list, as FI shows it
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


class PeriodWindow(BaseModel):
    """An aggregate window's inclusive transaction-date bounds. Shipped for
    every period, including the ones with nothing counted, so the client can
    filter the transaction list by the same window the periods table uses."""
    period: str
    start: str
    end: str


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
    windows: list[PeriodWindow]
    tx_count_total: int
    tx_counted_total: int   # of tx_count_total, how many are counted
    # the newest COMPANY_TX_LIMIT rows — an exact prefix of company-tx/{lei}.json
    recent_transactions: list[CompanyTransaction]
    computed_at: str


class CompanyTransactions(BaseModel):
    """company-tx/{lei}.json — the full history, in the same order as
    CompanyDetail.recent_transactions. Fetched only when the page needs it."""
    lei: str
    count: int
    transactions: list[CompanyTransaction]
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
