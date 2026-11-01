"""Config loading/validation tests."""

from __future__ import annotations

import copy
import json

import pytest

from seerauto import geometry as G
from seerauto.config import DEFAULT_CONFIG_PATH, load_config
from seerauto.errors import ConfigError


def test_default_config_loads_and_is_consistent():
    config = load_config()
    assert config.path == DEFAULT_CONFIG_PATH
    # reference layout measured from artifacts/game.png
    assert config.reference.capture_px == G.Size(2298, 1428)
    assert config.reference.stage_px.as_tuple() == (102.0, 128.0, 2092.0, 1220.0)
    assert config.reference.window_pt == G.Size(1149, 714)
    assert config.reference.scale == 2.0
    # only the four replay-verified templates are declared
    for name in ("battle_action_grid", "battle_turn_badge", "idle_action_bar", "map_panel_corner"):
        assert name in config.vision.templates
        assert (config.vision.template_dir / config.vision.templates[name].file).exists()
    # the battle_progress fingerprint is observed only (change-driven cadence)
    assert "battle_progress" in config.vision.fingerprints
    # The victory rule is now ENABLED: a real victory frame was captured from the
    # live client on 2026-11-02 and the template separates cleanly (real victory
    # frame 0.99 vs real battle frame 0.334 vs world frame 0.588, threshold 0.85).
    # See docs/PROGRESS.md Round 4 and seerauto/templates/battle_victory_banner.png.
    enabled = {r.rule_id for r in config.states.rules if r.enabled}
    assert "battle_result_victory" in enabled
    assert "battle_victory_banner" in config.vision.templates
    # Defeat still has NO fixture anywhere: not in artifacts/, not in the 40-minute
    # video (the author never loses), and not on the live client (8 wins, 0 losses).
    # reward_panel and error_text likewise remain unverified.
    disabled = {r.rule_id for r in config.states.rules if not r.enabled}
    assert {"battle_result_defeat", "battle_result_text", "reward_panel", "error_text"} <= disabled
    # battle_result_text must stay disabled: the string 失败 is permanently on
    # screen in the seal areas as the static note 战斗失败后可继续进行挑战！, so a
    # bare 失败 needle would latch BATTLE_RESULT forever (found by the video sweep).
    assert "battle_result_text" in disabled
    # nine states, config-driven durations
    assert len(config.states.definitions) == 9
    assert config.budgets.max_iterations > 0
    assert config.battle.max_turns_per_battle >= 200  # 真身 measured 165 turns


def test_task_coordinates_are_game_px():
    config = load_config()
    # Coordinates were recalibrated for the live 1200x800 window on 2026-11-02.
    # Assert the point is inside the window and matches the documented calibration,
    # so a future recalibration updates one place (config) plus this expectation.
    skill = config.game_px("skill")
    assert skill.as_tuple()[:2] == (1216.0, 1195.0)
    assert 0 <= skill.x <= config.reference.capture_px.w
    assert 0 <= skill.y <= config.reference.capture_px.h
    fight = config.game_px("fight")
    assert fight.as_tuple()[:2] == (1870.0, 1198.0)
    assert config.game_px("confirm").as_tuple() == (1191.0, 997.0, 1.0, 1.0)
    assert config.game_px("enemy").as_tuple() == (1178.0, 730.0, 1.0, 1.0)
    geo = G.WindowGeometry(origin_pt=G.Point(225, 53))
    point = config.to_global_pt("skill", geo)
    sx, sy = skill.x, skill.y
    assert point.as_tuple() == pytest.approx((225 + sx / 2, 53 + sy / 2))
    with pytest.raises(ConfigError):
        config.game_px("does_not_exist")


def test_vision_roi_lookup():
    config = load_config()
    roi = config.roi("battle_action_grid")
    assert len(roi) == 4
    assert 0.0 <= roi[0] < roi[2] <= 1.0
    with pytest.raises(ConfigError):
        config.roi("missing_roi")


def test_missing_config_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/seerauto-config.json")


def test_malformed_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(ConfigError, match="not valid JSON"):
        load_config(path)


def _mutated_config(**patch) -> dict:
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    for dotted, value in patch.items():
        node = raw
        keys = dotted.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = value
    return raw


def test_unknown_template_in_enabled_rule_is_rejected(tmp_path):
    raw = _mutated_config()
    # make the first enabled battle rule point at a template that does not exist
    for rule in raw["states"]["rules"]:
        if rule["id"] == "battle_turn_action_grid":
            rule["all"][0]["name"] = "ghost_template"
            break
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ConfigError, match="unknown template"):
        load_config(path)


def test_unknown_state_in_rule_is_rejected(tmp_path):
    raw = _mutated_config()
    # must be an *enabled* rule: disabled ones are documentation and are not
    # compiled or validated against the state set
    for rule in raw["states"]["rules"]:
        if rule["id"] == "battle_turn_action_grid":
            rule["state"] = "NOT_A_STATE"
            break
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ConfigError, match="unknown state"):
        load_config(path)


def test_bad_roi_values_are_rejected(tmp_path):
    raw = _mutated_config()
    raw["vision"]["rois"]["battle_action_grid"] = [0.9, 0.9, 0.1, 0.1]
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ConfigError, match="empty"):
        load_config(path)


def test_fingerprint_cells_length_validated(tmp_path):
    raw = _mutated_config()
    raw["vision"]["fingerprints"]["dialog_panel"]["cells"] = [1.0, 2.0]
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ConfigError, match="grid_w\\*grid_h\\*3"):
        load_config(path)


def test_duplicate_rule_ids_rejected(tmp_path):
    raw = _mutated_config()
    raw["states"]["rules"].append(copy.deepcopy(raw["states"]["rules"][0]))
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ConfigError, match="duplicate"):
        load_config(path)


def test_yaml_config_supported_when_pyyaml_present(tmp_path):
    yaml = pytest.importorskip("yaml")
    raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True))
    config = load_config(path)
    assert config.reference.capture_px == G.Size(2298, 1428)


def test_yaml_without_pyyaml_is_a_clear_error(tmp_path, monkeypatch):
    import builtins

    raw = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "cfg.yaml"
    path.write_text(json.dumps(raw))
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ConfigError, match="PyYAML"):
        load_config(path)
