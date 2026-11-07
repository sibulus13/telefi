"""Simulated time: run the real hub round (drain, self-watch, batch windows) against a fake clock and a fake
Telegram that records every send, stepping through a day in seconds.

    python -m telefi simulate scenario.json     print the timeline: what arrived, what was sent, and when

SimHub is also the harness of the integration tests (tests/sim/). Nothing is mocked inside the hub: the clock
(`core.clock`) is the only thing replaced, plus the channel adapters, which are passed in as the hub already allows.

Scenario file (all times local to `timezone`; "HH:MM" is on the start date, "+1 HH:MM" the next day, or a full
"YYYY-MM-DDTHH:MM"):
    {"timezone": "America/Los_Angeles", "start": "2026-10-28T06:00", "end": "+1 09:00", "step_min": 1,
     "arrivals": [{"at": "07:10", "source": "research", "kind": "result", "severity": "info", "title": "...",
                   "dedup_key": "...", "template": "...", "facts": {...}}],
     "telegram_outages": [{"from": "10:00", "to": "14:30"}], "hub_down": [{"from": "19:00", "to": "+1 07:30"}],
     "digest": {"windows": [...], "quiet": [...]}}
"""
from __future__ import annotations

import html
import json
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from telefi import core, digest, hub, schema, watch
from telefi.render import is_silent, render_telegram

DEFAULT_STEP = timedelta(minutes=1)
DEAD_PID = 99_999_999                       # no process has this id: a lock it holds is stale
CRASH_LOG_EVENTS = ("round failed", "digest round failed", "self-watch failed")
BATCH_ROUTES = [
    {"match": {"severity": ["critical"]}, "channels": ["telegram", "file"]},
    {"match": {"kind": ["blocker"]}, "channels": ["telegram", "file"]},
    {"match": {"severity": ["error", "warn"]}, "channels": ["telegram", "file"], "delivery": "batch"},
    {"match": {"kind": ["review", "result", "digest"]}, "channels": ["telegram", "file"], "delivery": "batch"},
    {"match": {}, "channels": ["file"]},
]
_TAG = re.compile(r"<[^>]+>")
_WHEN = re.compile(r"^(?:\+(\d+) )?(\d\d):(\d\d)$")


class SimKilled(BaseException):
    """The simulated process died here: nothing after this point ran (BaseException so no handler swallows it)."""


@dataclass
class Send:
    at: datetime            # simulated time, in the hub's zone
    text: str
    silent: bool
    envelope_id: str


class SimTelegram:
    """A fake Telegram channel: records (time, text, silent) per accepted send; refuses during outages."""

    def __init__(self, clock):
        self.clock = clock
        self.sends: list[Send] = []
        self.failures: list[datetime] = []
        self.outages: list[tuple[datetime, datetime]] = []      # [start, end) in UTC
        self.during_send = None                                  # called mid-send: lets a test run a second hub

    def is_down(self) -> bool:
        now = self.clock().astimezone(timezone.utc)
        return any(a <= now < b for a, b in self.outages)

    def send(self, env: dict, *, buttons) -> dict:
        now = self.clock()
        if self.is_down():
            self.failures.append(now)
            return {"ok": False, "error": "simulated Telegram outage"}
        if self.during_send:
            self.during_send()
        self.sends.append(Send(now, render_telegram(env, buttons=buttons)["text"], is_silent(env), env["id"]))
        return {"ok": True, "error": None, "message_id": len(self.sends)}


class SimFile:
    """A fake file channel (bell.jsonl): records which envelope reached it and when."""

    def __init__(self, clock):
        self.clock = clock
        self.writes: list[tuple[datetime, str]] = []

    def send(self, env: dict, *, buttons) -> dict:
        self.writes.append((self.clock(), env["id"]))
        return {"ok": True, "error": None}


def default_config(tz: str, extra_digest: dict | None = None) -> dict:
    return {**core.DEFAULT_CONFIG, "routes": BATCH_ROUTES, "digest": {"timezone": tz, **(extra_digest or {})}}


