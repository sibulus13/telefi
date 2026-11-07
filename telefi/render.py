"""The message format standard: glance first, details collapsed, links and actions as buttons.

    🟠 Ci · Build #43 gate failed                        <- headline: status dot, source, what happened (bold)
    No API key in the fresh checkout                      <- insight: the plain context (italic)
    What to do: add the key, then tap Retry               <- next_step: the one thing to do now
    > details: sections, body, ref, log path, collapsed   <- expandable quote; tap to open
    [ Card ] [ PR #716 ]                                   <- evidence links: buttons that open the URL
    [ Ack ] [ Retry ] [ Retry with note ]                 <- actions: only while inbound is enabled

At a glance the reader gets two lines; everything else is one tap away. Status is colour, not a word:
🔴 critical · 🟠 error · 🟡 needs you (warn, review, blocker) · 🟢 done (result) · 🔵 FYI (info, digest).
Green and blue arrive silently; the rest make a sound.

A message made from a template (envelope `template` + `facts`) follows the same grammar, but its wording, dot,
links and buttons come from telefi/messages.py; docs/MESSAGE_CATALOG.md is the canonical catalog. A template's
action buttons appear only when the configured inbound sinks cover the action's namespace (the envelope's source).

A batch window with several items renders as ONE message (batch_html): a headline, one bullet per item, then
one collapsed expandable quote per section (errors, research, reviews, receipts). The hub carries that
pre-rendered text in the envelope's private `_html` key (not part of envelope.v1; it never leaves the hub).
"""
from __future__ import annotations

import html
import re
import unicodedata

from telefi import messages

CALLBACK_MAX, CALLBACK_PREFIX, CALLBACK_SEP = messages.CALLBACK_MAX, messages.CALLBACK_PREFIX, messages.CALLBACK_SEP
TELEGRAM_MAX_TEXT = 3900
HUB_SOURCE = "telefi"          # messages the hub itself makes (the digest) carry no source prefix
DISPLAY_NAME = {HUB_SOURCE: "TeleFi"}   # how a source reads in a message prefix (default: capitalised)
ELLIPSIS = "…"
NEXT_STEP_LABEL = "What to do:"
WORD_JOINER = "\u2060"
# Telegram turns web addresses, commands and @names into live links by itself, whatever the parse mode, so text from
# outside could carry one. A list of domain endings can never be complete, so an invisible character goes after each
# trigger instead: it ends a link in Telegram's own detector, and the words still read the same. A real link belongs
# in evidence, as a button.
LINK_TRIGGER = re.compile(r"\.(?=[^\W\d]|\d+\.\d)|(?<![\w/])/(?=\w)|(?<!\w)@(?=\w)|:(?=//)")
BATCH_SECTIONS = (("errors", "🔴", "Errors"), ("research", "🔬", "Research"),
                  ("reviews", "🟡", "Reviews"), ("receipts", "✅", "Receipts"))   # the fixed order
ROLLUP_SECTIONS = (("reviews", "🙋", "Needs you"), ("errors", "⛔", "Stalls"),
                   ("research", "🔬", "Research"), ("receipts", "✅", "Done"))
BULLET_MAX = 70                # characters of a bullet's title
DETAIL_LINES = 3               # per item in its section, the title line included
DETAIL_MAX = 100               # characters per detail line
INSIGHT_MAX = 300              # an item with a next step keeps its whole context, the envelope's limit
ENTITY_BUDGET = 90             # Telegram allows 100 entities (b, i, a, code, blockquote ...) in one message
_ENTITY = re.compile(r"<(?:b|i|a|code|blockquote)[ >]")
PRIVATE_HTML = "_html"
BUTTONS_PER_ROW = 3
SEVERITY_DOT = {"critical": "🔴", "error": "🟠", "warn": "🟡"}
KIND_DOT = {"review": "🟡", "blocker": "🟡", "failure": "🟠", "result": "🟢"}
INFO_DOT = "🔵"
SILENT_DOTS = {"🟢", "🔵"}
_TAG = re.compile(r"<[^>]+>")
_OPEN_OR_CLOSE = re.compile(r"<(/?)(b|i|code|blockquote)(?=[\s>])[^>]*>")
_PARTIAL_ENTITY = re.compile(r"&#?\w{0,8}$")


def stop_links(text: str) -> str:
    return LINK_TRIGGER.sub(lambda m: m.group(0) + WORD_JOINER, text)


