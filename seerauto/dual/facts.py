"""Five independent facts about the night — and the refusal to merge them.

The night can look healthy in five unrelated ways, and every real incident so far
came from using one as a proxy for another:

=================  ==================================================  =================
fact               question                                            evidence source
=================  ==================================================  =================
``GOAL_ALIVE``     is the long-horizon goal still declared active?       checkpoint goal
``AGENT_DECIDING`` is the DSH agent (model layer) in a turn right now?   agent_turn.json
``SUPERVISOR_``    is ``tools/supervise.py`` running?                   ps / supervisor.json
``RUNNING``
``EXECUTOR_ALIVE`` is a local executor (the thing that actually drives   lock / heartbeat /
                   the game) alive?                                    wild_cycle.jsonl
``GAME_``          has verified progress been *proven* recently?        milestones + events
``PROGRESSING``
=================  ==================================================  =================

Concrete ways conflating them has already cost the project:

* a supervisor that is alive keeps *spawning* batches even when the executor is dead
  (2026-11-04: 26 batches, all no-ops) — supervisor alive is not executor alive;
* an executor that is alive clicks in a frozen client and records ``actions`` — alive
  is not progress (``UNVERIFIED_ACTIVITY`` exists for exactly this);
* a goal being active says nothing about whether the model layer is deciding; the
  goal survives the agent being idle, asleep, or having crashed;
* "the model is deciding" is expensive and must not be inferred from "the goal is
  alive", or every idle second would look like a paid turn.

So each fact is collected from its own source, carries its own evidence string and
age, and every fact has three states, not two:

``TRUE``    a positive artifact proves it;
``FALSE``   a positive artifact proves it *not* (e.g. no ``supervise.py`` process,
            or a heartbeat that exists and is too old);
``UNKNOWN`` there is no artifact at all — which is **not** FALSE.

``UNKNOWN`` is the important one: on a fresh night nobody has written
``agent_turn.json`` yet, so the agent-layer fact is UNKNOWN, not "the agent is idle".
Reporting UNKNOWN is what lets the operator see a missing integration instead of a
comfortable assumption.

Collection is deliberately cheap and local: file stats, one flock probe, and (for the
supervisor) a single ``ps`` scan that the watchdog throttles.  No model calls, no
network, no game input.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from .ledger import ROOT, Ledger, parse_ts
from .state import NightState, derive

__all__ = [
    "FactKind",
    "FactState",
    "FactReading",
    "FactSet",
    "collect",
    "probe_processes",
    "DEFAULT_SUPERVISOR_STATE",
    "DEFAULT_SUPERVISOR_LOG",
    "DEFAULT_WILD_CYCLE_LEDGER",
    "DEFAULT_EXECUTOR_LOCK",
    "DEFAULT_PAUSE_FILES",
    "PROGRESS_WINDOW_S",
    "HEARTBEAT_MAX_AGE_S",
    "DECIDE_MAX_AGE_S",
]

DEFAULT_SUPERVISOR_STATE = ROOT / "data" / "supervisor.json"
DEFAULT_SUPERVISOR_LOG = ROOT / "data" / "supervisor.log"
DEFAULT_WILD_CYCLE_LEDGER = ROOT / "data" / "overnight" / "wild_cycle.jsonl"
DEFAULT_EXECUTOR_LOCK = Path("/tmp/seerauto.executor.lock")
DEFAULT_PAUSE_FILES: tuple[str, ...] = ("/tmp/seerauto.pause",
                                        "/tmp/seerauto.master.pause",
                                        "/tmp/seerauto.stop")

#: How long a proven-progress record stays "recent".  Fifteen minutes is longer than
#: one supervisor batch and shorter than a human would wait before asking.
PROGRESS_WINDOW_S = 900.0

#: An executor heartbeat older than this is a dead executor, not a quiet one.
HEARTBEAT_MAX_AGE_S = 120.0

#: ``tools/supervise.py`` logs once per batch, and a batch may be 8-10 minutes, so
#: the supervisor's own log is only "dead" after a gap longer than a batch.
SUPERVISOR_MAX_AGE_S = 900.0

#: How long an "agent is deciding" marker stays believable.  A long model turn can be
#: several minutes; past that the marker is stale (the process may have died mid-turn)
#: and the honest answer is UNKNOWN, not "still deciding".
DECIDE_MAX_AGE_S = 180.0


class FactState(str, Enum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class FactKind(str, Enum):
    GOAL_ALIVE = "GOAL_ALIVE"
    AGENT_DECIDING = "AGENT_DECIDING"
    SUPERVISOR_RUNNING = "SUPERVISOR_RUNNING"
    EXECUTOR_ALIVE = "EXECUTOR_ALIVE"
    GAME_PROGRESSING = "GAME_PROGRESSING"


FACT_ORDER: tuple[FactKind, ...] = (
    FactKind.GOAL_ALIVE,
    FactKind.AGENT_DECIDING,
    FactKind.SUPERVISOR_RUNNING,
    FactKind.EXECUTOR_ALIVE,
    FactKind.GAME_PROGRESSING,
)


@dataclass(frozen=True)
class FactReading:
    """One fact's state plus the evidence that produced it (never inferred)."""

    kind: FactKind
    state: FactState
    evidence: str
    source: str
    age_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "evidence": self.evidence,
            "source": self.source,
            "age_s": round(self.age_s, 3) if self.age_s is not None else None,
        }


