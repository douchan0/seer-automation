"""CLI tests: doctor / replay / bench must run without the game or network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from seerauto.cli import main

ARTIFACTS = Path("artifacts")


def test_doctor_reports_honestly(capsys):
    rc = main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "seerauto" in out
    assert "game window:" in out  # FOUND / NOT FOUND, never a crash
    assert "honesty:" in out  # replay-tested vs live separation is printed
    assert "config:" in out
    assert "capture:" in out


def test_replay_limited_run(capsys):
    if not (ARTIFACTS / "quest.png").exists():
        pytest.skip("artifacts missing")
    rc = main(["replay", str(ARTIFACTS), "--limit", "3"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "replay:" in out
    assert "states:" in out
    assert "note: replay is replay-tested only" in out


def test_replay_writes_json_report(tmp_path, capsys):
    if not (ARTIFACTS / "flee.png").exists():
        pytest.skip("artifacts missing")
    out_path = tmp_path / "report.json"
    rc = main(["replay", str(ARTIFACTS), "--limit", "2", "--json", str(out_path)])
    assert rc == 0
    payload = json.loads(out_path.read_text())
    assert payload["mode"] == "replay"
    assert len(payload["records"]) == 2
    for record in payload["records"]:
        assert "state" in record
        assert record.get("state") in {
            "UNKNOWN", "IDLE", "BATTLE_TURN", "BATTLE_RESULT",
            "REWARD", "DIALOG", "ERROR", "PAUSED", "STOPPED",
        }
    capsys.readouterr()


def test_replay_missing_directory_exits_2(capsys):
    rc = main(["replay", "/nonexistent/fixtures"])
    assert rc == 2
    assert "fixtures" in capsys.readouterr().err


def test_replay_detects_victory_over_still_visible_battle_controls(tmp_path):
    fixtures = Path("data/replay/live_2026-11-02")
    if not fixtures.is_dir():
        pytest.skip("live replay set not present")
    output = tmp_path / "replay.json"
    assert main(["replay", str(fixtures), "--json", str(output)]) == 0
    records = {r["file"]: r for r in json.loads(output.read_text())["records"]}
    labels = json.loads((fixtures / "labels.json").read_text())["frames"]
    for label in labels:
        record = records[label["file"]]
        assert record["state"] == label["expect_state"], record
    victory = records["victory_moguguai.png"]
    assert victory["templates"]["battle_action_grid"] >= 0.7
    assert "skill" not in victory["proposed_action"]


def test_bench_microbenchmark(capsys):
    frame = ARTIFACTS / "scene_0.png"
    if not frame.exists():
        pytest.skip("artifacts missing")
    rc = main(["bench", "--frame", str(frame), "--iterations", "2"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "bench:" in out
    assert "frame budget:" in out
    assert "detect_template" in out
    assert "NOT a live capture measurement" in out


def test_bench_missing_frame_exits_2(capsys):
    rc = main(["bench", "--frame", "/nonexistent/frame.png"])
    assert rc == 2


def test_module_invocation_works_from_repo_root():
    """``python3 -m seerauto replay`` is an acceptance command; prove it runs."""
    if not (ARTIFACTS / "quest.png").exists():
        pytest.skip("artifacts missing")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [".pydeps", ".", env.get("PYTHONPATH", "")]))
    proc = subprocess.run(
        [sys.executable, "-m", "seerauto", "replay", str(ARTIFACTS), "--limit", "2"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(Path.cwd()),
    )
    assert proc.returncode == 0, proc.stderr
    assert "replay:" in proc.stdout
    assert "states:" in proc.stdout


def test_version_flag_exits_zero():
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0


def test_missing_subcommand_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code != 0
