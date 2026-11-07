"""Message templates: one renderer per template turns validated `facts` into a view.

The canonical design is docs/MESSAGE_CATALOG.md. A producer sends `template` + `facts` in its envelope
(envelope.v1); TeleFi owns the wording, emoji, order, links and buttons. Facts are validated against
telefi/templates/<template>.json (schema.validate does it wherever an envelope is validated) plus the
cross-field rules in `extra_errors`.

A view is a plain dict:
    dot, headline, insight, details (<= 6 lines), links (<= 3 {label, url}), actions (<= 3, decision.ask 4),
    bullet (one line for a batch), section (errors | research | reviews | receipts), kind, severity

This module is pure (no I/O, no other telefi imports) so render.py, schema.py, core.py and inbound.py can use it.
"""
from __future__ import annotations

import re
from typing import Callable

CALLBACK_MAX = 64              # Telegram's callback_data limit, bytes
CALLBACK_PREFIX = "h"          # hub-made buttons; anything else belongs to a legacy handler
CALLBACK_SEP = "|"
MAX_DETAILS = 6
MAX_LINKS = 3
SEP = " · "
PR_NUMBER = re.compile(r"/pull/(\d+)")
NOTE_PROMPT = "Your answer?"
UNBLOCK_PROMPT = "What unblocks it? (a note for the researcher)"
OUTCOME_DOT = {"PURSUE": "🟢", "REFINE": "🟡", "DROP": "🔵", "BLOCKED": "🟡", "NO RESULT": "🟠"}
OUTCOME_ACTIONS = {
    "PURSUE": [("paper", "Start paper"), ("hold", "Hold")],
    "REFINE": [("rerun", "Re-run"), ("hold", "Hold")],
    "DROP": [("ack", "Ack")],
    "BLOCKED": [("unblock", "Unblock with note", UNBLOCK_PROMPT), ("drop", "Drop")],
    "NO RESULT": [("retry", "Retry")],
}
OUTCOME_SECTION = {"PURSUE": "research", "REFINE": "research", "DROP": "research", "BLOCKED": "reviews",
                   "NO RESULT": "errors"}
OUTCOME_KIND = {"PURSUE": ("result", "info"), "REFINE": ("review", "info"), "DROP": ("result", "info"),
                "BLOCKED": ("blocker", "warn"), "NO RESULT": ("failure", "error")}


def _num(x: float) -> str:
    return f"{x:g}"


def _link(label: str, url: str | None) -> list[dict]:
    return [{"label": label, "url": url}] if url else []


def _pr_link(url: str | None) -> list[dict]:
    m = PR_NUMBER.search(url or "")
    return _link(f"PR #{m.group(1)}" if m else "PR", url)


def _view(*, dot, headline, insight="", details=(), links=(), actions=(), bullet=None, section, kind, severity) -> dict:
    return {"dot": dot, "headline": headline, "insight": insight, "details": list(details)[:MAX_DETAILS],
            "links": list(links)[:MAX_LINKS], "actions": list(actions), "bullet": bullet or headline,
            "section": section, "kind": kind, "severity": severity}


def _act(spec: tuple) -> dict:
    out = {"id": spec[0], "label": spec[1]}
    if len(spec) > 2:
        out["prompt"] = spec[2]
    return out


def _backtest_line(bt: dict) -> str:
    """`<metric> <value> [vs <benchmark> [<benchmark_value>]] [<ci>]`; absent parts are left out (a difference
    metric names its benchmark and has no benchmark_value)."""
    out = f"{bt['metric']} {bt['value']}"
    if bt.get("benchmark"):
        out += f" vs {bt['benchmark']}" + (f" {bt['benchmark_value']}" if bt.get("benchmark_value") else "")
    if bt.get("ci"):
        out += f" {bt['ci']}"
    return out


