"""A minimal command sink: reads one inbound.v1 event on stdin; its last stdout line is the reply."""
import json
import sys

event = json.load(sys.stdin)
note = (event.get("payload") or {}).get("note")
print(f"{event.get('command') or event.get('action_id')} received" + (f" with note: {note}" if note else ""))
