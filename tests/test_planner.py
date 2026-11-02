"""Planner-layer tests: contract, deterministic policy, bridge rendezvous, AI transport.

Design of these tests:

* no game, no screencapture, no network - the AI transport is exercised by
  monkeypatching ``urllib.request.urlopen`` (or injecting a fake opener);
* the goal graph / world store are *not* imported: the planner is duck-typed
  against the digest/summary shapes, exactly like the runner will call it;
* every failure path asserts a *precise* message, because a planner that fails
  vaguely is a planner the runner cannot degrade from.

Node ids follow the contract example (``puni.<area>.<step>``) and the areas of
``docs/PUNI_TASK_GRAPH.md`` section 2; capability ids follow the ``game.*``
semantic interface from ``AGENTS.md`` section 7.
"""

from __future__ import annotations

import io
import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from seerauto.planner import (
    AIConfig,
    BridgePlanner,
    BridgeWatcher,
    CallMeta,
    DeterministicPlanner,
    OpenAICompatPlanner,
    PlanDecision,
    PlanError,
    PlanRequest,
    Planner,
    PlannerError,
    PlannerUnavailable,
    build_planner,
    format_prompt,
    next_seq,
    parse_decision,
    pending,
    request_name,
    response_name,
)
from seerauto.planner import base as plan_base
from seerauto.planner import bridge as bridge_module
from seerauto.planner import openai_compat


# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #
CAPABILITIES = [
    {
        "id": "game.observe_pet",
        "kind": "observe",
        "summary": "read the pet panel and record level/exp",
        "inputs": {},
        "outputs": {"level": "int"},
        "preconditions": ["session.scene is WORLD or PANEL"],
        "postconditions": ["pets.<name>.level known"],
        "max_duration_s": 90,
        "max_actions": 0,
        "requires_input": True,
        "requires_approval": False,
        "verified": "real",
        "evidence": ["docs/state.md"],
    },
    {
        "id": "game.run_battle",
        "kind": "act",
        "summary": "clear one battle with the default attack loop",
        "inputs": {"battles": {"type": "int", "required": False}},
        "outputs": {"wins": "int"},
        "preconditions": ["session.scene is WORLD"],
        "postconditions": [],
        "max_duration_s": 1800,
        "max_actions": 40,
        "requires_input": True,
        "requires_approval": False,
        "verified": "real",
        "evidence": ["docs/PROGRESS.md"],
    },
    {
        "id": "game.market_buy",
        "kind": "act",
        "summary": "buy one item from the 交易行",
        "inputs": {"name": {"type": "str", "required": True}},
        "outputs": {"bought": "bool"},
        "preconditions": ["unlocked.交易行 is True"],
        "postconditions": [],
        "max_duration_s": 900,
        "max_actions": 30,
        "requires_input": True,
        "requires_approval": True,
        "verified": "unverified",
        "evidence": [],
    },
]


def node(node_id: str, status: str, priority: int, capability: str | None, **kw):
    out = {"id": node_id, "title": node_id.replace(".", " "), "status": status,
           "priority": priority, "capability": capability, "budget": {}, "blocked_reason": ""}
    out.update(kw)
    return out


def graph() -> list[dict]:
    return [
        node("puni.market.unlock", "blocked", 95, "game.market_buy",
             blocked_reason="交易行未开通 (0/3 tasks)"),
        node("puni.prepare.level_up", "ready", 80, "game.run_battle",
             budget={"max_actions": 60, "max_duration_s": 2400}),
        node("puni.observe.pet", "ready", 70, "game.observe_pet"),
        node("puni.spt.mushroom", "pending", 60, "game.run_battle",
             blocked_reason="requires puni.prepare.level_up"),
    ]


def world_summary(**overrides) -> dict:
    summary = {
        "captured_at": "2026-11-02T23:00:00+08:00",
        "scene": "WORLD",
        "scene_source": "LIVE_VERIFIED",
        "logged_in": True,
        "frontmost": True,
        "active_pet": {"name": "烈火猴", "level": None, "hp": 120, "hp_max": 120,
                       "skills": []},
        "pets": {"烈火猴": {"level": None, "source": "UNKNOWN"}},
        "resources": {"赛尔豆": 0},
        "spt_defeated": [],
        "spt_unknown": ["蘑菇怪"],
        "unlocked": {},
        "unknowns": ["pets.烈火猴.level"],
        "notes": [],
    }
    summary.update(overrides)
    return summary


def request(**overrides) -> PlanRequest:
    payload = {
        "goal": "puni",
        "world_summary": world_summary(),
        "graph_digest": graph(),
        "capabilities": CAPABILITIES,
        "completed_nodes": [],
        "recent_events": [{"kind": "run.start", "goal": "puni"}],
        "last_result": None,
        "budgets": {"max_actions": 120, "max_duration_s": 3600},
        "constraints": {"allow_input": True, "forbid_approval": False, "max_spend": 0},
        "run_id": "run-20261102-1",
    }
    payload.update(overrides)
    return PlanRequest(**payload)


def decision_json(**overrides) -> dict:
    """The exact contract object from the task brief."""

    payload = {
        "goal": "puni",
        "selected_node": "puni.prepare.level_up",
        "capability": "game.run_battle",
        "reason": "当前精灵等级未达到目标",
        "expected_observation": "目标精灵等级或经验增加",
        "max_actions": 40,
        "max_duration_s": 1800,
        "requires_approval": False,
    }
    payload.update(overrides)
    return payload


def stop_json(**overrides) -> dict:
    payload = {
        "goal": "puni",
        "selected_node": "",
        "capability": "",
        "reason": "nothing schedulable",
        "expected_observation": "no observation",
        "max_actions": 0,
        "max_duration_s": 0,
        "requires_approval": False,
        "stop": True,
        "stop_reason": "交易行未开通 and level unknown",
    }
    payload.update(overrides)
    return payload


