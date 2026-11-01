"""Executor tests: budgets, static screens, stale frames, tick budget.

These are the behaviours that will silently rot if nobody tests them:

* a static screen must never hang the loop (ScreenCaptureKit is change-driven);
* a stale-but-valid frame is a normal observation, not an error;
* budgets fire even when no state change ever arrives;
* a click that provably changes nothing is retried then escalates;
* OCR is not on the poll path (only an UNKNOWN escalation);
* the hot tick stays inside the 16 ms frame budget (p95 assertion, replayed
  against a real artifact at full 2298x1428 resolution).
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from seerauto import capture as C
from seerauto import geometry as G
from seerauto import vision as V
from seerauto.actions import Actions, ClickAction, RecordingInput
from seerauto.config import load_config
from seerauto.errors import CaptureError
from seerauto.executor import Executor, TickContext
from seerauto.perf import PerfRecorder
from seerauto.state import (
    MachineModel,
    Rule,
    State,
    StateDefinition,
    StateMachine,
    TemplatePresent,
    TextSeenWithin,
    model_from_config,
)
from seerauto.vision import Fingerprint, Match, NullOcr, Signals, TextItem
from seerauto.tasks.battle import BattleTask
from seerauto.window import WindowInfo

pytest.importorskip("cv2")


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self, start_ns: int = 1_000_000_000):
        self.now = int(start_ns)

    def __call__(self) -> int:
        return self.now

    def advance_s(self, seconds: float) -> int:
        self.now += int(seconds * 1e9)
        return self.now


def make_clock_sleep(clock: FakeClock):
    def _sleep(seconds: float) -> None:
        clock.advance_s(max(0.0, float(seconds)))

    return _sleep


def frame_for(clock: FakeClock, tag: str, value: int = 40, size=(320, 200), seq: int = 1) -> C.Frame:
    array = np.full((size[1], size[0], 3), value, dtype=np.uint8)
    frame = C.make_frame(
        array,
        G.WindowGeometry(origin_pt=G.Point(300, 120)),
        G.Rect(0.0, 0.0, *G.DEFAULT_REFERENCE.capture_px.as_tuple()),
        seq=seq,
        timestamp_ns=clock(),
        meta={"tag": tag},
    )
    return frame


class StaticCapture:
    """Always returns the same frame object (a static screen)."""

    name = "static"

    def __init__(self, frame: C.Frame):
        self.frame = frame
        self.reads = 0

    @property
    def available(self) -> bool:
        return True

    def latest(self, rect=None) -> C.Frame:
        self.reads += 1
        return self.frame

    def grab(self, rect=None) -> C.Frame:
        return self.latest(rect)

    def close(self) -> None:
        return None


class ScriptedCapture:
    """Replays (frame, ticks) pairs; advances after ``ticks`` reads."""

    name = "scripted"

    def __init__(self, script, loop_last: bool = True):
        self.script = list(script)
        self.index = 0
        self.remaining = self.script[0][1] if self.script else 0
        self.loop_last = loop_last
        self.reads = 0

    @property
    def available(self) -> bool:
        return True

    def latest(self, rect=None) -> C.Frame:
        self.reads += 1
        self.remaining -= 1
        if self.remaining <= 0 and self.index < len(self.script) - 1:
            self.index += 1
            self.remaining = self.script[self.index][1]
        return self.script[self.index][0]

    def grab(self, rect=None) -> C.Frame:
        return self.latest(rect)

    def close(self) -> None:
        return None


class PreloadedCapture:
    """Pre-decoded frames, alternating two pixel patterns (worst-case hot loop)."""

    name = "preloaded"

    def __init__(self, frames, clock: FakeClock):
        self.frames = list(frames)
        self.clock = clock
        self.seq = 0

    @property
    def available(self) -> bool:
        return True

    def latest(self, rect=None) -> C.Frame:
        self.seq += 1
        base = self.frames[self.seq % len(self.frames)]
        return replace(base, seq=self.seq, timestamp_ns=self.clock())

    def grab(self, rect=None) -> C.Frame:
        return self.latest(rect)

    def close(self) -> None:
        return None


class FakeAnalyzer:
    """Signals without pixels; records whether OCR escalation was requested."""

    def __init__(self, scores_by_tag, escalate_scores=None, ocr=None):
        self.scores_by_tag = scores_by_tag
        self.escalate_scores = escalate_scores or {}
        self.ocr = ocr or NullOcr()
        self.calls: list[bool] = []

    def analyze(self, frame, now_ns=None, escalate: bool = False) -> Signals:
        self.calls.append(bool(escalate))
        tag = frame.meta.get("tag", "")
        signals = Signals()
        scores = dict(self.scores_by_tag.get(tag, {}))
        if escalate:
            scores.update(self.escalate_scores.get(tag, {}))
        for name, score in scores.items():
            rect = G.Rect(0, 0, 10, 10)
            signals.templates[name] = Match(name, score, rect, rect, 0.5)
        if escalate and isinstance(self.ocr, V.FakeOcr):
            signals.text = self.ocr.recognize(frame)
        signals.frame_seq = frame.seq
        signals.frame_ts_ns = frame.timestamp_ns
        signals.frame_age_ms = frame.age_ms(now_ns)
        return signals


class CountingTask:
    def __init__(self, action_state=State.BATTLE_TURN, target="skill", once=False):
        self.name = "counting"
        self.action_state = action_state
        self.target = target
        self.once = once
        self.acted = False
        self.calls = 0
        self.decisions: list[State] = []

    def reset(self) -> None:
        self.calls = 0
        self.acted = False
        self.decisions = []

    def decide(self, ctx: TickContext):
        self.calls += 1
        self.decisions.append(ctx.state)
        if self.once and self.acted:
            return None
        if ctx.state is self.action_state:
            self.acted = True
            return ClickAction(targets=(self.target,), label="test_click")
        return None

    def stats(self) -> dict:
        return {"calls": self.calls}


def make_test_model() -> MachineModel:
    return MachineModel(
        initial=State.UNKNOWN,
        rules=(
            Rule("battle", State.BATTLE_TURN, 60, "battle", (TemplatePresent("battle", 0.70),)),
            Rule("idle", State.IDLE, 20, "idle", (TemplatePresent("bar", 0.80),)),
        ),
        definitions={
            State.UNKNOWN: StateDefinition(State.UNKNOWN, timeout_s=None),
            State.IDLE: StateDefinition(State.IDLE, actions=("click_enemy",), timeout_s=None),
            State.BATTLE_TURN: StateDefinition(
                State.BATTLE_TURN, actions=("battle_round",), timeout_s=None, expects_motion=False
            ),
            State.ERROR: StateDefinition(State.ERROR),
            State.PAUSED: StateDefinition(State.PAUSED),
            State.STOPPED: StateDefinition(State.STOPPED),
        },
        transitions={
            State.UNKNOWN: tuple(State),
            State.IDLE: (State.BATTLE_TURN, State.ERROR, State.STOPPED),
            State.BATTLE_TURN: (State.IDLE, State.BATTLE_TURN, State.ERROR, State.STOPPED),
            State.ERROR: (State.UNKNOWN, State.STOPPED),
            State.PAUSED: (State.UNKNOWN, State.STOPPED),
            State.STOPPED: (State.STOPPED,),
        },
    )


def budgets(**overrides) -> SimpleNamespace:
    base = dict(
        max_iterations=30,
        max_duration_s=60.0,
        max_consecutive_errors=5,
        max_state_timeouts=3,
        action_confirm_timeout_s=1.5,
        action_retries=2,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def static_window(config) -> WindowInfo:
    return WindowInfo(
        window_id=-1,
        owner="SeerAuto",
        title="test",
        geometry=G.WindowGeometry(
            origin_pt=G.Point(*(config.window.expected_origin_pt or (0, 0))),
            size_pt=G.Size(*config.window.expected_size_pt),
            scale=config.window.scale,
        ),
    )


def build_executor(capture, analyzer, machine, task, clock, config, perf=None, budget=None):
    actions = Actions(
        RecordingInput(),
        static_window(config),
        finder=None,
        dry_run=True,
    )
    executor = Executor(
        capture=capture,
        analyzer=analyzer,
        machine=machine,
        actions=actions,
        task=task,
        config=config,
        perf=perf or PerfRecorder(),
        clock=clock,
        sleep=make_clock_sleep(clock),
        budgets=budget or budgets(),
    )
    return executor, actions


# --------------------------------------------------------------------------- #
# static screen / stale frames
# --------------------------------------------------------------------------- #
def test_static_screen_does_not_hang_and_hits_iteration_budget():
    clock = FakeClock()
    config = load_config()
    idle_frame = frame_for(clock, "idle", seq=1)
    machine = StateMachine(make_test_model(), clock=clock)
    analyzer = FakeAnalyzer({"idle": {"bar": 0.99}})
    task = CountingTask(action_state=State.DIALOG)  # never acts
    executor, _ = build_executor(StaticCapture(idle_frame), analyzer, machine, task, clock, config)
    executor.change_threshold = 0.0001  # any tiny difference counts as change
    executor.frame_age_warn_ms = 5.0  # 10 ms ticks -> age crosses this quickly

    result = executor.run()

    assert result.reason == "max_iterations"
    assert result.iterations == 30  # ran to the budget, no hang
    assert result.final_state is State.IDLE
    assert result.actions == 0
    assert executor.perf.counters.get("stale_frames", 0) > 0
    # analysis ran once, then was skipped while the picture was static
    assert len(analyzer.calls) < result.iterations


def test_acts_on_a_stale_but_valid_frame():
    """A hundreds-of-ms-old frame is a valid observation, not an error."""
    clock = FakeClock(start_ns=5_000_000_000)
    config = load_config()
    # frame timestamp is 4 seconds in the past relative to the executor clock
    battle_frame = frame_for(clock, "battle", seq=1)
    battle_frame = replace(battle_frame, timestamp_ns=clock() - 4_000_000_000)
    machine = StateMachine(make_test_model(), clock=clock)
    analyzer = FakeAnalyzer({"battle": {"battle": 0.99}})
    task = CountingTask(action_state=State.BATTLE_TURN, once=True)
    executor, actions = build_executor(StaticCapture(battle_frame), analyzer, machine, task, clock, config)
    executor.change_threshold = 0.0001
    executor.frame_age_max_ms = 1.0  # any age is "too old"...
    executor.frame_age_warn_ms = 1.0

    result = executor.run()

    assert result.reason == "max_iterations"
    assert result.final_state is State.BATTLE_TURN
    assert result.actions > 0  # ...but the state does not expect motion, so we act
    assert actions.records and actions.records[0].dry_run is True


def test_duration_budget_fires_without_any_state_change():
    clock = FakeClock()
    config = load_config()
    frame = frame_for(clock, "idle", seq=1)
    machine = StateMachine(make_test_model(), clock=clock)
    analyzer = FakeAnalyzer({"idle": {"bar": 0.99}})
    task = CountingTask(action_state=State.DIALOG)
    executor, _ = build_executor(
        StaticCapture(frame), analyzer, machine, task, clock, config,
        budget=budgets(max_iterations=10_000, max_duration_s=0.05),
    )
    executor.tick_s = 0.01
    executor.change_threshold = 0.0001

    result = executor.run()

    assert result.reason == "max_duration"
    assert result.duration_s >= 0.05
    assert result.iterations <= 10


def test_stale_frame_escalates_while_state_expects_motion():
    clock = FakeClock(start_ns=10_000_000_000)
    config = load_config()
    battle_frame = frame_for(clock, "battle", seq=1)
    battle_frame = replace(battle_frame, timestamp_ns=clock() - 9_000_000_000)

    model = MachineModel(
        initial=State.UNKNOWN,
        rules=(Rule("battle", State.BATTLE_TURN, 60, "", (TemplatePresent("battle", 0.7),)),),
        definitions={
            State.UNKNOWN: StateDefinition(State.UNKNOWN),
            State.BATTLE_TURN: StateDefinition(State.BATTLE_TURN, expects_motion=True),
            State.ERROR: StateDefinition(State.ERROR),
            State.STOPPED: StateDefinition(State.STOPPED),
        },
        transitions={State.UNKNOWN: tuple(State), State.BATTLE_TURN: (State.ERROR, State.STOPPED), State.ERROR: (State.STOPPED,), State.STOPPED: (State.STOPPED,)},
    )
    machine = StateMachine(model, clock=clock)
    analyzer = FakeAnalyzer({"battle": {"battle": 0.99}})
    task = CountingTask(action_state=State.DIALOG)
    executor, _ = build_executor(StaticCapture(battle_frame), analyzer, machine, task, clock, config)
    executor.change_threshold = 0.0001
    executor.frame_age_max_ms = 100.0

    result = executor.run()

    assert result.reason == "stale_frames"
    assert result.final_state is State.ERROR


# --------------------------------------------------------------------------- #
# action confirmation
# --------------------------------------------------------------------------- #
def test_unconfirmed_action_retries_then_escalates():
    clock = FakeClock()
    config = load_config()
    frame = frame_for(clock, "battle", seq=1)
    machine = StateMachine(make_test_model(), clock=clock)
    analyzer = FakeAnalyzer({"battle": {"battle": 0.99}})
    task = CountingTask(action_state=State.BATTLE_TURN)
    executor, _ = build_executor(
        StaticCapture(frame), analyzer, machine, task, clock, config,
        budget=budgets(action_confirm_timeout_s=0.02, action_retries=1),
    )
    executor.change_threshold = 1.1  # no pixel change is ever "confirmed"
    executor.tick_s = 0.01

    result = executor.run()

    assert result.reason == "action_unconfirmed"
    assert result.final_state is State.ERROR
    assert result.actions >= 2


def test_confirmed_action_is_recorded():
    clock = FakeClock()
    config = load_config()
    frame = frame_for(clock, "battle", seq=1)
    machine = StateMachine(make_test_model(), clock=clock)
    analyzer = FakeAnalyzer({"battle": {"battle": 0.99}})
    task = CountingTask(action_state=State.BATTLE_TURN)
    executor, _ = build_executor(StaticCapture(frame), analyzer, machine, task, clock, config)
    executor.change_threshold = -1.0  # everything counts as a change
    result = executor.run()
    assert result.reason == "max_iterations"
    assert any(e["event"] == "action_confirmed" for e in result.events)


# --------------------------------------------------------------------------- #
# OCR escalation only
# --------------------------------------------------------------------------- #
def test_ocr_is_not_on_the_poll_path_until_unknown_ages_out():
    clock = FakeClock()
    config = load_config()
    frame = frame_for(clock, "blank", seq=1)

    model = MachineModel(
        initial=State.UNKNOWN,
        rules=(
            Rule("battle", State.BATTLE_TURN, 60, "", (TemplatePresent("battle", 0.7),)),
            Rule("turn_text", State.BATTLE_TURN, 55, "", (TextSeenWithin("turn_counter", ("当前回合",), 5.0),)),
        ),
        definitions={
            State.UNKNOWN: StateDefinition(State.UNKNOWN, timeout_s=None),
            State.BATTLE_TURN: StateDefinition(State.BATTLE_TURN),
            State.ERROR: StateDefinition(State.ERROR),
            State.STOPPED: StateDefinition(State.STOPPED),
        },
        transitions={State.UNKNOWN: tuple(State), State.BATTLE_TURN: (State.ERROR, State.UNKNOWN, State.STOPPED), State.ERROR: (State.STOPPED,), State.STOPPED: (State.STOPPED,)},
    )
    machine = StateMachine(model, clock=clock)
    ocr = V.FakeOcr([TextItem("当前回合：1")])
    analyzer = FakeAnalyzer({}, escalate_scores={"blank": {"battle": 0.99}}, ocr=ocr)
    task = CountingTask(action_state=State.DIALOG)
    executor, _ = build_executor(StaticCapture(frame), analyzer, machine, task, clock, config)
    executor.unknown_ocr_after_s = 0.0  # escalate immediately for the test
    executor.change_threshold = 0.0001
    executor.refresh_s = 0.02  # re-analyse a static UNKNOWN screen every 20 ms

    result = executor.run()

    assert result.ocr_calls >= 1
    # the first observation must NOT escalate (unknown was merely "not analysed yet")
    assert analyzer.calls[0] is False
    assert any(analyzer.calls), "OCR escalation never fired for a persistent UNKNOWN state"
    assert any(e["event"] == "ocr_escalation" for e in result.events)


def test_ocr_not_called_when_state_is_known():
    clock = FakeClock()
    config = load_config()
    frame = frame_for(clock, "battle", seq=1)
    machine = StateMachine(make_test_model(), clock=clock)
    ocr = V.FakeOcr([TextItem("当前回合：1")])
    analyzer = FakeAnalyzer({"battle": {"battle": 0.99}}, ocr=ocr)
    task = CountingTask(action_state=State.DIALOG)
    executor, _ = build_executor(StaticCapture(frame), analyzer, machine, task, clock, config)
    executor.change_threshold = 0.0001
    executor.unknown_ocr_after_s = 0.0

    result = executor.run()

    assert result.ocr_calls == 0
    assert all(call is False for call in analyzer.calls)


# --------------------------------------------------------------------------- #
# pause / resume / stop (real clock + thread)
# --------------------------------------------------------------------------- #
def test_pause_resume_and_stop_from_another_thread():
    config = load_config()
    clock = time.monotonic_ns
    frame = C.make_frame(
        np.full((60, 80, 3), 40, dtype=np.uint8),
        G.WindowGeometry(origin_pt=G.Point(300, 120)),
        G.Rect(0.0, 0.0, *G.DEFAULT_REFERENCE.capture_px.as_tuple()),
        seq=1,
        timestamp_ns=clock(),
        meta={"tag": "idle"},
    )
    machine = StateMachine(make_test_model())
    analyzer = FakeAnalyzer({"idle": {"bar": 0.99}})
    task = CountingTask(action_state=State.DIALOG)
    executor = Executor(
        capture=StaticCapture(frame),
        analyzer=analyzer,
        machine=machine,
        actions=Actions(RecordingInput(), static_window(config), finder=None, dry_run=True),
        task=task,
        config=config,
        perf=PerfRecorder(),
        clock=clock,
        budgets=budgets(max_iterations=10_000, max_duration_s=2.0),
    )
    executor.tick_s = 0.002
    executor.change_threshold = 0.0001

    thread = threading.Thread(target=executor.run, daemon=True)
    thread.start()
    try:
        time.sleep(0.05)
        executor.pause()
        time.sleep(0.05)
        assert executor.paused
        assert machine.state is State.PAUSED
        executor.resume()
        time.sleep(0.03)
        assert machine.state is not State.PAUSED
    finally:
        executor.stop()
        thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_run_over_replay_fixtures_does_not_crash():
    """Full pipeline (real replay backend + real analyser + real config)."""
    config = load_config()
    fixtures = ["artifacts/quest.png", "artifacts/flee.png", "artifacts/game.png"]
    import os

    if not all(os.path.exists(p) for p in fixtures):
        pytest.skip("artifacts fixtures not available")
    capture = C.ReplayCapture(fixtures, stage_detector=V.detect_stage_rect)
    machine = StateMachine(model_from_config(config.states))
    perf = PerfRecorder()
    analyzer = V.Analyzer.from_config(config, perf=perf)
    actions = Actions(RecordingInput(), static_window(config), finder=None, dry_run=True)
    executor = Executor(
        capture=capture,
        analyzer=analyzer,
        machine=machine,
        actions=actions,
        task=BattleTask(config.battle),
        config=config,
        perf=perf,
        clock=time.monotonic_ns,
        budgets=budgets(max_iterations=3, max_duration_s=30.0),
        tick_s=0.0,
    )
    result = executor.run()
    assert result.reason == "max_iterations"
    assert result.iterations == 3
    states = {t.to_state for t in result.transitions}
    assert states  # the sequence produced at least one transition


# --------------------------------------------------------------------------- #
# hot-loop budget
# --------------------------------------------------------------------------- #
def test_tick_p95_stays_inside_16ms_frame_budget():
    """Replay-tested hot loop at full resolution: p95 tick work <= 16 ms.

    Frames are pre-decoded (as a warm ScreenCaptureKit stream would be) and the
    real analyser runs every tick on a 2298x1428 artifact, so this measures the
    actual decision path, not PNG I/O.  Capture handoff and OCR are excluded by
    design (OCR is an escalation path, never per tick).
    """
    import cv2

    image = cv2.imread("artifacts/game.png", cv2.IMREAD_COLOR)
    if image is None:
        pytest.skip("artifacts/game.png not available")
    config = load_config()
    clock = FakeClock()
    base = C.make_frame(
        image,
        G.WindowGeometry(origin_pt=G.Point(300, 120)),
        G.Rect(0.0, 0.0, *G.DEFAULT_REFERENCE.capture_px.as_tuple()),
        seq=1,
        timestamp_ns=clock(),
    )
    if base.stage is not None:
        base = base.with_stage(
            G.StageMap(
                stage_px=base.stage.stage_px,
                ref_size=config.reference.stage_px.size,
                ref_offset=G.Point(config.reference.stage_px.x, config.reference.stage_px.y),
            )
        )
    # two patterns so every tick counts as "changed" -> worst case analysis cost
    other = replace(base, array=image.copy())
    other.array[0:8, 0:8] = 255
    frames = [base, other]
    capture = PreloadedCapture(frames, clock)
    machine = StateMachine(model_from_config(config.states), clock=clock)
    perf = PerfRecorder()
    analyzer = V.Analyzer.from_config(config, perf=perf)
    actions = Actions(RecordingInput(), static_window(config), finder=None, dry_run=True)
    task = BattleTask(config.battle)
    executor = Executor(
        capture=capture,
        analyzer=analyzer,
        machine=machine,
        actions=actions,
        task=task,
        config=config,
        perf=perf,
        clock=clock,
        sleep=make_clock_sleep(clock),
        budgets=budgets(max_iterations=24, max_duration_s=120.0),
        tick_s=0.0,
    )

    result = executor.run()

    assert result.reason == "max_iterations", result.detail
    stats = perf.stages.get("tick_total")
    assert stats is not None and stats.count >= 20
    p95 = stats.as_dict()["p95_ms"]
    assert p95 <= 16.0, f"tick p95 {p95:.2f} ms exceeds the 16 ms frame budget"
    # the decision path must actually have run every tick
    assert result.iterations == 24