def safe(text: str) -> str:
    """Outside text, ready for the message HTML."""
    return html.escape(stop_links(text))


def shorten(text: str, limit: int) -> str:
    """Cut at a word boundary, marking the cut with an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit(" ", 1)[0] or text[:limit - 1]
    return cut.rstrip(" ,;:-") + ELLIPSIS


def callback_data(namespace: str, action_id: str, envelope_id: str) -> str:
    return CALLBACK_SEP.join((CALLBACK_PREFIX, namespace, action_id, envelope_id))[:CALLBACK_MAX]


def parse_callback(data: str) -> tuple[str, str, str] | None:
    parts = (data or "").split(CALLBACK_SEP)
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX:
        return None
    return parts[1], parts[2], parts[3]


def status_dot(env: dict) -> str:
    """Severity decides when it is above info; otherwise the kind does (a review needs you, a result is done)."""
    view = messages.view_of(env)
    if view:
        return view["dot"]
    return SEVERITY_DOT.get(env["severity"]) or KIND_DOT.get(env["kind"], INFO_DOT)


def is_silent(env: dict) -> bool:
    """No notification sound for done/FYI messages unless the producer says otherwise."""
    return env.get("silent", status_dot(env) in SILENT_DOTS)


def _is_problem(env: dict) -> bool:
    return env["kind"] in ("failure", "blocker") or env["severity"] in SEVERITY_DOT


_BULLET = re.compile(r"^[•·▪▫◦‣∙*+-]\s+")      # a list marker only counts when followed by whitespace ("-5%" keeps its sign)
_EMOJI_JOINERS = "\ufe0f\u200d"


def _split_marker(text: str) -> tuple[str, str]:
    """(status emoji, rest): the emoji prefix after an optional bullet, and the casefolded remaining text."""
    text = _BULLET.sub("", " ".join(text.split()))
    end = 0
    while end < len(text) and (unicodedata.category(text[end]) == "So" or text[end] in _EMOJI_JOINERS):
        end += 1
    return text[:end], text[end:].strip().casefold()


def _same_line(a: tuple[str, str], b: tuple[str, str]) -> bool:
    """Same text; a status emoji on only one side is decoration, but two different emoji are different lines."""
    return a[1] == b[1] and (a[0] == b[0] or not a[0] or not b[0])


def fresh_lines(lines: list[str], seen: list[str]) -> list[str]:
    """`lines` minus any whose text equals one in `seen` (title, insight) or an earlier kept line. Blank lines pass."""
    known = [k for k in map(_split_marker, seen) if k[1]]
    kept = []
    for line in lines:
        key = _split_marker(line)
        if key[1] and any(_same_line(key, k) for k in known):
            continue
        known.append(key)
        kept.append(line)
    return kept


def _details(env: dict) -> str:
    e = html.escape
    lines = []
    known = [env["title"], env.get("insight", "")]
    for sec in env.get("sections", []):
        mine = fresh_lines(sec["lines"], known)
        known += mine
        if not mine:
            continue
        lines.append(f"<b>{safe(sec['heading'])}</b>")
        lines += [f"• {safe(line)}" for line in mine]
        lines.append("")
    body = fresh_lines(env.get("body", "").splitlines(), known)
    if body:
        lines.append(safe("\n".join(body)))
    ev = env.get("evidence") or {}
    if ev.get("ref") and _is_problem(env):            # an id helps debug a failure; elsewhere it is noise
        lines.append(f"ref: <code>{e(ev['ref'])}</code>")
    if ev.get("log_path"):
        lines.append(f"log: <code>{e(ev['log_path'])}</code>")
    return "\n".join(lines).strip()


def source_label(source: str) -> str:
    return DISPLAY_NAME.get(source) or source.capitalize()


def buttons_on(buttons: "bool | frozenset[str] | None") -> bool:
    """`buttons` is False/None (inbound off), True (every namespace), or the set of sink namespaces ('*' = all)."""
    return isinstance(buttons, frozenset) or bool(buttons)


def namespace_allowed(buttons: "bool | frozenset[str] | None", namespace: str) -> bool:
    if isinstance(buttons, frozenset):
        return "*" in buttons or namespace in buttons
    return bool(buttons)


def _rows(buttons: list[dict]) -> list[list[dict]]:
    return [buttons[i:i + BUTTONS_PER_ROW] for i in range(0, len(buttons), BUTTONS_PER_ROW)]


def _closers(html_text: str) -> str:
    """Closing tags for whatever is still open at the end of `html_text`."""
    open_tags: list[str] = []
    for m in _OPEN_OR_CLOSE.finditer(html_text):
        if not m.group(1):
            open_tags.append(m.group(2))
        elif m.group(2) in open_tags:
            open_tags.remove(m.group(2))
    return "".join(f"</{t}>" for t in reversed(open_tags))


def truncate_html(text: str, limit: int) -> str:
    """Cut to `limit` characters without splitting a tag or an entity, then close what is open."""
    if len(text) <= limit:
        return text
    cut_at = limit - len(ELLIPSIS)
    for _ in range(2):                                  # the closers need room; pass two uses their real size
        head = text[:cut_at]
        if head.rfind("<") > head.rfind(">"):
            head = head[:head.rfind("<")]
        head = _PARTIAL_ENTITY.sub("", head)
        cut_at = limit - len(ELLIPSIS) - len(_closers(head))
    return head + ELLIPSIS + _closers(head)


def utf16_len(text: str) -> int:
    """Telegram counts message length in UTF-16 code units (an emoji is 2)."""
    return len(text.encode("utf-16-le")) // 2


def batch_item(env: dict, count: int, section: str) -> dict:
    """One envelope as a batch entry: its bullet, and its detail block (at most DETAIL_LINES lines + a link)."""
    e = html.escape
    suffix = f" ×{count}" if count > 1 else ""
    emoji = next(em for key, em, _ in BATCH_SECTIONS if key == section)
    view = messages.view_of(env)
    if view:                                                                  # a template: its own bullet and details
        title, bullet, insight, step = view["headline"], view["bullet"], view["insight"], view["next_step"]
        extra = [insight] + view["details"]
        ev = {"links": view["links"]}
    else:
        title = bullet = env["title"]
        insight, step = env.get("insight", ""), env.get("next_step", "")
        extra = [insight] + env.get("body", "").splitlines()
        extra += [line for sec in env.get("sections", []) for line in sec["lines"]]
        ev = env.get("evidence") or {}
    step_line = f"{NEXT_STEP_LABEL} {shorten(step, INSIGHT_MAX)}" if step else ""
    lines = [shorten(title, DETAIL_MAX - len(suffix)) + suffix]
    if step:                                                                  # it asks for something: say all of it
        lines += [shorten(insight, INSIGHT_MAX), step_line]
    else:
        lines += [shorten(t, DETAIL_MAX) for t in fresh_lines(extra, [title]) if t.strip()][:DETAIL_LINES - 1]
    link = (ev.get("links") or [None])[0]
    if link:
        tail = f'<a href="{e(link["url"], quote=True)}">{e(link["label"])}</a>'
    else:
        tail = f"log: <code>{e(ev['log_path'])}</code>" if ev.get("log_path") else ""
    return {"section": section, "count": count, "step": safe(step_line),
            "bullet": f"• {emoji} {safe(shorten(bullet, BULLET_MAX - len(suffix)) + suffix)}",
            "block": "\n".join([safe(t) for t in lines] + ([tail] if tail else []))}


def batch_html(items: list[dict], label: str, sections: tuple = BATCH_SECTIONS, counts: list[str] | None = None) -> str:
    """The batched message for a window. Over budget, section details are dropped first (last section first),
    then bullets from the end; the message then ends with "+N more in bell.jsonl". `counts` are lines under the head."""
    ordered = [i for key, _, _ in sections for i in items if i["section"] == key]
    head = f"{INFO_DOT} <b>{sum(i['count'] for i in ordered)} updates · {html.escape(label)}</b>"
    if counts:
        head += "\n" + "\n".join(html.escape(line) for line in counts)
    groups = []
    for key, emoji, name in sections:
        mine = [i for i in ordered if i["section"] == key]
        if mine:
            quote = f"<b>▸ {emoji} {name} ({sum(i['count'] for i in mine)})</b>\n" + "\n\n".join(i["block"] for i in mine)
            groups.append((key, len(mine), f"<blockquote expandable>{quote}</blockquote>"))

    def compose(n_bullets: int, n_groups: int) -> str:
        unseen = len(ordered) - n_bullets                                   # no bullet at all
        stripped = n_bullets - sum(n for _, n, _ in groups[:n_groups])       # a bullet, but its details are dropped
        kept = {key for key, _, _ in groups[:n_groups]}
        note = (f"+{unseen} more, details for {stripped} in bell.jsonl" if unseen
                else f"+{stripped} more in bell.jsonl" if stripped else "")
        bullets = [i["bullet"] + (f"\n{i['step']}" if i.get("step") and i["section"] not in kept else "") for i in ordered[:n_bullets]]
        parts = [head] + bullets + [q for _, _, q in groups[:n_groups]]
        return "\n".join(parts + ([note] if note else []))

    attempts = [(len(ordered), k) for k in range(len(groups), -1, -1)] + [(n, 0) for n in range(len(ordered) - 1, 0, -1)]
    for n_bullets, n_groups in attempts:
        text = compose(n_bullets, n_groups)
        if utf16_len(text) <= TELEGRAM_MAX_TEXT and len(_ENTITY.findall(text)) <= ENTITY_BUDGET:
            return text
    return truncate_html(text, TELEGRAM_MAX_TEXT)


def telegram_text(env: dict) -> str:
    """The message HTML before Telegram's size limit is applied."""
    if PRIVATE_HTML in env:                                                   # a hub-made batch, already rendered
        return env[PRIVATE_HTML]
    e = html.escape
    hub_digest = env["kind"] == "digest" and env["source"] == HUB_SOURCE      # the hub's own digest needs no prefix
    prefix = "" if hub_digest else f"{e(source_label(env['source']))} · "
    view = messages.view_of(env)
    if view:
        parts = [f"{view['dot']} {prefix}<b>{safe(view['headline'])}</b>"]
        if view["insight"]:
            parts.append(f"<i>{safe(view['insight'])}</i>")
        if view["next_step"]:
            parts.append(f"<b>{NEXT_STEP_LABEL}</b> {safe(view['next_step'])}")
        details = fresh_lines(view["details"], [view["headline"], view["insight"]])
        if details:
            parts.append("<blockquote expandable>" + "\n".join(safe(line) for line in details) + "</blockquote>")
        return "\n".join(parts)
    parts = [f"{status_dot(env)} {prefix}<b>{safe(env['title'])}</b>"]
    if env.get("insight"):
        parts.append(f"<i>{safe(env['insight'])}</i>")
    if env.get("next_step"):
        parts.append(f"<b>{NEXT_STEP_LABEL}</b> {safe(env['next_step'])}")
    details = _details(env)
    if details:
        parts.append(f"<blockquote expandable>{details}</blockquote>")
    return "\n".join(parts)


