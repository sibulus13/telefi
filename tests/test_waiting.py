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


def test_the_command_line_opens_and_closes_without_a_hub(root, capsys):
    assert main(["waiting", "open", "--source", "newsdesk", "--title", "Pull request 12 waits for review",
                 "--action", "Review and merge it.", "--link", "https://example.com/pr/12", "--key", "newsdesk:pr:12"]) == 0
    assert [i["key"] for i in waiting.items(root)] == ["newsdesk:pr:12"]
    assert main(["waiting", "close", "newsdesk:pr:12"]) == 0
    assert waiting.items(root) == []


def test_closing_an_unknown_key_fails(root):
    assert main(["waiting", "close", "nobody:nothing"]) == 1



class FlakyAdapter(MockAdapter):
    """Fails its first send, then delivers."""

    def __init__(self, name):
        super().__init__(name)
        self.calls = 0

    def send(self, env, **kw):
        self.calls += 1
        return {"ok": False, "error": "down"} if self.calls == 1 else super().send(env, **kw)


def test_a_delivery_retry_counts_the_item_once(root, monkeypatch):
    env = core.envelope(source="builder", kind="failure", severity="error", title="Deploy needs approval", dedup_key=KEY)
    env["waiting"] = "open"
    core.publish(env, spool=root)
    adapters = {"telegram": FlakyAdapter("telegram"), "file": FlakyAdapter("file")}
    core.drain(MOCK, adapters=adapters, spool=root)
    later = core.clock() + AN_HOUR
    monkeypatch.setattr(core, "clock", lambda: later)
    core.drain(MOCK, adapters=adapters, spool=root)
    assert [i["seen"] for i in waiting.items(root)] == [1]


def test_an_item_ages_from_when_its_envelope_was_made(root, monkeypatch):
    env = core.envelope(source="builder", kind="failure", severity="error", title="Deploy needs approval", dedup_key=KEY)
    env["waiting"] = "open"
    made = env["created_at"]
    core.publish(env, spool=root)
    later = core.clock() + AN_HOUR
    monkeypatch.setattr(core, "clock", lambda: later)
    core.drain(MOCK, adapters={"telegram": MockAdapter("telegram"), "file": MockAdapter("file")}, spool=root)
    assert waiting.items(root)[0]["opened_at"] == made


def test_overlong_fields_still_give_a_valid_list(root, capsys):
    assert main(["waiting", "open", "--source", "newsdesk", "--title", "t" * 500, "--action", "a" * 500,
                 "--link", "https://example.com/" + "x" * 900]) == 0
    capsys.readouterr()
    main(["waiting", "list", "--json"])
    assert schema.validate(json.loads(capsys.readouterr().out), "waiting.v1") == []


def test_a_collector_can_say_since_when_it_has_waited(root):
    main(["waiting", "open", "--source", "newsdesk", "--title", "Pull request 12 waits", "--since", "2026-10-01T09:00:00+00:00"])
    assert waiting.items(root)[0]["opened_at"] == "2026-10-01T09:00:00+00:00"


def test_a_since_that_is_not_a_time_is_refused(root):
    assert main(["waiting", "open", "--source", "newsdesk", "--title", "Pull request 12 waits", "--since", "last week"]) == 1
    assert waiting.items(root) == []
