"""Derive the night's state from the ledgers — the only reader the watchdog trusts.

The watchdog ticks twice a second, so "how long since verified progress?" cannot be
answered by trusting a process's memory: the question is about a *file* that survives
crashes.  :func:`derive` rebuilds the answer from the append-only ledgers plus the
checkpoint, and :class:`DerivedCache` makes that cheap by recomputing only when a
ledger actually grew.

Record conventions (the writers live in this package and in the executors):

``events.jsonl`` — the general timeline, one record per thing that happened::

    {"kind": "session.init",   "mode": "EFFICIENT", "policy": "ADAPTIVE"}
    {"kind": "batch.end",      "outcome": "NO_WORK", "battles": 0, "actions": 0,
                               "reason": "planner refused: nothing schedulable"}
    {"kind": "mode.switch",    "from": "EFFICIENT", "to": "MAX_RESULTS", ...}
    {"kind": "wake",           "wake": "STALL", "evidence": "..."}
    {"kind": "checkpoint"}

``failures.jsonl`` — one record per incident a human may need to look at::

    {"kind": "batch", "outcome": "BLOCKED", "reason": "no usable pet",
     "recovery": "heal_pet", "evidence": "data/overnight/frames/x.png"}

``milestones.jsonl`` — **verified progress only**, i.e. something with a proof
string.  A status field without proof is not evidence (AGENTS.md §5.3)::

    {"kind": "progress", "node": "puni.prepare.wild_45", "proof": "level 41",
     "evidence": "data/overnight/frames/wild_027.png"}

``runtime_health.jsonl`` / ``mode_switches.jsonl`` / ``world_snapshots.jsonl`` are
written by the watchdog / switch service / observers and are read elsewhere.

``model_usage.jsonl`` is owned by ``tools/usage_meter.py`` (``kind`` =
``"usage_snapshot"``); this module never parses its records, only its tail timestamp
is used as evidence that the agent layer was active.

The distinction this module must keep: *acting* is not *progress*.  ``batch.end``
records update ``last_outcome`` and the streaks; only ``milestones`` (or a
``progress`` event carrying a proof) move ``last_verified_progress_ts``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .ledger import Ledger, parse_ts
from .outcome import Outcome, canonical, classify_batch

__all__ = [
    "NightState",
    "derive",
    "DerivedCache",
    "PROGRESS_KINDS",
    "BATCH_KINDS",
    "FAILURE_KINDS",
]

#: Record kinds that are — by definition — proof of game progress.
PROGRESS_KINDS: frozenset[str] = frozenset({"progress", "milestone", "node.completed",
                                            "goal.progress"})

#: Record kinds that close out one batch/pass and therefore carry an outcome.
BATCH_KINDS: frozenset[str] = frozenset({"batch.end", "batch", "pass.end", "run.end",
                                         "outcome"})

#: Incident kinds; a BLOCKED incident clears only on later progress.
FAILURE_KINDS: frozenset[str] = frozenset({"batch", "failure", "incident", "blocked",
                                           "error"})


@dataclass(frozen=True)
class NightState:
    """Everything the watchdog needs to know about the night, as of a read.

    ``last_verified_progress_ts=None`` means "never recorded", which is different
    from "recorded a long time ago" and must not be treated as a stall (there is no
    baseline to be stalled against).
    """

    last_verified_progress_ts: float | None = None
    last_verified_progress: dict[str, Any] | None = None
    last_outcome: Outcome | None = None
    consecutive_no_work: int = 0
    consecutive_unverified: int = 0
    consecutive_failures: int = 0
    blocked: bool = False
    blocked_reason: str | None = None
    batch_count: int = 0
    milestone_seq: int = 0
    last_milestone: dict[str, Any] | None = None
    last_event_ts: float | None = None
    last_event: dict[str, Any] | None = None
    emitted_epoch: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def since_progress_s(self) -> float | None:
        """Seconds since verified progress, or None when there was never any."""

        if self.last_verified_progress_ts is None:
            return None
        return max(0.0, self.emitted_epoch - self.last_verified_progress_ts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "last_verified_progress_ts": self.last_verified_progress_ts,
            "last_verified_progress": self.last_verified_progress,
            "last_outcome": self.last_outcome.value if self.last_outcome else None,
            "consecutive_no_work": self.consecutive_no_work,
            "consecutive_unverified": self.consecutive_unverified,
            "consecutive_failures": self.consecutive_failures,
            "blocked": self.blocked,
            "blocked_reason": self.blocked_reason,
            "batch_count": self.batch_count,
            "milestone_seq": self.milestone_seq,
            "last_milestone": self.last_milestone,
            "last_event_ts": self.last_event_ts,
            "last_event": self.last_event,
            "since_progress_s": (round(self.since_progress_s, 3)
                                 if self.since_progress_s is not None else None),
            "notes": list(self.notes),
        }


def _record_ts(record: Mapping[str, Any]) -> float | None:
    return parse_ts(record.get("ts_epoch")) or parse_ts(record.get("ts"))


def _kind(record: Mapping[str, Any]) -> str:
    # ``kind`` is the convention; ``type`` is tolerated because an operator script
    # wrote a foreign timeline record into events.jsonl on 2026-11-06 (WAKE_TEST).
    # A record with an unknown kind is displayed but never counted as progress or
    # as a batch outcome, so a foreign record cannot silently move the night's state.
    return str(record.get("kind") or record.get("type") or "").strip().lower()


def _outcome_of(record: Mapping[str, Any], source: str) -> Outcome:
    """Explicit outcome first; otherwise the failures ledger's kind is the outcome."""

    explicit = canonical(record.get("outcome"))
    if explicit is not None:
        return explicit
    if source == "failures":
        by_kind = {"blocked": Outcome.BLOCKED, "error": Outcome.FAILED,
                   "failure": Outcome.FAILED}.get(_kind(record))
        if by_kind is not None:
            return by_kind
    return classify_batch(record)


