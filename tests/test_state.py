"""State-machine tests.

The important ones are the P7 regressions from ``docs/KNOWN_PROBLEMS.md``:

* a turn badge that disappears for an animation frame must not fall through to
  ``idle`` (``template_seen_within`` / ``text_seen_within`` keep it alive);
* an UNKNOWN state must escalate on a timeout instead of clicking blindly;
* only configured transitions are allowed, and repeated blocked detections
  escalate to UNKNOWN.

All signals here are synthetic - no game, no images, no cv2 needed.
"""

from __future__ import annotations

import pytest

from seerauto import geometry as G
from seerauto.state import (
    Condition,
    Detection,
    FingerprintNear,
    MachineModel,
    Rule,
    SignalBook,
    State,
    StateDefinition,
    StateMachine,
    TemplateAbsent,
    TemplatePresent,
    TemplateSeenWithin,
    TextContains,
    TextSeenWithin,
    condition_from_config,
    model_from_config,
)
from seerauto.vision import Fingerprint, Match, Signals


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self, start_ns: int = 0):
        self.now = int(start_ns)

    def __call__(self) -> int:
        return self.now

    def advance_s(self, seconds: float) -> int:
        self.now += int(seconds * 1e9)
        return self.now


def match(name: str, score: float) -> Match:
    rect = G.Rect(0, 0, 10, 10)
    return Match(name=name, score=score, rect_frame=rect, rect_canon=rect, threshold=0.5)


def signals_with(**scores: float) -> Signals:
    signals = Signals()
    for name, score in scores.items():
        signals.templates[name] = match(name, score)
    return signals


def text_signals(*texts: str) -> Signals:
    from seerauto.vision import TextItem

    signals = Signals()
    signals.text = [TextItem(text=t) for t in texts]
    return signals


def simple_model(**overrides) -> MachineModel:
    rules = overrides.pop(
        "rules",
        (
            Rule("battle", State.BATTLE_TURN, 60, "grid", (TemplatePresent("grid", 0.7),)),
            Rule("dialog", State.DIALOG, 50, "map", (TemplatePresent("map", 0.9),)),
            Rule("idle", State.IDLE, 20, "bar", (TemplatePresent("bar", 0.8),)),
        ),
    )
    definitions = overrides.pop(
        "definitions",
        {
            State.UNKNOWN: StateDefinition(State.UNKNOWN, timeout_s=5.0, on_timeout=State.ERROR),
            State.IDLE: StateDefinition(State.IDLE, actions=("click_enemy",), timeout_s=None),
            State.BATTLE_TURN: StateDefinition(
                State.BATTLE_TURN, actions=("battle_round",), timeout_s=None, expects_motion=True
            ),
            State.DIALOG: StateDefinition(State.DIALOG, actions=("dismiss",), timeout_s=None),
            State.ERROR: StateDefinition(State.ERROR),
            State.PAUSED: StateDefinition(State.PAUSED),
            State.STOPPED: StateDefinition(State.STOPPED),
        },
    )
    transitions = overrides.pop(
        "transitions",
        {
            State.UNKNOWN: tuple(State),
            State.IDLE: (State.BATTLE_TURN, State.DIALOG, State.ERROR, State.PAUSED, State.STOPPED),
            State.BATTLE_TURN: (State.BATTLE_TURN, State.IDLE, State.DIALOG, State.ERROR, State.PAUSED, State.STOPPED),
            State.DIALOG: (State.IDLE, State.BATTLE_TURN, State.ERROR, State.PAUSED, State.STOPPED),
            State.ERROR: (State.UNKNOWN, State.STOPPED),
            State.PAUSED: (State.UNKNOWN, State.STOPPED),
            State.STOPPED: (State.STOPPED,),
        },
    )
    return MachineModel(
        initial=State.UNKNOWN,
        rules=tuple(rules),
        definitions=definitions,
        transitions=transitions,
        blocked_switch_after=overrides.pop("blocked_switch_after", 3),
        **overrides,
    )


# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #
def test_classify_highest_priority_wins():
    clock = FakeClock()
    machine = StateMachine(simple_model(), clock=clock)
    detection = machine.classify(signals_with(grid=0.95, map=0.95, bar=0.99), clock())
    assert detection.state is State.BATTLE_TURN
    assert detection.rule_id == "battle"


