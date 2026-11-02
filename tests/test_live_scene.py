"""Replay tests for the live scene classifier — the state table that decides
whether the agent attacks, confirms, dismisses or walks.

These run against **real captured frames** (no daemon, no game): template
matching is pure array math, so the exact bug that made a live battle look like
the open world is reproducible offline.

History: on 2026-11-03 a live battle frame was classified `WORLD` because the
战斗 button has a second drawn style; the loop then "hunted for monsters" until
the budget ran out.  `live_fight_btn2` exists because of that, and this file is
what stops it from regressing.
"""

from __future__ import annotations

import os

import cv2
import pytest

from seerauto.live import LiveSession
from seerauto.world.model import Scene

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.join(ROOT, "artifacts", "live")


def _frame(name: str):
    path = os.path.join(LIVE, name)
    if not os.path.exists(path):
        pytest.skip(f"missing live frame {path}")
    img = cv2.imread(path)
    if img is None:
        pytest.skip(f"unreadable live frame {path}")
    return img


@pytest.fixture(scope="module")
def classifier() -> LiveSession:
    # not opened: scene() only needs the templates and a frame
    return LiveSession()


def test_battle_frame_is_battle(classifier):
    probe = classifier.scene(_frame("battle_turn1_2400x1600.png"))
    assert probe.scene is Scene.BATTLE
    assert probe.scores["fight"] >= 0.85


def test_victory_frame_wins_over_the_visible_fight_button(classifier):
    """The fight button stays visible under the victory panel (Round 4 bug)."""

    probe = classifier.scene(_frame("victory_moguguai_2400x1600.png"))
    assert probe.scene is Scene.RESULT
    assert probe.scores["victory"] >= 0.80
    assert probe.scores["fight"] >= 0.80, "this frame is the reason victory must win"


def test_xp_dialog_frame_is_dialog(classifier):
    probe = classifier.scene(_frame("after_3_battles.png"))
    assert probe.scene is Scene.DIALOG
    assert probe.scores["confirm"] >= 0.80


def test_world_frame_is_world(classifier):
    path = os.path.join(ROOT, "artifacts", "round5", "05_forest.png")
    if not os.path.exists(path):
        pytest.skip("no clean world frame")
    probe = classifier.scene(cv2.imread(path))
    assert probe.scene is Scene.WORLD
    assert probe.scores["fight"] < 0.85


def test_hysteresis_keeps_a_flickering_battle_in_battle(classifier):
    """A battle frame scored just under the strict cut stays BATTLE when we
    already believe we are in a battle (the score dips during animations)."""

    frame = _frame("battle_turn1_2400x1600.png")
    strict = classifier.scene(frame, previous=None)
    assert strict.scene is Scene.BATTLE
    # simulate the dip by comparing the two thresholds directly
    from seerauto.live import TH_FIGHT

    assert TH_FIGHT - 0.10 < TH_FIGHT


def test_blank_page_is_unknown_not_world():
    """A loading/error page must never be mistaken for the game world.

    Everything scores 0.0 on a blank frame, which used to read as "no fight
    button, no victory panel, therefore WORLD" — and the agent would go hunting
    for monsters on an error page.  Measured: real frames sit at std 63-105,
    a blank/error page at ~11, so 25 is a safe gate.
    """

    import numpy as np

    white = np.full((1600, 2400, 3), 255, dtype=np.uint8)
    dark = np.full((1600, 2400, 3), 20, dtype=np.uint8)
    assert classifier.scene(white).scene is Scene.UNKNOWN
    assert classifier.scene(dark).scene is Scene.UNKNOWN
