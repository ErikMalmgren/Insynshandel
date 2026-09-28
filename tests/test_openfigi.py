"""OpenFIGI — home-venue selection, batching, and Yahoo/Stockholm symbol shaping.

`fixtures/openfigi_mapping.json` holds real v3 mapping answers (2026-09-28,
`marketSecDes: Equity`), trimmed to a few listings each with the home one never
first — selection must not depend on position.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from insynshandel.sources.openfigi import (
    OpenFIGIClient,
    display_ticker,
    format_ticker,
    pick_home,
)

MAPPING = json.loads(
    (Path(__file__).parent / "fixtures" / "openfigi_mapping.json").read_text()
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers: dict[str, str] = {}
        self.bodies: list[list[dict]] = []

    def post(self, url, json, timeout):
        self.bodies.append(json)
        return self.response


def _client(response) -> OpenFIGIClient:
    client = OpenFIGIClient(api_key="", session=FakeSession(response))
    client.spacing_s = 0
    return client


# ── home-venue selection ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    "isin,exch,ticker",
    [
        ("SE0021921269", "SS", "SAABB"),   # Nasdaq Stockholm, among GR/GF/GD listings
        ("SE0010469221", "SF", "TSEC"),    # First North — invisible to a micCode XSTO filter
        ("SE0017911241", "KA", "TRANB"),   # Spotlight
        ("SE0009994445", "NG", "SDS"),     # NGM
    ],
)
def test_home_listing_is_picked_from_real_answers(isin, exch, ticker):
    hit = pick_home(MAPPING[isin]["data"])
    assert (hit["exchCode"], hit["ticker"]) == (exch, ticker)


def test_pre_split_isin_with_only_cross_listings_has_no_home():
    # SAAB's old ISIN: still maps, but only to X1 composites in GBX/EUR/GBP
    assert pick_home(MAPPING["SE0000112385"]["data"]) is None


def test_home_preference_order_and_equity_only():
    hits = [
        {"exchCode": "NOMX STOCKHOLM", "ticker": "SWEDA 11 04/26/19", "marketSector": "Corp"},
        {"exchCode": "SS", "ticker": "BOND", "marketSector": "Corp"},
        {"exchCode": "SF", "ticker": "FN", "marketSector": "Equity"},
        {"exchCode": "SS", "ticker": "MAIN", "marketSector": "Equity"},
    ]
    assert pick_home(hits)["ticker"] == "MAIN"
    assert pick_home(hits[:2]) is None     # a bond is never a company ticker


# ── batched client ────────────────────────────────────────────────────────
def test_map_isins_is_positional_and_maps_the_mic():
    isins = ["SE0021921269", "SE0010469221", "SE0027598178"]
    client = _client(FakeResponse([MAPPING[i] for i in isins]))
    got = client.map_isins(isins)

    assert [r.isin for r in got] == isins
    saab, tempest, bond = got
    assert (saab.raw_ticker, saab.exch_code, saab.mic_code, saab.ticker) == (
        "SAABB", "SS", "XSTO", "SAAB-B.ST")
    assert (tempest.exch_code, tempest.mic_code) == ("SF", "FNSE")
    assert bond.ticker is None and bond.raw_ticker is None   # "No identifier found."
    # one request, no MIC filter, equities only
    body = client.session.bodies[0]
    assert [j["idValue"] for j in body] == isins
    assert all("micCode" not in j and j["marketSecDes"] == "Equity" for j in body)


def test_per_job_error_is_a_negative_carrying_the_message():
    client = _client(FakeResponse([{"error": "Invalid idValue format."}]))
    (r,) = client.map_isins(["SEBOGUS"])
    assert r.ticker is None and r.error == "Invalid idValue format."


def test_short_answer_raises_rather_than_misattributing():
    client = _client(FakeResponse([MAPPING["SE0021921269"]]))
    with pytest.raises(ValueError):
        client.map_isins(["SE0021921269", "SE0010469221"])


def test_rate_limit_raises():
    client = _client(FakeResponse([], status=429))
    with pytest.raises(requests.HTTPError):
        client.map_isins(["SE0021921269"])


def test_batch_size_follows_the_api_key(monkeypatch):
    monkeypatch.delenv("OPENFIGI_API_KEY", raising=False)
    assert _client(FakeResponse([])).batch_size == 10
    assert OpenFIGIClient(api_key="k", session=FakeSession(None)).batch_size == 100
    with pytest.raises(ValueError):
        _client(FakeResponse([])).map_isins(["X"] * 11)


# ── format_ticker ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,name,expected",
    [
        # real OpenFIGI names: the class is in the name, not the ticker
        ("SAABB", "SAAB AB-B", "SAAB-B.ST"),
        ("SSABB", "SSAB AB - B SHARES", "SSAB-B.ST"),
        ("HMB", "HENNES & MAURITZ AB-B SHS", "HM-B.ST"),
        ("INVEB", "INVESTOR AB-B SHS", "INVE-B.ST"),
        ("ATCOA", "ATLAS COPCO AB-A SHS", "ATCO-A.ST"),
        ("TRANB", "TRANSFERATOR AB-B", "TRAN-B.ST"),
        ("NCCB", "NCC AB SER. B", "NCC-B.ST"),
        ("TELE2B", "TELE2 AB-B SHS", "TELE2-B.ST"),
        ("BALDB", "FASTIGHETS AB BALDER-B SHRS", "BALD-B.ST"),
        ("COREB", "COREM PROPERTY GROUP-B SHARE", "CORE-B.ST"),
        ("K2AB", "K2A KNAUST & ANDERSSON-B SHR", "K2A-B.ST"),
        ("VITB", "VITEC SOFTWARE GROUP AB-B SH", "VIT-B.ST"),
        ("PEABB", "PEAB AB-CLASS B", "PEAB-B.ST"),
        # no class in the name: never split on a trailing A–D
        ("TELIA", "TELIA CO AB", "TELIA.ST"),
        ("SAND", "SANDVIK AB", "SAND.ST"),
        ("VIVA", "VIVA WINE GROUP AB", "VIVA.ST"),
        ("ALFA", "ALFA LAVAL AB", "ALFA.ST"),
        ("ABB", "ABB LTD-REG", "ABB.ST"),
        ("TSEC", "TEMPEST SECURITY AB", "TSEC.ST"),
        # a space or SDB is already explicit
        ("INVE B", "INVESTOR AB SER. B", "INVE-B.ST"),
        ("ERIC B", "TELEFONAKTIEBOLAGET LM ERICSSON", "ERIC-B.ST"),
        ("ALIV SDB", "", "ALIV-SDB.ST"),
        ("VOLV", "VOLVO AB", "VOLV.ST"),
    ],
)
def test_format_ticker(raw, name, expected):
    assert format_ticker(raw, name) == expected


def test_class_in_name_must_match_the_tickers_last_letter():
    # the name says B but the ticker doesn't end in B: leave it alone
    assert format_ticker("XYZ", "XYZ AB-B") == "XYZ.ST"


def test_lowercase_and_whitespace_normalised():
    assert format_ticker("  inve b  ", "investor ab ser. b") == "INVE-B.ST"


@pytest.mark.parametrize(
    "raw,name,expected",
    [
        ("TELE2B", "TELE2 AB-B SHS", "TELE2-B"),
        ("INVE B", "INVESTOR AB SER. B", "INVE-B"),
        ("ALIV SDB", "", "ALIV-SDB"),
        ("TELIA", "TELIA CO AB", "TELIA"),
        ("TELE2B", "", "TELE2B"),    # no name (a v1 row): never guess the class
    ],
)
def test_display_ticker_is_format_ticker_without_the_suffix(raw, name, expected):
    assert display_ticker(raw, name) == expected
    assert format_ticker(raw, name) == expected + ".ST"