def test_classify_and_semantics_and_fallthrough():
    clock = FakeClock()
    rules = (
        Rule("both", State.DIALOG, 60, "both", (TemplatePresent("a", 0.8), TemplatePresent("b", 0.8))),
        Rule("only_a", State.IDLE, 40, "a", (TemplatePresent("a", 0.8),)),
    )
    machine = StateMachine(simple_model(rules=rules), clock=clock)
    assert machine.classify(signals_with(a=0.9), clock()).state is State.IDLE
    assert machine.classify(signals_with(a=0.9, b=0.9), clock()).state is State.DIALOG


def test_classify_no_match_is_unknown():
    machine = StateMachine(simple_model(), clock=FakeClock())
    detection = machine.classify(signals_with(), 0)
    assert detection.state is State.UNKNOWN
    assert detection.rule_id == "no_rule"


def test_disabled_rule_never_matches():
    machine = StateMachine(
        simple_model(rules=(Rule("battle", State.BATTLE_TURN, 60, "", (TemplatePresent("grid", 0.7),), enabled=False),)),
        clock=FakeClock(),
    )
    assert machine.classify(signals_with(grid=0.99), 0).state is State.UNKNOWN


def test_template_absent_condition():
    rules = (Rule("idle", State.IDLE, 10, "", (TemplateAbsent("grid", 0.4),)),)
    machine = StateMachine(simple_model(rules=rules), clock=FakeClock())
    assert machine.classify(signals_with(grid=0.1), 0).state is State.IDLE
    assert machine.classify(signals_with(grid=0.9), 0).state is State.UNKNOWN


# --------------------------------------------------------------------------- #
# last-seen-with-TTL (the P7 fix)
# --------------------------------------------------------------------------- #
def test_template_seen_within_survives_animation_gap_then_expires():
    clock = FakeClock()
    rules = (Rule("battle", State.BATTLE_TURN, 60, "", (TemplateSeenWithin("badge", 2.0, 0.75),)),)
    machine = StateMachine(simple_model(rules=rules), clock=clock)

    # badge visible
    assert machine.classify(signals_with(badge=0.9), clock()).state is State.BATTLE_TURN
    # ... animation hides it for 1 second: still battle (inconclusive, not idle)
    clock.advance_s(1.0)
    assert machine.classify(signals_with(), clock()).state is State.BATTLE_TURN
    # ... TTL expires: now it is genuinely gone
    clock.advance_s(1.5)
    assert machine.classify(signals_with(), clock()).state is State.UNKNOWN


def test_text_seen_within_is_not_a_binary_predicate():
    """The exact P7 bug: '当前回合' absent for 2 frames used to mean IDLE."""
    clock = FakeClock()
    rules = (
        Rule("turn_text", State.BATTLE_TURN, 55, "", (TextSeenWithin("turn_counter", ("当前回合",), 3.0),)),
        Rule("idle", State.IDLE, 20, "", (TemplatePresent("bar", 0.8),)),
    )
    machine = StateMachine(simple_model(rules=rules), clock=clock)
    assert machine.classify(text_signals("当前回合：2"), clock()).state is State.BATTLE_TURN
    clock.advance_s(1.0)
    detection = machine.classify(text_signals("克制-1415"), clock())
    assert detection.state is State.BATTLE_TURN  # inconclusive, NOT idle
    clock.advance_s(4.0)
    assert machine.classify(text_signals("克制-1415"), clock()).state is State.UNKNOWN


def test_text_contains_requires_current_observation():
    rules = (Rule("victory", State.BATTLE_RESULT, 90, "", (TextContains(("胜利",)),)),)
    machine = StateMachine(simple_model(rules=rules), clock=FakeClock())
    assert machine.classify(text_signals("胜利"), 0).state is State.BATTLE_RESULT
    assert machine.classify(text_signals("当前回合：9"), 0).state is State.UNKNOWN


def test_fingerprint_near_condition():
    expected = Fingerprint(grid=(2, 2), cells=(10.0,) * 12, mean=(10.0, 10.0, 10.0), std=1.0)
    near = Fingerprint(grid=(2, 2), cells=(12.0,) * 12, mean=(12.0, 12.0, 12.0), std=1.0)
    far = Fingerprint(grid=(2, 2), cells=(90.0,) * 12, mean=(90.0, 90.0, 90.0), std=1.0)
    rules = (Rule("dialog", State.DIALOG, 50, "", (FingerprintNear("panel", max_distance=25.0, expected=expected),)),)
    machine = StateMachine(simple_model(rules=rules), clock=FakeClock())
    signals = Signals()
    signals.fingerprints["panel"] = near
    assert machine.classify(signals, 0).state is State.DIALOG
    signals.fingerprints["panel"] = far
    assert machine.classify(signals, 0).state is State.UNKNOWN
    # missing fingerprint is not a match
    assert machine.classify(Signals(), 0).state is State.UNKNOWN


