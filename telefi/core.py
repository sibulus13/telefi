"""TeleFi core: config, publish, route, drain, the local record.

    spool/out/<id>.json      published, not yet delivered
    spool/done/<id>.json     delivered (or deduplicated / acknowledged); pruned after 7 days
    spool/digest/<id>.json   delivery=digest, held for the next batch (telefi/digest.py)
    spool/failed/<id>.json   rejected (schema) or undeliverable after retries, with <id>.error.txt
    spool/in/<namespace>/    inbound events for a namespace's queue sink
    spool/state/             acks.json, attempts.json, pending_prompts.json, telegram.json
    outbox.jsonl             one line per envelope: the envelope + every channel's delivery result
    inbox.jsonl              one line per inbound event and where it went

Design: docs/DESIGN.md. Contracts: telefi/schemas/*.json. Format: render.py. Rotation: logs.py.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telefi import logs, messages, schema
from telefi.adapters import build_adapters
from telefi.render import HUB_SOURCE

# TELEFI_* is current; the legacy NOTIFLY_* names are still read for installs that predate the rename.
HOME = Path(os.environ.get("TELEFI_HOME") or os.environ.get("NOTIFLY_HOME") or Path.home() / ".telefi")
CONFIG_PATH = HOME / "hub.json"
OUTBOX_SCAN_LINES = 5000
DEFAULT_ACK_DAYS = 7
DONE_RETENTION_DAYS = 7
DIGEST_DIR = "digest"             # spool/digest/: held for the next batch window
MAX_DELIVERY_ATTEMPTS = 10        # with backoff 30 s .. 1 h, about 6 hours of retrying
RETRY_BASE_S = 30
RETRY_MAX_S = 3600
LANE_NOW, LANE_BATCH = "now", "batch"
BATCH_ALIASES = ("batch", "digest")     # a sender's delivery value; "digest" is the pre-2026-10-28 name
DEFAULT_QUIET = ("22:00", "07:00")      # only critical goes out between these local times
_QUIET_WARNED: list[bool] = []          # a malformed digest.quiet is logged once per process

DEFAULT_CONFIG: dict = {
    "schema": "telefi.hub_config.v1",
    "mode": "live",
    "dedup_window_min": 360,
    "channels": {
        "telegram": {"type": "telegram", "token_env": "TELEGRAM_BOT_TOKEN", "chat_env": "TELEGRAM_CHAT_ID"},
        "file": {"type": "file", "path": "bell.jsonl"},
    },
    "routes": [
        {"match": {"severity": ["error", "critical"]}, "channels": ["telegram", "file"]},
        {"match": {"kind": ["blocker", "review", "result", "digest"]}, "channels": ["telegram", "file"]},  # digests arrive silently
        {"match": {}, "channels": ["file"]},
    ],
    "inbound": {"enabled": False, "channel": "telegram", "ack_days": DEFAULT_ACK_DAYS,
                "commands": {},
                "sinks": {"*": {"type": "queue"}}},
}


def clock() -> datetime:
    """The current time; tests and the simulator replace it."""
    return datetime.now().astimezone()


def now_iso() -> str:
    """The clock in UTC, seconds precision (the format logs.now_iso uses)."""
    return clock().astimezone(timezone.utc).isoformat(timespec="seconds")


def local_now(cfg: dict, now: datetime | None = None) -> datetime:
    """`now` (default: the clock) in digest.timezone, or the machine's local zone."""
    zone = cfg.get("digest", {}).get("timezone")
    now = now or clock()
    return now.astimezone(ZoneInfo(zone)) if zone else now.astimezone()


