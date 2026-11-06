"""Tests for the five independent facts and the de-flapping local watchdog.

Two properties are pinned here, and they are the whole point of the module:

1. **The five facts stay separate.**  Changing exactly one evidence source must
   change exactly one fact.  If a goal being alive could ever imply "the executor is
   alive", the control plane would repeat the 2026-11-04 mistake of reporting health
   it never observed.
2. **The watchdog cannot flap.**  A wake fires once, then is *suppressed and counted*
   while its cooldown lasts, then fires again afterwards.  "Unknown" is never turned
   into "stalled" and "not observed" is never turned into "dead".

No test sleeps: every step takes an explicit ``now`` and a fake clock, and all fact
sources point at ``tmp_path`` so the repo's real runtime files are never involved.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

import pytest

from seerauto.dual.facts import (DECIDE_MAX_AGE_S, FactKind, FactSet, FactReading,
                                FactState, collect)
from seerauto.dual.ledger import Ledger, stamp
from seerauto.dual.state import NightState
from seerauto.dual.watchdog import (Watchdog, WatchdogConfig, WakeKind,
                                    evaluate_criteria, parse_deadline,
                                    remaining_seconds)

NOW = 1_800_000_000.0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _iso(epoch: float) -> str:
    return stamp(epoch)[0]


def _facts(**states: str) -> FactSet:
    readings = {}
    for kind in FactKind:
        value = FactState(states.get(kind.value, "UNKNOWN"))
        readings[kind] = FactReading(kind, value, f"test:{value.value}", "test")
    return FactSet(readings=readings, checked_epoch=NOW)


def _night(**kwargs) -> NightState:
    base = {"emitted_epoch": NOW}
    base.update(kwargs)
    return NightState(**base)


def _touch_at(path: Path, epoch: float) -> None:
    """Give a file a deterministic mtime so fact ages do not depend on wall time."""

    os.utime(path, (epoch, epoch))


def _collect(tmp_path: Path, **kwargs):
    ledger = kwargs.pop("ledger", None) or Ledger(tmp_path, fsync=False)
    now = kwargs.pop("now", NOW)
    checkpoint = kwargs.pop("checkpoint", None)
    return collect(ledger=ledger, checkpoint=checkpoint, now=now,
                   agent_turn_file=kwargs.pop("agent_turn_file",
                                              tmp_path / "agent_turn.json"),
                   executor_heartbeat_file=kwargs.pop(
                       "executor_heartbeat_file", tmp_path / "hb.json"),
                   supervisor_state_file=kwargs.pop("supervisor_state_file",
                                                    tmp_path / "supervisor.json"),
                   supervisor_log_file=kwargs.pop("supervisor_log_file",
                                                  tmp_path / "supervisor.log"),
                   wild_cycle_ledger=kwargs.pop("wild_cycle_ledger",
                                                tmp_path / "wild.jsonl"),
                   lock_file=kwargs.pop("lock_file", tmp_path / "lock"),
                   **kwargs)


# --------------------------------------------------------------------------- #
# each fact from its own source, and UNKNOWN when there is no source
# --------------------------------------------------------------------------- #
def test_goal_alive_reads_the_checkpoint_declaration(tmp_path):
    assert _collect(tmp_path).state(FactKind.GOAL_ALIVE) is FactState.UNKNOWN
    active = _collect(tmp_path, checkpoint={"goal": {"status": "active"}})
    assert active.state(FactKind.GOAL_ALIVE) is FactState.TRUE
    paused = _collect(tmp_path, checkpoint={"goal": {"status": "paused"}})
    assert paused.state(FactKind.GOAL_ALIVE) is FactState.FALSE
    weird = _collect(tmp_path, checkpoint={"goal": {"status": "???"}})
    assert weird.state(FactKind.GOAL_ALIVE) is FactState.UNKNOWN


def test_agent_deciding_requires_a_fresh_explicit_marker(tmp_path):
    marker = tmp_path / "agent_turn.json"
    assert _collect(tmp_path).state(FactKind.AGENT_DECIDING) is FactState.UNKNOWN

    marker.write_text(json.dumps({"state": "deciding", "ts_epoch": NOW - 5}),
                      encoding="utf-8")
    assert _collect(tmp_path).state(FactKind.AGENT_DECIDING) is FactState.TRUE

    marker.write_text(json.dumps({"state": "idle", "ts_epoch": NOW - 5}),
                      encoding="utf-8")
    assert _collect(tmp_path).state(FactKind.AGENT_DECIDING) is FactState.FALSE

    # A stale "deciding" marker cannot prove a turn is still running.
    marker.write_text(json.dumps({"state": "deciding",
                                  "ts_epoch": NOW - DECIDE_MAX_AGE_S - 60}),
                      encoding="utf-8")
    reading = _collect(tmp_path).get(FactKind.AGENT_DECIDING)
    assert reading.state is FactState.UNKNOWN
    assert "may have died" in reading.evidence


def test_supervisor_running_prefers_ps_then_log_then_exit_report(tmp_path):
    probe_match = lambda: ["123 tools/supervise.py --minutes 60"]  # noqa: E731
    reading = _collect(tmp_path, process_probe=probe_match).get(
        FactKind.SUPERVISOR_RUNNING)
    assert reading.state is FactState.TRUE and reading.source == "ps"

    reading = _collect(tmp_path, process_probe=lambda: []).get(
        FactKind.SUPERVISOR_RUNNING)
    assert reading.state is FactState.FALSE and reading.source == "ps"

    # A probe that cannot run (the workspace sandbox blocks ps with EPERM) must fall
    # back to file evidence instead of concluding "not running".
    def blocked() -> list[str]:
        raise PermissionError("Operation not permitted: 'ps'")

    log = tmp_path / "supervisor.log"
    log.write_text("11-06 01:20:00 supervisor start\n"
                   "11-06 01:21:28 supervisor done: 3 batches\n", encoding="utf-8")
    _touch_at(log, NOW - 60)
    reading = _collect(tmp_path, process_probe=blocked).get(
        FactKind.SUPERVISOR_RUNNING)
    assert reading.state is FactState.FALSE
    assert reading.source == "supervisor.log"
    assert "probe unavailable" in reading.evidence

    # A fresh log without a "done" line is a heartbeat.
    log.write_text("11-06 02:00:00 supervisor start\n11-06 02:10:00 batch 1 ok\n",
                   encoding="utf-8")
    _touch_at(log, NOW - 60)
    reading = _collect(tmp_path, process_probe=None).get(FactKind.SUPERVISOR_RUNNING)
    assert reading.state is FactState.TRUE and reading.source == "supervisor.log"

    # Nothing at all -> UNKNOWN, not FALSE.
    (tmp_path / "supervisor.log").unlink()
    reading = _collect(tmp_path).get(FactKind.SUPERVISOR_RUNNING)
    assert reading.state is FactState.UNKNOWN


def test_supervisor_terminal_report_is_a_negative_observation(tmp_path):
    (tmp_path / "supervisor.json").write_text(json.dumps({
        "stopped_at": "2026-11-06T01:21:28", "stop_reason": "3 failures"},
        ), encoding="utf-8")
    reading = _collect(tmp_path).get(FactKind.SUPERVISOR_RUNNING)
    assert reading.state is FactState.FALSE
    assert "stopped at" in reading.evidence


def test_executor_liveness_from_heartbeat_lock_and_ledger(tmp_path):
    hb = tmp_path / "hb.json"
    assert _collect(tmp_path).state(FactKind.EXECUTOR_ALIVE) is FactState.UNKNOWN

    hb.write_text(json.dumps({"ts_epoch": NOW - 5, "pid": 42}), encoding="utf-8")
    reading = _collect(tmp_path).get(FactKind.EXECUTOR_ALIVE)
    assert reading.state is FactState.TRUE and reading.source == "heartbeat"
    assert reading.age_s == pytest.approx(5.0)

    hb.write_text(json.dumps({"ts_epoch": NOW - 600, "pid": 42}), encoding="utf-8")
    assert _collect(tmp_path).state(FactKind.EXECUTOR_ALIVE) is FactState.FALSE

    # A held executor lock is live truth even without a heartbeat.
    hb.unlink()
    lock_path = tmp_path / "lock"
    lock_path.write_text("pid=1\n", encoding="utf-8")
    with lock_path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        reading = _collect(tmp_path).get(FactKind.EXECUTOR_ALIVE)
        assert reading.state is FactState.TRUE and reading.source == "lock"
    # Released: the file still exists, but nothing holds it -> explicit FALSE.
    assert _collect(tmp_path).state(FactKind.EXECUTOR_ALIVE) is FactState.FALSE

    # wild_cycle's own ledger is the third source (it takes no lock).
    lock_path.unlink()
    wild = tmp_path / "wild.jsonl"
    wild.write_text(json.dumps({"pass": 1, "at": _iso(NOW - 10),
                                "outcome": "PROGRESS"}) + "\n", encoding="utf-8")
    reading = _collect(tmp_path).get(FactKind.EXECUTOR_ALIVE)
    assert reading.state is FactState.TRUE and reading.source == "ledger"


def test_game_progressing_requires_verified_progress(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    # Nothing recorded at all -> UNKNOWN: nothing to be stalled against.
    assert _collect(tmp_path, ledger=ledger).state(
        FactKind.GAME_PROGRESSING) is FactState.UNKNOWN

    # A session exists but no batch/proof -> FALSE (the recording path is live).
    started = _collect(tmp_path, ledger=ledger,
                       checkpoint={"session_started_ts": _iso(NOW - 60)})
    assert started.state(FactKind.GAME_PROGRESSING) is FactState.FALSE

    # A batch record without proof is still not progress.
    ledger.append("events", {"kind": "batch.end", "outcome": "UNVERIFIED_ACTIVITY",
                             "actions": 1, "battles": 0}, ts=NOW - 30)
    assert _collect(tmp_path, ledger=ledger).state(
        FactKind.GAME_PROGRESSING) is FactState.FALSE

    # A proof-backed milestone is progress.
    ledger.append("milestones", {"kind": "progress", "node": "wild_45",
                                 "proof": "Lv41"}, ts=NOW - 20)
    reading = _collect(tmp_path, ledger=ledger).get(FactKind.GAME_PROGRESSING)
    assert reading.state is FactState.TRUE
    assert reading.age_s == pytest.approx(20.0)
    assert "Lv41" in reading.evidence

    # Old proof is not "currently progressing" (separate night, so the newer proof
    # above cannot mask it — out-of-order writes must not move the baseline back).
    old_dir = tmp_path / "old"
    old_ledger = Ledger(old_dir, fsync=False)
    old_ledger.append("milestones", {"kind": "progress", "node": "wild_46",
                                     "proof": "Lv42"}, ts=NOW - 5000)
    assert _collect(old_dir, ledger=old_ledger).state(
        FactKind.GAME_PROGRESSING) is FactState.FALSE


# --------------------------------------------------------------------------- #
# the five facts are independent: flip one source, only one fact changes
# --------------------------------------------------------------------------- #
def test_changing_one_evidence_source_changes_exactly_one_fact(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    marker = tmp_path / "agent_turn.json"
    hb = tmp_path / "hb.json"
    log = tmp_path / "supervisor.log"

    def write_sources(*, agent="deciding", hb_age=5.0, log_done=False,
                      goal="active", probe=None) -> None:
        marker.write_text(json.dumps({"state": agent, "ts_epoch": NOW - 5}),
                          encoding="utf-8")
        hb.write_text(json.dumps({"ts_epoch": NOW - hb_age, "pid": 1}),
                      encoding="utf-8")
        log.write_text("11-06 02:00:00 supervisor start\n"
                       + ("11-06 02:05:00 supervisor done\n" if log_done
                          else "11-06 02:05:00 batch ok\n"), encoding="utf-8")
        _touch_at(log, NOW - 60)
        return probe

    checkpoint = {"goal": {"status": "active"},
                  "session_started_ts": _iso(NOW - 60)}

    def states(*, goal="active", probe=None, checkpoint=None):
        write_sources(goal=goal)
        return _collect(tmp_path, ledger=ledger,
                        checkpoint=checkpoint or {"goal": {"status": goal},
                                                  "session_started_ts":
                                                      _iso(NOW - 60)},
                        process_probe=probe or (lambda: ["pid 1 tools/supervise.py"])
                        ).compact()

    baseline = states()
    assert baseline == {
        "GOAL_ALIVE": "TRUE",
        "AGENT_DECIDING": "TRUE",
        "SUPERVISOR_RUNNING": "TRUE",
        "EXECUTOR_ALIVE": "TRUE",
        "GAME_PROGRESSING": "FALSE",
    }

    def flipped(kind: str, *, goal="active", agent="deciding", hb_age=5.0,
                log_done=False, probe_matches=True, progress=False) -> None:
        # Reset every source, then break exactly one.
        write_sources(agent=agent, hb_age=hb_age, log_done=log_done, goal=goal)
        if progress:
            ledger.append("milestones", {"kind": "progress", "node": "n",
                                         "proof": "p"}, ts=NOW - 5)
        probe = ((lambda: ["pid 1 tools/supervise.py"]) if probe_matches
                 else (lambda: []))
        result = _collect(
            tmp_path, ledger=ledger,
            checkpoint={"goal": {"status": goal},
                        "session_started_ts": _iso(NOW - 60)},
            process_probe=probe).compact()
        changed = {key for key in baseline if result[key] != baseline[key]}
        assert changed == {kind}, f"expected only {kind} to change, got {changed}"

    flipped("GOAL_ALIVE", goal="stopped")
    flipped("AGENT_DECIDING", agent="idle")
    flipped("EXECUTOR_ALIVE", hb_age=600.0)
    flipped("SUPERVISOR_RUNNING", probe_matches=False)
    flipped("GAME_PROGRESSING", progress=True)


# --------------------------------------------------------------------------- #
# criteria: each one fires only on its own fact
# --------------------------------------------------------------------------- #
def test_stall_needs_an_explicit_not_progressing_fact():
    cfg = WatchdogConfig(stall_s=600)
    fired = evaluate_criteria(cfg, _facts(GAME_PROGRESSING="FALSE"),
                              _night(last_verified_progress_ts=NOW - 1000),
                              now=NOW, deadline=None, session_start_ts=NOW - 2000,
                              seen_milestone_seq=0)
    stall = next(c for c in fired if c.kind is WakeKind.STALL)
    assert stall.fires is True
    assert "1000" in stall.reason

    unknown = evaluate_criteria(cfg, _facts(GAME_PROGRESSING="UNKNOWN"),
                                _night(), now=NOW, deadline=None,
                                session_start_ts=None, seen_milestone_seq=0)
    assert next(c for c in unknown if c.kind is WakeKind.STALL).fires is False

    fresh = evaluate_criteria(cfg, _facts(GAME_PROGRESSING="FALSE"),
                              _night(last_verified_progress_ts=NOW - 10),
                              now=NOW, deadline=None, session_start_ts=NOW - 2000,
                              seen_milestone_seq=0)
    assert next(c for c in fresh if c.kind is WakeKind.STALL).fires is False


def test_each_wake_kind_fires_from_its_own_condition(tmp_path):
    cfg = WatchdogConfig(stall_s=600, no_work_streak=2, unverified_streak=3,
                         deadline_warn_s=1800, pause_files=(),
                         cooldowns={})
    deadline = parse_deadline(_iso(NOW + 300))
    checks = {c.kind: c for c in evaluate_criteria(
        cfg,
        _facts(GAME_PROGRESSING="FALSE", EXECUTOR_ALIVE="FALSE"),
        _night(last_verified_progress_ts=NOW - 1000,
               consecutive_no_work=2, consecutive_unverified=3, blocked=True,
               blocked_reason="no usable pet", milestone_seq=2,
               last_milestone={"proof": "Lv41"}),
        now=NOW, deadline=deadline, session_start_ts=NOW - 5000,
        seen_milestone_seq=1)}
    for kind in (WakeKind.STALL, WakeKind.NO_WORK_STREAK, WakeKind.BLOCKED,
                 WakeKind.UNVERIFIED_ACTIVITY, WakeKind.MILESTONE,
                 WakeKind.DEADLINE, WakeKind.EXECUTOR_DEAD):
        assert checks[kind].fires is True, kind
    assert checks[WakeKind.PAUSE].fires is False

    # 1800 s warning, 300 s left -> warning; already past -> fires too.
    assert "deadline in" in checks[WakeKind.DEADLINE].reason
    past = {c.kind: c for c in evaluate_criteria(
        cfg, _facts(), _night(), now=NOW, deadline=parse_deadline(_iso(NOW - 60)),
        session_start_ts=None, seen_milestone_seq=0)}
    assert past[WakeKind.DEADLINE].fires is True
    assert "passed" in past[WakeKind.DEADLINE].reason

    # Pause flag present -> PAUSE fires.
    pause_file = tmp_path / "pause"
    pause_file.write_text("", encoding="utf-8")
    paused = {c.kind: c for c in evaluate_criteria(
        WatchdogConfig(pause_files=(str(pause_file),)), _facts(), _night(), now=NOW,
        deadline=None, session_start_ts=None, seen_milestone_seq=0)}
    assert paused[WakeKind.PAUSE].fires is True


def test_executor_dead_does_not_fire_on_unknown():
    cfg = WatchdogConfig()
    checks = {c.kind: c for c in evaluate_criteria(
        cfg, _facts(EXECUTOR_ALIVE="UNKNOWN"), _night(), now=NOW, deadline=None,
        session_start_ts=None, seen_milestone_seq=0)}
    assert checks[WakeKind.EXECUTOR_DEAD].fires is False
    checks = {c.kind: c for c in evaluate_criteria(
        cfg, _facts(EXECUTOR_ALIVE="FALSE"), _night(), now=NOW, deadline=None,
        session_start_ts=None, seen_milestone_seq=0)}
    assert checks[WakeKind.EXECUTOR_DEAD].fires is True


# --------------------------------------------------------------------------- #
# the watchdog loop: wake, suppress, count, and re-fire after the cooldown
# --------------------------------------------------------------------------- #
def _watchdog(tmp_path: Path, *, now: float = NOW, **config) -> Watchdog:
    ledger = Ledger(tmp_path, fsync=False)
    defaults = dict(
        stall_s=60.0,
        cooldown_s=300.0,
        persist_every_s=30.0,
        pause_files=(),
        stop_files=(str(tmp_path / "stop"),),
        agent_turn_file=str(tmp_path / "agent_turn.json"),
        executor_heartbeat_file=str(tmp_path / "hb.json"),
        executor_lock_file=str(tmp_path / "lock"),
        wild_cycle_ledger=str(tmp_path / "wild.jsonl"),
        supervisor_log_file=str(tmp_path / "supervisor.log"),
    )
    defaults.update(config)
    cfg = WatchdogConfig(**defaults)
    return Watchdog(ledger, cfg, clock=lambda: now, process_probe=lambda: [])


def test_watchdog_stall_wakes_once_then_suppresses_then_refires(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    ledger.append("events", {"kind": "session.init"}, ts=NOW - 2000)
    ledger.save_checkpoint({
        "mode": "EFFICIENT", "policy": "ADAPTIVE",
        "session_started_ts": _iso(NOW - 2000),
        "goal": {"status": "active"},
    })
    watchdog = _watchdog(tmp_path)

    first = watchdog.step(now=NOW)
    assert first["applied"] == ["STALL"]
    assert first["persisted"] is True
    assert first["night"]["last_outcome"] is None

    second = watchdog.step(now=NOW + 5)
    assert second["applied"] == []
    assert second["suppressed_this_tick"] == ["STALL"]
    assert second["suppressed_total"] == 1
    assert second["suppressed_pending"]["STALL"]["count"] == 1
    # A suppression is not worth an fsync on its own; it is counted and flushed later.
    assert second["persisted"] is False

    third = watchdog.step(now=NOW + 301)
    assert third["applied"] == ["STALL"]
    assert third["wakes_total"] == 2

    # The applied wake is in the event timeline, and health records exist on disk.
    events = [record for record in ledger.read("events") if record["kind"] == "wake"]
    assert [event["wake"] for event in events] == ["STALL", "STALL"]
    health = ledger.read("runtime_health")
    assert health and health[-1]["facts_compact"]["GAME_PROGRESSING"] in ("FALSE",
                                                                         "UNKNOWN")


def test_watchdog_no_work_streak_and_blocked_wake(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE",
                            "session_started_ts": _iso(NOW - 100),
                            "goal": {"status": "active"}})
    for index in range(2):
        ledger.append("events", {"kind": "batch.end", "outcome": "NO_WORK",
                                 "battles": 0, "actions": 0}, ts=NOW - 100 + index)
    watchdog = _watchdog(tmp_path, stall_s=10_000.0)
    report = watchdog.step(now=NOW)
    assert report["applied"] == ["NO_WORK_STREAK"]

    ledger.append("failures", {"kind": "blocked", "reason": "no usable pet"},
                  ts=NOW + 1)
    report = watchdog.step(now=NOW + 301)
    assert report["applied"] == ["BLOCKED"]
    assert report["facts_compact"]["GOAL_ALIVE"] == "TRUE"


def test_watchdog_milestone_wakes_once_per_new_milestone(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE",
                            "session_started_ts": _iso(NOW - 100),
                            "goal": {"status": "active"}})
    ledger.append("milestones", {"kind": "progress", "node": "wild_45",
                                 "proof": "Lv41"}, ts=NOW - 10)
    watchdog = _watchdog(tmp_path, stall_s=10_000.0)
    first = watchdog.step(now=NOW)
    assert first["applied"] == ["MILESTONE"]
    second = watchdog.step(now=NOW + 400)
    assert "MILESTONE" not in second["applied"]

    ledger.append("milestones", {"kind": "progress", "node": "wild_46",
                                 "proof": "Lv42"}, ts=NOW + 401)
    third = watchdog.step(now=NOW + 401)
    assert third["applied"] == ["MILESTONE"]
    checkpoint = ledger.load_checkpoint()
    assert checkpoint["watchdog"]["seen_milestone_seq"] == 2


def test_watchdog_deadline_and_pause_wakes(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE",
                            "session_started_ts": _iso(NOW - 100),
                            "goal": {"status": "active"}})
    pause = tmp_path / "pause"
    pause.write_text("", encoding="utf-8")
    watchdog = _watchdog(tmp_path, deadline=_iso(NOW + 120), stall_s=10_000.0,
                         pause_files=(str(pause),))
    report = watchdog.step(now=NOW)
    assert set(report["applied"]) == {"DEADLINE", "PAUSE"}
    assert report["remaining_s"] == pytest.approx(120.0, abs=1.0)


def test_watchdog_persist_throttle_and_state_round_trip(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE",
                            "session_started_ts": _iso(NOW - 2000),
                            "goal": {"status": "active"}})
    watchdog = _watchdog(tmp_path, stall_s=60.0, cooldown_s=300.0,
                         persist_every_s=30.0)
    watchdog.step(now=NOW)          # applied -> persisted
    # 10 suppressed ticks without a change: counted, but only one flush at >= 30 s.
    for offset in range(1, 11):
        watchdog.step(now=NOW + offset)
    health_count = len(ledger.read("runtime_health"))
    assert watchdog.suppressed_total == 10
    watchdog.step(now=NOW + 31)     # interval elapsed -> persisted
    assert len(ledger.read("runtime_health")) == health_count + 1

    # A new Watchdog on the same files must not re-wake immediately: the cooldown
    # state survives in the checkpoint.
    restored = _watchdog(tmp_path, stall_s=60.0, cooldown_s=300.0)
    report = restored.step(now=NOW + 40)
    assert report["applied"] == []
    assert report["suppressed_total"] >= 1


def test_watchdog_run_respects_cycles_and_the_stop_flag(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE",
                            "session_started_ts": _iso(NOW - 100),
                            "goal": {"status": "active"}})
    watchdog = _watchdog(tmp_path, stall_s=10_000.0, persist_every_s=0.0)
    seen = []
    assert watchdog.run(cycles=3, sleep=lambda _s: None,
                        on_report=seen.append) == 3
    assert len(seen) == 3

    stop = tmp_path / "stop"
    stop.write_text("", encoding="utf-8")
    cycles = watchdog.run(cycles=10, sleep=lambda _s: None)
    assert cycles == 1
    events = ledger.read("events")
    assert events[-1]["kind"] == "watchdog.stop"


def test_parse_deadline_rejects_a_naive_timestamp():
    with pytest.raises(ValueError, match="timezone offset"):
        parse_deadline("2026-11-07T07:00:00")
    assert parse_deadline(None) is None
    assert remaining_seconds(parse_deadline("2026-11-07T07:00:00+08:00"),
                             1_800_000_000.0) is not None


def test_watchdog_config_is_serialisable(tmp_path):
    cfg = WatchdogConfig(deadline=_iso(NOW), pause_files=(str(tmp_path / "p"),))
    payload = cfg.to_dict()
    json.dumps(payload)  # no Path or Enum leaks into the record
    assert payload["pause_files"] == [str(tmp_path / "p")]
    assert cfg.cooldown_for(WakeKind.DEADLINE) == 600.0
    assert cfg.cooldown_for(WakeKind.STALL) == 300.0