def test_signal_book_ttl():
    """TTL must actually expire (last seen 1 ms ago vs a 0.1 ms window)."""
    book = SignalBook()
    book.note("k", True, 1_000_000)  # t = 1 ms
    assert book.seen_within("k", 1.0, 2_000_000)  # 1 ms ago, 1 s window -> alive
    assert not book.seen_within("k", 0.0001, 2_000_000)  # 1 ms ago > 0.1 ms window
    assert not book.seen_within("missing", 10.0, 0)
    assert book.seen_count("k") == 1
    assert "last_seen_ns" in book.snapshot()


# --------------------------------------------------------------------------- #
# transitions
# --------------------------------------------------------------------------- #
def test_allowed_transition_is_recorded():
    clock = FakeClock()
    machine = StateMachine(simple_model(), clock=clock)
    detection = machine.classify(signals_with(bar=0.99), clock())
    transition = machine.update(detection, clock())
    assert transition is not None
    assert transition.from_state is State.UNKNOWN
    assert transition.to_state is State.IDLE
    assert machine.state is State.IDLE
    assert machine.allowed_actions() == ("click_enemy",)
    assert len(machine.transitions) == 1


def test_same_state_does_not_emit_transition():
    clock = FakeClock()
    machine = StateMachine(simple_model(), clock=clock)
    machine.update(machine.classify(signals_with(bar=0.99), clock()), clock())
    assert machine.update(machine.classify(signals_with(bar=0.99), clock()), clock()) is None


def test_blocked_transitions_escalate_to_unknown():
    clock = FakeClock()
    model = simple_model(blocked_switch_after=2)
    machine = StateMachine(model, clock=clock)
    # put the machine in DIALOG
    machine.update(machine.classify(signals_with(map=0.99), clock()), clock())
    assert machine.state is State.DIALOG
    # REWARD is not reachable from DIALOG in this test model -> blocked
    blocked_detection = Detection(
        state=State.REWARD, rule_id="x", detail="", evidence=(), at_ns=clock()
    )
    assert machine.update(blocked_detection, clock()) is None
    transition = machine.update(blocked_detection, clock())
    assert transition is not None
    assert transition.to_state is State.UNKNOWN
    assert "blocked" in transition.reason


def test_unknown_is_always_reachable():
    clock = FakeClock()
    machine = StateMachine(simple_model(), clock=clock)
    machine.update(machine.classify(signals_with(bar=0.99), clock()), clock())
    unknown_detection = Detection(state=State.UNKNOWN, rule_id="no_rule", detail="", evidence=(), at_ns=clock())
    transition = machine.update(unknown_detection, clock())
    assert transition is not None and transition.to_state is State.UNKNOWN


# --------------------------------------------------------------------------- #
# timeouts / progress
# --------------------------------------------------------------------------- #
def test_timeout_escalates_from_unknown():
    clock = FakeClock()
    model = simple_model()
    machine = StateMachine(model, clock=clock)
    assert machine.state is State.UNKNOWN
    assert machine.tick(clock()) is None
    clock.advance_s(5.5)
    transition = machine.tick(clock())
    assert transition is not None
    assert transition.to_state is State.ERROR
    assert "timeout" in transition.reason
    assert machine.timeouts == 1


def test_progress_timeout_resets_when_evidence_changes():
    clock = FakeClock()
    definitions = {
        State.UNKNOWN: StateDefinition(State.UNKNOWN, timeout_s=None),
        State.BATTLE_TURN: StateDefinition(State.BATTLE_TURN, timeout_s=2.0, on_timeout=State.ERROR),
        State.ERROR: StateDefinition(State.ERROR),
    }
    model = simple_model(definitions=definitions)
    machine = StateMachine(model, clock=clock)
    # battle round 1
    machine.update(machine.classify(signals_with(grid=0.90), clock()), clock())
    assert machine.state is State.BATTLE_TURN
    clock.advance_s(1.5)
    # still battle, but evidence changed (score moved) -> progress
    machine.update(machine.classify(signals_with(grid=0.95), clock()), clock())
    assert machine.tick(clock()) is None
    # no change for > timeout -> escalate
    clock.advance_s(2.5)
    transition = machine.tick(clock())
    assert transition is not None and transition.to_state is State.ERROR


