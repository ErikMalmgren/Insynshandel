"""Phase 5 — reference data pipeline (§7). Independent of ingest; a failure here
must never leave ``transaction_norm`` half-built.

* :func:`fx` — Riksbank SWEA rates into ``fx_rate`` (§6.3.1).
* :func:`figi` — OpenFIGI ISIN→ticker into ``figi_lookup``, unknowns only (§7.1.1).
* :func:`build_companies` — the LEI-keyed ``company`` entity + name variants (§7.2).
* :func:`marketcaps` — pluggable providers append ``market_cap`` snapshots (§7.4).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from .. import config
from ..db import immediate
from ..sources import openfigi, riksbank
from ..sources.marketcap import Company, get_providers

log = logging.getLogger(__name__)


# ── FX ─────────────────────────────────────────────────────────────────────
@dataclass
class FxSummary:
    currencies: dict[str, int] = field(default_factory=dict)   # ccy -> rows upserted
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# (done, total, detail) — one SWEA request per currency, and a backfill request
# spans 2016->today, so the meter ticks *before* each fetch as well as after:
# the interesting moment is the one currency currently in flight.
FxProgress = Callable[[int, int, str], None]


def fx(
    conn: sqlite3.Connection,
    client: riksbank.RiksbankClient | None = None,
    *,
    backfill: bool = False,
    days: int = 10,
    progress: FxProgress | None = None,
) -> FxSummary:
    client = client or riksbank.RiksbankClient()
    start = (
        date.fromisoformat(config.FI_EARLIEST) if backfill
        else config.today() - timedelta(days=days)
    )
    end = config.today()
    s = FxSummary()
    total = len(config.FX_CURRENCIES)
    for done, ccy in enumerate(config.FX_CURRENCIES, start=1):
        if progress:
            progress(done - 1, total, f"fetching {ccy} {start}..{end}")
        try:
            obs = client.observations(ccy, start, end)
        except riksbank.RiksbankError as exc:
            s.errors.append(f"{ccy}: {exc}")
            if progress:
                progress(done, total, f"{ccy} FAILED")
            continue
        with immediate(conn):
            conn.executemany(
                "INSERT INTO fx_rate (currency, rate_date, sek_per_unit) "
                "VALUES (?, ?, ?) ON CONFLICT(currency, rate_date) "
                "DO UPDATE SET sek_per_unit = excluded.sek_per_unit",
                [(ccy, d, v) for d, v in obs],
            )
        s.currencies[ccy] = len(obs)
        if progress:
            progress(done, total, f"{ccy} {len(obs)} rows")
    return s


# ── OpenFIGI ───────────────────────────────────────────────────────────────
@dataclass
class FigiSummary:
    asked: int = 0
    resolved: int = 0
    negative: int = 0
    transport_errors: int = 0


# (done, total, live summary) — fired after every ISIN, transport errors included.
# OpenFIGI is one request per ISIN at 2.4 s spacing unauthenticated, so a full run
# is ~an hour; the CLI passes a printer so the terminal is not silent (§7.1.1).
FigiProgress = Callable[[int, int, FigiSummary], None]


def _isins_to_resolve(conn: sqlite3.Connection) -> list[str]:
    return [
        r["isin"] for r in conn.execute(
            """
            SELECT DISTINCT n.isin FROM transaction_norm n
            LEFT JOIN figi_lookup f ON f.isin = n.isin
            WHERE n.isin <> ''
              AND (f.isin IS NULL
                   OR (f.ticker IS NULL AND f.attempts < 5
                       AND f.looked_up_at < date('now', '-90 days')))
            """
        )
    ]


def figi(
    conn: sqlite3.Connection, client: openfigi.OpenFIGIClient | None = None,
    *, limit: int | None = None, progress: FigiProgress | None = None,
) -> FigiSummary:
    client = client or openfigi.OpenFIGIClient()
    isins = _isins_to_resolve(conn)
    if limit:
        isins = isins[:limit]
    s = FigiSummary(asked=len(isins))
    now = config.now_local_iso()
    for done, isin in enumerate(isins, start=1):
        try:
            res = client.map_isin(isin)
        except Exception as exc:  # noqa: BLE001 — transport failure: do NOT cache
            s.transport_errors += 1
            conn.execute(
                "UPDATE figi_lookup SET last_error = ?, attempts = attempts + 1 "
                "WHERE isin = ?", (str(exc)[:200], isin),
            )
            if progress:
                progress(done, s.asked, s)
            continue
        with immediate(conn):
            conn.execute(
                "INSERT INTO figi_lookup (isin, ticker, raw_ticker, name, exch_code, "
                "mic_code, looked_up_at, attempts) VALUES (?, ?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(isin) DO UPDATE SET ticker=excluded.ticker, "
                "raw_ticker=excluded.raw_ticker, name=excluded.name, "
                "exch_code=excluded.exch_code, mic_code=excluded.mic_code, "
                "looked_up_at=excluded.looked_up_at, attempts=figi_lookup.attempts+1, "
                "last_error=NULL",
                (isin, res.ticker, res.raw_ticker, res.name, res.exch_code,
                 res.mic_code, now),
            )
        if res.ticker:
            s.resolved += 1
        else:
            s.negative += 1
        if progress:
            progress(done, s.asked, s)
    return s


# ── company entity (§7.2) ──────────────────────────────────────────────────
_DISPLAY_NAME = """
SELECT issuer_name FROM transaction_norm
 WHERE lei = :lei AND transaction_date >= date(:today, '-12 months')
 GROUP BY issuer_name ORDER BY COUNT(*) DESC, MAX(transaction_date) DESC LIMIT 1
