"""Tests for the DSH token meter (`tools/usage_meter.py`).

Everything runs against a temporary fake ``$DSH_HOME``: fake procache JSON plus fake
raw event logs stored as plain ``.jsonl`` (so the tests do not need the ``zstd``
binary and never read the real ``~/.dsh``).  No network, no game.

The maths that must not drift:

* totals = uncached input + output + cache read + cache write;
* cache hit = cacheRead / (cacheRead + uncachedInput);
* a snapshot's delta is measured against the previous snapshot of the *same*
  session, or against session start when there is none;
* summing a session's deltas must reproduce its measured totals;
* the per-hour table from the raw log must reconcile exactly with the procache
  totals (that is what makes it reproducible instead of GUI folklore).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import datetime

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("usage_meter_tool", _ROOT / "tools" / "usage_meter.py")
um = importlib.util.module_from_spec(_SPEC)
sys.modules["usage_meter_tool"] = um
_SPEC.loader.exec_module(um)

# 2026-11-06 22:40:00 +08:00 as epoch ms, the experiment's night window.
T0 = 1793976000000
HOUR_MS = 3_600_000
RAW_KEYS = {
    "uncached_input": "uncachedInputTokens",
    "output": "outputTokens",
    "cache_read": "cacheReadTokens",
    "cache_write": "cacheWriteTokens",
}


# ---------------------------------------------------------------------------
# fake DSH home builders
# ---------------------------------------------------------------------------


def session_doc(
    cwd: str,
    *, uncached: int = 0, output: int = 0, cache_read: int = 0, cache_write: int = 0,
    steps: int = 0, turns: int = 1, surface: int = 1000, window: int = 1_000_000,
    created_at: int = T0, drop: tuple[str, ...] = (),
) -> dict:
    """A procache document shaped exactly like DSH's (rows optional via ``drop``)."""
    rows: dict = {}
    if "tokenUsage" not in drop:
        rows["tokenUsage"] = {
            "ver": 2,
            "seq": 10,
            "val": {
                "totals": {
                    "uncachedInputTokens": uncached,
                    "outputTokens": output,
                    "cacheReadTokens": cache_read,
                    "cacheWriteTokens": cache_write,
                },
                "last": {"turn": turns, "step": steps, "buckets": {}},
            },
        }
    if "sessionStats" not in drop:
        rows["sessionStats"] = {"ver": 1, "seq": 10, "val": {"turns": turns, "steps": steps, "llmMs": 1, "toolMs": 1}}
    if "contextPressure" not in drop:
        rows["contextPressure"] = {
            "ver": 5,
            "seq": 10,
            "val": {"surfaceTokens": surface, "contextWindow": window, "pressureTokens": surface},
        }
    if "contextBreakdown" not in drop:
        rows["contextBreakdown"] = {
            "ver": 6,
            "seq": 10,
            "val": {
                "surfaceTokens": surface,
                "breakdown": {"systemTokens": 100, "toolsTokens": 200, "messageTokens": surface - 300},
            },
        }
    return {
        "version": 7,
        "record": {
            "identity": {"formatVersion": 4, "createdAt": created_at, "cwd": cwd, "isSeeded": False},
            "rows": rows,
        },
    }


def write_procache(home: pathlib.Path, session_id: str, doc: dict) -> pathlib.Path:
    directory = home / "storages" / "session_projcache" / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def usage_event(ts_ms: int, step: int, *, uncached: int, output: int, cache_read: int = 0, turn: int = 1) -> dict:
    return {
        "type": "assistant/message",
        "seq": step * 2,
        "time": ts_ms,
        "data": {
            "turn": turn,
            "step": step,
            "usage": {
                "inputTokens": uncached,
                "outputTokens": output,
                "totalTokens": uncached + output + cache_read,
                "cacheReadTokens": cache_read,
            },
        },
    }