def dump(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 1. contract: happy paths
# --------------------------------------------------------------------------- #
def test_parse_decision_accepts_the_contract_example():
    raw = dump(decision_json())
    parsed = parse_decision(raw)
    assert parsed.goal == "puni"
    assert parsed.selected_node == "puni.prepare.level_up"
    assert parsed.capability == "game.run_battle"
    assert parsed.reason == "当前精灵等级未达到目标"
    assert parsed.expected_observation == "目标精灵等级或经验增加"
    assert parsed.max_actions == 40
    assert parsed.max_duration_s == 1800
    assert parsed.requires_approval is False
    assert parsed.stop is False and parsed.need_human is False
    assert not parsed.is_refusal
    # the wire form is exactly the contract keys - nothing internal leaks
    assert set(parsed.to_dict()) == set(plan_base.REQUIRED_KEYS)


def test_parse_decision_tolerates_json_fences_and_prose():
    body = dump(decision_json())
    assert parse_decision(f"```json\n{body}\n```").selected_node == "puni.prepare.level_up"
    assert parse_decision(f"```\n{body}\n```").selected_node == "puni.prepare.level_up"
    chattery = f"Sure, here is the decision:\n{body}\nHope that helps!"
    assert parse_decision(chattery).capability == "game.run_battle"
    spaced = "\n\n  " + body + "  \n"
    assert parse_decision(spaced).goal == "puni"


def test_parse_decision_keeps_a_json_object_containing_braces_in_strings():
    body = dump(decision_json(reason='the world says {"scene":"WORLD"} so go'))
    assert "WORLD" in parse_decision(body).reason


def test_decision_round_trips_through_the_wire_form():
    first = parse_decision(dump(decision_json()))
    again = parse_decision(dump(first.to_dict()))
    assert again.to_dict() == first.to_dict()


def test_validate_accepts_known_node_and_capability():
    parsed = parse_decision(dump(decision_json()))
    returned = parsed.validate(
        known_nodes=graph(),
        known_capabilities=CAPABILITIES,
        budgets={"max_actions": 120, "max_duration_s": 3600},
    )
    assert returned is parsed


def test_validate_accepts_id_lists_and_mappings():
    parsed = parse_decision(dump(decision_json()))
    parsed.validate(
        known_nodes=["puni.prepare.level_up"],
        known_capabilities={"game.run_battle": {"id": "game.run_battle"}},
    )


def test_validate_skips_membership_when_no_reference_is_given():
    parse_decision(dump(decision_json())).validate()


def test_refusal_is_normalised_and_needs_no_work():
    parsed = parse_decision(dump(stop_json()))
    assert parsed.stop is True
    assert parsed.need_human is False
    assert parsed.selected_node == ""
    assert parsed.capability == ""
    assert parsed.max_actions == 0
    assert parsed.max_duration_s == 0
    assert parsed.stop_reason == "交易行未开通 and level unknown"
    parsed.validate(known_nodes=[], known_capabilities=[])


def test_need_human_implies_stop():
    parsed = parse_decision(dump({
        **stop_json(stop=False),
        "stop": False,
        "need_human": True,
        "human_question": "Which capability should we build first?",
    }))
    assert parsed.stop is True and parsed.need_human is True
    assert parsed.human_question == "Which capability should we build first?"


def test_refusal_with_a_named_node_is_normalised_not_rejected():
    parsed = parse_decision(dump(stop_json(
        selected_node="puni.prepare.level_up", capability="game.run_battle",
        max_actions=40, max_duration_s=1800, requires_approval=True)))
    assert parsed.selected_node == "" and parsed.capability == ""
    assert parsed.max_actions == 0 and parsed.requires_approval is False


def test_meta_is_attached_locally_and_never_parsed_from_the_model():
    parsed = parse_decision(dump(decision_json()))
    parsed.meta = CallMeta(model="deepseek-v4.1-flash", latency_ms=12,
                           prompt_chars=100, response_chars=20)
    assert "meta" not in parsed.to_dict()
    assert parsed.to_dict(with_meta=True)["meta"]["latency_ms"] == 12
    with pytest.raises(PlanError, match="unknown top-level key"):
        parse_decision(dump(decision_json(meta={"latency_ms": 1})))


# --------------------------------------------------------------------------- #
# 2. contract: sad paths
# --------------------------------------------------------------------------- #
def test_sad_path_missing_required_field():
    for key in plan_base.REQUIRED_KEYS:
        broken = {k: v for k, v in decision_json().items() if k != key}
        with pytest.raises(PlanError, match="missing required key"):
            parse_decision(dump(broken))


def test_sad_path_wrong_types():
    cases = {
        "max_actions": "40",
        "max_duration_s": "1800",
        "requires_approval": "false",
        "goal": 5,
        "reason": None,
        "expected_observation": "",
        "selected_node": 3,
        "capability": ["game.run_battle"],
    }
    for key, value in cases.items():
        with pytest.raises(PlanError):
            parse_decision(dump(decision_json(**{key: value})))
    # bool is an int subclass in Python; the contract must not accept it
    with pytest.raises(PlanError, match="max_actions"):
        parse_decision(dump(decision_json(max_actions=True)))


def test_sad_path_unknown_top_level_key():
    with pytest.raises(PlanError, match="unknown top-level key"):
        parse_decision(dump(decision_json(confidence=0.9)))
    with pytest.raises(PlanError, match="unknown top-level key"):
        parse_decision(dump(decision_json(image="frame.png")))


@pytest.mark.parametrize("field,value", [
    ("max_actions", 0), ("max_actions", 201), ("max_actions", -3),
    ("max_duration_s", 0), ("max_duration_s", 7201), ("max_duration_s", -1),
])
def test_sad_path_out_of_range_budgets(field, value):
    with pytest.raises(PlanError, match=field):
        parse_decision(dump(decision_json(**{field: value})))


def test_sad_path_unknown_node():
    parsed = parse_decision(dump(decision_json(selected_node="puni.does.not.exist")))
    with pytest.raises(PlanError, match="unknown node"):
        parsed.validate(known_nodes=graph(), known_capabilities=CAPABILITIES)


def test_sad_path_unknown_capability():
    parsed = parse_decision(dump(decision_json(capability="game.summon_dragon")))
    with pytest.raises(PlanError, match="unknown capability"):
        parsed.validate(known_nodes=graph(), known_capabilities=CAPABILITIES)


def test_sad_path_decision_exceeds_run_budget():
    parsed = parse_decision(dump(decision_json()))
    with pytest.raises(PlanError, match="exceeds the run budget"):
        parsed.validate(known_nodes=graph(), known_capabilities=CAPABILITIES,
                        budgets={"max_actions": 10})
    with pytest.raises(PlanError, match="exceeds the run budget"):
        parsed.validate(known_nodes=graph(), known_capabilities=CAPABILITIES,
                        budgets={"max_duration_s": 60})


def test_sad_path_stop_without_a_reason():
    with pytest.raises(PlanError, match="stop_reason"):
        parse_decision(dump(stop_json(stop_reason="")))


def test_sad_path_need_human_without_a_question():
    with pytest.raises(PlanError, match="human_question"):
        parse_decision(dump({**stop_json(), "need_human": True, "human_question": ""}))


def test_sad_path_stop_reason_without_stop():
    broken = decision_json(stop_reason="but I am not stopping")
    with pytest.raises(PlanError, match="stop_reason"):
        parse_decision(dump(broken))


def test_sad_path_not_json_and_not_an_object():
    with pytest.raises(PlanError, match="not valid JSON"):
        parse_decision("sorry, I cannot help with that")
    with pytest.raises(PlanError, match="one JSON object"):
        parse_decision("[1, 2, 3]")
    with pytest.raises(PlanError, match="empty"):
        parse_decision("   ")
    with pytest.raises(PlanError, match="JSON text"):
        parse_decision({"goal": "puni"})  # type: ignore[arg-type]
    with pytest.raises(PlanError, match="unterminated"):
        parse_decision("```json\n" + dump(decision_json()))


def test_sad_path_refusal_that_still_schedules_work_is_rejected():
    # ``validate()`` normalises first, so this invariant is only reachable from
    # the inside - assert the guard directly.
    parsed = PlanDecision(goal="puni", reason="stop", expected_observation="none",
                          stop=True, stop_reason="blocked", max_actions=10,
                          max_duration_s=60)
    with pytest.raises(PlanError, match="must not schedule work"):
        parsed._validate_budgets({})


def test_sad_path_empty_goal():
    with pytest.raises(PlanError, match="goal"):
        parse_decision(dump(decision_json(goal="")))


def test_plan_request_round_trips_and_tolerates_extra_keys():
    req = request()
    payload = req.to_dict()
    payload["seq"] = 7
    payload["created_at"] = "2026-11-02T23:00:00+08:00"
    again = PlanRequest.from_dict(payload)
    assert again.to_dict() == req.to_dict()
    assert again.node_ids == req.node_ids
    assert again.capability_ids == req.capability_ids
    assert again.node("puni.prepare.level_up")["priority"] == 80
    assert again.capability("game.run_battle")["max_actions"] == 40
    assert again.budget("max_actions") == 120
    assert again.constraint("allow_input") is True
    assert again.constraint("missing", "fallback") == "fallback"


def test_plan_request_build_accepts_a_duck_typed_world():
    class World:
        def summary(self):
            return world_summary(scene="BATTLE")

    req = PlanRequest.build("puni", World(), graph_digest=graph(),
                            capabilities=CAPABILITIES, budgets={"max_actions": 10},
                            run_id="run-1")
    assert req.world_summary["scene"] == "BATTLE"
    assert req.run_id == "run-1"
    assert req.budget("max_actions") == 10


def test_planner_protocol_is_structural():
    assert isinstance(DeterministicPlanner(), Planner)
    assert isinstance(BridgePlanner(directory="/tmp"), Planner)
    assert isinstance(OpenAICompatPlanner(environ={}), Planner)


# --------------------------------------------------------------------------- #
# 3. deterministic planner
# --------------------------------------------------------------------------- #
def test_deterministic_picks_the_highest_priority_ready_node():
    planned = DeterministicPlanner().decide(request())
    assert planned.selected_node == "puni.prepare.level_up"
    assert planned.capability == "game.run_battle"
    assert planned.goal == "puni"
    assert planned.max_actions == 40          # capability cap (40) < node (60) < run (120)
    assert planned.max_duration_s == 1800     # capability cap < node 2400 < run 3600
    assert planned.requires_approval is False
    planned.validate(known_nodes=graph(), known_capabilities=CAPABILITIES,
                     budgets={"max_actions": 120, "max_duration_s": 3600})


def test_deterministic_reason_names_the_evidence_it_used():
    planned = DeterministicPlanner().decide(request())
    reason = planned.reason
    assert "puni.prepare.level_up" in reason
    assert "priority 80" in reason
    assert "UNKNOWN" in reason          # the world fact that justified the pick
    assert "puni.market.unlock" in reason  # the skipped higher-priority node
    assert "2 node(s) ready" in reason


def test_deterministic_reason_uses_the_last_failure_as_evidence():
    last = {"skill": "game.run_battle", "status": "FAIL", "reason": "click did nothing"}
    planned = DeterministicPlanner().decide(request(last_result=last))
    assert "game.run_battle ended FAIL" in planned.reason
    assert "click did nothing" in planned.reason


def test_deterministic_skips_unregistered_capabilities():
    graph_digest = [
        node("puni.catch", "ready", 99, "game.catch_pet"),
        node("puni.prepare.level_up", "ready", 80, "game.run_battle",
             budget={"max_actions": 60, "max_duration_s": 2400}),
    ]
    planned = DeterministicPlanner().decide(request(graph_digest=graph_digest))
    assert planned.selected_node == "puni.prepare.level_up"
    assert "puni.catch" in planned.reason


def test_deterministic_respects_forbid_approval():
    graph_digest = [node("puni.market.unlock", "ready", 95, "game.market_buy"),
                    node("puni.prepare.level_up", "ready", 80, "game.run_battle")]
    planned = DeterministicPlanner().decide(request(
        graph_digest=graph_digest,
        constraints={"allow_input": True, "forbid_approval": True}))
    assert planned.selected_node == "puni.prepare.level_up"
    assert planned.requires_approval is False
    assert "forbid_approval" in planned.reason

    # without the constraint the approval-requiring node wins and the flag is set
    allowed = DeterministicPlanner().decide(request(
        graph_digest=graph_digest,
        constraints={"allow_input": True, "forbid_approval": False}))
    assert allowed.selected_node == "puni.market.unlock"
    assert allowed.requires_approval is True


def test_deterministic_respects_allow_input_false():
    planned = DeterministicPlanner().decide(request(constraints={"allow_input": False}))
    assert planned.stop is True
    assert planned.need_human is False       # input gates are policy, not missing work
    assert "input disabled" in planned.stop_reason
    assert planned.selected_node == ""


def test_deterministic_stop_lists_every_unmet_reason():
    graph_digest = [
        node("puni.market.unlock", "blocked", 95, "game.market_buy",
             blocked_reason="交易行未开通 (0/3 tasks)"),
        node("puni.spt.mushroom", "pending", 60, "game.run_battle",
             blocked_reason="requires puni.prepare.level_up"),
    ]
    planned = DeterministicPlanner().decide(request(graph_digest=graph_digest))
    assert planned.stop is True and planned.need_human is False
    assert planned.stop_reason.startswith("nothing schedulable:")
    assert "交易行未开通" in planned.stop_reason
    assert "requires puni.prepare.level_up" in planned.stop_reason
    assert planned.max_actions == 0 and planned.max_duration_s == 0
    planned.validate(known_nodes=graph_digest, known_capabilities=CAPABILITIES)


def test_deterministic_need_human_only_when_capabilities_are_missing():
    graph_digest = [
        node("puni.catch", "ready", 50, "game.catch_pet"),
        node("puni.fuse", "ready", 40, "game.fuse_pet"),
    ]
    planned = DeterministicPlanner().decide(request(graph_digest=graph_digest))
    assert planned.stop is True and planned.need_human is True
    assert "game.catch_pet" in planned.human_question
    assert "game.fuse_pet" in planned.stop_reason
    assert planned.human_question.endswith("?")


def test_deterministic_mixed_blockers_do_not_ask_a_human():
    graph_digest = [
        node("puni.catch", "ready", 50, "game.catch_pet"),
        node("puni.market.unlock", "ready", 40, "game.market_buy"),
    ]
    planned = DeterministicPlanner().decide(request(
        graph_digest=graph_digest,
        constraints={"allow_input": True, "forbid_approval": True}))
    assert planned.stop is True
    assert planned.need_human is False
    assert "game.catch_pet" in planned.stop_reason


def test_deterministic_empty_graph_and_all_done():
    empty = DeterministicPlanner().decide(request(graph_digest=[]))
    assert empty.stop is True and "empty" in empty.stop_reason

    done_graph = [node("puni.prepare.level_up", "done", 80, "game.run_battle")]
    finished = DeterministicPlanner().decide(request(
        graph_digest=done_graph, completed_nodes=["puni.prepare.level_up"]))
    assert finished.stop is True
    assert "already done" in finished.stop_reason


def test_deterministic_skips_completed_nodes_even_if_marked_ready():
    graph_digest = [
        node("puni.prepare.level_up", "ready", 90, "game.run_battle"),
        node("puni.observe.pet", "ready", 70, "game.observe_pet"),
    ]
    planned = DeterministicPlanner().decide(request(
        graph_digest=graph_digest, completed_nodes=["puni.prepare.level_up"]))
    assert planned.selected_node == "puni.observe.pet"


def test_deterministic_clamps_to_the_run_budget():
    planned = DeterministicPlanner().decide(request(
        budgets={"max_actions": 12, "max_duration_s": 300}))
    assert planned.max_actions == 12
    assert planned.max_duration_s == 300
    planned.validate(known_nodes=graph(), known_capabilities=CAPABILITIES,
                     budgets={"max_actions": 12, "max_duration_s": 300})


def test_deterministic_stops_when_the_run_budget_is_exhausted():
    planned = DeterministicPlanner().decide(request(
        budgets={"max_actions": 0, "max_duration_s": 0}))
    assert planned.stop is True
    assert "budget is exhausted" in planned.stop_reason


def test_deterministic_labels_a_node_without_a_capability_as_missing_work():
    graph_digest = [{"id": "puni.manual", "status": "ready", "priority": 10,
                     "capability": "", "budget": {}, "blocked_reason": ""}]
    planned = DeterministicPlanner().decide(request(graph_digest=graph_digest))
    assert planned.need_human is True
    assert "declares no capability" in planned.stop_reason


def test_deterministic_uses_observe_wording_for_observation_capabilities():
    graph_digest = [node("puni.observe.pet", "ready", 70, "game.observe_pet")]
    planned = DeterministicPlanner().decide(request(graph_digest=graph_digest))
    assert planned.selected_node == "puni.observe.pet"
    assert "UNKNOWN" in planned.reason
    assert planned.max_actions == 40          # capability declares 0 -> default
    assert planned.expected_observation.startswith("game.observe_pet reports PASS")


def test_deterministic_reads_the_goal_graph_native_digest_shape():
    """A digest built from ``GoalNode.to_dict()`` + ``status_of`` must just work."""

    digest = [
        {   # static "partial" status + scheduler status in its own key
            "id": "puni.prepare.tower_farm", "title": "刷塔", "status": "partial",
            "scheduler_status": "missing_prereq", "priority": 92, "executable": False,
            "skill": "game.run_battle", "blocked_reason": "塔入口未标定",
            "budget": {"max_minutes": 30, "max_battles": 10, "max_spend": 0},
        },
        {
            "id": "puni.prepare.level_up", "title": "练到 36", "status": "unverified",
            "scheduler_status": "ready", "priority": 50, "executable": True,
            "skill": "game.run_battle", "blocked_reason": "",
            "budget": {"max_minutes": 5, "max_battles": 7, "max_spend": 0},
        },
        {
            "id": "puni.prepare.signin", "title": "签到", "status": "completed",
            "scheduler_status": "completed", "priority": 70, "executable": True,
            "skill": "game.run_battle", "blocked_reason": "", "budget": {},
        },
    ]
    planned = DeterministicPlanner().decide(request(graph_digest=digest))
    assert planned.selected_node == "puni.prepare.level_up"
    assert planned.capability == "game.run_battle"
    assert planned.max_actions == 7          # node budget max_battles
    assert planned.max_duration_s == 300     # node budget 5 max_minutes
    assert "puni.prepare.tower_farm" in planned.reason
    assert "puni.prepare.signin" not in planned.reason    # completed, not a blocker


def test_deterministic_asks_a_human_for_a_graph_missing_capability_status():
    digest = [{
        "id": "puni.market.unlock", "status": "missing_capability", "priority": 90,
        "executable": True, "skill": "game.market_buy",
        "blocked_reason": "missing capability 'market.buy' (交易行出入口)",
    }]
    planned = DeterministicPlanner().decide(request(graph_digest=digest))
    assert planned.stop is True and planned.need_human is True
    assert "game.market_buy" in planned.human_question
    assert "missing capability 'market.buy'" in planned.stop_reason


def test_deterministic_prioritises_by_priority_not_by_digest_order():
    digest = [
        node("puni.low", "ready", 10, "game.observe_pet"),
        node("puni.high", "ready", 90, "game.run_battle"),
        node("puni.mid", "ready", 50, "game.run_battle"),
    ]
    assert DeterministicPlanner().decide(
        request(graph_digest=digest)).selected_node == "puni.high"


def test_deterministic_understands_the_runner_budget_terms():
    """The runner budgets in ``max_battles`` / ``max_minutes``; honour both."""

    planned = DeterministicPlanner().decide(request(
        budgets={"max_battles": 9, "max_minutes": 4}))
    assert planned.max_actions == 9
    assert planned.max_duration_s == 240
    planned.validate(known_nodes=graph(), known_capabilities=CAPABILITIES,
                     budgets={"max_battles": 9, "max_minutes": 4})


def test_deterministic_stops_when_the_runner_budget_is_exhausted():
    planned = DeterministicPlanner().decide(request(
        budgets={"max_battles": 0, "max_minutes": 30}))
    assert planned.stop is True
    assert "budget is exhausted" in planned.stop_reason


def test_sad_path_decision_exceeds_a_minutes_run_budget():
    parsed = parse_decision(dump(decision_json(max_duration_s=1800)))
    with pytest.raises(PlanError, match="exceeds the run budget"):
        parsed.validate(known_nodes=graph(), known_capabilities=CAPABILITIES,
                        budgets={"max_minutes": 10})


def test_deterministic_renders_blocked_reason_lists():
    """``GoalGraph.unmet`` returns a list; it must read as one sentence."""

    graph_digest = [{
        "id": "puni.blocked", "status": "missing_prereq", "priority": 50,
        "capability": "game.run_battle",
        "blocked_reason": ["prerequisite node 'puni.a' not completed: unobserved",
                           "missing capability 'battle.repeat'"],
    }]
    planned = DeterministicPlanner().decide(request(graph_digest=graph_digest))
    assert planned.stop is True
    assert ("prerequisite node 'puni.a' not completed: unobserved; "
            "missing capability 'battle.repeat'") in planned.stop_reason


def test_deterministic_respects_a_missing_playbook():
    """The runner refuses nodes without a playbook; the fallback planner should too."""

    graph_digest = [
        node("puni.prepare.level_up", "ready", 90, "game.run_battle", playbook=False),
        node("puni.observe.pet", "ready", 70, "game.observe_pet", playbook=True),
    ]
    planned = DeterministicPlanner().decide(request(graph_digest=graph_digest))
    assert planned.selected_node == "puni.observe.pet"
    assert "puni.prepare.level_up" in planned.reason
    assert "playbook" in planned.reason

    only = DeterministicPlanner().decide(request(graph_digest=[
        node("puni.prepare.level_up", "ready", 90, "game.run_battle", playbook=False)]))
    assert only.stop is True and only.need_human is True
    assert "game.run_battle" in only.human_question
    assert "playbook" in only.stop_reason


def test_deterministic_is_stable_for_equal_priorities():
    graph_digest = [
        node("puni.a", "ready", 50, "game.run_battle"),
        node("puni.b", "ready", 50, "game.run_battle"),
    ]
    planned = DeterministicPlanner().decide(request(graph_digest=graph_digest))
    assert planned.selected_node == "puni.a"


def test_deterministic_refuses_without_a_goal():
    with pytest.raises(PlannerError, match="goal is empty"):
        DeterministicPlanner().decide(request(goal=""))
    with pytest.raises(PlannerError, match="PlanRequest"):
        DeterministicPlanner().decide({"goal": "puni"})  # type: ignore[arg-type]


def test_deterministic_never_calls_the_network(monkeypatch):
    def explode(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("the deterministic planner must not open a connection")

    monkeypatch.setattr(urllib.request, "urlopen", explode)
    DeterministicPlanner().decide(request())


# --------------------------------------------------------------------------- #
# 4. build_planner
# --------------------------------------------------------------------------- #
def test_build_planner_returns_each_implementation(tmp_path):
    assert isinstance(build_planner("deterministic"), DeterministicPlanner)
    assert isinstance(build_planner("ai", environ={}), OpenAICompatPlanner)
    assert isinstance(build_planner("bridge", directory=tmp_path), BridgePlanner)
    assert build_planner("  Deterministic  ").name == "deterministic"


def test_build_planner_rejects_unknown_names():
    with pytest.raises(PlannerError, match="unknown planner"):
        build_planner("gpt-please")
    with pytest.raises(PlannerError, match="unknown planner"):
        build_planner("")


# --------------------------------------------------------------------------- #
# 5. bridge planner
# --------------------------------------------------------------------------- #
class Responder(threading.Thread):
    """Plays the supervising agent: answers the first pending request."""

    def __init__(self, directory, factory, *, interval=0.01, attempts=800):
        super().__init__(daemon=True)
        self.directory = directory
        self.factory = factory
        self.interval = interval
        self.attempts = attempts
        self.seen: list[dict] = []
        self.error: BaseException | None = None
        self.timed_out = False

    def run(self):  # pragma: no cover - thread body
        try:
            for _ in range(self.attempts):
                names = pending(self.directory)
                if names:
                    path = self.directory / names[0]
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    self.seen.append(payload)
                    answer = self.factory(payload)
                    text = answer if isinstance(answer, str) else dump(answer)
                    (self.directory / response_name(int(payload["seq"]))).write_text(
                        text, encoding="utf-8")
                    return
                time.sleep(self.interval)
            self.timed_out = True
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            self.error = exc


def _wrapped(payload: dict, decision: dict) -> dict:
    return {"seq": payload["seq"], "decision": decision}


def test_bridge_round_trip(tmp_path):
    planner = BridgePlanner(directory=tmp_path, timeout_s=5.0, poll_s=0.01)
    responder = Responder(tmp_path, lambda payload: _wrapped(payload, decision_json()))
    responder.start()
    try:
        planned = planner.decide(request())
    finally:
        responder.join(timeout=10)

    assert responder.error is None, responder.error
    assert not responder.timed_out
    assert planned.selected_node == "puni.prepare.level_up"
    assert planned.capability == "game.run_battle"
    assert planned.max_actions == 40

    # the request file contains the full PlanRequest plus the rendezvous keys
    seen = responder.seen[0]
    assert seen["seq"] == 1
    assert seen["goal"] == "puni"
    assert seen["world_summary"] == request().world_summary
    assert seen["graph_digest"] == request().graph_digest
    assert seen["capabilities"] == request().capabilities
    assert seen["budgets"] == request().budgets
    assert seen["constraints"] == request().constraints
    assert seen["run_id"] == "run-20261102-1"
    assert seen["reply_with"] == "response-0001.json"
    assert (tmp_path / request_name(1)).exists()
    assert (tmp_path / response_name(1)).exists()
    # atomic writes leave nothing behind
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "request-0001.json", "response-0001.json"]


def test_bridge_accepts_a_bare_decision_object(tmp_path):
    planner = BridgePlanner(directory=tmp_path, timeout_s=5.0, poll_s=0.01)
    responder = Responder(tmp_path, lambda payload: decision_json())
    responder.start()
    try:
        planned = planner.decide(request())
    finally:
        responder.join(timeout=10)
    assert responder.error is None, responder.error
    assert planned.capability == "game.run_battle"


def test_bridge_accepts_a_fenced_response_written_by_hand(tmp_path):
    planner = BridgePlanner(directory=tmp_path, timeout_s=5.0, poll_s=0.01)
    responder = Responder(
        tmp_path, lambda payload: "```json\n" + dump(decision_json()) + "\n```")
    responder.start()
    try:
        assert planner.decide(request()).goal == "puni"
    finally:
        responder.join(timeout=10)
    assert responder.error is None, responder.error


def test_bridge_timeout_raises_with_the_paths(tmp_path):
    planner = BridgePlanner(directory=tmp_path, timeout_s=0.2, poll_s=0.01)
    started = time.monotonic()
    with pytest.raises(PlannerError, match="no answer for request-0001.json"):
        planner.decide(request())
    elapsed = time.monotonic() - started
    assert 0.15 <= elapsed < 3.0
    assert (tmp_path / request_name(1)).exists()      # the request stays on disk


def test_bridge_ignores_a_response_older_than_the_request(tmp_path, monkeypatch):
    """A leftover answer from a previous run must never be accepted.

    The file name and the payload seq both match; only the mtime betrays that it
    was written before the request, which is exactly the case a human ``mv``ing
    an old answer into place would create.
    """

    stale = tmp_path / response_name(1)
    stale.write_text(dump({"seq": 1, "decision": decision_json()}), encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))

    # force sequence reuse, which the directory scan normally prevents
    monkeypatch.setattr(bridge_module, "next_seq", lambda directory: 1)
    planner = BridgePlanner(directory=tmp_path, timeout_s=0.25, poll_s=0.01)
    with pytest.raises(PlannerError, match="no answer"):
        planner.decide(request())
    assert BridgePlanner._read_fresh(stale, time.time_ns()) is None
    assert BridgePlanner._read_fresh(stale, 0) == stale.read_text(encoding="utf-8")


