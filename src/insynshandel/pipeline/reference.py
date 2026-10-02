"""Phase 5 — reference data pipeline. Independent of ingest; a failure here
must never leave ``transaction_norm`` half-built.

* :func:`fx` — Riksbank SWEA rates into ``fx_rate``.
* :func:`figi` — OpenFIGI ISIN→ticker into ``figi_lookup``, unknowns only.
* :func:`build_companies` — the LEI-keyed ``company`` entity + name variants.
* :func:`yahoo_symbols` — Yahoo's ISIN→symbol into ``yahoo_symbol``, unknowns only.
* :func:`marketcaps` — pluggable providers append ``market_cap`` snapshots.
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
from ..sources.marketcap import yahoo as yahoo_source

log = logging.getLogger(__name__)


# ── FX ─────────────────────────────────────────────────────────────────────
# Fetched on demand, never on a timer. `fx_rate` is contiguous per currency up
# to its newest `rate_date` (the watermark), so any row dated on or before it
# already forward-fills to the right rate. A currency is fetched only when a
# row has moved past its watermark, and then only watermark+1..today. Most
# builds make zero SWEA calls (the spacing between calls is 25 s).
#
# A non-SEK market cap is valued at *today's* rate, so its currency is also
# refreshed once the watermark is more than this many days old — it only feeds
# the outlier check, a week of drift is irrelevant there.
FX_MARKETCAP_MAX_AGE_DAYS = 7


@dataclass
class FxSummary:
    currencies: dict[str, int] = field(default_factory=dict)   # ccy -> rows upserted
    errors: list[str] = field(default_factory=list)
    up_to_date: int = 0                 # currencies that needed no call

    @property
    def ok(self) -> bool:
        return not self.errors


# (done, total, detail) — one SWEA request per currency, and a backfill request
# spans 2016->today, so the meter ticks *before* each fetch as well as after:
# the interesting moment is the one currency currently in flight.
FxProgress = Callable[[int, int, str], None]


def _fx_needed(conn: sqlite3.Connection) -> dict[str, date]:
    """Currency -> first date to fetch, for every currency that needs a call.

    Only `config.FX_CURRENCIES` — an unclassified currency is not fetched here,
    it stays loud as `no_fx_rate` in `doctor`.
    """
    # ISO strings compared as strings, like FxTable: transaction_date is FI's
    # text unvalidated, and a malformed one must not crash the hourly build.
    today = config.today()
    watermark = dict(conn.execute(
        "SELECT currency, MAX(rate_date) FROM fx_rate GROUP BY currency").fetchall())
    # clamped to today: a future-dated typo must not refetch on every run
    latest_tx = {
        ccy: min(d, today.isoformat())
        for ccy, d in conn.execute(
            "SELECT currency, MAX(transaction_date) FROM transaction_norm "
            "WHERE currency <> 'SEK' AND transaction_date IS NOT NULL "
            "GROUP BY currency")
    }
    mcap_ccys = {r["currency"] for r in conn.execute(
        "SELECT DISTINCT currency FROM market_cap_current WHERE currency <> 'SEK'")}
    stale_before = (today - timedelta(days=FX_MARKETCAP_MAX_AGE_DAYS)).isoformat()

    need: dict[str, date] = {}
    for ccy in config.FX_CURRENCIES:
        wm = watermark.get(ccy)
        tx = latest_tx.get(ccy)
        if wm is None:
            if tx is not None or ccy in mcap_ccys:
                need[ccy] = date.fromisoformat(config.FI_EARLIEST)
        elif (tx is not None and tx > wm) or (ccy in mcap_ccys and wm < stale_before):
            need[ccy] = date.fromisoformat(wm) + timedelta(days=1)
    return need


def fx(
    conn: sqlite3.Connection,
    client: riksbank.RiksbankClient | None = None,
    *,
    backfill: bool = False,
    progress: FxProgress | None = None,
) -> FxSummary:
    """Fetch the SWEA rates that rows actually need; ``backfill`` refetches all."""
    end = config.today()
    if backfill:
        start_at = dict.fromkeys(config.FX_CURRENCIES, date.fromisoformat(config.FI_EARLIEST))
    else:
        start_at = _fx_needed(conn)
    s = FxSummary(up_to_date=len(config.FX_CURRENCIES) - len(start_at))
    if not start_at:
        return s
    client = client or riksbank.RiksbankClient()
    total = len(start_at)
    for done, (ccy, start) in enumerate(start_at.items(), start=1):
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
# Bump to re-ask every cached answer once. v1: `micCode: XSTO` filter (missed
# First North / Spotlight / NGM, cached bond "tickers"). v2: all equity
# listings, Swedish home venue picked client-side.
FIGI_RESOLVER_VERSION = 2

# FI instrument types that can be a company's listed share ('' = 2016–2018 rows,
# which carry no type). BTA, warrants, options, bonds and swaps never carry the
# company's ticker; asking about them only burns the rate limit.
_SHARE_TYPES = ("Aktie", "Depåbevis", "Kapitalandelsbevis", "")
_SHARE_TYPES_SQL = ", ".join(f"'{t}'" for t in _SHARE_TYPES)
_HOME_EXCH_SQL = ", ".join(f"'{e}'" for e in openfigi.HOME_EXCH)


@dataclass
class FigiSummary:
    asked: int = 0
    resolved: int = 0
    negative: int = 0
    transport_errors: int = 0


# (done, total, live summary) — fired after every batch, transport errors
# included. Unauthenticated OpenFIGI is 10 ISINs per request at 2.4 s spacing,
# so a full re-run is ~15 minutes; the CLI passes a printer so the terminal is
# not silent.
FigiProgress = Callable[[int, int, FigiSummary], None]


def _isins_to_resolve(conn: sqlite3.Connection) -> list[str]:
    # most recently traded first: a run cut short has still done the companies
    # the leaderboard shows
    return [
        r["isin"] for r in conn.execute(
            f"""
            SELECT n.isin FROM transaction_norm n
            LEFT JOIN figi_lookup f ON f.isin = n.isin
            WHERE n.isin <> ''
              AND COALESCE(n.instrument_type, '') IN ({_SHARE_TYPES_SQL})
              AND (f.isin IS NULL
                   OR f.resolver_version < :version
                   OR (f.ticker IS NULL AND f.attempts < 5
                       AND f.looked_up_at < date('now', '-90 days')))
            GROUP BY n.isin
            ORDER BY MAX(n.transaction_date) DESC, n.isin
            """,
            {"version": FIGI_RESOLVER_VERSION},
        )
    ]


_FIGI_UPSERT = """
INSERT INTO figi_lookup (isin, ticker, raw_ticker, name, exch_code, mic_code,
                         looked_up_at, attempts, last_error, resolver_version)
VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
ON CONFLICT(isin) DO UPDATE SET ticker=excluded.ticker,
  raw_ticker=excluded.raw_ticker, name=excluded.name,
  exch_code=excluded.exch_code, mic_code=excluded.mic_code,
  looked_up_at=excluded.looked_up_at, last_error=excluded.last_error,
  attempts=CASE WHEN figi_lookup.resolver_version < excluded.resolver_version
                THEN 1 ELSE figi_lookup.attempts + 1 END,
  resolver_version=excluded.resolver_version
"""


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
    for start in range(0, len(isins), client.batch_size):
        batch = isins[start:start + client.batch_size]
        done = start + len(batch)
        try:
            results = client.map_isins(batch)
        except Exception as exc:  # noqa: BLE001 — transport failure: do NOT cache
            s.transport_errors += len(batch)
            conn.executemany(
                "UPDATE figi_lookup SET last_error = ?, attempts = attempts + 1 "
                "WHERE isin = ?", [(str(exc)[:200], isin) for isin in batch],
            )
            if progress:
                progress(done, s.asked, s)
            continue
        with immediate(conn):
            conn.executemany(_FIGI_UPSERT, [
                (r.isin, r.ticker, r.raw_ticker, r.name, r.exch_code, r.mic_code,
                 now, r.error, FIGI_RESOLVER_VERSION)
                for r in results
            ])
        for r in results:
            if r.ticker:
                s.resolved += 1
            else:
                s.negative += 1
        if progress:
            progress(done, s.asked, s)
    return s


# ── company entity ─────────────────────────────────────────────────────────
_DISPLAY_NAME = """
SELECT issuer_name FROM transaction_norm
 WHERE lei = :lei AND transaction_date >= date(:today, '-12 months')
 GROUP BY issuer_name ORDER BY COUNT(*) DESC, MAX(transaction_date) DESC LIMIT 1
