"""Phase 2 — rebuild ``transaction_norm`` from ``raw_live`` (§5).

A full rebuild every run: ``transaction_norm`` is a pure function of
``raw_live`` + parsing rules and holds no state of its own (§5.0). The derived
columns (``sign`` … ``gross_value_sek``) are left ``NULL`` — ``insyn aggregate``
writes those, and it must run next (invariant 6).
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from ..db import immediate
from ..sources import fi

# transaction_norm column  ->  raw field name (or a callable in _PARSE below)
_TEXT_MAP: dict[str, str] = {
    "issuer_name": "emittent",
    "lei": "lei_kod",
    "notifier": "anmalningsskyldig",
    "pdmr": "person_i_ledande_stallning",
    "position": "befattning",
    "correction_note": "beskrivning_av_korrigering",
    "nature": "karaktar",
    "instrument_type": "instrumenttyp",
    "instrument_name": "instrumentnamn",
    "isin": "isin",
    "volume_unit": "volymsenhet",
    "currency": "valuta",
    "venue": "handelsplats",
    "status": "status",
}
_BOOL_MAP: dict[str, str] = {
    "is_closely_assoc": "narstaende",
    "is_correction": "korrigering",
    "is_initial_report": "ar_forstagangsrapportering",
    "is_share_program": "ar_kopplad_till_aktieprogram",
}
# Required: verified always present, even in 2016 (measured 2026-09-08). Stored
# as NULL when blank so doctor catches a regression. NOTE: `lei` is deliberately
# NOT here — §5.3 lists it, but 78.7% of 2016-07 rows and 61.6% of 2017-01 rows
# carry no LEI. It is carried through verbatim ('' when absent); phase 3 owns the
# fallback-key decision (see CLAUDE.md gotchas, invariants 9/12).
_REQUIRED = ("published_date", "transaction_date", "nature", "status")

_NORM_COLUMNS = (
    "raw_id", "published_at", "published_date", "transaction_date",
    *_TEXT_MAP.keys(), *_BOOL_MAP.keys(), "volume", "price",
)
_INSERT_SQL = (
    f"INSERT INTO transaction_norm ({', '.join(_NORM_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _NORM_COLUMNS)})"
)


@dataclass
class NormalizeSummary:
    rows_in: int = 0
    rows_out: int = 0
    unparseable_numbers: int = 0
    odd_booleans: Counter = field(default_factory=Counter)
    distinct_volume_unit: Counter = field(default_factory=Counter)
    distinct_currency: Counter = field(default_factory=Counter)
    distinct_status: Counter = field(default_factory=Counter)
    missing_required: Counter = field(default_factory=Counter)
    rows_without_lei: int = 0            # measured statistic, not an error (§ inv 9/12)
    rows_without_lei_or_isin: int = 0

    @property
    def ok(self) -> bool:
        return self.rows_in == self.rows_out and not self.missing_required


def parse_number(s: str) -> float | None:
    """`'2 600 000,0'` → 2600000.0. Returns None on failure (§5.2), never raises."""
    s = s.replace("\xa0", "").replace(" ", "").replace(",", ".").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _bool(raw: str, col: str, summary: NormalizeSummary) -> int:
    v = raw.strip()
    if v == "Ja":
        return 1
    if v == "":
        return 0
    summary.odd_booleans[f"{col}={v!r}"] += 1
    return 0


def normalize(conn: sqlite3.Connection) -> NormalizeSummary:
    summary = NormalizeSummary()
    src = conn.execute(
        "SELECT id, " + ", ".join(fi.FIELD_NAMES) + " FROM raw_live ORDER BY id"
    ).fetchall()
    summary.rows_in = len(src)

    batch: list[tuple] = []
    for row in src:
        f = {name: (row[name] or "").strip() for name in fi.FIELD_NAMES}

        published_at = f["publiceringsdatum"]
        published_date = published_at.split(" ", 1)[0]
        transaction_date = f["transaktionsdatum"].split(" ", 1)[0]
        volume = parse_number(f["volym"])
        price = parse_number(f["pris"])
        if f["volym"] and volume is None:
            summary.unparseable_numbers += 1
        if f["pris"] and price is None:
            summary.unparseable_numbers += 1

        values = {
            "raw_id": row["id"],
            "published_at": published_at or None,
            "published_date": published_date or None,
            "transaction_date": transaction_date or None,
            **{col: (f[raw] or None) if col in _REQUIRED else f[raw]
               for col, raw in _TEXT_MAP.items()},
            **{col: _bool(f[raw], col, summary) for col, raw in _BOOL_MAP.items()},
            "volume": volume,
            "price": price,
        }

        for col in _REQUIRED:
            if not values.get(col):
                summary.missing_required[col] += 1

        if not f["lei_kod"]:
            summary.rows_without_lei += 1
            if not f["isin"]:
                summary.rows_without_lei_or_isin += 1

        summary.distinct_volume_unit[f["volymsenhet"]] += 1
        summary.distinct_currency[f["valuta"]] += 1
        summary.distinct_status[f["status"]] += 1

        batch.append(tuple(values[c] for c in _NORM_COLUMNS))

    with immediate(conn):
        conn.execute("DELETE FROM transaction_norm")
        conn.executemany(_INSERT_SQL, batch)

    summary.rows_out = conn.execute("SELECT COUNT(*) FROM transaction_norm").fetchone()[0]
    return summary
