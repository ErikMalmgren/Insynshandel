"""Phase 6 — shared reads (§8.2) + static export (§9). Hermetic."""

from __future__ import annotations

import json
from datetime import date

import pytest

from insynshandel import config, export_static
from insynshandel.api import reads
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


# ── reads: the shared layer ───────────────────────────────────────────────
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
    # §8.2: leaderboard sorting lives on the client, so the shared function must
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


def test_company_detail_unknown_lei_is_none(built):
    assert reads.company_detail(built, "NOSUCHLEI0000000000") is None


def test_company_index_carries_no_person_data(built):
    ci = reads.company_index(built)
    dumped = json.dumps(ci.model_dump())
    assert "pdmr" not in dumped
    # every field is exactly lei / name / ticker
    assert set(ci.companies[0].model_dump()) == {"lei", "name", "ticker"}


# ── export_static ─────────────────────────────────────────────────────────
def test_export_writes_the_documented_file_set(built, tmp_path):
    s = export_static.export(built, tmp_path / "dist")
    names = {p.relative_to(s.out_dir).as_posix() for p in s.out_dir.rglob("*.json")}
    assert {"meta.json", "companies.json", "data-quality.json",
            "leaderboard-30d.json", "leaderboard-90d.json",
            "leaderboard-365d.json", "leaderboard-all.json"} <= names
    assert "leaderboard-ytd.json" not in names           # §9 file list omits ytd
    assert any(n.startswith("company/") for n in names)
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
    assert s.bytes < 5_000_000  # "a few MB, not tens" (§9)


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
