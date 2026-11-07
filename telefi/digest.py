"""The batch lane: envelopes the hub holds (route delivery=batch, a sender's delivery=batch|digest, or quiet
hours) wait in spool/digest/ and leave as ONE silent Telegram message per window, at/after each of
digest.windows (default 08:00 12:00 16:00 20:00) in digest.timezone (default: local). An empty window sends
nothing. A window with one item sends that item's normal message; with several, the batched format
(render.batch_html).

    spool/digest/<id>.json                collected (drain moves them here instead of sending)
    spool/digest/sending/<date>/<id>.json claimed by a sender that has not finished (a crash leaves them here)
    spool/digest/sent/<date>/<id>.json    summarised
    spool/digest/bad/                     unreadable or invalid files, quarantined with a log line
    spool/state/digest.json               {"last_window": "<date>T<HH:MM>", "failures", "next"}: one send per
                                          window, retry backoff

Safety: every sender (a serve round, `digest --send`) holds spool/state/digest.lock around claim -> send ->
release; it claims items with os.replace into sending/, sends, then moves them to sent/ and marks the window.
Any failure or exception puts the claimed items back in the queue and records a backoff. After a crash
`recover()` (once at `serve` start, only while holding the lock) requeues what a dead sender left in
sending/: a rare duplicate is accepted over a lost message.
"""
from __future__ import annotations

import ctypes
import json
import os
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path

from telefi import core, messages, schema, watch
from telefi.adapters import build_adapters
from telefi.render import HUB_SOURCE, PRIVATE_HTML, ROLLUP_SECTIONS, batch_html, batch_item, source_label

DEFAULT_WINDOWS = ("08:00", "12:00", "16:00", "20:00")
DEFAULT_RESEARCH_SOURCES = ("research",)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
LOOKBACK_DAYS = 7               # a window missed on a sending day is found up to a week later
STATE_FILE = "digest.json"
LOCK_FILE = "digest.lock"
LOCK_STALE_S = 600
LOCK_WRITE_GRACE_S = 5                                  # an unreadable lock younger than this is mid-write
WIN_QUERY_LIMITED = 0x1000      # PROCESS_QUERY_LIMITED_INFORMATION
SENT_DIR, SENDING_DIR, BAD_DIR = "sent", "sending", "bad"
BACKOFF_MIN = (5, 15, 60)       # wait after the 1st, 2nd, 3rd+ failed send
DIGEST_FAILURE_LIMIT = 3        # failed sends in a day before the hub raises its own issue
OLD_DAY_DONE = "T99:99"         # a pre-windows `last_date` marks that whole day done


def windows(cfg: dict) -> list[time]:
    """The window times: digest.windows, else the single digest.at, else the defaults; sorted."""
    d = cfg.get("digest", {})
    raw = d.get("windows") or ([d["at"]] if d.get("at") else DEFAULT_WINDOWS)
    return sorted(core.parse_hhmm(t) for t in raw)


def sends_on(cfg: dict, day: datetime) -> bool:
    """True when windows run on this date: digest.days (e.g. ["Mon", "Thu"]), every day when unset."""
    days = cfg.get("digest", {}).get("days")
    return not days or WEEKDAYS[day.weekday()] in days


def window_key(cfg: dict, local: datetime, *, first_if_none: bool = False) -> str | None:
    """"<date>T<HH:MM>" of the latest window at or before `local`; None before today's first window or on a
    day digest.days leaves out (or today's first window when `first_if_none`: a manual send counts as it)."""
    times = windows(cfg)
    if not sends_on(cfg, local) and not first_if_none:
        return None
    passed = [t for t in times if t <= local.time()] or (times[:1] if first_if_none else [])
    return f"{local.date().isoformat()}T{passed[-1]:%H:%M}" if passed else None


def _queue(root: Path) -> Path:
    return root / core.DIGEST_DIR


def _log(root: Path, event: str, **fields) -> None:
    """hub.log lives beside the spool (TELEFI_HOME in a normal install), like outbox.jsonl."""
    core.append_jsonl(root.parent / "hub.log", {"at": core.now_iso(), "event": event, **fields})


