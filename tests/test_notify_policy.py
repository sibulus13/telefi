"""Notification policy: lanes, batch windows, the batched message, self-watch.
Every test runs in a temp home (conftest); the clock is passed in, never read."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from telefi import core, digest, schema, watch
from telefi.adapters import MockAdapter
from telefi.render import fresh_lines, render_plain, render_telegram, utf16_len
from test_telefi import MOCK, _ads, _queue_files

DAY = (2026, 10, 28)
TG_LIMIT = 3900


def at(h, m=0, day=0):
    return datetime(*DAY, h, m, tzinfo=timezone.utc) + timedelta(days=day)


# a hub whose non-critical Telegram routes are batch routes
BATCH_ROUTES = [
    {"match": {"severity": ["critical"]}, "channels": ["telegram", "file"]},
    {"match": {"kind": ["blocker"]}, "channels": ["telegram", "file"]},
    {"match": {"severity": ["error", "warn"]}, "channels": ["telegram", "file"], "delivery": "batch"},
    {"match": {"kind": ["review", "result", "digest"]}, "channels": ["telegram", "file"], "delivery": "batch"},
    {"match": {}, "channels": ["file"]},
]
CFG = {**MOCK, "routes": BATCH_ROUTES, "digest": {"timezone": "UTC"}}
NOW_ROUTE = {**CFG, "routes": [{"match": {}, "channels": ["telegram", "file"]}]}


def item(kind="result", severity="info", title="t", source="research", **kw):
    return core.envelope(source=source, kind=kind, severity=severity, title=title, **kw)


def drain(env, cfg=CFG, root=None, now=None):
    core.publish(env, spool=root)
    tg = MockAdapter("telegram")
    recs = core.drain(cfg, adapters=_ads(tg), spool=root, now=now or at(12, 30))
    return tg, recs[0]


# ---------------------------------------------------------------- 1. lanes

def test_a_batch_route_holds_a_result_for_the_next_window(root):
    tg, rec = drain(item(), root=root)
    assert tg.sent == [] and rec["digest_held"] is True and len(_queue_files(root)) == 1


def test_critical_goes_now_even_on_a_batch_route(root):
    routes = [{"match": {}, "channels": ["telegram", "file"], "delivery": "batch"}]
    tg, _ = drain(item(kind="failure", severity="critical"), cfg={**CFG, "routes": routes}, root=root)
    assert len(tg.sent) == 1 and _queue_files(root) == []


def test_a_blocker_goes_now(root):
    tg, _ = drain(item(kind="blocker", severity="warn"), root=root)
    assert len(tg.sent) == 1


def test_a_sender_can_make_a_now_route_calmer_but_never_louder(root):
    tg, _ = drain(item(kind="failure", severity="error", delivery="batch"), cfg=NOW_ROUTE, root=root)
    assert tg.sent == [] and len(_queue_files(root)) == 1
    tg, _ = drain(item(kind="failure", severity="error", delivery="now"), root=root)   # the route says batch
    assert tg.sent == [] and len(_queue_files(root)) == 2


def test_a_senders_digest_value_still_means_batch(root):
    tg, _ = drain(item(delivery="digest"), cfg=NOW_ROUTE, root=root)
    assert tg.sent == [] and len(_queue_files(root)) == 1


def drain_all(root, cfg=CFG, now=None, ads=None):
    tg = MockAdapter("telegram")
    recs = core.drain(cfg, adapters=ads or _ads(tg), spool=root, now=now or at(12, 30))
    return tg, recs


def test_a_blocker_waits_in_the_spool_through_quiet_hours_and_goes_at_seven(root):
    core.publish(item(kind="blocker", severity="warn"), spool=root)
    tg, recs = drain_all(root, now=at(6, 59))
    assert tg.sent == [] and recs == [] and _queue_files(root) == []
    tg, _ = drain_all(root, now=at(7, 0))
    assert len(tg.sent) == 1


def test_critical_breaks_quiet_hours(root):
    tg, _ = drain(item(kind="failure", severity="critical"), root=root, now=at(23, 0))
    assert len(tg.sent) == 1


def test_a_batch_item_in_quiet_hours_joins_the_queue(root):
    tg, rec = drain(item(), root=root, now=at(23, 0))
    assert tg.sent == [] and rec["digest_held"] is True


def test_a_one_element_quiet_setting_is_invalid_and_harmless(root):
    bad = {**CFG, "digest": {"timezone": "UTC", "quiet": ["22:00"]}}
    assert schema.validate(bad, "hub_config.v1")
    tg, _ = drain(item(kind="blocker", severity="warn"), cfg=bad, root=root, now=at(23, 0))
    assert len(tg.sent) == 1


def test_quiet_hours_can_be_switched_off(root):
    off = {**CFG, "digest": {"timezone": "UTC", "quiet": []}}
    tg, _ = drain(item(kind="blocker", severity="warn"), cfg=off, root=root, now=at(23, 0))
    assert len(tg.sent) == 1


def test_info_stays_file_only(root):
    tg, rec = drain(item(kind="info"), root=root)
    assert tg.sent == [] and [d["channel"] for d in rec["deliveries"]] == ["file"]


def test_the_schemas_accept_route_delivery_and_envelope_batch():
    assert schema.validate(CFG, "hub_config.v1") == []
    assert schema.validate(item(delivery="batch"), "envelope.v1") == []
    bad = {**CFG, "routes": [{"match": {}, "channels": ["file"], "delivery": "soon"}]}
    assert schema.validate(bad, "hub_config.v1")


# ---------------------------------------------------------------- 2. windows

def _held(root, *envs):
    for e in envs:
        drain(e, root=root)


def send_at(root, when, tg=None):
    tg = tg or MockAdapter("telegram")
    rec = digest.send_if_due(CFG, adapters=_ads(tg), spool=root, now=when)
    return tg, rec


def test_default_windows_are_08_12_16_20():
    assert [f"{w:%H:%M}" for w in digest.windows(CFG)] == ["08:00", "12:00", "16:00", "20:00"]


def test_one_message_per_window_and_none_between(root):
    _held(root, item(title="a"), item(title="b"))
    tg = MockAdapter("telegram")
    assert send_at(root, at(7, 59), tg)[1] is None                       # before the first window
    send_at(root, at(12, 1), tg)
    assert len(tg.sent) == 1
    _held(root, item(title="c"))
    send_at(root, at(12, 40), tg)                                        # same window again: nothing
    assert len(tg.sent) == 1
    send_at(root, at(16, 0), tg)
    assert len(tg.sent) == 2


def test_an_empty_window_sends_nothing_and_late_items_wait_for_the_next_window(root):
    tg = MockAdapter("telegram")
    assert send_at(root, at(12, 0), tg)[1] is None
    _held(root, item(title="late"))
    send_at(root, at(12, 30), tg)
    assert tg.sent == []
    send_at(root, at(16, 0), tg)
    assert len(tg.sent) == 1


def test_a_missed_window_is_sent_at_the_next_start(root):
    _held(root, item(title="a"))
    tg, _ = send_at(root, at(14, 0))
    assert len(tg.sent) == 1


def test_digest_at_still_means_one_window_a_day(root):
    cfg = {**CFG, "digest": {"timezone": "UTC", "at": "18:00"}}
    assert [f"{w:%H:%M}" for w in digest.windows(cfg)] == ["18:00"]
    _held(root, item(title="a"))
    tg = MockAdapter("telegram")
    assert digest.send_if_due(cfg, adapters=_ads(tg), spool=root, now=at(12, 5)) is None
    digest.send_if_due(cfg, adapters=_ads(tg), spool=root, now=at(18, 5))
    assert len(tg.sent) == 1


def test_windows_win_over_at():
    cfg = {**CFG, "digest": {"timezone": "UTC", "at": "18:00", "windows": ["21:00", "09:30"]}}
    assert [f"{w:%H:%M}" for w in digest.windows(cfg)] == ["09:30", "21:00"]


def test_nothing_is_sent_in_quiet_hours(root):
    cfg = {**CFG, "digest": {"timezone": "UTC", "windows": ["20:00"]}}
    _held(root, item(title="a"))
    digest.send_if_due(cfg, adapters=_ads(MockAdapter("telegram", fail=True)), spool=root, now=at(21, 50))
    tg = MockAdapter("telegram")
    digest.send_if_due(cfg, adapters=_ads(tg), spool=root, now=at(23, 0))      # the retry is due but it is night
    assert tg.sent == []
    digest.send_if_due(cfg, adapters=_ads(tg), spool=root, now=at(7, 0, day=1))
    assert len(tg.sent) == 1


def test_a_failed_window_retries_with_the_existing_backoff(root):
    _held(root, item(title="a"))
    send_at(root, at(12, 0), MockAdapter("telegram", fail=True))
    assert core.read_state(root, "digest.json")["failures"] == 1
    tg, _ = send_at(root, at(12, 2))
    assert tg.sent == []
    tg, _ = send_at(root, at(12, 6))
    assert len(tg.sent) == 1


def test_the_old_state_file_still_marks_its_day_done(root):
    core.write_state(root, "digest.json", {"last_date": "2026-10-28"})
    _held(root, item(title="a"))
    cfg = {**CFG, "digest": {"timezone": "UTC", "at": "18:00"}}
    tg = MockAdapter("telegram")
    assert digest.send_if_due(cfg, adapters=_ads(tg), spool=root, now=at(19, 0)) is None and tg.sent == []


# ---------------------------------------------------------------- 3. the batched message

FOUR = [
    item(title="#710 breakout study: DROP", source="research", insight="No edge after costs", dedup_key="r710"),
    item(title="#712 ACME spread study: REFINE", source="research", insight="Edge holds at low volume",
         evidence={"links": [{"label": "Card", "url": "https://example.com/card/712"}]}),
    item(kind="failure", severity="error", title="Researcher run failed (input refused)", source="builder",
         evidence={"log_path": "D:/logs/run-9.log"}),
    item(title="Price Watch: 8 items priced, 0 alerts", source="price-watch"),
]


def build(items=FOUR, when=None):
    return digest.build(items, when or at(12, 3), label="12:00")


def html_of(env):
    return render_telegram(env, buttons=False)["text"]


def test_the_batch_follows_the_wireframe():
    text = html_of(build())
    lines = text.split("\n")
    assert lines[0] == "🔵 <b>4 updates · 12:00</b>"
    assert lines[1:5] == ["• 🔴 Researcher run failed (input refused)", "• 🔬 #710 breakout study: DROP",
                          "• 🔬 #712 ACME spread study: REFINE", "• ✅ Price Watch: 8 items priced, 0 alerts"]
    heads = re.findall(r"<blockquote expandable><b>(.*?)</b>", text)
    assert heads == ["▸ 🔴 Errors (1)", "▸ 🔬 Research (2)", "▸ ✅ Receipts (1)"]       # fixed order, empty ones absent


def test_the_batch_is_silent_and_sections_are_collapsed_quotes():
    env = build()
    assert env["kind"] == "digest" and env["silent"] is True
    text = html_of(env)
    assert text.count("<blockquote expandable>") == text.count("</blockquote>") == 3


def test_each_item_shows_at_most_three_lines_and_its_link():
    long_body = "\n".join(f"line {i}" for i in range(10))
    link = {"links": [{"label": "PR #716", "url": "https://example.com/pr/46"}]}
    env = build([item(title="T", body=long_body, insight="why", evidence=link), item(title="U", kind="info")])
    quote = re.search(r"<blockquote expandable><b>▸ 🔬(.*?)</blockquote>", html_of(env), re.S).group(1)
    block = quote.split("\n", 1)[1]
    assert block.count("\n") + 1 == 4 and 'href="https://example.com/pr/46"' in block and "line 5" not in block


def test_a_bullet_is_cut_to_about_70_characters():
    bullet = html_of(build([item(title="word " * 30), item(title="x")])).split("\n")[1]
    assert len(bullet) <= 70 + len("• 🔬 ") and bullet.endswith("…")


def test_a_single_item_window_is_the_normal_message_without_a_wrapper():
    one = item(title="Price Watch: 8 items priced", source="price-watch", insight="0 alerts")
    env = digest.build([one], at(12, 3), label="12:00")
    assert env is one and "updates" not in html_of(env)


def test_repeated_dedup_keys_collapse_to_one_item_with_a_count():
    same = [item(kind="failure", severity="error", title="Disk 91%", dedup_key="disk") for _ in range(3)]
    env = build(same + [item(title="x")])
    assert "Disk 91% ×3" in html_of(env) and env["title"] == "4 updates · 12:00"


def test_the_markup_is_escaped():
    text = html_of(build([item(title="<b>&"), item(title="y")]))
    assert "&lt;b&gt;&amp;" in text and "<b>&" not in text


def many(n, **kw):
    return [item(title=f"#{i} " + "result detail " * 6, insight="why " * 20, body="more " * 40, **kw) for i in range(n)]


def test_a_big_batch_fits_the_budget_drops_details_first_and_says_how_many():
    text = html_of(build(many(40)))
    assert utf16_len(text) <= TG_LIMIT
    assert text.count("• 🔬 #") >= 20 and re.search(r"\+\d+ more.* in bell\.jsonl$", text)


def test_details_go_before_bullets():
    text = html_of(build(many(12)))
    assert utf16_len(text) <= TG_LIMIT and text.count("• 🔬 #") >= 12 and "in bell.jsonl" in text


def test_a_batch_of_few_items_has_no_overflow_line():
    assert "bell.jsonl" not in html_of(build())


def test_tags_stay_balanced_at_every_size():
    for n in (2, 5, 9, 15, 30, 60):
        text = html_of(build(many(n)))
        assert utf16_len(text) <= TG_LIMIT
        for tag in ("b", "i", "blockquote", "a", "code"):
            assert len(re.findall(rf"<{tag}[ >]", text)) == len(re.findall(rf"</{tag}>", text)), (n, tag)


def test_emoji_heavy_items_still_fit_in_utf16_units():
    env = build([item(title="🔬" * 60, insight="🔥" * 100) for _ in range(30)])
    assert utf16_len(html_of(env)) <= TG_LIMIT


def test_the_plain_preview_shows_every_section():
    out = render_plain(build(), buttons=False)
    assert "4 updates · 12:00" in out and out.count("▸") >= 3 and "Errors (1)" in out


# ---------------------------------------------------------------- 4. self-watch

def issues(root):
    return [json.loads(p.read_text(encoding="utf-8")) for p in (root / "out").glob("*.json")]


def test_the_hub_raises_its_own_envelope(root):
    assert watch.raise_issue(root, "poll-failures", "TeleFi cannot reach Telegram", "5 failed polls in a row", now=at(12))
    (env,) = issues(root)
    assert env["source"] == "telefi" and env["kind"] == "failure" and env["severity"] == "error"
    assert schema.validate(env, "envelope.v1") == []


def test_one_issue_per_key_per_day(root):
    for hour in (9, 12, 15):
        watch.raise_issue(root, "poll-failures", "t", "i", now=at(hour))
    assert len(issues(root)) == 1
    watch.raise_issue(root, "poll-failures", "t", "i", now=at(9, day=1))
    watch.raise_issue(root, "digest-failed", "t", "i", now=at(9))
    assert len(issues(root)) == 3


def test_five_failed_polls_raise_one_issue(root):
    for streak in range(1, 7):
        watch.poll_failed(root, streak=streak, transient=True, error="TimeoutError", now=at(12))
    assert len(issues(root)) == 1 and issues(root)[0]["dedup_key"].startswith("telefi:poll-failures:")


def test_a_non_transient_serve_error_raises_at_once(root):
    watch.poll_failed(root, streak=1, transient=False, error="KeyError: 'x'", now=at(12))
    assert len(issues(root)) == 1


def test_a_digest_send_failing_three_times_raises(root):
    _held(root, item(title="a"))
    bad = MockAdapter("telegram", fail=True)
    for minute in (0, 6, 22):
        digest.send_if_due(CFG, adapters=_ads(bad), spool=root, now=at(12, minute))
    assert core.read_state(root, "digest.json")["failures"] == 3
    assert [e["dedup_key"].split(":")[1] for e in issues(root)] == ["digest-send-failed"]


def test_a_quarantined_item_raises(root):
    env = item(kind="failure", severity="error", title="t")
    core.publish(env, spool=root)
    p = root / "out" / f"{env['id']}.json"
    p.write_text(json.dumps({**env, "severity": "loud"}), encoding="utf-8")
    recs = core.drain(CFG, adapters=_ads(), spool=root, now=at(12))
    watch.after_drain(root, CFG, recs, now=at(12))
    assert [e["source"] for e in issues(root)] == ["telefi"]


def test_a_corrupt_digest_file_raises(root):
    (root / "digest").mkdir(parents=True)
    (root / "digest" / "junk.json").write_text("{nope", encoding="utf-8")
    digest.collected(root)
    assert len(issues(root)) == 1


def test_a_quarantined_telefi_envelope_does_not_loop(root):
    recs = [{"envelope": {"source": "telefi", "id": "x"}, "rejected": ["bad"], "deliveries": []}]
    watch.after_drain(root, CFG, recs, now=at(12))
    assert issues(root) == []


def test_telegram_down_for_two_hours_is_critical(root):
    watch.note_telegram(root, ok=False, now=at(9))
    watch.note_telegram(root, ok=False, now=at(10, 59))
    assert issues(root) == []
    watch.note_telegram(root, ok=False, now=at(11, 0))
    (env,) = issues(root)
    assert env["severity"] == "critical" and env["source"] == "telefi"


def test_one_telegram_success_resets_the_outage_clock(root):
    watch.note_telegram(root, ok=False, now=at(9))
    watch.note_telegram(root, ok=True, now=at(10))
    watch.note_telegram(root, ok=False, now=at(11))
    watch.note_telegram(root, ok=False, now=at(12, 30))
    assert issues(root) == []


def test_drained_telegram_failures_feed_the_outage_clock(root):
    cfg = {**CFG, "routes": [{"match": {}, "channels": ["telegram"]}]}
    core.publish(item(kind="failure", severity="error", title="t"), spool=root)
    recs = core.drain(cfg, adapters={"telegram": MockAdapter("telegram", fail=True)}, spool=root, now=at(9))
    watch.after_drain(root, cfg, recs, now=at(9))
    assert core.read_state(root, "self_watch.json")["telegram_down_since"]


def test_the_heartbeat_file_has_a_documented_shape(tmp_path):
    path = tmp_path / "heartbeat.json"
    watch.heartbeat(path, root=tmp_path, now=at(12, 1))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert {"at", "pid", "telegram_ok", "telegram_down_since"} <= set(data) and data["at"].startswith("2026-10-28T12:01")


def test_serve_writes_the_heartbeat_into_the_temp_home(tmp_path, monkeypatch):
    from telefi import __main__ as cli
    home = tmp_path / "home"

    class Stop(BaseException):
        pass

    def stop(_):
        raise Stop
    monkeypatch.setattr(cli.time, "sleep", stop)
    with pytest.raises(Stop):
        cli._serve(30)
    assert json.loads((home / "heartbeat.json").read_text(encoding="utf-8"))["pid"]


def test_the_example_config_with_batch_routes_is_valid():
    ex = Path(__file__).resolve().parent.parent / "examples" / "hub.batch.json"
    cfg = json.loads(ex.read_text(encoding="utf-8"))
    assert schema.validate(cfg, "hub_config.v1") == []
    assert cfg["digest"]["windows"] == ["08:00", "12:00", "16:00", "20:00"]


# ---------------------------------------------------------------- review fixes

def _wake(root):
    """Make every backing-off envelope due now."""
    state = core.read_state(root, "attempts.json")
    for v in state.values():
        v["next"] = "2000-01-01T00:00:00+00:00"
    core.write_state(root, "attempts.json", state)


def test_a_critical_whose_telegram_leg_failed_is_retried_for_telegram_only(root):
    core.publish(item(kind="failure", severity="critical", title="down"), spool=root)
    file = MockAdapter("file")
    drain_all(root, ads={"telegram": MockAdapter("telegram", fail=True), "file": file})
    assert len(list((root / "out").glob("*.json"))) == 1 and len(file.sent) == 1
    _wake(root)
    tg = MockAdapter("telegram")
    drain_all(root, ads={"telegram": tg, "file": file})
    assert len(tg.sent) == 1 and len(file.sent) == 1 and list((root / "out").glob("*.json")) == []


def test_the_retry_ceiling_still_quarantines_a_stuck_telegram_leg(root):
    core.publish(item(kind="blocker", severity="warn"), spool=root)
    bad = {"telegram": MockAdapter("telegram", fail=True), "file": MockAdapter("file")}
    for _ in range(core.MAX_DELIVERY_ATTEMPTS):
        drain_all(root, ads=bad)
        _wake(root)
    assert list((root / "out").glob("*.json")) == [] and len(list((root / "failed").glob("*.json"))) == 1


def test_the_heartbeat_reports_the_telegram_state(root, tmp_path):
    path = tmp_path / "hb.json"
    watch.heartbeat(path, root=root, now=at(9))
    assert json.loads(path.read_text(encoding="utf-8"))["telegram_ok"] is True
    watch.note_telegram(root, ok=False, now=at(9))
    watch.heartbeat(path, root=root, now=at(9, 5))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["telegram_ok"] is False and data["telegram_down_since"].startswith("2026-10-28T09:00")


def test_a_digest_send_failure_issue_goes_out_now_not_into_the_batch_queue(root):
    watch.raise_issue(root, "digest-send-failed", "t", "i", now=at(12), deliver_now=True)
    tg, recs = drain_all(root)
    assert len(tg.sent) == 1 and not recs[0].get("digest_held")


def test_a_catch_up_of_yesterdays_window_does_not_consume_todays(root):
    cfg = {**CFG, "digest": {"timezone": "UTC", "windows": ["08:00", "20:00"]}}
    core.write_state(root, "digest.json", {"last_window": "2026-10-27T08:00"})
    _held(root, item(title="a"))
    tg = MockAdapter("telegram")
    digest.send_if_due(cfg, adapters=_ads(tg), spool=root, now=at(7, 30))
    assert core.read_state(root, "digest.json")["last_window"] == "2026-10-27T20:00"
    _held(root, item(title="b"))
    digest.send_if_due(cfg, adapters=_ads(tg), spool=root, now=at(8, 5))
    assert len(tg.sent) == 2


def test_the_overflow_line_counts_unseen_items_and_names_the_details(root):
    env = build([item(title=f"receipt number {i} " + "x" * 50, source="price-watch") for i in range(80)])
    text = html_of(env)
    shown = text.count("• ✅")
    assert shown < 80 and f"+{80 - shown} more" in text and f"details for {shown}" in text


def test_one_failed_streak_does_not_outlive_two_quiet_hours(root):
    watch.note_telegram(root, ok=False, now=at(9))
    watch.note_telegram(root, ok=False, now=at(13))               # nothing was attempted in between
    assert issues(root) == []


def test_old_once_a_day_marks_are_pruned(root):
    watch.raise_issue(root, "a", "t", "i", now=at(9))
    watch.raise_issue(root, "b", "t", "i", now=at(9, day=3))
    assert set(core.read_state(root, "self_watch.json")["raised"]) == {"b"}


def test_a_message_stays_under_telegrams_entity_limit(root):
    env = build([item(title="a", evidence={"links": [{"label": "x", "url": f"https://e.co/{i}"}]}) for i in range(88)])
    text = html_of(env)
    assert len(re.findall(r"<(?:b|i|a|code|blockquote)[ >]", text)) <= 90 and utf16_len(text) <= TG_LIMIT


# ---------------------------------------------------------------- 6. found by the simulated-time suite

def _failing_telegram_then_ok(root, env, *, retry_after_s):
    core.publish(env, spool=root)
    down = MockAdapter("telegram", fail=True)
    core.drain(NOW_ROUTE, adapters=_ads(down), spool=root, now=at(10, 0))
    up = MockAdapter("telegram")
    core.drain(NOW_ROUTE, adapters=_ads(up), spool=root, now=at(10, 0) + timedelta(seconds=retry_after_s))
    return up


def test_a_keyed_alert_whose_telegram_leg_failed_is_retried_not_deduplicated_against_itself(root):
    """Regression (simulated outage): the file leg succeeded, so the retry saw 'already delivered' and dropped it."""
    up = _failing_telegram_then_ok(root, item(kind="failure", severity="critical", dedup_key="k"), retry_after_s=31)
    assert len(up.sent) == 1


def test_drain_backoff_follows_the_clock_it_is_given(root):
    """Regression: the retry timer read the machine clock, so a simulated or replayed `now` never waited."""
    up = _failing_telegram_then_ok(root, item(kind="failure", severity="critical"), retry_after_s=31)
    assert len(up.sent) == 1


# ---------------------------------------------------------------- 7. criticals are never lost; a retry is not a duplicate of a different message

TG_ONLY = {**CFG, "routes": [{"match": {}, "channels": ["telegram"]}]}


def test_a_retry_is_skipped_when_a_different_envelope_with_its_key_was_sent_meanwhile(root):
    a = item(kind="failure", severity="error", dedup_key="K")
    core.publish(a, spool=root)
    core.drain(TG_ONLY, adapters=_ads(MockAdapter("telegram", fail=True)), spool=root, now=at(10, 0))
    c = item(kind="failure", severity="error", dedup_key="K")
    core.publish(c, spool=root)
    tg = MockAdapter("telegram")
    core.drain(TG_ONLY, adapters=_ads(tg), spool=root, now=at(10, 0) + timedelta(seconds=5))       # C goes; A is backing off
    core.drain(TG_ONLY, adapters=_ads(tg), spool=root, now=at(10, 0) + timedelta(seconds=40))      # A's retry is due
    assert [s["envelope_id"] for s in tg.sent] == [c["id"]]


def _fail_all(root, env, times, cfg=NOW_ROUTE):
    core.publish(env, spool=root)
    down = MockAdapter("telegram", fail=True)
    for i in range(times):
        core.drain(cfg, adapters=_ads(down), spool=root, now=at(10, 0) + timedelta(hours=i * 2))


def test_a_critical_never_gives_up_other_messages_do(root, monkeypatch):
    monkeypatch.setattr(core, "MAX_DELIVERY_ATTEMPTS", 2)
    crit, err = item(kind="failure", severity="critical"), item(kind="failure", severity="error")
    _fail_all(root, crit, 4)
    _fail_all(root, err, 4)
    assert (root / "out" / f"{crit['id']}.json").exists() and (root / "failed" / f"{err['id']}.json").exists()


def test_a_waiting_critical_retries_no_less_often_than_hourly(root, monkeypatch):
    monkeypatch.setattr(core, "MAX_DELIVERY_ATTEMPTS", 2)
    crit = item(kind="failure", severity="critical")
    _fail_all(root, crit, 6)
    wait = core.read_state(root, "attempts.json")[crit["id"]]["next"]
    assert datetime.fromisoformat(wait) - at(10, 0) - timedelta(hours=10) <= timedelta(seconds=core.RETRY_MAX_S)


def _quarantined_critical(root, reason="10 delivery attempts failed: telegram: HTTP 502"):
    crit = item(kind="failure", severity="critical")
    (root / "failed").mkdir(parents=True)
    (root / "failed" / f"{crit['id']}.json").write_text(json.dumps(crit), encoding="utf-8")
    (root / "failed" / f"{crit['id']}.error.txt").write_text(reason, encoding="utf-8")
    return crit


def test_a_critical_quarantined_before_this_change_is_requeued_on_the_next_telegram_success(root):
    crit = _quarantined_critical(root)
    watch.note_telegram(root, ok=True, now=at(12))
    assert (root / "out" / f"{crit['id']}.json").exists() and not (root / "failed" / f"{crit['id']}.json").exists()


def test_a_rejected_critical_is_not_requeued(root):
    crit = _quarantined_critical(root)
    bad = {**crit, "severity": "bogus"}
    (root / "failed" / f"{crit['id']}.json").write_text(json.dumps(bad), encoding="utf-8")
    watch.note_telegram(root, ok=True, now=at(12))
    assert (root / "failed" / f"{crit['id']}.json").exists()


def test_nothing_is_requeued_while_telegram_still_fails(root):
    crit = _quarantined_critical(root)
    watch.note_telegram(root, ok=False, now=at(12))
    assert (root / "failed" / f"{crit['id']}.json").exists()


# ---------------------------------------------------------------- repeated lines inside one item

def test_a_detail_line_equal_to_the_insight_or_title_is_not_repeated():
    env = item(kind="failure", severity="error", title="Brief failed", insight="6 attempts in 6h",
               body="6 attempts in 6h\n  brief   FAILED \nreal detail",
               sections=[{"heading": "Why", "lines": ["🔴 6 attempts in 6h", "real detail", "real detail"]}])
    quote = re.search(r"<blockquote expandable>(.*?)</blockquote>", html_of(env), re.S).group(1)
    assert "6 attempts" not in quote and quote.count("real detail") == 1


@pytest.mark.parametrize("a, b", [
    ("-5% drawdown", "5% drawdown"),
    ("+5% gain", "-5% gain"),
    ("$100 loss", "100 loss"),
    ("(3) failed", "3) failed"),
    ("✅ deploy ok", "❌ deploy ok"),
    ("⚠️ deploy ok", "🟢 deploy ok"),
])
def test_lines_differing_in_a_meaning_bearing_leading_character_are_both_kept(a, b):
    assert fresh_lines([b], seen=[a]) == [b]


@pytest.mark.parametrize("a, b", [
    ("6 attempts in 6h", "  6 ATTEMPTS   in 6h"),
    ("• views-commit-failed", "views-commit-failed"),
    ("- views-commit-failed", "* Views-Commit-Failed"),
])
def test_bullets_and_case_and_spacing_alone_do_not_make_a_line_new(a, b):
    assert fresh_lines([b], seen=[a]) == []


def test_a_batch_block_does_not_repeat_the_title_or_insight():
    env = build([item(title="Wrote a brief", insight="Wrote a brief", body="Wrote a brief\nsecond"), item(title="x")])
    assert html_of(env).count("Wrote a brief") == 2          # bullet + block headline only
    assert "second" in html_of(env)


def test_a_repeated_failure_keeps_its_first_position_and_its_newest_content():
    first = item(kind="failure", severity="error", title="Brief failed (attempt 1)", dedup_key="brief:2026-10-31")
    other = item(kind="failure", severity="error", title="Other failed", dedup_key="other")
    last = item(kind="failure", severity="error", title="Brief failed (attempt 3)", dedup_key="brief:2026-10-31")
    bullets = [l for l in html_of(build([first, other, last])).split("\n") if l.startswith("• ")]
    assert "attempt 3) ×2" in bullets[0] and "Other failed" in bullets[1] and len(bullets) == 2


# ---------------------------------------------------------------- twice-weekly digest
WEEKLY = {**CFG, "digest": {"timezone": "UTC", "windows": ["08:00"], "days": ["Mon", "Thu"]}}
MON_0900 = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)


def test_days_limit_a_window_to_those_weekdays():
    assert digest.window_key(WEEKLY, MON_0900) == "2026-11-09T08:00"
    assert digest.window_key(WEEKLY, MON_0900 + timedelta(days=1)) is None


def test_a_missed_window_is_still_found_days_later(tmp_path):
    wednesday = MON_0900 + timedelta(days=2)
    core.write_state(tmp_path, digest.STATE_FILE, {"last_window": "2026-11-05T08:00"})
    assert digest.pending_window(WEEKLY, tmp_path, wednesday) == "2026-11-09T08:00"


def test_no_days_means_every_day():
    cfg = {**CFG, "digest": {"timezone": "UTC", "windows": ["08:00"]}}
    assert digest.window_key(cfg, MON_0900 + timedelta(days=1)) == "2026-11-10T08:00"


def _rollup_item(source, title, kind="result", severity="info", key=None):
    return core.envelope(source=source, kind=kind, severity=severity, title=title, dedup_key=key or f"{source}:{title}:{id(object())}")


def test_rollup_counts_each_project_and_collapses_repeated_runs():
    items = [_rollup_item("assistant", "Price Watch run done", key=f"run:{n}") for n in range(3)]
    items += [_rollup_item("assistant", "Researcher blocked", kind="failure", severity="error"),
              _rollup_item("builder", "PR ready", kind="review")]
    html_text = digest.build(items, MON_0900, "Mon", rollup=True)["_html"]
    assert "3 ok, 1 failed" in html_text
    assert html_text.count("Price Watch run done") <= 2       # a bullet and its detail, once, not per run
    assert "×3" in html_text


def test_rollup_puts_what_needs_the_owner_first():
    items = [_rollup_item("assistant", "Run done"), _rollup_item("builder", "PR ready", kind="review"),
             _rollup_item("assistant", "Run failed", kind="failure", severity="error")]
    html_text = digest.build(items, MON_0900, "Mon", rollup=True)["_html"]
    assert html_text.index("Needs you") < html_text.index("Stalls") < html_text.index("Done")


def test_hub_config_accepts_the_twice_weekly_settings():
    cfg = {**MOCK, "digest": {"windows": ["08:00"], "days": ["Mon", "Thu"], "rollup": True}}
    assert not [e for e in schema.validate(cfg, "hub_config.v1") if "digest" in e]
    bad = {**MOCK, "digest": {"days": ["Monday"]}}
    assert [e for e in schema.validate(bad, "hub_config.v1") if "digest" in e]


# ---------------------------------------------------------------- state files
def test_a_corrupt_state_file_is_set_aside_not_fatal(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "attempts.json").write_bytes(b"\x00\x00")
    assert core.read_state(tmp_path, "attempts.json") == {}
    assert not (state / "attempts.json").exists()
    assert list(state.glob("attempts.json.corrupt*"))
