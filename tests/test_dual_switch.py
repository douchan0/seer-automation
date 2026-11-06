"""Tests for the mode-switch audit trail and the two anti-oscillation brakes.

The spec a human will audit: every mode switch record must carry ``ts``, ``from``,
``to``, a reason from the enum, an evidence pointer, the cooldown state that was
consulted, and whether it was applied or suppressed.  A suppressed request must still
be *recorded* — "we decided not to switch" is part of the night's decision log.

The oscillation test at the bottom is the behavioural one: an adversarial caller that
keeps asking for a switch every few minutes, alternating reasons (so a per-reason
cooldown alone would not stop it), must still be held to roughly one switch per
minimum-dwell window.
"""

from __future__ import annotations

import pytest

from seerauto.dual.ledger import Ledger
from seerauto.dual.modes import Mode, Policy, Signals, SwitchReason
from seerauto.dual.switch import (DEFAULT_MIN_DWELL_S, DEFAULT_REASON_COOLDOWN_S,
                                  ModeSwitchService, SUPPRESS_ALREADY_IN_MODE,
                                  SUPPRESS_MIN_DWELL, SUPPRESS_REASON_COOLDOWN,
                                  decide, mode_switches)

NOW = 1_800_000_000.0


# --------------------------------------------------------------------------- #
# the pure decision
# --------------------------------------------------------------------------- #
def test_decide_applies_when_nothing_blocks_it():
    decision = decide(current="EFFICIENT", to="MAX_RESULTS", reason="NEED_RESEARCH",
                      evidence="docs/research/x.md", now=NOW,
                      last_same_reason_epoch=None, last_switch_epoch=None)
    assert decision.applied is True
    assert decision.suppressed is False
    assert decision.from_mode is Mode.EFFICIENT
    assert decision.to is Mode.MAX_RESULTS
    assert decision.reason is SwitchReason.NEED_RESEARCH
    assert decision.cooldown["reason_cooldown_s"] == DEFAULT_REASON_COOLDOWN_S
    assert decision.cooldown["min_dwell_s"] == DEFAULT_MIN_DWELL_S


def test_decide_suppresses_a_no_op():
    decision = decide(current="EFFICIENT", to="EFFICIENT", reason="RESOLVED",
                      evidence="x", now=NOW, last_same_reason_epoch=None,
                      last_switch_epoch=None)
    assert decision.applied is False
    assert decision.suppressed_by == SUPPRESS_ALREADY_IN_MODE


def test_decide_suppresses_the_same_reason_within_cooldown():
    decision = decide(current="EFFICIENT", to="MAX_RESULTS", reason="LONG_NO_PROGRESS",
                      evidence="events.jsonl", now=NOW,
                      last_same_reason_epoch=NOW - 100,
                      last_switch_epoch=NOW - 5000)
    assert decision.applied is False
    assert decision.suppressed_by == SUPPRESS_REASON_COOLDOWN
    assert decision.cooldown["since_same_reason_s"] == pytest.approx(100.0)
    assert decision.cooldown["reason_remaining_s"] == pytest.approx(
        DEFAULT_REASON_COOLDOWN_S - 100)


def test_decide_suppresses_any_switch_within_min_dwell():
    # A *different* reason this time: only the dwell brake can stop it.
    decision = decide(current="MAX_RESULTS", to="EFFICIENT", reason="RESOLVED",
                      evidence="events.jsonl", now=NOW,
                      last_same_reason_epoch=None,
                      last_switch_epoch=NOW - 60)
    assert decision.applied is False
    assert decision.suppressed_by == SUPPRESS_MIN_DWELL
    assert decision.cooldown["dwell_remaining_s"] == pytest.approx(
        DEFAULT_MIN_DWELL_S - 60)


def test_decide_uses_configured_cooldowns():
    decision = decide(current="EFFICIENT", to="MAX_RESULTS", reason="OPPORTUNITY",
                      evidence="x", now=NOW, last_same_reason_epoch=NOW - 100,
                      last_switch_epoch=NOW - 5000, reason_cooldown_s=60,
                      min_dwell_s=60)
    assert decision.applied is True