@dataclass(frozen=True)
class FactSet:
    """All five facts as of one collection.  No aggregate "healthy" boolean.

    There is intentionally no ``is_healthy``/``ok`` property: any caller that wants
    a summary must say which combination it means, so the five remain distinguishable
    in the audit trail.
    """

    readings: Mapping[FactKind, FactReading]
    checked_epoch: float

    def get(self, kind: FactKind) -> FactReading:
        return self.readings[kind]

    def state(self, kind: FactKind) -> FactState:
        return self.readings[kind].state

    @property
    def unknown(self) -> tuple[FactKind, ...]:
        return tuple(kind for kind in FACT_ORDER
                     if self.readings[kind].state is FactState.UNKNOWN)

    def to_dict(self) -> dict[str, Any]:
        return {kind.value: self.readings[kind].to_dict() for kind in FACT_ORDER}

    def compact(self) -> dict[str, str]:
        """One line per fact: ``{"GOAL_ALIVE": "TRUE", ...}``."""

        return {kind.value: self.readings[kind].state.value for kind in FACT_ORDER}


def _reading(kind: FactKind, state: FactState, evidence: str, source: str,
             age_s: float | None = None) -> FactReading:
    return FactReading(kind=kind, state=state, evidence=evidence, source=source,
                       age_s=age_s)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def probe_processes(pattern: str, *, timeout_s: float = 5.0,
                    exclude_pids: Iterable[int] = ()) -> list[str]:
    """Local ``ps`` scan for a command-line pattern; returns matched command lines.

    stdout is parsed, stderr ignored.  A broken/blocked ``ps`` raises — callers turn
    that into UNKNOWN rather than "not running", because a failed probe is not a
    negative observation.

    ``exclude_pids`` plus our own pid are filtered out.  This is not optional
    hygiene: the *caller's own command line* contains the pattern when an operator
    runs ``overnight_control.py status --supervisor-pattern tools/supervise.py``, so
    without the filter "the supervisor is running" would become true because the
    status command asked about it (acceptance finding A22 — "alive is not working").
    Lines that are the ``ps`` invocation itself or ``grep`` are skipped for the same
    reason; ``overnight_control.py`` lines are skipped by name because a health loop
    re-invokes the CLI.
    """

    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True,
                         text=True, timeout=timeout_s, check=False)
    skip = {int(pid) for pid in exclude_pids} | {os.getpid()}
    matches = []
    for line in (out.stdout or "").splitlines():
        text = line.strip()
        if not text or pattern not in text:
            continue
        if "grep" in text or "ps -axo" in text or "overnight_control.py" in text:
            continue
        pid_text, _, _command = text.partition(" ")
        if pid_text.isdigit() and int(pid_text) in skip:
            continue
        matches.append(text)
    return matches


def _goal_reading(checkpoint: Mapping[str, Any] | None) -> FactReading:
    kind = FactKind.GOAL_ALIVE
    if not checkpoint:
        return _reading(kind, FactState.UNKNOWN,
                        "no checkpoint.json: the control plane was never initialised",
                        "checkpoint")
    goal = checkpoint.get("goal")
    if not isinstance(goal, Mapping):
        return _reading(kind, FactState.UNKNOWN,
                        "checkpoint has no goal record (only a mode/deadline was set)",
                        "checkpoint")
    status = str(goal.get("status") or "").strip().lower()
    updated = goal.get("updated_ts") or goal.get("started_ts")
    if status in ("active", "running", "alive", "in_progress"):
        return _reading(kind, FactState.TRUE,
                        f"checkpoint goal status={status!r} "
                        f"(updated {updated or 'unknown'})", "checkpoint")
    if status in ("paused", "stopped", "closed", "done", "complete", "completed",
                  "failed", "blocked", "abandoned"):
        return _reading(kind, FactState.FALSE,
                        f"checkpoint goal status={status!r} "
                        f"(updated {updated or 'unknown'})", "checkpoint")
    return _reading(kind, FactState.UNKNOWN,
                    f"checkpoint goal status={status!r} is not a known state",
                    "checkpoint")


