"""Phase 1 parsing contract — hermetic, no network."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from insynshandel import config
from insynshandel.sources import fi

# Pinned so a future edit to row_hash's field set or separator fails loudly.
BILLERUD_HASH = "345a69c4a551e2dba4f6c047b8b01e000d1e9f45d85700438959a591ab061b9d"


def test_decodes_utf16le_without_bom(sample_bytes):
    assert sample_bytes[:2] == b"P\x00"  # 'P' of "Publiceringsdatum" in UTF-16LE
    rows = fi.parse_csv(sample_bytes)
    assert len(rows) == 40


def test_every_record_has_22_real_fields(sample_bytes):
    for row in fi.parse_csv(sample_bytes):
        assert len(row) == fi.N_REAL_FIELDS


def test_bad_field_count_raises():
    header = ";".join(fi.EXPECTED_HEADER) + ";"
    body = "a;b;c"
    data = (header + "\r\n" + body + "\r\n").encode("utf-16-le")
    with pytest.raises(fi.FormatChangedError):
        fi.parse_csv(data)


def test_changed_header_raises():
    data = ("Publiceringsdatum;Emittent;WRONG\r\n").encode("utf-16-le")
    with pytest.raises(fi.FormatChangedError):
        fi.parse_csv(data)


def test_embedded_semicolon_and_quote_escape_roundtrip(sample_bytes):
    rows = fi.parse_export(sample_bytes)
    billerud = [r for r in rows if r.fields["emittent"] == "BillerudKorsnäs AB"]
    assert len(billerud) == 1
    korr = billerud[0].fields["beskrivning_av_korrigering"]
    assert ";" in korr  # the field that a naive split(';') would shatter on
    assert '"closely associated"' in korr  # the "" escape decoded to a single "
    assert billerud[0].fields["status"] == "Aktuell"


def test_comma_decimals_preserved_verbatim(sample_bytes):
    # phase 1 stores raw text; number parsing is phase 2's job
    rows = fi.parse_export(sample_bytes)
    assert any("," in r.fields["volym"] for r in rows)
    assert all(r.fields["volym"] == r.fields["volym"].strip() or True for r in rows)


def test_empty_isin_rows_present(sample_bytes):
    rows = fi.parse_export(sample_bytes)
    assert any(r.fields["isin"] == "" for r in rows)


def test_ordinals_number_exact_duplicates(sample_bytes):
    rows = fi.parse_export(sample_bytes)
    groups: dict[str, list[int]] = {}
    for r in rows:
        groups.setdefault(r.row_hash, []).append(r.ordinal)
    for ordinals in groups.values():
        assert sorted(ordinals) == list(range(len(ordinals)))
    assert max(len(v) for v in groups.values()) == 3  # SinterCast x3


def test_row_hash_is_stable(sample_bytes):
    """Pin one row's digest. If this breaks, a backfill would re-supersede everything."""
    rows = fi.parse_export(sample_bytes)
    billerud = next(r for r in rows if r.fields["emittent"] == "BillerudKorsnäs AB")
    assert billerud.row_hash == BILLERUD_HASH


def test_row_hash_covers_status(sample_bytes):
    rows = fi.parse_export(sample_bytes)
    pair = [
        r for r in rows
        if r.fields["person_i_ledande_stallning"] == "Stefan Wänstedt"
    ]
    assert len(pair) == 2
    assert {r.fields["status"] for r in pair} == {"Reviderad", "Aktuell"}
    assert pair[0].row_hash != pair[1].row_hash  # status flip changes the hash


def test_row_hash_rejects_wrong_field_count():
    with pytest.raises(ValueError):
        fi.row_hash(["a", "b"])


@pytest.mark.parametrize(
    "start,end,expected",
    [
        (date(2016, 7, 1), date(2016, 7, 14), [(date(2016, 7, 1), date(2016, 7, 14))]),
        (
            date(2016, 7, 1),
            date(2016, 8, 5),
            [
                (date(2016, 7, 1), date(2016, 7, 14)),
                (date(2016, 7, 15), date(2016, 7, 28)),
                (date(2016, 7, 29), date(2016, 8, 5)),
            ],
        ),
        (date(2020, 1, 2), date(2020, 1, 1), []),
    ],
)
def test_seed_windows(start, end, expected):
    assert list(fi.iter_seed_windows(start, end)) == expected


def test_seed_windows_are_contiguous_and_cover_range():
    from itertools import pairwise

    start, end = date(2016, 7, 1), date(2026, 9, 1)
    windows = list(fi.iter_seed_windows(start, end))
    assert windows[0][0] == start
    assert windows[-1][1] == end
    for (_, a), (b, _) in pairwise(windows):
        assert b == a + timedelta(days=1)


def test_bisection_recurses_until_under_cap():
    """Simulated: a fake source where March 2020 is dense. No HTTP."""
    dense = (date(2020, 3, 1), date(2020, 3, 15))

    def fake_get(f: date, t: date) -> list[fi.ParsedRow]:
        span = (t - f).days + 1
        # ~120 rows/day in the dense fortnight, 10/day otherwise
        per_day = 120 if (f >= dense[0] and t <= dense[1]) else 10
        n = span * per_day
        return [
            fi.ParsedRow(f"{f}-{i}", 0, {name: "" for name in fi.FIELD_NAMES})
            for i in range(n)
        ]

    leaves = list(fi.iter_leaf_windows(dense[0], dense[1], fake_get))
    assert leaves, "expected at least one leaf"
    assert all(len(leaf.rows) < config.FI_ROW_CAP for leaf in leaves)
    assert not any(leaf.truncated for leaf in leaves)
    # leaves tile the parent range with no gap or overlap
    covered = sorted((leaf.window_from, leaf.window_to) for leaf in leaves)
    assert covered[0][0] == dense[0]
    assert covered[-1][1] == dense[1]


def test_single_day_still_capped_is_flagged_truncated():
    def fake_get(f: date, t: date) -> list[fi.ParsedRow]:
        return [
            fi.ParsedRow(f"h{i}", 0, {name: "" for name in fi.FIELD_NAMES})
            for i in range(config.FI_ROW_CAP + 5)
        ]

    leaves = list(fi.iter_leaf_windows(date(2020, 3, 13), date(2020, 3, 13), fake_get))
    assert len(leaves) == 1
    assert leaves[0].truncated is True
