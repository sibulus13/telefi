"""Inbound: the owner's taps, replies and commands -> inbound.v1 events -> per-namespace sinks.

```
tap h|ns|action|env  --action has input?--> ForceReply prompt --reply--> event{payload.note} --> sink
tap h|ns|ack|env     --> hub acks the envelope's dedup_key (no more re-alerts for ack_days)
/ci retry 43 note    --> the "ci" manifest -> its sink (commands.py)
anything else        --> kind=legacy with the raw update --> the 'legacy' sink (an existing bot handler)
```

The hub is the ONLY getUpdates consumer for the bot; whatever it does not own it forwards whole.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from telefi import commands, core
from telefi.adapters import telegram_call, telegram_creds
from telefi.render import parse_callback

COMMAND_TIMEOUT_S = 120
REPLY_MAX = 900
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HUB_NS = "hub"
ACK = "ack"
REPLY = "_reply"               # a reply to any hub message: becomes a note for its first input action


def _chat(update: dict) -> str | None:
    msg = (update.get("callback_query") or {}).get("message") or update.get("message") or {}
    cid = (msg.get("chat") or {}).get("id")
    return None if cid is None else str(cid)


def _event(update: dict, kind: str, namespace: str, **fields) -> dict:
    return {"schema": "telefi.inbound.v1", "id": f"tg-{update.get('update_id')}", "kind": kind,
            "namespace": namespace[:24], "channel": "telegram", "at": core.now_iso(), **fields}


def _forward_as(update: dict, text: str) -> dict:
    """The raw update with its text replaced: '/news status' reaches an older handler as '/status'."""
    return {**update, "message": {**update["message"], "text": text}}


def parse_update(update: dict, cfg: dict, pending: dict, sent: dict | None = None,
                 manifests: dict | None = None) -> dict | None:
    """Telegram update -> inbound.v1, or None to ignore. The chat check is the caller's.
    `sent` maps Telegram message ids of hub messages to their envelope ids."""
    cq = update.get("callback_query")
    if cq:
        hub = parse_callback(cq.get("data", ""))
        if not hub:
            return _event(update, "legacy", "legacy", raw=update)
        ns, action, env_id = hub
        return _event(update, "action", ns, action_id=action[:24], envelope_id=env_id[:64])
    msg = update.get("message") or {}
    reply_to = str((msg.get("reply_to_message") or {}).get("message_id", ""))
    if reply_to and reply_to in pending:
        p = pending[reply_to]
        return _event(update, "action", p["namespace"], action_id=p["action_id"], envelope_id=p["envelope_id"],
                      payload={"note": (msg.get("text") or "")[:2000]})
    if reply_to and sent and reply_to in sent:
        return _event(update, "action", HUB_NS, action_id=REPLY, envelope_id=sent[reply_to],
                      payload={"note": (msg.get("text") or "")[:2000]})
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return None
    words = text[1:].split()
    cmd = words[0].split("@")[0].lower() if words else ""
    manifests = manifests or {}
    if cmd in commands.TOP_LEVEL:
        return _event(update, "command", HUB_NS, command=cmd, args=[w[:200] for w in words[1:10]])
    if cmd in manifests:
        app, verb_name = manifests[cmd], (words[1].lower() if len(words) > 1 else "")
        c = commands.verb(manifests, cmd, verb_name)
        if app["app"] == HUB_NS and c:
            return _event(update, "command", HUB_NS, command=verb_name, args=[w[:200] for w in words[2:10]])
        if not c:                                         # unknown or missing verb: show the app's help
            return _event(update, "command", HUB_NS, command="help", args=[cmd])
        if c.get("legacy_text"):
            fwd = " ".join([c["legacy_text"], *words[2:]])
            return _event(update, "legacy", app["sink"], raw=_forward_as(update, fwd))
        return _event(update, "command", app["sink"], command=verb_name, args=[w[:200] for w in words[2:10]])
    ns = cfg.get("inbound", {}).get("commands", {}).get(cmd)
    if not ns:
        return _event(update, "legacy", "legacy", raw=update)
    return _event(update, "command", ns, command=cmd[:40], args=[w[:200] for w in words[1:10]])


def dispatch(event: dict, cfg: dict, root: Path, *, run: Callable = subprocess.run) -> dict:
    """Send one event to its namespace's sink: a queue file, or a command fed the JSON on stdin."""
    sinks = cfg.get("inbound", {}).get("sinks", {})
    sink = sinks.get(event["namespace"]) or sinks.get("*") or {"type": "queue"}
    if sink["type"] == "queue":
        qdir = root / "in" / event["namespace"]
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / f"{event['id']}.json").write_text(json.dumps(event), encoding="utf-8")
        result = {"sink": "queue", "ok": True, "reply": f"Queued for {event['namespace']}."}
    else:
        try:
            p = run(sink["argv"], input=json.dumps(event), capture_output=True, text=True, cwd=sink.get("cwd"),
                    timeout=sink.get("timeout_s", COMMAND_TIMEOUT_S), creationflags=NO_WINDOW)
            out = (p.stdout or "").strip()
            result = {"sink": "command", "ok": p.returncode == 0, "rc": p.returncode,
                      "reply": out.splitlines()[-1][:REPLY_MAX] if out else "",
                      "output": (p.stdout + p.stderr)[-1500:]}
        except (OSError, subprocess.TimeoutExpired) as ex:
            result = {"sink": "command", "ok": False, "reply": "", "error": f"{type(ex).__name__}: {ex}"[:300]}
    core.append_jsonl(root.parent / "inbox.jsonl", {"at": core.now_iso(), "event": event, "result": result})
    return result


