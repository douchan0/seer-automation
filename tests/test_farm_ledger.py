"""Tests for the tower farming ledger — the part that must be right unattended.

The failure this locks down is the 2026-11-04 night: 26 batches, every one exiting 0,
no floor cleared, reported as healthy.  A farming loop must be able to say "that
round produced nothing" and count it.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from seerauto.farm.ledger import (
    ROUND_BLOCKED,
    ROUND_FAILED,
    ROUND_IDLE,
    ROUND_OK,
    ROUND_PAUSED,
    Guards,
    RoundRecord,
    TowerLedger,
)

T0 = datetime(2026, 11, 5, 23, 0, 0)


def _round(i=1, *, won=1, xp=1300.0, before=None, after=None, error="", notes=(),
           interrupted=False, blocked=False):
    r = RoundRecord(index=i, started_at=T0.isoformat(), floors_won=won,
                    xp_income=xp, tower_floor_before=before, tower_floor_after=after,
                    error=error, notes=list(notes), interrupted=interrupted,
                    blocked=blocked)
    return r


def test_a_win_is_progress_and_resets_streaks():
    led = TowerLedger(guards=Guards(max_no_work=2))
    led.add(_round(won=1, xp=1300))
    assert led.floors_won == 1
    assert led.xp_income == 1300
    assert led.no_work_streak == 0
    assert led.rounds[-1].outcome == ROUND_OK


def test_an_empty_round_counts_as_no_work():
    led = TowerLedger(guards=Guards(max_no_work=2))
    led.add(_round(won=0, xp=0))
    assert led.rounds[-1].outcome == ROUND_IDLE
    assert led.no_work_streak == 1


def test_two_empty_rounds_stop_the_loop():
    """The exact shape of the 2026-11-04 spin: nothing scheduled, reported as fine."""

    led = TowerLedger(guards=Guards(max_no_work=2))
    led.add(_round(won=0, xp=0))
    keep, why = led.should_continue(now=T0, deadline=None)
    assert keep, "one idle round is not proof of a stall"
    led.add(_round(won=0, xp=0))
    keep, why = led.should_continue(now=T0, deadline=None)
    assert not keep
    assert "no verified floor progress" in why


def test_a_failure_streak_stops_the_loop():
    led = TowerLedger(guards=Guards(max_failures=2, max_no_work=99))
    led.add(_round(won=0, xp=0, blocked=True,
                   notes=["guardian click did not start a battle"]))
    led.add(_round(won=0, xp=0, blocked=True,
                   notes=["guardian click did not start a battle"]))
    keep, why = led.should_continue(now=T0, deadline=None)
    assert not keep
    assert "failed" in why


def test_a_pause_is_not_a_failure_and_does_not_trip_guards():
    """「暂停，保存」 must never be counted as a bad round."""

    led = TowerLedger(guards=Guards(max_no_work=1, max_failures=1))
    led.add(_round(won=0, xp=0, interrupted=True))
    assert led.rounds[-1].outcome == ROUND_PAUSED
    assert led.no_work_streak == 0
    assert led.failure_streak == 0
    assert led.should_continue(now=T0, deadline=None)[0]


def test_a_blocked_round_carries_its_error():
    led = TowerLedger()
    led.add(_round(won=0, xp=0, error="client is not showing game content"))
    assert led.rounds[-1].outcome == ROUND_BLOCKED
    assert "blank" not in led.rounds[-1].outcome


def test_deadline_beats_everything():
    led = TowerLedger(guards=Guards(max_rounds=99))
    led.add(_round(won=3))
    keep, why = led.should_continue(now=T0 + timedelta(minutes=1),
                                   deadline=T0 + timedelta(minutes=1))
    assert not keep and "deadline" in why


def test_round_budget_stops():
    led = TowerLedger(guards=Guards(max_rounds=1))
    led.add(_round(won=1))
    keep, why = led.should_continue(now=T0, deadline=None)
    assert not keep and "round budget" in why


def test_time_budget_is_measured_from_the_ledger_start():
    led = TowerLedger(guards=Guards(max_minutes=10))
    led.started_at = T0
    keep, _ = led.should_continue(now=T0 + timedelta(minutes=9), deadline=None)
    assert keep
    keep, why = led.should_continue(now=T0 + timedelta(minutes=11), deadline=None)
    assert not keep and "time budget" in why


def test_floors_gained_needs_both_readings():
    r = _round(before=14, after=17)
    assert r.floors_gained == 3
    assert _round(before=None, after=17).floors_gained is None


def test_spend_is_summed_per_currency():
    led = TowerLedger()
    a = _round(won=1)
    a.spend = {"赛尔豆": 100.0}
    b = _round(won=1)
    b.spend = {"赛尔豆": 50.0, "XIN豆": 5.0}
    led.add(a)
    led.add(b)
    assert led.spend_total() == {"赛尔豆": 150.0, "XIN豆": 5.0}


def test_summary_is_json_serialisable():
    import json

    led = TowerLedger(guards=Guards(max_rounds=2))
    led.started_at = T0
    led.add(_round(won=1, before=14, after=15))
    json.dumps(led.summary())  # must not raise


def test_a_paid_win_still_resets_the_idle_streak():
    led = TowerLedger(guards=Guards(max_no_work=1))
    led.add(_round(won=0, xp=0))
    assert led.no_work_streak == 1
    led.add(_round(won=1, xp=0))
    assert led.no_work_streak == 0


def test_a_diagnostic_note_does_not_turn_no_work_into_a_failure():
    """The tool appends a frame path to every round; that must not be a classification."""

    r = _round(won=0, xp=0, notes=["frame data/overnight/frames/round_001.png"])
    assert r.outcome == ROUND_IDLE
    assert r.outcome != ROUND_FAILED


def test_a_refusal_is_blocked_not_idle():
    r = _round(won=0, xp=0, blocked=True, error="farm_tower BLOCKED: input disabled")
    assert r.outcome == ROUND_BLOCKED


def test_a_paused_round_is_never_counted_as_no_work():
    led = TowerLedger(guards=Guards(max_no_work=1))
    led.add(_round(won=0, xp=0, interrupted=True))
    assert led.no_work_streak == 0
