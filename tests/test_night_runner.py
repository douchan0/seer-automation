"""Tests for the night runner and the fix_startup crash regression.

Part A (bottom of this file first): `tools/fix_startup.py` crashed with
`NameError: name 'time' is not defined` on the *act* path (line 68, ``time.sleep``)
because the module never imported ``time``.  `--check` never reaches that line, so a
`--check`-only test would have missed the bug: the regression test drives the act path
with fakes and asserts the sleep actually happened.

Part B: `tools/night_runner.py` is a thin auditable wrapper around the already
verified `tools/wild_cycle.py` — it adds a heartbeat, turns *numeric* ledger deltas
into control-plane milestones, and bounds restarts.  The tests pin the parts that
matter operationally: dry-run argv, heartbeat schema, "delta only, never liveness",
pause/stop cooperation, restart budget and backoff, and a truncated ledger tail.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import signal
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# =========================================================================== #
# Part A — tools/fix_startup.py: the crash that blocked tonight's bring-up
# =========================================================================== #
@pytest.fixture()
def fix_startup():
    return _load("fix_startup_under_test", "tools/fix_startup.py")


def test_fix_startup_binds_time(fix_startup):
    """The module must import time: the act path calls time.sleep after activating."""

    assert hasattr(fix_startup, "time")
    assert callable(fix_startup.time.sleep)
    source = (_ROOT / "tools" / "fix_startup.py").read_text(encoding="utf-8")
    assert "\nimport time\n" in source


def test_fix_startup_check_with_no_window_exits_4(fix_startup, monkeypatch, capsys):
    monkeypatch.setattr(fix_startup, "list_game_windows", lambda: [])
    monkeypatch.setattr(sys, "argv", ["fix_startup.py", "--check"])
    assert fix_startup.main() == 4
    assert "no SeerGame window" in capsys.readouterr().out


def test_fix_startup_check_reports_aux_dialog_without_clicking(fix_startup, monkeypatch,
                                                             capsys):
    windows = [{"id": 1, "w": 1200, "h": 800, "aux": False},
               {"id": 2, "w": 300, "h": 180, "aux": True}]
    monkeypatch.setattr(fix_startup, "list_game_windows", lambda: windows)

    def no_session(*_args, **_kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("--check must not open a LiveSession")

    monkeypatch.setattr(fix_startup, "LiveSession", no_session)
    monkeypatch.setattr(sys, "argv", ["fix_startup.py", "--check"])
    assert fix_startup.main() == 3
    assert "BLOCKED" in capsys.readouterr().out


def test_fix_startup_act_path_reaches_the_sleep(fix_startup, monkeypatch, capsys):
    """Regression: the old code NameError'd here.  Drive the act path with fakes."""

    windows = [{"id": 1, "w": 1200, "h": 800, "aux": False},
               {"id": 2, "w": 300, "h": 180, "aux": True}]
    state = {"aux": True}

    def list_windows():
        return [windows[0]] + ([windows[1]] if state["aux"] else [])

    monkeypatch.setattr(fix_startup, "list_game_windows", list_windows)

    class FakeClient:
        def __init__(self):
            self.sent = []

        def send(self, op, **kwargs):
            self.sent.append((op, kwargs))
            state["aux"] = False  # dismissing works in this fake
            return {}

    class FakeSession:
        def __init__(self, **_kwargs):
            self.client = FakeClient()

        def open(self):
            return self

        def close(self):
            pass

    slept = []
    monkeypatch.setattr(fix_startup, "LiveSession", FakeSession)
    monkeypatch.setattr(fix_startup.time, "sleep", lambda seconds: slept.append(seconds))

    class FakeResult:
        status = type("Status", (), {"value": "PASS"})()
        proof = "clicked 暂不导入"
        reason = ""

    class FakeSkill:
        def __call__(self, _context):
            return FakeResult()

    monkeypatch.setattr(fix_startup, "DismissStartupPopup", FakeSkill)
    monkeypatch.setattr(sys, "argv", ["fix_startup.py"])

    assert fix_startup.main() == 0
    # The exact statement that raised NameError before the fix:
    assert slept == [0.8]
    out = capsys.readouterr().out
    assert "skill PASS" in out
    assert "OK: auxiliary dialog is gone" in out


