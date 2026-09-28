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
    def __init__(self, answers, batch_size=1):
        self.answers = answers  # isin -> FigiResult | Exception
        self.batch_size = batch_size
        self.batches: list[list[str]] = []

    def map_isins(self, isins):
        from insynshandel.sources.openfigi import FigiResult

        self.batches.append(list(isins))
        out = []
        for isin in isins:
            a = self.answers.get(isin, FigiResult(isin, None, None, None, None, None))
            if isinstance(a, Exception):
                raise a  # a transport failure takes the whole request down
            out.append(a)
        return out

    @property
    def seen(self) -> list[str]:
        return [i for b in self.batches for i in b]


def _hit(isin, raw, name, exch="SS"):
    from insynshandel.sources.openfigi import HOME_EXCH, FigiResult, format_ticker

    return FigiResult(isin, format_ticker(raw, name), raw, name, exch, HOME_EXCH[exch])


def _write_rows(conn, rows):
    """Ingest hand-made FI rows: (lei, isin, 'YYYY-MM-DD', instrumenttyp)."""
    prs = []
    for i, (lei, isin, txdate, itype) in enumerate(rows):
        f = {n: "" for n in fi.FIELD_NAMES}
        f.update(publiceringsdatum=f"{txdate} 10:00:00", transaktionsdatum=f"{txdate} 00:00:00",
                 lei_kod=lei, emittent=f"{lei} AB", karaktar="Förvärv", instrumenttyp=itype,
                 isin=isin, volym="1", volymsenhet="Antal", pris="1", valuta="SEK",
                 status="Aktuell", person_i_ledande_stallning="P")
        prs.append(fi.ParsedRow(fi.row_hash([f[n] for n in fi.FIELD_NAMES]) + str(i), 0, f))
    leaf = fi.Leaf(date(2016, 7, 1), date(2027, 1, 1), prs, truncated=False)
    ingest.write_leaf(conn, ingest._open_batch(conn, "t", leaf), leaf)
    normalize.normalize(conn)


def test_figi_only_queries_unknown_isins(db_conn, sample_export_bytes):
    _ingest(db_conn, sample_export_bytes)
    known = db_conn.execute(
        "SELECT isin FROM transaction_norm WHERE isin <> '' AND instrument_type = 'Aktie' "
        "LIMIT 1"
    ).fetchone()[0]
    db_conn.execute(
        "INSERT INTO figi_lookup (isin, ticker, looked_up_at, attempts, resolver_version) "
        "VALUES (?, 'X.ST', date('now'), 1, ?)", (known, reference.FIGI_RESOLVER_VERSION),
    )

    rec = FakeFigi({})
    reference.figi(db_conn, rec)
    assert rec.seen                                    # the others were asked
    assert known not in rec.seen                       # resolved ISIN not re-queried
    assert db_conn.execute(                            # its row is untouched
        "SELECT ticker FROM figi_lookup WHERE isin = ?", (known,)
    ).fetchone()["ticker"] == "X.ST"


def test_figi_skips_instruments_that_are_never_the_share(db_conn, sample_export_bytes):
    _ingest(db_conn, sample_export_bytes)
    rec = FakeFigi({})
    reference.figi(db_conn, rec)
    only_other = {r["isin"] for r in db_conn.execute(
        "SELECT isin FROM transaction_norm WHERE isin <> '' GROUP BY isin "
        "HAVING SUM(COALESCE(instrument_type, '') IN "
        "('Aktie', 'Depåbevis', 'Kapitalandelsbevis', '')) = 0"
    )}
    assert only_other                                  # the fixture has bonds/warrants
    assert not only_other & set(rec.seen)


def test_figi_reasks_an_older_resolver_version_in_place(db_conn, sample_export_bytes):
    _ingest(db_conn, sample_export_bytes)
    isin = reference._isins_to_resolve(db_conn)[0]
    db_conn.execute(  # a v1 answer: a bond "ticker", attempts nearly used up
        "INSERT INTO figi_lookup (isin, ticker, raw_ticker, exch_code, looked_up_at, "
        "attempts, resolver_version) VALUES (?, 'B.ST', 'SWEDA 11 04/26/19', "
        "'NOMX STOCKHOLM', date('now'), 4, 1)", (isin,),
    )
    reference.figi(db_conn, FakeFigi({isin: _hit(isin, "SAABB", "SAAB AB-B")}))
    row = db_conn.execute("SELECT * FROM figi_lookup WHERE isin = ?", (isin,)).fetchone()
    assert (row["raw_ticker"], row["exch_code"], row["mic_code"]) == ("SAABB", "SS", "XSTO")
    assert row["resolver_version"] == reference.FIGI_RESOLVER_VERSION
    assert row["attempts"] == 1                        # a new resolver starts over
    assert isin not in reference._isins_to_resolve(db_conn)


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


