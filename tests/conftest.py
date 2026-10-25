"""Every test runs with TeleFi's home and spool in a temp directory: nothing reaches the real
~/.telefi, and no Telegram credentials leak in from the machine."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEFI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TELEFI_SPOOL", str(tmp_path / "home" / "spool"))
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NOTIFLY_HOME", "NOTIFLY_HUB_SPOOL"):
        monkeypatch.delenv(var, raising=False)