"""
_DISPLAY_NAME_FALLBACK = """
SELECT issuer_name FROM transaction_norm
 WHERE lei = :lei ORDER BY transaction_date DESC LIMIT 1
"""
_PRIMARY_ISIN = """
SELECT isin FROM transaction_norm
 WHERE lei = :lei AND instrument_type = 'Aktie' AND isin <> ''
 GROUP BY isin ORDER BY COUNT(*) DESC, MAX(transaction_date) DESC LIMIT 1
"""
_ANY_ISIN = """
SELECT isin FROM transaction_norm
 WHERE lei = :lei AND isin <> ''
 GROUP BY isin ORDER BY COUNT(*) DESC, MAX(transaction_date) DESC LIMIT 1
"""


@dataclass
class CompanySummary:
    companies: int = 0
    with_ticker: int = 0
    name_variants: int = 0


def build_companies(conn: sqlite3.Connection) -> CompanySummary:
    today = config.today().isoformat()
    leis = [r["lei"] for r in conn.execute(
        "SELECT DISTINCT lei FROM transaction_norm WHERE lei <> ''"
    )]
    s = CompanySummary()
    now = config.now_local_iso()

    with immediate(conn):
        conn.execute("DELETE FROM company")
        conn.execute("DELETE FROM company_name_variant")
        for lei in leis:
            name = (conn.execute(_DISPLAY_NAME, {"lei": lei, "today": today}).fetchone()
                    or conn.execute(_DISPLAY_NAME_FALLBACK, {"lei": lei}).fetchone())
            display = name["issuer_name"] if name else lei
            isin = (conn.execute(_PRIMARY_ISIN, {"lei": lei}).fetchone()
                    or conn.execute(_ANY_ISIN, {"lei": lei}).fetchone())
            primary_isin = isin["isin"] if isin else None

            figi = None
            if primary_isin:
                figi = conn.execute(
                    "SELECT raw_ticker, mic_code, exch_code, ticker FROM figi_lookup "
                    "WHERE isin = ? AND ticker IS NOT NULL", (primary_isin,),
                ).fetchone()

            conn.execute(
                "INSERT INTO company (lei, display_name, primary_isin, raw_ticker, "
                "mic_code, exch_code, ticker_source, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (lei, display, primary_isin,
                 figi["raw_ticker"] if figi else None,
                 figi["mic_code"] if figi else None,
                 figi["exch_code"] if figi else None,
                 "openfigi" if figi else None, now),
            )
            s.companies += 1
            if figi:
                s.with_ticker += 1

            for v in conn.execute(
                "SELECT issuer_name, MIN(transaction_date) f, MAX(transaction_date) l, "
                "COUNT(*) n FROM transaction_norm WHERE lei = ? GROUP BY issuer_name",
                (lei,),
            ):
                conn.execute(
                    "INSERT INTO company_name_variant (lei, name, first_seen, "
                    "last_seen, n_rows) VALUES (?,?,?,?,?)",
                    (lei, v["issuer_name"], v["f"], v["l"], v["n"]),
                )
                s.name_variants += 1
    return s


# ── market caps (§7.4) ─────────────────────────────────────────────────────
@dataclass
class MarketCapSummary:
    attempted: int = 0
    written: int = 0
    by_provider: dict[str, int] = field(default_factory=dict)
    failures: int = 0
    degraded: list[str] = field(default_factory=list)


# (provider name, done, total) — the provider ticks over its addressable
# companies; the CLI rotates one meter per provider.
MarketCapProgress = Callable[[str, int, int], None]


def _companies(conn: sqlite3.Connection) -> list[Company]:
    return [
        Company(r["lei"], r["display_name"], r["primary_isin"], r["raw_ticker"],
                r["mic_code"], r["exch_code"])
        for r in conn.execute(
            "SELECT lei, display_name, primary_isin, raw_ticker, mic_code, exch_code "
            "FROM company"
        )
    ]


def marketcaps(
    conn: sqlite3.Connection, *, providers: list | None = None, standalone: bool = False,
    progress: MarketCapProgress | None = None,
) -> MarketCapSummary:
    from .fx import FxTable

    companies = _companies(conn)
    s = MarketCapSummary(attempted=len(companies))
    if not companies:
        return s
    fxt = FxTable.load(conn)
    remaining = {c.lei: c for c in companies}

    for provider in providers or get_providers():
        if not remaining:
            break
        batch = list(remaining.values())
        tick = (
            # p=provider: bound at creation, not call time. Harmless today (the
            # callback only runs inside this iteration's provider.fetch), but
            # B023 flags the shape, and the shape is one refactor from a bug.
            (lambda done, total, p=provider: progress(p.name, done, total))
            if progress else None
        )
        quotes, failures = provider.fetch(batch, progress=tick)
        s.by_provider[provider.name] = len(quotes)
        s.failures += len(failures)
        # denominator = companies this provider CAN address, not every unpriced one
        addressable = sum(1 for c in batch if provider.symbol_for(c) is not None)
        if addressable > 5:
            ratio = len(quotes) / addressable
            if ratio < config.MARKETCAP_DEGRADED_FLOOR:
                s.degraded.append(f"{provider.name} {len(quotes)}/{addressable}")

        with immediate(conn):
            for q in quotes:
                hit = fxt.rate(q.currency, q.as_of)
                mc_sek = q.market_cap * hit.sek_per_unit if hit else 0.0
                conn.execute(
                    "INSERT INTO market_cap (lei, as_of, market_cap, currency, "
                    "market_cap_sek, source) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(lei, as_of) DO UPDATE SET market_cap=excluded.market_cap, "
                    "currency=excluded.currency, market_cap_sek=excluded.market_cap_sek, "
                    "source=excluded.source",
                    (q.lei, q.as_of, q.market_cap, q.currency, max(mc_sek, 0.0), q.source),
                )
                remaining.pop(q.lei, None)
                s.written += 1

    if s.degraded:
        log.error("market-cap providers degraded: %s", "; ".join(s.degraded))
        if standalone:
            raise SystemExit(1)
    return s
