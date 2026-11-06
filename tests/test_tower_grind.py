"""Tests for `tools/tower_grind.py` — the guardian-body tower grinder.

Everything here is UNIT_ONLY: scripted OCR frames, a fake battle runner, no game.
The live path is the operator's: fill `seerauto/config/tower_calibration.json`,
run `--dry-run` until it says `calibrated: true`, then `--allow-input`.

The tests pin the things that made the previous tower tooling fail:
* the guardian body must be clicked at most 3 times and then BLOCKED (never blind);
* the painted floor needs two consecutive agreeing readings (no guesses);
* a won battle whose floor did not advance is UNVERIFIED_ACTIVITY, not progress;
* a defeat re-enters the floor picker a bounded number of times;
* PP exhaustion is BLOCKED/`pp_exhausted`, not more clicking.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

from seerauto.dual.outcome import Outcome
from seerauto.world.model import GameWorldState

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("tower_grind_cli",
                                               _ROOT / "tools" / "tower_grind.py")
cli = importlib.util.module_from_spec(_SPEC)
sys.modules["tower_grind_cli"] = cli
_SPEC.loader.exec_module(cli)

from seerauto.skills.base import Budget, SkillContext  # noqa: E402


class FakeSession:
    def __init__(self, frames: list[list[dict]], *, pixel_size=(2400, 1600)):
        self.frames = frames
        self.calls = 0
        self.sent: list[tuple[str, dict]] = []
        self.client = self
        self.clicks: list[tuple[float, float, str]] = []
        self.pixel_size = pixel_size
        self.closed = False

    def ocr(self, *, min_conf: float = 0.3, path: str | None = None, **_kw):
        index = min(self.calls, len(self.frames) - 1)
        self.calls += 1
        return [dict(item) for item in self.frames[index]]

    def click_px(self, x, y, reason="", **_kw):
        self.clicks.append((float(x), float(y), reason))
        return object()

    def latest(self):
        return FakeFrame(OUTSIDE_BGR)

    def send(self, op, **kwargs):
        self.sent.append((op, dict(kwargs)))
        return {"ok": True}

    def close(self):
        self.closed = True


def item(text: str, x: float = 500.0, y: float = 400.0) -> dict:
    return {"text": text, "conf": 0.95, "px": (x, y)}


def ctx_for(session: FakeSession) -> SkillContext:
    return SkillContext(session=session, world=GameWorldState(), dry_run=False,
                        allow_input=True, budget=Budget(max_duration_s=600))


def _raw() -> dict:
    return json.loads((_ROOT / "seerauto" / "config" / "tower_calibration.json")
                      .read_text(encoding="utf-8"))


def _write_calibration(tmp_path: pathlib.Path, *, calibrated: bool = True) -> pathlib.Path:
    raw = _raw()
    raw["status"] = "CALIBRATED" if calibrated else "UNCALIBRATED"
    raw["tower"]["inside"]["floor_roi_px"] = [100.0, 1050.0, 700.0, 1400.0]
    raw["tower"]["inside"]["signature"] = {
        "mode": "roi_mean_color",
        "roi_px": [1150.0, 1230.0, 1340.0, 1370.0],
        "expect_rgb": [60, 180, 170],
        "tolerance": 40,
        "channel_order": "RGB",
    }
    path = tmp_path / "tower_calibration.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return path


def _cal(tmp_path, *, calibrated=True):
    return cli.load_calibration(_write_calibration(tmp_path, calibrated=calibrated))


DIALOG = [item("选择楼层", 1484, 887), item("继续挑战", 1481, 1007),
          item("你当前到达第 23 层", 900, 500)]
FLOOR1 = [item("01", 400, 1200)]
FLOOR2 = [item("02", 400, 1200)]
BATTLE = [item("第 1 回合", 700, 300), item("当前战报", 700, 500)]
RESULT = [item("XIN精灵获得经验：1710", 900, 800),
          item("离升级还需经验：4650", 900, 860)]
CONFIRM = [item("确认", 1200, 1000)]


def _happy_frames() -> list[list[dict]]:
    return [DIALOG, DIALOG,          # screen classify -> dialog
            FLOOR1, FLOOR1,          # floor after preset (two agreeing)
            BATTLE, BATTLE,          # battle markers (engage skipped)
            RESULT, RESULT, RESULT,  # result sample + one extra OCR
            CONFIRM, [],             # dismiss one dialog
            FLOOR2, FLOOR2]          # NEXT: floor advanced


NOW = 1_800_000_000.0


def _grinder(calibration, **kwargs):
    """No test may ever wait: fixed clock, sleep is a no-op."""

    return cli.TowerGrind(calibration, clock=lambda: NOW, sleep=lambda _s: None,
                          **kwargs)


class FakeFrame:
    """A frame that answers `roi_mean_rgb` directly (BGR, like cv2 frames)."""

    def __init__(self, bgr):
        self.bgr = tuple(float(v) for v in bgr)

    def roi_mean_rgb(self, x0, y0, x1, y1):  # noqa: ARG002 - shape is irrelevant
        return self.bgr


INSIDE_BGR = (170.0, 180.0, 60.0)   # BGR of expect_rgb [60,180,170]
OUTSIDE_BGR = (10.0, 20.0, 30.0)


def _battle_runner(*results: dict):
    queue = list(results)
    calls: list[float] = []

    def run(_ctx, max_seconds: float):
        calls.append(max_seconds)
        return queue.pop(0) if queue else {"won": True, "xp": 0.0}

    run.calls = calls  # type: ignore[attr-defined]
    return run


# --------------------------------------------------------------------------- #
# calibration is a refusal surface
# --------------------------------------------------------------------------- #
def test_missing_calibration_raises(tmp_path):
    with pytest.raises(cli.CalibrationError) as excinfo:
        cli.load_calibration(tmp_path / "nope.json")
    assert "not found" in str(excinfo.value)


def test_uncalibrated_or_incomplete_calibration_lists_the_missing_fields(tmp_path):
    raw = _raw()
    raw["status"] = "UNCALIBRATED"
    raw["tower"]["inside"]["floor_roi_px"] = None
    raw["tower"]["inside"].pop("signature", None)
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    info = cli.calibration_problems(path)
    assert info["calibrated"] is False
    assert "tower.inside.floor_roi_px" in info["required_fields"]
    assert any("floor_roi_px is null" in problem for problem in info["problems"])
    with pytest.raises(cli.CalibrationError) as excinfo:
        cli.load_calibration(path, require_calibrated=True)
    assert any("not CALIBRATED" in p for p in excinfo.value.problems)


def test_incomplete_presets_are_rejected(tmp_path):
    raw = _raw()
    raw["status"] = "CALIBRATED"
    raw["tower"]["inside"]["floor_roi_px"] = [100, 1050, 700, 1400]
    raw["tower"]["dialog"]["floor_presets"] = {}
    path = tmp_path / "cal.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(cli.CalibrationError) as excinfo:
        cli.load_calibration(path)
    assert any("floor_presets" in p for p in excinfo.value.problems)


def test_window_geometry_mismatch_blocks_before_any_click(tmp_path):
    grinder = _grinder(_cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
                             battle_runner=_battle_runner({"won": True}))
    session = FakeSession([DIALOG], pixel_size=(2000, 1400))
    final = grinder.run(ctx_for(session), minutes=1)
    assert final["outcome"] == Outcome.BLOCKED.value
    assert "window geometry changed" in final["reason"]
    assert session.clicks == []


# --------------------------------------------------------------------------- #
# the happy cycle
# --------------------------------------------------------------------------- #
def test_happy_cycle_reads_verbatim_xp_and_requires_floor_advance(tmp_path):
    ledger = tmp_path / "tower.jsonl"
    runner = _battle_runner({"won": True, "xp": 1710.0, "status": "PASS",
                             "reason": "", "after": {}})
    grinder = _grinder(_cal(tmp_path), ledger_path=ledger,
                             battle_runner=runner,
                             limits=cli.TowerLimits(max_cycles=1))
    session = FakeSession(_happy_frames())
    final = grinder.run(ctx_for(session), minutes=5)

    assert final["outcome"] == Outcome.PROGRESS.value
    assert final["wins"] == 1 and final["xp_total"] == 1710.0
    assert final["floor_first"] == 1 and final["floor_last"] == 2
    clicks = [click[2] for click in session.clicks]
    assert clicks[0] == "pick_floor"
    assert clicks[1].startswith("preset:")
    assert "guardian" not in clicks  # battle markers were already present
    records = cli.read_ledger(ledger)
    cycle = [record for record in records if record.get("kind") == "cycle"][-1]
    assert cycle["xp_text"] == "XIN精灵获得经验：1710"
    assert cycle["xp_value"] == 1710.0
    assert cycle["xp_source"] == "result_ocr"
    assert cycle["outcome"] == Outcome.PROGRESS.value
    assert cycle["floor_before"] == 1 and cycle["floor_after"] == 2
    assert isinstance(cycle["dialogs"], list) and cycle["ts_epoch"] > 0


def test_won_without_xp_text_and_floor_unchanged_is_unverified(tmp_path):
    noise = [item("战场清理中", 900, 700)]
    frames = [DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE, BATTLE,
              noise, noise, noise, CONFIRM, [], FLOOR1, FLOOR1]
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": True, "xp": 0.0}),
        limits=cli.TowerLimits(max_cycles=1))
    final = grinder.run(ctx_for(FakeSession(frames)), minutes=5)
    assert final["outcome"] == Outcome.UNVERIFIED_ACTIVITY.value
    assert final["xp_total"] == 0.0  # no progress claim, no XP total
    assert "no XP text" in final["reason"]


def test_two_consecutive_floor_readings_are_required(tmp_path):
    # A painted-floor area that never yields a number: refuse instead of guessing.
    unreadable = [item("??", 400, 1200), item("装饰", 450, 1300)]
    frames = [DIALOG, DIALOG, unreadable, unreadable]
    session = FakeSession(frames)
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": True}),
        limits=cli.TowerLimits(max_cycles=1))
    final = grinder.run(ctx_for(session), minutes=5)
    assert final["outcome"] == Outcome.BLOCKED.value
    assert "floor number not readable" in final["reason"]
    assert [c[2] for c in session.clicks if c[2] == "guardian"] == []


# --------------------------------------------------------------------------- #
# engage: two body clicks, one extra at most, then BLOCKED
# --------------------------------------------------------------------------- #
def test_engage_retry_cap_is_three_clicks_then_blocked(tmp_path):
    frames = [DIALOG, DIALOG, FLOOR1, FLOOR1]  # battle markers never appear
    session = FakeSession(frames)
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": True}),
        limits=cli.TowerLimits(max_cycles=1))
    final = grinder.run(ctx_for(session), minutes=5)
    assert final["outcome"] == Outcome.BLOCKED.value
    assert "did not start after" in final["reason"]
    guardian_clicks = [c for c in session.clicks if c[2] == "guardian"]
    approach_clicks = [c for c in session.clicks if c[2] == "approach"]
    # 2026-11-07: the guardian changes position every floor, so the posture sweeps
    # guardian_px + guardian_candidates_px. Clicking stays bounded: a sweep of N
    # points gets 2 rounds, a single point gets 3.
    cal = _cal(tmp_path)
    sweep = {tuple(cal.guardian_px)}
    sweep.update(tuple(p) for p in cal.guardian_candidates_px)
    sweep = sorted(sweep)
    rounds = 3 if len(sweep) == 1 else 2
    assert len(guardian_clicks) == min(rounds * len(sweep), 12)
    assert len(approach_clicks) == rounds       # approach -> sweep, per round
    assert len(guardian_clicks) <= 12           # hard bound on blind clicking
    assert guardian_clicks[0][:2] == (1220.0, 800.0)   # live-verified primary body point (5/5)
    assert approach_clicks[0][:2] == (1238.0, 900.0)   # live-verified floor point


def test_defeat_reenters_the_floor_picker_bounded(tmp_path):
    attempt1 = [DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE, BATTLE,
                [item("很遗憾，这次挑战未能获胜", 900, 700)],
                [item("很遗憾，这次挑战未能获胜", 900, 700)], CONFIRM, [],
                FLOOR1, FLOOR1]
    attempt2 = [DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE, BATTLE,
                RESULT, RESULT, RESULT, CONFIRM, [], FLOOR2, FLOOR2]
    frames = attempt1 + attempt2
    runner = _battle_runner({"won": False, "reason": "defeat", "xp": 0.0},
                            {"won": True, "xp": 1710.0, "reason": ""})
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl", battle_runner=runner,
        limits=cli.TowerLimits(max_cycles=1, max_reentries=2))
    final = grinder.run(ctx_for(FakeSession(frames)), minutes=5)
    assert final["outcome"] == Outcome.PROGRESS.value
    assert final["reentries"] == 1
    events = [r for r in cli.read_ledger(tmp_path / "l.jsonl")
              if r.get("kind") == "cycle.reentry"]
    assert events and events[0]["cycle"] == 1


def test_defeat_budget_caps_reentries_and_fails(tmp_path):
    attempt = [DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE, BATTLE,
               [item("很遗憾")], [item("很遗憾")], CONFIRM, [], FLOOR1, FLOOR1]
    frames = attempt * 3
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": False, "reason": "defeat"},
                                     {"won": False, "reason": "defeat"},
                                     {"won": False, "reason": "defeat"}),
        limits=cli.TowerLimits(max_cycles=1, max_reentries=1, max_failures=3))
    final = grinder.run(ctx_for(FakeSession(frames)), minutes=5)
    assert final["outcome"] == Outcome.FAILED.value
    assert final["reentries"] == 1
    assert final["failures"] == 1


# --------------------------------------------------------------------------- #
# PP exhaustion, stop/pause, ledger durability
# --------------------------------------------------------------------------- #
def test_pp_exhaustion_is_blocked_not_more_clicking(tmp_path):
    frames = [DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE, BATTLE,
              [item("没有PP了，无法使用技能", 900, 700)],
              [item("没有PP了，无法使用技能", 900, 700)]]
    session = FakeSession(frames)
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": False, "reason": "no pp"}),
        limits=cli.TowerLimits(max_cycles=1))
    final = grinder.run(ctx_for(session), minutes=5)
    assert final["outcome"] == Outcome.BLOCKED.value
    assert final["reason"].startswith("pp_exhausted")
    assert [c[2] for c in session.clicks if c[2] == "guardian"] == []


def test_stop_flag_stops_at_a_cycle_boundary(tmp_path):
    stop = tmp_path / "stop"
    stop.write_text("", encoding="utf-8")
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": True}),
        limits=cli.TowerLimits(max_cycles=10, stop_file=str(stop)))
    session = FakeSession([DIALOG])
    final = grinder.run(ctx_for(session), minutes=5)
    assert final["outcome"] == Outcome.PAUSED.value
    assert final["cycles"] == 0
    assert session.clicks == []


def test_ledger_truncated_tail_is_tolerated_and_recovered(tmp_path):
    ledger = tmp_path / "tower.jsonl"
    cli.append_ledger(ledger, {"kind": "cycle", "cycle": 1})
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "cycle", "cyc')  # crash mid-append
    assert [r["kind"] for r in cli.read_ledger(ledger)] == ["cycle"]
    cli.append_ledger(ledger, {"kind": "cycle", "cycle": 2})
    assert [r["cycle"] for r in cli.read_ledger(ledger)] == [1, 2]


# --------------------------------------------------------------------------- #
# CLI: dry-run, refusal, past deadline
# --------------------------------------------------------------------------- #
def test_cli_dry_run_prints_plan_and_gaps_without_running(tmp_path, capsys):
    raw = _raw()
    raw["status"] = "UNCALIBRATED"
    raw["tower"]["inside"].pop("signature", None)
    path = tmp_path / "uncalibrated.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    code = cli.main(["--dry-run", "--calibration", str(path),
                     "--ledger", str(tmp_path / "l.jsonl")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["dry_run"] is True and payload["starts_nothing"] is True
    assert payload["calibration"]["calibrated"] is False
    assert payload["blocked_reason"].startswith("calibration is not CALIBRATED")
    assert payload["plan"] and payload["precondition"]
    assert not (tmp_path / "l.jsonl").exists()


def test_cli_dry_run_with_calibrated_file_prints_the_command(tmp_path, capsys):
    path = _write_calibration(tmp_path)
    code = cli.main(["--dry-run", "--calibration", str(path),
                     "--ledger", str(tmp_path / "l.jsonl")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["calibration"]["calibrated"] is True
    assert "--allow-input" in payload["command"]


def test_cli_refuses_uncalibrated_and_past_deadline(tmp_path, capsys):
    ledger = str(tmp_path / "l.jsonl")
    raw = _raw()
    raw["status"] = "UNCALIBRATED"
    uncalibrated = tmp_path / "uncalibrated.json"
    uncalibrated.write_text(json.dumps(raw), encoding="utf-8")
    assert cli.main(["--calibration", str(uncalibrated), "--allow-input",
                     "--ledger", ledger]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["problems"]

    path = _write_calibration(tmp_path)
    assert cli.main(["--calibration", str(path), "--allow-input", "--ledger", ledger,
                     "--deadline-at", "2020-01-01T00:00:00+08:00"]) == 2
    assert "past" in json.loads(capsys.readouterr().out)["error"]

    assert cli.main(["--calibration", str(path), "--ledger", ledger]) == 3
    assert "allow-input" in json.loads(capsys.readouterr().out)["error"]


# --------------------------------------------------------------------------- #
# 2026-11-07 live finding: the painted floor is art lettering that OCR misreads
# (true 03 read as 08 at conf 0.30).  The calibrated visual signature is the
# INSIDE fallback, and XP text is the primary NEXT evidence.
# --------------------------------------------------------------------------- #
UNREADABLE_FLOOR = [item("??", 400, 1200), item("装饰", 450, 1300)]


def _floor_unreadable_frames(*tail: list[dict]) -> list[list[dict]]:
    """Enough unreadable floor frames for one full sample loop, then the tail."""

    frames: list[list[dict]] = [DIALOG, DIALOG]
    frames += [UNREADABLE_FLOOR] * 14      # _read_floor's full sample budget
    frames += list(tail)
    return frames


class PhaseSession:
    """A click/battle-driven session: OCR content follows the grinder's phase.

    The real run has no fixed OCR script either — the screen changes because a click
    or a battle happened.  This makes the signature test independent of how many
    samples each predicate consumes.
    """

    PIXEL = (2400, 1600)

    def __init__(self, battle_phase):
        self.clicks: list[tuple[float, float, str]] = []
        self.phase = "pre"          # pre -> inside -> battle -> result -> post
        self.pixel_size = self.PIXEL
        self.battle_phase = battle_phase  # callable(session) -> sets phase

    def ocr(self, *, min_conf: float = 0.3, path: str | None = None, **_kw):
        if self.phase == "pre":
            return [dict(i) for i in DIALOG]
        if self.phase == "inside":
            return [dict(i) for i in UNREADABLE_FLOOR]
        if self.phase == "battle":
            return [dict(i) for i in BATTLE]
        if self.phase == "result":
            return [dict(i) for i in RESULT]
        return [dict(i) for i in UNREADABLE_FLOOR]

    def click_px(self, x, y, reason="", **_kw):
        self.clicks.append((float(x), float(y), reason))
        if reason.startswith("preset:"):
            self.phase = "inside"
        elif reason == "guardian" and self.phase == "inside":
            self.phase = "battle"
        elif reason.startswith("confirm:"):
            self.phase = "post"
        return object()

    def latest(self):
        return FakeFrame(INSIDE_BGR)

    def close(self):
        pass


def test_signature_hit_engages_when_floor_is_unreadable(tmp_path):
    session = PhaseSession(battle_phase=None)

    def battle(_ctx, _max_seconds):
        session.phase = "result"
        return {"won": True, "xp": 1710.0}

    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl", battle_runner=battle,
        frame_reader=lambda: FakeFrame(INSIDE_BGR),
        limits=cli.TowerLimits(max_cycles=1))
    final = grinder.run(ctx_for(session), minutes=5)

    assert final["outcome"] == Outcome.PROGRESS.value      # XP text is the evidence
    assert final["xp_total"] == 1710.0
    cycle = [r for r in cli.read_ledger(tmp_path / "l.jsonl")
             if r.get("kind") == "cycle"][-1]
    assert cycle["floor_source"] == "signature"
    assert cycle["floor_before"] is None
    assert cycle["xp_text"] == "XIN精灵获得经验：1710"
    assert [c[2] for c in session.clicks if c[2] == "guardian"]  # it did engage


def test_signature_miss_keeps_the_runner_blocked(tmp_path):
    frames = _floor_unreadable_frames()
    session = FakeSession(frames)
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": True, "xp": 1710.0}),
        frame_reader=lambda: FakeFrame(OUTSIDE_BGR),
        limits=cli.TowerLimits(max_cycles=1))
    final = grinder.run(ctx_for(session), minutes=5)
    assert final["outcome"] == Outcome.BLOCKED.value
    assert "floor number not readable" in final["reason"]
    assert "signature did not match" in final["reason"]
    assert [c[2] for c in session.clicks if c[2] == "guardian"] == []


def test_unreadable_next_floor_with_xp_still_counts_as_progress(tmp_path):
    # INSIDE reads fine, the battle is won, the NEXT floor read is unreadable, but
    # the payout text is present: XP is the primary evidence (live 1710/3003 XP).
    frames = [DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE, BATTLE,
              RESULT, RESULT, RESULT, CONFIRM, []]
    frames += [UNREADABLE_FLOOR] * 14
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": True, "xp": 1710.0}),
        frame_reader=lambda: FakeFrame(INSIDE_BGR),
        limits=cli.TowerLimits(max_cycles=1))
    final = grinder.run(ctx_for(FakeSession(frames)), minutes=5)
    assert final["outcome"] == Outcome.PROGRESS.value
    cycle = [r for r in cli.read_ledger(tmp_path / "l.jsonl")
             if r.get("kind") == "cycle"][-1]
    assert cycle["floor_source"] == "xp_text"
    assert cycle["xp_value"] == 1710.0


def test_misread_floor_cross_check_does_not_undo_an_xp_win(tmp_path):
    # Live misread: true 03 reported as 08 twice.  The win is still a win.
    floor8 = [item("08", 400, 1200)]
    frames = [DIALOG, DIALOG, floor8, floor8, BATTLE, BATTLE,
              RESULT, RESULT, RESULT, CONFIRM, []]
    frames += [floor8] * 14
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": True, "xp": 1710.0}),
        frame_reader=lambda: FakeFrame(INSIDE_BGR),
        limits=cli.TowerLimits(max_cycles=1))
    final = grinder.run(ctx_for(FakeSession(frames)), minutes=5)
    assert final["outcome"] == Outcome.PROGRESS.value
    cycle = [r for r in cli.read_ledger(tmp_path / "l.jsonl")
             if r.get("kind") == "cycle"][-1]
    assert "cross-check disagreed" in cycle["reason"]


# --------------------------------------------------------------------------- #
# REPLAY: the calibrated INSIDE signature against real live frames.
# Skips when the live frame directory is absent (it is runtime evidence, not source).
# --------------------------------------------------------------------------- #
def test_calibrated_signature_matches_inside_frames_and_rejects_others(tmp_path):
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    from PIL import Image

    live = _ROOT / "data" / "overnight" / "20261106_dual_mode" / "live"
    inside = ["tower_now.png", "tower_floor03_shot.png", "cycle2_inside_0013.png",
              "floor2_after_0017.png"]
    outside = ["room_after_tower_0006.png", "base_enter_0009.png",
               "aux_dialog_2248.png"]
    frames = {name: live / name for name in inside + outside}
    if not all(path.exists() for path in frames.values()):
        pytest.skip("live tower frames are not present")
    full = []
    for name, path in frames.items():
        try:
            with Image.open(path) as image:
                if image.size == (2400, 1600):
                    full.append(name)
        except OSError:
            continue
    inside = [name for name in inside if name in full]
    outside = [name for name in outside if name in full]
    assert len(inside) >= 3 and outside, "not enough full-size live frames"

    calibration = cli.load_calibration(str(cli.DEFAULT_CALIBRATION),
                                       require_calibrated=True)
    assert calibration.signature, "the shipped calibration should carry a signature"
    frame_reader = None
    grinder = _grinder(calibration, ledger_path=None,
                       frame_reader=lambda: frame_reader)
    ctx = ctx_for(FakeSession([[item("x")]]))

    import numpy as np

    for name in inside:
        rgb = np.asarray(Image.open(frames[name]).convert("RGB"))
        bgr = rgb[:, :, ::-1]  # cv2 frames are BGR
        frame_reader = bgr
        hit, evidence = grinder.signature_probe(ctx)
        assert hit is True, f"{name}: {evidence}"
    for name in outside:
        rgb = np.asarray(Image.open(frames[name]).convert("RGB"))
        bgr = rgb[:, :, ::-1]
        frame_reader = bgr
        hit, evidence = grinder.signature_probe(ctx)
        assert hit is False, f"{name}: {evidence}"


# --------------------------------------------------------------------------- #
# 2026-11-07 field run fixes: settlement chain, two-step posture, UNVERIFIED is
# not terminal.
# --------------------------------------------------------------------------- #
def test_settlement_loop_captures_xp_revealed_after_a_confirm_click(tmp_path):
    # Post-battle chain: 确认 first, the XP panel only appears after that click.
    confirm_only = [item("确认", 1200, 1000)]
    xp_only = [item("XIN精灵获得经验：1710", 900, 800)]
    frames = [DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE, BATTLE,
              confirm_only, xp_only, FLOOR2, FLOOR2]
    session = FakeSession(frames)
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": True, "xp": 0.0}),
        limits=cli.TowerLimits(max_cycles=1))
    final = grinder.run(ctx_for(session), minutes=5)

    assert final["outcome"] == Outcome.PROGRESS.value
    assert final["xp_total"] == 1710.0
    cycle = [r for r in cli.read_ledger(tmp_path / "l.jsonl")
             if r.get("kind") == "cycle"][-1]
    assert cycle["xp_text"] == "XIN精灵获得经验：1710"
    assert cycle["xp_source"] == "result_ocr"
    assert cycle["confirm_clicks"] == 1
    assert [c[2] for c in session.clicks if c[2].startswith("confirm:")] == ["confirm:确认"]


def test_unverified_cycles_are_counted_and_do_not_stop_the_run(tmp_path):
    # Each cycle: battle won, no XP text, floor unchanged -> UNVERIFIED.  The runner
    # must keep going until max_unverified, then stop with a stop_reason.
    noise = [item("战场清理中", 900, 700)]
    one_cycle = [DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE, BATTLE,
                 noise, noise, FLOOR1, FLOOR1]
    frames = one_cycle * 3
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": True, "xp": 0.0},
                                     {"won": True, "xp": 0.0},
                                     {"won": True, "xp": 0.0}),
        limits=cli.TowerLimits(max_unverified=2))
    final = grinder.run(ctx_for(FakeSession(frames)), minutes=60)

    assert final["outcome"] == Outcome.UNVERIFIED_ACTIVITY.value
    assert final["cycles"] == 2
    assert final["unverified"] == 2
    assert final["stop_reason"] == "unverified_limit"
    assert len(final["cycles_detail"]) == 2
    assert [r["kind"] for r in cli.read_ledger(tmp_path / "l.jsonl")
            ].count("cycle") == 2


def test_stop_reason_is_recorded_for_budget_and_stop_flag(tmp_path):
    grinder = _grinder(_cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
                       battle_runner=_battle_runner({"won": True, "xp": 1710.0}),
                       limits=cli.TowerLimits(max_cycles=1))
    frames = _happy_frames()
    final = grinder.run(ctx_for(FakeSession(frames)), minutes=5)
    assert final["stop_reason"] == "cycle_budget"

    stop = tmp_path / "stop"
    stop.write_text("", encoding="utf-8")
    grinder = _grinder(_cal(tmp_path), ledger_path=None,
                       battle_runner=_battle_runner({"won": True}),
                       limits=cli.TowerLimits(max_cycles=5, stop_file=str(stop)))
    final = grinder.run(ctx_for(FakeSession([DIALOG])), minutes=5)
    assert final["stop_reason"] == "stop_flag"


def test_two_step_posture_is_used_even_though_battle_markers_are_absent(tmp_path):
    # Frames: no battle markers until the guardian click; the posture must click the
    # approachable floor first (reason "approach"), then the body.
    class PostureSession(FakeSession):
        def __init__(self):
            super().__init__([DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE])
            self.engaged = False

        def ocr(self, **kw):
            index = self.calls
            self.calls += 1
            if not self.engaged:
                return [dict(i) for i in (DIALOG if index < 2 else FLOOR1)]
            return [dict(i) for i in BATTLE]

        def click_px(self, x, y, reason="", **kw):
            self.clicks.append((float(x), float(y), reason))
            if reason == "guardian":
                self.engaged = True
            return object()

    session = PostureSession()
    grinder = _grinder(_cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
                       battle_runner=_battle_runner({"won": True, "xp": 3300.0}),
                       limits=cli.TowerLimits(max_cycles=1))
    final = grinder.run(ctx_for(session), minutes=5)
    assert final["outcome"] == Outcome.PROGRESS.value
    reasons = [c[2] for c in session.clicks]
    assert reasons.index("approach") < reasons.index("guardian")
    assert session.clicks[reasons.index("approach")][:2] == (1238.0, 900.0)
    assert session.clicks[reasons.index("guardian")][:2] == (1220.0, 800.0)


# --------------------------------------------------------------------------- #
# 2026-11-07 (second field run): RunBattle consumes the reward panels
# (rewards=[], xp=0.0), so progress is proven independently via the pet panel:
# 升级所需经验值 shrinking / 等级 growing, every --verify-every cycles.
# --------------------------------------------------------------------------- #
PANEL_5000 = [item("等级：47", 1650, 600), item("升级所需经验值：5000", 1650, 640)]
PANEL_3300 = [item("等级：47", 1650, 600), item("升级所需经验值：3300", 1650, 640)]


def test_pet_panel_delta_is_the_independent_progress_proof(tmp_path):
    frames = [DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE, BATTLE,
              RESULT, RESULT, CONFIRM, [], FLOOR2, FLOOR2,
              PANEL_5000, PANEL_5000,
              FLOOR2, FLOOR2, BATTLE, BATTLE,
              [item("战场清理中", 900, 700)],
              FLOOR2, FLOOR2,
              PANEL_3300, PANEL_3300]
    session = FakeSession(frames)
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": True, "xp": 0.0},
                                     {"won": True, "xp": 0.0}),
        limits=cli.TowerLimits(max_cycles=2, verify_every=1))
    final = grinder.run(ctx_for(session), minutes=10)

    checks = final["pet_panel_checks"]
    assert [c["verdict"] for c in checks] == ["BASELINE", "PROGRESS"]
    assert checks[1]["xp_delta"] == 1700
    # The second cycle had no floor advance and no XP text, but the panel delta
    # proves it was a real win: it must not stay UNVERIFIED.
    second = final["cycles_detail"][1]
    assert second["outcome"] == Outcome.PROGRESS.value
    assert second["xp_source"] == "pet_panel_delta"
    assert second["xp_value"] == 1700
    assert final["outcome"] == Outcome.PROGRESS.value
    assert final["xp_total"] >= 1700
    # The panel is closed with ESC (close_px is null in the live calibration).
    assert any(op == "key" for op, _ in session.sent)


def test_two_identical_panel_reads_are_unverified(tmp_path):
    frames = [DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE, BATTLE,
              RESULT, RESULT, CONFIRM, [], FLOOR2, FLOOR2,
              PANEL_5000, PANEL_5000,
              FLOOR2, FLOOR2, BATTLE, BATTLE,
              [item("战场清理中", 900, 700)],
              FLOOR2, FLOOR2,
              PANEL_5000, PANEL_5000,
              FLOOR2, FLOOR2, BATTLE, BATTLE,
              [item("战场清理中", 900, 700)],
              FLOOR2, FLOOR2,
              PANEL_5000, PANEL_5000]
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner(*([{"won": True, "xp": 0.0}] * 3)),
        limits=cli.TowerLimits(max_unverified=3, verify_every=1))
    final = grinder.run(ctx_for(FakeSession(frames)), minutes=10)
    verdicts = [c["verdict"] for c in final["pet_panel_checks"]]
    assert verdicts[:3] == ["BASELINE", "UNVERIFIED", "UNVERIFIED"]
    # Two identical readings are recorded as no-progress evidence; the run counts
    # them as UNVERIFIED cycles (it must not claim a win from a stale panel).
    assert final["unverified"] >= 1
    assert final["stop_reason"] in ("unverified_limit", "blocked", "no_work_limit")


# --------------------------------------------------------------------------- #
# 2026-11-07 live: a defeat sends you to the space-station hall (bottom bar is
# back); the tower has no heal entry, but the hall does.  `--auto-recover`
# self-heals with verified skills only: heal -> travel(tutor_room) -> preset 1.
# --------------------------------------------------------------------------- #
def _skill_result(status="PASS", *, after=None, reason=""):
    return SimpleNamespace(status=SimpleNamespace(value=status),
                           after=after or {}, reason=reason)


PANEL_FULL = [item("等级：47", 1650, 600), item("升级所需经验值：5000", 1650, 640),
              item("144/144", 840, 730)]
PANEL_ZERO = [item("等级：49", 1650, 600), item("升级所需经验值：4650", 1650, 640),
              item("0/144", 840, 730)]


def _defeat_cycle_frames(verdict="很遗憾，这次挑战未能获胜"):
    return [DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE, BATTLE,
            [item(verdict, 900, 700)], FLOOR1, FLOOR1]


def _recover_frames():
    # observation frame (no popup), tower dialog twice, floor read twice
    return [[item("太空站大厅", 900, 500)], DIALOG, DIALOG, FLOOR1, FLOOR1]


def _progress_cycle_frames():
    return [FLOOR1, FLOOR1, BATTLE, BATTLE,
            [item("战场清理中", 900, 700)], FLOOR2, FLOOR2]


def test_auto_recover_after_defeat_heals_travels_and_picks_floor_one(tmp_path):
    frames = [PANEL_FULL, PANEL_FULL] + _defeat_cycle_frames() + \
        _recover_frames() + _progress_cycle_frames()
    session = FakeSession(frames)
    healed = []
    traveled = []

    def heal(_ctx):
        healed.append(1)
        return _skill_result(after={"hp_after": [{"hp": 144}]})

    def travel(_ctx, destination):
        traveled.append(destination)
        return _skill_result()

    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": False, "reason": "defeat"},
                                     {"won": True, "xp": 0.0}),
        heal_runner=heal, travel_runner=travel,
        limits=cli.TowerLimits(max_cycles=2, auto_recover=True,
                               max_reentries=0))
    final = grinder.run(ctx_for(session), minutes=10)

    assert healed == [1] and traveled == ["tutor_room"]
    assert final["recovers"] == 1
    assert final["outcome"] == Outcome.PROGRESS.value     # recovery continued the run
    assert final["cycles"] == 2
    records = [r for r in cli.read_ledger(tmp_path / "l.jsonl")
               if r.get("kind") == "recover"]
    assert records and records[-1]["ok"] is True
    assert records[-1]["heal_ok"] is True
    assert records[-1]["floor_picked"] == 1
    # preset 1 was actually re-picked after the travel
    assert [c[2] for c in session.clicks if c[2].startswith("preset:")]


def test_recover_continues_when_heal_is_unavailable_but_travel_works(tmp_path):
    """2026-11-07: heal can fail while the character is in the world; the chain
    records `heal_unavailable` and continues — travel is the decisive step."""

    frames = [PANEL_FULL, PANEL_FULL] + _defeat_cycle_frames() + _recover_frames() \
        + _progress_cycle_frames()
    session = FakeSession(frames)
    healed = []

    def heal(_ctx):
        healed.append(1)
        return _skill_result("FAIL", reason="pet-bag icon not found on screen")

    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": False, "reason": "defeat"},
                                     {"won": True, "xp": 0.0}),
        heal_runner=heal,
        travel_runner=lambda _ctx, _d: _skill_result(),
        limits=cli.TowerLimits(max_cycles=2, auto_recover=True,
                               max_reentries=0))
    final = grinder.run(ctx_for(session), minutes=10)

    assert healed == [1]
    assert final["recovers"] == 1
    assert final["outcome"] == Outcome.PROGRESS.value   # the run continued
    row = [r for r in cli.read_ledger(tmp_path / "l.jsonl")
           if r.get("kind") == "recover"][-1]
    assert row["ok"] is True and row["heal_unavailable"] is True
    heal_step = next(s for s in row["steps"] if s["step"] == "heal")
    assert heal_step["ok"] is False and "heal_unavailable" in heal_step["reason"]
    assert any(s["step"] == "preset1" and s["ok"] for s in row["steps"])


def test_recover_is_blocked_when_travel_also_fails(tmp_path):
    frames = [PANEL_FULL, PANEL_FULL] + _defeat_cycle_frames() + _recover_frames()
    session = FakeSession(frames)
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": False, "reason": "defeat"}),
        heal_runner=lambda _ctx: _skill_result("FAIL", reason="no bag icon"),
        travel_runner=lambda _ctx, _d: _skill_result("FAIL", reason="map not found"),
        limits=cli.TowerLimits(max_cycles=5, auto_recover=True,
                               max_reentries=0))
    final = grinder.run(ctx_for(session), minutes=10)

    assert final["outcome"] == Outcome.BLOCKED.value
    assert final["stop_reason"] == "recover_failed"
    assert "step 'travel'" in final["reason"]
    row = [r for r in cli.read_ledger(tmp_path / "l.jsonl")
           if r.get("kind") == "recover"][-1]
    travel_step = next(s for s in row["steps"] if s["step"] == "travel")
    assert travel_step["ok"] is False and "map not found" in travel_step["reason"]


def test_recover_budget_is_bounded(tmp_path):
    # defeat -> recover ok -> defeat -> recover refused (budget) -> BLOCKED
    frames = ([PANEL_FULL, PANEL_FULL] + _defeat_cycle_frames()
              + _recover_frames() + _defeat_cycle_frames())
    session = FakeSession(frames)
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": False, "reason": "defeat"},
                                     {"won": False, "reason": "defeat"}),
        heal_runner=lambda _ctx: _skill_result(after={"hp_after": [{"hp": 144}]}),
        travel_runner=lambda _ctx, _d: _skill_result(),
        limits=cli.TowerLimits(max_cycles=5, auto_recover=True, max_recovers=1,
                               max_reentries=0))
    final = grinder.run(ctx_for(session), minutes=10)
    assert final["recovers"] == 1
    assert final["outcome"] == Outcome.BLOCKED.value
    assert "recover budget exhausted" in final["reason"]


def test_pause_during_recover_stops_at_the_boundary(tmp_path):
    pause = tmp_path / "pause"
    frames = [PANEL_FULL, PANEL_FULL] + _defeat_cycle_frames() + _recover_frames()
    session = FakeSession(frames)

    def heal(_ctx):
        pause.write_text("", encoding="utf-8")   # human pauses right after the heal
        return _skill_result(after={"hp_after": [{"hp": 144}]})

    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": False, "reason": "defeat"}),
        heal_runner=heal,
        travel_runner=lambda _ctx, _d: _skill_result(),
        limits=cli.TowerLimits(max_cycles=5, auto_recover=True,
                               max_reentries=0, pause_files=(str(pause),)))
    final = grinder.run(ctx_for(session), minutes=10)
    assert final["outcome"] == Outcome.PAUSED.value
    assert final["stop_reason"] == "pause_flag"
    assert final["recovers"] == 1


# --------------------------------------------------------------------------- #
# 2026-11-07 (third field run): HP=0 inside the tower shows up as the classic
# "no engageable monster within 20s of hunting" (docs/state.md), so recover must
# be HP-aware: read HP at start / before healing, and only heal when it is real.
# --------------------------------------------------------------------------- #
def test_hp0_at_start_triggers_heal_and_floor_one(tmp_path):
    frames = ([PANEL_ZERO, PANEL_ZERO]
              + [[item("太空站大厅", 900, 500)], DIALOG, DIALOG, FLOOR1, FLOOR1]
              + _progress_cycle_frames())
    session = FakeSession(frames)
    healed = []

    def heal(_ctx):
        healed.append(1)
        return _skill_result(after={"hp_after": [{"hp": 144}]})

    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner({"won": True, "xp": 0.0}),
        heal_runner=heal,
        travel_runner=lambda _ctx, _d: _skill_result(),
        limits=cli.TowerLimits(max_cycles=1, auto_recover=True,
                               max_reentries=0))
    final = grinder.run(ctx_for(session), minutes=10)

    assert healed == [1]
    assert final["recovers"] == 1
    assert final["outcome"] == Outcome.PROGRESS.value
    recover_rows = [r for r in cli.read_ledger(tmp_path / "l.jsonl")
                    if r.get("kind") == "recover"]
    assert recover_rows[-1]["trigger"] == "hp0_at_start"
    assert recover_rows[-1]["ok"] is True and recover_rows[-1]["floor_picked"] == 1


def test_no_engageable_monster_with_good_hp_retries_instead_of_healing(tmp_path):
    # HP read before the decision: 144/144 means the hunt simply missed, so another
    # posture is tried and game.heal_pet is NOT called.
    frames = ([PANEL_FULL, PANEL_FULL]
              + [DIALOG, DIALOG, FLOOR1, FLOOR1, BATTLE, BATTLE,
                 [item("战场清理中", 900, 700)], FLOOR1, FLOOR1,
                 PANEL_FULL, PANEL_FULL]
              + _progress_cycle_frames())
    session = FakeSession(frames)
    healed = []
    grinder = _grinder(
        _cal(tmp_path), ledger_path=tmp_path / "l.jsonl",
        battle_runner=_battle_runner(
            {"won": False, "reason": "no engageable monster within 20s of hunting"},
            {"won": True, "xp": 0.0}),
        frame_reader=lambda: FakeFrame(INSIDE_BGR),   # still inside the tower
        heal_runner=lambda _ctx: healed.append(1) or _skill_result(
            after={"hp_after": [{"hp": 144}]}),
        travel_runner=lambda _ctx, _d: _skill_result(),
        limits=cli.TowerLimits(max_cycles=2, auto_recover=True,
                               max_reentries=0))
    final = grinder.run(ctx_for(session), minutes=10)

    assert healed == []                      # no heal round trip on a hunt miss
    assert final["outcome"] == Outcome.PROGRESS.value
    retry_rows = [r for r in cli.read_ledger(tmp_path / "l.jsonl")
                  if r.get("kind") == "recover"
                  and r.get("trigger") == "no_engageable_monster"]
    assert retry_rows and retry_rows[-1]["action"] == "posture_retry"


def test_hp_ratio_is_none_when_the_field_is_missing(tmp_path):
    grinder = _grinder(_cal(tmp_path))
    assert grinder.hp_ratio(None) is None
    assert grinder.hp_ratio({"hp": None, "hp_max": 144}) is None
    assert grinder.hp_ratio({"hp": 0, "hp_max": 144}) == 0.0
    assert grinder.hp_ratio({"hp": 72, "hp_max": 144}) == 0.5


def test_cli_recover_only_refuses_before_any_live_import(tmp_path, capsys):
    """--recover-only still needs calibration + --allow-input (no accidental clicks)."""

    raw = _raw()
    raw["status"] = "UNCALIBRATED"
    uncalibrated = tmp_path / "uncalibrated.json"
    uncalibrated.write_text(json.dumps(raw), encoding="utf-8")
    assert cli.main(["--recover-only", "--calibration", str(uncalibrated),
                     "--allow-input"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and payload["problems"]

    calibrated = _write_calibration(tmp_path)
    assert cli.main(["--recover-only", "--calibration", str(calibrated)]) == 3
    assert "allow-input" in json.loads(capsys.readouterr().out)["error"]
