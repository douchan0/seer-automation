"""Recorded pixels and offline Vision, with every replay click simulated."""

from pathlib import Path
import os

import cv2
import pytest

from seerauto import capture as C
from seerauto import geometry as G
from seerauto.skills.act import RunBattle
from seerauto.skills.tower import GuardianFinder, TowerBattleLocal, TowerSettlement
from tools.tower_live import BIN, DaemonSession, locate_guardian, read_floor_stable


ROOT = Path(__file__).resolve().parents[1]
GEO = G.WindowGeometry(G.Point(0, 0), G.Size(1200, 800), 2)


def test_floor_14_sprite_matches_but_another_floor_template_does_not():
    frame = cv2.imread(str(ROOT / "artifacts/round6/tower_floor14.png"))
    finder = GuardianFinder()
    result = finder.locate(frame, [], 14, (1221, 1255))
    assert result is not None
    assert result["px"] == pytest.approx((1208.5, 751.5))
    assert finder.locate(frame, [], 2, (1221, 1255)) is None


def test_pp_counter_cannot_be_assigned_to_two_slots():
    readings = [{"text": "使用次数：8/25", "px": (1200, 1281)}]
    cells = RunBattle.read_skills(None, items=readings)
    assert [(cell["slot"], cell["pp"]) for cell in cells] == [(1, 8)]


@pytest.fixture
def replay():
    folder = os.environ.get("SEERAUTO_TOWER_FRAME_DIR")
    if not folder or not Path(BIN).is_file():
        pytest.skip("needs the private historical tower frames and a built seerdx")
    client = C.DaemonClient(BIN)
    try:
        yield Path(folder), client
    finally:
        client.close()


def test_guardian_frames_and_profile_overlay(replay):
    folder, client = replay
    session = DaemonSession(client, GEO)
    finder = GuardianFinder()
    cases = (
        ("towerD_turn2_2350", 1, (1217.5, 776.5)),
        ("ext_floor2_clear_0827", 2, (1221.0, 780.5)),
        ("ext_floor3_clear_0834", 3, (1211.5, 781.5)),
        ("floor2_rewards_done_0019", 3, (1211.5, 781.5)),
        ("ext_floor2_clean_0803", 2, None),
        ("ext_floor2_0759", 2, None),
    )
    for name, floor, expected in cases:
        frame = cv2.imread(str(folder / f"{name}.png"))
        assert frame is not None, name
        session.latest = lambda: frame
        result, _ = locate_guardian(session, finder, floor)
        if expected is None:
            assert result is None, name
        else:
            assert result is not None, name
            assert result["px"] == pytest.approx(expected, abs=6)


def test_reward_chain_changes_panel_once_per_replay_click(replay):
    folder, client = replay
    names = ("catch_battle_ui_2355", "towerD_dialog1_2357", "towerD_dialog2_2357",
             "towerD_dialog3_2357", "towerD_dialog4_2357")
    frames = [cv2.imread(str(folder / f"{name}.png")) for name in names]
    assert all(frame is not None for frame in frames)

    class ReplaySession(DaemonSession):
        def __init__(self):
            super().__init__(client, GEO)
            self.clicks = []

        def latest(self):
            return frames[len(self.clicks)]

        def click_px(self, x, y, reason=""):
            self.clicks.append((x, y))
            return True

    session = ReplaySession()
    defeat = cv2.imread(str(folder / "tower_defeat_panel_0107.png"))
    assert defeat is not None
    battle = TowerBattleLocal()
    assert battle._state(session)[0] == "victory"
    classify_defeat = DaemonSession(client, GEO)
    classify_defeat.latest = lambda: defeat
    assert battle._state(classify_defeat)[0] == "defeat"

    result = TowerSettlement(positive_evidence=lambda: read_floor_stable(
        session, require_clear=True) == 2).settle(session)
    assert result.closed
    assert result.confirms == 4
    for x, y in session.clicks:
        assert (x, y) == pytest.approx((1200, 990), abs=12)
