"""CAP-CATCH: throw a capsule in battle, and prove what actually happened.

Why this file exists: `docs/PUNI_TASK_GRAPH.md` lists CAP-CATCH / CAP-FUSION /
CAP-TEAM as unimplemented, and every 谱尼 prerequisite that needs a captured pet
(波克尔/皮皮/吉尔/克里) is gated on it.  Tonight the only honest way to make progress
on "收集精灵" is to add this capability — carefully, because a bad loop here spends real
capsules and can click irreversible UI.

The spec is `docs/research/20261106_CATCH_SPEC.md`.  Its three hard rules are enforced
here in code, not in a playbook:

1. **No guessed coordinates.**  Every click target comes from
   ``seerauto/config/catch_calibration.json``.  The shipped file is ``UNCALIBRATED``;
   the skill refuses to run and names the missing field.  A calibration whose
   ``reference.capture_px`` no longer matches the live window also refuses — the window
   geometry changes every session (AGENTS.md §5.5), and a stale coordinate is how you
   throw a capsule at the wrong UI.
2. **One narrow click chain only**: 「道具」button → capsule slot → 「使用/投掷」button,
   plus dismiss-a-dialog confirmations.  A hard-coded allowlist of click *reasons* and
   a hard-coded denylist of words (放生/出售/交易/购买/还原/重组/融合/赋形/收藏/展示/
   放回仓库/放入背包) mean no code path here can ever buy, sell, release or rearrange
   a pet.  Capsule spending is bounded: ≤5 throws, ≥1 capsule kept in reserve, and 3
   failed attempts stop the session.
3. **PASS needs game evidence**, not "the click did not raise".  A capture is PASS only
   when the battle-result text says so on two consecutive samples; otherwise the outcome
   is ``UNVERIFIED_ACTIVITY`` — never PASS.  The inventory delta (capsules before/after)
   is recorded as corroboration when it is readable, and ``inventory_verified`` says
   whether it was.

State machine (one attempt)::

    PRECHECK -> ITEM_PANEL -> THROW -> VERDICT -> CLEANUP -> VERIFY

Every transition needs its two-consecutive-sample predicate; there is no fixed sleep as
a controller (AGENTS.md §5.1-3).  The session around it owns: pause/stop flags, the
absolute deadline, the throw budget, the failure budget and the ledger.

Nothing here imports the capture/vision stack directly, so it is unit-testable with a
fake session; the CLI (`tools/catch_pet.py`) is the only place a LiveSession is opened.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..dual.ledger import stamp
from ..dual.outcome import Outcome
from .base import (Skill, SkillContext, SkillKind, SkillResult, SkillSpec, Status)

__all__ = [
    "Calibration",
    "CalibrationError",
    "CatchLimits",
    "CatchAttempt",
    "CatchReport",
    "CatchSession",
    "CatchPet",
    "SafetyViolation",
    "load_calibration",
    "calibration_problems",
    "append_catch_ledger",
    "read_catch_ledger",
    "DEFAULT_CALIBRATION",
    "DEFAULT_LEDGER",
    "HARD_MAX_THROWS",
    "HARD_MAX_FAILURES",
    "FORBIDDEN_WORDS",
]

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CALIBRATION = ROOT / "seerauto" / "config" / "catch_calibration.json"
DEFAULT_LEDGER = ROOT / "data" / "overnight" / "catch_ledger.jsonl"

#: Safety ceilings that a calibration file may tighten but never loosen.
HARD_MAX_THROWS = 5
HARD_MAX_FAILURES = 3
HARD_RESERVE = 1
HARD_CONFIRM_WORDS = ("确认", "确定", "我知道了", "关闭", "取消")

#: Never clickable, never automated.  The list is deliberately broader than today's
#: needs: it is cheaper to over-block a word that never appears than to discover a
#: 放生 click in the ledger at 06:00 (SPEC §3 "⛔ 绝对不要自动化").
FORBIDDEN_WORDS = (
    "放生", "出售", "交易", "购买", "丢弃", "还原", "重组", "融合", "赋形",
    "收藏", "展示", "放回仓库", "放入背包",
)

#: The only click reasons this module will ever issue.  The safety check runs on the
#: reason string at the single click helper, so a future edit cannot click something
#: new without also editing this tuple.
ALLOWED_CLICK_REASONS = ("item_button", "capsule_slot", "use_button", "confirm")

class CalibrationError(ValueError):
    """The calibration file is missing, malformed, or not calibrated."""

    def __init__(self, problems: Sequence[str], *, path: Path | None = None) -> None:
        self.problems = [str(p) for p in problems]
        self.path = path
        where = f" ({path})" if path else ""
        super().__init__("catch calibration is unusable" + where + ": "
                         + "; ".join(self.problems))


class SafetyViolation(RuntimeError):
    """A click or text was requested outside the catch allowlist."""


# --------------------------------------------------------------------------- #
# calibration: schema, load, validate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Reference:
    capture_px: tuple[int, int]
    window_pt: tuple[float, float]
    scale: float


@dataclass(frozen=True)
class _Panel:
    markers: tuple[str, ...]
    capsule_slot_px: tuple[float, float] | None
    use_button_px: tuple[float, float] | None
    selection_markers: tuple[str, ...]
    timeout_s: float


@dataclass(frozen=True)
class _ResultRules:
    roi_px: tuple[float, float, float, float] | None
    success: tuple[str, ...]
    escaped: tuple[str, ...]
    fled: tuple[str, ...]
    defeated: tuple[str, ...]
    sample_gap_s: float
    verdict_timeout_s: float


@dataclass(frozen=True)
class _BattleRules:
    report_roi_px: tuple[float, float, float, float] | None
    opponent_regex: re.Pattern[str] | None
    item_button_px: tuple[float, float] | None
    panel: _Panel
    result: _ResultRules


@dataclass(frozen=True)
class _Target:
    key: str
    display_name: str
    scene: str
    level_max: int


@dataclass(frozen=True)
class Calibration:
    """A validated view of ``catch_calibration.json``."""

    path: Path
    status: str
    problems: tuple[str, ...]
    reference: _Reference
    battle: _BattleRules
    inventory_roi_px: tuple[float, float, float, float] | None
    max_throws: int
    reserve: int
    max_failures: int
    confirm_words: tuple[str, ...]
    extra_forbidden: tuple[str, ...]
    targets: Mapping[str, _Target]
    raw: Mapping[str, Any]

    @property
    def calibrated(self) -> bool:
        return self.status == "CALIBRATED" and not self.problems

    def target(self, key: str) -> _Target:
        try:
            return self.targets[key]
        except KeyError:
            raise CalibrationError(
                [f"unknown target {key!r}; calibrated targets: {sorted(self.targets)}"],
                path=self.path) from None

    def forbidden_words(self) -> tuple[str, ...]:
        return FORBIDDEN_WORDS + tuple(w for w in self.extra_forbidden
                                       if w not in FORBIDDEN_WORDS)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "status": self.status,
            "calibrated": self.calibrated,
            "problems": list(self.problems),
            "reference": {"capture_px": list(self.reference.capture_px),
                          "scale": self.reference.scale},
            "targets": sorted(self.targets),
            "budget": {"max_throws": self.max_throws, "reserve": self.reserve,
                       "max_failures": self.max_failures},
            "required_fields": REQUIRED_FIELDS,
        }


#: Named so `--dry-run` can tell the live agent exactly what is still uncalibrated.
REQUIRED_FIELDS = (
    "battle.item_button_px",
    "battle.item_panel.capsule_slot_px",
    "battle.item_panel.use_button_px",
    "battle.report_roi_px",
    "battle.result_roi_px",
    "battle.opponent_regex",
    "battle.result_keywords.success",
    "battle.result_keywords.escaped",
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _px(value: Any, where: str, problems: list[str], *,
        bounds: tuple[int, int] | None = None) -> tuple[float, float] | None:
    if value is None:
        problems.append(f"{where} is null (needs a game_px coordinate)")
        return None
    if (not isinstance(value, Sequence) or isinstance(value, (str, bytes))
            or len(value) != 2 or not all(_is_number(v) for v in value)):
        problems.append(f"{where} must be [x, y] numbers, got {value!r}")
        return None
    x, y = float(value[0]), float(value[1])
    if bounds is not None:
        w, h = bounds
        if not (0.0 <= x <= w and 0.0 <= y <= h):
            problems.append(f"{where} {value!r} is outside the calibrated capture "
                            f"{w}x{h}")
    return (x, y)


def _roi(value: Any, where: str, problems: list[str],
         bounds: tuple[int, int] | None = None
         ) -> tuple[float, float, float, float] | None:
    if value is None:
        problems.append(f"{where} is null (needs [x0, y0, x1, y1] game_px)")
        return None
    if (not isinstance(value, Sequence) or isinstance(value, (str, bytes))
            or len(value) != 4 or not all(_is_number(v) for v in value)):
        problems.append(f"{where} must be [x0, y0, x1, y1] numbers, got {value!r}")
        return None
    x0, y0, x1, y1 = (float(v) for v in value)
    if not (x1 > x0 and y1 > y0):
        problems.append(f"{where} is empty: {value!r}")
        return None
    if bounds is not None:
        w, h = bounds
        if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
            problems.append(f"{where} {value!r} is outside the calibrated capture "
                            f"{w}x{h}")
    return (x0, y0, x1, y1)


def _text_list(value: Any, where: str, problems: list[str], *,
               required: bool = True) -> tuple[str, ...]:
    if value is None:
        if required:
            problems.append(f"{where} is missing")
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        problems.append(f"{where} must be a list of strings, got {value!r}")
        return ()
    out = tuple(str(v).strip() for v in value if str(v).strip())
    if required and not out:
        problems.append(f"{where} is empty")
    for word in out:
        for bad in FORBIDDEN_WORDS:
            if bad in word:
                problems.append(f"{where} contains the forbidden word {bad!r}: {word!r}")
    return out


def _int_in(value: Any, where: str, problems: list[str], *, low: int, high: int
            ) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        problems.append(f"{where} must be an integer, got {value!r}")
        return None
    if not low <= value <= high:
        problems.append(f"{where} must be between {low} and {high}, got {value}")
        return None
    return value


def load_calibration(path: str | Path | None = None, *,
                     require_calibrated: bool = True) -> Calibration:
    """Load and validate the calibration file.

    Raises :class:`CalibrationError` when the file is missing/unreadable, when a
    required field is absent or mistyped, when a coordinate is outside the calibrated
    capture, or (with ``require_calibrated``) when ``status != "CALIBRATED"``.  That
    is the refusal path the spec asks for: the skill must never guess a coordinate.
    """

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

    reference_raw = raw.get("reference")
    reference = _Reference(capture_px=(2400, 1600), window_pt=(1200.0, 800.0),
                           scale=2.0)
    bounds: tuple[int, int] | None = None
    if not isinstance(reference_raw, Mapping):
        problems.append("reference is missing")
    else:
        capture = reference_raw.get("capture_px")
        if (not isinstance(capture, Sequence) or isinstance(capture, (str, bytes))
                or len(capture) != 2 or not all(isinstance(v, int) and v > 0
                                                for v in capture)):
            problems.append(f"reference.capture_px must be [w, h] positive ints, "
                            f"got {capture!r}")
        else:
            bounds = (int(capture[0]), int(capture[1]))
        window = reference_raw.get("window_pt")
        scale = reference_raw.get("scale")
        if not _is_number(scale) or float(scale) <= 0:
            problems.append(f"reference.scale must be a positive number, got {scale!r}")
        reference = _Reference(
            capture_px=(int(bounds[0]), int(bounds[1])) if bounds else (2400, 1600),
            window_pt=(float(window[0]), float(window[1]))
            if (isinstance(window, Sequence) and len(window) == 2
                and all(_is_number(v) for v in window)) else (1200.0, 800.0),
            scale=float(scale) if _is_number(scale) else 2.0)

    battle_raw = raw.get("battle")
    battle = _BattleRules(
        report_roi_px=None, opponent_regex=None, item_button_px=None,
        panel=_Panel(markers=(), capsule_slot_px=None, use_button_px=None,
                     selection_markers=(), timeout_s=5.0),
        result=_ResultRules(roi_px=None, success=(), escaped=(), fled=(),
                            defeated=(), sample_gap_s=0.6, verdict_timeout_s=8.0))
    if not isinstance(battle_raw, Mapping):
        problems.append("battle section is missing")
    else:
        panel_raw = battle_raw.get("item_panel")
        panel = battle.panel
        if not isinstance(panel_raw, Mapping):
            problems.append("battle.item_panel is missing")
        else:
            panel = _Panel(
                markers=_text_list(panel_raw.get("markers"),
                                   "battle.item_panel.markers", problems),
                capsule_slot_px=_px(panel_raw.get("capsule_slot_px"),
                                    "battle.item_panel.capsule_slot_px", problems,
                                    bounds=bounds),
                use_button_px=_px(panel_raw.get("use_button_px"),
                                  "battle.item_panel.use_button_px", problems,
                                  bounds=bounds),
                selection_markers=_text_list(panel_raw.get("selection_markers"),
                                             "battle.item_panel.selection_markers",
                                             problems),
                timeout_s=float(panel_raw.get("timeout_s", 5.0))
                if _is_number(panel_raw.get("timeout_s", 5.0)) else 5.0)
        result_raw = battle_raw.get("result_keywords")
        keywords = result_raw if isinstance(result_raw, Mapping) else {}
        result = _ResultRules(
            roi_px=_roi(battle_raw.get("result_roi_px"), "battle.result_roi_px",
                        problems, bounds=bounds),
            success=_text_list(keywords.get("success"),
                               "battle.result_keywords.success", problems),
            escaped=_text_list(keywords.get("escaped"),
                               "battle.result_keywords.escaped", problems),
            fled=_text_list(keywords.get("fled"), "battle.result_keywords.fled",
                            problems, required=False),
            defeated=_text_list(keywords.get("defeated"),
                                "battle.result_keywords.defeated", problems,
                                required=False),
            sample_gap_s=float(battle_raw.get("sample_gap_s", 0.6))
            if _is_number(battle_raw.get("sample_gap_s", 0.6)) else 0.6,
            verdict_timeout_s=float(battle_raw.get("verdict_timeout_s", 8.0))
            if _is_number(battle_raw.get("verdict_timeout_s", 8.0)) else 8.0)
        regex = battle_raw.get("opponent_regex")
        compiled = None
        if not isinstance(regex, str) or not regex.strip():
            problems.append("battle.opponent_regex is missing")
        else:
            try:
                compiled = re.compile(regex)
            except re.error as exc:
                problems.append(f"battle.opponent_regex does not compile: {exc}")
            else:
                if "name" not in compiled.groupindex:
                    problems.append("battle.opponent_regex needs a named group "
                                    "'name' for the opponent")
                if "level" not in compiled.groupindex:
                    problems.append("battle.opponent_regex needs a named group "
                                    "'level' (may be optional)")
        battle = _BattleRules(
            report_roi_px=_roi(battle_raw.get("report_roi_px"),
                               "battle.report_roi_px", problems, bounds=bounds),
            opponent_regex=compiled,
            item_button_px=_px(battle_raw.get("item_button_px"),
                               "battle.item_button_px", problems, bounds=bounds),
            panel=panel, result=result)

    inventory_raw = raw.get("inventory")
    inventory_roi = None
    if isinstance(inventory_raw, Mapping):
        value = inventory_raw.get("capsule_count_roi_px")
        if value is not None:
            inventory_roi = _roi(value, "inventory.capsule_count_roi_px", problems,
                                 bounds=bounds)

    budget_raw = raw.get("budget") if isinstance(raw.get("budget"), Mapping) else {}
    max_throws = _int_in(budget_raw.get("max_throws_per_session"),
                         "budget.max_throws_per_session", problems, low=1,
                         high=HARD_MAX_THROWS)
    reserve = _int_in(budget_raw.get("reserve_capsules"),
                      "budget.reserve_capsules", problems, low=HARD_RESERVE,
                      high=HARD_MAX_THROWS)
    max_failures = _int_in(budget_raw.get("max_failures"),
                           "budget.max_failures", problems, low=1,
                           high=HARD_MAX_FAILURES)

    safety_raw = raw.get("safety") if isinstance(raw.get("safety"), Mapping) else {}
    confirm = _text_list(safety_raw.get("confirm_words", list(HARD_CONFIRM_WORDS[:4])),
                         "safety.confirm_words", problems, required=False)
    bad_confirm = [w for w in confirm if w not in HARD_CONFIRM_WORDS]
    if bad_confirm:
        problems.append(f"safety.confirm_words may only use {list(HARD_CONFIRM_WORDS)}; "
                        f"got {bad_confirm}")
    extra_forbidden = _text_list(safety_raw.get("extra_forbidden_words", []),
                                 "safety.extra_forbidden_words", problems,
                                 required=False)

    targets: dict[str, _Target] = {}
    targets_raw = raw.get("targets")
    if not isinstance(targets_raw, Mapping) or not targets_raw:
        problems.append("targets is missing or empty")
    else:
        for key, tgt in targets_raw.items():
            if not isinstance(tgt, Mapping):
                problems.append(f"targets.{key} must be an object")
                continue
            name = str(tgt.get("display_name") or "").strip()
            if not name:
                problems.append(f"targets.{key}.display_name is missing")
            level_max = _int_in(tgt.get("level_max", 3), f"targets.{key}.level_max",
                                problems, low=1, high=100)
            targeting = _Target(key=str(key), display_name=name,
                                scene=str(tgt.get("scene") or ""),
                                level_max=level_max if level_max is not None else 3)
            if targeting.display_name:
                targets[str(key)] = targeting

    calibration = Calibration(
        path=target,
        status=status if status in ("CALIBRATED", "UNCALIBRATED") else "UNCALIBRATED",
        problems=tuple(problems),
        reference=reference, battle=battle, inventory_roi_px=inventory_roi,
        max_throws=max_throws or HARD_MAX_THROWS,
        reserve=reserve if reserve is not None else HARD_RESERVE,
        max_failures=max_failures or HARD_MAX_FAILURES,
        confirm_words=confirm, extra_forbidden=extra_forbidden,
        targets=targets, raw=dict(raw))

    if require_calibrated and not calibration.calibrated:
        raise CalibrationError(
            (["status is not CALIBRATED (live calibration has not been filled in)"]
             if calibration.status != "CALIBRATED" else []) + list(problems),
            path=target)
    return calibration


def calibration_problems(path: str | Path | None = None) -> dict[str, Any]:
    """Non-raising view for `--dry-run`: what is missing, without importing a client."""

    target = Path(path) if path else DEFAULT_CALIBRATION
    try:
        calibration = load_calibration(target, require_calibrated=False)
    except CalibrationError as exc:
        return {"path": str(target), "usable": False, "problems": exc.problems,
                "calibrated": False, "required_fields": list(REQUIRED_FIELDS)}
    return {
        "path": str(target),
        "usable": not calibration.problems,
        "problems": list(calibration.problems),
        "calibrated": calibration.calibrated,
        "required_fields": list(REQUIRED_FIELDS),
        "targets": sorted(calibration.targets),
        "budget": {"max_throws": calibration.max_throws,
                   "reserve": calibration.reserve,
                   "max_failures": calibration.max_failures},
    }


# --------------------------------------------------------------------------- #
# result model
# --------------------------------------------------------------------------- #
@dataclass
class CatchAttempt:
    """One click-through: PRECHECK → THROW → VERDICT.  Evidence or it did not happen."""

    attempt: int
    outcome: Outcome
    reason: str = ""
    throws_used: int = 0
    target_name: str | None = None
    target_level: int | None = None
    clicks: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    capsules_before: int | None = None
    capsules_after: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "throws_used": self.throws_used,
            "target_name": self.target_name,
            "target_level": self.target_level,
            "clicks": list(self.clicks),
            "evidence": list(self.evidence),
            "capsules_before": self.capsules_before,
            "capsules_after": self.capsules_after,
        }


@dataclass
class CatchReport:
    """The session's auditable result — sparse when evidence was unreadable."""

    outcome: Outcome
    target: str
    reason: str = ""
    target_name: str | None = None
    target_level: int | None = None
    captures: int = 0
    throws_used: int = 0
    failures: int = 0
    capsules_before: int | None = None
    capsules_after: int | None = None
    inventory_verified: bool = False
    inventory_mismatch: bool = False
    attempts: list[CatchAttempt] = field(default_factory=list)
    elapsed_s: float = 0.0
    proof: str = ""
    ledger: str | None = None

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PROGRESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "target": self.target,
            "reason": self.reason,
            "target_name": self.target_name,
            "target_level": self.target_level,
            "captures": self.captures,
            "throws_used": self.throws_used,
            "failures": self.failures,
            "capsules_before": self.capsules_before,
            "capsules_after": self.capsules_after,
            "inventory_verified": self.inventory_verified,
            "inventory_mismatch": self.inventory_mismatch,
            "elapsed_s": round(self.elapsed_s, 2),
            "proof": self.proof,
            "ledger": self.ledger,
            "attempts": [a.to_dict() for a in self.attempts],
        }


