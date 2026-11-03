"""Tests for the two things the live session taught us the hard way.

1. The client owns **more than one window**.  A small startup dialog (「未检测本地有
   美术包」) sits in front of the playfield, which then stays on 「版本检测中」 forever.
   Code that only asks for "the window" cannot see it — that cost a whole night.
2. Icons carry no OCR-able text; the client names them in a **hover tooltip**.  The
   pet-bag heal button can only be found that way.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

import pytest

from seerauto import live as L
from seerauto.skills.act import DismissStartupPopup, HealPet, ProbeButtons
from seerauto.skills.base import Budget, SkillContext, Status
from seerauto.world.model import GameWorldState


# --------------------------------------------------------------------------- #
# window enumeration
# --------------------------------------------------------------------------- #
FAKE_WL = json.dumps([
    {"id": 1, "layer": 0, "owner": "访达", "title": "x",
     "bounds": {"x": 0, "y": 0, "w": 900, "h": 600}},
    {"id": 80890, "layer": 0, "owner": "SeerGame", "title": "",
     "bounds": {"x": 605, "y": 183, "w": 260, "h": 258}},
    {"id": 80889, "layer": 0, "owner": "SeerGame", "title": "xin计划(1.0.8.9)",
     "bounds": {"x": 270, "y": 74, "w": 1200, "h": 800}},
])


@pytest.fixture
def fake_seerctl(tmp_path, monkeypatch):
    script = tmp_path / "seerctl"
    script.write_text("#!/bin/sh\ncat <<'EOF'\n" + FAKE_WL + "\nEOF\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def test_list_game_windows_sees_the_auxiliary_dialog(fake_seerctl):
    windows = L.list_game_windows(fake_seerctl)
    assert [w["w"] for w in windows] == [1200.0, 260.0], "biggest window first"
    assert windows[0]["aux"] is False
    assert windows[1]["aux"] is True, "the 260x258 startup dialog must be flagged"


def test_list_game_windows_ignores_other_apps(fake_seerctl):
    assert all(w.get("id") != 1 for w in L.list_game_windows(fake_seerctl))


def test_list_game_windows_survives_a_broken_cli(tmp_path):
    missing = str(tmp_path / "nope")
    assert L.list_game_windows(missing) == []


# --------------------------------------------------------------------------- #
# hover-based identification
# --------------------------------------------------------------------------- #
class HoverSession:
    """Fake client whose tooltips are looked up by x position."""

    def __init__(self, tooltips: dict[int, str]):
        self.tooltips = tooltips
        self.hovers: list[tuple[float, float]] = []
        self.clicks: list[tuple[float, float, str]] = []
        self.client = None
        self.pixel_size = (2400.0, 1600.0)

    def hover_px(self, x, y, *, settle_s=0.9, min_conf=0.25):
        self.hovers.append((x, y))
        return {"text": self.tooltips.get(int(x), ""), "items": [], "px": (x, y)}

    def click_px(self, x, y, reason="", **kw):
        self.clicks.append((x, y, reason))

        class _Rec:
            sent = True
            refused = None

        return _Rec()

    def ocr(self, **kw):
        return []


def ctx_for(session) -> SkillContext:
    return SkillContext(session=session, world=GameWorldState(), dry_run=False,
                        allow_input=True, budget=Budget(max_duration_s=60, max_actions=6))


def test_probe_buttons_returns_the_tooltip_of_each_icon():
    session = HoverSession({660: "身边跟随", 760: "", 860: "精灵背包",
                            960: "图鉴", 1060: "属性", 1160: "恢复体力"})
    result = ProbeButtons()(ctx_for(session), x0=660, x1=1160, y=1131, count=6,
                            settle_s=0.0)
    assert result.ok
    buttons = result.after["buttons"]
    assert len(buttons) == 6
    assert buttons[-1]["text"] == "恢复体力"
    assert len(session.hovers) == 6


def test_heal_skill_clicks_the_button_whose_tooltip_says_restore():
    """The heal icon is found by its tooltip, not by a coordinate or a template."""

    session = HoverSession({640: "身边跟随", 744: "", 848: "背包", 952: "图鉴",
                            1056: "属性", 1160: "恢复体力"})

    # the bag reader is exercised elsewhere; stub it to keep this test about hover
    import seerauto.skills.act as act

    class _FakeBag:
        def __init__(self):
            self.calls = 0

        def __call__(self, ctx, **kw):
            self.calls += 1
            hp = [{"hp": 0 if self.calls == 1 else 95, "hp_max": 95}]
            from seerauto.skills.base import SkillResult

            return SkillResult.pass_(self.id, after={"pets": hp, "can_battle": True,
                                                     "level": 34, "xp_to_next": 2573})

    bag = _FakeBag()
    bag.id = "game.read_pet_bag"
    orig = act.ReadPetBag
    act.ReadPetBag = lambda: bag  # type: ignore[assignment]
    try:
        result = HealPet()(ctx_for(session))
    finally:
        act.ReadPetBag = orig
    assert result.ok, result.reason
    assert result.after["healed"] is True
    assert "恢复" in result.proof
    assert session.clicks and session.clicks[0][0] == 1160


def test_heal_skill_fails_honestly_when_no_tooltip_matches():
    session = HoverSession({640: "身边跟随", 744: "", 848: "背包", 952: "图鉴",
                            1056: "属性", 1160: "收藏"})
    import seerauto.skills.act as act

    class _Bag:
        id = "game.read_pet_bag"

        def __call__(self, ctx, **kw):
            from seerauto.skills.base import SkillResult

            return SkillResult.pass_(self.id, after={"pets": [{"hp": 0, "hp_max": 95}]})

    bag = _Bag()
    orig = act.ReadPetBag
    act.ReadPetBag = lambda: bag  # type: ignore[assignment]
    try:
        result = HealPet()(ctx_for(session))
    finally:
        act.ReadPetBag = orig
    assert result.status is Status.FAIL
    assert "no heal button" in result.reason


# --------------------------------------------------------------------------- #
# popup dismissal
# --------------------------------------------------------------------------- #
def test_popup_preference_is_safe_first():
    order = DismissStartupPopup.DEFAULT_PREFERENCE
    assert order.index("暂不导入") < order.index("确定")
    assert "前往下载" not in order, "downloading is never an automatic choice"


def test_popup_skill_skips_when_there_is_no_aux_window(monkeypatch):
    monkeypatch.setattr(L, "list_game_windows", lambda *a, **kw: [])
    session = HoverSession({})
    result = DismissStartupPopup()(ctx_for(session))
    assert result.status is Status.SKIPPED
    assert not session.clicks
