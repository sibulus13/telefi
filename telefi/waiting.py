"""The waiting list: everything that is stopped until the owner acts, one row per key, with its age.

    spool/state/waiting.json   the open items, keyed by the envelope's dedup_key (or source:title)
    waiting-closed.jsonl       one line per closed item, beside the outbox

A sender opens an item with `"waiting": "open"` on an envelope and closes it with `"waiting": "closed"`
and the same key. A repeat keeps the one item and counts how often it was seen, also when the message
itself was deduplicated. Views (a dashboard, a notes database, the batch) read the list; none keeps its own.

    python -m telefi waiting list [--json]     print the list (--json: the telefi.waiting.v1 contract)
    python -m telefi waiting open --source S --title T --action A [--link URL] [--key K]
    python -m telefi waiting close KEY [--reason TEXT]
"""
from __future__ import annotations

from pathlib import Path

from telefi import core

STATE_FILE = "waiting.json"
CLOSED_FILE = "waiting-closed.jsonl"
SCHEMA_ID = "telefi.waiting.v1"
OPEN, CLOSED = "open", "closed"


def closed_path(root: Path) -> Path:
    return root.parent / CLOSED_FILE


def key_of(env: dict) -> str:
    """What makes two envelopes the same waiting item."""
    return env.get("dedup_key") or f"{env['source']}:{env['title']}"


def items(root: Path) -> list[dict]:
    """The open items, the longest-waiting first."""
    return sorted(core.read_state(root, STATE_FILE).values(), key=lambda i: i["opened_at"])


def listing(root: Path) -> dict:
    """The list in its published shape (schemas/waiting.v1.json)."""
    return {"schema": SCHEMA_ID, "at": core.now_iso(), "items": items(root)}


def open_item(root: Path, *, key: str, source: str, title: str, action: str = "", link: str = "") -> dict:
    """Open the item, or note that an open one was seen again. Returns the item."""
    state = core.read_state(root, STATE_FILE)
    now = core.now_iso()
    known = state.get(key)
    item = {"key": key, "source": source, "title": title, "action": action, "link": link,
            "opened_at": known["opened_at"] if known else now, "last_seen_at": now,
            "seen": known["seen"] + 1 if known else 1}
    state[key] = item
    core.write_state(root, STATE_FILE, state)
    return item


def close_item(root: Path, key: str, *, reason: str = "") -> dict | None:
    """Take the item off the list and record it as closed. None when no such item is open."""
    state = core.read_state(root, STATE_FILE)
    item = state.pop(key, None)
    if item is None:
        return None
    core.write_state(root, STATE_FILE, state)
    core.append_jsonl(closed_path(root), {**item, "closed_at": core.now_iso(), "reason": reason})
    return item


def observe(root: Path, env: dict) -> None:
    """Apply an accepted envelope's `waiting` field to the list; an envelope without one changes nothing."""
    state = env.get("waiting")
    if state == OPEN:
        links = env.get("evidence", {}).get("links", [])
        open_item(root, key=key_of(env), source=env["source"], title=env["title"],
                  action=env.get("next_step", ""), link=links[0]["url"] if links else "")
    elif state == CLOSED:
        close_item(root, key_of(env), reason="the sender reported it resolved")


def render(root: Path) -> str:
    """The list as plain lines for a terminal."""
    rows = items(root)
    if not rows:
        return "nothing is waiting"
    return "\n".join(f"{i['opened_at'][:10]}  {i['source']}: {i['title']}"
                     + (f"\n            do: {i['action']}" if i["action"] else "")
                     + (f"\n            {i['link']}" if i["link"] else "") for i in rows)
