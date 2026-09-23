"""Read functions behind the static export (Phase 6). Each takes a read
connection and returns a :mod:`schemas` model; ``export_static.py`` serializes
it to one JSON file.

`pct_of_mcap` and `market_cap` are read **through `market_cap_current`** so the
0 sentinel is already SQL ``NULL`` before any arithmetic (§6.4). Never divide a
raw `market_cap` in Python here.
"""

from __future__ import annotations

import sqlite3

from . import config, schemas

FI_SEARCH = (
    "https://marknadssok.fi.se/Publiceringsklient/sv-SE/Search/Search"
    "?SearchFunctionType=Insyn&Utgivare={issuer}"
)


def _iso_now() -> str:
    return config.now_local_iso()


# ── meta.json ──────────────────────────────────────────────────────────────
def meta(conn: sqlite3.Connection) -> schemas.Meta:
    cov = conn.execute(
        "SELECT MIN(pub_date) lo, MAX(pub_date) hi FROM coverage_day"
    ).fetchone()
    missing = conn.execute(
        """
        WITH RECURSIVE d(day) AS (
          SELECT ? UNION ALL SELECT date(day, '+1 day') FROM d
           WHERE day < date('now', '-1 day')
        )
        SELECT d.day FROM d LEFT JOIN coverage_day c ON c.pub_date = d.day
         WHERE c.pub_date IS NULL ORDER BY d.day
        """,
        (config.FI_EARLIEST,),
    ).fetchall()
    missing_days = [r["day"] for r in missing]

    longest = cur = 0
    prev = None
    from datetime import date, timedelta

    for iso in missing_days:
        d = date.fromisoformat(iso)
        cur = cur + 1 if prev is not None and d == prev + timedelta(days=1) else 1
        longest = max(longest, cur)
        prev = d

    verification = {
        r["verification"]: r["n"]
        for r in conn.execute(
            "SELECT verification, COUNT(*) n FROM transaction_norm "
            "WHERE verification IS NOT NULL GROUP BY verification"
        )
    }
    unmapped = {
        r["nature"]: r["n"]
        for r in conn.execute(
            "SELECT nature, COUNT(*) n FROM transaction_norm "
            "WHERE nature NOT IN (SELECT karaktar FROM nature_map) GROUP BY nature"
        )
    }
    return schemas.Meta(
        coverage_first_day=cov["lo"],
        coverage_last_day=cov["hi"],
        missing_days=len(missing_days),
        longest_gap_days=longest,
        last_ingest_at=conn.execute(
            "SELECT MAX(finished_at) m FROM fetch_batch WHERE finished_at IS NOT NULL"
        ).fetchone()["m"],
        raw_live_rows=conn.execute("SELECT COUNT(*) c FROM raw_live").fetchone()["c"],
        norm_rows=conn.execute("SELECT COUNT(*) c FROM transaction_norm").fetchone()["c"],
        counted_rows=conn.execute(
            "SELECT COUNT(*) c FROM transaction_norm WHERE is_counted = 1"
        ).fetchone()["c"],
        currencies=[
            r["currency"] for r in conn.execute(
                "SELECT DISTINCT currency FROM transaction_norm "
                "WHERE currency <> '' ORDER BY currency"
            )
        ],
        unmapped_natures=unmapped,
        verification=verification,
        market_caps_known=conn.execute(
            "SELECT COUNT(*) c FROM market_cap_current WHERE market_cap_sek IS NOT NULL"
        ).fetchone()["c"],
        computed_at=_iso_now(),
    )