def parse_hhmm(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def in_quiet_hours(cfg: dict, local: datetime) -> bool:
    """True inside the quiet window (it may wrap midnight); `digest.quiet: []` switches it off."""
    span = cfg.get("digest", {}).get("quiet", DEFAULT_QUIET)
    if not span:
        return False
    if len(span) != 2:                                  # malformed: fail open (no quiet hours), say so once
        if not _QUIET_WARNED:
            _QUIET_WARNED.append(True)
            append_jsonl(HOME / "hub.log", {"at": now_iso(), "event": "digest.quiet ignored: need [start, end]", "quiet": span})
        return False
    start, end = parse_hhmm(span[0]), parse_hhmm(span[1])
    t = local.time()
    return start <= t < end if start <= end else t >= start or t < end


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_PATH
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else DEFAULT_CONFIG
    bad = schema.validate(cfg, "hub_config.v1")
    if bad:
        raise ValueError(f"hub config {path}: " + "; ".join(bad))
    zone = cfg.get("digest", {}).get("timezone")
    if zone:
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as ex:
            raise ValueError(f"hub config {path}: digest.timezone {zone!r} is not a known IANA zone ({ex}); "
                             "on Windows `pip install tzdata` may be needed") from ex
    logs.configure(cfg)
    return cfg


def resolve(raw: str) -> Path:
    """A config path: `~` expands; a relative path is relative to TELEFI_HOME (so a config copied
    into a throwaway home never writes to the real one)."""
    p = Path(os.path.expanduser(raw))
    return p if p.is_absolute() else HOME / p


def spool_root(cfg: dict) -> Path:
    env = os.environ.get("TELEFI_SPOOL") or os.environ.get("NOTIFLY_HUB_SPOOL")
    return Path(os.path.expanduser(env)) if env else resolve(cfg.get("spool", "spool"))


def append_jsonl(path: Path, rec: dict) -> None:
    logs.append(path, rec)


def read_state(root: Path, name: str) -> dict:
    """The saved state, or {} when there is none. An unreadable file (a torn write after a crash) is set aside
    and logged: state is retry and dedup bookkeeping, and losing it must never stop the hub from delivering."""
    p = root / "state" / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError as ex:
        kept = p.with_name(f"{p.name}.corrupt-{clock():%Y%m%dT%H%M%S}")
        os.replace(p, kept)
        append_jsonl(root.parent / "hub.log", {"at": now_iso(), "event": "state file set aside", "file": kept.name, "error": str(ex)[:120]})
        return {}


def write_state(root: Path, name: str, data: dict) -> None:
    p = root / "state" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(data))
        f.flush()
        os.fsync(f.fileno())                            # a crash must not leave a renamed file with no data in it
    os.replace(tmp, p)


# ---------------------------------------------------------------- publish (producer side)


def envelope(*, source: str, kind: str, severity: str, title: str, insight: str = "", next_step: str = "", body: str = "",
             evidence: dict | None = None, actions: list | None = None, dedup_key: str | None = None,
             not_before: str | None = None, dedup_window_min: float | None = None,
             sections: list[dict] | None = None, silent: bool | None = None,
             delivery: str | None = None, template: str | None = None, facts: dict | None = None) -> dict:
    env = {"schema": "telefi.envelope.v1", "id": f"{source}-{uuid.uuid4().hex[:12]}", "source": source,
           "kind": kind, "severity": severity, "title": title[:200], "body": body[:3500], "created_at": now_iso()}
    if insight:
        env["insight"] = insight[:300]
    if next_step:
        env["next_step"] = next_step[:200]
    if evidence:
        env["evidence"] = evidence
    if actions:
        env["actions"] = actions
    if dedup_key:
        env["dedup_key"] = dedup_key[:200]
    if not_before:
        env["not_before"] = not_before
    if dedup_window_min is not None:
        env["dedup_window_min"] = dedup_window_min
    if sections:
        env["sections"] = sections
    if silent is not None:
        env["silent"] = silent
    if delivery:
        env["delivery"] = delivery
    if template:
        env["template"] = template
        env["facts"] = facts or {}
    return env


