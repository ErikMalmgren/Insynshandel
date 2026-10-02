"""Riksbank SWEA — daily SEK exchange rates.

Free, no key. One call per currency and range; a decade-wide backfill range
returns ~100 KB. Day to day, :func:`insynshandel.pipeline.reference.fx` asks
only for the currencies whose rows have moved past the stored rates, usually
none. Gaps on weekends and Swedish holidays are stored as-is; forward-fill
happens at read time in :mod:`insynshandel.pipeline.fx`.
"""

from __future__ import annotations

import os
import time
from datetime import date

import requests

from .. import config

SWEA_URL = "https://api.riksbank.se/swea/v1/Observations"

# currency -> SWEA series id. `value` is SEK per 1 unit of the currency.
SERIES: dict[str, str] = {
    "USD": "SEKUSDPMI",
    "EUR": "SEKEURPMI",
    "GBP": "SEKGBPPMI",
    "CAD": "SEKCADPMI",
    "CHF": "SEKCHFPMI",
    "NOK": "SEKNOKPMI",
    "DKK": "SEKDKKPMI",
    "RUB": "SEKRUBPMI",
}

# ~3 requests/minute before a 429. Only paid between calls in one run — a
# backfill (one call per currency) or the rare build that needs several.
RIKSBANK_SPACING_S = float(os.environ.get("INSYN_RIKSBANK_SPACING_S", "25"))


class RiksbankError(RuntimeError):
    pass


class RiksbankClient:
    def __init__(self, *, session: requests.Session | None = None,
                 spacing_s: float = RIKSBANK_SPACING_S) -> None:
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": config.USER_AGENT}
        )
        self.spacing_s = spacing_s
        self._last_at: float | None = None

    def _space(self) -> None:
        if self._last_at is not None:
            wait = self.spacing_s - (time.monotonic() - self._last_at)
            if wait > 0:
                time.sleep(wait)

    def observations(
        self, currency: str, start: date, end: date
    ) -> list[tuple[str, float]]:
        """`[(YYYY-MM-DD, sek_per_unit), ...]` for business days in the range."""
        series = SERIES.get(currency)
        if series is None:
            raise RiksbankError(f"no SWEA series for {currency!r}")

        for attempt in range(config.HTTP_MAX_RETRIES + 1):
            self._space()
            resp = self.session.get(
                f"{SWEA_URL}/{series}/{start.isoformat()}/{end.isoformat()}",
                timeout=config.REQUEST_TIMEOUT_S,
            )
            self._last_at = time.monotonic()
            if resp.status_code == 429:
                if attempt == config.HTTP_MAX_RETRIES:
                    raise RiksbankError(f"{currency}: rate-limited after retries")
                time.sleep(30)
                continue
            if resp.status_code == 204:
                # a range with no business day yet (weekend, before ~16:15
                # publication, or a discontinued series such as RUB)
                return []
            if resp.status_code != 200:
                raise RiksbankError(f"{currency}: HTTP {resp.status_code}")
            data = resp.json()
            return [(row["date"], float(row["value"])) for row in data]
        raise RiksbankError(f"{currency}: exhausted retries")
