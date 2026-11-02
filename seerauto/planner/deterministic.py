"""Deterministic planner: the safety net when no model is available.

Selection rule (deliberately boring, so it can be trusted at 3 a.m.):

1. order the graph digest by priority (descending), ties in digest order;
2. skip nodes the scheduler does not report as ``ready`` (a graph-reported
   ``missing_capability`` is remembered as missing work, not as "blocked");
3. skip nodes whose capability is not registered, that need GUI input while
   ``constraints.allow_input`` is false, or that need approval while
   ``constraints.forbid_approval`` is set;
4. clamp the action/time budget to ``min(capability, node, run)``;
5. return the first survivor.

If nothing survives it returns a **refusal** (``stop`` / ``need_human``) whose
``stop_reason`` lists the unmet reasons, because a planner that keeps clicking
when the graph is stuck is worse than one that stops.

The ``reason`` of every scheduling decision must name the evidence it used
(the scheduler status, the world unknowns, the last failure, or the skipped
higher-priority nodes) - see :meth:`DeterministicPlanner._because`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .base import (
    MAX_ACTIONS_MAX,
    MAX_ACTIONS_MIN,
    MAX_DURATION_MAX,
    MAX_DURATION_MIN,
    PlanDecision,
    PlanRequest,
    PlannerError,
)

READY = "ready"
#: Statuses that mean "no work left" (``GoalGraph.status_of`` says ``completed``).
DONE_STATUSES = ("done", "completed", "skipped")
#: Statuses under which the *graph* already reports a missing capability.
MISSING_CAPABILITY_STATUS = "missing_capability"
#: Blockers that mean "the code for this node does not exist yet".
MISSING_WORK_KINDS = frozenset({"missing_capability", "missing_playbook"})


@dataclass(frozen=True)
class Blocker:
    """Why one node could not be scheduled."""

    kind: str
    node_id: str
    detail: str
    priority: int = 0
    capability: str = ""

    def render(self) -> str:
        label = {
            "missing_capability": "capability missing",
            "input_disabled": "input disabled",
            "approval_forbidden": "approval forbidden",
            "budget": "budget",
            "node_status": "not ready",
        }.get(self.kind, self.kind)
        return f"{self.node_id}: {label} - {self.detail}"


class DeterministicPlanner:
    """Priority-ordered, no-model planner.  Never calls the network."""

    name = "deterministic"

    def __init__(self, *,
                 default_max_actions: int = 40,
                 default_max_duration_s: float = 1800.0,
                 max_blockers: int = 4,
                 max_reason_chars: int = 480,
                 max_blocker_chars: int = 220,
                 max_stop_reason_chars: int = 1000) -> None:
        self.default_max_actions = int(default_max_actions)
        self.default_max_duration_s = float(default_max_duration_s)
        self.max_blockers = int(max_blockers)
        self.max_reason_chars = int(max_reason_chars)
        self.max_blocker_chars = int(max_blocker_chars)
        self.max_stop_reason_chars = int(max_stop_reason_chars)

    # -- protocol ---------------------------------------------------------- #
    def decide(self, req: PlanRequest) -> PlanDecision:
        if not isinstance(req, PlanRequest):
            raise PlannerError(
                f"{self.name}: decide() expects a PlanRequest, got {type(req).__name__}")
        if not str(req.goal or "").strip():
            raise PlannerError(
                f"{self.name}: PlanRequest.goal is empty; refusing to decide without "
                "the user-facing goal")

        nodes = [dict(n) for n in (req.graph_digest or ()) if isinstance(n, Mapping)]
        specs = {str(c.get("id")): dict(c) for c in (req.capabilities or ())
                 if isinstance(c, Mapping) and c.get("id")}
        completed = {str(n) for n in (req.completed_nodes or ())}
        allow_input = bool(req.constraint("allow_input", False))
        forbid_approval = bool(req.constraint("forbid_approval", False))

        run_budget = self._run_budget(req)
        if run_budget is None:
            return self._refuse(
                req, [], [Blocker("budget", "<run>",
                                  "the run budget is exhausted (max_actions or "
                                  "max_duration_s is below 1)")])
        run_actions, run_duration = run_budget

        ready_nodes = [n for n in nodes if self._status(n) == READY]
        blockers: list[Blocker] = []
        for priority, index, node in self._ordered(nodes):
            node_id = self._node_id(node, index)
            status = self._status(node)
            if node_id in completed or status in DONE_STATUSES:
                continue
            if status == MISSING_CAPABILITY_STATUS:
                capability = self._capability_of(node)
                detail = _text(node.get("blocked_reason")) or (
                    f"the graph reports a missing capability"
                    + (f" ({capability})" if capability else ""))
                blockers.append(Blocker(
                    "missing_capability", node_id, detail, priority, capability))
                continue
            if status != READY:
                blockers.append(Blocker(
                    "node_status", node_id,
                    self._status_detail(node, status), priority))
                continue
            if node.get("executable") is False:
                blockers.append(Blocker(
                    "node_status", node_id, "the graph marks it not executable", priority))
                continue

            capability = self._capability_of(node)
            if node.get("playbook") is False:
                blockers.append(Blocker(
                    "missing_playbook", node_id,
                    "the runner has no verified skill sequence (playbook) for this "
                    "node yet", priority, capability))
                continue
            spec = specs.get(capability)
            if not capability:
                blockers.append(Blocker(
                    "missing_capability", node_id, "the node declares no capability",
                    priority))
                continue
            if spec is None:
                blockers.append(Blocker(
                    "missing_capability", node_id,
                    f"capability {capability!r} is not registered", priority, capability))
                continue
            if spec.get("requires_input") and not allow_input:
                blockers.append(Blocker(
                    "input_disabled", node_id,
                    f"{capability} drives the mouse/keyboard but constraints.allow_input "
                    "is false", priority, capability))
                continue
            requires_approval = bool(spec.get("requires_approval")) or bool(
                node.get("requires_approval"))
            if requires_approval and forbid_approval:
                blockers.append(Blocker(
                    "approval_forbidden", node_id,
                    f"{capability} needs human approval but constraints.forbid_approval "
                    "is set", priority, capability))
                continue

            budget = self._budget_for(node, spec, run_actions, run_duration)
            if budget is None:
                blockers.append(Blocker(
                    "budget", node_id,
                    "no action/time left inside the run budget", priority, capability))
                continue

            decision = self._schedule(req, node_id, priority, capability, spec,
                                      budget, requires_approval, len(ready_nodes), blockers)
            decision.validate(
                known_nodes=nodes,
                known_capabilities=specs,
                budgets=req.budgets or None,
            )
            return decision

        return self._refuse(req, nodes, blockers)

    # -- selection helpers ------------------------------------------------- #
    @staticmethod
    def _ordered(nodes: Sequence[Mapping[str, Any]]):
        """``(priority, index, node)`` sorted by priority desc, digest order asc."""

        decorated = [
            (DeterministicPlanner._priority(node), index, node)
            for index, node in enumerate(nodes)
        ]
        decorated.sort(key=lambda item: (-item[0], item[1]))
        return decorated

    @staticmethod
    def _priority(node: Mapping[str, Any]) -> int:
        value = node.get("priority")
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _status(node: Mapping[str, Any]) -> str:
        """Scheduler status; ``scheduler_status`` wins over the static file status.

        A goal node carries its *static* status in the graph file ("todo",
        "partial"...); the runner must add the scheduler status.  When it does
        so under ``scheduler_status`` we must not read the static one by
        accident.
        """

        for key in ("scheduler_status", "status"):
            value = node.get(key)
            if value is not None and str(value).strip():
                return str(value).strip().lower()
        return ""

    @staticmethod
    def _capability_of(node: Mapping[str, Any]) -> str:
        """The graph calls it ``skill``; the skill registry calls it ``id``."""

        for key in ("capability", "skill"):
            value = node.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _node_id(node: Mapping[str, Any], index: int) -> str:
        node_id = node.get("id")
        return str(node_id) if node_id else f"<node#{index}>"

    @staticmethod
    def _status_detail(node: Mapping[str, Any], status: str) -> str:
        blocked = _text(node.get("blocked_reason") or node.get("reason") or "")
        if status and blocked:
            return f"status={status}: {blocked}"
        if status:
            return f"status={status}"
        if blocked:
            return f"status unknown: {blocked}"
        return "status unknown"

    def _run_budget(self, req: PlanRequest) -> tuple[int, float] | None:
        actions_raw, actions_key = self._run_cap(
            req, (("max_actions", 1), ("max_battles", 1)))
        duration_raw, duration_key = self._run_cap(
            req, (("max_duration_s", 1), ("max_seconds", 1), ("max_minutes", 60)))
        actions = MAX_ACTIONS_MAX if actions_raw is None else self._as_int(
            actions_raw, actions_key)
        duration = MAX_DURATION_MAX if duration_raw is None else self._as_float(
            duration_raw, duration_key)
        actions = int(min(actions, MAX_ACTIONS_MAX))
        duration = float(min(duration, MAX_DURATION_MAX))
        if actions < MAX_ACTIONS_MIN or duration < MAX_DURATION_MIN:
            return None
        return actions, duration

    @staticmethod
    def _run_cap(req: PlanRequest,
                 keys: Sequence[tuple[str, float]]) -> tuple[Any, str]:
        for key, scale in keys:
            value = req.budget(key)
            if value is None:
                continue
            return (value * scale if scale != 1 else value), key
        return None, ""

    def _budget_for(self, node: Mapping[str, Any], spec: Mapping[str, Any],
                    run_actions: int, run_duration: float) -> tuple[int, float] | None:
        node_budget = node.get("budget")
        if not isinstance(node_budget, Mapping):
            node_budget = {}
        actions = min(
            self._positive_int(spec.get("max_actions"), self.default_max_actions),
            self._positive_int(
                self._first_present(node_budget, "max_actions", "max_battles"),
                self.default_max_actions),
            run_actions,
        )
        duration = min(
            self._positive_float(spec.get("max_duration_s"), self.default_max_duration_s),
            self._positive_float(self._node_duration_s(node_budget),
                                 self.default_max_duration_s),
            run_duration,
        )
        if actions < MAX_ACTIONS_MIN or duration < MAX_DURATION_MIN:
            return None
        return int(actions), _clean_number(duration)

    # -- decision builders ------------------------------------------------- #
    def _schedule(self, req: PlanRequest, node_id: str, priority: int, capability: str,
                  spec: Mapping[str, Any], budget: tuple[int, float],
                  requires_approval: bool, ready_count: int,
                  skipped: Sequence[Blocker]) -> PlanDecision:
        actions, duration = budget
        reason = self._reason(req, node_id, priority, capability, spec, ready_count, skipped)
        return PlanDecision(
            goal=req.goal,
            selected_node=node_id,
            capability=capability,
            reason=reason,
            expected_observation=self._expectation(capability, spec),
            max_actions=actions,
            max_duration_s=duration,
            requires_approval=requires_approval,
        )

    def _reason(self, req: PlanRequest, node_id: str, priority: int, capability: str,
                spec: Mapping[str, Any], ready_count: int,
                skipped: Sequence[Blocker]) -> str:
        head = (f"{ready_count} node(s) ready; picked {node_id} (priority {priority}) "
                f"because {self._because(req, capability, spec)}")
        if skipped:
            shown = ", ".join(
                f"{b.node_id} ({_clip(b.detail, 80)})" for b in skipped[:3])
            head += f"; skipped {len(skipped)} higher-priority node(s): {shown}"
        return _clip(head, self.max_reason_chars)

    def _because(self, req: PlanRequest, capability: str,
                 spec: Mapping[str, Any]) -> str:
        """Explain the pick with the evidence actually present in the request."""

        world = req.world_summary if isinstance(req.world_summary, Mapping) else {}
        unknowns = [str(u) for u in (world.get("unknowns") or ())]
        scene = world.get("scene")
        pet = world.get("active_pet")
        level = pet.get("level") if isinstance(pet, Mapping) else None
        last = req.last_result if isinstance(req.last_result, Mapping) else {}
        kind = str(spec.get("kind") or "").strip().lower()
        verified = str(spec.get("verified") or "unverified")

        if last.get("skill") and str(last.get("status") or "").upper() not in ("", "PASS"):
            return (f"the last action {last['skill']} ended {last.get('status')} "
                    f"({_clip(last.get('reason') or 'no reason given', 120)}), so the retry "
                    f"goes back through {capability}")

        if kind == "observe":
            if level is None:
                return (f"the active pet level is UNKNOWN, and {capability} is a read-only "
                        "observation - cheaper and safer than battling on a guess")
            if unknowns:
                return (f"{len(unknowns)} world fact(s) are UNKNOWN "
                        f"({', '.join(unknowns[:3])}) and {capability} is the cheapest way "
                        "to resolve them")
            return (f"{capability} is a read-only observation, so a wrong assumption costs "
                    "no game actions")

        if unknowns:
            return (f"{len(unknowns)} world fact(s) remain UNKNOWN "
                    f"({', '.join(unknowns[:3])}), but the scheduler already reports this "
                    f"node ready and {capability} is registered ({verified})")
        if scene is not None and str(scene).strip():
            return (f"the scheduler reports it ready, the world shows scene={scene}, and "
                    f"{capability} is a registered capability ({verified})")
        return (f"the scheduler reports it ready and {capability} is a registered "
                f"capability ({verified})")

    @staticmethod
    def _expectation(capability: str, spec: Mapping[str, Any]) -> str:
        post = [str(p) for p in (spec.get("postconditions") or ()) if str(p).strip()]
        if post:
            return (f"{capability} reports PASS and {', '.join(post[:3])} can be "
                    "re-observed")
        outputs = sorted((spec.get("outputs") or {}).keys())
        if outputs:
            return (f"{capability} reports PASS and returns {', '.join(outputs[:4])}")
        return f"{capability} reports PASS and the driver observes a state change"

    def _refuse(self, req: PlanRequest, nodes: Sequence[Mapping[str, Any]],
                blockers: list[Blocker]) -> PlanDecision:
        reasons = list(blockers)
        if not reasons:
            completed = {str(c) for c in (req.completed_nodes or ())}
            if nodes and all(str(n.get("id") or "") in completed
                             or self._status(n) in DONE_STATUSES for n in nodes):
                stop_reason = "all goal nodes are already done; nothing left to schedule"
            elif not nodes:
                stop_reason = "the goal graph is empty; there is no node to schedule"
            else:
                stop_reason = ("no node is ready and the graph reported no unmet reason "
                               "(status/blocked_reason missing in the digest)")
            return self._stop_decision(req, stop_reason)

        reasons.sort(key=lambda b: (-b.priority, b.node_id))
        # one line per blocker, clipped: a run log wants the reason, not a novel
        shown = [_clip(b.render(), self.max_blocker_chars)
                 for b in reasons[:self.max_blockers]]
        if len(reasons) > len(shown):
            shown.append(f"... {len(reasons) - len(shown)} more")
        stop_reason = _clip("nothing schedulable: " + "; ".join(shown),
                            self.max_stop_reason_chars)

        kinds = {b.kind for b in reasons}
        if kinds <= MISSING_WORK_KINDS:
            needs = ", ".join(
                f"{b.node_id} needs "
                f"{b.capability or 'an undeclared capability'}"
                f" ({b.kind.replace('missing_', 'missing ')})"
                for b in reasons[:3])
            question = (
                f"The ready node(s) cannot be executed with the current registry: "
                f"{needs}. Should that work be implemented, or should the run stop?")
            return self._stop_decision(req, stop_reason, need_human=True,
                                       human_question=question)
        return self._stop_decision(req, stop_reason)

    def _stop_decision(self, req: PlanRequest, stop_reason: str, *,
                       need_human: bool = False, human_question: str = "") -> PlanDecision:
        decision = PlanDecision(
            goal=req.goal,
            selected_node="",
            capability="",
            reason=stop_reason,
            expected_observation="no observation: the planner schedules no work",
            max_actions=0,
            max_duration_s=0,
            requires_approval=False,
            stop=True,
            stop_reason=stop_reason,
            need_human=need_human,
            human_question=human_question,
        )
        decision.normalize()
        decision.validate(budgets=req.budgets or None)
        return decision

    # -- parsing helpers --------------------------------------------------- #
    @staticmethod
    def _as_int(value: Any, key: str) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PlannerError(
                f"deterministic: budgets.{key} must be a number, got "
                f"{type(value).__name__}")
        return int(value)

    @staticmethod
    def _as_float(value: Any, key: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PlannerError(
                f"deterministic: budgets.{key} must be a number, got "
                f"{type(value).__name__}")
        return float(value)

    @staticmethod
    def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
        for key in keys:
            value = mapping.get(key)
            if value is not None:
                return value
        return None

    @classmethod
    def _node_duration_s(cls, budget: Mapping[str, Any]) -> Any:
        """Node budgets are written in minutes ("max_minutes") or seconds."""

        seconds = cls._first_present(budget, "max_duration_s", "max_seconds")
        if seconds is not None:
            return seconds
        minutes = cls._first_present(budget, "max_minutes")
        if minutes is None:
            return None
        try:
            return float(minutes) * 60.0
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        """Non-positive / absent means "not declared" -> ``default``."""

        try:
            number = int(value)
        except (TypeError, ValueError):
            return int(default)
        return number if number > 0 else int(default)

    @staticmethod
    def _positive_float(value: Any, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return float(default)
        return number if number > 0 else float(default)


def _text(value: Any) -> str:
    """Blocked reasons may be a string or a list of strings (``graph.unmet``)."""

    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "; ".join(str(item).strip() for item in value if str(item).strip())
    return "" if value is None else str(value).strip()


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    if limit > 0 and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "..."
    return text


def _clean_number(value: float) -> float | int:
    return int(value) if float(value).is_integer() else float(value)


__all__ = ["DeterministicPlanner", "Blocker", "READY", "DONE_STATUSES",
           "MISSING_CAPABILITY_STATUS", "MISSING_WORK_KINDS"]