def test_bridge_rejects_a_response_for_a_different_sequence(tmp_path):
    planner = BridgePlanner(directory=tmp_path, timeout_s=2.0, poll_s=0.01)
    responder = Responder(tmp_path, lambda payload: {"seq": payload["seq"] + 40,
                                                     "decision": decision_json()})
    responder.start()
    try:
        with pytest.raises(PlannerError, match="stale answer"):
            planner.decide(request())
    finally:
        responder.join(timeout=10)
    assert responder.error is None, responder.error


def test_bridge_continues_after_an_answered_run(tmp_path):
    first = BridgePlanner(directory=tmp_path, timeout_s=5.0, poll_s=0.01)
    responder = Responder(tmp_path, lambda payload: _wrapped(payload, decision_json()))
    responder.start()
    try:
        first.decide(request())
    finally:
        responder.join(timeout=10)
    assert next_seq(tmp_path) == 2

    second = BridgePlanner(directory=tmp_path, timeout_s=5.0, poll_s=0.01)
    responder = Responder(tmp_path, lambda payload: _wrapped(payload, decision_json()))
    responder.start()
    try:
        second.decide(request())
    finally:
        responder.join(timeout=10)
    assert responder.seen[0]["seq"] == 2
    assert (tmp_path / request_name(2)).exists()


