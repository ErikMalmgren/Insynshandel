"""Phase 3 — classify every ``transaction_norm`` row, then build the aggregates.

Two passes, in strict order (§6.2):

1. **classify** — per row, in one Python loop: value it in SEK (FX forward-fill),
   assign ``sign`` from ``nature_map``, decide ``verification`` against the
   issuer's market cap, then evaluate the ordered §6.2 filter and record either
   ``is_counted = 1`` or the first ``exclude_reason`` that fired.
2. **aggregate** — pure SQL ``GROUP BY`` canonical LEI over the counted rows,
   into ``agg_company_period`` for the precomputed windows.

An unmapped ``Karaktär`` fails the whole thing (§6.1.2).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from .. import config, db
from ..db import immediate
from .fx import FxTable

# The ordered §6.2 filter is the if/elif chain in classify(); the FIRST match
# wins, so its order is the contract. For reference / doctor:
#   no_lei -> not_current -> nature_not_counted -> volume_unit
#   -> unparseable_number -> no_fx_series / no_fx_rate
#   -> implausible_unit_price -> outlier -> (counted)
#
# `no_fx_series` and `no_fx_rate` are the same branch split by cause: the
# Riksbank publishes no series for this currency at all (permanent, listed in
# config.FX_NO_SERIES), versus we simply have not fetched the rate (fixable,
# and a hard `doctor` failure). Never collapse them — the second is a bug.
EXCLUDE_REASONS = (
    "no_lei", "not_current", "nature_not_counted", "volume_unit",
    "unparseable_number", "no_fx_series", "no_fx_rate",
    "implausible_unit_price", "outlier",
)


def _implausible_price(row, fx_rate: float, gross_sek: float) -> bool:
    """FI wrote a total amount where a unit price belongs (§6.2.1).

    Two arms, both needed. The type gate alone misses a bond whose nominal was
    written into both columns; `volume == price` alone misses an equity row
    where only `Pris` is wrong. Deliberately independent of market cap — it is
    the only encoding guard the ~80% of companies without one ever get.
    """
    if (row["instrument_type"] in config.EQUITY_INSTRUMENT_TYPES
            and row["price"] * fx_rate > config.MAX_EQUITY_UNIT_PRICE_SEK):
        return True
    return (row["volume"] == row["price"]
            and gross_sek > config.AMOUNT_IN_BOTH_COLUMNS_FLOOR_SEK)


@dataclass
class ClassifySummary:
    rows: int = 0
    counted: int = 0
    unmapped_natures: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    by_verification: dict[str, int] = field(default_factory=dict)
    market_caps_available: int = 0
    no_fx_rows: int = 0
    no_fx_series_rows: int = 0
    implausible_price_rows: int = 0

    @property
    def ok(self) -> bool:
        return not self.unmapped_natures


@dataclass
class AggregateSummary:
    classify: ClassifySummary
    periods: dict[str, int] = field(default_factory=dict)   # period -> company rows
    degraded_note: str | None = None

    @property
    def ok(self) -> bool:
        return self.classify.ok


_DERIVED_COLS = (
    "sign", "is_counted", "verification", "exclude_reason",
    "gross_value", "fx_rate_sek", "fx_rate_date", "gross_value_sek",
)
_UPDATE_SQL = (
    "UPDATE transaction_norm SET "
    + ", ".join(f"{c} = :{c}" for c in _DERIVED_COLS)
    + " WHERE raw_id = :raw_id"
)


def _unmapped_natures(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        r["nature"]: r["n"]
        for r in conn.execute(
            "SELECT nature, COUNT(*) n FROM transaction_norm "
            "WHERE nature NOT IN (SELECT karaktar FROM nature_map) "
            "GROUP BY nature ORDER BY n DESC"
        )
    }


def classify(conn: sqlite3.Connection) -> ClassifySummary:
    db.load_seeds(conn)  # nature_map / issuer_alias / ticker_override, fresh
    s = ClassifySummary()

    s.unmapped_natures = _unmapped_natures(conn)
    if s.unmapped_natures:
        return s  # caller prints and exits non-zero (§6.1.2)

    nature = {
        r["karaktar"]: (r["sign"], r["counted"])
        for r in conn.execute("SELECT karaktar, sign, counted FROM nature_map")
    }
    alias = {
        r["alias_lei"]: r["canonical_lei"]
        for r in conn.execute("SELECT alias_lei, canonical_lei FROM issuer_alias")
    }
    mcap = {
        r["lei"]: r["market_cap_sek"]           # NULL-filtered by the view
        for r in conn.execute("SELECT lei, market_cap_sek FROM market_cap_current")
    }
    s.market_caps_available = sum(1 for v in mcap.values() if v is not None)
    fx = FxTable.load(conn)

    rows = conn.execute(
        "SELECT raw_id, lei, pdmr, nature, transaction_date, volume, price, "
        "currency, volume_unit, status, instrument_type FROM transaction_norm"
    ).fetchall()
    s.rows = len(rows)

    updates: list[dict] = []
    for r in rows:
        sign, counted = nature[r["nature"]]
        canon = alias.get(r["lei"], r["lei"])
        mc = mcap.get(canon)  # SEK or None

        gross_value = fx_rate = fx_date = gross_sek = None
        if r["volume"] is not None and r["price"] is not None:
            gross_value = r["volume"] * r["price"]
            hit = fx.rate(r["currency"], r["transaction_date"])
            if hit is not None:
                fx_rate, fx_date = hit.sek_per_unit, hit.rate_date
                gross_sek = gross_value * fx_rate

        if mc is None:
            verification = "unverifiable"
        elif gross_sek is not None and gross_sek > mc:
            verification = "outlier"
        else:
            verification = "ok"

        # ── ordered §6.2 filter — first hit wins ──
        reason: str | None = None
        if not r["lei"]:
            reason = "no_lei"
        elif r["status"] != "Aktuell":
            reason = "not_current"
        elif counted == 0:
            reason = "nature_not_counted"
        elif r["volume_unit"] != "Antal":
            reason = "volume_unit"
        elif r["volume"] is None or r["price"] is None:
            reason = "unparseable_number"
        elif gross_sek is None:
            reason = ("no_fx_series" if r["currency"] in config.FX_NO_SERIES
                      else "no_fx_rate")
        elif _implausible_price(r, fx_rate, gross_sek):
            reason = "implausible_unit_price"
        elif verification == "outlier":
            reason = "outlier"
        is_counted = 1 if reason is None else 0

        if reason == "no_fx_rate":
            s.no_fx_rows += 1
        elif reason == "no_fx_series":
            s.no_fx_series_rows += 1
        elif reason == "implausible_unit_price":
            s.implausible_price_rows += 1
        s.by_reason[reason or "_counted"] = s.by_reason.get(reason or "_counted", 0) + 1
        s.by_verification[verification] = s.by_verification.get(verification, 0) + 1
        if is_counted:
            s.counted += 1

        updates.append({
            "raw_id": r["raw_id"],
            "sign": sign,
            "is_counted": is_counted,
            "verification": verification,
            "exclude_reason": reason,
            "gross_value": gross_value,
            "fx_rate_sek": fx_rate,
            "fx_rate_date": fx_date,
            "gross_value_sek": gross_sek,
        })

    with immediate(conn):
        conn.executemany(_UPDATE_SQL, updates)
    return s


# ── aggregate pass ─────────────────────────────────────────────────────────
def _period_bounds(period: str, today: date) -> tuple[str, str]:
    end = today.isoformat()
    if period == "all":
        return config.FI_EARLIEST, end
    if period == "ytd":
        return date(today.year, 1, 1).isoformat(), end
    days = {"30d": 30, "90d": 90, "365d": 365}[period]
    return (today - timedelta(days=days)).isoformat(), end


_AGG_SELECT = """
WITH counted AS (
  SELECT COALESCE(a.canonical_lei, n.lei) AS lei,
         n.pdmr, n.sign, n.gross_value_sek, n.verification, n.transaction_date
    FROM transaction_norm n
    LEFT JOIN issuer_alias a ON a.alias_lei = n.lei
   WHERE n.is_counted = 1
     AND n.transaction_date BETWEEN :start AND :end
)
SELECT
  lei,
  COALESCE(SUM(CASE WHEN sign = 1  THEN gross_value_sek END), 0) AS buy_value_sek,
  COALESCE(SUM(CASE WHEN sign = -1 THEN gross_value_sek END), 0) AS sell_value_sek,
  COUNT(*)                                                       AS tx_count,
  COUNT(DISTINCT CASE WHEN sign = 1  THEN pdmr END)              AS buyer_count,
  COUNT(DISTINCT CASE WHEN sign = -1 THEN pdmr END)              AS seller_count,
  SUM(CASE WHEN verification = 'unverifiable' THEN 1 ELSE 0 END) AS n_unverifiable
FROM counted
GROUP BY lei
"""


def aggregate_periods(conn: sqlite3.Connection) -> dict[str, int]:
    today = config.today()
    now = config.now_local_iso()
    written: dict[str, int] = {}
    with immediate(conn):
        conn.execute("DELETE FROM agg_company_period")
        for period in config.AGG_PERIODS:
            start, end = _period_bounds(period, today)
            n = 0
            for row in conn.execute(_AGG_SELECT, {"start": start, "end": end}).fetchall():
                buy, sell = row["buy_value_sek"], row["sell_value_sek"]
                conn.execute(
                    "INSERT INTO agg_company_period (lei, period, period_start, "
                    "period_end, buy_value_sek, sell_value_sek, net_value_sek, "
                    "tx_count, buyer_count, seller_count, n_unverifiable, computed_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["lei"], period, start, end, buy, sell, buy - sell,
                     row["tx_count"], row["buyer_count"], row["seller_count"],
                     row["n_unverifiable"], now),
                )
                n += 1
            written[period] = n
    return written


def run(conn: sqlite3.Connection) -> AggregateSummary:
    cls = classify(conn)
    summary = AggregateSummary(classify=cls)
    if not cls.ok:
        return summary
    summary.periods = aggregate_periods(conn)
    return summary