# --------------------------------------------------------------------------- #
# the service: audit record on every path
# --------------------------------------------------------------------------- #
def _service(tmp_path, **kwargs):
    ledger = Ledger(tmp_path, fsync=False)
    return ledger, ModeSwitchService(ledger, **kwargs)


def test_applied_switch_writes_a_complete_audit_record(tmp_path):
    ledger, service = _service(tmp_path)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE"})
    assert service.request("MAX_RESULTS", "NEED_RESEARCH",
                           evidence="docs/research/20261106_PUNI_ROUTE.md",
                           note="open questions block the graph", now=NOW)["applied"]

    record = ledger.read("mode_switches")[-1]
    for field in ("ts", "ts_epoch", "from", "to", "reason", "evidence", "applied",
                  "suppressed", "cooldown", "policy"):
        assert field in record, field
    assert record["from"] == "EFFICIENT" and record["to"] == "MAX_RESULTS"
    assert record["reason"] == "NEED_RESEARCH"
    assert record["evidence"] == "docs/research/20261106_PUNI_ROUTE.md"
    assert record["suppressed"] is False and record["suppressed_by"] is None
    assert record["policy"] == "ADAPTIVE"
    assert set(record["cooldown"]) >= {"reason_cooldown_s", "min_dwell_s",
                                      "last_switch_epoch", "since_last_switch_s"}
    # The checkpoint is the effective mode, the ledger is the history.
    assert ledger.load_checkpoint()["mode"] == "MAX_RESULTS"
    assert ledger.last("events")["kind"] == "mode.switch"


def test_suppressed_switch_is_recorded_but_changes_nothing(tmp_path):
    ledger, service = _service(tmp_path)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE"})
    service.request("MAX_RESULTS", "NEED_RESEARCH", evidence="x", now=NOW)
    record = service.request("EFFICIENT", "RESOLVED", evidence="events.jsonl",
                             now=NOW + 10)
    assert record["applied"] is False
    assert record["suppressed"] is True
    assert record["suppressed_by"] == SUPPRESS_MIN_DWELL
    assert record["from"] == "MAX_RESULTS" and record["to"] == "EFFICIENT"

    records = ledger.read("mode_switches")
    assert len(records) == 2  # the suppressed request is part of the audit trail
    assert records[-1]["ts_epoch"] == pytest.approx(NOW + 10, abs=1.0)
    assert ledger.load_checkpoint()["mode"] == "MAX_RESULTS"
    assert ledger.last("events")["kind"] == "mode.switch.suppressed"


def test_returning_after_the_dwell_is_applied(tmp_path):
    ledger, service = _service(tmp_path)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE"})
    service.request("MAX_RESULTS", "NEED_RESEARCH", evidence="x", now=NOW)
    later = NOW + DEFAULT_MIN_DWELL_S + 1
    record = service.request("EFFICIENT", "RESOLVED", evidence="y", now=later)
    assert record["applied"] is True
    assert ledger.load_checkpoint()["mode"] == "EFFICIENT"


def test_reason_cooldown_is_a_separate_brake_from_the_dwell(tmp_path):
    """With a 60 s dwell and a 900 s reason cooldown the two brakes are separable.

    With the *default* values (900 s reason, 1800 s dwell) the dwell is the stronger
    one and hides the reason cooldown; the brakes are independent when tuned, which
    is why both exist.
    """

    ledger, service = _service(tmp_path, reason_cooldown_s=900.0, min_dwell_s=60.0)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE"})
    t0 = NOW
    service.request("MAX_RESULTS", "GRAPH_BLOCKED", evidence="x", now=t0)
    # 70 s later, a different reason is allowed (dwell is 60 s).
    assert service.request("EFFICIENT", "RESOLVED", evidence="y",
                           now=t0 + 70)["applied"] is True
    # And 100 s after the first switch, the original reason is still cooling down.
    record = service.request("MAX_RESULTS", "GRAPH_BLOCKED", evidence="z",
                             now=t0 + 100)
    assert record["applied"] is False
    assert record["suppressed_by"] == SUPPRESS_REASON_COOLDOWN
    assert record["cooldown"]["since_same_reason_s"] == pytest.approx(100.0)