def test_bridge_tolerates_a_half_written_file(tmp_path):
    """A JSON decode error is retried (the file may still be growing)."""

    def factory(payload):
        return "{\"seq\": 1, \"decision\": {\"goal\": \"puni\""   # truncated on purpose

    planner = BridgePlanner(directory=tmp_path, timeout_s=0.4, poll_s=0.01)
    responder = Responder(tmp_path, factory)
    responder.start()
    try:
        with pytest.raises(PlannerError, match="neither JSON nor a decision document"):
            planner.decide(request())
    finally:
        responder.join(timeout=10)
    assert responder.error is None, responder.error


def test_bridge_requires_an_existing_directory(tmp_path):
    missing = tmp_path / "does-not-exist"
    planner = BridgePlanner(directory=missing, timeout_s=0.1, poll_s=0.01)
    with pytest.raises(PlannerUnavailable, match="does not exist"):
        planner.decide(request())
    with pytest.raises(PlannerUnavailable, match="directory is empty"):
        BridgePlanner(directory="", timeout_s=0.1).decide(request())


def test_bridge_refuses_without_a_goal(tmp_path):
    with pytest.raises(PlannerError, match="goal is empty"):
        BridgePlanner(directory=tmp_path, timeout_s=0.1).decide(request(goal=""))


