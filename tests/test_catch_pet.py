"""Tests for CAP-CATCH: `seerauto.skills.catch` + `tools/catch_pet.py`.

Everything here is **UNIT_ONLY** — no game, no client, no OCR.  The live client is
owned by another agent; the executable acceptance path is the live agent filling in
`seerauto/config/catch_calibration.json` and running `tools/catch_pet.py`.

What these tests pin, in order of how much damage the bug would do:

* calibration is a refusal surface: missing / uncalibrated / incomplete / a moved
  window must all *refuse to click*, with the missing fields named;
* the click chain is exactly 道具 → capsule slot → 使用/投掷 (+ confirm words), and
  a hard denylist makes 放生/出售/购买/… unreachable;
* PASS requires two consecutive agreeing result-text readings; unreadable evidence is
  UNVERIFIED_ACTIVITY, never PASS;
* the budget (≤5 throws, ≥1 reserve) and the 3-failure stop are enforced;
* the ledger is append-only JSONL and tolerates a crash-truncated last line.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

from seerauto.dual.outcome import Outcome
from seerauto.skills.base import Budget, SkillContext, Status
from seerauto.skills.catch import (DEFAULT_CALIBRATION, HARD_MAX_THROWS,
                                   CatchLimits, CatchPet, CatchSession,
                                   CalibrationError, FORBIDDEN_WORDS,
                                   append_catch_ledger, calibration_problems,
                                   load_calibration, read_catch_ledger)
from seerauto.world.model import GameWorldState

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("catch_pet_cli",
                                               _ROOT / "tools" / "catch_pet.py")
cli = importlib.util.module_from_spec(_SPEC)
sys.modules["catch_pet_cli"] = cli
_SPEC.loader.exec_module(cli)


# --------------------------------------------------------------------------- #
# fakes: an OCR-scripted session, and calibration files
# --------------------------------------------------------------------------- #
class FakeSession:
    """Returns scripted OCR frames, one per call; records every click."""

    def __init__(self, frames: list[list[dict]], *, pixel_size=(2400, 1600)):
        self.frames = frames
        self.calls = 0
        self.clicks: list[tuple[float, float, str]] = []
        self.pixel_size = pixel_size
        self.closed = False

    def ocr(self, *, min_conf: float = 0.3, path: str | None = None, **_kw):
        index = min(self.calls, len(self.frames) - 1)
        self.calls += 1
        return [dict(item) for item in self.frames[index]]

    def click_px(self, x, y, reason="", **_kw):
        self.clicks.append((float(x), float(y), reason))
        return SimpleNamespace(sent=True, refused=None)

    def close(self):
        self.closed = True


def item(text: str, x: float = 800.0, y: float = 200.0) -> dict:
    return {"text": text, "conf": 0.95, "px": (x, y)}


def ctx_for(session: FakeSession) -> SkillContext:
    return SkillContext(session=session, world=GameWorldState(), dry_run=False,
                        allow_input=True,
                        budget=Budget(max_duration_s=60.0, max_actions=20))


def _raw_calibration() -> dict:
    return json.loads(DEFAULT_CALIBRATION.read_text(encoding="utf-8"))


def _write_calibration(tmp_path: pathlib.Path, *, calibrated: bool = True,
                       **overrides) -> pathlib.Path:
    raw = _raw_calibration()
    raw["battle"]["item_button_px"] = [1900.0, 1200.0]
    raw["battle"]["item_panel"]["capsule_slot_px"] = [1000.0, 900.0]
    raw["battle"]["item_panel"]["use_button_px"] = [1200.0, 900.0]
    raw["status"] = "CALIBRATED" if calibrated else "UNCALIBRATED"
    raw["calibrated_at"] = "2026-11-06T23:50:00+08:00" if calibrated else None
    raw["calibrated_by"] = "pytest" if calibrated else None
    for key, value in overrides.items():
        node = raw
        parts = key.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    path = tmp_path / "catch_calibration.json"
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _calibrated(tmp_path: pathlib.Path, **overrides):
    return load_calibration(_write_calibration(tmp_path, **overrides),
                            require_calibrated=True)


def _skill(tmp_path: pathlib.Path, *, clock=None, sleep=None, limits=None):
    calibration = _calibrated(tmp_path)
    if limits is None:
        # Keep the user's real pause/stop protection intact while fakes run.
        limits = CatchLimits(pause_files=(str(tmp_path / "pause"),),
                             stop_file=str(tmp_path / "stop"))
    return CatchPet(
        calibration, limits=limits,
        ledger_path=tmp_path / "catch_ledger.jsonl",
        clock=clock or (lambda: 1_800_000_000.0),
        sleep=sleep or (lambda _s: None)), calibration


def _frames(*groups: list[dict], repeat: int = 1) -> list[list[dict]]:
    """The session's OCR calls, two identical samples per phase (SPEC §7)."""

    frames: list[list[dict]] = []
    for group in groups:
        frames.append(group)
        frames.append(group)
    return frames * repeat


