"""Mode-switch policy: auditable, and impossible to oscillate.

A mode switch is the one control-plane action that costs money and changes what the
night can do, so every request produces a record in ``mode_switches.jsonl`` — even
when the switch is *refused*.  The record carries: ``ts``, ``from``, ``to``, the
reason enum, an evidence pointer, the cooldown state that was consulted, and
``applied``/``suppressed``.  "We considered switching and did not" is exactly the
kind of decision a post-mortem needs and a log usually loses.

Two independent brakes stop oscillation, because either one alone is insufficient:

* **reason cooldown** (default 900 s) — the same reason cannot drive two switches
  back-to-back.  This stops a repeated detector from toggling the mode.
* **minimum dwell** (default 1800 s) — *any* second switch is refused this soon after
  the last applied one, whatever its reason.  Without this, an honest
  ``NEED_RESEARCH`` escalation followed by an honest ``RESOLVED`` return could
  alternate every few minutes with two different, individually-valid reasons.  That
  is flapping with good manners, and it is still flapping.

``resolved`` suppression categories are explicit strings, so a reader never has to
infer why a record has ``applied: false``.

``SwitchReason`` is defined in :mod:`seerauto.dual.modes` (the rules there produce
one member each) and re-exported here; ``switch`` imports ``modes`` and never the
other way round.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from pathlib import Path

from .ledger import Ledger, parse_ts
from .modes import Mode, Policy, SwitchReason, resolve  # noqa: F401 - resolve in __all__

__all__ = [
    "SwitchReason",
    "SwitchDecision",
    "ModeSwitchService",
    "decide",
    "mode_switches",
    "SUPPRESS_ALREADY_IN_MODE",
    "SUPPRESS_REASON_COOLDOWN",
    "SUPPRESS_MIN_DWELL",
]

SUPPRESS_ALREADY_IN_MODE = "already_in_mode"
SUPPRESS_REASON_COOLDOWN = "reason_cooldown"
SUPPRESS_MIN_DWELL = "min_dwell"

DEFAULT_REASON_COOLDOWN_S = 900.0
DEFAULT_MIN_DWELL_S = 1800.0


@dataclass(frozen=True)
class SwitchDecision:
    """The decision before it is written down."""

    applied: bool
    to: Mode
    reason: SwitchReason
    from_mode: Mode | None = None
    suppressed_by: str | None = None
    evidence: str = ""
    cooldown: Mapping[str, Any] = field(default_factory=dict)
    note: str | None = None

    @property
    def suppressed(self) -> bool:
        return not self.applied

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "suppressed": self.suppressed,
            "suppressed_by": self.suppressed_by,
            "from": self.from_mode.value if self.from_mode else None,
            "to": self.to.value,
            "reason": self.reason.value,
            "evidence": self.evidence,
            "cooldown": dict(self.cooldown),
            "note": self.note,
        }


def _cooldown_state(*, now: float, last_same_reason_epoch: float | None,
                    last_switch_epoch: float | None, reason_cooldown_s: float,
                    min_dwell_s: float) -> dict[str, Any]:
    since_same = (now - last_same_reason_epoch
                  if last_same_reason_epoch is not None else None)
    since_switch = (now - last_switch_epoch
                    if last_switch_epoch is not None else None)
    return {
        "reason_cooldown_s": reason_cooldown_s,
        "min_dwell_s": min_dwell_s,
        "last_same_reason_epoch": (round(last_same_reason_epoch, 3)
                                   if last_same_reason_epoch is not None else None),
        "since_same_reason_s": round(since_same, 3) if since_same is not None else None,
        "reason_remaining_s": (round(reason_cooldown_s - since_same, 3)
                               if since_same is not None else None),
        "last_switch_epoch": (round(last_switch_epoch, 3)
                              if last_switch_epoch is not None else None),
        "since_last_switch_s": (round(since_switch, 3)
                                if since_switch is not None else None),
        "dwell_remaining_s": (round(min_dwell_s - since_switch, 3)
                              if since_switch is not None else None),
    }


def decide(*, current: Mode | str | None, to: Mode | str, reason: SwitchReason | str,
           evidence: str, now: float, last_same_reason_epoch: float | None,
           last_switch_epoch: float | None,
           reason_cooldown_s: float = DEFAULT_REASON_COOLDOWN_S,
           min_dwell_s: float = DEFAULT_MIN_DWELL_S,
           note: str | None = None) -> SwitchDecision:
    """Pure switch decision; no I/O.  Order of checks is the policy.

    1. no change requested -> suppressed ``already_in_mode``;
    2. same reason used within ``reason_cooldown_s`` -> suppressed ``reason_cooldown``;
    3. any switch within ``min_dwell_s`` -> suppressed ``min_dwell``;
    4. otherwise applied.
    """

    target = Mode.parse(to)
    current_mode = Mode.parse(current) if current is not None else None
    reason_value = SwitchReason.parse(reason)
    cooldown = _cooldown_state(
        now=now, last_same_reason_epoch=last_same_reason_epoch,
        last_switch_epoch=last_switch_epoch, reason_cooldown_s=reason_cooldown_s,
        min_dwell_s=min_dwell_s)

    if current_mode is target:
        return SwitchDecision(False, target, reason_value, current_mode,
                              SUPPRESS_ALREADY_IN_MODE, evidence, cooldown, note)

    since_same = cooldown.get("since_same_reason_s")
    if since_same is not None and since_same < reason_cooldown_s:
        return SwitchDecision(False, target, reason_value, current_mode,
                              SUPPRESS_REASON_COOLDOWN, evidence, cooldown, note)

    since_switch = cooldown.get("since_last_switch_s")
    if since_switch is not None and since_switch < min_dwell_s:
        return SwitchDecision(False, target, reason_value, current_mode,
                              SUPPRESS_MIN_DWELL, evidence, cooldown, note)

    return SwitchDecision(True, target, reason_value, current_mode, None, evidence,
                          cooldown, note)


def mode_switches(ledger: Ledger | None = None) -> list[dict[str, Any]]:
    """The audit trail, oldest first (convenience wrapper for readers)."""

    return (ledger or Ledger()).read("mode_switches")


class ModeSwitchService:
    """Applies (or refuses) mode switches and writes the audit record either way."""

    def __init__(self, ledger: Ledger | None = None, *,
                 checkpoint_path: str | None = None,
                 reason_cooldown_s: float = DEFAULT_REASON_COOLDOWN_S,
                 min_dwell_s: float = DEFAULT_MIN_DWELL_S,
                 clock=time.time) -> None:
        self.ledger = ledger or Ledger()
        self.checkpoint_path = checkpoint_path
        self.reason_cooldown_s = float(reason_cooldown_s)
        self.min_dwell_s = float(min_dwell_s)
        self.clock = clock

    # -- reading the audit trail ------------------------------------------- #
    def records(self) -> list[dict[str, Any]]:
        return self.ledger.read("mode_switches")

    def _applied(self) -> list[dict[str, Any]]:
        return [record for record in self.records() if record.get("applied") is True]

    def last_applied(self) -> dict[str, Any] | None:
        applied = self._applied()
        return applied[-1] if applied else None

    def last_applied_for(self, reason: SwitchReason | str) -> dict[str, Any] | None:
        value = SwitchReason.parse(reason).value
        matches = [record for record in self._applied()
                   if record.get("reason") == value]
        return matches[-1] if matches else None

    def current(self) -> tuple[Mode | None, Policy | None]:
        checkpoint = self.ledger.load_checkpoint(path=self.checkpoint_path, default={}) or {}
        mode = Mode.parse(checkpoint["mode"]) if checkpoint.get("mode") else None
        policy = Policy.parse(checkpoint["policy"]) if checkpoint.get("policy") else None
        return mode, policy

    # -- the one action ---------------------------------------------------- #
    def request(self, to: Mode | str, reason: SwitchReason | str, evidence: str, *,
                note: str | None = None, now: float | None = None,
                policy: Policy | str | None = None) -> dict[str, Any]:
        """Request a switch; append the audit record; update the checkpoint if applied.

        ``evidence`` must be a non-empty pointer (a ledger path, frame path, or an
        explicit ``NOT FOUND``) — an unexplained switch is not auditable, so it is
        rejected before anything is written.
        """

        target = Mode.parse(to)
        reason_value = SwitchReason.parse(reason)
        evidence_text = str(evidence or "").strip()
        if not evidence_text:
            raise ValueError("a switch needs an evidence pointer "
                             "(path, ledger record or an explicit 'NOT FOUND')")
        now = float(now if now is not None else self.clock())
        checkpoint_target = (Path(self.checkpoint_path) if self.checkpoint_path
                             else self.ledger.checkpoint_path)

        # One checkpoint lock covers read-modify-write of both the mode and the last
        # switch timestamp.  Without it the watchdog's periodic state save can land
        # between our load and our save and silently restore the old mode (acceptance
        # finding A19: the audit trail said MAX_RESULTS while the checkpoint stayed
        # EFFICIENT).
        with self.ledger.checkpoint_lock(path=checkpoint_target):
            checkpoint = self.ledger.load_checkpoint(path=self.checkpoint_path,
                                                     default={}) or {}
            current_mode = (Mode.parse(checkpoint["mode"])
                            if checkpoint.get("mode") else None)
            current_policy = (Policy.parse(checkpoint["policy"])
                              if checkpoint.get("policy") else None)
            if policy is not None:
                current_policy = Policy.parse(policy)
            last_same = self.last_applied_for(reason_value)
            last_any = self.last_applied()
            last_same_epoch = parse_ts((last_same or {}).get("ts_epoch")) or parse_ts(
                (last_same or {}).get("ts"))
            last_any_epoch = parse_ts((last_any or {}).get("ts_epoch")) or parse_ts(
                (last_any or {}).get("ts"))

            decision = decide(current=current_mode, to=target, reason=reason_value,
                              evidence=evidence_text, now=now,
                              last_same_reason_epoch=last_same_epoch,
                              last_switch_epoch=last_any_epoch,
                              reason_cooldown_s=self.reason_cooldown_s,
                              min_dwell_s=self.min_dwell_s, note=note)

            record = {
                "kind": "mode.switch",
                **decision.to_dict(),
                "policy": current_policy.value if current_policy else None,
                "actor": "overnight_control",
                "effective_epoch": round(now, 3),
            }
            # The record is stamped with the *decision* epoch (not the append wall
            # clock) so the audit trail's `ts` and `effective_epoch` agree and the
            # cooldown maths across records is exact.  In production `now` is the
            # clock anyway.
            written = self.ledger.append("mode_switches", record, ts=now)

            if decision.applied:
                checkpoint["mode"] = target.value
                checkpoint["mode_since_epoch"] = round(now, 3)
                checkpoint["mode_since_ts"] = written.get("ts")
                if current_policy is not None:
                    checkpoint["policy"] = current_policy.value
                checkpoint["last_switch_ts"] = written.get("ts")
                checkpoint["last_switch_epoch"] = round(now, 3)
                self.ledger._save_checkpoint_unlocked(checkpoint, checkpoint_target)
                self.ledger.append("events", {
                    "kind": "mode.switch",
                    "from": decision.from_mode.value if decision.from_mode else None,
                    "to": target.value,
                    "reason": reason_value.value,
                    "evidence": evidence_text,
                }, ts=now)
            else:
                self.ledger.append("events", {
                    "kind": "mode.switch.suppressed",
                    "from": decision.from_mode.value if decision.from_mode else None,
                    "to": target.value,
                    "reason": reason_value.value,
                    "suppressed_by": decision.suppressed_by,
                    "evidence": evidence_text,
                }, ts=now)
        return written

    # -- convenience ------------------------------------------------------- #
    def request_from_signals(self, signals, *, evidence: str, **kwargs: Any) -> dict[str, Any]:
        """Resolve the effective mode from signals and request that switch.

        Used by the agent layer when it wants the adaptive rules to drive the mode
        rather than an operator decision.  The reason comes from the rule that
        matched, so the audit record is never reasonless.
        """

        current_mode, current_policy = self.current()
        policy = kwargs.pop("policy", None) or current_policy or Policy.ADAPTIVE
        resolution = resolve(policy, signals, current=current_mode,
                             **{k: v for k, v in kwargs.items()
                                if k in ("deadline_window_s", "stall_minutes",
                                         "no_work_threshold", "unverified_threshold",
                                         "vision_failure_threshold",
                                         "recovery_failure_threshold",
                                         "static_mode")})
        if resolution.reason is None:
            raise ValueError("the policy produced no switch reason; a STATIC policy "
                             "cannot drive an automatic switch")
        note = f"rule={resolution.rule}: {'; '.join(resolution.because)}"
        written = self.request(resolution.mode, resolution.reason,
                               evidence=evidence, note=note)
        written["resolution"] = resolution.to_dict()
        return written