def collected(root: Path) -> list[dict]:
    """The waiting envelopes, oldest first. An unreadable or invalid file is quarantined to bad/ and logged."""
    items = []
    for p in sorted(_queue(root).glob("*.json")):
        try:
            env = json.loads(p.read_text(encoding="utf-8"))
            bad = schema.validate(env, "envelope.v1")
        except FileNotFoundError:                       # another process claimed it
            continue
        except ValueError as ex:
            bad = [f"unreadable JSON: {ex}"]
        if bad:
            core._move(p, _queue(root) / BAD_DIR)
            _log(root, "digest item quarantined", file=p.name, errors=bad[:3])
            watch.raise_known(root, "digest-item-quarantined", detail=f"{p.name} is in spool/digest/bad", now=core.clock())
            continue
        items.append(env)
    return sorted(items, key=lambda e: (e["created_at"], e["id"]))


def claim(root: Path, items: list[dict], day: str) -> list[dict]:
    """Take ownership of items by moving them into sending/<day>/; those another process took first are skipped."""
    folder = _queue(root) / SENDING_DIR / day
    folder.mkdir(parents=True, exist_ok=True)
    mine = []
    for it in items:
        try:
            os.replace(_queue(root) / f"{it['id']}.json", folder / f"{it['id']}.json")
        except FileNotFoundError:
            continue
        mine.append(it)
    return mine


def _release(root: Path, items: list[dict], day: str, to: Path) -> None:
    for it in items:
        try:
            core._move(_queue(root) / SENDING_DIR / day / f"{it['id']}.json", to)
        except FileNotFoundError:                       # removed behind our back: log it, finish the rest
            _log(root, "digest item vanished", id=it["id"])


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":                                 # platform-only: os.kill(pid, 0) would terminate it on Windows
        handle = ctypes.windll.kernel32.OpenProcess(WIN_QUERY_LIMITED, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
        return bool(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lock_is_stale(path: Path) -> bool:
    try:
        held = json.loads(path.read_text(encoding="utf-8"))
        age = (core.clock() - core._when(held["at"])).total_seconds()
        return age > LOCK_STALE_S or not _pid_alive(int(held["pid"]))
    except FileNotFoundError:
        return True
    except (ValueError, KeyError, TypeError):           # unreadable: a holder may be mid-write, so young = held
        try:
            return datetime.now().timestamp() - path.stat().st_mtime > LOCK_WRITE_GRACE_S
        except FileNotFoundError:
            return True


@contextmanager
def sender_lock(root: Path):
    """Yield True when this process now owns the digest lock (spool/state/digest.lock), else False."""
    path = root / "state" / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    got = False
    for _ in range(2):                                  # second pass: after clearing a stale lock
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not _lock_is_stale(path):
                break
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "at": core.now_iso()}, f)
        got = True
        break
    try:
        yield got
    finally:
        if got:
            path.unlink(missing_ok=True)


def recover(root: Path) -> int:
    """Run once at serve start: items a crashed sender left in sending/ go back to the queue (sent again).
    Does nothing while another sender holds the lock (it may be mid-send)."""
    n = 0
    with sender_lock(root) as got:
        if not got:
            return 0
        for p in (_queue(root) / SENDING_DIR).glob("*/*.json"):
            core._move(p, _queue(root))
            n += 1
    if n:
        _log(root, "digest recovered", items=n)
    return n


def _collapse(items: list[dict]) -> list[tuple[dict, int]]:
    """Identical (source, dedup_key) entries collapse to one row: the latest content, the first one's place, a count."""
    seen: dict[object, tuple[dict, int]] = {}
    for it in items:
        key = (it["source"], it["dedup_key"]) if it.get("dedup_key") else it["id"]
        seen[key] = (it, seen.get(key, (None, 0))[1] + 1)
    return list(seen.values())


def section_of(env: dict, research_sources: tuple[str, ...] | list[str]) -> str:
    """Which batch section an item belongs to: errors, research, reviews or receipts."""
    view = messages.view_of(env)
    if view:
        return view["section"]
    if env["kind"] == "failure" or env["severity"] in ("error", "critical"):
        return "errors"
    if env["kind"] == "result" and env["source"] in research_sources:
        return "research"
    if env["kind"] in ("review", "blocker") or env["severity"] == "warn":
        return "reviews"
    return "receipts"


