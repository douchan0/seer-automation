"""Vision tests against the real screenshots in ``artifacts/``.

Every number asserted here was measured from the fixtures (never from a live
game); thresholds leave margin so small decoder differences do not flake.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from seerauto import capture as C
from seerauto import geometry as G
from seerauto import vision as V
from seerauto.config import load_config
from seerauto.state import StateMachine, model_from_config

pytest.importorskip("cv2")

ARTIFACTS = Path("artifacts")


def has(name: str) -> bool:
    return (ARTIFACTS / name).exists()


def load_frame(name: str) -> C.Frame:
    image = C.read_image(str(ARTIFACTS / name))
    frame = C.make_frame(
        image,
        G.WindowGeometry(),
        G.Rect(0.0, 0.0, *G.DEFAULT_REFERENCE.capture_px.as_tuple()),
    )
    return frame


def with_detected_stage(frame: C.Frame, ref: G.ReferenceSpace = G.DEFAULT_REFERENCE) -> C.Frame:
    rect = V.detect_stage_rect(frame)
    if rect is None:
        return frame
    return frame.with_stage(
        G.StageMap(
            stage_px=rect,
            ref_size=ref.stage_px.size,
            ref_offset=G.Point(ref.stage_px.x, ref.stage_px.y),
        )
    )


# --------------------------------------------------------------------------- #
# stage detection
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not has("game.png"), reason="artifacts/game.png missing")
def test_detect_stage_on_reference_screenshot():
    frame = load_frame("game.png")
    rect = V.detect_stage_rect(frame)
    assert rect is not None
    # measured on the screenshot: stage starts at (102,128), size 2092x1220
    assert rect.x == pytest.approx(102, abs=6)
    assert rect.y == pytest.approx(128, abs=6)
    assert rect.w == pytest.approx(2092, abs=8)
    assert rect.h == pytest.approx(1220, abs=8)


@pytest.mark.skipif(not has("flee.png"), reason="artifacts/flee.png missing")
def test_detect_stage_on_cropped_capture():
    frame = load_frame("flee.png")  # 1100x630 window crop
    rect = V.detect_stage_rect(frame)
    assert rect is not None
    assert rect.w / frame.width == pytest.approx(0.84, abs=0.03)


@pytest.mark.skipif(not has("panel_crop.png"), reason="artifacts/panel_crop.png missing")
def test_detect_stage_returns_none_for_a_panel_crop():
    assert V.detect_stage_rect(load_frame("panel_crop.png")) is None


def test_ensure_stage_falls_back_to_proportional_layout():
    image = np.full((143, 229, 3), 10, dtype=np.uint8)  # tiny, no letterbox
    frame = C.make_frame(image, G.WindowGeometry(), G.Rect(0, 0, 2298, 1428))
    frame.stage = None  # type: ignore[assignment]
    healed = V.ensure_stage(frame)
    assert healed.stage is not None
    assert healed.stage.stage_px.w > 0
    assert healed.stage.ref_size == G.DEFAULT_REFERENCE.stage_px.size


# --------------------------------------------------------------------------- #
# template matching (measured separation)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not has("flee.png"), reason="missing fixture")
def test_battle_action_grid_matches_battle_frames_only():
    config = load_config()
    detector = V.TemplateDetector(V.TemplateCache(config.vision.template_dir))
    spec = V.TemplateSpec(
        "battle_action_grid", roi=config.roi("battle_action_grid"), threshold=0.70
    )
    battle = with_detected_stage(load_frame("flee.png"))
    idle = with_detected_stage(load_frame("quest.png"))
    battle_match = detector.match(battle, spec)
    idle_match = detector.match(idle, spec)
    assert battle_match is not None and battle_match.score > 0.90
    assert idle_match is not None and idle_match.score < 0.50


@pytest.mark.skipif(not (has("flee.png") and has("quest.png")), reason="missing fixture")
def test_idle_action_bar_matches_idle_frames_only():
    config = load_config()
    detector = V.TemplateDetector(V.TemplateCache(config.vision.template_dir))
    spec = V.TemplateSpec("idle_action_bar", roi=config.roi("idle_action_bar"), threshold=0.80)
    idle = detector.match(with_detected_stage(load_frame("quest.png")), spec)
    battle = detector.match(with_detected_stage(load_frame("flee.png")), spec)
    assert idle is not None and idle.score > 0.90
    assert battle is not None and battle.score < 0.50


@pytest.mark.skipif(not (has("game.png") and has("quest.png")), reason="missing fixture")
def test_map_panel_corner():
    config = load_config()
    detector = V.TemplateDetector(V.TemplateCache(config.vision.template_dir))
    spec = V.TemplateSpec("map_panel_corner", roi=config.roi("map_panel_corner"), threshold=0.90)
    map_frame = detector.match(with_detected_stage(load_frame("game.png")), spec)
    idle = detector.match(with_detected_stage(load_frame("quest.png")), spec)
    assert map_frame is not None and map_frame.score > 0.90
    assert idle is not None and idle.score < 0.60


def test_template_cache_missing_template_raises():
    cache = V.TemplateCache()
    with pytest.raises(C.CaptureError):
        cache.load_gray("definitely_not_a_template")


def test_match_returns_none_when_roi_is_not_visible():
    detector = V.TemplateDetector()
    tiny = C.make_frame(
        np.zeros((40, 40, 3), dtype=np.uint8),
        G.WindowGeometry(),
        G.Rect(0, 0, 2298, 1428),
    )
    match = detector.match(tiny, V.TemplateSpec("battle_action_grid", roi=(0.9, 0.9, 0.99, 0.99)))
    assert match is None


# --------------------------------------------------------------------------- #
# fingerprints
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(not (has("ev_box.png") and has("quest.png")), reason="missing fixture")
def test_dialog_panel_fingerprint_separates_panels_from_scenes():
    config = load_config()
    spec = config.vision.fingerprints["dialog_panel"]
    expected = V.Fingerprint(grid=spec.grid, cells=spec.cells, mean=spec.mean, std=spec.std)
    panel = V.patch_signature(with_detected_stage(load_frame("ev_box.png")), spec.roi, grid=spec.grid)
    scene = V.patch_signature(with_detected_stage(load_frame("quest.png")), spec.roi, grid=spec.grid)
    assert panel is not None and scene is not None
    assert V.signature_distance(panel, expected) <= spec.max_distance
    assert V.signature_distance(scene, expected) >= spec.min_distance


@pytest.mark.skipif(not (has("game.png") and has("quest.png")), reason="missing fixture")
def test_map_interior_fingerprint():
    config = load_config()
    spec = config.vision.fingerprints["map_interior"]
    expected = V.Fingerprint(grid=spec.grid, cells=spec.cells, mean=spec.mean, std=spec.std)
    game_map = V.patch_signature(with_detected_stage(load_frame("game.png")), spec.roi, grid=spec.grid)
    scene = V.patch_signature(with_detected_stage(load_frame("quest.png")), spec.roi, grid=spec.grid)
    assert game_map is not None and scene is not None
    assert V.signature_distance(game_map, expected) <= spec.max_distance
    assert V.signature_distance(scene, expected) >= spec.min_distance


def test_patch_signature_shape_and_identical_distance():
    image = np.full((300, 400, 3), 50, dtype=np.uint8)
    frame = C.make_frame(image, G.WindowGeometry(), G.Rect(0, 0, 2298, 1428))
    fp = V.patch_signature(frame, None, grid=(4, 3))
    assert fp is not None
    assert len(fp.cells) == 4 * 3 * 3
    assert V.signature_distance(fp, fp) == 0.0
    tiny = C.make_frame(np.zeros((2, 2, 3), dtype=np.uint8), G.WindowGeometry(), G.Rect(0, 0, 2298, 1428))
    assert V.patch_signature(tiny, None, grid=(4, 4)) is None


# --------------------------------------------------------------------------- #
# change detection
# --------------------------------------------------------------------------- #
def test_frame_delta_identical_is_zero():
    image = np.full((200, 300, 3), 20, dtype=np.uint8)
    a = C.make_frame(image, G.WindowGeometry(), G.Rect(0, 0, 2298, 1428))
    b = C.make_frame(image.copy(), G.WindowGeometry(), G.Rect(0, 0, 2298, 1428))
    assert V.frame_delta(a, b) == pytest.approx(0.0)
    assert V.frame_delta(None, a) == 1.0


def test_frame_delta_detects_a_change():
    image = np.full((200, 300, 3), 20, dtype=np.uint8)
    changed = image.copy()
    changed[:, :] = 200
    a = C.make_frame(image, G.WindowGeometry(), G.Rect(0, 0, 2298, 1428))
    b = C.make_frame(changed, G.WindowGeometry(), G.Rect(0, 0, 2298, 1428))
    assert V.frame_delta(a, b) > 0.5


# --------------------------------------------------------------------------- #
# OCR interface
# --------------------------------------------------------------------------- #
def test_null_fake_and_daemon_ocr():
    frame = C.make_frame(np.zeros((10, 10, 3), dtype=np.uint8), G.WindowGeometry(), G.Rect(0, 0, 2298, 1428))
    assert V.NullOcr().recognize(frame) == []
    fake = V.FakeOcr([V.TextItem("当前回合：3", 0.9, (0.4, 0.8, 0.2, 0.05))])
    assert fake.recognize(frame)[0].text == "当前回合：3"

    class Client:
        available = True

        def send(self, op, **kwargs):
            assert op == "ocr"
            return {
                "items": [
                    {"text": "当前回合：7", "conf": 0.88, "x": 0.4, "y": 0.8, "w": 0.2, "h": 0.05}
                ]
            }

    daemon = V.DaemonOcr(Client())
    frame = C.make_frame(
        np.zeros((10, 10, 3), dtype=np.uint8),
        G.WindowGeometry(),
        G.Rect(0, 0, 2298, 1428),
        meta={"path": "/tmp/x.png"},
    )
    items = daemon.recognize(frame)
    assert items[0].text == "当前回合：7"
    # Vision boxes are bottom-left origin -> converted to top-left normalised
    assert items[0].rect_norm[1] == pytest.approx(0.15)
    # no path -> no OCR call
    assert daemon.recognize(C.make_frame(np.zeros((10, 10, 3), dtype=np.uint8), G.WindowGeometry(), G.Rect(0, 0, 2298, 1428))) == []


def test_find_text():
    items = [V.TextItem("当前回合：2"), V.TextItem("使用次数：25/30"), V.TextItem("胜利")]
    assert V.find_text(items, ["当前回合"])[0].text == "当前回合：2"
    assert V.find_text(items, ["胜利"], exact=True)[0].text == "胜利"
    assert V.find_text(items, ["失败"]) == []


# --------------------------------------------------------------------------- #
# end-to-end classification on the real fixtures
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "fixture,expected",
    [
        ("quest.png", "IDLE"),
        ("flee.png", "BATTLE_TURN"),
        ("battle1.png", "BATTLE_TURN"),
        ("game.png", "DIALOG"),
        ("ev_box.png", "DIALOG"),
    ],
)
def test_real_fixtures_classify_as_expected(fixture, expected):
    if not has(fixture):
        pytest.skip(f"{fixture} missing")
    config = load_config()
    expected_fps = {
        name: V.Fingerprint(grid=spec.grid, cells=spec.cells, mean=spec.mean, std=spec.std)
        for name, spec in config.vision.fingerprints.items()
    }
    analyzer = V.Analyzer.from_config(config)
    machine = StateMachine(model_from_config(config.states, expected_fps))
    frame = with_detected_stage(load_frame(fixture))
    signals = analyzer.analyze(frame)
    detection = machine.classify(signals)
    assert detection.state.value == expected, (fixture, detection.as_dict(), signals.as_dict())


def test_analyzer_only_calls_ocr_when_escalated():
    class CountingOcr:
        name = "counting"

        def __init__(self):
            self.calls = 0

        def recognize(self, frame):
            self.calls += 1
            return []

    config = load_config()
    ocr = CountingOcr()
    analyzer = V.Analyzer.from_config(config, ocr=ocr)
    frame = with_detected_stage(load_frame("quest.png"))
    analyzer.analyze(frame)
    assert ocr.calls == 0
    analyzer.analyze(frame, escalate=True)
    assert ocr.calls == 1
