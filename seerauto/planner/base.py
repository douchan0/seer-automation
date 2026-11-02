"""Planner contract: the *only* place where a model may pick the next goal node.

Layer position (see ``docs/NEXT_ACTIONS.md`` and ``AGENTS.md`` section 7)::

    User Goal -> AI Planner / Supervisor -> Goal Graph + World State -> Scheduler
              -> Verified Semantic Skills -> Deterministic Vision Executor -> Game

The planner is deliberately dumb in the horizontal direction and smart in the
vertical one:

* it may choose **one** goal node id and **one** registered capability id;
* it may explain a blocker, refuse to continue (``stop``) or ask a human
  (``need_human``);
* it must never emit coordinates, key presses, OCR thresholds or per-frame
  decisions - those belong to the deterministic executor.

Everything in this module is plain data plus validation, so it can be imported
by the runner, the bridge (a human/agent answering files) and the tests without
pulling in the capture layer, the goal graph or any network client.

Wire contract (one JSON object, no extra top-level keys)::

    {
      "goal": "puni",
      "selected_node": "puni.prepare.level_up",
      "capability": "game.run_battle",
      "reason": "当前精灵等级未达到目标",
      "expected_observation": "目标精灵等级或经验增加",
      "max_actions": 40,
      "max_duration_s": 1800,
      "requires_approval": false
    }

Optional refusal keys (a refusal schedules no work)::

    {"stop": true, "stop_reason": "..."}
    {"need_human": true, "human_question": "..."}     # implies stop
"""

from __future__ import annotations

import enum
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - type checking only, never imported at runtime
    from ..world.model import GameWorldState


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class PlannerError(RuntimeError):
    """Any planner failure the runner must degrade from (never silently ignore)."""


class PlannerUnavailable(PlannerError):
    """The planner cannot even attempt a decision (no key, no dir, call cap)."""


class PlanError(PlannerError):
    """The decision violates the contract (schema, unknown ids, budgets)."""


# --------------------------------------------------------------------------- #
# contract constants
# --------------------------------------------------------------------------- #
MAX_ACTIONS_MIN = 1
MAX_ACTIONS_MAX = 200
MAX_DURATION_MIN = 1
MAX_DURATION_MAX = 7200

#: Keys every decision must carry, in contract order.
REQUIRED_KEYS: tuple[str, ...] = (
    "goal",
    "selected_node",
    "capability",
    "reason",
    "expected_observation",
    "max_actions",
    "max_duration_s",
    "requires_approval",
)
#: Optional refusal keys.
OPTIONAL_KEYS: tuple[str, ...] = (
    "stop",
    "stop_reason",
    "need_human",
    "human_question",
)
ALLOWED_KEYS: frozenset[str] = frozenset(REQUIRED_KEYS + OPTIONAL_KEYS)

