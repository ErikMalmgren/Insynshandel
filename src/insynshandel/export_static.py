"""Phase 6 — dump the read layer to static JSON.

Every file is produced by calling a :mod:`insynshandel.reads` function and
serializing its Pydantic model (:mod:`insynshandel.schemas`).

    dist/meta.json
    dist/leaderboard-{30d,90d,365d,all}.json     (complete, UNSORTED)
    dist/companies.json                          (lei, name, ticker — no pdmr)
    dist/company/{lei}.json                      (detail + recent tx, capped)
    dist/company-tx/{lei}.json                   (full tx history, compact —
                                                  fetched only on demand)
    dist/data-quality.json
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import config, reads

_LEI_RE = re.compile(r"^[A-Za-z0-9]{18,20}$")


@dataclass
class ExportSummary:
    out_dir: Path
    files: int = 0
    bytes: int = 0
    companies: int = 0
    warnings: list[str] = field(default_factory=list)


def _write(path: Path, model, summary: ExportSummary, indent: int | None = 2) -> None:
    payload = json.dumps(model.model_dump(), ensure_ascii=False, indent=indent,
                         sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload + "\n", encoding="utf-8")
    summary.files += 1
    summary.bytes += len(payload) + 1


def export(conn: sqlite3.Connection, out_dir: Path | str) -> ExportSummary:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    s = ExportSummary(out_dir=out)

    _write(out / "meta.json", reads.meta(conn), s)
    _write(out / "data-quality.json", reads.data_quality(conn), s)

    for period in config.EXPORT_PERIODS:
        lb = reads.leaderboard(conn, period)
        _write(out / f"leaderboard-{period}.json", lb, s)
        # guard: the 0 sentinel must never escape market_cap_current.
        # `pct_of_mcap < 0` was part of this test until 2026-09-09 — wrongly: the
        # SQL already requires market_cap_sek > 0, so a negative pct means only
        # net_value_sek < 0, i.e. a company whose insiders were net sellers.
        for e in lb.entries:
            if e.market_cap == 0:
                s.warnings.append(
                    f"{period}/{e.lei}: market_cap={e.market_cap} pct={e.pct_of_mcap}"
                )

    _write(out / "companies.json", reads.company_index(conn), s)

    windows = reads.period_windows(conn)
    for row in conn.execute("SELECT lei FROM company ORDER BY lei"):
        lei = row["lei"]
        if not _LEI_RE.match(lei):
            s.warnings.append(f"skipped company with odd lei {lei!r}")
            continue
        detail = reads.company_detail(conn, lei, windows)
        if detail is not None:
            _write(out / "company" / f"{lei}.json", detail, s)
            # unindented: the full history is most of dist/, and nobody reads
            # it by eye
            _write(out / "company-tx" / f"{lei}.json",
                   reads.company_transactions(conn, lei), s, indent=None)
            s.companies += 1

    return s
