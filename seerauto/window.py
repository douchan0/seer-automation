"""Window discovery, pinning and geometry.

Why this module exists
----------------------
The previous tooling hardcoded ``scale = 2.0`` and never re-checked the window
(``KNOWN_PROBLEMS.md`` P6).  A dragged window silently shifts every click.
Here every actionable object carries a :class:`WindowInfo` and every input has
to pass :meth:`WindowInfo.matches` first.

Discovery is layered, and every layer degrades honestly:

1. :class:`DaemonWindowFinder` - the JSON-line daemon (cheap, authoritative).
2. :class:`ExternalJSONWindowFinder` - any command printing a window list as
   JSON, e.g. the existing ``scripts/seerctl wl`` (CGWindowListCopyWindowInfo;
   note that only the *image* APIs were removed in macOS 15, the list API is
   still available).
3. :class:`StaticWindowFinder` - explicitly supplied geometry (tests, or a
   user-pinned window when enumeration is denied).

None of these need a running game; when nothing is found ``WindowNotFound`` is
raised with the reasons from every layer, which is exactly what
``python3 -m seerauto doctor`` prints.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from . import geometry as G
from .errors import WindowMoved, WindowNotFound

#: Repository root (the package lives in ``<repo>/seerauto``).
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_OWNER_ALIASES = ("seergame", "xin计划", "xin 计划", "xin计划")


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WindowInfo:
    """A concrete game window: identity + geometry."""

    window_id: int
    owner: str
    title: str
    geometry: G.WindowGeometry
    source: str = "unknown"

    # -- identity ---------------------------------------------------------- #
    @property
    def size_pt(self) -> G.Size:
        return self.geometry.size_pt

    @property
    def pixel_size(self) -> G.Size:
        return self.geometry.pixel_size

    def describe(self) -> str:
        g = self.geometry
        return (
            f"id={self.window_id} owner={self.owner!r} title={self.title!r} "
            f"origin=({g.origin_pt.x:.1f},{g.origin_pt.y:.1f})pt "
            f"size={g.size_pt.w:.0f}x{g.size_pt.h:.0f}pt scale={g.scale:.2f} "
            f"source={self.source}"
        )

    def matches(self, other: "WindowInfo", tol_pt: float = 2.0) -> bool:
        """True when ``other`` is the same window in the same place.

        The window id is only compared when both sides know it (a re-created
        window id is still the same logical window, so id mismatch alone is
        not fatal - geometry is the safety property that matters).
        """
        if self.window_id > 0 and other.window_id > 0 and self.window_id != other.window_id:
            return False
        return self.geometry.is_close(other.geometry, tol_pt=tol_pt)

    def assert_matches(self, other: "WindowInfo", tol_pt: float = 2.0) -> None:
        if not self.matches(other, tol_pt=tol_pt):
            raise WindowMoved(
                "window geometry/identity changed beyond tolerance "
                f"({tol_pt}pt): expected [{self.describe()}], found [{other.describe()}]"
            )

    def with_geometry(self, geo: G.WindowGeometry) -> "WindowInfo":
        return replace(self, geometry=geo)


# --------------------------------------------------------------------------- #
# finder protocol
# --------------------------------------------------------------------------- #
class WindowFinder(Protocol):
    """Finds game windows.  ``find`` raises :class:`WindowNotFound`."""

    name: str

    def find(self) -> WindowInfo: ...

    def list_windows(self) -> list[WindowInfo]: ...


FETCH_TIMEOUT_S = 3.0


def _normalise_entry(raw: Mapping[str, Any], scale: float, source: str) -> WindowInfo | None:
    """Convert one raw JSON window record into a :class:`WindowInfo`."""
    bounds = raw.get("bounds") or {}
    try:
        x = float(bounds.get("x", raw.get("x", 0.0)))
        y = float(bounds.get("y", raw.get("y", 0.0)))
        w = float(bounds.get("w", raw.get("w", 0.0)))
        h = float(bounds.get("h", raw.get("h", 0.0)))
        window_id = int(raw.get("id", raw.get("window_id", -1)))
    except (TypeError, ValueError):
        return None
    if w <= 1 or h <= 1:
        return None
    layer = raw.get("layer")
    if layer is not None and int(layer) != 0:
        return None
    return WindowInfo(
        window_id=window_id,
        owner=str(raw.get("owner", raw.get("owner_name", ""))),
        title=str(raw.get("title", raw.get("name", ""))),
        geometry=G.WindowGeometry(origin_pt=G.Point(x, y), size_pt=G.Size(w, h), scale=scale),
        source=source,
    )


def _owner_matches(owner: str, title: str, aliases: Sequence[str]) -> bool:
    haystack = f"{owner}|{title}".lower()
    return any(alias.lower() in haystack for alias in aliases)


def _parse_json_payload(text: str) -> Any:
    """Parse a window list from stdout that may be one JSON array, or JSON lines."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # try last parsable line (commands sometimes log before the payload)
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


