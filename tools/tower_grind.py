#!/usr/bin/env python3
"""tower_grind — unattended 试炼之塔 grinder for the live-verified guardian-body path.

Why this file exists: on 2026-11-06 23:59 the tower was proven to be the night's
levelling route (2-round win, 「XIN精灵获得经验：1710」, floor auto-advances), but
`tools/farm_tower_repeat.py` had been BLOCKED all night for a specific, now-known
reason: **the tower interior has no 「战斗」 button**.  A fight starts by clicking the
guardian's *body* at (1190,760) twice — first click walks, second engages — and the
painted floor number on the ground (01, 02, …) is the only current-floor evidence
(the dialog's 「你当前到达第 N 层」 is the historical highest).

So this runner is a standalone state machine over that verified path:

    DIALOG -> PICK_FLOOR -> INSIDE -> ENGAGE -> BATTLE -> RESULT -> NEXT

* DIALOG: the tower entry panel (选择楼层 / 继续挑战 / 当前到达第).
* PICK_FLOOR: click 选择楼层 then a calibrated preset (1 / 11 / 21); preset 1
  teleports inside.
* INSIDE: read the painted floor with two consecutive agreeing OCR samples.
* ENGAGE: click the guardian body twice; if no battle markers appear, one more click
  after a gap, then BLOCKED (never keep clicking blindly).
* BATTLE: delegate to the existing verified battle layer (`seerauto.skills.act.
  RunBattle`) — untouched — or to an injected runner in tests.
* RESULT: read the payout text verbatim (「XIN精灵获得经验：1710」), note reward
  dialogs, dismiss only with calibrated confirm words.
* NEXT: the painted floor must advance.  If the battle was won but the floor did not
  advance and there is no defeat marker, the cycle is UNVERIFIED_ACTIVITY, not a win.

Bounded: per-cycle timeout, ≤3 re-entries after a defeat, ≤3 failures, absolute
deadline, pause/stop flags checked at cycle boundaries.  Every cycle is appended to
`data/overnight/tower_grind.jsonl` (ts, cycle, floors, verbatim xp text, dialogs,
click counts, outcome).

Calibration-first: all coordinates/ROIs/keywords live in
`seerauto/config/tower_calibration.json`, shipped **UNCALIBRATED**.  The runner refuses
to click until the live operator fills the measured values, and refuses if the live
capture size no longer matches the calibrated reference (AGENTS.md §5.5).

    PYTHONPATH=.pydeps python3 tools/tower_grind.py --dry-run
    PYTHONPATH=.pydeps python3 tools/tower_grind.py --preset 1 --minutes 400 \
        --deadline-at 2026-11-07T07:00:00+08:00 --allow-input

Nothing here modifies `farm_tower_repeat.py`, `tower_cycle.py` or `seerauto/`: they are
kept as evidence.  The battle layer is imported lazily so `--dry-run` works without a
client or the vision stack.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seerauto.dual.outcome import Outcome  # noqa: E402

DEFAULT_CALIBRATION = ROOT / "seerauto" / "config" / "tower_calibration.json"
DEFAULT_LEDGER = ROOT / "data" / "overnight" / "tower_grind.jsonl"
PAUSE_FILES = ("/tmp/seerauto.master.pause", "/tmp/seerauto.pause")
STOP_FILE = "/tmp/seerauto.stop"

EXIT_CODES = {
    Outcome.PROGRESS.value: 0,
    Outcome.NO_WORK.value: 3,
    Outcome.FAILED.value: 1,
    Outcome.UNVERIFIED_ACTIVITY.value: 4,
    Outcome.BLOCKED.value: 7,
    Outcome.DEADLINE_REACHED.value: 124,
    Outcome.PAUSED.value: 130,
    Outcome.INTERRUPTED.value: 130,
}

REQUIRED_FIELDS = (
    "tower.inside.floor_roi_px",
    "tower.guardian_px",
    "tower.dialog.pick_floor_px",
    "tower.dialog.floor_presets",
    "tower.result.roi_px",
    "tower.result.xp_regex",
    "tower.result.confirm_words",
    "tower.result.defeat_markers",
)

#: Optional but recommended: without it, an unreadable painted floor (art lettering
#: that OCR misreads) leaves INSIDE unable to prove "we are in the tower" and the
#: runner refuses (by design).
OPTIONAL_FIELDS = (
    "tower.inside.signature {mode: roi_mean_color, roi_px, expect_rgb, tolerance}",
)

PLAN = [
    "DIALOG: recognise the tower entry panel by calibrated markers, two consecutive "
    "readings",
    "PICK_FLOOR: click 选择楼层, then the calibrated preset (default 1); clicking 1 "
    "teleports inside",
    "INSIDE: read the painted floor number in tower.inside.floor_roi_px; two "
    "consecutive agreeing readings or refuse",
    "ENGAGE: click the guardian body twice (walk + engage); if no battle markers, one "
    "more click after a gap, then BLOCKED — never keep clicking",
    "BATTLE: delegate to seerauto.skills.act.RunBattle (verified, unmodified) or an "
    "injected runner",
    "RESULT: copy the 「XIN精灵获得经验」 text verbatim, note reward dialogs, dismiss "
    "with calibrated confirm words only",
    "NEXT: the painted floor must advance; won but not advanced and no defeat marker "
    "=> UNVERIFIED_ACTIVITY",
    "bounded: per-cycle timeout, <=3 re-entries after defeats, <=3 failures, "
    "deadline/pause/stop at cycle boundaries",
]


class CalibrationError(ValueError):
    def __init__(self, problems: Sequence[str], *, path: Path | None = None) -> None:
        self.problems = [str(p) for p in problems]
        self.path = path
        super().__init__("tower calibration is unusable"
                         + (f" ({path})" if path else "") + ": "
                         + "; ".join(self.problems))


# --------------------------------------------------------------------------- #
# calibration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Calibration:
    path: Path
    status: str
    problems: tuple[str, ...]
    capture_px: tuple[int, int]
    guardian_px: tuple[float, float] | None
    guardian_sweep_px: tuple[tuple[float, float], ...]
    approach_px: tuple[float, float] | None
    approach_wait_s: float
    guardian_gap_s: float
    pick_floor_px: tuple[float, float] | None
    continue_px: tuple[float, float] | None
    dialog_markers: tuple[str, ...]
    presets: Mapping[int, tuple[float, float]]
    preset_default: int
    floor_roi_px: tuple[float, float, float, float] | None
    floor_regex: re.Pattern[str] | None
    inside_timeout_s: float
    battle_markers: tuple[str, ...]
    enter_wait_s: float
    battle_max_seconds: float
    result_roi_px: tuple[float, float, float, float] | None
    xp_regex: re.Pattern[str] | None
    xp_to_next_regex: re.Pattern[str] | None
    confirm_words: tuple[str, ...]
    defeat_markers: tuple[str, ...]
    pp_markers: tuple[str, ...]
    max_confirm_clicks: int
    signature: Mapping[str, Any] | None
    levelup_markers: tuple[str, ...]
    max_cycles: int
    max_reentries: int
    max_failures: int
    max_unverified: int
    cycle_timeout_s: float
    raw: Mapping[str, Any]
    #: extra guardian sprite positions to try when the per-floor position moved
    #: (live 2026-11-07: the guardian changes place every floor; a single fixed
    #: point fails). Tried in order after `guardian_px`, inside the same posture.
    guardian_candidates_px: tuple[tuple[float, float], ...] = ()

    @property
    def pet_panel(self) -> Mapping[str, Any] | None:
        tower = self.raw.get("tower")
        panel = tower.get("pet_panel") if isinstance(tower, Mapping) else None
        return panel if isinstance(panel, Mapping) else None

    @property
    def verify_every(self) -> int:
        every = (self.pet_panel or {}).get("verify_every", 3)
        return int(every) if isinstance(every, int) and every >= 1 else 3

    @property
    def pet_panel_xp_roi_px(self) -> tuple[float, float, float, float] | None:
        panel = self.pet_panel or {}
        roi = panel.get("xp_roi_px") or panel.get("xp_to_next_roi_px")
        try:
            return tuple(float(v) for v in roi) if roi else None
        except (TypeError, ValueError):
            return None

    @property
    def pet_panel_level_roi_px(self) -> tuple[float, float, float, float] | None:
        roi = (self.pet_panel or {}).get("level_roi_px")
        try:
            return tuple(float(v) for v in roi) if roi else None
        except (TypeError, ValueError):
            return None

    @property
    def pet_panel_xp_regex(self) -> "re.Pattern[str] | None":
        pattern = (self.pet_panel or {}).get("xp_to_next_regex")
        try:
            return re.compile(pattern) if pattern else None
        except re.error:
            return None

    @property
    def pet_panel_hp_roi_px(self) -> tuple[float, float, float, float] | None:
        roi = (self.pet_panel or {}).get("hp_roi_px")
        try:
            return tuple(float(v) for v in roi) if roi else None
        except (TypeError, ValueError):
            return None

    @property
    def pet_panel_hp_regex(self) -> "re.Pattern[str] | None":
        pattern = (self.pet_panel or {}).get("hp_regex")
        try:
            return re.compile(pattern) if pattern else None
        except re.error:
            return None

    @property
    def pet_panel_level_regex(self) -> "re.Pattern[str] | None":
        pattern = (self.pet_panel or {}).get("level_regex", r"(?P<level>\d+)")
        try:
            return re.compile(pattern) if pattern else None
        except re.error:
            return None

    @property
    def calibrated(self) -> bool:
        return self.status == "CALIBRATED" and not self.problems

    def preset_px(self, preset: int | None = None) -> tuple[float, float] | None:
        return self.presets.get(int(preset or self.preset_default))

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "status": self.status,
                "calibrated": self.calibrated, "problems": list(self.problems),
                "capture_px": list(self.capture_px),
                "required_fields": list(REQUIRED_FIELDS),
                "presets": sorted(self.presets),
                "budget": {"max_cycles": self.max_cycles,
                           "max_reentries": self.max_reentries,
                           "max_failures": self.max_failures}}


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _px(value: Any, where: str, problems: list[str],
        bounds: tuple[int, int] | None = None) -> tuple[float, float] | None:
    if value is None:
        problems.append(f"{where} is null (needs a game_px coordinate)")
        return None
    if (not isinstance(value, Sequence) or isinstance(value, (str, bytes))
            or len(value) != 2 or not all(_num(v) for v in value)):
        problems.append(f"{where} must be [x, y] numbers, got {value!r}")
        return None
    x, y = float(value[0]), float(value[1])
    if bounds and not (0 <= x <= bounds[0] and 0 <= y <= bounds[1]):
        problems.append(f"{where} {value!r} is outside capture {bounds}")
    return (x, y)


def _roi(value: Any, where: str, problems: list[str],
         bounds: tuple[int, int] | None = None
         ) -> tuple[float, float, float, float] | None:
    if value is None:
        problems.append(f"{where} is null (needs [x0, y0, x1, y1] game_px)")
        return None
    if (not isinstance(value, Sequence) or isinstance(value, (str, bytes))
            or len(value) != 4 or not all(_num(v) for v in value)):
        problems.append(f"{where} must be [x0, y0, x1, y1] numbers, got {value!r}")
        return None
    x0, y0, x1, y1 = (float(v) for v in value)
    if not (x1 > x0 and y1 > y0):
        problems.append(f"{where} is empty: {value!r}")
        return None
    if bounds and not (0 <= x0 < x1 <= bounds[0] and 0 <= y0 < y1 <= bounds[1]):
        problems.append(f"{where} {value!r} is outside capture {bounds}")
    return (x0, y0, x1, y1)


def _words(value: Any, where: str, problems: list[str],
           *, required: bool = True) -> tuple[str, ...]:
    if value is None:
        if required:
            problems.append(f"{where} is missing")
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        problems.append(f"{where} must be a list of strings")
        return ()
    out = tuple(str(v).strip() for v in value if str(v).strip())
    if required and not out:
        problems.append(f"{where} is empty")
    return out


def _regex(value: Any, where: str, problems: list[str]) -> re.Pattern[str] | None:
    if not isinstance(value, str) or not value.strip():
        problems.append(f"{where} is missing")
        return None
    try:
        return re.compile(value)
    except re.error as exc:
        problems.append(f"{where} does not compile: {exc}")
        return None


def load_calibration(path: str | Path | None = None, *,
                     require_calibrated: bool = True) -> Calibration:
    target = Path(path) if path else DEFAULT_CALIBRATION
    problems: list[str] = []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CalibrationError(["calibration file not found"], path=target) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationError([f"calibration file is unreadable: {exc}"],
                               path=target) from None
    if not isinstance(raw, dict):
        raise CalibrationError(["calibration root must be a JSON object"], path=target)

    if raw.get("schema_version") != 1:
        problems.append(f"schema_version must be 1, got {raw.get('schema_version')!r}")
    status = str(raw.get("status") or "").upper()
    if status not in ("CALIBRATED", "UNCALIBRATED"):
        problems.append(f"status must be CALIBRATED or UNCALIBRATED, got {status!r}")

    bounds: tuple[int, int] | None = None
    reference = raw.get("reference") if isinstance(raw.get("reference"), Mapping) else {}
    capture = reference.get("capture_px") if isinstance(reference, Mapping) else None
    if (isinstance(capture, Sequence) and not isinstance(capture, (str, bytes))
            and len(capture) == 2 and all(isinstance(v, int) and v > 0 for v in capture)):
        bounds = (int(capture[0]), int(capture[1]))
    else:
        problems.append(f"reference.capture_px must be [w, h] positive ints, "
                        f"got {capture!r}")

    tower = raw.get("tower") if isinstance(raw.get("tower"), Mapping) else {}
    dialog = tower.get("dialog") if isinstance(tower.get("dialog"), Mapping) else {}
    inside = tower.get("inside") if isinstance(tower.get("inside"), Mapping) else {}
    battle = tower.get("battle") if isinstance(tower.get("battle"), Mapping) else {}
    result = tower.get("result") if isinstance(tower.get("result"), Mapping) else {}
    budget = tower.get("budget") if isinstance(tower.get("budget"), Mapping) else {}

    presets: dict[int, tuple[float, float]] = {}
    presets_raw = dialog.get("floor_presets")
    if not isinstance(presets_raw, Mapping) or not presets_raw:
        problems.append("tower.dialog.floor_presets is missing or empty")
    else:
        for key, value in presets_raw.items():
            try:
                preset = int(key)
            except (TypeError, ValueError):
                problems.append(f"tower.dialog.floor_presets key {key!r} is not a floor")
                continue
            px = _px(value, f"tower.dialog.floor_presets.{key}", problems, bounds)
            if px is not None:
                presets[preset] = px

    signature = inside.get("signature")
    signature_problems: list[str] = []
    if signature is not None:
        if not isinstance(signature, Mapping):
            signature_problems.append("tower.inside.signature must be an object")
        else:
            mode = str(signature.get("mode") or "").lower()
            if mode not in ("roi_mean_color", "template"):
                signature_problems.append(
                    "tower.inside.signature.mode must be roi_mean_color or template")
            elif mode == "roi_mean_color":
                _roi(signature.get("roi_px"),
                     "tower.inside.signature.roi_px", signature_problems, bounds)
                expect = signature.get("expect_rgb")
                if (not isinstance(expect, Sequence)
                        or isinstance(expect, (str, bytes)) or len(expect) != 3
                        or not all(isinstance(v, int) and 0 <= v <= 255
                                   for v in expect)):
                    signature_problems.append(
                        "tower.inside.signature.expect_rgb must be [r, g, b] ints 0..255")
                tolerance = signature.get("tolerance", 40)
                if not _num(tolerance) or float(tolerance) <= 0:
                    signature_problems.append(
                        "tower.inside.signature.tolerance must be > 0")
                order = str(signature.get("channel_order", "RGB")).upper()
                if order not in ("RGB", "BGR"):
                    signature_problems.append(
                        "tower.inside.signature.channel_order must be RGB or BGR")
            else:
                template = signature.get("template_png")
                if not isinstance(template, str) or not template.strip():
                    signature_problems.append(
                        "tower.inside.signature.template_png is missing")
                else:
                    candidate = Path(template)
                    if not candidate.is_absolute():
                        candidate = ROOT / candidate
                    if not candidate.exists():
                        signature_problems.append(
                            f"tower.inside.signature.template_png not found: {candidate}")
                score = signature.get("min_score", 0.8)
                if not _num(score) or not 0 < float(score) <= 1:
                    signature_problems.append(
                        "tower.inside.signature.min_score must be inside (0, 1]")
            # An operator who added the block intends to use it: incomplete is a
            # problem, not silently ignored (they would discover it at midnight).
            problems.extend(signature_problems)

    levelup_markers = _words(result.get("levelup_markers"),
                             "tower.result.levelup_markers", problems,
                             required=False)

    approach_px = _px(tower.get("approach_px"), "tower.approach_px", problems, bounds)
    sweep_raw = tower.get("guardian_sweep_px")
    if sweep_raw is None:
        sweep_raw = tower.get("guardian_candidates_px") or []
    sweep: list[tuple[float, float]] = []
    if not isinstance(sweep_raw, Sequence) or isinstance(sweep_raw, (str, bytes)):
        problems.append("tower.guardian_sweep_px must be a list of [x, y] points")
    else:
        for index, point in enumerate(sweep_raw):
            px = _px(point, f"tower.guardian_sweep_px[{index}]", problems, bounds)
            if px is not None:
                sweep.append(px)
        if not sweep:
            problems.append("tower.guardian_sweep_px is empty (needs the guardian "
                            "candidate points)")

    pet_panel = tower.get("pet_panel")
    verify_every = 3
    pet_panel_problems: list[str] = []
    if pet_panel is not None:
        if not isinstance(pet_panel, Mapping):
            pet_panel_problems.append("tower.pet_panel must be an object")
        else:
            _px(pet_panel.get("open_px"), "tower.pet_panel.open_px",
                pet_panel_problems, bounds)
            close_px = pet_panel.get("close_px")
            if close_px is not None:
                _px(close_px, "tower.pet_panel.close_px", pet_panel_problems, bounds)
            xp_roi_value = (pet_panel.get("xp_roi_px")
                            or pet_panel.get("xp_to_next_roi_px"))
            xp_roi_checked = _roi(xp_roi_value, "tower.pet_panel.xp_roi_px",
                                  pet_panel_problems, bounds)
            xp_re = _regex(pet_panel.get("xp_to_next_regex"),
                           "tower.pet_panel.xp_to_next_regex", pet_panel_problems)
            if xp_re is not None and "xp" not in xp_re.groupindex:
                pet_panel_problems.append(
                    "tower.pet_panel.xp_to_next_regex needs a named group 'xp'")
            level_re = _regex(pet_panel.get("level_regex", "(?P<level>\d+)"),
                              "tower.pet_panel.level_regex", pet_panel_problems)
            if level_re is not None and "level" not in level_re.groupindex:
                pet_panel_problems.append(
                    "tower.pet_panel.level_regex needs a named group 'level'")
            every = pet_panel.get("verify_every", 3)
            if not isinstance(every, int) or isinstance(every, bool) or every < 1:
                pet_panel_problems.append(
                    "tower.pet_panel.verify_every must be a positive int")
            else:
                verify_every = every
            level_roi_checked = None
            if pet_panel.get("level_roi_px") is not None:
                level_roi_checked = _roi(pet_panel.get("level_roi_px"),
                                         "tower.pet_panel.level_roi_px",
                                         pet_panel_problems, bounds)
            problems.extend(pet_panel_problems)
    approach_wait = tower.get("approach_wait_s", 1.8)
    if not _num(approach_wait) or float(approach_wait) <= 0:
        problems.append("tower.approach_wait_s must be a positive number")
    max_unverified = budget.get("max_unverified", 3)
    if not isinstance(max_unverified, int) or isinstance(max_unverified, bool) \
            or not 1 <= max_unverified <= 10:
        problems.append("tower.budget.max_unverified must be an int 1..10")

    raw_candidates = tower.get("guardian_candidates_px") or []
    guardian_candidates: list[tuple[float, float]] = []
    if not isinstance(raw_candidates, (list, tuple)):
        problems.append("tower.guardian_candidates_px must be a list of [x, y] pairs")
    else:
        for index, entry in enumerate(raw_candidates):
            point = _px(entry, f"tower.guardian_candidates_px[{index}]", [], bounds)
            if point is not None and point not in guardian_candidates:
                guardian_candidates.append(point)

    calibration = Calibration(
        path=target,
        status=status if status in ("CALIBRATED", "UNCALIBRATED") else "UNCALIBRATED",
        capture_px=bounds or (2400, 1600),
        guardian_px=_px(tower.get("guardian_px"), "tower.guardian_px", problems, bounds),
        guardian_sweep_px=tuple(sweep),
        approach_px=approach_px,
        approach_wait_s=float(approach_wait) if _num(approach_wait) else 1.8,
        guardian_gap_s=float(tower.get("guardian_gap_s", 1.2))
        if _num(tower.get("guardian_gap_s", 1.2)) else 1.2,
        pick_floor_px=_px(dialog.get("pick_floor_px"), "tower.dialog.pick_floor_px",
                          problems, bounds),
        continue_px=_px(dialog.get("continue_px"), "tower.dialog.continue_px",
                        problems, bounds),
        dialog_markers=_words(dialog.get("markers"), "tower.dialog.markers", problems),
        presets=presets,
        preset_default=int(dialog.get("preset", 1))
        if isinstance(dialog.get("preset", 1), int) else 1,
        floor_roi_px=_roi(inside.get("floor_roi_px"), "tower.inside.floor_roi_px",
                          problems, bounds),
        floor_regex=_regex(inside.get("floor_regex"), "tower.inside.floor_regex",
                           problems),
        inside_timeout_s=float(inside.get("timeout_s", 6.0))
        if _num(inside.get("timeout_s", 6.0)) else 6.0,
        battle_markers=_words(battle.get("battle_markers"),
                              "tower.battle.battle_markers", problems),
        enter_wait_s=float(battle.get("enter_wait_s", 12.0))
        if _num(battle.get("enter_wait_s", 12.0)) else 12.0,
        battle_max_seconds=float(battle.get("max_seconds", 300.0))
        if _num(battle.get("max_seconds", 300.0)) else 300.0,
        result_roi_px=_roi(result.get("roi_px"), "tower.result.roi_px", problems,
                           bounds),
        xp_regex=_regex(result.get("xp_regex"), "tower.result.xp_regex", problems),
        xp_to_next_regex=_regex(result.get("xp_to_next_regex"),
                                "tower.result.xp_to_next_regex", problems),
        confirm_words=_words(result.get("confirm_words"),
                             "tower.result.confirm_words", problems),
        defeat_markers=_words(result.get("defeat_markers"),
                              "tower.result.defeat_markers", problems),
        pp_markers=_words(result.get("pp_markers"), "tower.result.pp_markers",
                          problems, required=False),
        max_confirm_clicks=int(result.get("max_confirm_clicks", 6))
        if isinstance(result.get("max_confirm_clicks", 6), int) else 6,
        signature=dict(signature) if isinstance(signature, Mapping) else None,
        guardian_candidates_px=tuple(guardian_candidates),
        levelup_markers=levelup_markers,
        max_cycles=int(budget.get("max_cycles", 0))
        if isinstance(budget.get("max_cycles", 0), int) else 0,
        max_reentries=int(budget.get("max_reentries", 3))
        if isinstance(budget.get("max_reentries", 3), int) else 3,
        max_failures=int(budget.get("max_failures", 3))
        if isinstance(budget.get("max_failures", 3), int) else 3,
        max_unverified=max_unverified if isinstance(max_unverified, int) else 3,
        cycle_timeout_s=float(budget.get("cycle_timeout_s", 180.0))
        if _num(budget.get("cycle_timeout_s", 180.0)) else 180.0,
        raw=dict(raw),
        # Last on purpose: kwargs evaluate left-to-right, and this must see every
        # validator's problem (a snapshot before them silently hid missing fields).
        problems=tuple(problems))

    if require_calibrated and not calibration.calibrated:
        raise CalibrationError(
            (["status is not CALIBRATED (live calibration has not been filled in)"]
             if calibration.status != "CALIBRATED" else []) + list(problems),
            path=target)
    return calibration


def calibration_problems(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else DEFAULT_CALIBRATION
    try:
        calibration = load_calibration(target, require_calibrated=False)
    except CalibrationError as exc:
        return {"path": str(target), "usable": False, "calibrated": False,
                "problems": exc.problems, "required_fields": list(REQUIRED_FIELDS)}
    return {"path": str(target), "usable": not calibration.problems,
            "calibrated": calibration.calibrated,
            "problems": list(calibration.problems),
            "required_fields": list(REQUIRED_FIELDS),
            "capture_px": list(calibration.capture_px),
            "presets": sorted(calibration.presets),
            "guardian_sweep_points": len(calibration.guardian_sweep_px),
            "engage_candidates": (1 + len(calibration.guardian_candidates_px)
                                  if calibration.guardian_candidates_px
                                  else max(1, len(calibration.guardian_sweep_px))),
            "pet_panel": bool(calibration.pet_panel),
            "verify_every": calibration.verify_every}


# --------------------------------------------------------------------------- #
# ledger
# --------------------------------------------------------------------------- #
def append_ledger(path: str | Path | None, record: Mapping[str, Any]) -> dict | None:
    if path is None:
        return None
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    if "ts" not in payload:
        now = datetime.now().astimezone()
        payload["ts"] = now.isoformat(timespec="seconds")
        payload["ts_epoch"] = round(now.timestamp(), 3)
    line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    fd = os.open(str(target), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line
        view = memoryview(line)
        while view:
            written = os.write(fd, view)
            if written <= 0:  # pragma: no cover
                raise OSError(f"short write to {target}")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return payload


def read_ledger(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    out: list[dict[str, Any]] = []
    try:
        with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
    except FileNotFoundError:
        return []
    return out


# --------------------------------------------------------------------------- #
# session model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TowerLimits:
    max_cycles: int = 0
    max_reentries: int = 3
    max_failures: int = 3
    max_unverified: int = 3
    verify_every: int = 3
    auto_recover: bool = False
    max_recovers: int = 3
    preset: int | None = None
    deadline_epoch: float | None = None
    pause_files: tuple[str, ...] = PAUSE_FILES
    stop_file: str = STOP_FILE
    cycle_timeout_s: float = 180.0


@dataclass
class CycleResult:
    cycle: int
    outcome: Outcome
    floor_before: int | None = None
    floor_after: int | None = None
    floor_source: str | None = None
    xp_text: str | None = None
    xp_value: float | None = None
    xp_source: str | None = None
    dialogs: list[str] = field(default_factory=list)
    battle: dict[str, Any] = field(default_factory=dict)
    defeat_marker: bool = False
    pp_marker: bool = False
    confirm_clicks: int = 0
    clicks: int = 0
    engage_clicks: int = 0
    reentries: int = 0
    elapsed_s: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "outcome": self.outcome.value,
            "floor_before": self.floor_before,
            "floor_after": self.floor_after,
            "floor_source": self.floor_source,
            "xp_text": self.xp_text,
            "xp_value": self.xp_value,
            "xp_source": self.xp_source,
            "dialogs": list(self.dialogs),
            "battle": dict(self.battle),
            "defeat_marker": self.defeat_marker,
            "pp_marker": self.pp_marker,
            "confirm_clicks": self.confirm_clicks,
            "clicks": self.clicks,
            "engage_clicks": self.engage_clicks,
            "reentries": self.reentries,
            "elapsed_s": round(self.elapsed_s, 2),
            "reason": self.reason,
        }


class TowerGrind:
    """Unattended tower loop.  Every click target comes from the calibration."""

    def __init__(self, calibration: Calibration, *,
                 limits: TowerLimits | None = None,
                 ledger_path: str | Path | None = DEFAULT_LEDGER,
                 battle_runner: Callable[[Any, float], Mapping[str, Any]] | None = None,
                 frame_reader: Callable[[], Any] | None = None,
                 heal_runner: Callable[[Any], Any] | None = None,
                 travel_runner: Callable[[Any, str], Any] | None = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.calibration = calibration
        self.limits = limits or TowerLimits()
        self.ledger_path = ledger_path
        self.clock = clock
        self.sleep = sleep
        self._battle_runner = battle_runner
        self._frame_reader = frame_reader
        self._heal_runner = heal_runner
        self._travel_runner = travel_runner
        self.cycles = 0
        self.wins = 0
        self.xp_total = 0.0
        self.reentries = 0
        self.failures = 0
        self.clicks = 0
        self.started_epoch = 0.0
        self.window_end = 0.0
        self.floor_first: int | None = None
        self.floor_last: int | None = None
        self._last_pet_reading: dict[str, Any] | None = None
        self._pet_checks: list[dict[str, Any]] = []
        self._stale_verifications = 0
        self.recovers = 0

    # -- plumbing ---------------------------------------------------------- #
    def _ocr(self, ctx: Any, *, min_conf: float = 0.3) -> list[dict[str, Any]]:
        return list(ctx.session.ocr(min_conf=min_conf))

    @staticmethod
    def _text(items: Iterable[Mapping[str, Any]]) -> list[str]:
        return [str(i.get("text") or "").strip() for i in items
                if str(i.get("text") or "").strip()]

    def _in_roi(self, items: Iterable[Mapping[str, Any]],
                roi: tuple[float, float, float, float] | None) -> list[dict[str, Any]]:
        if roi is None:
            return list(items)
        x0, y0, x1, y1 = roi
        out = []
        for item in items:
            px = item.get("px") or (None, None)
            try:
                x, y = float(px[0]), float(px[1])
            except (TypeError, ValueError, IndexError):
                continue
            if x0 <= x <= x1 and y0 <= y <= y1:
                out.append(dict(item))
        return out

    def _sample(self, ctx: Any, predicate: Callable[[list[dict]], Any], *,
                timeout_s: float, gap_s: float = 0.6,
                label: str = "") -> tuple[Any, list[str]]:
        max_samples = max(3, int(max(timeout_s, 0.0) / max(gap_s, 0.05)) + 2)
        deadline = self.clock() + max(timeout_s, 0.0)
        previous: Any = None
        last_texts: list[str] = []
        for _ in range(max_samples):
            items = self._ocr(ctx)
            last_texts = self._text(items)
            try:
                value = predicate(items)
            except Exception:  # noqa: BLE001 - a bad predicate is no evidence
                value = None
            if value is not None and value == previous:
                return value, last_texts
            previous = value
            if self.clock() >= deadline:
                break
            self.sleep(gap_s)
        return None, last_texts

    def _click(self, ctx: Any, px: tuple[float, float], reason: str,
               current: CycleResult) -> None:
        if not reason.startswith(("pick_floor", "preset", "approach", "guardian",
                                  "confirm", "continue", "pet_panel_open",
                                  "pet_panel_close")):
            raise RuntimeError(f"click reason {reason!r} is outside the tower allowlist")
        ctx.session.click_px(px[0], px[1], reason)
        self.clicks += 1
        current.clicks += 1

    def _paused(self) -> str | None:
        for path in self.limits.pause_files:
            if os.path.exists(path):
                return path
        return None

    def _stopped(self) -> bool:
        return os.path.exists(self.limits.stop_file)

    def _deadline_hit(self) -> bool:
        return (self.limits.deadline_epoch is not None
                and self.clock() >= self.limits.deadline_epoch)

    def _window_over(self) -> bool:
        if self.window_end is not None and self.clock() >= self.window_end:
            return True
        return self._deadline_hit()

    # -- screen reading ---------------------------------------------------- #
    def _dialog_present(self, items: list[dict]) -> bool:
        text = " ".join(self._text(items))
        return any(marker in text for marker in self.calibration.dialog_markers)

    def _battle_present(self, items: list[dict]) -> bool:
        text = " ".join(self._text(items))
        return any(marker in text for marker in self.calibration.battle_markers)

    def _read_floor_once(self, items: list[dict]) -> int | None:
        roi = self.calibration.floor_roi_px
        regex = self.calibration.floor_regex
        if roi is None or regex is None:  # pragma: no cover - loader refuses first
            return None
        scope = self._in_roi(items, roi)
        for text in self._text(scope):
            match = regex.search(text.strip())
            if match and match.groupdict().get("floor"):
                return int(match.group("floor"))
        return None

    def _read_floor(self, ctx: Any) -> int | None:
        """Two consecutive agreeing readings, or None (never a guess)."""

        value, _texts = self._sample(
            ctx, self._read_floor_once,
            timeout_s=self.calibration.inside_timeout_s, label="floor")
        return value

    # -- "we are inside the tower" visual signature ------------------------ #
    def _read_frame(self, ctx: Any) -> Any:
        try:
            self._frame_error = None
            return self._frame_reader() if self._frame_reader is not None \
                else ctx.session.latest()
        except Exception as exc:  # noqa: BLE001 - no frame means no signature evidence
            # Keep the reason: a placeholder/blank capture must be visible in the
            # BLOCKED text, not collapsed into "no frame readable".
            self._frame_error = f"{type(exc).__name__}: {exc}"
            return None

    @staticmethod
    def _mean_rgb(frame: Any, roi: Sequence[float]) -> tuple[float, float, float] | None:
        """Mean colour of a game_px ROI.  Accepts a fake or a numpy frame."""

        x0, y0, x1, y1 = (int(round(float(v))) for v in roi)
        if hasattr(frame, "roi_mean_rgb"):
            try:
                values = frame.roi_mean_rgb(x0, y0, x1, y1)
                return tuple(float(v) for v in list(values)[:3])  # type: ignore[return-value]
            except Exception:  # noqa: BLE001
                return None
        try:
            region = frame[y0:y1, x0:x1]
            if hasattr(region, "size") and int(region.size) == 0:
                return None  # ROI outside this frame (e.g. a smaller window)
            if hasattr(region, "mean"):
                mean = region.mean(axis=(0, 1))
                values = tuple(float(v) for v in list(mean)[:3])
                if any(v != v for v in values):  # NaN: empty/invalid region
                    return None
                return values  # type: ignore[return-value]
        except Exception:  # noqa: BLE001
            pass
        try:  # nested-list fallback (no numpy)
            rows = frame[y0:y1]
            pixels = [pixel for row in rows for pixel in row[x0:x1]]
            count = len(pixels) or 1
            return tuple(sum(float(pixel[i]) for pixel in pixels) / count
                         for i in range(3))  # type: ignore[return-value]
        except Exception:  # noqa: BLE001
            return None

    def signature_probe_settled(self, ctx: Any, *, tries: int = 3,
                                gap_s: float = 1.0) -> tuple[bool, str]:
        """signature_probe with retries.

        Live 2026-11-07: a single frame taken right after `open -a` (or during a
        scene transition) is temporarily dark, so the signature misses even though
        the client is inside the tower (measured (61,107,150) vs (159,181,170) on
        the transient frame; (159,185,173) one second later).  Sampling three times
        with a gap turns that false negative into a safe retry instead of a BLOCKED.
        """

        evidence = "signature: not sampled"
        for attempt in range(max(1, tries)):
            hit, evidence = self.signature_probe(ctx)
            if hit:
                return True, evidence
            if attempt + 1 < max(1, tries):
                self.sleep(gap_s)
        return False, evidence

    def signature_probe(self, ctx: Any) -> tuple[bool, str]:
        """Is this frame 'inside the tower'?  (calibrated visual signature)"""

        signature = self.calibration.signature
        if not signature:
            return False, "no tower signature calibrated (tower.inside.signature)"
        mode = str(signature.get("mode") or "").lower()
        frame = self._read_frame(ctx)
        if frame is None:
            detail = getattr(self, "_frame_error", None)
            return False, ("signature: no frame readable"
                           + (f" ({detail})" if detail else ""))
        if mode == "roi_mean_color":
            roi = signature.get("roi_px")
            mean = self._mean_rgb(frame, roi) if roi else None
            if mean is None:
                return False, f"signature: cannot sample roi {roi!r}"
            expect = tuple(int(v) for v in (signature.get("expect_rgb") or ()))
            tolerance = float(signature.get("tolerance", 40))
            sampled = tuple(mean)
            # cv2 frames are BGR; expect_rgb is RGB.  Convert unless the operator
            # explicitly measured in BGR order.
            if str(signature.get("channel_order", "RGB")).upper() == "RGB":
                sampled = (mean[2], mean[1], mean[0])
            hit = len(expect) == 3 and all(
                abs(sampled[i] - expect[i]) <= tolerance for i in range(3))
            return hit, (f"signature {'hit' if hit else 'miss'}: mean_rgb="
                         f"{tuple(round(v) for v in sampled)} expect={expect} "
                         f"tol={tolerance:.0f}")
        if mode == "template":
            try:
                import cv2  # lazy: only template mode needs it
                import numpy as np

                template = Path(str(signature["template_png"]))
                if not template.is_absolute():
                    template = ROOT / template
                needle = cv2.imread(str(template))
                hay = frame if hasattr(frame, "shape") else np.asarray(frame)
                if needle is None or hay is None:
                    return False, "signature: template/frame unreadable"
                roi = signature.get("roi_px")
                region = hay
                if roi:
                    x0, y0, x1, y1 = (int(round(float(v))) for v in roi)
                    region = hay[y0:y1, x0:x1]
                if region.shape[0] < needle.shape[0] or \
                        region.shape[1] < needle.shape[1]:
                    return False, "signature: template larger than the ROI"
                result = cv2.matchTemplate(region, needle, cv2.TM_CCOEFF_NORMED)
                score = float(result.max())
                threshold = float(signature.get("min_score", 0.8))
                return score >= threshold, (f"signature template score {score:.3f} "
                                            f">= {threshold:.2f}")
            except Exception as exc:  # noqa: BLE001
                return False, f"signature: template mode failed: {exc}"
        return False, f"signature: unknown mode {mode!r}"

    def _classify(self, items: list[dict]) -> tuple[str, int | None] | None:
        if self._dialog_present(items):
            return ("dialog", None)
        floor = self._read_floor_once(items)
        if floor is not None:
            return ("floor", floor)
        if self._battle_present(items):
            return ("battle", None)
        return None

    def _screen(self, ctx: Any) -> tuple[str, int | None] | None:
        """Two consecutive agreeing screen classifications, or None (unknown)."""

        value, _texts = self._sample(ctx, self._classify, timeout_s=2.4,
                                     label="screen")
        return value

    def _at_dialog(self, ctx: Any) -> bool:
        """Two consecutive readings of the tower dialog markers."""

        value, _texts = self._sample(
            ctx, lambda items: True if self._dialog_present(items) else None,
            timeout_s=2.4, label="dialog")
        return value is True

    def _at_battle(self, ctx: Any) -> bool:
        value, _texts = self._sample(
            ctx, lambda items: True if self._battle_present(items) else None,
            timeout_s=2.4, label="battle")
        return value is True

    def _settle_result(self, ctx: Any, current: CycleResult) -> None:
        """Click-and-read the whole settlement chain, capturing XP whenever it shows.

        The post-battle screen is a *chain*: a 「确认」 first, then the
        「XIN精灵获得经验：N」 panel, then reward dialogs.  No single frame contains
        everything, so this loop samples, records any calibrated XP text/markers it
        sees, then clicks a calibrated confirm word and sleeps 0.6-0.8 s before the
        next sample.  The first XP text wins; later frames may overwrite the panels
        but not the evidence.
        """

        empty_samples = 0
        for _ in range(max(1, self.calibration.max_confirm_clicks)):
            if self._window_over() or self._stopped():
                return
            items = self._ocr(ctx)
            scope = self._in_roi(items, self.calibration.result_roi_px)
            lines = self._text(scope)
            joined = " ".join(lines)
            if any(marker in joined for marker in self.calibration.defeat_markers):
                current.defeat_marker = True
            if any(marker in joined for marker in self.calibration.pp_markers):
                current.pp_marker = True
            for line in lines:
                match = (self.calibration.xp_regex.search(line)
                         if self.calibration.xp_regex is not None else None)
                if match and current.xp_text is None:
                    current.xp_text = line
                    try:
                        current.xp_value = float(match.group("xp"))
                        current.xp_source = "result_ocr"
                    except (IndexError, ValueError):
                        current.xp_value = None
                if any(word in line for word in self.calibration.levelup_markers) \
                        and line not in current.dialogs:
                    current.dialogs.append(line)
            for line in lines:
                if any(word in line for word in self.calibration.confirm_words) \
                        or "获得" in line or "放入" in line:
                    if line not in current.dialogs:
                        current.dialogs.append(line)
            hit = None
            for word in self.calibration.confirm_words:
                for item in items:
                    text = str(item.get("text") or "")
                    if word in text:
                        hit = (word, item)
                        break
                if hit:
                    break
            if hit is None:
                if not lines and empty_samples < 2:
                    # The panel may still be animating in; give it two short looks
                    # before declaring the chain finished.
                    empty_samples += 1
                    self.sleep(0.7)
                    continue
                return
            word, item = hit
            px = item.get("px") or (None, None)
            if px[0] is None:
                return
            try:
                self._click(ctx, (float(px[0]), float(px[1])),
                            f"confirm:{word}", current)
                current.confirm_clicks += 1
            except Exception:  # noqa: BLE001 - a refused confirm must not crash a run
                return
            self.sleep(0.7)

    # -- self-healing RECOVER stage ---------------------------------------- #
    def _observe_window(self, ctx: Any) -> tuple[bool, str]:
        """5 s observation window before RECOVER clicks: screenshot + no popups.

        RECOVER is the one place that clicks outside the calibrated tower path, so it
        waits, captures a frame and refuses to act while a dialog/marker is on screen.
        """

        self.sleep(5.0)
        try:
            path = "/tmp/tower_grind_recover_observe.png"
            exported = getattr(ctx.session, "export_png", None)
            if exported is not None:
                exported(path)
            else:
                client = getattr(ctx.session, "client", None)
                if client is not None:
                    client.send("shot", path=path)
        except Exception:  # noqa: BLE001 - a failed screenshot is not a reason to click
            return False, "could not capture the pre-recover observation frame"
        try:
            items = self._ocr(ctx)
        except Exception as exc:  # noqa: BLE001
            return False, f"could not OCR the pre-recover frame: {exc}"
        text = " ".join(self._text(items))
        blocking = tuple(self.calibration.confirm_words) + tuple(
            self.calibration.defeat_markers)
        hit = next((word for word in blocking if word in text), None)
        if hit:
            # 2026-11-07 live: the login-time banner
            # 「检测到本日还未开启8倍经验与4倍学习力…[确认]」 made every RECOVER
            # refuse before doing anything.  That banner is benign - its 确认 only
            # dismisses the notice - so it is dismissed once and the window is
            # re-observed.  Any other popup still blocks (no blind clicking).
            benign = ("还未开启" in text or "8倍经验" in text
                      or "检测到" in text)
            if hit in self.calibration.confirm_words and benign:
                target = None
                for item in items:
                    if hit in str(item.get("text") or ""):
                        px = item.get("px") or (None, None)
                        if px and px[0] is not None:
                            target = (float(px[0]), float(px[1]))
                            break
                if target is not None:
                    marker = CycleResult(cycle=self.cycles,
                                         outcome=Outcome.UNVERIFIED_ACTIVITY)
                    try:
                        self._click(ctx, target, f"observe_dismiss:{hit}", marker)
                    except Exception:  # noqa: BLE001 - dismissal is best effort
                        return False, (f"popup/marker {hit!r} is on screen before "
                                       f"RECOVER and could not be dismissed")
                    self.sleep(1.5)
                    try:
                        items = self._ocr(ctx)
                        text = " ".join(self._text(items))
                    except Exception as exc:  # noqa: BLE001
                        return False, f"could not OCR after dismissal: {exc}"
                    hit = next((word for word in blocking if word in text), None)
                    if hit is None:
                        return True, "dismissed the benign 8x-exp banner"
            return False, f"popup/marker {hit!r} is on screen before RECOVER"
        return True, ""

    def _screen_summary(self, ctx: Any, lines: int = 5) -> list[str]:
        try:
            return [text for text in self._text(self._ocr(ctx)) if text][:lines]
        except Exception:  # noqa: BLE001 - no screen is just an empty summary
            return []

    def recover_steps(self, ctx: Any, *, trigger: str = "manual",
                      hp_reading: Mapping[str, Any] | None = None,
                      observe: bool = True) -> dict[str, Any]:
        """The RECOVER chain as an explicit step report (also used by --recover-only).

        Steps: observe (5 s, no popup) -> heal (tolerated failure) -> travel to the
        tutor room -> tower dialog -> preset 1.  Only travel/dialog/preset failing
        ends the chain; a heal failure is recorded as `heal_unavailable` and the chain
        continues, because the point of RECOVER is to get back to a floor, and the
        hall/bottom-bar heal path is not always the one that is available.
        """

        steps: list[dict[str, Any]] = []

        def step(name: str, ok: bool, reason: str = "", **extra: Any) -> None:
            steps.append({"step": name, "ok": bool(ok), "reason": reason, **extra})

        if observe:
            observed, evidence = self._observe_window(ctx)
            step("observe", observed, evidence)
            if not observed:
                return {"ok": False, "steps": steps,
                        "screen_text": self._screen_summary(ctx)}

        pause_path = self._paused()
        if pause_path:
            step("cooperation", False, f"paused by {pause_path}")
            return {"ok": False, "paused": True, "steps": steps,
                    "screen_text": self._screen_summary(ctx)}
        if self._stopped():
            step("cooperation", False, f"stop flag {self.limits.stop_file}")
            return {"ok": False, "paused": True, "steps": steps,
                    "screen_text": self._screen_summary(ctx)}

        inside, signature = self.signature_probe(ctx)
        ratio = self.hp_ratio(hp_reading)
        if inside and ratio is not None and ratio <= 0.10:
            step("hp_gate", False,
                 "hp <= 10% while inside the tower (no heal entry, no bottom bar); "
                 "a human round trip is required", hp_ratio=ratio)
            return {"ok": False, "steps": steps,
                    "screen_text": self._screen_summary(ctx)}

        heal_ok: bool | None = None
        heal_unavailable = False
        if not inside:
            heal = (self._heal_runner or self._default_heal_runner)(ctx)
            heal_status = str(getattr(getattr(heal, "status", None), "value", ""))
            hp_after = getattr(heal, "after", None) or {}
            hp_list = hp_after.get("hp_after") if isinstance(hp_after, dict) else None
            lead_hp = None
            if isinstance(hp_list, (list, tuple)) and hp_list:
                first = hp_list[0]
                lead_hp = first.get("hp") if isinstance(first, dict) else first
            heal_ok = heal_status == "PASS"
            if lead_hp is not None:
                heal_ok = heal_ok and int(lead_hp) > 0
            bottom_bar = None
            try:
                from seerauto.skills.act import _bottom_bar_visible

                bottom_bar = _bottom_bar_visible(ctx.session)
            except Exception:  # noqa: BLE001 - detection is optional evidence
                bottom_bar = None
            if heal_ok:
                step("heal", True, f"hp_after={hp_after.get('hp_after')!r}",
                     bottom_bar=bottom_bar)
            else:
                # A failed heal is NOT fatal: the goal of RECOVER is to get back to
                # a tower floor, and the travel path may still work.  It is recorded
                # so a human can see the hall heal was unavailable.
                heal_unavailable = True
                step("heal", False,
                     f"heal_unavailable: {getattr(heal, 'reason', '')[:160]}",
                     heal_unavailable=True, bottom_bar=bottom_bar,
                     tolerated=True)
        else:
            step("heal", True, "skipped: already inside the tower")

        if self._stopped() or self._paused():
            step("cooperation", False, "pause/stop requested before travel")
            return {"ok": False, "paused": True, "steps": steps,
                    "screen_text": self._screen_summary(ctx)}

        travel = (self._travel_runner or self._default_travel_runner)(
            ctx, "tutor_room")
        travel_status = str(getattr(getattr(travel, "status", None), "value", ""))
        if travel_status != "PASS":
            reason = f"travel failed: {getattr(travel, 'reason', '')[:160]}"
            step("travel", False, reason)
            return {"ok": False, "steps": steps,
                    "screen_text": self._screen_summary(ctx)}
        step("travel", True, "tutor_room")

        # 2026-11-07 live: travel only moves the character into the training room; the
        # tower dialog is opened by *clicking the machine*.  The old chain checked for
        # the dialog without ever clicking the entrance, so `dialog` always failed
        # (live: three identical runs; a single manual click at (1195,1150) opened it
        # immediately).  Candidates are the operator-measured points, tried in order.
        opened = self._at_dialog(ctx)
        if not opened:
            entrance_points = ((1195.0, 1150.0), (1195.0, 1180.0), (1230.0, 1150.0))
            entrance_cycle = CycleResult(cycle=self.cycles, outcome=Outcome.FAILED)
            for point in entrance_points:
                if self._stopped() or self._paused():
                    break
                try:
                    self._click(ctx, point, "tower_entrance", entrance_cycle)
                except Exception:  # noqa: BLE001 - the click guard may refuse
                    continue
                self.sleep(2.0)
                if self._at_dialog(ctx):
                    opened = True
                    break
        step("entrance", opened,
             "" if opened else "tower entrance click did not open the dialog")
        if not opened:
            step("dialog", False, "tower dialog not found after travel")
            return {"ok": False, "steps": steps,
                    "screen_text": self._screen_summary(ctx)}
        step("dialog", True, "tower dialog visible")

        dummy = CycleResult(cycle=self.cycles, outcome=Outcome.FAILED)
        if not self._open_and_pick(ctx, dummy):
            step("preset1", False, dummy.reason or "could not pick floor 1")
            return {"ok": False, "steps": steps,
                    "screen_text": self._screen_summary(ctx)}
        step("preset1", True, f"floor {dummy.floor_before}",
             floor=dummy.floor_before)
        return {"ok": True, "steps": steps, "heal_ok": heal_ok,
                "heal_unavailable": heal_unavailable,
                "floor_picked": self.limits.preset or self.calibration.preset_default,
                "screen_text": []}

    def _recover(self, ctx: Any, current: CycleResult,
                 from_reason: str, *, trigger: str | None = None,
                 hp_reading: Mapping[str, Any] | None = None) -> tuple[str, str]:
        """Run RECOVER once, write one ledger row, and report (state, reason)."""

        if self.recovers >= max(1, self.limits.max_recovers):
            return "blocked", (f"recover budget exhausted ({self.recovers}/"
                               f"{self.limits.max_recovers})")
        self.recovers += 1
        report = self.recover_steps(ctx, trigger=trigger or from_reason,
                                    hp_reading=hp_reading)
        record = {"kind": "recover", "from": from_reason,
                  "trigger": trigger or from_reason, "attempt": self.recovers,
                  "hp": (hp_reading or {}).get("hp"),
                  "hp_max": (hp_reading or {}).get("hp_max"),
                  "heal_ok": report.get("heal_ok"),
                  "heal_unavailable": report.get("heal_unavailable", False),
                  "floor_picked": report.get("floor_picked"), "ok": report["ok"],
                  "steps": report.get("steps", []),
                  "screen_text": report.get("screen_text", []),
                  "reason": "" if report["ok"] else self._failed_step(report)}
        append_ledger(self.ledger_path, record)
        if report["ok"]:
            return "ok", ""
        if report.get("paused"):
            return "paused", record["reason"]
        return "blocked", record["reason"]

    @staticmethod
    def _failed_step(report: Mapping[str, Any]) -> str:
        for item in report.get("steps", []):
            if not item.get("ok") and not item.get("tolerated"):
                return (f"RECOVER failed at step {item.get('step')!r}: "
                        f"{item.get('reason', '')}")
        return "RECOVER failed"

    def _default_heal_runner(self, ctx: Any) -> Any:
        from seerauto.skills.act import HealPet

        return HealPet()(ctx)

    def _default_travel_runner(self, ctx: Any, destination: str) -> Any:
        from seerauto.skills.act import Travel

        return Travel()(ctx, destination=destination)

    def _open_and_pick(self, ctx: Any, current: CycleResult) -> bool:
        px = self.calibration.pick_floor_px
        if px is None:  # pragma: no cover - loader refuses first
            current.outcome = Outcome.BLOCKED
            current.reason = "tower.dialog.pick_floor_px is not calibrated"
            return False
        self._click(ctx, px, "pick_floor", current)
        self.sleep(1.0)
        preset = self.limits.preset or self.calibration.preset_default
        preset_px = self.calibration.preset_px(preset)
        if preset_px is None:
            current.outcome = Outcome.BLOCKED
            current.reason = (f"floor preset {preset} is not calibrated "
                              f"(known: {sorted(self.calibration.presets)})")
            return False
        self._click(ctx, preset_px, f"preset:{preset}", current)
        floor = self._read_floor(ctx)
        if floor is None:
            hit, evidence = self.signature_probe_settled(ctx)
            if not hit:
                current.outcome = Outcome.BLOCKED
                current.reason = ("painted floor number not readable after choosing "
                                  "a preset (two agreeing readings required) and the "
                                  "tower signature did not match; refusing to click. "
                                  f"[{evidence}]")
                return False
            current.floor_source = "signature"
            current.floor_before = None
            return True
        current.floor_before = floor
        current.floor_source = "floor_ocr"
        return True

    # -- independent progress evidence: the pet panel ---------------------- #
    def _read_pet_panel(self, ctx: Any,
                        current: CycleResult | None = None) -> dict[str, Any] | None:
        """Open the pet panel, read 升级所需经验值 (+level), close, return numbers.

        This is the independent evidence that a floor was actually cleared even when
        `RunBattle` has consumed the reward dialogs (live 2026-11-07: it returns
        `rewards=[]`, `xp=0.0`).  Two consecutive agreeing readings are required;
        the panel is closed with the calibrated close_px or ESC (key code 53).
        """

        panel = self.calibration.pet_panel
        xp_regex = self.calibration.pet_panel_xp_regex
        if not panel or xp_regex is None:
            return None
        open_px = panel.get("open_px")
        if open_px is None:  # pragma: no cover - validation refuses first
            return None
        sink = current or CycleResult(cycle=self.cycles, outcome=Outcome.FAILED)
        try:
            self._click(ctx, (float(open_px[0]), float(open_px[1])),
                        "pet_panel_open", sink)
        except Exception:  # noqa: BLE001 - panel bookkeeping must not crash a run
            return None
        self.sleep(float(panel.get("open_wait_s", 1.2)))

        def read_once(items: list[dict]) -> tuple[int | None, int | None] | None:
            scope = self._in_roi(items, self.calibration.pet_panel_xp_roi_px)
            xp_value = None
            for line in self._text(scope):
                match = xp_regex.search(line)
                if match and match.groupdict().get("xp"):
                    xp_value = int(match.group("xp"))
                    break
            level = None
            level_regex = self.calibration.pet_panel_level_regex
            if level_regex is not None:
                for line in self._text(self._in_roi(
                        items, self.calibration.pet_panel_level_roi_px)):
                    match = level_regex.search(line)
                    if match and match.groupdict().get("level"):
                        level = int(match.group("level"))
                        break
            hp_value = hp_max = None
            hp_regex = self.calibration.pet_panel_hp_regex
            if hp_regex is not None:
                for line in self._text(self._in_roi(
                        items, self.calibration.pet_panel_hp_roi_px)):
                    match = hp_regex.search(line)
                    if match and match.groupdict().get("hp"):
                        try:
                            hp_value = int(match.group("hp"))
                            hp_max = int(match.groupdict().get("hpmax") or 0) or None
                        except (TypeError, ValueError):
                            hp_value = hp_max = None
                        break
            if xp_value is None and level is None and hp_value is None:
                return None
            return (xp_value, level, hp_value, hp_max)

        reading, texts = self._sample(
            ctx, read_once,
            timeout_s=float(panel.get("read_timeout_s", 4.0)), label="pet_panel")
        self._close_pet_panel(ctx, panel)
        if reading is None:
            return None
        return {"xp_to_next": reading[0], "level": reading[1],
                "hp": reading[2], "hp_max": reading[3], "texts": texts[:8]}

    @staticmethod
    def hp_ratio(reading: Mapping[str, Any] | None) -> float | None:
        """HP fraction from a pet-panel reading, or None when unreadable."""

        if not reading:
            return None
        hp = reading.get("hp")
        hp_max = reading.get("hp_max") or (reading.get("level") and None)
        if hp is None or not hp_max:
            return None
        return float(hp) / float(hp_max)

    def _close_pet_panel(self, ctx: Any, panel: Mapping[str, Any]) -> None:
        close_px = panel.get("close_px")
        try:
            if close_px is not None:
                self._click(ctx, (float(close_px[0]), float(close_px[1])),
                            "pet_panel_close",
                            CycleResult(cycle=self.cycles, outcome=Outcome.FAILED))
            else:
                client = getattr(ctx.session, "client", None)
                if client is not None:
                    client.send("key", code=int(panel.get("close_key", 53)))
        except Exception:  # noqa: BLE001 - a panel close must not crash a run
            pass
        self.sleep(0.5)

    @staticmethod
    def compare_pet_readings(previous: Mapping[str, Any] | None,
                             current: Mapping[str, Any] | None
                             ) -> dict[str, Any]:
        """Pure verdict: xp_to_next shrank or level grew => PROGRESS.

        This is the independent game-side proof, not a text scrape of a dialog that
        may already be gone.  Two identical readings => UNVERIFIED.  A level-up
        resets xp_to_next, so in that case the XP delta is unknown but the level is
        proof enough.
        """

        if current is None:
            return {"verdict": "UNREADABLE", "xp_delta": None, "level_delta": None}
        if previous is None:
            return {"verdict": "BASELINE", "xp_delta": None, "level_delta": None}
        prev_xp, cur_xp = previous.get("xp_to_next"), current.get("xp_to_next")
        prev_lv, cur_lv = previous.get("level"), current.get("level")
        level_delta = (int(cur_lv) - int(prev_lv)
                       if cur_lv is not None and prev_lv is not None else None)
        xp_delta = (int(prev_xp) - int(cur_xp)
                    if cur_xp is not None and prev_xp is not None else None)
        if level_delta is not None and level_delta > 0:
            return {"verdict": "PROGRESS", "xp_delta": None,
                    "level_delta": level_delta}
        if xp_delta is not None and xp_delta > 0:
            return {"verdict": "PROGRESS", "xp_delta": xp_delta,
                    "level_delta": level_delta}
        return {"verdict": "UNVERIFIED", "xp_delta": xp_delta,
                "level_delta": level_delta}

    def _engage(self, ctx: Any, current: CycleResult) -> bool:
        """Approach + guardian sweep (the guardian changes position every floor).

        Live 2026-11-07: a single body point works for a few floors and then stops
        (8 clicks at the same coordinate produced no battle).  So after the walkable
        approach click the runner sweeps ``[guardian_px] + guardian_candidates_px``
        one point at a time and stops the moment battle markers appear.  A sweep of
        N points gets 2 rounds (a single point gets 3), with a hard cap of 12
        guardian clicks — never an unbounded click loop.
        """

        approach = self.calibration.approach_px
        candidates: list[tuple[float, float]] = []
        if self.calibration.guardian_px is not None:
            candidates.append(self.calibration.guardian_px)
        candidates.extend(self.calibration.guardian_candidates_px)
        # Optional wider grid the operator may add: used only when no explicit
        # candidate list is present (the candidate list is the authoritative sweep).
        if len(candidates) <= 1:
            candidates.extend(self.calibration.guardian_sweep_px)
        sweep = []
        for point in candidates:
            if point not in sweep:
                sweep.append(point)
        if not sweep:  # pragma: no cover - loader refuses first
            current.outcome = Outcome.BLOCKED
            current.reason = "guardian sweep is not calibrated"
            return False
        rounds = 3 if len(sweep) == 1 else 2
        max_guardian_clicks = 12
        for round_no in range(rounds):
            if self._window_over() or self._stopped() or self._paused():
                current.outcome = Outcome.BLOCKED
                current.reason = "stop/pause requested during ENGAGE"
                return False
            if approach is not None:
                self._click(ctx, approach, "approach", current)
                self.sleep(self.calibration.approach_wait_s)
            for point in sweep:
                if current.engage_clicks >= max_guardian_clicks:
                    break
                self._click(ctx, point, "guardian", current)
                current.engage_clicks += 1
                self.sleep(self.calibration.guardian_gap_s)
                if self._at_battle(ctx):
                    current.reason = (f"battle started after {current.engage_clicks} "
                                      f"guardian click(s), round {round_no + 1}")
                    return True
            current.reason = (f"posture round {round_no + 1}/{rounds}: "
                              f"{current.engage_clicks} guardian clicks, no battle")
            if current.engage_clicks >= max_guardian_clicks:
                break
        current.outcome = Outcome.BLOCKED
        current.reason = (f"battle did not start after {rounds} sweep round(s) / "
                          f"{current.engage_clicks} guardian clicks (sweep of "
                          f"{len(sweep)} point(s), cap {max_guardian_clicks}); "
                          f"stopping instead of clicking blindly")
        return False

    def _default_battle_runner(self, ctx: Any, max_seconds: float) -> Mapping[str, Any]:
        # Lazy import: --dry-run must not need the vision stack, and this layer is
        # verified and must not be modified (task-6 constraint).
        from seerauto.skills.act import RunBattle

        result = RunBattle()(ctx, battles=1, max_seconds=max_seconds)
        after = dict(getattr(result, "after", None) or {})
        return {"won": result.status.value == "PASS",
                "status": result.status.value,
                "reason": result.reason,
                "xp": float(after.get("xp") or 0.0),
                "after": after}

    def _battle(self, ctx: Any, current: CycleResult) -> Mapping[str, Any]:
        runner = self._battle_runner or self._default_battle_runner
        return runner(ctx, self.calibration.battle_max_seconds)

    # -- one cycle --------------------------------------------------------- #
    def run_cycle(self, ctx: Any, cycle: int) -> CycleResult:
        started = self.clock()
        attempts = max(1, self.limits.max_reentries + 1)
        current = CycleResult(cycle=cycle, outcome=Outcome.FAILED)
        for attempt in range(attempts):
            if attempt:
                self.reentries += 1
                current = CycleResult(cycle=cycle, outcome=Outcome.FAILED,
                                      reentries=self.reentries)
            finished = self._attempt(ctx, current)
            if finished:
                current.elapsed_s = self.clock() - started
                return current
            # Defeat is the only retryable cycle ending: go back to the floor picker
            # (bounded by max_reentries), then give up.
            self.append_event("cycle.reentry", {
                "cycle": cycle, "attempt": attempt + 1,
                "reason": current.reason[:160]})
        current.elapsed_s = self.clock() - started
        return current

    def _attempt(self, ctx: Any, current: CycleResult) -> bool:
        """One DIALOG..NEXT pass.  Returns True when the cycle is finished."""

        screen = self._screen(ctx)
        if screen is None:
            # The painted floor is art lettering that OCR misreads (live: true 03
            # read as 08), so an unreadable floor is expected.  The calibrated
            # visual signature is the fallback proof that we are inside the tower;
            # without it, refusing is still the only safe answer.
            hit, evidence = self.signature_probe_settled(ctx)
            if not hit:
                current.outcome = Outcome.BLOCKED
                current.reason = ("unknown screen: no tower dialog markers, no "
                                  "readable painted floor, no battle markers and the "
                                  "tower signature did not match; not clicking "
                                  f"blindly. [{evidence}]")
                return True
            kind, floor = "inside_signature", None
            current.floor_source = "signature"
            current.floor_before = None
        else:
            kind, floor = screen
            if kind == "dialog":
                if not self._open_and_pick(ctx, current):
                    return True
            elif kind == "floor":
                current.floor_before = floor
                current.floor_source = "floor_ocr"
            else:  # already in a battle
                current.floor_before = self._read_floor(ctx)
                if current.floor_before is not None:
                    current.floor_source = "floor_ocr"

        if kind != "battle" and not self._at_battle(ctx):
            if not self._engage(ctx, current):
                return True

        battle = self._battle(ctx, current)
        current.battle = dict(battle)
        won = bool(battle.get("won"))
        if won:
            self.wins += 1

        self._settle_result(ctx, current)
        if current.xp_value is None and battle.get("xp"):
            current.xp_value = float(battle["xp"])
            current.xp_source = "battle_runner_after"
        for reward in (battle.get("after") or {}).get("rewards", []):
            if isinstance(reward, str) and reward not in current.dialogs:
                current.dialogs.append(reward)
        # 2026-11-07 live fix: the battle layer may already have clicked through the
        # settlement panels before RESULT runs, so the result ROI is empty by then.
        # The reward strings the battle layer captured carry the same verbatim
        # 「XIN精灵获得经验：N」 line - parse it instead of losing the proof.
        if current.xp_value is None and self.calibration.xp_regex is not None:
            for reward in (battle.get("after") or {}).get("rewards", []):
                if not isinstance(reward, str):
                    continue
                match = self.calibration.xp_regex.search(reward)
                if not match:
                    continue
                try:
                    current.xp_value = float(match.group("xp"))
                except (IndexError, ValueError):
                    continue
                current.xp_text = reward
                current.xp_source = "battle_rewards"
                break

        # 2026-11-07 live fix: a bare substring test for "pp" also matched the word
        # "approach" inside an engage reason, so a cycle that never even started a
        # battle was reported as pp_exhausted.  PP exhaustion is now only a blocker
        # when the battle did NOT succeed AND the calibrated marker was actually seen.
        if current.pp_marker and not won:
            current.outcome = Outcome.BLOCKED
            current.reason = "pp_exhausted: PP/HP markers say the pet cannot fight; " \
                             "not clicking blindly"
            return True

        current.floor_after = self._read_floor(ctx)
        if current.defeat_marker or not won:
            current.outcome = Outcome.FAILED
            detail = "defeat" if current.defeat_marker else "battle not won"
            current.reason = (f"{detail}: {str(battle.get('reason') or '')[:120]} "
                              f"(floor {current.floor_before} -> {current.floor_after})")
            return False  # retryable via re-entry

        # The painted floor is unreliable art lettering; the XP payout is the
        # primary completion evidence (2026-11-07 live: floor 1 = 1710 XP, floor 2 =
        # 3003 XP).  A readable floor is a secondary cross-check, not a requirement.
        xp_ok = current.xp_value is not None and float(current.xp_value) > 0
        advanced: bool | None = None
        if (current.floor_after is not None
                and current.floor_before is not None):
            advanced = current.floor_after > current.floor_before
        if xp_ok:
            current.outcome = Outcome.PROGRESS
            if advanced is False:
                current.reason = (f"xp {current.xp_value} proves the win; painted "
                                  f"floor reported {current.floor_before}->"
                                  f"{current.floor_after} (cross-check disagreed, "
                                  f"treated as OCR noise)")
            else:
                current.reason = (f"xp {current.xp_value} "
                                  f"(floor {current.floor_before} -> "
                                  f"{current.floor_after})")
            if current.floor_source != "signature":
                current.floor_source = ("floor_ocr" if advanced
                                        else "xp_text")
            if current.floor_source is None:
                current.floor_source = "xp_text"
            self.xp_total += float(current.xp_value)
            return True
        if advanced:
            current.outcome = Outcome.PROGRESS
            current.floor_source = current.floor_source or "floor_ocr"
            current.reason = (f"floor {current.floor_before} -> "
                              f"{current.floor_after}; no XP text captured")
            return True
        current.outcome = Outcome.UNVERIFIED_ACTIVITY
        current.reason = ("battle won but no XP text was captured and the painted "
                          f"floor did not advance ({current.floor_before} -> "
                          f"{current.floor_after}); not counting it as progress")
        return True

    # -- run --------------------------------------------------------------- #
    def append_event(self, kind: str, payload: Mapping[str, Any]) -> None:
        append_ledger(self.ledger_path, {"kind": kind, **dict(payload)})

    def run(self, ctx: Any, *, minutes: float = 0.0) -> dict[str, Any]:
        self.started_epoch = self.clock()
        # 2026-11-07 live: the first frames after a fresh capture session are dark
        # (measured mean_rgb (62,107,150) vs the tower's (159,181,170)); sampling
        # immediately made cycle 1 see an 'unknown screen'.  Let the stream settle.
        warmup_s = 3.0  # capture warm-up seconds (see comment above)
        if warmup_s > 0:
            self.sleep(warmup_s)
        size = getattr(ctx.session, "pixel_size", None)
        if size is not None:
            try:
                actual = (int(size[0]), int(size[1]))
            except (TypeError, IndexError, ValueError):
                actual = None
            if actual is not None and actual != tuple(self.calibration.capture_px):
                final = {"outcome": Outcome.BLOCKED.value,
                         "reason": (f"window geometry changed: live capture {actual} "
                                    f"vs calibrated {tuple(self.calibration.capture_px)}; "
                                    f"recalibrate before clicking"),
                         "cycles": 0, "wins": 0, "xp_total": 0.0,
                         "floor_first": None, "floor_last": None, "reentries": 0,
                         "failures": 0, "clicks": 0, "elapsed_s": 0.0,
                         "ledger": str(self.ledger_path) if self.ledger_path else None,
                         "cycles_detail": []}
                self.append_event("session.end", {k: v for k, v in final.items()
                                                  if k != "cycles_detail"})
                print(json.dumps(final, ensure_ascii=False,
                                 separators=(",", ":")), flush=True)
                return final
        ends = []
        if minutes:
            ends.append(self.started_epoch + max(0.0, minutes) * 60.0)
        if self.limits.deadline_epoch:
            ends.append(self.limits.deadline_epoch)
        self.window_end = min(ends) if ends else None
        self.append_event("session.start", {
            "preset": self.limits.preset or self.calibration.preset_default,
            "window_end": (datetime.fromtimestamp(self.window_end).astimezone()
                           .isoformat(timespec="seconds"))
            if self.window_end is not None else None,
            "calibration": {"path": str(self.calibration.path),
                            "capture_px": list(self.calibration.capture_px)}})

        outcome = Outcome.NO_WORK
        reason = "no cycle ran"
        stop_reason = "no_cycle"
        start_reading: dict[str, Any] | None = None
        if self.limits.auto_recover and self.calibration.pet_panel:
            start_reading = self._read_pet_panel(ctx)
            self.append_event("session.start_hp", {
                "hp": (start_reading or {}).get("hp"),
                "hp_max": (start_reading or {}).get("hp_max"),
                "hp_ratio": self.hp_ratio(start_reading)})
            ratio = self.hp_ratio(start_reading)
            if ratio is not None and ratio <= 0.10:
                state, why = self._recover(ctx, CycleResult(cycle=0,
                                                            outcome=Outcome.FAILED),
                                           "hp0_at_start", trigger="hp0_at_start",
                                           hp_reading=start_reading)
                if state == "paused":
                    outcome, reason, stop_reason = Outcome.PAUSED, why, "pause_flag"
                elif state != "ok":
                    outcome, reason = Outcome.BLOCKED, why
                    stop_reason = "recover_failed"
                else:
                    self._last_pet_reading = start_reading
        unverified = 0
        cycles: list[CycleResult] = []
        while True:
            pause_path = self._paused()
            if pause_path:
                outcome, reason = Outcome.PAUSED, f"paused by {pause_path}"
                stop_reason = "pause_flag"
                break
            if self._stopped():
                outcome, reason = Outcome.PAUSED, f"stop flag {self.limits.stop_file}"
                stop_reason = "stop_flag"
                break
            if self._window_over():
                outcome = Outcome.DEADLINE_REACHED
                reason = "window/deadline reached"
                stop_reason = "deadline"
                break
            if self.failures >= self.limits.max_failures:
                outcome = Outcome.FAILED
                reason = f"{self.failures} failure(s) >= {self.limits.max_failures}"
                stop_reason = "failure_limit"
                break
            if unverified >= self.limits.max_unverified:
                outcome = Outcome.UNVERIFIED_ACTIVITY
                reason = (f"{unverified} consecutive UNVERIFIED_ACTIVITY cycle(s) >= "
                          f"{self.limits.max_unverified}; stopping")
                stop_reason = "unverified_limit"
                break
            if self.limits.max_cycles and self.cycles >= self.limits.max_cycles:
                # Report what the last cycle actually proved; "we won at least one
                # battle" must not turn an UNVERIFIED final cycle into PROGRESS.
                last = cycles[-1].outcome if cycles else None
                if last is Outcome.PROGRESS:
                    outcome = Outcome.PROGRESS
                elif last is Outcome.UNVERIFIED_ACTIVITY:
                    outcome = Outcome.UNVERIFIED_ACTIVITY
                elif last is Outcome.NO_WORK:
                    outcome = Outcome.NO_WORK
                else:
                    outcome = Outcome.PROGRESS if self.wins else Outcome.NO_WORK
                reason = ("cycle budget reached"
                          + (f"; last cycle: {cycles[-1].reason[:160]}"
                             if cycles else ""))
                stop_reason = "cycle_budget"
                break

            self.cycles += 1
            current = self.run_cycle(ctx, self.cycles)
            current.elapsed_s = self.clock() - self.started_epoch if not current.elapsed_s \
                else current.elapsed_s
            cycles.append(current)
            if current.floor_before is not None and self.floor_first is None:
                self.floor_first = current.floor_before
            if current.floor_after is not None:
                self.floor_last = current.floor_after
            record = current.to_dict()
            record["kind"] = "cycle"
            record["xp_total"] = round(self.xp_total, 1)
            append_ledger(self.ledger_path, record)
            self._summary(current)

            # Independent evidence: every --verify-every cycles, read the pet panel.
            # RunBattle consumes the reward dialogs (rewards=[], xp=0.0), so this is
            # the only trustworthy "a floor really was cleared" signal.
            if (self.limits.verify_every and self.calibration.pet_panel
                    and self.cycles % self.limits.verify_every == 0):
                reading = self._read_pet_panel(ctx, current)
                verdict = self.compare_pet_readings(self._last_pet_reading, reading)
                check = {"kind": "pet_panel", "cycle": self.cycles, **verdict,
                         "reading": reading, "previous": self._last_pet_reading}
                self._pet_checks.append(check)
                append_ledger(self.ledger_path, check)
                if reading is not None:
                    self._last_pet_reading = reading
                if verdict["verdict"] == "PROGRESS":
                    current.outcome = Outcome.PROGRESS
                    current.xp_source = "pet_panel_delta"
                    current.xp_value = verdict.get("xp_delta")
                    if current.xp_value:
                        self.xp_total += float(current.xp_value)
                    self._stale_verifications = 0
                    unverified = 0
                    current.reason = (f"pet panel delta proves progress: "
                                      f"xp_delta={verdict.get('xp_delta')} "
                                      f"level_delta={verdict.get('level_delta')}")
                elif verdict["verdict"] == "UNVERIFIED":
                    self._stale_verifications += 1
                    if self._stale_verifications >= 2:
                        current.outcome = Outcome.UNVERIFIED_ACTIVITY
                        self._stale_verifications = 0
                        current.reason = ("pet panel read the same xp/level twice; "
                                          "no independent progress evidence")
                    else:
                        # first unchanged reading: not enough to call it unverified
                        pass

            if current.outcome is Outcome.BLOCKED:
                # A BLOCKED cycle that cannot find the tower signature is the
                # "sent back to the hall / lost inside" case: heal + travel + preset
                # 1 can self-heal it (live 2026-11-07, defeat sends you to the hall).
                if self.limits.auto_recover:
                    if self.recovers >= max(1, self.limits.max_recovers):
                        outcome = Outcome.BLOCKED
                        reason = (f"recover budget exhausted ({self.recovers}/"
                                  f"{self.limits.max_recovers}) on a lost/unknown "
                                  f"screen; needs a human")
                        stop_reason = "recover_failed"
                        break
                    inside, _sig = self.signature_probe(ctx)
                    if not inside:
                        state, why = self._recover(ctx, current,
                                                   "blocked_unknown_screen",
                                                   trigger="blocked")
                        if state == "ok":
                            unverified = 0
                            self._last_pet_reading = None
                            continue
                        if state == "paused":
                            outcome, reason = Outcome.PAUSED, why
                            stop_reason = "pause_flag"
                            break
                        outcome, reason = Outcome.BLOCKED, why
                        stop_reason = "recover_failed"
                        break
                outcome, reason = current.outcome, current.reason
                stop_reason = "blocked"
                break
            if current.outcome is Outcome.FAILED:
                self.failures += 1
                unverified = 0
                battle_reason = str((current.battle or {}).get("reason") or "").lower()
                no_monster = "no engageable monster" in battle_reason
                if no_monster and self.limits.auto_recover:
                    # HP=0 masks itself as "nothing to fight" (docs/state.md).  Re-read
                    # HP before acting: a high-HP hunt miss is not a heal situation.
                    hp_reading = self._read_pet_panel(ctx)
                    ratio = self.hp_ratio(hp_reading)
                    inside, _sig = self.signature_probe(ctx)
                    if ratio is not None and ratio > 0.10 and inside:
                        self.append_event("recover", {
                            "trigger": "no_engageable_monster",
                            "action": "posture_retry", "hp_ratio": ratio,
                            "reason": "HP is fine; another posture is cheaper than "
                                      "a heal round trip"})
                        unverified = 0
                        continue
                    if self.recovers >= max(1, self.limits.max_recovers):
                        outcome = Outcome.BLOCKED
                        reason = (f"recover budget exhausted ({self.recovers}/"
                                  f"{self.limits.max_recovers}); no engageable "
                                  f"monster and HP state needs a human")
                        stop_reason = "recover_failed"
                        break
                    state, why = self._recover(
                        ctx, current, "no_engageable_monster",
                        trigger="no_engageable_monster", hp_reading=hp_reading)
                    if state == "ok":
                        self.failures = max(0, self.failures - 1)
                        unverified = 0
                        self._last_pet_reading = hp_reading
                        continue
                    if state == "paused":
                        outcome, reason, stop_reason = Outcome.PAUSED, why, "pause_flag"
                        break
                    outcome, reason = Outcome.BLOCKED, why
                    stop_reason = "recover_failed"
                    break
                if current.defeat_marker and self.limits.auto_recover:
                    if self.recovers >= max(1, self.limits.max_recovers):
                        outcome = Outcome.BLOCKED
                        reason = (f"recover budget exhausted ({self.recovers}/"
                                  f"{self.limits.max_recovers}); a defeat cannot be "
                                  f"self-healed now")
                        stop_reason = "recover_failed"
                        break
                    state, why = self._recover(ctx, current, "defeat",
                                               trigger="defeat")
                    if state == "ok":
                        self.failures = max(0, self.failures - 1)
                        unverified = 0
                        self._last_pet_reading = None
                        continue
                    if state == "paused":
                        outcome, reason = Outcome.PAUSED, why
                        stop_reason = "pause_flag"
                        break
                    outcome, reason = Outcome.BLOCKED, why
                    stop_reason = "recover_failed"
                    break
                outcome, reason = Outcome.FAILED, current.reason
                if self.failures >= self.limits.max_failures:
                    stop_reason = "failure_limit"
                    break
                if self.limits.max_cycles and self.cycles >= self.limits.max_cycles:
                    stop_reason = "cycle_budget"
                    break
                continue
            if current.outcome is Outcome.PROGRESS:
                unverified = 0
                continue
            if current.outcome is Outcome.NO_WORK:
                unverified += 1
                if unverified >= self.limits.max_unverified:
                    outcome, reason = Outcome.NO_WORK, current.reason
                    stop_reason = "no_work_limit"
                    break
                continue
            if current.outcome is Outcome.UNVERIFIED_ACTIVITY:
                # Not terminal: count it, keep grinding, and only stop after the
                # calibrated streak limit (2026-11-07 field run: one unread XP frame
                # ended the whole night's grinder because UNVERIFIED was treated as
                # final).
                unverified += 1
                current.reason = current.reason
                if unverified >= self.limits.max_unverified:
                    outcome, reason = Outcome.UNVERIFIED_ACTIVITY, current.reason
                    stop_reason = "unverified_limit"
                    break
                continue
            # any other outcome: keep going (defensive)
            continue

        final = {
            "outcome": outcome.value,
            "reason": reason,
            "stop_reason": stop_reason,
            "unverified": unverified,
            "cycles": self.cycles,
            "wins": self.wins,
            "xp_total": round(self.xp_total, 1),
            "floor_first": self.floor_first,
            "floor_last": self.floor_last,
            "reentries": self.reentries,
            "failures": self.failures,
            "verify_every": self.limits.verify_every,
            "recovers": self.recovers,
            "pet_panel_checks": self._pet_checks,
            "clicks": self.clicks,
            "elapsed_s": round(self.clock() - self.started_epoch, 1),
            "ledger": str(self.ledger_path) if self.ledger_path else None,
            "cycles_detail": [c.to_dict() for c in cycles],
        }
        self.append_event("session.end", {k: v for k, v in final.items()
                                          if k != "cycles_detail"})
        print(json.dumps(final, ensure_ascii=False, separators=(",", ":")), flush=True)
        return final

    @staticmethod
    def _summary(current: CycleResult) -> None:
        print(f"cycle {current.cycle}: {current.outcome.value} "
              f"floor {current.floor_before}->{current.floor_after} "
              f"xp={current.xp_value} ({current.xp_text or 'no xp text'}) "
              f"{current.reason}"[:300], flush=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _default_capture(ctx: Any) -> tuple[int, int] | None:
    size = getattr(ctx.session, "pixel_size", None)
    try:
        return (int(size[0]), int(size[1])) if size else None
    except (TypeError, IndexError, ValueError):
        return None


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Unattended 试炼之塔 grinder (guardian-body click path)")
    ap.add_argument("--minutes", type=float, default=0.0,
                    help="window length; 0 = until --max-cycles/--deadline-at")
    ap.add_argument("--deadline-at", default=None,
                    help="absolute ISO-8601 deadline with offset")
    ap.add_argument("--max-cycles", type=int, default=0)
    ap.add_argument("--preset", type=int, default=None, choices=[1, 11, 21])
    ap.add_argument("--calibration", default=str(DEFAULT_CALIBRATION))
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    ap.add_argument("--recover-only", action="store_true",
                    help="run only the RECOVER chain (heal/travel/dialog/preset 1), "
                         "print {ok, steps}, exit; no cycles are run")
    ap.add_argument("--auto-recover", action="store_true",
                    help="self-heal after a defeat/lost state: heal in the hall -> "
                         "travel to the tutor room -> pick floor 1 (max 3/night)")
    ap.add_argument("--max-recovers", type=int, default=3)
    ap.add_argument("--verify-every", type=int, default=0,
                    help="read the pet panel every N cycles (0 = calibration value)")
    ap.add_argument("--pause-file", action="append", default=None)
    ap.add_argument("--stop-file", default=STOP_FILE)
    ap.add_argument("--allow-input", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sample-signature", action="store_true",
                    help="measure tower.inside.signature from a PNG and exit "
                         "(read-only; needs --image and --roi)")
    ap.add_argument("--image", default=None, help="PNG/JPEG for --sample-signature")
    ap.add_argument("--roi", default=None,
                    help="x0,y0,x1,y1 in game_px for --sample-signature")
    return ap


def dry_run_payload(args: argparse.Namespace) -> dict:
    info = calibration_problems(args.calibration)
    payload = {
        "dry_run": True,
        "starts_nothing": True,
        "calibration": info,
        "plan": PLAN,
        "preset": args.preset if args.preset is not None else (
            (info.get("presets") or [1])[0]),
        "max_cycles": args.max_cycles,
        "verify_every": args.verify_every or info.get("verify_every", 3),
        "guardian_sweep_points": info.get("guardian_sweep_points"),
        "engage_candidates": info.get("engage_candidates"),
        "window_minutes": args.minutes,
        "deadline_at": args.deadline_at,
        "ledger": args.ledger,
        "precondition": ("start at the tower entry dialog (选择楼层/继续挑战) or already "
                         "inside the tower; this runner does not navigate"),
        "auto_recover": ("off by default; with --auto-recover a defeat/lost state "
                         "heals in the hall (game.heal_pet), travels to tutor_room "
                         "(game.travel), then picks floor 1 via the calibrated "
                         "dialog; max --max-recovers (3) per run, every step bounded "
                         "and failures end BLOCKED"),
        "engage_posture": ("two-step (live-verified): approach_px -> wait "
                           "approach_wait_s -> guardian_px; the whole posture is "
                           "retried at most 3 times, then BLOCKED"),
        "floor_fallback": ("tower.inside.signature (roi_mean_color) is the INSIDE "
                           "fallback when the painted floor cannot be read twice "
                           "consistently; without it an unreadable floor is BLOCKED"),
        "safety": {
            "clicks": ["选择楼层", "calibrated floor preset", "guardian body",
                       "calibrated confirm words"],
            "bounded": "<=3 re-entries after a defeat, <=3 failures, cycle timeout "
                       f"{info.get('cycle_timeout_s', 180)}s, pause/stop at cycle "
                       "boundaries",
            "never": ["blind repeated clicking after 3 guardian clicks",
                      "clicking a heal or any uncalibrated coordinate"],
        },
    }
    if info.get("calibrated"):
        payload["command"] = (
            f"PYTHONPATH=.pydeps python3 tools/tower_grind.py --preset "
            f"{args.preset or 1} --minutes {args.minutes or 400} "
            f"--deadline-at <ISO-with-offset> --allow-input")
    else:
        payload["blocked_reason"] = ("calibration is not CALIBRATED; fill the "
                                     "required_fields and set status=CALIBRATED")
    return payload


def sample_signature(args: argparse.Namespace) -> int:
    """Print the mean colour of an ROI so the operator can fill expect_rgb."""

    if not args.image or not args.roi:
        print(json.dumps({"ok": False,
                          "error": "--sample-signature needs --image and --roi"}))
        return 2
    try:
        values = [float(v) for v in str(args.roi).replace(" ", "").split(",")]
        if len(values) != 4:
            raise ValueError
    except ValueError:
        print(json.dumps({"ok": False,
                          "error": "--roi must be x0,y0,x1,y1 in game_px"}))
        return 2
    try:
        from PIL import Image

        image = Image.open(args.image).convert("RGB")
        width, height = image.size
        x0, y0, x1, y1 = (int(round(v)) for v in values)
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            print(json.dumps({"ok": False, "error": f"ROI outside image {width}x{height}"}))
            return 2
        region = image.crop((x0, y0, x1, y1))
        pixels = list(region.getdata())
        count = len(pixels) or 1
        mean = [round(sum(pixel[i] for pixel in pixels) / count) for i in range(3)]
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps({"ok": True, "image": args.image, "image_size": [width, height],
                      "roi_px": [x0, y0, x1, y1], "mean_rgb": mean,
                      "mean_bgr": [mean[2], mean[1], mean[0]],
                      "json_snippet": {
                          "mode": "roi_mean_color", "roi_px": [x0, y0, x1, y1],
                          "expect_rgb": mean, "tolerance": 40,
                          "channel_order": "RGB"}}, ensure_ascii=False),
          flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.sample_signature:
        return sample_signature(args)
    if args.dry_run:
        print(json.dumps(dry_run_payload(args), ensure_ascii=False,
                         separators=(",", ":")), flush=True)
        return 0

    try:
        calibration = load_calibration(args.calibration, require_calibrated=True)
    except CalibrationError as exc:
        print(json.dumps({"ok": False, "error": "tower calibration unusable",
                          "problems": exc.problems,
                          "hint": "--dry-run lists the required fields"},
                         ensure_ascii=False), flush=True)
        return 3
    if not args.allow_input:
        print(json.dumps({"ok": False, "error": "refusing to click: pass --allow-input"},
                         ensure_ascii=False), flush=True)
        return 3

    deadline_epoch = None
    if args.deadline_at:
        try:
            deadline = datetime.fromisoformat(args.deadline_at)
        except ValueError as exc:
            print(json.dumps({"ok": False,
                              "error": f"--deadline-at is not ISO-8601: {exc}"}))
            return 2
        if deadline.tzinfo is None:
            print(json.dumps({"ok": False, "error": "--deadline-at needs an offset"}))
            return 2
        if deadline.timestamp() <= time.time():
            print(json.dumps({"ok": False, "error": "--deadline-at is in the past"}))
            return 2
        deadline_epoch = deadline.timestamp()

    try:
        from seerauto.live import LiveSession
        from seerauto.skills.base import Budget, SkillContext
        from seerauto.world.model import GameWorldState
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"live stack unavailable: {exc}"}))
        return 3

    session = None
    try:
        session = LiveSession(dry_run=False, enable_input=True).open()
        ctx = SkillContext(session=session, world=GameWorldState(), dry_run=False,
                           allow_input=True, budget=Budget(max_duration_s=900.0))
        capture = _default_capture(ctx)
        if capture is not None and capture != calibration.capture_px:
            print(json.dumps({"ok": False,
                              "error": f"window geometry changed: live capture "
                                       f"{capture} vs calibrated "
                                       f"{calibration.capture_px}; recalibrate"}))
            return 3
        limits = TowerLimits(
            max_cycles=args.max_cycles,
            max_reentries=calibration.max_reentries,
            max_failures=calibration.max_failures,
            max_unverified=calibration.max_unverified,
            verify_every=args.verify_every or calibration.verify_every,
            auto_recover=args.auto_recover,
            max_recovers=max(1, args.max_recovers),
            preset=args.preset,
            deadline_epoch=deadline_epoch,
            pause_files=tuple(args.pause_file) if args.pause_file else PAUSE_FILES,
            stop_file=args.stop_file,
            cycle_timeout_s=calibration.cycle_timeout_s)
        grinder = TowerGrind(calibration, limits=limits, ledger_path=args.ledger or None)
        if args.recover_only:
            hp_reading = grinder._read_pet_panel(ctx)
            report = grinder.recover_steps(ctx, trigger="manual",
                                           hp_reading=hp_reading)
            grinder.append_event("recover.only", {
                "ok": report["ok"], "steps": report["steps"]})
            payload = {"ok": report["ok"], "mode": "recover-only",
                       "steps": report["steps"],
                       "screen_text": report.get("screen_text", []),
                       "heal_ok": report.get("heal_ok"),
                       "heal_unavailable": report.get("heal_unavailable", False),
                       "floor_picked": report.get("floor_picked")}
            print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                  flush=True)
            return 0 if report["ok"] else 7
        final = grinder.run(ctx, minutes=args.minutes)
        return EXIT_CODES.get(final["outcome"], 1)
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "outcome": Outcome.INTERRUPTED.value,
                          "error": "KeyboardInterrupt"}), flush=True)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}),
              flush=True)
        return 1
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    raise SystemExit(main())
