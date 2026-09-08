"""`insyn` command-line entry point.

Implemented: ``db migrate``, ``ingest {backfill,recent,gaps}``, ``normalize``,
``refdata {fx,figi,marketcaps}``, ``aggregate``, ``build``, ``doctor``.
``export-static`` / ``serve`` arrive in later phases.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from . import config, db
from .pipeline import aggregate, ingest, normalize, reference

_NOT_YET = {
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


def _print_classify(s: aggregate.ClassifySummary) -> None:
    if s.unmapped_natures:
        print("  !! UNMAPPED Karaktär values — add rows to data/seed/nature_map.csv:")
        for nature, n in s.unmapped_natures.items():
            print(f"       {nature!r}: {n} rows")
        return
    print(f"  classified {s.rows} rows: {s.counted} counted")
    print(f"  by exclude_reason: {s.by_reason}")
    print(f"  verification: {s.by_verification} "
          f"(market caps for {s.market_caps_available} companies)")
    if s.no_fx_rows:
        print(f"  !! {s.no_fx_rows} rows have no FX rate — run `insyn refdata fx --backfill`")
    if not s.market_caps_available:
        print("  !! no market caps loaded — every counted row is 'unverifiable' (§6.4). "
              "Run `insyn refdata figi marketcaps` (needs the backfill first).")


def _cmd_aggregate(args: argparse.Namespace) -> int:
    conn = db.connect()
    if not conn.execute("SELECT 1 FROM transaction_norm LIMIT 1").fetchone():
        print("transaction_norm is empty — run `insyn normalize` first", file=sys.stderr)
        return 1
    s = aggregate.run(conn)
    print("aggregate:")
    _print_classify(s.classify)
    if not s.classify.ok:
        return 1
    print(f"  agg_company_period rows by window: {s.periods}")
    return 0


def _cmd_refdata(args: argparse.Namespace) -> int:
    conn = db.connect()
    rc = 0
    for step in args.steps:
        if step == "fx":
            fs = reference.fx(conn, backfill=args.backfill)
            print(f"refdata fx: {fs.currencies}" + (f" errors={fs.errors}" if fs.errors else ""))
            rc |= 0 if fs.ok else 1
        elif step == "figi":
            gs = reference.figi(conn, limit=args.limit)
            print(f"refdata figi: asked={gs.asked} resolved={gs.resolved} "
                  f"negative={gs.negative} transport_errors={gs.transport_errors}")
        elif step == "marketcaps":
            reference.build_companies(conn)
            try:
                ms = reference.marketcaps(conn, standalone=True)
            except SystemExit:
                print("refdata marketcaps: provider degraded (exit 1)", file=sys.stderr)
                return 1
            print(f"refdata marketcaps: attempted={ms.attempted} written={ms.written} "
                  f"by_provider={ms.by_provider} failures={ms.failures}")
    return rc


def _cmd_build(args: argparse.Namespace) -> int:
    conn = db.connect()
    if not conn.execute("SELECT 1 FROM raw_live LIMIT 1").fetchone():
        print("raw_live is empty — run `insyn ingest backfill` first", file=sys.stderr)
        return 1

    print("build: normalize")
    ns = normalize.normalize(conn)
    print(f"  {ns.rows_out} rows"
          + (f", {ns.rows_without_lei} without LEI" if ns.rows_without_lei else ""))

    print("build: refdata")
    fs = reference.fx(conn, backfill=args.fx_backfill)
    print(f"  fx {fs.currencies}")
    if args.no_figi:
        print("  figi skipped (--no-figi)")
    else:
        gs = reference.figi(conn, limit=args.figi_limit)
        print(f"  figi resolved={gs.resolved} negative={gs.negative}")
    cs = reference.build_companies(conn)
    print(f"  companies {cs.companies} ({cs.with_ticker} with ticker)")
    if args.no_marketcaps or cs.with_ticker == 0:
        why = "--no-marketcaps" if args.no_marketcaps else "no company has a ticker yet"
        print(f"  marketcaps skipped ({why}) — every company will be 'unverifiable'")
    else:
        ms = reference.marketcaps(conn, standalone=False)   # degraded => warn, continue
        print(f"  marketcaps written={ms.written}"
              + (f" DEGRADED {ms.degraded}" if ms.degraded else ""))

    print("build: aggregate")
    ag = aggregate.run(conn)
    _print_classify(ag.classify)
    if not ag.classify.ok:
        return 1
    print(f"  windows {ag.periods}")
    return 0


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

    rd = sub.add_parser("refdata", help="reference data: fx / figi / marketcaps (§7)")
    rd.add_argument("steps", nargs="+", choices=["fx", "figi", "marketcaps"])
    rd.add_argument("--backfill", action="store_true", help="fx: fetch from 2016-07-01")
    rd.add_argument("--limit", type=int, default=None, help="figi: cap ISINs this run")

    sub.add_parser("aggregate", help="classify transaction_norm + build aggregates (§6)")

    bd = sub.add_parser("build", help="normalize -> refdata -> aggregate (§5.0)")
    bd.add_argument("--fx-backfill", action="store_true")
    bd.add_argument("--figi-limit", type=int, default=None)
    bd.add_argument("--no-figi", action="store_true", help="skip the OpenFIGI step")
    bd.add_argument("--no-marketcaps", action="store_true",
                    help="skip yfinance (slow/flaky); everything stays 'unverifiable'")

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
    if args.command == "refdata":
        return _cmd_refdata(args)
    if args.command == "aggregate":
        return _cmd_aggregate(args)
    if args.command == "build":
        return _cmd_build(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command in _NOT_YET:
        print(f"insyn {args.command}: not implemented yet — {_NOT_YET[args.command]}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
