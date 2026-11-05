"""Repeatable resource production: the accounting half of a tower grind.

`puni.prepare.tower_farm` is a one-shot goal node — it completes the first time a
floor is cleared, which is exactly what makes it useless as a *farming* routine:
the node is done, and the account still needs millions of experience.

This module holds the part of a repeated grind that must be right even when nobody
is watching: the ledger and the stop rules.  It is deliberately pure — no session,
no daemon, no clock of its own — so a night of farming can be replayed in a test.

What the ledger answers, and why each one is needed:

    floors_won          did the last round actually clear a floor?  (a PASS that
                        won nothing is noise, not production)
    xp_income           the reward dialogs' own numbers, summed
    spend               anything the round paid out of a currency
    consecutive_fail  the only honest reason to give up

The stop rules encode the 2026-11-04 lesson: a loop that cannot tell "nothing
happened" from "it worked" runs all night and reports health.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


#: Outcome of one farming round, in the same vocabulary the goal runner uses.
ROUND_OK = "PROGRESS"
ROUND_IDLE = "NO_WORK"
ROUND_FAILED = "FAILED"
ROUND_BLOCKED = "BLOCKED"
ROUND_PAUSED = "INTERRUPTED"


@dataclass
class RoundRecord:
    """One ``game.farm_tower`` pass, with what it produced and what it cost."""

    index: int
    started_at: str
    ended_at: str = ""
    floors_won: int = 0
    tower_floor_before: int | None = None
    tower_floor_after: int | None = None
    xp_income: float = 0.0
    xp_events: list[dict[str, Any]] = field(default_factory=list)
    spend: dict[str, float] = field(default_factory=dict)
    currencies_after: dict[str, float] = field(default_factory=dict)
    interrupted: bool = False
    #: The skill refused or was blocked (no input, wrong scene).  Distinct from
    #: "it ran and produced nothing", which is NO_WORK.
    blocked: bool = False
    notes: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def outcome(self) -> str:
        # The classification must never be derived from ``notes``: this tool appends a
        # diagnostic note to every round (the evidence frame path), so "has a note"
        # made every no-win round look like a hard failure and erased the NO_WORK
        # signal that is the whole reason this module exists.
        if self.interrupted:
            return ROUND_PAUSED
        if self.error:
            return ROUND_BLOCKED
        if self.blocked:
            return ROUND_BLOCKED
        if self.floors_won > 0:
            return ROUND_OK
        return ROUND_IDLE

    @property
    def floors_gained(self) -> int | None:
        """Only a real before/after pair says anything."""

        if self.tower_floor_before is None or self.tower_floor_after is None:
            return None
        return self.tower_floor_after - self.tower_floor_before

    def to_dict(self) -> dict[str, Any]:
        d = {
            "index": self.index,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "floors_won": self.floors_won,
            "tower_floor_before": self.tower_floor_before,
            "tower_floor_after": self.tower_floor_after,
            "floors_gained": self.floors_gained,
            "xp_income": self.xp_income,
            "xp_events": self.xp_events,
            "spend": self.spend,
            "currencies_after": self.currencies_after,
            "outcome": self.outcome,
            "interrupted": self.interrupted,
            "blocked": self.blocked,
            "notes": self.notes,
            "error": self.error,
        }
        return d


@dataclass
class Guards:
    """When to stop.  Every field is a budget the caller states up front."""

    max_rounds: int = 0            # 0 = unbounded (the deadline still applies)
    max_minutes: float = 0.0       # 0 = unbounded
    max_no_work: int = 2           # consecutive rounds that produced nothing
    max_failures: int = 3          # consecutive hard failures
    min_xp_per_round: float = 1.0  # below this a round is "no work", not production


@dataclass
class TowerLedger:
    """Cumulative production for one run, plus the streak counters the guards read."""

    guards: Guards = field(default_factory=Guards)
    started_at: datetime | None = None
    rounds: list[RoundRecord] = field(default_factory=list)
    no_work_streak: int = 0
    failure_streak: int = 0
    stopped_because: str = ""

    # -- production totals -------------------------------------------------- #
    @property
    def floors_won(self) -> int:
        return sum(r.floors_won for r in self.rounds)

    @property
    def xp_income(self) -> float:
        return sum(r.xp_income for r in self.rounds)

    @property
    def productive_rounds(self) -> int:
        return sum(1 for r in self.rounds if r.outcome == ROUND_OK)

    def spend_total(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.rounds:
            for name, amount in r.spend.items():
                out[name] = out.get(name, 0.0) + float(amount)
        return out

    # -- bookkeeping -------------------------------------------------------- #
    def add(self, record: RoundRecord) -> None:
        self.rounds.append(record)
        outcome = record.outcome
        if outcome == ROUND_OK and record.xp_income >= self.guards.min_xp_per_round:
            self.no_work_streak = 0
            self.failure_streak = 0
        elif outcome == ROUND_OK:
            # A "win" that paid nothing is still progress, just not production.
            self.no_work_streak = 0
            self.failure_streak = 0
        elif outcome == ROUND_PAUSED:
            # A pause is not evidence of failure, and must never trip a guard.
            return
        elif outcome == ROUND_FAILED:
            # Ran, tried, and produced nothing usable.
            self.failure_streak += 1
            self.no_work_streak += 1
        elif outcome == ROUND_BLOCKED:
            # A refusal (input disabled, wrong scene, client gone) is a configuration
            # fault, not evidence that the tower has nothing to give.  Letting it bump
            # no_work_streak made two refusals look exactly like "we farmed and got
            # nothing", which is the distinction this module exists to keep.
            self.failure_streak += 1
        else:  # NO_WORK
            self.failure_streak = 0
            self.no_work_streak += 1

    # -- the decision ------------------------------------------------------- #
    def should_continue(self, *, now: datetime, deadline: datetime | None) -> tuple[bool, str]:
        """``(keep_going, reason)`` — the reason is recorded even when it is ""."""

        g = self.guards
        if deadline is not None and now >= deadline:
            return False, f"deadline {deadline:%H:%M} reached"
        if g.max_rounds and len(self.rounds) >= g.max_rounds:
            return False, f"round budget {g.max_rounds} reached"
        if g.max_minutes and self.started_at is not None:
            elapsed_min = (now - self.started_at).total_seconds() / 60.0
            if elapsed_min >= g.max_minutes:
                return False, f"time budget {g.max_minutes:.0f} min reached"
        if g.max_no_work and self.no_work_streak >= g.max_no_work:
            return False, (f"{self.no_work_streak} consecutive rounds produced no "
                           "verified floor progress")
        if g.max_failures and self.failure_streak >= g.max_failures:
            return False, f"{self.failure_streak} consecutive rounds failed"
        return True, ""

    def summary(self) -> dict[str, Any]:
        return {
            "rounds": len(self.rounds),
            "productive_rounds": self.productive_rounds,
            "floors_won": self.floors_won,
            "xp_income": self.xp_income,
            "spend": self.spend_total(),
            "no_work_streak": self.no_work_streak,
            "failure_streak": self.failure_streak,
            "stopped_because": self.stopped_because,
            "started_at": None if self.started_at is None else self.started_at.isoformat(
                timespec="seconds"),
            "last_round": None if not self.rounds else self.rounds[-1].to_dict(),
        }


__all__ = [
    "Guards", "RoundRecord", "TowerLedger",
    "ROUND_OK", "ROUND_IDLE", "ROUND_FAILED", "ROUND_BLOCKED", "ROUND_PAUSED",
]
