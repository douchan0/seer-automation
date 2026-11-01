"""Config loading/validation.

Config is data, not code: coordinates, ROIs, thresholds, state rules,
transitions, budgets and task parameters all live in
``seerauto/config/default.json`` (JSON; YAML is accepted when PyYAML happens
to be installed, but is never required).  The loader validates eagerly so a
typo fails at startup instead of half-way through a battle.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import geometry as G
from .errors import ConfigError

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config" / "default.json"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be an object, got {type(value).__name__}")
    return value


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a number, got {value!r}")
    return float(value)


def _int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} must be an integer, got {value!r}")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where} must be a boolean, got {value!r}")
    return value


def _rect_px(value: Any, where: str) -> G.Rect:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        raise ConfigError(f"{where} must be [x, y, w, h]")
    return G.Rect(*(float(v) for v in value))


def _norm_rect(value: Any, where: str) -> tuple[float, float, float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        raise ConfigError(f"{where} must be [x0, y0, x1, y1]")
    out = tuple(float(v) for v in value)
    if not all(0.0 <= v <= 1.0 for v in out):
        raise ConfigError(f"{where} values must be inside 0..1, got {value!r}")
    x0, y0, x1, y1 = out
    if x1 <= x0 or y1 <= y0:
        raise ConfigError(f"{where} is empty: {value!r}")
    return out  # type: ignore[return-value]


def _read_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise ConfigError(
                f"{path} is YAML but PyYAML is not installed; use JSON instead "
                "(python3 -m pip install --target .pydeps pyyaml)"
            ) from exc
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    return dict(_require_mapping(data, str(path)))


# --------------------------------------------------------------------------- #
# typed sections
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WindowConfig:
    owner_aliases: tuple[str, ...]
    scale: float
    expected_origin_pt: tuple[float, float] | None
    expected_size_pt: tuple[float, float]
    tolerance_pt: float
    finder_command: tuple[str, ...] | None
    pin_on_start: bool


@dataclass(frozen=True)
class CaptureConfig:
    backend: str  # auto | daemon | screencapture | none
    daemon_binary: str | None
    daemon_args: tuple[str, ...]
    screencapture_path: str
    tick_ms: float
    frame_age_warn_ms: float
    frame_age_max_ms: float
    change_threshold: float
    refresh_ms: float
    unknown_ocr_after_ms: float


@dataclass(frozen=True)
class Budgets:
    max_iterations: int
    max_duration_s: float
    max_consecutive_errors: int
    max_state_timeouts: int
    action_confirm_timeout_s: float
    action_retries: int


@dataclass(frozen=True)
class TemplateFile:
    name: str
    file: str


@dataclass(frozen=True)
class FingerprintSpec:
    name: str
    roi: tuple[float, float, float, float]
    grid: tuple[int, int]
    cells: tuple[float, ...]
    mean: tuple[float, float, float]
    std: float
    max_distance: float
    min_distance: float


@dataclass(frozen=True)
class VisionConfig:
    template_dir: Path
    templates: dict[str, TemplateFile]
    rois: dict[str, tuple[float, float, float, float]]
    fingerprints: dict[str, FingerprintSpec]
    ocr_min_confidence: float


@dataclass(frozen=True)
class StateRuleConfig:
    rule_id: str
    state: str
    priority: int
    detail: str
    conditions: tuple[Mapping[str, Any], ...]
    enabled: bool


@dataclass(frozen=True)
class StateDefinition:
    state: str
    actions: tuple[str, ...]
    timeout_s: float | None
    on_timeout: str
    expects_motion: bool
    description: str


@dataclass(frozen=True)
class StateMachineConfig:
    initial: str
    rules: tuple[StateRuleConfig, ...]
    definitions: dict[str, StateDefinition]
    transitions: dict[str, tuple[str, ...]]
    blocked_switch_after: int


@dataclass(frozen=True)
class BattleTaskConfig:
    enabled: bool
    max_iterations: int
    max_enemies: int
    coordinates: dict[str, tuple[float, float]]
    skill_sequence: tuple[str, ...]
    thresholds: dict[str, float]
    dialog_dismiss_limit: int
    max_turns_per_battle: int
    max_battles: int
    on_loss: str


@dataclass(frozen=True)
class AppConfig:
    path: Path
    raw: Mapping[str, Any]
    reference: G.ReferenceSpace
    window: WindowConfig
    capture: CaptureConfig
    budgets: Budgets
    vision: VisionConfig
    states: StateMachineConfig
    battle: BattleTaskConfig
    notes: tuple[str, ...] = field(default_factory=tuple)

    def roi(self, name: str) -> tuple[float, float, float, float]:
        try:
            return self.vision.rois[name]
        except KeyError as exc:
            raise ConfigError(f"unknown ROI {name!r}; known: {sorted(self.vision.rois)}") from exc

    def game_px(self, name: str) -> G.Rect:
        """Task coordinate -> game_px rect (click targets are points)."""
        try:
            x, y = self.battle.coordinates[name]
        except KeyError as exc:
            raise ConfigError(
                f"unknown coordinate {name!r}; known: {sorted(self.battle.coordinates)}"
            ) from exc
        return G.Rect(float(x), float(y), 1.0, 1.0)

    def to_global_pt(self, name: str, geometry: G.WindowGeometry) -> G.Point:
        rect = self.game_px(name)
        return geometry.global_pt_from_game_px(rect.x, rect.y)


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _build_reference(raw: Mapping[str, Any]) -> G.ReferenceSpace:
    section = raw.get("reference") or {}
    capture = section.get("capture_px", list(G.CAPTURE_PX))
    stage = section.get("stage_px", list(G.STAGE_PX))
    window = section.get("window_pt", list(G.WINDOW_PT))
    scale = _number(section.get("scale", G.RETINA_SCALE), "reference.scale")
    if len(capture) != 2 or len(window) != 2 or len(stage) != 4:
        raise ConfigError("reference section must provide capture_px/window_pt/stage_px")
    return G.ReferenceSpace(
        capture_px=G.Size(float(capture[0]), float(capture[1])),
        stage_px=G.Rect(*(float(v) for v in stage)),
        window_pt=G.Size(float(window[0]), float(window[1])),
        scale=scale,
    )


def _build_window(raw: Mapping[str, Any]) -> WindowConfig:
    section = _require_mapping(raw.get("window", {}), "window")
    aliases = section.get("owner_aliases", ["SeerGame", "xin计划"])
    if not isinstance(aliases, Sequence) or isinstance(aliases, (str, bytes)):
        raise ConfigError("window.owner_aliases must be a list of strings")
    origin = section.get("expected_origin_pt")
    size = section.get("expected_size_pt", list(G.WINDOW_PT))
    command = section.get("finder_command")
    if command is not None:
        if not isinstance(command, Sequence) or isinstance(command, (str, bytes)):
            raise ConfigError("window.finder_command must be a list of strings")
        command = tuple(str(c) for c in command)
    return WindowConfig(
        owner_aliases=tuple(str(a) for a in aliases),
        scale=_number(section.get("scale", G.RETINA_SCALE), "window.scale"),
        expected_origin_pt=(float(origin[0]), float(origin[1])) if origin else None,
        expected_size_pt=(float(size[0]), float(size[1])),
        tolerance_pt=_number(section.get("tolerance_pt", 2.0), "window.tolerance_pt"),
        finder_command=command,  # type: ignore[arg-type]
        pin_on_start=_bool(section.get("pin_on_start", False), "window.pin_on_start"),
    )


def _build_capture(raw: Mapping[str, Any]) -> CaptureConfig:
    section = _require_mapping(raw.get("capture", {}), "capture")
    binary = section.get("daemon_binary")
    args = section.get("daemon_args", [])
    if not isinstance(args, Sequence) or isinstance(args, (str, bytes)):
        raise ConfigError("capture.daemon_args must be a list")
    return CaptureConfig(
        backend=str(section.get("backend", "auto")),
        daemon_binary=str(binary) if binary else None,
        daemon_args=tuple(str(a) for a in args),
        screencapture_path=str(section.get("screencapture_path", "/usr/sbin/screencapture")),
        tick_ms=_number(section.get("tick_ms", 10.0), "capture.tick_ms"),
        frame_age_warn_ms=_number(section.get("frame_age_warn_ms", 1000.0), "capture.frame_age_warn_ms"),
        frame_age_max_ms=_number(section.get("frame_age_max_ms", 8000.0), "capture.frame_age_max_ms"),
        change_threshold=_number(section.get("change_threshold", 0.002), "capture.change_threshold"),
        refresh_ms=_number(section.get("refresh_ms", 500.0), "capture.refresh_ms"),
        unknown_ocr_after_ms=_number(
            section.get("unknown_ocr_after_ms", 2000.0), "capture.unknown_ocr_after_ms"
        ),
    )


def _build_budgets(raw: Mapping[str, Any]) -> Budgets:
    section = _require_mapping(raw.get("budgets", {}), "budgets")
    return Budgets(
        max_iterations=_int(section.get("max_iterations", 500), "budgets.max_iterations"),
        max_duration_s=_number(section.get("max_duration_s", 600.0), "budgets.max_duration_s"),
        max_consecutive_errors=_int(
            section.get("max_consecutive_errors", 5), "budgets.max_consecutive_errors"
        ),
        max_state_timeouts=_int(section.get("max_state_timeouts", 3), "budgets.max_state_timeouts"),
        action_confirm_timeout_s=_number(
            section.get("action_confirm_timeout_s", 1.5), "budgets.action_confirm_timeout_s"
        ),
        action_retries=_int(section.get("action_retries", 2), "budgets.action_retries"),
    )


def _build_vision(raw: Mapping[str, Any]) -> VisionConfig:
    section = _require_mapping(raw.get("vision", {}), "vision")
    template_dir = Path(section.get("template_dir") or TEMPLATE_DIR)
    if not template_dir.is_absolute():
        template_dir = (DEFAULT_CONFIG_PATH.parent.parent / template_dir).resolve()
    templates_raw = _require_mapping(section.get("templates", {}), "vision.templates")
    templates = {
        str(name): TemplateFile(name=str(name), file=str(_require_mapping(spec, f"vision.templates.{name}").get("file", f"{name}.png")))
        for name, spec in templates_raw.items()
    }
    rois_raw = _require_mapping(section.get("rois", {}), "vision.rois")
    rois = {str(name): _norm_rect(value, f"vision.rois.{name}") for name, value in rois_raw.items()}
    fps_raw = _require_mapping(section.get("fingerprints", {}), "vision.fingerprints")
    fingerprints: dict[str, FingerprintSpec] = {}
    for name, spec in fps_raw.items():
        spec = _require_mapping(spec, f"vision.fingerprints.{name}")
        grid = spec.get("grid", [2, 2])
        if not isinstance(grid, Sequence) or len(grid) != 2:
            raise ConfigError(f"vision.fingerprints.{name}.grid must be [cols, rows]")
        roi_value = spec.get("roi", rois.get(name))
        if roi_value is None:
            raise ConfigError(f"vision.fingerprints.{name} needs an roi")
        cells = spec.get("cells")
        if not isinstance(cells, Sequence) or len(cells) != int(grid[0]) * int(grid[1]) * 3:
            raise ConfigError(
                f"vision.fingerprints.{name}.cells must hold grid_w*grid_h*3 numbers"
            )
        mean = spec.get("mean", [0.0, 0.0, 0.0])
        fingerprints[str(name)] = FingerprintSpec(
            name=str(name),
            roi=_norm_rect(roi_value, f"vision.fingerprints.{name}.roi"),
            grid=(int(grid[0]), int(grid[1])),
            cells=tuple(float(v) for v in cells),
            mean=tuple(float(v) for v in mean),  # type: ignore[arg-type]
            std=_number(spec.get("std", 0.0), f"vision.fingerprints.{name}.std"),
            max_distance=_number(
                spec.get("max_distance", 25.0), f"vision.fingerprints.{name}.max_distance"
            ),
            min_distance=_number(
                spec.get("min_distance", 25.0), f"vision.fingerprints.{name}.min_distance"
            ),
        )
    return VisionConfig(
        template_dir=template_dir,
        templates=templates,
        rois=rois,
        fingerprints=fingerprints,
        ocr_min_confidence=_number(
            section.get("ocr_min_confidence", 0.35), "vision.ocr_min_confidence"
        ),
    )


def _build_states(raw: Mapping[str, Any]) -> StateMachineConfig:
    section = _require_mapping(raw.get("states", {}), "states")
    rules_raw = section.get("rules", [])
    if not isinstance(rules_raw, Sequence) or isinstance(rules_raw, (str, bytes)):
        raise ConfigError("states.rules must be a list")
    rules: list[StateRuleConfig] = []
    seen: set[str] = set()
    for item in rules_raw:
        item = _require_mapping(item, "states.rules[]")
        rule_id = str(item.get("id") or item.get("state"))
        if rule_id in seen:
            raise ConfigError(f"duplicate state rule id {rule_id!r}")
        seen.add(rule_id)
        conditions = item.get("all", [])
        if not isinstance(conditions, Sequence) or isinstance(conditions, (str, bytes)):
            raise ConfigError(f"states.rules[{rule_id}].all must be a list")
        if not conditions:
            raise ConfigError(f"states.rules[{rule_id}] has no conditions")
        rules.append(
            StateRuleConfig(
                rule_id=rule_id,
                state=str(item["state"]),
                priority=_int(item.get("priority", 50), f"states.rules[{rule_id}].priority"),
                detail=str(item.get("detail", rule_id)),
                conditions=tuple(_require_mapping(c, "condition") for c in conditions),
                enabled=_bool(item.get("enabled", True), f"states.rules[{rule_id}].enabled"),
            )
        )
    definitions_raw = _require_mapping(section.get("definitions", {}), "states.definitions")
    definitions: dict[str, StateDefinition] = {}
    for state, spec in definitions_raw.items():
        spec = _require_mapping(spec, f"states.definitions.{state}")
        timeout = spec.get("timeout_s")
        definitions[str(state)] = StateDefinition(
            state=str(state),
            actions=tuple(str(a) for a in spec.get("actions", [])),
            timeout_s=None if timeout is None else _number(timeout, f"states.definitions.{state}.timeout_s"),
            on_timeout=str(spec.get("on_timeout", "ERROR")),
            expects_motion=_bool(spec.get("expects_motion", False), f"states.definitions.{state}.expects_motion"),
            description=str(spec.get("description", "")),
        )
    transitions_raw = _require_mapping(section.get("transitions", {}), "states.transitions")
    transitions = {
        str(src): tuple(str(dst) for dst in targets)
        for src, targets in transitions_raw.items()
        if isinstance(targets, Sequence)
    }
    return StateMachineConfig(
        initial=str(section.get("initial", "UNKNOWN")),
        rules=tuple(rules),
        definitions=definitions,
        transitions=transitions,
        blocked_switch_after=_int(section.get("blocked_switch_after", 3), "states.blocked_switch_after"),
    )


def _build_battle(raw: Mapping[str, Any]) -> BattleTaskConfig:
    section = _require_mapping(raw.get("tasks", {}).get("battle", {}), "tasks.battle")
    coords_raw = _require_mapping(section.get("coordinates", {}), "tasks.battle.coordinates")
    coordinates: dict[str, tuple[float, float]] = {}
    for name, value in coords_raw.items():
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
            raise ConfigError(f"tasks.battle.coordinates.{name} must be [x, y] in game_px")
        coordinates[str(name)] = (float(value[0]), float(value[1]))
    thresholds_raw = _require_mapping(section.get("thresholds", {}), "tasks.battle.thresholds")
    thresholds = {str(k): float(v) for k, v in thresholds_raw.items()}
    return BattleTaskConfig(
        enabled=_bool(section.get("enabled", True), "tasks.battle.enabled"),
        max_iterations=_int(section.get("max_iterations", 400), "tasks.battle.max_iterations"),
        max_enemies=_int(section.get("max_enemies", 20), "tasks.battle.max_enemies"),
        coordinates=coordinates,
        skill_sequence=tuple(str(s) for s in section.get("skill_sequence", ["fight", "skill"])),
        thresholds=thresholds,
        dialog_dismiss_limit=_int(
            section.get("dialog_dismiss_limit", 5), "tasks.battle.dialog_dismiss_limit"
        ),
        max_turns_per_battle=_int(
            section.get("max_turns_per_battle", 400), "tasks.battle.max_turns_per_battle"
        ),
        max_battles=_int(section.get("max_battles", 20), "tasks.battle.max_battles"),
        on_loss=str(section.get("on_loss", "stop")),
    )


def load_config(path: str | Path | None = None) -> AppConfig:
    """Load and validate configuration (defaults to the packaged file)."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw = _read_raw(config_path)
    config = AppConfig(
        path=config_path,
        raw=copy.deepcopy(raw),
        reference=_build_reference(raw),
        window=_build_window(raw),
        capture=_build_capture(raw),
        budgets=_build_budgets(raw),
        vision=_build_vision(raw),
        states=_build_states(raw),
        battle=_build_battle(raw),
        notes=tuple(str(n) for n in raw.get("notes", [])),
    )
    _validate_cross_references(config)
    return config


