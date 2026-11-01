"""First task: repeatable PVE battle loop skeleton (config-driven).

What is real here
-----------------
* coordinates, turn budget, battle budget, dialog limit and loss policy all
  come from ``seerauto/config/default.json`` (``tasks.battle``) - nothing is
  hardcoded, in particular no ``range(40)`` turn cap (real fights measured
  21 / 42 / 125 / 165 turns in the reference video);
* the per-turn cadence is **change-driven**: after clicking fight+skill the task
  waits for the *battle progress fingerprint* (turn badge + HP bars) to change
  instead of sleeping 1.35 s; a cadence timeout exists only as a fallback for
  when vision cannot tell (documented);
* a turn-counter reset inside the same task run is treated as a *new battle*,
  not as task completion (needed for 真身, which is two consecutive battles).

What is NOT verified
--------------------
* victory/defeat/reward recognition: ``artifacts/`` has no result fixture, so
  those rules are disabled in config and this task therefore treats
  "BATTLE_TURN -> IDLE/BATTLE_RESULT" as a heuristic battle end;
* loss handling (``on_loss``) needs OCR of ``失败``: pure-vision mode cannot see
  it yet, so the policy is a documented hook, not a tested behaviour;
* nothing in this module has been run against the live game.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..actions import Action, ClickAction, StopAction
from ..executor import TickContext
from ..state import State
from ..vision import signature_distance


@dataclass
class BattleStats:
    battles_started: int = 0
    battles_finished: int = 0
    rounds_clicked: int = 0
    engages: int = 0
    dialogs_dismissed: int = 0
    results_dismissed: int = 0
    progress_waits: int = 0
    cadence_fallbacks: int = 0
    losses_seen: int = 0
    stops: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "battles_started": self.battles_started,
            "battles_finished": self.battles_finished,
            "rounds_clicked": self.rounds_clicked,
            "engages": self.engages,
            "dialogs_dismissed": self.dialogs_dismissed,
            "results_dismissed": self.results_dismissed,
            "progress_waits": self.progress_waits,
            "cadence_fallbacks": self.cadence_fallbacks,
            "losses_seen": self.losses_seen,
            "stops": self.stops,
        }


class BattleTask:
    """A minimal but honest PVE loop: engage -> fight/skill -> dismiss -> repeat."""

    name = "battle"

    def __init__(self, task_config: Any, progress_fingerprint: str = "battle_progress"):
        self.cfg = task_config
        self.progress_fp = progress_fingerprint
        thresholds = dict(getattr(task_config, "thresholds", {}) or {})
        self.settle_s = float(thresholds.get("turn_settle_s", 1.2))
        self.progress_distance = float(thresholds.get("turn_progress_distance", 6.0))
        self.engage_cooldown_s = float(thresholds.get("engage_cooldown_s", 0.8))
        self.result_dismiss_limit = int(thresholds.get("result_dismiss_limit", 6))
        self.max_turns = int(getattr(task_config, "max_turns_per_battle", 400))
        self.max_battles = int(getattr(task_config, "max_battles", 20))
        self.on_loss = str(getattr(task_config, "on_loss", "stop"))
        self.counters = BattleStats()
        self._loss_pending = False
        self.reset()

    # -- lifecycle --------------------------------------------------------- #
    def reset(self) -> None:
        self.counters = BattleStats()
        self._in_battle = False
        self._rounds = 0
        self._awaiting_round = False
        self._stored_fp: Any = None
        self._pending_engage = False
        self._processed_transition: tuple[Any, ...] | None = None
        self._result_actions = 0
        self._dialog_actions = 0

    def stats_dict(self) -> dict[str, int]:
        return self.counters.as_dict()

    def stats(self) -> dict[str, Any]:  # protocol name
        return self.counters.as_dict()

    # -- decisions --------------------------------------------------------- #
    def decide(self, ctx: TickContext) -> Action | StopAction | None:
        self._consume_transition(ctx)
        if self._loss_pending:
            self._loss_pending = False
            if self.on_loss == "stop":
                self.counters.stops += 1
                return StopAction(reason="loss detected (on_loss=stop)")
        state = ctx.state

        if state is State.BATTLE_TURN:
            return self._on_battle_turn(ctx)
        if state is State.IDLE:
            return self._on_idle(ctx)
        if state is State.BATTLE_RESULT:
            return self._on_result(ctx)
        if state is State.REWARD:
            return self._on_reward(ctx)
        if state is State.DIALOG:
            return self._on_dialog(ctx)
        # UNKNOWN / ERROR / PAUSED / STOPPED: never click blindly
        return None

    # -- state handlers ---------------------------------------------------- #
    def _on_idle(self, ctx: TickContext) -> Action | StopAction | None:
        if self.counters.battles_started >= self.max_battles:
            self.counters.stops += 1
            return StopAction(reason=f"max_battles reached ({self.max_battles})")
        if self._pending_engage and ctx.since_action_s is not None and ctx.since_action_s < self.engage_cooldown_s:
            return None
        self._pending_engage = True
        self.counters.engages += 1
        return ClickAction(targets=("enemy",), label="engage_enemy")

    def _on_battle_turn(self, ctx: TickContext) -> Action | StopAction | None:
        if self._rounds >= self.max_turns:
            self.counters.stops += 1
            return StopAction(reason=f"turn budget reached ({self.max_turns} rounds)")

        progress = self._progress_state(ctx)
        since = ctx.since_action_s
        if self._awaiting_round:
            if progress is True:
                self._awaiting_round = False
            elif since is not None and since >= self.settle_s:
                # no progress fingerprint available/changed within the cadence
                # window: fall back to acting anyway (counted, so it is visible
                # in task stats rather than hidden in a sleep)
                if progress is None:
                    self.counters.cadence_fallbacks += 1
                self._awaiting_round = False
            else:
                self.counters.progress_waits += 1
                return None

        self._awaiting_round = True
        self._stored_fp = ctx.signals.fingerprints.get(self.progress_fp)
        self._rounds += 1
        self.counters.rounds_clicked += 1
        targets = tuple(self.cfg.skill_sequence)
        return ClickAction(targets=targets, label="battle_round")

    def _on_result(self, ctx: TickContext) -> Action | StopAction | None:
        if self._result_actions >= self.result_dismiss_limit:
            self.counters.stops += 1
            return StopAction(reason="result dialog never dismissed")
        if ctx.since_action_s is not None and ctx.since_action_s < self.settle_s:
            return None
        self._result_actions += 1
        self.counters.results_dismissed += 1
        return ClickAction(targets=("confirm",), label="dismiss_result")

    def _on_reward(self, ctx: TickContext) -> Action | StopAction | None:
        if ctx.since_action_s is not None and ctx.since_action_s < self.settle_s:
            return None
        self.counters.results_dismissed += 1
        return ClickAction(targets=("confirm",), label="dismiss_reward")

    def _on_dialog(self, ctx: TickContext) -> Action | StopAction | None:
        limit = int(getattr(self.cfg, "dialog_dismiss_limit", 5))
        detail = (ctx.detection.detail if ctx.detection else "") or ""
        if self._dialog_actions >= limit:
            self.counters.stops += 1
            return StopAction(reason=f"dialog dismissal limit reached ({limit})")
        if ctx.since_action_s is not None and ctx.since_action_s < max(0.3, self.settle_s * 0.4):
            return None
        target = "dialog_close" if "map" in detail.lower() else "confirm"
        self._dialog_actions += 1
        self.counters.dialogs_dismissed += 1
        return ClickAction(targets=(target,), label=f"dismiss_dialog:{detail or 'generic'}")

    # -- progress / transitions --------------------------------------------- #
    def _progress_state(self, ctx: TickContext) -> bool | None:
        """True=progress seen, False=definitely none, None=no fingerprint."""
        current = ctx.signals.fingerprints.get(self.progress_fp)
        if current is None or self._stored_fp is None:
            return None
        return signature_distance(current, self._stored_fp) >= self.progress_distance

    def _consume_transition(self, ctx: TickContext) -> None:
        transition = ctx.transition
        if transition is None:
            return
        key = (transition.at_ns, transition.from_state, transition.to_state)
        if key == self._processed_transition:
            return
        self._processed_transition = key
        self._result_actions = 0
        self._dialog_actions = 0

        if transition.to_state is State.BATTLE_TURN and not self._in_battle:
            self._in_battle = True
            self._rounds = 0
            self._awaiting_round = False
            self._stored_fp = None
            self._pending_engage = False
            self.counters.battles_started += 1
        elif transition.from_state is State.BATTLE_TURN and transition.to_state is not State.BATTLE_TURN:
            if self._in_battle:
                self._in_battle = False
                self.counters.battles_finished += 1
        if transition.to_state is State.IDLE:
            self._pending_engage = False

        # Loss hook: OCR-only in this build (documented as unverified).
        text = " ".join(item.text for item in ctx.signals.text)
        if text and ("失败" in text or "挑战结束" in text):
            self.counters.losses_seen += 1
            self._loss_pending = self.on_loss == "stop"