def write_raw_log(
    home: pathlib.Path, cwd: str, session_id: str, events: list[dict], *, parent: str | None = None, depth: int = 0,
) -> pathlib.Path:
    slug = um.cwd_slug(cwd)
    assert slug is not None
    directory = home / "sessions" / slug / session_id
    directory.mkdir(parents=True, exist_ok=True)
    header = {"type": "session", "version": 4, "id": session_id, "cwd": cwd, "delegationDepth": depth}
    if parent:
        header["parentSession"] = parent
        header["origin"] = "subagent"
    path = directory / "session.v4.jsonl"
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(header) + "\n")
        for event in events:
            stream.write(json.dumps(event) + "\n")
    return path


def local_hour(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0).astimezone().strftime("%Y-%m-%d %H")


# ---------------------------------------------------------------------------
# schema parsing and maths
# ---------------------------------------------------------------------------


def test_parse_session_doc_reads_native_totals(tmp_path):
    path = write_procache(
        tmp_path, "session-root",
        session_doc(str(_ROOT), uncached=1000, output=200, cache_read=9000, steps=7, turns=2, surface=4321),
    )
    session = um.load_session_file(path)
    assert session["session"] == "session-root"
    assert session["cwd"] == str(_ROOT)
    assert session["totals"] == {"uncached_input": 1000, "output": 200, "cache_read": 9000, "cache_write": 0}
    assert session["total"] == 10200
    assert session["steps"] == 7 and session["turns"] == 2
    assert session["surface_tokens"] == 4321
    assert session["missing"] == []
    assert session["breakdown"]["toolsTokens"] == 200


def test_missing_rows_are_tolerated_and_named(tmp_path):
    path = write_procache(tmp_path, "session-bare", session_doc(str(_ROOT), drop=("tokenUsage", "sessionStats", "contextPressure")))
    session = um.load_session_file(path)
    assert session["totals"] == {"uncached_input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    assert session["total"] == 0
    assert session["steps"] == 0
    assert "tokenUsage.totals" in session["missing"]
    assert "sessionStats" in session["missing"]
    assert "contextPressure" in session["missing"]


def test_cache_hit_math():
    assert um.cache_hit_of({"uncached_input": 100, "cache_read": 900}) == pytest.approx(0.9)
    assert um.cache_hit_of({"uncached_input": 0, "cache_read": 0}) == 0.0
    assert um.cache_hit_of({"uncached_input": 500}) == 0.0
    assert um.total_of({"uncached_input": 1, "output": 2, "cache_read": 3, "cache_write": 4}) == 10


# ---------------------------------------------------------------------------
# snapshot deltas
# ---------------------------------------------------------------------------


def _session(**kw) -> dict:
    doc = session_doc(str(_ROOT), **kw)
    return um.parse_session_doc(doc, pathlib.Path("/fake/session-x.json"))


def test_first_snapshot_delta_starts_at_session_start():
    session = _session(uncached=1000, output=200, cache_read=9000, steps=10, created_at=T0)
    record = um.build_snapshot_record(session, None, label="EFFICIENT", now=datetime.fromtimestamp((T0 + 600_000) / 1000))
    assert record["delta"]["basis"] == "session_start"
    assert record["delta"]["since_ts"] is None
    assert record["delta"]["requests"] == 10
    assert record["delta"]["total"] == 10200
    assert record["delta"]["uncached_input"] == 1000
    assert record["delta"]["seconds"] == pytest.approx(600.0)
    assert record["totals"]["cache_read"] == 9000


def test_second_snapshot_delta_is_previous_difference():
    first = um.build_snapshot_record(_session(uncached=1000, output=200, cache_read=9000, steps=10), None,
                                     label="EFFICIENT", now=datetime.fromtimestamp(T0 / 1000))
    later = _session(uncached=1500, output=500, cache_read=21_000, steps=14)
    record = um.build_snapshot_record(later, first, label="EFFICIENT",
                                     now=datetime.fromtimestamp((T0 + 1_800_000) / 1000))
    assert record["delta"]["basis"] == "previous_snapshot"
    assert record["delta"]["since_ts"] == first["ts"]
    assert record["delta"]["requests"] == 4
    assert record["delta"]["total"] == 1500 + 500 + 21_000 - 10_200
    assert record["delta"]["seconds"] == pytest.approx(1800.0)
    # 12,800 tokens over 4 requests and 0.5 h
    assert record["delta"]["avg_total_per_request"] == pytest.approx(3200.0)
    assert record["delta"]["avg_total_per_hour"] == pytest.approx(25_600.0)


def test_snapshot_clamps_regressed_totals():
    first = um.build_snapshot_record(_session(uncached=5_000, steps=20), None, label="X", now=datetime.fromtimestamp(T0 / 1000))
    lower = _session(uncached=10, steps=2)
    record = um.build_snapshot_record(lower, first, label="X", now=datetime.fromtimestamp((T0 + 60_000) / 1000))
    assert record["delta"]["totals_regressed"] is True
    assert record["delta"]["total"] == 0
    assert record["delta"]["requests"] == 0


def test_snapshot_ignores_previous_record_from_another_session():
    other = um.build_snapshot_record(_session(uncached=5000, steps=20), None, label="X")
    other["session"] = "session-someone-else"
    record = um.build_snapshot_record(_session(uncached=100, steps=1), other, label="X")
    assert record["delta"]["basis"] == "session_start"
    assert record["delta"]["requests"] == 1


def test_snapshot_record_carries_provenance_fields():
    session = _session(steps=3, surface=12_345)
    session["path"] = "/fake/procache/session-x.json"
    record = um.build_snapshot_record(session, None, label="MAX_RESULTS", note="after switch")
    assert record["schema"] == "usage_snapshot.v1"
    assert record["kind"] == "usage_snapshot"
    assert record["source"] == "/fake/procache/session-x.json"
    assert record["context_breakdown"]["system"] == 100
    assert record["label"] == "MAX_RESULTS"
    assert record["note"] == "after switch"


def test_append_and_read_usage_records_skip_noise(tmp_path):
    path = tmp_path / "model_usage.jsonl"
    good = um.build_snapshot_record(_session(steps=1, uncached=10), None, label="EFFICIENT")
    um.append_jsonl(path, good)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "ledger_event", "what": "mode_switch"}) + "\n")
        stream.write("{" + "truncated\n")
        stream.write("\n")
    um.append_jsonl(path, um.build_snapshot_record(_session(steps=2, uncached=20), good, label="MAX_RESULTS"))

    records, meta = um.read_usage_records(path)
    assert len(records) == 2
    assert meta["foreign"] == 1
    assert meta["malformed"] == 1
    assert records[0]["label"] == "EFFICIENT" and records[1]["label"] == "MAX_RESULTS"
    assert um.last_snapshot_for(records, records[1]["session"])["label"] == "MAX_RESULTS"