def _research(f: dict) -> dict:
    outcome, bt = f["outcome"], f.get("backtest")
    lines = []
    if f.get("hypothesis"):
        lines.append(f"Hypothesis: {f['hypothesis']}")
    if bt:
        parts = [bt.get("period"), _backtest_line(bt), f"max DD {bt['max_dd']}" if bt.get("max_dd") else None]
        lines.append("Backtest: " + SEP.join(p for p in parts if p))
    if f.get("paper"):
        p = f["paper"]
        parts = [p["status"], f"{_num(p['days'])} days" if "days" in p else None,
                 f"return {p['return']} vs {p['benchmark_return']}" if p.get("return") and p.get("benchmark_return") else None]
        lines.append("Paper: " + SEP.join(x for x in parts if x))
    if f.get("source_rules"):
        lines.append(f"Source rules: {f['source_rules']}")
    if f.get("why"):
        lines.append(f"Why: {f['why']}")
    headline = f"#{int(f['issue'])} {f['name']}: {outcome}"
    ln = f.get("links", {})
    kind, severity = OUTCOME_KIND[outcome]
    return _view(dot=OUTCOME_DOT[outcome], headline=headline, insight=f.get("insight") or _backtest_line(bt), details=lines,
                 links=_link("Card", ln.get("card")) + _pr_link(ln.get("pr")) + _link("Result", ln.get("result")),
                 actions=[_act(a) for a in OUTCOME_ACTIONS[outcome]],
                 bullet=headline + (f", {f['bullet_note']}" if f.get("bullet_note") else ""),
                 section=OUTCOME_SECTION[outcome], kind=kind, severity=severity)


def _receipt(f: dict) -> dict:
    counts = SEP.join(f"{k} {_num(v)}" for k, v in f.get("counts", {}).items())
    return _view(dot="🟢", headline=f"{f['agent']}: {f['summary']}",
                 insight=SEP.join(x for x in (f.get("cost"), f.get("duration")) if x),
                 details=[f"Counts: {counts}"] if counts else [], links=_link("Notion", f.get("links", {}).get("notion")),
                 section="receipts", kind="result", severity="info")


def _failure(f: dict) -> dict:
    spent = ", ".join(x for x in (f.get("cost"), f.get("duration")) if x)
    lines = [f"Problem: {p}" for p in f.get("problems", [])] + ([f"Spent: {spent}"] if spent else [])
    lines += [f"log: {f['log_path']}"] if f.get("log_path") else []
    ln = f.get("links", {})
    return _view(dot="🟠", headline=f"{f['agent']} run failed", insight=f["reason"], details=lines,
                 links=_link("Log", ln.get("log")) + _link("Fix issue", ln.get("fix_issue")),
                 actions=[{"id": "retry", "label": "Retry"}, {"id": "ack", "label": "Ack"}],
                 bullet=f"{f['agent']} run failed: {f['reason']}", section="errors", kind="failure", severity="error")


def _fix(dot: str, headline: str, bullet: str, link_key: str, link_label: str, actions: list, section: str,
         kind: str, severity: str) -> Callable[[dict], dict]:
    def build(f: dict) -> dict:
        url = f["links"].get(link_key)
        return _view(dot=dot, headline=headline.format(**f), links=_pr_link(url) if link_key == "pr" else _link(link_label, url),
                     actions=[_act(a) for a in actions], bullet=bullet.format(**f), section=section, kind=kind,
                     severity=severity)
    return build


def _decision(f: dict) -> dict:
    options = sorted(f["options"], key=lambda o: not o.get("recommended"))      # recommended first, the rest keep order
    lines = [f"{'★ ' if o.get('recommended') else ''}{o['label']}" + (f": {o['implication']}" if o.get("implication") else "")
             for o in options]
    return _view(dot="🟡", headline=f["question"],
                 insight=f"Recommended: {options[0]['label']}" if options[0].get("recommended") else "", details=lines,
                 actions=[{"id": o["id"], "label": o["label"]} for o in options]
                 + [{"id": "note", "label": "Answer with note", "prompt": NOTE_PROMPT}],
                 bullet=f"Decision needed: {f['question']}", section="reviews", kind="blocker", severity="warn")


