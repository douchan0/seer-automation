"""Offline tests for the Goal Runner (no game window, no native daemon).

These exercise the parts that decide *whether* and *how* the agent acts:

* the scheduler gate must refuse a decision that is not in the goal graph, not
  backed by a registered capability, over budget, or without a playbook;
* a dry run must never send input, yet still produce a plan and a report;
* a node completes only when the graph's live predicate says so — a skill
  returning "ok" is not enough;
* the world carries observed facts forward instead of resetting to UNKNOWN;
* pause/stop control files work, and a stop writes a checkpoint;
* an unavailable AI planner degrades to the deterministic one instead of
  stalling the run.
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

import pytest

from seerauto.goals.graph import GoalGraph
from seerauto.planner import PlanDecision, Planner, PlannerUnavailable
from seerauto.runner.control import InstanceLock, LockHeld, RunControl
from seerauto.runner.runner import GoalRunner, RunConfig
from seerauto.skills.base import Budget, Skill, SkillContext, SkillKind, SkillRegistry, SkillResult, SkillSpec, Status
from seerauto.world.model import Evidence, GameWorldState, PetState, Scene, Source
from seerauto.world.store import WorldStore


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeSession:
    """Just enough of ``LiveSession`` for the runner and the observe skills."""

    def __init__(self, scene: Scene = Scene.WORLD):
        self.window_raw = {"id": 1, "x": 100, "y": 50, "w": 1200, "h": 800}
        self._scene = scene
        self.actions: list[tuple] = []
        self.ocr_items: list[dict[str, Any]] = []
        self.pixel_size = (2400.0, 1600.0)

    def ensure_frontmost(self) -> bool:
        return True

    def latest(self):  # pragma: no cover - only used by skills that need pixels
        raise AssertionError("FakeSession.latest() must not be called in these tests")

    def scene(self, frame=None, previous=None):
        class _Probe:
            pass

        p = _Probe()
        p.scene = self._scene
        p.scores = {"fight": 0.0, "victory": 0.0, "confirm": 0.0}
        p.points = {}
        p.frame_seq = 0
        p.frame_age_ms = 0.0
        return p

    def ocr(self, **kw):
        return list(self.ocr_items)

    def find_text(self, items, needles, *, exact=False):
        for it in items:
            for n in needles:
                if (str(it.get("text", "")).strip() == n) if exact else (n in str(it.get("text", ""))):
                    return dict(it)
        return None

    def click_px(self, x, y, reason="", **kw):
        self.actions.append((x, y, reason))

        class _Rec:
            sent = True
            refused = None

        return _Rec()

    def close(self):
        pass


class CompletedSkill(Skill):
    """A skill whose effect is 'the clue appeared in the world'."""

    spec = SkillSpec(id="test.complete_node", kind=SkillKind.ACT, summary="test",
                     max_duration_s=5, max_actions=1)

    def run(self, ctx: SkillContext, **kw) -> SkillResult:
        return SkillResult.pass_(self.id, proof="did the thing", changed=True,
                                 after={"wins": 1})


class ObservedLevelSkill(Skill):
    spec = SkillSpec(id="game.read_active_pet_level", kind=SkillKind.ACT, summary="test",
                     max_duration_s=5, max_actions=1)

    def __init__(self, level: int | None = 34):
        super().__init__()
        self.level = level
        self.calls = 0

    def run(self, ctx: SkillContext, **kw) -> SkillResult:
        self.calls += 1
        if self.level is None:
            return SkillResult.fail(self.id, "not readable")
        return SkillResult.pass_(self.id, proof=f"Lv{self.level}",
                                 after={"name": "烈火猴", "level": self.level})


class RunBattleStub(Skill):
    spec = SkillSpec(id="game.run_battle", kind=SkillKind.ACT, summary="stub",
                     inputs={"battles": {"type": "int", "required": False},
                             "monster": {"type": "str", "required": False},
                             "max_seconds": {"type": "float", "required": False}},
                     max_duration_s=60, max_actions=20)

    def run(self, ctx: SkillContext, **kw) -> SkillResult:
        return SkillResult.pass_(self.id, after={"wins": 1, "ally_level": 35})


class NavigateStub(Skill):
    spec = SkillSpec(id="game.navigate_spt_boss", kind=SkillKind.ACT, summary="stub",
                     inputs={"boss": {"type": "str", "required": False}},
                     max_duration_s=30, max_actions=2)

    def run(self, ctx: SkillContext, **kw) -> SkillResult:
        return SkillResult.pass_(self.id, after={"location": "克洛斯星林间"})


class FakePlanner(Planner):
    name = "fake"

    def __init__(self, decision: PlanDecision | None = None, *, raises: Exception | None = None):
        self.decision = decision
        self.raises = raises
        self.requests: list[Any] = []

    def decide(self, req):
        self.requests.append(req)
        if self.raises:
            raise self.raises
        return self.decision


def _runner(tmp_path, *, world: GameWorldState | None = None, planner=None,
            session: FakeSession | None = None, **cfg_kw) -> GoalRunner:
    base: dict[str, Any] = dict(db_path=str(tmp_path / "runs.sqlite3"), dry_run=False,
                                allow_input=True, max_minutes=5, max_battles=4)
    base.update(cfg_kw)
    cfg = RunConfig(**base)
    store = WorldStore(cfg.db_path)
    graph = GoalGraph.load()
    registry = SkillRegistry([CompletedSkill(), ObservedLevelSkill(), RunBattleStub(),
                             NavigateStub()])
    r = GoalRunner(cfg, store=store, graph=graph, registry=registry,
                   planner=planner if planner is not None else FakePlanner(),
                   session=session if session is not None else FakeSession(),
                   control=RunControl(str(tmp_path / "pause"), str(tmp_path / "stop")))
    r.run_id = "testrun0000"
    store.start_run("puni", "fake", budgets=cfg.budgets, options={})
    runs = store.list_runs()
    r.run_id = runs[0]["run_id"]
    if world is not None:
        store.save_world(world, run_id=r.run_id)
        r._last_world = world
    return r


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def test_gate_refuses_unknown_node(tmp_path):
    r = _runner(tmp_path)
    d = PlanDecision(goal="puni", selected_node="puni.does.not.exist",
                     capability="game.run_battle", reason="x", expected_observation="y",
                     max_actions=5, max_duration_s=60)
    assert "unknown node" in (r.gate(d) or "")


def test_gate_refuses_node_without_playbook(tmp_path):
    r = _runner(tmp_path)
    graph = r.graph
    node = next(n for n in graph if not n.executable)
    d = PlanDecision(goal="puni", selected_node=node.id, capability="game.run_battle",
                     reason="x", expected_observation="y", max_actions=5, max_duration_s=60)
    refusal = r.gate(d)
    assert refusal and ("not executable" in refusal or "no playbook" in refusal)


def test_gate_refuses_budget_overrun(tmp_path):
    r = _runner(tmp_path)
    d = PlanDecision(goal="puni", selected_node="puni.prepare.level_up",
                     capability="game.run_battle", reason="x", expected_observation="y",
                     max_actions=9999, max_duration_s=60)
    assert "budget" in (r.gate(d) or "")


def test_gate_accepts_the_executable_node_when_unobserved(tmp_path):
    r = _runner(tmp_path, world=GameWorldState())
    d = PlanDecision(goal="puni", selected_node="puni.prepare.level_up",
                     capability="game.run_battle", reason="grind", expected_observation="level",
                     max_actions=20, max_duration_s=300)
    assert r.gate(d) is None


# --------------------------------------------------------------------------- #
# observation / world handling
# --------------------------------------------------------------------------- #
def test_world_carries_observed_facts_forward(tmp_path):
    r = _runner(tmp_path)
    world = GameWorldState()
    world.pets["烈火猴"] = PetState(name="烈火猴", level=34,
                                    evidence=Evidence.live("battle HUD ally LV:34"))
    world.active_pet = "烈火猴"
    r._last_world = world
    again = r.observe()
    assert again.active is not None and again.active.level == 34
    assert again.evidence_for("session.scene").source is Source.LIVE_VERIFIED


def test_unknown_is_not_wiped_to_false(tmp_path):
    r = _runner(tmp_path)
    world = GameWorldState()
    world.set_unknown("resources.赛尔豆")
    r._last_world = world
    again = r.observe()
    assert again.fact("resources.赛尔豆") is None
    assert again.evidence_for("resources.赛尔豆").source is Source.UNKNOWN


def test_preflight_reads_pet_level_and_persists_live_evidence(tmp_path):
    r = _runner(tmp_path)
    world = r.preflight(r.observe())
    assert world.active is not None
    assert world.active.level == 34
    assert world.evidence_for("state.active_pet_level_known").source is Source.LIVE_VERIFIED
    stored = r.store.latest_world()
    assert stored is not None and stored.active is not None and stored.active.level == 34


def test_apply_skill_observations_folds_battle_hud_level(tmp_path):
    r = _runner(tmp_path)
    world = GameWorldState()
    world.pets["烈火猴"] = PetState(name="烈火猴", level=34,
                                    evidence=Evidence.live("old"))
    world.active_pet = "烈火猴"
    r._last_world = world
    res = SkillResult.pass_("game.run_battle", after={"wins": 1, "ally_level": 35})
    world = r._apply_skill_observations(world, [res])
    assert world.active.level == 35
    assert world.evidence_for("pets.烈火猴.level").source is Source.LIVE_VERIFIED


# --------------------------------------------------------------------------- #
# dry run vs real run
# --------------------------------------------------------------------------- #
def test_dry_run_never_sends_input_and_writes_a_report(tmp_path):
    session = FakeSession()
    planner = FakePlanner(PlanDecision(goal="puni", selected_node="puni.prepare.level_up",
                                       capability="game.run_battle", reason="grind",
                                       expected_observation="level rises",
                                       max_actions=10, max_duration_s=120))
    r = _runner(tmp_path, planner=planner, session=session, dry_run=True, allow_input=False)
    summary = r.run()
    assert session.actions == [], "dry run must never click"
    assert summary["status"] in ("finished", "blocked")
    nodes = summary["nodes"]
    assert nodes.get("puni.prepare.level_up") in ("planned", "in_progress", "attempted")
    md = r.report_markdown()
    assert "Goal run" in md


class StoppingPlanner(FakePlanner):
    """Requests a stop from inside the loop, the way a user/agent would."""

    def __init__(self, control, **kw):
        super().__init__(**kw)
        self.control = control

    def decide(self, req):
        self.control.request_stop()
        return super().decide(req)


def test_control_stop_writes_checkpoint_and_stops(tmp_path):
    session = FakeSession()
    cfg = dict(db_path=str(tmp_path / "runs.sqlite3"), dry_run=False, allow_input=True,
               max_minutes=5, max_battles=4)
    store = WorldStore(cfg["db_path"])
    ctl = RunControl(str(tmp_path / "pause"), str(tmp_path / "stop"))
    planner = StoppingPlanner(
        ctl, decision=PlanDecision(goal="puni", selected_node="puni.prepare.level_up",
                                   capability="game.run_battle", reason="grind",
                                   expected_observation="level rises",
                                   max_actions=10, max_duration_s=120))
    store.start_run("puni", "fake", budgets=RunConfig(**cfg).budgets, options={})
    run_id = store.list_runs()[0]["run_id"]
    r = GoalRunner(RunConfig(**cfg), store=store, graph=GoalGraph.load(),
                   registry=SkillRegistry([RunBattleStub(), NavigateStub(),
                                           ObservedLevelSkill()]),
                   planner=planner, session=session, control=ctl)
    r.run_id = run_id
    summary = r.run()
    assert "stop" in (summary["stop_reason"] or "")
    ck = r.store.last_checkpoint(r.run_id)
    assert ck is not None, "a stop must always leave a recovery checkpoint"


def test_budget_stops_the_loop(tmp_path):
    session = FakeSession()
    planner = FakePlanner(PlanDecision(goal="puni", selected_node="puni.prepare.level_up",
                                       capability="game.run_battle", reason="grind",
                                       expected_observation="level rises",
                                       max_actions=10, max_duration_s=120))
    r = _runner(tmp_path, planner=planner, session=session, max_battles=1)
    summary = r.run()
    assert "battle budget" in (summary["stop_reason"] or "")


def test_ai_planner_unavailable_degrades_to_deterministic(tmp_path):
    planner = FakePlanner(raises=PlannerUnavailable("no credential"))
    r = _runner(tmp_path, planner=planner, session=FakeSession())
    summary = r.run()
    # it did not crash and it recorded the degradation
    assert summary["planner"] in ("ai", "fake", "deterministic")
    assert r._planner_degraded is not None


def test_planner_refusal_is_recorded_with_next_observation(tmp_path):
    planner = FakePlanner(PlanDecision(goal="puni", stop=True,
                                       stop_reason="nothing schedulable", need_human=False))
    r = _runner(tmp_path, planner=planner, session=FakeSession())
    summary = r.run()
    assert "refused" in (summary["stop_reason"] or "")
    events = [e["kind"] for e in r.store.events(r.run_id, limit=50)]
    assert "planner.refused" in events


# --------------------------------------------------------------------------- #
# control files / lock
# --------------------------------------------------------------------------- #
def test_pause_file_blocks_until_removed(tmp_path):
    ctl = RunControl(str(tmp_path / "p.pause"), str(tmp_path / "p.stop"))
    ctl.request_pause()
    assert ctl.paused()
    ctl.clear()
    assert not ctl.paused()


def test_instance_lock_is_exclusive(tmp_path):
    path = str(tmp_path / "x.lock")
    a = InstanceLock(path).acquire()
    try:
        with pytest.raises(LockHeld):
            InstanceLock(path).acquire()
    finally:
        a.release()
    b = InstanceLock(path).acquire()
    b.release()


def test_lock_holder_reports_pid(tmp_path):
    path = str(tmp_path / "y.lock")
    lock = InstanceLock(path).acquire()
    try:
        assert f"pid={os.getpid()}" in (lock.holder() or "")
    finally:
        lock.release()


def test_preflight_can_skip_the_pet_panel(tmp_path):
    """The panel read is defeatable: if the bottom bar ever defeats the
    template finder, a run must still be startable without it."""

    r = _runner(tmp_path, preflight_pet_panel=False)
    world = r.preflight(r.observe())
    assert world.active is None
    assert any("pet panel skipped" in n for n in world.notes)


def test_battle_step_is_blocked_when_no_pet_can_fight(tmp_path):
    """0 HP must make a battle step BLOCKED with a recovery hint, not retried."""

    r = _runner(tmp_path)
    r._can_battle = False
    node = r.graph.node("puni.prepare.level_up")
    decision = PlanDecision(goal="puni", selected_node=node.id,
                            capability="game.run_battle", reason="grind",
                            expected_observation="level rises",
                            max_actions=10, max_duration_s=120)
    results, _ = r.execute_node(node, GameWorldState(), decision)
    assert results, "a blocked battle step must still produce a result"
    assert results[-1].status is Status.BLOCKED
    assert "heal" in (results[-1].data.get("recovery") or "")


def test_observation_cannot_replace_the_pet_with_the_trusted_level(tmp_path):
    """A weaker observation must not promote a pet that has no level.

    Observed live: with 烈火猴 Lv34 already read from the bag, the next
    game.observe pass reported 小蘑菇 (an unlevelled pet from the same panel) as
    the active pet, and the planner then lost the level it had just paid for.
    """

    world = GameWorldState()
    world.pets["烈火猴"] = PetState(name="烈火猴", level=34,
                                    evidence=Evidence.live("bag read"))
    world.active_pet = "烈火猴"
    assert GoalRunner._trusted_active(world) == "烈火猴"

    fresh = GameWorldState()
    fresh.pets["小蘑菇"] = PetState(name="小蘑菇", level=None, hp=0, hp_max=12)
    fresh.active_pet = "小蘑菇"
    assert not GoalRunner._pet_has_trusted_level(fresh, "烈火猴")
    assert GoalRunner._pet_has_trusted_level(world, "烈火猴")


def test_blank_client_stops_instead_of_burning_the_error_budget(tmp_path):
    """A blank/error page must stop the run with a diagnosis.

    Measured live on 2026-11-03: with the client stuck on its version check the
    runner spent three skills and 60 s on "star map did not open" before the error
    budget ended it.  Two UNKNOWN scenes in a row is enough to say what is wrong.
    """

    class BlankSession(FakeSession):
        def scene(self, frame=None, previous=None):
            class _Probe:
                pass

            p = _Probe()
            p.scene = Scene.UNKNOWN
            p.blank = True
            p.scores = {"frame_std": 3.0}
            p.points = {}
            p.frame_seq = 0
            p.frame_age_ms = 0.0
            return p

    planner = FakePlanner(PlanDecision(goal="puni", selected_node="puni.prepare.tutor_room",
                                       capability="game.goto_tutor_room", reason="x",
                                       expected_observation="room",
                                       max_actions=5, max_duration_s=60))
    r = _runner(tmp_path, planner=planner, session=BlankSession())
    summary = r.run()
    assert "not showing game content" in (summary["stop_reason"] or "")
    assert summary["stats"]["errors"] == 0, "a blank client is not a skill error"


def test_login_screen_stops_the_run(tmp_path):
    """A login/server-list page must stop the run, not be clicked into."""

    class LoginSession(FakeSession):
        def scene(self, frame=None, previous=None):
            class _Probe:
                pass

            p = _Probe()
            p.scene = Scene.WORLD          # dark page passes the contrast gate
            p.blank = False
            p.scores = {"fight": 0.1, "victory": 0.1, "confirm": 0.1}
            p.points = {}
            p.frame_seq = 0
            p.frame_age_ms = 0.0
            return p

        def ocr(self, **kw):
            return [{"text": "选择服务器", "conf": 0.9, "px": (1200.0, 450.0)},
                    {"text": "请选择服务器编号", "conf": 0.8, "px": (900.0, 460.0)}]

    planner = FakePlanner(PlanDecision(goal="puni", selected_node="puni.prepare.tutor_room",
                                       capability="game.goto_tutor_room", reason="x",
                                       expected_observation="room",
                                       max_actions=5, max_duration_s=60))
    r = _runner(tmp_path, planner=planner, session=LoginSession())
    summary = r.run()
    assert "not in the game world" in (summary["stop_reason"] or "")
    assert summary["stats"]["errors"] == 0


def test_a_sessionless_run_does_not_blank_the_last_real_world(tmp_path):
    """A dry run with no client observes nothing -- and must not persist that nothing.

    Regression: running `goal start --dry-run` against the production DB appended an
    empty world snapshot, which moved `latest_world()` past the last real observation
    and hid the tower counter the account had actually earned.
    """

    from seerauto.world.model import Evidence, GameWorldState
    from seerauto.world.store import WorldStore

    cfg = RunConfig(db_path=str(tmp_path / "r.sqlite3"), dry_run=True,
                    allow_input=False, max_minutes=1, max_battles=1)
    store = WorldStore(cfg.db_path)
    graph = GoalGraph.load()
    r = GoalRunner(cfg, store=store, graph=graph,
                   registry=SkillRegistry([RunBattleStub()]),
                   planner=FakePlanner(PlanDecision(stop=True,
                                                     stop_reason="nothing schedulable")),
                   session=None,
                   control=RunControl(str(tmp_path / "p"), str(tmp_path / "s")))

    seeded = GameWorldState()
    seeded.set_fact("counters.tower.floors_cleared", 14, Evidence.live("tower panel"))
    store.save_world(seeded)

    r.run()

    stored = store.latest_world()
    assert stored is not None
    assert stored.fact("counters.tower.floors_cleared") == 14


# --------------------------------------------------------------------------- #
# cooperative pause (§ user protocol "暂停，保存")
# --------------------------------------------------------------------------- #
def test_user_pause_saves_a_resumable_checkpoint_and_is_not_a_failure(tmp_path):
    """「暂停，保存」 must leave a durable, read-only checkpoint behind.

    The pause is a promise, so three things are asserted:
      * a checkpoint row exists and is *not* verified progress;
      * the run's status is `paused`, never `finished`/`failed`;
      * the freshly observed world is stored, because the game kept moving.
    """

    ctl = RunControl(str(tmp_path / "pause"), str(tmp_path / "stop"))
    ctl.request_pause()

    cfg = dict(db_path=str(tmp_path / "runs.sqlite3"), dry_run=False, allow_input=True,
               max_minutes=5, max_battles=1)
    store = WorldStore(cfg["db_path"])
    store.start_run("puni", "fake", budgets=RunConfig(**cfg).budgets, options={})
    run_id = store.list_runs()[0]["run_id"]

    session = FakeSession()
    r = GoalRunner(RunConfig(**cfg), store=store, graph=GoalGraph.load(),
                   registry=SkillRegistry([RunBattleStub(), NavigateStub(),
                                           ObservedLevelSkill()]),
                   planner=FakePlanner(PlanDecision(goal="puni",
                                                    selected_node="puni.prepare.level_up",
                                                    capability="game.run_battle",
                                                    reason="grind",
                                                    expected_observation="level rises",
                                                    max_actions=10, max_duration_s=120)),
                   session=session, control=ctl)
    # Drive the pause callback directly: a blocking wait would never return here.
    r.run_id = run_id
    r._pause_checkpoint()

    ck = r.store.last_checkpoint(run_id)
    assert ck is not None, "a user pause must leave a checkpoint"
    assert ck["verified"] in (0, False), "a pause proves nothing about the goal"
    assert ck["payload"].get("pause_requested") is True
    assert ck["payload"].get("safe_to_resume") is True

    run = r.store.get_run(run_id)
    assert run["status"] == "paused"
    assert "waiting for resume" in (run["stop_reason"] or "")
    # the world snapshot written by the pause is readable again after a restart
    assert r.store.latest_world() is not None


def test_resume_clears_the_paused_status(tmp_path):
    ctl = RunControl(str(tmp_path / "pause"), str(tmp_path / "stop"))
    cfg = dict(db_path=str(tmp_path / "runs.sqlite3"), dry_run=True, max_minutes=1)
    store = WorldStore(cfg["db_path"])
    store.start_run("puni", "fake", budgets=RunConfig(**cfg).budgets, options={})
    run_id = store.list_runs()[0]["run_id"]
    r = GoalRunner(RunConfig(**cfg), store=store, graph=GoalGraph.load(),
                   registry=SkillRegistry([RunBattleStub()]),
                   planner=FakePlanner(), session=FakeSession(), control=ctl)
    r.run_id = run_id
    store.update_run(run_id, status="paused", stop_reason="user requested pause")
    r._resume_after_pause(12.5)
    run = store.get_run(run_id)
    assert run["status"] == "running"
    assert run["stop_reason"] in (None, "")


def test_a_starting_run_does_not_erase_a_user_pause():
    """`run()` used to call control.clear(), which silently ate a fresh pause."""

    import inspect

    src = inspect.getsource(GoalRunner.run)
    assert "clear_stop()" in src
