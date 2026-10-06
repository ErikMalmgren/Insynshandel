"""`insyn doctor` — acceptance checks. Exit non-zero on any failure.

Two tiers:

* **DB checks** (default) — structural guarantees and, if a backfill has landed,
  data-shape checks. Never touches the network. Safe to run anywhere, any time.
* **network checks** (``--network``) — the measured single-window row counts and
  the bisection-recovery count. These fetch from FI (a dozen requests,
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

# (window, expected exact count) — windows chosen because they do not cap.
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
        "no truncated windows",
        lambda: (
            (n := conn.execute(
                "SELECT COUNT(*) FROM fetch_batch WHERE truncated = 1"
            ).fetchone()[0]) == 0,
            f"{n} fetch_batch rows with truncated=1",
        ),
    )

    c.check(
        "no coverage_day row for a truncated window",
        lambda: _no_coverage_for_truncated(conn),
    )

    if not have_data:
        c.skip("live-row count band", "no data — run `insyn ingest backfill`")
        c.skip("zero missing days", "no data")
        c.skip("duplicate-prevention index", "no data")
        return

    c.check(
        "duplicate-prevention index rejects a live dup",
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
            "live-row count in plausible band",
            lambda: (lo <= total <= hi, f"{total} live rows (band {lo}-{hi})"),
        )
        c.check(
            "zero missing days after backfill",
            lambda: (
                len(m := ingest.missing_days(conn)) == 0,
                f"{len(m)} missing days"
                + (f", first {m[0]}" if m else ""),
            ),
        )
    else:
        c.skip(
            "live-row count band",
            f"partial data ({total} rows, {span[0]}..{span[1]}) — not a full backfill",
        )
        c.skip("zero missing days", "not a full backfill")


def _no_coverage_for_truncated(conn: sqlite3.Connection) -> tuple[bool, str]:
    bad = conn.execute(
        """
        SELECT COUNT(*) FROM coverage_day c
        JOIN fetch_batch b ON b.truncated = 1
         AND c.pub_date BETWEEN b.window_from AND b.window_to
        """
    ).fetchone()[0]
    return bad == 0, f"{bad} coverage rows inside a truncated window"


def _currency_coverage(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Every non-SEK currency must have rates, or be a known no-series currency.

    A currency in neither set is the failure that matters: it means the register
    grew a currency nobody has classified yet, and its rows are silently
    uncounted. Add it to ``config.FX_CURRENCIES`` or ``config.FX_NO_SERIES``.
    """
    used = {r["currency"] for r in conn.execute(
        "SELECT DISTINCT currency FROM transaction_norm WHERE currency <> 'SEK'")}
    have = {r["currency"] for r in conn.execute("SELECT DISTINCT currency FROM fx_rate")}
    miss = used - have - set(config.FX_NO_SERIES)
    return not miss, (
        f"unclassified currencies: {sorted(miss)} — add to config.FX_CURRENCIES "
        f"(if SWEA has a series) or config.FX_NO_SERIES"
        if miss else f"{len(used)} non-SEK currencies, all classified"
    )


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

        c.check(f"fetch {f}..{t} == {expected} (frozen)", run)

    for f, t, expected, measured in RECENT_WINDOWS:
        def run(f=f, t=t, expected=expected) -> tuple[bool, str]:
            n = len(client.fetch_window(date.fromisoformat(f), date.fromisoformat(t)))
            delta = n - expected
            return n <= expected, f"got {n} ({delta:+d} since plan)"

        c.check(f"fetch {f}..{t} <= {expected} (measured {measured})", run)

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

    c.check(f"bisection recovers {bf}..{bt} == {expected}", bisection)

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

    c.check("re-running an unchanged window writes nothing", idempotent)

    def billerud() -> tuple[bool, str]:
        rows = client.fetch_window(date(2020, 3, 18), date(2020, 3, 18))
        hit = [
            r for r in rows
            if ";" in r.fields["beskrivning_av_korrigering"]
        ]
        return bool(hit), f"{len(hit)} rows with an embedded ; in beskrivning_av_korrigering"

    c.check("2020-03-18 row round-trips an embedded ;", billerud)