def test_bridge_answer_validates_before_writing(tmp_path):
    with pytest.raises(PlanError):
        BridgeWatcher.answer(tmp_path, 1, {"selected_node": "puni.prepare.level_up"})
    assert not (tmp_path / response_name(1)).exists()

    path = BridgeWatcher.answer(tmp_path, 1, decision_json())
    assert path == tmp_path / response_name(1)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["seq"] == 1
    assert payload["decision"]["selected_node"] == "puni.prepare.level_up"
    with pytest.raises(PlannerError, match="positive integer"):
        BridgeWatcher.answer(tmp_path, 0, decision_json())


def test_bridge_pending_lists_only_unanswered_requests(tmp_path):
    assert pending(tmp_path) == []
    (tmp_path / request_name(1)).write_text(dump(request().to_dict()), encoding="utf-8")
    time.sleep(0.01)
    (tmp_path / request_name(2)).write_text(dump(request().to_dict()), encoding="utf-8")
    assert pending(tmp_path) == ["request-0001.json", "request-0002.json"]
    assert pending(tmp_path, unanswered_only=False) == [
        "request-0001.json", "request-0002.json"]

    time.sleep(0.01)
    BridgeWatcher.answer(tmp_path, 1, decision_json())
    assert pending(tmp_path) == ["request-0002.json"]
    assert pending(tmp_path, unanswered_only=False) == [
        "request-0001.json", "request-0002.json"]


