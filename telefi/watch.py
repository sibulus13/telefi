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
SUBJECT_MAX = 90                            # characters of a lost message's title quoted in the notice
# key: title, context, what to do. Written for the reader of the phone, not for the person debugging the hub.
ISSUES = {
    "serve-error": ("TeleFi keeps failing to check for messages",
                    "TeleFi delivers these alerts and your replies. It failed {count} times in a row, so replies may be slow until it recovers.",
                    "If replies stay slow for an hour, restart the TeleFi service."),
    "poll-failures": ("TeleFi cannot reach Telegram",
                      "It failed {count} times in a row to fetch your replies. It keeps trying, and nothing you sent is lost.",
                      "If this lasts an hour, check the computer's internet connection."),
    "telegram-down": ("Telegram sends have failed for 2 hours",
                      "Alerts are waiting in a queue and will go out once Telegram works again.",
                      "Check the computer's internet connection, then that the bot token is still valid."),
    "spool-quarantined": ("A message from {sender} never reached you",
                          "It was badly formed, so TeleFi set it aside. Its subject was: {subject}",
                          "Ask a session to read the note left beside it and fix the sender."),
    "spool-gave-up": ("A message from {sender} could not be delivered",
                      "TeleFi kept trying, then gave up, so you never saw it. Its subject was: {subject}",
                      "Check the computer's internet connection. If it still matters, ask a session to resend it."),
    "digest-item-quarantined": ("A held message was damaged",
                                "One message waiting for the next summary could not be read, so it will not arrive.",
                                "Ask a session to look at what was set aside and fix the sender."),
    "digest-send-failed": ("The scheduled summary failed to send",
                           "TeleFi tried {count} times today. The held messages are safe and it keeps retrying.",
                           "If no summary arrives by the next window, check the computer's internet connection."),
}
TELEGRAM_DOWN_AFTER = timedelta(hours=2)
DETAIL_MAX = 300
RAISED_KEEP_DAYS = 2                        # older once-a-day marks are pruned


def issue_envelope(key: str, *, detail: str = "", severity: str = "error", deliver_now: bool = False, day: str = "",
                   **facts) -> dict:
    """One of the hub's own messages. The reader gets plain words; the technical detail stays collapsed."""
    title, insight, next_step = (text.format(**facts) for text in ISSUES[key])
    return core.envelope(source=HUB_SOURCE, kind="failure", severity=severity, title=title, insight=insight,
                         next_step=next_step, body=detail, dedup_key=f"{HUB_SOURCE}:{key}:{day}",
                         delivery=core.LANE_NOW if deliver_now else None)


def raise_known(root: Path, key: str, *, detail: str = "", severity: str = "error", now: datetime | None = None,
                deliver_now: bool = False, **facts) -> bool:
    """Publish one of ISSUES, at most once a day; False when it already fired today."""
    day = (now or core.clock()).date().isoformat()
    env = issue_envelope(key, detail=detail, severity=severity, deliver_now=deliver_now, day=day, **facts)
    return _once_a_day(root, key, env, now)


def raise_issue(root: Path, key: str, title: str, insight: str, *, severity: str = "error",
                now: datetime | None = None, deliver_now: bool = False) -> bool:
    """Publish one failure envelope from the hub; False when this key already fired today.
    `deliver_now` skips the batch lane (for an issue about the batch lane itself)."""
    day = (now or core.clock()).date().isoformat()
    env = core.envelope(source=HUB_SOURCE, kind="failure", severity=severity, title=title, insight=insight,
                        dedup_key=f"{HUB_SOURCE}:{key}:{day}", delivery=core.LANE_NOW if deliver_now else None)
    return _once_a_day(root, key, env, now)


def _once_a_day(root: Path, key: str, env: dict, now: datetime | None) -> bool:
    now = now or core.clock()
    day = now.date().isoformat()
    state = core.read_state(root, STATE_FILE)
    oldest = (now.date() - timedelta(days=RAISED_KEEP_DAYS)).isoformat()
    raised = {k: d for k, d in state.get("raised", {}).items() if d >= oldest}
    state["raised"] = raised
    if raised.get(key) == day:
        return False
    core.publish(env, spool=root)
    raised[key] = day
    core.write_state(root, STATE_FILE, state)
    return True


def poll_failed(root: Path, *, streak: int, transient: bool, error: str, now: datetime | None = None) -> None:
    """A failed serve round. A round that recovers is not news: only the 5th failure in a row raises."""
    if streak >= POLL_FAILURE_LIMIT:
        raise_known(root, "poll-failures" if transient else "serve-error", detail=error[:DETAIL_MAX], count=streak, now=now)


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
        raise_known(root, "telegram-down", severity="critical", now=now)


def _one_line(text: object) -> str:
    return " ".join(str(text).split())[:SUBJECT_MAX]


def after_drain(root: Path, cfg: dict, records: list[dict], *, now: datetime | None = None) -> None:
    for rec in records:
        env = rec["envelope"]
        if (rec.get("rejected") or rec.get("gave_up")) and env.get("source") != HUB_SOURCE:    # never loop on our own
            raise_known(root, "spool-quarantined" if rec.get("rejected") else "spool-gave-up",
                        detail=f"{env.get('id')} is in spool/failed; see its .error.txt", now=now,
                        sender=_one_line(env.get("source") or "a sender"), subject=_one_line(env.get("title")))
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
