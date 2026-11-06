"""The night's outcome vocabulary — eight distinct facts, no umbrella "ok".

Two concrete failures shaped this module.

**2026-11-04**: the supervisor ran 26 batches, 25 of them with
``battles=0 actions=0`` and stop reason ``planner refused: nothing schedulable``,
and counted every one as healthy because ``goal start`` exited 0.  "The process
returned" was silently standing in for "the goal moved".

**2026-11-06**: the live executors were already emitting a *different* vocabulary
in their ledgers — ``data/overnight/wild_cycle.jsonl`` writes
``outcome: PROGRESS|NO_WORK`` per pass and ``data/overnight/tower_ledger.jsonl``
writes ``PROGRESS|NO_WORK|BLOCKED`` plus an ``interrupted`` flag — while
``seerauto.runner.outcome`` used ``SUCCESS/FAILED/TIMEOUT`` and
``tools/supervise.py`` judged batches by that.  The same night therefore had three
answers to "how did it end" and no way to compare them.

This module is the single vocabulary the control plane reads and writes.  It is a
*superset* of the older ones rather than a replacement, so neither executor has to
change tonight:

==========================  ===================================  ==========================
Outcome                     means                                legacy source
==========================  ===================================  ==========================
``PROGRESS``                the goal verifiably moved            runner ``SUCCESS``,
                            (a proof string backs it)            wild_cycle ``PROGRESS``
``NO_WORK``                 nothing was schedulable / nothing    runner ``NO_WORK``
                            was attempted
``BLOCKED``                 a hard blocker retrying cannot fix   runner ``BLOCKED``
                            (a human or research must unblock it)
``FAILED``                  crash, client gone, error budget,     runner ``FAILED``
                            stuck node
``UNVERIFIED_ACTIVITY``     the executor acted, but no proof of   (new) — 2026-11-04's
                            progress and no failure either        ``actions=1, battles=0``
``PAUSED``                  a human asked it to stop              runner ``PAUSED``
``INTERRUPTED``             stopped by a signal / recovery, not   (new) — tower ledger
                            by a human decision                   ``interrupted: true``
``DEADLINE_REACHED``        a time budget / wall-clock deadline   runner ``TIMEOUT``
                            ended the run
==========================  ===================================  ==========================

Why ``PROGRESS`` and ``UNVERIFIED_ACTIVITY`` must not collapse into "success":
on 2026-11-04 a batch reported ``actions=1`` — a single stray click — and the
supervisor read the exit code as health.  "It acted" (work) and "it moved the goal"
(verified progress) are separate observations and are reported separately.

Why ``PAUSED`` and ``INTERRUPTED`` must not collapse either: a human pause is a
decision to be respected and resumed on request; an interruption is a failure of the
process that has to be explained.  The runner's ``PAUSED`` covers both because it
only ever saw a stop reason; the control plane sees the signal source and splits it.

Exit codes stay owned by :mod:`seerauto.runner.outcome` — deliberately.  Two exit
code tables that disagree is how a supervisor learns to trust the wrong number, and
the dual layer never spawns a subprocess that needs one.  What it does expose is
:func:`to_runner` / :func:`from_runner`, so a numeric code can always be derived
from the canonical outcome instead of the other way round.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Mapping

from ..runner.outcome import Outcome as RunnerOutcome

__all__ = [
    "Outcome",
    "ORDER",
    "RUNNER_MAP",
    "NO_RUNNER_EQUIVALENT",
    "LOSSY_RUNNER_MAP",
    "SYNONYMS",
    "canonical",
    "coerce",
    "from_runner",
    "to_runner",
    "classify_batch",
    "counts_as_progress",
    "split_last",
    "PROGRESS_OUTCOMES",
    "FAILURE_OUTCOMES",
    "HUMAN_STOP_OUTCOMES",
]


class Outcome(str, Enum):
    """One batch/pass's terminal classification.  Exactly these eight, no aliases."""

    PROGRESS = "PROGRESS"
    NO_WORK = "NO_WORK"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    UNVERIFIED_ACTIVITY = "UNVERIFIED_ACTIVITY"
    PAUSED = "PAUSED"
    INTERRUPTED = "INTERRUPTED"
    DEADLINE_REACHED = "DEADLINE_REACHED"


#: Canonical order (reporting order, not alphabetical) — the tuple tests pin.
ORDER: tuple[Outcome, ...] = (
    Outcome.PROGRESS,
    Outcome.NO_WORK,
    Outcome.BLOCKED,
    Outcome.FAILED,
    Outcome.UNVERIFIED_ACTIVITY,
    Outcome.PAUSED,
    Outcome.INTERRUPTED,
    Outcome.DEADLINE_REACHED,
)

#: Which outcomes mean "the goal verifiably moved".
PROGRESS_OUTCOMES: frozenset[Outcome] = frozenset({Outcome.PROGRESS})