# ── leaderboard-{period}.json ──────────────────────────────────────────────
_LEADERBOARD_SQL = """
SELECT a.lei, a.period_start, a.period_end,
       COALESCE(co.display_name, a.lei) AS name,
       co.raw_ticker AS ticker,
       a.buy_value_sek, a.sell_value_sek, a.net_value_sek,
       a.tx_count, a.buyer_count, a.seller_count, a.n_unverifiable,
       mc.market_cap_sek AS market_cap,
       CASE WHEN mc.market_cap_sek > 0
            THEN a.net_value_sek / mc.market_cap_sek END AS pct_of_mcap
  FROM agg_company_period a
  LEFT JOIN company co ON co.lei = a.lei
  LEFT JOIN market_cap_current mc ON mc.lei = a.lei
 WHERE a.period = ?
"""


def leaderboard(conn: sqlite3.Connection, period: str) -> schemas.Leaderboard:
    if period not in config.AGG_PERIODS:
        raise ValueError(f"unknown period {period!r}")
    rows = conn.execute(_LEADERBOARD_SQL, (period,)).fetchall()
    entries = [
        schemas.LeaderboardEntry(
            lei=r["lei"], name=r["name"], ticker=r["ticker"] or None,
            net_value_sek=r["net_value_sek"], buy_value_sek=r["buy_value_sek"],
            sell_value_sek=r["sell_value_sek"], tx_count=r["tx_count"],
            buyer_count=r["buyer_count"], seller_count=r["seller_count"],
            n_unverifiable=r["n_unverifiable"],
            market_cap=r["market_cap"], pct_of_mcap=r["pct_of_mcap"],
            verification="ok" if r["market_cap"] is not None else "unverifiable",
        )
        for r in rows
    ]
    span = conn.execute(
        "SELECT period_start, period_end FROM agg_company_period WHERE period = ? LIMIT 1",
        (period,),
    ).fetchone()
    return schemas.Leaderboard(
        period=period,
        period_start=span["period_start"] if span else "",
        period_end=span["period_end"] if span else "",
        count=len(entries),
        entries=entries,   # UNSORTED — the client sorts (§8.2)
        computed_at=_iso_now(),
    )


# ── companies.json ─────────────────────────────────────────────────────────
def company_index(conn: sqlite3.Connection) -> schemas.CompanyIndex:
    """Complete company index (§8.2 — the client sorts and filters)."""
    rows = conn.execute(
        "SELECT lei, display_name, raw_ticker FROM company ORDER BY display_name"
    ).fetchall()
    return schemas.CompanyIndex(
        count=len(rows),
        companies=[
            schemas.CompanyIndexEntry(
                lei=r["lei"], name=r["display_name"] or r["lei"],
                ticker=r["raw_ticker"] or None,
            )
            for r in rows
        ],
        computed_at=_iso_now(),
    )


# ── company/{lei}.json ─────────────────────────────────────────────────────
# rows for a company, following any issuer_alias merge (§7.2.3)
_TX_FROM = (
    "FROM transaction_norm WHERE COALESCE("
    "(SELECT canonical_lei FROM issuer_alias WHERE alias_lei = transaction_norm.lei), "
    "transaction_norm.lei) = ?"
)
_TX_COLS = (
    "transaction_date, published_date, pdmr, position, nature, sign, is_counted, "
    "instrument_type, instrument_name, isin, volume, price, currency, "
    "gross_value_sek, verification, exclude_reason"
)


def _tx(r: sqlite3.Row) -> schemas.CompanyTransaction:
    return schemas.CompanyTransaction(
        transaction_date=r["transaction_date"], published_date=r["published_date"],
        pdmr=r["pdmr"], position=r["position"], nature=r["nature"], sign=r["sign"],
        is_counted=r["is_counted"], instrument_type=r["instrument_type"],
        instrument_name=r["instrument_name"], isin=r["isin"], volume=r["volume"],
        price=r["price"], currency=r["currency"], gross_value_sek=r["gross_value_sek"],
        verification=r["verification"] or "", exclude_reason=r["exclude_reason"],
    )


