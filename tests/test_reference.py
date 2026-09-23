"""Phase 5 — reference pipeline. HTTP is mocked; no network."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from insynshandel.pipeline import ingest, normalize, reference
from insynshandel.sources import fi
from insynshandel.sources.marketcap import FetchFailure, MarketCapQuote


def _ingest(conn, data: bytes):
    rows = fi.parse_export(data)
    leaf = fi.Leaf(date(2016, 7, 1), date(2026, 5, 1), rows, truncated=False)
    ingest.write_leaf(conn, ingest._open_batch(conn, "t", leaf), leaf)
    normalize.normalize(conn)


# ── FX ────────────────────────────────────────────────────────────────────
class FakeRiksbank:
    def __init__(self, series):
        self.series = series
        self.calls = []

    def observations(self, currency, start, end):
        self.calls.append((currency, start, end))
        return self.series.get(currency, [])


def test_fx_upserts_and_is_idempotent(db_conn):
    fake = FakeRiksbank({
        "USD": [("2026-09-01", 9.5), ("2026-09-02", 9.6)],
        "EUR": [("2026-09-01", 11.1)],
        "GBP": [], "CAD": [],
    })
    s1 = reference.fx(db_conn, fake, days=7)
    assert s1.ok
    assert s1.currencies["USD"] == 2
    n1 = db_conn.execute("SELECT COUNT(*) FROM fx_rate").fetchone()[0]
    reference.fx(db_conn, fake, days=7)
    n2 = db_conn.execute("SELECT COUNT(*) FROM fx_rate").fetchone()[0]
    assert n1 == n2 == 3


def test_fx_backfill_starts_at_register_epoch(db_conn):
    fake = FakeRiksbank({})
    reference.fx(db_conn, fake, backfill=True)
    assert all(call[1] == date(2016, 7, 1) for call in fake.calls)


def test_fx_progress_brackets_every_currency_fetch(db_conn):
    from insynshandel import config

    fake = FakeRiksbank({"USD": [("2026-09-01", 9.5)]})
    seen: list[tuple[int, int, str]] = []
    reference.fx(db_conn, fake, days=7,
                 progress=lambda d, t, detail: seen.append((d, t, detail)))

    n = len(config.FX_CURRENCIES)
    assert len(seen) == 2 * n                       # a tick before and after each
    window = f"{config.today() - timedelta(days=7)}..{config.today()}"
    assert seen[0] == (0, n, f"fetching {config.FX_CURRENCIES[0]} {window}")
    assert seen[1] == (1, n, "USD 1 rows")
    assert seen[-1][:2] == (n, n)                   # ends on total, so the meter closes


def test_fx_progress_reports_a_failed_currency(db_conn):
    from insynshandel.sources.riksbank import RiksbankError

    class Boom:
        def observations(self, *a):
            raise RiksbankError("429")

    seen: list[tuple[int, int, str]] = []
    s = reference.fx(db_conn, Boom(),
                     progress=lambda d, t, detail: seen.append((d, t, detail)))
    assert not s.ok
    # a currency that raised still advances the counter — a stalled meter reads
    # as a hang, which is the opposite of what happened
    assert [d for d, _, _ in seen][-1] == len(seen) // 2
    assert seen[1][2].endswith("FAILED")


def test_fx_error_is_recorded_not_raised(db_conn):
    from insynshandel.sources.riksbank import RiksbankError

    class Boom:
        def observations(self, *a):
            raise RiksbankError("429")

    s = reference.fx(db_conn, Boom())
    assert not s.ok
    assert len(s.errors) == len(reference.config.FX_CURRENCIES)


# ── OpenFIGI ──────────────────────────────────────────────────────────────
class FakeFigi:
    def __init__(self, answers):
        self.answers = answers  # isin -> FigiResult | Exception

    def map_isin(self, isin):
        from insynshandel.sources.openfigi import FigiResult

        a = self.answers.get(isin, FigiResult(isin, None, None, None, None, None))
        if isinstance(a, Exception):
            raise a
        return a


def test_figi_only_queries_unknown_isins(db_conn, sample_export_bytes):
    from insynshandel.sources.openfigi import FigiResult

    _ingest(db_conn, sample_export_bytes)
    known = db_conn.execute(
        "SELECT isin FROM transaction_norm WHERE isin <> '' LIMIT 1"
    ).fetchone()[0]
    db_conn.execute(
        "INSERT INTO figi_lookup (isin, ticker, looked_up_at, attempts) "
        "VALUES (?, 'X.ST', date('now'), 1)", (known,),
    )

    class Recorder(FakeFigi):
        def __init__(self):
            super().__init__({})
            self.seen: list[str] = []

        def map_isin(self, isin):
            self.seen.append(isin)
            return FigiResult(isin, None, None, None, None, None)

    rec = Recorder()
    reference.figi(db_conn, rec)
    assert known not in rec.seen                       # resolved ISIN not re-queried
    assert db_conn.execute(                            # its row is untouched
        "SELECT ticker FROM figi_lookup WHERE isin = ?", (known,)
    ).fetchone()["ticker"] == "X.ST"


def test_figi_negative_is_cached(db_conn, sample_export_bytes):
    _ingest(db_conn, sample_export_bytes)
    fake = FakeFigi({})  # everything returns "no match"
    s = reference.figi(db_conn, fake, limit=3)
    assert s.asked == 3
    assert s.negative == 3
    rows = db_conn.execute(
        "SELECT ticker FROM figi_lookup WHERE ticker IS NULL"
    ).fetchall()
    assert len(rows) == 3  # NULL rows written so they are not re-queried


def test_figi_progress_fires_once_per_isin_including_errors(db_conn, sample_export_bytes):
    _ingest(db_conn, sample_export_bytes)
    isins = [
        r["isin"] for r in db_conn.execute(
            "SELECT DISTINCT isin FROM transaction_norm WHERE isin <> '' LIMIT 3"
        )
    ]
    fake = FakeFigi({isins[0]: ConnectionError("reset")})   # 1 error, 2 negatives
    seen: list[tuple[int, int, int, int]] = []
    s = reference.figi(
        db_conn, fake, limit=3,
        progress=lambda done, total, fs: seen.append(
            (done, total, fs.negative, fs.transport_errors)
        ),
    )
    assert [d for d, *_ in seen] == [1, 2, 3]        # a tick per ISIN, in order
    assert {t for _, t, *_ in seen} == {3}           # total is the asked count
    assert seen[-1][2:] == (s.negative, s.transport_errors)  # live summary


def test_figi_transport_error_writes_no_negative_row(db_conn, sample_export_bytes):
    _ingest(db_conn, sample_export_bytes)
    isins = [
        r["isin"] for r in db_conn.execute(
            "SELECT DISTINCT isin FROM transaction_norm WHERE isin <> '' LIMIT 2"
        )
    ]
    fake = FakeFigi({isins[0]: ConnectionError("reset")})
    s = reference.figi(db_conn, fake, limit=2)
    assert s.transport_errors == 1
    # no figi_lookup row for the ISIN that only had a transport failure
    assert db_conn.execute(
        "SELECT COUNT(*) FROM figi_lookup WHERE isin = ?", (isins[0],)
    ).fetchone()[0] == 0


# ── company entity ────────────────────────────────────────────────────────
def test_display_name_prefers_recent_then_frequent(db_conn):
    def row(name, txdate):
        f = {n: "" for n in fi.FIELD_NAMES}
        f.update(publiceringsdatum=f"{txdate} 10:00:00", transaktionsdatum=f"{txdate} 00:00:00",
                 lei_kod="LEI1", emittent=name, karaktar="Förvärv", instrumenttyp="Aktie",
                 isin="SE0001", volym="1", volymsenhet="Antal", pris="1", valuta="SEK",
                 status="Aktuell", person_i_ledande_stallning="P")
        return f
    today = reference.config.today().isoformat()
    old = (reference.config.today().replace(year=reference.config.today().year - 3)).isoformat()
    raws = [row("Momentum Group AB", old)] * 10 + [row("Alligo AB", today)] * 2
    prs = [fi.ParsedRow(fi.row_hash([f[n] for n in fi.FIELD_NAMES]) + str(i), 0, f)
           for i, f in enumerate(raws)]
    leaf = fi.Leaf(date(2016, 7, 1), date(2027, 1, 1), prs, truncated=False)
    ingest.write_leaf(db_conn, ingest._open_batch(db_conn, "t", leaf), leaf)
    normalize.normalize(db_conn)
    reference.build_companies(db_conn)
    assert db_conn.execute(
        "SELECT display_name FROM company WHERE lei = 'LEI1'"
    ).fetchone()[0] == "Alligo AB"  # recency wins over the 10 old rows


def test_primary_isin_prefers_aktie_but_falls_back(db_conn, sample_export_bytes):
    _ingest(db_conn, sample_export_bytes)
    reference.build_companies(db_conn)
    for r in db_conn.execute(
        "SELECT lei, primary_isin FROM company WHERE primary_isin IS NOT NULL"
    ):
        # the chosen ISIN is always a real ISIN this LEI actually used
        assert db_conn.execute(
            "SELECT COUNT(*) FROM transaction_norm WHERE lei = ? AND isin = ?",
            (r["lei"], r["primary_isin"]),
        ).fetchone()[0] > 0
        # and if the LEI has any Aktie ISIN, the primary must be one of them
        has_aktie = db_conn.execute(
            "SELECT COUNT(*) FROM transaction_norm WHERE lei = ? "
            "AND instrument_type = 'Aktie' AND isin <> ''", (r["lei"],),
        ).fetchone()[0]
        if has_aktie:
            assert db_conn.execute(
                "SELECT COUNT(*) FROM transaction_norm WHERE lei = ? AND isin = ? "
                "AND instrument_type = 'Aktie'", (r["lei"], r["primary_isin"]),
            ).fetchone()[0] > 0


def test_name_variants_kept_for_search(db_conn, sample_export_bytes):
    _ingest(db_conn, sample_export_bytes)
    s = reference.build_companies(db_conn)
    assert s.name_variants >= s.companies


# ── market caps ───────────────────────────────────────────────────────────
class DictProvider:
    def __init__(self, name, caps):
        self.name = name
        self.caps = caps  # lei -> (market_cap, currency)

    def symbol_for(self, company):
        return company.lei if company.lei in self.caps else None

    def fetch(self, companies, *, progress=None):
        q, f = [], []
        addressable = sum(1 for c in companies if self.symbol_for(c) is not None)
        for c in companies:
            if c.lei in self.caps:
                mc, ccy = self.caps[c.lei]
                q.append(MarketCapQuote(c.lei, mc, ccy, "2026-09-08", self.name))
                if progress and addressable:
                    progress(len(q), addressable)
            else:
                f.append(FetchFailure(c.lei, None, "unknown"))
        return q, f


@pytest.fixture
def companies_built(db_conn, sample_export_bytes):
    _ingest(db_conn, sample_export_bytes)
    reference.build_companies(db_conn)
    return db_conn


def test_provider_order_first_hit_wins(companies_built):
    leis = [r["lei"] for r in companies_built.execute("SELECT lei FROM company LIMIT 3")]
    p1 = DictProvider("p1", {leis[0]: (1e9, "SEK")})
    p2 = DictProvider("p2", {leis[0]: (2e9, "SEK"), leis[1]: (3e9, "SEK")})
    reference.marketcaps(companies_built, providers=[p1, p2])
    got = {r["lei"]: (r["market_cap"], r["source"]) for r in companies_built.execute(
        "SELECT lei, market_cap, source FROM market_cap")}
    assert got[leis[0]] == (1e9, "p1")   # p1 ran first
    assert got[leis[1]] == (3e9, "p2")


def test_manual_provider_alone_completes(companies_built, monkeypatch, tmp_path):
    seed = tmp_path / "market_cap_manual.csv"
    lei = companies_built.execute("SELECT lei FROM company LIMIT 1").fetchone()[0]
    seed.write_text(f"lei,market_cap,currency,as_of,note\n{lei},4.2e8,SEK,2026-09-01,x\n")
    monkeypatch.setattr(reference.config, "SEED_DIR", tmp_path)
    from insynshandel.sources.marketcap.manual import ManualProvider

    s = reference.marketcaps(companies_built, providers=[ManualProvider()])
    assert s.written == 1
    assert companies_built.execute(
        "SELECT market_cap_sek FROM market_cap_current WHERE lei = ?", (lei,)
    ).fetchone()["market_cap_sek"] == pytest.approx(4.2e8)


def test_manual_provider_rejects_future_as_of(companies_built, monkeypatch, tmp_path):
    lei = companies_built.execute("SELECT lei FROM company LIMIT 1").fetchone()[0]
    (tmp_path / "market_cap_manual.csv").write_text(
        f"lei,market_cap,currency,as_of,note\n{lei},1e8,SEK,2099-01-01,typo\n"
    )
    monkeypatch.setattr(reference.config, "SEED_DIR", tmp_path)
    from insynshandel.sources.marketcap.manual import ManualProvider

    s = reference.marketcaps(companies_built, providers=[ManualProvider()])
    assert s.written == 0
    assert companies_built.execute(
        "SELECT COUNT(*) FROM market_cap WHERE lei = ?", (lei,)
    ).fetchone()[0] == 0


def test_marketcaps_progress_is_labelled_with_the_provider(companies_built):
    leis = [r["lei"] for r in companies_built.execute("SELECT lei FROM company LIMIT 2")]
    seen: list[tuple[str, int, int]] = []
    reference.marketcaps(
        companies_built,
        providers=[DictProvider("p1", {leis[0]: (1e9, "SEK")})],
        progress=lambda name, done, total: seen.append((name, done, total)),
    )
    assert seen == [("p1", 1, 1)]      # provider.name bound, (done, total) forwarded


def test_marketcaps_progress_omitted_leaves_providers_unticked(companies_built):
    leis = [r["lei"] for r in companies_built.execute("SELECT lei FROM company LIMIT 1")]
    s = reference.marketcaps(
        companies_built, providers=[DictProvider("p1", {leis[0]: (1e9, "SEK")})],
    )
    assert s.written == 1              # no progress= is still the normal path


def test_yahoo_ticks_only_over_companies_it_can_address(monkeypatch):
    from insynshandel.sources.marketcap import Company
    from insynshandel.sources.marketcap.yahoo import YahooProvider

    provider = YahooProvider()
    monkeypatch.setattr(
        YahooProvider, "_one",
        lambda self, c: (
            MarketCapQuote(c.lei, 1e9, "SEK", "2026-09-08", "yahoo"), None
        ),
    )
    companies = [
        Company("L1", "One AB", None, "ONE B", None, None),      # addressable
        Company("L2", "Two AB", None, None, None, None),         # no ticker
        Company("L3", "Three AB", None, "THREE", None, None),    # addressable
    ]
    ticks: list[tuple[int, int]] = []
    provider.fetch(companies, progress=lambda d, t: ticks.append((d, t)))
    assert ticks == [(1, 2), (2, 2)]   # total is 2, not 3 — the meter must not stall


def test_yahoo_progress_silent_when_nothing_is_addressable():
    from insynshandel.sources.marketcap import Company
    from insynshandel.sources.marketcap.yahoo import YahooProvider

    ticks: list[tuple[int, int]] = []
    YahooProvider().fetch(
        [Company("L2", "Two AB", None, None, None, None)],
        progress=lambda d, t: ticks.append((d, t)),
    )
    assert ticks == []                 # a 0/0 tick would divide by zero in the ETA


def test_total_failure_writes_no_rows(companies_built):
    class Dead:
        name = "dead"
        def symbol_for(self, c): return c.lei
        def fetch(self, companies, *, progress=None):
            return [], [FetchFailure(c.lei, c.lei, "outage") for c in companies]

    before = companies_built.execute("SELECT COUNT(*) FROM market_cap").fetchone()[0]
    s = reference.marketcaps(companies_built, providers=[Dead()])
    assert s.written == 0
    assert companies_built.execute("SELECT COUNT(*) FROM market_cap").fetchone()[0] == before


def test_non_sek_market_cap_converted(companies_built):
    companies_built.executemany(
        "INSERT INTO fx_rate (currency, rate_date, sek_per_unit) VALUES (?, ?, ?)",
        [("USD", "2026-09-01", 9.5)],
    )
    lei = companies_built.execute("SELECT lei FROM company LIMIT 1").fetchone()[0]
    p = DictProvider("p", {lei: (1_000_000.0, "USD")})
    # provider returns as_of 2026-09-08; fx has 2026-09-01 -> forward-filled
    reference.marketcaps(companies_built, providers=[p])
    row = companies_built.execute(
        "SELECT currency, market_cap, market_cap_sek FROM market_cap WHERE lei = ?", (lei,)
    ).fetchone()
    assert row["currency"] == "USD"
    assert row["market_cap_sek"] == pytest.approx(9_500_000.0)
