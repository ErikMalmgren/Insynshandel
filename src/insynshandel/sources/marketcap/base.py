"""The provider seam.

`yfinance` is unofficial scraping — treat it as one implementation, never the
interface. A provider's `fetch()` **never raises**; partial success is normal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ... import config


@dataclass(frozen=True, slots=True)
class Company:
    """Provider-neutral identity. Each provider derives its own symbol."""

    lei: str
    display_name: str
    primary_isin: str | None
    raw_ticker: str | None       # OpenFIGI verbatim, e.g. 'INVE B'
    mic_code: str | None
    exch_code: str | None


@dataclass(frozen=True, slots=True)
class MarketCapQuote:
    lei: str
    market_cap: float            # in `currency`, NOT converted
    currency: str                # read from the provider, never assumed
    as_of: str                   # YYYY-MM-DD
    source: str


@dataclass(frozen=True, slots=True)
class FetchFailure:
    lei: str
    symbol: str | None
    reason: str


# (done, total) over the companies this provider can actually address — never
# over every company: most of `company` has no symbol for a given provider, so a
# total of len(companies) would jump to ~75% instantly and then crawl. Not called
# at all when nothing is addressable (0/0 has no ETA).
FetchProgress = Callable[[int, int], None]


@runtime_checkable
class MarketCapProvider(Protocol):
    name: str

    def symbol_for(self, company: Company) -> str | None: ...

    def fetch(
        self, companies: list[Company], *, progress: FetchProgress | None = None
    ) -> tuple[list[MarketCapQuote], list[FetchFailure]]: ...


def get_providers(names: list[str] | None = None) -> list[MarketCapProvider]:
    """Instantiate the configured providers, in order (`config.MARKETCAP_PROVIDERS`)."""
    from .manual import ManualProvider
    from .yahoo import YahooProvider

    registry = {"yahoo": YahooProvider, "manual": ManualProvider}
    out: list[MarketCapProvider] = []
    for name in names or config.MARKETCAP_PROVIDERS:
        try:
            out.append(registry[name]())
        except KeyError:
            raise ValueError(f"unknown market-cap provider {name!r}") from None
    return out