def _attempt_frames(name: str = "对方【皮皮】登场了！", verdict: str = "捕捉成功！",
                    *, panel: bool = True, selected: bool = True) -> list[list[dict]]:
    group = [item(name)]
    panel_group = [item("道具"), item("胶囊")]
    selected_group = [item("已选择 胶囊 x1")]
    verdict_group = [item(verdict, 800.0, 500.0)]
    out: list[list[dict]] = []
    for g in (group, panel_group, selected_group, verdict_group):
        out.append(g)
        out.append(g)
    return out


# --------------------------------------------------------------------------- #
# calibration is a refusal surface
# --------------------------------------------------------------------------- #
def test_missing_calibration_file_raises_with_path(tmp_path):
    with pytest.raises(CalibrationError) as excinfo:
        load_calibration(tmp_path / "nope.json")
    assert "not found" in str(excinfo.value)
    assert "nope.json" in str(excinfo.value)


def test_shipped_calibration_is_uncalibrated_and_names_the_missing_fields():
    info = calibration_problems()
    assert info["calibrated"] is False
    assert set(info["required_fields"]) == {
        "battle.item_button_px", "battle.item_panel.capsule_slot_px",
        "battle.item_panel.use_button_px", "battle.report_roi_px",
        "battle.result_roi_px", "battle.opponent_regex",
        "battle.result_keywords.success", "battle.result_keywords.escaped"}
    assert any("item_button_px" in problem for problem in info["problems"])
    with pytest.raises(CalibrationError) as excinfo:
        load_calibration(require_calibrated=True)
    assert any("not CALIBRATED" in problem for problem in excinfo.value.problems)


def test_incomplete_calibration_lists_every_missing_click_target(tmp_path):
    path = _write_calibration(tmp_path, **{"battle.item_button_px": None})
    with pytest.raises(CalibrationError) as excinfo:
        load_calibration(path)
    assert any("battle.item_button_px" in problem
               for problem in excinfo.value.problems)


def test_bad_coordinates_are_rejected_not_clicked(tmp_path):
    path = _write_calibration(tmp_path,
                              **{"battle.item_button_px": [9999.0, 1200.0]})
    with pytest.raises(CalibrationError) as excinfo:
        load_calibration(path)
    assert any("outside the calibrated capture" in p for p in excinfo.value.problems)

    path = _write_calibration(tmp_path,
                              **{"battle.item_panel.use_button_px": [1.0]})
    with pytest.raises(CalibrationError) as excinfo:
        load_calibration(path)
    assert any("use_button_px must be [x, y]" in p for p in excinfo.value.problems)


def test_budget_and_confirm_words_cannot_be_widened(tmp_path):
    path = _write_calibration(tmp_path,
                              **{"budget.max_throws_per_session": 9})
    with pytest.raises(CalibrationError) as excinfo:
        load_calibration(path)
    assert any("between 1 and 5" in p for p in excinfo.value.problems)

    path = _write_calibration(tmp_path,
                              **{"safety.confirm_words": ["确认", "放生"]})
    with pytest.raises(CalibrationError) as excinfo:
        load_calibration(path)
    assert any("forbidden word" in p or "may only use" in p
               for p in excinfo.value.problems)


