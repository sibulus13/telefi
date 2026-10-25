"""The command registry: one commands.v1 manifest per app -> /help, /list, the '/' menu, routing.

    /help            every app, every command, usage + one line
    /help <app>      one app
    /list            one line per app: its verbs
    /<app> <verb> x  routed to the app's sink (rewritten to legacy_text for an app's old bare form)
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from telefi import schema

HUB_MANIFEST: dict = {
    "schema": "telefi.commands.v1", "app": "hub", "summary": "the messaging hub itself", "sink": "hub",
    "commands": [
        {"verb": "help", "usage": "/help [app]", "summary": "every command, or one app's"},
        {"verb": "list", "usage": "/list", "summary": "apps and their commands, one line each"},
        {"verb": "ack", "usage": "/hub ack", "summary": "stop re-alerts for the latest failure"},
    ],
}
TOP_LEVEL = ("help", "list")             # hub verbs that also work as bare commands


def load_manifests(paths: list[str]) -> dict[str, dict]:
    """app -> manifest, the hub's own first. An invalid manifest raises: a typo must not silently
    drop an app's commands from /help."""
    out = {"hub": HUB_MANIFEST}
    for raw in paths:
        p = Path(os.path.expanduser(raw))
        m = json.loads(p.read_text(encoding="utf-8"))
        bad = schema.validate(m, "commands.v1")
        if bad:
            raise ValueError(f"commands manifest {p}: " + "; ".join(bad))
        out[m["app"]] = m
    return out


def verb(manifests: dict, app: str, name: str) -> dict | None:
    return next((c for c in manifests.get(app, {}).get("commands", []) if c["verb"] == name), None)


def help_text(manifests: dict, app: str | None = None) -> str:
    apps = [manifests[app]] if app in manifests else list(manifests.values())
    blocks = []
    for m in apps:
        lines = [f"{m['app']}: {m['summary']}"] + [f"  {c['usage']}  -  {c['summary']}" for c in m["commands"]]
        blocks.append("\n".join(lines))
    head = "" if app in manifests else ("Unknown app: " + app + "\n\n" if app else "")
    return head + "\n\n".join(blocks)


def list_text(manifests: dict) -> str:
    return "\n".join(f"/{m['app']}  " + " · ".join(c["verb"] for c in m["commands"]) for m in manifests.values())


def bot_commands(manifests: dict) -> list[dict]:
    """Telegram's '/' menu (setMyCommands): help, list, then one entry per app."""
    menu = [{"command": "help", "description": "every command, or /help <app>"},
            {"command": "list", "description": "apps and their commands"}]
    return menu + [{"command": m["app"], "description": m["summary"][:60]} for m in manifests.values() if m["app"] != "hub"]
