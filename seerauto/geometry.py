"""Coordinate spaces and conversions.

seerauto deals with four spaces.  Keeping them named and explicit is the only
way to avoid the classic Retina bug (``click at 1151,1281`` landing in the wrong
quadrant).  All helpers are pure and cheap.

======================  ===================================================
name                    meaning
======================  ===================================================
``pt``                  global logical points - the unit macOS CGEvent,
                        CGWindow and AX use.  Screen is 2940x1912 pt.
``game_px``             window-local *pixels of a 2x reference capture*
                        (2298x1428).  This is the space used by
                        ``docs/state.md``, so documented coordinates can be
                        pasted into config unchanged.
``frame_px``            pixels of whatever image a capture backend returned
                        (could be 2298x1428, 1150x715, 600x373, ...).
``ref_stage_px``        canonical *letterbox-free playfield* pixels inside a
                        window capture: reference size 2092x1220, offset
                        (102,128) inside the 2x reference capture.  Vision
                        rules and templates live in this space, which makes
                        them independent of window decoration, DPI and the
                        black letterbox bars.
======================  ===================================================

The reference layout was measured on real screenshots: every artifact from the
game (2298x1428, 1150x715, 998x620, 600x373, ...) is the same window layout
scaled, so a single reference plus proportional stage mapping lines them up.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Tuple

Number = float | int

# Window logical size of the SeerGame client and the Retina factor of this host.
WINDOW_PT = (1149.0, 714.0)
RETINA_SCALE = 2.0

#: Reference capture pixel size (window at 2x).
CAPTURE_PX = (2298.0, 1428.0)

#: Playfield (game stage) rect inside the reference capture, in reference px.
#: Measured from ``artifacts/game.png``: title bar 0..55, full-width black
#: margin 56..127, stage 102..2193 x 128..1347 (2092x1220), right margin
#: 2194..2297.  All artifacts share this layout proportionally.
STAGE_PX = (102.0, 128.0, 2092.0, 1220.0)


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def __add__(self, other: "Point") -> "Point":
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: "Point") -> "Point":
        return Point(self.x - other.x, self.y - other.y)

    def scaled(self, k: float) -> "Point":
        return Point(self.x * k, self.y * k)

    def as_tuple(self) -> Tuple[float, float]:
        return (self.x, self.y)

    def rounded(self) -> "Point":
        return Point(float(round(self.x)), float(round(self.y)))

    def distance_to(self, other: "Point") -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


@dataclass(frozen=True, slots=True)
class Size:
    w: float
    h: float

    @property
    def area(self) -> float:
        return self.w * self.h

    @property
    def aspect(self) -> float:
        return self.w / self.h if self.h else float("inf")

    def as_tuple(self) -> Tuple[float, float]:
        return (self.w, self.h)


@dataclass(frozen=True, slots=True)
class Rect:
    """Axis aligned rectangle; ``x``/``y`` is the top-left corner."""

    x: float
    y: float
    w: float
    h: float

    # -- constructors ------------------------------------------------------ #
    @classmethod
    def from_ltrb(cls, left: Number, top: Number, right: Number, bottom: Number) -> "Rect":
        return cls(float(left), float(top), float(right) - float(left), float(bottom) - float(top))

    @classmethod
    def from_size(cls, x: Number, y: Number, size: Size) -> "Rect":
        return cls(float(x), float(y), size.w, size.h)

    @classmethod
    def from_norm(cls, norm: Tuple[float, float, float, float], into: "Rect | None" = None) -> "Rect":
        """Build a rect from normalised ``(x0, y0, x1, y1)`` inside ``into``.

        ``None`` target means the unit square (i.e. result is still normalised
        but expressed as a :class:`Rect`).
        """
        base = into or Rect(0.0, 0.0, 1.0, 1.0)
        x0, y0, x1, y1 = (float(v) for v in norm)
        return cls(
            base.x + x0 * base.w,
            base.y + y0 * base.h,
            (x1 - x0) * base.w,
            (y1 - y0) * base.h,
        )

    # -- derived values ---------------------------------------------------- #
    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def center(self) -> Point:
        return Point(self.cx, self.cy)

    @property
    def size(self) -> Size:
        return Size(self.w, self.h)

    @property
    def is_empty(self) -> bool:
        return self.w <= 0 or self.h <= 0

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return (self.x, self.y, self.w, self.h)

    def as_int_tuple(self) -> Tuple[int, int, int, int]:
        return (
            int(round(self.x)),
            int(round(self.y)),
            int(round(self.w)),
            int(round(self.h)),
        )

    def as_ltrb(self) -> Tuple[float, float, float, float]:
        return (self.x, self.y, self.x2, self.y2)

    # -- operations -------------------------------------------------------- #
    def offset(self, dx: Number, dy: Number) -> "Rect":
        return Rect(self.x + dx, self.y + dy, self.w, self.h)

    def translate(self, p: Point) -> "Rect":
        return Rect(self.x + p.x, self.y + p.y, self.w, self.h)

    def scale(self, kx: float, ky: float | None = None) -> "Rect":
        ky = kx if ky is None else ky
        return Rect(self.x * kx, self.y * ky, self.w * kx, self.h * ky)

    def inset(self, dx: Number, dy: Number | None = None) -> "Rect":
        dy = dx if dy is None else dy
        return Rect(self.x + dx, self.y + dy, max(0.0, self.w - 2 * dx), max(0.0, self.h - 2 * dy))

    def clamp_to(self, bounds: "Rect") -> "Rect":
        """Intersection with ``bounds`` (empty rect if disjoint)."""
        x0 = max(self.x, bounds.x)
        y0 = max(self.y, bounds.y)
        x1 = min(self.x2, bounds.x2)
        y1 = min(self.y2, bounds.y2)
        if x1 <= x0 or y1 <= y0:
            return Rect(x0, y0, 0.0, 0.0)
        return Rect(x0, y0, x1 - x0, y1 - y0)

    def contains(self, p: Point) -> bool:
        return self.x <= p.x < self.x2 and self.y <= p.y < self.y2

    def contains_rect(self, other: "Rect", tol: float = 0.0) -> bool:
        return (
            other.x >= self.x - tol
            and other.y >= self.y - tol
            and other.x2 <= self.x2 + tol
            and other.y2 <= self.y2 + tol
        )

    def iou(self, other: "Rect") -> float:
        inter = self.clamp_to(other).area
        union = self.area + other.area - inter
        return inter / union if union else 0.0


# --------------------------------------------------------------------------- #
# unit conversions
# --------------------------------------------------------------------------- #
def logical_to_pixel(value: Number, scale: float = RETINA_SCALE) -> float:
    """Convert logical points -> device pixels."""
    return float(value) * scale


def pixel_to_logical(value: Number, scale: float = RETINA_SCALE) -> float:
    """Convert device pixels -> logical points."""
    return float(value) / scale


def rect_logical_to_pixel(rect: Rect, scale: float = RETINA_SCALE) -> Rect:
    return rect.scale(scale)


def rect_pixel_to_logical(rect: Rect, scale: float = RETINA_SCALE) -> Rect:
    return rect.scale(1.0 / scale)


def clamp(value: Number, lo: Number, hi: Number) -> float:
    return max(float(lo), min(float(hi), float(value)))


# --------------------------------------------------------------------------- #
# window geometry
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class WindowGeometry:
    """Geometry of the game window as the OS reports it, in global points."""

    origin_pt: Point = Point(0.0, 0.0)
    size_pt: Size = Size(*WINDOW_PT)
    scale: float = RETINA_SCALE

    # -- derived ----------------------------------------------------------- #
    @property
    def pixel_size(self) -> Size:
        return Size(self.size_pt.w * self.scale, self.size_pt.h * self.scale)

    @property
    def window_rect_pt(self) -> Rect:
        return Rect(self.origin_pt.x, self.origin_pt.y, self.size_pt.w, self.size_pt.h)

    def is_close(self, other: "WindowGeometry", tol_pt: float = 2.0) -> bool:
        return (
            self.origin_pt.distance_to(other.origin_pt) <= tol_pt
            and abs(self.size_pt.w - other.size_pt.w) <= tol_pt
            and abs(self.size_pt.h - other.size_pt.h) <= tol_pt
            and abs(self.scale - other.scale) <= 0.01
        )

    # -- game_px <-> global points ---------------------------------------- #
    def global_pt_from_game_px(self, px: Number, py: Number) -> Point:
        """game_px (window-local, 2x) -> global logical points (CGEvent input)."""
        return Point(
            self.origin_pt.x + float(px) / self.scale,
            self.origin_pt.y + float(py) / self.scale,
        )

    def game_px_from_global_pt(self, p: Point) -> Point:
        """global logical points -> window-local game_px."""
        return Point(
            (p.x - self.origin_pt.x) * self.scale,
            (p.y - self.origin_pt.y) * self.scale,
        )

    def global_rect_from_game_px(self, rect: Rect) -> Rect:
        """game_px rect -> global point rect (use for ``screencapture -R``)."""
        return Rect(
            self.origin_pt.x + rect.x / self.scale,
            self.origin_pt.y + rect.y / self.scale,
            rect.w / self.scale,
            rect.h / self.scale,
        )

    # -- stage mapping ----------------------------------------------------- #
    def stage_map(self, frame_size_px: Size | None = None, ref: "ReferenceSpace | None" = None) -> "StageMap":
        """Map the canonical stage onto a window-sized frame.

        ``frame_size_px`` defaults to this window's own pixel size.  The
        reference layout is proportional, which is why fixtures captured at
        other resolutions still line up.
        """
        ref = ref or DEFAULT_REFERENCE
        size = frame_size_px or self.pixel_size
        stage = ref.stage_px.scale(size.w / ref.capture_px.w, size.h / ref.capture_px.h)
        return StageMap(
            stage_px=stage,
            ref_size=ref.stage_px.size,
            ref_offset=Point(ref.stage_px.x, ref.stage_px.y),
        )


# --------------------------------------------------------------------------- #
# reference space
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class ReferenceSpace:
    """Canonical layout shared by every capture resolution."""

    capture_px: Size = Size(*CAPTURE_PX)
    stage_px: Rect = Rect(*STAGE_PX)
    window_pt: Size = Size(*WINDOW_PT)
    scale: float = RETINA_SCALE

    def geometry(self, origin_pt: Point = Point(0.0, 0.0)) -> WindowGeometry:
        return WindowGeometry(origin_pt=origin_pt, size_pt=self.window_pt, scale=self.scale)


DEFAULT_REFERENCE = ReferenceSpace()


@dataclass(frozen=True, slots=True)
class StageMap:
    """Linear map between canonical stage pixels and one frame's pixels.

    Canonical stage space is the letterbox-free playfield with origin at the
    stage's top-left corner and size ``ref_size`` (2092x1220 for the 2x
    reference capture).  Templates and vision rules live here, so they are
    independent of window decoration, Retina factor and black bars.

    ``stage_px`` is where the stage sits *inside the frame being processed*;
    ``ref_offset`` is where the stage sits inside a reference *window capture*
    (i.e. what ``docs/state.md`` coordinates are relative to).
    """

    stage_px: Rect
    ref_size: Size = Size(*STAGE_PX[2:])
    ref_offset: Point = Point(*STAGE_PX[:2])

    # -- scales ------------------------------------------------------------ #
    @property
    def scale_x(self) -> float:
        return self.stage_px.w / self.ref_size.w

    @property
    def scale_y(self) -> float:
        return self.stage_px.h / self.ref_size.h

    @property
    def scale(self) -> float:
        """Mean scale, for template resizing decisions."""
        return (self.scale_x + self.scale_y) / 2.0

    # -- forward: canonical stage px -> frame px --------------------------- #
    def frame_from_canon_rect(self, rect: Rect) -> Rect:
        return Rect(
            self.stage_px.x + rect.x * self.scale_x,
            self.stage_px.y + rect.y * self.scale_y,
            rect.w * self.scale_x,
            rect.h * self.scale_y,
        )

    def frame_from_canon_point(self, p: Point) -> Point:
        return Point(
            self.stage_px.x + p.x * self.scale_x,
            self.stage_px.y + p.y * self.scale_y,
        )

    def frame_size_from_canon(self, size: Size) -> Size:
        return Size(size.w * self.scale_x, size.h * self.scale_y)

    # -- inverse: frame px -> canonical stage px --------------------------- #
    def canon_from_frame_rect(self, rect: Rect) -> Rect:
        return Rect(
            (rect.x - self.stage_px.x) / self.scale_x,
            (rect.y - self.stage_px.y) / self.scale_y,
            rect.w / self.scale_x,
            rect.h / self.scale_y,
        )

    def canon_from_frame_point(self, p: Point) -> Point:
        return Point(
            (p.x - self.stage_px.x) / self.scale_x,
            (p.y - self.stage_px.y) / self.scale_y,
        )

    # -- normalised helpers (0..1 of the stage) ---------------------------- #
    def canon_rect_from_norm(self, norm: Tuple[float, float, float, float]) -> Rect:
        return Rect.from_norm(norm, into=Rect(0.0, 0.0, self.ref_size.w, self.ref_size.h))

    def frame_rect_from_norm(self, norm: Tuple[float, float, float, float]) -> Rect:
        return self.frame_from_canon_rect(self.canon_rect_from_norm(norm))

    # -- reference window capture (game_px / docs space) <-> canonical ----- #
    def canon_from_game_px(self, rect: Rect) -> Rect:
        return rect.offset(-self.ref_offset.x, -self.ref_offset.y)

    def game_px_from_canon(self, rect: Rect) -> Rect:
        return rect.offset(self.ref_offset.x, self.ref_offset.y)

    def frame_rect_from_game_px(self, rect: Rect) -> Rect:
        return self.frame_from_canon_rect(self.canon_from_game_px(rect))

    def game_px_from_frame_rect(self, rect: Rect) -> Rect:
        return self.game_px_from_canon(self.canon_from_frame_rect(rect))

    # -- derivations ------------------------------------------------------- #
    def translate(self, dx: float, dy: float) -> "StageMap":
        return StageMap(
            stage_px=self.stage_px.offset(dx, dy),
            ref_size=self.ref_size,
            ref_offset=self.ref_offset,
        )

    def with_stage_px(self, stage_px: Rect) -> "StageMap":
        return StageMap(stage_px=stage_px, ref_size=self.ref_size, ref_offset=self.ref_offset)

    @classmethod
    def for_capture(
        cls, stage_rect_in_capture: Rect, ref: "ReferenceSpace" = DEFAULT_REFERENCE
    ) -> "StageMap":
        """Stage map for a frame whose pixels are in reference capture space."""
        return cls(
            stage_px=stage_rect_in_capture,
            ref_size=ref.stage_px.size,
            ref_offset=Point(ref.stage_px.x, ref.stage_px.y),
        )


def game_px_rect_to_stage_rect(rect: Rect, ref: ReferenceSpace = DEFAULT_REFERENCE) -> Rect:
    """Reference ``game_px`` rect -> canonical stage rect."""
    return rect.offset(-ref.stage_px.x, -ref.stage_px.y)


def stage_rect_to_game_px_rect(rect: Rect, ref: ReferenceSpace = DEFAULT_REFERENCE) -> Rect:
    """Canonical stage rect -> reference ``game_px`` rect."""
    return rect.offset(ref.stage_px.x, ref.stage_px.y)


def game_px_point_to_stage_point(p: Point, ref: ReferenceSpace = DEFAULT_REFERENCE) -> Point:
    return Point(p.x - ref.stage_px.x, p.y - ref.stage_px.y)


def stage_point_to_game_px_point(p: Point, ref: ReferenceSpace = DEFAULT_REFERENCE) -> Point:
    return Point(p.x + ref.stage_px.x, p.y + ref.stage_px.y)


def merge_rects(rects: Iterable[Rect]) -> Rect | None:
    """Smallest rect containing all inputs (``None`` for empty input)."""
    items = [r for r in rects if not r.is_empty]
    if not items:
        return None
    x0 = min(r.x for r in items)
    y0 = min(r.y for r in items)
    x1 = max(r.x2 for r in items)
    y1 = max(r.y2 for r in items)
    return Rect.from_ltrb(x0, y0, x1, y1)