def _failed(env: dict) -> bool:
    return env["kind"] == "failure" or env["severity"] in ("error", "critical")


def project_counts(items: list[dict]) -> list[str]:
    """One line per source: how many of its updates went fine and how many failed."""
    lines = []
    for source in sorted({i["source"] for i in items}):
        mine = [i for i in items if i["source"] == source]
        failed = sum(_failed(i) for i in mine)
        lines.append(f"{source_label(source)}: {len(mine) - failed} ok, {failed} failed")
    return lines


def _collapse_runs(items: list[dict]) -> list[tuple[dict, int]]:
    """For a rollup, repeats of one story are one row even with unique dedup keys: a key per run, a title per story."""
    seen: dict[object, tuple[dict, int]] = {}
    for it in items:
        key = (it["source"], it["title"])
        seen[key] = (it, seen.get(key, (None, 0))[1] + 1)
    return list(seen.values())


def build(items: list[dict], when: datetime, label: str | None = None,
          research_sources: tuple[str, ...] | list[str] = DEFAULT_RESEARCH_SOURCES, *, rollup: bool = False) -> dict:
    """What to send for a window: the item itself when there is one, else a batch envelope whose private
    `_html` is the rendered message (within Telegram's limit; see render.batch_html). A rollup adds counts per
    project, merges repeated runs and leads with what needs the owner."""
    entries = _collapse_runs(items) if rollup else _collapse(items)
    if len(entries) == 1 and entries[0][1] == 1:
        return entries[0][0]
    label = label or f"{when:%H:%M}"
    batch = [batch_item(it, n, section_of(it, research_sources)) for it, n in entries]
    env = core.envelope(source=HUB_SOURCE, kind="digest", severity="info",
                        title=f"{sum(n for _, n in entries)} updates · {label}", silent=True)
    env[PRIVATE_HTML] = batch_html(batch, label, ROLLUP_SECTIONS, project_counts(items)) if rollup else batch_html(batch, label)
    return env


def _research_sources(cfg: dict) -> list[str]:
    return cfg.get("digest", {}).get("research_sources", DEFAULT_RESEARCH_SOURCES)


def preview(cfg: dict, root: Path, now: datetime | None = None) -> dict | None:
    items = collected(root)
    local = core.local_now(cfg, now)
    key = window_key(cfg, local, first_if_none=True)
    return build(items, local, key[-5:], _research_sources(cfg), rollup=_rollup(cfg)) if items else None


def _rollup(cfg: dict) -> bool:
    return bool(cfg.get("digest", {}).get("rollup"))


def _last_window(state: dict) -> str:
    return state.get("last_window") or (f"{state['last_date']}{OLD_DAY_DONE}" if "last_date" in state else "")


def pending_window(cfg: dict, root: Path, now: datetime | None = None) -> str | None:
    """The window key to handle now: one has passed that was not yet handled, outside quiet hours and any
    retry backoff. None otherwise."""
    local = core.local_now(cfg, now)
    state = core.read_state(root, STATE_FILE)
    key = window_key(cfg, local)
    if key is None and state:          # before today's first window: the last sending day's may be unsent
        last = next(d for n in range(1, LOOKBACK_DAYS + 1) if sends_on(cfg, d := local - timedelta(days=n)))
        key = f"{last.date().isoformat()}T{windows(cfg)[-1]:%H:%M}"
    retry_at = core._when(state["next"]) if state.get("next") else None
    ready = (key is not None and not core.in_quiet_hours(cfg, local) and key > _last_window(state)
             and (retry_at is None or local >= retry_at))
    return key if ready else None


def due(cfg: dict, root: Path, now: datetime | None = None) -> bool:
    return pending_window(cfg, root, now) is not None


def _no_route_result() -> dict:
    return {"sent": False, "no_route": True, "deliveries": [], "error": "no Telegram channel in the route of kind digest"}


