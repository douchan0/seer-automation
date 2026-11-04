"""How a goal run ended — and whether that ending is progress.

The 2026-11-04 supervisor produced 26 batches that all exited 0 while doing
nothing: `stats battles=0 actions=0`, stop reason `planner refused: nothing
schedulable` every time.  Exit 0 meant "the process returned", so the supervisor
kept its failure counter at zero and looped until the deadline.

This module exists so that mistake cannot be made again by accident: the runner's
own stop reason is mapped onto an explicit outcome, and callers get a distinct
exit code per outcome instead of one blanket 0.

The distinction that matters for long-horizon work is:

    did the run make the goal move?   (verified_progress / did_work)
    or did it merely finish?          (everything else)

A batch that fights battles is *work*; only a completion predicate turning true is
*verified progress*.  Both are reported separately because a grind node can do
real work for many batches before its predicate flips, and a supervisor that
conflates the two either gives up too early or spins forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

__all__ = [
    "Outcome",
    "RunVerdict",
    "classify_run",
    "classify_reason",
    "completed_with_proof",
    "exit_code_for",
    "EXIT_CODES",
]


class Outcome(str, Enum):
    """Terminal classification of one `goal start` / `goal resume` batch."""

    SUCCESS = "SUCCESS"      # a node completed, or the goal ran out of executable work
    NO_WORK = "NO_WORK"      # nothing was schedulable; the run did not act at all
    FAILED = "FAILED"        # crash, client gone, error budget, stuck node
    TIMEOUT = "TIMEOUT"      # a time/battle/action budget ran out
    PAUSED = "PAUSED"        # a human asked it to stop, or it was interrupted
    BLOCKED = "BLOCKED"      # a hard blocker that retrying cannot fix (needs a human)


# Distinct process exit codes.  Callers (supervise.py, cron, CI) must branch on
# these rather than on "non-zero == bad": NO_WORK is not a crash, and PAUSED is not
# a failure, but neither is success.
EXIT_CODES: dict[Outcome, int] = {
    Outcome.SUCCESS: 0,
    Outcome.FAILED: 1,
    Outcome.NO_WORK: 3,
    Outcome.TIMEOUT: 124,   # matches coreutils `timeout`, which scripts already know
    # 130 = "terminated by Ctrl-C", the convention scripts already read as
    # "a human stopped this".  Sharing SUCCESS's 0 made a human pause look healthy.
    Outcome.PAUSED: 130,
    Outcome.BLOCKED: 7,
}


def exit_code_for(outcome: Outcome) -> int:
    return EXIT_CODES[outcome]


def completed_with_proof(nodes: Mapping[str, Any] | None,
                         proofs: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Node ids that are `completed` *and* carry a proof string.

    One definition, used by both the classifier and the supervisor.  The two used to
    disagree: the classifier demanded a proof, the supervisor took the status string
    at face value, so the same node counted as verified progress in one place and not
    in the other (found by the 2026-11-05 independent review).
    """

    proof_map = proofs or {}
    return tuple(sorted(
        node_id for node_id, status in (nodes or {}).items()
        if str(status) == "completed" and proof_map.get(node_id)
    ))


# Reason prefixes the runner actually emits (seerauto/runner/runner.py).  Matching
# on them keeps this module free of imports from the runner, so it stays unit
# testable and cannot create a cycle.
_REFUSAL_MARKERS = ("planner refused", "nothing schedulable")
_CLIENT_MARKERS = ("client is not showing game content", "not in the game world")
_CRASH_MARKERS = ("crashed:",)
_STUCK_MARKERS = ("without the completion predicate turning true",)
_PAUSE_MARKERS = ("stop requested", "interrupted (sigint)")
_BLOCKED_MARKERS = ("blocked:",)
_SUCCESS_MARKERS = ("all executable nodes complete", "goal progressed: node completed")
_TIMEOUT_MARKERS = (
    "time budget", "battle budget", "action budget", "loop budget",
)
_ERROR_BUDGET = "error budget"