def test_uncalibrated_object_refuses_to_run_and_clicks_nothing(tmp_path):
    calibration = load_calibration(str(_write_calibration(tmp_path,
                                                          calibrated=False)),
                                    require_calibrated=False)
    assert calibration.calibrated is False
    session = FakeSession([[item("对方【皮皮】登场了！")]])
    skill = CatchPet(calibration, ledger_path=tmp_path / "ledger.jsonl")
    result = skill(ctx_for(session), target="pippi")
    assert result.status is Status.BLOCKED
    assert result.data["outcome"] == Outcome.BLOCKED.value
    assert session.clicks == []
    assert not (tmp_path / "ledger.jsonl").exists()


def test_window_geometry_mismatch_refuses_to_click(tmp_path):
    skill, _calibration = _skill(tmp_path)
    session = FakeSession([[item("对方【皮皮】登场了！")]], pixel_size=(2000, 1400))
    result = skill(ctx_for(session), target="pippi")
    assert result.status is Status.BLOCKED
    assert "window geometry changed" in result.reason
    assert session.clicks == []


# --------------------------------------------------------------------------- #
# happy path: evidence, exact click chain, ledger
# --------------------------------------------------------------------------- #
def test_success_path_passes_only_with_result_text_and_records_the_ledger(tmp_path):
    skill, calibration = _skill(tmp_path)
    session = FakeSession(_attempt_frames())
    result = skill(ctx_for(session), target="pippi")

    assert result.status is Status.PASS
    assert result.data["outcome"] == Outcome.PROGRESS.value
    assert result.data["captures"] == 1
    assert result.data["throws_used"] == 1
    assert result.data["target_name"] == "皮皮"
    assert "捕捉成功" in result.proof
    assert [click[2] for click in session.clicks] == ["item_button", "capsule_slot",
                                                      "use_button"]
    assert session.clicks[0][:2] == (1900.0, 1200.0)  # calibrated coordinates only

    records = read_catch_ledger(tmp_path / "catch_ledger.jsonl")
    assert [record["kind"] for record in records] == ["session.start", "attempt",
                                                      "session.end"]
    attempt = records[1]
    assert attempt["outcome"] == Outcome.PROGRESS.value
    assert attempt["clicks"][0]["reason"] == "item_button"
    assert attempt["capsules_before"] is None  # inventory ROI not calibrated yet
    assert records[0]["calibration"]["capture_px"] == [2400, 1600]


def test_wrong_target_never_throws(tmp_path):
    skill, _calibration = _skill(tmp_path)
    session = FakeSession(_attempt_frames(name="对方【蘑菇怪】登场了！"))
    result = skill(ctx_for(session), target="pippi")
    assert result.status is Status.BLOCKED
    assert "wrong target" in result.reason
    assert session.clicks == []


def test_level_above_limit_never_throws(tmp_path):
    skill, _calibration = _skill(tmp_path)
    session = FakeSession(_attempt_frames(name="对方【皮皮】Lv9登场了！"))
    result = skill(ctx_for(session), target="pippi")
    assert result.status is Status.BLOCKED
    assert "level 9" in result.reason
    assert session.clicks == []


def test_item_panel_not_opening_is_not_clicked_twice(tmp_path):
    skill, _calibration = _skill(tmp_path)
    frames = _frames([item("对方【皮皮】登场了！")], [item("无关文字")],
                     [item("无关文字")], [item("无关文字")])
    session = FakeSession(frames)
    result = skill(ctx_for(session), target="pippi")
    assert result.status is Status.BLOCKED  # unknown UI state -> human, not a retry
    assert result.data["outcome"] == Outcome.BLOCKED.value
    assert "did not appear" in result.reason
    assert [click[2] for click in session.clicks] == ["item_button"]  # one click only


