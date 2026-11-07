"""Scenarios 5-9: a Telegram outage, a burst, duplicates, a crash mid-send, two hubs."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import timedelta

from conftest import result, stamps, utc
from telefi import digest
from telefi.render import utf16_len
from telefi.simulate import SimHub, plain

ENTITY = re.compile(r"<(?:b|i|a|code|blockquote)[ >]")


def hub_log(hub: SimHub, event: str) -> list[dict]:
    rows = [json.loads(line) for line in (hub.root.parent / "hub.log").read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r["event"] == event]


def beat_at(hub: SimHub, when) -> dict:
    return next(b for at, b in hub.beats if at == when)


# ---------------------------------------------------------------- 5. Telegram outage 10:00-14:30

def outage_hub(make_hub):
    hub = make_hub(utc(9, 0), "UTC")
    hub.telegram.outages = [(utc(10, 0), utc(14, 30))]
    arrivals = [(utc(9, 30), result("held for noon", source="research")),
                (utc(13, 0), {"source": "builder", "kind": "failure", "severity": "critical", "title": "Builder down"})]
    hub.advance_to(utc(17, 0), arrivals)
    return hub


def test_the_noon_batch_retries_with_backoff_and_is_delivered_once_after_recovery(make_hub):
    hub = outage_hub(make_hub)
    tries = hub_log(hub, "digest send failed")
    assert [t["retry_in_min"] for t in tries[:3]] == [5, 15, 60]                  # the documented backoff
    batch = [s for s in hub.telegram.sends if "held for noon" in s.text]
    assert len(batch) == 1 and batch[0].at >= utc(14, 30)


def test_the_hub_raises_one_self_watch_error_and_one_critical_after_two_hours(make_hub):
    hub = outage_hub(make_hub)
    texts = [plain(s.text) for s in hub.telegram.sends]
    assert sum("could not send the batch message" in t for t in texts) == 1       # once, however many retries
    assert sum("Telegram sends have failed for 2 hours" in t for t in texts) == 1
    critical = next(s for s in hub.telegram.sends if "failed for 2 hours" in s.text)
    assert critical.at >= utc(14, 0) and not critical.silent                      # not before two hours of failing


def test_the_heartbeat_tells_when_telegram_went_down_and_came_back(make_hub):
    hub = outage_hub(make_hub)
    before, during, late, after = (beat_at(hub, utc(h, m)) for h, m in ((11, 59), (12, 30), (14, 25), (16, 59)))
    assert before["telegram_ok"] is True and before["telegram_down_since"] is None
    assert during["telegram_ok"] is False and during["telegram_down_since"].startswith("2026-10-28T12:0")
    assert late["telegram_down_since"] == during["telegram_down_since"]            # the streak keeps its start
    assert after["telegram_ok"] is True and after["telegram_down_since"] is None


def test_the_file_leg_is_written_once_per_message_despite_telegram_retries(make_hub):
    hub = outage_hub(make_hub)
    per_message = Counter(env_id for _, env_id in hub.file.writes)
    assert per_message and max(per_message.values()) == 1


def test_nothing_sent_during_the_outage_is_recorded_as_sent(make_hub):
    hub = outage_hub(make_hub)
    assert all(not (utc(10, 0) <= s.at < utc(14, 30)) for s in hub.telegram.sends)


# ---------------------------------------------------------------- 6. burst

def test_sixty_results_in_one_window_make_one_message_within_telegram_limits(make_hub):
    hub = make_hub(utc(8, 30), "UTC", step=timedelta(minutes=5))
    arrivals = [(utc(9, 0) + timedelta(seconds=30 * i),
                 result(f"#{100 + i} experiment {i:02d} finished with a long descriptive verdict text",
                        evidence={"links": [{"label": "Card", "url": f"https://example.com/c/{i}"}]}))
                for i in range(60)]
    hub.advance_to(utc(12, 30), arrivals)
    assert stamps(hub) == ["28 12:00"]
    text = hub.telegram.sends[0].text
    assert utf16_len(text) <= 3900 and len(ENTITY.findall(text)) <= 90


def test_the_plus_n_more_note_counts_exactly_the_bullets_that_were_cut(make_hub):
    hub = make_hub(utc(8, 30), "UTC", step=timedelta(minutes=5))
    arrivals = [(utc(9, 0) + timedelta(seconds=30 * i),
                 result(f"#{100 + i} experiment {i:02d} finished with a long descriptive verdict text")) for i in range(60)]
    hub.advance_to(utc(12, 30), arrivals)
    text = plain(hub.telegram.sends[0].text)
    shown = sum(line.startswith("• ") for line in text.splitlines())
    cut = int(re.search(r"\+(\d+) more", text).group(1))
    assert text.startswith("🔵 60 updates · 12:00") and 0 < shown < 60 and shown + cut == 60


# ---------------------------------------------------------------- 7. dedup

def test_one_key_five_times_across_a_window_is_one_bullet(make_hub):
    hub = make_hub(utc(8, 30), "UTC", step=timedelta(minutes=5))
    same = [(utc(9, 0) + timedelta(minutes=20 * i), result("Disk 91% on build host", source="ci", dedup_key="disk")) for i in range(5)]
    hub.advance_to(utc(12, 30), same + [(utc(10, 0), result("Other thing done", source="assistant"))])
    text = plain(hub.telegram.sends[0].text)
    assert len(hub.telegram.sends) == 1 and text.count("• ") == 2 and text.count("Disk 91%") >= 1
    assert text.startswith("🔵 2 updates")


def test_five_copies_arriving_together_collapse_to_one_bullet_with_a_count(make_hub):
    hub = make_hub(utc(8, 30), "UTC", step=timedelta(minutes=5))
    same = [(utc(9, 0), result("Disk 91% on build host", source="ci", dedup_key="disk")) for _ in range(5)]
    hub.advance_to(utc(12, 30), same + [(utc(10, 0), result("Other thing done", source="assistant"))])
    text = plain(hub.telegram.sends[0].text)
    assert text.startswith("🔵 6 updates") and "• 🔬" not in text and "Disk 91% on build host ×5" in text


# ---------------------------------------------------------------- 8. crash mid-send

def test_a_crash_after_telegram_accepted_loses_nothing_and_duplicates_at_most_once(make_hub):
    hub = make_hub(utc(8, 30), "UTC", step=timedelta(minutes=5))
    hub.kill_after_next_batch_send()
    hub.advance_to(utc(12, 30), [(utc(9, 0), result("first")), (utc(9, 30), result("second"))])
    assert hub.crashed and len(hub.telegram.sends) == 1                           # sent, but never recorded
    assert hub.restart() == 2                                                     # both items claimed by the dead sender
    hub.advance_to(utc(14, 0))
    texts = [plain(s.text) for s in hub.telegram.sends]
    assert len(texts) == 2 and texts[0] == texts[1]                               # one duplicate, same content
    sent = list((hub.root / "digest" / "sent").glob("*/*.json"))
    assert len(sent) == 2 and not list((hub.root / "digest").glob("*.json"))      # nothing lost, nothing waiting


# ---------------------------------------------------------------- 9. two hubs

def test_two_hubs_on_one_spool_send_a_window_once(make_hub):
    a = make_hub(utc(8, 30), "UTC", step=timedelta(minutes=5))
    second_hub_saw = []

    def second_hub_wakes_while_a_is_sending():
        a.telegram.during_send = None
        second_hub_saw.append(digest.send_if_due(a.cfg, adapters=a.adapters, spool=a.root, now=a.clock()))

    a.telegram.during_send = second_hub_wakes_while_a_is_sending
    a.advance_to(utc(12, 30), [(utc(9, 0), result("one")), (utc(9, 30), result("two"))])
    assert stamps(a) == ["28 12:00"]                                              # the other hub sent nothing
    assert second_hub_saw and second_hub_saw[0]["sent"] is False and "in progress" in second_hub_saw[0]["error"]
