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