def _ack_last(root: Path) -> dict | None:
    for rec in reversed(core.outbox_records(root)):
        env = rec.get("envelope", {})
        if env.get("dedup_key") and env.get("severity") in ("error", "critical") and rec.get("deliveries"):
            return env
    return None


def handle_update(update: dict, cfg: dict, root: Path, *, tg: Callable, chat_id: str,
                  run: Callable = subprocess.run) -> dict:
    """One update end to end. `tg(method, body) -> {"ok", "result"?}` is the Bot API (injected)."""
    if _chat(update) != str(chat_id):
        return {"outcome": "ignored_foreign_chat"}
    pending = core.read_state(root, "pending_prompts.json")
    manifests = commands.load_manifests(cfg.get("inbound", {}).get("manifests", []))
    ev = parse_update(update, cfg, pending, core.sent_messages(root), manifests)
    if ev is None:
        return {"outcome": "ignored"}
    cq_id = (update.get("callback_query") or {}).get("id")

    if ev["kind"] == "legacy":
        res = dispatch(ev, cfg, root, run=run)            # the legacy handler answers the tap itself
        return {"outcome": "legacy", "ok": res["ok"]}

    if ev["kind"] == "command" and ev["namespace"] == HUB_NS and ev["command"] in ("help", "list"):
        text = (commands.list_text(manifests) if ev["command"] == "list"
                else commands.help_text(manifests, (ev.get("args") or [None])[0]))
        tg("sendMessage", {"chat_id": chat_id, "text": text[:4000]})
        return {"outcome": ev["command"]}

    if ev["kind"] == "command" and ev["command"] == ACK:
        env = _ack_last(root)
        if env:
            core.ack(root, env["dedup_key"])
        text = f"Acknowledged: {env['title']}" if env else "Nothing to acknowledge."
        tg("sendMessage", {"chat_id": chat_id, "text": text})
        return {"outcome": "acked" if env else "nothing_to_ack"}

    if ev["kind"] == "action":
        env = core.find_envelope(root, ev["envelope_id"])
        if ev["action_id"] == REPLY:                      # threaded reply: any number per message
            action = next((a for a in (env or {}).get("actions", []) if a.get("input")), None)
            if not action:
                tg("sendMessage", {"chat_id": chat_id, "text": "This message takes no notes; use its buttons or a /command."})
                return {"outcome": "reply_not_accepted"}
            ev["namespace"], ev["action_id"] = action["namespace"], action["id"]
        action = next((a for a in (env or {}).get("actions", []) if a["id"] == ev["action_id"]), None)
        if not action:
            if cq_id:
                tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "This button has expired."})
            return {"outcome": "expired"}
        if action["id"] == ACK or action["namespace"] == HUB_NS:
            if env.get("dedup_key"):
                core.ack(root, env["dedup_key"])
            tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "Acknowledged. No more alerts for this."})
            core.append_jsonl(root.parent / "inbox.jsonl", {"at": core.now_iso(), "event": ev, "result": {"sink": "hub", "ok": True}})
            return {"outcome": "acked"}
        note = (ev.get("payload") or {}).get("note")
        if action.get("input") and note is None:
            prompt = f"{action['input']['prompt']}\n(re: {env['title']})"
            sent = tg("sendMessage", {"chat_id": chat_id, "text": prompt,
                                      "reply_markup": {"force_reply": True, "input_field_placeholder": "type your note"}})
            mid = (sent.get("result") or {}).get("message_id")
            if mid is not None:
                pending[str(mid)] = {"namespace": ev["namespace"], "action_id": ev["action_id"], "envelope_id": ev["envelope_id"]}
                core.write_state(root, "pending_prompts.json", pending)
            if cq_id:
                tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "Reply with your note."})
            return {"outcome": "prompted"}
        ev["payload"] = {**action.get("payload", {}), **({"note": note} if note is not None else {})}
        if note is not None and pending.pop(str(update["message"]["reply_to_message"]["message_id"]), None):
            core.write_state(root, "pending_prompts.json", pending)

    res = dispatch(ev, cfg, root, run=run)
    if cq_id:
        tg("answerCallbackQuery", {"callback_query_id": cq_id, "text": "Done." if res["ok"] else "Failed: see the reply."})
    reply = res.get("reply") or ("Done." if res["ok"] else f"Failed: {res.get('error') or res.get('output', '')[-200:]}")
    tg("sendMessage", {"chat_id": chat_id, "text": reply[:REPLY_MAX]})
    return {"outcome": "dispatched", "ok": res["ok"]}