def test_figi_progress_fires_once_per_batch_including_errors(db_conn, sample_export_bytes):
    _ingest(db_conn, sample_export_bytes)
    isins = reference._isins_to_resolve(db_conn)[:3]
    fake = FakeFigi({isins[0]: ConnectionError("reset")})   # 1 error, 2 negatives
    seen: list[tuple[int, int, int, int]] = []
    s = reference.figi(
        db_conn, fake, limit=3,
        progress=lambda done, total, fs: seen.append(
            (done, total, fs.negative, fs.transport_errors)
        ),
    )
    assert [d for d, *_ in seen] == [1, 2, 3]        # a tick per batch, in order
    assert {t for _, t, *_ in seen} == {3}           # total is the asked count
    assert seen[-1][2:] == (s.negative, s.transport_errors) == (2, 1)  # live summary


def test_figi_batches_and_a_failed_batch_counts_every_isin(db_conn, sample_export_bytes):
    _ingest(db_conn, sample_export_bytes)
    isins = reference._isins_to_resolve(db_conn)[:3]
    fake = FakeFigi({isins[0]: ConnectionError("reset")}, batch_size=2)
    ticks: list[int] = []
    s = reference.figi(db_conn, fake, limit=3,
                       progress=lambda done, total, fs: ticks.append(done))
    assert fake.batches == [isins[:2], isins[2:]]
    assert ticks == [2, 3]
    assert (s.transport_errors, s.negative) == (2, 1)


def test_figi_transport_error_writes_no_negative_row(db_conn, sample_export_bytes):
    _ingest(db_conn, sample_export_bytes)
    isins = reference._isins_to_resolve(db_conn)[:2]
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


def test_listed_isin_beats_a_busier_pre_split_isin(db_conn):
    today = reference.config.today()
    recent = (today - timedelta(days=30)).isoformat()
    old = today.replace(year=today.year - 3).isoformat()
    # SAAB's shape: the old ISIN has the history and is still misreported lately
    _write_rows(db_conn, [("LEI1", "SE0000000OLD", old, "Aktie")] * 8
                + [("LEI1", "SE0000000OLD", recent, "Aktie")] * 3
                + [("LEI1", "SE0000000NEW", recent, "Aktie")] * 2)
    reference.figi(db_conn, FakeFigi({"SE0000000NEW": _hit("SE0000000NEW", "SAABB",
                                                          "SAAB AB-B")}))
    reference.build_companies(db_conn)
    co = db_conn.execute("SELECT * FROM company WHERE lei = 'LEI1'").fetchone()
    assert (co["primary_isin"], co["raw_ticker"], co["figi_name"]) == (
        "SE0000000NEW", "SAABB", "SAAB AB-B")
    assert co["ticker_source"] == "openfigi"


def test_a_bond_answer_never_becomes_the_ticker(db_conn):
    _write_rows(db_conn, [("LEI2", "SE0000000BND", "2026-01-02", "Aktie")])
    db_conn.execute(  # what the v1 resolver cached for bond ISINs
        "INSERT INTO figi_lookup (isin, ticker, raw_ticker, exch_code, mic_code, "
        "looked_up_at) VALUES ('SE0000000BND', 'HARMNY-F.ST', 'HARMNY F 02/13/29', "
        "'NOMX STOCKHOLM', 'XSTO', date('now'))"
    )
    reference.build_companies(db_conn)
    co = db_conn.execute("SELECT * FROM company WHERE lei = 'LEI2'").fetchone()
    assert co["raw_ticker"] is None and co["ticker_source"] is None


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
        Company("L1", "One AB", None, "ONE B", "XSTO", "SS"),    # addressable
        Company("L2", "Two AB", None, None, None, None),         # no ticker
        Company("L3", "Three AB", None, "THREE", "FNSE", "SF"),  # addressable
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


