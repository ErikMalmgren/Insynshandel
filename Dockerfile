# §10.4 — one image, run identically on a VPS and a home server.
# uv's own image pins the interpreter by .python-version alone (no second
# place to keep in sync).
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# 1. Dependency layer — rebuilt only when the lockfile changes.
#    --no-install-project so this layer does not need README.md or src/.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# 2. Application layer. README.md is required: pyproject.toml points `readme`
#    at it and uv_build reads it while installing the local package.
COPY README.md ./
COPY src/ ./src/
COPY data/seed/ ./data/seed/
RUN uv sync --frozen --no-dev

# The database and cache live on the mounted volume, never in the image
# (§10.4 rule 1). INSYN_DATA_DIR moves DB_PATH + CACHE_DIR there in one knob;
# SEED_DIR stays at /app/data/seed inside the image.
#
# NOTE: the plan sketch used `ENV INSYN_DB`; the code reads `INSYN_DB_PATH`
# (or, as here, `INSYN_DATA_DIR`). The code is authoritative.
ENV INSYN_DATA_DIR=/data
VOLUME ["/data"]

EXPOSE 8000
# Bind to 0.0.0.0 *inside* the container; the host proxy still binds 127.0.0.1
# (see docker-compose.yml). uvicorn, not `insyn serve`, so --workers/--reload
# use the import string form.
CMD ["uv", "run", "uvicorn", "insynshandel.api.app:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
