"""Goal-graph tests.

These are the tests that keep the planner honest:

* the YAML really contains the 42 nodes of ``docs/PUNI_TASK_GRAPH.md`` and it is
  a DAG (a wrong edge turns "blocked by a gate" into "ready to run");
* an unknown predicate is a load-time error, not a silent ``False``;
* ``ready()`` only ever returns ``executable`` nodes whose capabilities and
  prerequisites hold;
* a HISTORICAL fact (a sentence copied out of a document) can never complete a
  node and never counts as ``False`` either.

No game, no images, no cv2 needed.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from seerauto.goals import GoalGraph, GoalGraphError
from seerauto.goals.graph import DEFAULT_PATH, PHASES, STATUSES
from seerauto.goals.predicates import (
    PREDICATES,
    STATE_PREDICATES,
    GraphError,
    evaluate,
)
from seerauto.world.model import (
    Evidence,
    Fact,
    GameWorldState,
    PetState,
    ResourceState,
    Scene,
    Source,
    SptEntry,
    empty_world,
)

PUNI_YAML = DEFAULT_PATH


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def live(origin: str = "test") -> Evidence:
    return Evidence(Source.LIVE_VERIFIED, origin=origin)


def historical(origin: str = "docs") -> Evidence:
    return Evidence(Source.HISTORICAL, origin=origin)


def observed_world() -> GameWorldState:
    """The world as Round 4 actually left it: logged in, in a reachable scene.

    No pet level, no resources, no SPT rows — exactly the interesting case,
    because every predicate must say "cannot decide" rather than invent values.
    """

    world = empty_world(run_id="test")
    world.set_fact("session.logged_in", True, live("login flow"))
    world.set_fact("session.scene", Scene.WORLD.value, live("scene template"))
    world.set_fact("session.in_battle", False, live("fight button absent"))
    return world


def world_with_active_pet(level: int = 15, source: Source = Source.LIVE_VERIFIED,
                          name: str = "烈火猴") -> GameWorldState:
    world = observed_world()
    world.pets[name] = PetState(name=name, level=level,
                                evidence=Evidence(source, origin="battle OCR"))
    world.active_pet = name
    return world


def raw_yaml() -> dict:
    with PUNI_YAML.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_variant(tmp_path: Path, mutate) -> GoalGraph:
    """Write a deliberately broken copy of the real graph and return the error."""

    raw = copy.deepcopy(raw_yaml())
    mutate(raw)
    path = tmp_path / "puni_broken.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return GoalGraph.load(path)


def node_entry(raw: dict, node_id: str) -> dict:
    for entry in raw["nodes"]:
        if entry["id"] == node_id:
            return entry
    raise AssertionError(f"{node_id} missing from the YAML")


@pytest.fixture(scope="module")
def graph() -> GoalGraph:
    return GoalGraph.load()


# --------------------------------------------------------------------------- #
# the file itself
# --------------------------------------------------------------------------- #
def test_default_path_and_load(graph: GoalGraph) -> None:
    assert PUNI_YAML.name == "puni.yaml"
    assert graph.path == DEFAULT_PATH
    assert graph.goal == "puni"
    assert graph.title == "获取谱尼"
    assert graph.version == 1
    assert graph.source == "docs/PUNI_TASK_GRAPH.md"


def test_at_least_42_nodes_and_every_legacy_id(graph: GoalGraph) -> None:
    assert len(graph) >= 42
    expected = {f"P{phase}-{n:02d}" for phase, count in
                (("0", 7), ("1", 15), ("2", 9), ("3", 7), ("4", 4))
                for n in range(1, count + 1)}
    # Round 5 added one derived node (P4-05, the 辅导员 training room the 8x-XP
    # toggle lives in).  Every node still carries a unique source-style id, and
    # the 42 original ids must all be present.
    legacy = {node.legacy_id for node in graph}
    assert expected <= legacy
    assert len(legacy) == len(graph)
    for node in graph:
        doc_phase = node.legacy_id.split("-")[0][1:]
        if node.id == "puni.prepare.level_up":
            # Deliberate re-grouping, documented in the YAML: the level-up node
            # keeps its source id P1-01 but is planned as a P0 prerequisite.
            assert node.phase == "P0" and doc_phase == "1"
        elif node.id == "puni.prepare.tutor_room":
            # Derived node (Round 5): new P4-05 id, scheduled as P0 because it is
            # a prerequisite of the level-up grind.
            assert node.phase == "P0" and doc_phase == "4"
        else:
            assert node.phase == f"P{doc_phase}", node.id


def test_reference_node_matches_the_spec_example(graph: GoalGraph) -> None:
    """The one node the task specification spells out in full."""

    node = graph.node("puni.prepare.level_up")
    assert node.legacy_id == "P1-01"
    assert node.phase == "P0"
    assert node.priority == 50
    assert node.executable is True
    assert node.status in {"unverified", "todo"}
    assert node.prerequisites == ("capability.battle.repeat", "state.active_pet_level_known")
    assert node.requires_capabilities == ("battle.repeat",)
    assert node.completion.type == "verified_account_state"
    assert node.completion.predicate == "pet_level_at_least"
    assert node.completion.args["pet"] == "烈火猴"
    assert node.completion.args["level"] == 36
    assert node.skill == "game.run_battle"
    assert node.skill_inputs == {"battles": 6}
    assert node.budget == {"max_minutes": 45, "max_battles": 15, "max_spend": 0}
    assert node.on_failure == "pause_and_replan"
    assert node.blocked_reason == ""
    assert node.evidence


def test_ids_are_unique_in_yaml_and_graph(graph: GoalGraph) -> None:
    raw_ids = [entry["id"] for entry in raw_yaml()["nodes"]]
    assert len(raw_ids) == len(set(raw_ids))
    assert set(raw_ids) == set(graph.nodes)
    assert len({node.legacy_id for node in graph}) == len(raw_ids)


def test_every_phase_present_and_counts(graph: GoalGraph) -> None:
    phases = graph.by_phase()
    assert set(phases) == set(PHASES)
    assert sum(len(v) for v in phases.values()) == len(graph)
    assert all(phases[p] for p in PHASES)


def test_every_node_is_complete(graph: GoalGraph) -> None:
    for node in graph:
        assert node.completion.predicate in PREDICATES
        assert node.completion.type in {"verified_account_state", "capability_demonstrated"}
        assert set(node.budget) >= {"max_minutes", "max_battles", "max_spend"}
        assert all(isinstance(node.budget[k], (int, float))
                   and not isinstance(node.budget[k], bool) for k in node.budget)
        assert node.on_failure in {"pause_and_replan", "retry", "mark_blocked"}
        assert node.status in STATUSES
        assert node.evidence, node.id
        assert node.notes.strip(), node.id
        if not node.executable:
            assert node.blocked_reason.strip(), node.id


def test_capability_registry(graph: GoalGraph) -> None:
    assert len(graph.capabilities) >= 20
    for name, cap in graph.capabilities.items():
        assert cap["verified"] in {"real", "replay", "unverified"}, name
        assert str(cap["summary"]).strip(), name
        assert cap["evidence"], name
    real = graph.available_capabilities()
    # Round-4 facts, not the Round-2 baseline: these are the only ones we may rely on.
    assert {"battle.repeat", "observe.ocr", "observe.template", "input.click",
            "spt.panel", "budget.stop"} <= real
    assert "battle.skill_select" not in real
    assert "battle.log_read" not in real
    assert "market.buy" not in real


# --------------------------------------------------------------------------- #
# structure: DAG + topological order
# --------------------------------------------------------------------------- #
def test_build_order_is_a_valid_topological_order(graph: GoalGraph) -> None:
    order = graph.build_order()
    assert len(order) == len(graph)
    assert set(order) == set(graph.nodes)
    position = {node_id: i for i, node_id in enumerate(order)}
    for node in graph:
        for dep in node.node_prerequisites:
            assert dep in graph.nodes
            assert position[dep] < position[node.id], f"{dep} must precede {node.id}"


def test_build_order_is_deterministic(graph: GoalGraph) -> None:
    assert graph.build_order() == graph.build_order()


def test_dependents(graph: GoalGraph) -> None:
    assert graph.dependents("puni.prepare.market_unlock") == [
        "puni.roster.buy_flash_pipi",
        "puni.roster.buy_kaolu",
        "puni.roster.buy_nier",
        "puni.roster.buy_salake",
    ]
    assert graph.dependents("puni.truebody.life_6_final") == []
    with pytest.raises(GoalGraphError):
        graph.dependents("puni.nope.nothing")


def test_node_lookup_by_id_and_legacy_hint(graph: GoalGraph) -> None:
    assert graph.node("puni.prepare.level_up").legacy_id == "P1-01"
    with pytest.raises(GoalGraphError) as excinfo:
        graph.node("P1-01")
    assert "legacy id" in str(excinfo.value)


def test_cycle_is_rejected(tmp_path: Path) -> None:
    def mutate(raw: dict) -> None:
        node_entry(raw, "puni.prepare.market_unlock")["prerequisites"] = [
            "puni.prepare.level_up"]
        node_entry(raw, "puni.prepare.level_up")["prerequisites"].append(
            "puni.prepare.market_unlock")

    with pytest.raises(GoalGraphError) as excinfo:
        load_variant(tmp_path, mutate)
    assert "cyclic" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "mutate, needle",
    [
        pytest.param(
            lambda raw: raw["nodes"].append(copy.deepcopy(node_entry(raw, "puni.prepare.daily_signin"))),
            "duplicate node id", id="duplicate-id"),
        pytest.param(
            lambda raw: node_entry(raw, "puni.prepare.daily_signin").update(
                legacy_id="P0-03"), "duplicate legacy_id", id="duplicate-legacy-id"),
        pytest.param(
            lambda raw: node_entry(raw, "puni.prepare.level_up")["completion"].update(
                predicate="llm_says_so"), "not registered", id="unknown-predicate"),
        pytest.param(
            lambda raw: node_entry(raw, "puni.prepare.level_up")["prerequisites"].append(
                "puni.nope.nothing"), "neither a node id", id="unknown-prerequisite"),
        pytest.param(
            lambda raw: node_entry(raw, "puni.prepare.level_up")["prerequisites"].append(
                "state.does_not_exist"), "not registered", id="unknown-state-predicate"),
        pytest.param(
            lambda raw: node_entry(raw, "puni.prepare.daily_signin").pop("blocked_reason"),
            "blocked_reason", id="missing-blocked-reason"),
        pytest.param(
            lambda raw: node_entry(raw, "puni.prepare.level_up")["completion"].update(
                type="capability_demonstrated"), "only 'verified_account_state'",
            id="executable-wrong-completion-type"),
        pytest.param(
            lambda raw: node_entry(raw, "puni.prepare.level_up")["budget"].pop("max_spend"),
            "max_spend", id="budget-missing-key"),
        pytest.param(
            lambda raw: node_entry(raw, "puni.prepare.level_up")["budget"].update(
                max_minutes="soon"), "must be numeric", id="budget-not-numeric"),
        pytest.param(
            lambda raw: node_entry(raw, "puni.prepare.level_up").update(status="blocked"),
            "display-only", id="executable-but-blocked"),
        pytest.param(
            lambda raw: node_entry(raw, "puni.prepare.level_up")["requires_capabilities"].append(
                "battle.teleport"), "unknown capability", id="unknown-capability"),
        pytest.param(
            lambda raw: node_entry(raw, "puni.prepare.level_up")["completion"].update(
                predicate="node_completed_before"), "no-op stub", id="completed-before-stub"),
        pytest.param(
            lambda raw: (node_entry(raw, "puni.prepare.level_up").pop("skill"),
                         node_entry(raw, "puni.prepare.level_up").pop("skill_inputs")),
            "must name the skill", id="executable-without-skill"),
    ],
)
def test_invalid_graphs_raise_with_precise_message(tmp_path: Path, mutate, needle: str) -> None:
    with pytest.raises(GoalGraphError) as excinfo:
        load_variant(tmp_path, mutate)
    message = str(excinfo.value)
    assert needle in message
    assert "puni.prepare" in message  # names the offending node


def test_goal_graph_error_is_a_value_error() -> None:
    assert issubclass(GoalGraphError, ValueError)
    assert issubclass(GraphError, RuntimeError)


# --------------------------------------------------------------------------- #
# ready() — only executable nodes, prerequisites + capabilities honoured
# --------------------------------------------------------------------------- #
def test_ready_is_empty_without_a_known_pet_level(graph: GoalGraph) -> None:
    assert graph.ready(world=observed_world(), completed=set(),
                       capabilities={"battle.repeat"}) == []


def test_ready_returns_only_the_executable_node(graph: GoalGraph) -> None:
    world = world_with_active_pet(level=15)
    ready = graph.ready(world=world, completed=set(), capabilities={"battle.repeat"})
    assert [node.id for node in ready] == ["puni.prepare.level_up"]
    assert ready[0].executable is True


def test_ready_skips_everything_not_executable(graph: GoalGraph) -> None:
    """Even with every capability (verified or not) and a fully-known world,
    the display-only nodes never become ready."""

    world = world_with_active_pet(level=15)
    everything = set(graph.capabilities)
    ready = graph.ready(world=world, completed=set(), capabilities=everything)
    ids = [node.id for node in ready]
    # Round 5 added a second executable node (the Puni entrance navigation, which
    # needs no battle at all); the invariant under test is "display-only nodes
    # never appear", not a frozen list.
    assert "puni.prepare.level_up" in ids
    assert "puni.seal.entry_nav" in ids
    assert all(node.executable for node in ready)


def test_ready_respects_missing_capability(graph: GoalGraph) -> None:
    world = world_with_active_pet(level=15)
    assert graph.ready(world=world, completed=set(), capabilities=set()) == []


def test_ready_respects_completed_set(graph: GoalGraph) -> None:
    world = world_with_active_pet(level=15)
    ready = graph.ready(world=world, completed={"puni.prepare.level_up"},
                        capabilities={"battle.repeat"})
    assert ready == []


def test_ready_skips_nodes_already_true_in_the_world(graph: GoalGraph) -> None:
    world = world_with_active_pet(level=40)
    ready = graph.ready(world=world, completed=set(), capabilities={"battle.repeat"})
    assert ready == []
    assert graph.status_of("puni.prepare.level_up", world=world,
                           completed=set()) == "completed"


def test_ready_sorted_by_priority_desc(graph: GoalGraph) -> None:
    world = world_with_active_pet(level=15)
    everything = set(graph.capabilities)
    ready = graph.ready(world=world, completed=set(), capabilities=everything)
    priorities = [node.priority for node in ready]
    assert priorities == sorted(priorities, reverse=True)


# --------------------------------------------------------------------------- #
# status_of — the six distinguishable answers
# --------------------------------------------------------------------------- #
def test_status_of_completed_vs_ready_vs_missing(graph: GoalGraph) -> None:
    level_up = "puni.prepare.level_up"
    # bookkeeping says done
    assert graph.status_of(level_up, world=observed_world(),
                           completed={level_up}) == "completed"
    # world says done (historical world would not)
    assert graph.status_of(level_up, world=world_with_active_pet(level=36),
                           completed=set()) == "completed"
    # ready
    assert graph.status_of(level_up, world=world_with_active_pet(level=15),
                           completed=set(), capabilities={"battle.repeat"}) == "ready"
    # missing capability
    assert graph.status_of(level_up, world=world_with_active_pet(level=15),
                           completed=set(), capabilities=set()) == "missing_capability"
    # missing prerequisite (pet level never observed)
    assert graph.status_of(level_up, world=observed_world(),
                           completed=set(), capabilities={"battle.repeat"}) == "missing_prereq"


def test_status_of_not_executable_blocked_unknown(graph: GoalGraph) -> None:
    world = world_with_active_pet(level=15)
    caps = set(graph.capabilities)
    assert graph.status_of("puni.prepare.daily_signin", world=world,
                           completed=set(), capabilities=caps) == "not_executable"
    assert graph.status_of("puni.roster.buy_salake", world=world,
                           completed=set(), capabilities=caps) == "blocked"
    assert graph.status_of("puni.prepare.market_unlock", world=world,
                           completed=set(), capabilities=caps) == "unknown"


def test_visible_skips_are_still_queryable(graph: GoalGraph) -> None:
    """A display-only node with no prerequisites must not leak into ready()."""

    node = graph.node("puni.seal.boss_pp_empty")
    assert node.prerequisites == ()
    assert node.executable is False
    assert node.blocked_reason
    world = world_with_active_pet(level=15)
    assert node.id not in [n.id for n in
                           graph.ready(world=world, completed=set(),
                                       capabilities=set(graph.capabilities))]


def test_unmet_reasons_are_human_readable(graph: GoalGraph) -> None:
    world = observed_world()
    reasons = graph.unmet("puni.prepare.level_up", world=world, completed=set(),
                          capabilities={"battle.repeat"})
    assert any("state prerequisite" in r and "active_pet_level_known" in r for r in reasons)
    assert len(reasons) == 1

    reasons = graph.unmet("puni.prepare.level_up", world=world, completed=set(),
                          capabilities=set())
    assert any(r.startswith("missing capability 'battle.repeat'") for r in reasons)

    reasons = graph.unmet("puni.roster.buy_salake", world=world, completed=set(),
                          capabilities=set())
    joined = " | ".join(reasons)
    assert "puni.prepare.market_unlock" in joined
    assert "market.buy" in joined


def test_unmet_includes_unfinished_prerequisite_nodes(graph: GoalGraph) -> None:
    world = world_with_active_pet(level=40)
    reasons = graph.unmet("puni.truebody.enter", world=world, completed=set(),
                          capabilities=set(graph.capabilities))
    joined = " | ".join(reasons)
    assert "puni.seal.holy_7" in joined
    assert "not completed" in joined


# --------------------------------------------------------------------------- #
# predicate truth table
# --------------------------------------------------------------------------- #
def test_pet_level_truth_table() -> None:
    need = {"pet": "烈火猴", "level": 36}

    absent = observed_world()
    holds, why = evaluate("pet_level_at_least", absent, need)
    assert holds is None and "not observed" in why

    histor = world_with_active_pet(level=15, source=Source.HISTORICAL)
    holds, why = evaluate("pet_level_at_least", histor, need)
    assert holds is None
    assert why == "pet 烈火猴 level UNKNOWN (HISTORICAL evidence only)"

    now = world_with_active_pet(level=15)
    holds, why = evaluate("pet_level_at_least", now, need)
    assert holds is False and "(LIVE_VERIFIED)" in why

    high = world_with_active_pet(level=40)
    holds, why = evaluate("pet_level_at_least", high, need)
    assert holds is True and "40" in why

    # aliases: evolution renames the pet, the level goal must survive that
    renamed = world_with_active_pet(level=40, name="烈焰猩猩")
    renamed.active_pet = "烈焰猩猩"
    holds, _ = evaluate("pet_level_at_least", renamed,
                        {"pet": "烈火猴", "level": 36, "aliases": ["烈焰猩猩"]})
    assert holds is True


def test_pet_exists_does_not_treat_absence_as_proof() -> None:
    holds, why = evaluate("pet_exists", observed_world(), {"pet": "萨帕克"})
    assert holds is None and "not observed" in why
    holds, _ = evaluate("pet_exists", world_with_active_pet(source=Source.HISTORICAL),
                        {"pet": "烈火猴"})
    assert holds is None
    holds, _ = evaluate("pet_exists", world_with_active_pet(), {"pet": "烈火猴"})
    assert holds is True


def _spt_world(rows: dict[str, bool | None]) -> GameWorldState:
    world = observed_world()
    for name, defeated in rows.items():
        world.spt[name] = SptEntry(boss_id=name, name=name, defeated=defeated,
                                   evidence=live("SPT panel OCR"))
    return world


def test_spt_predicates() -> None:
    world = _spt_world({"蘑菇怪": True, "雷伊": None})
    assert evaluate("spt_defeated", world, {"boss": "蘑菇怪"})[0] is True
    assert evaluate("spt_defeated", world, {"boss": "雷伊"})[0] is None
    holds, why = evaluate("spt_defeated", world, {"boss": "哈莫雷特"})
    assert holds is None and "not observed" in why

    # 1 defeated + 1 undecided, 2 needed -> cannot decide
    assert evaluate("spt_defeated_count_at_least", world, {"count": 2})[0] is None
    assert evaluate("spt_defeated_count_at_least", world, {"count": 1})[0] is True
    assert evaluate("spt_defeated_count_at_least", world, {"count": 3})[0] is False

    histor = observed_world()
    histor.spt["蘑菇怪"] = SptEntry(boss_id="蘑菇怪", name="蘑菇怪", defeated=True,
                                    evidence=historical())
    holds, why = evaluate("spt_defeated", histor, {"boss": "蘑菇怪"})
    assert holds is None and "HISTORICAL" in why


def test_resource_predicate_and_stale_currency() -> None:
    world = observed_world()
    # docs §0.1 says 赛尔豆 0 — that is HISTORICAL, so it may not answer "can we afford it?"
    world.resources["赛尔豆"] = ResourceState(name="赛尔豆", amount=0,
                                              evidence=historical())
    holds, why = evaluate("resource_at_least", world, {"resource": "赛尔豆", "amount": 1})
    assert holds is None and "HISTORICAL" in why

    holds, why = evaluate("resource_at_least", world, {"resource": "金豆", "amount": 1})
    assert holds is None and "not observed" in why

    world.resources["赛尔豆"] = ResourceState(name="赛尔豆", amount=56000,
                                              evidence=live("currency bar OCR"))
    assert evaluate("resource_at_least", world, {"resource": "赛尔豆", "amount": 56000})[0] is True
    assert evaluate("resource_at_least", world, {"resource": "赛尔豆", "amount": 60000})[0] is False


def test_feature_and_counter_predicates() -> None:
    world = observed_world()
    holds, why = evaluate("feature_unlocked", world, {"feature": "market"})
    assert holds is None and "never observed" in why

    world.unlocked["market"] = Fact(False, live("market panel closed"))
    assert evaluate("feature_unlocked", world, {"feature": "market"})[0] is False

    world.unlocked["market"] = Fact(True, historical())
    assert evaluate("feature_unlocked", world, {"feature": "market"})[0] is None

    world.unlocked["market"] = Fact(True, live("market panel opened"))
    assert evaluate("feature_unlocked", world, {"feature": "market"})[0] is True

    assert evaluate("counter_at_least", world, {"counter": "tower.floors_cleared",
                                                "at_least": 1})[0] is None
    world.facts["counters.tower.floors_cleared"] = Fact(2, live("tower run"))
    assert evaluate("counter_at_least", world,
                    {"counter": "tower.floors_cleared", "at_least": 1})[0] is True
    assert evaluate("counter_at_least", world,
                    {"counter": "tower.floors_cleared", "at_least": 3})[0] is False


def test_capability_demonstrated_requires_a_recorded_run() -> None:
    world = observed_world()
    assert evaluate("capability_demonstrated", world,
                    {"capability": "battle.log_read"})[0] is None
    world.observations["capabilities"] = {
        "battle.log_read": {"demonstrated": True, "source": "HISTORICAL"},
    }
    assert evaluate("capability_demonstrated", world,
                    {"capability": "battle.log_read"})[0] is None
    world.observations["capabilities"]["battle.log_read"] = {
        "demonstrated": True, "source": "LIVE_VERIFIED", "ref": "artifacts/log.png"}
    holds, why = evaluate("capability_demonstrated", world,
                          {"capability": "battle.log_read"})
    assert holds is True and "artifacts/log.png" in why


def test_capability_nodes_never_auto_complete(graph: GoalGraph) -> None:
    """``capability_demonstrated`` is deliberately not auto-completing."""

    node = graph.node("puni.seal.boss_pp_empty")
    assert node.completion.type == "capability_demonstrated"
    world = world_with_active_pet(level=15)
    world.observations["capabilities"] = {
        "battle.log_read": {"demonstrated": True, "source": "LIVE_VERIFIED"}}
    assert graph.status_of(node.id, world=world, completed=set()) == "not_executable"
    # a human/tool may still record it
    assert graph.status_of(node.id, world=world, completed={node.id}) == "completed"


def test_node_completed_before_raises() -> None:
    with pytest.raises(GraphError) as excinfo:
        evaluate("node_completed_before", observed_world(), {"node": "puni.prepare.level_up"})
    assert "no-op stub" in str(excinfo.value)


def test_unknown_predicate_raises() -> None:
    with pytest.raises(GraphError) as excinfo:
        evaluate("llm_says_so", observed_world(), {})
    assert "unknown predicate" in str(excinfo.value)
    with pytest.raises(GraphError):
        evaluate("", observed_world(), {})


def test_evaluate_enforces_the_result_contract(monkeypatch) -> None:
    monkeypatch.setitem(PREDICATES, "returns_bool_only", lambda world, args: True)
    with pytest.raises(GraphError) as excinfo:
        evaluate("returns_bool_only", observed_world(), {})
    assert "expected (bool | None, str)" in str(excinfo.value)

    monkeypatch.setitem(PREDICATES, "returns_string", lambda world, args: ("yes", "why"))
    with pytest.raises(GraphError):
        evaluate("returns_string", observed_world(), {})

    with pytest.raises(GraphError):
        evaluate("pet_level_at_least", observed_world(), {})  # missing arg


def test_state_predicates_are_arg_aware() -> None:
    world = observed_world()
    assert evaluate("state.logged_in", world, {})[0] is True
    assert evaluate("state.scene_is", world, {})[0] is True
    holds, why = evaluate("state.scene_is", world, {"scene": Scene.BATTLE.value})
    assert holds is False and "WORLD" in why
    holds, _ = evaluate("state.scene_is", world, {"scene": Scene.WORLD.value})
    assert holds is True

    empty = empty_world()
    assert evaluate("state.logged_in", empty, {})[0] is None
    assert evaluate("state.scene_is", empty, {})[0] is None
    assert evaluate("state.active_pet_level_known", empty, {})[0] is None
    assert evaluate("state.spt_panel_open", empty, {})[0] is None
    assert evaluate("state.battle_capable", empty, {})[0] is None

    battle = observed_world()
    battle.set_fact("session.in_battle", True, live("fight button"))
    battle.set_fact("session.scene", Scene.BATTLE.value, live("fight button"))
    assert evaluate("state.battle_capable", battle, {})[0] is True
    battle.facts["panel.spt_open"] = Fact(True, live("SPT panel"))
    assert evaluate("state.spt_panel_open", battle, {})[0] is True


def test_historical_cannot_complete_a_node(graph: GoalGraph) -> None:
    """The core rule: a document saying "level 15" is not today's level."""

    world = observed_world()
    world.pets["烈火猴"] = PetState(name="烈火猴", level=40,
                                    evidence=historical("docs/state.md"))
    world.active_pet = "烈火猴"
    # state prerequisite *and* completion both refuse historical evidence
    assert graph.status_of("puni.prepare.level_up", world=world, completed=set(),
                           capabilities={"battle.repeat"}) == "missing_prereq"
    assert graph.ready(world=world, completed=set(),
                       capabilities={"battle.repeat"}) == []