def test_bridge_format_prompt_is_answerable_without_python():
    brief = format_prompt(request(), seq=7, directory="/tmp/bridge")
    for expected in (
        "PLANNER BRIEF", "goal        : puni", "READY NODES",
        "puni.prepare.level_up", "CAPABILITIES", "game.run_battle",
        "requires_approval=yes",               # game.market_buy is gated
        "unknowns", "pets.烈火猴.level",
        "ANSWER", "/tmp/bridge/response-0007.json",
        '"selected_node"', "stop_reason", "human_question",
        "Never answer with coordinates",
    ):
        assert expected in brief, expected
    assert "puni.market.unlock" in brief          # shown as not selectable
    # the brief must not leak the entire request payload (kept readable)
    assert len(brief) < 6000


def test_bridge_format_prompt_includes_last_result_and_events():
    req = request(
        last_result={"skill": "game.run_battle", "status": "FAIL",
                     "reason": "click did nothing", "changed": False, "proof": ""},
        recent_events=[{"kind": "skill.fail", "skill": "game.run_battle"}])
    brief = format_prompt(req)
    assert "LAST RESULT" in brief
    assert "click did nothing" in brief
    assert "RECENT EVENTS" in brief
    assert "skill.fail" in brief


# --------------------------------------------------------------------------- #
# 6. AI transport (no real network)
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self) -> bytes:
        return self._body