@dataclass(frozen=True)
class CatchLimits:
    """Session bounds.  A calibration/CLI value can tighten these, never loosen."""

    max_throws: int = HARD_MAX_THROWS
    reserve: int = HARD_RESERVE
    max_failures: int = HARD_MAX_FAILURES
    deadline_epoch: float | None = None
    pause_files: tuple[str, ...] = ("/tmp/seerauto.pause", "/tmp/seerauto.master.pause")
    stop_file: str = "/tmp/seerauto.stop"

    def validated(self) -> "CatchLimits":
        return CatchLimits(
            max_throws=max(1, min(int(self.max_throws), HARD_MAX_THROWS)),
            reserve=max(HARD_RESERVE, int(self.reserve)),
            max_failures=max(1, min(int(self.max_failures), HARD_MAX_FAILURES)),
            deadline_epoch=self.deadline_epoch,
            pause_files=self.pause_files, stop_file=self.stop_file)


# --------------------------------------------------------------------------- #
# ledger (append-only JSONL; a half-written last line is tolerated on read)
# --------------------------------------------------------------------------- #
def append_catch_ledger(path: str | Path | None, record: Mapping[str, Any]) -> dict | None:
    """Append one record to ``catch_ledger.jsonl`` with a single fsynced write."""

    if path is None:
        return None
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    iso, epoch = stamp()
    payload = dict(record)
    payload.setdefault("ts", iso)
    payload.setdefault("ts_epoch", epoch)
    line = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    fd = os.open(str(target), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        data = line.encode("utf-8")
        # If the previous append was interrupted mid-line, start on a fresh line: a
        # new record concatenated onto a truncated one would be unparsable (and the
        # new record silently lost).  This is the write-side half of the
        # truncated-tail tolerance on read.
        if os.fstat(fd).st_size and os.pread(fd, 1, os.fstat(fd).st_size - 1) != b"\n":
            data = b"\n" + data
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:  # pragma: no cover
                raise OSError(f"short write to {target}")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return payload


def read_catch_ledger(path: str | Path | None) -> list[dict[str, Any]]:
    """Read the ledger, skipping a crash-truncated tail line (and any malformed line)."""

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
# the session state machine
# --------------------------------------------------------------------------- #
def _iso(epoch: float) -> str:
    return stamp(epoch)[0]


class CatchSession:
    """Runs at most one catch session against one already-in-battle target.

    The caller provides the live `ctx`; navigation/hunt are deliberately *not* part of
    this class (the existing battle skills own them, and they are already verified —
    SPEC §2 Phase 1 reuses `run_battle`'s hunt/engage segment).
    """

    def __init__(self, calibration: Calibration, *,
                 limits: CatchLimits | None = None,
                 ledger_path: str | Path | None = DEFAULT_LEDGER,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 on_event: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.calibration = calibration
        self.limits = (limits or CatchLimits(
            max_throws=calibration.max_throws,
            reserve=calibration.reserve,
            max_failures=calibration.max_failures)).validated()
        self.ledger_path = ledger_path
        self.clock = clock
        self.sleep = sleep
        self.on_event = on_event
        self.attempts: list[CatchAttempt] = []
        self.clicks = 0

    # -- tiny plumbing ----------------------------------------------------- #
    def _ocr(self, ctx: SkillContext, *, min_conf: float = 0.3) -> list[dict[str, Any]]:
        return list(ctx.session.ocr(min_conf=min_conf))

    @staticmethod
    def _text(items: Iterable[Mapping[str, Any]]) -> list[str]:
        return [str(item.get("text") or "").strip() for item in items
                if str(item.get("text") or "").strip()]

    def _in_roi(self, items: Iterable[Mapping[str, Any]],
                roi: tuple[float, float, float, float] | None) -> list[dict[str, Any]]:
        if roi is None:
            return list(items)
        x0, y0, x1, y1 = roi
        out = []
        for item in items:
            try:
                x, y = item.get("px") or (None, None)
            except (TypeError, ValueError):
                continue
            if x is not None and y is not None and x0 <= float(x) <= x1 and y0 <= float(y) <= y1:
                out.append(dict(item))
        return out

    def _sample(self, ctx: SkillContext, predicate: Callable[[list[dict]], Any],
                *, timeout_s: float, gap_s: float,
                label: str) -> tuple[Any, list[dict], list[str]]:
        """Sample until two consecutive identical non-None answers (SPEC §7 rule)."""

        max_samples = max(3, int(max(timeout_s, 0.0) / max(gap_s, 0.05)) + 2)
        deadline = self.clock() + max(timeout_s, 0.0)
        previous: Any = None
        last_items: list[dict] = []
        last_texts: list[str] = []
        for _ in range(max_samples):
            items = self._ocr(ctx)
            last_items = items
            last_texts = self._text(items)
            try:
                value = predicate(items)
            except Exception:  # noqa: BLE001 - a bad predicate is no evidence
                value = None
            if value is not None and value == previous:
                return value, last_items, last_texts
            previous = value
            if self.clock() >= deadline:
                break
            self.sleep(gap_s)
        return None, last_items, last_texts

    def _click(self, ctx: SkillContext, px: tuple[float, float], reason: str,
               attempt: CatchAttempt) -> None:
        if not any(reason.split(":")[0] == allowed
                   for allowed in ALLOWED_CLICK_REASONS):
            raise SafetyViolation(f"click reason {reason!r} is not in the allowlist "
                                  f"{ALLOWED_CLICK_REASONS}")
        if safe_text := reason.split(":", 1)[-1]:
            self._assert_safe_text(safe_text)
        ctx.session.click_px(px[0], px[1], reason)
        self.clicks += 1
        attempt.clicks.append({"reason": reason, "px": [px[0], px[1]]})

    def _assert_safe_text(self, text: str) -> None:
        for word in self.calibration.forbidden_words():
            if word in str(text):
                raise SafetyViolation(f"refusing to touch {text!r}: contains {word!r}")

    def _write(self, kind: str, payload: Mapping[str, Any]) -> None:
        record = {"kind": kind, **dict(payload)}
        append_catch_ledger(self.ledger_path, record)
        if self.on_event is not None:
            try:
                self.on_event(kind, record)
            except Exception:  # noqa: BLE001 - observers must not break a catch
                pass

    def _blocked_reason(self) -> str | None:
        for path in self.limits.pause_files:
            if os.path.exists(path):
                return f"paused by {path}"
        if os.path.exists(self.limits.stop_file):
            return f"stop flag {self.limits.stop_file}"
        return None

    def _deadline_hit(self) -> bool:
        return (self.limits.deadline_epoch is not None
                and self.clock() >= self.limits.deadline_epoch)

    # -- calibration ground truths ----------------------------------------- #
    def _check_geometry(self, ctx: SkillContext) -> str | None:
        size = getattr(ctx.session, "pixel_size", None)
        if size is None:
            return None
        try:
            actual = (int(size[0]), int(size[1]))
        except (TypeError, IndexError, ValueError):
            return f"cannot read the live capture size {size!r}"
        expected = tuple(self.calibration.reference.capture_px)
        if actual != expected:
            return (f"window geometry changed: live capture {actual[0]}x{actual[1]} vs "
                    f"calibrated {expected[0]}x{expected[1]}; recalibrate before "
                    f"clicking (AGENTS.md §5.5)")
        return None

    def _read_capsules(self, ctx: SkillContext) -> tuple[int | None, str]:
        roi = self.calibration.inventory_roi_px
        if roi is None:
            return None, "inventory ROI not calibrated (SPEC §5 B1)"
        items = self._in_roi(self._ocr(ctx), roi)
        for text in reversed(self._text(items)):
            match = re.search(r"(\d+)", text.replace("×", "x"))
            if match:
                return int(match.group(1)), f"inventory text {text!r}"
        return None, "no capsule count in the calibrated inventory ROI"

    # -- attempt phases ---------------------------------------------------- #
    def _precheck(self, ctx: SkillContext, target: _Target,
                  attempt: CatchAttempt) -> CatchAttempt | None:
        regex = self.calibration.battle.opponent_regex
        roi = self.calibration.battle.report_roi_px

        def parse(items: list[dict]) -> tuple[str, int | None] | None:
            scope = self._in_roi(items, roi)
            for item in scope:
                text = str(item.get("text") or "")
                match = regex.search(text) if regex is not None else None
                if match is None:
                    continue
                name = match.groupdict().get("name")
                level = match.groupdict().get("level")
                if name:
                    return (name.strip(), int(level) if level and level.isdigit() else None)
            return None

        value, _items, texts = self._sample(
            ctx, parse, timeout_s=self.calibration.battle.result.verdict_timeout_s,
            gap_s=self.calibration.battle.result.sample_gap_s, label="opponent")
        attempt.evidence.extend(texts[:6])
        if value is None:
            attempt.outcome = Outcome.UNVERIFIED_ACTIVITY
            attempt.reason = ("cannot confirm the opponent from the battle report "
                              "(two consecutive readings); refusing to throw")
            return attempt
        name, level = value
        attempt.target_name, attempt.target_level = name, level
        if name != target.display_name:
            attempt.outcome = Outcome.BLOCKED
            attempt.reason = (f"wrong target: battle report says {name!r}, expected "
                              f"{target.display_name!r}; not throwing")
            return attempt
        if level is not None and level > target.level_max:
            attempt.outcome = Outcome.BLOCKED
            attempt.reason = (f"target level {level} > allowed {target.level_max}; "
                              f"not throwing")
            return attempt
        return None

    def _open_item_panel(self, ctx: SkillContext, attempt: CatchAttempt) -> CatchAttempt | None:
        px = self.calibration.battle.item_button_px
        if px is None:  # pragma: no cover - load_calibration already refuses
            attempt.outcome = Outcome.BLOCKED
            attempt.reason = "battle.item_button_px is not calibrated"
            return attempt
        self._click(ctx, px, "item_button", attempt)
        markers = self.calibration.battle.panel.markers

        def panel_visible(items: list[dict]) -> tuple[str, ...] | None:
            texts = " ".join(self._text(items))
            hits = tuple(sorted(m for m in markers if m in texts))
            return hits or None

        hits, _items, texts = self._sample(
            ctx, panel_visible,
            timeout_s=self.calibration.battle.panel.timeout_s,
            gap_s=self.calibration.battle.result.sample_gap_s, label="item_panel")
        attempt.evidence.extend(texts[:8])
        if hits is None:
            # SPEC §3: no repeat clicks when a panel does not open; and since we do
            # not know why, this needs a human (BLOCKED), not a blind retry.
            attempt.outcome = Outcome.BLOCKED
            attempt.reason = ("item panel did not appear after one click on the "
                              "calibrated 道具 button (not clicking again; needs a "
                              "human look)")
            return attempt
        return None

    def _select_capsule(self, ctx: SkillContext, attempt: CatchAttempt) -> CatchAttempt | None:
        px = self.calibration.battle.panel.capsule_slot_px
        if px is None:  # pragma: no cover
            attempt.outcome = Outcome.BLOCKED
            attempt.reason = "battle.item_panel.capsule_slot_px is not calibrated"
            return attempt
        self._click(ctx, px, "capsule_slot", attempt)
        markers = self.calibration.battle.panel.selection_markers

        def selected(items: list[dict]) -> tuple[str, ...] | None:
            texts = " ".join(self._text(items))
            hits = tuple(sorted(m for m in markers if m in texts))
            return hits or None

        if not markers:  # pragma: no cover - load_calibration already refuses
            attempt.outcome = Outcome.BLOCKED
            attempt.reason = "capsule selection markers are not calibrated"
            return attempt
        hits, _items, texts = self._sample(
            ctx, selected, timeout_s=self.calibration.battle.panel.timeout_s,
            gap_s=self.calibration.battle.result.sample_gap_s, label="capsule_selected")
        attempt.evidence.extend(texts[:8])
        if hits is None:
            attempt.outcome = Outcome.BLOCKED
            attempt.reason = ("capsule selection was not confirmed by the panel text; "
                              "not clicking use/throw (needs a human look)")
            return attempt
        return None

    def _classify_result(self, items: list[dict]) -> tuple[str, str] | None:
        rules = self.calibration.battle.result
        scope = " ".join(self._text(self._in_roi(items, rules.roi_px)))
        for name, words in (("success", rules.success), ("escaped", rules.escaped),
                            ("fled", rules.fled), ("defeated", rules.defeated)):
            for word in words:
                if word in scope:
                    return name, word
        return None

    def _throw_and_verdict(self, ctx: SkillContext, attempt: CatchAttempt) -> CatchAttempt:
        px = self.calibration.battle.panel.use_button_px
        if px is None:  # pragma: no cover
            attempt.outcome = Outcome.BLOCKED
            attempt.reason = "battle.item_panel.use_button_px is not calibrated"
            return attempt
        self._click(ctx, px, "use_button", attempt)
        attempt.throws_used = 1  # the click happened; accounting is the ledger's job
        rules = self.calibration.battle.result

        def verdict(items: list[dict]) -> tuple[str, str] | None:
            return self._classify_result(items)

        value, _items, texts = self._sample(
            ctx, verdict, timeout_s=rules.verdict_timeout_s, gap_s=rules.sample_gap_s,
            label="verdict")
        attempt.evidence.extend(texts[:10])
        if value is None:
            # SPEC §2 Phase 3 safety valve: no evidence -> do NOT throw again blindly.
            attempt.outcome = Outcome.UNVERIFIED_ACTIVITY
            attempt.reason = ("no capture-result evidence within the timeout; the "
                              "capsule may already be consumed, so no second throw")
            return attempt
        kind, word = value
        attempt.evidence.append(f"result:{kind}:{word}")
        if kind == "success":
            attempt.outcome = Outcome.PROGRESS
            attempt.reason = f"capture confirmed by result text {word!r}"
            self._cleanup(ctx, attempt)
            return attempt
        if kind == "escaped":
            attempt.outcome = Outcome.FAILED
            attempt.reason = f"escaped ({word!r}); capsule spent"
            return attempt
        if kind == "fled":
            attempt.outcome = Outcome.NO_WORK
            attempt.reason = f"opponent fled ({word!r}); nothing to catch here"
            return attempt
        attempt.outcome = Outcome.BLOCKED
        attempt.reason = f"battle ended badly ({word!r}); stopping"
        return attempt

    def _cleanup(self, ctx: SkillContext, attempt: CatchAttempt) -> int:
        """Dismiss result dialogs by clicking *known* confirm words only."""

        clicked = 0
        for _ in range(2):
            items = self._ocr(ctx)
            hit = None
            for word in self.calibration.confirm_words:
                for item in items:
                    text = str(item.get("text") or "").strip()
                    if text == word or (word in text and len(text) <= len(word) + 6):
                        hit = (word, item)
                        break
                if hit:
                    break
            if hit is None:
                break
            word, item = hit
            px = item.get("px") or item.get("norm")
            if not px or len(px) < 2:
                break
            try:
                self._assert_safe_text(word)
                self._click(ctx, (float(px[0]), float(px[1])),
                            f"confirm:{word}", attempt)
                clicked += 1
            except SafetyViolation:
                break
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                break
            self.sleep(0.8)
        return clicked

    def _attempt(self, ctx: SkillContext, target: _Target,
                 number: int) -> CatchAttempt:
        attempt = CatchAttempt(attempt=number, outcome=Outcome.FAILED)
        try:
            result = self._precheck(ctx, target, attempt)
            if result is not None:
                return result
            for step in (self._open_item_panel, self._select_capsule):
                result = step(ctx, attempt)
                if result is not None:
                    return result
            return self._throw_and_verdict(ctx, attempt)
        except SafetyViolation as exc:
            attempt.outcome = Outcome.BLOCKED
            attempt.reason = f"safety refusal: {exc}"
            return attempt
        except Exception as exc:  # noqa: BLE001 - no input path may crash the session
            attempt.outcome = Outcome.BLOCKED
            attempt.reason = f"input/observation failed: {type(exc).__name__}: {exc}"
            return attempt

    # -- the session ------------------------------------------------------- #
    def run(self, ctx: SkillContext, target_key: str = "pippi") -> CatchReport:
        t0 = self.clock()
        report = CatchReport(outcome=Outcome.FAILED, target=target_key,
                             ledger=str(self.ledger_path) if self.ledger_path else None)
        try:
            target = self.calibration.target(target_key)
        except CalibrationError as exc:
            report.outcome = Outcome.BLOCKED
            report.reason = exc.problems[0]
            return report
        if not self.calibration.calibrated:
            report.outcome = Outcome.BLOCKED
            report.reason = ("catch calibration is not CALIBRATED; refusing to throw "
                             "(run tools/catch_pet.py --dry-run for the missing fields)")
            return report
        geometry = self._check_geometry(ctx)
        if geometry:
            report.outcome = Outcome.BLOCKED
            report.reason = geometry
            return report

        limit_reason = self._blocked_reason()
        if limit_reason:
            report.outcome = Outcome.PAUSED
            report.reason = limit_reason
            self._finish(report, t0, ctx)
            return report
        if self._deadline_hit():
            report.outcome = Outcome.DEADLINE_REACHED
            report.reason = "session deadline reached before the first throw"
            self._finish(report, t0, ctx)
            return report

        report.capsules_before, capsule_evidence = self._read_capsules(ctx)
        budget = min(self.limits.max_throws, self.calibration.max_throws,
                     HARD_MAX_THROWS)
        if report.capsules_before is not None:
            budget = min(budget,
                         max(0, report.capsules_before - max(self.limits.reserve,
                                                             HARD_RESERVE)))
        self._write("session.start", {
            "target": target_key, "display_name": target.display_name,
            "capsules_before": report.capsules_before,
            "capsule_evidence": capsule_evidence,
            "budget": budget, "limits": self.limits.__dict__,
            "calibration": {"path": str(self.calibration.path),
                            "capture_px": list(self.calibration.reference.capture_px)},
        })
        if budget <= 0:
            report.outcome = Outcome.BLOCKED
            report.reason = (f"no capsule budget: capsules_before={report.capsules_before}, "
                             f"reserve={max(self.limits.reserve, HARD_RESERVE)}; "
                             f"refusing to throw (restock is a human decision)")
            self._finish(report, t0, ctx)
            return report

        while True:
            limit_reason = self._blocked_reason()
            if limit_reason:
                report.outcome = Outcome.PAUSED
                report.reason = limit_reason
                break
            if self._deadline_hit():
                report.outcome = Outcome.DEADLINE_REACHED
                report.reason = "session deadline reached"
                break
            if report.throws_used >= budget:
                report.outcome = (Outcome.PROGRESS if report.captures else Outcome.FAILED)
                report.reason = (report.reason or
                                 f"throw budget exhausted ({report.throws_used} throw(s), "
                                 f"no capture)")
                break
            if report.failures >= self.limits.max_failures:
                report.outcome = Outcome.FAILED
                report.reason = (f"{report.failures} failed attempt(s) >= "
                                 f"{self.limits.max_failures}; stopping")
                break

            attempt = self._attempt(ctx, target, len(self.attempts) + 1)
            self.attempts.append(attempt)
            report.attempts.append(attempt)
            report.throws_used += attempt.throws_used
            report.target_name = report.target_name or attempt.target_name
            report.target_level = report.target_level or attempt.target_level
            report.outcome = attempt.outcome
            self._write("attempt", attempt.to_dict())
            if attempt.outcome is Outcome.PROGRESS:
                report.captures += 1
                report.proof = "; ".join(attempt.evidence[-2:])[:200]
                report.reason = attempt.reason
                break
            if attempt.outcome in (Outcome.BLOCKED, Outcome.UNVERIFIED_ACTIVITY,
                                   Outcome.PAUSED, Outcome.INTERRUPTED,
                                   Outcome.DEADLINE_REACHED, Outcome.NO_WORK):
                report.reason = attempt.reason
                break
            report.failures += 1
            self.sleep(1.0)

        self._finish(report, t0, ctx)
        return report

    def _finish(self, report: CatchReport, t0: float, ctx: SkillContext) -> None:
        try:
            report.capsules_after, _after_evidence = self._read_capsules(ctx)
        except Exception:  # noqa: BLE001 - a read failure must not fabricate a number
            report.capsules_after = None
        report.inventory_verified = (report.capsules_before is not None
                                     and report.capsules_after is not None)
        if report.inventory_verified and report.throws_used:
            accounted = report.capsules_before - report.capsules_after
            report.inventory_mismatch = accounted != report.throws_used
        report.elapsed_s = self.clock() - t0
        if report.outcome is Outcome.PROGRESS and not report.proof:
            report.proof = "capture confirmed by result text"
        if report.outcome is Outcome.PROGRESS and report.inventory_mismatch:
            report.reason = (report.reason + " [WARNING: capsule delta "
                             f"{report.capsules_before}->{report.capsules_after} does "
                             f"not match {report.throws_used} throw(s)]")
        self._write("session.end", report.to_dict())


# --------------------------------------------------------------------------- #
# the skill
# --------------------------------------------------------------------------- #
class CatchPet(Skill):
    """Throw capsules at one already-in-battle target, with proof or UNVERIFIED.

    Contract notes for the planner (SPEC §3/§7):

    * never throws at an unconfirmed or wrong target;
    * never throws more than the budget (≤5, ≥1 capsule kept in reserve);
    * stops after 3 failed attempts;
    * PASS only when the result screen says so twice; otherwise the returned
      ``data["outcome"]`` is ``UNVERIFIED_ACTIVITY`` and the status is not PASS;
    * every throw/verdict is written to ``data/overnight/catch_ledger.jsonl``.
    """

    spec = SkillSpec(
        id="game.catch_pet",
        kind=SkillKind.ACT,
        summary="战斗内投胶囊抓宠：道具→胶囊→投掷，两次采样判决，证据不足返回 UNVERIFIED",
        inputs={"target": {"type": "str", "required": True},
                "max_throws": {"type": "int", "required": False}},
        outputs={"outcome": "str", "throws_used": "int", "captures": "int",
                 "capsules_before": "int|null", "capsules_after": "int|null"},
        preconditions=("in_battle",),
        postconditions=(),
        max_duration_s=180.0,
        max_actions=14,
        requires_input=True,
        # A capsule is a purchased consumable → the planner must treat this as spend.
        requires_approval=True,
        # Never executed against the live client yet.  Do not claim otherwise.
        verified="unverified",
        evidence=("docs/research/20261106_CATCH_SPEC.md",),
    )

    def __init__(self, calibration: Calibration | str | Path | None = None, *,
                 limits: CatchLimits | None = None,
                 ledger_path: str | Path | None = DEFAULT_LEDGER,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__()
        self._calibration_arg = calibration
        self._limits = limits
        self._ledger_path = ledger_path
        self._clock = clock
        self._sleep = sleep

    def guard(self, ctx: SkillContext) -> SkillResult | None:
        """Same contract as `act._Base.guard`, without importing the vision stack."""

        if ctx.session is None:
            return SkillResult.blocked(self.id, "no live session (offline or dry run)",
                                       escalation="pause")
        return None

    def _calibration(self) -> Calibration:
        if isinstance(self._calibration_arg, Calibration):
            return self._calibration_arg
        return load_calibration(self._calibration_arg, require_calibrated=True)

    def run(self, ctx: SkillContext, target: str = "pippi",
            max_throws: int | None = None, **_: Any) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        if ctx.dry_run or not ctx.allow_input:
            return SkillResult(
                skill=self.id, status=Status.SKIPPED,
                reason="dry run / input not allowed: plan only, no click sent",
                data={"outcome": Outcome.NO_WORK.value, "dry_run": True})
        try:
            calibration = self._calibration()
        except CalibrationError as exc:
            return SkillResult.blocked(
                self.id, "catch calibration unusable: " + "; ".join(exc.problems),
                escalation="pause",
                data={"outcome": Outcome.BLOCKED.value,
                      "calibration_problems": exc.problems})
        limits = self._limits
        if limits is None:
            limits = CatchLimits(max_throws=calibration.max_throws,
                                 reserve=calibration.reserve,
                                 max_failures=calibration.max_failures)
        if max_throws is not None:
            limits = CatchLimits(max_throws=min(int(max_throws), HARD_MAX_THROWS),
                                 reserve=limits.reserve,
                                 max_failures=limits.max_failures,
                                 deadline_epoch=limits.deadline_epoch,
                                 pause_files=limits.pause_files,
                                 stop_file=limits.stop_file)
        session = CatchSession(calibration, limits=limits,
                               ledger_path=self._ledger_path,
                               clock=self._clock, sleep=self._sleep)
        before = None
        try:
            before = session._read_capsules(ctx)[0]
        except Exception:  # noqa: BLE001 - a read failure must not prevent the run
            before = None
        try:
            report = session.run(ctx, target)
        except CalibrationError as exc:
            return SkillResult.blocked(
                self.id, "; ".join(exc.problems), escalation="pause",
                data={"outcome": Outcome.BLOCKED.value})
        data = report.to_dict()
        data["calibration"] = {"path": str(calibration.path),
                               "capture_px": list(calibration.reference.capture_px)}
        escalation = None
        if report.outcome in (Outcome.PAUSED, Outcome.DEADLINE_REACHED):
            escalation = "pause"
        elif report.outcome in (Outcome.BLOCKED, Outcome.INTERRUPTED):
            escalation = "pause"
        elif report.outcome is Outcome.UNVERIFIED_ACTIVITY:
            escalation = "replan"
        return SkillResult(
            skill=self.id,
            status=self._status_for(report.outcome),
            reason=report.reason,
            before={"capsules": before if before is not None
                    else report.capsules_before},
            after={"capsules": report.capsules_after},
            changed=report.captures > 0,
            proof=report.proof,
            actions=session.clicks,
            data=data,
            escalation=escalation,
        )

    @staticmethod
    def _status_for(outcome: Outcome) -> Status:
        if outcome is Outcome.PROGRESS:
            return Status.PASS
        if outcome is Outcome.BLOCKED:
            return Status.BLOCKED
        if outcome in (Outcome.NO_WORK, Outcome.PAUSED, Outcome.DEADLINE_REACHED):
            return Status.SKIPPED
        # FAILED / INTERRUPTED / UNVERIFIED_ACTIVITY are never PASS.
        return Status.FAIL
