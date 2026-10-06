"""Read functions behind the static export (Phase 6). Each takes a read
connection and returns a :mod:`schemas` model; ``export_static.py`` serializes
it to one JSON file.

`market_cap` is read **through `market_cap_current`** so the 0 sentinel is
already SQL ``NULL`` before it reaches any JSON — the leaderboard divides by
it in the browser. Never read a raw `market_cap` here.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date
from typing import NamedTuple

from . import config, positions, schemas
from .sources.openfigi import display_ticker

FI_SEARCH = (
    "https://marknadssok.fi.se/Publiceringsklient/sv-SE/Search/Search"
    "?SearchFunctionType=Insyn&Utgivare={issuer}"
)


def _iso_now() -> str:
    return config.now_local_iso()


def _bare(ticker: str) -> str:
    return ticker.upper().replace("-", "").replace(" ", "")


def _ticker(raw: str | None, figi_name: str | None, yahoo: str | None) -> str | None:
    """`company.raw_ticker` is OpenFIGI verbatim ('TELE2B'); the site shows the
    class dash-separated ('TELE2-B').

    Yahoo's symbol spells the class out ('TELE2-B.ST') and is taken when it is
    the same ticker; OpenFIGI's name often does not ('BEIJER REF AB' for
    BEIJB), so it is only the fallback. No class in either stays undashed —
    guessing one from a trailing letter is what made TELIA into TELI-A."""
    if not raw:
        return None
    if yahoo and yahoo.endswith(".ST") and _bare(yahoo[:-3]) == _bare(raw):
        return yahoo[:-3]
    return display_ticker(raw, figi_name or "")


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
    from datetime import timedelta

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


# ── facts-meta.json + facts/{year}.json ────────────────────────────────────
# Every row with a trustworthy SEK value, whatever its nature, so the
# leaderboard can aggregate any nature / date range / instrument / role group in
# the browser. With the default natures (the counted ones) over a preset window
# it must reproduce agg_company_period exactly — `insyn doctor` checks that.
_FACTS_SQL = """
SELECT COALESCE(a.canonical_lei, n.lei) AS lei, n.transaction_date, n.pdmr,
       n.position, n.nature, n.instrument_type, n.gross_value_sek
  FROM transaction_norm n
  LEFT JOIN issuer_alias a ON a.alias_lei = n.lei
 WHERE n.value_exclude_reason IS NULL
   AND n.transaction_date BETWEEN :start AND :end
 ORDER BY n.transaction_date, n.published_at, n.raw_id
