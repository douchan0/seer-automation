"""Machine-readable outcomes for a single Goal Runner invocation.

A subprocess exit code is not proof that the account advanced. In particular
a planner refusal must never be reported as success. The overnight supervisor
consumes this classification, not just subprocess return codes.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


EXIT_CODES = {
    "PROGRESS": 0,
    "DRY_RUN": 0,
    "NO_WORK": 10,
    "BLOCKED": 11,
    "UNVERIFIED_ACTIVITY": 12,
    "FAILED": 1,
    "INTERRUPTED": 130,
}


def classify_run(summary: Mapping[str, Any]) -> str:
    """Classify one run, not completion of the overall Puni goal."""
    reason = str(summary.get("stop_reason") or "").lower()
    status = str(summary.get("status") or "").lower()
    stats = summary.get("stats") or {}
    if not isinstance(stats, Mapping):
        stats = {}
    actions = int(stats.get("actions") or 0)
    battles = int(stats.get("battles") or 0)
    verified_nodes = int(stats.get("verified_nodes") or 0)

    if "crashed:" in reason or "error budget" in reason or status == "failed":
        return "FAILED"
    if "interrupted" in reason or "stop requested" in reason or status == "paused":
        return "INTERRUPTED"
    if "planner refused" in reason or "nothing schedulable" in reason:
        return "NO_WORK"
    if (status == "blocked" or reason.startswith("blocked:")
            or "client is not showing" in reason or "not in the game" in reason
            or ("login" in reason and "screen" in reason)):
        return "BLOCKED"
    if "dry run complete" in reason:
        return "DRY_RUN"
    if verified_nodes > 0 or "goal progressed: node completed" in reason:
        return "PROGRESS"
    if "all executable nodes complete" in reason:
        # All currently executable nodes != entire Puni objective.
        return "UNVERIFIED_ACTIVITY" if actions or battles else "NO_WORK"
    if actions or battles:
        return "UNVERIFIED_ACTIVITY"
    return "NO_WORK"


def run_exit_code(summary: Mapping[str, Any]) -> int:
    """CLI status; return code zero alone is never proof of live progress."""
    return EXIT_CODES[classify_run(summary)]


__all__ = ["EXIT_CODES", "classify_run", "run_exit_code"]
