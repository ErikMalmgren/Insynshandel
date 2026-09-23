"""`insyn` command-line entry point.

Implemented: ``db migrate``, ``ingest {backfill,recent,gaps}``, ``normalize``,
``refdata {fx,figi,marketcaps}``, ``aggregate``, ``build``, ``export-static``,
``doctor``.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date

from . import config, db
from .pipeline import aggregate, ingest, normalize, reference


class _Progress:
    """A one-line, self-erasing progress meter for the long refdata steps.

    On a TTY it rewrites a single line at most 4x/s; piped to a log (CI,
    cron) it emits a plain line at most every 30 s so a full FIGI run costs
    ~2 lines/minute instead of one per ISIN. ETA is measured, not derived from
    the client's request spacing.
    """

    def __init__(self, label: str, *, stream=None, min_interval_s: float | None = None):
        self.label = label
        self.stream = stream if stream is not None else sys.stderr
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.every = min_interval_s if min_interval_s is not None else (
            0.25 if self.tty else 30.0
        )
        self.start = time.monotonic()
        self._last = 0.0
        self._dirty = False

    @staticmethod
    def _hms(seconds: float) -> str:
        s = max(0, int(seconds))
        return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else (
            f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"
        )

    def __call__(self, done: int, total: int, detail: object = None) -> None:
        now = time.monotonic()
        last = done >= total
        if not last and now - self._last < self.every:
            return
        self._last = now
        elapsed = now - self.start
        pct = 100 * done / total if total else 100.0
        eta = (self._hms((total - done) * (elapsed / done)) if done else "?")
        line = (f"  {self.label} {done}/{total} ({pct:.0f}%)"
                + (f" {detail}" if detail else "")
                + f" elapsed {self._hms(elapsed)} eta {eta}")
        if self.tty:
            self.stream.write("\r\x1b[2K" + line)
            self._dirty = True
        else:
            self.stream.write(line + "\n")
        self.stream.flush()
        if last:
            self.done()

    def done(self) -> None:
        if self._dirty:
            self.stream.write("\n")
            self.stream.flush()
            self._dirty = False


def _figi_progress(quiet: bool):
    """Adapt :class:`_Progress` to ``reference.figi``'s (done, total, summary)."""
    if quiet:
        return None
    meter = _Progress("figi")
    return lambda done, total, fs: meter(
        done, total,
        f"resolved={fs.resolved} negative={fs.negative} err={fs.transport_errors}",
    )


def _fx_progress(quiet: bool):
    """``reference.fx``'s (done, total, detail) is already _Progress's signature."""
    return None if quiet else _Progress("fx")


def _marketcap_progress(quiet: bool):
    """Adapt :class:`_Progress` to ``reference.marketcaps``'s (provider, done, total).

    Providers run in sequence, so one meter is rotated per provider — the
    outgoing one is flushed first or its half-written TTY line is overwritten.
    """
    if quiet:
        return None
    state: dict[str, object] = {"name": None, "meter": None}

    def tick(provider: str, done: int, total: int) -> None:
        if state["name"] != provider:
            if state["meter"] is not None:
                state["meter"].done()
            state["name"], state["meter"] = provider, _Progress(f"marketcaps {provider}")
        state["meter"](done, total)

    return tick


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
    if s.implausible_price_rows:
        print(f"  -- {s.implausible_price_rows} rows excluded as implausible_unit_price "
              f"(§6.2.1: a total written into the Pris column)")
    if s.no_fx_series_rows:
        print(f"  -- {s.no_fx_series_rows} rows in a currency with no Riksbank series "
              f"({', '.join(config.FX_NO_SERIES)}) — excluded as no_fx_series")
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
            fs = reference.fx(conn, backfill=args.backfill,
                              progress=_fx_progress(args.quiet))
            print(f"refdata fx: {fs.currencies}" + (f" errors={fs.errors}" if fs.errors else ""))
            rc |= 0 if fs.ok else 1
        elif step == "figi":
            gs = reference.figi(conn, limit=args.limit,
                                progress=_figi_progress(args.quiet))
            print(f"refdata figi: asked={gs.asked} resolved={gs.resolved} "
                  f"negative={gs.negative} transport_errors={gs.transport_errors}")
        elif step == "marketcaps":
            reference.build_companies(conn)
            try:
                ms = reference.marketcaps(
                    conn, standalone=True, progress=_marketcap_progress(args.quiet))
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
    fs = reference.fx(conn, backfill=args.fx_backfill,
                      progress=_fx_progress(args.quiet))
    print(f"  fx {fs.currencies}")
    if args.no_figi:
        print("  figi skipped (--no-figi)")
    else:
        gs = reference.figi(conn, limit=args.figi_limit,
                            progress=_figi_progress(args.quiet))
        print(f"  figi resolved={gs.resolved} negative={gs.negative}")
    cs = reference.build_companies(conn)
    print(f"  companies {cs.companies} ({cs.with_ticker} with ticker)")
    if args.no_marketcaps or cs.with_ticker == 0:
        why = "--no-marketcaps" if args.no_marketcaps else "no company has a ticker yet"
        print(f"  marketcaps skipped ({why}) — every company will be 'unverifiable'")
    else:
        ms = reference.marketcaps(               # degraded => warn, continue
            conn, standalone=False, progress=_marketcap_progress(args.quiet))
        print(f"  marketcaps written={ms.written}"
              + (f" DEGRADED {ms.degraded}" if ms.degraded else ""))

    print("build: aggregate")
    ag = aggregate.run(conn)
    _print_classify(ag.classify)
    if not ag.classify.ok:
        return 1
    print(f"  windows {ag.periods}")
    return 0


def _cmd_export_static(args: argparse.Namespace) -> int:
    from . import export_static

    conn = db.connect(read_only=True)
    if not conn.execute("SELECT 1 FROM agg_company_period LIMIT 1").fetchone():
        print("no aggregates — run `insyn build` first", file=sys.stderr)
        return 1
    s = export_static.export(conn, args.out)
    mb = s.bytes / 1_000_000
    print(f"export-static: {s.files} files ({mb:.2f} MB) -> {s.out_dir}/  "
          f"[{s.companies} companies]")
    for w in s.warnings:
        print(f"  !! {w}")
    return 1 if s.warnings else 0


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
    rd.add_argument("--quiet", action="store_true",
                    help="fx/figi/marketcaps: no progress meter, only the summary")

    sub.add_parser("aggregate", help="classify transaction_norm + build aggregates (§6)")

    bd = sub.add_parser("build", help="normalize -> refdata -> aggregate (§5.0)")
    bd.add_argument("--fx-backfill", action="store_true")
    bd.add_argument("--figi-limit", type=int, default=None)
    bd.add_argument("--no-figi", action="store_true", help="skip the OpenFIGI step")
    bd.add_argument("--no-marketcaps", action="store_true",
                    help="skip yfinance (slow/flaky); everything stays 'unverifiable'")
    bd.add_argument("--quiet", action="store_true",
                    help="no fx/figi/marketcaps progress meter, only the summaries")

    es = sub.add_parser("export-static", help="dump the read layer to static JSON (§9)")
    es.add_argument("--out", default="dist", help="output directory (default: dist/)")

    doc = sub.add_parser("doctor", help="run acceptance checks (§11)")
    doc.add_argument("--network", action="store_true",
                     help="also run the live single-window fetch checks (§4.6)")

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
    if args.command == "export-static":
        return _cmd_export_static(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
