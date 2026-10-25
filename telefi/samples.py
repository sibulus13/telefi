"""One sample envelope per common message type, for `preview`, `samples` and the tests.

`python -m telefi preview` renders them as the owner sees them (collapsed, then expanded);
`python -m telefi samples --send` pushes them through the live pipeline once.
"""
from __future__ import annotations

from telefi.core import envelope

REPO = "https://github.com/example/project"
SHA = "0123456789abcdef0123456789abcdef01234567"


def ci_actions(issue: int) -> list[dict]:
    """A typical action set on a failed job: acknowledge, retry, retry with a note, redirect."""
    p = {"repo": "example/project", "issue": issue}
    return [
        {"id": "ack", "label": "Ack", "namespace": "hub"},
        {"id": "retry", "label": "Retry", "namespace": "ci", "payload": p},
        {"id": "retry_note", "label": "Retry with note", "namespace": "ci", "payload": p,
         "input": {"prompt": "What should the retry do differently?"}},
        {"id": "redirect", "label": "Redirect", "namespace": "ci", "payload": p,
         "input": {"prompt": "New direction (overrides the original where they conflict):"}},
    ]


def samples() -> dict[str, dict]:
    return {
        "ci.failure": envelope(
            source="ci", kind="failure", severity="error",
            title="Build #43 gate failed",
            insight="The health check found no API key in the fresh checkout; all tests passed.",
            body="[FAIL] health_check (ERROR)\nx api_credentials  API_KEY not set",
            evidence={"ref": "run 43-c84f43", "log_path": "~/.ci/gate-logs/43-c84f43.log",
                      "links": [{"label": "PR #44", "url": f"{REPO}/pull/44"}, {"label": "Issue #43", "url": f"{REPO}/issues/43"}]},
            actions=ci_actions(43), dedup_key="ci:failure:43-c84f43"),
        "ci.blocker": envelope(
            source="ci", kind="blocker", severity="critical",
            title="Build #33 needs you",
            insight="Its PR failed the merge gate 6 times; automatic retries stopped.",
            body="gate exited 1 -- 6 retries exhausted (exponential backoff)",
            evidence={"ref": "run 33-66dc03", "links": [{"label": "PR #39", "url": f"{REPO}/pull/39"}]},
            actions=ci_actions(33), dedup_key="ci:blocker:33-66dc03"),
        "job.failure": envelope(
            source="scheduler", kind="failure", severity="critical",
            title="nightly-export failed",
            insight="Exited 1; its output file is 39h old.",
            body="last exit code 1 -- the job ran and reported failure\nexport/latest.parquet is 39h old (max 30h)",
            evidence={"ref": "job_watchdog", "log_path": "/var/log/nightly-export.log"},
            actions=[{"id": "ack", "label": "Ack", "namespace": "hub"}],
            dedup_key="scheduler:nightly-export"),
        "research.result": envelope(
            source="research", kind="result", severity="info",
            title="Study #40: REJECT",
            insight="By the time a stock is up 50%, the move is priced in.",
            body="+0.38pp vs the benchmark over 63 days out of sample (CI -1.41 to +1.45; floor 2pp).",
            evidence={"ref": f"study 40 @ {SHA[:7]}", "links": [
                {"label": "Card", "url": f"{REPO}/blob/{SHA}/docs/cards/40.md"},
                {"label": "Write-up", "url": f"{REPO}/blob/{SHA}/docs/results/40.md"},
                {"label": "PR #46", "url": f"{REPO}/pull/46"}]},
            actions=[{"id": "redirect", "label": "Re-test with note", "namespace": "research",
                      "payload": {"study": 40}, "input": {"prompt": "What should a re-test change?"}}]),
        "info.notice": envelope(
            source="inbox", kind="info", severity="info",
            title="Inbox item done: check momentum after a 50% jump",
            insight="Filed as study #40; result REJECT.", evidence={"ref": "inbox e2d1e51315af"}),
    }
