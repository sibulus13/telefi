"""Every test runs with TeleFi's home and spool in a temp directory: nothing reaches the real
~/.telefi, and no Telegram credentials leak in from the machine."""
from __future__ import annotations

from datetime import datetime

import pytest

from telefi import core

NOON = datetime(2026, 10, 28, 12, 0).astimezone()   # noon local time: outside quiet hours


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEFI_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(core, "HOME", tmp_path / "home")      # read at import time: patch it too
    monkeypatch.setattr(core, "CONFIG_PATH", tmp_path / "home" / "hub.json")
    monkeypatch.setattr(core, "clock", lambda: NOON)
    monkeypatch.setenv("TELEFI_SPOOL", str(tmp_path / "home" / "spool"))
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NOTIFLY_HOME", "NOTIFLY_HUB_SPOOL"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def root(tmp_path):
    return tmp_path / "spool"