def _normalize_checks(c: _Checks, conn: sqlite3.Connection) -> None:
    if not conn.execute("SELECT 1 FROM transaction_norm LIMIT 1").fetchone():
        c.skip("transaction_norm rebuilt", "empty — run `insyn normalize`")
        return

    c.check(
        "transaction_norm row count == raw_live",
        lambda: (
            (n := conn.execute("SELECT COUNT(*) FROM transaction_norm").fetchone()[0])
            == (m := conn.execute("SELECT COUNT(*) FROM raw_live").fetchone()[0]),
            f"norm {n} vs raw_live {m}",
        ),
    )
    c.check(
        "no NULL published_date/transaction_date/nature/status",
        lambda: (
            (n := conn.execute(
                "SELECT COUNT(*) FROM transaction_norm WHERE published_date IS NULL "
                "OR transaction_date IS NULL OR nature IS NULL OR status IS NULL"
            ).fetchone()[0]) == 0,
            f"{n} rows with a NULL required field",
        ),
    )
    aggregated = bool(
        conn.execute("SELECT 1 FROM agg_company_period LIMIT 1").fetchone()
    )
    if not aggregated:
        c.check(
            "derived columns still NULL before aggregate",
            lambda: (
                (n := conn.execute(
                    "SELECT COUNT(*) FROM transaction_norm WHERE sign IS NOT NULL "
                    "OR is_counted IS NOT NULL OR gross_value_sek IS NOT NULL"
                ).fetchone()[0]) == 0,
                f"{n} rows have derived values but no aggregates — run `insyn aggregate`",
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
          f"({100 * no_lei / max(total, 1):.1f}%) — aggregate excludes these as no_lei")
    for r in rows:
        if r["no_lei"]:
            print(f"          {r['y']}: {r['no_lei']}/{r['n']} without LEI")


def _aggregate_checks(c: _Checks, conn: sqlite3.Connection) -> None:
    if not conn.execute("SELECT 1 FROM agg_company_period LIMIT 1").fetchone():
        c.skip("classification + aggregates", "empty — run `insyn build`")
        return

    c.check(
        "no unmapped Karaktär values",
        lambda: (
            len(u := {
                r["nature"] for r in conn.execute(
                    "SELECT DISTINCT nature FROM transaction_norm "
                    "WHERE nature NOT IN (SELECT karaktar FROM nature_map)"
                )
            }) == 0,
            f"unmapped: {sorted(u)}" if u else "all Karaktär values mapped",
        ),
    )
    c.check(
        "every uncounted row has a known exclude_reason",
        lambda: (
            (bad := conn.execute(
                "SELECT COUNT(*) FROM transaction_norm WHERE is_counted = 0 "
                "AND (exclude_reason IS NULL OR exclude_reason NOT IN "
                "('no_lei','not_current','nature_not_counted','volume_unit',"
                "'unparseable_number','no_fx_series','no_fx_rate',"
                "'implausible_unit_price','outlier'))"
            ).fetchone()[0]) == 0,
            f"{bad} uncounted rows with a missing/unknown reason",
        ),
    )
    c.check(
        # the query builder exports on value_exclude_reason, so a stale one
        # (migrated, never re-aggregated: NULL everywhere) would ship every
        # withdrawn and mis-priced row to the leaderboard
        "value_exclude_reason is exclude_reason without the nature step",
        lambda: (
            (bad := conn.execute(
                "SELECT COUNT(*) FROM transaction_norm WHERE "
                "CASE WHEN exclude_reason = 'nature_not_counted' "
                "THEN value_exclude_reason = 'nature_not_counted' "
                "ELSE value_exclude_reason IS NOT exclude_reason END"
            ).fetchone()[0]) == 0,
            f"{bad} rows disagree" + (" — re-run `insyn aggregate`" if bad else ""),
        ),
    )
    c.check(
        "every counted row has sign +1/-1 and a gross_value_sek",
        lambda: (
            (bad := conn.execute(
                "SELECT COUNT(*) FROM transaction_norm WHERE is_counted = 1 AND "
                "(sign NOT IN (-1, 1) OR gross_value_sek IS NULL)"
            ).fetchone()[0]) == 0,
            f"{bad} counted rows missing sign or value",
        ),
    )

    # Outliers. Only meaningful once market caps exist.
    have_mcap = bool(conn.execute(
        "SELECT 1 FROM market_cap_current WHERE market_cap_sek IS NOT NULL LIMIT 1"
    ).fetchone())
    if have_mcap:
        # The check exists to catch a broken market-cap join, so it must count
        # only rows the cap comparison itself rejected: `exclude_reason` is the
        # FIRST hit of the ordered filter chain, and `outlier` is its second-to-
        # last rule. Filtering instead on `<> 'implausible_unit_price'` swept in
        # every row excluded EARLIER (nature_not_counted, not_current,
        # volume_unit) that `verification` still labels an outlier — 189 of 198
        # on 2026-09-09, none of which the cap ever judged.
        c.check(
            "unexplained outlier count is small",
            lambda: (
                (n := conn.execute(
                    "SELECT COUNT(*) FROM transaction_norm "
                    "WHERE verification = 'outlier' AND exclude_reason = 'outlier'"
                ).fetchone()[0]) < 50,
                (f"{n} rows reached the market-cap comparison and failed it with "
                 f"no earlier explanation (a large number means the join is broken)"),
            ),
        )
        c.check(
            "unverifiable rows are still counted",
            lambda: (
                conn.execute(
                    "SELECT COUNT(*) FROM transaction_norm WHERE verification = "
                    "'unverifiable' AND is_counted = 0 AND exclude_reason NOT IN "
                    "('no_lei','not_current','nature_not_counted','volume_unit',"
                    "'unparseable_number','no_fx_series','no_fx_rate',"
                    "'implausible_unit_price')"
                ).fetchone()[0] == 0,
                "no unverifiable row was dropped for being unverifiable",
            ),
        )
    else:
        c.skip("outlier checks", "no market caps — every row is unverifiable")

    # The encoding guard. Measured, not pass/fail: a non-zero count is
    # the rule working. Zero would be the surprise.
    n_imp = conn.execute(
        "SELECT COUNT(*) FROM transaction_norm WHERE exclude_reason = 'implausible_unit_price'"
    ).fetchone()[0]
    print(f"  INFO  {n_imp} rows excluded as implausible_unit_price — FI wrote a "
          f"total into the Pris column")

    # Currency
    have_fx = bool(conn.execute("SELECT 1 FROM fx_rate LIMIT 1").fetchone())
    if have_fx:
        c.check(
            "no rows excluded for a missing FX rate",
            lambda: (
                (n := conn.execute(
                    "SELECT COUNT(*) FROM transaction_norm WHERE exclude_reason = 'no_fx_rate'"
                ).fetchone()[0]) == 0,
                f"{n} rows have no FX rate — widen `insyn refdata fx --backfill`",
            ),
        )
        c.check(
            "every currency is covered by fx_rate, SEK, or FX_NO_SERIES",
            lambda: _currency_coverage(conn),
        )
        # Rows we can never value, by currency. Not pass/fail — a measured fact
        # about the register, like the LEI line above.
        for r in conn.execute(
            "SELECT currency, COUNT(*) n FROM transaction_norm "
            "WHERE exclude_reason = 'no_fx_series' GROUP BY currency ORDER BY n DESC"
        ):
            print(f"  INFO  {r['n']} rows in {r['currency']} — no Riksbank series, "
                  f"excluded as no_fx_series (config.FX_NO_SERIES)")
        c.check(
            "non-SEK rows valued at a transaction-date rate, not today's",
            lambda: (
                (n := conn.execute(
                    "SELECT COUNT(*) FROM transaction_norm WHERE currency <> 'SEK' "
                    "AND fx_rate_date IS NOT NULL "
                    "AND julianday(transaction_date) - julianday(fx_rate_date) NOT BETWEEN 0 AND 7"
                ).fetchone()[0]) == 0,
                f"{n} non-SEK rows use a rate >7 days from the transaction date",
            ),
        )
    else:
        c.skip("currency checks", "no fx_rate data — run `insyn refdata fx --backfill`")


def _static_export_checks(c: _Checks, conn: sqlite3.Connection) -> None:
    if not conn.execute("SELECT 1 FROM agg_company_period LIMIT 1").fetchone():
        c.skip("static export", "no aggregates — run `insyn build`")
        return

    import json
    import tempfile

    from . import export_static

    with tempfile.TemporaryDirectory() as tmp:
        s = export_static.export(conn, tmp)
        files = sorted(p.relative_to(s.out_dir).as_posix() for p in s.out_dir.rglob("*.json"))

        c.check(
            "every dist file is non-empty valid JSON",
            lambda: (
                all(json.loads((s.out_dir / f).read_text()) for f in files),
                f"{len(files)} files, {s.bytes / 1_000_000:.2f} MB",
            ),
        )
        fm = json.loads((s.out_dir / "facts-meta.json").read_text())
        required = {
            "meta.json", "companies.json", "data-quality.json", "facts-meta.json",
            *(f"facts/{y}.json" for y in fm["years"]),
        }
        missing = required - set(files)
        c.check(
            "the documented file set is present",
            lambda: (not missing, f"missing: {sorted(missing)}" if missing else "all present"),
        )
        c.check(
            "the leaderboard's facts reproduce agg_company_period in every window",
            lambda: facts_parity(conn, s.out_dir),
        )
        c.check(
            "no company in facts-meta has market_cap 0 — the 0 sentinel escaped",
            lambda: (not s.warnings, "; ".join(s.warnings) or "clean"),
        )
        # Measured, not pass/fail: a row whose Befattning no pattern in
        # data/seed/position_group.csv matched is filtered as "Övrigt".
        print(f"  INFO  {s.position_unmatched}/{s.fact_rows} fact rows "
              f"({100 * s.position_unmatched / max(s.fact_rows, 1):.1f}%) have a Befattning "
              f"no position_group pattern matched — shown as Övrigt")
        c.check(
            # company/{lei}.json is what every company page loads first; the full
            # history lives in company-tx/ and is fetched only on demand. Assert
            # the cap, not its byte-count proxy.
            "no company file exceeds COMPANY_TX_LIMIT transactions",
            lambda: _company_tx_capped(s.out_dir),
        )
        c.check(
            "every company-tx file holds the full history, with company/ as its prefix",
            lambda: _company_tx_complete(s.out_dir),
        )
        # Size is a measured fact, not a gate: company-tx/ carries every filed row
        # and grows with the register, so a threshold here would only re-fire.
        # Promote it back to a check if `dist/` ever gets committed to a branch
        # (ingest.yml 6b).
        print(f"  INFO  dist/ is {s.bytes / 1_000_000:.2f} MB over {len(files)} files "
              f"— company-tx/ is the full history and grows with the register")
        c.check(
            "companies.json carries no person data",
            lambda: (
                "pdmr" not in (s.out_dir / "companies.json").read_text(),
                "no pdmr field in the company index",
            ),
        )


def _company_tx_capped(dist) -> tuple[bool, str]:
    """The actual worry: a company file carrying full history, not 50 rows."""
    import json

    worst_lei, worst_n, over = None, 0, 0
    for f in (dist / "company").glob("*.json"):
        n = len(json.loads(f.read_text(encoding="utf-8")).get("recent_transactions") or [])
        if n > worst_n:
            worst_lei, worst_n = f.stem, n
        if n > config.COMPANY_TX_LIMIT:
            over += 1
    if over:
        return False, f"{over} files over the {config.COMPANY_TX_LIMIT}-transaction cap"
    return True, f"max {worst_n}/{config.COMPANY_TX_LIMIT} ({worst_lei})"


def _company_tx_complete(dist) -> tuple[bool, str]:
    """company-tx/{lei}.json must hold tx_count_total rows, and the capped list
    in company/{lei}.json must be exactly its first rows — the page shows the
    capped list first and swaps in the full one, grouping by array order."""
    import json

    bad: list[str] = []
    n = 0
    for f in sorted((dist / "company").glob("*.json")):
        n += 1
        detail = json.loads(f.read_text(encoding="utf-8"))
        full_path = dist / "company-tx" / f.name
        if not full_path.exists():
            bad.append(f"{f.stem}: missing")
            continue
        full = json.loads(full_path.read_text(encoding="utf-8"))["transactions"]
        recent = detail["recent_transactions"]
        if len(full) != detail["tx_count_total"]:
            bad.append(f"{f.stem}: {len(full)} rows, total {detail['tx_count_total']}")
        elif full[:len(recent)] != recent:
            bad.append(f"{f.stem}: recent is not a prefix")
    if bad:
        return False, f"{len(bad)} bad: {'; '.join(bad[:5])}"
    return True, f"{n} companies complete"


def facts_parity(conn: sqlite3.Connection, dist) -> tuple[bool, str]:
    """Aggregate the exported facts the way leaderboard.js does with its
    default selection — the counted natures, every instrument and role — over
    every preset window, and compare with agg_company_period company by
    company. Money may differ by the per-row rounding to whole SEK; nothing
    else may differ at all."""
    import json
    from datetime import date

    meta = json.loads((dist / "facts-meta.json").read_text(encoding="utf-8"))
    epoch = date.fromisoformat(meta["epoch"])
    direction = [n["direction"] if n["default"] else None for n in meta["natures"]]
    leis = [co["lei"] for co in meta["companies"]]
    shards = [json.loads((dist / "facts" / f"{y}.json").read_text(encoding="utf-8"))
              for y in meta["years"]]

    bad: list[str] = [f"facts/{y}.json is build {f['build']}, meta is {meta['build']}"
                      for y, f in zip(meta["years"], shards) if f["build"] != meta["build"]]
    for w in meta["windows"]:
        lo = (date.fromisoformat(w["start"]) - epoch).days
        hi = (date.fromisoformat(w["end"]) - epoch).days
        got: dict[str, dict] = {}
        for f in shards:
            for d, c, p, n, v in zip(f["d"], f["c"], f["p"], f["n"], f["v"]):
                sign = direction[n]
                if sign is None or not lo <= d <= hi:
                    continue
                a = got.setdefault(leis[c], {"buy": 0, "sell": 0, "tx": 0,
                                             "buyers": set(), "sellers": set()})
                a["tx"] += 1
                a["buy" if sign > 0 else "sell"] += v
                if p >= 0:
                    a["buyers" if sign > 0 else "sellers"].add(p)
        want = {
            r["lei"]: r for r in conn.execute(
                "SELECT * FROM agg_company_period WHERE period = ?", (w["period"],))
        }
        if set(got) != set(want):
            bad.append(f"{w['period']}: {len(set(got) ^ set(want))} companies differ")
            continue
        for lei, a in got.items():
            r = want[lei]
            slack = 0.5 * r["tx_count"] + 1e-6
            if (a["tx"], len(a["buyers"]), len(a["sellers"])) != (
                    r["tx_count"], r["buyer_count"], r["seller_count"]) \
                    or abs(a["buy"] - r["buy_value_sek"]) > slack \
                    or abs(a["sell"] - r["sell_value_sek"]) > slack:
                bad.append(f"{w['period']}/{lei}")
    if bad:
        return False, f"{len(bad)} mismatches: {'; '.join(bad[:5])}"
    return True, f"{len(meta['windows'])} windows, {meta['row_count']} fact rows"


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
    print("doctor: normalize checks")
    _normalize_checks(c, conn)
    print("doctor: classify + aggregate checks")
    _aggregate_checks(c, conn)
    print("doctor: static export checks")
    _static_export_checks(c, conn)

    if network:
        print("doctor: network checks")
        _network_checks(c)
    else:
        print("doctor: network checks skipped (pass --network to run them)")

    print(f"\n{c.passed} passed, {c.failed} failed, {c.skipped} skipped")
    return 1 if c.failed else 0
