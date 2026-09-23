"""Finansinspektionen insider-register CSV export: fetch, parse, bisect.

Two concerns, deliberately separable for testing:

* pure parsing — :func:`parse_csv`, :func:`row_hash`, :func:`assign_ordinals`,
  :func:`iter_seed_windows`, :func:`iter_leaf_windows` — no network.
* HTTP — :class:`FIClient` — politeness spacing, retry/backoff, a per-run
  request budget.

The verified source facts (encoding, delimiter, field count, row cap) are in
:mod:`insynshandel.config`. The parser trusts none of them silently: a wrong
field count or a changed header raises.
"""

from __future__ import annotations

import csv
import hashlib
import io
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from .. import config

# ── Column contract ─────────────────────────────────────────────────────────
# DB column name  ->  exact CSV header label, in file order. 22 real columns;
# the export appends a 23rd empty field (trailing ';').
COLUMNS: tuple[tuple[str, str], ...] = (
    ("publiceringsdatum", "Publiceringsdatum"),
    ("emittent", "Emittent"),
    ("lei_kod", "LEI-kod"),
    ("anmalningsskyldig", "Anmälningsskyldig"),
    ("person_i_ledande_stallning", "Person i ledande ställning"),
    ("befattning", "Befattning"),
    ("narstaende", "Närstående"),
    ("korrigering", "Korrigering"),
    ("beskrivning_av_korrigering", "Beskrivning av korrigering"),
    ("ar_forstagangsrapportering", "Är förstagångsrapportering"),
    ("ar_kopplad_till_aktieprogram", "Är kopplad till aktieprogram"),
    ("karaktar", "Karaktär"),
    ("instrumenttyp", "Instrumenttyp"),
    ("instrumentnamn", "Instrumentnamn"),
    ("isin", "ISIN"),
    ("transaktionsdatum", "Transaktionsdatum"),
    ("volym", "Volym"),
    ("volymsenhet", "Volymsenhet"),
    ("pris", "Pris"),
    ("valuta", "Valuta"),
    ("handelsplats", "Handelsplats"),
    ("status", "Status"),
)
FIELD_NAMES: tuple[str, ...] = tuple(name for name, _ in COLUMNS)
EXPECTED_HEADER: tuple[str, ...] = tuple(label for _, label in COLUMNS)
N_REAL_FIELDS = len(COLUMNS)  # 22

_HASH_SEP = "\x1f"
ONE_DAY = timedelta(days=1)


class FormatChangedError(RuntimeError):
    """The export no longer matches the verified column contract. Fail loud."""


class RequestBudgetExceeded(RuntimeError):
    """A single run hit ``config.MAX_REQUESTS_PER_RUN``. Resume later."""


@dataclass(frozen=True, slots=True)
class ParsedRow:
    row_hash: str
    ordinal: int
    fields: dict[str, str]  # FIELD_NAMES -> verbatim value

    @property
    def pub_date(self) -> str:
        return self.fields["publiceringsdatum"][:10]


@dataclass(frozen=True, slots=True)
class Leaf:
    """A window that did not cap (or a single capped day we store anyway)."""

    window_from: date
    window_to: date
    rows: list[ParsedRow]
    truncated: bool


# ── Pure parsing ────────────────────────────────────────────────────────────
def row_hash(values: list[str]) -> str:
    """sha256 hex of the 22 verbatim fields joined by ``\\x1f``.

    Covers ``status`` too, so a ``Aktuell -> Reviderad`` flip surfaces as
    supersede + insert rather than a silent in-place edit. Changing this
    function after a backfill re-supersedes every row — pinned by a test.
    """
    if len(values) != N_REAL_FIELDS:
        raise ValueError(f"row_hash needs {N_REAL_FIELDS} fields, got {len(values)}")
    return hashlib.sha256(_HASH_SEP.join(values).encode("utf-8")).hexdigest()


def parse_csv(data: bytes) -> list[list[str]]:
    """Decode the UTF-16LE export and return one 22-field list per data row.

    Uses ``csv.reader`` (RFC 4180) — never ``line.split(';')``.
    Raises :class:`FormatChangedError` on a bad header or any row whose field
    count is not exactly ``config.FI_FIELD_COUNT`` (23).
    """
    text = data.decode(config.FI_ENCODING)
    reader = csv.reader(io.StringIO(text), delimiter=config.FI_DELIMITER)
    records = list(reader)
    if not records:
        raise FormatChangedError("empty response — no header row")

    header = records[0]
    if len(header) != config.FI_FIELD_COUNT or tuple(header[:N_REAL_FIELDS]) != EXPECTED_HEADER:
        raise FormatChangedError(f"unexpected header: {header!r}")

    out: list[list[str]] = []
    for i, rec in enumerate(records[1:], start=2):
        if len(rec) != config.FI_FIELD_COUNT:
            raise FormatChangedError(
                f"row {i}: expected {config.FI_FIELD_COUNT} fields, got {len(rec)}: {rec!r}"
            )
        out.append(rec[:N_REAL_FIELDS])
    return out


