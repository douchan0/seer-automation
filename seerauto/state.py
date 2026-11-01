"""Explicit, data-driven state machine.

The old battle loop classified frames with a single ``'当前回合' in text`` test
(``KNOWN_PROBLEMS.md`` P7).  That fails on every animation frame where the badge
is briefly hidden and silently falls through to ``idle``.  Here:

* states, rules, transitions, actions and timeouts all come from config;
* a rule is an AND of typed conditions over :class:`seerauto.vision.Signals`;
* **absence of a signal is inconclusive, not negative**: ``template_seen_within``
  and ``text_seen_within`` keep a signal alive for a TTL after it was last
  observed (the turn badge disappears between animations; the OCR text may be
  unreadable for a frame) - only after the TTL expires does it count as gone;
* ``UNKNOWN`` has a timeout that escalates instead of clicking blindly;
* every state declares the actions it allows and where it may go next.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .vision import Fingerprint, Signals, TextItem, find_text, signature_distance


# --------------------------------------------------------------------------- #
# states
# --------------------------------------------------------------------------- #
class State(str, Enum):
    UNKNOWN = "UNKNOWN"
    IDLE = "IDLE"
    BATTLE_TURN = "BATTLE_TURN"
    BATTLE_RESULT = "BATTLE_RESULT"
    REWARD = "REWARD"
    DIALOG = "DIALOG"
    ERROR = "ERROR"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"

    @classmethod
    def parse(cls, value: str | "State") -> "State":
        if isinstance(value, State):
            return value
        try:
            return cls[str(value).upper()]
        except KeyError as exc:
            raise ValueError(f"unknown state {value!r}") from exc


TERMINAL_STATES = frozenset({State.STOPPED})
MANUAL_STATES = frozenset({State.PAUSED, State.STOPPED})


# --------------------------------------------------------------------------- #
# conditions
# --------------------------------------------------------------------------- #
class Condition:
    """One typed predicate over a Signals snapshot (+ the last-seen book)."""

    kind = "base"

    def evaluate(self, signals: Signals, book: "SignalBook", now_ns: int) -> bool:
        raise NotImplementedError

    def describe(self) -> str:
        return self.kind

    def observations(self) -> Iterable[tuple[str, bool, Signals, dict[str, Any]]]:
        """Keys this condition wants the machine to refresh in the book."""
        return ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.describe()}>"


@dataclass(frozen=True)
class TemplatePresent(Condition):
    name: str
    min_score: float = 0.8
    roi: tuple[float, float, float, float] | None = None
    kind = "template_present"

    def evaluate(self, signals: Signals, book: "SignalBook", now_ns: int) -> bool:
        return signals.template_score(self.name) >= self.min_score

    def describe(self) -> str:
        return f"template({self.name}) >= {self.min_score:g}"


@dataclass(frozen=True)
class TemplateAbsent(Condition):
    name: str
    max_score: float = 0.4
    roi: tuple[float, float, float, float] | None = None
    kind = "template_absent"

    def evaluate(self, signals: Signals, book: "SignalBook", now_ns: int) -> bool:
        return signals.template_score(self.name) <= self.max_score

    def describe(self) -> str:
        return f"template({self.name}) <= {self.max_score:g}"


@dataclass(frozen=True)
class TemplateSeenWithin(Condition):
    """True while the template was matched at least once within ``ttl_s``."""

    name: str
    ttl_s: float
    min_score: float = 0.8
    kind = "template_seen_within"

    @property
    def key(self) -> str:
        return f"template:{self.name}"

    def evaluate(self, signals: Signals, book: "SignalBook", now_ns: int) -> bool:
        return book.seen_within(self.key, self.ttl_s, now_ns)

    def observations(self) -> Iterable[tuple[str, bool, Signals, dict[str, Any]]]:
        yield (self.key, "template", None, {"name": self.name, "min_score": self.min_score})

    def describe(self) -> str:
        return f"template({self.name}) seen within {self.ttl_s:g}s"


@dataclass(frozen=True)
class TextContains(Condition):
    needles: tuple[str, ...]
    exact: bool = False
    kind = "text_contains"

    def evaluate(self, signals: Signals, book: "SignalBook", now_ns: int) -> bool:
        return bool(find_text(signals.text, self.needles, exact=self.exact))

    def describe(self) -> str:
        return f"text({'|'.join(self.needles)})"


@dataclass(frozen=True)
class TextAbsent(Condition):
    needles: tuple[str, ...]
    exact: bool = False
    kind = "text_absent"

    def evaluate(self, signals: Signals, book: "SignalBook", now_ns: int) -> bool:
        return not find_text(signals.text, self.needles, exact=self.exact)

    def describe(self) -> str:
        return f"!text({'|'.join(self.needles)})"


@dataclass(frozen=True)
class TextSeenWithin(Condition):
    """Last-seen-with-TTL for OCR text (the turn-counter problem).

    Present now -> TTL refreshed.  Absent but seen recently -> still true
    (inconclusive: animation / OCR jitter).  Absent longer than TTL -> false.
    """

    signal: str
    needles: tuple[str, ...]
    ttl_s: float
    exact: bool = False
    kind = "text_seen_within"

    @property
    def key(self) -> str:
        return f"text:{self.signal}"

    def evaluate(self, signals: Signals, book: "SignalBook", now_ns: int) -> bool:
        return book.seen_within(self.key, self.ttl_s, now_ns)

    def observations(self) -> Iterable[tuple[str, bool, Signals, dict[str, Any]]]:
        yield (self.key, "text", None, {"needles": self.needles, "exact": self.exact})

    def describe(self) -> str:
        return f"text({self.signal}) seen within {self.ttl_s:g}s"


@dataclass(frozen=True)
class FingerprintNear(Condition):
    fingerprint: str
    max_distance: float = 25.0
    expected: Fingerprint | None = None
    kind = "fingerprint_near"

    def evaluate(self, signals: Signals, book: "SignalBook", now_ns: int) -> bool:
        observed = signals.fingerprints.get(self.fingerprint)
        if observed is None or self.expected is None:
            return False
        return signature_distance(observed, self.expected) <= self.max_distance

    def describe(self) -> str:
        return f"fingerprint({self.fingerprint}) <= {self.max_distance:g}"


@dataclass(frozen=True)
class FingerprintFar(Condition):
    fingerprint: str
    min_distance: float = 25.0
    expected: Fingerprint | None = None
    kind = "fingerprint_far"

    def evaluate(self, signals: Signals, book: "SignalBook", now_ns: int) -> bool:
        observed = signals.fingerprints.get(self.fingerprint)
        if observed is None or self.expected is None:
            return False
        return signature_distance(observed, self.expected) >= self.min_distance

    def describe(self) -> str:
        return f"fingerprint({self.fingerprint}) >= {self.min_distance:g}"


# --------------------------------------------------------------------------- #
# last-seen registry
# --------------------------------------------------------------------------- #
class SignalBook:
    """Monotonic last-seen timestamps for named signals (template/text)."""

    def __init__(self) -> None:
        self._last_seen: dict[str, int] = {}
        self._seen_count: dict[str, int] = {}

    def note(self, key: str, present: bool, now_ns: int) -> None:
        if present:
            self._last_seen[key] = int(now_ns)
            self._seen_count[key] = self._seen_count.get(key, 0) + 1

    def last_seen(self, key: str) -> int | None:
        return self._last_seen.get(key)

    def seen_count(self, key: str) -> int:
        return self._seen_count.get(key, 0)

    def seen_within(self, key: str, ttl_s: float, now_ns: int) -> bool:
        last = self._last_seen.get(key)
        if last is None:
            return False
        return (int(now_ns) - last) <= int(ttl_s * 1e9)

    def snapshot(self) -> dict[str, Any]:
        return {
            "last_seen_ns": dict(self._last_seen),
            "seen_count": dict(self._seen_count),
        }


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rule:
    rule_id: str
    state: State
    priority: int
    detail: str
    conditions: tuple[Condition, ...]
    enabled: bool = True

    def matches(self, signals: Signals, book: SignalBook, now_ns: int) -> bool:
        return all(c.evaluate(signals, book, now_ns) for c in self.conditions)

    def evidence(self, signals: Signals, book: SignalBook, now_ns: int) -> tuple[str, ...]:
        out: list[str] = []
        for condition in self.conditions:
            try:
                ok = condition.evaluate(signals, book, now_ns)
            except Exception:  # noqa: BLE001 - evidence must never break classify
                ok = False
            out.append(f"{'+' if ok else '-'}{condition.describe()}")
        return tuple(out)


@dataclass(frozen=True)
class Detection:
    state: State
    rule_id: str
    detail: str
    evidence: tuple[str, ...]
    at_ns: int = 0

    @property
    def key(self) -> str:
        """Changes when the *content* of the observation changes (progress)."""
        return f"{self.state.value}|{self.rule_id}|{'/'.join(self.evidence)}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "rule": self.rule_id,
            "detail": self.detail,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class Transition:
    from_state: State
    to_state: State
    reason: str
    at_ns: int
    detection: Detection | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.from_state.value,
            "to": self.to_state.value,
            "reason": self.reason,
            "at_ns": self.at_ns,
            "rule": self.detection.rule_id if self.detection else None,
        }


@dataclass(frozen=True)
class StateDefinition:
    state: State
    actions: tuple[str, ...] = ()
    timeout_s: float | None = None
    on_timeout: State = State.ERROR
    expects_motion: bool = False
    timeout_mode: str = "progress"  # progress | entry
    description: str = ""


@dataclass
class MachineModel:
    initial: State = State.UNKNOWN
    rules: tuple[Rule, ...] = ()
    definitions: dict[State, StateDefinition] = field(default_factory=dict)
    transitions: dict[State, tuple[State, ...]] = field(default_factory=dict)
    blocked_switch_after: int = 3

    def definition(self, state: State) -> StateDefinition:
        return self.definitions.get(state, StateDefinition(state=state))

    def can_transition(self, src: State, dst: State) -> bool:
        if dst is State.UNKNOWN:
            return True  # UNKNOWN is the always-reachable "I don't know" state
        allowed = self.transitions.get(src)
        if allowed is None:
            return False
        return dst in allowed

    def actions_for(self, state: State) -> tuple[str, ...]:
        return self.definition(state).actions


# --------------------------------------------------------------------------- #
# config -> model
# --------------------------------------------------------------------------- #
def _needles(raw: Mapping[str, Any]) -> tuple[str, ...]:
    needles = raw.get("needles") or raw.get("text") or []
    if isinstance(needles, str):
        needles = [needles]
    if not needles:
        raise ValueError("text condition needs 'needles'")
    return tuple(str(n) for n in needles)


def condition_from_config(
    raw: Mapping[str, Any],
    fingerprints: Mapping[str, Fingerprint] | None = None,
) -> Condition:
    kind = str(raw.get("kind", ""))
    fingerprints = fingerprints or {}
    if kind == "template_present":
        return TemplatePresent(
            name=str(raw["name"]),
            min_score=float(raw.get("min_score", 0.8)),
            roi=tuple(raw["roi"]) if raw.get("roi") else None,  # type: ignore[arg-type]
        )
    if kind == "template_absent":
        return TemplateAbsent(
            name=str(raw["name"]),
            max_score=float(raw.get("max_score", 0.4)),
            roi=tuple(raw["roi"]) if raw.get("roi") else None,  # type: ignore[arg-type]
        )
    if kind == "template_seen_within":
        return TemplateSeenWithin(
            name=str(raw["name"]),
            ttl_s=float(raw["ttl_s"]),
            min_score=float(raw.get("min_score", 0.8)),
        )
    if kind == "text_contains":
        return TextContains(needles=_needles(raw), exact=bool(raw.get("exact", False)))
    if kind == "text_absent":
        return TextAbsent(needles=_needles(raw), exact=bool(raw.get("exact", False)))
    if kind == "text_seen_within":
        return TextSeenWithin(
            signal=str(raw.get("signal") or "|".join(_needles(raw))),
            needles=_needles(raw),
            ttl_s=float(raw["ttl_s"]),
            exact=bool(raw.get("exact", False)),
        )
    if kind in ("fingerprint_near", "fingerprint_far"):
        name = str(raw.get("fingerprint") or raw.get("name"))
        expected = fingerprints.get(name)
        # Fingerprint carries pixels, not thresholds: distance bounds live on
        # the rule (config) and default here when omitted.
        if kind == "fingerprint_near":
            return FingerprintNear(
                fingerprint=name,
                max_distance=float(raw.get("max_distance", 25.0)),
                expected=expected,
            )
        return FingerprintFar(
            fingerprint=name,
            min_distance=float(raw.get("min_distance", 25.0)),
            expected=expected,
        )
    raise ValueError(f"unknown condition kind {kind!r}")


def model_from_config(
    states_cfg: Any,
    fingerprints: Mapping[str, Fingerprint] | None = None,
) -> MachineModel:
    """Build a :class:`MachineModel` from :class:`seerauto.config.StateMachineConfig`."""
    rules: list[Rule] = []
    for rule_cfg in states_cfg.rules:
        conditions = tuple(
            condition_from_config(raw, fingerprints) for raw in rule_cfg.conditions
        )
        rules.append(
            Rule(
                rule_id=rule_cfg.rule_id,
                state=State.parse(rule_cfg.state),
                priority=rule_cfg.priority,
                detail=rule_cfg.detail,
                conditions=conditions,
                enabled=rule_cfg.enabled,
            )
        )
    rules.sort(key=lambda r: (-r.priority, r.rule_id))
    definitions: dict[State, StateDefinition] = {}
    for name, def_cfg in states_cfg.definitions.items():
        state = State.parse(name)
        definitions[state] = StateDefinition(
            state=state,
            actions=tuple(def_cfg.actions),
            timeout_s=def_cfg.timeout_s,
            on_timeout=State.parse(def_cfg.on_timeout),
            expects_motion=def_cfg.expects_motion,
            timeout_mode=str(getattr(def_cfg, "timeout_mode", "progress")),
            description=def_cfg.description,
        )
    transitions: dict[State, tuple[State, ...]] = {}
    for src, targets in states_cfg.transitions.items():
        transitions[State.parse(src)] = tuple(State.parse(t) for t in targets)
    initial = State.parse(states_cfg.initial)
    definitions.setdefault(initial, StateDefinition(state=initial))
    transitions.setdefault(initial, tuple(State))
    return MachineModel(
        initial=initial,
        rules=tuple(rules),
        definitions=definitions,
        transitions=transitions,
        blocked_switch_after=int(states_cfg.blocked_switch_after),
    )


# --------------------------------------------------------------------------- #
# machine
# --------------------------------------------------------------------------- #
class StateMachine:
    """Classify each Signals snapshot and move between states safely."""

    def __init__(self, model: MachineModel, clock=None):
        import time as _time

        self.model = model
        self.clock = clock or (lambda: _time.monotonic_ns())
        self.book = SignalBook()
        self._state: State = model.initial
        self._entered_ns = self.clock()
        self._progress_ns = self._entered_ns
        self._last_detection: Detection | None = None
        self._blocked = 0
        self._paused_from: State | None = None
        self.transitions: list[Transition] = []
        self.timeouts = 0
        # pre-compute which signals the rules observe
        self._observers: list[tuple[str, Condition]] = []
        for rule in model.rules:
            for condition in rule.conditions:
                if isinstance(condition, (TemplateSeenWithin, TextSeenWithin)):
                    self._observers.append((condition.key, condition))
        self._template_thresholds: dict[str, float] = {}
        for condition in (c for _, c in self._observers):
            if isinstance(condition, TemplateSeenWithin):
                current = self._template_thresholds.get(condition.name)
                self._template_thresholds[condition.name] = (
                    condition.min_score if current is None else min(current, condition.min_score)
                )

    # -- read-only state --------------------------------------------------- #
    @property
    def state(self) -> State:
        return self._state

    @property
    def state_age_s(self) -> float:
        return (self.clock() - self._entered_ns) / 1e9

    @property
    def progress_age_s(self) -> float:
        return (self.clock() - self._progress_ns) / 1e9

    def allowed_actions(self) -> tuple[str, ...]:
        return self.model.actions_for(self._state)

    def definition(self) -> StateDefinition:
        return self.model.definition(self._state)

    # -- classification ---------------------------------------------------- #
    def _prime_book(self, signals: Signals, now_ns: int) -> None:
        for key, condition in self._observers:
            if isinstance(condition, TemplateSeenWithin):
                present = signals.template_score(condition.name) >= condition.min_score
            elif isinstance(condition, TextSeenWithin):
                present = bool(find_text(signals.text, condition.needles, exact=condition.exact))
            else:  # pragma: no cover - defensive
                present = False
            self.book.note(key, present, now_ns)

    def classify(self, signals: Signals, now_ns: int | None = None) -> Detection:
        now = int(now_ns if now_ns is not None else self.clock())
        self._prime_book(signals, now)
        for rule in self.model.rules:
            if not rule.enabled:
                continue
            if rule.matches(signals, self.book, now):
                detection = Detection(
                    state=rule.state,
                    rule_id=rule.rule_id,
                    detail=rule.detail,
                    evidence=rule.evidence(signals, self.book, now),
                    at_ns=now,
                )
                return detection
        return Detection(
            state=State.UNKNOWN,
            rule_id="no_rule",
            detail="no state rule matched",
            evidence=(),
            at_ns=now,
        )

    # -- transitions ------------------------------------------------------- #
    def update(self, detection: Detection, now_ns: int | None = None) -> Transition | None:
        """Apply a detection. Returns a transition when the state changed."""
        now = int(now_ns if now_ns is not None else self.clock())
        if self._state is State.STOPPED:
            return None
        if self._state is State.PAUSED:
            return None
        # progress tracking: same state but different evidence == progress
        if detection.state is self._state:
            if self._last_detection is None or detection.key != self._last_detection.key:
                self._progress_ns = now
            self._last_detection = detection
            self._blocked = 0
            return None
        if not self.model.can_transition(self._state, detection.state):
            self._blocked += 1
            self._last_detection = detection
            if self._blocked >= max(1, self.model.blocked_switch_after):
                return self._force(State.UNKNOWN, f"blocked {self._blocked}x -> {detection.state.value}", now, detection)
            return None
        return self._force(detection.state, f"rule:{detection.rule_id}", now, detection)

    def _force(self, new_state: State, reason: str, now: int, detection: Detection | None) -> Transition:
        transition = Transition(
            from_state=self._state,
            to_state=new_state,
            reason=reason,
            at_ns=now,
            detection=detection,
        )
        self._state = new_state
        self._entered_ns = now
        self._progress_ns = now
        self._last_detection = detection
        self._blocked = 0
        self.transitions.append(transition)
        return transition

    def tick(self, now_ns: int | None = None) -> Transition | None:
        """Check the current state's no-progress timeout; escalate if expired."""
        now = int(now_ns if now_ns is not None else self.clock())
        if self._state in MANUAL_STATES:
            return None
        definition = self.model.definition(self._state)
        if definition.timeout_s is None:
            return None
        reference = self._entered_ns if definition.timeout_mode == "entry" else self._progress_ns
        if (now - reference) / 1e9 < definition.timeout_s:
            return None
        target = definition.on_timeout
        if target is self._state:  # never loop on yourself
            target = State.ERROR
        self.timeouts += 1
        return self._force(
            target,
            f"timeout {definition.timeout_s:g}s in {self._state.value} "
            f"(mode={definition.timeout_mode})",
            now,
            self._last_detection,
        )

    # -- manual control ---------------------------------------------------- #
    def pause(self) -> Transition:
        now = self.clock()
        if self._state is State.STOPPED:
            return Transition(self._state, self._state, "already stopped", now)
        self._paused_from = self._state
        return self._force(State.PAUSED, "pause", now, None)

    def resume(self) -> Transition:
        now = self.clock()
        if self._state is not State.PAUSED:
            return Transition(self._state, self._state, "not paused", now)
        return self._force(State.UNKNOWN, "resume -> re-observe", now, None)

    def stop(self, reason: str = "stop requested") -> Transition:
        now = self.clock()
        return self._force(State.STOPPED, reason, now, None)

    # -- reporting --------------------------------------------------------- #
    def snapshot(self) -> dict[str, Any]:
        definition = self.model.definition(self._state)
        return {
            "state": self._state.value,
            "state_age_s": round(self.state_age_s, 3),
            "progress_age_s": round(self.progress_age_s, 3),
            "actions": list(definition.actions),
            "timeout_s": definition.timeout_s,
            "timeouts": self.timeouts,
            "blocked": self._blocked,
            "transitions": [t.as_dict() for t in self.transitions[-20:]],
            "signals": self.book.snapshot(),
        }

    def refresh_presence(self, signals: Signals, now_ns: int | None = None) -> None:
        """Keep last-seen signals alive when a frame did not change.

        Change-driven control flow skips the analyser on unchanged frames, but
        the TTL book must not forget a signal that is still on screen.  Since
        the pixels did not change, the previous ``Signals`` still describe the
        screen - this re-notes them at ``now``.
        """
        now = int(now_ns if now_ns is not None else self.clock())
        self._prime_book(signals, now)

    def fail(self, reason: str, now_ns: int | None = None) -> Transition:
        """Force the ERROR state (safe stop) regardless of the transition table."""
        now = int(now_ns if now_ns is not None else self.clock())
        return self._force(State.ERROR, reason, now, self._last_detection)