def _system(dot: str, headline: str, kind: str, severity: str, section: str) -> Callable[[dict], dict]:
    def build(f: dict) -> dict:
        return _view(dot=dot, headline=headline.format(**f), links=_link("Log", f.get("links", {}).get("log")),
                     actions=[{"id": "ack", "label": "Ack"}], section=section, kind=kind, severity=severity)
    return build


def _deal(f: dict) -> dict:
    headline = f"{f['product']} {f['price']}" + (f" ({f['discount']})" if f.get("discount") else "")
    return _view(dot="🟢", headline=headline, insight=f"Lowest in {_num(f['low_days'])} days" if "low_days" in f else "",
                 links=_link("Product", f["links"].get("product")),
                 actions=[{"id": "snooze7", "label": "Snooze 7 d"}, {"id": "ack", "label": "Ack"}],
                 section="receipts", kind="result", severity="info")


VIEWS: dict[str, Callable[[dict], dict]] = {
    "research.verdict": _research,
    "run.receipt": _receipt,
    "run.failure": _failure,
    "fix.merged": _fix("🟢", "Fixed: {problem} · merged", "Fixed: {problem}", "pr", "PR", [("revert", "Revert")],
                       "receipts", "result", "info"),
    "fix.proposed": _fix("🟡", "Fix ready: {problem} · needs your merge", "Fix ready: {problem}", "pr", "PR",
                         [("merge", "Merge"), ("hold", "Hold")], "reviews", "review", "warn"),
    "fix.diagnosis": _fix("🟡", "Can't auto-fix: {problem}", "Can't auto-fix: {problem}", "diagnosis", "Diagnosis",
                          [("ack", "Ack")], "errors", "review", "warn"),
    "decision.ask": _decision,
    "system.down": _system("🔴", "{what} since {since}", "failure", "critical", "errors"),
    "system.recovered": _system("🟢", "{what} back after {after}", "result", "info", "receipts"),
    "deal.alert": _deal,
}
TEMPLATES = tuple(VIEWS)


def render(template: str, facts: dict) -> dict:
    """The view for validated facts."""
    return VIEWS[template](facts)


def extra_errors(template: str, facts: dict) -> list[str]:
    """Cross-field rules the JSON Schema subset cannot express."""
    if template == "research.verdict" and not facts.get("insight") and not (facts.get("outcome") == "PURSUE" and facts.get("backtest")):
        return ["$.facts: insight is required (PURSUE may give backtest instead)"]
    return []


def callback_string(namespace: str, action_id: str, envelope_id: str) -> str:
    return CALLBACK_SEP.join((CALLBACK_PREFIX, namespace, action_id, envelope_id))


def callback_errors(env: dict) -> list[str]:
    """Buttons whose callback_data would pass Telegram's limit. Truncating would send a tap to the wrong
    action, so a template envelope that cannot fit is rejected instead."""
    out = []
    for a in actions_for(env):
        size = len(callback_string(a["namespace"], a["id"], env["id"]).encode("utf-8"))
        if size > CALLBACK_MAX:
            out.append(f"$: template {env['template']}: button {a['id']!r} callback_data would be {size} bytes "
                       f"(limit {CALLBACK_MAX}); shorten the option id or the envelope id/source")
    return out


def view_of(env: dict) -> dict | None:
    """The view of a templated envelope; None for a free-text envelope."""
    return render(env["template"], env["facts"]) if env.get("template") else None


def actions_for(env: dict) -> list[dict]:
    """The envelope's actions as inbound understands them: a free-text envelope's own, or the template's
    (id, label, namespace = the envelope's source, and the note prompt when the action asks for one)."""
    view = view_of(env)
    if view is None:
        return env.get("actions", [])
    return [{"id": a["id"], "label": a["label"], "namespace": env["source"],
             **({"input": {"prompt": a["prompt"]}} if a.get("prompt") else {})} for a in view["actions"]]
