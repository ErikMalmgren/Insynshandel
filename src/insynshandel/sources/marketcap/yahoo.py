"""YahooProvider — `yfinance`, the default.

`fetch()` never raises. Uses a `ThreadPoolExecutor(max_workers=5)` and the
404/429/timeout error taxonomy.

Symbol, first match wins: a `ticker_override` row; Yahoo's own answer for the
ISIN (:func:`search_symbol`, cached in `yahoo_symbol` by `refdata`); else
:func:`format_ticker` on OpenFIGI's ticker and name.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from ... import config
from ..openfigi import format_ticker
from .base import Company, FetchFailure, FetchProgress, MarketCapQuote

log = logging.getLogger(__name__)

# OpenFIGI home venues Yahoo quotes as '.ST'. NGM / Nordic SME ('NG') are not on
# Yahoo at all — their ISIN search comes back empty.
YAHOO_EXCH = ("SS", "SF", "KA")


def search_symbol(isin: str) -> str | None:
    """Yahoo's own symbol for an ISIN (`'SE0021921269'` → `'SAAB-B.ST'`), or None.
    A transport failure raises."""
    import yfinance as yf

    found = yf.Search(
        isin, max_results=5, news_count=0, lists_count=0, recommended=0,
        timeout=config.REQUEST_TIMEOUT_S,
    )
    # yfinance swallows a non-JSON answer into {} (hide_exceptions defaults on);
    # a real "no match" still carries an empty 'quotes'. Raise, so a throttled
    # reply is retried instead of cached as a 90-day negative.
    if "quotes" not in found.response:
        raise RuntimeError(f"Yahoo search for {isin}: answer has no 'quotes'")
    for q in found.quotes:
        symbol = q.get("symbol") or ""
        if symbol.endswith(".ST"):
            return symbol
    return None


class YahooProvider:
    name = "yahoo"

    def symbol_for(self, company: Company) -> str | None:
        # Called repeatedly (addressable counts, progress) — no network here.
        manual = company.override_for(self.name)
        if manual is not None:
            return manual or None
        if company.symbols.get(self.name):
            return company.symbols[self.name]
        if not company.raw_ticker or company.exch_code not in YAHOO_EXCH:
            return None
        return format_ticker(company.raw_ticker, company.figi_name or "")

    def _one(self, company: Company) -> tuple[MarketCapQuote | None, FetchFailure | None]:
        import yfinance as yf

        symbol = self.symbol_for(company)
        if not symbol:
            return None, FetchFailure(company.lei, None, "no symbol")
        try:
            # yfinance 1.7 FastInfo keys are camelCase: 'marketCap', 'currency'
            # (verified 2026-09-08 against ERIC-B.ST). '.get' exists on FastInfo.
            fast = yf.Ticker(symbol).fast_info
            cap = fast.get("marketCap")
            currency = fast.get("currency") or "SEK"
            if not cap:
                return None, FetchFailure(company.lei, symbol, "no marketCap in response")
            return (
                MarketCapQuote(
                    lei=company.lei, market_cap=float(cap), currency=str(currency),
                    as_of=config.today().isoformat(), source=self.name,
                ),
                None,
            )
        except Exception as exc:  # noqa: BLE001 — never raise out of fetch()
            msg = str(exc)
            if "404" in msg or "Not Found" in msg:
                reason = f"404 — {symbol} not found"
            elif "429" in msg or "Too Many Requests" in msg:
                reason = "429 — rate limited"
            elif "timeout" in msg.lower() or "connection" in msg.lower():
                reason = f"connection: {msg[:120]}"
            else:
                reason = f"{type(exc).__name__}: {msg[:120]}"
            return None, FetchFailure(company.lei, symbol, reason)

    def fetch(
        self, companies: list[Company], *, progress: FetchProgress | None = None
    ) -> tuple[list[MarketCapQuote], list[FetchFailure]]:
        quotes: list[MarketCapQuote] = []
        failures: list[FetchFailure] = []
        addressable = sum(1 for c in companies if self.symbol_for(c) is not None)
        done = 0
        with ThreadPoolExecutor(max_workers=5) as pool:
            # ordered map, not as_completed: a wedged company freezes the counter,
            # which is exactly the signal yfinance hangs need.
            for company, (quote, failure) in zip(
                companies, pool.map(self._one, companies), strict=True
            ):
                if quote is not None:
                    quotes.append(quote)
                if failure is not None:
                    failures.append(failure)
                if progress and addressable and self.symbol_for(company) is not None:
                    done += 1
                    progress(done, addressable)
        return quotes, failures