# --------------------------------------------------------------------------- #
# UNVERIFIED_ACTIVITY: the safety valve
# --------------------------------------------------------------------------- #
def test_no_result_evidence_returns_unverified_not_pass(tmp_path):
    skill, _calibration = _skill(tmp_path)
    noise = [item("战报滚动中...")]
    frames = _frames([item("对方【皮皮】登场了！")], [item("道具"), item("胶囊")],
                     [item("已选择 胶囊")], noise)
    session = FakeSession(frames)
    result = skill(ctx_for(session), target="pippi")

    assert result.status is not Status.PASS
    assert result.status is Status.FAIL
    assert result.data["outcome"] == Outcome.UNVERIFIED_ACTIVITY.value
    assert "UNVERIFIED" in result.reason or "no capture-result evidence" in result.reason
    assert result.proof == ""
    # One throw was attempted; the session must NOT throw a second capsule blindly.
    assert [click[2] for click in session.clicks].count("use_button") == 1


def test_unreadable_battle_report_never_throws(tmp_path):
    skill, _calibration = _skill(tmp_path)
    session = FakeSession(_frames([item("")], [item("***")]))
    result = skill(ctx_for(session), target="pippi")
    assert result.status is not Status.PASS
    assert result.data["outcome"] == Outcome.UNVERIFIED_ACTIVITY.value
    assert session.clicks == []


# --------------------------------------------------------------------------- #
# budgets and failure limits
# --------------------------------------------------------------------------- #
def test_throws_are_capped_by_the_cli_budget(tmp_path):
    skill, _calibration = _skill(tmp_path)
    session = FakeSession(_attempt_frames(verdict="挣脱了！") * 4)
    result = skill(ctx_for(session), target="pippi", max_throws=1)
    assert result.data["outcome"] == Outcome.FAILED.value
    assert result.data["throws_used"] == 1
    assert [click[2] for click in session.clicks].count("use_button") == 1
    assert "budget" in result.reason


def test_three_failures_stop_the_session(tmp_path):
    skill, _calibration = _skill(tmp_path)
    session = FakeSession(_attempt_frames(verdict="挣脱了！") * 3)
    result = skill(ctx_for(session), target="pippi")
    assert result.data["outcome"] == Outcome.FAILED.value
    assert result.data["failures"] == 3
    assert len(result.data["attempts"]) == 3
    assert "3 failed attempt(s)" in result.reason
    assert [click[2] for click in session.clicks].count("use_button") == 3


def test_third_attempt_can_still_succeed(tmp_path):
    skill, _calibration = _skill(tmp_path)
    frames = (_attempt_frames(verdict="挣脱了！") * 2) + _attempt_frames()
    session = FakeSession(frames)
    result = skill(ctx_for(session), target="pippi")
    assert result.status is Status.PASS
    assert result.data["failures"] == 2
    assert result.data["throws_used"] == 3


def test_escaped_is_a_failure_not_a_capture(tmp_path):
    skill, _calibration = _skill(tmp_path)
    session = FakeSession(_attempt_frames(verdict="胶囊被挣脱了！") * 3)
    result = skill(ctx_for(session), target="pippi")
    assert result.status is not Status.PASS
    assert result.data["captures"] == 0
    assert any("escaped" in attempt["reason"]
               for attempt in result.data["attempts"])


def test_opponent_flee_is_no_work(tmp_path):
    skill, _calibration = _skill(tmp_path)
    session = FakeSession(_attempt_frames(verdict="对方逃跑了！"))
    result = skill(ctx_for(session), target="pippi")
    assert result.data["outcome"] == Outcome.NO_WORK.value
    assert result.status is Status.SKIPPED
    assert result.data["throws_used"] == 1


# --------------------------------------------------------------------------- #
# cooperation flags and deadline
# --------------------------------------------------------------------------- #
def test_pause_flag_stops_before_any_click(tmp_path):
    pause = tmp_path / "pause"
    pause.write_text("", encoding="utf-8")
    skill, _calibration = _skill(tmp_path, limits=CatchLimits(
        pause_files=(str(pause),), stop_file=str(tmp_path / "stop")))
    session = FakeSession(_attempt_frames())
    result = skill(ctx_for(session), target="pippi")
    assert result.data["outcome"] == Outcome.PAUSED.value
    assert session.clicks == []


