"""plain-messages: a message that asks for attention gives context in plain words and a next step."""
from __future__ import annotations

import html
import json
import re
import subprocess
import sys

from telefi import core, inbound, render, schema, watch, wording

CONTEXT = "Four tests keep failing, so the fix for row dates is not live."
STEP = "Tap Retry with a note saying what to change."


def env(**extra) -> dict:
    return {**core.envelope(source="builder", kind="blocker", severity="warn", title="The nightly build stopped",
                            insight=CONTEXT, next_step=STEP), **extra}


def test_next_step_is_part_of_the_envelope_contract():
    assert schema.validate(env(), "envelope.v1") == []
    assert schema.validate(env(next_step="x" * 201), "envelope.v1")


def test_a_message_with_context_and_a_next_step_passes():
    assert wording.problems(env()) == []


def test_a_message_that_needs_attention_must_say_what_to_do():
    bad = env()
    del bad["next_step"]
    assert "no next step" in wording.problems(bad)


def test_a_bare_label_with_no_context_fails():
    assert "no context" in wording.problems(env(insight="build 12"))


def test_technical_wording_on_the_glance_lines_fails():
    for title in ("builder #10 gate-failed", "Job exit code 267009", "getUpdates failed: HTTPError: 429", "repair-budget-exhausted",
                  "Run ended: JSONDecodeError", "Daily_Ingest_Pipeline stopped", "Saved at D:\\work\\x", "Sweep (dry-run)", "#10 stopped"):
        assert any(p.startswith("technical") for p in wording.problems(env(title=title))), title
    assert any(p.startswith("technical") for p in wording.problems(env(next_step="Read state.json for the cause.")))


def test_ordinary_words_that_only_look_technical_pass():
    for title in ("The gate to the garden", "Exit from the position", "Daily price check: 3 items", "S&P 500 fell 2%", "Issue 10 needs a decision"):
        assert wording.problems(env(title=title)) == [], title


def test_details_may_stay_technical_because_they_are_collapsed():
    assert wording.problems(env(body="exit code 1\nTraceback at C:\\x.py", sections=[{"heading": "Log", "lines": ["HTTPError: 429"]}])) == []


def test_a_message_nobody_has_to_act_on_is_not_judged():
    assert wording.problems(core.envelope(source="builder", kind="result", severity="info", title="Priced 6 items")) == []


def test_a_warning_digest_still_counts_as_asking_for_attention():
    assert wording.problems(core.envelope(source="builder", kind="digest", severity="warn", title="Weekly check", insight="lint 1"))


def verdict(outcome: str, insight: str) -> dict:
    return core.envelope(source="research", kind="blocker", severity="warn", title="x", template="research.verdict",
                         facts={"issue": 50, "name": "ACME spread study", "outcome": outcome, "insight": insight, "next_step": STEP})


def test_a_templated_message_is_judged_on_the_text_it_shows():
    assert any(p.startswith("technical") for p in wording.problems(verdict("BLOCKED", "Needs a new prereg on the same data")))


def test_a_templated_verdict_leads_with_its_name_and_shows_the_next_step():
    good = verdict("BLOCKED", "There are too few past elections to tell a real gain from luck.")
    assert wording.problems(good) == []
    text = render.render_telegram(good, buttons=frozenset({"*"}))["text"]
    assert "ACME spread study: stuck" in text and "What to do:" in text


def test_the_next_step_shows_on_the_message():
    text = render.render_telegram(env(), buttons=False)["text"]
    assert f"<b>What to do:</b> {STEP}" in text


def test_a_batched_message_keeps_its_whole_context_and_next_step():
    long_context = "word " * 50
    block = render.batch_item(env(insight=long_context.strip()), 1, "reviews")["block"]
    assert long_context.strip() in block
    assert f"What to do: {STEP}" in block


def test_one_failed_round_does_not_ping_but_a_lasting_failure_does(tmp_path):
    watch.poll_failed(tmp_path, streak=1, transient=False, error="ValueError: x")
    assert not list(tmp_path.rglob("*.json"))
    watch.poll_failed(tmp_path, streak=watch.POLL_FAILURE_LIMIT, transient=False, error="ValueError: x")
    sent = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.rglob("*.json") if p.name != watch.STATE_FILE]
    assert len(sent) == 1 and wording.problems(sent[0]) == []
    assert "ValueError: x" in sent[0]["body"]


