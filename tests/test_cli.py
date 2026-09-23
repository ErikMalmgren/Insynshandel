"""CLI-only helpers: the progress meter the long refdata steps print."""

from __future__ import annotations

import io

from insynshandel.cli import (
    _figi_progress,
    _fx_progress,
    _marketcap_progress,
    _Progress,
)
from insynshandel.pipeline.reference import FigiSummary


def test_progress_non_tty_writes_one_plain_line_per_tick():
    out = io.StringIO()                       # StringIO has no isatty() -> log mode
    meter = _Progress("figi", stream=out, min_interval_s=0)
    for i in (1, 2, 3):
        meter(i, 3, "resolved=1")
    lines = out.getvalue().splitlines()
    assert len(lines) == 3
    assert "\r" not in out.getvalue()         # no escape codes in a log file
    assert lines[0].strip().startswith("figi 1/3 (33%) resolved=1")
    assert "eta" in lines[0] and "elapsed" in lines[0]


def test_progress_throttles_but_never_drops_the_last_tick():
    out = io.StringIO()
    meter = _Progress("figi", stream=out, min_interval_s=1000)
    for i in (1, 2, 3):
        meter(i, 3, None)
    lines = out.getvalue().splitlines()
    assert [ln.split()[1] for ln in lines] == ["1/3", "3/3"]  # 2/3 throttled away


def test_figi_progress_adapter_renders_the_summary_and_honours_quiet(capsys):
    assert _figi_progress(quiet=True) is None
    cb = _figi_progress(quiet=False)          # binds sys.stderr, which capsys owns
    cb(1, 2, FigiSummary(asked=2, resolved=1, negative=0, transport_errors=0))
    assert "figi 1/2 (50%) resolved=1 negative=0 err=0" in capsys.readouterr().err


def test_marketcap_progress_rotates_a_meter_per_provider(capsys):
    assert _marketcap_progress(quiet=True) is None
    cb = _marketcap_progress(quiet=False)
    cb("yahoo", 1, 2)
    cb("yahoo", 2, 2)
    cb("manual", 1, 1)
    err = capsys.readouterr().err
    assert "marketcaps yahoo 1/2 (50%)" in err
    assert "marketcaps manual 1/1 (100%)" in err


def test_progress_has_no_eta_before_the_first_item_lands():
    out = io.StringIO()
    meter = _Progress("fx", stream=out, min_interval_s=0)
    meter(0, 8, "fetching USD")               # fx ticks at done=0; 0/0 has no rate
    assert "fx 0/8 (0%) fetching USD elapsed 0s eta ?" in out.getvalue()


def test_fx_progress_is_the_meter_itself(capsys):
    assert _fx_progress(quiet=True) is None
    cb = _fx_progress(quiet=False)
    cb(1, 8, "USD 2510 rows")
    assert "fx 1/8 (12%) USD 2510 rows" in capsys.readouterr().err
