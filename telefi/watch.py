"""TeleFi watches itself: the hub raises its own envelopes and writes a heartbeat.

    raise_issue    source=telefi kind=failure, severity error (batch lane) or critical; once per key per day
    poll_failed    5 consecutive poll failures, or any non-transient serve error
    note_telegram  Telegram sends failing for 2 h in a row -> critical (a success resets the clock and
                   requeues criticals that were quarantined after failed deliveries)
    after_drain    feeds the two above from a drain: quarantined spool items, Telegram send results
    heartbeat      <TELEFI_HOME>/heartbeat.json, rewritten every serve round:
                     {"at": ISO time, "pid": n, "telegram_ok": bool, "telegram_down_since": ISO time | null}
                   An external watcher treats an `at` older than 30 min as "telefi-down" (hub dead),
                   and telegram_ok=false for over 2 h as "telefi-telegram-down" (hub alive, Telegram not).

State lives in spool/state/self_watch.json: {"raised": {key: day}, "telegram_down_since": ISO time,
"telegram_last_attempt": ISO time}.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from telefi import core
from telefi.render import HUB_SOURCE

STATE_FILE = "self_watch.json"
HEARTBEAT_FILE = "heartbeat.json"
POLL_FAILURE_LIMIT = 5                      # consecutive failed rounds
TELEGRAM_DOWN_AFTER = timedelta(hours=2)
DETAIL_MAX = 300
RAISED_KEEP_DAYS = 2                        # older once-a-day marks are pruned


def raise_issue(root: Path, key: str, title: str, insight: str, *, severity: str = "error",
                now: datetime | None = None, deliver_now: bool = False) -> bool:
    """Publish one failure envelope from the hub; False when this key already fired today.
    `deliver_now` skips the batch lane (for an issue about the batch lane itself)."""
    now = now or core.clock()
    day = now.date().isoformat()
    state = core.read_state(root, STATE_FILE)
    oldest = (now.date() - timedelta(days=RAISED_KEEP_DAYS)).isoformat()
    raised = {k: d for k, d in state.get("raised", {}).items() if d >= oldest}
    state["raised"] = raised
    if raised.get(key) == day:
        return False
    core.publish(core.envelope(source=HUB_SOURCE, kind="failure", severity=severity, title=title, insight=insight,
                               dedup_key=f"{HUB_SOURCE}:{key}:{day}",
                               delivery=core.LANE_NOW if deliver_now else None), spool=root)
    raised[key] = day
    core.write_state(root, STATE_FILE, state)
    return True


def poll_failed(root: Path, *, streak: int, transient: bool, error: str, now: datetime | None = None) -> None:
    """A failed serve round. Transient poll hiccups count; the 5th in a row raises. Anything else raises at once."""
    if not transient:
        raise_issue(root, "serve-error", "TeleFi hub error", error[:DETAIL_MAX], now=now)
    elif streak >= POLL_FAILURE_LIMIT:
        raise_issue(root, "poll-failures", "TeleFi cannot reach Telegram",
                    f"{streak} failed polls in a row: {error}"[:DETAIL_MAX], now=now)


def note_telegram(root: Path, *, ok: bool, now: datetime | None = None) -> None:
    now = now or core.clock()
    state = core.read_state(root, STATE_FILE)
    if ok:
        state.pop("telegram_down_since", None)
        state["telegram_last_attempt"] = now.isoformat(timespec="seconds")
        core.write_state(root, STATE_FILE, state)
        core.requeue_failed_criticals(root)             # Telegram works again: criticals given up on get another go
        return
    last = state.get("telegram_last_attempt")
    if last and now - datetime.fromisoformat(last) > TELEGRAM_DOWN_AFTER:       # a gap with no attempt: a new streak
        state.pop("telegram_down_since", None)
    state["telegram_last_attempt"] = now.isoformat(timespec="seconds")
    since = state.setdefault("telegram_down_since", now.isoformat(timespec="seconds"))
    core.write_state(root, STATE_FILE, state)
    if now - datetime.fromisoformat(since) >= TELEGRAM_DOWN_AFTER:
        raise_issue(root, "telegram-down", "Telegram sends have failed for 2 hours",
                    "Messages are queued and retried; check the network and the bot token.", severity="critical", now=now)


def after_drain(root: Path, cfg: dict, records: list[dict], *, now: datetime | None = None) -> None:
    for rec in records:
        env = rec["envelope"]
        if (rec.get("rejected") or rec.get("gave_up")) and env.get("source") != HUB_SOURCE:    # never loop on our own
            raise_issue(root, "spool-quarantined", "A message was quarantined",
                        f"{env.get('id')} is in spool/failed; see its .error.txt", now=now)
        for d in rec["deliveries"]:
            if core.is_telegram(cfg, d["channel"]):
                note_telegram(root, ok=d["ok"], now=now)


def heartbeat(path: Path, *, root: Path, now: datetime | None = None) -> None:
    down_since = core.read_state(root, STATE_FILE).get("telegram_down_since")
    beat = {"at": (now or core.clock()).isoformat(timespec="seconds"), "pid": os.getpid(),
            "telegram_ok": down_since is None, "telegram_down_since": down_since}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(beat), encoding="utf-8", newline="\n")
    os.replace(tmp, path)
