"""Task runner: budgets, change-driven polling, safe stop, timings.

Design rules that come straight from the project measurements:

* **Poll on our own clock, never block for a frame.**  ScreenCaptureKit emits
  on content change; a static window can be ~378 ms between frames.  The loop
  reads ``capture.latest()`` every tick (~10 ms), so a static screen costs
  almost nothing and can never hang the runner.
* **A stale frame is a valid observation.**  Each :class:`Frame` carries a
  monotonic timestamp and sequence; the executor decides from ``(frame, age)``.
  Only a state that *expects motion* (``BATTLE_TURN``) escalates on a stale
  frame; a static menu is normal.
* **No OCR on the poll path.**  OCR is an explicit escalation when the state
  has been UNKNOWN for ``unknown_ocr_after_ms`` (84% of the old loop's cost was
  OCR).
* **Budgets and safe stop are first class**: iterations, wall clock, action
  retries, consecutive errors, state timeouts; pause/resume/stop from another
  thread are honoured at the next tick.
* Every stage is recorded in :mod:`seerauto.perf` with the project-wide stage
  names (``capture_read``, ``capture_copy``, ``detect_*``, ``ocr``, ``decide``,
  ``model``, ``input``, ``wait``, ``confirm``) plus ``tick_total`` for the
  hot-loop budget assertion.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, Sequence

from . import geometry as G
from .actions import Action, Actions, StopAction
from .capture import Frame
from .errors import BudgetExceeded, SafetyViolation, SeerAutoError
from .perf import PerfRecorder
from .state import Detection, State, StateMachine, Transition
from .vision import (
    Analyzer,
    NullOcr,
    Signals,
    frame_signature,
    signature_delta,
)

DEFAULT_TICK_S = 0.010
STALE_ESCALATE_AFTER = 3


# --------------------------------------------------------------------------- #
# results / context
# --------------------------------------------------------------------------- #
@dataclass
class TickContext:
    """Everything a task may look at for one decision."""

    now_ns: int
    state: State
    signals: Signals
    detection: Detection | None
    transition: Transition | None
    frame: Frame
    actions: Actions
    config: Any
    elapsed_s: float
    since_action_s: float | None
    changed_since_action: bool
    perf: PerfRecorder

    def template_score(self, name: str) -> float:
        return self.signals.template_score(name)


@dataclass
class RunResult:
    reason: str
    detail: str
    iterations: int
    duration_s: float
    final_state: State
    actions: int
    ocr_calls: int
    transitions: list[Transition]
    perf: dict[str, Any]
    events: list[dict[str, Any]]
    task_stats: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.reason in ("task_done", "max_iterations", "max_duration", "stopped")

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "ok": self.ok,
            "iterations": self.iterations,
            "duration_s": round(self.duration_s, 3),
            "final_state": self.final_state.value,
            "actions": self.actions,
            "ocr_calls": self.ocr_calls,
            "transitions": [t.as_dict() for t in self.transitions],
            "task_stats": self.task_stats,
            "perf": self.perf,
            "events": self.events,
        }


class Task(Protocol):
    name: str

    def reset(self) -> None: ...

    def decide(self, ctx: TickContext) -> Action | StopAction | None: ...

    def stats(self) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- #
# executor
# --------------------------------------------------------------------------- #
class Executor:
    """Runs a task until it finishes, a budget expires, or a safety stop fires."""

    def __init__(
        self,
        capture: Any,
        analyzer: Analyzer,
        machine: StateMachine,
        actions: Actions,
        task: Task,
        config: Any,
        perf: PerfRecorder | None = None,
        clock=time.monotonic_ns,
        perf_clock=time.perf_counter_ns,
        sleep=time.sleep,
        tick_s: float | None = None,
        budgets: Any | None = None,
        max_events: int = 500,
    ):
        self.capture = capture
        self.analyzer = analyzer
        self.machine = machine
        self.actions = actions
        self.task = task
        self.config = config
        self.perf = perf or PerfRecorder()
        self.clock = clock
        # duration measurements must use a monotonic *wall* clock even when the
        # scheduler clock is faked in tests, otherwise tick p95 is meaningless.
        self.perf_clock = perf_clock
        self.sleep = sleep
        self.budgets = budgets or config.budgets
        capture_cfg = getattr(config, "capture", None)
        self.tick_s = float(tick_s if tick_s is not None else (capture_cfg.tick_ms / 1000.0 if capture_cfg else DEFAULT_TICK_S))
        self.change_threshold = float(getattr(capture_cfg, "change_threshold", 0.002))
        self.refresh_s = float(getattr(capture_cfg, "refresh_ms", 500.0)) / 1000.0
        self.frame_age_warn_ms = float(getattr(capture_cfg, "frame_age_warn_ms", 1000.0))
        self.frame_age_max_ms = float(getattr(capture_cfg, "frame_age_max_ms", 8000.0))
        self.unknown_ocr_after_s = float(getattr(capture_cfg, "unknown_ocr_after_ms", 2000.0)) / 1000.0
        self.max_events = int(max_events)
        self.events: list[dict[str, Any]] = []
        self._stop_requested = False
        self._paused = False
        self._pause_changed = False

    # -- external control (safe from another thread) ----------------------- #
    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def stop(self) -> None:
        self._stop_requested = True

    @property
    def paused(self) -> bool:
        return self._paused

    # -- events ------------------------------------------------------------ #
    def _event(self, kind: str, **fields: Any) -> None:
        entry = {
            "event": kind,
            "state": self.machine.state.value,
            "t_ms": round((self.clock() - self._start_ns) / 1e6, 2),
            **{k: v for k, v in fields.items() if v is not None},
        }
        self.events.append(entry)
        if len(self.events) > self.max_events:
            del self.events[0]

    # -- run --------------------------------------------------------------- #
    def run(self) -> RunResult:
        clock = self.clock
        perf = self.perf
        machine = self.machine
        task = self.task
        budgets = self.budgets
        if hasattr(task, "reset"):
            task.reset()

        start_ns = self._start_ns = clock()
        iterations = 0
        errors = 0
        actions_issued = 0
        ocr_calls = 0
        stale_streak = 0
        pending_at_ns: int | None = None
        pending_measured_at_ns = 0
        pending_retries = 0
        sig_at_action: Any = None
        last_frame: Frame | None = None
        last_signature: Any = None
        last_analysis_ns = -1
        last_ocr_ns = -1
        last_action_ns: int | None = None
        # Unset until we have observed UNKNOWN at least once: the first frame of
        # a run is often UNKNOWN simply because nothing was analysed yet, and
        # escalating OCR on it would put OCR on the startup path.
        unknown_since_ns: int | None = None
        signals: Signals | None = None
        detection: Detection | None = None
        last_transition: Transition | None = None
        self._event("run_start", backend=getattr(self.capture, "name", "?"), tick_ms=self.tick_s * 1000)

        def finish(reason: str, detail: str) -> RunResult:
            duration = (clock() - start_ns) / 1e9
            self._event("run_end", reason=reason, detail=detail)
            return RunResult(
                reason=reason,
                detail=detail,
                iterations=iterations,
                duration_s=duration,
                final_state=machine.state,
                actions=actions_issued,
                ocr_calls=ocr_calls,
                transitions=list(machine.transitions),
                perf=perf.summary(),
                events=list(self.events),
                task_stats=task.stats() if hasattr(task, "stats") else {},
            )

        while True:
            now = clock()
            if self._stop_requested:
                machine.stop("executor stop()")
                return finish("stopped", "stop requested")

            if self._paused:
                if not self._pause_changed:
                    machine.pause()
                    self._pause_changed = True
                    self._event("paused")
                self._sleep(self.tick_s)
                continue
            if self._pause_changed:
                machine.resume()
                self._pause_changed = False
                unknown_since_ns = clock()
                self._event("resumed")

            if iterations >= budgets.max_iterations:
                return finish("max_iterations", f"iteration budget {budgets.max_iterations} reached")
            elapsed_s = (now - start_ns) / 1e9
            if elapsed_s >= budgets.max_duration_s:
                return finish("max_duration", f"time budget {budgets.max_duration_s:g}s reached")
            if machine.state is State.ERROR:
                return finish("error_state", machine.definition().description or "ERROR state")
            if machine.state is State.STOPPED:
                return finish("stopped_state", "machine is STOPPED")

            tick_started = now
            tick_started_perf = self.perf_clock()
            try:
                frame, read_ns, decode_ns = self._read_frame()
                perf.record("capture_read", max(0, read_ns - decode_ns), backend=getattr(self.capture, "name", "?"))
                if decode_ns:
                    perf.record("capture_copy", decode_ns, what="decode")

                # ---- cheap change detection (sub-millisecond) ------------- #
                sig_started = self.perf_clock()
                signature = frame_signature(frame)
                perf.record("detect_fingerprint", self.perf_clock() - sig_started, what="frame_signature")
                delta = 1.0 if last_signature is None else signature_delta(last_signature, signature)
                changed = last_signature is None or delta >= self.change_threshold
                if sig_at_action is None:
                    changed_since_action = True
                else:
                    changed_since_action = (
                        signature_delta(sig_at_action, signature) >= self.change_threshold
                    )

                # ---- stale frame policy ---------------------------------- #
                age_ms = frame.age_ms(now)
                definition = machine.definition()
                if age_ms == age_ms and age_ms > self.frame_age_warn_ms:
                    perf.count("stale_frames")
                if (
                    age_ms == age_ms
                    and definition.expects_motion
                    and age_ms > self.frame_age_max_ms
                ):
                    stale_streak += 1
                    if stale_streak >= STALE_ESCALATE_AFTER:
                        machine.fail(f"stale frame {age_ms:.0f}ms while {machine.state.value} expects motion")
                        return finish(
                            "stale_frames",
                            f"no new frame for {age_ms:.0f}ms while {machine.state.value} expects motion",
                        )
                else:
                    stale_streak = 0

                # ---- analyse (only when the picture changed / refresh due) #
                due_refresh = (now - last_analysis_ns) / 1e9 >= self.refresh_s
                need_analysis = signals is None or changed or due_refresh
                transition: Transition | None = None
                if need_analysis:
                    escalate = self._should_escalate(machine, unknown_since_ns, last_ocr_ns, now)
                    escalate = escalate and not isinstance(self.analyzer.ocr, NullOcr)
                    # the analyser records detect_template / detect_fingerprint /
                    # ocr stages itself, so they are separable in perf output
                    signals = self.analyzer.analyze(frame, now, escalate=escalate)
                    if escalate:
                        ocr_calls += 1
                        last_ocr_ns = now
                        self._event("ocr_escalation", age_ms=round((now - (unknown_since_ns or now)) / 1e6, 1))
                    with perf.stage("decide", state=machine.state.value):
                        detection = machine.classify(signals, now)
                        transition = machine.update(detection, now)
                    last_analysis_ns = now
                    if detection is not None and detection.state is State.UNKNOWN and unknown_since_ns is None:
                        unknown_since_ns = now
                    if transition is not None:
                        last_transition = transition
                        self._event(
                            "transition",
                            frm=transition.from_state.value,
                            to=transition.to_state.value,
                            reason=transition.reason,
                            rule=transition.detection.rule_id if transition.detection else None,
                        )
                        if transition.to_state is State.UNKNOWN:
                            unknown_since_ns = now
                        elif transition.to_state is State.ERROR:
                            return finish("error_state", transition.reason)
                        else:
                            unknown_since_ns = None
                else:
                    # no visual change: keep TTL signals alive without re-running
                    # the analyser, and let pending timeouts mature.
                    if signals is not None:
                        machine.refresh_presence(signals, now)

                timeout_transition = machine.tick(now)
                if timeout_transition is not None:
                    last_transition = timeout_transition
                    self._event(
                        "timeout",
                        frm=timeout_transition.from_state.value,
                        to=timeout_transition.to_state.value,
                        reason=timeout_transition.reason,
                    )
                    if timeout_transition.to_state is State.ERROR:
                        return finish("state_timeout", timeout_transition.reason)
                    if timeout_transition.to_state is State.UNKNOWN:
                        unknown_since_ns = now

                # ---- confirm the previous action ------------------------- #
                if pending_at_ns is not None:
                    if changed_since_action:
                        perf.record("confirm", self.perf_clock() - pending_measured_at_ns, what="changed")
                        self._event("action_confirmed", latency_ms=round((clock() - pending_at_ns) / 1e6, 2))
                        pending_at_ns = None
                        pending_retries = 0
                    elif (now - pending_at_ns) / 1e9 >= budgets.action_confirm_timeout_s:
                        pending_retries += 1
                        perf.record("confirm", self.perf_clock() - pending_measured_at_ns, what="unconfirmed")
                        perf.count("actions_unconfirmed")
                        self._event("action_unconfirmed", retries=pending_retries)
                        pending_at_ns = None
                        if pending_retries > budgets.action_retries:
                            machine.fail(f"action not confirmed after {pending_retries} retries")
                            return finish("action_unconfirmed", "click produced no visible change")

                # ---- task policy ----------------------------------------- #
                since_action_s = (
                    None if last_action_ns is None else (now - last_action_ns) / 1e9
                )
                ctx = TickContext(
                    now_ns=now,
                    state=machine.state,
                    signals=signals,  # type: ignore[arg-type]
                    detection=detection,
                    transition=transition or last_transition,
                    frame=frame,
                    actions=self.actions,
                    config=self.config,
                    elapsed_s=elapsed_s,
                    since_action_s=since_action_s,
                    changed_since_action=changed_since_action,
                    perf=perf,
                )
                with perf.stage("model", state=machine.state.value):
                    action = task.decide(ctx)
                if isinstance(action, StopAction):
                    machine.stop(action.reason)
                    return finish("task_done", action.reason)
                if action is not None:
                    if pending_at_ns is not None:
                        # Superseding a click before it was confirmed is itself
                        # evidence that the previous click did nothing. Count it
                        # instead of silently restarting the wait (that is how
                        # the old tower.py loop span forever).
                        pending_retries += 1
                        perf.count("actions_superseded")
                        self._event("action_superseded", retries=pending_retries)
                        if pending_retries > budgets.action_retries:
                            machine.fail(
                                f"action not confirmed after {pending_retries} attempts"
                            )
                            return finish(
                                "action_unconfirmed",
                                "clicks superseded without any visible change",
                            )
                    with perf.stage("input", action=type(action).__name__):
                        self.actions.execute(action, self.config)
                    actions_issued += 1
                    pending_at_ns = clock()
                    pending_measured_at_ns = self.perf_clock()
                    last_action_ns = now
                    sig_at_action = signature
                    self._event("action", action=type(action).__name__, label=action.label)

                # ---- remember frame / tick accounting -------------------- #
                last_frame = frame
                last_signature = signature
                errors = 0
                iterations += 1
                perf.next_iteration()
                work_ns = self.perf_clock() - tick_started_perf
                perf.record("tick_total", work_ns)
                self._sleep_remaining(work_ns, perf)

            except SafetyViolation as exc:
                machine.fail(f"safety: {exc}")
                self._event("safety_violation", detail=str(exc))
                return finish("safety", str(exc))
            except SeerAutoError as exc:
                errors += 1
                perf.count("errors")
                self._event("error", detail=f"{type(exc).__name__}: {exc}")
                if errors > budgets.max_consecutive_errors:
                    machine.fail(f"too many errors: {exc}")
                    return finish("error", f"{type(exc).__name__}: {exc}")
                self._sleep(self.tick_s)
            except Exception as exc:  # noqa: BLE001 - never leave the game mid-input
                machine.fail(f"unexpected: {exc!r}")
                self._event("exception", detail=repr(exc))
                return finish("exception", repr(exc))

    # -- internals --------------------------------------------------------- #
    def _read_frame(self) -> tuple[Frame, int, int]:
        """Read the newest frame; returns (frame, total_ns, decode_ns)."""
        started = self.perf_clock()
        latest = getattr(self.capture, "latest", None)
        frame = latest() if callable(latest) else self.capture.grab()
        total = self.perf_clock() - started
        return frame, total, frame.decode_ns

    def _should_escalate(
        self,
        machine: StateMachine,
        unknown_since_ns: int | None,
        last_ocr_ns: int,
        now: int,
    ) -> bool:
        if machine.state is not State.UNKNOWN or unknown_since_ns is None:
            return False
        if (now - unknown_since_ns) / 1e9 < self.unknown_ocr_after_s:
            return False
        if last_ocr_ns >= 0 and (now - last_ocr_ns) / 1e9 < 2.0:
            return False
        return True

    def _sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleep(seconds)

    def _sleep_remaining(self, work_ns: int, perf: PerfRecorder) -> None:
        remaining = self.tick_s - work_ns / 1e9
        if remaining > 0:
            started = self.perf_clock()
            self.sleep(remaining)
            self.perf.record("wait", self.perf_clock() - started, what="tick")


