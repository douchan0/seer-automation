"""Regression tests for the no-work/false-success night-run failure.

These tests do not claim a game feature is LIVE_VERIFIED. They only enforce
correct outcome semantics so the DSH supervisor can choose the next task.
"""
from __future__ import annotations

import pytest

from seerauto.runner.outcomes import classify_run, run_exit_code


@pytest.mark.parametrize(("reason", "expected", "code"), [
    ("planner refused: nothing schedulable: market unlock unknown", "NO_WORK", 10),
    ("client is not showing game content (blank / loading)", "BLOCKED", 11),
    ("blocked: no pet can battle", "BLOCKED", 11),
    ("crashed: ValueError: bad window", "FAILED", 1),
    ("error budget 3 reached", "FAILED", 1),
    ("interrupted (SIGINT)", "INTERRUPTED", 130),
    ("stop requested", "INTERRUPTED", 130),
    ("dry run complete (no input was sent)", "DRY_RUN", 0),
    ("goal progressed: node completed", "PROGRESS", 0),
    ("all executable nodes complete", "NO_WORK", 10),
])
def test_outcome_reason(reason, expected, code):
    summary = {"status": "finished", "stop_reason": reason,
               "stats": {"actions": 0, "battles": 0}}
    assert classify_run(summary) == expected
    assert run_exit_code(summary) == code


def test_no_progress_after_an_actual_action_is_not_success():
    summary = {"status": "finished", "stop_reason": "action budget 20 reached",
               "stats": {"actions": 20, "battles": 0}}
    assert classify_run(summary) == "UNVERIFIED_ACTIVITY"
    assert run_exit_code(summary) != 0


def test_blocked_status_is_honored():
    summary = {"status": "blocked", "stop_reason": "unrecognized scene",
               "stats": {"actions": 0}}
    assert classify_run(summary) == "BLOCKED"


def test_all_executable_done_is_not_puni_completed():
    summary = {"status": "finished", "stop_reason": "all executable nodes complete",
               "stats": {"actions": 2, "battles": 0}}
    assert classify_run(summary) == "UNVERIFIED_ACTIVITY"
    assert run_exit_code(summary) != 0


def test_subprocess_success_without_run_progress_is_no_work():
    summary = {"status": "finished", "stop_reason": None,
               "stats": {"actions": 0, "battles": 0, "loops": 1}}
    assert classify_run(summary) == "NO_WORK"


def test_real_progress_requires_explicit_node_completion_reason():
    summary = {"status": "finished",
               "stop_reason": "goal progressed: node completed",
               "stats": {"actions": 12, "battles": 3}}
    assert classify_run(summary) == "PROGRESS"


def test_verified_node_progress_survives_a_later_budget_stop():
    summary = {"status": "finished", "stop_reason": "time budget 8 min reached",
               "stats": {"actions": 44, "battles": 5, "verified_nodes": 1}}
    assert classify_run(summary) == "PROGRESS"
