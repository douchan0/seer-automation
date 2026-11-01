"""Vision: template matching, pixel fingerprints, stage detection, thin OCR.

Precedence is deliberate and matches the project rule "template/fingerprint
first, OCR only as a fallback":

1. :func:`detect_stage_rect` - locate the letterbox-free playfield once per
   frame (or calibrate once for a pinned window);
2. :class:`TemplateDetector` - ``cv2.matchTemplate(TM_CCOEFF_NORMED)`` over an
   ROI, in canonical *stage* coordinates so rules survive resolution changes;
3. :func:`patch_signature` / :func:`signature_distance` - a tiny colour grid
   for states that have no distinctive icon (cheap enough for change
   detection too);
4. :class:`OcrEngine` implementations - only for reading text that no
   template can express.  ``NullOcr`` is the default; the loop must not depend
   on OCR.

All ROIs are ``(x0, y0, x1, y1)`` normalised to the stage, which makes every
threshold resolution independent.  Frames carry a
:class:`seerauto.geometry.StageMap`; if one is missing this module derives it
from the frame size using the reference layout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

import numpy as np

from . import geometry as G
from .capture import Frame, load_cv2, read_image
from .errors import CaptureError, SeerAutoError

Norm = Sequence[float]  # (x0, y0, x1, y1) inside the stage, 0..1


# --------------------------------------------------------------------------- #
# templates
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Match:
    """Result of one template match (frame pixels + canonical spaces)."""

    name: str
    score: float
    rect_frame: G.Rect
    rect_canon: G.Rect
    threshold: float

    @property
    def center_frame(self) -> G.Point:
        return self.rect_frame.center

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "score": round(self.score, 4),
            "rect_frame": [round(v, 2) for v in self.rect_frame.as_tuple()],
            "rect_canon": [round(v, 2) for v in self.rect_canon.as_tuple()],
        }


class TemplateCache:
    """Loads template PNGs once and caches scale-specific grayscale copies."""

    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory) if directory else DEFAULT_TEMPLATE_DIR
        self._gray: dict[str, np.ndarray] = {}
        self._color: dict[str, np.ndarray] = {}
        self._scaled: dict[tuple[str, int, int], np.ndarray] = {}
        self._missing: set[str] = set()

    def path_for(self, name: str) -> Path:
        p = Path(name)
        return p if p.suffix else self.directory / f"{name}.png"

    def exists(self, name: str) -> bool:
        return self.path_for(name).exists()

    def load_color(self, name: str) -> np.ndarray:
        if name in self._color:
            return self._color[name]
        path = self.path_for(name)
        if not path.exists():
            raise CaptureError(f"template not found: {path}")
        image = read_image(path)
        self._color[name] = image
        return image

    def load_gray(self, name: str) -> np.ndarray:
        if name in self._gray:
            return self._gray[name]
        cv2 = load_cv2()
        self._gray[name] = cv2.cvtColor(self.load_color(name), cv2.COLOR_BGR2GRAY)
        return self._gray[name]

    def scaled_gray(self, name: str, size: tuple[int, int]) -> np.ndarray:
        key = (name, int(size[0]), int(size[1]))
        cached = self._scaled.get(key)
        if cached is not None:
            return cached
        cv2 = load_cv2()
        base = self.load_gray(name)
        if (base.shape[1], base.shape[0]) != key[1:]:
            base = cv2.resize(base, (key[1], key[2]), interpolation=cv2.INTER_AREA)
        self._scaled[key] = base
        return base

    def names(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(p.stem for p in self.directory.glob("*.png"))


DEFAULT_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


def downsample(image: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Cheap area-ish downscale that works on any input size.

    ``cv2.resize(INTER_AREA)`` is pathologically slow in this OpenCV build
    (measured ~14 ms for 2298x1428 -> 160x100, vs ~0.04 ms for INTER_LINEAR),
    so we stride-subsample first and only then interpolate.  The result is a
    mean-like reduction good enough for signatures and templates.
    """
    cv2 = load_cv2()
    height, width = image.shape[:2]
    target_w, target_h = max(1, int(size[0])), max(1, int(size[1]))
    step_x = max(1, width // max(1, target_w))
    step_y = max(1, height // max(1, target_h))
    if step_x > 1 or step_y > 1:
        image = image[::step_x, ::step_y]
        if image.shape[0] > target_h * 2 or image.shape[1] > target_w * 2:
            image = cv2.resize(image, (target_w, target_h), interpolation=cv2.INTER_LINEAR)
            return image
    return cv2.resize(image, (target_w, target_h), interpolation=cv2.INTER_LINEAR)


# --------------------------------------------------------------------------- #
# stage detection
# --------------------------------------------------------------------------- #
def _longest_run(mask: np.ndarray) -> tuple[int, int] | None:
    """Longest inclusive run of ``True`` values (``None`` when empty)."""
    best: tuple[int, int] | None = None
    start: int | None = None
    for i, value in enumerate(mask):
        if value and start is None:
            start = i
        elif not value and start is not None:
            if best is None or (i - 1 - start) > (best[1] - best[0]):
                best = (start, i - 1)
            start = None
    if start is not None:
        if best is None or (len(mask) - 1 - start) > (best[1] - best[0]):
            best = (start, len(mask) - 1)
    return best


def detect_stage_rect(
    frame: Frame,
    dark_threshold: int = 45,
    black_row_ratio: float = 0.85,
    content_col_ratio: float = 0.60,
    content_row_ratio: float = 0.97,
    min_area_frac: float = 0.15,
    step: int = 4,
) -> G.Rect | None:
    """Find the game playfield inside a window capture with black letterbox.

    Layout measured on the real artifacts: title bar, then a full-width black
    strip, then the stage with black bars left/right/bottom.  The window also
    has a 1px bright border, so the stage is the *longest* run of non-black
    columns/rows - not simply the first/last one.

    Returns the stage rect in **frame pixels**, or ``None`` when the frame does
    not look like a window capture (e.g. an already-cropped panel).
    """
    image = frame.array
    if image is None or image.size == 0:
        return None
    height, width = image.shape[:2]
    if height < 48 or width < 48:
        return None
    step = max(1, int(step))
    # Downsample for the detection pass: this runs once per frame at most.
    small = image[::step, ::step]
    dark = small.max(axis=2) < dark_threshold
    rows = dark.mean(axis=1)

    # 1) the title bar ends where a mostly-black full-width strip begins.
    strip_start = None
    for y in range(rows.shape[0]):
        if rows[y] >= black_row_ratio:
            strip_start = y
            break
    if strip_start is None:
        return None
    # 2) playfield content starts after that strip.
    content_top = None
    for y in range(strip_start, rows.shape[0]):
        if rows[y] < black_row_ratio:
            content_top = y
            break
    if content_top is None:
        return None
    # 3) columns: longest run that is not a black side bar.
    cols = dark[content_top:].mean(axis=0)
    col_run = _longest_run(cols < content_col_ratio)
    if col_run is None:
        return None
    left, right = col_run
    # 4) rows: longest run with content inside the stage columns.
    sub = dark[:, left : right + 1]
    row_ratio = sub.mean(axis=1)
    row_run = _longest_run(row_ratio < content_row_ratio)
    if row_run is None:
        return None
    top, bottom = row_run
    if top < content_top:  # the title bar is also "content" in this profile
        top = content_top
    if bottom - top < 8 or right - left < 8:
        return None
    rect = G.Rect(
        float(left * step),
        float(top * step),
        float((right - left + 1) * step),
        float((bottom - top + 1) * step),
    )
    if rect.area / float(width * height) < min_area_frac:
        return None
    rect = rect.clamp_to(G.Rect(0.0, 0.0, float(width), float(height)))
    return rect if not rect.is_empty else None


def ensure_stage(frame: Frame, ref: G.ReferenceSpace = G.DEFAULT_REFERENCE) -> Frame:
    """Return a frame guaranteed to carry a usable :class:`StageMap`.

    Live frames already carry one from the window geometry; replay fixtures may
    need detection.  If detection fails the reference layout scaled to the
    frame is used, which is correct for every full-window capture.
    """
    if frame.stage is not None:
        return frame
    detected = detect_stage_rect(frame)
    if detected is not None:
        return frame.with_stage(
            G.StageMap(
                stage_px=detected,
                ref_size=ref.stage_px.size,
                ref_offset=G.Point(ref.stage_px.x, ref.stage_px.y),
            )
        )
    stage = ref.stage_px.scale(
        frame.width / ref.capture_px.w, frame.height / ref.capture_px.h
    )
    return frame.with_stage(
        G.StageMap(
            stage_px=stage,
            ref_size=ref.stage_px.size,
            ref_offset=G.Point(ref.stage_px.x, ref.stage_px.y),
        )
    )


# --------------------------------------------------------------------------- #
# template detector
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TemplateSpec:
    """One template lookup: name + optional ROI + score threshold."""

    name: str
    roi: tuple[float, float, float, float] | None = None
    threshold: float = 0.80
    max_score: float | None = None  # for "absent" style checks
    # match on a half-resolution ROI/template: ~4x cheaper and still accurate
    # for UI blocks.  Automatically disabled when the target gets too small.
    downscale: float = 2.0

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "threshold": self.threshold}
        if self.roi is not None:
            out["roi"] = list(self.roi)
        if self.max_score is not None:
            out["max_score"] = self.max_score
        return out


class TemplateDetector:
    """TM_CCOEFF_NORMED matching in canonical stage coordinates."""

    def __init__(self, cache: TemplateCache | None = None):
        self.cache = cache or TemplateCache()
        self._last_scale: float | None = None

    # -- single match ------------------------------------------------------ #
    MIN_TEMPLATE_PX = 16

    def match(self, frame: Frame, spec: TemplateSpec) -> Match | None:
        """Match ``spec`` inside ``frame``.  ``None`` when the ROI is not visible."""
        cv2 = load_cv2()
        frame = ensure_stage(frame)
        stage = frame.stage
        assert stage is not None  # ensured above

        # ROI in canonical stage pixels -> frame pixels -> array indices
        roi_frame = self._roi_frame_rect(frame, spec.roi)
        if roi_frame is None:
            return None
        x, y, w, h = roi_frame.clamp_to(frame.full_rect_px).as_int_tuple()
        if w < 4 or h < 4:
            return None
        roi = frame.array[y : y + h, x : x + w]
        if roi.size == 0:
            return None
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY) if roi.ndim == 3 else roi

        base_template = self.cache.load_gray(spec.name)
        ds = max(1.0, float(getattr(spec, "downscale", 2.0)))
        target_w = max(4, int(round(base_template.shape[1] * stage.scale_x / ds)))
        target_h = max(4, int(round(base_template.shape[0] * stage.scale_y / ds)))
        if target_w < self.MIN_TEMPLATE_PX or target_h < self.MIN_TEMPLATE_PX:
            ds = 1.0
            target_w = max(4, int(round(base_template.shape[1] * stage.scale_x)))
            target_h = max(4, int(round(base_template.shape[0] * stage.scale_y)))
        if target_w > gray.shape[1] * ds or target_h > gray.shape[0] * ds:
            return None
        if ds != 1.0:
            gray = downsample(gray, (max(4, int(round(gray.shape[1] / ds))), max(4, int(round(gray.shape[0] / ds)))))
        template = self.cache.scaled_gray(spec.name, (target_w, target_h))
        # TM_CCOEFF_NORMED = zero-mean normalised correlation: illumination
        # robust, and it is what the project's measurements are based on.
        result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        score = float(max_val)
        matched_frame = G.Rect(
            float(x + max_loc[0] * ds),
            float(y + max_loc[1] * ds),
            float(target_w * ds),
            float(target_h * ds),
        )
        return Match(
            name=spec.name,
            score=score,
            rect_frame=matched_frame,
            rect_canon=stage.canon_from_frame_rect(matched_frame),
            threshold=spec.threshold,
        )

    def match_all(self, frame: Frame, specs: Iterable[TemplateSpec]) -> dict[str, Match]:
        """Match several templates; returns only the ones that were visible."""
        out: dict[str, Match] = {}
        for spec in specs:
            match = self.match(frame, spec)
            if match is not None:
                out[spec.name] = match
        return out

    def _roi_frame_rect(self, frame: Frame, roi: Norm | None) -> G.Rect | None:
        assert frame.stage is not None
        if roi is None:
            return frame.full_rect_px
        canon = frame.stage.canon_rect_from_norm(roi)
        return frame.stage.frame_from_canon_rect(canon)


