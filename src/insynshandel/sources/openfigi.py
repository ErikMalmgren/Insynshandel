"""OpenFIGI — ISIN → exchange ticker.

Free, 25 requests/minute unauthenticated (2.4 s spacing, 10 jobs per request);
set ``OPENFIGI_API_KEY`` to raise it (100 jobs per request). Queried for ISINs
with no current ``figi_lookup`` answer.

The job is not filtered by MIC: a ``micCode: XSTO`` filter only ever finds Nasdaq
Stockholm main-market listings, so every First North, Spotlight and NGM company
came back empty. Instead we take all equity listings and pick the Swedish home
venue ourselves (:data:`HOME_EXCH`).

``display_ticker`` puts the share class back on OpenFIGI's ticker (SDB handling,
A/B share classes): ``'TELE2B'`` → ``'TELE2-B'``, the form the site shows.
``format_ticker`` adds Yahoo's ``.ST`` to that; it is the Yahoo provider's
fallback when Yahoo's own ISIN search has no answer.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass

import requests

from .. import config

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"

# Swedish home venues, in preference order: OpenFIGI exchCode -> MIC. A share
# listed nowhere here (delisted, foreign, pre-split ISIN) has no usable answer —
# the X1/EO/GR… composite and cross-listings it still maps to are not its home.
HOME_EXCH: dict[str, str] = {
    "SS": "XSTO",  # Nasdaq Stockholm
    "SF": "FNSE",  # First North Sweden
    "KA": "XSAT",  # Spotlight Stock Market
    "NG": "XNGM",  # Nordic Growth Market, incl. Nordic SME
}

_UNAUTH_SPACING_S = 2.4
_AUTH_SPACING_S = 0.3
_UNAUTH_BATCH = 10
_AUTH_BATCH = 100


@dataclass(frozen=True, slots=True)
class FigiResult:
    isin: str
    ticker: str | None       # formatted (Yahoo style); None = no usable answer
    raw_ticker: str | None   # OpenFIGI verbatim, pre-formatting
    name: str | None
    exch_code: str | None
    mic_code: str | None
    error: str | None = None  # OpenFIGI's per-job error, e.g. an invalid ISIN


def pick_home(hits: Sequence[dict]) -> dict | None:
    """The listing on the most preferred :data:`HOME_EXCH` venue, equities only
    (a bond ISIN answers with a 'SWEDA 11 04/26/19'-style ticker)."""
    equity = [h for h in hits if h.get("marketSector") == "Equity" and h.get("ticker")]
    for exch in HOME_EXCH:
        for h in equity:
            if h.get("exchCode") == exch:
                return h
    return None


def _result(isin: str, answer: dict) -> FigiResult:
    hit = pick_home(answer.get("data") or [])
    if hit is None:
        return FigiResult(isin, None, None, None, None, None, answer.get("error"))
    raw = hit["ticker"].strip()
    name = hit.get("name")
    return FigiResult(
        isin=isin,
        ticker=format_ticker(raw, name or ""),
        raw_ticker=raw,
        name=name,
        exch_code=hit["exchCode"],
        mic_code=HOME_EXCH[hit["exchCode"]],
    )


# OpenFIGI's Stockholm tickers carry no class separator ('SAABB', 'HMB'); its name
# does: 'SAAB AB-B', 'HENNES & MAURITZ AB-B SHS', 'SSAB AB - B SHARES',
# 'INVESTOR AB SER. B', 'FASTIGHETS AB BALDER-B SHRS', 'VITEC SOFTWARE GROUP AB-B SH'.
_CLASS_IN_NAME = re.compile(
    r"(?:-\s*|\b(?:SER|SERIES|CLASS)\.?\s*)([A-D])"
    r"(?:\s+(?:SH|SHS|SHR|SHRS|SHARE|SHARES))?$"
)


def display_ticker(raw: str, name: str) -> str:
    """`'SAABB'` + `'SAAB AB-B'` → `'SAAB-B'`. The class, dash-separated.

    A class suffix is split off only when ``name`` (OpenFIGI's) names the class.
    Guessing from a trailing A–D turned TELIA into TELI-A and SAND into SAN-D.
    """
    raw = (raw or "").strip().upper()
    name = (name or "").strip().upper()

    # 1) SDB depository shares: 'ALIV SDB' -> 'ALIV-SDB'
    if " SDB" in raw or " SDB" in name or raw.endswith("SDB"):
        s = raw.replace(" SDB", "-SDB").replace(" ", "-")
        if s.endswith("SDB") and not s.endswith("-SDB"):
            s = s[:-3] + "-SDB"
        return s

    # 2) an existing space is a share-class separator: 'INVE B' -> 'INVE-B'
    if " " in raw:
        return raw.replace(" ", "-")

    # 3) the name's class: 'SAAB AB-B' / 'NCC AB SER. B' -> split the matching letter
    m = _CLASS_IN_NAME.search(name)
    if m and len(raw) > 1 and raw.endswith(m.group(1)):
        return raw[:-1] + "-" + raw[-1]

    return raw


def format_ticker(raw: str, name: str) -> str:
    """`'SAABB'` + `'SAAB AB-B'` → `'SAAB-B.ST'`. Yahoo/Stockholm."""
    return display_ticker(raw, name) + ".ST"


class OpenFIGIClient:
    def __init__(self, *, api_key: str | None = None,
                 session: requests.Session | None = None) -> None:
        self.api_key = api_key or os.environ.get("OPENFIGI_API_KEY")
        self.session = session or requests.Session()
        self.session.headers.update({"Content-Type": "application/json",
                                     "User-Agent": config.USER_AGENT})
        if self.api_key:
            self.session.headers["X-OPENFIGI-APIKEY"] = self.api_key
        self.spacing_s = _AUTH_SPACING_S if self.api_key else _UNAUTH_SPACING_S
        self.batch_size = _AUTH_BATCH if self.api_key else _UNAUTH_BATCH
        self._last_at: float | None = None

    def _space(self) -> None:
        if self._last_at is not None:
            wait = self.spacing_s - (time.monotonic() - self._last_at)
            if wait > 0:
                time.sleep(wait)

    def map_isins(self, isins: Sequence[str]) -> list[FigiResult]:
        """Resolve up to :attr:`batch_size` ISINs in one request, answers in input
        order. A network failure raises for the whole batch; 'no match' is a
        FigiResult with ``ticker=None`` (a cacheable negative)."""
        if len(isins) > self.batch_size:
            raise ValueError(f"{len(isins)} ISINs exceeds batch size {self.batch_size}")
        self._space()
        body = [{"idType": "ID_ISIN", "idValue": i, "marketSecDes": "Equity"}
                for i in isins]
        resp = self.session.post(OPENFIGI_URL, json=body,
                                 timeout=config.REQUEST_TIMEOUT_S)
        self._last_at = time.monotonic()
        if resp.status_code == 429:
            raise requests.HTTPError("OpenFIGI 429 — slow down")
        resp.raise_for_status()

        answers = resp.json() or []
        # answers are positional; a short list would misattribute every ISIN after it
        if len(answers) != len(isins):
            raise ValueError(f"OpenFIGI answered {len(answers)} jobs for {len(isins)}")
        return [_result(i, a) for i, a in zip(isins, answers, strict=True)]
