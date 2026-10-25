"""TeleFi core: config, publish, route, drain, the local record.

    spool/out/<id>.json      published, not yet delivered
    spool/done/<id>.json     delivered (or deduplicated / acknowledged)
    spool/failed/<id>.json   rejected (schema) -- with a sibling <id>.error.txt
    spool/in/<namespace>/    inbound events for a namespace's queue sink
    spool/state/             acks.json, pending_prompts.json, telegram_offset.txt
    outbox.jsonl             one line per envelope: the envelope + every channel's delivery result
    inbox.jsonl              one line per inbound event and where it went

Design: docs/HUB_DESIGN.md. Contracts: telefi/schemas/*.json. Format: render.py. Rotation: logs.py.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telefi import logs, schema
from telefi.adapters import build_adapters

# TELEFI_* is current; the legacy NOTIFLY_* names are still read for installs that predate the rename.
HOME = Path(os.environ.get("TELEFI_HOME") or os.environ.get("NOTIFLY_HOME") or Path.home() / ".telefi")
CONFIG_PATH = HOME / "hub.json"
OUTBOX_SCAN_LINES = 5000
DEFAULT_ACK_DAYS = 7

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
        {"match": {"kind": ["blocker", "review", "result"]}, "channels": ["telegram", "file"]},
        {"match": {}, "channels": ["file"]},
    ],
    "inbound": {"enabled": False, "channel": "telegram", "ack_days": DEFAULT_ACK_DAYS,
                "commands": {},
                "sinks": {"*": {"type": "queue"}}},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8")) if path.exists() else DEFAULT_CONFIG
    bad = schema.validate(cfg, "hub_config.v1")
    if bad:
        raise ValueError(f"hub config {path}: " + "; ".join(bad))
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
    p = root / "state" / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def write_state(root: Path, name: str, data: dict) -> None:
    p = root / "state" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, p)


# ---------------------------------------------------------------- publish (producer side)


def envelope(*, source: str, kind: str, severity: str, title: str, insight: str = "", body: str = "",
             evidence: dict | None = None, actions: list | None = None, dedup_key: str | None = None,
             not_before: str | None = None, dedup_window_min: float | None = None) -> dict:
    env = {"schema": "telefi.envelope.v1", "id": f"{source}-{uuid.uuid4().hex[:12]}", "source": source,
           "kind": kind, "severity": severity, "title": title[:200], "body": body[:3500], "created_at": now_iso()}
    if insight:
        env["insight"] = insight[:300]
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
    return env


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


def route(env: dict, cfg: dict) -> list[str]:
    for r in cfg["routes"]:
        if all(env.get(k) in v for k, v in r["match"].items()):
            return r["channels"]
    return []


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


def _acked(root: Path, days: float) -> set[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return {k for k, at in read_state(root, "acks.json").items() if at >= cutoff}


def _last_delivered(root: Path) -> dict[str, str]:
    """dedup_key -> when it was last delivered to at least one channel."""
    last: dict[str, str] = {}
    for r in outbox_records(root):
        key = r.get("envelope", {}).get("dedup_key")
        if key and any(d.get("ok") for d in r.get("deliveries", [])):
            last[key] = max(last.get(key, ""), r.get("at", ""))
    return last


def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def drain(cfg: dict, *, adapters: dict | None = None, spool: Path | None = None) -> list[dict]:
    """Deliver every envelope in <spool>/out. Returns the outbox records written."""
    root = spool or spool_root(cfg)
    adapters = adapters if adapters is not None else build_adapters(cfg, resolve=resolve)
    inbound = cfg.get("inbound", {})
    buttons = bool(inbound.get("enabled"))
    last = _last_delivered(root)
    acked = _acked(root, inbound.get("ack_days", DEFAULT_ACK_DAYS))
    now = now_iso()
    records = []
    for p in sorted((root / "out").glob("*.json")):
        try:
            env = json.loads(p.read_text(encoding="utf-8"))
            bad = schema.validate(env, "envelope.v1")
        except FileNotFoundError:                       # another drainer took it
            continue
        except ValueError as ex:
            env, bad = {"id": p.stem}, [f"unreadable JSON: {ex}"]
        if not bad and env.get("not_before", "") > now:  # held: a grace period is still running
            continue
        key = env.get("dedup_key")
        window = env.get("dedup_window_min", cfg.get("dedup_window_min", 360))
        if bad:
            (root / "failed").mkdir(parents=True, exist_ok=True)
            os.replace(p, root / "failed" / p.name)
            (root / "failed" / f"{p.stem}.error.txt").write_text("\n".join(bad), encoding="utf-8")
            rec = {"at": now_iso(), "envelope": env, "rejected": bad, "deliveries": []}
        elif key in acked:
            rec = {"at": now_iso(), "envelope": env, "acknowledged": True, "deliveries": []}
        elif key and last.get(key, "") >= _ago(window):
            rec = {"at": now_iso(), "envelope": env, "deduplicated": True, "deliveries": []}
        else:
            deliveries = []
            for name in route(env, cfg):
                a = adapters.get(name)
                res = a.send(env, buttons=buttons) if a else {"ok": False, "error": f"no channel named {name}"}
                deliveries.append({"channel": name, **res})
            rec = {"at": now_iso(), "envelope": env, "deliveries": deliveries}
            if key and any(d["ok"] for d in deliveries):
                last[key] = rec["at"]
        if not bad:
            (root / "done").mkdir(parents=True, exist_ok=True)
            os.replace(p, root / "done" / p.name)
        append_jsonl(outbox_path(root), rec)
        records.append(rec)
    return records