def test_entry_timeout_mode_ignores_progress():
    clock = FakeClock()
    definitions = {
        State.UNKNOWN: StateDefinition(State.UNKNOWN, timeout_s=None),
        State.BATTLE_TURN: StateDefinition(
            State.BATTLE_TURN, timeout_s=1.0, timeout_mode="entry", on_timeout=State.IDLE
        ),
        State.IDLE: StateDefinition(State.IDLE),
    }
    machine = StateMachine(simple_model(definitions=definitions), clock=clock)
    machine.update(machine.classify(signals_with(grid=0.90), clock()), clock())
    clock.advance_s(0.9)
    machine.update(machine.classify(signals_with(grid=0.99), clock()), clock())
    clock.advance_s(0.2)
    transition = machine.tick(clock())
    assert transition is not None and transition.to_state is State.IDLE


# --------------------------------------------------------------------------- #
# manual control
# --------------------------------------------------------------------------- #
def test_pause_ignores_detections_and_resume_goes_unknown():
    clock = FakeClock()
    machine = StateMachine(simple_model(), clock=clock)
    machine.update(machine.classify(signals_with(bar=0.99), clock()), clock())
    machine.pause()
    assert machine.state is State.PAUSED
    assert machine.update(machine.classify(signals_with(grid=0.99), clock()), clock()) is None
    assert machine.state is State.PAUSED
    machine.resume()
    assert machine.state is State.UNKNOWN


def test_stop_is_terminal():
    clock = FakeClock()
    machine = StateMachine(simple_model(), clock=clock)
    machine.stop()
    assert machine.state is State.STOPPED
    assert machine.update(machine.classify(signals_with(grid=0.99), clock()), clock()) is None
    assert machine.tick(clock() + 10**12) is None


def test_fail_forces_error():
    clock = FakeClock()
    machine = StateMachine(simple_model(), clock=clock)
    machine.fail("boom")
    assert machine.state is State.ERROR


def test_snapshot_contains_state_and_actions():
    machine = StateMachine(simple_model(), clock=FakeClock())
    snap = machine.snapshot()
    assert snap["state"] == "UNKNOWN"
    assert snap["timeouts"] == 0
    assert "signals" in snap


# --------------------------------------------------------------------------- #
# config -> model
# --------------------------------------------------------------------------- #
def test_condition_from_config_kinds():
    from seerauto.vision import Fingerprint

    expected = Fingerprint(grid=(2, 2), cells=(0.0,) * 12, mean=(0.0, 0.0, 0.0), std=0.0)
    cases = [
        ({"kind": "template_present", "name": "t", "min_score": 0.7}, TemplatePresent),
        ({"kind": "template_absent", "name": "t", "max_score": 0.3}, TemplateAbsent),
        ({"kind": "template_seen_within", "name": "t", "ttl_s": 1.5}, TemplateSeenWithin),
        ({"kind": "text_contains", "needles": ["x"]}, TextContains),
        ({"kind": "text_seen_within", "signal": "s", "needles": ["x"], "ttl_s": 2.0}, TextSeenWithin),
        ({"kind": "fingerprint_near", "fingerprint": "f", "max_distance": 5.0}, FingerprintNear),
    ]
    for raw, cls in cases:
        condition = condition_from_config(raw, {"f": expected})
        assert isinstance(condition, cls), raw
    with pytest.raises(ValueError):
        condition_from_config({"kind": "nonsense"})


def test_model_from_default_config():
    """The shipped config must build a working model with all 9 states."""
    from seerauto.config import load_config

    config = load_config()
    machine = StateMachine(model_from_config(config.states), clock=FakeClock())
    assert machine.state is State.UNKNOWN
    # UNKNOWN must have a timeout escalation path, never a blind click
    unknown_def = config.states.definitions["UNKNOWN"]
    assert unknown_def.timeout_s is not None
    assert unknown_def.on_timeout == "ERROR"
    # every state from the required list exists
    for name in ("UNKNOWN", "IDLE", "BATTLE_TURN", "BATTLE_RESULT", "REWARD", "DIALOG", "ERROR", "PAUSED", "STOPPED"):
        assert name in config.states.definitions
    # battle turn budget is config-driven and big enough for 165-turn fights
    assert config.battle.max_turns_per_battle >= 200
