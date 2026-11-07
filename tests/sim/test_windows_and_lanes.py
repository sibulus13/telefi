"""Scenarios 1-2: an ordinary day and the quiet night, from the example scenario files the CLI prints."""
from __future__ import annotations

import json
from pathlib import Path

from conftest import stamps
from telefi.simulate import plain, run_scenario

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "simulate"
FAST = 5          # minutes per step; every arrival and window in the examples is a multiple of 5


def run(name, tmp_path):
    spec = json.loads((EXAMPLES / f"{name}.json").read_text(encoding="utf-8"))
    return run_scenario({**spec, "step_min": FAST}, tmp_path)


def test_an_ordinary_day_sends_only_at_the_four_windows(tmp_path):
    hub = run("ordinary_day", tmp_path)
    assert stamps(hub) == ["28 08:00", "28 12:00", "28 20:00", "29 08:00"]      # 16:00 was empty: nothing sent


def test_each_window_carries_exactly_the_items_that_arrived_before_it(tmp_path):
    hub = run("ordinary_day", tmp_path)
    texts = [plain(s.text) for s in hub.telegram.sends]
    assert [t.splitlines()[0] for t in texts] == ["🔵 2 updates · 08:00", "🔵 2 updates · 12:00", "🔵 3 updates · 20:00",
                                                  "🟢 Assistant · Price Watch: 6 items priced, 1 alert"]
    assert "• 🔬 #712 ACME spread study: REFINE" in texts[0]
    assert "• 🟡 PR #51 is ready for review" in texts[2]


def test_a_day_has_at_most_four_batch_messages_and_all_are_silent(tmp_path):
    hub = run("ordinary_day", tmp_path)
    first_day = [s for s in hub.telegram.sends if s.at.day == 28]
    assert len(first_day) <= 4 and all(s.silent for s in hub.telegram.sends)


def test_the_night_critical_goes_now_error_waits_for_eight_blocker_for_seven(tmp_path):
    hub = run("night", tmp_path)
    assert stamps(hub) == ["28 23:30", "29 07:00", "29 08:00"]
    assert [s.silent for s in hub.telegram.sends] == [False, False, True]


def test_the_night_messages_say_what_the_lanes_promise(tmp_path):
    hub = run("night", tmp_path)
    critical, blocker, batch = (plain(s.text) for s in hub.telegram.sends)
    assert "Builder daemon is down" in critical
    assert "basket study" in blocker and "updates" not in blocker          # the blocker goes alone
    assert batch.startswith("🔵 2 updates · 08:00") and "Researcher run failed" in batch
