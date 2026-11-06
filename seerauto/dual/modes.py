"""The two run modes, the two policies, and the rules that pick the effective mode.

The experiment this module exists for: run the same long-horizon goal in two modes
and find out whether the cheap one is enough.

* ``EFFICIENT`` — the local state machine does the work.  No model turn is spent on
  ordinary clicks, battles or navigation (the project's core rule).  This is the
  default and the mode the night should return to as quickly as possible.
* ``MAX_RESULTS`` — the agent layer may spend model turns on planning, research or
  vision-failure analysis.  Expensive on purpose; it is an *exception* state, not a
  place to live.

``Policy`` says how the mode is allowed to move:

* ``ADAPTIVE`` — :func:`resolve` recomputes the effective mode from observed signals
  each time it is asked.  No memory, no model call: the rules are a table.
* ``STATIC`` — the mode is whatever an operator pinned; signals are recorded but do
  not change it.  Used when a human wants a clean A/B window.

Why the reasons live here and not only in :mod:`seerauto.dual.switch`: every rule
below produces exactly one :class:`SwitchReason`, so a mode change always has a
machine-readable "why" and the audit record cannot contain free text where an enum
belongs.  ``switch`` re-exports the enum (see its docstring) to keep the import graph
acyclic: ``switch`` -> ``modes``, never back.

Thresholds are arguments with defaults, not constants, so tests can pin a rule
without sleeping and an operator can tune one without editing code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "Mode",
    "Policy",
    "SwitchReason",
    "Signals",
    "ModeResolution",
    "resolve",
    "DEFAULT_DEADLINE_WINDOW_S",
]


class Mode(str, Enum):
    """How much model work the night is allowed to buy."""

    EFFICIENT = "EFFICIENT"
    MAX_RESULTS = "MAX_RESULTS"

    @classmethod
    def parse(cls, value: "Mode | str | None", *, default: "Mode | None" = None) -> "Mode":
        """Case/space tolerant parse; ``default`` (or raise) when unusable."""

        if isinstance(value, cls):
            return value
        if value is None or str(value).strip() == "":
            if default is not None:
                return default
            raise ValueError("mode is required (EFFICIENT or MAX_RESULTS)")
        try:
            return cls(str(value).strip().upper())
        except ValueError as exc:
            raise ValueError(f"unknown mode {value!r}; expected EFFICIENT or "
                             f"MAX_RESULTS") from exc


class Policy(str, Enum):
    """Whether signals are allowed to move the mode."""

    ADAPTIVE = "ADAPTIVE"
    STATIC = "STATIC"

    @classmethod
    def parse(cls, value: "Policy | str | None", *, default: "Policy | None" = None) -> "Policy":
        if isinstance(value, cls):
            return value
        if value is None or str(value).strip() == "":
            if default is not None:
                return default
            raise ValueError("policy is required (ADAPTIVE or STATIC)")
        try:
            return cls(str(value).strip().upper())
        except ValueError as exc:
            raise ValueError(f"unknown policy {value!r}; expected ADAPTIVE or "
                             f"STATIC") from exc


class SwitchReason(str, Enum):
    """Why the effective mode changed.  One member per rule in :func:`resolve`.

    Escalations (EFFICIENT -> MAX_RESULTS): NEW_COMPLEX_TASK, GRAPH_BLOCKED,
    NEED_RESEARCH, EVIDENCE_CONFLICT, VISION_FAILURE, RECOVERY_FAILED,
    LONG_NO_PROGRESS, OPPORTUNITY.

    De-escalations (MAX_RESULTS -> EFFICIENT): RESOLVED (the thing that needed model
    work is settled) and MARGINAL_VALUE_DROP (more model work has stopped paying for
    itself, including the "deadline is too close to start anything new" floor).
    """

    NEW_COMPLEX_TASK = "NEW_COMPLEX_TASK"
    LONG_NO_PROGRESS = "LONG_NO_PROGRESS"
    GRAPH_BLOCKED = "GRAPH_BLOCKED"
    NEED_RESEARCH = "NEED_RESEARCH"
    VISION_FAILURE = "VISION_FAILURE"
    MARGINAL_VALUE_DROP = "MARGINAL_VALUE_DROP"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    OPPORTUNITY = "OPPORTUNITY"
    RESOLVED = "RESOLVED"

    @classmethod
    def parse(cls, value: "SwitchReason | str | None") -> "SwitchReason":
        if isinstance(value, cls):
            return value
        if value is None or str(value).strip() == "":
            raise ValueError("a switch reason is required")
        try:
            return cls(str(value).strip().upper())
        except ValueError as exc:
            raise ValueError(f"unknown switch reason {value!r}; expected one of "
                             f"{', '.join(m.value for m in cls)}") from exc


#: Below this much wall-clock slack, MAX_RESULTS is not worth entering: a research
#: arc started four minutes before 07:00 will not finish, so the rule fires
#: EFFICIENT/MARGINAL_VALUE_DROP.  Thirty minutes is one supervisor batch plus slack.
DEFAULT_DEADLINE_WINDOW_S = 1800.0


@dataclass(frozen=True)
class Signals:
    """What an adapter can observe without asking a model.

    Every field is optional and ``None`` means *unknown*, not zero: ``minutes_since_
    progress=None`` (nothing has ever been observed) must not fire the stall rule, or
    a fresh night would start in MAX_RESULTS on a fact nobody measured.
    """

    minutes_since_progress: float | None = None
    consecutive_no_work: int = 0
    consecutive_unverified: int = 0
    blocked: bool = False
    unresolved_questions: int = 0
    evidence_conflicts: int = 0
    vision_failures: int = 0
    recovery_failures: int = 0
    deadline_remaining_s: float | None = None
    new_task: bool = False
    opportunity: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "minutes_since_progress": self.minutes_since_progress,
            "consecutive_no_work": self.consecutive_no_work,
            "consecutive_unverified": self.consecutive_unverified,
            "blocked": self.blocked,
            "unresolved_questions": self.unresolved_questions,
            "evidence_conflicts": self.evidence_conflicts,
            "vision_failures": self.vision_failures,
            "recovery_failures": self.recovery_failures,
            "deadline_remaining_s": self.deadline_remaining_s,
            "new_task": self.new_task,
            "opportunity": self.opportunity,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ModeResolution:
    """The effective mode plus the rule that produced it (auditable, not a guess)."""

    mode: Mode
    policy: Policy
    reason: SwitchReason | None
    rule: str
    because: tuple[str, ...] = field(default_factory=tuple)
    signals: dict[str, Any] = field(default_factory=dict)

    @property
    def escalated(self) -> bool:
        return self.mode is Mode.MAX_RESULTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "policy": self.policy.value,
            "reason": self.reason.value if self.reason else None,
            "rule": self.rule,
            "because": list(self.because),
            "signals": dict(self.signals),
        }


def _resolution(mode: Mode, policy: Policy, reason: SwitchReason | None, rule: str,
                because: str | tuple[str, ...], signals: Signals) -> ModeResolution:
    parts = (because,) if isinstance(because, str) else because
    return ModeResolution(mode=mode, policy=policy, reason=reason, rule=rule,
                          because=tuple(parts), signals=signals.to_dict())


def resolve(policy: "Policy | str",
            signals: Signals | None = None,
            *,
            current: "Mode | str | None" = None,
            static_mode: "Mode | str" = Mode.EFFICIENT,
            deadline_window_s: float = DEFAULT_DEADLINE_WINDOW_S,
            stall_minutes: float = 20.0,
            no_work_threshold: int = 2,
            unverified_threshold: int = 3,
            vision_failure_threshold: int = 2,
            recovery_failure_threshold: int = 2) -> ModeResolution:
    """Pick the effective mode from policy + signals.  Pure; no I/O, no model call.

    ADAPTIVE rules are evaluated in this order and the **first** match owns the
    reason — safety/economy first, then "the local executor cannot proceed", then
    "we have data quality problems", then "we are not moving":

    1. deadline slack <= ``deadline_window_s``  -> EFFICIENT / MARGINAL_VALUE_DROP
    2. ``signals.new_task``                      -> MAX_RESULTS / NEW_COMPLEX_TASK
    3. ``signals.blocked``                       -> MAX_RESULTS / GRAPH_BLOCKED
    4. ``signals.unresolved_questions > 0``      -> MAX_RESULTS / NEED_RESEARCH
    5. ``signals.evidence_conflicts > 0``        -> MAX_RESULTS / EVIDENCE_CONFLICT
    6. vision failures >= threshold              -> MAX_RESULTS / VISION_FAILURE
    7. recovery failures >= threshold            -> MAX_RESULTS / RECOVERY_FAILED
    8. no-work streak or stall minutes >= limit  -> MAX_RESULTS / LONG_NO_PROGRESS
    9. ``signals.opportunity``                   -> MAX_RESULTS / OPPORTUNITY
    else EFFICIENT with RESOLVED (coming back from MAX_RESULTS) or
    MARGINAL_VALUE_DROP (staying efficient).

    Rule 1 runs first deliberately: entering MAX_RESULTS half an hour before the
    deadline spends tokens that cannot be realized, which is the exact waste this
    experiment is trying to measure.
    """

    policy = Policy.parse(policy)
    signals = signals or Signals()

    if policy is Policy.STATIC:
        mode = Mode.parse(static_mode)
        return _resolution(mode, policy, None, "static",
                           (f"policy=STATIC pins the mode to {mode.value}",), signals)

    remaining = signals.deadline_remaining_s
    if remaining is not None and remaining <= deadline_window_s:
        return _resolution(
            Mode.EFFICIENT, policy, SwitchReason.MARGINAL_VALUE_DROP, "deadline_floor",
            f"deadline in {remaining:.0f}s <= {deadline_window_s:.0f}s window; "
            f"model work cannot be realized before it", signals)

    if signals.new_task:
        return _resolution(
            Mode.MAX_RESULTS, policy, SwitchReason.NEW_COMPLEX_TASK, "new_task",
            "a fresh complex task needs a plan before the executor can act", signals)

    if signals.blocked:
        return _resolution(
            Mode.MAX_RESULTS, policy, SwitchReason.GRAPH_BLOCKED, "graph_blocked",
            "the local graph reports a blocked node; local retrying cannot clear it",
            signals)

    if signals.unresolved_questions > 0:
        return _resolution(
            Mode.MAX_RESULTS, policy, SwitchReason.NEED_RESEARCH, "need_research",
            f"{signals.unresolved_questions} unresolved question(s) gate the next "
            f"action", signals)

    if signals.evidence_conflicts > 0:
        return _resolution(
            Mode.MAX_RESULTS, policy, SwitchReason.EVIDENCE_CONFLICT, "evidence_conflict",
            f"{signals.evidence_conflicts} conflicting evidence item(s) need "
            f"arbitration", signals)

    if signals.vision_failures >= vision_failure_threshold:
        return _resolution(
            Mode.MAX_RESULTS, policy, SwitchReason.VISION_FAILURE, "vision_failure",
            f"{signals.vision_failures} vision failure(s) >= "
            f"{vision_failure_threshold}; local recovery is not reading the screen",
            signals)

    if signals.recovery_failures >= recovery_failure_threshold:
        return _resolution(
            Mode.MAX_RESULTS, policy, SwitchReason.RECOVERY_FAILED, "recovery_failed",
            f"{signals.recovery_failures} failed recovery attempt(s) >= "
            f"{recovery_failure_threshold}", signals)

    stall_note: str | None = None
    if signals.consecutive_no_work >= no_work_threshold:
        stall_note = (f"{signals.consecutive_no_work} consecutive NO_WORK batches "
                      f">= {no_work_threshold}")
    elif (signals.minutes_since_progress is not None
          and signals.minutes_since_progress >= stall_minutes):
        stall_note = (f"{signals.minutes_since_progress:.0f} min without verified "
                      f"progress >= {stall_minutes:.0f}")
    if stall_note:
        return _resolution(
            Mode.MAX_RESULTS, policy, SwitchReason.LONG_NO_PROGRESS, "long_no_progress",
            stall_note, signals)

    if signals.opportunity:
        return _resolution(
            Mode.MAX_RESULTS, policy, SwitchReason.OPPORTUNITY, "opportunity",
            "a concrete new opportunity was found that the local graph cannot take",
            signals)

    previous = Mode.parse(current) if current is not None else None
    if previous is Mode.MAX_RESULTS:
        return _resolution(
            Mode.EFFICIENT, policy, SwitchReason.RESOLVED, "resolved",
            "no signal still requires model work; returning to the local executor",
            signals)
    return _resolution(
        Mode.EFFICIENT, policy, SwitchReason.MARGINAL_VALUE_DROP, "efficient_default",
        "the local state machine can carry the night; model turns would not add value",
        signals)
