"""FastAPI application (§8).

Every route is ``GET``, read-only and cacheable. The handlers call the same
:mod:`insynshandel.api.reads` functions ``export_static.py`` serializes, so the
live API and the static JSON cannot drift.

``create_app`` does **not** touch the database at import time — the Dockerfile
imports ``insynshandel.api.app:app`` before the volume is mounted.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .. import config
from . import routes


def create_app(db_path: str | None = None) -> FastAPI:
    app = FastAPI(
        title="Insynshandel API",
        version="1",
        summary="Read-only Swedish insider-trading data (FI insynsregistret).",
    )
    app.state.db_path = str(db_path or config.DB_PATH)

    # CORS: the configured origins only, never "*" (§1.2). GET is the whole API.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.API_CORS_ORIGINS,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _cache_control(request: Request, call_next):
        resp = await call_next(request)
        # Data changes hourly at most (§8). Health stays uncached for monitoring.
        if (
            request.method == "GET"
            and resp.status_code < 400
            and request.url.path.startswith("/api/")
        ):
            resp.headers.setdefault("Cache-Control", "public, max-age=3600")
        return resp

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(routes.router, prefix="/api/v1")
    return app


app = create_app()
