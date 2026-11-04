"""Long-battle watchdog: turn budget, stuck detection, and an auditable snapshot log.

A 谱尼真身 fight is 300–600 rounds (the tutorial video plays it at 2× speed for
~12 minutes).  `game.run_battle` was built for 20-second wild fights: it has a
*time* budget and a stall timer, but no turn budget, no idea whether it is making
progress inside one battle, and no record of what the battle looked like when it
was interrupted.  A run that dies at round 400 of the truebody currently restarts
from "I am somewhere in a battle" with nothing to resume from.

This module adds the missing layer.  It is deliberately pure: it consumes plain
observations and returns a decision, so it can be tested without a game client.
Wiring it into the live loop is a separate, smaller change — and the live loop is
exactly the part that tonight could not be verified, so the two are kept apart.

Design rules:

* **A budget that is a number of turns, not seconds.**  Game speed varies with the
  animation settings, so a 20-minute fight can be 200 or 500 turns.
* **"Stuck" means the fingerprint stopped changing**, not "a long time passed".
  A 600-round fight is normal; 8 rounds with identical scene/HP/log is not.
* **The snapshot is the resume point.**  One JSON line per turn is cheap (600 lines
  is ~60 KB) and turns "crashed mid-battle" into "resume at round N of life M".
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

__all__ = [
    "BattleLimits",
    "BattleSnapshot",
    "BattleVerdict",
    "BattleWatchdog",
    "append_snapshot",
]


@dataclass(frozen=True)
class BattleLimits:
    """Hard stops for one battle pass.  A zero/None field means "no limit"."""

    max_turns: int = 0
    max_seconds: float = 0.0
    #: Consecutive turns with an unchanged fingerprint before we call it stuck.
    #:
    #: **Off by default on purpose.**  The live loop can only refresh ally HP a few
    #: times per battle and never fills `enemy_life`, so the fingerprint is coarse;
    #: a false "stuck" aborts a fight, and an aborted fight with no wins returns
    #: FAIL+pause, which stops the whole night.  Enable it per fight type (truebody),
    #: where the log genuinely does not move when the battle is frozen.
    max_stuck_turns: int = 0
    #: Consecutive fights that ended without a win (0 = no limit).  Off by default:
    #: nothing in the live loop reports a loss yet, so a non-zero default would be a
    #: limit that can never fire.
    max_failures: int = 0


@dataclass(frozen=True)
class BattleSnapshot:
    """One observation of a battle, small enough to log every turn."""

    turn: int
    scene: str
    ally_pet: str | None = None
    ally_hp: tuple[int, int] | None = None
    ally_level: int | None = None
    enemy: str | None = None
    enemy_life: int | None = None
    last_move: str | None = None
    wins: int = 0
    losses: int = 0
    log_sig: str | None = None
    at: float = field(default_factory=time.time)

    @property
    def fingerprint(self) -> tuple[Any, ...]:
        """What must change for this battle to count as progressing.

        HP, scene and the battle-log signature.  `turn` is excluded on purpose:
        the counter always changes, so including it would hide a stuck fight.
        """

        return (self.scene, self.ally_hp, self.enemy_life, self.log_sig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "scene": self.scene,
            "ally_pet": self.ally_pet,
            "ally_hp": list(self.ally_hp) if self.ally_hp else None,
            "ally_level": self.ally_level,
            "enemy": self.enemy,
            "enemy_life": self.enemy_life,
            "last_move": self.last_move,
            "wins": self.wins,
            "losses": self.losses,
            # part of `fingerprint`, so without it the file cannot explain a stop
            "log_sig": self.log_sig,
            "at": round(self.at, 3),
        }


@dataclass(frozen=True)
class BattleVerdict:
    stop: bool
    reason: str = ""

    def __bool__(self) -> bool:  # allows `if verdict:`
        return self.stop


class BattleWatchdog:
    """Accumulates snapshots and decides when a battle pass must end."""

    def __init__(self, limits: BattleLimits | None = None,
                 started: float | None = None) -> None:
        self.limits = limits or BattleLimits()
        self.started = time.time() if started is None else started
        self.turns = 0
        self.failures = 0
        self.snapshots: list[BattleSnapshot] = []
        self._last_fingerprint: tuple[Any, ...] | None = None
        self._stuck = 0
        self._last_progress_at = self.started

    # -- input ------------------------------------------------------------- #
    def note_failure(self, reason: str = "") -> None:
        """Record a fight that ended without a win."""
        self.failures += 1

    def observe(self, snapshot: BattleSnapshot) -> BattleVerdict:
        """Fold one turn in and return whether to keep going."""

        self.turns += 1
        self.snapshots.append(snapshot)

        if snapshot.fingerprint != self._last_fingerprint:
            self._stuck = 0
            self._last_progress_at = snapshot.at
        else:
            self._stuck += 1
        self._last_fingerprint = snapshot.fingerprint

        return self._decide(snapshot.at)

    def _decide(self, now: float) -> BattleVerdict:
        lim = self.limits
        if lim.max_turns and self.turns >= lim.max_turns:
            return BattleVerdict(True, f"turn budget {lim.max_turns} exceeded "
                                      f"({self.turns} turns)")
        if lim.max_seconds and (now - self.started) > lim.max_seconds:
            return BattleVerdict(True, f"battle time budget {lim.max_seconds:.0f}s exceeded")
        if lim.max_failures and self.failures >= lim.max_failures:
            return BattleVerdict(True, f"{self.failures} fights lost without a win")
        if lim.max_stuck_turns and self._stuck >= lim.max_stuck_turns:
            return BattleVerdict(
                True,
                f"no progress for {self._stuck} turns "
                f"(scene/HP/log unchanged since turn {self.turns - self._stuck})")
        return BattleVerdict(False)

    # -- reporting --------------------------------------------------------- #
    @property
    def last(self) -> BattleSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    def summary(self) -> dict[str, Any]:
        last = self.last
        return {
            "turns": self.turns,
            "failures": self.failures,
            "stuck_turns": self._stuck,
            "last_turn": None if last is None else last.turn,
            "last_scene": None if last is None else last.scene,
            "last_ally_hp": None if last is None else last.ally_hp,
            "last_enemy_life": None if last is None else last.enemy_life,
        }

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.snapshots[-n:]]


def append_snapshot(path: str, snapshot: BattleSnapshot, *, extra: Mapping[str, Any] | None = None) -> None:
    """Append one snapshot as a JSON line.  Never raises into the battle loop.

    A logging failure must not stop a fight: the caller is mid-battle and a
    half-written audit trail is much cheaper than an abandoned run.
    """

    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        row = snapshot.to_dict()
        if extra:
            row.update(dict(extra))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 - auditing must never break the battle
        return


def load_snapshots(path: str) -> list[dict[str, Any]]:
    """Read back an audit log (used by resume and by tests)."""

    out: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # A process killed mid-write leaves a partial last line.  The
                    # resume path must not raise on exactly the crash it exists for.
                    continue
    except FileNotFoundError:
        return []
    return out


def last_snapshot(path: str) -> dict[str, Any] | None:
    """The resume point: the most recent snapshot on disk, or None."""

    rows: Iterable[dict[str, Any]] = load_snapshots(path)
    last: dict[str, Any] | None = None
    for row in rows:
        last = row
    return last