def company_detail(conn: sqlite3.Connection, lei: str) -> schemas.CompanyDetail | None:
    co = conn.execute(
        "SELECT lei, display_name, raw_ticker, primary_isin FROM company WHERE lei = ?",
        (lei,),
    ).fetchone()
    if co is None:
        return None

    mc = conn.execute(
        "SELECT market_cap_sek, as_of FROM market_cap_current WHERE lei = ?", (lei,)
    ).fetchone()
    variants = [
        schemas.NameVariant(name=r["name"], first_seen=r["first_seen"],
                            last_seen=r["last_seen"], n_rows=r["n_rows"])
        for r in conn.execute(
            "SELECT name, first_seen, last_seen, n_rows FROM company_name_variant "
            "WHERE lei = ? ORDER BY n_rows DESC", (lei,)
        )
    ]
    periods = [
        schemas.CompanyPeriod(
            period=r["period"], period_start=r["period_start"], period_end=r["period_end"],
            buy_value_sek=r["buy_value_sek"], sell_value_sek=r["sell_value_sek"],
            net_value_sek=r["net_value_sek"], tx_count=r["tx_count"],
            buyer_count=r["buyer_count"], seller_count=r["seller_count"],
        )
        for r in conn.execute(
            "SELECT * FROM agg_company_period WHERE lei = ? ORDER BY period", (lei,)
        )
    ]
    total = conn.execute(f"SELECT COUNT(*) c {_TX_FROM}", (lei,)).fetchone()["c"]
    recent = conn.execute(
        f"SELECT {_TX_COLS} {_TX_FROM} "
        "ORDER BY transaction_date DESC, published_date DESC LIMIT ?",
        (lei, config.COMPANY_TX_LIMIT),
    ).fetchall()

    return schemas.CompanyDetail(
        lei=co["lei"], name=co["display_name"] or co["lei"],
        ticker=co["raw_ticker"] or None, primary_isin=co["primary_isin"],
        market_cap=mc["market_cap_sek"] if mc else None,
        market_cap_as_of=mc["as_of"] if mc else None,
        verification="ok" if (mc and mc["market_cap_sek"] is not None) else "unverifiable",
        name_variants=variants, periods=periods,
        tx_count_total=total,
        recent_transactions=[_tx(r) for r in recent],
        computed_at=_iso_now(),
    )


# ── data-quality.json ──────────────────────────────────────────────────────
def data_quality(conn: sqlite3.Connection) -> schemas.DataQuality:
    rows = conn.execute(
        """
        SELECT n.lei, COALESCE(co.display_name, n.issuer_name) AS name,
               n.transaction_date, n.published_date, n.nature, n.volume, n.price,
               n.currency, n.gross_value_sek, n.exclude_reason, n.issuer_name,
               mc.market_cap_sek AS market_cap
          FROM transaction_norm n
          LEFT JOIN company co ON co.lei = n.lei
          LEFT JOIN market_cap_current mc ON mc.lei = n.lei
         WHERE n.verification = 'outlier'
         ORDER BY n.gross_value_sek DESC
        """
    ).fetchall()
    contested = conn.execute(
        """
        SELECT COUNT(*) c FROM (
          SELECT isin FROM transaction_norm
           WHERE instrument_type = 'Aktie' AND isin <> ''
           GROUP BY isin HAVING COUNT(DISTINCT lei) > 1
        )
        """
    ).fetchone()["c"]
    return schemas.DataQuality(
        outlier_count=len(rows),
        outliers=[
            schemas.DataQualityEntry(
                lei=r["lei"], name=r["name"], transaction_date=r["transaction_date"],
                published_date=r["published_date"], nature=r["nature"],
                volume=r["volume"], price=r["price"], currency=r["currency"],
                gross_value_sek=r["gross_value_sek"], market_cap=r["market_cap"],
                exclude_reason=r["exclude_reason"],
                fi_search_url=FI_SEARCH.format(issuer=r["issuer_name"].replace(" ", "+")),
            )
            for r in rows
        ],
        contested_isin_count=contested,
        computed_at=_iso_now(),
    )