class ExternalJSONWindowFinder:
    """Window list from an external command printing JSON.

    Default command is the existing ``scripts/seerctl wl`` (a compiled Swift
    helper using ``CGWindowListCopyWindowInfo``, which still works on macOS 15
    even though the image APIs are gone).  Any command with the same output
    shape can be substituted via config:

    ``[{"id": 123, "owner": "SeerGame", "title": "...", "layer": 0,
        "bounds": {"x": 300, "y": 120, "w": 1149, "h": 714}}, ...]``
    """

    def __init__(
        self,
        command: Sequence[str] | None = None,
        owner_aliases: Sequence[str] = DEFAULT_OWNER_ALIASES,
        scale: float = G.RETINA_SCALE,
        timeout_s: float = FETCH_TIMEOUT_S,
    ):
        self.command = list(command) if command else self.default_command()
        self.owner_aliases = tuple(owner_aliases)
        self.scale = scale
        self.timeout_s = timeout_s
        self._last_error: str | None = None
        self.name = "external-json"

    @staticmethod
    def default_command() -> list[str]:
        seerctl = REPO_ROOT / "scripts" / "seerctl"
        if seerctl.exists() and os.access(seerctl, os.X_OK):
            return [str(seerctl), "wl"]
        found = shutil.which("seerctl")
        return [found, "wl"] if found else []

    @property
    def available(self) -> bool:
        return bool(self.command) and Path(self.command[0]).exists()

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _list_raw(self) -> list[WindowInfo]:
        if not self.command:
            self._last_error = "no window-enumeration command available"
            return []
        try:
            proc = subprocess.run(
                self.command,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - host dependent
            self._last_error = f"command failed: {exc}"
            return []
        payload = _parse_json_payload(proc.stdout or "")
        if payload is None:
            self._last_error = f"unparsable window list (rc={proc.returncode})"
            return []
        if isinstance(payload, Mapping):
            payload = payload.get("windows", [])
        if not isinstance(payload, list):
            self._last_error = "window list is not a JSON array"
            return []
        out: list[WindowInfo] = []
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            info = _normalise_entry(item, self.scale, source=self.name)
            if info is not None:
                out.append(info)
        self._last_error = None
        return out

    def list_windows(self) -> list[WindowInfo]:
        return self._list_raw()

    def find(self) -> WindowInfo:
        candidates = [
            w
            for w in self._list_raw()
            if _owner_matches(w.owner, w.title, self.owner_aliases)
        ]
        if not candidates:
            detail = f" ({self._last_error})" if self._last_error else ""
            raise WindowNotFound(
                f"no window matching {self.owner_aliases} via {self.command}{detail}"
            )
        # largest matching window wins (helper/utility windows are small)
        return max(candidates, key=lambda w: w.size_pt.area)


class DaemonWindowFinder:
    """Window lookup through the JSON-line daemon client (``op=win``)."""

    name = "daemon"

    def __init__(self, client: Any, scale: float = G.RETINA_SCALE):
        self.client = client
        self.scale = scale
        self._last_error: str | None = None

    @property
    def available(self) -> bool:
        return bool(getattr(self.client, "available", False))

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def list_windows(self) -> list[WindowInfo]:
        try:
            reply = self.client.win()
        except Exception as exc:  # noqa: BLE001 - surface as reason, never crash doctor
            self._last_error = str(exc)
            return []
        if not reply or reply.get("error"):
            self._last_error = (reply or {}).get("error", "daemon returned no data")
            return []
        return self._windows_from_reply(reply)

    def find(self) -> WindowInfo:
        windows = self.list_windows()
        if not windows:
            raise WindowNotFound(f"daemon reports no SeerGame window ({self._last_error or 'empty'})")
        return max(windows, key=lambda w: w.size_pt.area)

    def _windows_from_reply(self, reply: Mapping[str, Any]) -> list[WindowInfo]:
        # daemon may reply with one window {id,x,y,w,h} or {"windows": [...]}
        items = reply.get("windows") if isinstance(reply.get("windows"), list) else [reply]
        out: list[WindowInfo] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            info = _normalise_entry(item, self.scale, source=self.name)
            if info is not None:
                out.append(info)
        return out


class StaticWindowFinder:
    """Explicit geometry - used by tests and by a ``--window`` escape hatch."""

    name = "static"

    def __init__(self, info: WindowInfo | G.WindowGeometry):
        self.info = info if isinstance(info, WindowInfo) else WindowInfo(
            window_id=-1, owner="SeerGame", title="static", geometry=info, source="static"
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def last_error(self) -> str | None:
        return None

    def list_windows(self) -> list[WindowInfo]:
        return [self.info]

    def find(self) -> WindowInfo:
        return self.info


# --------------------------------------------------------------------------- #
# composite discovery
# --------------------------------------------------------------------------- #
@dataclass
class DiscoveryReport:
    """Diagnostics from :func:`find_window` - printed by ``seerauto doctor``."""

    found: WindowInfo | None
    attempts: list[tuple[str, str, int]]  # (finder name, outcome, n_windows)
    errors: list[tuple[str, str]]  # (finder name, error)

    def as_lines(self) -> list[str]:
        lines: list[str] = []
        for name, outcome, count in self.attempts:
            lines.append(f"  - {name}: {outcome} ({count} candidate window(s))")
        for name, err in self.errors:
            lines.append(f"  - {name}: NOT FOUND - {err}")
        return lines


def find_window(
    finders: Iterable[WindowFinder],
    owner_aliases: Sequence[str] = DEFAULT_OWNER_ALIASES,
) -> tuple[WindowInfo, DiscoveryReport]:
    """Try every finder in order; return the first hit plus a report."""
    report = DiscoveryReport(found=None, attempts=[], errors=[])
    for finder in finders:
        try:
            windows = finder.list_windows()
        except Exception as exc:  # noqa: BLE001
            windows = []
            report.errors.append((finder.name, str(exc)))
            continue
        matching = [w for w in windows if _owner_matches(w.owner, w.title, owner_aliases)]
        if matching:
            best = max(matching, key=lambda w: w.size_pt.area)
            report.attempts.append((finder.name, "window found", len(matching)))
            report.found = best
            return best, report
        reason = getattr(finder, "last_error", None) or "no matching window"
        report.attempts.append((finder.name, f"none ({reason})", len(windows)))
        report.errors.append((finder.name, reason))
    raise WindowNotFound(
        "SeerGame window not found. Tried: "
        + "; ".join(f"{name}: {err}" for name, err in report.errors)
    )


# --------------------------------------------------------------------------- #
# pinning
# --------------------------------------------------------------------------- #
class WindowPinner(Protocol):
    """Object able to move/resize the game window (daemon implements it)."""

    def pin(self, rect_pt: G.Rect) -> Mapping[str, Any]: ...


def pin_window(
    pinner: WindowPinner,
    rect_pt: G.Rect,
    scale: float = G.RETINA_SCALE,
    owner: str = "SeerGame",
    title: str = "pinned",
) -> WindowInfo:
    """Ask the backend to move the window to ``rect_pt`` and return its new info."""
    reply = pinner.pin(rect_pt)
    if reply.get("error"):
        raise WindowNotFound(f"pin failed: {reply['error']}")
    geo = G.WindowGeometry(
        origin_pt=G.Point(float(reply.get("x", rect_pt.x)), float(reply.get("y", rect_pt.y))),
        size_pt=G.Size(float(reply.get("w", rect_pt.w)), float(reply.get("h", rect_pt.h))),
        scale=scale,
    )
    return WindowInfo(
        window_id=int(reply.get("id", -1)), owner=owner, title=title, geometry=geo, source="pin"
    )


# --------------------------------------------------------------------------- #
# scale probing
# --------------------------------------------------------------------------- #
def probe_capture_scale(grab_png_size, rect_pt: G.Rect, fallback: float = G.RETINA_SCALE) -> float:
    """Infer the capture scale from one real capture.

    ``grab_png_size`` is a callable returning ``(width_px, height_px)`` for a
    capture of ``rect_pt`` (a *logical point* rect).  Returns
    ``width_px / rect_pt.w`` rounded to the nearest 0.25.  Never raises: if the
    probe fails the documented fallback is returned, because a wrong guess must
    be loudly visible in ``doctor`` output, not crash discovery.
    """
    if rect_pt.w <= 0:
        return fallback
    try:
        w_px, _ = grab_png_size()
    except Exception:  # noqa: BLE001 - probe is best effort
        return fallback
    if not w_px:
        return fallback
    return round((float(w_px) / rect_pt.w) * 4.0) / 4.0


def default_scale_from_env() -> float:
    try:
        return float(os.environ.get("SEERAUTO_SCALE", G.RETINA_SCALE))
    except ValueError:
        return G.RETINA_SCALE
