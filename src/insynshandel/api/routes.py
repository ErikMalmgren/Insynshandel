"""API routes (§8). Thin wrappers over :mod:`insynshandel.api.reads`.

Query parameters are Pydantic models with ``extra="forbid"``: an unknown
parameter is a 422, never a silent ignore. That is what makes three of the
Phase-4 acceptance checks hold at once —

* ``/leaderboard`` rejects ``order`` / ``dir`` / ``limit`` / ``offset`` (§8.2),
* ``/transactions`` rejects an ``order`` outside the allow-list, and
* ``?pdmr=`` is not an accepted filter anywhere (§8.1).

Sorting lives only where pagination lives (§8.2): ``/leaderboard`` and
``/companies`` ship complete and unsorted; ``/transactions`` and
``/companies/{lei}/transactions`` sort server-side on the ``reads`` allow-list.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import config, db
from . import reads, schemas

router = APIRouter()


def _conn(request: Request) -> Iterator[sqlite3.Connection]:
    """One read-only connection per request (§10.2.3)."""
    conn = db.connect(request.app.state.db_path, read_only=True)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(_conn)]


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


# ── query-parameter models ─────────────────────────────────────────────────
class _Forbid(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LeaderboardParams(_Forbid):
    # every AGG_PERIODS value — `ytd` has no static file but reads.leaderboard
    # serves it. No order/dir/limit/offset: the client holds the whole set (§8.2).
    period: Literal["30d", "90d", "365d", "ytd", "all"] = "all"


class CompanyIndexParams(_Forbid):
    q: str | None = None  # server-side filter over name / ticker / LEI, not a sort


class _PageParams(_Forbid):
    date_from: date | None = Field(default=None, alias="from")
    date_to: date | None = Field(default=None, alias="to")
    order: Literal["date", "value"] = "date"
    dir: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=config.API_PAGE_DEFAULT, ge=1, le=config.API_PAGE_MAX)
    offset: int = Field(default=0, ge=0)


class CompanyTxParams(_PageParams):
    pass


class TransactionsParams(_PageParams):
    lei: str | None = None
    isin: str | None = None
    # note: no `pdmr` — a person filter on a global feed is the person index §8.1
    # forbids. `extra="forbid"` turns `?pdmr=` into a 422.


# ── routes ─────────────────────────────────────────────────────────────────
@router.get("/meta", response_model=schemas.Meta, tags=["meta"])
def meta(conn: Conn) -> schemas.Meta:
    return reads.meta(conn)


@router.get("/leaderboard", response_model=schemas.Leaderboard, tags=["leaderboard"])
def leaderboard(
    conn: Conn, params: Annotated[LeaderboardParams, Query()]
) -> schemas.Leaderboard:
    return reads.leaderboard(conn, params.period)


@router.get("/companies", response_model=schemas.CompanyIndex, tags=["companies"])
def companies(
    conn: Conn, params: Annotated[CompanyIndexParams, Query()]
) -> schemas.CompanyIndex:
    return reads.company_index(conn, params.q)


@router.get(
    "/companies/{lei}", response_model=schemas.CompanyDetail, tags=["companies"]
)
def company(conn: Conn, lei: str) -> schemas.CompanyDetail:
    detail = reads.company_detail(conn, lei)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no company with LEI {lei}")
    return detail


@router.get(
    "/companies/{lei}/transactions",
    response_model=schemas.Paginated[schemas.CompanyTransaction],
    tags=["companies"],
)
def company_transactions(
    conn: Conn, lei: str, params: Annotated[CompanyTxParams, Query()]
) -> schemas.Paginated[schemas.CompanyTransaction]:
    page = reads.company_transactions(
        conn, lei,
        date_from=_iso(params.date_from), date_to=_iso(params.date_to),
        order=params.order, direction=params.dir,
        limit=params.limit, offset=params.offset,
    )
    if page is None:
        raise HTTPException(status_code=404, detail=f"no company with LEI {lei}")
    return page


@router.get(
    "/transactions",
    response_model=schemas.Paginated[schemas.Transaction],
    tags=["transactions"],
)
def transactions(
    conn: Conn, params: Annotated[TransactionsParams, Query()]
) -> schemas.Paginated[schemas.Transaction]:
    return reads.transactions(
        conn,
        date_from=_iso(params.date_from), date_to=_iso(params.date_to),
        lei=params.lei, isin=params.isin,
        order=params.order, direction=params.dir,
        limit=params.limit, offset=params.offset,
    )


@router.get(
    "/data-quality", response_model=schemas.DataQuality, tags=["data-quality"]
)
def data_quality(conn: Conn) -> schemas.DataQuality:
    return reads.data_quality(conn)
