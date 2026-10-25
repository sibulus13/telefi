"""Append-only JSON-lines records with size-based rotation.

A log is "full" at `max_bytes`; it rotates once it reaches `rotate_at` x `max_bytes` (the safety
margin), shifting `name.jsonl` -> `name.jsonl.1` -> ... -> `name.jsonl.<keep>` (the oldest is
dropped). Readers see the current file plus the newest rotated one, so dedup and reply lookups
survive a rotation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_ROTATE_AT = 0.9
DEFAULT_KEEP = 3

POLICY = {"max_bytes": DEFAULT_MAX_BYTES, "rotate_at": DEFAULT_ROTATE_AT, "keep": DEFAULT_KEEP}


def configure(cfg: dict) -> None:
    """Apply the hub config's `logs` section (missing keys keep their defaults)."""
    POLICY.update({k: v for k, v in (cfg.get("logs") or {}).items() if k in POLICY})


def _rotated(path: Path, n: int) -> Path:
    return path.with_name(f"{path.name}.{n}")


def rotate_if_needed(path: Path) -> bool:
    """Rotate `path` when it has reached the safety threshold. Returns True if it rotated."""
    limit = POLICY["max_bytes"] * POLICY["rotate_at"]
    if not path.exists() or path.stat().st_size < limit:
        return False
    keep = max(1, int(POLICY["keep"]))
    _rotated(path, keep).unlink(missing_ok=True)
    for n in range(keep - 1, 0, -1):
        if _rotated(path, n).exists():
            os.replace(_rotated(path, n), _rotated(path, n + 1))
    os.replace(path, _rotated(path, 1))
    return True


def append(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_if_needed(path)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read(path: Path, *, last: int) -> list[dict]:
    """The last `last` records across the newest rotated file and the current one."""
    lines: list[str] = []
    for p in (_rotated(path, 1), path):
        if p.exists():
            lines += p.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-last:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out
