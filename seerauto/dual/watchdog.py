"""Local health watchdog: a plain Python loop, no model calls, no flapping.

Why this file exists at all: on 2026-11-04 the night produced 26 batches that all
"exited 0" while doing nothing, and nobody noticed until the deadline.  The lesson
was not "add more logging" — the logs existed.  It was that **nobody was watching the
clock, the heartbeats and the proof trail at the same time**, and a model turn was
far too expensive to be the thing that looks at them.

So this is a local loop with a sub-second cadence that:

* reads the five independent facts each tick (:mod:`seerauto.dual.facts`);
* derives progress/streak/blocked state from the append-only ledgers
  (:mod:`seerauto.dual.state`);
* fires a structured **wake** when a human-or-agent decision is actually needed:
  ``STALL``, ``NO_WORK_STREAK``, ``BLOCKED``, ``UNVERIFIED_ACTIVITY``, ``MILESTONE``,
  ``DEADLINE``, ``PAUSE``, ``EXECUTOR_DEAD``;
* writes every evaluation to ``runtime_health.jsonl`` and every applied wake to
  ``events.jsonl`` — so "the watchdog was running and saw X" is evidence, not a claim.

**It cannot flap.**  Each wake kind has its own cooldown (default 300 s; the deadline
kind 600 s).  A criterion that is still true inside the cooldown is *suppressed*, and
the suppression is counted (`suppressed_total`, per-kind count/first/last) and
persisted — the point is not to hide repeats, it is to stop a once-per-cadence event
flood from burying the first one.  Suppressions are aggregated between health records
instead of one line per tick: at 0.5 s a persistently-stalled night would otherwise
write ~86 000 fsynced lines.  Every suppression is counted, none is dropped, and the
aggregate is flushed at the latest on the next ``persist_every_s`` boundary
(default 30 s), so a crash loses at most one flush window of counters.

**Persistence is throttled, detection is not.**  The loop evaluates every cadence;
it writes ``runtime_health.jsonl`` when something changed (a wake applied, a fact or
criterion flipped) or when ``persist_every_s`` elapsed.  That keeps a nine-hour night
at a few hundred health records instead of a hundred thousand, while an applied wake
is always written immediately.

Thresholds are constructor arguments (not globals) so a test can pin a rule without
sleeping and an operator can tune one without editing source.  Nothing here imports
an LLM SDK or touches the game; the only non-file probe is a local ``ps`` scan,
throttled to once per ``probe_every_s``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping

from .facts import (DECIDE_MAX_AGE_S, HEARTBEAT_MAX_AGE_S, PROGRESS_WINDOW_S,
                    SUPERVISOR_MAX_AGE_S, DEFAULT_PAUSE_FILES, FactKind, FactSet,
                    FactState, collect)
from .ledger import Ledger, LedgerError, parse_ts
from .state import DerivedCache, NightState

__all__ = [
    "WakeKind",
    "WatchdogConfig",
    "Criterion",
    "Watchdog",
    "evaluate_criteria",
    "parse_deadline",
    "remaining_seconds",
]

#: Shipped as the default foreground stop file; the watchdog exits instead of
#: fighting a human who asked everything to stop (AGENTS.md §5.6).
DEFAULT_STOP_FILES: tuple[str, ...] = ("/tmp/seerauto.stop",)


class WakeKind(str, Enum):
    """Why the watchdog wants attention.  One per separately-actionable condition."""

    STALL = "STALL"
    NO_WORK_STREAK = "NO_WORK_STREAK"
    BLOCKED = "BLOCKED"
    UNVERIFIED_ACTIVITY = "UNVERIFIED_ACTIVITY"
    MILESTONE = "MILESTONE"
    DEADLINE = "DEADLINE"
    PAUSE = "PAUSE"
    EXECUTOR_DEAD = "EXECUTOR_DEAD"


@dataclass(frozen=True)
class WatchdogConfig:
    """Thresholds and paths for one watchdog run.  All times are seconds."""

    deadline: str | None = None
    stall_s: float = 900.0
    heartbeat_max_age_s: float = HEARTBEAT_MAX_AGE_S
    no_work_streak: int = 2
    unverified_streak: int = 3
    deadline_warn_s: float = 1800.0
    cadence_s: float = 0.5
    persist_every_s: float = 30.0
    cooldown_s: float = 300.0
    #: Per-kind overrides keyed by ``WakeKind.value``; JSON/checkpoint friendly.
    #: The deadline repeats only every ten minutes: a warning that fires every five
    #: minutes stops being a warning.
    cooldowns: Mapping[str, float] = field(
        default_factory=lambda: {WakeKind.DEADLINE.value: 600.0})
    pause_files: tuple[str, ...] = DEFAULT_PAUSE_FILES
    stop_files: tuple[str, ...] = DEFAULT_STOP_FILES
    progress_window_s: float = PROGRESS_WINDOW_S
    decide_max_age_s: float = DECIDE_MAX_AGE_S
    probe_every_s: float = 10.0
    supervisor_pattern: str = "tools/supervise.py"
    #: A supervisor logs once per batch, so a gap shorter than a batch proves nothing.
    supervisor_max_age_s: float = SUPERVISOR_MAX_AGE_S
    supervisor_log_file: str | None = None
    #: Fact sources, overridable so a test (or a second experiment) can point the
    #: plane at its own evidence instead of the repo-wide runtime files.
    agent_turn_file: str | None = None
    executor_heartbeat_file: str | None = None
    executor_lock_file: str | None = None
    wild_cycle_ledger: str | None = None

    def cooldown_for(self, kind: WakeKind) -> float:
        return float(self.cooldowns.get(kind.value, self.cooldown_s))

    def to_dict(self) -> dict[str, Any]:
        return {
            "deadline": self.deadline,
            "stall_s": self.stall_s,
            "heartbeat_max_age_s": self.heartbeat_max_age_s,
            "no_work_streak": self.no_work_streak,
            "unverified_streak": self.unverified_streak,
            "deadline_warn_s": self.deadline_warn_s,
            "cadence_s": self.cadence_s,
            "persist_every_s": self.persist_every_s,
            "cooldown_s": self.cooldown_s,
            "cooldowns": dict(self.cooldowns),
            "pause_files": list(self.pause_files),
            "stop_files": list(self.stop_files),
            "progress_window_s": self.progress_window_s,
            "decide_max_age_s": self.decide_max_age_s,
            "probe_every_s": self.probe_every_s,
            "supervisor_pattern": self.supervisor_pattern,
            "supervisor_max_age_s": self.supervisor_max_age_s,
            "supervisor_log_file": self.supervisor_log_file,
            "agent_turn_file": self.agent_turn_file,
            "executor_heartbeat_file": self.executor_heartbeat_file,
            "executor_lock_file": self.executor_lock_file,
            "wild_cycle_ledger": self.wild_cycle_ledger,
        }


@dataclass(frozen=True)
class Criterion:
    """One evaluated wake condition.  ``fires=False`` is recorded too, on purpose."""

    kind: WakeKind
    fires: bool
    reason: str
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "fires": self.fires,
                "reason": self.reason, "evidence": self.evidence}


def parse_deadline(value: str | datetime | None) -> datetime | None:
    """Parse an absolute deadline; a naive timestamp is rejected, not guessed.

    A deadline without an offset is exactly the bug that let a "06:30" deadline roll
    into the next day, so this mirrors ``tools/supervise.py`` and refuses it.
    """

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value).strip())
        except ValueError as exc:
            raise ValueError(f"deadline {value!r} is not ISO-8601") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"deadline {value!r} must carry a timezone offset, e.g. "
                         f"2026-11-07T07:00:00+08:00")
    return moment


def remaining_seconds(deadline: datetime | None, now_epoch: float) -> float | None:
    if deadline is None:
        return None
    return deadline.timestamp() - float(now_epoch)


def _any_file(paths: tuple[str, ...]) -> str | None:
    for path in paths:
        if Path(path).exists():
            return path
    return None


def evaluate_criteria(cfg: WatchdogConfig, facts: FactSet, night: NightState, *,
                      now: float, deadline: datetime | None,
                      session_start_ts: float | None,
                      seen_milestone_seq: int) -> list[Criterion]:
    """Evaluate every wake condition; returns all of them, firing or not.

    Recording the non-firing ones matters for the audit: "the watchdog was running and
    the stall rule was *not* satisfied" is a fact a post-mortem needs.

    Rules are independent on purpose — a night can be stalled while the executor is
    dead, or ``NO_WORK`` while the executor is alive.  No rule derives from another.
    """

    out: list[Criterion] = []

    # 1. STALL — time since verified progress (or since session start when nothing has
    #    ever been proven).  Only fires on an explicit FALSE, never on UNKNOWN.
    progress_fact = facts.get(FactKind.GAME_PROGRESSING)
    baseline = night.last_verified_progress_ts or session_start_ts
    if progress_fact.state is FactState.FALSE and baseline is not None:
        elapsed = max(0.0, now - baseline)
        origin = ("last verified progress" if night.last_verified_progress_ts
                  else "session start (no verified progress ever)")
        out.append(Criterion(
            WakeKind.STALL, elapsed >= cfg.stall_s,
            f"no verified progress for {elapsed:.0f}s (> {cfg.stall_s:.0f}s)"
            if elapsed >= cfg.stall_s else
            f"verified progress {elapsed:.0f}s ago (<= {cfg.stall_s:.0f}s)",
            progress_fact.evidence,
        ))
    else:
        out.append(Criterion(WakeKind.STALL, False,
                             f"progress fact is {progress_fact.state.value}; "
                             f"no stall can be claimed", progress_fact.evidence))

    # 2. NO_WORK_STREAK — the 2026-11-04 signature: batches that refuse to schedule.
    out.append(Criterion(
        WakeKind.NO_WORK_STREAK,
        night.consecutive_no_work >= cfg.no_work_streak,
        f"{night.consecutive_no_work} consecutive NO_WORK batch(es) "
        f"(threshold {cfg.no_work_streak})",
        f"last outcome {night.last_outcome.value if night.last_outcome else 'none'}",
    ))

    # 3. BLOCKED — a hard blocker retrying cannot clear.
    out.append(Criterion(
        WakeKind.BLOCKED, bool(night.blocked),
        f"blocked: {night.blocked_reason}" if night.blocked else "not blocked",
        str(night.blocked_reason or ""),
    ))

    # 4. UNVERIFIED_ACTIVITY — acted, recorded actions, proved nothing.
    out.append(Criterion(
        WakeKind.UNVERIFIED_ACTIVITY,
        night.consecutive_unverified >= cfg.unverified_streak,
        f"{night.consecutive_unverified} consecutive batch(es) acted without proof "
        f"(threshold {cfg.unverified_streak})",
        "batch records carry battles/actions but no completion proof",
    ))

    # 5. MILESTONE — new proven progress since this watchdog last reported one.
    out.append(Criterion(
        WakeKind.MILESTONE, night.milestone_seq > seen_milestone_seq,
        f"milestone #{night.milestone_seq} recorded (already reported up to "
        f"#{seen_milestone_seq})" if night.milestone_seq > seen_milestone_seq
        else f"no new milestone after #{seen_milestone_seq}",
        str((night.last_milestone or {}).get("proof") or
            (night.last_milestone or {}).get("node") or ""),
    ))

    # 6. DEADLINE — the hard stop.
    remaining = remaining_seconds(deadline, now)
    if remaining is None:
        out.append(Criterion(WakeKind.DEADLINE, False,
                             "no deadline is configured", "checkpoint/watchdog"))
    elif remaining <= 0:
        out.append(Criterion(WakeKind.DEADLINE, True,
                             f"deadline passed {-remaining:.0f}s ago", "deadline"))
    elif remaining <= cfg.deadline_warn_s:
        out.append(Criterion(WakeKind.DEADLINE, True,
                             f"deadline in {remaining:.0f}s "
                             f"(<= {cfg.deadline_warn_s:.0f}s warning window)",
                             "deadline"))
    else:
        out.append(Criterion(WakeKind.DEADLINE, False,
                             f"deadline in {remaining:.0f}s", "deadline"))

    # 7. PAUSE — a human pressed pause.  Not an incident; still a wake, because the
    #    agent layer must stop spending turns while the human is in control.
    pause_path = _any_file(cfg.pause_files)
    out.append(Criterion(
        WakeKind.PAUSE, pause_path is not None,
        f"pause flag present: {pause_path}" if pause_path else "no pause flag",
        str(pause_path or ""),
    ))

    # 8. EXECUTOR_DEAD — explicitly dead, not merely unobserved.
    executor = facts.get(FactKind.EXECUTOR_ALIVE)
    out.append(Criterion(
        WakeKind.EXECUTOR_DEAD, executor.state is FactState.FALSE,
        (f"local executor is dead: {executor.evidence}"
         if executor.state is FactState.FALSE
         else f"executor fact is {executor.state.value}"),
        executor.evidence,
    ))
    return out


class Watchdog:
    """Runs the criteria each tick, fires de-flapped wakes, persists what it saw."""

    CHECKPOINT_KEY = "watchdog"

    def __init__(self, ledger: Ledger | None = None,
                 config: WatchdogConfig | None = None, *,
                 checkpoint_path: str | None = None,
                 clock: Callable[[], float] = time.time,
                 process_probe: Callable[[], list[str]] | None = None,
                 state: Mapping[str, Any] | None = None) -> None:
        self.ledger = ledger or Ledger()
        self.config = config or WatchdogConfig()
        self.checkpoint_path = checkpoint_path
        self.clock = clock
        self._process_probe = process_probe
        self._cache = DerivedCache(self.ledger)
        #: last applied wake per kind (epoch), plus suppression counters.
        self.state: dict[str, Any] = dict(state or {})
        self._probe_cache: list[str] | None = None
        self._probe_at: float = 0.0
        self._probe_failed: bool = False
        self._pending_suppressed: dict[str, dict[str, float]] = {}
        self._last_report_signature: tuple[Any, ...] | None = None
        self._last_persist_epoch: float = 0.0
        self._last_deadline: datetime | None = None
        self.cycles = 0

    # -- state ------------------------------------------------------------- #
    @property
    def wake_last(self) -> dict[str, float]:
        return self.state.setdefault("wake_last_epoch", {})

    @property
    def suppressed_total(self) -> int:
        return int(self.state.get("suppressed_total") or 0)

    @property
    def wakes_total(self) -> int:
        return int(self.state.get("wakes_total") or 0)

    def _load_checkpoint(self) -> dict[str, Any]:
        checkpoint = self.ledger.load_checkpoint(path=self.checkpoint_path,
                                                 default={}) or {}
        stored = checkpoint.get(self.CHECKPOINT_KEY)
        if isinstance(stored, Mapping) and not self.state:
            self.state.update({k: v for k, v in stored.items()
                               if k in ("wake_last_epoch", "seen_milestone_seq",
                                        "suppressed_total", "wakes_total",
                                        "started_epoch", "last_persist_epoch")})
        return checkpoint

    def _save_state(self) -> None:
        payload = {
            "wake_last_epoch": {k: round(v, 3) for k, v in self.wake_last.items()},
            "seen_milestone_seq": int(self.state.get("seen_milestone_seq") or 0),
            "suppressed_total": self.suppressed_total,
            "wakes_total": self.wakes_total,
            "last_persist_epoch": round(self._last_persist_epoch, 3),
            "started_epoch": self.state.get("started_epoch") or round(self.clock(), 3),
            "cooldown_s": self.config.cooldown_s,
        }
        # Locked read-modify-write: a mode switch saved by the CLI between our load
        # and our save must not be lost (2026-11-06 acceptance, A19).
        def mutate(checkpoint: dict[str, Any]) -> None:
            checkpoint[self.CHECKPOINT_KEY] = payload

        self.ledger.mutate_checkpoint(mutate, path=self.checkpoint_path)

    # -- supervisor probe (throttled: ps is not free) ---------------------- #
    def _probe(self, now: float) -> Callable[[], list[str]] | None:
        """The (throttled) supervisor probe, or None when there is no usable one.

        ``ps`` is not free: a sub-second loop calls it at most once per
        ``probe_every_s``, and a probe that raised is retried only at that interval.
        """

        if self._process_probe is None:
            return None
        if now - self._probe_at >= self.config.probe_every_s:
            self._probe_at = now
            try:
                self._probe_cache = list(self._process_probe())
                self._probe_failed = False
            except Exception:  # noqa: BLE001 - a broken probe must not kill the loop
                self._probe_cache = None
                self._probe_failed = True
        if self._probe_failed or self._probe_cache is None:
            return None
        return lambda: list(self._probe_cache)

    # -- one tick ---------------------------------------------------------- #
    def step(self, now: float | None = None) -> dict[str, Any]:
        """Evaluate once, fire wakes, persist according to the throttle."""

        now = float(now if now is not None else self.clock())
        self.cycles += 1
        checkpoint = self._load_checkpoint()
        deadline_error: str | None = None
        try:
            deadline = parse_deadline(self.config.deadline or checkpoint.get("deadline"))
        except ValueError as exc:
            # A corrupt stored deadline must degrade to "no deadline", never stop the
            # watchdog: at 03:00 a silent shutdown would be worse than a missing rule.
            deadline = None
            deadline_error = str(exc)
        session_start = parse_ts(checkpoint.get("session_started_ts"))
        night = self._cache.get(checkpoint=checkpoint, now=now)
        facts = collect(ledger=self.ledger, checkpoint=checkpoint, now=now,
                        process_probe=self._probe(now),
                        supervisor_log_file=self.config.supervisor_log_file,
                        supervisor_max_age_s=self.config.supervisor_max_age_s,
                        agent_turn_file=self.config.agent_turn_file,
                        executor_heartbeat_file=self.config.executor_heartbeat_file,
                        lock_file=self.config.executor_lock_file,
                        wild_cycle_ledger=self.config.wild_cycle_ledger,
                        progress_window_s=self.config.progress_window_s,
                        heartbeat_max_age_s=self.config.heartbeat_max_age_s,
                        decide_max_age_s=self.config.decide_max_age_s,
                        night=night)
        criteria = evaluate_criteria(
            self.config, facts, night, now=now, deadline=deadline,
            session_start_ts=session_start,
            seen_milestone_seq=int(self.state.get("seen_milestone_seq") or 0))

        wakes: list[dict[str, Any]] = []
        for criterion in criteria:
            if not criterion.fires:
                continue
            kind = criterion.kind
            last = self.wake_last.get(kind.value)
            since = (now - last) if last is not None else None
            cooldown = self.config.cooldown_for(kind)
            if last is not None and since is not None and since < cooldown:
                # Suppressed: still counted, and the count is what gets persisted.
                bucket = self._pending_suppressed.setdefault(
                    kind.value, {"count": 0.0, "first_epoch": now, "last_epoch": now})
                bucket["count"] += 1
                bucket["last_epoch"] = now
                self.state["suppressed_total"] = self.suppressed_total + 1
                wakes.append({
                    "kind": kind.value, "applied": False, "suppressed": True,
                    "reason": criterion.reason, "evidence": criterion.evidence,
                    "cooldown_s": cooldown,
                    "since_last_s": round(since, 3),
                    "remaining_cooldown_s": round(cooldown - since, 3),
                    "suppressed_in_cooldown": 1,
                })
                continue

            self.wake_last[kind.value] = now
            self.state["wakes_total"] = self.wakes_total + 1
            if kind is WakeKind.MILESTONE:
                self.state["seen_milestone_seq"] = night.milestone_seq
            event = {
                "kind": "wake",
                "wake": kind.value,
                "reason": criterion.reason,
                "evidence": criterion.evidence,
                "facts": facts.compact(),
            }
            self.ledger.append("events", event)
            wakes.append({
                "kind": kind.value, "applied": True, "suppressed": False,
                "reason": criterion.reason, "evidence": criterion.evidence,
                "cooldown_s": cooldown,
                "since_last_s": round(since, 3) if since is not None else None,
            })

        report = {
            "kind": "health",
            "facts": facts.to_dict(),
            "facts_compact": facts.compact(),
            "deadline": deadline.isoformat() if deadline else None,
            "deadline_error": deadline_error,
            "remaining_s": (round(remaining_seconds(deadline, now), 1)
                            if deadline else None),
            "night": night.to_dict(),
            "mode": checkpoint.get("mode"),
            "policy": checkpoint.get("policy"),
            "executor_age_s": facts.get(FactKind.EXECUTOR_ALIVE).age_s,
            "checks": [c.to_dict() for c in criteria],
            "wakes": wakes,
            "applied": [w["kind"] for w in wakes if w["applied"]],
            "suppressed_this_tick": [w["kind"] for w in wakes if w["suppressed"]],
            "suppressed_pending": {k: {"count": int(v["count"]),
                                       "first_epoch": round(v["first_epoch"], 3),
                                       "last_epoch": round(v["last_epoch"], 3)}
                                   for k, v in self._pending_suppressed.items()},
            "suppressed_total": self.suppressed_total,
            "wakes_total": self.wakes_total,
            "config": self.config.to_dict(),
        }

        persist = self._should_persist(report, now)
        report["persisted"] = persist
        if persist:
            record = self.ledger.append("runtime_health", report)
            report["ts"] = record.get("ts")
            report["ts_epoch"] = record.get("ts_epoch")
            self._pending_suppressed.clear()
            self._last_persist_epoch = now
            self._last_report_signature = self._signature(report)
            try:
                self._save_state()
                report["state_persisted"] = True
            except LedgerError as exc:
                # Another writer holds the checkpoint lock (acceptance finding A21).
                # The evaluation is still valid; degrade and leave a durable record
                # instead of killing the watch loop or claiming the state was saved.
                report["state_persisted"] = False
                report["state_error"] = f"{type(exc).__name__}: {exc}"
                self.note_lock_timeout(exc)
        return report

    def flush(self, now: float | None = None) -> dict[str, Any] | None:
        """Write a final health record so pending suppression counters are durable.

        The loop throttles ``runtime_health`` writes, so a clean ``--cycles N`` exit
        could otherwise drop the counters that only lived in memory: the exit JSON
        printed ``suppressed_total=4`` while the checkpoint still said 0 (acceptance
        finding A8).  A final record + a state save closes that gap.
        """

        now = float(now if now is not None else self.clock())
        pending = {k: {"count": int(v["count"]),
                       "first_epoch": round(v["first_epoch"], 3),
                       "last_epoch": round(v["last_epoch"], 3)}
                   for k, v in self._pending_suppressed.items()}
        record = self.ledger.append("runtime_health", {
            "kind": "health.final",
            "final": True,
            "cycles": self.cycles,
            "facts_compact": {},
            "wakes_total": self.wakes_total,
            "suppressed_total": self.suppressed_total,
            "suppressed_pending": pending,
            "deadline": None,
            "remaining_s": None,
            "night": {},
            "checks": [],
            "wakes": [],
            "applied": [],
            "suppressed_this_tick": [],
            "persisted": True,
        }, ts=now)
        self._pending_suppressed.clear()
        self._last_persist_epoch = now
        try:
            self._save_state()
        except LedgerError as exc:
            self.note_lock_timeout(exc)
        return record

    def note_lock_timeout(self, exc: BaseException) -> None:
        """Record checkpoint-lock contention durably; never raises.

        Called from ``step``/``flush``/``run`` when the checkpoint lock is held by
        another process.  ``runtime_health`` appends do not need the lock, so this
        trace survives even while the lock is unavailable.
        """

        self.lock_timeouts = int(getattr(self, "lock_timeouts", 0)) + 1
        self.ledger._append_safely("runtime_health", {
            "kind": "health.lock_timeout",
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "cycles": self.cycles,
            "lock_timeouts": self.lock_timeouts,
        })

    def _should_persist(self, report: Mapping[str, Any], now: float) -> bool:
        # An applied wake is a change worth an fsync immediately.  A *suppressed*
        # wake is not: at a 0.5 s cadence a stalled night would otherwise fsync a
        # health record twice a second all night.  Suppressions are counted in
        # state and flushed on the next change / persist_every_s boundary.
        if report["applied"]:
            return True
        if now - self._last_persist_epoch >= self.config.persist_every_s:
            return True
        signature = self._signature(report)
        return signature != self._last_report_signature

    @staticmethod
    def _signature(report: Mapping[str, Any]) -> tuple[Any, ...]:
        """What must change for a health record to be worth writing."""

        return (
            tuple(sorted((report.get("facts_compact") or {}).items())),
            tuple((c["kind"], c["fires"]) for c in report.get("checks", ())),
            report["night"].get("last_outcome"),
            report["night"].get("last_verified_progress_ts"),
            report.get("mode"),
        )

    # -- the loop ---------------------------------------------------------- #
    def run(self, *, cycles: int | None = None,
            sleep: Callable[[float], None] | None = None,
            on_report: Callable[[dict[str, Any]], None] | None = None) -> int:
        """Run the local loop.  ``cycles`` exists so tests never have to sleep.

        Exits early when a stop file appears (a human asked everything to stop) or on
        ``KeyboardInterrupt``; both are normal ends for a night watchdog, so neither
        is turned into an error.
        """

        sleep = sleep or time.sleep
        done = 0
        try:
            while cycles is None or done < cycles:
                try:
                    report = self.step()
                except LedgerError as exc:
                    # Defensive: step() already degrades its own state save, but a
                    # lock timeout anywhere else must not kill an unattended loop.
                    self.note_lock_timeout(exc)
                    done += 1
                    if cycles is not None and done >= cycles:
                        break
                    sleep(self.config.cadence_s)
                    continue
                done += 1
                if on_report is not None:
                    on_report(report)
                stop_path = _any_file(self.config.stop_files)
                if stop_path is not None:
                    self.ledger.append("events", {
                        "kind": "watchdog.stop", "reason": f"stop flag {stop_path}",
                        "cycles": done})
                    break
                if cycles is not None and done >= cycles:
                    break
                sleep(self.config.cadence_s)
        except KeyboardInterrupt:
            self.ledger.append("events", {"kind": "watchdog.stop",
                                          "reason": "KeyboardInterrupt",
                                          "cycles": done})
        finally:
            # Every suppression counted since the last health record is flushed here,
            # so the counters in the exit JSON are the counters on disk.  A lock
            # timeout at exit is recorded, never raised (A21).
            try:
                self.flush()
            except LedgerError as exc:
                self.note_lock_timeout(exc)
        return done