#: Outcomes that need a human or a new decision, in escalating order of severity.
FAILURE_OUTCOMES: frozenset[Outcome] = frozenset({
    Outcome.FAILED,
    Outcome.BLOCKED,
    Outcome.UNVERIFIED_ACTIVITY,
})

#: Outcomes caused by a person / an external stop, not by the goal's state.
HUMAN_STOP_OUTCOMES: frozenset[Outcome] = frozenset({
    Outcome.PAUSED,
    Outcome.INTERRUPTED,
})

#: Canonical outcomes the legacy runner vocabulary cannot express at all.  Mapping
#: them onto a wrong legacy value would hide the split this module exists to keep, so
#: :func:`to_runner` refuses instead of guessing.
NO_RUNNER_EQUIVALENT: frozenset[Outcome] = frozenset({Outcome.UNVERIFIED_ACTIVITY})

#: Canonical outcomes that *do* map, but not reversibly: the runner saw only a stop
#: reason, so it could not tell a human pause from a signal/recovery interrupt.
#: ``to_runner`` is allowed to lose this distinction; the ledger is where it survives.
LOSSY_RUNNER_MAP: frozenset[Outcome] = frozenset({Outcome.INTERRUPTED})

#: Many-to-one, because the runner's vocabulary is coarser (it has no signal source
#: and no "acted but unproven" bucket).  Kept explicit so the loss is auditable.
RUNNER_MAP: dict[RunnerOutcome, Outcome] = {
    RunnerOutcome.SUCCESS: Outcome.PROGRESS,
    RunnerOutcome.NO_WORK: Outcome.NO_WORK,
    RunnerOutcome.FAILED: Outcome.FAILED,
    RunnerOutcome.TIMEOUT: Outcome.DEADLINE_REACHED,
    RunnerOutcome.PAUSED: Outcome.PAUSED,
    RunnerOutcome.BLOCKED: Outcome.BLOCKED,
}

#: Strings executors already write into their JSONL ledgers, and the older names
#: that appear in reports.  ``coerce`` is case-insensitive and only trusts the exact
#: strings listed here; anything else is *not* silently assumed healthy.
SYNONYMS: dict[str, Outcome] = {
    "PROGRESS": Outcome.PROGRESS,
    "SUCCESS": Outcome.PROGRESS,
    "NO_WORK": Outcome.NO_WORK,
    "NO_WORK_SCHEDULABLE": Outcome.NO_WORK,
    "BLOCKED": Outcome.BLOCKED,
    "FAILED": Outcome.FAILED,
    "ERROR": Outcome.FAILED,
    "UNVERIFIED_ACTIVITY": Outcome.UNVERIFIED_ACTIVITY,
    "UNVERIFIED": Outcome.UNVERIFIED_ACTIVITY,
    "PAUSED": Outcome.PAUSED,
    "PAUSE": Outcome.PAUSED,
    "INTERRUPTED": Outcome.INTERRUPTED,
    "INTERRUPT": Outcome.INTERRUPTED,
    "DEADLINE_REACHED": Outcome.DEADLINE_REACHED,
    "DEADLINE": Outcome.DEADLINE_REACHED,
    "TIMEOUT": Outcome.DEADLINE_REACHED,
}


def canonical(outcome: "Outcome | RunnerOutcome | str | None") -> Outcome | None:
    """Coerce anything the repo writes into the canonical enum, or None.

    None means *not classifiable* — callers must decide (usually FAILED, because an
    unrecorded ending is not evidence of success).  Returning None instead of a
    default here keeps that decision visible at the call site.
    """

    if outcome is None:
        return None
    if isinstance(outcome, Outcome):
        return outcome
    if isinstance(outcome, RunnerOutcome):
        return RUNNER_MAP.get(outcome)
    text = str(outcome).strip()
    if not text:
        return None
    if text in SYNONYMS:
        return SYNONYMS[text]
    upper = text.upper().replace("-", "_").replace(" ", "_")
    return SYNONYMS.get(upper)


def coerce(outcome: "Outcome | RunnerOutcome | str | None") -> Outcome:
    """Like :func:`canonical`, but raises on an ending it cannot classify.

    Callers that must produce an outcome should catch that and record FAILED — an
    unrecorded ending is not evidence of success, but it is also not something this
    module will silently launder into a healthy value.
    """

    value = canonical(outcome)
    if value is None:
        raise ValueError(f"unknown outcome {outcome!r}; classify it explicitly")
    return value


