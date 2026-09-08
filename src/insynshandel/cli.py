"""`insyn` command-line entry point.

Implemented: ``db migrate``, ``ingest {backfill,recent,gaps}``, ``doctor``.
Later phases fill in ``build`` / ``export-static`` / ``serve`` (they print a
notice for now rather than crashing).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from . import config, db
from .pipeline import ingest, normalize

_NOT_YET = {
    "build": "phase 3 (normalize -> refdata -> aggregate); run `insyn normalize` for now",
    "aggregate": "phase 3",
    "refdata": "phase 5",
    "export-static": "phase 6",
    "serve": "phase 4",
}


def _print_summary(s: ingest.IngestSummary) -> None:
    print(
        f"{s.mode}: {s.window_from}..{s.window_to} | "
        f"leaves={s.leaves} skipped={s.windows_skipped} "
        f"inserted={s.rows_inserted} superseded={s.rows_superseded}"
    )
    for wf, wt in s.truncated_windows:
        print(f"  !! TRUNCATED {wf}..{wt} — a single day still hit the 1000-row cap")
    for wf, wt, msg in s.errors:
        print(f"  !! ERROR {wf}..{wt}: {msg}")
    if s.budget_exhausted:
        print(f"  .. request budget ({config.MAX_REQUESTS_PER_RUN}) hit — re-run to continue")


def _cmd_db(args: argparse.Namespace) -> int:
    if args.db_command != "migrate":
        print("usage: insyn db migrate", file=sys.stderr)
        return 2
    conn = db.connect()
    applied = db.migrate(conn)
    print("migrations applied:", ", ".join(applied) if applied else "(none — up to date)")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    from .sources.fi import FIClient

    conn = db.connect()
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_transaction'"
    ).fetchone():
        print("database not migrated — run `insyn db migrate` first", file=sys.stderr)
        return 1

    client = FIClient()

    if args.ingest_command == "backfill":
        s = ingest.backfill(
            conn, client,
            start=date.fromisoformat(args.from_date) if args.from_date else None,
            end=date.fromisoformat(args.to_date) if args.to_date else None,
        )
        _print_summary(s)
        _print_year_breakdown(conn)
        return 0 if s.ok and not s.budget_exhausted else 1

    if args.ingest_command == "recent":
        s = ingest.recent(conn, client, days=args.days)
        _print_summary(s)
        return 0 if s.ok else 1

    if args.ingest_command == "gaps":
        all_missing = len(ingest.missing_days(conn))
        if all_missing == 0:
            print("gaps: no missing days")
            return 0
        ranges, s = ingest.gaps(
            conn, client, dry_run=args.dry_run, max_days=args.max_days
        )
        this_run = sum((hi - lo).days + 1 for lo, hi in ranges)
        deferred = all_missing - this_run
        print(
            f"gaps: {all_missing} missing day(s); this run covers {this_run} "
            f"in {len(ranges)} range(s)"
            + (f", {deferred} deferred (raise --max-days)" if deferred else "")
        )
        for lo, hi in ranges:
            print(f"  {lo}..{hi}")
        if args.dry_run:
            print(f"(dry run — ~{_estimate_requests(this_run)} requests)")
            return 0
        if s is not None:
            _print_summary(s)
            return 0 if s.ok else 1
        return 0

    print("usage: insyn ingest {backfill,recent,gaps}", file=sys.stderr)
    return 2


def _estimate_requests(days: int) -> int:
    return max(1, -(-days // config.SEED_WINDOW_DAYS)) + 2


def _print_year_breakdown(conn) -> None:
    rows = conn.execute(
        "SELECT substr(pub_date,1,4) y, COUNT(*) n FROM raw_live GROUP BY y ORDER BY y"
    ).fetchall()
    total = sum(r["n"] for r in rows)
    print(f"raw_live: {total} rows")
    for r in rows:
        print(f"  {r['y']}: {r['n']}")


def _cmd_normalize(args: argparse.Namespace) -> int:
    conn = db.connect()
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transaction_norm'"
    ).fetchone():
        print("database not migrated — run `insyn db migrate` first", file=sys.stderr)
        return 1

    s = normalize.normalize(conn)
    print(f"normalize: {s.rows_in} raw_live rows -> {s.rows_out} transaction_norm rows")
    if s.unparseable_numbers:
        print(f"  {s.unparseable_numbers} unparseable volume/price values -> NULL")
    if s.rows_without_lei:
        pct = 100 * s.rows_without_lei / max(s.rows_out, 1)
        print(
            f"  LEI missing on {s.rows_without_lei} rows ({pct:.1f}%); "
            f"{s.rows_without_lei_or_isin} have neither LEI nor ISIN "
            f"— phase 3 keys on this (see CLAUDE.md)"
        )
    for label, counter in (
        ("volume_unit", s.distinct_volume_unit),
        ("currency", s.distinct_currency),
        ("status", s.distinct_status),
    ):
        print(f"  distinct {label}: {dict(counter)}")
    for odd, n in s.odd_booleans.items():
        print(f"  !! odd boolean {odd} x{n} (treated as 0)")
    for col, n in s.missing_required.items():
        print(f"  !! {n} rows with empty required field {col!r}")
    print("  next: `insyn aggregate` — until then every derived column is NULL (§5.0)")
    return 0 if s.ok else 1


def _cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    return doctor.run(network=args.network)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="insyn", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    db_p = sub.add_parser("db", help="database maintenance")
    db_p.add_argument("db_command", choices=["migrate"])

    ing = sub.add_parser("ingest", help="fetch the FI export into raw_transaction")
    ing_sub = ing.add_subparsers(dest="ingest_command", required=True)
    bf = ing_sub.add_parser("backfill", help="one-off full history fetch (~30-45 min)")
    bf.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD")
    bf.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD")
    rc = ing_sub.add_parser("recent", help="incremental fetch of the last N days")
    rc.add_argument("--days", type=int, default=config.RECENT_DAYS_DEFAULT)
    gp = ing_sub.add_parser("gaps", help="heal an outage (§4.7)")
    gp.add_argument("--dry-run", action="store_true")
    gp.add_argument("--max-days", type=int, default=400)

    sub.add_parser("normalize", help="rebuild transaction_norm from raw_live (§5)")

    doc = sub.add_parser("doctor", help="run acceptance checks (§11)")
    doc.add_argument("--network", action="store_true",
                     help="also run the live single-window fetch checks (§4.6)")

    for name, phase in _NOT_YET.items():
        sub.add_parser(name, help=f"{phase} — not implemented yet")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    if args.command == "db":
        return _cmd_db(args)
    if args.command == "ingest":
        return _cmd_ingest(args)
    if args.command == "normalize":
        return _cmd_normalize(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command in _NOT_YET:
        print(f"insyn {args.command}: not implemented yet — {_NOT_YET[args.command]}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
