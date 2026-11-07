"""The plain-message standard: anything that asks for attention gives context in plain words and a next step.

    python -m telefi check-wording [--since 2026-11-07] [--outbox PATH]    judge what was really sent
    python -m telefi check-wording --stdin                                 judge a JSON list of envelopes (a sender's test)

Only the lines read at a glance count: title, insight and next_step. Collapsed details may stay technical.
Exit 0 all pass, 1 some fail, 2 nothing to read.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from telefi import core, messages

DEFAULT_DAYS = 7
MIN_CONTEXT_WORDS = 4
ATTENTION_KINDS = {"blocker", "failure", "review"}
ATTENTION_SEVERITIES = {"warn", "error", "critical"}
ORDINARY_WORDS = re.compile(r"\b\w+-(?:of|to)-(?:the-)?\w+\b|\b(?:iPhone|iPad|iOS|macOS|eBay)\b", re.I)
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
TECHNICAL = (
    ("an exit code", re.compile(r"\bexit(?:ed)?(?: with)?(?: code)? \d+|\bexit code\b", re.I)),
    ("an HTTP code", re.compile(r"\bHTTP \d{3}\b")),
    ("an error class", re.compile(r"\b[A-Za-z]*(?:Error|Exception)\b|\bTraceback\b")),
    ("a status label", re.compile(r"\bgate[- ](?:failed|exited)\b|\bdry-run\b|\bneeds-human\b|\b[a-z]+(?:-[a-z]+){2,}\b", re.I)),
    ("a code name", re.compile(r"\b[A-Za-z0-9]+_[A-Za-z0-9_]+\b|\b[a-z]+[A-Z][a-z]+\w*\b")),
    ("a file path", re.compile(r"[A-Za-z]:[\\/]|(?<!\S)/\w+/\w+|\.(?:json|jsonl|lock|log|py|js|ps1|md|txt|yaml|yml|toml|exe)\b")),
    ("a bare id", re.compile(r"#\d+")),
    ("research shorthand", re.compile(r"\b(?:prereg|MDE|OOS)\b")),
)


def needs_attention(env: dict) -> bool:
    return env.get("kind") in ATTENTION_KINDS or env.get("severity") in ATTENTION_SEVERITIES


def glance(env: dict) -> tuple[str, str, str]:
    """The title, context and next step as the reader sees them; a template's come from its view."""
    view = messages.view_of(env)
    if view:
        return view["headline"], view["insight"], view["next_step"]
    return env.get("title") or "", env.get("insight") or "", env.get("next_step") or ""


def problems(env: dict) -> list[str]:
    if not needs_attention(env):
        return []
    title, insight, next_step = glance(env)
    found = []
    if not next_step.strip():
        found.append("no next step")
    if len(insight.split()) < MIN_CONTEXT_WORDS:
        found.append("no context")
    for line in (title, insight, next_step):
        text = ORDINARY_WORDS.sub("", line)
        found += [f"technical: {name}" for name, pattern in TECHNICAL if pattern.search(text)]
    return sorted(set(found))


def _read_outbox(path: Path, since: str) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn or blank line is not a message
        if row.get("at", "") >= since and not row.get("rejected"):            # a rejected row never reached anyone
            rows.append(row)
    return rows


def _report(envelopes: list[dict]) -> int:
    if not envelopes:
        print("nothing to read")
        return 2
    failed = [(e, problems(e)) for e in envelopes if problems(e)]
    judged = sum(needs_attention(e) for e in envelopes)
    print(f"{len(envelopes)} messages: {judged} asked for attention, {len(failed)} fail the standard")
    for source, count in Counter(e.get("source") for e, _ in failed).most_common():
        print(CONTROL_CHARS.sub("?", f"  {source}: {count}"))
    for env, found in failed:
        print(CONTROL_CHARS.sub("?", f"{env.get('source')}: {glance(env)[0][:70]} [{'; '.join(found)}]"))
    return 1 if failed else 0


def check_outbox(outbox: Path | None, since: str | None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    path = outbox or core.spool_root(core.load_config()).parent / "outbox.jsonl"
    if not path.is_file():
        print(f"no outbox at {path}")
        return 2
    return _report([r["envelope"] for r in _read_outbox(path, since or str(date.today() - timedelta(days=DEFAULT_DAYS)))])


def check_stdin(text: str) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    return _report(json.loads(text) if text.strip() else [])
