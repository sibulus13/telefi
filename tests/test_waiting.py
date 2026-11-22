"""The waiting list: what is stopped until the owner acts, one row per key, with its age."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from telefi import core, schema, waiting
from telefi.__main__ import main
from telefi.adapters import MockAdapter

MOCK = {**core.DEFAULT_CONFIG, "mode": "mock"}
KEY = "builder:deploy-approval"
AN_HOUR = timedelta(hours=1)


@pytest.fixture
def root():
    """The spool the command line uses too, so a test can mix envelopes and CLI calls."""
    return core.spool_root(MOCK)


def _send(root, state: str | None, **kw):
    env = core.envelope(source="builder", kind="failure", severity="error", title="Deploy needs approval",
                        next_step="Approve the deploy in the dashboard.", dedup_key=KEY,
                        evidence={"links": [{"label": "Run", "url": "https://example.com/run/7"}]}, **kw)
    if state:
        env["waiting"] = state
    core.publish(env, spool=root)
    return core.drain(MOCK, adapters={"telegram": MockAdapter("telegram"), "file": MockAdapter("file")}, spool=root)[0]


def test_an_open_envelope_puts_one_item_on_the_list(root):
    _send(root, "open")
    (item,) = waiting.items(root)
    assert item["key"] == KEY
    assert item["action"] == "Approve the deploy in the dashboard."
    assert item["link"] == "https://example.com/run/7"


def test_a_repeat_keeps_one_item_and_its_opening_time_even_when_deduplicated(root, monkeypatch):
    _send(root, "open")
    opened = waiting.items(root)[0]["opened_at"]
    later = core.clock() + AN_HOUR
    monkeypatch.setattr(core, "clock", lambda: later)
    rec = _send(root, "open")
    (item,) = waiting.items(root)
    assert rec.get("deduplicated") is True
    assert (item["opened_at"], item["seen"]) == (opened, 2)
    assert item["last_seen_at"] > opened


def test_a_closed_envelope_removes_the_item_and_records_it(root):
    _send(root, "open")
    _send(root, "closed")
    assert waiting.items(root) == []
    closed = [json.loads(line) for line in waiting.closed_path(root).read_text(encoding="utf-8").splitlines()]
    assert closed[-1]["key"] == KEY


def test_the_json_list_matches_its_published_schema(root, capsys):
    _send(root, "open")
    assert main(["waiting", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert schema.validate(listed, "waiting.v1") == []
    assert [i["key"] for i in listed["items"]] == [KEY]


def test_an_unknown_waiting_value_is_rejected(root):
    with pytest.raises(ValueError, match="waiting"):
        _send(root, "maybe")
    assert waiting.items(root) == []


def test_the_command_line_opens_and_closes_without_a_hub(root, capsys):
    assert main(["waiting", "open", "--source", "newsdesk", "--title", "Pull request 12 waits for review",
                 "--action", "Review and merge it.", "--link", "https://example.com/pr/12", "--key", "newsdesk:pr:12"]) == 0
    assert [i["key"] for i in waiting.items(root)] == ["newsdesk:pr:12"]
    assert main(["waiting", "close", "newsdesk:pr:12"]) == 0
    assert waiting.items(root) == []


def test_closing_an_unknown_key_fails(root):
    assert main(["waiting", "close", "nobody:nothing"]) == 1