def classify_reason(reason: str | None) -> Outcome:
    """Map one stop-reason string onto an outcome.  Pure; order matters.

    The refusal check comes first on purpose: a planner refusal embeds the reason
    for every node it skipped, so the string contains fragments like
    ``status=blocked:`` and ``not in the game world``.  Matching those before
    "planner refused" is how a batch that did nothing got classified BLOCKED
    (exit 7) instead of NO_WORK (exit 3) during the 2026-11-04 run.
    """

    text = (reason or "").strip()
    low = text.lower()
    if not text or low == "unknown":
        return Outcome.FAILED
    if any(m in low for m in _CRASH_MARKERS):
        return Outcome.FAILED
    if any(m in low for m in _REFUSAL_MARKERS):
        return Outcome.NO_WORK
    if any(m in low for m in _CLIENT_MARKERS):
        return Outcome.FAILED
    if any(m in low for m in _BLOCKED_MARKERS):
        return Outcome.BLOCKED
    if any(m in low for m in _STUCK_MARKERS):
        return Outcome.FAILED
    if any(m in low for m in _PAUSE_MARKERS):
        return Outcome.PAUSED
    if any(m in low for m in _SUCCESS_MARKERS):
        return Outcome.SUCCESS
    if _ERROR_BUDGET in low:
        return Outcome.FAILED
    if any(m in low for m in _TIMEOUT_MARKERS):
        return Outcome.TIMEOUT
    if low.startswith("dry run complete"):
        # A dry run that planned something is a successful dry run; it never
        # counts as verified progress because it sent no input.  `classify_run`
        # exempts this outcome from the SUCCESS->NO_WORK downgrade, otherwise this
        # branch is dead code.
        return Outcome.SUCCESS
    return Outcome.FAILED


@dataclass(frozen=True)
class RunVerdict:
    """One batch's outcome plus the numbers a supervisor needs to judge it."""

    outcome: Outcome
    reason: str
    battles: int = 0
    actions: int = 0
    errors: int = 0
    loops: int = 0
    elapsed_s: float = 0.0
    completed_nodes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.outcome)

    @property
    def verified_progress(self) -> bool:
        """True only when a completion predicate actually turned true."""
        return bool(self.completed_nodes)

    @property
    def did_work(self) -> bool:
        """True when the batch really acted: at least one battle was fought.

        Deliberately strict.  A single stray click (`actions=1`) is how the
        2026-11-04 batches looked while accomplishing nothing, so `actions` alone
        is not enough to call a batch productive.
        """
        return self.battles > 0

    @property
    def effective(self) -> bool:
        """A batch is effective if it fought, or if it verifiably completed a node."""
        return self.did_work or self.verified_progress

    @property
    def is_no_work(self) -> bool:
        return self.outcome is Outcome.NO_WORK

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "battles": self.battles,
            "actions": self.actions,
            "errors": self.errors,
            "loops": self.loops,
            "elapsed_s": self.elapsed_s,
            "completed_nodes": list(self.completed_nodes),
            "effective": self.effective,
            "verified_progress": self.verified_progress,
            "exit_code": self.exit_code,
        }


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def classify_run(summary: Mapping[str, Any] | None) -> RunVerdict:
    """Classify a `GoalRunner.summary()` dict.

    A missing summary is a FAILED batch, not a silent success: the caller could not
    establish what happened, and guessing "fine" is what caused the original bug.
    """

    if not summary:
        return RunVerdict(outcome=Outcome.FAILED, reason="no run summary was produced")

    reason = str(summary.get("stop_reason") or "")
    stats = summary.get("stats") or {}
    nodes = summary.get("nodes") or {}
    proofs = summary.get("proofs") or {}

    # A completion predicate must have produced a proof string.  A bare
    # "completed" status is a claim, not evidence (AGENTS.md §5.3).
    completed = completed_with_proof(nodes, proofs)
    # A node marked completed without a proof string is not evidence.  Fall back to
    # the stop reason so a genuine completion is not thrown away.
    outcome = classify_reason(reason)
    dry_run_ok = reason.lower().startswith("dry run complete")
    if (outcome is Outcome.SUCCESS and not completed and not dry_run_ok
            and reason != "goal progressed: node completed"
            and "all executable nodes complete" not in reason):
        outcome = Outcome.NO_WORK

    return RunVerdict(
        outcome=outcome,
        reason=reason or "(no stop reason recorded)",
        battles=_as_int(stats.get("battles")),
        actions=_as_int(stats.get("actions")),
        errors=_as_int(stats.get("errors")),
        loops=_as_int(stats.get("loops")),
        elapsed_s=float(stats.get("elapsed_s") or 0.0),
        completed_nodes=completed,
    )