"""
_DISPLAY_NAME_FALLBACK = """
SELECT issuer_name FROM transaction_norm
 WHERE lei = :lei ORDER BY transaction_date DESC LIMIT 1
"""
# The share OpenFIGI placed on a Swedish home venue, most traded lately. Ranking
# by all-time count alone picks a pre-split ISIN: SAAB's old SE0000112385 has
# 1,176 rows but maps to cross-listings only, the live SE0021921269 to 'SAABB'.
_LISTED_ISIN = f"""
SELECT n.isin FROM transaction_norm n
  JOIN figi_lookup f ON f.isin = n.isin
 WHERE n.lei = :lei AND f.ticker IS NOT NULL AND f.exch_code IN ({_HOME_EXCH_SQL})
   AND COALESCE(n.instrument_type, '') IN ({_SHARE_TYPES_SQL})
 GROUP BY n.isin
 ORDER BY SUM(n.transaction_date >= date(:today, '-12 months')) DESC,
          COUNT(*) DESC, MAX(n.transaction_date) DESC LIMIT 1
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
    # an all-provider override with a symbol; '' rows mean "has none"
    manual = {r["lei"] for r in conn.execute(
        "SELECT lei FROM ticker_override WHERE provider = '' AND symbol <> ''"
    )}

    with immediate(conn):
        conn.execute("DELETE FROM company")
        conn.execute("DELETE FROM company_name_variant")
        for lei in leis:
            name = (conn.execute(_DISPLAY_NAME, {"lei": lei, "today": today}).fetchone()
                    or conn.execute(_DISPLAY_NAME_FALLBACK, {"lei": lei}).fetchone())
            display = name["issuer_name"] if name else lei
            isin = (conn.execute(_LISTED_ISIN, {"lei": lei, "today": today}).fetchone()
                    or conn.execute(_PRIMARY_ISIN, {"lei": lei}).fetchone()
                    or conn.execute(_ANY_ISIN, {"lei": lei}).fetchone())
            primary_isin = isin["isin"] if isin else None

            figi = None
            if primary_isin:
                # home venue only: a v1 row may still hold a bond "ticker"
                figi = conn.execute(
                    "SELECT raw_ticker, mic_code, exch_code, name FROM figi_lookup "
                    f"WHERE isin = ? AND ticker IS NOT NULL AND exch_code IN ({_HOME_EXCH_SQL})",
                    (primary_isin,),
                ).fetchone()
            source = "openfigi" if figi else ("manual" if lei in manual else None)

            conn.execute(
                "INSERT INTO company (lei, display_name, primary_isin, raw_ticker, "
                "mic_code, exch_code, figi_name, ticker_source, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (lei, display, primary_isin,
                 figi["raw_ticker"] if figi else None,
                 figi["mic_code"] if figi else None,
                 figi["exch_code"] if figi else None,
                 figi["name"] if figi else None,
                 source, now),
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


# ── Yahoo symbols ──────────────────────────────────────────────────────────
@dataclass
class YahooSymbolSummary:
    asked: int = 0
    found: int = 0
    missing: int = 0
    transport_errors: int = 0
    gave_up: bool = False   # stopped early on consecutive transport errors


