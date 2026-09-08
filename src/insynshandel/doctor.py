"""`insyn doctor` — acceptance checks. Exit non-zero on any failure.

Two tiers:

* **DB checks** (default) — structural guarantees and, if a backfill has landed,
  data-shape checks. Never touches the network. Safe to run anywhere, any time.
* **network checks** (``--network``) — the measured single-window row counts and
  the bisection-recovery count from §4.6. These fetch from FI (a dozen requests,
  ~1 min) and run against throwaway in-memory databases, so they never mutate
  the real store.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date

from . import config, db
from .pipeline import ingest
from .sources import fi

# (window, expected exact count) — windows chosen in §4.6 because they do not cap.
# Frozen windows: old enough that FI will not revise them. Exact equality — all
# six held on 2026-09-08. If one of these ever drifts, the parser or bisector is
# losing/duplicating rows.
FROZEN_WINDOWS: list[tuple[str, str, int]] = [
    ("2016-07-01", "2016-07-31", 606),
    ("2017-01-01", "2017-01-31", 581),
    ("2024-01-08", "2024-01-14", 150),
    ("2024-05-15", "2024-05-21", 579),
    ("2020-03-13", "2020-03-13", 217),
    ("2015-01-01", "2015-01-31", 0),
]
# Recent windows still inside FI's revision horizon. The export returns Reviderad
# and Makulerad rows too, so a count can only *fall* as reports are withdrawn — a
# count above the pinned value means something else changed. (n, measured-on)
RECENT_WINDOWS: list[tuple[str, str, int, str]] = [
    ("2026-03-01", "2026-03-07", 339, "2026-09-07"),
]
BISECTION_WINDOW = ("2020-03-01", "2020-03-15", 1558)
PLAUSIBLE_BAND = (160_000, 200_000)


class _Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def ok(self, name: str, detail: str = "") -> None:
        self.passed += 1
        print(f"  PASS  {name}" + (f" — {detail}" if detail else ""))

    def fail(self, name: str, detail: str) -> None:
        self.failed += 1
        print(f"  FAIL  {name} — {detail}")

    def skip(self, name: str, why: str) -> None:
        self.skipped += 1
        print(f"  SKIP  {name} — {why}")

    def check(self, name: str, fn: Callable[[], tuple[bool, str]]) -> None:
        try:
            good, detail = fn()
        except Exception as exc:  # noqa: BLE001
            self.fail(name, f"{type(exc).__name__}: {exc}")
            return
        (self.ok if good else self.fail)(name, detail)


# ── DB checks ───────────────────────────────────────────────────────────────
def _db_checks(c: _Checks, conn: sqlite3.Connection) -> None:
    have_data = bool(
        conn.execute("SELECT 1 FROM raw_transaction LIMIT 1").fetchone()
    )

    c.check(
        "no truncated windows (§4.6)",
        lambda: (
            (n := conn.execute(
                "SELECT COUNT(*) FROM fetch_batch WHERE truncated = 1"
            ).fetchone()[0]) == 0,
            f"{n} fetch_batch rows with truncated=1",
        ),
    )

    c.check(
        "no coverage_day row for a truncated window (§4.7)",
        lambda: _no_coverage_for_truncated(conn),
    )

    if not have_data:
        c.skip("live-row count band (§4.6)", "no data — run `insyn ingest backfill`")
        c.skip("zero missing days (§11.2.2)", "no data")
        c.skip("duplicate-prevention index (§11.1.1)", "no data")
        return

    c.check(
        "duplicate-prevention index rejects a live dup (§11.1.1)",
        lambda: _dup_index_holds(conn),
    )

    total = conn.execute("SELECT COUNT(*) FROM raw_live").fetchone()[0]
    span = conn.execute(
        "SELECT MIN(pub_date), MAX(pub_date) FROM raw_live"
    ).fetchone()
    covers_history = span[0] is not None and span[0] <= "2016-08-01" and span[1] >= (
        (config.today().replace(day=1)).isoformat()
    )
    if covers_history:
        lo, hi = PLAUSIBLE_BAND
        c.check(
            "live-row count in plausible band (§4.6)",
            lambda: (lo <= total <= hi, f"{total} live rows (band {lo}-{hi})"),
        )
        c.check(
            "zero missing days after backfill (§11.2.2)",
            lambda: (
                len(m := ingest.missing_days(conn)) == 0,
                f"{len(m)} missing days"
                + (f", first {m[0]}" if m else ""),
            ),
        )
    else:
        c.skip(
            "live-row count band (§4.6)",
            f"partial data ({total} rows, {span[0]}..{span[1]}) — not a full backfill",
        )
        c.skip("zero missing days (§11.2.2)", "not a full backfill")


def _no_coverage_for_truncated(conn: sqlite3.Connection) -> tuple[bool, str]:
    bad = conn.execute(
        """
        SELECT COUNT(*) FROM coverage_day c
        JOIN fetch_batch b ON b.truncated = 1
         AND c.pub_date BETWEEN b.window_from AND b.window_to
        """
    ).fetchone()[0]
    return bad == 0, f"{bad} coverage rows inside a truncated window"


def _dup_index_holds(conn: sqlite3.Connection) -> tuple[bool, str]:
    row = conn.execute(
        "SELECT row_hash, ordinal FROM raw_transaction WHERE superseded_at IS NULL LIMIT 1"
    ).fetchone()
    if row is None:
        return True, "no live rows to test"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO raw_transaction "
            "(row_hash, ordinal, pub_date, first_seen_batch, last_seen_batch) "
            "VALUES (?, ?, '2000-01-01', 1, 1)",
            (row["row_hash"], row["ordinal"]),
        )
        return False, "duplicate live (row_hash, ordinal) insert was NOT rejected"
    except sqlite3.IntegrityError:
        return True, "IntegrityError raised as expected"
    finally:
        conn.execute("ROLLBACK")


# ── network checks ──────────────────────────────────────────────────────────
def _network_checks(c: _Checks) -> None:
    client = fi.FIClient()

    for f, t, expected in FROZEN_WINDOWS:
        def run(f=f, t=t, expected=expected) -> tuple[bool, str]:
            rows = client.fetch_window(date.fromisoformat(f), date.fromisoformat(t))
            return len(rows) == expected, f"got {len(rows)}"

        c.check(f"fetch {f}..{t} == {expected} (§4.6, frozen)", run)

    for f, t, expected, measured in RECENT_WINDOWS:
        def run(f=f, t=t, expected=expected) -> tuple[bool, str]:
            n = len(client.fetch_window(date.fromisoformat(f), date.fromisoformat(t)))
            delta = n - expected
            return n <= expected, f"got {n} ({delta:+d} since plan)"

        c.check(f"fetch {f}..{t} <= {expected} (§4.6, measured {measured})", run)

    bf, bt, expected = BISECTION_WINDOW

    def bisection() -> tuple[bool, str]:
        conn = db.connect(":memory:")
        db.migrate(conn)
        summary = ingest.run_ingest(
            conn, client, "doctor", date.fromisoformat(bf), date.fromisoformat(bt)
        )
        n = conn.execute(
            "SELECT COUNT(*) FROM raw_live WHERE pub_date BETWEEN ? AND ?", (bf, bt)
        ).fetchone()[0]
        single = len(client.fetch_window(date.fromisoformat(bf), date.fromisoformat(bt)))
        good = n == expected and n > single and summary.ok
        return good, f"recovered {n} (single query {single}, expected {expected})"

    c.check(f"bisection recovers {bf}..{bt} == {expected} (§4.6)", bisection)

    def idempotent() -> tuple[bool, str]:
        conn = db.connect(":memory:")
        db.migrate(conn)
        f, t = date.fromisoformat("2024-01-08"), date.fromisoformat("2024-01-14")
        s1 = ingest.run_ingest(conn, client, "doctor", f, t)
        n1 = conn.execute("SELECT COUNT(*) FROM raw_live").fetchone()[0]
        s2 = ingest.run_ingest(conn, client, "doctor", f, t)
        n2 = conn.execute("SELECT COUNT(*) FROM raw_live").fetchone()[0]
        good = s2.rows_inserted == 0 and s2.rows_superseded == 0 and n1 == n2
        return good, (
            f"first run +{s1.rows_inserted}, second run +{s2.rows_inserted}"
            f"/-{s2.rows_superseded}, count {n1}->{n2}"
        )

    c.check("re-running an unchanged window writes nothing (§4.5, inv. 4)", idempotent)

    def billerud() -> tuple[bool, str]:
        rows = client.fetch_window(date(2020, 3, 18), date(2020, 3, 18))
        hit = [
            r for r in rows
            if ";" in r.fields["beskrivning_av_korrigering"]
        ]
        return bool(hit), f"{len(hit)} rows with an embedded ; in beskrivning_av_korrigering"

    c.check("2020-03-18 row round-trips an embedded ; (§4.1.1)", billerud)


def _normalize_checks(c: _Checks, conn: sqlite3.Connection) -> None:
    if not conn.execute("SELECT 1 FROM transaction_norm LIMIT 1").fetchone():
        c.skip("transaction_norm rebuilt (§5.3)", "empty — run `insyn normalize`")
        return

    c.check(
        "transaction_norm row count == raw_live (§5.3)",
        lambda: (
            (n := conn.execute("SELECT COUNT(*) FROM transaction_norm").fetchone()[0])
            == (m := conn.execute("SELECT COUNT(*) FROM raw_live").fetchone()[0]),
            f"norm {n} vs raw_live {m}",
        ),
    )
    c.check(
        "no NULL published_date/transaction_date/nature/status (§5.3)",
        lambda: (
            (n := conn.execute(
                "SELECT COUNT(*) FROM transaction_norm WHERE published_date IS NULL "
                "OR transaction_date IS NULL OR nature IS NULL OR status IS NULL"
            ).fetchone()[0]) == 0,
            f"{n} rows with a NULL required field",
        ),
    )
    c.check(
        "derived columns still NULL before aggregate (§5.0)",
        lambda: (
            (n := conn.execute(
                "SELECT COUNT(*) FROM transaction_norm WHERE sign IS NOT NULL "
                "OR is_counted IS NOT NULL OR gross_value_sek IS NOT NULL"
            ).fetchone()[0]) == 0,
            f"{n} rows already have derived values (run `insyn aggregate`?)",
        ),
    )
    # LEI coverage is a measured fact about the register, not pass/fail.
    rows = conn.execute(
        "SELECT substr(transaction_date,1,4) y, COUNT(*) n, "
        "SUM(CASE WHEN lei = '' OR lei IS NULL THEN 1 ELSE 0 END) no_lei "
        "FROM transaction_norm GROUP BY y ORDER BY y"
    ).fetchall()
    total = sum(r["n"] for r in rows)
    no_lei = sum(r["no_lei"] for r in rows)
    print(f"  INFO  LEI missing on {no_lei}/{total} norm rows "
          f"({100 * no_lei / max(total, 1):.1f}%) — phase 3 fallback key (inv 9/12)")
    for r in rows:
        if r["no_lei"]:
            print(f"          {r['y']}: {r['no_lei']}/{r['n']} without LEI")


def run(*, network: bool = False) -> int:
    c = _Checks()
    print("doctor: DB checks")
    conn = db.connect()
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_transaction'"
    ).fetchone():
        print("  database not migrated — run `insyn db migrate`")
        return 1
    _db_checks(c, conn)
    print("doctor: normalize checks (§5.3)")
    _normalize_checks(c, conn)

    if network:
        print("doctor: network checks (§4.6)")
        _network_checks(c)
    else:
        print("doctor: network checks skipped (pass --network to run them)")

    print(f"\n{c.passed} passed, {c.failed} failed, {c.skipped} skipped")
    return 1 if c.failed else 0