def _agent_reading(path: Path, now: float, *, max_age_s: float) -> FactReading:
    kind = FactKind.AGENT_DECIDING
    if not path.exists():
        return _reading(kind, FactState.UNKNOWN,
                        f"{path.name} has never been written; the DSH layer has not "
                        f"reported a turn (absence of a marker is not an idle agent)",
                        "agent_turn")
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        return _reading(kind, FactState.UNKNOWN, f"{path.name} is unreadable",
                        "agent_turn")
    state = str(payload.get("state") or "").strip().lower()
    ts = parse_ts(payload.get("ts_epoch")) or parse_ts(payload.get("ts"))
    age = (now - ts) if ts is not None else None
    if state == "deciding":
        if age is None:
            return _reading(kind, FactState.UNKNOWN,
                            f"{path.name} says deciding but carries no timestamp",
                            "agent_turn")
        if age <= max_age_s:
            return _reading(kind, FactState.TRUE,
                            f"agent turn marker says deciding, {age:.0f}s old",
                            "agent_turn", age)
        return _reading(kind, FactState.UNKNOWN,
                        f"agent turn marker says deciding but is {age:.0f}s old "
                        f"(> {max_age_s:.0f}s); the turn may have died", "agent_turn",
                        age)
    if state in ("idle", "done", "finished", "waiting"):
        return _reading(kind, FactState.FALSE,
                        f"agent turn marker says {state!r}"
                        + (f", {age:.0f}s old" if age is not None else ""),
                        "agent_turn", age)
    return _reading(kind, FactState.UNKNOWN,
                    f"agent turn marker state={state!r} is not known", "agent_turn", age)


def _supervisor_log_reading(log_file: Path, now: float, *,
                            max_age_s: float) -> FactReading | None:
    """Liveness from ``data/supervisor.log``: it is appended once per batch.

    ``tools/supervise.py`` ends every run with a ``supervisor done`` line, so the log
    is both a heartbeat and a stop record.  None means "no log to read".
    """

    kind = FactKind.SUPERVISOR_RUNNING
    try:
        stat = log_file.stat()
    except OSError:
        return None
    try:
        with log_file.open("r", encoding="utf-8", errors="replace") as fh:
            lines = [line.strip() for line in fh if line.strip()]
    except OSError:
        return None
    last = lines[-1] if lines else ""
    age = max(0.0, now - stat.st_mtime)
    if "supervisor done" in last:
        return _reading(kind, FactState.FALSE,
                        f"supervisor.log records it finished: {last[:160]}",
                        "supervisor.log", age)
    if last and age <= max_age_s:
        return _reading(kind, FactState.TRUE,
                        f"supervisor.log written {age:.0f}s ago "
                        f"(<= {max_age_s:.0f}s); last line: {last[:140]}",
                        "supervisor.log", age)
    if last:
        return _reading(kind, FactState.FALSE,
                        f"supervisor.log has not been written for {age:.0f}s "
                        f"(> {max_age_s:.0f}s); last line: {last[:140]}",
                        "supervisor.log", age)
    return None


def _supervisor_reading(*, probe: Callable[[], list[str]] | None,
                        state_file: Path, log_file: Path, now: float,
                        max_age_s: float) -> FactReading:
    """Supervisor liveness: process probe first, then its log, then its exit report.

    A probe that cannot run (this workspace sandbox blocks ``ps`` with EPERM) is not
    treated as "not running": it falls through to file evidence, and only if every
    source is empty is the answer UNKNOWN.
    """

    kind = FactKind.SUPERVISOR_RUNNING
    probe_note = ""
    if probe is not None:
        try:
            matches = probe()
        except Exception as exc:  # noqa: BLE001 - fall back, do not conclude FALSE
            probe_note = f"process probe unavailable ({type(exc).__name__}); "
        else:
            if matches:
                return _reading(kind, FactState.TRUE,
                                f"{len(matches)} matching process: "
                                f"{matches[0][:120]}", "ps")
            # A successful empty scan is a real negative observation.
            return _reading(kind, FactState.FALSE,
                            "no tools/supervise.py process is running", "ps")

    from_log = _supervisor_log_reading(log_file, now, max_age_s=max_age_s)
    if from_log is not None:
        if probe_note:
            return FactReading(kind=from_log.kind, state=from_log.state,
                               evidence=probe_note + from_log.evidence,
                               source=from_log.source, age_s=from_log.age_s)
        return from_log

    payload = _read_json(state_file)
    if not isinstance(payload, Mapping):
        return _reading(kind, FactState.UNKNOWN,
                        probe_note + f"{state_file.name} is absent or unreadable and "
                        f"no supervisor log exists", "supervisor.json")
    stopped = payload.get("stopped_at")
    if stopped:
        return _reading(kind, FactState.FALSE,
                        probe_note + f"supervisor.json records it stopped at {stopped}: "
                        f"{payload.get('stop_reason') or 'no reason recorded'}",
                        "supervisor.json")
    return _reading(kind, FactState.FALSE,
                    probe_note + "supervisor.json exists without stopped_at; the "
                    "report is only written at exit, so this is a partial write",
                    "supervisor.json")


