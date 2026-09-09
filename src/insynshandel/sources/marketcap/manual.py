"""ManualProvider — the committed seed-file escape hatch (§7.4.1).

Reads ``data/seed/market_cap_manual.csv``: one row per company no automated
source can price.
"""

from __future__ import annotations

import csv

from ... import config
from .base import Company, FetchFailure, FetchProgress, MarketCapQuote

_MIN_AS_OF = "2000-01-01"


class ManualProvider:
    name = "manual"

    def __init__(self) -> None:
        self._rows: dict[str, dict[str, str]] = {}
        path = config.SEED_DIR / "market_cap_manual.csv"
        if path.exists():
            lines = [
                ln for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")
            ]
            for row in csv.DictReader(lines):
                if row.get("lei"):
                    self._rows[row["lei"].strip()] = row

    def symbol_for(self, company: Company) -> str | None:
        return company.lei if company.lei in self._rows else None

    def fetch(
        self, companies: list[Company], *, progress: FetchProgress | None = None
    ) -> tuple[list[MarketCapQuote], list[FetchFailure]]:
        quotes: list[MarketCapQuote] = []
        failures: list[FetchFailure] = []
        addressable = sum(1 for c in companies if self.symbol_for(c) is not None)
        done = 0
        for c in companies:
            row = self._rows.get(c.lei)
            if row is None:
                continue
            if progress and addressable:
                done += 1
                progress(done, addressable)
            try:
                as_of = row["as_of"].strip()
                # a future as_of would become the permanent MAX(as_of) 'current'
                # snapshot that no later real fetch could displace (§7.1).
                if not (_MIN_AS_OF <= as_of <= config.today().isoformat()):
                    failures.append(FetchFailure(
                        c.lei, c.lei, f"as_of {as_of!r} is out of range"))
                    continue
                quotes.append(MarketCapQuote(
                    lei=c.lei,
                    market_cap=float(row["market_cap"]),
                    currency=row["currency"].strip(),
                    as_of=as_of,
                    source=self.name,
                ))
            except (KeyError, ValueError) as exc:
                failures.append(FetchFailure(c.lei, c.lei, f"bad seed row: {exc}"))
        return quotes, failures
