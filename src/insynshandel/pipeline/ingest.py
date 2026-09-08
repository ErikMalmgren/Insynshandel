"""Phase 1 — fetch a publication-date range and *diff* it into ``raw_transaction``.

One code path for every mode (§4.3). ``backfill``, ``recent`` and ``gaps`` differ
only in the date range and whether already-covered seed windows are skipped.

The write path is a diff (§4.5): for each leaf window, compare the fetched
``(row_hash, ordinal)`` set against the live rows in that *leaf's* pub_date
range and write only the delta. A quiet tick inserts 0 and supersedes 0
(CLAUDE.md invariant 4).
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta

from .. import config
from ..db import immediate
from ..sources import fi

ONE_DAY = timedelta(days=1)

_INSERT_COLUMNS = (
    "row_hash",
    "ordinal",
    "pub_date",
    "first_seen_batch",
    "last_seen_batch",
    *fi.FIELD_NAMES,
)
_INSERT_SQL = (
    f"INSERT INTO raw_transaction ({', '.join(_INSERT_COLUMNS)}) "
    f"VALUES ({', '.join(':' + c for c in _INSERT_COLUMNS)})"
)

_COVERAGE_UPSERT = """
INSERT INTO coverage_day (pub_date, first_fetched_at, last_fetched_at, fetch_count, row_count)
VALUES (:day, :now, :now, 1, :rows)
ON CONFLICT(pub_date) DO UPDATE SET
  last_fetched_at = :now,
  fetch_count     = fetch_count + 1,
  row_count       = excluded.row_count
