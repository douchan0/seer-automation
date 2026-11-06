"""Dual-mode control plane — the local brainstem of the night, no model in the loop.

Nothing in this package imports an LLM/agent SDK, opens a socket, or touches the game
client: it is stdlib plus :mod:`seerauto.runner.outcome` for the legacy exit-code
vocabulary.  That is a hard architectural rule (AGENTS.md §7), not a preference — the
night must survive the agent layer being asleep, rate-limited or dead.

The plane keeps five facts about the night **separate** (see
:mod:`seerauto.dual.facts`):

    GOAL_ALIVE        the long-horizon goal is still declared active
    AGENT_DECIDING    the DSH agent is in a model turn right now
    SUPERVISOR_RUNNING the supervisor loop is alive
    EXECUTOR_ALIVE    a local executor is alive
    GAME_PROGRESSING  verified progress was *proven* recently

and it speaks one outcome vocabulary of eight members (see
:mod:`seerauto.dual.outcome`):

    PROGRESS NO_WORK BLOCKED FAILED UNVERIFIED_ACTIVITY PAUSED INTERRUPTED
    DEADLINE_REACHED

Layout — one job per module, in dependency order::

    modes.py     Mode/Policy/SwitchReason + the pure resolve() rule table
    outcome.py   the eight outcomes, legacy mapping, batch classifier
    ledger.py    append-only JSONL + atomic checkpoint
    state.py     derive the night's state from the ledgers
    facts.py     the five independent facts, each with evidence and UNKNOWN
    watchdog.py  local sub-second loop, wake criteria, cooldowns, suppression counts
    switch.py    auditable mode switches with reason cooldown + minimum dwell

The CLI over all of it is ``tools/overnight_control.py``.
"""

from __future__ import annotations

from . import facts, ledger, modes, outcome, state, switch, watchdog  # noqa: F401

__all__ = ["facts", "ledger", "modes", "outcome", "state", "switch", "watchdog"]