def templated(template: str, facts: dict, *, source: str, **kw) -> dict:
    """An envelope made from a template: kind, severity and title come from the template's view of `facts`.
    Raises ValueError on invalid facts, like publish."""
    env = envelope(source=source, kind="info", severity="info", title=template, template=template, facts=facts, **kw)
    bad = schema.validate(env, "envelope.v1")
    if bad:
        raise ValueError("; ".join(bad))
    view = messages.render(template, facts)
    return {**env, "kind": view["kind"], "severity": view["severity"], "title": view["headline"][:200]}


def publish(env: dict, *, spool: Path | None = None) -> Path:
    """Validate, then write atomically into <spool>/out/. Raises ValueError on a schema violation."""
    bad = schema.validate(env, "envelope.v1")
    if bad:
        raise ValueError("; ".join(bad))
    out = (spool or spool_root(DEFAULT_CONFIG)) / "out"
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / f".{env['id']}.tmp"
    tmp.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    dest = out / f"{env['id']}.json"
    os.replace(tmp, dest)
    return dest


# ---------------------------------------------------------------- route + drain


def route_rule(env: dict, cfg: dict) -> dict:
    for r in cfg["routes"]:
        if all(env.get(k) in v for k, v in r["match"].items()):
            return r
    return {"channels": []}


def route(env: dict, cfg: dict) -> list[str]:
    return route_rule(env, cfg)["channels"]


def lane(env: dict, rule: dict) -> str:
    """now or batch. critical and blockers are always now; otherwise the route decides and a sender can
    only make it calmer (now -> batch), never louder. The hub's own issues may ask for now."""
    if env["severity"] == "critical" or env["kind"] == "blocker":
        return LANE_NOW
    if env["source"] == HUB_SOURCE and env.get("delivery") == LANE_NOW:       # the hub's own urgent issue
        return LANE_NOW
    batch = rule.get("delivery") == LANE_BATCH or env.get("delivery") in BATCH_ALIASES
    return LANE_BATCH if batch else LANE_NOW


def button_policy(cfg: dict) -> "bool | frozenset[str]":
    """What render may show as action buttons: False while inbound is off, else the sink namespaces ('*' = any)."""
    inbound = cfg.get("inbound", {})
    return frozenset(inbound.get("sinks", {})) if inbound.get("enabled") else False


def is_telegram(cfg: dict, channel: str) -> bool:
    return cfg["channels"].get(channel, {}).get("type") == "telegram"


def digest_channels(cfg: dict) -> list[str]:
    """Telegram channels a batch (kind digest, made by the hub) is routed to; [] = it cannot be sent."""
    names = route({"source": HUB_SOURCE, "kind": "digest", "severity": "info"}, cfg)
    return [n for n in names if is_telegram(cfg, n)]


def outbox_path(root: Path) -> Path:
    return root.parent / "outbox.jsonl"


def outbox_records(root: Path) -> list[dict]:
    return logs.read(outbox_path(root), last=OUTBOX_SCAN_LINES)


def find_envelope(root: Path, envelope_id: str) -> dict | None:
    for rec in reversed(outbox_records(root)):
        if rec.get("envelope", {}).get("id") == envelope_id:
            return rec["envelope"]
    return None


def sent_messages(root: Path) -> dict[str, str]:
    """Telegram message id -> envelope id, for every hub message that reached Telegram."""
    out = {}
    for rec in outbox_records(root):
        for d in rec.get("deliveries", []):
            if d.get("channel") == "telegram" and d.get("message_id") is not None:
                out[str(d["message_id"])] = rec["envelope"]["id"]
    return out


def conversation_path(root: Path) -> Path:
    return root.parent / "conversation.jsonl"


def ack(root: Path, dedup_key: str) -> None:
    acks = read_state(root, "acks.json")
    acks[dedup_key] = now_iso()
    write_state(root, "acks.json", acks)


