"""format_ticker — ported Yahoo/Stockholm symbol shaping (§7.3)."""

from __future__ import annotations

import pytest

from insynshandel.sources.openfigi import EXCLUDE_TICKERS, format_ticker


@pytest.mark.parametrize(
    "raw,name,expected",
    [
        ("INVE B", "INVESTOR AB SER. B", "INVE-B.ST"),
        ("NCCB", "NCC AB SER. B", "NCC-B.ST"),
        ("ALIV SDB", "", "ALIV-SDB.ST"),
        ("ABB", "", "ABB.ST"),
        ("SSABB", "SSAB AB SER. B", "SSAB-B.ST"),
        ("ERIC B", "TELEFONAKTIEBOLAGET LM ERICSSON", "ERIC-B.ST"),
        ("VOLV", "VOLVO AB", "VOLV.ST"),
    ],
)
def test_format_ticker(raw, name, expected):
    assert format_ticker(raw, name) == expected


def test_excluded_tickers_never_suffixed():
    for t in EXCLUDE_TICKERS:
        assert format_ticker(t, "") == f"{t}.ST"


def test_lowercase_and_whitespace_normalised():
    assert format_ticker("  inve b  ", "investor ab ser. b") == "INVE-B.ST"