class FakeTransport:
    """Records the HTTP request and replays a canned chat-completion body."""

    def __init__(self, *, content=None, body=None, status=200,
                 raise_error: BaseException | None = None,
                 model="deepseek-v4.1-flash", finish_reason="stop",
                 usage=None):
        self.content = content if content is not None else dump(decision_json())
        self.body = body
        self.status = status
        self.raise_error = raise_error
        self.model = model
        self.finish_reason = finish_reason
        self.usage = usage if usage is not None else {
            "prompt_tokens": 1200, "completion_tokens": 90, "total_tokens": 1290}
        self.calls: list[dict] = []

    def response_body(self) -> dict:
        if self.body is not None:
            return self.body
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": self.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": self.content},
                "finish_reason": self.finish_reason,
            }],
            "usage": self.usage,
        }

    def __call__(self, request, timeout=None):
        self.calls.append({
            "url": request.full_url,
            "headers": {k.lower(): v for k, v in request.headers.items()},
            "body": json.loads(request.data.decode("utf-8")),
            "timeout": timeout,
        })
        if self.raise_error is not None:
            raise self.raise_error
        payload = self.response_body()
        raw = payload if isinstance(payload, bytes) else dump(payload).encode("utf-8")
        return FakeResponse(raw, status=self.status)


API_KEY_ENV = "SEERAUTO_AI_API_KEY"


@pytest.fixture
def ai_env(monkeypatch):
    monkeypatch.setenv(API_KEY_ENV, "test-key-not-a-real-secret")
    monkeypatch.delenv("SEERAUTO_AI_BASE_URL", raising=False)
    monkeypatch.delenv("SEERAUTO_AI_MODEL", raising=False)
    monkeypatch.delenv("SEERAUTO_AI_TIMEOUT_S", raising=False)
    monkeypatch.delenv("SEERAUTO_AI_MAX_CALLS", raising=False)
    monkeypatch.delenv("SEERAUTO_AI_SESSION", raising=False)
    monkeypatch.delenv("SEERAUTO_AI_JSON_MODE", raising=False)
    return monkeypatch


def test_ai_availability_is_env_driven(ai_env):
    planner = OpenAICompatPlanner()
    assert planner.available() is True
    assert planner.config.base_url == openai_compat.DEFAULT_BASE_URL
    assert planner.config.model == openai_compat.DEFAULT_MODEL
    assert planner.config.timeout_s == 45
    # the gateway requires a session header; a stable id is generated when unset
    assert planner.session.startswith("seerauto-")
    assert planner.session != OpenAICompatPlanner().session  # per instance

    ai_env.setenv("SEERAUTO_AI_SESSION", "session-abc")
    assert OpenAICompatPlanner().session == "session-abc"

    ai_env.setenv("SEERAUTO_AI_BASE_URL", "http://127.0.0.1:9/v1")
    assert OpenAICompatPlanner().available() is True
    assert OpenAICompatPlanner().config.endpoint == (
        "http://127.0.0.1:9/v1/chat/completions")

    ai_env.delenv(API_KEY_ENV)
    offline = OpenAICompatPlanner()
    assert offline.available() is False
    with pytest.raises(PlannerUnavailable, match=API_KEY_ENV):
        offline.decide(request())


def test_ai_config_rejects_garbage_env():
    with pytest.raises(PlannerError, match="SEERAUTO_AI_TIMEOUT_S"):
        AIConfig.from_env({"SEERAUTO_AI_TIMEOUT_S": "soon"})
    with pytest.raises(PlannerError, match="SEERAUTO_AI_MAX_CALLS"):
        AIConfig.from_env({"SEERAUTO_AI_MAX_CALLS": "many"})
    with pytest.raises(PlannerError, match="SEERAUTO_AI_BASE_URL"):
        AIConfig.from_env({"SEERAUTO_AI_BASE_URL": "opencode.ai/inference"})
    assert AIConfig.from_env({}).json_mode is True
    assert AIConfig.from_env({"SEERAUTO_AI_JSON_MODE": "0"}).json_mode is False
    assert AIConfig.from_env({"SEERAUTO_AI_JSON_MODE": "off"}).json_mode is False
    assert AIConfig.from_env({}).endpoint.endswith("/chat/completions")


