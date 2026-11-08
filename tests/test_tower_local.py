"""Regression tests for the local tower engine (no game, no network, no LLM).

These pin the three live failures measured on 2026-11-07:

* A - the first skill click must be **local and inside the 10 s countdown**
  (the hand-driven run lost 56.3 s, so the game picked the move);
* the real-time turn path must never call ``RunBattle.learn_power_table`` (it shells
  out to an external vision model with a 90 s timeout before the first click);
* B - settlement must read the popup, capture the verbatim XP line and click ONE
  confirm at a time, with the "visible -> click" latency recorded;
* C - the guardian must be found in the current frame, excluding the player sprite
  (clicking our own sprite is what opened the 个人信息 overlay on floor 2).
"""

from __future__ import annotations

import numpy as np
import pytest

from seerauto.skills.act import RunBattle
from seerauto.skills.tower import (
    GuardianFinder,
    TowerBattleLocal,
    TowerSettlement,
    XP_RE,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def item(text: str, x: float, y: float) -> dict:
    return {"text": text, "px": (x, y), "conf": 0.9}


class FakeClock:
    """Deterministic monotonic clock: sleep advances time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(0.0, float(seconds))


class BattleSession:
    """A scripted battle: every move click advances the round."""

    def __init__(self, rounds: int = 2, *, finish: str = "victory") -> None:
        self.round = 1
        self.rounds = rounds
        self.finish = finish
        self.clicks: list[tuple[float, float, str]] = []
        self.ended = False
        #: PP per slot; a move click must decrement it (the runner's PP evidence)
        self.pp = {1: 10, 2: 12}

    def ocr(self, min_conf: float = 0.15):  # noqa: ARG002 - API shape
        if self.ended:
            return [item("胜利", 900, 700), item("获得胜利", 950, 730),
                    item("确认", 1191, 996)]
        return [
            item(f"当前回合：{self.round}", 1500, 300),
            item(f"使用次数：{self.pp[1]}/25", 1200, 1246),
            item(f"使用次数：{self.pp[2]}/25", 1456, 1246),
        ]

    def click_px(self, x: float, y: float, reason: str = "") -> bool:
        self.clicks.append((x, y, reason))
        if reason == "tower_move":
            slot = 1 if x <= 1300 else 2
            self.pp[slot] = max(0, self.pp[slot] - 1)
            self.round += 1
            if self.round > self.rounds:
                self.ended = True
        return True


def make_engine(clock: FakeClock, **kwargs) -> TowerBattleLocal:
    return TowerBattleLocal(clock=clock.now, sleep=clock.sleep, **kwargs)


# --------------------------------------------------------------------------- #
# A - local decision, clicked before the countdown
# --------------------------------------------------------------------------- #

def test_first_skill_click_happens_locally_inside_the_countdown():
    clock = FakeClock()
    session = BattleSession(rounds=2)
    outcome = make_engine(clock).run(session)

    assert outcome.won is True
    assert outcome.turns == 2
    assert outcome.late_turns == 0
    # PP-evidence contract (04): the killing turn has no readable PP afterwards, so it
    # is an unverified (still real) effect rather than an upgraded active success.
    assert outcome.pp_verified_turns == 1
    assert outcome.active_success_turns == 1
    assert outcome.active_unverified == 1

    move_clicks = [c for c in session.clicks if c[2] == "tower_move"]
    assert len(move_clicks) == 2
    # the click lands on a calibrated slot, not a guess
    assert move_clicks[0][:2] in {(1200.0, 1246.0), (1456.0, 1246.0),
                                  (1200.0, 1368.0), (1458.0, 1368.0)}
    for timing in outcome.timings:
        assert timing["clicked"] is True
        assert timing["click_s"] < 10.0, timing
        assert timing["late_vs_countdown"] is False
        assert "t_click" in timing and "t_round_ui" in timing


def test_a_slow_turn_exhausts_the_budget_before_dispatch():
    """If the click lands at/after the countdown it must NOT count as active play."""

    clock = FakeClock()
    session = BattleSession(rounds=1)

    # make reading the move slots take longer than the countdown
    original = RunBattle.read_skills.__func__

    def slow_read_skills(cls, _session, **kwargs):
        clock.t += 11.0
        return original(cls, _session, **kwargs)

    RunBattle.read_skills = classmethod(slow_read_skills)
    try:
        outcome = make_engine(clock).run(session)
    finally:
        RunBattle.read_skills = classmethod(original)

    assert outcome.unknown is True
    assert outcome.active_success_turns == 0
    assert outcome.attempts == 0
    assert "countdown budget" in outcome.reason


def test_real_time_turn_path_never_calls_the_external_power_table_learner(monkeypatch):
    """learn_power_table shells out to a vision model with a 90 s timeout."""

    def boom(*_args, **_kwargs):
        raise AssertionError("learn_power_table must never run on the turn path")

    monkeypatch.setattr(RunBattle, "learn_power_table", classmethod(boom))

    clock = FakeClock()
    session = BattleSession(rounds=1)
    outcome = make_engine(clock).run(session)
    assert outcome.won is True
    assert outcome.turns == 1


def test_battle_ui_never_appearing_is_unknown_not_a_click():
    class NoBattle:
        def __init__(self) -> None:
            self.clicks = []

        def ocr(self, min_conf: float = 0.15):  # noqa: ARG002
            return [item("公屏聊天", 100, 100)]

        def click_px(self, x, y, reason=""):  # noqa: ARG002
            self.clicks.append((x, y))
            return True

    clock = FakeClock()
    session = NoBattle()
    outcome = make_engine(clock, turn_timeout_s=2.0).run(session)
    assert outcome.unknown is True
    assert outcome.won is False
    assert session.clicks == []


# --------------------------------------------------------------------------- #
# B - settlement: read then click, one confirm at a time, latency recorded
# --------------------------------------------------------------------------- #

class SettlementSession:
    """Popup chain: victory -> XP -> 积累经验 -> nothing."""

    def __init__(self) -> None:
        self.step = 0
        self.clicks: list[tuple[float, float, str]] = []

    def ocr(self, min_conf: float = 0.2):  # noqa: ARG002
        if self.step == 0:
            return [item("胜利", 900, 700), item("确认", 1191, 996)]
        if self.step == 1:
            return [item("XIN精灵获得经验：1710", 1100, 700),
                    item("离升级还需经验：4650", 1100, 740),
                    item("确认", 1188, 999)]
        if self.step == 2:
            return [item("恭喜你得到了5个积累经验", 1100, 700),
                    item("确认", 1198, 983)]
        return []

    def click_px(self, x: float, y: float, reason: str = "") -> bool:
        self.clicks.append((x, y, reason))
        self.step += 1
        return True


def test_settlement_captures_the_xp_line_before_clicking_it_away():
    session = SettlementSession()
    clock = FakeClock()
    result = TowerSettlement(clock=clock.now, sleep=clock.sleep,
                             positive_evidence=lambda: True).settle(session)

    assert result.xp_texts == ["XIN精灵获得经验：1710"]
    assert result.confirms == 3
    assert result.closed is True
    assert len(result.latencies) == 3
    # one click per popup, and every confirm hit the CURRENT popup's button
    assert [c[2] for c in session.clicks] == ["settle_确认"] * 3
    assert session.clicks[1][:2] == (1188.0, 999.0)
    # latency is measured locally and is small (no model round trip)
    assert max(result.latencies) < 1.0


def test_settlement_does_not_click_when_only_an_overlay_is_up():
    class OverlaySession:
        def __init__(self) -> None:
            self.clicks = []

        def ocr(self, min_conf: float = 0.2):  # noqa: ARG002
            return [item("昵称：hello", 700, 400), item("米米号：188365991", 700, 440),
                    item("精灵信息", 700, 480)]

        def click_px(self, x, y, reason=""):  # noqa: ARG002
            self.clicks.append((x, y))
            return True

    session = OverlaySession()
    clock = FakeClock()
    result = TowerSettlement(clock=clock.now, sleep=clock.sleep,
                             positive_evidence=lambda: True).settle(session)
    assert session.clicks == []
    assert result.confirms == 0


# --------------------------------------------------------------------------- #
# C - guardian found in the CURRENT frame, never the player sprite
# --------------------------------------------------------------------------- #

def _frame() -> np.ndarray:
    """Beige platform, cyan inlay, an orange ally sprite and a blue guardian."""

    frame = np.zeros((1600, 2400, 3), np.uint8)
    frame[:, :] = (170, 200, 215)          # BGR beige
    frame[600:1100, 900:1500] = (200, 190, 150)   # BGR cyan-ish inlay
    # our own pet: orange, left of centre
    frame[700:760, 300:360] = (30, 90, 240)       # BGR orange
    # the guardian: blue, near the platform centre
    frame[820:900, 1180:1260] = (220, 120, 40)    # BGR blue
    return frame


def test_guardian_finder_picks_the_coloured_sprite_not_the_platform():
    finder = GuardianFinder()
    found = finder.find(_frame(), [])
    assert found is not None
    cx, cy = found["px"]
    assert 1150 <= cx <= 1290 and 800 <= cy <= 920, found
    assert found["area"] >= finder.min_area_px


def test_an_orange_sprite_is_a_valid_candidate():
    """Colour must NOT decide faction (supervisor 2026-11-07 09:06).

    The floor-3 guardian measured hue 20 (BGR 109,182,218 - an orange 烈焰猩猩), so
    "orange == our pet, exclude it" deleted the guardian.  A coloured sprite is a
    candidate; the battle UI is what verifies it.
    """

    frame = np.zeros((1600, 2400, 3), np.uint8)
    frame[:, :] = (170, 200, 215)
    frame[700:760, 300:360] = (30, 90, 240)   # orange sprite
    finder = GuardianFinder()
    found = finder.find(frame, [])
    assert found is not None
    cx, cy = found["px"]
    assert 300 <= cx <= 360 and 700 <= cy <= 760


def test_the_player_sprite_is_excluded_by_its_name_label():
    """The player avatar is excluded by the OCR-visible ``hello`` label only."""

    frame = np.zeros((1600, 2400, 3), np.uint8)
    frame[:, :] = (170, 200, 215)
    frame[820:900, 1180:1260] = (220, 120, 40)   # a blue sprite where the player is
    items = [item("hello", 1220, 940)]           # its name label, under the sprite
    finder = GuardianFinder()
    # without the label the sprite is a candidate
    assert finder.find(frame, []) is not None
    # with the label over it, it is excluded
    assert finder.find(frame, items) is None


def test_xp_regex_matches_the_verbatim_line():
    assert XP_RE.search("XIN精灵获得经验：1710").group(1) == "1710"
    assert XP_RE.search("XIN精灵获得经验: 3003").group(1) == "3003"
    assert XP_RE.search("离升级还需经验：4650") is None
