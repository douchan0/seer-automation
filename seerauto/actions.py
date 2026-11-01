"""Input abstraction with a window-identity guard.

Hard rule from the project (``KNOWN_PROBLEMS.md`` P6): **no click without
verifying that the game window is still where it was and is still the same
window**.  A dragged window used to shift every coordinate silently - the
17:49 incident in ``docs/action-log.md``.

Every action computes its global logical point from ``game_px`` through the
*current* geometry, checks the point is inside the window, re-queries the
window finder, compares with the expected geometry within ``tolerance_pt`` and
only then sends input.  ``dry_run`` records intents without touching the OS,
which is how replay exercises task logic safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from . import geometry as G
from .errors import SafetyViolation, SeerAutoError, WindowNotFound
from .geometry import Point, Rect
from .window import WindowFinder, WindowInfo

DEFAULT_MARGIN_PX = 4.0


# --------------------------------------------------------------------------- #
# input backends
# --------------------------------------------------------------------------- #
class InputBackend(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    def click_global(self, point: Point, clicks: int = 1) -> None: ...

    def move_global(self, point: Point) -> None: ...

    def key(self, code: int) -> None: ...

    def type_text(self, text: str) -> None: ...


class DaemonInput:
    """Input through the JSON-line daemon (``click``/``move``/``key``/``type``)."""

    name = "daemon"

    def __init__(self, client: Any):
        self.client = client

    @property
    def available(self) -> bool:
        return bool(getattr(self.client, "available", False))

    def click_global(self, point: Point, clicks: int = 1) -> None:
        reply = self.client.send("click", x=point.x, y=point.y, n=int(clicks))
        if reply.get("error"):
            raise SeerAutoError(f"daemon click failed: {reply['error']}")

    def move_global(self, point: Point) -> None:
        reply = self.client.send("move", x=point.x, y=point.y)
        if reply.get("error"):
            raise SeerAutoError(f"daemon move failed: {reply['error']}")

    def key(self, code: int) -> None:
        self.client.send("key", code=int(code))

    def type_text(self, text: str) -> None:
        self.client.send("type", text=str(text))


class RecordingInput:
    """Records intents (and can forward them); used by replay and the tests."""

    name = "recording"

    def __init__(self, forward: InputBackend | None = None):
        self.forward = forward
        self.calls: list[tuple[str, Any]] = []

    @property
    def available(self) -> bool:
        return self.forward.available if self.forward is not None else True

    def click_global(self, point: Point, clicks: int = 1) -> None:
        self.calls.append(("click", (point.as_tuple(), clicks)))
        if self.forward is not None:
            self.forward.click_global(point, clicks)

    def move_global(self, point: Point) -> None:
        self.calls.append(("move", point.as_tuple()))
        if self.forward is not None:
            self.forward.move_global(point)

    def key(self, code: int) -> None:
        self.calls.append(("key", int(code)))
        if self.forward is not None:
            self.forward.key(code)

    def type_text(self, text: str) -> None:
        self.calls.append(("type", str(text)))
        if self.forward is not None:
            self.forward.type_text(text)


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Action:
    """Base class for task-emitted actions."""

    kind: str = "action"
    label: str = ""


@dataclass(frozen=True)
class ClickAction(Action):
    """Click one or more named coordinates (``tasks.battle.coordinates`` keys)."""

    targets: tuple[str, ...] = ()
    clicks_per_target: int = 1
    kind: str = "click"


@dataclass(frozen=True)
class ClickPointAction(Action):
    """Click an explicit game_px point (computed by a task)."""

    x: float = 0.0
    y: float = 0.0
    clicks: int = 1
    kind: str = "click_point"


@dataclass(frozen=True)
class KeyAction(Action):
    code: int = 0
    kind: str = "key"


@dataclass(frozen=True)
class MoveAction(Action):
    x: float = 0.0
    y: float = 0.0
    kind: str = "move"


@dataclass(frozen=True)
class StopAction(Action):
    reason: str = "task finished"
    kind: str = "stop"


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ActionRecord:
    kind: str
    label: str
    game_px: tuple[float, float] | None
    global_pt: tuple[float, float] | None
    verified: str  # live | static | dry-run | none
    dry_run: bool
    at_ns: int
    duration_us: float
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "game_px": [round(v, 1) for v in self.game_px] if self.game_px else None,
            "global_pt": [round(v, 2) for v in self.global_pt] if self.global_pt else None,
            "verified": self.verified,
            "dry_run": self.dry_run,
            "duration_us": round(self.duration_us, 1),
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# guarded actions
# --------------------------------------------------------------------------- #
class Actions:
    """Verifies window identity/geometry, then (optionally) sends input."""

    def __init__(
        self,
        backend: InputBackend,
        expected_window: WindowInfo,
        finder: WindowFinder | None = None,
        tolerance_pt: float = 2.0,
        dry_run: bool = False,
        perf: Any | None = None,
        clock=None,
        margin_px: float = DEFAULT_MARGIN_PX,
    ):
        self.backend = backend
        self.expected = expected_window
        self.finder = finder
        self.tolerance_pt = float(tolerance_pt)
        self.dry_run = dry_run
        self.perf = perf
        self.clock = clock
        self.margin_px = float(margin_px)
        self.records: list[ActionRecord] = []
        self.verify_count = 0
        self.refused = 0
        self._last_verified: WindowInfo | None = None
        self.geometry: G.WindowGeometry = expected_window.geometry

    # -- window guard ------------------------------------------------------ #
    @property
    def can_verify_live(self) -> bool:
        return self.finder is not None

    def verify_window(self) -> str:
        """Re-query the window and refuse on any geometry/identity change."""
        if self.dry_run:
            return "dry-run"
        if self.finder is None:
            # A static target is all we have (degraded mode); make it explicit
            # in every record instead of pretending we verified anything.
            return "static"
        found = self.finder.find()  # raises WindowNotFound when gone
        self.verify_count += 1
        self.expected.assert_matches(found, tol_pt=self.tolerance_pt)
        self._last_verified = found
        # follow sub-pixel window movement inside tolerance so clicks stay put
        self.geometry = found.geometry
        return "live"

    def _check_inside(self, point_px: Point) -> None:
        full = Rect(0.0, 0.0, *self.geometry.pixel_size.as_tuple())
        inner = full.inset(self.margin_px)
        if not inner.contains(point_px):
            raise SafetyViolation(
                f"refusing to click outside the game window: game_px=({point_px.x:.0f},{point_px.y:.0f}) "
                f"not inside {inner.as_int_tuple()} (window pixel size {self.geometry.pixel_size.w:.0f}x"
                f"{self.geometry.pixel_size.h:.0f})"
            )

    # -- primitives -------------------------------------------------------- #
    def click_game_px(self, x: float, y: float, clicks: int = 1, label: str = "") -> ActionRecord:
        import time as _time

        started = _time.perf_counter()
        point_px = Point(float(x), float(y))
        self._check_inside(point_px)
        verified = self.verify_window()
        global_pt = self.geometry.global_pt_from_game_px(point_px.x, point_px.y)
        duration_us = (_time.perf_counter() - started) * 1e6
        if self.dry_run:
            record = ActionRecord(
                kind="click", label=label, game_px=point_px.as_tuple(),
                global_pt=global_pt.as_tuple(), verified=verified, dry_run=True,
                at_ns=int(self.clock() if self.clock else _time.monotonic_ns()),
                duration_us=duration_us,
            )
        else:
            self.backend.click_global(global_pt, int(clicks))
            record = ActionRecord(
                kind="click", label=label, game_px=point_px.as_tuple(),
                global_pt=global_pt.as_tuple(), verified=verified, dry_run=False,
                at_ns=int(self.clock() if self.clock else _time.monotonic_ns()),
                duration_us=(_time.perf_counter() - started) * 1e6,
            )
        self.records.append(record)
        return record

    def move_game_px(self, x: float, y: float, label: str = "") -> ActionRecord:
        import time as _time

        started = _time.perf_counter()
        point_px = Point(float(x), float(y))
        self._check_inside(point_px)
        verified = self.verify_window()
        global_pt = self.geometry.global_pt_from_game_px(point_px.x, point_px.y)
        if not self.dry_run:
            self.backend.move_global(global_pt)
        record = ActionRecord(
            kind="move", label=label, game_px=point_px.as_tuple(),
            global_pt=global_pt.as_tuple(), verified=verified, dry_run=self.dry_run,
            at_ns=int(self.clock() if self.clock else 0.0),
            duration_us=(_time.perf_counter() - started) * 1e6,
        )
        self.records.append(record)
        return record

    def key(self, code: int, label: str = "") -> ActionRecord:
        import time as _time

        started = _time.perf_counter()
        verified = self.verify_window()
        if not self.dry_run:
            self.backend.key(int(code))
        record = ActionRecord(
            kind="key", label=label, game_px=None, global_pt=None,
            verified=verified, dry_run=self.dry_run,
            at_ns=int(self.clock() if self.clock else 0.0),
            duration_us=(_time.perf_counter() - started) * 1e6,
        )
        self.records.append(record)
        return record

    # -- task-facing dispatch --------------------------------------------- #
    def execute(self, action: Action, config: Any) -> list[ActionRecord]:
        """Execute a task action. ``StopAction`` is handled by the executor."""
        if isinstance(action, StopAction):
            raise SafetyViolation("StopAction must be handled by the executor")
        out: list[ActionRecord] = []
        if isinstance(action, ClickAction):
            for name in action.targets:
                rect = config.game_px(name)
                out.append(
                    self.click_game_px(
                        rect.x, rect.y, clicks=action.clicks_per_target,
                        label=f"{action.label or action.kind}:{name}",
                    )
                )
        elif isinstance(action, ClickPointAction):
            out.append(self.click_game_px(action.x, action.y, clicks=action.clicks, label=action.label))
        elif isinstance(action, MoveAction):
            out.append(self.move_game_px(action.x, action.y, label=action.label))
        elif isinstance(action, KeyAction):
            out.append(self.key(action.code, label=action.label))
        else:
            raise SafetyViolation(f"unsupported action type {type(action).__name__}")
        return out
