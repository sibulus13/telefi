"""Message templates (docs/MESSAGE_CATALOG.md): validation, rendering goldens, buttons, batching.
Golden files live in tests/golden/; UPDATE_GOLDEN=1 rewrites them (review the diff before committing)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from telefi import core, digest, inbound, messages, schema
from telefi.adapters import MockAdapter
from telefi.render import batch_html, batch_item, render_telegram
from golden_cases import CASES, make
from test_telefi import MOCK, _ads, _queue_files

GOLDEN = Path(__file__).parent / "golden"
ALL_SINKS = frozenset({"*"})
CFG = {**MOCK, "inbound": {**core.DEFAULT_CONFIG["inbound"], "enabled": True}}


def _compare(name: str, actual: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
    assert actual == path.read_text(encoding="utf-8"), f"{name} differs; UPDATE_GOLDEN=1 to accept"


def _single(env: dict) -> str:
    r = render_telegram(env, buttons=ALL_SINKS)
    return r["text"] + "\n--- keyboard\n" + json.dumps(r["reply_markup"], indent=1, ensure_ascii=False) + "\n"


def _batch(env: dict, label: str = "12:00") -> str:
    section = digest.section_of(env, digest.DEFAULT_RESEARCH_SOURCES)
    return batch_html([batch_item(env, 1, section)], label) + "\n"


# ---------------------------------------------------------------- goldens

@pytest.mark.parametrize("case", CASES)
def test_single_message_golden(case):
    _compare(f"{case}.single.txt", _single(make(case)))


@pytest.mark.parametrize("case", CASES)
def test_batched_golden(case):
    _compare(f"{case}.batch.txt", _batch(make(case)))


def test_four_item_batch_golden():
    items = [make(c) for c in ("research.refine", "research.drop", "run.failure", "run.receipt")]
    env = digest.build(items, None, "12:00")
    _compare("batch.four_items.txt", env["_html"] + "\n")


def test_every_template_has_a_golden_case_and_a_schema():
    assert {t for _, t, _ in CASES.values()} == set(messages.TEMPLATES)
    assert all((schema.TEMPLATE_DIR / f"{t}.json").exists() for t in messages.TEMPLATES)


def test_all_five_research_outcomes_are_covered():
    outcomes = {f["outcome"] for _, t, f in CASES.values() if t == "research.verdict"}
    assert outcomes == {"PURSUE", "REFINE", "DROP", "BLOCKED", "NO RESULT"}


# ---------------------------------------------------------------- validation

def _bad(**changes):
    env = make("research.refine")
    env.update(changes)
    return schema.validate(env, "envelope.v1")


def test_every_case_is_a_valid_envelope():
    assert all(schema.validate(make(c), "envelope.v1") == [] for c in CASES)


def test_free_text_envelopes_still_validate():
    assert schema.validate(core.envelope(source="ci", kind="info", severity="info", title="t"), "envelope.v1") == []


def test_unknown_template_is_rejected():
    assert any("not one of" in e for e in _bad(template="made.up"))


def test_missing_fact_is_rejected():
    facts = {k: v for k, v in make("research.refine")["facts"].items() if k != "name"}
    assert any("missing name" in e for e in _bad(facts=facts))


def test_unknown_fact_is_rejected():
    assert any("unexpected field mood" in e for e in _bad(facts={**make("research.refine")["facts"], "mood": "x"}))


def test_wrong_type_of_fact_is_rejected():
    assert any("expected number" in e for e in _bad(facts={**make("research.refine")["facts"], "issue": "712"}))


def test_outcome_must_be_one_of_the_five():
    assert any("not in" in e for e in _bad(facts={**make("research.refine")["facts"], "outcome": "MAYBE"}))


def test_non_url_link_is_rejected():
    facts = {**make("research.refine")["facts"], "links": {"card": "javascript:alert(1)"}}
    assert any("does not match" in e for e in _bad(facts=facts))


def test_facts_without_template_and_template_without_facts_are_rejected():
    env = make("deal.alert")
    assert schema.validate({k: v for k, v in env.items() if k != "template"}, "envelope.v1")
    assert schema.validate({k: v for k, v in env.items() if k != "facts"}, "envelope.v1")


def test_a_non_pursue_verdict_needs_its_insight():
    facts = {k: v for k, v in make("research.refine")["facts"].items() if k != "insight"}
    assert any("insight is required" in e for e in _bad(facts=facts))


def _backtest_details(backtest: dict) -> str:
    facts = {**make("research.refine")["facts"], "backtest": backtest}
    assert not schema.validate({**make("research.refine"), "facts": facts}, "envelope.v1")
    return next(x for x in messages.render("research.verdict", facts)["details"] if x.startswith("Backtest"))


def test_a_backtest_needs_only_metric_and_value():
    assert _backtest_details({"metric": "W1 sample value", "value": "+0.50 pp/yr"}) == "Backtest: W1 sample value +0.50 pp/yr"


def test_a_difference_metric_names_its_benchmark_without_a_benchmark_value():
    line = _backtest_details({"metric": "alpha", "value": "+1.10 pp/yr", "benchmark": "SPY"})
    assert line == "Backtest: alpha +1.10 pp/yr vs SPY"


def test_the_interval_follows_the_benchmark_and_max_dd_comes_last():
    line = _backtest_details({"period": "2019-2025", "metric": "alpha", "value": "+1.10 pp/yr", "benchmark": "SPY",
                              "ci": "95% CI -3.20 to +5.40", "max_dd": "-9%"})
    assert line == "Backtest: 2019-2025 · alpha +1.10 pp/yr vs SPY 95% CI -3.20 to +5.40 · max DD -9%"


def test_an_over_long_interval_is_rejected():
    facts = {**make("research.refine")["facts"], "backtest": {"metric": "m", "value": "v", "ci": "x" * 33}}
    assert _bad(facts=facts)


def test_a_decision_takes_at_most_three_options():
    opts = [{"id": f"o{i}", "label": f"Option {i}"} for i in range(4)]
    assert any("more than 3" in e for e in _bad(template="decision.ask", facts={"question": "q", "options": opts}))


def test_publish_refuses_bad_facts_with_a_clear_error(root):
    env = make("deal.alert")
    env["facts"] = {"product": "x"}
    with pytest.raises(ValueError, match="missing price"):
        core.publish(env, spool=root)


def test_drain_quarantines_a_bad_dropped_file_never_renders_it(root):
    env = make("deal.alert")
    env["facts"] = {"product": "x"}
    (root / "out").mkdir(parents=True)
    (root / "out" / f"{env['id']}.json").write_text(json.dumps(env), encoding="utf-8")
    tg = MockAdapter("telegram")
    rec = core.drain(CFG, adapters=_ads(tg), spool=root)[0]
    assert rec["rejected"] and tg.sent == [] and (root / "failed" / f"{env['id']}.json").exists()


# ---------------------------------------------------------------- buttons

def _keyboard(env, buttons):
    return render_telegram(env, buttons=buttons)["reply_markup"]["inline_keyboard"]


def _labels(rows, key):
    return [b["text"] for row in rows for b in row if key in b]


def test_action_buttons_need_a_sink_for_the_namespace():
    env = make("research.refine")                                   # source research
    assert _labels(_keyboard(env, frozenset({"research"})), "callback_data") == ["Re-run", "Hold"]
    assert _labels(_keyboard(env, frozenset({"assistant"})), "callback_data") == []


def test_the_wildcard_sink_covers_every_namespace():
    assert _labels(_keyboard(make("research.refine"), frozenset({"*"})), "callback_data") == ["Re-run", "Hold"]


def test_no_action_buttons_while_inbound_is_off():
    assert _labels(_keyboard(make("research.refine"), False), "callback_data") == []


def test_link_buttons_do_not_need_inbound():
    assert _labels(_keyboard(make("research.refine"), False), "url") == ["Card", "PR #45", "Result"]


def test_button_policy_reads_the_configured_sinks():
    cfg = {"inbound": {"enabled": True, "sinks": {"research": {"type": "queue"}}}}
    assert core.button_policy(cfg) == frozenset({"research"})
    assert core.button_policy({"inbound": {"enabled": False, "sinks": {"*": {"type": "queue"}}}}) is False


def test_drain_passes_the_sink_policy_to_the_channel(root):
    cfg = {**CFG, "inbound": {**CFG["inbound"], "sinks": {"assistant": {"type": "queue"}}}}
    core.publish(make("research.refine"), spool=root)
    core.publish(make("run.failure"), spool=root)
    tg = MockAdapter("telegram")
    core.drain(cfg, adapters=_ads(tg), spool=root)
    by_id = {s["envelope_id"]: s["rendered"]["reply_markup"]["inline_keyboard"] for s in tg.sent}
    assert _labels(by_id["g-research-refine"], "callback_data") == []
    assert _labels(by_id["g-run-failure"], "callback_data") == ["Retry", "Ack"]


def test_a_note_action_resolves_with_its_prompt_for_inbound():
    env = make("research.blocked")
    note = next(a for a in messages.actions_for(env) if a["id"] == "unblock")
    assert note["namespace"] == "research" and note["input"]["prompt"]


def test_the_note_button_is_added_to_a_decision():
    labels = [a["label"] for a in messages.actions_for(make("decision.ask"))]
    assert labels == ["Move to Friday", "Keep Tuesday", "Decide next week", "Answer with note"]


def test_free_text_actions_are_unchanged():
    env = core.envelope(source="ci", kind="failure", severity="error", title="t",
                        actions=[{"id": "retry", "label": "Retry", "namespace": "ci"}])
    assert _labels(_keyboard(env, True), "callback_data") == ["Retry"]
    assert _labels(_keyboard(env, frozenset()), "callback_data") == ["Retry"]


# ---------------------------------------------------------------- lanes and batching

def test_a_blocked_verdict_never_waits_for_a_batch(root):
    cfg = {**MOCK, "routes": [{"match": {}, "channels": ["telegram", "file"], "delivery": "batch"}]}
    core.publish(make("research.blocked"), spool=root)
    tg = MockAdapter("telegram")
    core.drain(cfg, adapters=_ads(tg), spool=root)
    assert len(tg.sent) == 1 and _queue_files(root) == []


def test_a_refine_verdict_batches_into_the_research_section():
    assert digest.section_of(make("research.refine"), digest.DEFAULT_RESEARCH_SOURCES) == "research"


def test_the_batch_uses_the_templates_own_bullet():
    assert "• 🔬 #712 ACME spread study: REFINE, higher costs next" in _batch(make("research.refine"))


def test_green_and_blue_verdicts_are_silent_others_sound():
    from telefi.render import is_silent
    assert [is_silent(make(c)) for c in ("research.pursue", "research.drop", "research.refine", "research.no_result")] \
        == [True, True, False, False]


def test_templated_env_builder_derives_kind_severity_and_title():
    env = make("system.down")
    assert (env["kind"], env["severity"], env["title"]) == ("failure", "critical", "Telegram unreachable since 21:50")


# ---------------------------------------------------------------- callback_data is never silently truncated

def _decision(option_id="friday", source="autofix"):
    return core.templated("decision.ask", {"question": "Move it?", "options": [{"id": option_id, "label": "Move"}]}, source=source)


def test_the_worst_known_source_with_the_longest_option_id_fits_telegrams_limit():
    env = _decision("o" * 16)
    assert schema.validate(env, "envelope.v1") == []
    assert all(len(b["callback_data"].encode()) <= 64 for row in _keyboard(env, ALL_SINKS) for b in row if "callback_data" in b)


def test_an_option_id_longer_than_the_schema_allows_is_rejected():
    env = {**_decision(), "facts": {"question": "q", "options": [{"id": "o" * 17, "label": "x"}]}}
    assert any("longer than 16" in e for e in schema.validate(env, "envelope.v1"))


def test_a_button_whose_callback_would_pass_64_bytes_is_rejected_naming_it():
    env = {**_decision("friday"), "id": "x" * 60}
    errors = schema.validate(env, "envelope.v1")
    assert any("decision.ask" in e and "friday" in e and "64" in e for e in errors)


def test_publish_refuses_such_an_envelope(root):
    with pytest.raises(ValueError, match="callback_data"):
        core.publish({**_decision("friday"), "id": "x" * 60}, spool=root)
