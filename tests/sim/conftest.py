"""Simulated-time integration layer: the real hub round against a fake clock and a fake Telegram (telefi/simulate.py).
Every test in tests/sim is marked `integration`; run only these with `pytest -m integration`."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from telefi.simulate import SimHub

DAY = (2026, 10, 28)


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/sim" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


def utc(h: int, m: int = 0, day: int = 0, base: tuple = DAY) -> datetime:
    return datetime(*base, h, m, tzinfo=timezone.utc) + timedelta(days=day)


def result(title: str, source: str = "research", **kw) -> dict:
    return {"source": source, "kind": "result", "severity": "info", "title": title, **kw}


def stamps(hub: SimHub) -> list[str]:
    """Local send times, 'dd HH:MM', one per Telegram message."""
    return [f"{s.at:%d %H:%M}" for s in hub.telegram.sends]


@pytest.fixture
def make_hub(tmp_path):
    """make_hub(start, tz='UTC', **kw) -> SimHub with its clock installed for the whole test."""
    hubs, stack = [], []

    def make(start: datetime, tz: str = "UTC", **kw) -> SimHub:
        hub = SimHub(tmp_path / f"hub{len(hubs)}", start=start, tz=tz, **kw)
        stack.append(hub.installed())
        stack[-1].__enter__()
        hubs.append(hub)
        return hub

    yield make
    for cm in reversed(stack):
        cm.__exit__(None, None, None)
    for hub in hubs:
        assert hub.log_errors() == [], "a hub round raised"