def test_ai_decide_sends_text_only_and_attaches_meta(ai_env, monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(urllib.request, "urlopen", transport)

    planner = OpenAICompatPlanner(prices={"deepseek-v4.1-flash": (0.5, 1.5)})
    planned = planner.decide(request())

    assert planned.selected_node == "puni.prepare.level_up"
    assert planned.capability == "game.run_battle"
    assert isinstance(planned.meta, CallMeta)
    meta = planned.meta
    assert meta.model == "deepseek-v4.1-flash"
    assert meta.latency_ms >= 0
    assert meta.prompt_chars > 0
    assert 0 < meta.response_chars < 5000
    assert meta.prompt_tokens == 1200 and meta.completion_tokens == 90
    assert meta.cost_usd == pytest.approx((1200 * 0.5 + 90 * 1.5) / 1_000_000)
    assert meta.finish_reason == "stop"
    assert meta.call_index == 1
    assert planned.to_dict(with_meta=True)["meta"]["model"] == "deepseek-v4.1-flash"

    call = transport.calls[0]
    assert call["url"] == "https://opencode.ai/inference/go/openai/v1/chat/completions"
    assert call["headers"]["authorization"] == "Bearer test-key-not-a-real-secret"
    assert call["headers"]["x-opencode-session"].startswith("seerauto-")
    assert call["headers"]["content-type"] == "application/json"
    assert call["timeout"] == 45
    body = call["body"]
    assert body["model"] == "deepseek-v4.1-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert all(isinstance(m["content"], str) for m in body["messages"])
    assert "image_url" not in json.dumps(body) and "base64" not in json.dumps(body)
    user_view = json.loads(body["messages"][1]["content"])
    assert user_view["goal"] == "puni"
    assert user_view["graph_digest"][1]["id"] == "puni.prepare.level_up"
    assert user_view["capabilities"][0]["id"] == "game.observe_pet"
    assert planner.calls_remaining() == 19


def test_ai_json_mode_can_be_disabled(ai_env, monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(urllib.request, "urlopen", transport)
    planner = OpenAICompatPlanner(environ=dict(os.environ, SEERAUTO_AI_JSON_MODE="false"))
    planner.decide(request())
    assert "response_format" not in transport.calls[0]["body"]


def test_ai_accepts_a_fenced_answer(ai_env, monkeypatch):
    transport = FakeTransport(content="```json\n" + dump(decision_json()) + "\n```")
    monkeypatch.setattr(urllib.request, "urlopen", transport)
    assert OpenAICompatPlanner().decide(request()).goal == "puni"


def test_ai_call_cap_is_enforced(ai_env, monkeypatch):
    transport = FakeTransport()
    monkeypatch.setattr(urllib.request, "urlopen", transport)
    ai_env.setenv("SEERAUTO_AI_MAX_CALLS", "2")
    planner = OpenAICompatPlanner()
    planner.decide(request())
    planner.decide(request())
    assert planner.available() is False
    assert planner.calls_remaining() == 0
    with pytest.raises(PlannerUnavailable, match="call cap"):
        planner.decide(request())
    assert len(transport.calls) == 2


def test_ai_http_401_surfaces_as_planner_error(ai_env):
    error = urllib.error.HTTPError(
        "https://example.invalid/v1/chat/completions", 401, "Unauthorized", None,
        io.BytesIO(b'{"error":{"message":"bad key"}}'))
    transport = FakeTransport(raise_error=error)
    planner = OpenAICompatPlanner(opener=transport)
    with pytest.raises(PlannerError, match="HTTP 401") as excinfo:
        planner.decide(request())
    assert "credential" in str(excinfo.value)
    assert "test-key-not-a-real-secret" not in str(excinfo.value)


def test_ai_timeout_surfaces_as_planner_error(ai_env):
    planner = OpenAICompatPlanner(opener=FakeTransport(raise_error=TimeoutError("slow")))
    with pytest.raises(PlannerError, match="did not answer"):
        planner.decide(request())

    wrapped = FakeTransport(raise_error=urllib.error.URLError(TimeoutError("slow")))
    with pytest.raises(PlannerError, match="did not answer"):
        OpenAICompatPlanner(opener=wrapped).decide(request())


def test_ai_connection_error_surfaces_as_planner_error(ai_env):
    planner = OpenAICompatPlanner(opener=FakeTransport(
        raise_error=urllib.error.URLError(ConnectionRefusedError("refused"))))
    with pytest.raises(PlannerError, match="cannot reach the AI gateway"):
        planner.decide(request())


def test_ai_non_json_and_error_bodies_are_errors(ai_env):
    with pytest.raises(PlannerError, match="non-JSON body"):
        OpenAICompatPlanner(opener=FakeTransport(body=b"<html>nope</html>")).decide(
            request())
    with pytest.raises(PlannerError, match="reported an error"):
        OpenAICompatPlanner(opener=FakeTransport(
            body={"error": {"message": "quota"}})).decide(request())
    with pytest.raises(PlannerError, match="no 'choices'"):
        OpenAICompatPlanner(opener=FakeTransport(body={"object": "list"})).decide(
            request())
    with pytest.raises(PlannerError, match="empty message content"):
        OpenAICompatPlanner(opener=FakeTransport(content="")).decide(request())


def test_ai_schema_violation_is_not_patched_up(ai_env):
    broken = decision_json()
    broken.pop("max_duration_s")
    planner = OpenAICompatPlanner(opener=FakeTransport(content=dump(broken)))
    with pytest.raises(PlanError, match="missing required key"):
        planner.decide(request())


def test_ai_unknown_node_or_capability_from_the_model_is_rejected(ai_env):
    with pytest.raises(PlanError, match="unknown node"):
        OpenAICompatPlanner(opener=FakeTransport(
            content=dump(decision_json(selected_node="puni.imaginary")))).decide(request())
    with pytest.raises(PlanError, match="unknown capability"):
        OpenAICompatPlanner(opener=FakeTransport(
            content=dump(decision_json(capability="game.teleport")))).decide(request())


def test_ai_truncated_answer_is_an_error(ai_env):
    transport = FakeTransport(content='{"goal": "puni", "selected_n',
                              finish_reason="length")
    with pytest.raises(PlannerError, match="truncated"):
        OpenAICompatPlanner(opener=transport).decide(request())


def test_ai_refusal_answer_is_accepted(ai_env):
    transport = FakeTransport(content=dump(stop_json(need_human=True,
                                                     human_question="Which node first?")))
    planned = OpenAICompatPlanner(opener=transport).decide(request())
    assert planned.stop is True and planned.need_human is True
    assert planned.selected_node == ""


def test_ai_refuses_without_a_goal(ai_env):
    transport = FakeTransport()
    with pytest.raises(PlannerError, match="goal is empty"):
        OpenAICompatPlanner(opener=transport).decide(request(goal=""))
    assert transport.calls == []


def test_ai_cost_is_none_for_a_model_without_a_price(ai_env):
    """Unknown models must report `None`, never a guessed price.

    The default model now HAS a price (from the gateway's catalogue), so the test
    pins the unknown-model path explicitly instead of relying on the table being
    empty.
    """

    ai_env.setenv("SEERAUTO_AI_MODEL", "no-such-model")
    planner = OpenAICompatPlanner(opener=FakeTransport())
    planned = planner.decide(request())
    assert planned.meta.cost_usd is None
    assert planned.meta.to_dict()["cost_usd"] is None
