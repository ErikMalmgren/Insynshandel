"""Phase 2 — transaction_norm rebuild from raw_live (§5). Hermetic."""

from __future__ import annotations

from datetime import date

import pytest

from insynshandel.pipeline import ingest, normalize
from insynshandel.sources import fi


@pytest.fixture
def normalized(db_conn, sample_bytes):
    """Ingest the full fixture (incl the 2 synthetic TEST rows) then normalize."""
    rows = fi.parse_export(sample_bytes)
    leaf = fi.Leaf(date(2016, 7, 1), date(2026, 5, 1), rows, truncated=False)
    ingest.write_leaf(db_conn, ingest._open_batch(db_conn, "test", leaf), leaf)
    summary = normalize.normalize(db_conn)
    return db_conn, summary


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2 600 000,0", 2600000.0),
        ("105,6997", 105.6997),
        ("1\xa0234,5", 1234.5),
        ("0,0", 0.0),
        ("", None),
        ("   ", None),
        ("not a number", None),
        ("7.0685", 7.0685),
    ],
)
def test_parse_number(raw, expected):
    assert normalize.parse_number(raw) == expected


def test_row_count_preserved(normalized):
    conn, s = normalized
    n_raw = conn.execute("SELECT COUNT(*) FROM raw_live").fetchone()[0]
    n_norm = conn.execute("SELECT COUNT(*) FROM transaction_norm").fetchone()[0]
    assert n_raw == n_norm == s.rows_in == s.rows_out
    assert s.ok


def test_clean_fixture_has_no_unparseable_numbers(normalized):
    _, s = normalized
    assert s.unparseable_numbers == 0


def test_unparseable_number_counted_and_stored_null(db_conn, sample_bytes):
    rows = fi.parse_export(sample_bytes)
    # corrupt one row's price into something float() rejects
    bad = rows[0]
    bad.fields["pris"] = "1,2,3"
    leaf = fi.Leaf(date(2016, 7, 1), date(2026, 5, 1), rows, truncated=False)
    ingest.write_leaf(db_conn, ingest._open_batch(db_conn, "t", leaf), leaf)
    s = normalize.normalize(db_conn)
    assert s.unparseable_numbers == 1
    assert db_conn.execute(
        "SELECT price FROM transaction_norm WHERE raw_id = "
        "(SELECT id FROM raw_transaction WHERE row_hash = ? AND ordinal = 0)",
        (bad.row_hash,),
    ).fetchone()[0] is None


def test_required_fields_never_null(normalized):
    conn, _ = normalized
    bad = conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE published_date IS NULL "
        "OR transaction_date IS NULL OR nature IS NULL OR status IS NULL"
    ).fetchone()[0]
    assert bad == 0


def test_missing_lei_is_carried_through_not_nulled(normalized):
    """2016-2017 rows lack a LEI (inv 9/12 overstate coverage). '' not NULL,
    counted as a statistic, never a hard failure."""
    conn, s = normalized
    nulls = conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE lei IS NULL"
    ).fetchone()[0]
    blanks = conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE lei = ''"
    ).fetchone()[0]
    assert nulls == 0
    assert blanks > 0  # the fixture's 2016 rows
    assert s.rows_without_lei == blanks
    assert s.ok  # missing LEI does not fail the rebuild


def test_derived_columns_left_null(normalized):
    conn, _ = normalized
    dirty = conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE sign IS NOT NULL "
        "OR is_counted IS NOT NULL OR verification IS NOT NULL "
        "OR exclude_reason IS NOT NULL OR gross_value IS NOT NULL "
        "OR fx_rate_sek IS NOT NULL OR gross_value_sek IS NOT NULL"
    ).fetchone()[0]
    assert dirty == 0


def test_transaction_date_has_no_time_component(normalized):
    conn, _ = normalized
    assert all(
        len(r["transaction_date"]) == 10
        for r in conn.execute("SELECT transaction_date FROM transaction_norm")
    )


def test_whitespace_stripped_from_isin(normalized):
    conn, _ = normalized
    # the fixture carries 'SE0005454873 ' (trailing space) as a synthetic row
    isins = [r["isin"] for r in conn.execute("SELECT isin FROM transaction_norm")]
    assert all(i == i.strip() for i in isins)
    assert "SE0005454873" in isins


def test_empty_isin_preserved_not_rejected(normalized):
    conn, _ = normalized
    assert conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE isin = ''"
    ).fetchone()[0] > 0


def test_nature_is_verbatim_including_the_synthetic_unmapped_value(normalized):
    conn, _ = normalized
    natures = {r["nature"] for r in conn.execute("SELECT DISTINCT nature FROM transaction_norm")}
    assert "Testkaraktär utan mappning" in natures  # aggregate must fail on this
    assert "Förvärv" in natures


def test_booleans_map_ja_to_one(normalized):
    conn, _ = normalized
    # fixture has "Förvärv linked to share program" rows (Är kopplad... = Ja)
    assert conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE is_share_program = 1"
    ).fetchone()[0] > 0
    assert conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE is_share_program NOT IN (0, 1)"
    ).fetchone()[0] == 0


def test_belopp_volume_unit_survives_verbatim(normalized):
    conn, _ = normalized
    # the Lyko 'Belopp' row — normalize keeps it; aggregate is what excludes it
    assert conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE volume_unit = 'Belopp'"
    ).fetchone()[0] == 1


def test_full_rebuild_is_idempotent_and_reflects_deletions(db_conn, sample_bytes):
    rows = fi.parse_export(sample_bytes)
    leaf = fi.Leaf(date(2016, 7, 1), date(2026, 5, 1), rows, truncated=False)
    ingest.write_leaf(db_conn, ingest._open_batch(db_conn, "t", leaf), leaf)

    normalize.normalize(db_conn)
    n1 = db_conn.execute("SELECT COUNT(*) FROM transaction_norm").fetchone()[0]
    s2 = normalize.normalize(db_conn)
    n2 = db_conn.execute("SELECT COUNT(*) FROM transaction_norm").fetchone()[0]
    assert n1 == n2 == s2.rows_out

    # supersede every live row -> next normalize empties transaction_norm
    db_conn.execute("UPDATE raw_transaction SET superseded_at = 'now'")
    s3 = normalize.normalize(db_conn)
    assert s3.rows_in == 0
    assert db_conn.execute("SELECT COUNT(*) FROM transaction_norm").fetchone()[0] == 0


def test_reviderad_rows_are_normalized_but_marked(normalized):
    conn, _ = normalized
    # raw keeps Reviderad/Makulerad rows; normalize carries status through so
    # aggregate (§6.2 rule 1) can exclude them
    statuses = {r["status"] for r in conn.execute("SELECT DISTINCT status FROM transaction_norm")}
    assert "Reviderad" in statuses