def test_read_usage_records_missing_file(tmp_path):
    records, meta = um.read_usage_records(tmp_path / "nope.jsonl")
    assert records == [] and meta["records"] == 0


# ---------------------------------------------------------------------------
# session discovery
# ---------------------------------------------------------------------------


def test_auto_session_prefers_toplevel_over_newer_subagent(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    root_path = write_procache(tmp_path, "session-root", session_doc(str(_ROOT), steps=30))
    sub_path = write_procache(tmp_path, "abc-sub", session_doc(str(_ROOT), steps=40))
    write_raw_log(tmp_path, str(_ROOT), "session-root", [], depth=0)
    write_raw_log(tmp_path, str(_ROOT), "abc-sub", [], parent="session-root", depth=1)
    # make the subagent session the newest file, as happens while teammates run
    import os
    os.utime(root_path, (1_700_000_000, 1_700_000_000))
    os.utime(sub_path, (1_800_000_000, 1_800_000_000))

    session = um.resolve_session(tmp_path, _ROOT, "auto")
    assert session["session"] == "session-root"
    newest = um.resolve_session(tmp_path, _ROOT, "newest")
    assert newest["session"] == "abc-sub"


def test_list_sessions_marks_delegation_and_foreign_cwd(tmp_path):
    write_procache(tmp_path, "session-root", session_doc(str(_ROOT), steps=1))
    write_procache(tmp_path, "abc-sub", session_doc(str(_ROOT), steps=2))
    write_procache(tmp_path, "session-elsewhere", session_doc("/somewhere/else", steps=3))
    write_raw_log(tmp_path, str(_ROOT), "session-root", [], depth=0)
    write_raw_log(tmp_path, str(_ROOT), "abc-sub", [], parent="session-root", depth=1)

    sessions = um.list_sessions(tmp_path, _ROOT)
    by_id = {s["session"]: s for s in sessions}
    assert set(by_id) == {"session-root", "abc-sub"}
    assert by_id["session-root"]["delegated"] is False
    assert by_id["abc-sub"]["delegated"] is True
    assert by_id["abc-sub"]["parent"] == "session-root"

    everything = um.list_sessions(tmp_path, _ROOT, all_cwds=True)
    assert "session-elsewhere" in {s["session"] for s in everything}


def test_resolve_session_by_id_and_unknown_id_error(tmp_path):
    write_procache(tmp_path, "session-root", session_doc(str(_ROOT), steps=5))
    assert um.resolve_session(tmp_path, _ROOT, "session-root")["steps"] == 5
    assert um.resolve_session(tmp_path, _ROOT, "root")["steps"] == 5  # prefix tolerated
    with pytest.raises(um.MeterError):
        um.resolve_session(tmp_path, _ROOT, "session-missing")


def test_delegate_usage_sums_child_sessions(tmp_path):
    write_procache(tmp_path, "session-root", session_doc(str(_ROOT), steps=10, uncached=1))
    write_procache(tmp_path, "aaa-child", session_doc(str(_ROOT), steps=4, uncached=100, output=10, cache_read=900))
    write_procache(tmp_path, "bbb-child", session_doc(str(_ROOT), steps=6, uncached=200, output=20, cache_read=1800))
    write_raw_log(tmp_path, str(_ROOT), "session-root", [], depth=0)
    write_raw_log(tmp_path, str(_ROOT), "aaa-child", [], parent="session-root", depth=1)
    write_raw_log(tmp_path, str(_ROOT), "bbb-child", [], parent="session-root", depth=1)
    write_raw_log(tmp_path, str(_ROOT), "unrelated", [], depth=0)

    payload = um.delegate_usage(tmp_path, "session-root", str(_ROOT))
    assert payload["count"] == 2
    assert payload["steps"] == 10
    assert payload["totals"] == {"uncached_input": 300, "output": 30, "cache_read": 2700, "cache_write": 0}
    assert payload["total"] == 3030


def test_find_raw_log_by_cwd_slug(tmp_path):
    cwd = "/Users/x/Documents/code/seer-automation-night"
    assert um.cwd_slug(cwd) == "--Users-x-Documents-code-seer-automation-night--"
    path = write_raw_log(tmp_path, cwd, "session-abc", [], depth=0)
    assert um.find_raw_log(tmp_path, "session-abc", cwd) == path
    assert um.find_raw_log(tmp_path, "abc", cwd) == path


# ---------------------------------------------------------------------------
# raw log: per-request events and hourly buckets
# ---------------------------------------------------------------------------


def test_usage_events_skip_requests_without_usage(tmp_path):
    log = write_raw_log(
        tmp_path, str(_ROOT), "session-root",
        [
            {"type": "tool/call", "seq": 1, "time": T0, "data": {}},
            usage_event(T0, 1, uncached=100, output=10, cache_read=900),
            {"type": "assistant/attempt", "seq": 3, "time": T0 + 10, "data": {"stream": []}},
            usage_event(T0 + 1000, 2, uncached=50, output=5, cache_read=950),
        ],
    )
    events = um.usage_events(log)
    assert len(events) == 2
    assert events[0]["uncached_input"] == 100 and events[0]["cache_read"] == 900
    assert events[1]["total"] == 1005


def test_hour_buckets_group_by_completion_hour_and_filter_day(tmp_path):
    later = T0 + 2 * HOUR_MS  # two hours after T0, on whatever local day that lands
    events = [
        usage_event(T0, 1, uncached=100, output=10, cache_read=900),
        usage_event(T0 + 60_000, 2, uncached=200, output=20, cache_read=1800),
        usage_event(T0 + HOUR_MS, 3, uncached=300, output=30, cache_read=2700),
        usage_event(later, 4, uncached=400, output=40, cache_read=3600),
    ]
    log = write_raw_log(tmp_path, str(_ROOT), "session-root", events)
    buckets = um.hour_buckets(um.usage_events(log))
    assert buckets["requests"] == 4
    assert len(buckets["rows"]) == 3
    assert buckets["rows"][0]["requests"] == 2
    assert buckets["rows"][0]["total"] == 100 + 10 + 900 + 200 + 20 + 1800
    assert buckets["rows"][0]["hour"] == local_hour(T0)
    assert buckets["sum"] == {"uncached_input": 1000, "output": 100, "cache_read": 9000, "cache_write": 0}

    day = local_hour(later)[:10]
    only_day = um.hour_buckets(um.usage_events(log), day=day)
    assert only_day["requests"] == 1
    assert all(row["hour"].startswith(day) for row in only_day["rows"])


def test_first_n_totals_compares_like_for_like(tmp_path):
    raw = [
        usage_event(T0, 1, uncached=100, output=10, cache_read=0),
        usage_event(T0, 2, uncached=200, output=20, cache_read=800),
        usage_event(T0, 3, uncached=300, output=30, cache_read=2700),
    ]
    log = write_raw_log(tmp_path, str(_ROOT), "session-root", raw)
    events = um.usage_events(log)
    prefix = um.first_n_totals(events, 2)
    assert prefix["requests"] == 2
    assert prefix["totals"]["uncached_input"] == 300
    assert prefix["total"] == 100 + 10 + 200 + 20 + 800
    assert prefix["total_per_request"] == pytest.approx(565.0)


def test_request_stats_reports_median_and_p95(tmp_path):
    raw = [usage_event(T0, i, uncached=(i + 1) * 100, output=10, cache_read=900) for i in range(20)]
    log = write_raw_log(tmp_path, str(_ROOT), "session-root", raw)
    stats = um.request_stats(um.usage_events(log))
    assert stats["requests"] == 20
    assert stats["prompt_median"] == pytest.approx(1950.0)  # mean of the two middle prompts
    assert stats["prompt_max"] == 20 * 100 + 900
    assert stats["output_mean"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# aggregation, flags, integrity
# ---------------------------------------------------------------------------


def _chain(tmp_path, values, labels):
    """Build a snapshot chain where values[i] = (uncached, output, cache_read, steps)."""
    records = []
    previous = None
    for index, (uncached, output, cache_read, steps) in enumerate(values):
        session = _session(uncached=uncached, output=output, cache_read=cache_read, steps=steps, surface=1000 + index * 1000)
        session["session"] = "session-root"
        record = um.build_snapshot_record(
            session, previous, label=labels[index], now=datetime.fromtimestamp((T0 + index * 1_800_000) / 1000)
        )
        records.append(record)
        previous = record
    return records


def test_aggregate_report_groups_deltas_by_label():
    records = _chain(
        None,
        [(1000, 100, 9000, 5), (3000, 300, 33_000, 15), (5000, 600, 60_000, 25)],
        ["EFFICIENT", "EFFICIENT", "MAX_RESULTS"],
    )
    report = um.aggregate_report(records)
    efficient = report["labels"]["EFFICIENT"]
    assert efficient["snapshots"] == 2
    assert efficient["requests"] == 15  # 5 + 10
    assert efficient["total"] == (1000 + 100 + 9000) + (2000 + 200 + 24_000)
    assert efficient["from_session_start"] == 1
    assert report["labels"]["MAX_RESULTS"]["requests"] == 10
    assert report["overall"]["requests"] == 25
    assert report["overall"]["total"] == 5000 + 600 + 60_000
    integrity = report["integrity"]["sessions"][0]
    assert integrity["identity_ok"] is True
    assert integrity["origin_known"] is True
    assert integrity["steps"] == 25


def test_aggregate_report_detects_session_change_and_zero_delta():
    first = um.build_snapshot_record(_session(steps=3, uncached=100), None, label="EFFICIENT")
    second = um.build_snapshot_record(_session(steps=3, uncached=100), first, label="EFFICIENT")
    first["session"] = "session-a"
    second["session"] = "session-b"
    report = um.aggregate_report([first, second])
    assert report["session_changes"], "a session change must be reported"
    assert report["session_changes"][0]["from"] == "session-a"
    assert report["zero_deltas"], "a zero-request interval must be reported"


def test_anomalies_flag_low_cache_hit_and_fast_surface_growth():
    low_hit = _chain(None, [(900_000, 1000, 100_000, 10)], ["MAX_RESULTS"])[0]
    report = um.aggregate_report([low_hit])
    flags = um.anomalies(report)
    assert any("cache hit below" in flag["what"] for flag in flags)

    fast_growth = _chain(
        None,
        [(1000, 100, 9000, 5), (2000, 200, 18_000, 10)],
        ["EFFICIENT", "EFFICIENT"],
    )
    # surface 1000 -> 2000 over 10 requests is slow; force a fast one by rewriting surface
    fast_growth[1]["surface_tokens"] = 80_000
    report = um.aggregate_report(fast_growth)
    flags = um.anomalies(report)
    assert any("grew faster" in flag["what"] for flag in flags)


def test_anomalies_report_baseline_comparison_only_for_mode_labels():
    records = _chain(None, [(1000, 100, 9000, 5)], ["EFFICIENT"])
    report = um.aggregate_report(records)
    flags = um.anomalies(report, {"total_per_request": 10_100.0})
    baseline_flags = [flag for flag in flags if "prior-night baseline" in flag["what"]]
    assert baseline_flags and baseline_flags[0]["label"] == "EFFICIENT"

    prep = _chain(None, [(1000, 100, 9000, 5)], ["PREP"])
    prep_flags = um.anomalies(um.aggregate_report(prep), {"total_per_request": 10_100.0})
    assert not any("prior-night baseline" in flag["what"] for flag in prep_flags)


def test_cli_report_excludes_prep_from_baseline_comparison(tmp_path, monkeypatch, capsys):
    usage_file = tmp_path / "model_usage.jsonl"
    first = um.build_snapshot_record(_session(uncached=1000, output=100, cache_read=9000, steps=10), None, label="PREP")
    second = um.build_snapshot_record(
        _session(uncached=2000, output=200, cache_read=18_000, steps=20), first, label="EFFICIENT"
    )
    um.append_jsonl(usage_file, first)
    um.append_jsonl(usage_file, second)
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(
        json.dumps({"session": "session-ref", "source": "/fake/procache.json", "total_per_request": 100_000.0, "requests": 656}),
        encoding="utf-8",
    )

    assert um.main(["report", "--file", str(usage_file), "--baseline", str(baseline_file)]) == 0
    text = capsys.readouterr().out
    assert "not a mode window" in text
    assert "EFFICIENT:" in text and "lower than baseline" in text
    assert "/fake/procache.json" in text


# ---------------------------------------------------------------------------
# baseline document
# ---------------------------------------------------------------------------


def test_build_baseline_has_provenance_and_reconciles(tmp_path):
    events = [
        usage_event(T0, 1, uncached=100, output=10, cache_read=900),
        usage_event(T0 + HOUR_MS, 2, uncached=200, output=20, cache_read=1800),
    ]
    log = write_raw_log(tmp_path, str(_ROOT), "session-ref", events)
    path = write_procache(tmp_path, "session-ref", session_doc(str(_ROOT), uncached=300, output=30, cache_read=2700, steps=2))
    session = um.load_session_file(path)

    baseline = um.build_baseline(session, log)
    assert baseline["schema"] == "usage_baseline.v1"
    assert baseline["requests"] == 2
    assert baseline["total"] == 3030
    assert baseline["total_per_request"] == pytest.approx(1515.0)
    assert baseline["reconcile"]["ok"] is True
    assert baseline["reconcile"]["raw_sum"]["cache_read"] == 2700
    assert len(baseline["hours"]) == 2
    assert baseline["source"] == str(path)
    assert baseline["raw_log"] == str(log)

    no_raw = um.build_baseline(session, None)
    assert no_raw["reconcile"]["ok"] is None
    assert no_raw["hours"] == []


def test_baseline_marks_raw_log_mismatch(tmp_path):
    events = [usage_event(T0, 1, uncached=100, output=10, cache_read=900)]
    log = write_raw_log(tmp_path, str(_ROOT), "session-ref", events)
    path = write_procache(tmp_path, "session-ref", session_doc(str(_ROOT), uncached=999, output=30, cache_read=2700, steps=1))
    baseline = um.build_baseline(um.load_session_file(path), log)
    assert baseline["reconcile"]["ok"] is False


# ---------------------------------------------------------------------------
# CLI end to end (fake DSH_HOME, temp output file)
# ---------------------------------------------------------------------------


def test_cli_snapshot_report_roundtrip(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    write_procache(
        tmp_path, "session-root",
        session_doc(str(_ROOT), uncached=1000, output=100, cache_read=9000, steps=10, surface=5000),
    )
    write_raw_log(tmp_path, str(_ROOT), "session-root", [], depth=0)
    usage_file = tmp_path / "model_usage.jsonl"

    assert um.main(["snapshot", "--label", "EFFICIENT", "--file", str(usage_file)]) == 0
    out = capsys.readouterr().out
    assert "session=session-root" in out
    assert "session_start" in out
    assert str(usage_file) in out

    # the procache grows, so the second snapshot must show a real delta
    write_procache(
        tmp_path, "session-root",
        session_doc(str(_ROOT), uncached=1400, output=180, cache_read=15_000, steps=14, surface=6500),
    )
    assert um.main(["snapshot", "--label", "MAX_RESULTS", "--file", str(usage_file)]) == 0
    out = capsys.readouterr().out
    assert "previous_snapshot" in out

    assert um.main(["report", "--file", str(usage_file), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["labels"]["EFFICIENT"]["requests"] == 10
    assert payload["labels"]["MAX_RESULTS"]["requests"] == 4
    assert payload["sources"]["usage_file"] == str(usage_file)
    assert payload["integrity"]["sessions"][0]["identity_ok"] is True

    assert um.main(["report", "--file", str(usage_file)]) == 0
    table = capsys.readouterr().out
    assert "EFFICIENT" in table and "MAX_RESULTS" in table and "OVERALL" in table
    assert str(usage_file) in table


def test_cli_hours_reconciles_with_procache(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    events = [
        usage_event(T0, 1, uncached=100, output=10, cache_read=900),
        usage_event(T0 + HOUR_MS, 2, uncached=200, output=20, cache_read=1800),
    ]
    write_raw_log(tmp_path, str(_ROOT), "session-root", events, depth=0)
    write_procache(tmp_path, "session-root", session_doc(str(_ROOT), uncached=300, output=30, cache_read=2700, steps=2))

    assert um.main(["hours", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requests"] == 2
    assert payload["reconcile_ok"] is True
    assert payload["sum_total"] == 3030

    assert um.main(["hours"]) == 0
    text = capsys.readouterr().out
    assert "reconcile raw log sum vs procache totals: OK" in text


def test_cli_sessions_and_error_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    write_procache(tmp_path, "session-root", session_doc(str(_ROOT), steps=3))
    write_raw_log(tmp_path, str(_ROOT), "session-root", [], depth=0)

    assert um.main(["sessions"]) == 0
    assert "session-root" in capsys.readouterr().out

    assert um.main(["sessions", "--all"]) == 0
    assert "session-root" in capsys.readouterr().out

    assert um.main(["report", "--file", str(tmp_path / "empty.jsonl")]) == 0
    assert "no snapshots yet" in capsys.readouterr().out

    monkeypatch.setenv("DSH_HOME", str(tmp_path / "empty-home"))
    assert um.main(["snapshot", "--label", "EFFICIENT", "--file", str(tmp_path / "x.jsonl")]) == 2
    assert "not found" in capsys.readouterr().err