def to_runner(outcome: "Outcome | str") -> RunnerOutcome:
    """Canonical outcome -> the (coarser) legacy runner outcome.

    ``UNVERIFIED_ACTIVITY`` has no legacy equivalent and raises: the whole point of
    the member is that "acted but proved nothing" is not expressible as SUCCESS /
    NO_WORK / FAILED without lying about it.

    ``INTERRUPTED`` maps to the runner's ``PAUSED`` (same "stopped from outside"
    family) — the runner could never tell the two apart because it only saw a reason
    string; the split exists in the canonical enum and in the ledger.
    """

    value = canonical(outcome)
    if value is None:
        raise ValueError(f"unknown outcome {outcome!r}")
    if value in NO_RUNNER_EQUIVALENT:
        raise ValueError(f"{value.value} has no equivalent in the runner vocabulary")
    return _RUNNER_BACK_MAP[value]


_RUNNER_BACK_MAP: dict[Outcome, RunnerOutcome] = {
    Outcome.PROGRESS: RunnerOutcome.SUCCESS,
    Outcome.NO_WORK: RunnerOutcome.NO_WORK,
    Outcome.BLOCKED: RunnerOutcome.BLOCKED,
    Outcome.FAILED: RunnerOutcome.FAILED,
    Outcome.PAUSED: RunnerOutcome.PAUSED,
    Outcome.INTERRUPTED: RunnerOutcome.PAUSED,
    Outcome.DEADLINE_REACHED: RunnerOutcome.TIMEOUT,
}


def from_runner(runner_outcome: RunnerOutcome, *, interrupted: bool = False) -> Outcome:
    """Legacy runner outcome -> canonical.

    ``interrupted=True`` splits the runner's overloaded ``PAUSED``: a stop that came
    from a signal/recovery, not from a human, is ``INTERRUPTED``.
    """

    mapped = RUNNER_MAP.get(runner_outcome)
    if mapped is None:
        raise ValueError(f"unknown runner outcome {runner_outcome!r}")
    if interrupted and mapped is Outcome.PAUSED:
        return Outcome.INTERRUPTED
    return mapped


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def classify_batch(record: Mapping[str, Any] | None) -> Outcome:
    """Classify one executor/supervisor ledger record.

    Accepts the record shapes the repo actually writes tonight:

    * ``data/overnight/wild_cycle.jsonl`` — ``outcome`` plus ``wins``/``xp``/``reason``;
    * ``data/overnight/tower_ledger.jsonl`` — ``outcome`` plus ``interrupted``;
    * a supervisor batch — ``outcome`` (SUCCESS/NO_WORK/FAILED/TIMEOUT/PAUSED/BLOCKED)
      plus ``new_completed_nodes``, ``stats.battles``, ``stats.actions``;
    * a raw observation with no outcome at all (falls back to what was attempted).

    Rules, in order:

    1. an explicit interrupt flag wins over everything else (the record says so);
    2. an explicit ``PAUSED``/``INTERRUPTED`` wins next;
    3. a completion proof — ``new_completed_nodes``, ``proof``, or a ``progress``
       milestone — makes it PROGRESS;
    4. otherwise the recorded ``outcome`` string is used;
    5. with no usable outcome: acted (battles+actions > 0) -> UNVERIFIED_ACTIVITY;
       acted not at all -> NO_WORK; the record itself is missing -> FAILED.

    The last rule is the 2026-11-04 lesson: "no evidence" is never "fine", but a
    batch that admits it acted without proof is more precisely UNVERIFIED_ACTIVITY
    than FAILED.
    """

    if not record:
        return Outcome.FAILED

    if bool(record.get("interrupted")):
        return Outcome.INTERRUPTED

    explicit = canonical(_first(record, "outcome", "verdict", "status"))
    if explicit in HUMAN_STOP_OUTCOMES:
        return explicit

    proofs = record.get("new_completed_nodes") or record.get("completed_nodes") or ()
    if record.get("proof") or record.get("milestones") or proofs:
        return Outcome.PROGRESS
    if explicit is not None:
        return explicit

    stats = record.get("stats") if isinstance(record.get("stats"), Mapping) else {}
    acted = (_as_int(stats.get("battles"))
             + _as_int(stats.get("actions"))
             + _as_int(record.get("battles"))
             + _as_int(record.get("actions"))
             + _as_int(record.get("wins")))
    if record.get("reason") and not acted:
        # "planner refused: nothing schedulable" is NO_WORK even without an
        # explicit outcome field; that refusal is why the 2026-11-04 night spun.
        if "nothing schedulable" in str(record.get("reason", "")).lower():
            return Outcome.NO_WORK
    return Outcome.UNVERIFIED_ACTIVITY if acted else Outcome.NO_WORK


def counts_as_progress(outcome: "Outcome | str") -> bool:
    """True only for outcomes that carry proof of game progress."""

    return coerce(outcome) in PROGRESS_OUTCOMES


def split_last(records: Iterable[Mapping[str, Any]]) -> tuple[Outcome | None, int]:
    """(last outcome, trailing streak length) over a chronological batch list."""

    last: Outcome | None = None
    streak = 0
    for record in records:
        outcome = classify_batch(record)
        if outcome is last:
            streak += 1
        else:
            last, streak = outcome, 1
    return last, streak