def _progress_record(record: Mapping[str, Any]) -> bool:
    """A record counts as progress only when it carries proof, not just a status."""

    kind = _kind(record)
    if kind in PROGRESS_KINDS and record.get("proof"):
        return True
    if kind in PROGRESS_KINDS and kind != "progress":
        # milestone / node.completed are only ever written with a proof; accept a
        # record that at least names the node.
        return bool(record.get("node") or record.get("milestone"))
    return False


def derive(ledger: Ledger | None = None, *, checkpoint: Mapping[str, Any] | None = None,
           now: float | None = None) -> NightState:
    """Rebuild :class:`NightState` from ``events``/``failures``/``milestones``.

    Reads the ledgers in write order (they are append-only, so file order is time
    order) and folds one pass over the combined timeline.  ``checkpoint`` seeds
    ``last_verified_progress_ts`` so a ledger rotation does not erase progress.
    """

    ledger = ledger or Ledger()
    checkpoint = checkpoint or {}
    emitted = float(now if now is not None else 0.0) or 0.0

    last_progress_ts = parse_ts(checkpoint.get("last_verified_progress_ts"))
    last_progress = (checkpoint.get("last_verified_progress")
                     if isinstance(checkpoint.get("last_verified_progress"), Mapping)
                     else None)
    milestone_seq = 0
    last_milestone: dict[str, Any] | None = None
    last_outcome: Outcome | None = None
    no_work = unverified = failures = 0
    batch_count = 0
    blocked = False
    blocked_reason: str | None = None
    last_event_ts: float | None = None
    last_event: dict[str, Any] | None = None

    def fold(record: dict[str, Any], *, source: str) -> None:
        nonlocal last_progress_ts, last_progress, milestone_seq, last_milestone
        nonlocal last_outcome, no_work, unverified, failures, batch_count
        nonlocal blocked, blocked_reason, last_event_ts, last_event

        ts = _record_ts(record)
        if ts is not None and (last_event_ts is None or ts >= last_event_ts):
            last_event_ts = ts
            last_event = {"source": source, "record": record}

        if _progress_record(record):
            milestone_seq += 1
            last_milestone = record
            if ts is not None and (last_progress_ts is None or ts >= last_progress_ts):
                last_progress_ts = ts
                last_progress = record
            blocked = False
            blocked_reason = None
            return

        kind = _kind(record)
        if kind in BATCH_KINDS or (source == "failures" and kind in FAILURE_KINDS):
            outcome = _outcome_of(record, source)
            batch_count += 1
            last_outcome = outcome
            if outcome is Outcome.NO_WORK:
                no_work += 1
            else:
                no_work = 0
            if outcome is Outcome.UNVERIFIED_ACTIVITY:
                unverified += 1
            else:
                unverified = 0
            if outcome in (Outcome.FAILED, Outcome.BLOCKED):
                failures += 1
            else:
                failures = 0
            if outcome is Outcome.BLOCKED:
                blocked = True
                blocked_reason = str(record.get("reason") or record.get("error") or
                                     "blocked")
            return

        if kind in ("batch.progress",):
            milestone_seq += 1
            last_milestone = record

    for record in ledger.read("events"):
        fold(record, source="events")
    for record in ledger.read("failures"):
        fold(record, source="failures")
    # milestones ledger is folded last so its timestamps win ties with events; both
    # are written in the same second, and "proof exists" must not be lost.
    for record in ledger.read("milestones"):
        fold(record, source="milestones")

    return NightState(
        last_verified_progress_ts=last_progress_ts,
        last_verified_progress=dict(last_progress) if last_progress else None,
        last_outcome=last_outcome,
        consecutive_no_work=no_work,
        consecutive_unverified=unverified,
        consecutive_failures=failures,
        blocked=blocked,
        blocked_reason=blocked_reason,
        batch_count=batch_count,
        milestone_seq=milestone_seq,
        last_milestone=last_milestone,
        last_event_ts=last_event_ts,
        last_event=last_event,
        emitted_epoch=emitted,
    )


class DerivedCache:
    """Recompute :func:`derive` only when a ledger's size/mtime changed.

    The watchdog runs at 0.5 s; re-parsing every ledger twice a second would be pure
    waste, and *not* re-parsing is what makes a long night affordable.
    """

    def __init__(self, ledger: Ledger | None = None) -> None:
        self.ledger = ledger or Ledger()
        self._signature: tuple[tuple[str, int, float], ...] | None = None
        self._state: NightState | None = None
        self.reads = 0
        self.recomputes = 0

    def get(self, *, checkpoint: Mapping[str, Any] | None = None,
            now: float | None = None) -> NightState:
        signature = self.ledger.signature()
        if self._state is None or signature != self._signature:
            self._signature = signature
            self._state = derive(self.ledger, checkpoint=checkpoint, now=now)
            self.recomputes += 1
        self.reads += 1
        return self._state

    def invalidate(self) -> None:
        self._signature = None
        self._state = None
