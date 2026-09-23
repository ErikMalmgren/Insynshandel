"""OpenFIGI — ISIN → exchange ticker.

Free, 25 requests/minute unauthenticated (2.4 s spacing); set ``OPENFIGI_API_KEY``
to raise it. Only ever queried for ISINs with no ``figi_lookup`` row.

``format_ticker`` is Yahoo-specific string shaping (``.ST`` suffix, SDB handling, A/B share classes).
It lives here for now; when a second provider lands it moves into ``yahoo.py``.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import requests

from .. import config

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
PREFERRED_MIC = "XSTO"  # Nasdaq Stockholm

# Tickers that legitimately end in A/B/C/D and must NOT be split into `-X` forms.
EXCLUDE_TICKERS = {"ABB", "ALFA", "ACA", "ACSA", "DIOS"}

_UNAUTH_SPACING_S = 2.4
_AUTH_SPACING_S = 0.3


@dataclass(frozen=True, slots=True)
class FigiResult:
    isin: str
    ticker: str | None       # formatted (Yahoo style); None = no usable answer
    raw_ticker: str | None   # OpenFIGI verbatim, pre-formatting
    name: str | None
    exch_code: str | None
    mic_code: str | None


def format_ticker(raw: str, name: str) -> str:
    """`'INVE B'` + `'INVESTOR AB SER. B'` → `'INVE-B.ST'`. Yahoo/Stockholm."""
    raw = (raw or "").strip().upper()
    name = (name or "").upper()

    # 1) SDB depository shares: 'ALIV SDB' -> 'ALIV-SDB.ST'
    if " SDB" in raw or " SDB" in name or raw.endswith("SDB"):
        s = raw.replace(" SDB", "-SDB").replace(" ", "-")
        if s.endswith("SDB") and not s.endswith("-SDB"):
            s = s[:-3] + "-SDB"
        return s + ".ST"

    # 2) an existing space is a share-class separator: 'INVE B' -> 'INVE-B.ST'
    if " " in raw:
        return raw.replace(" ", "-") + ".ST"

    # 3) derive the '-' from the name's 'ser. B' / 'class B'
    m = re.search(r"\b(?:SER|SERIES|CLASS)\.?\s*([A-D])\b", name, re.IGNORECASE)
    if m and raw.endswith(m.group(1)):
        return raw[:-1] + "-" + raw[-1] + ".ST"

    # 4) cautious heuristic: raw ends A/B/C/D, no '-' yet, not an excluded ticker
    if (
        raw.endswith(tuple("ABCD"))
        and raw not in EXCLUDE_TICKERS
        and len(raw) >= 4
        and raw[-2] != raw[-1]
    ):
        return raw[:-1] + "-" + raw[-1] + ".ST"

    return raw + ".ST"


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
        self._last_at: float | None = None

    def _space(self) -> None:
        if self._last_at is not None:
            wait = self.spacing_s - (time.monotonic() - self._last_at)
            if wait > 0:
                time.sleep(wait)

    def map_isin(self, isin: str) -> FigiResult:
        """Resolve one ISIN. A network failure raises; 'no match' returns a
        FigiResult with ``ticker=None`` (a cacheable negative)."""
        self._space()
        body = [{"idType": "ID_ISIN", "idValue": isin, "micCode": PREFERRED_MIC}]
        resp = self.session.post(OPENFIGI_URL, json=body,
                                 timeout=config.REQUEST_TIMEOUT_S)
        self._last_at = time.monotonic()
        if resp.status_code == 429:
            raise requests.HTTPError("OpenFIGI 429 — slow down")
        resp.raise_for_status()

        hits = (resp.json() or [{}])[0].get("data") or []
        if not hits:
            return FigiResult(isin, None, None, None, None, None)
        h = hits[0]
        raw = (h.get("ticker") or "").strip()
        name = h.get("name")
        return FigiResult(
            isin=isin,
            ticker=format_ticker(raw, name or "") if raw else None,
            raw_ticker=raw or None,
            name=name,
            exch_code=h.get("exchCode"),
            mic_code=h.get("micCode") or PREFERRED_MIC,
        )
