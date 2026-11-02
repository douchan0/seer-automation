"""Skill contract: the only vocabulary the planner is allowed to speak.

A *skill* is one semantic capability with a declared contract:

* ``inputs`` / ``outputs`` — typed, validated before execution;
* ``preconditions`` / ``postconditions`` — predicates over
  :class:`~seerauto.world.model.GameWorldState`, checked by the scheduler;
* ``max_duration_s`` / ``max_actions`` — a hard budget the skill itself
  enforces, not a suggestion;
* ``requires_input`` / ``requires_approval`` — the safety gate;
* ``verified`` — where the evidence for this capability came from
  (``real`` / ``replay`` / ``unverified``), which is what the delivery report
  reports and what decides whether a goal node may rely on it.

A result is never just ``ok: True``.  Every mutating skill must return the
*observed before/after* state and a ``changed`` flag computed from observations,
so "it clicked twice" can never be mistaken for "it worked".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..world.model import Evidence, GameWorldState


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


class SkillKind(str, Enum):
    OBSERVE = "observe"
    ACT = "act"
    GOAL = "goal"


class SkillError(RuntimeError):
    """Raised for contract violations (bad input, missing precondition)."""


@dataclass(frozen=True)
class SkillSpec:
    id: str
    kind: SkillKind
    summary: str
    #: ``{name: {"type": "int|float|str|bool|list|dict", "required": bool, ...}}``
    inputs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    outputs: Mapping[str, str] = field(default_factory=dict)
    preconditions: tuple[str, ...] = ()
    postconditions: tuple[str, ...] = ()
    max_duration_s: float = 60.0
    max_actions: int = 0
    #: True when the skill can move the mouse / press keys.
    requires_input: bool = False
    #: True when the skill spends currency, consumes items or is irreversible.
    requires_approval: bool = False
    #: ``real`` = verified on the live client, ``replay`` = verified on recorded
    #: frames only, ``unverified`` = never executed against the game.
    verified: str = "unverified"
    #: Where the real-world evidence lives (screenshot, log, doc anchor).
    evidence: tuple[str, ...] = ()

    def validate_inputs(self, kwargs: Mapping[str, Any]) -> None:
        allowed = set(self.inputs)
        unknown = set(kwargs) - allowed
        if unknown:
            raise SkillError(
                f"{self.id}: unknown input(s) {sorted(unknown)}; "
                f"contract accepts {sorted(allowed)}")
        for name, spec in self.inputs.items():
            if spec.get("required", False) and name not in kwargs:
                raise SkillError(f"{self.id}: missing required input {name!r}")
            if name not in kwargs:
                continue
            want = spec.get("type")
            if want and not _type_ok(kwargs[name], str(want)):
                raise SkillError(
                    f"{self.id}: input {name!r} must be {want}, got "
                    f"{type(kwargs[name]).__name__}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "summary": self.summary,
            "inputs": {k: dict(v) for k, v in self.inputs.items()},
            "outputs": dict(self.outputs),
            "preconditions": list(self.preconditions),
            "postconditions": list(self.postconditions),
            "max_duration_s": self.max_duration_s,
            "max_actions": self.max_actions,
            "requires_input": self.requires_input,
            "requires_approval": self.requires_approval,
            "verified": self.verified,
            "evidence": list(self.evidence),
        }


def _type_ok(value: Any, want: str) -> bool:
    table: dict[str, tuple[type, ...]] = {
        "int": (int,),
        "float": (int, float),
        "number": (int, float),
        "str": (str,),
        "bool": (bool,),
        "list": (list, tuple),
        "dict": (dict,),
    }
    types = table.get(want)
    if types is None:
        return True
    if want in ("int", "float", "number") and isinstance(value, bool):
        return False
    return isinstance(value, types)


@dataclass
class SkillResult:
    skill: str
    status: Status
    reason: str = ""
    #: Observed state before / after, when the skill mutates something.
    before: Mapping[str, Any] = field(default_factory=dict)
    after: Mapping[str, Any] = field(default_factory=dict)
    #: Did the *observed* world actually change in the expected direction?
    changed: bool = False
    #: The concrete observable that proves it (level 15 -> 16, 赛尔豆 +100 ...).
    proof: str = ""
    actions: int = 0
    elapsed_s: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)
    evidence: list[Evidence] = field(default_factory=list)
    error: str | None = None
    #: Set when the skill wants the run to stop / replan / ask the user.
    escalation: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is Status.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "status": self.status.value,
            "reason": self.reason,
            "before": _plain(self.before),
            "after": _plain(self.after),
            "changed": self.changed,
            "proof": self.proof,
            "actions": self.actions,
            "elapsed_s": round(self.elapsed_s, 3),
            "data": _plain(self.data),
            "evidence": [e.to_dict() for e in self.evidence],
            "error": self.error,
            "escalation": self.escalation,
        }

    @classmethod
    def pass_(cls, skill: str, *, proof: str = "", changed: bool = True,
              **kw: Any) -> "SkillResult":
        return cls(skill=skill, status=Status.PASS, proof=proof, changed=changed, **kw)

    @classmethod
    def fail(cls, skill: str, reason: str, *, error: str | None = None,
             escalation: str | None = None, **kw: Any) -> "SkillResult":
        return cls(skill=skill, status=Status.FAIL, reason=reason,
                   error=error or reason, escalation=escalation, **kw)

    @classmethod
    def blocked(cls, skill: str, reason: str, *, escalation: str = "pause",
                **kw: Any) -> "SkillResult":
        return cls(skill=skill, status=Status.BLOCKED, reason=reason,
                   escalation=escalation, **kw)


def _plain(value: Any) -> Any:
    from ..world.model import _jsonable  # local import keeps this module light

    return _jsonable(value)


@dataclass
class Budget:
    """Per-call budget handed to a skill."""

    max_duration_s: float = 60.0
    max_actions: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return (time.monotonic() - self.started_at) > self.max_duration_s

    def remaining_s(self) -> float:
        return max(0.0, self.max_duration_s - (time.monotonic() - self.started_at))


@dataclass
class SkillContext:
    """Everything a skill may use.  No skill reaches outside this object."""

    #: Deterministic executor (LiveSession) or None for offline/replay skills.
    session: Any = None
    #: The world state as observed *before* the skill runs.
    world: GameWorldState = field(default_factory=GameWorldState)
    #: Persistence hook (SQLite store in production, in-memory in tests).
    store: Any = None
    run: Any = None
    budget: Budget = field(default_factory=Budget)
    #: False means "never send input" (dry run).
    dry_run: bool = True
    allow_input: bool = False
    #: Maximum spend of any currency in this skill (0 = no spending allowed).
    max_spend: float = 0.0
    #: Free-form knobs from the runner (pause file, output dir...).
    options: dict[str, Any] = field(default_factory=dict)

    def child_budget(self, max_duration_s: float | None = None,
                     max_actions: int | None = None) -> Budget:
        return Budget(
            max_duration_s=float(max_duration_s if max_duration_s is not None
                                 else self.budget.max_duration_s),
            max_actions=int(max_actions if max_actions is not None else self.budget.max_actions),
        )


class Skill:
    """Base class.  Subclasses set :attr:`spec` and implement :meth:`run`."""

    spec: SkillSpec

    def __init__(self) -> None:
        if not getattr(self, "spec", None):
            raise SkillError(f"{type(self).__name__} has no spec")

    @property
    def id(self) -> str:
        return self.spec.id

    def preconditions_ok(self, world: GameWorldState) -> tuple[bool, str]:
        """Default: no precondition DSL, subclasses may override."""

        return True, ""

    def run(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:  # pragma: no cover
        raise NotImplementedError

    def __call__(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:
        self.spec.validate_inputs(kwargs)
        ok, why = self.preconditions_ok(ctx.world)
        if not ok:
            return SkillResult.blocked(self.id, f"precondition failed: {why}",
                                       escalation="replan")
        t0 = time.perf_counter()
        try:
            result = self.run(ctx, **kwargs)
        except Exception as exc:  # noqa: BLE001 - a crash is a FAIL, not a raise
            result = SkillResult.fail(self.id, f"{type(exc).__name__}: {exc}",
                                      escalation="pause")
        if result.elapsed_s == 0.0:
            result.elapsed_s = time.perf_counter() - t0
        return result


class SkillRegistry:
    """Name -> skill, with contract introspection for the planner and tests."""

    def __init__(self, skills: Iterable[Skill] = ()) -> None:
        self._skills: dict[str, Skill] = {}
        for skill in skills:
            self.register(skill)

    def register(self, skill: Skill) -> Skill:
        if skill.id in self._skills:
            raise SkillError(f"duplicate skill id {skill.id!r}")
        self._skills[skill.id] = skill
        return skill

    def get(self, skill_id: str) -> Skill:
        try:
            return self._skills[skill_id]
        except KeyError as exc:
            raise SkillError(
                f"unknown skill {skill_id!r}; known: {sorted(self._skills)}") from exc

    def __contains__(self, skill_id: object) -> bool:
        return skill_id in self._skills

    def ids(self, kind: SkillKind | None = None) -> list[str]:
        return sorted(k for k, s in self._skills.items()
                      if kind is None or s.spec.kind is kind)

    def specs(self) -> list[dict[str, Any]]:
        return [self._skills[k].spec.to_dict() for k in self.ids()]

    def catalog(self) -> dict[str, dict[str, Any]]:
        return {k: self._skills[k].spec.to_dict() for k in self.ids()}

    def call(self, ctx: SkillContext, skill_id: str, **kwargs: Any) -> SkillResult:
        return self.get(skill_id)(ctx, **kwargs)


def skill(spec: SkillSpec) -> Callable[[type], type]:
    """Class decorator: attach a :class:`SkillSpec` and register nothing."""

    def wrap(cls: type) -> type:
        cls.spec = spec  # type: ignore[attr-defined]
        return cls

    return wrap


__all__ = [
    "Status",
    "SkillKind",
    "SkillSpec",
    "SkillResult",
    "SkillContext",
    "Skill",
    "SkillError",
    "SkillRegistry",
    "Budget",
    "skill",
]