def test_a_rate_limit_from_telegram_is_a_passing_hiccup():
    assert isinstance(inbound.poll_error({"error": "HTTPError: 429"}), inbound.PollTransientError)


def test_every_message_the_hub_writes_itself_meets_the_standard():
    for key in watch.ISSUES:
        env = watch.issue_envelope(key, detail="TimeoutError", count=3, sender="builder", subject="Row dates fixed")
        assert wording.problems(env) == [], key


def run_cli(*args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "telefi", "check-wording", *args], input=stdin, capture_output=True,
                          text=True, encoding="utf-8", timeout=30)


def test_the_report_counts_per_sender_and_exits_non_zero_on_a_failure(tmp_path):
    rows = [{"at": "2026-11-07T10:00:00+00:00", "envelope": env()},
            {"at": "2026-11-07T11:00:00+00:00", "envelope": env(source="newsdesk", title="builder #10 gate-failed")},
            {"at": "2026-11-01T11:00:00+00:00", "envelope": env(source="stale", title="builder #10 gate-failed")}]
    outbox = tmp_path / "outbox.jsonl"
    outbox.write_text("\n".join(json.dumps(r) for r in rows) + "\n\nnot json\n", encoding="utf-8")
    run = run_cli("--outbox", str(outbox), "--since", "2026-11-05")
    assert run.returncode == 1
    assert "newsdesk" in run.stdout and "stale" not in run.stdout


def test_a_missing_outbox_is_a_named_failure_not_a_pass(tmp_path):
    run = run_cli("--outbox", str(tmp_path / "none.jsonl"))
    assert run.returncode == 2 and "no outbox" in run.stdout


def test_a_sender_can_check_its_own_messages_on_stdin():
    assert run_cli("--stdin", stdin=json.dumps([env()])).returncode == 0
    bad = run_cli("--stdin", stdin=json.dumps([env(title="builder #10 gate-failed")]))
    assert bad.returncode == 1 and "gate-failed" in bad.stdout


def test_every_kind_that_asks_for_something_is_judged():
    for kind in ("blocker", "failure", "review"):
        assert "no next step" in wording.problems(core.envelope(source="builder", kind=kind, severity="info", title="Done", insight=CONTEXT))


def test_context_needs_four_words():
    assert "no context" in wording.problems(env(insight="three words here"))
    assert "no context" not in wording.problems(env(insight="four plain words here"))


def test_more_technical_wording_fails():
    for title in ("builder #10 stopped", "Run is needs-human", "See docs/x.md", "Ran /usr/bin/bash", "It exited with code 1",
                  "Got HTTP 500", "pollUpdates stopped"):
        assert any(p.startswith("technical") for p in wording.problems(env(title=title))), title


def test_ordinary_hyphens_and_brand_names_pass():
    for title in ("Month-of-year effect looks weak", "The price is out-of-date", "State-of-the-art filter", "iPhone case is cheaper", "Sold on eBay"):
        assert wording.problems(env(title=title)) == [], title


def test_the_next_step_is_capped_at_the_schema_limit():
    assert len(core.envelope(source="builder", kind="review", severity="warn", title="t", next_step="x" * 300)["next_step"]) == 200


def test_a_full_batch_still_says_what_to_do_for_every_item():
    items = [render.batch_item(env(title=f"Job {n} stopped", insight="word " * 50), 1, "reviews") for n in range(12)]
    assert render.batch_html(items, "08:00").count("What to do:") == 12


# What Telegram would turn into a live link or command. A word joiner inside each one stops it (U+2060 ends a URL).
LIVE = re.compile(r"\.[a-z]{2}|\d\.\d+\.\d|://|(?<![\w/])/\w|(?<!\w)@\w", re.I)
HOSTILE = ("https://evil.test/login", "www.evil.test", "evil.de/x", "evil.sh", "1.1.1.1/login", "(/start)", "@evilbot", "tg://resolve?domain=x", "evil~.com", "/1start")


