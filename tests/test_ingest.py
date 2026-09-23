"""Phase 1 diff write path — the checks that protect the hourly cron.

All hermetic: parsed rows are fed straight in, no HTTP.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from insynshandel.pipeline import ingest
from insynshandel.sources import fi


def _leaf(rows: list[fi.ParsedRow], f="2020-03-02", t="2020-03-08", truncated=False):
    return fi.Leaf(date.fromisoformat(f), date.fromisoformat(t), rows, truncated)


def _mk(row_hash: str, ordinal: int, pub: str, **overrides) -> fi.ParsedRow:
    fields = {name: "" for name in fi.FIELD_NAMES}
    fields["publiceringsdatum"] = f"{pub} 12:00:00"
    fields.update(overrides)
    return fi.ParsedRow(row_hash, ordinal, fields)


def _open_batch(conn: sqlite3.Connection, leaf: fi.Leaf) -> int:
    return ingest._open_batch(conn, "test", leaf)


def _live_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM raw_live").fetchone()[0]


# ── the idempotence guarantee ──────────────────────────────────────────────
def test_reingesting_identical_data_writes_nothing(db_conn, sample_export_bytes):
    rows = fi.parse_export(sample_export_bytes)
    leaf = _leaf(rows, "2016-07-01", "2026-05-01")

    ins, sup = ingest.write_leaf(db_conn, _open_batch(db_conn, leaf), leaf)
    assert ins == len(rows)
    assert sup == 0
    first = _live_count(db_conn)

    for _ in range(11):
        ins, sup = ingest.write_leaf(db_conn, _open_batch(db_conn, leaf), leaf)
        assert (ins, sup) == (0, 0)
        assert _live_count(db_conn) == first


def test_unchanged_rows_only_touch_last_seen_batch(db_conn):
    rows = [_mk("h1", 0, "2020-03-03"), _mk("h2", 0, "2020-03-04")]
    leaf = _leaf(rows)
    b1 = _open_batch(db_conn, leaf)
    ingest.write_leaf(db_conn, b1, leaf)
    b2 = _open_batch(db_conn, leaf)
    ingest.write_leaf(db_conn, b2, leaf)
    seen = {r["row_hash"]: r["last_seen_batch"] for r in db_conn.execute(
        "SELECT row_hash, last_seen_batch FROM raw_live")}
    assert set(seen.values()) == {b2}


# ── the duplicate-prevention index ─────────────────────────────────────────
def test_partial_unique_index_rejects_second_live_copy(db_conn):
    leaf = _leaf([_mk("dup", 0, "2020-03-03")])
    ingest.write_leaf(db_conn, _open_batch(db_conn, leaf), leaf)
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO raw_transaction "
            "(row_hash, ordinal, pub_date, first_seen_batch, last_seen_batch) "
            "VALUES ('dup', 0, '2020-03-03', 1, 1)"
        )


def test_superseded_key_can_be_reinserted_as_new_live_row(db_conn):
    leaf1 = _leaf([_mk("h", 0, "2020-03-03", status="Aktuell")])
    ingest.write_leaf(db_conn, _open_batch(db_conn, leaf1), leaf1)
    # FI drops the row entirely -> superseded
    empty = _leaf([])
    ingest.write_leaf(db_conn, _open_batch(db_conn, empty), empty)
    assert _live_count(db_conn) == 0
    # ... then returns the identical row later -> a fresh live row, no IntegrityError
    ingest.write_leaf(db_conn, _open_batch(db_conn, leaf1), leaf1)
    assert _live_count(db_conn) == 1
    assert db_conn.execute("SELECT COUNT(*) FROM raw_transaction").fetchone()[0] == 2


# ── revisions ─────────────────────────────────────────────────────────────
def test_status_flip_supersedes_old_and_inserts_new(db_conn, sample_export_bytes):
    rows = fi.parse_export(sample_export_bytes)
    wanstedt = [r for r in rows if r.fields["person_i_ledande_stallning"] == "Stefan Wänstedt"]
    reviderad = next(r for r in wanstedt if r.fields["status"] == "Reviderad")
    aktuell = next(r for r in wanstedt if r.fields["status"] == "Aktuell")

    # before the revision the register showed one row, Aktuell/uncorrected —
    # a different status is a genuinely different row_hash
    orig_fields = {**reviderad.fields, "status": "Aktuell", "korrigering": ""}
    original = fi.ParsedRow(
        fi.row_hash([orig_fields[n] for n in fi.FIELD_NAMES]), 0, orig_fields)
    assert original.pub_date == "2026-04-21"
    leaf_a = _leaf([original], "2026-04-21", "2026-04-21")
    ingest.write_leaf(db_conn, _open_batch(db_conn, leaf_a), leaf_a)
    assert _live_count(db_conn) == 1

    # re-fetch: FI now returns the Reviderad + the new Aktuell
    leaf_b = _leaf([reviderad, aktuell], "2026-04-21", "2026-04-21")
    ins, sup = ingest.write_leaf(db_conn, _open_batch(db_conn, leaf_b), leaf_b)
    assert (ins, sup) == (2, 1)
    live = {r["status"] for r in db_conn.execute("SELECT status FROM raw_live")}
    assert live == {"Reviderad", "Aktuell"}
    assert db_conn.execute(
        "SELECT COUNT(*) FROM raw_transaction WHERE superseded_at IS NOT NULL"
    ).fetchone()[0] == 1


# ── leaf scoping ──────────────────────────────────────────────────────────
def test_leaf_diff_never_touches_a_sibling_leafs_rows(db_conn):
    left = _leaf([_mk("L", 0, "2020-03-02")], "2020-03-01", "2020-03-07")
    right = _leaf([_mk("R", 0, "2020-03-10")], "2020-03-08", "2020-03-15")
    ingest.write_leaf(db_conn, _open_batch(db_conn, left), left)
    ingest.write_leaf(db_conn, _open_batch(db_conn, right), right)
    assert _live_count(db_conn) == 2

    # re-process only the left leaf with the same content: right's row must survive
    ins, sup = ingest.write_leaf(db_conn, _open_batch(db_conn, left), left)
    assert (ins, sup) == (0, 0)
    assert _live_count(db_conn) == 2


def test_a_wrongly_parent_scoped_diff_would_be_caught(db_conn):
    """If write_leaf diffed against the parent range it would kill the sibling."""
    left = _leaf([_mk("L", 0, "2020-03-02")], "2020-03-01", "2020-03-04")
    right = _leaf([_mk("R", 0, "2020-03-10")], "2020-03-05", "2020-03-08")
    ingest.write_leaf(db_conn, _open_batch(db_conn, left), left)
    ingest.write_leaf(db_conn, _open_batch(db_conn, right), right)
    before = _live_count(db_conn)
    ingest.write_leaf(db_conn, _open_batch(db_conn, left), left)
    assert _live_count(db_conn) == before  # never reduced


# ── truncated windows ─────────────────────────────────────────────────────
def test_truncated_window_inserts_only_never_supersedes(db_conn):
    full = _leaf([_mk("a", 0, "2020-03-13"), _mk("b", 0, "2020-03-13")],
                 "2020-03-13", "2020-03-13")
    ingest.write_leaf(db_conn, _open_batch(db_conn, full), full)
    assert _live_count(db_conn) == 2

    # a later capped fetch of the same day only sees row "a" + a new row "c"
    capped = _leaf([_mk("a", 0, "2020-03-13"), _mk("c", 0, "2020-03-13")],
                   "2020-03-13", "2020-03-13", truncated=True)
    ins, sup = ingest.write_leaf(db_conn, _open_batch(db_conn, capped), capped)
    assert ins == 1
    assert sup == 0  # "b" is NOT superseded despite being absent from the fetch
    assert _live_count(db_conn) == 3


def test_truncated_window_writes_no_coverage_day(db_conn):
    capped = _leaf([_mk("x", 0, "2020-03-13")], "2020-03-13", "2020-03-13", truncated=True)
    ingest.write_leaf(db_conn, _open_batch(db_conn, capped), capped)
    assert db_conn.execute("SELECT COUNT(*) FROM coverage_day").fetchone()[0] == 0


# ── coverage_day ──────────────────────────────────────────────────────────
def test_coverage_day_written_for_every_calendar_day_including_empty(db_conn):
    leaf = _leaf([_mk("x", 0, "2020-03-03"), _mk("y", 0, "2020-03-03")],
                 "2020-03-02", "2020-03-05")
    ingest.write_leaf(db_conn, _open_batch(db_conn, leaf), leaf)
    got = {r["pub_date"]: r["row_count"] for r in db_conn.execute(
        "SELECT pub_date, row_count FROM coverage_day ORDER BY pub_date")}
    assert got == {
        "2020-03-02": 0, "2020-03-03": 2, "2020-03-04": 0, "2020-03-05": 0,
    }


def test_coverage_day_fetch_count_increments_on_reprocess(db_conn):
    leaf = _leaf([_mk("x", 0, "2020-03-03")], "2020-03-03", "2020-03-03")
    ingest.write_leaf(db_conn, _open_batch(db_conn, leaf), leaf)
    ingest.write_leaf(db_conn, _open_batch(db_conn, leaf), leaf)
    row = db_conn.execute("SELECT fetch_count, row_count FROM coverage_day").fetchone()
    assert row["fetch_count"] == 2
    assert row["row_count"] == 1


# ── the full run loop with a fake client ──────────────────────────────────
class FakeClient:
    def __init__(self, windows: dict[tuple[str, str], list[fi.ParsedRow]]):
        self.windows = windows
        self.calls: list[tuple[str, str]] = []

    def fetch_window(self, f: date, t: date) -> list[fi.ParsedRow]:
        key = (f.isoformat(), t.isoformat())
        self.calls.append(key)
        if key not in self.windows:
            raise AssertionError(f"unexpected window {key}")
        return self.windows[key]


def test_run_ingest_skips_covered_seed_windows(db_conn):
    rows = [_mk("h", 0, "2016-07-05")]
    client = FakeClient({("2016-07-01", "2016-07-14"): rows})
    s1 = ingest.run_ingest(db_conn, client, "backfill",
                           date(2016, 7, 1), date(2016, 7, 14), skip_covered=True)
    assert s1.rows_inserted == 1
    s2 = ingest.run_ingest(db_conn, client, "backfill",
                           date(2016, 7, 1), date(2016, 7, 14), skip_covered=True)
    assert s2.windows_skipped == 1
    assert s2.leaves == 0
    assert len(client.calls) == 1  # second run made no HTTP call


def test_run_ingest_records_error_and_continues(db_conn):
    class Boom:
        calls = 0

        def fetch_window(self, f, t):
            Boom.calls += 1
            if Boom.calls == 1:
                raise RuntimeError("connection reset")
            return [_mk("ok", 0, "2016-07-20")]

    s = ingest.run_ingest(db_conn, Boom(), "backfill",
                          date(2016, 7, 1), date(2016, 7, 28))
    assert len(s.errors) == 1
    assert s.rows_inserted == 1  # the second seed window still processed
    assert db_conn.execute(
        "SELECT COUNT(*) FROM fetch_batch WHERE error IS NOT NULL"
    ).fetchone()[0] == 1


def test_missing_days_and_contiguous_ranges(db_conn):
    for day in ("2016-07-01", "2016-07-02", "2016-07-05"):
        db_conn.execute(
            "INSERT INTO coverage_day (pub_date, first_fetched_at, last_fetched_at, "
            "fetch_count, row_count) VALUES (?, 'now', 'now', 1, 0)", (day,))
    missing = ingest.missing_days(db_conn, until=date(2016, 7, 6))
    assert missing == ["2016-07-03", "2016-07-04", "2016-07-06"]
    assert ingest.contiguous_ranges(missing) == [
        (date(2016, 7, 3), date(2016, 7, 4)),
        (date(2016, 7, 6), date(2016, 7, 6)),
    ]
