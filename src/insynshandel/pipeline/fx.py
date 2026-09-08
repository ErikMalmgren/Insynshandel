"""Forward-filled FX lookup (§6.3.1).

`fx_rate` holds business-day rates only. A transaction on a weekend or holiday
converts at the **most recent earlier** business day's rate — never interpolated,
never skipped. Load once, bisect in Python (a per-row SQL query over 180k rows
is the one place this phase would get slow).
"""

from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FxHit:
    sek_per_unit: float
    rate_date: str          # the business day the rate was taken from


class FxTable:
    def __init__(self, by_currency: dict[str, list[tuple[str, float]]]) -> None:
        # each list sorted ascending by date
        self._c = {k: (
            [d for d, _ in v], [r for _, r in v]
        ) for k, v in by_currency.items()}

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> FxTable:
        rows = conn.execute(
            "SELECT currency, rate_date, sek_per_unit FROM fx_rate "
            "ORDER BY currency, rate_date"
        ).fetchall()
        by: dict[str, list[tuple[str, float]]] = {}
        for r in rows:
            by.setdefault(r["currency"], []).append((r["rate_date"], r["sek_per_unit"]))
        return cls(by)

    def currencies(self) -> set[str]:
        return set(self._c) | {"SEK"}

    def rate(self, currency: str, on: str) -> FxHit | None:
        """Rate for ``currency`` on transaction date ``on`` (YYYY-MM-DD).

        SEK is identity. Otherwise the latest rate with ``rate_date <= on``.
        ``None`` when the currency has no series or ``on`` predates it.
        """
        if currency == "SEK":
            return FxHit(1.0, on)
        entry = self._c.get(currency)
        if not entry:
            return None
        dates, rates = entry
        i = bisect.bisect_right(dates, on) - 1
        if i < 0:
            return None
        return FxHit(rates[i], dates[i])
