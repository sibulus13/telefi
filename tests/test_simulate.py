"""The simulator's pure parts and its CLI (the scenarios themselves are tests/sim)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telefi.__main__ import main
from telefi.simulate import parse_when

ZONE = ZoneInfo("America/Los_Angeles")
START = datetime(2026, 10, 28, 6, 0, tzinfo=ZONE).astimezone(timezone.utc)


def test_parse_when_reads_clock_next_day_and_full_forms():
    assert parse_when("07:30", START, ZONE).astimezone(ZONE).isoformat() == "2026-10-28T07:30:00-07:00"
    assert parse_when("+1 07:30", START, ZONE).astimezone(ZONE).day == 29
    assert parse_when("2026-11-02T09:00", START, ZONE).astimezone(ZONE).isoformat() == "2026-11-02T09:00:00-08:00"


def test_simulate_prints_arrivals_and_the_send_with_its_time(tmp_path, capsys):
    scenario = {"timezone": "UTC", "start": "2026-10-28T07:00", "end": "08:10", "step_min": 5,
                "arrivals": [{"at": "07:10", "source": "research", "kind": "result", "severity": "info", "title": "A done"}]}
    path = tmp_path / "s.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    assert main(["simulate", str(path)]) == 0
    out = capsys.readouterr().out
    assert "07:10  arrive" in out and "08:00  SEND" in out and "1 Telegram sends" in out