def plain(text: str) -> str:
    return html.unescape(_TAG.sub("", text))


class SimHub:
    """One hub over a spool in `home`, its clock, and its fake channels. `now` is UTC; `clock()` is local."""

    def __init__(self, home: Path, *, start: datetime, tz: str = "UTC", cfg: dict | None = None,
                 step: timedelta = DEFAULT_STEP):
        self.zone = ZoneInfo(tz)
        self.now = start.astimezone(timezone.utc)
        self.step = step
        self.cfg = cfg or default_config(tz)
        assert schema.validate(self.cfg, "hub_config.v1") == []
        home.mkdir(parents=True, exist_ok=True)
        self.root = home / "spool"
        self.heartbeat_path = home / watch.HEARTBEAT_FILE
        self.telegram = SimTelegram(self.clock)
        self.file = SimFile(self.clock)
        self.adapters = {"telegram": self.telegram, "file": self.file}
        self.down = False
        self.arrivals: list[tuple[datetime, dict]] = []          # for the timeline
        self.beats: list[tuple[datetime, dict]] = []             # heartbeat after each round
        self._counter = 0
        self._kill_next_send = False
        self.crashed = False

    def clock(self) -> datetime:
        return self.now.astimezone(self.zone)

    @contextmanager
    def installed(self):
        """Replace the process clock with this hub's for the duration."""
        with mock.patch.object(core, "clock", self.clock):
            yield self

    def local(self, hhmm: str, day: int = 0) -> datetime:
        """A wall-clock time on the start date (+ `day` days) in this hub's zone, as UTC."""
        base = self.clock().date() + timedelta(days=day)
        h, m = map(int, hhmm.split(":"))
        return datetime(base.year, base.month, base.day, h, m, tzinfo=self.zone).astimezone(timezone.utc)

    # ------------------------------------------------------------ producing

    def publish(self, env: dict | None = None, **fields) -> dict:
        """An envelope arrives in the spool now. Ids are sequential so same-second arrivals keep their order."""
        if env is None:
            env = (core.templated(fields.pop("template"), fields.pop("facts"), **fields) if "template" in fields
                   else core.envelope(**fields))
        self._counter += 1
        env = {**env, "id": f"{env['source']}-{self._counter:05d}"}
        core.publish(env, spool=self.root)
        self.arrivals.append((self.now, env))
        return env

    # ------------------------------------------------------------ running

    def round(self) -> None:
        """One serve round at the current simulated time (a dead hub runs nothing)."""
        if self.down:
            return
        real_release = digest._release

        def release(root, items, day, to):
            if self._kill_next_send and to.parent.name == digest.SENT_DIR:      # Telegram said ok; the process dies here
                raise SimKilled
            real_release(root, items, day, to)

        try:
            with mock.patch.object(digest, "_release", release):
                hub.serve_round(self.cfg, root=self.root, now=self.clock(), adapters=self.adapters,
                                heartbeat=self.heartbeat_path, log=self.root.parent / "hub.log")
        except SimKilled:
            self._die()
            return
        self.beats.append((self.now, json.loads(self.heartbeat_path.read_text(encoding="utf-8"))))

    def kill_after_next_batch_send(self) -> None:
        """The hub process dies right after Telegram accepts the next batch message, before it records it."""
        self._kill_next_send = True

    def _die(self) -> None:
        self._kill_next_send = False
        self.down = self.crashed = True
        lock = self.root / "state" / digest.LOCK_FILE            # a dead sender leaves its lock behind
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"pid": DEAD_PID, "at": core.now_iso()}), encoding="utf-8")

    def restart(self) -> int:
        """The hub starts again: recover what a dead sender left claimed (as `serve` does at start)."""
        self.down = self.crashed = False
        with self.installed():
            return digest.recover(self.root)

    def advance_to(self, end: datetime, arrivals: list[tuple[datetime, dict]] | None = None,
                   down: list[tuple[datetime, datetime]] | None = None) -> None:
        """Step to `end` (UTC, inclusive), running a round at each step. `arrivals` are (when, envelope kwargs);
        `down` are periods with no hub (it restarts when one ends)."""
        pending = sorted(arrivals or [], key=lambda a: a[0])
        down = down or []
        with self.installed():
            while self.now <= end:
                stepped = self.now
                while pending and pending[0][0] <= stepped:
                    self.now = pending[0][0]
                    self.publish(**pending.pop(0)[1])
                self.now = stepped
                if not self.crashed:                                  # a crashed hub stays down until restart()
                    is_down = any(a <= stepped < b for a, b in down)
                    if self.down and not is_down:
                        self.restart()
                    self.down = is_down
                self.round()
                self.now += self.step

    # ------------------------------------------------------------ observing

    def heartbeat(self) -> dict:
        return json.loads(self.heartbeat_path.read_text(encoding="utf-8"))

    def log_errors(self) -> list[dict]:
        log = self.root.parent / "hub.log"
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        return [r for r in rows if r.get("event") in CRASH_LOG_EVENTS]

    def timeline(self) -> list[str]:
        """Every arrival, failed attempt and send in time order, as printable lines."""
        events: list[tuple[datetime, int, str]] = []
        for at, env in self.arrivals:
            events.append((at, 0, f"arrive  {env['kind']}/{env['severity']}  {env['source']}: {env['title']}"))
        for at in self.telegram.failures:
            events.append((at.astimezone(timezone.utc), 1, "FAILED  Telegram unreachable, will retry"))
        for s in self.telegram.sends:
            body = plain(s.text).splitlines()
            events.append((s.at.astimezone(timezone.utc), 2, f"SEND    {'silent' if s.silent else 'sound ':6}  {body[0]}"
                           + "".join(f"\n{' ' * 29}{line}".rstrip() for line in body[1:])))
        return [f"{at.astimezone(self.zone):%Y-%m-%d %H:%M}  {text}" for at, _, text in sorted(events, key=lambda e: e[:2])]


