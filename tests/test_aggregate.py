"""Phase 3 — classify (§6.2) + aggregate (§6.5). Hermetic; FX is seeded, not fetched."""

from __future__ import annotations

from datetime import date

import pytest

from insynshandel.pipeline import aggregate, ingest, normalize
from insynshandel.sources import fi

# a small FX table covering the fixture's non-SEK currencies for the relevant years
_FX = [
    ("GBP", "2016-01-01", 11.5), ("GBP", "2026-01-01", 12.9),
    ("CAD", "2016-01-01", 6.5), ("CAD", "2026-01-01", 7.0),
    ("EUR", "2016-01-01", 9.3), ("EUR", "2026-01-01", 11.2),
    ("USD", "2016-01-01", 8.5), ("USD", "2026-01-01", 9.5),
]


def _seed_fx(conn):
    conn.executemany(
        "INSERT INTO fx_rate (currency, rate_date, sek_per_unit) VALUES (?, ?, ?)", _FX
    )


def _ingest(conn, data: bytes):
    rows = fi.parse_export(data)
    leaf = fi.Leaf(date(2016, 7, 1), date(2026, 5, 1), rows, truncated=False)
    ingest.write_leaf(conn, ingest._open_batch(conn, "t", leaf), leaf)
    normalize.normalize(conn)


@pytest.fixture
def classified(db_conn, sample_export_bytes):
    _seed_fx(db_conn)
    _ingest(db_conn, sample_export_bytes)
    s = aggregate.classify(db_conn)
    return db_conn, s


# ── unmapped Karaktär must fail the build (§6.1.2) ─────────────────────────
def test_unmapped_nature_fails_classify(db_conn, sample_bytes):
    _seed_fx(db_conn)
    _ingest(db_conn, sample_bytes)  # full fixture incl the synthetic unmapped row
    s = aggregate.classify(db_conn)
    assert not s.ok
    assert "Testkaraktär utan mappning" in s.unmapped_natures
    # nothing was written to the derived columns
    assert db_conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE is_counted IS NOT NULL"
    ).fetchone()[0] == 0


def test_run_returns_early_on_unmapped(db_conn, sample_bytes):
    _seed_fx(db_conn)
    _ingest(db_conn, sample_bytes)
    result = aggregate.run(db_conn)
    assert not result.ok
    assert db_conn.execute("SELECT COUNT(*) FROM agg_company_period").fetchone()[0] == 0


# ── the ordered §6.2 filter ───────────────────────────────────────────────
def test_every_uncounted_row_has_a_reason(classified):
    conn, _ = classified
    orphans = conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE is_counted = 0 AND exclude_reason IS NULL"
    ).fetchone()[0]
    assert orphans == 0


def test_exclude_reasons_are_all_known(classified):
    conn, _ = classified
    reasons = {
        r["exclude_reason"]
        for r in conn.execute(
            "SELECT DISTINCT exclude_reason FROM transaction_norm WHERE is_counted = 0"
        )
    }
    assert reasons <= set(aggregate.EXCLUDE_REASONS)


def test_forvarv_and_avyttring_are_counted(classified):
    conn, _ = classified
    for nature, sign in (("Förvärv", 1), ("Avyttring", -1)):
        rows = conn.execute(
            "SELECT is_counted, sign FROM transaction_norm WHERE nature = ? "
            "AND status = 'Aktuell' AND volume_unit = 'Antal' AND lei <> ''",
            (nature,),
        ).fetchall()
        assert rows
        assert all(r["sign"] == sign for r in rows)
        assert all(r["is_counted"] == 1 for r in rows)


def test_reviderad_excluded_as_not_current(classified):
    conn, _ = classified
    with_lei = conn.execute(
        "SELECT exclude_reason FROM transaction_norm "
        "WHERE status = 'Reviderad' AND lei <> ''"
    ).fetchall()
    assert with_lei
    assert all(r["exclude_reason"] == "not_current" for r in with_lei)
    # a Reviderad row that ALSO lacks a LEI reads 'no_lei' — no_lei is rule 0
    assert all(
        r["exclude_reason"] == "no_lei"
        for r in conn.execute(
            "SELECT exclude_reason FROM transaction_norm "
            "WHERE status = 'Reviderad' AND lei = ''"
        )
    )


def test_belopp_volume_unit_excluded(classified):
    conn, _ = classified
    row = conn.execute(
        "SELECT exclude_reason, is_counted FROM transaction_norm WHERE volume_unit = 'Belopp'"
    ).fetchone()
    assert row["is_counted"] == 0
    assert row["exclude_reason"] == "volume_unit"


def test_no_lei_rows_excluded_first(classified):
    conn, _ = classified
    rows = conn.execute(
        "SELECT exclude_reason, is_counted FROM transaction_norm WHERE lei = ''"
    ).fetchall()
    assert rows  # the fixture's 2016 rows
    assert all(r["is_counted"] == 0 and r["exclude_reason"] == "no_lei" for r in rows)