def test_unknown_is_not_false() -> None:
    empty = empty_world()
    for predicate, args in (
        ("pet_level_at_least", {"pet": "烈火猴", "level": 36}),
        ("pet_exists", {"pet": "萨帕克"}),
        ("spt_defeated", {"boss": "蘑菇怪"}),
        ("resource_at_least", {"resource": "赛尔豆", "amount": 1}),
        ("feature_unlocked", {"feature": "market"}),
        ("counter_at_least", {"counter": "tower.floors_cleared", "at_least": 1}),
    ):
        holds, why = evaluate(predicate, empty, args)
        assert holds is None, predicate
        assert why

    # ... and the graph reports "missing prerequisite", never "cannot happen"
    graph = GoalGraph.load()
    status = graph.status_of("puni.prepare.level_up", world=empty,
                             completed=set(), capabilities={"battle.repeat"})
    assert status == "missing_prereq"


# --------------------------------------------------------------------------- #
# serialisation
# --------------------------------------------------------------------------- #
def test_to_dict_round_trip_shape(graph: GoalGraph) -> None:
    payload = graph.to_dict()
    assert payload["goal"] == "puni"
    assert payload["version"] == 1
    assert len(payload["nodes"]) == len(graph)
    required = {"id", "legacy_id", "title", "description", "phase", "priority",
                "prerequisites", "requires_capabilities", "completion", "status",
                "executable", "blocked_reason", "skill", "skill_inputs", "budget",
                "on_failure", "evidence", "notes"}
    for entry in payload["nodes"]:
        assert required <= set(entry)
        assert entry["completion"]["predicate"] in PREDICATES
        assert set(entry["budget"]) >= {"max_minutes", "max_battles", "max_spend"}


def test_only_verified_account_state_may_auto_complete(graph: GoalGraph) -> None:
    for node in graph:
        if node.executable:
            assert node.completion.type == "verified_account_state"
        if node.completion.type != "verified_account_state":
            assert not node.executable
