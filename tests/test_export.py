"""Phase 6 — reads + static export. Hermetic."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from insynshandel import config, db, export_static, reads
from insynshandel.pipeline import aggregate, ingest, normalize, reference
from insynshandel.sources import fi
from insynshandel.sources.marketcap import MarketCapQuote

_FX = [(ccy, d, r) for ccy in ("GBP", "CAD", "EUR", "USD")
       for d, r in (("2016-01-01", 10.0), ("2026-01-01", 11.0))]


@pytest.fixture
def built(db_conn, sample_export_bytes):
    db_conn.executemany(
        "INSERT INTO fx_rate (currency, rate_date, sek_per_unit) VALUES (?, ?, ?)", _FX
    )
    rows = fi.parse_export(sample_export_bytes)
    leaf = fi.Leaf(date(2016, 7, 1), date(2026, 5, 1), rows, truncated=False)
    ingest.write_leaf(db_conn, ingest._open_batch(db_conn, "t", leaf), leaf)
    normalize.normalize(db_conn)
    reference.build_companies(db_conn)
    aggregate.run(db_conn)
    return db_conn


class OneCapProvider:
    name = "test"

    def __init__(self, lei, cap, ccy="SEK"):
        self.lei, self.cap, self.ccy = lei, cap, ccy

    def symbol_for(self, c):
        return c.lei if c.lei == self.lei else None

    def fetch(self, companies, *, progress=None):
        return (
            [MarketCapQuote(self.lei, self.cap, self.ccy, "2026-09-08", self.name)],
            [],
        )


# ── reads ─────────────────────────────────────────────────────────────────
def test_meta_shape(built):
    m = reads.meta(built)
    assert m.raw_live_rows == m.norm_rows > 0
    assert m.counted_rows <= m.norm_rows
    assert "SEK" in m.currencies
    assert m.unmapped_natures == {}  # sample_export_bytes has no unmapped nature


def test_leaderboard_is_complete_and_unsorted(built):
    lb = reads.leaderboard(built, "all")
    n_companies = built.execute(
        "SELECT COUNT(DISTINCT COALESCE("
        "(SELECT canonical_lei FROM issuer_alias WHERE alias_lei = lei), lei)) "
        "FROM transaction_norm WHERE is_counted = 1"
    ).fetchone()[0]
    assert lb.count == n_companies              # every active company, no limit
    # Leaderboard sorting lives on the client, so the shared function must
    # expose no server-side sort/paginate knob — assert that against the signature.
    import inspect

    assert list(inspect.signature(reads.leaderboard).parameters) == ["conn", "period"]


def test_leaderboard_entry_has_every_sortable_field(built):
    lb = reads.leaderboard(built, "all")
    e = lb.entries[0]
    for f in ("net_value_sek", "buy_value_sek", "sell_value_sek", "tx_count",
              "buyer_count", "seller_count", "market_cap", "pct_of_mcap", "verification"):
        assert hasattr(e, f)


def test_missing_market_cap_serialises_as_null_never_zero(built):
    lb = reads.leaderboard(built, "all")
    for e in lb.entries:
        assert e.market_cap is None
        assert e.pct_of_mcap is None
        assert e.verification == "unverifiable"


def test_present_market_cap_yields_a_real_pct(built):
    lei = built.execute(
        "SELECT lei FROM agg_company_period WHERE period = 'all' "
        "ORDER BY ABS(net_value_sek) DESC LIMIT 1"
    ).fetchone()[0]
    reference.marketcaps(built, providers=[OneCapProvider(lei, 5.0e9)])
    aggregate.run(built)

    lb = reads.leaderboard(built, "all")
    hit = next(e for e in lb.entries if e.lei == lei)
    assert hit.market_cap == pytest.approx(5.0e9)
    assert hit.pct_of_mcap == pytest.approx(hit.net_value_sek / 5.0e9)
    assert hit.verification == "ok"
    # everyone else still null
    assert all(e.market_cap is None for e in lb.entries if e.lei != lei)


def test_company_detail_caps_transactions_but_reports_total(built, monkeypatch):
    monkeypatch.setattr(config, "COMPANY_TX_LIMIT", 2)
    lei = built.execute(
        "SELECT n.lei FROM transaction_norm n JOIN company c ON c.lei = n.lei "
        "GROUP BY n.lei ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()[0]
    d = reads.company_detail(built, lei)
    assert d is not None
    assert len(d.recent_transactions) <= 2
    assert d.tx_count_total >= len(d.recent_transactions)


def test_company_detail_follows_issuer_alias(built):
    # the two busiest LEIs: fold `alias` into `canon`
    alias, canon = [r[0] for r in built.execute(
        "SELECT n.lei FROM transaction_norm n JOIN company c ON c.lei = n.lei "
        "GROUP BY n.lei ORDER BY COUNT(*) DESC, n.lei LIMIT 2"
    )]
    n = dict(built.execute(
        "SELECT lei, COUNT(*) FROM transaction_norm WHERE lei IN (?, ?) GROUP BY lei",
        (alias, canon),
    ).fetchall())
    built.execute("INSERT INTO issuer_alias VALUES (?, ?, 'test')", (alias, canon))

    merged = reads.company_detail(built, canon)
    assert merged.tx_count_total == n[alias] + n[canon]
    assert len(merged.recent_transactions) == min(n[alias] + n[canon],
                                                  config.COMPANY_TX_LIMIT)
    # the alias keeps its company row but owns no transactions any more
    folded = reads.company_detail(built, alias)
    assert folded.tx_count_total == 0
    assert folded.recent_transactions == []

    # the full history follows the same merge
    assert reads.company_transactions(built, canon).count == n[alias] + n[canon]
    assert reads.company_transactions(built, alias).transactions == []


def _busiest_lei(conn):
    return conn.execute(
        "SELECT n.lei FROM transaction_norm n JOIN company c ON c.lei = n.lei "
        "GROUP BY n.lei ORDER BY COUNT(*) DESC, n.lei LIMIT 1"
    ).fetchone()[0]


def test_company_transactions_is_the_full_history_with_recent_as_prefix(built, monkeypatch):
    monkeypatch.setattr(config, "COMPANY_TX_LIMIT", 2)
    lei = _busiest_lei(built)
    d = reads.company_detail(built, lei)
    full = reads.company_transactions(built, lei)
    assert full.count == len(full.transactions) == d.tx_count_total
    assert d.tx_counted_total == sum(t.is_counted for t in full.transactions)
    assert d.tx_count_total > len(d.recent_transactions) == 2
    # the page shows the capped list, then swaps in the full one and groups by
    # array order — so the capped list must be exactly its first rows
    assert full.transactions[:2] == d.recent_transactions


def test_every_transaction_carries_the_name_it_was_filed_under(built):
    lei = _busiest_lei(built)
    d = reads.company_detail(built, lei)
    rows = reads.company_transactions(built, lei).transactions
    assert all(t.issuer_name for t in rows)
    # what the name-variant filter relies on: each variant's rows are exactly
    # the rows carrying that issuer_name
    per_name: dict[str, int] = {}
    for t in rows:
        per_name[t.issuer_name] = per_name.get(t.issuer_name, 0) + 1
    assert per_name == {v.name: v.n_rows for v in d.name_variants}


def test_company_detail_ships_every_period_window(built):
    lei = _busiest_lei(built)
    windows = reads.company_detail(built, lei).windows
    assert [w.period for w in windows] == list(config.AGG_PERIODS)
    assert all(w.start <= w.end for w in windows)
    # where the aggregate wrote a window, the bounds are the periods table's own
    stored = {
        r["period"]: (r["period_start"], r["period_end"])
        for r in built.execute(
            "SELECT DISTINCT period, period_start, period_end FROM agg_company_period")
    }
    for w in windows:
        if w.period in stored:
            assert (w.start, w.end) == stored[w.period]
        else:
            assert (w.start, w.end) == config.period_bounds(w.period, config.today())


def test_company_detail_unknown_lei_is_none(built):
    assert reads.company_detail(built, "NOSUCHLEI0000000000") is None


def test_company_index_carries_no_person_data(built):
    ci = reads.company_index(built)
    dumped = json.dumps(ci.model_dump())
    assert "pdmr" not in dumped
    # every field is exactly lei / name / ticker
    assert set(ci.companies[0].model_dump()) == {"lei", "name", "ticker"}


def _exported_tickers(conn, lei):
    return {
        next(e for e in reads.leaderboard(conn, "all").entries if e.lei == lei).ticker,
        next(c for c in reads.company_index(conn).companies if c.lei == lei).ticker,
        reads.company_detail(conn, lei).ticker,
    }


@pytest.mark.parametrize(
    "raw,figi_name,yahoo,expected",
    [
        ("TELE2B", "TELE2 AB-B SHS", None, "TELE2-B"),     # the class from the name
        ("BEIJB", "BEIJER REF AB", "BEIJ-B.ST", "BEIJ-B"),  # the class from Yahoo
        ("BEIJB", "BEIJER REF AB", "OTHR-A.ST", "BEIJB"),   # not the same ticker
        ("BEIJB", "BEIJER REF AB", None, "BEIJB"),          # no class anywhere
    ],
)
def test_ticker_is_exported_with_the_class_dash_separated(built, raw, figi_name, yahoo,
                                                          expected):
    lei, isin = built.execute(
        "SELECT a.lei, c.primary_isin FROM agg_company_period a "
        "JOIN company c ON c.lei = a.lei "
        "WHERE a.period = 'all' AND c.primary_isin IS NOT NULL LIMIT 1"
    ).fetchone()
    built.execute("UPDATE company SET raw_ticker = ?, figi_name = ? WHERE lei = ?",
                  (raw, figi_name, lei))
    if yahoo:
        built.execute("INSERT INTO yahoo_symbol (isin, symbol, looked_up_at) "
                      "VALUES (?, ?, date('now'))", (isin, yahoo))
    assert _exported_tickers(built, lei) == {expected}
    # the stored value stays OpenFIGI's verbatim
    assert built.execute("SELECT raw_ticker FROM company WHERE lei = ?",
                         (lei,)).fetchone()[0] == raw


# ── export_static ─────────────────────────────────────────────────────────
def test_export_connection_is_read_only(built, tmp_path):
    """`insyn export-static` reads through `db.connect(read_only=True)`: reads
    work, and any write is refused by SQLite rather than silently applied."""
    ro = db.connect(tmp_path / "t.db", read_only=True)
    try:
        assert reads.meta(ro).norm_rows > 0
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE hack (x)")
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("UPDATE transaction_norm SET nature = 'x'")
    finally:
        ro.close()


def test_export_writes_the_documented_file_set(built, tmp_path):
    s = export_static.export(built, tmp_path / "dist")
    names = {p.relative_to(s.out_dir).as_posix() for p in s.out_dir.rglob("*.json")}
    assert {"meta.json", "companies.json", "data-quality.json",
            "leaderboard-30d.json", "leaderboard-90d.json",
            "leaderboard-365d.json", "leaderboard-all.json"} <= names
    assert "leaderboard-ytd.json" not in names           # the export has no ytd file
    assert any(n.startswith("company/") for n in names)
    assert {n.replace("company/", "company-tx/", 1)
            for n in names if n.startswith("company/")} <= names
    assert not s.warnings


def test_every_exported_file_is_nonempty_valid_json(built, tmp_path):
    s = export_static.export(built, tmp_path / "dist")
    for f in s.out_dir.rglob("*.json"):
        doc = json.loads(f.read_text())
        assert doc


def test_export_is_deterministic(built, tmp_path):
    a = export_static.export(built, tmp_path / "a")
    b = export_static.export(built, tmp_path / "b")
    for fa in a.out_dir.rglob("*.json"):
        fb = b.out_dir / fa.relative_to(a.out_dir)
        # computed_at differs; everything else must be byte-identical
        da = json.loads(fa.read_text())
        db_ = json.loads(fb.read_text())
        for d in (da, db_):
            d.pop("computed_at", None)
        assert da == db_


def test_export_total_size_is_small(built, tmp_path):
    s = export_static.export(built, tmp_path / "dist")
    assert s.bytes < 5_000_000  # "a few MB, not tens"


def test_export_flags_a_market_cap_zero_leak(built, tmp_path, monkeypatch):
    # force the guard's failure mode: a leaderboard entry with market_cap == 0
    real = reads.leaderboard

    def poisoned(conn, period):
        lb = real(conn, period)
        if lb.entries:
            lb.entries[0].market_cap = 0.0
            lb.entries[0].pct_of_mcap = -1.0
        return lb

    monkeypatch.setattr(reads, "leaderboard", poisoned)
    s = export_static.export(built, tmp_path / "dist")
    assert s.warnings
