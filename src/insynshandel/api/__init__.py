"""Read API surface.

* `schemas.py` — Pydantic response models (the one schema for both read paths).
* `reads.py`   — shared read functions: each takes a read connection, returns a
  `schemas` model. The single source of truth.
* `app.py` / `routes.py` — the FastAPI wrapper (`insyn serve`).

`export_static.py` calls the identical `reads` functions, so the live API and
the static JSON cannot drift (§1.1, §8.2)."""