def _lock_held(path: Path) -> bool | None:
    """Is a shared (non-destructive) lock currently held?  None when unprobeable.

    A *shared* lock request fails when the executor holds the exclusive lock, and
    succeeds (briefly) when nothing does.  It never writes to the file and never
    blocks, so a read-only status call cannot steal the executor slot.
    """

    try:
        fd = path.open("rb")
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        fd.close()


def _executor_reading(now: float, *, heartbeat_file: Path, lock_file: Path,
                      wild_cycle_ledger: Path,
                      max_age_s: float) -> FactReading:
    """Executor liveness from three independent sources, strongest first.

    1. the executor lock being held is live truth;
    2. an explicit heartbeat file (the contract for a future executor) is
       "fresh => alive, stale => dead";
    3. the executor's own progress ledger gives an age even when it holds no lock
       (``tools/wild_cycle.py`` writes ``data/overnight/wild_cycle.jsonl`` per pass
       and does not take the lock).

    An executor holding no lock and writing nothing is UNKNOWN, not FALSE: the
    integration may simply not be wired yet.
    """

    kind = FactKind.EXECUTOR_ALIVE

    held = _lock_held(lock_file)
    if held is True:
        return _reading(kind, FactState.TRUE,
                        f"executor lock is held ({lock_file})", "lock")

    heartbeat = _read_json(heartbeat_file) if heartbeat_file.exists() else None
    if isinstance(heartbeat, Mapping):
        ts = parse_ts(heartbeat.get("ts_epoch")) or parse_ts(heartbeat.get("ts"))
        if ts is None:
            return _reading(kind, FactState.UNKNOWN,
                            f"{heartbeat_file.name} carries no timestamp", "heartbeat")
        age = max(0.0, now - ts)
        if age <= max_age_s:
            return _reading(kind, FactState.TRUE,
                            f"executor heartbeat {age:.0f}s old "
                            f"(<= {max_age_s:.0f}s), pid={heartbeat.get('pid')}",
                            "heartbeat", age)
        return _reading(kind, FactState.FALSE,
                        f"executor heartbeat is {age:.0f}s old "
                        f"(> {max_age_s:.0f}s)", "heartbeat", age)

    tail = _ledger_tail(wild_cycle_ledger)
    if tail is not None:
        ts = (parse_ts(tail.get("ts_epoch")) or parse_ts(tail.get("at"))
              or parse_ts(tail.get("ts")) or parse_ts(tail.get("ended_at"))
              or parse_ts(tail.get("started_at")))
        if ts is not None:
            age = max(0.0, now - ts)
            if age <= max_age_s:
                return _reading(kind, FactState.TRUE,
                                f"wild_cycle ledger written {age:.0f}s ago",
                                "ledger", age)
            return _reading(kind, FactState.FALSE,
                            f"last wild_cycle ledger record is {age:.0f}s old "
                            f"(> {max_age_s:.0f}s: the executor stopped writing)",
                            "ledger", age)

    if held is False:
        return _reading(kind, FactState.FALSE,
                        f"executor lock exists at {lock_file} but nothing holds it",
                        "lock")
    return _reading(kind, FactState.UNKNOWN,
                    "no lock, no heartbeat file and no executor ledger: liveness was "
                    "never reported", "none")


def _ledger_tail(path: Path) -> dict[str, Any] | None:
    """Last parseable record of a foreign JSONL file, tolerating a truncated tail."""

    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = [line for line in fh if line.strip()]
    except OSError:
        return None
    for line in reversed(lines[-5:]):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            return record
    return None