def _acked(root: Path, days: float, now: datetime) -> set[str]:
    cutoff = (now - timedelta(days=days)).isoformat()
    return {k for k, at in read_state(root, "acks.json").items() if at >= cutoff}


def _last_delivered(root: Path) -> dict[str, tuple[str, str]]:
    """dedup_key -> (when, envelope id) of its latest delivery to at least one channel."""
    last: dict[str, tuple[str, str]] = {}
    for r in outbox_records(root):
        key = r.get("envelope", {}).get("dedup_key")
        if key and any(d.get("ok") for d in r.get("deliveries", [])):
            last[key] = max(last.get(key, ("", "")), (r.get("at", ""), r["envelope"].get("id", "")))
    return last


def _ago(minutes: float, now: datetime) -> str:
    return (now - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def _when(raw: str) -> datetime | None:
    """An ISO time in any offset -> aware UTC datetime (a naive one is read as UTC); None if invalid."""
    try:
        t = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _move(p: Path, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    os.replace(p, folder / p.name)


def _quarantine(p: Path, root: Path, reasons: list[str]) -> None:
    _move(p, root / "failed")
    (root / "failed" / f"{p.stem}.error.txt").write_text("\n".join(reasons), encoding="utf-8")


def prune_done(root: Path, *, days: float = DONE_RETENTION_DAYS) -> int:
    """Delete delivered envelopes older than `days` (their record stays in outbox.jsonl)."""
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    n = 0
    for p in (root / "done").glob("*.json"):
        if p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            n += 1
    return n


def requeue_failed_criticals(root: Path) -> int:
    """Put criticals that an earlier version gave up on (spool/failed, "delivery attempts failed") back in the
    queue. Rejected (invalid) files stay. Moving them out of failed/ is what makes this happen once."""
    n = 0
    for p in sorted((root / "failed").glob("*.json")):
        err = p.with_name(f"{p.stem}.error.txt")
        try:
            env = json.loads(p.read_text(encoding="utf-8"))
        except (ValueError, FileNotFoundError):
            continue
        gave_up = err.exists() and "delivery attempts failed" in err.read_text(encoding="utf-8")
        if gave_up and env.get("severity") == "critical" and not schema.validate(env, "envelope.v1"):
            _move(p, root / "out")
            err.unlink(missing_ok=True)
            n += 1
    if n:
        append_jsonl(root.parent / "hub.log", {"at": now_iso(), "event": "critical requeued", "count": n})
    return n


def _delivered(cfg: dict, names: list[str], done_legs: set[str], deliveries: list[dict]) -> bool:
    """Done when nothing was routed, or (a Telegram leg accepted it, when routed to one) / (any leg, when not)."""
    ok = done_legs | {d["channel"] for d in deliveries if d["ok"]}
    phone = [n for n in names if is_telegram(cfg, n)]
    return not names or bool(ok & set(phone) if phone else ok)


def drain(cfg: dict, *, adapters: dict | None = None, spool: Path | None = None,
          now: datetime | None = None) -> list[dict]:
    """Deliver every due envelope in <spool>/out. Returns the outbox records written.

    A delivery that reaches no channel is retried with exponential backoff (state/attempts.json)
    and quarantined in failed/ after MAX_DELIVERY_ATTEMPTS, so a Telegram outage never loses a
    message. Every attempt is recorded in outbox.jsonl. `now` (default: the clock) is the time for quiet hours, backoff, dedup and ack windows."""
    root = spool or spool_root(cfg)
    adapters = adapters if adapters is not None else build_adapters(cfg, resolve=resolve)
    inbound = cfg.get("inbound", {})
    buttons = button_policy(cfg)
    now_utc = (now or clock()).astimezone(timezone.utc)
    last = _last_delivered(root)
    acked = _acked(root, inbound.get("ack_days", DEFAULT_ACK_DAYS), now_utc)
    attempts = read_state(root, "attempts.json")
    quiet = in_quiet_hours(cfg, local_now(cfg, now))
    records = []
    for p in sorted((root / "out").glob("*.json")):
        try:
            env = json.loads(p.read_text(encoding="utf-8"))
            bad = schema.validate(env, "envelope.v1")
        except FileNotFoundError:                       # another drainer took it
            continue
        except ValueError as ex:
            env, bad = {"id": p.stem}, [f"unreadable JSON: {ex}"]
        if not bad and env.get("not_before") and _when(env["not_before"]) is None:
            bad = [f"$.not_before: {env['not_before']!r} is not an ISO time"]
        if not bad and env.get("not_before") and _when(env["not_before"]) > now_utc:
            continue                                    # held: a grace period is still running
        retry = attempts.get(p.stem)
        if retry and (_when(retry["next"]) or now_utc) > now_utc:
            continue                                    # backing off after a failed delivery
        key = env.get("dedup_key")
        window = env.get("dedup_window_min", cfg.get("dedup_window_min", 360))
        if bad:
            _quarantine(p, root, bad)
            rec = {"at": now_iso(), "envelope": env, "rejected": bad, "deliveries": []}
        elif key in acked:
            _move(p, root / "done")
            rec = {"at": now_iso(), "envelope": env, "acknowledged": True, "deliveries": []}
        elif key and last.get(key, ("", ""))[0] >= _ago(window, now_utc) and last[key][1] != env["id"]:   # a retry is not its own duplicate
            _move(p, root / "done")
            rec = {"at": now_iso(), "envelope": env, "deduplicated": True, "deliveries": []}
        else:
            rule = route_rule(env, cfg)
            names = rule["channels"]
            to_phone = any(is_telegram(cfg, n) for n in names)
            now_lane = lane(env, rule) == LANE_NOW
            if now_lane and quiet and to_phone and env["severity"] != "critical":
                continue                                # only critical breaks quiet hours: wait in out/ for 07:00
            held = (not now_lane and to_phone
                    and bool(digest_channels(cfg)))      # never hold what no batch could ever send
            done_legs = set((retry or {}).get("sent", []))
            deliveries = []
            for name in names:
                if held and is_telegram(cfg, name):
                    continue                            # waits for the next batch window (telefi/digest.py)
                if name in done_legs:
                    continue                            # delivered on an earlier attempt: never twice
                a = adapters.get(name)
                res = a.send(env, buttons=buttons) if a else {"ok": False, "error": f"no channel named {name}"}
                deliveries.append({"channel": name, **res})
            rec = {"at": now_iso(), "envelope": env, "deliveries": deliveries}
            if held:
                rec["digest_held"] = True
                _move(p, root / DIGEST_DIR)
                attempts.pop(p.stem, None)
            elif _delivered(cfg, names, done_legs, deliveries):
                _move(p, root / "done")
                attempts.pop(p.stem, None)
                if key and deliveries:
                    last[key] = (rec["at"], env["id"])
            else:
                n = (retry or {}).get("n", 0) + 1
                if n >= MAX_DELIVERY_ATTEMPTS and env["severity"] != "critical":      # a critical never gives up
                    _quarantine(p, root, [f"{n} delivery attempts failed: {d['channel']}: {d['error']}"
                                          for d in deliveries if not d["ok"]])
                    attempts.pop(p.stem, None)
                    rec["gave_up"] = n
                else:
                    wait = min(RETRY_BASE_S * 2 ** (min(n, MAX_DELIVERY_ATTEMPTS) - 1), RETRY_MAX_S)   # hourly at most
                    sent = sorted(done_legs | {d["channel"] for d in deliveries if d["ok"]})
                    attempts[p.stem] = {"n": n, "sent": sent,
                                        "next": (now_utc + timedelta(seconds=wait)).isoformat(timespec="seconds")}
                    rec["retry_in_s"] = wait
        append_jsonl(outbox_path(root), rec)
        records.append(rec)
    write_state(root, "attempts.json", attempts)
    return records
