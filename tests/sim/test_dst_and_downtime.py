"""Scenarios 3-4: midnight and DST (America/Los_Angeles) lose or repeat no window; a hub that was down catches up once."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from conftest import result, stamps, utc
from telefi.simulate import SimHub, plain

ZONE = ZoneInfo("America/Los_Angeles")
STEP = timedelta(minutes=5)


def local(y, mo, d, h, m, fold=0) -> datetime:
    return datetime(y, mo, d, h, m, tzinfo=ZONE, fold=fold).astimezone(timezone.utc)


def headlines(hub) -> list[str]:
    return [plain(s.text).splitlines()[0] for s in hub.telegram.sends]


def run_dst_weekend(make_hub, day_before: tuple, arrivals: list[tuple]) -> SimHub:
    y, mo, d = day_before
    hub = make_hub(local(y, mo, d, 19, 0), "America/Los_Angeles", step=STEP)
    hub.advance_to(arrivals[-1][0] + timedelta(hours=14), [(at, result(title)) for at, title in arrivals])
    return hub


def test_spring_forward_keeps_every_window_once(make_hub):
    # clocks jump 02:00 -> 03:00 on Sun 2026-03-08; one item before the gap, one after, one the evening before
    items = [(local(2026, 3, 7, 19, 30), "sat evening"), (local(2026, 3, 7, 23, 30), "sat late"),
             (local(2026, 3, 8, 1, 30), "before the gap"), (local(2026, 3, 8, 3, 10), "after the gap"),
             (local(2026, 3, 8, 7, 30), "sun morning"), (local(2026, 3, 8, 11, 0), "sun noon"),
             (local(2026, 3, 8, 15, 0), "sun afternoon"), (local(2026, 3, 8, 19, 0), "sun evening")]
    hub = run_dst_weekend(make_hub, (2026, 3, 7), items)
    assert stamps(hub) == ["07 20:00", "08 08:00", "08 12:00", "08 16:00", "08 20:00"]
    assert headlines(hub)[1] == "🔵 4 updates · 08:00"                         # 23:30, 01:30, 03:10, 07:30: none lost


def test_fall_back_repeats_an_hour_but_no_window(make_hub):
    # clocks fall 02:00 -> 01:00 on Sun 2026-11-01: 01:30 happens twice
    items = [(local(2026, 10, 31, 23, 30), "sat late"), (local(2026, 11, 1, 1, 30, fold=0), "first 01:30"),
             (local(2026, 11, 1, 1, 30, fold=1), "second 01:30"), (local(2026, 11, 1, 7, 30), "sun morning"),
             (local(2026, 11, 1, 11, 0), "sun noon"), (local(2026, 11, 1, 19, 0), "sun evening")]
    hub = run_dst_weekend(make_hub, (2026, 10, 31), items)
    assert stamps(hub) == ["01 08:00", "01 12:00", "01 20:00"]
    assert headlines(hub)[0] == "🔵 4 updates · 08:00"


@pytest.mark.parametrize("night", [(2026, 3, 7), (2026, 10, 31)])
def test_a_window_is_never_sent_twice_across_the_transition(make_hub, night):
    y, mo, d = night
    hub = make_hub(local(y, mo, d, 19, 0), "America/Los_Angeles", step=STEP)
    hub.advance_to(local(y, mo, d, 19, 0) + timedelta(days=2))                  # two days, no items
    assert hub.telegram.sends == []


def test_midnight_does_not_split_a_window(make_hub):
    hub = make_hub(utc(21, 0), "UTC", step=STEP)
    hub.advance_to(utc(9, 0, day=1), [(utc(23, 55), result("before midnight")), (utc(0, 5, day=1), result("after midnight"))])
    assert stamps(hub) == ["29 08:00"] and headlines(hub) == ["🔵 2 updates · 08:00"]


def test_a_hub_down_overnight_sends_yesterdays_last_window_once_then_todays_on_time(make_hub):
    hub = make_hub(utc(6, 0), "UTC", step=STEP)
    arrivals = [(utc(7, 30), result("morning")), (utc(10, 0), result("late morning")), (utc(14, 0), result("afternoon")),
                (utc(17, 0), result("evening, held before the crash")),
                (utc(19, 30), result("while down 1")), (utc(21, 0), result("while down 2")),
                (utc(7, 45, day=1), result("after the catch-up 1")), (utc(7, 55, day=1), result("after the catch-up 2"))]
    hub.advance_to(utc(8, 30, day=1), [(at, e) for at, e in arrivals], down=[(utc(19, 0), utc(7, 30, day=1))])
    assert stamps(hub) == ["28 08:00", "28 12:00", "28 16:00", "29 07:30", "29 08:00"]
    catch_up, today = (plain(s.text) for s in hub.telegram.sends[-2:])
    assert catch_up.startswith("🔵 3 updates · 20:00")                         # yesterday's window, once
    assert today.startswith("🔵 2 updates · 08:00") and "after the catch-up 2" in today
