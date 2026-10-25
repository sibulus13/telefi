"""TeleFi: the fundamental behaviours only (~/.claude/playbooks/testing.md).
Mock by default; TELEFI_LIVE=1 adds one real Telegram send per sample type."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from telefi import core, inbound, schema
from telefi.adapters import MockAdapter
from telefi.render import callback_data, render_telegram
from telefi.samples import samples

CHAT = "42"
MOCK = {**core.DEFAULT_CONFIG, "mode": "mock"}


@pytest.fixture
def root(tmp_path):
    return tmp_path / "spool"


def _inbound_cfg(sinks=None) -> dict:
    return {**MOCK, "inbound": {**core.DEFAULT_CONFIG["inbound"], "enabled": True, **({"sinks": sinks} if sinks else {})}}


def _deliver(root, env, cfg=MOCK, telegram=None):
    core.publish(env, spool=root)
    return core.drain(cfg, adapters={"telegram": telegram or MockAdapter("telegram"), "file": MockAdapter("file")}, spool=root)[0]


class FakeTG:
    def __init__(self):
        self.sent = []

    def __call__(self, method, body):
        self.sent.append(body.get("text"))
        return {"ok": True, "result": {"message_id": len(self.sent)}}


def _tap(env, action):
    a = next(x for x in env["actions"] if x["id"] == action)
    return {"update_id": 1, "callback_query": {"id": "q", "data": callback_data(a["namespace"], action, env["id"]),
                                               "message": {"chat": {"id": int(CHAT)}}}}


def _msg(text, reply_to=None, chat=CHAT):
    m = {"text": text, "chat": {"id": int(chat)}, **({"reply_to_message": {"message_id": reply_to}} if reply_to else {})}
    return {"update_id": 2, "message": m}


# ---------------------------------------------------------------- contract

def test_samples_meet_the_published_contract():
    assert all(schema.validate(e, "envelope.v1") == [] for e in samples().values())


def test_publish_refuses_a_contract_violation(root):
    with pytest.raises(ValueError):
        core.publish({**samples()["info.notice"], "severity": "panic"}, spool=root)


# ---------------------------------------------------------------- delivery

def test_failures_reach_the_phone_info_stays_local():
    assert "telegram" in core.route(samples()["ci.failure"], MOCK)
    assert "telegram" not in core.route(samples()["info.notice"], MOCK)


def test_the_same_alert_is_delivered_once(root):
    env = samples()["ci.failure"]
    _deliver(root, env)
    assert _deliver(root, {**env, "id": "again"}).get("deduplicated")


def test_an_acked_alert_is_not_delivered_again(root):
    env = samples()["ci.failure"]
    core.ack(root, env["dedup_key"])
    assert _deliver(root, env).get("acknowledged")


def test_a_failed_delivery_is_logged_with_its_error(root):
    _deliver(root, samples()["job.failure"], telegram=MockAdapter("telegram", fail=True))
    logged = json.loads(core.outbox_path(root).read_text().splitlines()[-1])
    assert logged["deliveries"][0]["error"]


# ---------------------------------------------------------------- format

def test_glance_first_details_collapsed_links_open():
    r = render_telegram(samples()["research.result"], buttons=False)
    glance, details = r["text"].split("<blockquote expandable>")
    assert "REJECT" in glance and "priced in" in glance and "CI" in details
    assert all("url" in b for row in r["reply_markup"]["inline_keyboard"] for b in row)


def test_action_buttons_only_when_replies_are_handled():
    env = samples()["ci.failure"]
    n_off = sum(map(len, render_telegram(env, buttons=False)["reply_markup"]["inline_keyboard"]))
    n_on = sum(map(len, render_telegram(env, buttons=True)["reply_markup"]["inline_keyboard"]))
    assert n_on == n_off + len(env["actions"])


# ---------------------------------------------------------------- replies

def test_retry_with_note_delivers_the_note_to_the_owner_sink(root):
    env = samples()["ci.failure"]
    _deliver(root, env)
    echo = [sys.executable, "-c", "import sys,json; print(json.load(sys.stdin)['payload']['note'])"]
    cfg, tg = _inbound_cfg({"ci": {"type": "command", "argv": echo}}), FakeTG()
    inbound.handle_update(_tap(env, "retry_note"), cfg, root, tg=tg, chat_id=CHAT)
    inbound.handle_update(_msg("use 2016-2025 only", reply_to=1), cfg, root, tg=tg, chat_id=CHAT)
    assert tg.sent[-1] == "use 2016-2025 only"


def test_ack_button_stops_realerts(root):
    env = samples()["ci.failure"]
    _deliver(root, env)
    inbound.handle_update(_tap(env, "ack"), _inbound_cfg(), root, tg=FakeTG(), chat_id=CHAT)
    assert _deliver(root, {**env, "id": "later"}, {**MOCK, "dedup_window_min": 0}).get("acknowledged")


def test_updates_the_hub_does_not_own_reach_the_legacy_handler_whole(root):
    seen = []
    run = lambda argv, input, **kw: seen.append(json.loads(input)) or subprocess.CompletedProcess(argv, 0, "", "")
    legacy_tap = {"update_id": 7, "callback_query": {"id": "c", "data": "ab12:approve", "message": {"chat": {"id": 42}}}}
    inbound.handle_update(legacy_tap, _inbound_cfg({"legacy": {"type": "command", "argv": ["x"]}}), root,
                          tg=FakeTG(), chat_id=CHAT, run=run)
    assert seen[0]["raw"] == legacy_tap


def test_other_chats_are_ignored(root):
    tg = FakeTG()
    inbound.handle_update(_msg("/retry 43", chat="99"), _inbound_cfg(), root, tg=tg, chat_id=CHAT)
    assert tg.sent == []


# ---------------------------------------------------------------- sanity

def test_preview_cli_runs():
    out = subprocess.run([sys.executable, "-m", "telefi", "preview"], capture_output=True, text=True,
                         timeout=30, encoding="utf-8")
    assert out.returncode == 0 and "details" in out.stdout


@pytest.mark.skipif(os.environ.get("TELEFI_LIVE") != "1", reason="live send: set TELEFI_LIVE=1 and TELEGRAM_*")
def test_live_send_each_sample_type(root):
    for env in samples().values():
        core.publish(env, spool=root)
    assert all(d["ok"] for r in core.drain({**core.load_config(), "mode": "live"}, spool=root) for d in r["deliveries"])


def test_any_number_of_replies_to_an_alert_reach_its_handler(root):
    env = samples()["ci.failure"]
    _deliver(root, env)                                          # MockAdapter: Telegram message id 1
    echo = [sys.executable, "-c", "import sys,json; print(json.load(sys.stdin)['payload']['note'])"]
    cfg, tg = _inbound_cfg({"ci": {"type": "command", "argv": echo}}), FakeTG()
    for note in ("first thought", "second thought"):
        inbound.handle_update(_msg(note, reply_to=1), cfg, root, tg=tg, chat_id=CHAT)
    assert tg.sent[-2:] == ["first thought", "second thought"]


class FakeAPI:
    """urlopen stand-in: one getUpdates batch, then every Bot API call succeeds."""
    def __init__(self, updates):
        self.updates = updates

    def __call__(self, req, timeout=10):
        method = req.full_url.rsplit("/", 1)[-1]
        result = self.updates if method == "getUpdates" else {"message_id": 900}
        self.updates = [] if method == "getUpdates" else self.updates
        body = json.dumps({"ok": True, "result": result}).encode()
        return type("R", (), {"status": 200, "read": lambda s: body, "__enter__": lambda s: s, "__exit__": lambda s, *a: False})()


def test_the_conversation_is_recorded_both_ways(root, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    inbound.poll_once(_inbound_cfg(), root=root, opener=FakeAPI([{"update_id": 5, **_msg("/help")}]))
    talk = [json.loads(l) for l in core.conversation_path(root).read_text().splitlines()]
    assert {t["dir"] for t in talk} == {"in", "out"}


# ---------------------------------------------------------------- command registry (/help, /list)

NEWS = {"schema": "telefi.commands.v1", "app": "news", "summary": "news monitor", "sink": "legacy",
        "commands": [{"verb": "status", "usage": "/news status", "summary": "what needs attention", "legacy_text": "/status"}]}
CI = {"schema": "telefi.commands.v1", "app": "ci", "summary": "dispatcher", "sink": "ci",
           "commands": [{"verb": "retry", "usage": "/ci retry <issue> [note]", "summary": "re-queue"}]}


def _registry_cfg(tmp_path, seen):
    paths = []
    for m in (NEWS, CI):
        p = tmp_path / f"{m['app']}.json"
        p.write_text(json.dumps(m))
        paths.append(str(p))
    cfg = _inbound_cfg({"legacy": {"type": "command", "argv": ["news"]}, "ci": {"type": "command", "argv": ["ci"]}})
    cfg["inbound"]["manifests"] = paths
    run = lambda argv, input, **kw: seen.append((argv[0], json.loads(input))) or subprocess.CompletedProcess(argv, 0, "ok\n", "")
    return cfg, run


def test_help_lists_every_app_command(root, tmp_path):
    cfg, run = _registry_cfg(tmp_path, [])
    tg = FakeTG()
    inbound.handle_update(_msg("/help"), cfg, root, tg=tg, chat_id=CHAT, run=run)
    assert "/news status" in tg.sent[-1] and "/ci retry" in tg.sent[-1]


def test_list_shows_each_app_on_one_line(root, tmp_path):
    cfg, run = _registry_cfg(tmp_path, [])
    tg = FakeTG()
    inbound.handle_update(_msg("/list"), cfg, root, tg=tg, chat_id=CHAT, run=run)
    assert any(line.startswith("/news") for line in tg.sent[-1].splitlines())


def test_app_verb_reaches_an_old_handler_in_its_old_form(root, tmp_path):
    seen = []
    cfg, run = _registry_cfg(tmp_path, seen)
    inbound.handle_update(_msg("/news status"), cfg, root, tg=FakeTG(), chat_id=CHAT, run=run)
    assert seen[0][1]["raw"]["message"]["text"] == "/status"


def test_app_verb_reaches_the_app_with_its_arguments(root, tmp_path):
    seen = []
    cfg, run = _registry_cfg(tmp_path, seen)
    inbound.handle_update(_msg("/ci retry 43 tighten it"), cfg, root, tg=FakeTG(), chat_id=CHAT, run=run)
    assert seen[0][0] == "ci" and seen[0][1]["args"] == ["43", "tighten", "it"]


# ---------------------------------------------------------------- rotation, holds, compatibility

def test_a_log_rotates_before_it_is_full_and_old_records_stay_readable(tmp_path, monkeypatch):
    from telefi import logs
    monkeypatch.setitem(logs.POLICY, "max_bytes", 1000)
    monkeypatch.setitem(logs.POLICY, "rotate_at", 0.5)
    path = tmp_path / "outbox.jsonl"
    for i in range(30):
        logs.append(path, {"i": i, "pad": "x" * 40})
    assert path.stat().st_size < 1000 * 0.5 + 100 and logs.read(path, last=3)[-1]["i"] == 29


def test_a_held_message_waits_until_its_time(root):
    env = core.envelope(source="ci", kind="failure", severity="error", title="t", not_before="2999-01-01T00:00:00+00:00")
    core.publish(env, spool=root)
    assert core.drain(MOCK, adapters={"telegram": MockAdapter("telegram"), "file": MockAdapter("file")}, spool=root) == []


def test_old_telegram_channel_sends_through_the_one_client():
    from telefi.compat import TelegramChannel
    sent = []
    def opener(req, timeout=10):
        sent.append(req.full_url)
        return type("R", (), {"status": 200, "read": lambda s: b'{"ok": true}', "__enter__": lambda s: s, "__exit__": lambda s, *a: False})()
    assert TelegramChannel("t", "1", opener=opener).send("title", "body", reply_markup={"inline_keyboard": []})
    assert sent[0].endswith("/sendMessage")


def test_every_example_file_meets_its_schema():
    from pathlib import Path
    ex = Path(__file__).resolve().parent.parent / "examples"
    kinds = {"hub.example.json": "hub_config.v1", "hub.minimal.json": "hub_config.v1", "commands.example.json": "commands.v1",
             "envelope.example.json": "envelope.v1", "inbound.example.json": "inbound.v1"}
    assert {n: schema.validate(json.loads((ex / n).read_text()), s) for n, s in kinds.items()} == {n: [] for n in kinds}


def test_relative_config_paths_stay_inside_the_home(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "HOME", tmp_path / "home")
    assert core.spool_root({"spool": "spool"}) == tmp_path / "home" / "spool"
