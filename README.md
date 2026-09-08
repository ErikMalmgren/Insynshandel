# Insynshandel

Every insider transaction reported to Finansinspektionen's *insynsregistret*
since July 2016, ingested into SQLite, aggregated per company, and published as
static JSON plus a read-only API.

> **Status: under construction.** The pipeline is being built phase by phase —
> see [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md). This README gets its
> real content (screenshot, live link, the data-quality write-up) once the
> pipeline runs end to end (§15).

## Toolchain

Python 3.14, pinned. Uses [`uv`](https://docs.astral.sh/uv/) — never `pip`.

```bash
uv sync
uv run insyn            # shows the (planned) command surface
```

## Attribution

Transaction data from Finansinspektionen; FX rates from Sveriges Riksbank.
