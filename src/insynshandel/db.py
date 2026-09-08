"""SQLite connection and migration helpers.

The ingest is the only writer; the API opens the same file read-only. All money
and number parsing happens downstream — this layer is schema plumbing only.
"""

from __future__ import annotations

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
    return newly
