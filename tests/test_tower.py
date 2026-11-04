"""Tests for the 试炼之塔 path — the 25x experience route.

Two things are pinned here, both of which were wrong before 2026-11-05:

1. **The counter means what the yaml says.**  `tower.floors_cleared` is a *count of
   floors cleared*, but `game.tower_status` used to write the reached floor number
   straight into it, so "你当前到达第14层" was recorded as "14 floors cleared".
   Reaching floor N proves N-1 cleared, and a won guardian battle proves one more.

2. **The node actually farms.**  Its playbook used to end at the read-only
   `game.tower_status`, so the node could be "completed" while not one extra floor
   had been fought.  The playbook must now include `game.farm_tower`.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from seerauto.goals.graph import GoalGraph
from seerauto.runner import playbooks
from seerauto.skills import act
from seerauto.skills.act import FarmTower, ReadTowerFloor, build_act_skills
from seerauto.skills.base import Budget, SkillContext
from seerauto.world.model import GameWorldState, Scene


class _Session:
    """Minimal session: OCR returns fixed items, clicks are recorded."""

    def __init__(self, items=None):
        self.items = list(items or [])
        self.clicks: list[tuple[float, float, str]] = []

    def ocr(self, **kw):
        return [dict(i) for i in self.items]

    def click_px(self, x, y, reason=""):
        self.clicks.append((x, y, reason))

        class _Rec:
            sent = True
            refused = None

        return _Rec()

    def export_png(self, path):
        raise RuntimeError("no capture in this test")


class _BattleSession(_Session):
    """A session that is already in a battle, so the tower loop skips the click."""

    def scene(self, frame=None):
        return SimpleNamespace(scene=Scene.BATTLE)

    def wait_for(self, pred, timeout_s=12.0):
        return True


class _StubBattle:
    ok = True
    reason = ""

    def __call__(self, ctx, battles=1, max_seconds=240.0):
        return self


def _ctx(session, world=None):
    return SkillContext(session=session, world=world or GameWorldState(),
                        dry_run=False, allow_input=True,
                        budget=Budget(max_duration_s=10, max_actions=50))


@pytest.fixture
def no_ui(monkeypatch):
    """Neutralise the screen-scraping helpers so only the counter logic runs."""

    monkeypatch.setattr(act, "ensure_world", lambda ctx: None)
    monkeypatch.setattr(act, "_dismiss_dialogs", lambda ctx, limit=3: None)
    monkeypatch.setattr(act.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(ReadTowerFloor, "_read_floor_by_vision",
                        staticmethod(lambda session: None))
    return monkeypatch


# --------------------------------------------------------------------------- #
# counter semantics
# --------------------------------------------------------------------------- #
def test_panel_records_reached_but_never_cleared(no_ui):
    """Reading the panel must not advance `floors_cleared`.

    `floors_cleared` has exactly one writer: the farm loop, per won battle.  The
    first version of this fix derived N-1 from the panel reading, which still let
    the completion predicate be satisfied without a single fight.
    """

    world = GameWorldState()
    session = _Session([{"text": "你当前到达第14层", "conf": 0.9, "px": (500, 300)}])
    result = ReadTowerFloor().run(_ctx(session, world))

    assert result.ok
    assert world.facts["counters.tower.floors_reached"].value == 14
    assert "counters.tower.floors_cleared" not in world.facts


def test_floor_one_means_nothing_cleared_yet(no_ui):
    world = GameWorldState()
    session = _Session([{"text": "你当前到达第1层", "conf": 0.9, "px": (500, 300)}])
    ReadTowerFloor().run(_ctx(session, world))
    assert world.facts["counters.tower.floors_reached"].value == 1
    assert "counters.tower.floors_cleared" not in world.facts


def test_a_vision_only_read_never_advances_the_cleared_counter(no_ui):
    """The VLM misread 14 as 7 on 2026-11-04; it may not move a verified counter."""

    no_ui.setattr(ReadTowerFloor, "_read_floor_by_vision",
                  staticmethod(lambda session: 7))
    world = GameWorldState()
    session = _Session([{"text": "没有层数文字", "conf": 0.9, "px": (1, 1)}])
    result = ReadTowerFloor().run(_ctx(session, world))

    assert result.ok
    assert world.observations["tower_floor_vlm"] == 7
    assert "counters.tower.floors_cleared" not in world.facts
    assert world.facts["counters.tower.floors_reached"].evidence.source.rank == 2  # INFERRED


def test_panel_leaves_an_existing_cleared_count_alone(no_ui):
    """The panel is not the owner of `floors_cleared`, so it must not touch it."""

    from seerauto.world.model import Evidence, Fact

    world = GameWorldState()
    world.facts["counters.tower.floors_cleared"] = Fact(
        20, Evidence.live("earlier battle wins"))
    session = _Session([{"text": "你当前到达第14层", "conf": 0.9, "px": (500, 300)}])
    ReadTowerFloor().run(_ctx(session, world))
    assert world.facts["counters.tower.floors_cleared"].value == 20


# --------------------------------------------------------------------------- #
# the farm loop
# --------------------------------------------------------------------------- #
def test_farm_tower_adds_one_cleared_floor_per_win(no_ui):
    no_ui.setattr(act, "RunBattle", lambda: _StubBattle())
    world = GameWorldState()
    result = FarmTower().run(_ctx(_BattleSession(), world), floors=2, max_seconds=60)

    assert result.ok
    assert result.after["wins"] == 2
    assert world.facts["counters.tower.floors_cleared"].value == 2
    assert world.facts["counters.tower.floors_cleared"].evidence.source.rank >= 3  # LIVE


def test_farm_tower_with_no_win_does_not_touch_the_counter(no_ui):
    class _LosingBattle(_StubBattle):
        ok = False
        reason = "lost"

    no_ui.setattr(act, "RunBattle", lambda: _LosingBattle())
    world = GameWorldState()
    result = FarmTower().run(_ctx(_BattleSession(), world), floors=2, max_seconds=60)

    assert not result.ok
    assert "counters.tower.floors_cleared" not in world.facts


# --------------------------------------------------------------------------- #
# wiring: the node must actually farm
# --------------------------------------------------------------------------- #
def test_tower_playbook_ends_with_the_farm_loop():
    steps = playbooks.playbook("puni.prepare.tower_farm")
    skills = [s.skill for s in steps]
    assert "game.farm_tower" in skills, (
        "the tower playbook must fight, not just read the floor; "
        f"it currently runs {skills}")
    assert skills[-1] == "game.farm_tower"


def test_tower_graph_node_points_at_the_farm_skill():
    graph = GoalGraph.load()
    node = graph.node("puni.prepare.tower_farm")
    assert node.skill == "game.farm_tower"
    # ...and the skill the graph names must exist, or the gate refuses the node.
    registered = {s.spec.id for s in build_act_skills()}
    assert node.skill in registered


def test_every_playbook_step_names_a_registered_skill():
    """A playbook that calls a skill nobody registered fails only at run time.

    Both registries count: act skills *and* observe skills (the truebody life
    detector is an observe skill, and it is a legitimate playbook step).
    """

    from seerauto.skills.observe import build_observe_skills

    registered = ({s.spec.id for s in build_act_skills()}
                  | {s.spec.id for s in build_observe_skills()})
    missing = [(node, step.skill)
               for node, steps in playbooks.PLAYBOOKS.items()
               for step in steps if step.skill not in registered]
    assert missing == []
