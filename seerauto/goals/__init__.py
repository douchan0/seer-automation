"""Goal graph for the 谱尼 (Puni) objective.

``puni.yaml`` is the machine-readable form of ``docs/PUNI_TASK_GRAPH.md``: 42
nodes, each with a prerequisite DAG, a capability requirement, a completion
predicate over :class:`~seerauto.world.model.GameWorldState`, a budget and an
explicit failure policy.

The importable surface is small on purpose:

>>> from seerauto.goals import GoalGraph
>>> graph = GoalGraph.load()
>>> graph.node("puni.prepare.level_up").legacy_id
'P1-01'
"""

from __future__ import annotations

from .graph import (
    BUDGET_KEYS,
    COMPLETION_TYPES,
    DEFAULT_PATH,
    ON_FAILURE,
    PHASES,
    STATUSES,
    Completion,
    GoalGraph,
    GoalGraphError,
    GoalNode,
)
from .predicates import (
    PREDICATES,
    STATE_ARG_KEYS,
    STATE_PREDICATES,
    GraphError,
    PredicateError,
    PredicateResult,
    evaluate,
)

__all__ = [
    "GoalGraph",
    "GoalGraphError",
    "GoalNode",
    "Completion",
    "DEFAULT_PATH",
    "COMPLETION_TYPES",
    "STATUSES",
    "ON_FAILURE",
    "PHASES",
    "BUDGET_KEYS",
    "PREDICATES",
    "STATE_PREDICATES",
    "STATE_ARG_KEYS",
    "PredicateResult",
    "GraphError",
    "PredicateError",
    "evaluate",
]
