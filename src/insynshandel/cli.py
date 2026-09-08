"""`insyn` command-line entry point.

Phase 0 stub: the pipeline commands land phase by phase (see IMPLEMENTATION_PLAN.md).
Until then this only prints the planned command surface so `uv run insyn` is not a
confusing crash.
"""

from __future__ import annotations

import argparse
import sys

# Planned command surface (plan/00-hygiene.md §2.1). Wired up per phase.
_PLANNED = [
    ("db migrate", "apply src/insynshandel/migrations/*.sql"),
    ("ingest backfill", "one-off full fetch of the FI export (~30-45 min)"),
    ("ingest recent", "hourly / nightly incremental fetch"),
    ("ingest gaps", "heal an outage (§4.7)"),
    ("refdata figi|fx|marketcaps", "OpenFIGI tickers, Riksbank FX, market caps"),
    ("build", "normalize -> refdata -> aggregate (use this, §5.0)"),
    ("normalize", "phase 2 only, debugging"),
    ("aggregate", "phase 3 only, debugging"),
    ("export-static", "write dist/ JSON"),
    ("serve", "run the read-only API"),
    ("doctor", "acceptance checks (§11)"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="insyn", description=__doc__)
    parser.add_argument("command", nargs="?", help="pipeline command")
    parser.parse_args(argv if argv is not None else sys.argv[1:])

    print("insyn: no commands are implemented yet (Phase 0 — toolchain only).")
    print("Planned surface:")
    for name, desc in _PLANNED:
        print(f"  insyn {name:<28} {desc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