# ── Yahoo symbols ─────────────────────────────────────────────────────────
@pytest.fixture
def listed(db_conn):
    """Three companies: Nasdaq Stockholm, First North, NGM."""
    _write_rows(db_conn, [("LSS", "SE00000000SS", "2026-01-02", "Aktie"),
                          ("LSF", "SE00000000SF", "2026-01-02", "Aktie"),
                          ("LNG", "SE00000000NG", "2026-01-02", "Aktie")])
    reference.figi(db_conn, FakeFigi({
        "SE00000000SS": _hit("SE00000000SS", "SAABB", "SAAB AB-B", "SS"),
        "SE00000000SF": _hit("SE00000000SF", "TSEC", "TEMPEST SECURITY AB", "SF"),
        "SE00000000NG": _hit("SE00000000NG", "SDS", "SEAMLESS", "NG"),
    }))
    reference.build_companies(db_conn)
    return db_conn


def test_yahoo_symbols_asks_once_and_skips_ngm(listed):
    asked: list[str] = []

    def search(isin):
        asked.append(isin)
        return "SAAB-B.ST" if isin == "SE00000000SS" else None

    s = reference.yahoo_symbols(listed, search)
    assert sorted(asked) == ["SE00000000SF", "SE00000000SS"]  # NG is not on Yahoo
    assert (s.found, s.missing) == (1, 1)
    reference.yahoo_symbols(listed, search)
    assert len(asked) == 2                              # negatives cached too


def test_yahoo_symbols_transport_errors_are_not_cached_and_stop_early(listed, monkeypatch):
    monkeypatch.setattr(reference, "_YAHOO_MAX_CONSECUTIVE_ERRORS", 1)

    def down(isin):
        raise ConnectionError("429")

    s = reference.yahoo_symbols(listed, down)
    assert s.gave_up and s.transport_errors == 1
    assert listed.execute("SELECT COUNT(*) FROM yahoo_symbol").fetchone()[0] == 0


def test_yahoo_symbol_precedence(listed):
    from insynshandel.sources.marketcap.yahoo import YahooProvider

    reference.yahoo_symbols(listed, lambda isin: "SAAB-B.ST" if isin.endswith("SS") else None)
    listed.executemany(
        "INSERT INTO ticker_override (lei, provider, symbol, note) VALUES (?, ?, ?, '')",
        [("LNG", "", "SDS.ST")],
    )
    sym = {c.lei: YahooProvider().symbol_for(c) for c in reference._companies(listed)}
    assert sym == {
        "LSS": "SAAB-B.ST",   # Yahoo's own answer
        "LSF": "TSEC.ST",     # Yahoo had none: formatter on OpenFIGI's ticker + name
        "LNG": "SDS.ST",      # NGM gets nothing, unless overridden
    }

    listed.executemany(
        "INSERT INTO ticker_override (lei, provider, symbol, note) VALUES (?, ?, ?, '')",
        [("LSS", "yahoo", "OTHER.ST"), ("LSF", "", "")],
    )
    sym = {c.lei: YahooProvider().symbol_for(c) for c in reference._companies(listed)}
    assert sym["LSS"] == "OTHER.ST"   # a manual row beats the search
    assert sym["LSF"] is None         # '' = checked, no symbol


def test_all_provider_override_marks_the_company_manual(db_conn):
    _write_rows(db_conn, [("LEI3", "SE000000NONE", "2026-01-02", "Aktie")])
    db_conn.execute("INSERT INTO ticker_override (lei, provider, symbol, note) "
                    "VALUES ('LEI3', '', 'ABC.ST', '')")
    reference.build_companies(db_conn)
    assert db_conn.execute(
        "SELECT ticker_source FROM company WHERE lei = 'LEI3'"
    ).fetchone()[0] == "manual"


def test_search_symbol_raises_on_a_swallowed_yahoo_error(monkeypatch):
    import yfinance as yf

    from insynshandel.sources.marketcap.yahoo import search_symbol

    class Answer:
        def __init__(self, response):
            self.response = response
            self.quotes = response.get("quotes", [])

    monkeypatch.setattr(yf, "Search", lambda *a, **k: Answer(
        {"quotes": [{"symbol": "SAABB.F"}, {"symbol": "SAAB-B.ST"}]}))
    assert search_symbol("SE0021921269") == "SAAB-B.ST"   # the Stockholm one
    monkeypatch.setattr(yf, "Search", lambda *a, **k: Answer({"quotes": []}))
    assert search_symbol("SE0009994445") is None          # a real "no match"
    monkeypatch.setattr(yf, "Search", lambda *a, **k: Answer({}))
    with pytest.raises(RuntimeError):                     # faulty body, not a negative
        search_symbol("SE0021921269")
