"""One round of the hub: heartbeat, drain, self-watch, batch windows.

`python -m telefi serve` runs it forever; telefi/simulate.py runs it against a fake clock and fake channels.
"""
from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path

from telefi import core, digest, watch

ERROR_TAIL = 2000


def digest_round(cfg: dict, log: Path, *, root: Path | None = None, now: datetime | None = None,
                  adapters: dict | None = None) -> None:
    """The batch windows, isolated: a failure is logged and never skips drain, prune or inbound."""
    try:
        digest.send_if_due(cfg, adapters=adapters, spool=root, now=now)
    except Exception:    # best-effort side path of the serve loop; the traceback is logged
        core.append_jsonl(log, {"at": core.now_iso(), "event": "digest round failed", "error": traceback.format_exc()[-ERROR_TAIL:]})


def serve_round(cfg: dict, *, root: Path | None = None, now: datetime | None = None, adapters: dict | None = None,
                heartbeat: Path | None = None, log: Path | None = None) -> None:
    """One round of the hub: heartbeat, drain, self-watch, batch windows. The simulator calls it with a fake
    clock (`now`), spool and channels; `serve` calls it with the defaults."""
    root = root or core.spool_root(cfg)
    watch.heartbeat(heartbeat or core.HOME / watch.HEARTBEAT_FILE, root=root, now=now)
    watch.after_drain(root, cfg, core.drain(cfg, adapters=adapters, spool=root, now=now), now=now)
    digest_round(cfg, log or core.HOME / "hub.log", root=root, now=now, adapters=adapters)