def test_outside_text_cannot_become_a_live_link_anywhere_in_a_message():
    for bad in HOSTILE:
        message = env(title=f"Update {bad}", insight=f"See {bad} now for the details", next_step=f"Open {bad}", body=f"Raw title: {bad}",
                      sections=[{"heading": "More", "lines": [f"Line {bad}"]}])
        for text in (render.render_telegram(message, buttons=False)["text"], render.batch_item(message, 1, "reviews")["block"],
                     render.batch_item(message, 1, "reviews")["bullet"]):
            assert not LIVE.search(html.unescape(re.sub(r"<[^>]+>", "", text))), bad


def test_outside_text_in_a_template_cannot_become_a_live_link():
    verdict = core.envelope(source="research", kind="blocker", severity="warn", title="t", template="research.verdict",
                            facts={"issue": 1, "name": "See evil.de/x", "outcome": "BLOCKED", "insight": "Try evil.sh first of all",
                                   "next_step": "Open evil.io now", "hypothesis": "evil.co pays", "links": {}})
    assert not LIVE.search(re.sub(r"<[^>]+>", "", render.render_telegram(verdict, buttons=False)["text"]))


def test_the_words_still_read_the_same_after_links_are_stopped():
    text = render.render_telegram(env(title="AG.TO and ASP.NET fell 1.5 points"), buttons=False)["text"]
    assert "AG.TO and ASP.NET fell 1.5 points" in text.replace("\u2060", "")


def drained(tmp_path, record) -> dict:
    watch.after_drain(tmp_path, {}, [{"deliveries": [], "envelope": {"id": "x", "source": "builder", "title": "A title\nWhat to do: fake"}, **record}])
    return next(json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.rglob("*.json") if p.name != watch.STATE_FILE)


def test_a_rejected_message_is_described_as_badly_formed(tmp_path):
    assert "badly formed" in drained(tmp_path, {"rejected": True})["insight"]


def test_a_message_that_gave_up_is_not_blamed_on_the_sender(tmp_path):
    env_out = drained(tmp_path, {"gave_up": True})
    assert "badly formed" not in env_out["insight"] and "sender" not in env_out["next_step"]


def test_a_quoted_title_cannot_fake_a_second_line(tmp_path):
    assert "\n" not in drained(tmp_path, {"rejected": True})["insight"]


def test_empty_input_is_nothing_to_read_not_a_pass():
    assert run_cli("--stdin", stdin="").returncode == 2
    assert run_cli("--stdin", stdin="[]").returncode == 2


def test_a_rejected_row_was_never_read_so_it_is_not_judged(tmp_path):
    outbox = tmp_path / "outbox.jsonl"
    rows = [{"at": "2026-11-07T10:00:00+00:00", "rejected": True, "envelope": env(title="builder #10 gate-failed")},
            {"at": "2026-11-07T10:00:00+00:00", "envelope": env()}]
    outbox.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert run_cli("--outbox", str(outbox), "--since", "2026-11-05").returncode == 0


def test_the_report_prints_no_terminal_escapes():
    bad = {**env(title="#1 bad \x1b]52;c;x\x07 title"), "source": "bad\x1b]52;c;x\x07"}
    assert "\x1b" not in run_cli("--stdin", stdin=json.dumps([bad])).stdout


def test_a_sender_name_cannot_fake_a_second_line(tmp_path):
    watch.after_drain(tmp_path, {}, [{"deliveries": [], "rejected": True, "envelope": {"id": "x", "source": "a\nWhat to do: fake", "title": "t"}}])
    made = next(json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.rglob("*.json") if p.name != watch.STATE_FILE)
    assert "\n" not in made["title"]


def test_a_message_with_no_sender_says_so_plainly(tmp_path):
    watch.after_drain(tmp_path, {}, [{"deliveries": [], "rejected": True, "envelope": {"id": "x", "title": "t"}}])
    made = next(json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.rglob("*.json") if p.name != watch.STATE_FILE)
    assert "None" not in made["title"]