"""


@dataclass
class IngestSummary:
    mode: str
    window_from: date
    window_to: date
    leaves: int = 0
    windows_skipped: int = 0
    rows_inserted: int = 0
    rows_superseded: int = 0
    truncated_windows: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str, str]] = field(default_factory=list)  # (from, to, msg)
    budget_exhausted: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and not self.truncated_windows


def _days(start: date, end: date) -> Iterator[str]:
    cur = start
    while cur <= end:
        yield cur.isoformat()
        cur += ONE_DAY


def _covered_days(conn: sqlite3.Connection, start: date, end: date) -> set[str]:
    rows = conn.execute(
        "SELECT pub_date FROM coverage_day WHERE pub_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    )
    return {r["pub_date"] for r in rows}


def write_leaf(
    conn: sqlite3.Connection, batch_id: int, leaf: fi.Leaf, *, now: str | None = None
) -> tuple[int, int]:
    """Diff one leaf window into the DB in a single ``BEGIN IMMEDIATE`` txn.

    Returns ``(rows_inserted, rows_superseded)``. When ``leaf.truncated`` the
    supersede step is skipped entirely and no ``coverage_day`` row is written
    (§4.5.4, §4.7): a capped window did not see everything.
    """
    now = now or config.now_local_iso()
    f, t = leaf.window_from.isoformat(), leaf.window_to.isoformat()
    fetched = {(r.row_hash, r.ordinal): r for r in leaf.rows}

    with immediate(conn):
        live = {
            (r["row_hash"], r["ordinal"]): r["id"]
            for r in conn.execute(
                "SELECT id, row_hash, ordinal FROM raw_transaction "
                "WHERE superseded_at IS NULL AND pub_date BETWEEN ? AND ?",
                (f, t),
            )
        }
        to_insert = [k for k in fetched if k not in live]
        to_supersede = [rid for k, rid in live.items() if k not in fetched]
        unchanged = [rid for k, rid in live.items() if k in fetched]

        if to_supersede and not leaf.truncated:
            conn.executemany(
                "UPDATE raw_transaction SET superseded_at = ? WHERE id = ?",
                [(now, rid) for rid in to_supersede],
            )

        for key in to_insert:
            row = fetched[key]
            conn.execute(
                _INSERT_SQL,
                {
                    "row_hash": row.row_hash,
                    "ordinal": row.ordinal,
                    "pub_date": row.pub_date,
                    "first_seen_batch": batch_id,
                    "last_seen_batch": batch_id,
                    **row.fields,
                },
            )

        if unchanged:
            conn.executemany(
                "UPDATE raw_transaction SET last_seen_batch = ? WHERE id = ?",
                [(batch_id, rid) for rid in unchanged],
            )

        if not leaf.truncated:
            per_day = Counter(r.pub_date for r in leaf.rows)
            for day in _days(leaf.window_from, leaf.window_to):
                conn.execute(
                    _COVERAGE_UPSERT, {"day": day, "now": now, "rows": per_day.get(day, 0)}
                )

        superseded = 0 if leaf.truncated else len(to_supersede)
        conn.execute(
            "UPDATE fetch_batch SET finished_at = ?, row_count = ?, "
            "rows_inserted = ?, rows_superseded = ? WHERE id = ?",
            (now, len(leaf.rows), len(to_insert), superseded, batch_id),
        )

    return len(to_insert), (0 if leaf.truncated else len(to_supersede))


def _open_batch(conn: sqlite3.Connection, mode: str, leaf: fi.Leaf) -> int:
    cur = conn.execute(
        "INSERT INTO fetch_batch (mode, window_from, window_to, started_at, "
        "truncated, http_status) VALUES (?, ?, ?, ?, ?, 200)",
        (mode, leaf.window_from.isoformat(), leaf.window_to.isoformat(), config.now_local_iso(),
         int(leaf.truncated)),
    )
    return int(cur.lastrowid)


def _record_error(conn: sqlite3.Connection, mode: str, wf: date, wt: date, msg: str) -> None:
    conn.execute(
        "INSERT INTO fetch_batch (mode, window_from, window_to, started_at, "
        "finished_at, error) VALUES (?, ?, ?, ?, ?, ?)",
        (mode, wf.isoformat(), wt.isoformat(), config.now_local_iso(), config.now_local_iso(), msg[:2000]),
    )


def run_ingest(
    conn: sqlite3.Connection,
    client: fi.FIClient,
    mode: str,
    window_from: date,
    window_to: date,
    *,
    skip_covered: bool = False,
) -> IngestSummary:
    """Fetch ``[window_from, window_to]`` in seed windows, bisecting on the cap."""
    summary = IngestSummary(mode, window_from, window_to)

    for seed_from, seed_to in fi.iter_seed_windows(window_from, window_to):
        if skip_covered:
            need = set(_days(seed_from, seed_to))
            if need <= _covered_days(conn, seed_from, seed_to):
                summary.windows_skipped += 1
                continue

        try:
            for leaf in fi.iter_leaf_windows(seed_from, seed_to, client.fetch_window):
                batch_id = _open_batch(conn, mode, leaf)
                ins, sup = write_leaf(conn, batch_id, leaf)
                summary.leaves += 1
                summary.rows_inserted += ins
                summary.rows_superseded += sup
                if leaf.truncated:
                    summary.truncated_windows.append(
                        (leaf.window_from.isoformat(), leaf.window_to.isoformat())
                    )
        except fi.RequestBudgetExceeded:
            summary.budget_exhausted = True
            return summary
        except Exception as exc:  # noqa: BLE001 — one bad window must not abort the run
            _record_error(conn, mode, seed_from, seed_to, f"{type(exc).__name__}: {exc}")
            summary.errors.append((seed_from.isoformat(), seed_to.isoformat(), str(exc)))

    return summary


# ── Mode wrappers (§4.3) ────────────────────────────────────────────────────
def backfill(
    conn: sqlite3.Connection, client: fi.FIClient, *, start: date | None = None,
    end: date | None = None,
) -> IngestSummary:
    start = start or date.fromisoformat(config.FI_EARLIEST)
    end = end or config.today()
    return run_ingest(conn, client, "backfill", start, end, skip_covered=True)


def recent(
    conn: sqlite3.Connection, client: fi.FIClient, *, days: int = config.RECENT_DAYS_DEFAULT
) -> IngestSummary:
    end = config.today()
    start = end - timedelta(days=days)
    return run_ingest(conn, client, "recent", start, end, skip_covered=False)


def missing_days(conn: sqlite3.Connection, *, until: date | None = None) -> list[str]:
    """Every calendar day from register start to yesterday with no coverage row."""
    until = until or (config.today() - ONE_DAY)
    rows = conn.execute(
        """
        WITH RECURSIVE d(day) AS (
          SELECT ?
          UNION ALL SELECT date(day, '+1 day') FROM d WHERE day < ?
        )
        SELECT d.day FROM d
        LEFT JOIN coverage_day c ON c.pub_date = d.day
        WHERE c.pub_date IS NULL
        ORDER BY d.day
        """,
        (config.FI_EARLIEST, until.isoformat()),
    )
    return [r["day"] for r in rows]


def contiguous_ranges(days: list[str]) -> list[tuple[date, date]]:
    """Collapse a sorted list of ISO days into ``[from, to]`` runs."""
    ranges: list[tuple[date, date]] = []
    for iso in days:
        d = date.fromisoformat(iso)
        if ranges and d == ranges[-1][1] + ONE_DAY:
            ranges[-1] = (ranges[-1][0], d)
        else:
            ranges.append((d, d))
    return ranges


def gaps(
    conn: sqlite3.Connection, client: fi.FIClient, *, dry_run: bool = False,
    max_days: int = 400,
) -> tuple[list[tuple[date, date]], IngestSummary | None]:
    ranges = contiguous_ranges(missing_days(conn))
    # Trim to the request budget proxy: at most `max_days` calendar days this run.
    trimmed: list[tuple[date, date]] = []
    budget = max_days
    for lo, hi in ranges:
        if budget <= 0:
            break
        span = (hi - lo).days + 1
        if span > budget:
            hi = lo + timedelta(days=budget - 1)
            span = budget
        trimmed.append((lo, hi))
        budget -= span

    if dry_run or not trimmed:
        return trimmed, None

    summary = IngestSummary("gaps", trimmed[0][0], trimmed[-1][1])
    for lo, hi in trimmed:
        part = run_ingest(conn, client, "gaps", lo, hi, skip_covered=True)
        summary.leaves += part.leaves
        summary.rows_inserted += part.rows_inserted
        summary.rows_superseded += part.rows_superseded
        summary.truncated_windows += part.truncated_windows
        summary.errors += part.errors
        if part.budget_exhausted:
            summary.budget_exhausted = True
            break
    return trimmed, summary