# (done, total, live summary) — fired after every ISIN.
YahooSymbolProgress = Callable[[int, int, YahooSymbolSummary], None]

# consecutive transport failures before the step stops: a rate-limited Yahoo
# fails every request, and hammering it only extends the block
_YAHOO_MAX_CONSECUTIVE_ERRORS = 5


def _yahoo_isins_to_resolve(conn: sqlite3.Connection) -> list[str]:
    exch = ", ".join(f"'{e}'" for e in yahoo_source.YAHOO_EXCH)
    return [
        r["primary_isin"] for r in conn.execute(
            f"""
            SELECT DISTINCT c.primary_isin FROM company c
            LEFT JOIN yahoo_symbol y ON y.isin = c.primary_isin
            WHERE c.raw_ticker IS NOT NULL AND c.exch_code IN ({exch})
              AND (y.isin IS NULL
                   OR (y.symbol IS NULL AND y.attempts < 5
                       AND y.looked_up_at < date('now', '-90 days')))
            ORDER BY c.primary_isin
            """
        )
    ]


def yahoo_symbols(
    conn: sqlite3.Connection,
    search: Callable[[str], str | None] | None = None,
    *, progress: YahooSymbolProgress | None = None,
) -> YahooSymbolSummary:
    """Ask Yahoo for the symbol of every listed company's primary ISIN not yet
    asked. Needs :func:`build_companies` first."""
    search = search or yahoo_source.search_symbol
    isins = _yahoo_isins_to_resolve(conn)
    s = YahooSymbolSummary(asked=len(isins))
    now = config.now_local_iso()
    streak = 0
    for done, isin in enumerate(isins, start=1):
        try:
            symbol = search(isin)
        except Exception as exc:  # noqa: BLE001 — transport failure: do NOT cache
            s.transport_errors += 1
            conn.execute(
                "UPDATE yahoo_symbol SET last_error = ?, attempts = attempts + 1 "
                "WHERE isin = ?", (str(exc)[:200], isin),
            )
            streak += 1
            if progress:
                progress(done, s.asked, s)
            if streak >= _YAHOO_MAX_CONSECUTIVE_ERRORS:
                s.gave_up = True
                log.warning("yahoo symbols: %d consecutive errors, stopping (%s)",
                            streak, exc)
                break
            continue
        streak = 0
        conn.execute(
            "INSERT INTO yahoo_symbol (isin, symbol, looked_up_at, attempts) "
            "VALUES (?, ?, ?, 1) ON CONFLICT(isin) DO UPDATE SET "
            "symbol=excluded.symbol, looked_up_at=excluded.looked_up_at, "
            "attempts=yahoo_symbol.attempts+1, last_error=NULL",
            (isin, symbol, now),
        )
        if symbol:
            s.found += 1
        else:
            s.missing += 1
        if progress:
            progress(done, s.asked, s)
    return s


# ── market caps ────────────────────────────────────────────────────────────
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
    overrides: dict[str, dict[str, str]] = {}
    for r in conn.execute("SELECT lei, provider, symbol FROM ticker_override"):
        overrides.setdefault(r["lei"], {})[r["provider"]] = r["symbol"] or ""
    return [
        Company(r["lei"], r["display_name"], r["primary_isin"], r["raw_ticker"],
                r["mic_code"], r["exch_code"], r["figi_name"],
                symbols={"yahoo": r["yahoo_symbol"]} if r["yahoo_symbol"] else {},
                overrides=overrides.get(r["lei"], {}))
        for r in conn.execute(
            "SELECT c.lei, c.display_name, c.primary_isin, c.raw_ticker, c.mic_code, "
            "c.exch_code, c.figi_name, y.symbol AS yahoo_symbol FROM company c "
            "LEFT JOIN yahoo_symbol y ON y.isin = c.primary_isin"
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
