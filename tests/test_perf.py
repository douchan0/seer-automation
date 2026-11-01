"""Perf-recorder tests: deterministic clock, no real waiting."""

from __future__ import annotations

import json
import os

import pytest

from seerauto.perf import PerfRecorder, percentile, summarize_samples


class FakeClock:
    """Clock that advances by a fixed step every call."""

    def __init__(self, step_ns: int = 1_000_000):
        self.now = 0
        self.step_ns = step_ns

    def __call__(self) -> int:
        self.now += self.step_ns
        return self.now


def test_percentile_nearest_rank():
    values = list(range(1, 101))
    assert percentile(values, 0) == 1
    assert percentile(values, 100) == 100
    p50 = percentile(values, 50)
    assert p50 in (50, 51)
    assert percentile(values, 95) >= 94
    assert percentile([], 50) != percentile([], 50)  # nan


def test_stage_timer_records_into_summary():
    clock = FakeClock(step_ns=2_000_000)
    rec = PerfRecorder(clock=clock)
    for _ in range(3):
        with rec.stage("capture") as t:
            t.extra["hits"] = 1
    summary = rec.summary()
    stats = summary["stages"]["capture"]
    assert stats["count"] == 3
    assert stats["mean_ms"] == pytest.approx(2.0)
    assert stats["min_ms"] == pytest.approx(2.0)
    assert stats["max_ms"] == pytest.approx(2.0)
    assert stats["p95_ms"] == pytest.approx(2.0)
    # the tag survived
    assert any(s.get("hits") == 1 for s in rec.samples if s["stage"] == "capture")


def test_exception_still_records_with_ok_false():
    clock = FakeClock(step_ns=1_000_000)
    rec = PerfRecorder(clock=clock)
    with pytest.raises(RuntimeError):
        with rec.stage("detect"):
            raise RuntimeError("boom")
    assert rec.stages["detect"].count == 1
    assert rec.samples[0]["ok"] is False


def test_p50_p95_ordering():
    clock = FakeClock(step_ns=1_000_000)
    rec = PerfRecorder(clock=clock)
    for ns in (1, 2, 3, 4, 10):
        rec.record("wait", ns * 1_000_000)
    stats = rec.stages["wait"].as_dict()
    assert stats["mean_ms"] == pytest.approx((1 + 2 + 3 + 4 + 10) / 5)
    assert stats["p50_ms"] <= stats["p95_ms"] <= stats["max_ms"]


def test_counters_iterations_and_format_table():
    rec = PerfRecorder(clock=FakeClock())
    rec.record("capture", 1_000_000)
    rec.record("input", 2_000_000)
    rec.count("clicks", 3)
    rec.next_iteration()
    rec.next_iteration()
    table = rec.format_table()
    assert "capture" in table and "input" in table
    summary = rec.summary()
    assert summary["iterations"] == 2
    assert summary["counters"] == {"clicks": 3}
    assert rec.format_table(stages=[]) == "(no timings recorded)"


def test_dump_json(tmp_path):
    rec = PerfRecorder(clock=FakeClock())
    rec.record("capture", 1_000_000)
    rec.note("hello")
    out = tmp_path / "perf" / "run.json"
    rec.dump_json(str(out), extra={"tag": "test"})
    data = json.loads(out.read_text())
    assert data["stages"]["capture"]["count"] == 1
    assert data["notes"] == ["hello"]
    assert data["extra"] == {"tag": "test"}
    # atomic write leaves no tmp file behind
    assert not os.path.exists(str(out) + ".tmp")


def test_disabled_recorder_records_nothing():
    rec = PerfRecorder(enabled=False)
    with rec.stage("capture"):
        pass
    assert rec.summary()["stages"] == {}
    assert rec.format_table() == "(no timings recorded)"


def test_reset():
    rec = PerfRecorder(clock=FakeClock())
    rec.record("capture", 5)
    rec.next_iteration()
    rec.reset()
    assert rec.summary()["stages"] == {}
    assert rec.summary()["iterations"] == 0


def test_summarize_samples():
    s = summarize_samples([1_000_000, 3_000_000])
    assert s["count"] == 2
    assert s["mean_ms"] == pytest.approx(2.0)
    assert s["max_ms"] == pytest.approx(3.0)
    assert summarize_samples([]) == {"count": 0}
