"""Tests for the append-only ledgers, the atomic checkpoint, and the CLI wiring.

The night is unattended, so the two properties that must hold are:

* an append is a commit — one JSON line, then fsync; a crash can truncate the *last*
  line and readers must skip exactly that line without losing the earlier ones;
* the checkpoint is replaced, never edited — a crash during a save leaves the old
  complete checkpoint in place, never a half-written one.

The tests below also pin that `status` is safe (and side-effect free) with nothing on
disk, because an operator running status at 03:00 must not get a traceback — and that
the "five facts" style JSON a caller parses is actually produced.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import time

import pytest

from seerauto.dual.ledger import Ledger, LedgerError, parse_ts
from seerauto.dual.outcome import Outcome
from seerauto.dual.state import DerivedCache, derive

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "overnight_control", _ROOT / "tools" / "overnight_control.py")
ctl = importlib.util.module_from_spec(_SPEC)
sys.modules["overnight_control"] = ctl
_SPEC.loader.exec_module(ctl)


# --------------------------------------------------------------------------- #
# append / read
# --------------------------------------------------------------------------- #
def test_append_injects_ts_and_reads_back(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    record = ledger.append("events", {"kind": "session.init", "mode": "EFFICIENT"})
    assert record["kind"] == "session.init"
    assert record["ts_epoch"] == pytest.approx(record["ts_epoch"])
    assert parse_ts(record["ts"]) == pytest.approx(record["ts_epoch"], abs=1.0)
    assert ledger.read("events") == [record]


def test_ledger_owns_ts_so_a_record_cannot_be_backdated(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    record = ledger.append("events", {"kind": "x", "ts": "1999-01-01T00:00:00+08:00",
                                      "ts_epoch": 1.0})
    assert record["ts_epoch"] > time.time() - 60
    assert record["ts_epoch"] != 1.0


def test_explicit_ts_is_allowed_for_backfills(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    record = ledger.append("events", {"kind": "x"}, ts=1_800_000_000)
    assert record["ts_epoch"] == pytest.approx(1_800_000_000.0)
    assert parse_ts(record["ts"]) == pytest.approx(1_800_000_000.0, abs=1.0)


def test_append_rejects_unknown_ledger_and_bad_payloads(tmp_path):
    ledger = Ledger(tmp_path, fsync=False).ensure()
    with pytest.raises(LedgerError, match="unknown ledger"):
        ledger.append("eventz", {"kind": "x"})
    with pytest.raises(LedgerError, match="mapping"):
        ledger.append("events", ["not", "a", "mapping"])  # type: ignore[arg-type]
    with pytest.raises(LedgerError, match="split it"):
        ledger.append("events", {"kind": "x", "blob": "y" * (65 * 1024)})


def test_fsync_is_called_for_every_append(tmp_path, monkeypatch):
    calls = []
    real_fsync = os.fsync

    def counting_fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    ledger = Ledger(tmp_path, fsync=True)
    ledger.append("events", {"kind": "a"})
    ledger.append("events", {"kind": "b"})
    # one fsync per append (the directory fsync only happens for checkpoints)
    assert len(calls) == 2
    assert [record["kind"] for record in ledger.read("events")] == ["a", "b"]


def test_append_only_order_is_preserved(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    for index in range(5):
        ledger.append("runtime_health", {"kind": "health", "n": index})
    assert [record["n"] for record in ledger.read("runtime_health")] == list(range(5))
    assert ledger.count("runtime_health") == 5
    assert ledger.last("runtime_health")["n"] == 4
    assert [record["n"] for record in ledger.tail("runtime_health", 2)] == [3, 4]


# --------------------------------------------------------------------------- #
# crash durability: a truncated last line is tolerated, a hole is reported
# --------------------------------------------------------------------------- #
def test_truncated_last_line_is_skipped_and_flagged(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    ledger.append("events", {"kind": "ok", "n": 1})
    path = ledger.path("events")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "half-writt')  # process died mid-write
    assert [record["kind"] for record in ledger.read("events")] == ["ok"]
    problems = ledger.problems("events")
    assert problems == [{"line": 2, "truncated_tail": True}]
    assert path.read_text(encoding="utf-8").endswith('{"kind": "half-writt')


def test_corrupt_middle_line_is_reported_as_not_the_tail(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    ledger.append("events", {"kind": "a"})
    with ledger.path("events").open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
    ledger.append("events", {"kind": "c"})
    assert [record["kind"] for record in ledger.read("events")] == ["a", "c"]
    assert ledger.problems("events") == [{"line": 2, "truncated_tail": False}]


def test_missing_ledger_reads_as_empty_without_creating_it(tmp_path):
    target = tmp_path / "nope"
    ledger = Ledger(target, fsync=False)
    assert ledger.read("events") == []
    assert ledger.last("events") is None
    assert ledger.size("events") == 0
    assert not target.exists()


# --------------------------------------------------------------------------- #
# checkpoint: atomic save, tolerant load
# --------------------------------------------------------------------------- #
def test_checkpoint_round_trips_atomically(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE"})
    assert ledger.load_checkpoint() == {"mode": "EFFICIENT", "policy": "ADAPTIVE"}
    assert not (tmp_path / "checkpoint.json.tmp").exists()
    # replace leaves no stale tmp behind on the next save either
    ledger.save_checkpoint({"mode": "MAX_RESULTS"})
    assert ledger.load_checkpoint()["mode"] == "MAX_RESULTS"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_checkpoint_failure_keeps_the_previous_complete_file(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path, fsync=False)
    ledger.save_checkpoint({"mode": "EFFICIENT"})
    original = (tmp_path / "checkpoint.json").read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full during replace")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        ledger.save_checkpoint({"mode": "MAX_RESULTS"})
    monkeypatch.undo()

    assert (tmp_path / "checkpoint.json").read_text(encoding="utf-8") == original
    assert not (tmp_path / "checkpoint.json.tmp").exists()
    assert ledger.load_checkpoint()["mode"] == "EFFICIENT"


def test_corrupt_checkpoint_is_moved_aside_and_logged(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    (tmp_path / "checkpoint.json").write_text("{truncated", encoding="utf-8")
    assert ledger.load_checkpoint(default={"fallback": True}) == {"fallback": True}
    assert not (tmp_path / "checkpoint.json").exists()
    assert list(tmp_path.glob("checkpoint.json.corrupt-*"))
    event = ledger.last("events")
    assert event["kind"] == "checkpoint.corrupt"


def test_update_checkpoint_merges_and_stamps(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    ledger.save_checkpoint({"mode": "EFFICIENT"})
    state = ledger.update_checkpoint({"deadline": "2026-11-07T07:00:00+08:00"},
                                     mode="MAX_RESULTS")
    assert state["mode"] == "MAX_RESULTS"
    assert state["deadline"] == "2026-11-07T07:00:00+08:00"
    assert "checkpoint_updated_epoch" in state
    assert ledger.load_checkpoint()["mode"] == "MAX_RESULTS"


def test_sizes_signature_and_tmp_sweep(tmp_path):
    ledger = Ledger(tmp_path, fsync=False).ensure()
    assert set(ledger.sizes()) == set(ledger.sizes())
    assert ledger.sizes()["events"] == 0
    stale = tmp_path / "events.jsonl.tmp"
    stale.write_text("x", encoding="utf-8")
    assert ledger.sweep_tmp() == [str(stale)]
    assert not stale.exists()
    before = ledger.signature()
    ledger.append("events", {"kind": "x"})
    assert ledger.signature() != before


# --------------------------------------------------------------------------- #
# state derivation from the ledgers
# --------------------------------------------------------------------------- #
def test_derive_tracks_no_work_streak_and_last_outcome(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    base = 1_800_000_000.0
    for index in range(3):
        ledger.append("events", {"kind": "batch.end", "outcome": "NO_WORK",
                                 "battles": 0, "actions": 0}, ts=base + index)
    ledger.append("events", {"kind": "batch.end", "outcome": "PROGRESS",
                             "battles": 2, "actions": 10}, ts=base + 10)
    ledger.append("events", {"kind": "batch.end", "outcome": "NO_WORK",
                             "battles": 0, "actions": 0}, ts=base + 20)
    night = derive(ledger, now=base + 30)
    assert night.batch_count == 5
    assert night.last_outcome is Outcome.NO_WORK
    assert night.consecutive_no_work == 1
    assert night.last_verified_progress_ts is None


def test_derive_only_counts_proof_as_progress(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    base = 1_800_000_000.0
    ledger.append("milestones", {"kind": "progress", "node": "wild_45",
                                 "proof": "Lv41", "evidence": "frames/x.png"},
                  ts=base)
    night = derive(ledger, now=base + 60)
    assert night.last_verified_progress_ts == pytest.approx(base)
    assert night.last_verified_progress["proof"] == "Lv41"
    assert night.milestone_seq == 1
    assert night.since_progress_s == pytest.approx(60.0)


def test_derive_blocked_clears_on_later_progress(tmp_path):
    ledger = Ledger(tmp_path, fsync=False)
    base = 1_800_000_000.0
    ledger.append("failures", {"kind": "blocked", "reason": "no usable pet"}, ts=base)
    ledger.append("events", {"kind": "batch.end", "outcome": "BLOCKED",
                             "reason": "no usable pet"}, ts=base + 1)
    night = derive(ledger, now=base + 10)
    assert night.blocked is True
    assert night.blocked_reason == "no usable pet"
    ledger.append("milestones", {"kind": "progress", "node": "x", "proof": "y"},
                  ts=base + 20)
    night = derive(ledger, now=base + 30)
    assert night.blocked is False


def test_derived_cache_recomputes_only_when_a_ledger_grows(tmp_path):
    ledger = Ledger(tmp_path, fsync=False).ensure()
    ledger.append("events", {"kind": "x"})
    cache = DerivedCache(ledger)
    first = cache.get(now=time.time())
    second = cache.get(now=time.time())
    assert first is second
    assert cache.recomputes == 1
    ledger.append("events", {"kind": "y"})
    third = cache.get(now=time.time())
    assert third is not second
    assert cache.recomputes == 2


# --------------------------------------------------------------------------- #
# CLI: safe with nothing on disk, and the JSON schema a caller parses
# --------------------------------------------------------------------------- #
def test_cli_status_is_safe_and_read_only_with_no_state(tmp_path, capsys):
    target = tmp_path / "empty"
    code = ctl.main(["--data-dir", str(target), "status"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["initialized"] is False
    assert payload["mode"] is None and payload["deadline"] is None
    assert set(payload["ledgers"]) == {"events", "model_usage", "mode_switches",
                                       "world_snapshots", "runtime_health",
                                       "failures", "milestones"}
    assert set(payload["facts"]) == {"GOAL_ALIVE", "AGENT_DECIDING",
                                     "SUPERVISOR_RUNNING", "EXECUTOR_ALIVE",
                                     "GAME_PROGRESSING"}
    # status must not create anything: an operator running it at 03:00 is a read.
    assert not target.exists()


def test_cli_init_then_status_round_trip(tmp_path, capsys):
    args = ["--data-dir", str(tmp_path)]
    assert ctl.main(args + ["init", "--deadline-at",
                            "2026-11-07T07:00:00+08:00"]) == 0
    init = json.loads(capsys.readouterr().out)
    assert init["mode"] == "EFFICIENT" and init["policy"] == "ADAPTIVE"
    assert init["deadline"] == "2026-11-07T07:00:00+08:00"
    assert init["remaining_s"] > 0

    assert ctl.main(args + ["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["initialized"] is True
    assert status["mode"] == "EFFICIENT"
    assert status["facts"]["GOAL_ALIVE"] == "TRUE"
    assert status["deadline"] == "2026-11-07T07:00:00+08:00"
    assert status["last_event"]["kind"] == "session.init"
    assert status["ledgers"]["events"] > 0
    # init is idempotent unless forced
    assert ctl.main(args + ["init"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["already_initialized"] is True


def test_cli_switch_records_applied_and_suppressed(tmp_path, capsys):
    args = ["--data-dir", str(tmp_path)]
    ctl.main(args + ["init"])
    capsys.readouterr()
    assert ctl.main(args + ["switch", "--to", "MAX_RESULTS", "--reason",
                            "NEED_RESEARCH", "--evidence", "docs/x.md"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True
    assert applied["from"] == "EFFICIENT" and applied["to"] == "MAX_RESULTS"
    assert applied["reason"] == "NEED_RESEARCH"
    assert applied["evidence"] == "docs/x.md"
    assert "cooldown" in applied and "reason_cooldown_s" in applied["cooldown"]

    # Immediate return is refused by the minimum-dwell brake and still recorded.
    assert ctl.main(args + ["switch", "--to", "EFFICIENT", "--reason", "RESOLVED",
                            "--evidence", "events.jsonl"]) == 0
    suppressed = json.loads(capsys.readouterr().out)
    assert suppressed["applied"] is False
    assert suppressed["suppressed_by"] == "min_dwell"
    assert "dwell_remaining_s" in suppressed["cooldown"]

    assert ctl.main(args + ["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["mode"] == "MAX_RESULTS"  # unchanged by the suppressed request
    assert status["ledgers"]["mode_switches"] > 0


def test_cli_switch_requires_evidence_and_initialisation(tmp_path, capsys):
    args = ["--data-dir", str(tmp_path)]
    assert ctl.main(args + ["switch", "--to", "MAX_RESULTS", "--reason",
                            "OPPORTUNITY", "--evidence", ""]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "initialised" in payload["error"]

    ctl.main(args + ["init"])
    capsys.readouterr()
    assert ctl.main(args + ["switch", "--to", "MAX_RESULTS", "--reason",
                            "OPPORTUNITY", "--evidence", ""]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert "evidence pointer" in payload["error"]


def test_cli_progress_records_proof_and_moves_the_watchdog_baseline(tmp_path, capsys):
    args = ["--data-dir", str(tmp_path)]
    ctl.main(args + ["init"])
    capsys.readouterr()
    assert ctl.main(args + ["progress", "--node", "puni.prepare.wild_45",
                            "--proof", "烈焰猩猩 Lv41", "--evidence",
                            "data/overnight/frames/wild_027.png"]) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["milestone"] is True
    assert recorded["recorded"]["proof"] == "烈焰猩猩 Lv41"

    assert ctl.main(args + ["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["last_verified_progress"]["proof"] == "烈焰猩猩 Lv41"
    assert status["facts"]["GAME_PROGRESSING"] == "TRUE"
    assert status["night"]["milestones"] == 1

    # A bare status is not proof, and is refused.
    assert ctl.main(args + ["progress", "--proof", "   "]) == 3


def test_cli_health_once_writes_health_and_is_serialisable(tmp_path, capsys):
    args = ["--data-dir", str(tmp_path), "--supervisor-log", str(tmp_path / "nope.log")] \
        if False else ["--data-dir", str(tmp_path)]
    ctl.main(args + ["init"])
    capsys.readouterr()
    assert ctl.main(args + ["health", "--stall-s", "60",
                            "--supervisor-log", str(tmp_path / "none.log")]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is True
    assert report["kind"] == "health"
    assert report["persisted"] is True
    assert set(report["facts_compact"]) == {"GOAL_ALIVE", "AGENT_DECIDING",
                                            "SUPERVISOR_RUNNING", "EXECUTOR_ALIVE",
                                            "GAME_PROGRESSING"}
    assert any(check["kind"] == "STALL" for check in report["checks"])
    assert (tmp_path / "runtime_health.jsonl").stat().st_size > 0


def test_cli_event_deadline_checkpoint_and_agent_turn(tmp_path, capsys):
    args = ["--data-dir", str(tmp_path)]
    ctl.main(args + ["init", "--deadline-at", "2026-11-07T07:00:00+08:00"])
    capsys.readouterr()

    assert ctl.main(args + ["deadline", "--set", "2026-11-07T06:00:00+08:00"]) == 0
    deadline = json.loads(capsys.readouterr().out)
    assert deadline["deadline"] == "2026-11-07T06:00:00+08:00"
    assert ctl.main(args + ["deadline", "--set", "2026-11-07T06:00:00"]) == 3
    assert json.loads(capsys.readouterr().out)["ok"] is False  # naive is rejected

    assert ctl.main(args + ["event", "--kind", "phase", "--message", "research done",
                            "--data", "phase=1", "--data", "note=ready"]) == 0
    event = json.loads(capsys.readouterr().out)["recorded"]
    assert event["message"] == "research done" and event["phase"] == 1

    assert ctl.main(args + ["checkpoint", "--set", "mode=MAX_RESULTS"]) == 0
    checkpoint = json.loads(capsys.readouterr().out)
    assert checkpoint["checkpoint"]["mode"] == "MAX_RESULTS"

    assert ctl.main(args + ["agent-turn", "--state", "deciding",
                            "--session", "s1"]) == 0
    turn = json.loads(capsys.readouterr().out)
    assert turn["recorded"]["state"] == "deciding"
    assert (tmp_path / "agent_turn.json").exists()

    assert ctl.main(args + ["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["mode"] == "MAX_RESULTS"
    assert status["facts"]["AGENT_DECIDING"] == "TRUE"