def send(cfg: dict, *, adapters: dict | None = None, spool: Path | None = None, now: datetime | None = None,
         window: str | None = None) -> dict | None:
    """Send the held items now. None when nothing is collected. Otherwise a record with `sent`: true only when
    a Telegram channel accepted the message (then the items move to sent/<date>/ and the window is marked done).
    On any non-success, including an exception, the items return to the queue and a backoff is recorded.
    Only Telegram channels are used: items already reached the file channel one by one.
    `window` is the key to mark (the one due() evaluated); a manual send marks the current window."""
    root = spool or core.spool_root(cfg)
    local = core.local_now(cfg, now)
    day = local.date().isoformat()
    key = window or window_key(cfg, local, first_if_none=True)
    targets = core.digest_channels(cfg)
    if not targets:
        return _no_route_result()
    with sender_lock(root) as got:
        if not got:
            return {"sent": False, "deliveries": [], "error": "another digest send is in progress"}
        items = claim(root, collected(root), day)
        if not items:
            return None
        rec, errors = None, ["interrupted"]
        try:
            env = build(items, local, key[-5:], _research_sources(cfg), rollup=_rollup(cfg))
            bad = schema.validate({k: v for k, v in env.items() if k != PRIVATE_HTML}, "envelope.v1")
            if bad:
                raise ValueError("digest envelope: " + "; ".join(bad))
            adapters = adapters if adapters is not None else build_adapters(cfg, resolve=core.resolve)
            buttons = core.button_policy(cfg)
            deliveries = []
            for name in targets:
                a = adapters.get(name)
                res = a.send(env, buttons=buttons) if a else {"ok": False, "error": f"no channel named {name}"}
                deliveries.append({"channel": name, **res})
                watch.note_telegram(root, ok=res["ok"], now=local)
            sent = any(d["ok"] for d in deliveries)
            rec = {"at": core.now_iso(), "envelope": env, "deliveries": deliveries,
                   "digest_items": [i["id"] for i in items], "sent": sent}
            errors = [d.get("error") for d in deliveries if not d["ok"]]
        finally:
            if rec and rec["sent"]:
                _release(root, items, day, _queue(root) / SENT_DIR / day)
                core.write_state(root, STATE_FILE, {"last_window": key})
            else:
                _release(root, items, day, _queue(root))
                _record_failure(root, errors, day, local)
        core.append_jsonl(core.outbox_path(root), rec)
        return rec


def _record_failure(root: Path, errors: list, day: str, now: datetime) -> None:
    """Back off 5, 15, then 60 minutes; the count restarts each day. One log line per failure; the
    third failure of a day raises an issue."""
    state = core.read_state(root, STATE_FILE)
    n = state.get("failures", 0) + 1 if state.get("failures_day") == day else 1
    wait = BACKOFF_MIN[min(n - 1, len(BACKOFF_MIN) - 1)]
    core.write_state(root, STATE_FILE, {**state, "failures": n, "failures_day": day,
                                        "next": (now + timedelta(minutes=wait)).isoformat(timespec="seconds")})
    _log(root, "digest send failed", attempt=n, retry_in_min=wait, errors=errors)
    if n >= DIGEST_FAILURE_LIMIT:
        watch.raise_known(root, "digest-send-failed", detail=f"{n} failed attempts today: {errors[:1]}", count=n, now=now, deliver_now=True)


def send_if_due(cfg: dict, *, adapters: dict | None = None, spool: Path | None = None, now: datetime | None = None) -> dict | None:
    root = spool or core.spool_root(cfg)
    key = pending_window(cfg, root, now)
    if key is None:
        return None
    local = core.local_now(cfg, now)
    rec = send(cfg, adapters=adapters, spool=root, now=now, window=key)
    if rec is None:                                     # an empty window sends nothing and is spent
        core.write_state(root, STATE_FILE, {"last_window": key})
    elif rec.get("no_route"):                           # items are not held without a route; say so once a day
        day = local.date().isoformat()
        state = core.read_state(root, STATE_FILE)
        if state.get("route_error_day") != day:
            core.write_state(root, STATE_FILE, {**state, "route_error_day": day})
            _log(root, "digest cannot be sent", error=rec["error"])
    return rec
