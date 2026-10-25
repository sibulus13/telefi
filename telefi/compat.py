"""A minimal direct-send API for simple scripts.

`TelegramChannel(token, chat).send(title, body, level, reply_markup=...)` keeps working, but sends
through TeleFi's one Bot API client (`adapters.telegram_call`). Prefer publishing envelopes
(`telefi.envelope` + `telefi.publish`): they get routing, dedup, records and replies.
"""
from __future__ import annotations

import html
from typing import Callable

from telefi.adapters import telegram_call

TELEGRAM_MAX_BODY = 3800


class TelegramChannel:
    name = "telegram"
    external = True

    def __init__(self, bot_token: str | None, chat_id: str | None, *, opener: Callable | None = None) -> None:
        self.bot_token, self.chat_id, self._opener = bot_token, chat_id, opener

    def is_configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, title: str, body: str, level: str = "info", *, reply_markup: dict | None = None, **meta) -> bool:
        """Same message shape as before: bold title, preformatted body, optional inline keyboard."""
        if not self.is_configured():
            return False
        text = f"<b>{html.escape(title)}</b>\n\n<pre>{html.escape(body[:TELEGRAM_MAX_BODY])}</pre>"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return telegram_call(self.bot_token, "sendMessage", payload, opener=self._opener)["ok"]