# =========================================================================== #
# Part B helpers — fakes, so the runner is tested without a game or a child
# =========================================================================== #
@pytest.fixture()
def nr():
    return _load("night_runner_under_test", "tools/night_runner.py")


class FakeClock:
    """Deterministic clock: sleeping advances it, so no test ever waits."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))


class FakeProcess:
    """A child stand-in: exits after ``alive_s`` fake seconds, or on SIGINT."""

    def __init__(self, clock: FakeClock, *, code: int = 0, alive_s: float = 2.0,
                 pid: int = 4321) -> None:
        self.clock = clock
        self.code = code
        self.pid = pid
        self.signals: list[int] = []
        self._exit_at = clock.now + alive_s
        self._exited = False

    def poll(self):
        if self._exited:
            return self.code
        if self.clock.now >= self._exit_at:
            self._exited = True
            return self.code
        return None

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGINT:
            self._exited = True
            self.code = 130
        return None

    def wait(self, timeout=None):
        return self.code


def _config(nr, tmp_path, **overrides):
    defaults = dict(
        minutes=0.05,  # 3 fake seconds: deterministic shutdown
        deadline_at=None,
        battles=3,
        heal_every=3,
        data_dir=tmp_path / "data",
        wild_cycle=tmp_path / "wild_cycle.py",
        wild_ledger=tmp_path / "wild.jsonl",
        wild_summary=tmp_path / "wild_summary.json",
        heartbeat_file=tmp_path / "data" / "executor_heartbeat.json",
        control=tmp_path / "overnight_control.py",
        python="python3",
        poll_s=1.0,
        heartbeat_s=5.0,
        max_restarts=3,
        backoff_s=(30.0, 60.0, 120.0),
        max_no_progress_runs=3,
        pause_files=(str(tmp_path / "pause"),),
        stop_file=str(tmp_path / "stop"),
    )
    defaults.update(overrides)
    return nr.Config(**defaults)


def _runner(nr, tmp_path, clock, *, codes=(), alive_s=2.0, sleep=None,
            config=None, on_spawn=None):
    spawned: list[list[str]] = []
    progress: list[list[str]] = []
    pending = list(codes)

    def spawn(argv, log_path):
        spawned.append(list(argv))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(f"child log line {i}" for i in range(30)) + "\n",
                            encoding="utf-8")
        if on_spawn is not None:
            on_spawn(argv)
        code = pending.pop(0) if pending else 0
        return FakeProcess(clock, code=code, alive_s=alive_s)

    def run_control(argv, timeout):
        if "status" in argv:
            return 0, json.dumps({"ok": True, "mode": "EFFICIENT"})
        if "progress" in argv:
            progress.append(list(argv))
            return 0, '{"ok":true}'
        return 0, '{"ok":true}'

    cfg = config or _config(nr, tmp_path)
    runner = nr.NightRunner(cfg, spawn=spawn, run_control=run_control, clock=clock,
                            sleep=sleep or clock.sleep,
                            child_log=tmp_path / "data" / "child.log")
    return runner, spawned, progress


def _append_ledger(path: pathlib.Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# =========================================================================== #
# Part B — dry run, argv, heartbeat
# =========================================================================== #
def test_dry_run_builds_the_child_argv_and_starts_nothing(nr, tmp_path, capsys):
    data = tmp_path / "data"
    wild = tmp_path / "wild.jsonl"
    code = nr.main(["--dry-run", "--minutes", "40", "--battles", "2",
                    "--heal-every", "3", "--data-dir", str(data),
                    "--wild-ledger", str(wild)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["dry_run"] is True and payload["starts_nothing"] is True
    argv = payload["child_argv"]
    assert argv[1].endswith("wild_cycle.py")
    assert argv[argv.index("--battles") + 1] == "2"
    assert argv[argv.index("--heal-every") + 1] == "3"
    assert argv[argv.index("--minutes") + 1] == "40.0"
    assert "--deadline-at" in argv
    assert payload["window_minutes"] == pytest.approx(40.0, abs=0.2)
    # A dry run must not create the data dir, the ledger or the heartbeat.
    assert not data.exists()
    assert not wild.exists()


def test_build_child_argv_passes_remaining_minutes_and_absolute_deadline(nr, tmp_path):
    config = _config(nr, tmp_path)
    argv = nr.build_child_argv(config, 2400.0, 1_800_003_400.0)
    assert argv[argv.index("--minutes") + 1] == "40.0"
    assert argv[argv.index("--deadline-at") + 1].endswith("+08:00")


def test_heartbeat_has_the_documented_schema(nr, tmp_path):
    clock = FakeClock()
    runner, _spawned, _progress = _runner(nr, tmp_path, clock)
    runner.started_epoch = clock.now
    runner.end_epoch = clock.now + 600
    runner._start_child(600.0)
    payload = json.loads((tmp_path / "data" / "executor_heartbeat.json").read_text())
    for key in ("ts", "ts_epoch", "pid", "child_pid", "started_at", "window_end",
                "last_output_ts", "wins_total", "xp_total",
                "consecutive_no_progress", "current_mode", "restarts", "milestones",
                "state"):
        assert key in payload, key
    assert payload["child_pid"] == 4321
    assert payload["current_mode"] == "EFFICIENT"  # read from overnight_control status
    assert payload["wins_total"] == 0
    assert payload["state"] == "running"


# =========================================================================== #
# Part B — milestones only on a real numeric delta
# =========================================================================== #
def test_milestone_only_on_numeric_delta(nr, tmp_path):
    clock = FakeClock()
    ledger = tmp_path / "wild.jsonl"
    # A previous night's record must not count: the runner starts at EOF.
    _append_ledger(ledger, [{"pass": 1, "wins": 2, "xp": 104.0, "at": "old"}])
    runner, _spawned, progress = _runner(nr, tmp_path, clock)
    runner.tail.skip_to_end()

    _append_ledger(ledger, [{"pass": 2, "wins": 0, "xp": 0.0, "outcome": "NO_WORK"}])
    assert runner.drain_ledger() == []
    assert runner.wins == 0 and runner.consecutive_no_progress == 1
    assert progress == []  # activity is not progress

    _append_ledger(ledger, [{"pass": 3, "wins": 2, "xp": 104.0,
                             "level": {"name": "烈焰猩猩", "level": 41}}])
    emitted = runner.drain_ledger()
    assert len(emitted) == 1
    assert len(progress) == 1
    argv = progress[0]
    assert argv[argv.index("--node") + 1] == "wild.farm"
    proof = json.loads(argv[argv.index("--proof") + 1])
    assert proof["wins"] == 2 and proof["xp"] == 104.0
    assert runner.wins == 2 and runner.xp == 104.0
    assert runner.consecutive_no_progress == 0


def test_milestone_is_not_repeated_for_an_already_read_record(nr, tmp_path):
    clock = FakeClock()
    ledger = tmp_path / "wild.jsonl"
    runner, _spawned, progress = _runner(nr, tmp_path, clock)
    runner.tail.skip_to_end()
    _append_ledger(ledger, [{"pass": 1, "wins": 1, "xp": 106.0}])
    runner.drain_ledger()
    runner.drain_ledger()  # no new bytes -> no second milestone
    assert len(progress) == 1
    assert runner.wins == 1


# =========================================================================== #
# Part B — pause, restart budget, truncated tail, full window
# =========================================================================== #
def test_pause_sigints_the_child_and_is_not_a_restart(nr, tmp_path):
    clock = FakeClock()
    pause = tmp_path / "pause"
    calls = {"n": 0}
    real_sleep = clock.sleep

    def sleep(seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            pause.write_text("", encoding="utf-8")
        elif calls["n"] == 2 and pause.exists():
            pause.unlink()
        real_sleep(seconds)

    runner, spawned, _progress = _runner(nr, tmp_path, clock, alive_s=999.0,
                                         sleep=sleep)
    final = runner.run()
    assert len(spawned) == 2  # child 1 was paused, child 2 after resume
    assert final["restarts"] == 0  # a pause is not a failure
    assert final["outcome"] == nr.Outcome.DEADLINE_REACHED.value
    events = [json.loads(line) for line in
              (tmp_path / "data" / "events.jsonl").read_text().splitlines()]
    assert any(e.get("kind") == "runner.paused" for e in events)
    assert not pause.exists()


def test_restart_budget_uses_30_60_120_backoff_and_then_fails(nr, tmp_path):
    clock = FakeClock()
    runner, spawned, _progress = _runner(nr, tmp_path, clock, codes=[1, 1, 1, 1],
                                         alive_s=0.5,
                                         config=_config(nr, tmp_path, minutes=30.0))
    final = runner.run()
    assert final["outcome"] == nr.Outcome.FAILED.value
    assert final["restarts"] == 3
    assert "budget" in final["reason"]
    assert len(spawned) == 4  # first run + three restarts
    failures = [json.loads(line) for line in
                (tmp_path / "data" / "failures.jsonl").read_text().splitlines()]
    restarts = [f for f in failures if f["kind"] == "restart"]
    assert [f["backoff_s"] for f in restarts] == [30.0, 60.0, 120.0]
    assert all(len(f["log_tail"]) == 20 for f in restarts)
    assert all(f["outcome"] == nr.Outcome.FAILED.value for f in restarts)
    terminal = [f for f in failures if f["kind"] == "runner.failed"]
    assert len(terminal) == 1 and terminal[0]["restarts"] == 3


def test_no_progress_runs_stop_with_no_work(nr, tmp_path):
    clock = FakeClock()
    ledger = tmp_path / "wild.jsonl"

    def on_spawn(_argv):
        _append_ledger(ledger, [{"pass": 1, "wins": 0, "xp": 0.0}])

    runner, spawned, progress = _runner(nr, tmp_path, clock, codes=[10, 10, 10, 10],
                                        alive_s=0.5, on_spawn=on_spawn,
                                        config=_config(nr, tmp_path, minutes=30.0))
    final = runner.run()
    assert final["outcome"] == nr.Outcome.NO_WORK.value
    assert "no numeric progress" in final["reason"]
    assert progress == []  # nothing moved -> no milestone
    assert final["restarts"] == 0


def test_truncated_ledger_line_is_tolerated_and_not_double_counted(nr, tmp_path):
    clock = FakeClock()
    ledger = tmp_path / "wild.jsonl"
    runner, _spawned, progress = _runner(nr, tmp_path, clock)
    runner.tail.skip_to_end()

    _append_ledger(ledger, [{"pass": 1, "wins": 1, "xp": 50.0}])
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write('{"pass": 2, "wins": 99')  # crash mid-write
    assert len(runner.drain_ledger()) == 1
    assert runner.tail.malformed == 0
    # The half-written line must not be counted until it is completed.
    assert len(runner.drain_ledger()) == 0

    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(', "xp": 60.0}\n')
    emitted = runner.drain_ledger()
    assert len(emitted) == 1
    assert runner.wins == 100
    assert len(progress) == 2


def test_happy_window_records_milestones_and_a_final_json(nr, tmp_path):
    clock = FakeClock()
    ledger = tmp_path / "wild.jsonl"

    def on_spawn(_argv):
        _append_ledger(ledger, [{"pass": 1, "wins": 2, "xp": 104.0,
                                 "level": {"name": "烈焰猩猩", "level": 41}}])

    runner, spawned, progress = _runner(nr, tmp_path, clock, alive_s=999.0,
                                        on_spawn=on_spawn)
    final = runner.run()
    assert final["outcome"] == nr.Outcome.DEADLINE_REACHED.value
    for key in ("outcome", "reason", "elapsed_s", "wins", "xp", "restarts",
                "milestones"):
        assert key in final, key
    assert final["wins"] == 2 and final["xp"] == 104.0
    assert final["milestones"] == 1
    assert len(progress) == 1
    events = [json.loads(line) for line in
              (tmp_path / "data" / "events.jsonl").read_text().splitlines()]
    assert events[-1]["kind"] == "runner.final"
    heartbeat = json.loads((tmp_path / "data" / "executor_heartbeat.json").read_text())
    assert heartbeat["state"] == "stopped"
    assert heartbeat["wins_total"] == 2
