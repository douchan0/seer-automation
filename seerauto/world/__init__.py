"""What the account looks like, and where that belief is persisted.

Two modules with deliberately separate jobs:

* :mod:`seerauto.world.model` -- plain data (snapshots, facts and the evidence
  that justifies them).  No I/O, so it can be diffed, replayed and copied.
* :mod:`seerauto.world.store` -- the only place that writes it to disk
  (stdlib ``sqlite3``, crash-safe, one transaction per multi-row write).

Importing this package pulls in nothing heavier than ``sqlite3``.
"""

from .model import (
    Evidence,
    Fact,
    GameWorldState,
    PetState,
    ResourceState,
    Scene,
    Source,
    SptEntry,
    WindowState,
    empty_world,
    now_iso,
    with_fact,
)
from .store import StoreError, WorldStore

__all__ = [
    # model
    "Evidence",
    "Fact",
    "GameWorldState",
    "PetState",
    "ResourceState",
    "Scene",
    "Source",
    "SptEntry",
    "WindowState",
    "empty_world",
    "now_iso",
    "with_fact",
    # store
    "StoreError",
    "WorldStore",
]
