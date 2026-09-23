"""YahooProvider — `yfinance`, the default.

`fetch()` never raises. Uses a `ThreadPoolExecutor(max_workers=5)` and the
404/429/timeout error taxonomy.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from ... import config
from ..openfigi import format_ticker
from .base import Company, FetchFailure, FetchProgress, MarketCapQuote

log = logging.getLogger(__name__)


class YahooProvider:
    name = "yahoo"

    def symbol_for(self, company: Company) -> str | None:
        if not company.raw_ticker:
            return None
        # Yahoo/Stockholm string shaping lives here, not in shared code.
        return format_ticker(company.raw_ticker, company.display_name or "")

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
