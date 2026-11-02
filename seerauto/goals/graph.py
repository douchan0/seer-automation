"""The goal graph: a versioned, machine-readable view of ``docs/PUNI_TASK_GRAPH.md``.

Why this exists
---------------
The human analysis (``docs/PUNI_TASK_GRAPH.md``, 42 nodes) has to be read by a
planner on every run.  A 900-line markdown file read by an LLM is a guess
generator: "P1-05 is blocked" becomes "so buy 萨帕克", and the gate that makes
it blocked is in a *different* paragraph.  This module turns that document into
data with three invariants the planner cannot talk its way around:

1. **A node is completed by world evidence, not by assertion.**  Every node
   carries a ``completion`` predicate over :class:`GameWorldState`; only
   ``completion.type == 'verified_account_state'`` may auto-complete a node, and
   the predicate must be registered in :mod:`seerauto.goals.predicates`.
2. **Not executable means not selected.**  A node with ``executable: false`` is
   display-only and must carry an explicit ``blocked_reason``; the planner can
   show it, never run it.
3. **The graph is a DAG with typed prerequisites.**  A prerequisite is either a
   node id, a ``capability.<name>`` from the graph's capability registry, or a
   ``state.<name>`` from ``STATE_PREDICATES`` (optionally ``state.scene_is:WORLD``).

Loading validates all of that and raises :class:`GoalGraphError` with the exact
node and field at fault — the failure mode should be "the YAML is wrong", never
"the planner did something strange at 3am".

Usage
-----
>>> g = GoalGraph.load()
>>> g.node("puni.prepare.level_up").legacy_id
'P1-01'
>>> [n.id for n in g.ready(world=w, completed=set(), capabilities={"battle.repeat"})]

``status_of`` returns one of ``completed`` / ``ready`` / ``missing_prereq`` /
``missing_capability`` / ``not_executable`` / ``blocked`` / ``unknown``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .predicates import (
    PREDICATES,
    STATE_ARG_KEYS,
    STATE_PREDICATES,
    GraphError,
    evaluate,
)

#: Default graph file (this package's ``puni.yaml``).
DEFAULT_PATH = Path(__file__).with_name("puni.yaml")

#: Only ``verified_account_state`` may ever auto-complete a node.
COMPLETION_TYPES = frozenset({"verified_account_state", "capability_demonstrated"})

#: Status vocabulary (see ``puni.yaml`` ``conventions`` for the exact meaning).
STATUSES = frozenset({"proven", "partial", "todo", "unverified", "blocked", "unknown"})

#: What the runner does when a node fails.
ON_FAILURE = frozenset({"pause_and_replan", "retry", "mark_blocked"})

PHASES = ("P0", "P1", "P2", "P3", "P4")

#: Every node must carry all three numbers (a budget without a limit is not one).
BUDGET_KEYS = ("max_minutes", "max_battles", "max_spend")

#: Source-document node id (``P0-01`` .. ``P4-04``) — the traceability anchor.
_LEGACY_ID = re.compile(r"^P[0-4]-\d{2}$")

#: Capability ``verified`` values, strongest first.
CAPABILITY_VERIFIED = frozenset({"real", "replay", "unverified"})

#: Statuses that may never be marked executable (no point planning a run that
#: cannot happen, or one whose mechanics are unknown).
_EXECUTABLE_FORBIDDEN_STATUS = frozenset({"blocked", "unknown"})


class GoalGraphError(ValueError):
    """The graph file or a query is invalid.  The message names the culprit."""


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Completion:
    """How a node may be recognised as done."""

    type: str
    predicate: str
    args: Mapping[str, Any] = field(default_factory=dict)

    @property
    def may_auto_complete(self) -> bool:
        return self.type == "verified_account_state"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "predicate": self.predicate, "args": dict(self.args)}


@dataclass(frozen=True)
class GoalNode:
    id: str
    title: str
    description: str
    phase: str
    priority: int
    prerequisites: tuple[str, ...]
    requires_capabilities: tuple[str, ...]
    completion: Completion
    status: str
    executable: bool
    skill: str | None
    skill_inputs: Mapping[str, Any]
    budget: Mapping[str, Any]
    on_failure: str
    evidence: tuple[str, ...]
    notes: str
    legacy_id: str
    #: Required when ``executable`` is false; names what is missing, and why.
    blocked_reason: str = ""

    @property
    def node_prerequisites(self) -> tuple[str, ...]:
        return tuple(p for p in self.prerequisites if not _is_symbolic(p))

    @property
    def capability_prerequisites(self) -> tuple[str, ...]:
        return tuple(p for p in self.prerequisites if p.startswith("capability."))

    @property
    def state_prerequisites(self) -> tuple[str, ...]:
        return tuple(p for p in self.prerequisites if p.startswith("state."))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "legacy_id": self.legacy_id,
            "title": self.title,
            "description": self.description,
            "phase": self.phase,
            "priority": self.priority,
            "prerequisites": list(self.prerequisites),
            "requires_capabilities": list(self.requires_capabilities),
            "completion": self.completion.to_dict(),
            "status": self.status,
            "executable": self.executable,
            "blocked_reason": self.blocked_reason,
            "skill": self.skill,
            "skill_inputs": dict(self.skill_inputs),
            "budget": dict(self.budget),
            "on_failure": self.on_failure,
            "evidence": list(self.evidence),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class GoalGraph:
    goal: str
    title: str
    version: int
    capabilities: Mapping[str, Mapping[str, Any]]
    nodes: Mapping[str, GoalNode]
    #: Provenance of the source document, kept for reporting.
    source: str = ""
    updated: str = ""
    #: World-key conventions the predicates rely on (documentation, not behaviour).
    conventions: Mapping[str, Any] = field(default_factory=dict)
    path: Path | None = None

    # -- construction ------------------------------------------------------ #
    @classmethod
    def load(cls, path: str | Path | None = None) -> "GoalGraph":
        """Read and validate a graph file (default: this package's ``puni.yaml``)."""

        target = Path(path) if path is not None else DEFAULT_PATH
        raw = _read_yaml(target)
        if not isinstance(raw, Mapping):
            raise GoalGraphError(f"{target}: top level must be a mapping, got {type(raw).__name__}")
        graph = _build(raw, target)
        _validate(graph)
        return graph

    # -- lookups ----------------------------------------------------------- #
    def node(self, node_id: str) -> GoalNode:
        try:
            return self.nodes[node_id]
        except KeyError:
            legacy = {n.legacy_id: n.id for n in self.nodes.values()}
            hint = ""
            if node_id in legacy:
                hint = f" — {node_id!r} is a legacy id; use {legacy[node_id]!r}"
            elif node_id.startswith("P") and "-" in node_id:
                hint = " — legacy ids live in GoalNode.legacy_id, not in graph.nodes"
            raise GoalGraphError(
                f"unknown goal node {node_id!r}{hint}; known: {sorted(self.nodes)}") from None

    def capability(self, name: str) -> Mapping[str, Any]:
        try:
            return self.capabilities[name]
        except KeyError:
            raise GoalGraphError(
                f"unknown capability {name!r}; registered: {sorted(self.capabilities)}") from None

    def ids(self) -> list[str]:
        """Node ids in file order."""

        return list(self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    def __contains__(self, node_id: object) -> bool:
        return node_id in self.nodes

    def __iter__(self) -> Iterator[GoalNode]:
        return iter(self.nodes.values())

    def by_phase(self) -> dict[str, list[GoalNode]]:
        out: dict[str, list[GoalNode]] = {p: [] for p in PHASES}
        for node in self.nodes.values():
            out.setdefault(node.phase, []).append(node)
        return out

    def available_capabilities(self) -> set[str]:
        """Capabilities proven on the real client — what may be relied on today.

        ``replay``-verified capabilities are *not* included: this graph drives the
        live machine, where a replay-only capability is still unproven.
        """

        return {name for name, cap in self.capabilities.items()
                if str(cap.get("verified", "")).strip() == "real"}

    # -- structure --------------------------------------------------------- #
    def dependents(self, node_id: str) -> list[str]:
        """Node ids that name ``node_id`` directly as a prerequisite (sorted)."""

        self.node(node_id)
        return sorted(n.id for n in self.nodes.values() if node_id in n.prerequisites)

    def build_order(self) -> list[str]:
        """A topological order of every node (priority breaks ties).

        Symbolic prerequisites (``capability.*`` / ``state.*``) are not nodes and
        are ignored here; they are checked at run time against the capability set
        and the world.
        """

        remaining = {nid: {p for p in n.prerequisites if not _is_symbolic(p)}
                     for nid, n in self.nodes.items()}
        ordered: list[str] = []
        while remaining:
            ready = [nid for nid, deps in remaining.items() if not deps]
            if not ready:
                raise GoalGraphError(
                    "goal graph is cyclic; unresolved nodes: " + ", ".join(sorted(remaining)))
            # highest priority first, then id for determinism
            ready.sort(key=lambda nid: (-self.nodes[nid].priority, nid))
            for nid in ready:
                ordered.append(nid)
                del remaining[nid]
            for deps in remaining.values():
                deps.difference_update(ready)
        return ordered

    # -- runtime queries --------------------------------------------------- #
    def status_of(self, node_id: str, *, world: Any, completed: Iterable[str] = (),
                  capabilities: Iterable[str] | None = None) -> str:
        """One-word state of ``node_id`` for this world snapshot.

        ``completed`` is scheduler bookkeeping (ids the runner observed finishing);
        ``capabilities`` defaults to :meth:`available_capabilities` (the
        real-verified set), so a caller can pass a narrower set for a dry run.
        """

        node = self.node(node_id)
        done = set(completed or ())
        caps = set(self.available_capabilities() if capabilities is None else capabilities)
        if node.id in done:
            return "completed"
        if self._completion_result(node, world)[0] is True:
            return "completed"
        if node.status == "blocked":
            return "blocked"
        if node.status == "unknown":
            return "unknown"
        if not node.executable:
            return "not_executable"
        unmet = self.unmet(node.id, world=world, completed=done, capabilities=caps)
        if any(reason.startswith("missing capability") for reason in unmet):
            return "missing_capability"
        if unmet:
            return "missing_prereq"
        return "ready"

    def ready(self, *, world: Any, completed: Iterable[str] = (),
              capabilities: Iterable[str] | None = None) -> list[GoalNode]:
        """Executable nodes whose prerequisites hold, highest priority first."""

        done = set(completed or ())
        caps = set(self.available_capabilities() if capabilities is None else capabilities)
        out = [n for n in self.nodes.values()
               if self.status_of(n.id, world=world, completed=done, capabilities=caps) == "ready"]
        out.sort(key=lambda n: (-n.priority, n.id))
        return out

    def unmet(self, node_id: str, *, world: Any, completed: Iterable[str] = (),
              capabilities: Iterable[str] | None = None) -> list[str]:
        """Human-readable reasons ``node_id`` cannot run now (empty list = can)."""

        node = self.node(node_id)
        done = set(completed or ())
        caps = set(self.available_capabilities() if capabilities is None else capabilities)
        reasons: list[str] = []
        seen_caps: set[str] = set()
        for name in node.requires_capabilities:
            if name in caps or name in seen_caps:
                continue
            seen_caps.add(name)
            reasons.append(f"missing capability {name!r} ({self._capability_note(name)})")
        for ref in node.prerequisites:
            base, _args = _parse_prerequisite(ref)
            if base.startswith("capability."):
                name = base[len("capability."):]
                if name in caps or name in seen_caps:
                    continue
                seen_caps.add(name)
                reasons.append(f"missing capability {name!r} ({self._capability_note(name)})")
            elif base.startswith("state."):
                holds, why = self._state_result(base, _args, world)
                if holds is not True:
                    reasons.append(f"state prerequisite {ref!r} not satisfied: {why}")
            else:
                if base in done:
                    continue
                holds, why = self._completion_result(self.nodes[base], world)
                if holds is True:
                    continue
                reasons.append(
                    f"prerequisite node {base!r} ({self.nodes[base].legacy_id} "
                    f"{self.nodes[base].title}) not completed: {why}")
        return reasons

    # -- internals --------------------------------------------------------- #
    def _capability_note(self, name: str) -> str:
        cap = self.capabilities.get(name)
        if cap is None:
            return "not in the capability registry"
        return f"verified={cap.get('verified', 'unverified')}"

    def _completion_result(self, node: GoalNode, world: Any) -> tuple[bool | None, str]:
        if not node.completion.may_auto_complete:
            return None, (f"completion.type={node.completion.type!r} never auto-completes "
                          "(only verified_account_state may)")
        try:
            return evaluate(node.completion.predicate, world, node.completion.args)
        except GraphError as exc:
            return None, f"predicate error: {exc}"

    def _state_result(self, ref: str, args: Mapping[str, Any],
                      world: Any) -> tuple[bool | None, str]:
        try:
            return evaluate(ref, world, args)
        except GraphError as exc:
            return None, f"predicate error: {exc}"

    # -- serialisation ----------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "goal": self.goal,
            "title": self.title,
            "updated": self.updated,
            "source": self.source,
            "capabilities": {k: dict(v) for k, v in self.capabilities.items()},
            "nodes": [n.to_dict() for n in self.nodes.values()],
            "conventions": _plain(self.conventions),
        }


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _read_yaml(path: Path) -> Any:
    try:
        import yaml  # noqa: PLC0415 - PyYAML is optional for the rest of the core
    except ImportError as exc:  # pragma: no cover - environment issue
        raise GoalGraphError(
            "PyYAML is required to load the goal graph "
            "(run with PYTHONPATH=.pydeps)") from exc
    if not path.exists():
        raise GoalGraphError(f"goal graph file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as exc:  # pragma: no cover - syntax error path
        raise GoalGraphError(f"{path}: invalid YAML: {exc}") from exc


def _is_symbolic(ref: str) -> bool:
    return ref.startswith("capability.") or ref.startswith("state.")


def _parse_prerequisite(ref: str) -> tuple[str, dict[str, Any]]:
    """Split ``state.scene_is:WORLD`` into the predicate and its single argument."""

    if not isinstance(ref, str):
        raise GoalGraphError(f"prerequisite must be a string, got {ref!r}")
    if ":" not in ref:
        return ref, {}
    base, _, value = ref.partition(":")
    if not base or not value:
        raise GoalGraphError(f"malformed prerequisite {ref!r} (expected <id> or <id>:<value>)")
    if base.startswith("state."):
        name = base[len("state."):]
        arg_key = STATE_ARG_KEYS.get(name)
        if arg_key is None:
            raise GoalGraphError(
                f"prerequisite {ref!r} passes an argument to state predicate {name!r}, "
                f"which takes none (argument-taking: {sorted(STATE_ARG_KEYS)})")
        return base, {arg_key: value}
    raise GoalGraphError(
        f"prerequisite {ref!r}: only state.* prerequisites accept ':<value>' arguments")


def _build(raw: Mapping[str, Any], path: Path) -> GoalGraph:
    for key in ("version", "goal", "title", "capabilities", "nodes"):
        if key not in raw:
            raise GoalGraphError(f"{path}: missing required top-level key {key!r}")

    capabilities = raw["capabilities"]
    if not isinstance(capabilities, Mapping) or not capabilities:
        raise GoalGraphError(f"{path}: 'capabilities' must be a non-empty mapping")

    raw_nodes = raw["nodes"]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise GoalGraphError(f"{path}: 'nodes' must be a non-empty list")

    nodes: dict[str, GoalNode] = {}
    for index, entry in enumerate(raw_nodes):
        node = _build_node(entry, index, path)
        if node.id in nodes:
            raise GoalGraphError(
                f"{path}: duplicate node id {node.id!r} (nodes[{index}], "
                f"first at nodes[{_index_of(raw_nodes, node.id)}])")
        nodes[node.id] = node

    return GoalGraph(
        goal=str(raw["goal"]),
        title=str(raw["title"]),
        version=_int(raw["version"], "version", path),
        capabilities={str(k): dict(v) for k, v in capabilities.items()},
        nodes=nodes,
        source=str(raw.get("source") or ""),
        updated=str(raw.get("updated") or ""),
        conventions=dict(raw.get("conventions") or {}),
        path=path,
    )


def _index_of(entries: list[Any], node_id: str) -> int:
    for i, entry in enumerate(entries):
        if isinstance(entry, Mapping) and entry.get("id") == node_id:
            return i
    return -1


def _int(value: Any, field_name: str, where: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise GoalGraphError(f"{where}: {field_name} must be an integer, got {value!r}") from None
    return value


#: Required node fields.  ``skill`` / ``skill_inputs`` are intentionally absent:
#: both are optional (no semantic skill may know how to do a node yet), and an
#: executable node without a skill is rejected separately with a precise message.
_NODE_KEYS = (
    "id", "title", "description", "phase", "priority", "prerequisites",
    "requires_capabilities", "completion", "status", "executable",
    "budget", "on_failure", "evidence", "notes", "legacy_id",
)


def _build_node(entry: Any, index: int, path: Path) -> GoalNode:
    if not isinstance(entry, Mapping):
        raise GoalGraphError(f"{path}: nodes[{index}] must be a mapping, got {type(entry).__name__}")
    node_id = entry.get("id")
    if not isinstance(node_id, str) or not node_id:
        raise GoalGraphError(f"{path}: nodes[{index}] has no string 'id'")
    where = f"{path}: node {node_id!r}"

    for key in _NODE_KEYS:
        if key not in entry:
            raise GoalGraphError(f"{where}: missing required field {key!r}")

    completion_raw = entry["completion"]
    if not isinstance(completion_raw, Mapping):
        raise GoalGraphError(f"{where}: 'completion' must be a mapping")
    for key in ("type", "predicate"):
        if key not in completion_raw:
            raise GoalGraphError(f"{where}: completion is missing {key!r}")
    args = completion_raw.get("args") or {}
    if not isinstance(args, Mapping):
        raise GoalGraphError(f"{where}: completion.args must be a mapping")
    completion = Completion(
        type=str(completion_raw["type"]),
        predicate=str(completion_raw["predicate"]),
        args=dict(args),
    )

    prerequisites = entry["prerequisites"]
    if not isinstance(prerequisites, (list, tuple)):
        raise GoalGraphError(f"{where}: 'prerequisites' must be a list")
    requires = entry["requires_capabilities"]
    if not isinstance(requires, (list, tuple)):
        raise GoalGraphError(f"{where}: 'requires_capabilities' must be a list")

    budget = entry["budget"]
    if not isinstance(budget, Mapping):
        raise GoalGraphError(f"{where}: 'budget' must be a mapping")
    skill_inputs = entry.get("skill_inputs") or {}
    if not isinstance(skill_inputs, Mapping):
        raise GoalGraphError(f"{where}: 'skill_inputs' must be a mapping")

    evidence = entry["evidence"]
    if isinstance(evidence, str):
        evidence = [evidence]
    if not isinstance(evidence, (list, tuple)):
        raise GoalGraphError(f"{where}: 'evidence' must be a list of anchors")

    skill = entry.get("skill")
    if skill is not None and not isinstance(skill, str):
        raise GoalGraphError(f"{where}: 'skill' must be a string when present")

    executable = entry["executable"]
    if not isinstance(executable, bool):
        raise GoalGraphError(f"{where}: 'executable' must be a bool, got {executable!r}")

    return GoalNode(
        id=node_id,
        title=str(entry["title"]),
        description=str(entry["description"]),
        phase=str(entry["phase"]),
        priority=_int(entry["priority"], "priority", where),
        prerequisites=tuple(str(p) for p in prerequisites),
        requires_capabilities=tuple(str(c) for c in requires),
        completion=completion,
        status=str(entry["status"]),
        executable=executable,
        skill=skill,
        skill_inputs=dict(skill_inputs),
        budget=dict(budget),
        on_failure=str(entry["on_failure"]),
        evidence=tuple(str(e) for e in evidence),
        notes=str(entry["notes"]),
        legacy_id=str(entry["legacy_id"]),
        blocked_reason=str(entry.get("blocked_reason") or ""),
    )


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _validate(graph: GoalGraph) -> None:
    if graph.version < 1:
        raise GoalGraphError(f"{graph.path}: version must be >= 1, got {graph.version}")
    if not graph.goal.strip():
        raise GoalGraphError(f"{graph.path}: goal must be non-empty")
    if not graph.title.strip():
        raise GoalGraphError(f"{graph.path}: title must be non-empty")

    for name, cap in graph.capabilities.items():
        if not isinstance(cap, Mapping):
            raise GoalGraphError(f"{graph.path}: capability {name!r} must be a mapping")
        for key in ("summary", "verified", "evidence"):
            if key not in cap:
                raise GoalGraphError(
                    f"{graph.path}: capability {name!r} is missing {key!r}")
        verified = str(cap["verified"]).strip()
        if verified not in CAPABILITY_VERIFIED:
            raise GoalGraphError(
                f"{graph.path}: capability {name!r} has verified={verified!r}; "
                f"allowed: {sorted(CAPABILITY_VERIFIED)}")

    seen_legacy: dict[str, str] = {}
    for node in graph.nodes.values():
        _validate_node(graph, node)
        if node.legacy_id in seen_legacy:
            raise GoalGraphError(
                f"{graph.path}: duplicate legacy_id {node.legacy_id!r} on "
                f"{node.id!r} and {seen_legacy[node.legacy_id]!r}")
        seen_legacy[node.legacy_id] = node.id

    _validate_prerequisites(graph)
    _validate_acyclic(graph)


def _validate_node(graph: GoalGraph, node: GoalNode) -> None:
    where = f"{graph.path}: node {node.id!r}"

    if not node.id.startswith(f"{graph.goal}."):
        raise GoalGraphError(
            f"{where}: id must start with {graph.goal + '.'!r} (goal={graph.goal!r})")
    parts = node.id.split(".")
    if len(parts) < 3:
        raise GoalGraphError(f"{where}: id must look like {graph.goal}.<phase>.<slug>")

    if node.phase not in PHASES:
        raise GoalGraphError(f"{where}: phase must be one of {list(PHASES)}, got {node.phase!r}")
    if not _LEGACY_ID.match(node.legacy_id):
        raise GoalGraphError(
            f"{where}: legacy_id must look like 'P0-01' .. 'P4-04' "
            f"(source document id), got {node.legacy_id!r}")

    if node.status not in STATUSES:
        raise GoalGraphError(
            f"{where}: status must be one of {sorted(STATUSES)}, got {node.status!r}")
    if node.on_failure not in ON_FAILURE:
        raise GoalGraphError(
            f"{where}: on_failure must be one of {sorted(ON_FAILURE)}, got {node.on_failure!r}")

    if not node.title.strip():
        raise GoalGraphError(f"{where}: title must be non-empty")
    if not node.description.strip():
        raise GoalGraphError(f"{where}: description must be non-empty")
    if not node.notes.strip():
        raise GoalGraphError(f"{where}: notes must be non-empty (state the evidence or the gap)")

    # -- completion -------------------------------------------------------- #
    if node.completion.type not in COMPLETION_TYPES:
        raise GoalGraphError(
            f"{where}: completion.type {node.completion.type!r} is not allowed; "
            f"use one of {sorted(COMPLETION_TYPES)}")
    if node.completion.predicate not in PREDICATES:
        raise GoalGraphError(
            f"{where}: completion.predicate {node.completion.predicate!r} is not registered "
            f"(known: {sorted(PREDICATES)})")

    # -- budget ------------------------------------------------------------ #
    for key in BUDGET_KEYS:
        if key not in node.budget:
            raise GoalGraphError(f"{where}: budget is missing {key!r}")
        value = node.budget[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GoalGraphError(
                f"{where}: budget.{key} must be numeric, got {value!r}")
        if value < 0:
            raise GoalGraphError(f"{where}: budget.{key} must be >= 0, got {value!r}")

    # -- skill ------------------------------------------------------------- #
    if node.skill is not None and not node.skill.strip():
        raise GoalGraphError(f"{where}: skill must be a non-empty string when present")
    if node.skill is None and node.skill_inputs:
        raise GoalGraphError(f"{where}: skill_inputs given without a skill")

    # -- capabilities ------------------------------------------------------ #
    for name in node.requires_capabilities:
        if name not in graph.capabilities:
            raise GoalGraphError(
                f"{where}: requires unknown capability {name!r}; "
                f"registered: {sorted(graph.capabilities)}")

    # -- executability ----------------------------------------------------- #
    if node.executable:
        if node.skill is None:
            raise GoalGraphError(
                f"{where}: executable node must name the skill that executes it "
                "(otherwise the planner has only 'click something')")
        if node.completion.type != "verified_account_state":
            raise GoalGraphError(
                f"{where}: executable={True} but completion.type is "
                f"{node.completion.type!r}; only 'verified_account_state' may auto-complete")
        if node.status in _EXECUTABLE_FORBIDDEN_STATUS:
            raise GoalGraphError(
                f"{where}: executable={True} but status is {node.status!r} — "
                f"a {node.status} node must stay display-only")
        if node.blocked_reason:
            raise GoalGraphError(
                f"{where}: executable node must not carry a blocked_reason "
                f"({node.blocked_reason!r})")
        if not node.evidence:
            raise GoalGraphError(f"{where}: executable node must cite evidence")
    else:
        if not node.blocked_reason.strip():
            raise GoalGraphError(
                f"{where}: executable={False} requires an explicit blocked_reason")
    if node.completion.predicate == "node_completed_before":
        raise GoalGraphError(
            f"{where}: completion.predicate 'node_completed_before' is a no-op stub; "
            "a node may never be completed by the planner's own bookkeeping")


def _validate_prerequisites(graph: GoalGraph) -> None:
    for node in graph.nodes.values():
        where = f"{graph.path}: node {node.id!r}"
        for ref in node.prerequisites:
            base, _args = _parse_prerequisite(ref)
            if base.startswith("capability."):
                name = base[len("capability."):]
                if name not in graph.capabilities:
                    raise GoalGraphError(
                        f"{where}: prerequisite {ref!r} names capability {name!r}, which is "
                        f"not in the capability registry")
                continue
            if base.startswith("state."):
                name = base[len("state."):]
                if name not in STATE_PREDICATES:
                    raise GoalGraphError(
                        f"{where}: prerequisite {ref!r} names state predicate {name!r}, which "
                        f"is not registered (known: {sorted(STATE_PREDICATES)})")
                continue
            if base not in graph.nodes:
                raise GoalGraphError(
                    f"{where}: prerequisite {ref!r} is neither a node id nor a "
                    f"'capability.*' / 'state.*' reference")
        for ref in node.requires_capabilities:
            if ref.startswith("capability."):
                raise GoalGraphError(
                    f"{where}: requires_capabilities must hold bare names, got {ref!r}")


def _validate_acyclic(graph: GoalGraph) -> None:
    order = graph.build_order()  # raises GoalGraphError naming the cycle
    position = {nid: i for i, nid in enumerate(order)}
    for node in graph.nodes.values():
        for dep in node.node_prerequisites:
            if position[dep] >= position[node.id]:  # pragma: no cover - build_order guarantees
                raise GoalGraphError(
                    f"{graph.path}: prerequisite {dep!r} does not precede {node.id!r}")
    missing = set(graph.nodes) - set(order)
    if missing:  # pragma: no cover - build_order covers all nodes
        raise GoalGraphError(f"{graph.path}: nodes missing from build order: {sorted(missing)}")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


__all__ = [
    "GoalGraphError",
    "Completion",
    "GoalNode",
    "GoalGraph",
    "DEFAULT_PATH",
    "COMPLETION_TYPES",
    "STATUSES",
    "ON_FAILURE",
    "PHASES",
    "BUDGET_KEYS",
]