def test_stop_flag_and_deadline_stop_before_any_click(tmp_path):
    stop = tmp_path / "stop"
    stop.write_text("", encoding="utf-8")
    skill, _calibration = _skill(tmp_path, limits=CatchLimits(
        pause_files=(), stop_file=str(stop)))
    session = FakeSession(_attempt_frames())
    result = skill(ctx_for(session), target="pippi")
    assert result.data["outcome"] == Outcome.PAUSED.value

    skill, _calibration = _skill(tmp_path, limits=CatchLimits(
        pause_files=(), stop_file=str(tmp_path / "none"),
        deadline_epoch=1_700_000_000.0))
    session = FakeSession(_attempt_frames())
    result = skill(ctx_for(session), target="pippi")
    assert result.data["outcome"] == Outcome.DEADLINE_REACHED.value
    assert session.clicks == []


# --------------------------------------------------------------------------- #
# safety: allowlist, denylist, ledger durability
# --------------------------------------------------------------------------- #
def test_click_reason_and_forbidden_text_are_enforced(tmp_path):
    skill, calibration = _skill(tmp_path)
    session = FakeSession(_attempt_frames())
    catch_session = CatchSession(calibration, ledger_path=None,
                                 clock=lambda: 1_800_000_000.0,
                                 sleep=lambda _s: None)
    attempt = SimpleNamespace(clicks=[], evidence=[])
    from seerauto.skills.catch import CatchAttempt, SafetyViolation
    attempt = CatchAttempt(attempt=1, outcome=Outcome.FAILED)
    with pytest.raises(SafetyViolation):
        catch_session._click(ctx_for(session), (1.0, 2.0), "放生", attempt)
    with pytest.raises(SafetyViolation):
        catch_session._click(ctx_for(session), (1.0, 2.0), "confirm:放生", attempt)
    assert session.clicks == []
    assert "放生" in FORBIDDEN_WORDS


def test_ledger_tolerates_a_truncated_last_line(tmp_path):
    ledger = tmp_path / "catch_ledger.jsonl"
    append_catch_ledger(ledger, {"kind": "session.start", "target": "pippi"})
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "attention", "outco')  # process died mid-write
    records = read_catch_ledger(ledger)
    assert [record["kind"] for record in records] == ["session.start"]
    append_catch_ledger(ledger, {"kind": "session.end", "outcome": "PROGRESS"})
    records = read_catch_ledger(ledger)
    assert [record["kind"] for record in records] == ["session.start", "session.end"]


# --------------------------------------------------------------------------- #
# CLI: dry-run is the safe entry point, and the real path needs --allow-input
# --------------------------------------------------------------------------- #
def test_cli_dry_run_prints_plan_and_gaps_without_touching_anything(tmp_path, capsys):
    ledger = tmp_path / "catch_ledger.jsonl"
    code = cli.main(["--dry-run", "--calibration", str(DEFAULT_CALIBRATION),
                     "--ledger", str(ledger), "--target", "pippi"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["dry_run"] is True and payload["starts_nothing"] is True
    assert payload["calibration"]["calibrated"] is False
    assert payload["target"] == "pippi" and payload["target_known"] is True
    assert payload["blocked_reason"].startswith("calibration is not CALIBRATED")
    assert payload["safety"]["never"]
    assert not ledger.exists()


def test_cli_dry_run_with_a_calibrated_file_prints_the_real_command(tmp_path, capsys):
    path = _write_calibration(tmp_path)
    code = cli.main(["--dry-run", "--calibration", str(path),
                     "--ledger", str(tmp_path / "ledger.jsonl")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["calibration"]["calibrated"] is True
    assert "--allow-input" in payload["command"]
    assert payload["max_capsules_effective"] <= HARD_MAX_THROWS


def test_cli_refuses_bad_max_capsules_and_missing_allow_input(tmp_path, capsys):
    assert cli.main(["--dry-run", "--max-capsules", "9"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and "1..5" in payload["error"]

    path = _write_calibration(tmp_path)
    assert cli.main(["--calibration", str(path),
                     "--ledger", str(tmp_path / "ledger.jsonl")]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert "allow-input" in payload["error"]


def test_cli_real_path_refuses_uncalibrated_calibration(tmp_path, capsys):
    code = cli.main(["--calibration", str(DEFAULT_CALIBRATION), "--allow-input",
                     "--ledger", str(tmp_path / "ledger.jsonl")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["ok"] is False
    assert payload["problems"]