def test_switch_requires_evidence_and_rejects_unknown_values(tmp_path):
    ledger, service = _service(tmp_path)
    with pytest.raises(ValueError, match="evidence pointer"):
        service.request("MAX_RESULTS", "OPPORTUNITY", evidence="   ")
    with pytest.raises(ValueError, match="unknown mode"):
        service.request("TURBO", "OPPORTUNITY", evidence="x")
    with pytest.raises(ValueError, match="unknown switch reason"):
        service.request("MAX_RESULTS", "because", evidence="x")
    assert ledger.read("mode_switches") == []


def test_reader_helpers_agree_with_the_written_trail(tmp_path):
    ledger, service = _service(tmp_path)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE"})
    service.request("MAX_RESULTS", "OPPORTUNITY", evidence="a", now=NOW)
    service.request("EFFICIENT", "RESOLVED", evidence="b", now=NOW + 10)
    assert len(mode_switches(ledger)) == 2
    assert service.last_applied()["reason"] == "OPPORTUNITY"
    assert service.last_applied_for(SwitchReason.RESOLVED) is None
    assert service.current() == (Mode.MAX_RESULTS, Policy.ADAPTIVE)


def test_adaptive_request_records_the_rule_that_fired(tmp_path):
    ledger, service = _service(tmp_path)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE"})
    written = service.request_from_signals(
        Signals(blocked=True),
        evidence="data/overnight/20261106_dual_mode/failures.jsonl")
    assert written["applied"] is True
    assert written["reason"] == "GRAPH_BLOCKED"
    assert "rule=graph_blocked" in written["note"]
    assert written["resolution"]["mode"] == "MAX_RESULTS"


def test_adaptive_request_refuses_a_static_policy(tmp_path):
    ledger, service = _service(tmp_path)
    ledger.save_checkpoint({"mode": "MAX_RESULTS", "policy": "STATIC"})
    with pytest.raises(ValueError, match="STATIC"):
        service.request_from_signals(Signals(blocked=True), evidence="x")


# --------------------------------------------------------------------------- #
# the anti-oscillation property (behavioural, not just per-call)
# --------------------------------------------------------------------------- #
def test_adversarial_alternation_cannot_flap_the_mode(tmp_path):
    """Ask every 60 s for an hour, alternating two valid reasons."""

    ledger, service = _service(tmp_path)
    ledger.save_checkpoint({"mode": "EFFICIENT", "policy": "ADAPTIVE"})
    applied = []
    targets = [("MAX_RESULTS", "NEED_RESEARCH"), ("EFFICIENT", "RESOLVED"),
               ("MAX_RESULTS", "OPPORTUNITY"), ("EFFICIENT", "MARGINAL_VALUE_DROP")]
    for index in range(60):
        target, reason = targets[index % len(targets)]
        record = service.request(target, reason, evidence="stress test",
                                 now=NOW + index * 60)
        if record["applied"]:
            applied.append((index, target, reason))
        if index == 0:
            assert record["applied"] is True

    # 60 minutes / 30 minutes dwell => at most two more switches after the first.
    assert len(applied) <= 3
    assert {entry[1] for entry in applied} <= {"MAX_RESULTS", "EFFICIENT"}
    # Every one of the 60 requests is on disk, applied or suppressed.
    assert len(ledger.read("mode_switches")) == 60
    suppressed = [r for r in ledger.read("mode_switches") if not r["applied"]]
    assert suppressed and all(r["suppressed_by"] for r in suppressed)
    # The mode on disk equals the last applied switch, never the last *request*.
    assert ledger.load_checkpoint()["mode"] == applied[-1][1]
