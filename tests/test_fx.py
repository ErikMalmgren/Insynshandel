"""FxTable — forward-filled Riksbank lookup (§6.3.1)."""

from __future__ import annotations

from insynshandel.pipeline.fx import FxTable


def _table():
    return FxTable({
        "GBP": [("2016-07-01", 12.0), ("2016-07-04", 12.1), ("2016-07-05", 12.2)],
        "USD": [("2020-01-02", 9.4), ("2020-01-03", 9.5)],
    })


def test_sek_is_identity():
    hit = _table().rate("SEK", "2021-05-05")
    assert hit.sek_per_unit == 1.0
    assert hit.rate_date == "2021-05-05"


def test_exact_business_day():
    hit = _table().rate("GBP", "2016-07-04")
    assert (hit.sek_per_unit, hit.rate_date) == (12.1, "2016-07-04")


def test_weekend_forward_fills_from_preceding_business_day():
    # 2016-07-02 and 03 are a weekend; must resolve to Friday the 1st
    hit = _table().rate("GBP", "2016-07-03")
    assert (hit.sek_per_unit, hit.rate_date) == (12.0, "2016-07-01")


def test_before_series_start_is_none():
    assert _table().rate("USD", "2019-12-31") is None


def test_unknown_currency_is_none():
    assert _table().rate("JPY", "2020-01-02") is None


def test_after_last_observation_uses_last():
    hit = _table().rate("GBP", "2026-01-01")
    assert hit.rate_date == "2016-07-05"


def test_load_from_db(db_conn):
    db_conn.executemany(
        "INSERT INTO fx_rate (currency, rate_date, sek_per_unit) VALUES (?, ?, ?)",
        [("EUR", "2016-07-01", 9.4), ("EUR", "2016-07-04", 9.5)],
    )
    t = FxTable.load(db_conn)
    assert t.rate("EUR", "2016-07-03").rate_date == "2016-07-01"
    assert t.currencies() == {"EUR", "SEK"}
