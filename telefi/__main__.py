"""TeleFi CLI.

    python -m telefi drain                 deliver everything in the spool once (the scheduled task)
    python -m telefi serve [--interval 30] drain forever; also long-polls Telegram if inbound.enabled
    python -m telefi preview [type ...]    print each sample exactly as the owner would see it (no send)
    python -m telefi samples [--send]      route the samples; mock unless --send (then really delivers)
    python -m telefi validate <file.json>  check an envelope or inbound event against its schema
    python -m telefi schema <name>         print a published schema (envelope.v1 | inbound.v1 | hub_config.v1)
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

from telefi import core, inbound, schema
from telefi.render import render_plain
from telefi.samples import samples

SERVE_RETRY_S = 5
SERVE_RETRY_MAX_S = 300
PRUNE_EVERY_S = 3600


def _preview(names: list[str], buttons: bool) -> int:
    for name, env in samples().items():
        if names and name not in names:
            continue
        print(f"===== {name}  (routes to: {', '.join(core.route(env, core.DEFAULT_CONFIG)) or 'nothing'})")
        print(render_plain(env, buttons=buttons))
        print()
    return 0


def _samples(send: bool) -> int:
    cfg = core.load_config()
    if not send:
        cfg = {**cfg, "mode": "mock"}
    with tempfile.TemporaryDirectory() as tmp:
        spool = Path(tmp) / "spool"
        for env in samples().values():
            core.publish(env, spool=spool)
        for rec in core.drain(cfg, spool=spool):
            env = rec["envelope"]
            res = ", ".join(f"{d['channel']}={'ok' if d['ok'] else 'FAILED ' + str(d['error'])}" for d in rec["deliveries"])
            print(f"{env['source']}.{env['kind']} [{env['severity']}] -> {res or 'no route'}")
    print("mode:", "LIVE (really sent)" if send else "mock (nothing sent)")
    return 0


def _serve(interval: float) -> int:
    """Drain + long-poll forever. A failing round is logged to ~/.telefi/hub.log and retried; it
    never ends the process (the scheduled task would only restart it 5 minutes later)."""
    cfg = core.load_config()
    log = core.HOME / "hub.log"
    core.append_jsonl(log, {"at": core.now_iso(), "event": "serve started", "inbound": cfg.get("inbound", {}).get("enabled")})
    if cfg.get("inbound", {}).get("enabled"):
        menu = inbound.register_menu(cfg)
        core.append_jsonl(log, {"at": core.now_iso(), "event": "command menu", "ok": menu["ok"], "error": menu.get("error")})
    failures, pruned_at = 0, 0.0
    while True:
        try:
            core.drain(cfg)
            if time.monotonic() - pruned_at > PRUNE_EVERY_S:
                core.prune_done(core.spool_root(cfg))
                pruned_at = time.monotonic()
            if cfg.get("inbound", {}).get("enabled"):
                inbound.poll_once(cfg, timeout_s=int(interval))
            else:
                time.sleep(interval)
            failures = 0
        except Exception:
            failures += 1
            wait = min(SERVE_RETRY_S * 2 ** (failures - 1), SERVE_RETRY_MAX_S)   # an outage never spins
            core.append_jsonl(log, {"at": core.now_iso(), "event": "round failed", "retry_in_s": wait,
                                    "error": traceback.format_exc()[-2000:]})
            time.sleep(wait)


def main(argv=None) -> int:
    if sys.stdout is not None:                    # None under pythonw.exe (no console)
        sys.stdout.reconfigure(encoding="utf-8")   # Windows consoles default to cp1252
    ap = argparse.ArgumentParser(prog="python -m telefi")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("drain")
    s = sub.add_parser("serve")
    s.add_argument("--interval", type=float, default=30.0)
    p = sub.add_parser("preview")
    p.add_argument("types", nargs="*")
    p.add_argument("--no-buttons", action="store_true", help="hide action buttons (as sent while inbound is disabled)")
    sm = sub.add_parser("samples")
    sm.add_argument("--send", action="store_true")
    v = sub.add_parser("validate")
    v.add_argument("file")
    sc = sub.add_parser("schema")
    sc.add_argument("name", choices=schema.SCHEMAS)
    a = ap.parse_args(argv)

    if a.cmd == "drain":
        recs = core.drain(core.load_config())
        failed = [r for r in recs if r.get("rejected") or any(not d["ok"] for d in r["deliveries"])]
        print(f"[hub] delivered {len(recs) - len(failed)}/{len(recs)}", flush=True)
        return 1 if failed else 0
    if a.cmd == "serve":
        return _serve(a.interval)
    if a.cmd == "preview":
        return _preview(a.types, buttons=not a.no_buttons)
    if a.cmd == "samples":
        return _samples(a.send)
    if a.cmd == "validate":
        obj = json.loads(Path(a.file).read_text(encoding="utf-8"))
        name = "inbound.v1" if str(obj.get("schema", "")).endswith(".inbound.v1") else "envelope.v1"
        bad = schema.validate(obj, name)
        print("\n".join(bad) or f"valid {name}")
        return 1 if bad else 0
    if a.cmd == "schema":
        print(json.dumps(schema.load(a.name), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
