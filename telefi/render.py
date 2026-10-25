"""The message format standard: glance first, details collapsed, links and actions as buttons.

    [ERROR] ci · Build #43 gate failed                   <- headline: severity, source, what happened
    No API key in the fresh checkout                      <- insight: why it matters / what next (italic)
    > details (body, ref, log path), collapsed            <- expandable quote; tap to open
    [ Card ] [ PR #46 ]                                   <- evidence links: buttons that open the URL
    [ Ack ] [ Retry ] [ Retry with note ]                 <- actions: only while inbound is enabled

At a glance the owner reads two lines; everything else is one tap away.
"""
from __future__ import annotations

import html
import re

CALLBACK_MAX = 64              # Telegram's callback_data limit, bytes
CALLBACK_PREFIX = "h"          # hub-made buttons; anything else belongs to a legacy handler
CALLBACK_SEP = "|"
TELEGRAM_MAX_TEXT = 3900
BUTTONS_PER_ROW = 3
SEVERITY_TAG = {"info": "INFO", "warn": "WARN", "error": "ERROR", "critical": "CRITICAL"}
_TAG = re.compile(r"<[^>]+>")


def callback_data(namespace: str, action_id: str, envelope_id: str) -> str:
    return CALLBACK_SEP.join((CALLBACK_PREFIX, namespace, action_id, envelope_id))[:CALLBACK_MAX]


def parse_callback(data: str) -> tuple[str, str, str] | None:
    parts = (data or "").split(CALLBACK_SEP)
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX:
        return None
    return parts[1], parts[2], parts[3]


def _details(env: dict) -> str:
    e = html.escape
    lines = [e(env["body"])] if env.get("body") else []
    ev = env.get("evidence") or {}
    if ev.get("ref"):
        lines.append(f"ref: <code>{e(ev['ref'])}</code>")
    if ev.get("log_path"):
        lines.append(f"log: <code>{e(ev['log_path'])}</code>")
    return "\n".join(lines)


def _rows(buttons: list[dict]) -> list[list[dict]]:
    return [buttons[i:i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)]


def render_telegram(env: dict, *, buttons: bool) -> dict:
    """The exact text + keyboard the owner sees (HTML parse mode, every field escaped)."""
    e = html.escape
    parts = [f"<b>[{SEVERITY_TAG[env['severity']]}] {e(env['source'])}</b> · {e(env['title'])}"]
    if env.get("insight"):
        parts.append(f"<i>{e(env['insight'])}</i>")
    details = _details(env)
    if details:
        parts.append(f"<blockquote expandable>{details}</blockquote>")
    text = "\n".join(parts)[:TELEGRAM_MAX_TEXT]
    rows = _rows([{"text": link["label"], "url": link["url"]} for link in (env.get("evidence") or {}).get("links", [])])
    if buttons and env.get("actions"):
        rows += _rows([{"text": a["label"], "callback_data": callback_data(a["namespace"], a["id"], env["id"])}
                       for a in env["actions"]])
    return {"text": text, "reply_markup": {"inline_keyboard": rows} if rows else None}


def render_plain(env: dict, *, buttons: bool) -> str:
    """Terminal preview: what the owner sees collapsed, then what expanding reveals."""
    r = render_telegram(env, buttons=buttons)
    quote = re.search(r"<blockquote expandable>(.*?)</blockquote>", r["text"], re.S)
    glance = r["text"][:quote.start()] if quote else r["text"]
    out = [html.unescape(_TAG.sub("", glance)).rstrip()]
    if quote:
        body = html.unescape(_TAG.sub("", quote.group(1)))
        out.append("  ▸ details (tap to expand):\n" + "\n".join(f"    | {l}" for l in body.splitlines()))
    if r["reply_markup"]:
        for row in r["reply_markup"]["inline_keyboard"]:
            out.append("  " + "  ".join(f"[ {b['text']}{' ↗' if 'url' in b else ''} ]" for b in row))
    return "\n".join(out)