# --------------------------------------------------------------------------- #
# fingerprints
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Fingerprint:
    """Colour grid signature of a stage-normalised ROI."""

    grid: tuple[int, int]
    cells: tuple[float, ...]  # BGR means, row-major
    mean: tuple[float, float, float]
    std: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "grid": list(self.grid),
            "cells": [round(v, 2) for v in self.cells],
            "mean": [round(v, 2) for v in self.mean],
            "std": round(self.std, 2),
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Fingerprint":
        return cls(
            grid=tuple(int(v) for v in data["grid"]),
            cells=tuple(float(v) for v in data["cells"]),
            mean=tuple(float(v) for v in data.get("mean", (0, 0, 0))),
            std=float(data.get("std", 0.0)),
        )


def patch_signature(
    frame: Frame, roi: Norm | None = None, grid: tuple[int, int] = (4, 4)
) -> Fingerprint | None:
    """Cheap colour-grid fingerprint of a normalised stage ROI.

    Cost is dominated by two tiny resizes, well under a millisecond.
    """
    cv2 = load_cv2()
    frame = ensure_stage(frame)
    if frame.stage is None:
        return None
    rect = frame.full_rect_px if roi is None else frame.stage.frame_rect_from_norm(roi)
    x, y, w, h = rect.clamp_to(frame.full_rect_px).as_int_tuple()
    if w < grid[0] or h < grid[1]:
        return None
    patch = frame.array[y : y + h, x : x + w]
    small = downsample(patch, (grid[0], grid[1]))
    cells = tuple(float(v) for v in small.reshape(-1).astype(np.float32))
    mean = tuple(float(v) for v in small.reshape(-1, 3).mean(axis=0))
    # std from the tiny grid: a full-patch std pass is a measurable extra pass
    std = float(small.astype(np.float32).std())
    return Fingerprint(grid=grid, cells=cells, mean=mean, std=std)


