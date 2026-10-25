"""Outbound channel adapters: `send(envelope, *, buttons) -> {"ok", "error", "message_id"?}`.

A new channel (Slack, SMS, email) is one class here plus one `type` in hub_config.v1.json.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from telefi.render import render_telegram

HTTP_TIMEOUT_S = 10
TELEGRAM_API = "https://api.telegram.org"


def telegram_call(token: str, method: str, body: dict, *, opener: Callable | None = None,
                  timeout: float = HTTP_TIMEOUT_S) -> dict:
    """One Bot API call. Returns {"ok", "result"?, "error"?}; never raises, never echoes the URL
    (it carries the token)."""
    req = urllib.request.Request(f"{TELEGRAM_API}/bot{token}/{method}", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
            raw = resp.read() if hasattr(resp, "read") else b"{}"
            data = json.loads(raw or b"{}")
            if resp.status != 200 or not data.get("ok", True):
                return {"ok": False, "error": f"HTTP {resp.status}: {str(data.get('description', ''))[:120]}"}
            return {"ok": True, "result": data.get("result")}
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {getattr(ex, 'code', '')}".strip()}


@dataclass
class MockAdapter:
    """Records what would be sent; never touches the network. The default in tests and mode=mock."""
    name: str
    sent: list = field(default_factory=list)
    fail: bool = False

    def send(self, env: dict, *, buttons: bool) -> dict:
        if self.fail:
            return {"ok": False, "error": "mock failure"}
        self.sent.append({"envelope_id": env["id"], "rendered": render_telegram(env, buttons=buttons)})
        return {"ok": True, "error": None, "message_id": len(self.sent)}


@dataclass
class TelegramAdapter:
    name: str
    token: str | None
    chat_id: str | None
    opener: Callable | None = None

    def send(self, env: dict, *, buttons: bool) -> dict:
        if not (self.token and self.chat_id):
            return {"ok": False, "error": "telegram not configured (token/chat env missing)"}
        r = render_telegram(env, buttons=buttons)
        body = {"chat_id": self.chat_id, "text": r["text"], "parse_mode": "HTML", "disable_web_page_preview": True}
        if r["reply_markup"]:
            body["reply_markup"] = r["reply_markup"]
        res = telegram_call(self.token, "sendMessage", body, opener=self.opener)
        return {"ok": res["ok"], "error": res.get("error"),
                "message_id": (res.get("result") or {}).get("message_id")}


@dataclass
class FileAdapter:
    name: str
    path: Path

    def send(self, env: dict, *, buttons: bool) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "envelope": env}, ensure_ascii=False) + "\n")
        return {"ok": True, "error": None}


def telegram_creds(cfg: dict, name: str = "telegram") -> tuple[str | None, str | None]:
    c = cfg["channels"].get(name, {})
    return os.environ.get(c.get("token_env", "TELEGRAM_BOT_TOKEN")), os.environ.get(c.get("chat_env", "TELEGRAM_CHAT_ID"))


def build_adapters(cfg: dict, *, opener: Callable | None = None, resolve: Callable = lambda raw: Path(os.path.expanduser(raw))) -> dict:
    out = {}
    for name, c in cfg["channels"].items():
        if cfg["mode"] == "mock" or c["type"] == "mock":
            out[name] = MockAdapter(name)
        elif c["type"] == "telegram":
            out[name] = TelegramAdapter(name, *telegram_creds(cfg, name), opener)
        elif c["type"] == "file":
            out[name] = FileAdapter(name, resolve(c["path"]))
    return out