def assign_ordinals(field_rows: list[list[str]]) -> list[ParsedRow]:
    """Hash each row and number exact duplicates 0, 1, 2… in returned order.

    The source genuinely repeats some rows byte-for-byte; ``ordinal``
    preserves that multiplicity instead of collapsing it.
    """
    seen: dict[str, int] = {}
    parsed: list[ParsedRow] = []
    for values in field_rows:
        h = row_hash(values)
        ordinal = seen.get(h, 0)
        seen[h] = ordinal + 1
        parsed.append(ParsedRow(h, ordinal, dict(zip(FIELD_NAMES, values, strict=True))))
    return parsed


def parse_export(data: bytes) -> list[ParsedRow]:
    return assign_ordinals(parse_csv(data))


def iter_seed_windows(
    start: date, end: date, size_days: int = config.SEED_WINDOW_DAYS
) -> Iterator[tuple[date, date]]:
    """Contiguous non-overlapping ``[from, to]`` windows covering ``[start, end]``."""
    if start > end:
        return
    cur = start
    step = timedelta(days=size_days - 1)
    while cur <= end:
        yield cur, min(cur + step, end)
        cur = min(cur + step, end) + ONE_DAY


def iter_leaf_windows(
    window_from: date,
    window_to: date,
    get_rows: Callable[[date, date], list[ParsedRow]],
) -> Iterator[Leaf]:
    """Bisect on the 1000-row cap; yield only leaf windows.

    ``get_rows`` performs one fetch. A capped window's rows are never yielded —
    only its non-capped halves are. A single day that still caps is yielded with
    ``truncated=True`` and must be insert-only downstream.
    """
    rows = get_rows(window_from, window_to)
    if len(rows) < config.FI_ROW_CAP:
        yield Leaf(window_from, window_to, rows, truncated=False)
        return
    if window_from >= window_to:
        yield Leaf(window_from, window_to, rows, truncated=True)
        return
    span = (window_to - window_from).days
    mid = window_from + timedelta(days=span // 2)
    yield from iter_leaf_windows(window_from, mid, get_rows)
    yield from iter_leaf_windows(mid + ONE_DAY, window_to, get_rows)


# ── HTTP ────────────────────────────────────────────────────────────────────
class FIClient:
    """Polite client for the export endpoint: 3 s spacing, backoff, run budget."""

    def __init__(
        self,
        *,
        spacing_s: float | None = None,
        timeout_s: float | None = None,
        max_requests: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.spacing_s = config.REQUEST_SPACING_S if spacing_s is None else spacing_s
        self.timeout_s = config.REQUEST_TIMEOUT_S if timeout_s is None else timeout_s
        self.max_requests = (
            config.MAX_REQUESTS_PER_RUN if max_requests is None else max_requests
        )
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = config.USER_AGENT
        self.request_count = 0
        self._last_request_at: float | None = None

    def _sleep_for_spacing(self) -> None:
        if self._last_request_at is None:
            return
        wait = self.spacing_s - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)

    def fetch_raw(self, window_from: date, window_to: date) -> bytes:
        """One GET for a publication-date window. Retries 5xx / connection resets."""
        if self.request_count >= self.max_requests:
            raise RequestBudgetExceeded(
                f"hit MAX_REQUESTS_PER_RUN={self.max_requests}; resume in a later run"
            )
        params = {
            "SearchFunctionType": "Insyn",
            "Utgivare": "",
            "PersonILedandeStallningNamn": "",
            "Transaktionsdatum.From": "",
            "Transaktionsdatum.To": "",
            "Publiceringsdatum.From": window_from.isoformat(),
            "Publiceringsdatum.To": window_to.isoformat(),
            "button": "export",
            "Page": "1",
        }
        backoff = 3.0
        last_exc: Exception | None = None
        for attempt in range(config.HTTP_MAX_RETRIES + 1):
            self._sleep_for_spacing()
            try:
                resp = self.session.get(
                    config.FI_SEARCH_URL, params=params, timeout=self.timeout_s
                )
                self.request_count += 1
                self._last_request_at = time.monotonic()
                if resp.status_code >= 500:
                    last_exc = requests.HTTPError(f"{resp.status_code} from FI")
                elif resp.status_code != 200:
                    resp.raise_for_status()
                else:
                    ctype = resp.headers.get("Content-Type", "")
                    if "csv" not in ctype:
                        raise FormatChangedError(f"expected text/csv, got {ctype!r}")
                    return resp.content
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                self._last_request_at = time.monotonic()
            if attempt < config.HTTP_MAX_RETRIES:
                time.sleep(backoff)
                backoff *= 2
        raise RuntimeError(f"FI fetch {window_from}..{window_to} failed") from last_exc

    def fetch_window(self, window_from: date, window_to: date) -> list[ParsedRow]:
        return parse_export(self.fetch_raw(window_from, window_to))
