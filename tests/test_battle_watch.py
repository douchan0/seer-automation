"""Tests for the long-battle watchdog.

The gap it closes: `game.run_battle` had a *time* budget but no turn budget, no way
to tell a slow fight from a stuck one, and no per-turn record — so a 谱尼真身 run
that died at round 400 of 600 had nothing to resume from.

These are pure tests: the watchdog takes observations and returns decisions, which
is precisely why the long-battle logic can be verified without a game client.
"""

from __future__ import annotations

import json

import pytest

from seerauto.runner.battle_watch import (
    BattleLimits,
    BattleSnapshot,
    BattleWatchdog,
    append_snapshot,
    last_snapshot,
    load_snapshots,
)


def snap(turn, *, hp=(361, 361), life=None, log="sig-1", scene="battle",
         wins=0, losses=0, at=None):
    kwargs = {} if at is None else {"at": at}
    return BattleSnapshot(turn=turn, scene=scene, ally_hp=hp, enemy_life=life,
                          log_sig=log, wins=wins, losses=losses, **kwargs)


# --------------------------------------------------------------------------- #
# budgets
# --------------------------------------------------------------------------- #
def test_no_limits_never_stops():
    w = BattleWatchdog()
    for i in range(200):
        assert not w.observe(snap(i, hp=(300, 361), log=f"sig-{i}")).stop
    assert w.turns == 200


def test_turn_budget_stops_a_truebody_that_would_run_forever():
    w = BattleWatchdog(BattleLimits(max_turns=400))
    verdict = None
    for i in range(1000):
        verdict = w.observe(snap(i, hp=(300, 361), log=f"sig-{i}"))
        if verdict.stop:
            break
    assert verdict.stop
    assert "turn budget 400" in verdict.reason
    assert w.turns == 400          # the budget is inclusive: at most 400 turns


def test_time_budget():
    w = BattleWatchdog(BattleLimits(max_seconds=10), started=1000.0)
    assert not w.observe(snap(1, at=1005.0)).stop
    v = w.observe(snap(2, at=1012.0))
    assert v.stop and "time budget" in v.reason


def test_failure_budget():
    w = BattleWatchdog(BattleLimits(max_failures=2))
    w.note_failure("lost one")
    assert not w.observe(snap(1)).stop
    w.note_failure("lost two")
    v = w.observe(snap(2))
    assert v.stop and "fights lost" in v.reason


# --------------------------------------------------------------------------- #
# stuck detection
# --------------------------------------------------------------------------- #
def test_identical_turns_are_detected_as_stuck():
    w = BattleWatchdog(BattleLimits(max_stuck_turns=4))
    verdict = None
    for i in range(20):
        verdict = w.observe(snap(i, hp=(200, 361), log="same"))
        if verdict.stop:
            break
    assert verdict.stop
    assert "no progress" in verdict.reason
    assert w.turns == 5            # first observation establishes the fingerprint


def test_changing_hp_counts_as_progress():
    w = BattleWatchdog(BattleLimits(max_stuck_turns=3))
    for i in range(50):
        hp = (361 - i, 361)
        assert not w.observe(snap(i, hp=hp, log="same")).stop


def test_changing_battle_log_counts_as_progress():
    """The log changes even when HP does not (buff/status messages)."""

    w = BattleWatchdog(BattleLimits(max_stuck_turns=3))
    for i in range(50):
        assert not w.observe(snap(i, hp=(361, 361), log=f"turn-{i}")).stop


def test_turn_counter_alone_does_not_count_as_progress():
    """The regression guard: `turn` must NOT be part of the fingerprint."""

    w = BattleWatchdog(BattleLimits(max_stuck_turns=3))
    stopped_at = None
    for i in range(10):
        if w.observe(snap(i, hp=(361, 361), log="frozen")).stop:
            stopped_at = i
            break
    assert stopped_at is not None and stopped_at <= 5


def test_stuck_counter_resets_after_real_progress():
    w = BattleWatchdog(BattleLimits(max_stuck_turns=3))
    w.observe(snap(0, hp=(300, 361), log="a"))
    w.observe(snap(1, hp=(300, 361), log="a"))     # stuck 1
    w.observe(snap(2, hp=(280, 361), log="a"))     # progress -> reset
    w.observe(snap(3, hp=(280, 361), log="a"))     # stuck 1
    assert not w.observe(snap(4, hp=(280, 361), log="a")).stop   # stuck 2
    assert w.summary()["stuck_turns"] == 2


# --------------------------------------------------------------------------- #
# snapshots
# --------------------------------------------------------------------------- #
def test_snapshot_fingerprint_excludes_turn():
    a = snap(1, hp=(10, 20), log="x")
    b = snap(99, hp=(10, 20), log="x")
    assert a.fingerprint == b.fingerprint


def test_snapshot_to_dict_is_json_safe():
    row = snap(3, hp=(100, 200), life=2).to_dict()
    assert row["ally_hp"] == [100, 200]
    assert row["enemy_life"] == 2
    json.dumps(row)                      # must not raise


def test_summary_reports_the_last_turn():
    w = BattleWatchdog()
    w.observe(snap(1, hp=(361, 361), life=1))
    w.observe(snap(2, hp=(300, 361), life=2))
    s = w.summary()
    assert s["turns"] == 2
    assert s["last_ally_hp"] == (300, 361)
    assert s["last_enemy_life"] == 2


# --------------------------------------------------------------------------- #
# the audit log / resume point
# --------------------------------------------------------------------------- #
def test_append_and_read_back(tmp_path):
    path = str(tmp_path / "battle.jsonl")
    w = BattleWatchdog()
    for i in range(3):
        s = snap(i, hp=(361 - i, 361), life=1)
        w.observe(s)
        append_snapshot(path, s)

    rows = load_snapshots(path)
    assert len(rows) == 3
    assert rows[-1]["turn"] == 2
    assert last_snapshot(path)["turn"] == 2


def test_missing_log_is_not_an_error(tmp_path):
    assert load_snapshots(str(tmp_path / "nope.jsonl")) == []
    assert last_snapshot(str(tmp_path / "nope.jsonl")) is None


def test_logging_failure_never_raises_into_the_battle(tmp_path):
    # A path whose parent is a file, not a directory: makedirs must fail.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    append_snapshot(str(blocker / "sub" / "battle.jsonl"), snap(1))   # must not raise


def test_extra_fields_are_recorded(tmp_path):
    path = str(tmp_path / "battle.jsonl")
    append_snapshot(path, snap(7), extra={"run_id": "r1", "node": "puni.truebody.life_3_energy"})
    row = last_snapshot(path)
    assert row["run_id"] == "r1"
    assert row["node"] == "puni.truebody.life_3_energy"


# --------------------------------------------------------------------------- #
# the scenario this module exists for
# --------------------------------------------------------------------------- #
def test_a_realistic_truebody_fight_stops_at_the_budget_not_at_the_crash():
    """600 rounds of real progress must run to the budget, not be called stuck."""

    w = BattleWatchdog(BattleLimits(max_turns=600, max_stuck_turns=8))
    for i in range(1000):
        # HP oscillates, log changes every turn: a healthy, very long fight.
        hp = (200 + (i % 7), 361)
        v = w.observe(snap(i, hp=hp, log=f"round-{i}", life=3))
        if v.stop:
            break
    assert v.stop
    assert "turn budget" in v.reason
    assert "no progress" not in v.reason