def render_telegram(env: dict, *, buttons: "bool | frozenset[str] | None") -> dict:
    """The exact text + keyboard the owner sees (HTML parse mode, every field escaped)."""
    text = truncate_html(telegram_text(env), TELEGRAM_MAX_TEXT)
    view = messages.view_of(env)
    links = view["links"] if view else (env.get("evidence") or {}).get("links", [])
    rows = _rows([{"text": link["label"], "url": link["url"]} for link in links])
    if view:
        actions = [a for a in messages.actions_for(env) if namespace_allowed(buttons, a["namespace"])]
    else:
        actions = env.get("actions", []) if buttons_on(buttons) else []
    if actions:
        rows += _rows([{"text": a["label"], "callback_data": callback_data(a["namespace"], a["id"], env["id"])}
                       for a in actions])
    return {"text": text, "reply_markup": {"inline_keyboard": rows} if rows else None}


def render_plain(env: dict, *, buttons: "bool | frozenset[str] | None") -> str:
    """Terminal preview: what the owner sees collapsed, then what expanding reveals."""
    r = render_telegram(env, buttons=buttons)
    quotes = list(re.finditer(r"<blockquote expandable>(.*?)</blockquote>", r["text"], re.S))
    glance = r["text"][:quotes[0].start()] if quotes else r["text"]
    out = [html.unescape(_TAG.sub("", glance)).rstrip()]
    for quote in quotes:
        body = html.unescape(_TAG.sub("", quote.group(1)))
        out.append("  ▸ details (tap to expand):\n" + "\n".join(f"    | {l}" for l in body.splitlines()))
    if r["reply_markup"]:
        for row in r["reply_markup"]["inline_keyboard"]:
            out.append("  " + "  ".join(f"[ {b['text']}{' ↗' if 'url' in b else ''} ]" for b in row))
    return "\n".join(out)
