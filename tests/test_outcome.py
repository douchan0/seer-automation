"""Tests for the run-outcome classifier.

The motivating bug: on 2026-11-04 the supervisor ran 26 batches, 25 of them with
`battles=0 actions=0` and stop reason `planner refused: nothing schedulable`, and
counted every one as healthy because `goal start` exited 0.  These tests pin down
that a refusal is NO_WORK, that NO_WORK is not success, and that only a proof-backed
completed node counts as verified progress.
"""

from __future__ import annotations

import pytest

from seerauto.runner.outcome import (
    EXIT_CODES,
    Outcome,
    classify_reason,
    classify_run,
    exit_code_for,
)


def _summary(**overrides):
    base = {
        "run_id": "r1",
        "status": "finished",
        "stop_reason": "",
        "stats": {"battles": 0, "actions": 0, "errors": 0, "loops": 1,
                  "elapsed_s": 1.0},
        "nodes": {},
        "proofs": {},
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# reason -> outcome
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reason, expected", [
    ("planner refused: nothing schedulable: x", Outcome.NO_WORK),
    ("nothing schedulable: 43 nodes not ready", Outcome.NO_WORK),
    ("goal progressed: node completed", Outcome.SUCCESS),
    ("all executable nodes complete", Outcome.SUCCESS),
    ("client is not showing game content (blank / loading / error page)", Outcome.FAILED),
    ("client is at the login screen ('账密登录'), not in the game world", Outcome.FAILED),
    ("crashed: KeyError: 'x'", Outcome.FAILED),
    ("node puni.x attempted 6 times without the completion predicate turning true",
     Outcome.FAILED),
    ("error budget 3 reached", Outcome.FAILED),
    ("time budget 8 min reached", Outcome.TIMEOUT),
    ("battle budget 8 reached", Outcome.TIMEOUT),
    ("action budget 400 reached", Outcome.TIMEOUT),
    ("stop requested", Outcome.PAUSED),
    ("interrupted (SIGINT)", Outcome.PAUSED),
    ("blocked: no usable pet", Outcome.BLOCKED),
])
def test_classify_reason(reason, expected):
    assert classify_reason(reason) is expected


def test_unknown_reason_is_failure_not_success():
    # An unrecorded ending must never be assumed fine.
    assert classify_reason("unknown") is Outcome.FAILED
    assert classify_reason("") is Outcome.FAILED
    assert classify_reason(None) is Outcome.FAILED


def test_refusal_with_embedded_blocked_status_is_still_no_work():
    """Regression: a refusal embeds every skipped node's status.

    The live 2026-11-04 reason contained `status=blocked: missing capability
    'market.buy'`, which matched the blocked check and reported exit 7 (needs a
    human) instead of exit 3 (nothing to do).  The refusal must win.
    """

    reason = ("planner refused: nothing schedulable: "
              "puni.prepare.market_unlock: not ready - status=unknown; "
              "puni.roster.buy_salake: not ready - status=blocked: missing "
              "capability 'market.buy'; puni.seal.boss_pp_empty: not ready - "
              "status=not_executable: ... 35 more")
    assert classify_reason(reason) is Outcome.NO_WORK


def test_a_real_blocker_is_still_blocked():
    assert classify_reason("blocked: no usable pet") is Outcome.BLOCKED


# --------------------------------------------------------------------------- #
# the 2026-11-04 pathology
# --------------------------------------------------------------------------- #
def test_refused_batch_is_no_work_and_not_success():
    verdict = classify_run(_summary(
        stop_reason="planner refused: nothing schedulable: puni.prepare.market_unlock: "
                    "not ready - status=unknown",
        stats={"battles": 0, "actions": 0, "errors": 0, "loops": 2, "elapsed_s": 7.2},
    ))
    assert verdict.outcome is Outcome.NO_WORK
    assert verdict.exit_code == 3
    assert verdict.exit_code != 0
    assert verdict.effective is False
    assert verdict.verified_progress is False
    assert verdict.did_work is False


def test_single_stray_action_does_not_count_as_work():
    # The last spinning batch reported actions=1 and accomplished nothing.
    verdict = classify_run(_summary(
        stop_reason="planner refused: nothing schedulable: ...",
        stats={"battles": 0, "actions": 1, "errors": 0, "loops": 2, "elapsed_s": 37.4},
    ))
    assert verdict.effective is False


# --------------------------------------------------------------------------- #
# verified progress requires a proof
# --------------------------------------------------------------------------- #
def test_completed_node_with_proof_is_progress():
    verdict = classify_run(_summary(
        stop_reason="goal progressed: node completed",
        nodes={"puni.prepare.tower_farm": "completed"},
        proofs={"puni.prepare.tower_farm": "counter tower.floors_cleared=14 >= 1"},
        stats={"battles": 3, "actions": 40, "errors": 0, "loops": 5, "elapsed_s": 300},
    ))
    assert verdict.outcome is Outcome.SUCCESS
    assert verdict.verified_progress is True
    assert verdict.completed_nodes == ("puni.prepare.tower_farm",)


def test_completed_without_proof_is_not_evidence():
    # A status string alone is not evidence; the project rule is proof or nothing.
    verdict = classify_run(_summary(
        stop_reason="goal progressed: node completed",
        nodes={"puni.prepare.tower_farm": "completed"},
        proofs={},
    ))
    assert verdict.verified_progress is False


def test_battles_are_work_but_not_verified_progress():
    verdict = classify_run(_summary(
        stop_reason="battle budget 8 reached",
        stats={"battles": 8, "actions": 120, "errors": 0, "loops": 20, "elapsed_s": 480},
    ))
    assert verdict.outcome is Outcome.TIMEOUT
    assert verdict.did_work is True
    assert verdict.effective is True
    assert verdict.verified_progress is False


def test_missing_summary_is_failure():
    assert classify_run(None).outcome is Outcome.FAILED
    assert classify_run({}).outcome is Outcome.FAILED


# --------------------------------------------------------------------------- #
# exit codes are distinct and stable
# --------------------------------------------------------------------------- #
def test_exit_codes_are_distinct_where_it_matters():
    assert exit_code_for(Outcome.SUCCESS) == 0
    assert exit_code_for(Outcome.NO_WORK) == 3
    assert exit_code_for(Outcome.FAILED) == 1
    assert exit_code_for(Outcome.TIMEOUT) == 124
    assert len({EXIT_CODES[o] for o in (Outcome.SUCCESS, Outcome.NO_WORK,
                                        Outcome.FAILED, Outcome.TIMEOUT)}) == 4


def test_verdict_round_trips_to_dict():
    verdict = classify_run(_summary(stop_reason="planner refused: x"))
    payload = verdict.to_dict()
    assert payload["outcome"] == "NO_WORK"
    assert payload["exit_code"] == 3
    assert payload["effective"] is False
