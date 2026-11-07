"""TeleFi: the fundamental behaviours only.
Mock by default; TELEFI_LIVE=1 adds one real Telegram send per sample type."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from datetime import datetime, timezone

from telefi import core, digest, hub, inbound, schema
from telefi.__main__ import main
from telefi.adapters import MockAdapter
from telefi.render import callback_data, is_silent, render_telegram, utf16_len
from telefi.samples import samples

CHAT = "42"
MOCK = {**core.DEFAULT_CONFIG, "mode": "mock"}


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


def test_status_is_a_colour_dot_not_a_word():
    def dot(kind, severity):
        return render_telegram(core.envelope(source="x", kind=kind, severity=severity, title="t"), buttons=False)["text"][:1]
    assert [dot("failure", "critical"), dot("review", "info"), dot("result", "info"), dot("digest", "info")] == ["🔴", "🟡", "🟢", "🔵"]


def test_sections_stay_collapsed_until_expanded():
    env = core.envelope(source="news", kind="digest", severity="info", title="Daily", insight="3 on watch",
                        sections=[{"heading": "Watchlist", "lines": ["AG.TO sev 84"]}])
    glance, details = render_telegram(env, buttons=False)["text"].split("<blockquote expandable>")
    assert "AG.TO" not in glance and "AG.TO" in details


def test_ids_show_only_on_problems():
    def ref_shown(kind, severity):
        env = core.envelope(source="x", kind=kind, severity=severity, title="t", evidence={"ref": "run-123"})
        return "run-123" in render_telegram(env, buttons=False)["text"]
    assert (ref_shown("result", "info"), ref_shown("failure", "error")) == (False, True)


def test_done_and_fyi_arrive_silently_needs_you_makes_a_sound():
    def silent(kind, severity):
        return is_silent(core.envelope(source="x", kind=kind, severity=severity, title="t"))
    assert (silent("result", "info"), silent("review", "info"), silent("failure", "error")) == (True, False, False)


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


def test_a_legacy_handlers_reply_reaches_the_owner_and_its_tap_is_answered(root):
    run = lambda argv, input, **kw: subprocess.CompletedProcess(argv, 0, "log line\nOpen events: 2", "")
    tap = {"update_id": 8, "callback_query": {"id": "c9", "data": "ab12:approve", "message": {"chat": {"id": int(CHAT)}}}}
    tg = FakeTG()
    inbound.handle_update(tap, _inbound_cfg({"legacy": {"type": "command", "argv": ["x"]}}), root, tg=tg, chat_id=CHAT, run=run)
    assert tg.sent == [None, "Open events: 2"], "tap answered (no text), then the handler's last line sent"


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


# ---------------------------------------------------------------- pre-release review regressions (2026-10-25)

def test_an_undelivered_message_is_retried_not_lost_then_quarantined(root, monkeypatch):
    env = {**samples()["job.failure"], "severity": "error"}      # a critical never gives up (see test_notify_policy)
    core.publish(env, spool=root)
    down = {"telegram": MockAdapter("telegram", fail=True), "file": MockAdapter("file", fail=True)}
    rec = core.drain(MOCK, adapters=down, spool=root)[0]
    assert rec.get("retry_in_s") and (root / "out" / f"{env['id']}.json").exists()
    monkeypatch.setattr(core, "MAX_DELIVERY_ATTEMPTS", 1)
    core.write_state(root, "attempts.json", {})
    core.drain(MOCK, adapters=down, spool=root)
    assert (root / "failed" / f"{env['id']}.json").exists()


def test_a_telegram_outage_does_not_spin_the_poll_loop(root, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    def down(req, timeout=10):
        raise OSError("network unreachable")
    with pytest.raises(RuntimeError):
        inbound.poll_once(_inbound_cfg(), root=root, opener=down)


def test_ids_and_namespaces_cannot_escape_the_spool(root):
    env = core.envelope(source="x", kind="info", severity="info", title="t")
    with pytest.raises(ValueError):
        core.publish({**env, "id": "../../evil"}, spool=root)
    assert schema.validate({**samples()["ci.failure"], "actions": [{"id": "a", "label": "a", "namespace": "../x"}]}, "envelope.v1")


def test_a_non_web_link_is_refused_before_it_can_break_delivery(root):
    env = core.envelope(source="x", kind="info", severity="info", title="t",
                        evidence={"links": [{"label": "bad", "url": "file:///etc/passwd"}]})
    with pytest.raises(ValueError):
        core.publish(env, spool=root)


def test_a_hold_in_another_timezone_is_honoured(root):
    from datetime import timedelta, timezone
    later = (core.clock().astimezone(timezone(timedelta(hours=-7))) + timedelta(hours=1)).isoformat(timespec="seconds")
    core.publish(core.envelope(source="x", kind="failure", severity="error", title="t", not_before=later), spool=root)
    assert core.drain(MOCK, adapters={"telegram": MockAdapter("telegram"), "file": MockAdapter("file")}, spool=root) == []


def test_a_forged_button_cannot_redirect_to_another_namespace(root):
    env = samples()["ci.failure"]
    _deliver(root, env)
    seen = []
    run = lambda argv, input, **kw: seen.append(argv[0]) or subprocess.CompletedProcess(argv, 0, "ok\n", "")
    cfg = _inbound_cfg({"ci": {"type": "command", "argv": ["ci"]}, "evil": {"type": "command", "argv": ["evil"]}})
    forged = {"update_id": 9, "callback_query": {"id": "q", "data": callback_data("evil", "retry", env["id"]),
                                                 "message": {"chat": {"id": int(CHAT)}}}}
    inbound.handle_update(forged, cfg, root, tg=FakeTG(), chat_id=CHAT, run=run)
    assert seen == ["ci"]


# ---------------------------------------------------------------- digest lane

BEFORE = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
AFTER = datetime(2026, 10, 27, 19, 0, tzinfo=timezone.utc)
UTC_CFG = {**MOCK, "digest": {"at": "18:00", "timezone": "UTC"}}


def _digest_item(source, title, **kw):
    return core.envelope(source=source, kind="result", severity="info", title=title, delivery="digest", **kw)


def _collect(root, *envs):
    tg = MockAdapter("telegram")
    for e in envs:
        core.publish(e, spool=root)
    core.drain(MOCK, adapters={"telegram": tg, "file": MockAdapter("file")}, spool=root)
    return tg


def test_a_digest_envelope_is_held_not_sent(root):
    tg = _collect(root, _digest_item("alpha", "Nightly run finished"))
    assert tg.sent == [] and len(list((root / "digest").glob("*.json"))) == 1


def test_one_combined_message_at_the_time(root):
    _collect(root, _digest_item("alpha", "A1"), _digest_item("alpha", "A2"), _digest_item("beta", "B1"))
    tg = MockAdapter("telegram")
    assert digest.send_if_due(UTC_CFG, adapters={"telegram": tg, "file": MockAdapter("file")}, spool=root, now=BEFORE) is None
    digest.send_if_due(UTC_CFG, adapters={"telegram": tg, "file": MockAdapter("file")}, spool=root, now=AFTER)
    assert len(tg.sent) == 1 and "3 updates" in tg.sent[0]["rendered"]["text"]
    assert len(list((root / "digest" / "sent" / "2026-10-27").glob("*.json"))) == 3


def test_nothing_collected_sends_nothing(root):
    tg = MockAdapter("telegram")
    assert digest.send_if_due(UTC_CFG, adapters={"telegram": tg}, spool=root, now=AFTER) is None and tg.sent == []


def test_a_restart_does_not_resend_and_a_missed_time_sends_at_next_start(root):
    _collect(root, _digest_item("alpha", "A1"))
    tg = MockAdapter("telegram")
    ads = {"telegram": tg, "file": MockAdapter("file")}
    digest.send_if_due(UTC_CFG, adapters=ads, spool=root, now=AFTER)     # first start after 18:00 sends
    _collect(root, _digest_item("alpha", "A2"))
    digest.send_if_due(UTC_CFG, adapters=ads, spool=root, now=AFTER)     # restarted the same day: no second one
    assert len(tg.sent) == 1


def test_preview_shows_the_digest_and_sends_nothing(root, capsys, monkeypatch):
    _collect(root, _digest_item("alpha", "Nightly run finished"))
    monkeypatch.setenv("TELEFI_SPOOL", str(root))
    assert main(["digest", "--preview"]) == 0
    assert "Nightly run finished" in capsys.readouterr().out and len(list((root / "digest").glob("*.json"))) == 1


def test_a_long_title_is_cut_at_a_word(root):
    items = [_digest_item("alpha", "word " * 40 + str(i)) for i in range(12)]
    text = render_telegram(digest.build(items, AFTER), buttons=False)["text"]
    assert "word…" in text and len(text) <= 3900


# ---------------------------------------------------------------- digest lane: review fixes

def _ads(telegram=None, file=None):
    return {"telegram": telegram or MockAdapter("telegram"), "file": file or MockAdapter("file")}


def _queue_files(root, sub=""):
    return list((root / "digest" / sub).glob("*.json")) if sub else list((root / "digest").glob("*.json"))


def test_only_a_telegram_acceptance_counts_as_sent(root):
    _collect(root, _digest_item("alpha", "A1"))
    rec = digest.send_if_due(UTC_CFG, adapters=_ads(telegram=MockAdapter("telegram", fail=True)), spool=root, now=AFTER)
    assert rec["sent"] is False and len(_queue_files(root)) == 1 and "last_window" not in core.read_state(root, "digest.json")


def test_without_a_telegram_route_nothing_is_marked_sent(root):
    _collect(root, _digest_item("alpha", "A1"))
    file_only = {**UTC_CFG, "routes": [{"match": {}, "channels": ["file"]}]}
    digest.send_if_due(file_only, adapters=_ads(), spool=root, now=AFTER)
    assert len(_queue_files(root)) == 1 and "last_window" not in core.read_state(root, "digest.json")


def _long_items(n_sources, per_source):
    return [_digest_item(f"source{s}", " ".join(f"<word{s}-{i}-{w}>" for w in range(14)))
            for s in range(n_sources) for i in range(per_source)]


def test_a_max_size_digest_fits_in_one_message_with_tags_closed():
    env = digest.build(_long_items(7, 10), AFTER)
    text = render_telegram(env, buttons=False)["text"]
    assert utf16_len(text) <= 3900 and text.count("<blockquote") == text.count("</blockquote>")
    assert "in bell.jsonl" in text and not text.endswith("…")


def test_truncation_never_cuts_inside_a_tag_or_entity():
    from telefi.render import truncate_html
    cut = truncate_html("<b>" + "a&amp;" * 50 + "</b>", 20)
    assert len(cut) <= 20 and cut.endswith("…</b>") and not cut.replace("&amp;", "").count("&")


def test_a_digest_failure_does_not_stop_the_serve_round(monkeypatch, tmp_path):
    from telefi.hub import digest_round
    monkeypatch.setattr(digest, "send_if_due", lambda cfg, **kw: 1 / 0)
    digest_round(MOCK, tmp_path / "hub.log")
    assert "digest round failed" in (tmp_path / "hub.log").read_text(encoding="utf-8")


def test_an_unknown_timezone_fails_the_config_load(tmp_path):
    cfg = tmp_path / "hub.json"
    cfg.write_text(json.dumps({**MOCK, "digest": {"timezone": "Mars/Olympus"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="timezone"):
        core.load_config(cfg)


def test_a_corrupt_digest_file_is_quarantined_once(root):
    _collect(root, _digest_item("alpha", "A1"))
    (root / "digest" / "junk.json").write_text("{not json", encoding="utf-8")
    assert len(digest.collected(root)) == 1 and (root / "digest" / "bad" / "junk.json").exists()
    assert len(digest.collected(root)) == 1


def test_items_another_process_already_claimed_are_skipped(root):
    _collect(root, _digest_item("alpha", "A1"), _digest_item("alpha", "A2"))
    items = digest.collected(root)
    (root / "digest" / f"{items[0]['id']}.json").unlink()          # the other process won this one
    assert [i["id"] for i in digest.claim(root, items, "2026-10-27")] == [items[1]["id"]]


def test_items_left_by_a_crashed_sender_are_sent_after_restart(root):
    _collect(root, _digest_item("alpha", "A1"))
    digest.claim(root, digest.collected(root), "2026-10-26")        # claimed, then the process died
    assert _queue_files(root) == [] and digest.recover(root) == 1 and len(_queue_files(root)) == 1


def test_a_failed_send_backs_off_then_retries(root):
    from datetime import timedelta
    _collect(root, _digest_item("alpha", "A1"))
    bad = _ads(telegram=MockAdapter("telegram", fail=True))
    digest.send_if_due(UTC_CFG, adapters=bad, spool=root, now=AFTER)
    good = MockAdapter("telegram")
    digest.send_if_due(UTC_CFG, adapters=_ads(good), spool=root, now=AFTER + timedelta(minutes=1))
    assert good.sent == []
    digest.send_if_due(UTC_CFG, adapters=_ads(good), spool=root, now=AFTER + timedelta(minutes=6))
    assert len(good.sent) == 1


def test_items_after_a_manual_send_wait_for_the_next_days_digest(root):
    from datetime import timedelta
    _collect(root, _digest_item("alpha", "A1"))
    digest.send(UTC_CFG, adapters=_ads(), spool=root, now=BEFORE)    # --send marks 2026-10-27 done
    _collect(root, _digest_item("alpha", "A2"))
    tg = MockAdapter("telegram")
    digest.send_if_due(UTC_CFG, adapters=_ads(tg), spool=root, now=AFTER)
    assert tg.sent == []
    digest.send_if_due(UTC_CFG, adapters=_ads(tg), spool=root, now=AFTER + timedelta(days=1))
    assert len(tg.sent) == 1


# ---------------------------------------------------------------- digest lane: second review

class RaisingAdapter:
    def send(self, env, *, buttons):
        raise RuntimeError("boom")


def test_an_exception_mid_send_returns_the_items_and_records_a_backoff(root):
    _collect(root, _digest_item("alpha", "A1"))
    with pytest.raises(RuntimeError):
        digest.send_if_due(UTC_CFG, adapters={"telegram": RaisingAdapter()}, spool=root, now=AFTER)
    assert len(_queue_files(root)) == 1 and core.read_state(root, "digest.json")["failures"] == 1


NO_DIGEST_ROUTE = {**UTC_CFG, "routes": [{"match": {"kind": ["result"]}, "channels": ["telegram", "file"]},
                                         {"match": {}, "channels": ["file"]}]}


def test_an_item_is_not_held_when_the_digest_itself_could_not_be_sent(root):
    core.publish(_digest_item("alpha", "A1"), spool=root)
    tg = MockAdapter("telegram")
    core.drain(NO_DIGEST_ROUTE, adapters=_ads(tg), spool=root)
    assert len(tg.sent) == 1 and _queue_files(root) == []


def test_a_missing_digest_route_is_logged_once_a_day(root):
    for _ in range(2):
        digest.send_if_due(NO_DIGEST_ROUTE, adapters=_ads(), spool=root, now=AFTER)
    assert (root.parent / "hub.log").read_text(encoding="utf-8").count("digest cannot be sent") == 1


def _hold_lock(root, at):
    lock = root / "state" / "digest.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "at": at}), encoding="utf-8")


def test_recover_and_send_leave_a_live_senders_claim_alone(root):
    _collect(root, _digest_item("alpha", "A1"))
    digest.claim(root, digest.collected(root), "2026-10-27")
    _hold_lock(root, core.now_iso())                               # a live sender is mid-send
    assert digest.recover(root) == 0 and digest.send(UTC_CFG, adapters=_ads(), spool=root, now=AFTER)["sent"] is False


def test_a_stale_lock_is_taken_over(root):
    _collect(root, _digest_item("alpha", "A1"))
    _hold_lock(root, "2020-01-01T00:00:00+00:00")
    assert digest.send(UTC_CFG, adapters=_ads(), spool=root, now=AFTER)["sent"] is True


def test_a_half_written_lock_is_respected(root):
    _collect(root, _digest_item("alpha", "A1"))
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "state" / digest.LOCK_FILE).write_text("", encoding="utf-8")
    assert digest.send(UTC_CFG, adapters=_ads(), spool=root, now=AFTER)["sent"] is False


def test_releasing_an_item_that_vanished_does_not_crash(root):
    _collect(root, _digest_item("alpha", "A1"))
    items = digest.claim(root, digest.collected(root), "2026-10-27")
    (root / "digest" / "sending" / "2026-10-27" / f"{items[0]['id']}.json").unlink()
    digest._release(root, items, "2026-10-27", root / "digest")
    assert "vanished" in (root.parent / "hub.log").read_text(encoding="utf-8")


def test_the_failure_count_restarts_each_day_and_a_manual_send_failure_backs_off(root):
    from datetime import timedelta
    _collect(root, _digest_item("alpha", "A1"))
    bad = _ads(telegram=MockAdapter("telegram", fail=True))
    digest.send(UTC_CFG, adapters=bad, spool=root, now=AFTER)                      # manual --send failing
    digest.send(UTC_CFG, adapters=bad, spool=root, now=AFTER + timedelta(days=1))
    assert core.read_state(root, "digest.json")["failures"] == 1


def test_only_the_hubs_own_digest_loses_the_source_prefix():
    other = core.envelope(source="news", kind="digest", severity="info", title="Daily")
    hub_info = core.envelope(source="telefi", kind="info", severity="info", title="Hello")
    assert [render_telegram(e, buttons=False)["text"].startswith("🔵 News") for e in (other,)] == [True]
    assert render_telegram(hub_info, buttons=False)["text"].startswith("🔵 TeleFi")


def test_the_size_budget_counts_utf16_units(root):
    from telefi.render import telegram_text, utf16_len
    items = [_digest_item(f"source{s}", "😀" * 60 + f" {i}") for s in range(3) for i in range(10)]
    assert utf16_len(telegram_text(digest.build(items, AFTER))) <= 3900


def test_the_headline_counts_real_items_not_collapsed_lines():
    items = [_digest_item("alpha", "Disk 91%", dedup_key="disk") for _ in range(5)]
    assert "5 updates" in digest.build(items, AFTER)["title"]


# --- poll noise: transient getUpdates errors are one compact line, not a traceback ----------------

def _serve_script(monkeypatch, tmp_path, outcomes):
    """Run _serve with poll_once scripted: each outcome is an error string (a failed getUpdates) or None
    (success). KeyboardInterrupt ends the loop once the script is spent. Returns the hub.log events."""
    from telefi import __main__ as m
    script = iter(outcomes)
    monkeypatch.setattr(core, "HOME", tmp_path / "home")

    def poll(cfg, timeout_s):
        try:
            err = next(script)
        except StopIteration:
            raise KeyboardInterrupt
        if err:
            res = {"ok": False, "error": err}
            raise inbound.poll_error(res)
    monkeypatch.setattr(core, "load_config", lambda *a, **k: {"inbound": {"enabled": True}, "spool": str(tmp_path)})
    monkeypatch.setattr(core, "drain", lambda cfg, **kw: [])
    monkeypatch.setattr(core, "spool_root", lambda cfg: tmp_path)
    monkeypatch.setattr(core, "prune_done", lambda root: None)
    monkeypatch.setattr(inbound, "register_menu", lambda cfg: {"ok": True})
    monkeypatch.setattr(inbound, "poll_once", poll)
    monkeypatch.setattr(digest, "recover", lambda root: 0)
    monkeypatch.setattr(hub, "digest_round", lambda cfg, log, **kw: None)
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    with pytest.raises(KeyboardInterrupt):
        m._serve(1)
    lines = (core.HOME / "hub.log").read_text(encoding="utf-8").splitlines()
    return [json.loads(l) for l in lines if json.loads(l)["event"] not in ("serve started", "command menu")]


def test_a_transient_poll_error_logs_one_compact_line(monkeypatch, tmp_path):
    ev = _serve_script(monkeypatch, tmp_path, ["TimeoutError:"])
    assert [e["event"] for e in ev] == ["poll transient"]
    assert ev[0]["error_class"] == "TimeoutError" and "error" not in ev[0] and "Traceback" not in json.dumps(ev[0])


@pytest.mark.parametrize("err", ["RemoteDisconnected:", "ConnectionResetError:", "HTTPError: 502", "URLError:"])
def test_other_transient_errors_are_compact_too(monkeypatch, tmp_path, err):
    assert [e["event"] for e in _serve_script(monkeypatch, tmp_path, [err])] == ["poll transient"]


def test_consecutive_transient_errors_escalate_at_the_limit(monkeypatch, tmp_path):
    from telefi.__main__ import POLL_TRANSIENT_LIMIT as n
    ev = _serve_script(monkeypatch, tmp_path, ["TimeoutError:"] * n)
    assert [e["event"] for e in ev] == ["poll transient"] * (n - 1) + ["round failed"]
    assert "Traceback" in ev[-1]["error"]


def test_a_non_transient_poll_error_escalates_immediately(monkeypatch, tmp_path):
    ev = _serve_script(monkeypatch, tmp_path, ["HTTP 409: Conflict"])
    assert [e["event"] for e in ev] == ["round failed"] and "Traceback" in ev[0]["error"]


def test_an_http_4xx_is_not_transient(monkeypatch, tmp_path):
    assert [e["event"] for e in _serve_script(monkeypatch, tmp_path, ["HTTPError: 401"])] == ["round failed"]


def test_success_resets_the_transient_streak(monkeypatch, tmp_path):
    from telefi.__main__ import POLL_TRANSIENT_LIMIT as n
    k = n - 1
    ev = _serve_script(monkeypatch, tmp_path, ["TimeoutError:"] * k + [None] + ["TimeoutError:"] * k)
    assert [e["event"] for e in ev] == ["poll transient"] * (2 * k)


def test_a_failing_self_watch_never_stops_serve(monkeypatch, tmp_path):
    from telefi import watch

    def broken(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(watch, "poll_failed", broken)
    events = [e["event"] for e in _serve_script(monkeypatch, tmp_path, ["ConnectionRefusedError", None])]
    assert "self-watch failed" in events


def test_samples_show_every_type_during_quiet_hours(monkeypatch, capsys):
    """Regression: samples held for morning sat in a temp spool and were lost without a word."""
    from telefi.__main__ import _samples
    from telefi.samples import samples
    monkeypatch.setattr(core, "load_config", lambda: {**MOCK, "digest": {"quiet": ["00:00", "23:59"]}})
    _samples(False)
    assert len(capsys.readouterr().out.strip().splitlines()) == len(samples()) + 1   # one line each, plus the mode


def test_samples_say_when_a_batch_route_holds_them(monkeypatch, capsys):
    import json
    from pathlib import Path
    from telefi.__main__ import _samples
    batch = json.loads((Path(__file__).parent.parent / "examples" / "hub.batch.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(core, "load_config", lambda: {**core.DEFAULT_CONFIG, **batch, "mode": "mock"})
    _samples(False)
    assert "held for a batch" in capsys.readouterr().out
