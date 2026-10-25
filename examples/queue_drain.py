"""Drain a queue sink: process each inbound.v1 event your app received, then delete it.

Run from your app's loop or a cron job:  python examples/queue_drain.py jobs
"""
import json
import sys
from pathlib import Path

from telefi import core

namespace = sys.argv[1] if len(sys.argv) > 1 else "jobs"
queue = core.spool_root(core.load_config()) / "in" / namespace
for path in sorted(queue.glob("*.json")):
    event = json.loads(path.read_text(encoding="utf-8"))
    print(f"{event['kind']} {event.get('command') or event.get('action_id')} {event.get('args') or event.get('payload')}")
    path.unlink()                       # done: remove it so it is handled once
