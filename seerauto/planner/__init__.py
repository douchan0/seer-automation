"""Planner layer - the only seam between AI decisions and the deterministic kernel.

Public surface::

    from seerauto.planner import build_planner, PlanRequest, PlanDecision

    planner = build_planner("deterministic")
    decision = planner.decide(request)
    if decision.stop or decision.need_human:
        ...   # log decision.stop_reason / ask the human
    else:
        scheduler.run(decision.selected_node, decision.capability,
                      max_actions=decision.max_actions,
                      max_duration_s=decision.max_duration_s)

Three implementations, one contract:

``deterministic``
    No model, no network.  Priority-ordered pick with an evidence-citing
    reason; the safe default and the offline fallback (``AGENTS.md`` rule 13).
``ai``
    OpenAI-compatible ``/chat/completions`` over stdlib ``urllib``.  High-level
    only: one node, one capability, never coordinates.
``bridge``
    Filesystem rendezvous: write ``request-<seq>.json``, wait for
    ``response-<seq>.json`` written by a human or an agent session.

Every planner raises :class:`PlannerError` (or :class:`PlannerUnavailable` when
it cannot even attempt a decision) instead of inventing a decision, so the
runner can always degrade to the pure-vision path.
"""

from __future__ import annotations

from typing import Any

from .base import (
    ALLOWED_KEYS,
    MAX_ACTIONS_MAX,
    MAX_ACTIONS_MIN,
    MAX_DURATION_MAX,
    MAX_DURATION_MIN,
    OPTIONAL_KEYS,
    REQUIRED_KEYS,
    PlanDecision,
    PlanError,
    PlanRequest,
    Planner,
    PlannerError,
    PlannerUnavailable,
    parse_decision,
)
from .bridge import (
    BridgePlanner,
    BridgeWatcher,
    answer,
    format_prompt,
    next_seq,
    pending,
    request_name,
    response_name,
)
from .deterministic import Blocker, DeterministicPlanner
from .openai_compat import AIConfig, CallMeta, OpenAICompatPlanner

#: Names accepted by :func:`build_planner`.
PLANNER_NAMES: tuple[str, ...] = ("deterministic", "ai", "bridge")


def build_planner(name: str, **kwargs: Any) -> Planner:
    """Construct a planner by name.

    ``"deterministic"`` -> :class:`DeterministicPlanner`, ``"ai"`` ->
    :class:`OpenAICompatPlanner`, ``"bridge"`` -> :class:`BridgePlanner`
    (requires ``directory=``).  Unknown names raise rather than silently
    falling back, because a typo in a run config must not quietly disable the
    AI layer.
    """

    key = str(name or "").strip().lower()
    if key in ("deterministic", "det", "offline", "none"):
        return DeterministicPlanner(**kwargs)
    if key in ("ai", "llm", "openai", "openai_compat"):
        return OpenAICompatPlanner(**kwargs)
    if key in ("bridge", "file", "human"):
        return BridgePlanner(**kwargs)
    raise PlannerError(
        f"unknown planner {name!r}; supported names: {list(PLANNER_NAMES)} "
        "(aliases: det/offline/none, llm/openai, file/human)")


__all__ = [
    # contract
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
    # implementations
    "DeterministicPlanner",
    "Blocker",
    "OpenAICompatPlanner",
    "AIConfig",
    "CallMeta",
    "BridgePlanner",
    "BridgeWatcher",
    "pending",
    "answer",
    "format_prompt",
    "next_seq",
    "request_name",
    "response_name",
    # factory
    "build_planner",
    "PLANNER_NAMES",
]
