from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "insyn_sample.csv"


@pytest.fixture(scope="session")
def sample_bytes() -> bytes:
    return FIXTURE.read_bytes()


@pytest.fixture(scope="session")
def sample_export_bytes(sample_bytes: bytes) -> bytes:
    """The fixture minus the two synthetic TEST rows — a stand-in for a real fetch."""
    text = sample_bytes.decode("utf-16-le")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    kept = [rows[0]] + [
        r for r in rows[1:]
        if not r[4].startswith("TEST ") and r[11] != "Testkaraktär utan mappning"
    ]
    buf = io.StringIO()
    csv.writer(buf, delimiter=";", lineterminator="\r\n").writerows(kept)
    return buf.getvalue().encode("utf-16-le")


@pytest.fixture
def db_conn(tmp_path):
    from insynshandel import db

    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    yield conn
    conn.close()