def register_menu(cfg: dict, *, opener: Callable | None = None) -> dict:
    """Publish every app's command to Telegram's '/' menu (setMyCommands)."""
    token, _ = telegram_creds(cfg, cfg["inbound"].get("channel", "telegram"))
    menu = commands.bot_commands(commands.load_manifests(cfg["inbound"].get("manifests", [])))
    return telegram_call(token, "setMyCommands", {"commands": menu}, opener=opener)


def poll_once(cfg: dict, *, root: Path | None = None, timeout_s: int = 50, opener: Callable | None = None,
              run: Callable = subprocess.run) -> int:
    """One long-poll round. Only while inbound.enabled: getUpdates has exactly one consumer per bot."""
    if not cfg.get("inbound", {}).get("enabled"):
        raise RuntimeError("inbound disabled in hub config")
    root = root or core.spool_root(cfg)
    token, chat = telegram_creds(cfg, cfg["inbound"].get("channel", "telegram"))
    talk = core.conversation_path(root)

    def tg(method: str, body: dict) -> dict:
        """Every hub->owner call is recorded in conversation.jsonl (the in-chat transcript)."""
        res = telegram_call(token, method, body, opener=opener)
        core.append_jsonl(talk, {"at": core.now_iso(), "dir": "out", "method": method, "text": body.get("text"),
                                 "message_id": (res.get("result") or {}).get("message_id") if isinstance(res.get("result"), dict) else None,
                                 "ok": res["ok"], "error": res.get("error")})
        return res
    offset = int(core.read_state(root, "telegram.json").get("offset", 0))
    res = telegram_call(token, "getUpdates", {"offset": offset, "timeout": timeout_s}, opener=opener, timeout=timeout_s + 10)
    n = 0
    for u in res.get("result") or []:
        msg = u.get("message") or (u.get("callback_query") or {}).get("message") or {}
        rec = {"at": core.now_iso(), "dir": "in", "update_id": u.get("update_id"), "chat": _chat(u),
               "text": (u.get("message") or {}).get("text"), "button": (u.get("callback_query") or {}).get("data"),
               "reply_to": ((u.get("message") or {}).get("reply_to_message") or {}).get("message_id"),
               "message_id": msg.get("message_id")}
        try:
            rec["outcome"] = handle_update(u, cfg, root, tg=tg, chat_id=chat, run=run).get("outcome")
            core.append_jsonl(talk, rec)
        except Exception as ex:
            core.append_jsonl(talk, {**rec, "outcome": "error", "error": f"{type(ex).__name__}: {ex}"[:300]})                           # one bad update must never stall the queue
            core.append_jsonl(root.parent / "inbox.jsonl", {"at": core.now_iso(), "error": f"{type(ex).__name__}: {ex}"[:300],
                                                            "update_id": u.get("update_id")})
        core.write_state(root, "telegram.json", {"offset": u["update_id"] + 1})
        n += 1
    return n
