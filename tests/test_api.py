"""Phase 4 — FastAPI routes (§8). Hermetic: no network, throwaway file DB.

The six acceptance checks from `plan/04-api.md`:

1. every route returns the same shape as its static counterpart
   (`test_every_route_matches_its_static_file`);
2. `/leaderboard` rejects `order` / `dir` / `limit` / `offset`
   (`test_leaderboard_rejects_sort_and_paginate_params`);
3. `/transactions` rejects an `order` outside the allow-list
   (`test_transactions_rejects_order_outside_allowlist`);
4. no person index anywhere (`test_no_person_index`);
5. CORS echoes the configured origin, never `*`
   (`test_cors_echoes_configured_origin_never_star`);
6. the DB connection is read-only (`test_db_connection_is_read_only`).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from insynshandel import config, db, export_static
from insynshandel.api.app import create_app
from insynshandel.pipeline import aggregate, ingest, normalize, reference
from insynshandel.sources import fi

_FX = [(ccy, d, r) for ccy in ("GBP", "CAD", "EUR", "USD")
       for d, r in (("2016-01-01", 10.0), ("2026-01-01", 11.0))]


@pytest.fixture
def api(db_conn, tmp_path, sample_export_bytes):
    """(TestClient, write-conn). `db_conn` is a file at ``tmp_path/t.db``; the
    app opens its own read-only connections to the same file."""
    db_conn.executemany(
        "INSERT INTO fx_rate (currency, rate_date, sek_per_unit) VALUES (?, ?, ?)", _FX
    )
    rows = fi.parse_export(sample_export_bytes)
    leaf = fi.Leaf(date(2016, 7, 1), date(2026, 5, 1), rows, truncated=False)
    ingest.write_leaf(db_conn, ingest._open_batch(db_conn, "t", leaf), leaf)
    normalize.normalize(db_conn)
    reference.build_companies(db_conn)
    aggregate.run(db_conn)
    return TestClient(create_app(db_path=str(tmp_path / "t.db"))), db_conn


def _norm(doc: dict) -> dict:
    doc = dict(doc)
    doc.pop("computed_at", None)
    return doc


# ── 1. route ⇄ static-file parity ─────────────────────────────────────────
def test_every_route_matches_its_static_file(api, tmp_path):
    client, conn = api
    # seed one real (non-null) market cap so the float path — market_cap and
    # pct_of_mcap — is parity-checked too, not just the None path.
    lei = conn.execute(
        "SELECT lei FROM agg_company_period WHERE period = 'all' "
        "ORDER BY ABS(net_value_sek) DESC LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO market_cap (lei, as_of, market_cap, currency, market_cap_sek, "
        "source) VALUES (?, '2026-09-08', 5.0e9, 'SEK', 5.0e9, 'test')", (lei,)
    )

    s = export_static.export(conn, tmp_path / "dist")

    def disk(name: str) -> dict:
        return json.loads((s.out_dir / name).read_text())

    assert _norm(client.get("/api/v1/meta").json()) == _norm(disk("meta.json"))
    assert _norm(client.get("/api/v1/data-quality").json()) == _norm(
        disk("data-quality.json")
    )
    for period in config.EXPORT_PERIODS:
        got = client.get(f"/api/v1/leaderboard?period={period}").json()
        assert _norm(got) == _norm(disk(f"leaderboard-{period}.json"))
    # the seeded market cap must actually be in the compared payload
    hit = next(e for e in client.get("/api/v1/leaderboard?period=all").json()["entries"]
               if e["lei"] == lei)
    assert hit["market_cap"] == 5.0e9 and hit["pct_of_mcap"] is not None
    assert _norm(client.get("/api/v1/companies").json()) == _norm(disk("companies.json"))

    seen = 0
    for f in sorted((s.out_dir / "company").glob("*.json")):
        got = client.get(f"/api/v1/companies/{f.stem}").json()
        assert _norm(got) == _norm(json.loads(f.read_text())), f.stem
        seen += 1
    assert seen > 0


# ── 2. leaderboard has no sort/paginate knob (§8.2) ──────────────────────
def test_leaderboard_rejects_sort_and_paginate_params(api):
    client, _ = api
    for qs in ("order=net_value_sek", "dir=desc", "limit=10", "offset=5", "foo=1"):
        assert client.get(f"/api/v1/leaderboard?{qs}").status_code == 422, qs
    assert client.get("/api/v1/leaderboard?period=all").status_code == 200
    assert client.get("/api/v1/leaderboard?period=ytd").status_code == 200
    assert client.get("/api/v1/leaderboard?period=bogus").status_code == 422


# ── 3. /transactions order is a closed allow-list ────────────────────────
def test_transactions_rejects_order_outside_allowlist(api):
    client, _ = api
    assert client.get("/api/v1/transactions?order=net_value_sek").status_code == 422
    assert client.get("/api/v1/transactions?order=gross_value_sek").status_code == 422
    assert client.get("/api/v1/transactions?order=date").status_code == 200
    assert client.get("/api/v1/transactions?order=value&dir=asc").status_code == 200
    assert client.get("/api/v1/transactions?dir=sideways").status_code == 422


# ── 4. no person index (§8.1) ───────────────────────────────────────────
def test_no_person_index(api):
    client, _ = api
    assert client.get("/api/v1/persons").status_code == 404
    assert client.get("/api/v1/persons/Anna").status_code == 404
    # a person filter on any feed is a 422, not a silent ignore
    assert client.get("/api/v1/transactions?pdmr=Anna").status_code == 422
    assert client.get("/api/v1/companies/x/transactions?pdmr=Anna").status_code == 422

    feed = client.get("/api/v1/transactions?limit=20").json()
    assert feed["items"]
    for row in feed["items"]:
        assert "pdmr" not in row and "position" not in row

    # a company's OWN transaction list keeps pdmr — that is the §8.1 grant
    lei = client.get("/api/v1/companies").json()["companies"][0]["lei"]
    own = client.get(f"/api/v1/companies/{lei}/transactions").json()
    if own["items"]:
        assert "pdmr" in own["items"][0]


# ── 5. CORS ─────────────────────────────────────────────────────────────
def test_cors_echoes_configured_origin_never_star(api):
    client, _ = api
    origin = config.API_CORS_ORIGINS[0]
    ok = client.options(
        "/api/v1/meta",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    assert ok.headers["access-control-allow-origin"] == origin
    assert ok.headers["access-control-allow-origin"] != "*"

    denied = client.options(
        "/api/v1/meta",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in denied.headers


# ── 6. read-only DB (§10.2.3) ───────────────────────────────────────────
def test_db_connection_is_read_only(api, tmp_path):
    client, _ = api
    assert client.get("/api/v1/meta").status_code == 200  # reads work

    ro = db.connect(tmp_path / "t.db", read_only=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE hack (x)")
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("UPDATE transaction_norm SET nature = 'x'")
    finally:
        ro.close()


# ── pagination correctness ──────────────────────────────────────────────
def test_transactions_pagination_covers_every_row_once(api):
    client, _ = api
    full = client.get("/api/v1/transactions?limit=500&offset=0").json()
    assert full["total"] == len(full["items"]) > 0  # sample fits one page

    paged: list = []
    for off in range(0, full["total"], 7):
        page = client.get(f"/api/v1/transactions?limit=7&offset={off}").json()
        assert page["total"] == full["total"]
        assert page["limit"] == 7 and page["offset"] == off
        paged += page["items"]
    assert paged == full["items"]  # stable order, no overlap, no gap


def test_transactions_from_to_actually_narrows(api):
    client, _ = api
    rows = client.get("/api/v1/transactions?limit=500").json()["items"]
    dates = sorted(r["transaction_date"] for r in rows)
    cut = dates[len(dates) // 2]

    narrowed = client.get(f"/api/v1/transactions?limit=500&from={cut}").json()
    assert 0 < narrowed["total"] < len(rows)
    assert all(r["transaction_date"] >= cut for r in narrowed["items"])

    assert client.get("/api/v1/transactions?from=not-a-date").status_code == 422


def test_transactions_lei_and_isin_filters(api):
    client, conn = api
    lei = conn.execute(
        "SELECT lei FROM transaction_norm WHERE lei <> '' GROUP BY lei "
        "ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()[0]
    by_lei = client.get(f"/api/v1/transactions?lei={lei}&limit=500").json()
    assert by_lei["total"] > 0
    assert {r["lei"] for r in by_lei["items"]} == {lei}


def test_company_transactions_paginates_and_sorts(api):
    client, conn = api
    lei = conn.execute(
        "SELECT n.lei FROM transaction_norm n JOIN company c ON c.lei = n.lei "
        "GROUP BY n.lei ORDER BY COUNT(*) DESC LIMIT 1"
    ).fetchone()[0]
    asc = client.get(
        f"/api/v1/companies/{lei}/transactions?order=date&dir=asc&limit=500"
    ).json()
    desc = client.get(
        f"/api/v1/companies/{lei}/transactions?order=date&dir=desc&limit=500"
    ).json()
    assert asc["total"] == desc["total"] > 0
    d = [r["transaction_date"] for r in asc["items"]]
    assert d == sorted(d)
    assert [r["transaction_date"] for r in desc["items"]] == sorted(d, reverse=True)


# ── method & 404 hygiene ────────────────────────────────────────────────
def test_all_routes_are_get_only(api):
    client, _ = api
    assert client.post("/api/v1/meta").status_code == 405
    assert client.put("/api/v1/leaderboard").status_code == 405
    assert client.delete("/api/v1/transactions").status_code == 405


def test_unknown_company_is_404(api):
    client, _ = api
    assert client.get("/api/v1/companies/NOSUCHLEI0000000000").status_code == 404
    assert (
        client.get("/api/v1/companies/NOSUCHLEI0000000000/transactions").status_code
        == 404
    )


def test_health_is_uncached_and_dumb(api):
    client, _ = api
    r = client.get("/health")
    assert r.json() == {"status": "ok"}
    assert "cache-control" not in r.headers


def test_api_responses_are_cacheable(api):
    client, _ = api
    assert (
        client.get("/api/v1/meta").headers["cache-control"]
        == "public, max-age=3600"
    )


# ── /companies?q= is a filter, not a paginate ──────────────────────────
def test_companies_q_filters_without_breaking_export_parity(api, tmp_path):
    client, conn = api
    s = export_static.export(conn, tmp_path / "dist")
    disk = json.loads((s.out_dir / "companies.json").read_text())

    assert _norm(client.get("/api/v1/companies").json()) == _norm(disk)

    frag = disk["companies"][0]["name"][:4]
    hit = client.get(f"/api/v1/companies?q={frag}").json()
    assert 0 < hit["count"] < disk["count"]  # strictly narrows, doesn't pass everything
    # a fragment no company carries → genuinely empty, not "everything"
    assert client.get("/api/v1/companies?q=zzqqxx-nomatch").json()["count"] == 0
    assert client.get("/api/v1/companies?limit=10").status_code == 422  # not a paginate