#: Free-text keys that must be non-empty (an empty reason is not a decision).
_NON_EMPTY_TEXT: tuple[str, ...] = ("goal", "reason", "expected_observation")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _jsonable(value: Any) -> Any:
    """Best-effort JSON conversion; the planner never imports the world layer."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, enum.Enum):
        return _jsonable(value.value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict())
        except Exception:  # noqa: BLE001 - a repr is still better than a crash
            pass
    return repr(value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _ids(items: Iterable[Any] | None) -> list[str]:
    """Accept ``["a"]``, ``[{"id": "a"}]`` or a mapping keyed by id."""

    if items is None:
        return []
    if isinstance(items, Mapping):
        return [str(k) for k in items]
    out: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            node_id = item.get("id") or item.get("node") or item.get("capability")
            if node_id is not None:
                out.append(str(node_id))
        else:
            out.append(str(item))
    return out


def _budget_cap(budgets: Mapping[str, Any],
                keys: tuple[tuple[str, float], ...]) -> tuple[float | None, str]:
    """First present run-budget cap, converted to contract units.

    Returns ``(cap, label)`` where ``label`` names the key the cap came from, so
    a violation message can point at ``max_battles`` instead of inventing a
    ``max_actions`` that the caller never set.
    """

    for key, scale in keys:
        value = budgets.get(key)
        if value is None:
            continue
        if not _is_number(value):
            raise PlanError(
                f"budgets.{key} must be a number, got {type(value).__name__}")
        label = key if scale == 1.0 else f"{key}={_clean_number(value)}"
        return float(value) * scale, label
    return None, ""


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if limit > 0 and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "..."
    return text


def _snippet(text: str, limit: int = 240) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[:limit] + "..."


# --------------------------------------------------------------------------- #
# request
# --------------------------------------------------------------------------- #
@dataclass
class PlanRequest:
    """Everything the planner is allowed to know.  No images, no coordinates.

    ``graph_digest`` is produced by the runner from the goal graph.  One entry
    per node, with this shape (aliases in brackets are accepted, so a digest can
    be ``GoalNode.to_dict() | {"scheduler_status": graph.status_of(...)}``
    without a translation layer):

    ``{"id": "puni.prepare.level_up", "title": "...", "status": "ready"
       [scheduler_status], "priority": 80, "capability": "game.run_battle" [skill],
       "executable": true,
       "budget": {"max_actions": 40, "max_duration_s": 1800}
                 [max_battles / max_minutes / max_seconds],
       "blocked_reason": "..."}``

    ``status`` is the *scheduler* status, i.e. the goal graph's ``status_of``:
    ``ready`` / ``missing_prereq`` / ``missing_capability`` / ``not_executable`` /
    ``blocked`` / ``unknown`` / ``completed``.  Only ``ready`` nodes may be
    selected.  When both ``status`` and ``scheduler_status`` are present the
    latter wins (the goal file's own ``status`` is a static "todo"/"partial"
    label, never a schedulability answer).
    """

    #: The user-facing goal this run is chasing ("puni").  Required.
    goal: str = ""
    #: ``GameWorldState.summary()`` - facts, sources and UNKNOWN markers.
    world_summary: dict[str, Any] = field(default_factory=dict)
    #: Compact view of every goal node (see class docstring).
    graph_digest: list[dict[str, Any]] = field(default_factory=list)
    #: ``SkillSpec.to_dict()`` for every registered capability.
    capabilities: list[dict[str, Any]] = field(default_factory=list)
    #: Node ids the graph already reports as done (evidence-backed).
    completed_nodes: list[str] = field(default_factory=list)
    #: Structured tail of the event log (newest last).
    recent_events: list[dict[str, Any]] = field(default_factory=list)
    #: ``SkillResult.to_dict()`` of the previous action, if any.
    last_result: dict[str, Any] | None = None
    #: Run-level caps.  Either contract terms (``max_actions`` /
    #: ``max_duration_s``) or the runner's terms (``max_battles`` /
    #: ``max_minutes``); both are understood.
    budgets: dict[str, Any] = field(default_factory=dict)
    #: Hard policy switches, e.g. ``allow_input`` / ``forbid_approval`` / ``max_spend``.
    constraints: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None

    # -- views ------------------------------------------------------------- #
    @property
    def node_ids(self) -> list[str]:
        return [str(n.get("id")) for n in self.graph_digest or [] if isinstance(n, Mapping)
                and n.get("id") is not None]

    @property
    def capability_ids(self) -> list[str]:
        return [str(c.get("id")) for c in self.capabilities or [] if isinstance(c, Mapping)
                and c.get("id") is not None]

    def node(self, node_id: str) -> dict[str, Any] | None:
        for node in self.graph_digest or []:
            if isinstance(node, Mapping) and str(node.get("id")) == node_id:
                return dict(node)
        return None

    def capability(self, cap_id: str) -> dict[str, Any] | None:
        for spec in self.capabilities or []:
            if isinstance(spec, Mapping) and str(spec.get("id")) == cap_id:
                return dict(spec)
        return None

    def budget(self, key: str, default: float | int | None = None) -> float | int | None:
        value = (self.budgets or {}).get(key)
        return default if value is None else value

    def constraint(self, key: str, default: Any = None) -> Any:
        return (self.constraints or {}).get(key, default)

    # -- serialisation ----------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "world_summary": _jsonable(self.world_summary),
            "graph_digest": _jsonable(self.graph_digest),
            "capabilities": _jsonable(self.capabilities),
            "completed_nodes": _jsonable(self.completed_nodes),
            "recent_events": _jsonable(self.recent_events),
            "last_result": None if self.last_result is None else _jsonable(self.last_result),
            "budgets": _jsonable(self.budgets),
            "constraints": _jsonable(self.constraints),
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PlanRequest":
        """Tolerant reader (unknown keys ignored) so old request files still load."""

        return cls(
            goal=str(raw.get("goal") or ""),
            world_summary=dict(raw.get("world_summary") or {}),
            graph_digest=[dict(n) for n in raw.get("graph_digest") or () if isinstance(n, Mapping)],
            capabilities=[dict(c) for c in raw.get("capabilities") or () if isinstance(c, Mapping)],
            completed_nodes=[str(n) for n in raw.get("completed_nodes") or ()],
            recent_events=[dict(e) for e in raw.get("recent_events") or () if isinstance(e, Mapping)],
            last_result=raw.get("last_result") if isinstance(raw.get("last_result"), Mapping) else None,
            budgets=dict(raw.get("budgets") or {}),
            constraints=dict(raw.get("constraints") or {}),
            run_id=None if raw.get("run_id") is None else str(raw.get("run_id")),
        )

    @classmethod
    def build(cls, goal: str, world: "GameWorldState | Any", *,
              graph_digest: Iterable[Mapping[str, Any]] = (),
              capabilities: Iterable[Mapping[str, Any]] = (),
              completed_nodes: Iterable[str] = (),
              recent_events: Iterable[Mapping[str, Any]] = (),
              last_result: Mapping[str, Any] | None = None,
              budgets: Mapping[str, Any] | None = None,
              constraints: Mapping[str, Any] | None = None,
              run_id: str | None = None) -> "PlanRequest":
        """Convenience constructor used by the runner.

        ``world`` only has to expose ``summary()`` (duck-typed on purpose: the
        planner never imports :mod:`seerauto.world`).
        """

        summary: dict[str, Any]
        if world is None:
            summary = {}
        elif hasattr(world, "summary"):
            summary = dict(world.summary())
        elif isinstance(world, Mapping):
            summary = dict(world)
        else:
            summary = dict(getattr(world, "to_dict", dict)())
        return cls(
            goal=goal,
            world_summary=summary,
            graph_digest=[dict(n) for n in graph_digest],
            capabilities=[dict(c) for c in capabilities],
            completed_nodes=[str(n) for n in completed_nodes],
            recent_events=[dict(e) for e in recent_events],
            last_result=None if last_result is None else dict(last_result),
            budgets=dict(budgets or {}),
            constraints=dict(constraints or {}),
            run_id=run_id,
        )


# --------------------------------------------------------------------------- #
# decision
# --------------------------------------------------------------------------- #
@dataclass
class PlanDecision:
    """One planner decision.  Build it, then call :meth:`validate`.

    A refusal (``stop`` or ``need_human``) carries no work: the node,
    capability and budgets are normalised to empty/zero and ``need_human``
    implies ``stop``.  A scheduling decision must name a node *and* a
    capability and stay inside the run budget.
    """

    goal: str = ""
    selected_node: str = ""
    capability: str = ""
    reason: str = ""
    expected_observation: str = ""
    max_actions: int = 0
    max_duration_s: float | int = 0
    requires_approval: bool = False
    #: The planner refuses to continue (nothing will be scheduled).
    stop: bool = False
    stop_reason: str = ""
    need_human: bool = False
    human_question: str = ""
    #: Transport metadata attached by the caller (``openai_compat.CallMeta``).
    meta: Any = field(default=None, repr=False, compare=False)

    # -- contract ---------------------------------------------------------- #
    @property
    def is_refusal(self) -> bool:
        return bool(self.stop or self.need_human)

    def normalize(self) -> "PlanDecision":
        """Apply the documented refusal rules; idempotent."""

        if self.need_human:
            self.stop = True
        if self.stop:
            self.selected_node = ""
            self.capability = ""
            self.max_actions = 0
            self.max_duration_s = 0
            self.requires_approval = False
        return self

    def validate(self,
                 known_nodes: Iterable[Any] | None = None,
                 known_capabilities: Iterable[Any] | None = None,
                 budgets: Mapping[str, Any] | None = None) -> "PlanDecision":
        """Strict check; raises :class:`PlanError` with a precise message.

        ``known_nodes`` / ``known_capabilities`` accept ids, digest dicts or a
        mapping keyed by id; ``None`` skips that check.  ``budgets`` holds the
        run-level caps (``max_actions`` / ``max_duration_s``).
        """

        self._validate_shape()
        self.normalize()
        self._validate_refusal()
        self._validate_selection(known_nodes, known_capabilities, budgets)
        return self

    # -- internals --------------------------------------------------------- #
    def _validate_refusal(self) -> None:
        if self.stop and not _str(self.stop_reason).strip():
            raise PlanError(
                "a refusal must explain itself: 'stop'/'need_human' is set but "
                "'stop_reason' is missing or empty")
        if self.need_human and not _str(self.human_question).strip():
            raise PlanError(
                "'need_human' is set but 'human_question' is missing or empty; "
                "ask one concrete question a human can answer")
        if not self.stop and _str(self.stop_reason).strip():
            raise PlanError(
                "'stop_reason' is set but 'stop' is not true; set 'stop': true "
                "to refuse, or drop 'stop_reason' to schedule work")
        if not self.need_human and _str(self.human_question).strip():
            raise PlanError(
                "'human_question' is set but 'need_human' is not true; set "
                "'need_human': true to ask, or drop 'human_question' to schedule work")

    def _validate_selection(self, known_nodes: Iterable[Any] | None,
                            known_capabilities: Iterable[Any] | None,
                            budgets: Mapping[str, Any] | None) -> None:
        nodes = _ids(known_nodes)
        caps = _ids(known_capabilities)
        if not self.stop:
            if not self.selected_node:
                raise PlanError(
                    "'selected_node' is empty but no refusal key is set; name a "
                    "node id, or set 'stop': true")
            if not self.capability:
                raise PlanError(
                    "'capability' is empty but no refusal key is set; name a "
                    "registered capability id, or set 'stop': true")
            if known_nodes is not None and self.selected_node not in nodes:
                raise PlanError(
                    f"unknown node {self.selected_node!r}; the goal graph has "
                    f"{len(nodes)} node(s): {_preview(nodes)}")
            if known_capabilities is not None and self.capability not in caps:
                raise PlanError(
                    f"unknown capability {self.capability!r}; the registry has "
                    f"{len(caps)} capability(ies): {_preview(caps)}")
        self._validate_budgets(budgets or {})

    def _validate_budgets(self, budgets: Mapping[str, Any]) -> None:
        # Run budgets are written either in action/minutes terms (the runner's
        # ``max_battles`` / ``max_minutes``) or in contract terms.
        cap_actions, actions_label = _budget_cap(
            budgets, (("max_actions", 1.0), ("max_battles", 1.0)))
        cap_duration, duration_label = _budget_cap(
            budgets, (("max_duration_s", 1.0), ("max_seconds", 1.0), ("max_minutes", 60.0)))
        if cap_actions is not None and self.max_actions > cap_actions:
            raise PlanError(
                f"max_actions={self.max_actions} exceeds the run budget "
                f"{actions_label}")
        if cap_duration is not None and self.max_duration_s > cap_duration:
            raise PlanError(
                f"max_duration_s={self.max_duration_s} exceeds the run budget "
                f"{duration_label}")
        if self.stop and (self.max_actions != 0 or self.max_duration_s != 0):
            raise PlanError(
                "a refusal must not schedule work: leave 'max_actions' and "
                "'max_duration_s' at 0 when 'stop'/'need_human' is set")

    def _validate_shape(self) -> None:
        if not isinstance(self.goal, str) or not self.goal.strip():
            raise PlanError(f"'goal' must be a non-empty string, got {_render(self.goal)}")
        for key in ("selected_node", "capability", "stop_reason", "human_question"):
            value = getattr(self, key)
            if not isinstance(value, str):
                raise PlanError(f"'{key}' must be a string, got {_render(value)}")
        for key in _NON_EMPTY_TEXT[1:]:
            value = getattr(self, key)
            if not isinstance(value, str):
                raise PlanError(f"'{key}' must be a string, got {_render(value)}")
            if not value.strip():
                raise PlanError(f"'{key}' must not be empty ({key!r} is blank)")
        for key in ("requires_approval", "stop", "need_human"):
            value = getattr(self, key)
            if not isinstance(value, bool):
                raise PlanError(f"'{key}' must be a JSON boolean, got {_render(value)}")
        if not _is_int(self.max_actions):
            raise PlanError(f"'max_actions' must be an integer, got {_render(self.max_actions)}")
        low = 0 if (self.stop or self.need_human) else MAX_ACTIONS_MIN
        if not low <= self.max_actions <= MAX_ACTIONS_MAX:
            raise PlanError(
                f"'max_actions' must be within [{low}, {MAX_ACTIONS_MAX}], "
                f"got {self.max_actions}")
        if not _is_number(self.max_duration_s):
            raise PlanError(
                f"'max_duration_s' must be a number, got {_render(self.max_duration_s)}")
        low_s = 0 if (self.stop or self.need_human) else MAX_DURATION_MIN
        if not low_s <= self.max_duration_s <= MAX_DURATION_MAX:
            raise PlanError(
                f"'max_duration_s' must be within [{low_s}, {MAX_DURATION_MAX}], "
                f"got {self.max_duration_s}")

    # -- serialisation ----------------------------------------------------- #
    def to_dict(self, *, with_meta: bool = False) -> dict[str, Any]:
        """The exact wire form (round-trips through :func:`parse_decision`)."""

        out: dict[str, Any] = {
            "goal": self.goal,
            "selected_node": self.selected_node,
            "capability": self.capability,
            "reason": self.reason,
            "expected_observation": self.expected_observation,
            "max_actions": self.max_actions,
            "max_duration_s": _clean_number(self.max_duration_s),
            "requires_approval": self.requires_approval,
        }
        if self.stop:
            out["stop"] = True
            out["stop_reason"] = self.stop_reason
        if self.need_human:
            out["need_human"] = True
            out["human_question"] = self.human_question
        if with_meta and self.meta is not None:
            out["meta"] = _meta_dict(self.meta)
        return out

    def summary(self) -> str:
        """One-line form for logs / event records."""

        if self.stop or self.need_human:
            what = "STOP" if not self.need_human else "NEED_HUMAN"
            return f"{what} goal={self.goal!r}: {_clip(self.stop_reason, 160)}"
        return (f"{self.selected_node} -> {self.capability} "
                f"(<= {self.max_actions} actions / {_clean_number(self.max_duration_s)}s"
                f"{', approval required' if self.requires_approval else ''})")

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "PlanDecision":
        """Strict reader for one decision object (unknown keys rejected)."""

        if not isinstance(raw, Mapping):
            raise PlanError(
                f"a decision must be a JSON object, got {type(raw).__name__}")
        unknown = sorted(set(map(str, raw.keys())) - ALLOWED_KEYS)
        if unknown:
            raise PlanError(
                f"unknown top-level key(s) {unknown}; the contract accepts only "
                f"{list(REQUIRED_KEYS + OPTIONAL_KEYS)}")
        missing = [key for key in REQUIRED_KEYS if key not in raw]
        if missing:
            raise PlanError(
                f"missing required key(s) {missing}; every decision needs "
                f"{list(REQUIRED_KEYS)}")
        for key in REQUIRED_KEYS:
            if key in ("max_actions", "max_duration_s", "requires_approval"):
                continue
            value = raw.get(key)
            if not isinstance(value, str):
                raise PlanError(
                    f"'{key}' must be a string, got {_render(value)}")
        if not isinstance(raw.get("requires_approval"), bool):
            raise PlanError(
                "'requires_approval' must be a JSON boolean, got "
                f"{_render(raw.get('requires_approval'))}")
        if not _is_int(raw.get("max_actions")):
            raise PlanError(
                f"'max_actions' must be a JSON integer, got {_render(raw.get('max_actions'))}")
        if not _is_number(raw.get("max_duration_s")):
            raise PlanError(
                f"'max_duration_s' must be a JSON number, got "
                f"{_render(raw.get('max_duration_s'))}")
        if "stop" in raw and not isinstance(raw["stop"], bool):
            raise PlanError(f"'stop' must be a JSON boolean, got {_render(raw.get('stop'))}")
        if "need_human" in raw and not isinstance(raw["need_human"], bool):
            raise PlanError(
                f"'need_human' must be a JSON boolean, got {_render(raw.get('need_human'))}")
        for key in ("stop_reason", "human_question"):
            if key in raw and not isinstance(raw[key], str):
                raise PlanError(f"'{key}' must be a string, got {_render(raw.get(key))}")

        decision = cls(
            goal=raw["goal"],
            selected_node=raw["selected_node"],
            capability=raw["capability"],
            reason=raw["reason"],
            expected_observation=raw["expected_observation"],
            max_actions=raw["max_actions"],
            max_duration_s=raw["max_duration_s"],
            requires_approval=raw["requires_approval"],
            stop=bool(raw.get("stop", False)),
            stop_reason=str(raw.get("stop_reason") or ""),
            need_human=bool(raw.get("need_human", False)),
            human_question=str(raw.get("human_question") or ""),
        )
        decision.normalize()
        decision._validate_shape()
        decision._validate_refusal()
        return decision


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _render(value: Any) -> str:
    """Type-and-value rendering for error messages (never a bare repr)."""

    if isinstance(value, bool):
        return f"bool {value}"
    if isinstance(value, str):
        return f"str {value!r}"
    if isinstance(value, (int, float)):
        return f"{type(value).__name__} {value}"
    if value is None:
        return "null"
    return type(value).__name__


def _clean_number(value: float | int) -> float | int:
    """Emit ``1800`` rather than ``1800.0`` when the value is integral."""

    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _preview(items: Iterable[str], limit: int = 12) -> list[str]:
    values = list(items)
    if len(values) <= limit:
        return values
    return values[:limit] + [f"... {len(values) - limit} more"]


def _meta_dict(meta: Any) -> dict[str, Any]:
    if hasattr(meta, "to_dict"):
        return dict(meta.to_dict())
    if isinstance(meta, Mapping):
        return dict(meta)
    return {"repr": repr(meta)}


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"```[ \t]*(?:json|JSON)?[ \t]*\r?\n?(.*?)```", re.DOTALL)


def parse_decision(raw_json_text: str | bytes) -> PlanDecision:
    """Parse one decision from model output.

    Tolerates a fenced ```` ```json ... ``` ```` block and a JSON object
    surrounded by prose, but the object itself is checked strictly
    (unknown keys / missing keys / types / ranges).  Node and capability
    membership is *not* checked here - call :meth:`PlanDecision.validate`.
    """

    if isinstance(raw_json_text, bytes):
        try:
            text = raw_json_text.decode("utf-8")
        except UnicodeDecodeError as exc:  # pragma: no cover - defensive
            raise PlanError(f"decision is not valid UTF-8: {exc}") from exc
    elif isinstance(raw_json_text, str):
        text = raw_json_text
    else:
        raise PlanError(
            f"parse_decision expects JSON text, got {type(raw_json_text).__name__}")
    if not text.strip():
        raise PlanError("decision text is empty")

    body = _strip_fence(text)
    payload = _loads_object(body)
    return PlanDecision.from_raw(payload)


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        match = _FENCE_RE.search(stripped)
        if not match:
            raise PlanError(
                f"unterminated ``` fence in decision text: {_snippet(stripped)}")
        return match.group(1).strip()
    return stripped


def _loads_object(text: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as first_error:
        candidate = _extract_object(text)
        if candidate is None:
            raise PlanError(
                f"decision is not valid JSON ({first_error.msg} at char "
                f"{first_error.pos}): {_snippet(text)}") from first_error
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as second_error:
            raise PlanError(
                f"decision is not valid JSON ({second_error.msg} at char "
                f"{second_error.pos}): {_snippet(candidate)}") from second_error
    if not isinstance(payload, Mapping):
        raise PlanError(
            f"decision must be one JSON object, got {type(payload).__name__}")
    return payload


def _extract_object(text: str) -> str | None:
    """Return the first balanced ``{...}`` block, ignoring braces in strings."""

    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


# --------------------------------------------------------------------------- #
# protocol
# --------------------------------------------------------------------------- #
@runtime_checkable
class Planner(Protocol):
    """The seam between the AI layer and the deterministic kernel.

    Implementations are free to be deterministic (no model at all), a bridge to
    a human/agent answering files, or a remote chat-completions endpoint.  They
    may return an empty-handed *refusal* but must never invent a node id, a
    capability id or an action plan outside the contract.
    """

    name: str

    def decide(self, req: PlanRequest) -> PlanDecision:  # pragma: no cover - protocol
        ...


__all__ = [
    "PlanRequest",
    "PlanDecision",
    "Planner",
    "PlannerError",
    "PlannerUnavailable",
    "PlanError",
    "parse_decision",
    "REQUIRED_KEYS",
    "OPTIONAL_KEYS",
    "ALLOWED_KEYS",
    "MAX_ACTIONS_MIN",
    "MAX_ACTIONS_MAX",
    "MAX_DURATION_MIN",
    "MAX_DURATION_MAX",
]
