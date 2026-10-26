# TeleFi

> **Developed and verified on Windows 11** (build 26200, Python 3.12.4). Pure standard-library
> Python, so other platforms should work, but only Windows 11 is tested today.

**Telegram as a two-way control surface for your scripts, CI and autonomous agents.** Programs
drop a small JSON file in a folder; TeleFi sends it to your Telegram in a readable format and
records the delivery. You answer from your phone (a button, a typed note, a `/command`) and TeleFi
hands your answer back to the program as a validated JSON event. Pure Python standard library:
no runtime dependencies.

![TeleFi: programs drop envelope JSON into the spool; the hub sends it to Telegram, records it, and routes taps, replies and /commands back to the program](https://raw.githubusercontent.com/sibulus13/telefi/main/docs/images/architecture.svg)

## Why TeleFi

- **Steer autonomous code from your phone.** An agent or CI job hits a failure; you tap *Retry with
  note*, type "use the 2016-2025 window", and the job receives that note as an event. No laptop, no
  remote session.
- **One way to notify, from any language.** Producers write a JSON file: no SDK, no network code, no
  bot token in every program. A producer works even while the hub is down (the file waits).
- **Readable at a glance.** A one-line headline, a one-line "so what", details folded away, links
  as buttons. Every program's messages look the same.
- **Everything is on record.** Every message, every delivery result, every reply and the whole chat
  transcript, as JSON lines on your disk, rotated before they fill up.
- **Contracts, not conventions.** Messages, replies, config and command lists are JSON Schemas; a
  new program adopts TeleFi by reading `telefi schema envelope.v1`.

## Features

| feature | how |
|---|---|
| Routing | config rules by source, kind and severity -> Telegram, a local file, or a mock |
| Dedup, holds, ack | one delivery per `dedup_key` per window; `not_before` holds a message (a grace period); Ack silences re-alerts for N days |
| Glance-first format | headline + insight, collapsed details, URL buttons, action buttons |
| Two-way | buttons, ForceReply prompts for notes, replies to any message, `/commands` |
| Command registry | each program ships a manifest; `/help`, `/list` and Telegram's `/` menu are built from them |
| Sinks | `queue` (a folder your program drains) or `command` (a program fed the event on stdin; its last output line is the reply) |
| Log rotation | each log rotates at `rotate_at` x `max_bytes` (the safety margin), keeping `keep` old files |
| Safe tests | a mock adapter by default; live sends only with `TELEFI_LIVE=1` |

## Requirements

| | |
|---|---|
| OS | Windows 11 (verified). Linux/macOS untested. |
| Python | 3.12 (verified with 3.12.4) |
| Runtime dependencies | none ([requirements.txt](requirements.txt) says so explicitly) |
| Test dependencies | pinned in [requirements-dev.txt](requirements-dev.txt) (pytest 8.4.2 and its deps) |
| Telegram | a bot token from @BotFather and your chat id |

## Try it locally, step by step

Each step is self-contained and was run end to end on 2026-10-25. Steps 1-5 need no Telegram.

**1. Install and run the tests** (expect all green, one live test skipped)

```bash
git clone <this repo> telefi && cd telefi
pip install -e . -r requirements-dev.txt
python -m pytest
```

**2. Use a throwaway home** so nothing touches a real install

```bash
export TELEFI_HOME="$PWD/.try"          # PowerShell: $env:TELEFI_HOME = "$PWD\.try"
```

**3. See every message type exactly as Telegram shows it** (sends nothing)

```bash
python -m telefi preview
```

**4. Route the samples through the full pipeline with the mock channel**

```bash
python -m telefi samples
# ci.failure [error] -> telegram=ok, file=ok   ... mode: mock (nothing sent)
```

**5. Publish your own message, deliver it, and read the record**

```bash
python -c "from telefi import envelope, publish; publish(envelope(source='me', kind='info', severity='info', title='hello from TeleFi'))"
python -m telefi drain                   # info routes to the local file only
tail -1 .try/outbox.jsonl                # the envelope and each channel's result
```

**6. Connect Telegram.** Copy `examples/.env.example` to `.env` (git-ignored) and fill it in. Create a bot with [@BotFather](https://t.me/BotFather), send it any message,
and read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.

| variable | required | meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | the bot token from BotFather |
| `TELEGRAM_CHAT_ID` | yes | the only chat TeleFi talks to; every other chat is ignored |
| `TELEFI_HOME` | no | config and records (default `~/.telefi`) |
| `TELEFI_SPOOL` | no | the drop folder (default `$TELEFI_HOME/spool`) |

```bash
python -m telefi samples --send          # one real message of each type
```

**7. Turn on replies.** Copy the full example config (every option, documented in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md)); it enables replies, routes `/ci` commands to
`examples/echo_sink.py` and lists `examples/commands.example.json`:

```bash
cp examples/hub.example.json "$TELEFI_HOME/hub.json"
python -m telefi serve                   # leave running; one hub per bot
```

In Telegram: send `/help`, then `/list`, then `/ci retry 7 try again`; the echo sink answers
`retry received`. Tap *Retry with note* on a sample from step 6, type a note, and see it echoed.

**8. Check the records**

```bash
tail -3 "$TELEFI_HOME/conversation.jsonl"   # both directions, in order
tail -1 "$TELEFI_HOME/inbox.jsonl"          # your reply and what the sink returned
```

## Run it in the background (optional)

The hub is one long-running command, `python -m telefi serve`: run it under whatever you already
use to keep processes alive. Two ready-made examples:

- Windows 11 (verified): `powershell -NoProfile -File examples/windows-service.ps1` registers a
  scheduled task running `pythonw.exe -m telefi serve` (no console window), restarted every 5
  minutes if it stops.
- Linux (untested): `examples/telefi.service` (systemd user unit).

Command sinks run in the hub's working directory, so give them absolute paths (or `cwd`).

## Send from your program

```python
from telefi import envelope, publish

publish(envelope(source="ci", kind="failure", severity="error",
                 title="Build #43 failed", insight="Tests passed; the deploy step timed out.",
                 body="step deploy: timeout after 600s", dedup_key="ci:build:43",
                 actions=[{"id": "retry_note", "label": "Retry with note", "namespace": "ci",
                           "input": {"prompt": "What should change?"}}]))
```

From any other language, write the same JSON to `$TELEFI_HOME/spool/out/<id>.json` atomically
(write a temp file, then rename): see `examples/publish.mjs` for Node. Replies reach your program
through a command sink (`examples/echo_sink.py`) or a queue folder (`examples/queue_drain.py`).

## Examples (one per configurable part, all validated by the tests)

| file | what it configures |
|---|---|
| `examples/.env.example` | secrets and paths |
| `examples/hub.minimal.json` | send-only hub |
| `examples/hub.example.json` | every hub option: routes, channels, dedup, logs, replies, sinks |
| `examples/commands.example.json` | an app's `/commands` |
| `examples/envelope.example.json` | every message field |
| `examples/inbound.example.json` | what your program receives |
| `examples/echo_sink.py`, `examples/queue_drain.py` | the two kinds of reply handler |
| `examples/publish.mjs` | publishing from Node |
| `examples/windows-service.ps1`, `examples/telefi.service` | running in the background (optional) |

## Reference

- Contracts: `python -m telefi schema envelope.v1 | inbound.v1 | hub_config.v1 | commands.v1`
- [Configuration reference](docs/CONFIGURATION.md) · [Design](docs/DESIGN.md) · [Similar projects](docs/LANDSCAPE.md)

## License

MIT, see [LICENSE](LICENSE).
