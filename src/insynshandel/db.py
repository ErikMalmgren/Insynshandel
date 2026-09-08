"""SQLite connection and migration helpers.

The ingest is the only writer; the API opens the same file read-only. All money
and number parsing happens downstream — this layer is schema plumbing only.
"""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import config


def connect(path: Path | str | None = None, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the pipeline database.

    ``isolation_level=None`` (autocommit) is deliberate: the diff write path
    issues its own ``BEGIN IMMEDIATE`` so two overlapping cron runs serialize
    instead of corrupting the table (§4.5.2). Python's default deferred ``BEGIN``
    would not give that guarantee.
    """
    path = Path(path) if path is not None else config.DB_PATH
    if read_only:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not read_only:
        conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside a single ``BEGIN IMMEDIATE`` … ``COMMIT`` transaction."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migration_files() -> list[Path]:
    return sorted(config.MIGRATIONS_DIR.glob("*.sql"))


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every migration not yet recorded in ``schema_migrations``.

    Migrations are plain ``.sql`` run with ``executescript`` (which commits any
    open transaction first — so ``PRAGMA journal_mode = WAL`` at the top of a
    file runs outside a transaction, as it must).
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    applied = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    newly: list[str] = []
    for sql_file in migration_files():
        if sql_file.name in applied:
            continue
        conn.executescript(sql_file.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations(name) VALUES (?)", (sql_file.name,))
        newly.append(sql_file.name)
    load_seeds(conn)
    return newly


def _read_seed_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """Read a seed CSV, skipping ``#`` comment lines and blank lines."""
    lines = [
        ln for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    rows = list(csv.reader(lines))
    return rows[0], rows[1:]


def load_seeds(conn: sqlite3.Connection) -> dict[str, int]:
    """Reload every seed table from ``data/seed/*.csv``.

    Runs on every ``migrate`` **and** at the start of ``insyn aggregate`` — the
    plan's only lever for the classification (edit ``nature_map.csv``, re-run)
    depends on this being a full reload, not a load-once (§6.1.2).
    """
    counts: dict[str, int] = {}

    hdr, rows = _read_seed_csv(config.SEED_DIR / "nature_map.csv")
    assert hdr == ["karaktar", "sign", "counted", "category", "note"], hdr
    with immediate(conn):
        conn.execute("DELETE FROM nature_map")
        conn.executemany(
            "INSERT INTO nature_map (karaktar, sign, counted, category, note) "
            "VALUES (?, ?, ?, ?, ?)",
            [(r[0], int(r[1]), int(r[2]), r[3], r[4]) for r in rows],
        )
    counts["nature_map"] = len(rows)

    hdr, rows = _read_seed_csv(config.SEED_DIR / "issuer_alias.csv")
    assert hdr == ["alias_lei", "canonical_lei", "note"], hdr
    with immediate(conn):
        conn.execute("DELETE FROM issuer_alias")
        conn.executemany(
            "INSERT INTO issuer_alias (alias_lei, canonical_lei, note) VALUES (?, ?, ?)",
            [(r[0], r[1], r[2] if len(r) > 2 else "") for r in rows],
        )
    counts["issuer_alias"] = len(rows)

    # ticker_override: '?' symbol = unresolved worklist, skipped (data/seed/README.md)
    hdr, rows = _read_seed_csv(config.SEED_DIR / "ticker_override.csv")
    assert hdr == ["lei", "provider", "symbol", "note"], hdr
    loaded = [r for r in rows if (r[2] if len(r) > 2 else "") != "?"]
    with immediate(conn):
        conn.execute("DELETE FROM ticker_override")
        conn.executemany(
            "INSERT INTO ticker_override (lei, provider, symbol, note) VALUES (?, ?, ?, ?)",
            [(r[0], r[1] or "", (r[2] if len(r) > 2 else ""),
              (r[3] if len(r) > 3 else "")) for r in loaded],
        )
    counts["ticker_override"] = len(loaded)
    counts["ticker_override_worklist_skipped"] = len(rows) - len(loaded)

    return counts
