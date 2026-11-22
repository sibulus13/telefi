"""The waiting list: everything that is stopped until the owner acts, one row per key, with its age.

    spool/state/waiting.json   the open items, keyed by the envelope's dedup_key (or source:title)
    waiting-closed.jsonl       one line per closed item, beside the outbox

A sender opens an item with `"waiting": "open"` on an envelope and closes it with `"waiting": "closed"`
and the same key. A repeat keeps the one item and counts how often it was seen, also when the message
itself was deduplicated. Views (a dashboard, a notes database, the batch) read the list; none keeps its own.

    python -m telefi waiting list [--json]     print the list (--json: the telefi.waiting.v1 contract)
    python -m telefi waiting open --source S --title T [--action A] [--link URL] [--key K] [--since ISO]
    python -m telefi waiting close KEY [--reason TEXT]
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from telefi import core, schema   # import cycle: core.drain calls observe(), and this module uses core's state files

STATE_FILE = "waiting.json"
CLOSED_FILE = "waiting-closed.jsonl"
LOCK_FILE = "waiting.lock"
SCHEMA_NAME = "waiting.v1"
SCHEMA_ID = "telefi.waiting.v1"
OPEN, CLOSED = "open", "closed"
LOCK_WAIT_S = 5.0           # the hub and a command-line call may change the list at the same moment
LOCK_POLL_S = 0.05
LOCK_STALE_S = 30.0         # a lock left by a process that died is taken over after this long


def closed_path(root: Path) -> Path:
    return root.parent / CLOSED_FILE


def key_of(env: dict) -> str:
    """What makes two envelopes the same waiting item."""
    return env.get("dedup_key") or f"{env['source']}:{env['title']}"


def _limits() -> dict[str, int]:
    """Each text field's longest length, from the published schema, so the list always validates against it."""
    fields = schema.load(SCHEMA_NAME)["properties"]["items"]["items"]["properties"]
    return {name: spec["maxLength"] for name, spec in fields.items() if "maxLength" in spec}


@contextmanager
def _locked(root: Path) -> Iterator[None]:
    """One writer at a time: read-modify-write of the list is not atomic by itself. Raises TimeoutError
    after LOCK_WAIT_S rather than waiting forever; a lock older than LOCK_STALE_S is taken over."""
    lock = root / "state" / LOCK_FILE
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_WAIT_S
    while True:
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"the waiting list is locked by another process ({lock})") from None
            time.sleep(LOCK_POLL_S)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def items(root: Path) -> list[dict]:
    """The open items, the longest-waiting first."""
    return sorted(core.read_state(root, STATE_FILE).values(), key=lambda i: i["opened_at"])


def listing(root: Path) -> dict:
    """The list in its published shape (schemas/waiting.v1.json)."""
    return {"schema": SCHEMA_ID, "at": core.now_iso(), "items": items(root)}


def open_item(root: Path, *, key: str, source: str, title: str, action: str = "", link: str = "",
              since: str = "") -> dict:
    """Open the item, or note that an open one was seen again. `since` (an ISO time) is when it started
    waiting; it only applies when the item is new. Raises ValueError when `since` is not a time."""
    if since and core._when(since) is None:
        raise ValueError(f"since: {since!r} is not an ISO time")
    cut = _limits()
    with _locked(root):
        state = core.read_state(root, STATE_FILE)
        now = core.now_iso()
        key = key[:cut["key"]]
        known = state.get(key)
        item = {"key": key, "source": source[:cut["source"]], "title": title[:cut["title"]],
                "action": action[:cut["action"]], "link": link if len(link) <= cut["link"] else "",
                "opened_at": known["opened_at"] if known else since or now, "last_seen_at": now,
                "seen": known["seen"] + 1 if known else 1}
        state[key] = item
        core.write_state(root, STATE_FILE, state)
    return item


def close_item(root: Path, key: str, *, reason: str = "") -> dict | None:
    """Take the item off the list and record it as closed. None when no such item is open."""
    with _locked(root):
        state = core.read_state(root, STATE_FILE)
        item = state.pop(key, None)
        if item is None:
            return None
        # the record first: after a crash, a second closed line does less harm than an item that vanished unrecorded
        core.append_jsonl(closed_path(root), {**item, "closed_at": core.now_iso(), "reason": reason})
        core.write_state(root, STATE_FILE, state)
    return item


def observe(root: Path, env: dict) -> None:
    """Apply an accepted envelope's `waiting` field to the list; an envelope without one changes nothing."""
    state = env.get("waiting")
    if state == OPEN:
        links = env.get("evidence", {}).get("links", [])
        made = env.get("created_at", "")
        open_item(root, key=key_of(env), source=env["source"], title=env["title"],
                  action=env.get("next_step", ""), link=links[0]["url"] if links else "",
                  since=made if core._when(made) else "")
    elif state == CLOSED:
        close_item(root, key_of(env), reason="the sender reported it resolved")


def _day(iso: str) -> str:
    return datetime.fromisoformat(iso).date().isoformat()


def render(root: Path) -> str:
    """The list as plain lines for a terminal."""
    rows = items(root)
    if not rows:
        return "nothing is waiting"
    return "\n".join(f"{_day(i['opened_at'])}  {i['source']}: {i['title']}"
                     + (f"\n            do: {i['action']}" if i["action"] else "")
                     + (f"\n            {i['link']}" if i["link"] else "") for i in rows)