def test_status_beats_volume_unit_in_ordering(db_conn):
    """A row that is both Reviderad and Belopp reads 'not_current', not 'volume_unit'."""
    _seed_fx(db_conn)
    fields = {n: "" for n in fi.FIELD_NAMES}
    fields.update(
        publiceringsdatum="2024-05-01 10:00:00", transaktionsdatum="2024-05-01 00:00:00",
        lei_kod="LEI123", emittent="X AB", karaktar="Förvärv", instrumenttyp="Konvertibel",
        volym="100", volymsenhet="Belopp", pris="1", valuta="SEK", status="Reviderad",
        person_i_ledande_stallning="P",
    )
    row = fi.ParsedRow(fi.row_hash([fields[n] for n in fi.FIELD_NAMES]), 0, fields)
    leaf = fi.Leaf(date(2024, 5, 1), date(2024, 5, 1), [row], truncated=False)
    ingest.write_leaf(db_conn, ingest._open_batch(db_conn, "t", leaf), leaf)
    normalize.normalize(db_conn)
    aggregate.classify(db_conn)
    assert db_conn.execute(
        "SELECT exclude_reason FROM transaction_norm WHERE lei = 'LEI123'"
    ).fetchone()[0] == "not_current"


# ── currency conversion (§6.3) ────────────────────────────────────────────
def test_sek_rows_convert_at_identity(classified):
    conn, _ = classified
    for r in conn.execute(
        "SELECT volume, price, gross_value, gross_value_sek, fx_rate_sek "
        "FROM transaction_norm WHERE currency = 'SEK' AND volume IS NOT NULL "
        "AND price IS NOT NULL LIMIT 20"
    ):
        assert r["fx_rate_sek"] == 1.0
        assert r["gross_value_sek"] == pytest.approx(r["volume"] * r["price"])


def test_gbp_row_uses_transaction_date_rate_not_today(classified):
    conn, _ = classified
    r = conn.execute(
        "SELECT transaction_date, fx_rate_date, fx_rate_sek, gross_value, gross_value_sek "
        "FROM transaction_norm WHERE currency = 'GBP' AND gross_value IS NOT NULL LIMIT 1"
    ).fetchone()
    assert r is not None
    # fixture GBP row (SinterCast) is 2026-dated -> the 2026 rate, not the 2016 one
    assert r["fx_rate_sek"] == 12.9
    assert r["fx_rate_date"] <= r["transaction_date"]
    assert r["gross_value_sek"] == pytest.approx(r["gross_value"] * 12.9)


# ── outliers (§6.4) ───────────────────────────────────────────────────────
def test_outlier_needs_a_market_cap_to_fire(classified):
    conn, s = classified
    # no market caps seeded -> nothing can be an outlier, everything unverifiable
    assert s.by_verification.get("outlier", 0) == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE verification = 'unverifiable'"
    ).fetchone()[0] == s.rows


def test_outlier_excluded_but_unverifiable_counted(db_conn):
    _seed_fx(db_conn)
    def mk(lei, vol, price, cap):
        f = {n: "" for n in fi.FIELD_NAMES}
        f.update(publiceringsdatum="2024-05-02 10:00:00",
                 transaktionsdatum="2024-05-02 00:00:00", lei_kod=lei, emittent=lei,
                 karaktar="Förvärv", instrumenttyp="Aktie", isin=f"ISIN{lei}",
                 volym=str(vol), volymsenhet="Antal", pris=str(price), valuta="SEK",
                 status="Aktuell", person_i_ledande_stallning="P")
        return f, cap
    specs = [mk("BIG", 100_000_000, 100_000_000, 3.0e11),   # 1e16 >> cap -> outlier
             mk("OKAY", 1000, 50, 3.0e11),                   # tiny -> ok
             mk("NOCAP", 1000, 50, None)]                    # no cap -> unverifiable
    rows = [fi.ParsedRow(fi.row_hash([f[n] for n in fi.FIELD_NAMES]), 0, f)
            for f, _ in specs]
    leaf = fi.Leaf(date(2024, 5, 2), date(2024, 5, 2), rows, truncated=False)
    ingest.write_leaf(db_conn, ingest._open_batch(db_conn, "t", leaf), leaf)
    normalize.normalize(db_conn)
    for (f, cap) in specs:
        if cap is not None:
            db_conn.execute(
                "INSERT INTO market_cap (lei, as_of, market_cap, currency, market_cap_sek, "
                "source) VALUES (?, '2024-05-02', ?, 'SEK', ?, 'test')",
                (f["lei_kod"], cap, cap),
            )
    aggregate.run(db_conn)
    got = {
        r["lei"]: (r["verification"], r["is_counted"], r["exclude_reason"])
        for r in db_conn.execute(
            "SELECT lei, verification, is_counted, exclude_reason FROM transaction_norm"
        )
    }
    assert got["BIG"] == ("outlier", 0, "outlier")
    assert got["OKAY"] == ("ok", 1, None)
    assert got["NOCAP"] == ("unverifiable", 1, None)   # unverifiable IS counted
    # the outlier is absent from every aggregate window
    assert db_conn.execute(
        "SELECT COUNT(*) FROM agg_company_period WHERE lei = 'BIG'"
    ).fetchone()[0] == 0
    assert db_conn.execute(
        "SELECT COUNT(*) FROM agg_company_period WHERE lei = 'NOCAP'"
    ).fetchone()[0] > 0


