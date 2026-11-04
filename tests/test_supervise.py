"""Tests for the long-horizon supervisor's accounting and stop guards.

`tools/supervise.py` is a script, not a package module, so it is loaded by path.
Nothing here touches the game: the guards are pure functions of a ledger, which is
exactly why they can be tested at all.

The headline regression is `test_twenty_six_empty_batches_stop_after_two`: replay the
2026-11-04 failure — 26 batches, every one refusing to schedule anything, every one
exiting 0 — and assert the supervisor now stops after two instead of burning the
night.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from seerauto.runner.outcome import Outcome

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("supervise_tool",
                                               _ROOT / "tools" / "supervise.py")
sup = importlib.util.module_from_spec(_SPEC)
sys.modules["supervise_tool"] = sup
_SPEC.loader.exec_module(sup)

NOW = datetime(2026, 11, 4, 23, 40, 0)
DEADLINE = datetime(2026, 11, 5, 6, 30, 0)


def _verdict(outcome, *, battles=0, actions=0, reason="", completed=()):
    return SimpleNamespace(outcome=outcome, battles=battles, actions=actions,
                           errors=0, loops=1, elapsed_s=1.0, reason=reason,
                           completed_nodes=tuple(completed))


# --------------------------------------------------------------------------- #
# parsing the batch's own report
# --------------------------------------------------------------------------- #
def test_parse_outcome_reads_the_cli_line():
    out = ("goal start: goal=puni\n"
           "stats   battles=0 actions=0\n"
           "outcome NO_WORK (exit 3): planner refused: nothing schedulable: x\n")
    outcome, reason = sup.parse_outcome(out)
    assert outcome is Outcome.NO_WORK
    assert reason == "planner refused: nothing schedulable: x"


def test_parse_outcome_absent_line_returns_none():
    assert sup.parse_outcome("nothing useful here") == (None, "")
    assert sup.parse_outcome("") == (None, "")


def test_verdict_from_batch_falls_back_to_exit_code():
    v = sup.verdict_from_batch(3, "no outcome line", new_nodes=[], stats={})
    assert v.outcome is Outcome.NO_WORK
    v = sup.verdict_from_batch(124, "", new_nodes=[], stats={})
    assert v.outcome is Outcome.TIMEOUT
    v = sup.verdict_from_batch(5, "", new_nodes=[], stats={})
    assert v.outcome is Outcome.FAILED


def test_verdict_from_batch_prefers_the_reported_outcome():
    v = sup.verdict_from_batch(0, "outcome SUCCESS (exit 0): goal progressed: node completed",
                               new_nodes=["puni.prepare.tower_farm"], stats={"battles": 3})
    assert v.outcome is Outcome.SUCCESS
    assert v.battles == 3


# --------------------------------------------------------------------------- #
# ledger accounting
# --------------------------------------------------------------------------- #
def test_ledger_counts_no_work_and_resets_streak_on_work():
    ledger = sup.Ledger()
    ledger.record(_verdict(Outcome.NO_WORK, reason="refused"), new_nodes=[], now=NOW)
    ledger.record(_verdict(Outcome.NO_WORK, reason="refused"), new_nodes=[], now=NOW)
    assert ledger.no_work_batches == 2
    assert ledger.consecutive_no_work == 2
    assert ledger.effective_batches == 0

    ledger.record(_verdict(Outcome.TIMEOUT, battles=8), new_nodes=[], now=NOW)
    assert ledger.consecutive_no_work == 0
    assert ledger.effective_batches == 1
    assert ledger.total_battles == 8


def test_ledger_counts_verified_progress_once_per_node():
    ledger = sup.Ledger()
    ledger.record(_verdict(Outcome.SUCCESS), new_nodes=["a"], now=NOW)
    ledger.record(_verdict(Outcome.SUCCESS), new_nodes=["a"], now=NOW)
    assert ledger.verified_progress == 2          # two verified transitions recorded
    assert ledger.completed_nodes == ["a"]        # but the node set stays unique


def test_ledger_tracks_failure_streak():
    ledger = sup.Ledger()
    for _ in range(3):
        ledger.record(_verdict(Outcome.FAILED, reason="crash"), new_nodes=[], now=NOW)
    assert ledger.failed_batches == 3
    assert ledger.consecutive_failures == 3


def test_ledger_paused_and_blocked_do_not_count_as_failures():
    ledger = sup.Ledger()
    ledger.record(_verdict(Outcome.PAUSED), new_nodes=[], now=NOW)
    ledger.record(_verdict(Outcome.BLOCKED), new_nodes=[], now=NOW)
    assert ledger.consecutive_failures == 0
    assert ledger.paused_batches == 1
    assert ledger.blocked_batches == 1


# --------------------------------------------------------------------------- #
# stop guards
# --------------------------------------------------------------------------- #
def test_deadline_beats_everything():
    ledger = sup.Ledger(batches=99, consecutive_no_work=9)
    action, why = sup.next_action(ledger, sup.Guards(), now=DEADLINE, deadline=DEADLINE)
    assert action == "stop"
    assert "deadline" in why


def test_no_work_streak_stops_the_loop():
    guards = sup.Guards(max_no_work_streak=2)
    ledger = sup.Ledger(consecutive_no_work=2, last_stop_reason="planner refused: x")
    action, why = sup.next_action(ledger, guards, now=NOW, deadline=DEADLINE)
    assert action == "stop"
    assert "no schedulable work" in why
    assert "planner refused" in why          # the reason is carried, not lost


def test_failure_streak_stops_the_loop():
    guards = sup.Guards(max_failures=3)
    ledger = sup.Ledger(consecutive_failures=3, last_stop_reason="crash")
    action, why = sup.next_action(ledger, guards, now=NOW, deadline=DEADLINE)
    assert action == "stop"
    assert "consecutive failures" in why


def test_idle_guard_stops_a_run_that_stopped_progressing():
    guards = sup.Guards(max_idle_minutes=90)
    ledger = sup.Ledger(batches=10,
                        last_progress_at=(NOW - timedelta(minutes=120)).isoformat())
    action, why = sup.next_action(ledger, guards, now=NOW, deadline=DEADLINE)
    assert action == "stop"
    assert "without an effective batch" in why


def test_idle_guard_does_not_fire_while_work_continues():
    guards = sup.Guards(max_idle_minutes=90)
    ledger = sup.Ledger(batches=10,
                        last_progress_at=(NOW - timedelta(minutes=5)).isoformat())
    action, _ = sup.next_action(ledger, guards, now=NOW, deadline=DEADLINE)
    assert action == "continue"


def test_never_effective_run_stops_eventually():
    guards = sup.Guards(max_idle_minutes=90)
    ledger = sup.Ledger(batches=3)          # no progress timestamp at all
    action, why = sup.next_action(ledger, guards, now=NOW, deadline=DEADLINE)
    assert action == "stop"
    assert "not one was effective" in why


def test_batch_cap_is_respected():
    guards = sup.Guards(max_batches=5)
    ledger = sup.Ledger(batches=5)
    action, why = sup.next_action(ledger, guards, now=NOW, deadline=DEADLINE)
    assert action == "stop"
    assert "batch cap" in why


def test_healthy_state_continues():
    action, why = sup.next_action(sup.Ledger(batches=1), sup.Guards(),
                                  now=NOW, deadline=DEADLINE)
    assert action == "continue"
    assert why is None


# --------------------------------------------------------------------------- #
# the regression this file exists for
# --------------------------------------------------------------------------- #
def test_twenty_six_empty_batches_stop_after_two():
    """Replay 2026-11-04: 26 batches, all exit 0, all battles=0, all refused."""

    guards = sup.Guards()                     # defaults: no_work streak of 2
    ledger = sup.Ledger()
    ran = 0
    for _ in range(26):
        action, _why = sup.next_action(ledger, guards, now=NOW, deadline=DEADLINE)
        if action == "stop":
            break
        verdict = sup.verdict_from_batch(
            0,
            "outcome NO_WORK (exit 3): planner refused: nothing schedulable: ...",
            new_nodes=[],
            stats={"battles": 0, "actions": 0, "errors": 0, "loops": 2,
                   "elapsed_s": 7.2},
        )
        ledger.record(verdict, new_nodes=[], now=NOW)
        ran += 1

    assert ran == 2, f"expected the loop to stop after 2 empty batches, ran {ran}"
    assert ledger.batches == 2
    assert ledger.no_work_batches == 2
    assert ledger.effective_batches == 0
    action, why = sup.next_action(ledger, guards, now=NOW, deadline=DEADLINE)
    assert action == "stop"
    assert "no schedulable work" in why


def test_a_productive_run_is_not_stopped_by_the_no_work_guard():
    guards = sup.Guards()
    ledger = sup.Ledger()
    for _ in range(6):
        verdict = sup.verdict_from_batch(124, "", new_nodes=[], stats={"battles": 8})
        ledger.record(verdict, new_nodes=[], now=NOW)
        action, _ = sup.next_action(ledger, guards, now=NOW, deadline=DEADLINE)
        assert action == "continue"
    assert ledger.effective_batches == 6


# --------------------------------------------------------------------------- #
# before/after diff
# --------------------------------------------------------------------------- #
def test_diff_snapshots_reports_new_nodes_and_deltas():
    before = {"run_id": "a", "status": "running", "stats": {"battles": 2, "actions": 10},
              "completed": ["puni.x"]}
    after = {"run_id": "b", "status": "finished", "stats": {"battles": 7, "actions": 40},
             "completed": ["puni.x", "puni.y"], "stop_reason": "battle budget 8 reached"}
    diff = sup.diff_snapshots(before, after)
    assert diff["new_completed_nodes"] == ["puni.y"]
    assert diff["battles_delta"] == 5
    assert diff["actions_delta"] == 30
    assert diff["run_changed"] is True
    assert diff["after_stop_reason"] == "battle budget 8 reached"


def test_diff_snapshots_tolerates_missing_state():
    diff = sup.diff_snapshots({}, {})
    assert diff["new_completed_nodes"] == []
    assert diff["battles_delta"] == 0


# --------------------------------------------------------------------------- #
# the whole loop, with the subprocess and the store stubbed out
# --------------------------------------------------------------------------- #
def _patch_loop(monkeypatch, tmp_path, results):
    """Stub the game and the store; return the list of commands that ran."""

    monkeypatch.setattr(sup, "LOG_PATH", str(tmp_path / "supervisor.log"))
    monkeypatch.setattr(sup, "REPORT", str(tmp_path / "supervisor.json"))
    monkeypatch.setattr(sup, "ensure_in_game", lambda: True)
    monkeypatch.setattr(sup, "heal_if_needed", lambda **kw: True)
    monkeypatch.setattr(sup, "snapshot", lambda: {})
    calls = []

    def fake_run(cmd, timeout):
        calls.append(list(cmd))
        return results.pop(0) if results else (3, "outcome NO_WORK (exit 3): nothing")

    monkeypatch.setattr(sup, "run", fake_run)
    return calls


def test_main_stops_after_two_no_work_batches(monkeypatch, tmp_path):
    calls = _patch_loop(monkeypatch, tmp_path, [
        (3, "outcome NO_WORK (exit 3): planner refused: nothing schedulable"),
        (3, "outcome NO_WORK (exit 3): planner refused: nothing schedulable"),
        (3, "outcome NO_WORK (exit 3): planner refused: nothing schedulable"),
    ])
    code = sup.main(["--dry-run", "--minutes", "30", "--batch-minutes", "1",
                     "--batch-battles", "2", "--max-no-work", "2"])
    assert code == 3                      # NO_WORK, not success
    assert len(calls) == 2                # stopped after the second empty batch
    import json
    report = json.loads((tmp_path / "supervisor.json").read_text())
    assert report["ledger"]["batches"] == 2          # counted once each, not twice
    assert report["ledger"]["no_work_batches"] == 2
    assert report["ledger"]["effective_batches"] == 0
    assert "no schedulable work" in (report["stop_reason"] or "")


def test_main_keeps_going_while_batches_are_productive(monkeypatch, tmp_path):
    _patch_loop(monkeypatch, tmp_path, [
        (124, "outcome TIMEOUT (exit 124): battle budget 2 reached"),
        (124, "outcome TIMEOUT (exit 124): battle budget 2 reached"),
        (124, "outcome TIMEOUT (exit 124): battle budget 2 reached"),
    ])
    monkeypatch.setattr(sup, "snapshot",
                        lambda: {"stats": {"battles": 2, "actions": 30},
                                 "completed": []})
    code = sup.main(["--dry-run", "--minutes", "30", "--batch-minutes", "1",
                     "--batch-battles", "2", "--max-batches", "3"])
    assert code == 0                       # real work happened
    import json
    report = json.loads((tmp_path / "supervisor.json").read_text())
    assert report["ledger"]["batches"] == 3
    assert report["ledger"]["effective_batches"] == 3
    assert report["ledger"]["total_battles"] == 6


def test_main_stops_on_a_hard_blocker(monkeypatch, tmp_path):
    _patch_loop(monkeypatch, tmp_path, [
        (7, "outcome BLOCKED (exit 7): blocked: no usable pet"),
        (3, "outcome NO_WORK (exit 3): should never run"),
    ])
    code = sup.main(["--dry-run", "--minutes", "30", "--batch-minutes", "1",
                     "--batch-battles", "2"])
    assert code == 7                       # a hard blocker is its own exit code
    import json
    report = json.loads((tmp_path / "supervisor.json").read_text())
    assert report["ledger"]["batches"] == 1   # the blocker stopped it immediately
    assert "blocked" in (report["stop_reason"] or "")


# --------------------------------------------------------------------------- #
# regressions found by the 2026-11-05 independent review
# --------------------------------------------------------------------------- #
def test_a_completed_node_without_a_proof_is_not_verified_progress():
    """Blocker: the supervisor and outcome.classify_run disagreed on "completed".

    A bare `completed` status is a claim, not evidence (AGENTS.md 5.3).  The
    supervisor used to take it at face value and count it as verified progress.
    """

    from seerauto.runner.outcome import completed_with_proof

    nodes = {"puni.a": "completed", "puni.b": "completed"}
    proofs = {"puni.a": "counter x=1 (LIVE)"}
    assert completed_with_proof(nodes, proofs) == ("puni.a",)
    assert completed_with_proof(nodes, {}) == ()
    assert completed_with_proof({}, {}) == ()


def test_exit_zero_without_an_outcome_line_is_failed():
    """Blocker: a missing outcome line used to be read as SUCCESS."""

    v = sup.verdict_from_batch(0, "no outcome line here", new_nodes=[], stats={})
    assert v.outcome is Outcome.FAILED
    assert "cannot establish" in v.reason


def test_lock_held_and_cannot_start_are_failures_not_success():
    assert sup.verdict_from_batch(4, "", new_nodes=[], stats={}).outcome is Outcome.FAILED
    assert sup.verdict_from_batch(5, "", new_nodes=[], stats={}).outcome is Outcome.FAILED


def test_negative_stats_are_clamped():
    v = sup.verdict_from_batch(124, "", new_nodes=[], stats={"battles": -3, "actions": -1})
    assert v.battles == 0
    assert v.actions == 0


def test_paused_has_its_own_exit_code():
    from seerauto.runner.outcome import EXIT_CODES
    assert EXIT_CODES[Outcome.PAUSED] != EXIT_CODES[Outcome.SUCCESS]
    assert EXIT_CODES[Outcome.PAUSED] == 130


def test_min_battles_threshold_is_honoured():
    ledger = sup.Ledger()
    guards = sup.Guards(min_battles_for_work=3)
    ledger.record(_verdict(Outcome.TIMEOUT, battles=1), new_nodes=[], now=NOW,
                  min_battles=guards.min_battles_for_work)
    assert ledger.effective_batches == 0          # 1 battle is below the threshold
    ledger.record(_verdict(Outcome.TIMEOUT, battles=3), new_nodes=[], now=NOW,
                  min_battles=guards.min_battles_for_work)
    assert ledger.effective_batches == 1


def test_a_batch_that_started_no_run_does_not_inherit_battles(monkeypatch, tmp_path):
    """Major: `after.stats` belongs to the previous run when the batch never started one.

    A failed batch (lock held / cannot start) used to inherit the last run's battle
    count, look "effective", and disarm the idle guard.
    """

    import json

    monkeypatch.setattr(sup, "LOG_PATH", str(tmp_path / "s.log"))
    monkeypatch.setattr(sup, "REPORT", str(tmp_path / "s.json"))
    monkeypatch.setattr(sup, "ensure_in_game", lambda: True)
    monkeypatch.setattr(sup, "heal_if_needed", lambda **kw: True)
    monkeypatch.setattr(sup, "run", lambda cmd, timeout: (4, ""))   # lock held

    prev = {"run_id": "old", "stats": {"battles": 9, "actions": 99}, "completed": []}
    same = {"run_id": "old", "stats": {"battles": 9, "actions": 99}, "completed": []}
    monkeypatch.setattr(sup, "snapshot", lambda: same)

    code = sup.main(["--dry-run", "--minutes", "30", "--batch-minutes", "1",
                     "--batch-battles", "2", "--max-batches", "1"])
    report = json.loads((tmp_path / "s.json").read_text())
    assert report["ledger"]["total_battles"] == 0     # did NOT inherit 9
    assert report["ledger"]["effective_batches"] == 0
    assert code != 0


def test_yaml_has_no_duplicate_node_keys():
    """The review found two `notes:` on buy_potions; yaml silently keeps the last."""

    import re
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "seerauto" / "goals" / "puni.yaml"
    node = None
    seen: dict[str, int] = {}
    dupes = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = re.match(r"^  - id: (\S+)", line)
        if m:
            node, seen = m.group(1), {}
            continue
        m = re.match(r"^    ([A-Za-z_]\w*):", line)
        if m and node:
            key = m.group(1)
            if key in seen:
                dupes.append((node, key, seen[key], i))
            else:
                seen[key] = i
    assert dupes == [], f"duplicate keys (node, key, first_line, dup_line): {dupes}"