def signature_distance(a: Fingerprint, b: Fingerprint) -> float:
    """Mean absolute per-channel difference in 0..255 (0 = identical colours)."""
    if a.cells and b.cells:
        va = np.asarray(a.cells, dtype=np.float32)
        vb = np.asarray(b.cells, dtype=np.float32)
        if va.shape == vb.shape:
            return float(np.abs(va - vb).mean())
    return float(abs(sum(a.mean) - sum(b.mean)) / 3.0)


# --------------------------------------------------------------------------- #
# change detection (the executor's hot primitive)
# --------------------------------------------------------------------------- #
def frame_signature(frame: Frame, size: tuple[int, int] = (160, 100)) -> np.ndarray:
    """Tiny grayscale signature of the whole frame.

    Downsample *first*, then convert: a full-frame ``cvtColor`` costs ~0.66 ms
    (measured) while this path is ~4x cheaper and change detection does not
    need full-resolution colour.
    """
    cv2 = load_cv2()
    array = frame.array
    small = downsample(array, size)
    if small.ndim == 3:
        return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return small


def signature_delta(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute difference of two signatures, normalised to 0..1."""
    if a.shape != b.shape:
        return 1.0
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean() / 255.0)


def frame_delta(prev: Frame | None, current: Frame, size: tuple[int, int] = (160, 100)) -> float:
    """Convenience wrapper: 0.0 when nothing changed, 1.0 when very different."""
    if prev is None:
        return 1.0
    return signature_delta(frame_signature(prev, size), frame_signature(current, size))


# --------------------------------------------------------------------------- #
# OCR (fallback only)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TextItem:
    """Recognised text in normalised *stage* coordinates (x, y, w, h in 0..1)."""

    text: str
    confidence: float = 0.0
    rect_norm: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    @property
    def center_norm(self) -> tuple[float, float]:
        x, y, w, h = self.rect_norm
        return (x + w / 2, y + h / 2)


class OcrEngine(Protocol):
    name: str

    def recognize(self, frame: Frame) -> list[TextItem]: ...


class NullOcr:
    """No OCR: the pure-vision path.  Always available, always empty."""

    name = "null"

    def recognize(self, frame: Frame) -> list[TextItem]:
        return []


class FakeOcr:
    """Deterministic OCR stub for tests (never used in production paths)."""

    name = "fake"

    def __init__(self, items: Sequence[TextItem] = ()):
        self.items = list(items)

    def recognize(self, frame: Frame) -> list[TextItem]:
        return list(self.items)


class DaemonOcr:
    """OCR delegated to the daemon (Vision framework lives on the Swift side)."""

    name = "daemon"

    def __init__(self, client: Any, min_confidence: float = 0.30):
        self.client = client
        self.min_confidence = min_confidence

    def recognize(self, frame: Frame) -> list[TextItem]:
        path = frame.meta.get("path")
        if not path:
            return []
        reply = self.client.send("ocr", path=str(path), min=self.min_confidence)
        items: list[TextItem] = []
        for raw in reply.get("items", []) or []:
            # daemon returns Vision bounding boxes in 0..1 window space (origin
            # bottom-left).  Convert to a normalised stage rect for rules.
            try:
                x = float(raw["x"])
                y = float(raw["y"])
                w = float(raw["w"])
                h = float(raw["h"])
            except (KeyError, TypeError, ValueError):
                continue
            items.append(
                TextItem(
                    text=str(raw.get("text", "")),
                    confidence=float(raw.get("conf", 0.0)),
                    rect_norm=(x, 1.0 - y - h, w, h),
                )
            )
        return items


def find_text(items: Iterable[TextItem], needles: Iterable[str], exact: bool = False) -> list[TextItem]:
    """Filter OCR items by substring (or exact) match, case-insensitively."""
    needles = [n.lower() for n in needles]
    hits: list[TextItem] = []
    for item in items:
        text = item.text.lower()
        if exact:
            if text in needles:
                hits.append(item)
        elif any(n in text for n in needles):
            hits.append(item)
    return hits


# --------------------------------------------------------------------------- #
# analysis facade
# --------------------------------------------------------------------------- #
@dataclass
class Signals:
    """Everything the state machine is allowed to look at for one frame."""

    templates: dict[str, Match] = field(default_factory=dict)
    fingerprints: dict[str, Fingerprint] = field(default_factory=dict)
    text: list[TextItem] = field(default_factory=list)
    frame_seq: int = 0
    frame_ts_ns: int = 0
    frame_age_ms: float = 0.0
    stage_rect_px: tuple[float, float, float, float] | None = None
    notes: list[str] = field(default_factory=list)

    def template_score(self, name: str) -> float:
        match = self.templates.get(name)
        return match.score if match else 0.0

    def has_template(self, name: str, threshold: float = 0.0) -> bool:
        match = self.templates.get(name)
        return bool(match and match.score >= threshold)

    def text_contains(self, needles: Iterable[str]) -> bool:
        return bool(find_text(self.text, needles))

    def as_dict(self) -> dict[str, Any]:
        return {
            "templates": {name: round(m.score, 4) for name, m in self.templates.items()},
            "fingerprints": {name: fp.as_dict() for name, fp in self.fingerprints.items()},
            "text": [t.text for t in self.text],
            "frame": {
                "seq": self.frame_seq,
                "age_ms": round(self.frame_age_ms, 2),
                "stage_rect": self.stage_rect_px,
            },
            "notes": list(self.notes),
        }


# --------------------------------------------------------------------------- #
# analyzer: frame -> Signals (no OCR on the poll path)
# --------------------------------------------------------------------------- #
class Analyzer:
    """Turns a :class:`Frame` into :class:`Signals` for the state machine.

    Only ROI template matches and patch fingerprints run every tick: both are
    sub-millisecond on this host.  OCR is **opt-in** via ``escalate=True`` and
    is never called from the normal poll path (84% of the old loop cost).
    """

    def __init__(
        self,
        templates: Mapping[str, TemplateSpec],
        fingerprint_rois: Mapping[str, tuple[float, float, float, float]],
        detector: TemplateDetector | None = None,
        ocr: OcrEngine | None = None,
        perf: Any | None = None,
        ref: G.ReferenceSpace = G.DEFAULT_REFERENCE,
    ):
        self.templates = dict(templates)
        self.fingerprint_rois = dict(fingerprint_rois)
        self.detector = detector or TemplateDetector()
        self.ocr = ocr or NullOcr()
        self.perf = perf
        self.ref = ref
        self._stage: G.StageMap | None = None  # cached stage for a pinned window

    # -- building from config (duck-typed to avoid a config<->vision cycle) - #
    @classmethod
    def from_config(cls, config: Any, detector: TemplateDetector | None = None,
                    ocr: OcrEngine | None = None, perf: Any | None = None) -> "Analyzer":
        specs: dict[str, TemplateSpec] = {}
        for rule in config.states.rules:
            if not rule.enabled:
                continue
            for raw in rule.conditions:
                kind = raw.get("kind")
                if kind not in ("template_present", "template_absent", "template_seen_within"):
                    continue
                name = str(raw["name"])
                roi_name = raw.get("roi")
                roi = None
                if roi_name is not None:
                    roi = config.vision.rois[str(roi_name)] if isinstance(roi_name, str) else tuple(roi_name)
                if roi is None and name in config.vision.rois:
                    roi = config.vision.rois[name]
                threshold = float(raw.get("min_score", raw.get("max_score", 0.5)))
                prev = specs.get(name)
                if prev is not None:
                    threshold = min(prev.threshold, threshold)
                    roi = prev.roi if prev.roi is not None else roi
                specs[name] = TemplateSpec(name=name, roi=roi, threshold=threshold)
        # templates that rules reference without a condition-level ROI
        fps = {name: spec.roi for name, spec in config.vision.fingerprints.items()}
        return cls(
            templates=specs,
            fingerprint_rois=fps,
            detector=detector or TemplateDetector(TemplateCache(config.vision.template_dir)),
            ocr=ocr or NullOcr(),
            perf=perf,
            ref=config.reference,
        )

    # -- analysis ---------------------------------------------------------- #
    def analyze(
        self,
        frame: Frame,
        now_ns: int | None = None,
        escalate: bool = False,
    ) -> Signals:
        import time as _time

        frame = ensure_stage(frame, self.ref)
        assert frame.stage is not None
        signals = Signals()
        now = int(now_ns if now_ns is not None else _time.monotonic_ns())
        signals.frame_seq = frame.seq
        signals.frame_ts_ns = frame.timestamp_ns
        signals.frame_age_ms = frame.age_ms(now)
        signals.stage_rect_px = tuple(round(v, 1) for v in frame.stage.stage_px.as_tuple())

        if self.perf is not None:
            for name, spec in self.templates.items():
                with self.perf.stage("detect_template", template=name):
                    match = self.detector.match(frame, spec)
                if match is not None:
                    signals.templates[name] = match
            for name, roi in self.fingerprint_rois.items():
                with self.perf.stage("detect_fingerprint", fingerprint=name):
                    fp = patch_signature(frame, roi)
                if fp is not None:
                    signals.fingerprints[name] = fp
            if escalate:
                with self.perf.stage("ocr", engine=self.ocr.name):
                    signals.text = list(self.ocr.recognize(frame))
        else:
            for name, spec in self.templates.items():
                match = self.detector.match(frame, spec)
                if match is not None:
                    signals.templates[name] = match
            for name, roi in self.fingerprint_rois.items():
                fp = patch_signature(frame, roi)
                if fp is not None:
                    signals.fingerprints[name] = fp
            if escalate:
                signals.text = list(self.ocr.recognize(frame))

        if frame.stage.stage_px.is_empty:
            signals.notes.append("empty stage")
        return signals