def _validate_cross_references(config: AppConfig) -> None:
    """Catch dangling names early - a rule referencing a missing ROI is a bug."""
    known_states = set(config.states.definitions)
    for rule in config.states.rules:
        if not rule.enabled:
            # Disabled rules are documentation of states we cannot detect yet
            # (e.g. victory banner: no fixture exists). They are not compiled.
            continue
        if rule.state not in known_states:
            raise ConfigError(f"rule {rule.rule_id!r} targets unknown state {rule.state!r}")
        if rule.state not in config.states.transitions:
            raise ConfigError(f"rule {rule.rule_id!r} targets state {rule.state!r} with no transitions entry")
        for condition in rule.conditions:
            kind = condition.get("kind")
            name = condition.get("name") or condition.get("fingerprint") or condition.get("template")
            if kind in ("template_present", "template_absent", "template_seen_within"):
                if name not in config.vision.templates:
                    raise ConfigError(f"rule {rule.rule_id!r} references unknown template {name!r}")
                roi = condition.get("roi")
                if roi is not None and roi not in config.vision.rois:
                    raise ConfigError(f"rule {rule.rule_id!r} references unknown ROI {roi!r}")
                if kind == "template_seen_within" and "ttl_s" not in condition:
                    raise ConfigError(f"rule {rule.rule_id!r} template_seen_within needs ttl_s")
            elif kind in ("fingerprint_near", "fingerprint_far"):
                if name not in config.vision.fingerprints:
                    raise ConfigError(f"rule {rule.rule_id!r} references unknown fingerprint {name!r}")
            elif kind in ("text_contains", "text_absent", "text_seen_within"):
                if not condition.get("needles"):
                    raise ConfigError(f"rule {rule.rule_id!r} text condition needs needles")
                if kind == "text_seen_within" and "ttl_s" not in condition:
                    raise ConfigError(f"rule {rule.rule_id!r} text_seen_within needs ttl_s")
            elif kind is None:
                raise ConfigError(f"rule {rule.rule_id!r} has a condition without 'kind'")
            else:
                raise ConfigError(f"rule {rule.rule_id!r} uses unknown condition kind {kind!r}")
    for src, targets in config.states.transitions.items():
        if src not in known_states:
            raise ConfigError(f"transition source {src!r} is not a defined state")
        for dst in targets:
            if dst not in known_states:
                raise ConfigError(f"transition target {dst!r} is not a defined state")
    for name in config.battle.skill_sequence:
        if name not in config.battle.coordinates:
            raise ConfigError(
                f"tasks.battle.skill_sequence references unknown coordinate {name!r}"
            )
