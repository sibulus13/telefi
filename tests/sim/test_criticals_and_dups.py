"""Owner rule: a critical is never lost, and a retry is never a second copy of a message already sent."""
from __future__ import annotations

import json
from datetime import timedelta

from conftest import result, stamps, utc
from telefi.simulate import default_config, plain

STEP = timedelta(minutes=1)
TELEGRAM_ONLY = [{"match": {}, "channels": ["telegram"]}]


def test_a_stale_retry_does_not_send_a_second_copy_after_its_replacement_went(make_hub):
    cfg = {**default_config("UTC"), "routes": TELEGRAM_ONLY}
    hub = make_hub(utc(9, 50), "UTC", step=STEP, cfg=cfg)
    hub.telegram.outages = [(utc(10, 0), utc(10, 5))]
    err = {"source": "ci", "kind": "failure", "severity": "error", "dedup_key": "K"}
    hub.advance_to(utc(10, 30), [(utc(10, 0), {**err, "title": "first"}), (utc(10, 5), {**err, "title": "second"})])
    assert [plain(s.text) for s in hub.telegram.sends] == ["🟠 Ci · second"]


def test_a_critical_survives_a_five_hour_outage_and_arrives_once_right_after(make_hub):
    hub = make_hub(utc(9, 50), "UTC", step=timedelta(minutes=2))
    hub.telegram.outages = [(utc(10, 0), utc(15, 0))]
    crit = {"source": "builder", "kind": "failure", "severity": "critical", "title": "Builder down"}
    hub.advance_to(utc(17, 0), [(utc(10, 0), crit)])
    sent = [s for s in hub.telegram.sends if "Builder down" in s.text]
    assert len(sent) == 1 and utc(15, 0) <= sent[0].at.astimezone(utc(0).tzinfo) <= utc(16, 4)
    assert not list((hub.root / "failed").glob("*.json"))


def test_a_critical_already_in_failed_comes_back_when_telegram_does(make_hub):
    hub = make_hub(utc(9, 0), "UTC", step=timedelta(minutes=5))
    env = hub.publish(source="builder", kind="failure", severity="critical", title="Lost before the fix")
    failed = hub.root / "failed"
    failed.mkdir()
    (hub.root / "out" / f"{env['id']}.json").replace(failed / f"{env['id']}.json")
    (failed / f"{env['id']}.error.txt").write_text("10 delivery attempts failed: telegram: HTTP 502", encoding="utf-8")
    hub.advance_to(utc(13, 0), [(utc(9, 30), result("a result for the noon batch"))])
    assert stamps(hub).count("28 12:00") == 1                                      # the noon batch went, Telegram was fine
    assert sum("Lost before the fix" in s.text for s in hub.telegram.sends) == 1   # once
