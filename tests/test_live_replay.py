"""Replay tests over REAL GAME frames (not video, not synthetic).

`data/replay/live_2026-11-02/` holds three frames captured from the live client
(1.0.8.9, window 1200x800 pt -> 2400x1600 px) plus `labels.json` describing what
each one is. Every label in that file was confirmed by looking at the image, not
by trusting OCR.

What these tests protect:
  * the victory rule really fires on a real victory screen and does NOT fire on a
    real battle screen or a real world screen (measured 0.99 / 0.334 / 0.588 at a
    0.85 threshold);
  * a live window whose detected stage has a different aspect from the reference
    capture must degrade to "no match", never crash. That crash was real: the
    template ends up larger than the search ROI and cv2 raises.
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import pytest

from seerauto import capture as C
from seerauto import geometry as G
from seerauto import vision as V
from seerauto.config import load_config
from seerauto.state import StateMachine, model_from_config

REPLAY = Path(__file__).resolve().parent.parent / "data" / "replay" / "live_2026-11-02"

pytestmark = pytest.mark.skipif(not REPLAY.exists(), reason="live replay set not present")


def _labels():
    return json.loads((REPLAY / "labels.json").read_text(encoding="utf-8"))


def _frame(name: str) -> C.Frame:
    img = cv2.imread(str(REPLAY / name))
    assert img is not None, f"cannot read {name}"
    geo = G.WindowGeometry(
        origin_pt=G.Point(225.0, 53.0),
        size_pt=G.Size(1200.0, 800.0),
        scale=G.RETINA_SCALE,
    )
    return C.make_frame(img, geo)


@pytest.fixture(scope="module")
def analyzer():
    return V.Analyzer.from_config(load_config())


def test_replay_set_is_labelled_and_loadable():
    data = _labels()
    assert data["source"].startswith("real client")
    frames = data["frames"]
    assert len(frames) >= 3
    for entry in frames:
        assert (REPLAY / entry["file"]).exists(), entry["file"]
        for key in ("expect_state", "what"):
            assert entry[key], f"{entry['file']} missing {key}"


def test_victory_frame_scores_high_and_others_do_not(analyzer):
    """The discriminator itself, independent of the state machine."""
    scores = {}
    for entry in _labels()["frames"]:
        sig = analyzer.analyze(_frame(entry["file"]))
        scores[entry["file"]] = sig.template_score("battle_victory_banner")
    victory = scores["victory_moguguai.png"]
    assert victory >= 0.85, f"victory banner only scored {victory:.3f}"
    for name, score in scores.items():
        if name == "victory_moguguai.png":
            continue
        assert score < 0.8, f"{name} false-positive on the victory template: {score:.3f}"


def test_victory_frame_resolves_to_battle_result(analyzer):
    cfg = load_config()
    machine = StateMachine(model_from_config(cfg.states))
    f = _frame("victory_moguguai.png")
    sig = analyzer.analyze(f)
    det = machine.classify(sig)
    assert det.state.value == "BATTLE_RESULT", (
        f"real victory frame classified as {det.state.value} via {det.rule_id}")
    assert det.rule_id == "battle_result_victory"


def test_battle_and_world_frames_are_not_mistaken_for_victory(analyzer):
    cfg = load_config()
    for name in ("battle_turn1.png", "world_after_battles.png"):
        machine = StateMachine(model_from_config(cfg.states))
        sig = analyzer.analyze(_frame(name))
        det = machine.classify(sig)
        assert det.state.value != "BATTLE_RESULT", (
            f"{name} was misread as a result screen via {det.rule_id}")


def test_mismatched_stage_degrades_to_no_match_instead_of_crashing(analyzer):
    """A live window whose stage aspect differs must never raise from cv2.

    Regression guard: before the fix this raised
    `cv2.error: (-215) _img.size().height <= _templ.size().height` because the
    stage-scaled template ended up bigger than the search ROI.
    """
    for entry in _labels()["frames"]:
        # analyze() runs every enabled template; any geometry mismatch must be
        # handled as "not visible" rather than propagating an exception
        sig = analyzer.analyze(_frame(entry["file"]))
        assert isinstance(sig.templates, dict)
        for match in sig.templates.values():
            assert 0.0 <= match.score <= 1.0