def _progress_reading(night: NightState, checkpoint: Mapping[str, Any] | None,
                      now: float, *, window_s: float) -> FactReading:
    kind = FactKind.GAME_PROGRESSING
    if night.last_verified_progress_ts is not None:
        age = max(0.0, now - night.last_verified_progress_ts)
        proof = ""
        if isinstance(night.last_verified_progress, Mapping):
            proof = str(night.last_verified_progress.get("proof") or
                        night.last_verified_progress.get("node") or "")
        state = FactState.TRUE if age <= window_s else FactState.FALSE
        return _reading(kind, state,
                        f"last proven progress {age:.0f}s ago"
                        + (f" ({proof[:100]})" if proof else "")
                        + ("" if state is FactState.TRUE
                           else f"; too old (window {window_s:.0f}s)"),
                        "milestones", age)

    if night.batch_count:
        return _reading(kind, FactState.FALSE,
                        f"{night.batch_count} batch record(s) exist but none carries a "
                        f"proof of progress; the recording path is live, nothing moved",
                        "events")
    if checkpoint and checkpoint.get("session_started_ts") is not None:
        return _reading(kind, FactState.FALSE,
                        "the session is initialised but no batch or milestone has been "
                        "recorded yet", "checkpoint")
    return _reading(kind, FactState.UNKNOWN,
                    "no batch, no milestone and no session start: nothing has ever "
                    "been recorded about progress", "none")


def collect(*, ledger: Ledger | None = None, checkpoint: Mapping[str, Any] | None = None,
            now: float | None = None,
            executor_heartbeat_file: str | Path | None = None,
            agent_turn_file: str | Path | None = None,
            supervisor_state_file: str | Path | None = None,
            supervisor_log_file: str | Path | None = None,
            supervisor_max_age_s: float = SUPERVISOR_MAX_AGE_S,
            wild_cycle_ledger: str | Path | None = None,
            lock_file: str | Path | None = None,
            process_probe: Callable[[], list[str]] | None = None,
            progress_window_s: float = PROGRESS_WINDOW_S,
            heartbeat_max_age_s: float = HEARTBEAT_MAX_AGE_S,
            decide_max_age_s: float = DECIDE_MAX_AGE_S,
            night: NightState | None = None) -> FactSet:
    """Collect all five facts from their own sources.  Pure reads; no model calls.

    ``process_probe`` is the only non-file source (a local ``ps`` scan for the
    supervisor).  Callers that need this to be cheap — the watchdog ticks at 0.5 s —
    pass a throttled wrapper; tests pass a fake and never touch ``ps``.
    """

    ledger = ledger or Ledger()
    data_dir = ledger.data_dir
    now = float(now if now is not None else time.time())

    checkpoint_payload = checkpoint
    heartbeat_path = Path(executor_heartbeat_file) if executor_heartbeat_file \
        else data_dir / "executor_heartbeat.json"
    agent_path = Path(agent_turn_file) if agent_turn_file else data_dir / "agent_turn.json"
    supervisor_path = (Path(supervisor_state_file) if supervisor_state_file
                       else DEFAULT_SUPERVISOR_STATE)
    supervisor_log = (Path(supervisor_log_file) if supervisor_log_file
                      else DEFAULT_SUPERVISOR_LOG)
    wild_path = (Path(wild_cycle_ledger) if wild_cycle_ledger
                 else DEFAULT_WILD_CYCLE_LEDGER)
    lock_path = Path(lock_file) if lock_file else DEFAULT_EXECUTOR_LOCK

    if night is None:
        night = derive(ledger, checkpoint=checkpoint_payload or {}, now=now)

    readings = {
        FactKind.GOAL_ALIVE: _goal_reading(checkpoint_payload),
        FactKind.AGENT_DECIDING: _agent_reading(agent_path, now,
                                                max_age_s=decide_max_age_s),
        FactKind.SUPERVISOR_RUNNING: _supervisor_reading(
            probe=process_probe, state_file=supervisor_path,
            log_file=supervisor_log, now=now, max_age_s=supervisor_max_age_s),
        FactKind.EXECUTOR_ALIVE: _executor_reading(now, heartbeat_file=heartbeat_path,
                                                   lock_file=lock_path,
                                                   wild_cycle_ledger=wild_path,
                                                   max_age_s=heartbeat_max_age_s),
        FactKind.GAME_PROGRESSING: _progress_reading(night, checkpoint_payload, now,
                                                     window_s=progress_window_s),
    }
    return FactSet(readings=readings, checked_epoch=now)