"""

# Legal forms, for the leaderboard's short name. Stripped repeatedly from the
# end ("Smart Eye Aktiebolag (publ)" -> "Smart Eye") and once from the front
# ("AB Volvo" -> "Volvo"); never from the middle ("Investment AB Öresund").
_PUBL = re.compile(r"\s*,?\s*\(publ\.?\)\s*$", re.IGNORECASE)
_FORM_TAIL = re.compile(
    r"[\s,]+(publ|ab|aktiebolag|asa|as|a/s|oyj|abp|plc|ltd|limited|inc|corp|"
    r"s\.a|sa|se|n\.v|nv|b\.v|bv|ag|gmbh)\.?$", re.IGNORECASE)
_FORM_HEAD = re.compile(r"^(ab|aktiebolaget)\s+", re.IGNORECASE)


def short_name(name: str) -> str:
    s = name.strip()
    while True:
        t = _FORM_TAIL.sub("", _PUBL.sub("", s)).strip()
        if t == s or len(t) < 2:
            break
        s = t
    t = _FORM_HEAD.sub("", s)
    return t if len(t) >= 2 else s


class Facts(NamedTuple):
    meta: schemas.FactsMeta
    years: list[schemas.FactsYear]
    position_unmatched: int     # rows whose Befattning matched no pattern


def facts(conn: sqlite3.Connection,
          windows: list[schemas.PeriodWindow] | None = None) -> Facts:
    windows = windows if windows is not None else period_windows(conn)
    span = next(w for w in windows if w.period == "all")
    epoch = date.fromisoformat(config.FI_EARLIEST)
    roles = positions.Classifier.load(conn)

    natures = conn.execute(
        "SELECT karaktar, direction, category, counted FROM nature_map ORDER BY rowid"
    ).fetchall()
    nature_ix = {r["karaktar"]: k for k, r in enumerate(natures)}

    rows = conn.execute(_FACTS_SQL, {"start": span.start, "end": span.end}).fetchall()

    instrument_rows: dict[str, int] = {}
    for r in rows:
        instrument_rows[r["instrument_type"]] = instrument_rows.get(r["instrument_type"], 0) + 1
    instruments = sorted(instrument_rows, key=lambda k: (-instrument_rows[k], k))
    instrument_ix = {name: k for k, name in enumerate(instruments)}

    leis = sorted({r["lei"] for r in rows})
    company_ix = {lei: k for k, lei in enumerate(leis)}
    # numbered per company in order of first appearance, so the same number at
    # two companies says nothing about whether it is the same person
    people: dict[str, dict[str, int]] = {lei: {} for lei in leis}
    day: dict[str, int] = {}

    cols: dict[int, dict[str, list[int]]] = {}
    for r in rows:
        tx = r["transaction_date"]
        if tx not in day:
            day[tx] = (date.fromisoformat(tx) - epoch).days
        year = cols.setdefault(int(tx[:4]), {k: [] for k in "dcpnirv"})
        seen = people[r["lei"]]
        pdmr = r["pdmr"]
        if pdmr is not None and pdmr not in seen:
            seen[pdmr] = len(seen)
        year["d"].append(day[tx])
        year["c"].append(company_ix[r["lei"]])
        year["p"].append(-1 if pdmr is None else seen[pdmr])
        year["n"].append(nature_ix[r["nature"]])
        year["i"].append(instrument_ix[r["instrument_type"]])
        year["r"].append(roles.mask(r["position"]))
        year["v"].append(round(r["gross_value_sek"]))

    info = {
        r["lei"]: r
        for r in conn.execute(
            "SELECT c.lei, c.display_name, c.raw_ticker, c.figi_name, "
            "y.symbol AS yahoo_symbol, mc.market_cap_sek "
            "FROM company c LEFT JOIN yahoo_symbol y ON y.isin = c.primary_isin "
            "LEFT JOIN market_cap_current mc ON mc.lei = c.lei"
        )
    }
    companies = []
    for lei in leis:
        co = info.get(lei)
        name = (co["display_name"] if co else None) or lei
        companies.append(schemas.FactsCompany(
            lei=lei, name=name, short_name=short_name(name),
            ticker=_ticker(co["raw_ticker"], co["figi_name"], co["yahoo_symbol"]) if co else None,
            market_cap=co["market_cap_sek"] if co else None,
        ))

    build = hashlib.sha256(json.dumps(
        [epoch.isoformat(), leis, [r["karaktar"] for r in natures], instruments,
         list(config.POSITION_GROUPS)], ensure_ascii=False,
    ).encode()).hexdigest()[:16]
    now = _iso_now()
    years = [
        schemas.FactsYear(build=build, year=y, count=len(c["d"]), computed_at=now, **c)
        for y, c in sorted(cols.items())
    ]
    meta = schemas.FactsMeta(
        build=build,
        epoch=epoch.isoformat(),
        windows=windows,
        years=[y.year for y in years],
        companies=companies,
        natures=[
            schemas.FactsNature(name=r["karaktar"], direction=r["direction"],
                                category=r["category"] or "", default=bool(r["counted"]))
            for r in natures
        ],
        instruments=[schemas.FactsInstrument(name=k, rows=instrument_rows[k])
                     for k in instruments],
        position_groups=list(config.POSITION_GROUPS),
        row_count=len(rows),
        computed_at=now,
    )
    return Facts(meta, years, roles.unmatched)


# ── companies.json ─────────────────────────────────────────────────────────
def company_index(conn: sqlite3.Connection) -> schemas.CompanyIndex:
    """Complete company index (the client sorts and filters)."""
    rows = conn.execute(
        "SELECT c.lei, c.display_name, c.raw_ticker, c.figi_name, y.symbol AS yahoo_symbol "
        "FROM company c LEFT JOIN yahoo_symbol y ON y.isin = c.primary_isin "
        "ORDER BY c.display_name"
    ).fetchall()
    return schemas.CompanyIndex(
        count=len(rows),
        companies=[
            schemas.CompanyIndexEntry(
                lei=r["lei"], name=r["display_name"] or r["lei"],
                ticker=_ticker(r["raw_ticker"], r["figi_name"], r["yahoo_symbol"]),
            )
            for r in rows
        ],
        computed_at=_iso_now(),
    )


# ── company/{lei}.json ─────────────────────────────────────────────────────
# rows for a company, following any issuer_alias merge: its aliases' LEIs, plus
# its own unless it is itself an alias. Same rows as
# `COALESCE(<canonical of lei>, lei) = :lei`, but as `lei IN (...)` it can use
# ix_norm_lei_txdate — the COALESCE form scanned the whole table per company,
# which was ~99% of export-static's (and so doctor's) runtime.
_TX_FROM = (
    "FROM transaction_norm WHERE lei IN ("
    "SELECT alias_lei FROM issuer_alias WHERE canonical_lei = :lei "
    "UNION ALL "
    "SELECT :lei WHERE NOT EXISTS (SELECT 1 FROM issuer_alias WHERE alias_lei = :lei))"
)
_TX_COLS = (
    "transaction_date, published_date, issuer_name, pdmr, position, nature, sign, "
    "is_counted, instrument_type, instrument_name, isin, volume, price, currency, "
    "gross_value_sek, verification, exclude_reason"
)
# a total order: a tie on the dates alone left the LIMIT cutoff to the query
# plan, so the same DB could export different rows. Shared by the capped and
# the full query — the client relies on the capped list being an exact prefix
# of the full one, and groups adjacent rows by array order alone.
_TX_ORDER = "ORDER BY transaction_date DESC, published_at DESC, raw_id DESC"


def _tx(r: sqlite3.Row) -> schemas.CompanyTransaction:
    return schemas.CompanyTransaction(
        transaction_date=r["transaction_date"], published_date=r["published_date"],
        issuer_name=r["issuer_name"], pdmr=r["pdmr"], position=r["position"], nature=r["nature"], sign=r["sign"],
        is_counted=r["is_counted"], instrument_type=r["instrument_type"],
        instrument_name=r["instrument_name"], isin=r["isin"], volume=r["volume"],
        price=r["price"], currency=r["currency"], gross_value_sek=r["gross_value_sek"],
        verification=r["verification"] or "", exclude_reason=r["exclude_reason"],
    )


def period_windows(conn: sqlite3.Connection) -> list[schemas.PeriodWindow]:
    """Every aggregate window's bounds, in AGG_PERIODS order. Read from
    agg_company_period so they match the periods table exactly; a window with
    nothing counted anywhere has no row there and is recomputed instead."""
    stored = {
        r["period"]: (r["period_start"], r["period_end"])
        for r in conn.execute(
            "SELECT DISTINCT period, period_start, period_end FROM agg_company_period"
        )
    }
    today = config.today()
    windows = []
    for p in config.AGG_PERIODS:
        start, end = stored.get(p) or config.period_bounds(p, today)
        windows.append(schemas.PeriodWindow(period=p, start=start, end=end))
    return windows


def company_detail(
    conn: sqlite3.Connection, lei: str,
    windows: list[schemas.PeriodWindow] | None = None,
) -> schemas.CompanyDetail | None:
    co = conn.execute(
        "SELECT c.lei, c.display_name, c.raw_ticker, c.figi_name, c.primary_isin, "
        "y.symbol AS yahoo_symbol FROM company c "
        "LEFT JOIN yahoo_symbol y ON y.isin = c.primary_isin WHERE c.lei = ?",
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
    total = conn.execute(
        f"SELECT COUNT(*) n, COALESCE(SUM(is_counted), 0) k {_TX_FROM}", {"lei": lei}
    ).fetchone()
    recent = conn.execute(
        f"SELECT {_TX_COLS} {_TX_FROM} {_TX_ORDER} LIMIT :limit",
        {"lei": lei, "limit": config.COMPANY_TX_LIMIT},
    ).fetchall()

    return schemas.CompanyDetail(
        lei=co["lei"], name=co["display_name"] or co["lei"],
        ticker=_ticker(co["raw_ticker"], co["figi_name"], co["yahoo_symbol"]),
        primary_isin=co["primary_isin"],
        market_cap=mc["market_cap_sek"] if mc else None,
        market_cap_as_of=mc["as_of"] if mc else None,
        verification="ok" if (mc and mc["market_cap_sek"] is not None) else "unverifiable",
        name_variants=variants, periods=periods,
        windows=windows if windows is not None else period_windows(conn),
        tx_count_total=total["n"], tx_counted_total=total["k"],
        recent_transactions=[_tx(r) for r in recent],
        computed_at=_iso_now(),
    )


# ── company-tx/{lei}.json ──────────────────────────────────────────────────
def company_transactions(
    conn: sqlite3.Connection, lei: str,
) -> schemas.CompanyTransactions:
    """A company's full history — company_detail's query without the LIMIT."""
    rows = conn.execute(f"SELECT {_TX_COLS} {_TX_FROM} {_TX_ORDER}", {"lei": lei}).fetchall()
    return schemas.CompanyTransactions(
        lei=lei, count=len(rows),
        transactions=[_tx(r) for r in rows],
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
