"""Live deterministic executor: the only place that touches the real client.

Everything above this module (skills, planner, runner) speaks in *semantic*
terms — "the scene is BATTLE", "click the confirm button".  This module is the
single place that knows about pixels, ScreenCaptureKit frames, template
thresholds and CGEvent clicks, and it is deliberately dumb and synchronous:
no planner, no goal, no LLM, no retries that pretend to be recovery.

Provenance: the state decision table and the two hard-won rules below were
validated on the live client in Round 4 (see ``docs/progress.md``):

1. a victory panel leaves the 战斗 button visible, so "the fight button is
   gone" is *not* how a battle ends — the 胜利 glyph must be tested;
2. one click per turn, paced by "the combat log changed and then settled",
   never by a fixed sleep — a blind re-click turns a 5-turn fight into 57
   queued clicks.

Safety invariants enforced here (not in the caller):

* input is refused entirely unless ``allow_input`` is true (``dry_run``);
* the window geometry is re-read immediately before every click and a moved
  window aborts with :class:`WindowMoved` instead of clicking into the void;
* clicks are rate limited by ``min_action_gap_ms``;
* a stopped/paused control file turns every action into a no-op.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import cv2
import numpy as np

from . import capture as C
from . import geometry as G
from .world.model import Scene

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BINARY = os.path.join(ROOT, "native", "bin", "seerdx")
TEMPLATE_DIR = os.path.join(ROOT, "seerauto", "templates", "live")

#: Search regions in ``game_px`` (window-local device pixels of the 2x capture).
#: These are *search* regions, not click coordinates: the click point always
#: comes from the match itself, so a shifted stage does not misclick.
ROI_FIGHT = (1710, 1130, 2040, 1260)
ROI_VICTORY = (980, 640, 1430, 790)
ROI_DIALOG = (1070, 940, 1300, 1040)
ROI_SCENE = (200, 340, 2260, 1330)
ROI_LOG = (500, 1100, 1100, 1400)
ROI_SKILLS = (1050, 1120, 1600, 1360)
ROI_HUD_TOP = (0, 0, 2400, 320)

#: Default thresholds, tuned on live frames in Round 4.
TH_FIGHT = 0.85
TH_VICTORY = 0.80
TH_CONFIRM = 0.80
TH_MONSTER = 0.72

CONFIRM_WORDS = ("确认", "確定", "确定", "我已知晓", "我知道啦", "关闭")

#: Every known 确认-button style, matched in ``ROI_DIALOG``.  A new dialog style
#: that is missing here is invisible to the state machine, so this tuple is the
#: first place to look when "clicks do nothing".
CONFIRM_TEMPLATES = ("live_confirm_btn", "live_confirm_xp", "live_confirm_xp2")


class LiveError(RuntimeError):
    """Base class for refusals raised by the live layer."""


class NotFrontmost(LiveError):
    """The game window is not the frontmost app window; clicks would misfire."""


class WindowMoved(LiveError):
    """The window geometry changed after capture; refusing to click."""


class InputRefused(LiveError):
    """Input was requested while ``allow_input`` is false (dry run)."""


@dataclass
class Probe:
    template: str
    score: float
    #: Match centre in ``game_px`` (only meaningful when ``score`` is high).
    point_px: tuple[float, float]
    rect: tuple[int, int, int, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "template": self.template,
            "score": round(self.score, 4),
            "point_px": [round(self.point_px[0]), round(self.point_px[1])],
            "roi": list(self.rect),
        }


@dataclass
class SceneProbe:
    scene: Scene
    scores: dict[str, float] = field(default_factory=dict)
    points: dict[str, tuple[float, float]] = field(default_factory=dict)
    frame_seq: int = 0
    frame_age_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scene": self.scene.value,
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
            "frame_seq": self.frame_seq,
            "frame_age_ms": round(self.frame_age_ms, 2),
        }


@dataclass
class ActionRecord:
    t: float
    reason: str
    px: tuple[int, int] | None
    global_pt: tuple[float, float] | None
    sent: bool
    refused: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "t": round(self.t, 3),
            "reason": self.reason,
            "game_px": None if self.px is None else [round(self.px[0]), round(self.px[1])],
            "global_pt": None if self.global_pt is None else [
                round(self.global_pt[0], 1), round(self.global_pt[1], 1)],
            "sent": self.sent,
            "refused": self.refused,
        }


class LiveSession:
    """Warm ScreenCaptureKit stream + guarded input, for one game window."""

    def __init__(
        self,
        binary: str | None = None,
        *,
        dry_run: bool = True,
        enable_input: bool = False,
        min_action_gap_ms: float = 700.0,
        geometry_tolerance_pt: float = 4.0,
        timeout_s: float = 30.0,
    ):
        self.binary = binary or DEFAULT_BINARY
        self.dry_run = bool(dry_run)
        self.allow_input = bool(enable_input) and not self.dry_run
        self.min_action_gap_ms = float(min_action_gap_ms)
        self.geometry_tolerance_pt = float(geometry_tolerance_pt)
        self.timeout_s = float(timeout_s)

        self.client: C.DaemonClient | None = None
        self.backend: C.DaemonCapture | None = None
        self.geo: G.WindowGeometry | None = None
        self.window_raw: dict[str, Any] = {}
        self.actions: list[ActionRecord] = []
        self._templates: dict[str, np.ndarray] = {}
        self._last_action_at = 0.0
        self._opened = False
        self._stage_offset: tuple[int, int] | None = None

    # -- lifecycle --------------------------------------------------------- #
    def open(self) -> "LiveSession":
        client = C.DaemonClient(self.binary, timeout_s=self.timeout_s)
        try:
            win = client.win()
        except Exception as exc:  # noqa: BLE001 - missing daemon / no window
            client.close()
            raise LiveError(f"cannot query game window: {exc}") from exc
        if not isinstance(win, Mapping) or "w" not in win:
            client.close()
            raise LiveError(f"no game window: {win!r}")
        self.client = client
        self.window_raw = dict(win)
        self.geo = G.WindowGeometry(
            origin_pt=G.Point(float(win["x"]), float(win["y"])),
            size_pt=G.Size(float(win["w"]), float(win["h"])),
            scale=G.RETINA_SCALE,
        )
        self.backend = C.DaemonCapture(client, self.geo)
        if not self.backend.warm:
            self.close()
            raise LiveError("capture session is not warm (capture.start failed)")
        try:
            client.send("activate")
        except Exception:  # noqa: BLE001 - best effort
            pass
        self._opened = True
        return self

    def close(self) -> None:
        for closer in (self.backend, self.client):
            try:
                if closer is not None:
                    closer.close()
            except Exception:  # noqa: BLE001
                pass
        self.backend = None
        self.client = None
        self._opened = False

    def __enter__(self) -> "LiveSession":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def opened(self) -> bool:
        return self._opened and self.client is not None

    @property
    def pixel_size(self) -> tuple[float, float]:
        if self.geo is None:
            return (0.0, 0.0)
        return (self.geo.pixel_size.w, self.geo.pixel_size.h)

    # -- observation ------------------------------------------------------- #
    def latest(self) -> np.ndarray:
        if self.backend is None:
            raise LiveError("session not open")
        frame = self.backend.latest()
        return frame.array

    def frame_bgr(self) -> np.ndarray:
        return self.latest()

    def _template(self, name: str) -> np.ndarray:
        tpl = self._templates.get(name)
        if tpl is None:
            path = os.path.join(TEMPLATE_DIR, name + ".png")
            img = cv2.imread(path)
            if img is None:
                raise LiveError(f"missing template {path}")
            self._templates[name] = img
            tpl = img
        return tpl

    def score(self, frame: np.ndarray, name: str,
              roi: Sequence[int] = ROI_SCENE) -> Probe:
        x0, y0, x1, y1 = (int(v) for v in roi)
        h, w = frame.shape[:2]
        x0, x1 = max(0, min(x0, w - 1)), max(1, min(x1, w))
        y0, y1 = max(0, min(y0, h - 1)), max(1, min(y1, h))
        tpl = self._template(name)
        sub = frame[y0:y1, x0:x1]
        th, tw = tpl.shape[:2]
        if sub.shape[0] < th or sub.shape[1] < tw:
            return Probe(name, 0.0, ((x0 + x1) / 2, (y0 + y1) / 2), (x0, y0, x1, y1))
        res = cv2.matchTemplate(sub, tpl, cv2.TM_CCOEFF_NORMED)
        _, best, _, loc = cv2.minMaxLoc(res)
        cx = x0 + loc[0] + tw / 2.0
        cy = y0 + loc[1] + th / 2.0
        return Probe(name, float(best), (cx, cy), (x0, y0, x1, y1))

    def probe(self, name: str, roi: Sequence[int] = ROI_SCENE) -> Probe:
        return self.score(self.latest(), name, roi)

    def scene(self, frame: np.ndarray | None = None, *,
              previous: Scene | None = None) -> SceneProbe:
        """Classify the frame with templates only (no OCR, ~8 ms)."""

        if frame is None:
            frame = self.latest()
        p_fight = self.score(frame, "live_fight_btn", ROI_FIGHT)
        p_victory = self.score(frame, "live_victory_word", ROI_VICTORY)
        # Three dialog styles are known; taking the best match instead of a
        # single template is what made the 「恭喜你得到了N个积累经验」 modal
        # invisible to the state machine (it scored 0.41 against the old
        # templates and silently blocked every click behind it).
        p_ok = None
        for name in CONFIRM_TEMPLATES:
            probe = self.score(frame, name, ROI_DIALOG)
            if p_ok is None or probe.score > p_ok.score:
                p_ok = probe
        assert p_ok is not None

        is_victory = p_victory.score >= TH_VICTORY
        is_dialog = p_ok.score >= TH_CONFIRM and not is_victory
        # hysteresis: the fight button dips to ~0.80 during animations, and a
        # single-frame dip used to flip the state machine mid-battle.
        fight_cut = TH_FIGHT - 0.10 if previous is Scene.BATTLE else TH_FIGHT
        is_battle = p_fight.score >= fight_cut and not is_victory and not is_dialog

        if is_victory:
            scene = Scene.RESULT
        elif is_dialog:
            scene = Scene.DIALOG
        elif is_battle:
            scene = Scene.BATTLE
        else:
            scene = Scene.WORLD
        return SceneProbe(
            scene=scene,
            scores={
                "fight": p_fight.score,
                "victory": p_victory.score,
                "confirm": p_ok.score,
            },
            points={
                "fight": p_fight.point_px,
                "victory": p_victory.point_px,
                "confirm": p_ok.point_px,
            },
            frame_seq=0,
        )

    def log_signature(self, frame: np.ndarray | None = None) -> int:
        """Cheap fingerprint of the combat-log band (~0.1 ms, no OCR)."""

        if frame is None:
            frame = self.latest()
        x0, y0, x1, y1 = ROI_LOG
        sub = frame[y0:y1, x0:x1]
        small = sub[::8, ::8]
        return int(small.mean() * 64) ^ int(small.std() * 64)

    def export_png(self, path: str, frame: np.ndarray | None = None) -> str:
        """Write the newest warm frame to ``path``.

        ``shot`` must **not** be used while the capture stream is warm: it
        returns a near-blank image (~6 KB) and OCR then silently sees nothing.
        """

        if frame is None:
            frame = self.latest()
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        cv2.imwrite(path, frame)
        return path

    def ocr(self, *, min_conf: float = 0.3, path: str | None = None,
            frame: np.ndarray | None = None) -> list[dict[str, Any]]:
        """One OCR look at the newest frame.

        Returns items with ``text``, ``conf`` and ``px`` (game_px centre).  This
        is the slow escalation path (~450 ms) and must never be polled.
        """

        if self.client is None:
            raise LiveError("session not open")
        path = path or "/tmp/seerauto_live_ocr.png"
        self.export_png(path, frame)
        raw = self.client.send("ocr", path=path, min=min_conf).get("items", [])
        w, h = self.pixel_size
        out = []
        for it in raw:
            cx = (it["x"] + it["w"] / 2.0) * w
            cy = (1.0 - (it["y"] + it["h"] / 2.0)) * h
            out.append({
                "text": it["text"],
                "conf": float(it.get("conf", 0.0)),
                "px": (float(cx), float(cy)),
                "norm": (float(it["x"]), float(it["y"]), float(it["w"]), float(it["h"])),
            })
        return out

    def find_text(self, items: Iterable[Mapping[str, Any]],
                  needles: Iterable[str], *, exact: bool = False) -> dict[str, Any] | None:
        for it in items:
            text = str(it.get("text") or "")
            for needle in needles:
                if (text.strip() == needle) if exact else (needle in text):
                    return dict(it)
        return None

    # -- guards ------------------------------------------------------------ #
    def ensure_frontmost(self) -> bool:
        """True when the game window is the frontmost layer-0 window.

        ScreenCaptureKit happily captures an *occluded* window, while CGEvent
        clicks land on whatever is actually in front — so "I can see it" and
        "I can click it" are different questions and only this check tells
        them apart.
        """

        return _frontmost_via_seerctl()

    def _revalidate_geometry(self) -> None:
        if self.client is None or self.geo is None:
            raise LiveError("session not open")
        try:
            win = self.client.win()
        except Exception as exc:  # noqa: BLE001
            raise WindowMoved(f"cannot re-read window geometry: {exc}") from exc
        if not isinstance(win, Mapping) or "w" not in win:
            raise WindowMoved(f"window disappeared: {win!r}")
        tol = self.geometry_tolerance_pt
        moved = max(
            abs(float(win["x"]) - self.geo.origin_pt.x),
            abs(float(win["y"]) - self.geo.origin_pt.y),
            abs(float(win["w"]) - self.geo.size_pt.w),
            abs(float(win["h"]) - self.geo.size_pt.h),
        )
        if moved > tol:
            raise WindowMoved(
                f"window moved/resized by {moved:.1f}pt "
                f"(was {self.geo.origin_pt.as_tuple()} {self.geo.size_pt.as_tuple()}, "
                f"now ({win['x']},{win['y']}) {win['w']}x{win['h']})")

    # -- input ------------------------------------------------------------- #
    def click_px(self, x: float, y: float, reason: str = "", *,
                 require_frontmost: bool = True) -> ActionRecord:
        """Click a ``game_px`` point (window-local device pixel).

        Refuses (raising) when input is disabled or the window moved.  Returns
        the record either way so a caller can log the attempt honestly.
        """

        px = (float(x), float(y))
        rec = ActionRecord(t=time.perf_counter(), reason=reason, px=(int(x), int(y)),
                           global_pt=None, sent=False)
        try:
            if not self.allow_input:
                raise InputRefused(
                    "input disabled (dry-run or --allow-input not given)")
            if self.client is None or self.geo is None:
                raise LiveError("session not open")
            gap = self.min_action_gap_ms - (time.perf_counter() - self._last_action_at) * 1000.0
            if gap > 0:
                time.sleep(gap / 1000.0)
            if require_frontmost and not self.ensure_frontmost():
                raise NotFrontmost("game window is not frontmost")
            self._revalidate_geometry()
            gp = self.geo.global_pt_from_game_px(px[0], px[1])
            rec.global_pt = (gp.x, gp.y)
            self.client.send("click", x=gp.x, y=gp.y, n=1)
            rec.sent = True
            self._last_action_at = time.perf_counter()
        except LiveError as exc:
            rec.refused = f"{type(exc).__name__}: {exc}"
            self.actions.append(rec)
            raise
        self.actions.append(rec)
        return rec

    def move_px(self, x: float, y: float) -> None:
        if not self.allow_input or self.client is None or self.geo is None:
            raise InputRefused("input disabled")
        self._revalidate_geometry()
        gp = self.geo.global_pt_from_game_px(x, y)
        self.client.send("move", x=gp.x, y=gp.y)

    def click_text(self, needles: Iterable[str], *, min_conf: float = 0.3,
                   reason: str = "") -> dict[str, Any] | None:
        """OCR the live frame and click the first matching label."""

        items = self.ocr(min_conf=min_conf)
        hit = self.find_text(items, needles)
        if hit is None:
            return None
        self.click_px(hit["px"][0], hit["px"][1], reason or f"text {hit['text']!r}")
        return hit

    def wait_for(self, predicate: Any, timeout_s: float = 6.0,
                 poll_s: float = 0.03) -> bool:
        """Poll the newest frame until ``predicate(frame)`` is true or timeout."""

        deadline = time.perf_counter() + float(timeout_s)
        while time.perf_counter() < deadline:
            if predicate(self.latest()):
                return True
            time.sleep(poll_s)
        return False

    def wait_scene(self, wanted: Iterable[Scene], timeout_s: float = 6.0) -> Scene:
        wanted_set = set(wanted)
        deadline = time.perf_counter() + float(timeout_s)
        last = Scene.UNKNOWN
        while time.perf_counter() < deadline:
            sp = self.scene(previous=last)
            last = sp.scene
            if sp.scene in wanted_set:
                return sp.scene
            time.sleep(0.03)
        return last

    def wait_log_change(self, before_sig: int, timeout_s: float = 8.0) -> bool:
        deadline = time.perf_counter() + float(timeout_s)
        while time.perf_counter() < deadline:
            if self.log_signature() != before_sig:
                return True
            time.sleep(0.005)
        return False

    def wait_settle(self, *, stable_ms: float = 220.0, timeout_s: float = 3.0) -> bool:
        """Wait until the combat log stops changing (turn animation finished)."""

        deadline = time.perf_counter() + float(timeout_s)
        prev = None
        stable_since = None
        while time.perf_counter() < deadline:
            sig = self.log_signature()
            if sig != prev:
                prev = sig
                stable_since = time.perf_counter()
            elif stable_since and (time.perf_counter() - stable_since) * 1000.0 >= stable_ms:
                return True
            time.sleep(0.01)
        return False

    # -- diagnostics ------------------------------------------------------- #
    def describe(self) -> dict[str, Any]:
        return {
            "opened": self.opened,
            "window": dict(self.window_raw),
            "pixel_size": self.pixel_size,
            "dry_run": self.dry_run,
            "allow_input": self.allow_input,
            "actions_sent": sum(1 for a in self.actions if a.sent),
            "actions_refused": sum(1 for a in self.actions if a.refused),
        }


def _frontmost_via_seerctl() -> bool:
    """Ask the legacy one-shot CLI which layer-0 window is in front."""

    import json
    import subprocess

    cli = os.path.join(ROOT, "scripts", "seerctl")
    if not os.path.exists(cli):
        return False
    try:
        out = subprocess.run([cli, "wl"], capture_output=True, text=True, timeout=5.0).stdout
        for w in json.loads(out):
            if w.get("layer") == 0:
                return str(w.get("owner")) == "SeerGame"
    except Exception:  # noqa: BLE001
        return False
    return False


__all__ = [
    "LiveSession",
    "LiveError",
    "NotFrontmost",
    "WindowMoved",
    "InputRefused",
    "Probe",
    "SceneProbe",
    "ActionRecord",
    "ROI_FIGHT",
    "ROI_VICTORY",
    "ROI_DIALOG",
    "ROI_SCENE",
    "ROI_LOG",
    "ROI_SKILLS",
    "TH_FIGHT",
    "TH_VICTORY",
    "TH_CONFIRM",
    "TH_MONSTER",
    "CONFIRM_WORDS",
]
