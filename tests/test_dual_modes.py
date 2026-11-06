"""Tests for the two-mode vocabulary and the rule table that picks the mode.

The motivating failures are documented in the modules; these tests pin the parts an
auditor will check:

* exactly two modes, exactly two policies, and exactly the ten switch reasons;
* the eight canonical outcomes exist, are distinct, and map losslessly onto the
  legacy runner vocabulary (except where that is impossible, which is explicit);
* every adaptive rule is reachable and the priority order is the documented one —
  in particular the deadline floor runs before any escalation, and "unknown" is
  never treated as a stall;
* the dual package imports no LLM SDK, no network client and nothing that can touch
  the game client (AGENTS.md §7 — the core is stdlib + seerauto only).
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from seerauto.dual import modes as M
from seerauto.dual import outcome as O
from seerauto.runner import outcome as runner_outcome

PKG = pathlib.Path(__file__).resolve().parents[1] / "seerauto" / "dual"

EXPECTED_REASONS = {
    "NEW_COMPLEX_TASK", "LONG_NO_PROGRESS", "GRAPH_BLOCKED", "NEED_RESEARCH",
    "VISION_FAILURE", "MARGINAL_VALUE_DROP", "EVIDENCE_CONFLICT", "RECOVERY_FAILED",
    "OPPORTUNITY", "RESOLVED",
}

EXPECTED_OUTCOMES = {
    "PROGRESS", "NO_WORK", "BLOCKED", "FAILED", "UNVERIFIED_ACTIVITY", "PAUSED",
    "INTERRUPTED", "DEADLINE_REACHED",
}


# --------------------------------------------------------------------------- #
# modes / policies / reasons
# --------------------------------------------------------------------------- #
def test_exactly_two_modes_and_two_policies():
    assert {mode.value for mode in M.Mode} == {"EFFICIENT", "MAX_RESULTS"}
    assert {policy.value for policy in M.Policy} == {"ADAPTIVE", "STATIC"}


def test_exactly_the_ten_switch_reasons():
    assert {reason.value for reason in M.SwitchReason} == EXPECTED_REASONS


@pytest.mark.parametrize("value, expected", [
    ("efficient", M.Mode.EFFICIENT),
    (" MAX_RESULTS ", M.Mode.MAX_RESULTS),
    (M.Mode.MAX_RESULTS, M.Mode.MAX_RESULTS),
])
def test_mode_parse_is_case_and_space_tolerant(value, expected):
    assert M.Mode.parse(value) is expected


def test_mode_parse_rejects_unknown_values():
    with pytest.raises(ValueError, match="unknown mode"):
        M.Mode.parse("TURBO")
    with pytest.raises(ValueError, match="required"):
        M.Mode.parse(None)
    assert M.Mode.parse(None, default=M.Mode.EFFICIENT) is M.Mode.EFFICIENT


# --------------------------------------------------------------------------- #
# resolve(): one rule per reason, priority pinned
# --------------------------------------------------------------------------- #
def _resolve(**kwargs):
    signals = kwargs.pop("signals", {})
    return M.resolve(kwargs.pop("policy", M.Policy.ADAPTIVE), M.Signals(**signals),
                     **kwargs)


def test_default_is_efficient():
    resolution = _resolve()
    assert resolution.mode is M.Mode.EFFICIENT
    assert resolution.reason is M.SwitchReason.MARGINAL_VALUE_DROP
    assert resolution.rule == "efficient_default"


def test_deadline_floor_beats_every_escalation():
    # Every escalation signal is set; the deadline still wins because spending model
    # turns you cannot realize before 07:00 is pure waste.
    resolution = _resolve(signals={
        "deadline_remaining_s": 60,
        "new_task": True,
        "blocked": True,
        "unresolved_questions": 3,
        "evidence_conflicts": 2,
        "vision_failures": 5,
        "recovery_failures": 5,
        "consecutive_no_work": 5,
        "minutes_since_progress": 120,
        "opportunity": True,
    })
    assert resolution.mode is M.Mode.EFFICIENT
    assert resolution.reason is M.SwitchReason.MARGINAL_VALUE_DROP
    assert resolution.rule == "deadline_floor"


@pytest.mark.parametrize("signals, expected_mode, expected_reason, expected_rule", [
    ({"new_task": True}, M.Mode.MAX_RESULTS, M.SwitchReason.NEW_COMPLEX_TASK,
     "new_task"),
    ({"blocked": True}, M.Mode.MAX_RESULTS, M.SwitchReason.GRAPH_BLOCKED,
     "graph_blocked"),
    ({"unresolved_questions": 2}, M.Mode.MAX_RESULTS, M.SwitchReason.NEED_RESEARCH,
     "need_research"),
    ({"evidence_conflicts": 1}, M.Mode.MAX_RESULTS, M.SwitchReason.EVIDENCE_CONFLICT,
     "evidence_conflict"),
    ({"vision_failures": 2}, M.Mode.MAX_RESULTS, M.SwitchReason.VISION_FAILURE,
     "vision_failure"),
    ({"recovery_failures": 2}, M.Mode.MAX_RESULTS, M.SwitchReason.RECOVERY_FAILED,
     "recovery_failed"),
    ({"consecutive_no_work": 2}, M.Mode.MAX_RESULTS, M.SwitchReason.LONG_NO_PROGRESS,
     "long_no_progress"),
    ({"minutes_since_progress": 25}, M.Mode.MAX_RESULTS,
     M.SwitchReason.LONG_NO_PROGRESS, "long_no_progress"),
    ({"opportunity": True}, M.Mode.MAX_RESULTS, M.SwitchReason.OPPORTUNITY,
     "opportunity"),
])
def test_each_adaptive_rule_is_reachable(signals, expected_mode, expected_reason,
                                         expected_rule):
    resolution = _resolve(signals=signals)
    assert resolution.mode is expected_mode
    assert resolution.reason is expected_reason
    assert resolution.rule == expected_rule
    assert resolution.because  # a rule without an explanation is not auditable


def test_priority_blocked_beats_research_and_vision():
    resolution = _resolve(signals={"blocked": True, "unresolved_questions": 4,
                                   "vision_failures": 9})
    assert resolution.rule == "graph_blocked"


def test_unknown_progress_never_fires_the_stall_rule():
    # minutes_since_progress=None (never observed) must not look like "stalled for
    # zero minutes" *or* like a stall.  A fresh night must start EFFICIENT.
    resolution = _resolve(signals={"minutes_since_progress": None})
    assert resolution.mode is M.Mode.EFFICIENT
    assert resolution.rule == "efficient_default"


def test_returning_from_max_results_uses_resolved():
    resolution = _resolve(current=M.Mode.MAX_RESULTS)
    assert resolution.mode is M.Mode.EFFICIENT
    assert resolution.reason is M.SwitchReason.RESOLVED
    assert resolution.rule == "resolved"


def test_static_policy_ignores_signals_and_has_no_reason():
    resolution = _resolve(policy=M.Policy.STATIC, signals={
        "blocked": True, "unresolved_questions": 5, "deadline_remaining_s": 10},
        static_mode=M.Mode.MAX_RESULTS)
    assert resolution.mode is M.Mode.MAX_RESULTS
    assert resolution.reason is None
    assert resolution.rule == "static"
    assert resolution.policy is M.Policy.STATIC


def test_resolution_serialises_for_the_audit_trail():
    payload = _resolve(signals={"blocked": True}).to_dict()
    assert payload["mode"] == "MAX_RESULTS"
    assert payload["reason"] == "GRAPH_BLOCKED"
    assert payload["policy"] == "ADAPTIVE"
    assert payload["signals"]["blocked"] is True


# --------------------------------------------------------------------------- #
# outcome vocabulary (eight facts, no umbrella)
# --------------------------------------------------------------------------- #
def test_exactly_eight_canonical_outcomes():
    assert {member.value for member in O.Outcome} == EXPECTED_OUTCOMES
    assert len(O.Outcome) == 8
    # no aliasing: every member is distinct
    assert len({id(member) for member in O.Outcome}) == 8


def test_canonical_order_covers_every_member_once():
    assert set(O.ORDER) == set(O.Outcome)
    assert len(O.ORDER) == 8


@pytest.mark.parametrize("text, expected", [
    ("PROGRESS", O.Outcome.PROGRESS),
    ("success", O.Outcome.PROGRESS),
    ("NO_WORK", O.Outcome.NO_WORK),
    ("blocked", O.Outcome.BLOCKED),
    ("FAILED", O.Outcome.FAILED),
    ("UNVERIFIED_ACTIVITY", O.Outcome.UNVERIFIED_ACTIVITY),
    ("unverified", O.Outcome.UNVERIFIED_ACTIVITY),
    ("PAUSED", O.Outcome.PAUSED),
    ("INTERRUPTED", O.Outcome.INTERRUPTED),
    ("TIMEOUT", O.Outcome.DEADLINE_REACHED),
    ("deadline-reached", O.Outcome.DEADLINE_REACHED),
])
def test_ledger_outcome_strings_coerce(text, expected):
    assert O.canonical(text) is expected


def test_unrecorded_outcome_is_not_laundered_into_a_healthy_value():
    assert O.canonical(None) is None
    assert O.canonical("") is None
    assert O.canonical("PASS") is None  # wild_cycle's status field, not an outcome
    with pytest.raises(ValueError):
        O.coerce("something else")


def test_every_outcome_round_trips_or_refuses_explicitly():
    """Completeness: no canonical outcome may silently lose its meaning."""

    for member in O.Outcome:
        if member in O.NO_RUNNER_EQUIVALENT:
            with pytest.raises(ValueError, match="no equivalent"):
                O.to_runner(member)
            continue
        legacy = O.to_runner(member)
        assert isinstance(legacy, runner_outcome.Outcome)
        if member in O.LOSSY_RUNNER_MAP:
            # documented loss: the legacy vocabulary has one "stopped" member
            assert legacy is runner_outcome.Outcome.PAUSED
            assert O.from_runner(legacy) is O.Outcome.PAUSED
            continue
        assert O.from_runner(legacy) is member


def test_runner_paused_splits_into_paused_and_interrupted():
    assert O.from_runner(runner_outcome.Outcome.PAUSED) is O.Outcome.PAUSED
    assert (O.from_runner(runner_outcome.Outcome.PAUSED, interrupted=True)
            is O.Outcome.INTERRUPTED)


def test_runner_vocabulary_is_a_subset_of_the_canonical_one():
    # No legacy outcome may be left unmapped: that would be a silent "unknown".
    assert set(O.RUNNER_MAP) == set(runner_outcome.Outcome)
    # Progress and deadline are the two renames; the rest keep their name.
    assert O.RUNNER_MAP[runner_outcome.Outcome.SUCCESS] is O.Outcome.PROGRESS
    assert (O.RUNNER_MAP[runner_outcome.Outcome.TIMEOUT]
            is O.Outcome.DEADLINE_REACHED)


# --------------------------------------------------------------------------- #
# batch classification (the 2026-11-04 pathology)
# --------------------------------------------------------------------------- #
def test_refused_batch_is_no_work_not_success():
    assert O.classify_batch({
        "outcome": "NO_WORK", "battles": 0, "actions": 0,
        "reason": "planner refused: nothing schedulable: x",
    }) is O.Outcome.NO_WORK


def test_stray_click_is_unverified_activity_not_progress():
    # The 2026-11-04 signature: actions=1, battles=0, no proof.
    assert O.classify_batch({"actions": 1, "battles": 0}) is O.Outcome.UNVERIFIED_ACTIVITY


def test_proof_makes_it_progress():
    assert O.classify_batch({
        "new_completed_nodes": ["puni.prepare.wild_45"], "outcome": "SUCCESS",
    }) is O.Outcome.PROGRESS
    # A proof string on a milestone record is enough, no outcome field required.
    assert O.classify_batch({"node": "x", "proof": "level 41"}) is O.Outcome.PROGRESS


def test_interrupted_flag_wins_over_the_recorded_outcome():
    assert O.classify_batch({
        "outcome": "PROGRESS", "interrupted": True,
    }) is O.Outcome.INTERRUPTED


def test_missing_record_is_failed_never_healthy():
    assert O.classify_batch(None) is O.Outcome.FAILED
    assert O.classify_batch({}) is O.Outcome.FAILED
    # A record that admits nothing was attempted is NO_WORK, not a failure.
    assert O.classify_batch({"reason": "nothing was schedulable"}) is O.Outcome.NO_WORK


def test_split_last_counts_trailing_streak():
    last, streak = O.split_last([
        {"outcome": "PROGRESS"},
        {"outcome": "NO_WORK"},
        {"outcome": "NO_WORK"},
    ])
    assert last is O.Outcome.NO_WORK
    assert streak == 2


# --------------------------------------------------------------------------- #
# package hygiene (architectural rule, checked not assumed)
# --------------------------------------------------------------------------- #
def _imports(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                roots.append("." * node.level + (node.module or ""))
            elif node.module:
                roots.append(node.module)
    return roots


def test_dual_package_has_no_llm_network_or_gui_imports():
    forbidden_roots = {"requests", "httpx", "urllib", "socket", "http", "openai",
                       "anthropic", "dsh", "selenium", "playwright"}
    forbidden_seerauto = ("seerauto.capture", "seerauto.live", "seerauto.skills",
                          "seerauto.window", "seerauto.vision", "seerauto.actions",
                          "seerauto.executor")
    files = sorted(PKG.glob("*.py"))
    assert files, "no dual modules found"
    for path in files:
        for root in _imports(path):
            head = root.lstrip(".").split(".")[0]
            assert head not in forbidden_roots, f"{path.name} imports {root}"
            assert not root.startswith(forbidden_seerauto), \
                f"{path.name} imports {root} (the control plane must not touch the GUI)"
