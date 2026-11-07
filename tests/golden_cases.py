"""Fixed template fixtures for the golden-file tests (tests/golden/). Ids and times are fixed so output is exact."""
from __future__ import annotations

from telefi import core

CREATED_AT = "2026-10-28T12:00:00+00:00"
BT = {"period": "2019-2025", "metric": "Sharpe", "value": "0.9", "benchmark": "QQQ B&H", "benchmark_value": "1.1",
      "max_dd": "-9%"}
RESEARCH_LINKS = {"card": "https://notion.so/card-712", "pr": "https://github.com/o/r/pull/45",
                  "result": "https://example.com/result/712"}

# case name -> (source, template, facts)
CASES: dict[str, tuple[str, str, dict]] = {
    "research.pursue": ("research", "research.verdict", {
        "issue": 712, "name": "ACME spread study", "outcome": "PURSUE", "bullet_note": "start paper next",
        "hypothesis": "the spread on ACME covers costs", "backtest": {**BT, "value": "1.4"},
        "paper": {"status": "not started"}, "source_rules": "transcribed (copied from the source)",
        "links": RESEARCH_LINKS}),
    "research.refine": ("research", "research.verdict", {
        "issue": 712, "name": "ACME spread study", "outcome": "REFINE", "bullet_note": "higher costs next",
        "insight": "Edge holds only at low volume; re-run with higher costs",
        "hypothesis": "the spread on ACME covers costs", "backtest": BT, "paper": {"status": "not started"},
        "source_rules": "transcribed (copied from the source)", "why": "edge disappears at realistic fill rate",
        "links": RESEARCH_LINKS}),
    "research.drop": ("research", "research.verdict", {
        "issue": 710, "name": "breakout study", "outcome": "DROP",
        "insight": "Sharpe 0.2 vs QQQ B&H 1.1; no edge after costs",
        "backtest": {**BT, "value": "0.2", "max_dd": "-31%"}, "links": {"card": "https://notion.so/card-710"}}),
    "research.diff_ci": ("research", "research.verdict", {
        "issue": 703, "name": "index timing study", "outcome": "DROP",
        "insight": "H1 alpha vs SPY +1.10 pp/yr, interval spans zero",
        "backtest": {"period": "2010-2020", "metric": "H1 sample alpha", "value": "+1.10 pp/yr",
                     "benchmark": "SPY", "ci": "95% CI -3.20 to +5.40"},
        "links": {"card": "https://notion.so/card-703"}}),
    "research.blocked": ("research", "research.verdict", {
        "issue": 714, "name": "basket study", "outcome": "BLOCKED", "bullet_note": "needs tick data",
        "insight": "Missing data: ticks 2019-2022; cheapest unblock is a one-month sample",
        "hypothesis": "the basket beats cash after costs", "links": {"card": "https://notion.so/card-714"}}),
    "research.no_result": ("research", "research.verdict", {
        "issue": 716, "name": "seasonal study", "outcome": "NO RESULT",
        "insight": "Run timed out with no result file; Auto Fix is on it",
        "links": {"card": "https://notion.so/card-716"}}),
    "run.receipt": ("assistant", "run.receipt", {
        "agent": "Price Watch", "summary": "8 items priced, 0 alerts", "cost": "$1.72", "duration": "6 min",
        "counts": {"created": 0, "updated": 8}, "links": {"notion": "https://notion.so/run-1"}}),
    "run.failure": ("assistant", "run.failure", {
        "agent": "Researcher", "reason": "Shell refused the input; Auto Fix filed a fix",
        "problems": ["input contained a blocked command", "retry limit reached"], "cost": "$0.81", "duration": "1 min",
        "log_path": "C:/logs/researcher.log",
        "links": {"log": "https://example.com/log/9", "fix_issue": "https://github.com/o/r/issues/12"}}),
    "fix.merged": ("autofix", "fix.merged", {
        "problem": "stale cache key in Price Watch", "links": {"pr": "https://github.com/o/r/pull/51"}}),
    "fix.proposed": ("autofix", "fix.proposed", {
        "problem": "Researcher refuses long inputs", "links": {"pr": "https://github.com/o/r/pull/52"}}),
    "fix.diagnosis": ("autofix", "fix.diagnosis", {
        "problem": "Builder daemon hangs on shutdown", "links": {"diagnosis": "https://example.com/diag/3"}}),
    "decision.ask": ("builder", "decision.ask", {
        "question": "Move the deploy window to Friday?",
        "options": [{"id": "keep", "label": "Keep Tuesday", "implication": "no change"},
                    {"id": "friday", "label": "Move to Friday", "implication": "weekend soak time", "recommended": True},
                    {"id": "later", "label": "Decide next week"}]}),
    "system.down": ("telefi", "system.down", {
        "what": "Telegram unreachable", "since": "21:50", "links": {"log": "https://example.com/hub.log"}}),
    "system.recovered": ("telefi", "system.recovered", {"what": "Telegram", "after": "2 h 10 min"}),
    "deal.alert": ("deal", "deal.alert", {
        "product": "Widget Pro", "price": "$129", "discount": "-28%", "low_days": 90,
        "links": {"product": "https://example.com/p/widget-pro"}}),
}


def make(case: str) -> dict:
    """The case as an envelope with a fixed id and time."""
    source, template, facts = CASES[case]
    env = core.templated(template, facts, source=source)
    return {**env, "id": f"g-{case.replace('.', '-')}", "created_at": CREATED_AT}
