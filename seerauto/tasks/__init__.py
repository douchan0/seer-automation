"""Task policies.

A task is a small policy object: it looks at a :class:`seerauto.executor.TickContext`
and returns an :class:`seerauto.actions.Action` (or ``None`` to wait).  It never
touches pixels or input itself, which keeps it unit-testable and safe in replay.
"""

from .battle import BattleStats, BattleTask

__all__ = ["BattleTask", "BattleStats"]