def test_market_cap_zero_serialises_as_null_never_negative(db_conn):
    db_conn.execute(
        "INSERT INTO market_cap (lei, as_of, market_cap, currency, market_cap_sek, source) "
        "VALUES ('Z', '2024-01-01', 0, 'SEK', 0, 'test')"
    )
    row = db_conn.execute(
        "SELECT market_cap_sek FROM market_cap_current WHERE lei = 'Z'"
    ).fetchone()
    assert row["market_cap_sek"] is None  # the CASE-without-ELSE guard (§6.4)


# ── aggregate pass (§6.5) ─────────────────────────────────────────────────
def test_windows_written_and_all_is_widest(db_conn, sample_export_bytes):
    _seed_fx(db_conn)
    _ingest(db_conn, sample_export_bytes)
    summary = aggregate.run(db_conn)
    assert set(summary.periods) == set(aggregate.config.AGG_PERIODS)
    rows = {
        r["period"]: (r["period_start"], r["period_end"])
        for r in db_conn.execute("SELECT DISTINCT period, period_start, period_end "
                                 "FROM agg_company_period")
    }
    assert rows["all"][0] == aggregate.config.FI_EARLIEST


def test_net_equals_buy_minus_sell_and_tx_count_matches_counted(db_conn, sample_export_bytes):
    _seed_fx(db_conn)
    _ingest(db_conn, sample_export_bytes)
    aggregate.run(db_conn)
    for r in db_conn.execute(
        "SELECT lei, buy_value_sek, sell_value_sek, net_value_sek, tx_count "
        "FROM agg_company_period WHERE period = 'all'"
    ):
        assert r["net_value_sek"] == pytest.approx(r["buy_value_sek"] - r["sell_value_sek"])
        counted = db_conn.execute(
            "SELECT COUNT(*) FROM transaction_norm n LEFT JOIN issuer_alias a "
            "ON a.alias_lei = n.lei WHERE COALESCE(a.canonical_lei, n.lei) = ? "
            "AND n.is_counted = 1", (r["lei"],),
        ).fetchone()[0]
        assert r["tx_count"] == counted


def test_issuer_alias_folds_two_leis_into_one(db_conn, sample_export_bytes):
    _seed_fx(db_conn)
    _ingest(db_conn, sample_export_bytes)
    # classify first (it reloads issuer_alias from the seed CSV); insert the
    # alias afterwards so the aggregate pass is what we're testing
    aggregate.classify(db_conn)
    leis = [
        r["lei"] for r in db_conn.execute(
            "SELECT lei, COUNT(*) n FROM transaction_norm WHERE is_counted = 1 "
            "GROUP BY lei ORDER BY n DESC LIMIT 2"
        )
    ]
    assert len(leis) == 2

    aggregate.aggregate_periods(db_conn)
    before = db_conn.execute(
        "SELECT tx_count FROM agg_company_period WHERE lei = ? AND period = 'all'",
        (leis[0],),
    ).fetchone()["tx_count"]
    alias_tx = db_conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE lei = ? AND is_counted = 1",
        (leis[1],),
    ).fetchone()[0]

    db_conn.execute(
        "INSERT INTO issuer_alias (alias_lei, canonical_lei, note) VALUES (?, ?, 't')",
        (leis[1], leis[0]),
    )
    aggregate.aggregate_periods(db_conn)

    assert db_conn.execute(
        "SELECT COUNT(*) FROM agg_company_period WHERE lei = ? AND period = 'all'",
        (leis[1],),
    ).fetchone()[0] == 0  # the alias LEI never appears as its own row
    assert db_conn.execute(
        "SELECT tx_count FROM agg_company_period WHERE lei = ? AND period = 'all'",
        (leis[0],),
    ).fetchone()["tx_count"] == before + alias_tx


def test_reaggregation_is_idempotent(db_conn, sample_export_bytes):
    _seed_fx(db_conn)
    _ingest(db_conn, sample_export_bytes)
    aggregate.run(db_conn)
    n1 = db_conn.execute("SELECT COUNT(*), SUM(net_value_sek) FROM agg_company_period").fetchone()
    aggregate.run(db_conn)
    n2 = db_conn.execute("SELECT COUNT(*), SUM(net_value_sek) FROM agg_company_period").fetchone()
    assert tuple(n1) == tuple(n2)