def parse_when(text: str, start: datetime, zone: ZoneInfo) -> datetime:
    """"HH:MM", "+N HH:MM" (N days after the start date) or a full local ISO time -> UTC."""
    m = _WHEN.match(text)
    if not m:
        return datetime.fromisoformat(text).replace(tzinfo=zone).astimezone(timezone.utc)
    day = start.astimezone(zone).date() + timedelta(days=int(m.group(1) or 0))
    return datetime(day.year, day.month, day.day, int(m.group(2)), int(m.group(3)), tzinfo=zone).astimezone(timezone.utc)


def run_scenario(spec: dict, home: Path) -> SimHub:
    """Run a scenario file's dict to its end and return the hub (timeline, sends, heartbeats)."""
    tz = spec.get("timezone", "UTC")
    zone = ZoneInfo(tz)
    start = datetime.fromisoformat(spec["start"]).replace(tzinfo=zone).astimezone(timezone.utc)
    at = lambda t: parse_when(t, start, zone)
    hub = SimHub(home, start=start, tz=tz, cfg=default_config(tz, spec.get("digest")),
                 step=timedelta(minutes=spec.get("step_min", 1)))
    hub.telegram.outages = [(at(o["from"]), at(o["to"])) for o in spec.get("telegram_outages", [])]
    arrivals = [(at(a["at"]), {k: v for k, v in a.items() if k != "at"}) for a in spec.get("arrivals", [])]
    down = [(at(d["from"]), at(d["to"])) for d in spec.get("hub_down", [])]
    hub.advance_to(at(spec["end"]), arrivals, down)
    return hub


def simulate(scenario_file: str) -> int:
    spec = json.loads(Path(scenario_file).read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        hub = run_scenario(spec, Path(tmp))
        print("\n".join(hub.timeline()))
        print(f"\n{len(hub.telegram.sends)} Telegram sends, {len(hub.telegram.failures)} failed attempts, "
              f"{len(hub.arrivals)} arrivals")
        return 1 if hub.log_errors() else 0
