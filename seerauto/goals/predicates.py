"""Predicate registry for the goal graph.

A goal node may only be *completed* by evidence about this account, read out of
:class:`seerauto.world.model.GameWorldState`.  This module is where that rule is
enforced in code, so the planner cannot "decide" that a node is done.

Contract
--------
Every predicate is ``(world: GameWorldState, args: Mapping[str, Any]) ->
PredicateResult`` where ``PredicateResult = tuple[bool | None, str]``:

* ``True``  — the evidence proves the predicate holds *now*;
* ``False`` — the evidence proves it does not hold *now*;
* ``None``  — the world does not contain enough evidence to decide.

``None`` is **not** ``False``.  "Nobody looked" must never be mistaken for
"it did not happen" — :func:`evaluate` callers (the graph) treat ``None`` as
"unknown", and the graph's ``status_of`` reports such a node as still
incomplete rather than blocked.

Evidence gate
-------------
``Source.HISTORICAL`` (a claim copied out of a document) and ``Source.UNKNOWN``
may never satisfy a predicate.  A pet that ``docs/state.md`` says is level 15
returns ``(None, "pet 烈火猴 level UNKNOWN (HISTORICAL evidence only)")``: the
document is real evidence *about the document*, not about today's account.

World conventions used by the predicates
----------------------------------------
The world model is generic (``facts`` / ``unlocked`` / ``observations``), so the
graph fixes the key names here, once:

===========================  ===============================================
``world.pets[<name>]``       :class:`PetState` (``level``, ``evidence``)
``world.resources[<name>]``  :class:`ResourceState` (``amount``, ``evidence``)
``world.spt[<boss_id>]``     :class:`SptEntry` (``defeated``, ``name``)
``world.unlocked[<key>]``    ``Fact(bool)`` — e.g. ``market``, ``puni.seal.1``
``world.facts["counters.<name>"]``  ``Fact(int)`` — monotonic counters
``world.facts["panel.spt_open"]``   ``Fact(bool)`` — SPT panel visible
``world.observations["capabilities"][<name>]``  dict — a recorded demo, e.g.
                             ``{"demonstrated": True, "source": "LIVE_VERIFIED"}``

Unlock keys used by ``puni.yaml`` are listed in that file's ``conventions``
block; they are deliberately explicit rather than inferred, because "the market
is open" must be something somebody *saw*.

Trap
----
:func:`node_completed_before` exists only so the validator can *recognise* the
temptation ("this node is done because the planner said it ran it") and reject
it: it raises :class:`GraphError` whenever it is evaluated.  ``completed`` is
scheduler bookkeeping, never a world fact.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ..world.model import Evidence, GameWorldState, Scene, Source

#: ``(holds, why)`` — ``None`` means "cannot decide".
PredicateResult = tuple[bool | None, str]
Predicate = Callable[[GameWorldState, Mapping[str, Any]], PredicateResult]


class GraphError(RuntimeError):
    """A predicate is undefined, misused, or unanswerable by design."""


#: Alias kept for callers that prefer the narrower name.
PredicateError = GraphError


# --------------------------------------------------------------------------- #
# evidence plumbing
# --------------------------------------------------------------------------- #
def is_trusted(evidence: Evidence) -> bool:
    """True when the evidence may complete a goal node (observed *this* account).

    ``HISTORICAL`` is excluded on purpose: a document says what was true when it
    was written, and the whole point of the ``Source`` enum is that it cannot
    stand in for an observation.
    """

    return evidence.source.can_complete_goal


def _src(evidence: Evidence) -> str:
    return evidence.source.value


def _historical_or_unknown(evidence: Evidence) -> bool:
    return evidence.source in (Source.HISTORICAL, Source.UNKNOWN)


def _unknown_reason(subject: str, evidence: Evidence) -> str:
    if evidence.source is Source.HISTORICAL:
        return f"{subject} level UNKNOWN (HISTORICAL evidence only)"
    return f"{subject} UNKNOWN ({_src(evidence)} evidence)"


def _need(args: Mapping[str, Any], key: str) -> Any:
    if key not in args:
        raise GraphError(
            f"predicate argument {key!r} is required; got {sorted(args)}")
    return args[key]


def _num(args: Mapping[str, Any], key: str) -> float:
    value = _need(args, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GraphError(f"predicate argument {key!r} must be a number, got {value!r}")
    return float(value)


# --------------------------------------------------------------------------- #
# completion predicates (account state)
# --------------------------------------------------------------------------- #
def pet_level_at_least(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """The pet line reached at least ``level``, observed now.

    ``aliases`` lets one node name a line that is renamed by evolution
    (``烈火猴`` -> ``烈焰猩猩``): the evolution itself must not make a level goal
    unverifiable.  A ``HISTORICAL`` level is reported as ``UNKNOWN`` and returns
    ``None`` — ``docs/state.md`` saying "≈15" is not today's level.
    """

    name = str(_need(args, "pet"))
    need = _num(args, "level")
    aliases = tuple(str(a) for a in (args.get("aliases") or ()))
    saw_below: list[str] = []
    unknown: list[str] = []
    for candidate in (name, *aliases):
        pet = world.pet(candidate)
        if pet is None:
            unknown.append(f"pet {candidate} level UNKNOWN (not observed)")
            continue
        if pet.level is None:
            unknown.append(f"pet {candidate} level UNKNOWN ({_src(pet.evidence)} evidence)")
            continue
        if not is_trusted(pet.evidence):
            unknown.append(_unknown_reason(f"pet {candidate}", pet.evidence))
            continue
        if float(pet.level) >= need:
            return True, (f"pet {candidate} level {int(pet.level)} >= {int(need)} "
                          f"({_src(pet.evidence)})")
        saw_below.append(f"pet {candidate} level {int(pet.level)} < {int(need)} "
                         f"({_src(pet.evidence)})")
    if saw_below:
        return False, "; ".join(saw_below + unknown)
    return None, "; ".join(unknown) or f"pet {name} not observed"


def pet_exists(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """The pet is in the account, observed now.

    Absence from the world model is *not* proof of absence: a pet nobody looked
    for returns ``None``.
    """

    name = str(_need(args, "pet"))
    pet = world.pet(name)
    if pet is None:
        return None, f"pet {name} not observed (absence of an observation is not absence of the pet)"
    if not is_trusted(pet.evidence):
        return None, f"pet {name} UNKNOWN ({_src(pet.evidence)} evidence only)"
    return True, f"pet {name} observed ({_src(pet.evidence)})"


def spt_defeated(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """One SPT boss row says ``已击败`` (matched by boss id or by panel name)."""

    key = str(_need(args, "boss"))
    entry = _spt_entry(world, key)
    if entry is None:
        return None, f"spt {key} not observed (panel not read for this boss)"
    if entry.defeated is None:
        return None, f"spt {key} defeated UNKNOWN ({_src(entry.evidence)} evidence)"
    if not is_trusted(entry.evidence):
        return None, f"spt {key} defeated={entry.defeated} ({_src(entry.evidence)} evidence only)"
    if entry.defeated:
        return True, f"spt {key} defeated ({_src(entry.evidence)})"
    return False, f"spt {key} not defeated ({_src(entry.evidence)})"


def spt_defeated_count_at_least(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """At least ``count`` SPT bosses are defeated, counted from panel rows."""

    need = _num(args, "count")
    trusted = 0
    undecided = 0
    for entry in world.spt.values():
        if entry.defeated is None or not is_trusted(entry.evidence):
            undecided += 1
        elif entry.defeated:
            trusted += 1
    if trusted >= need:
        return True, f"{trusted} SPT bosses defeated (>= {int(need)})"
    if trusted + undecided < need:
        return False, f"only {trusted} SPT bosses defeated (< {int(need)}); no undecided rows left"
    return None, (f"{trusted} SPT bosses defeated of {int(need)} needed; "
                  f"{undecided} row(s) undecided")


def resource_at_least(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """A resource balance (赛尔豆 / 金豆 / 积累经验 ...) reached ``amount``."""

    name = str(_need(args, "resource"))
    need = _num(args, "amount")
    res = world.resource(name)
    if res is None or res.amount is None:
        return None, f"resource {name} not observed"
    if not is_trusted(res.evidence):
        return None, (f"resource {name} amount UNKNOWN "
                      f"({_src(res.evidence)} evidence only; last written value {res.amount})")
    if float(res.amount) >= need:
        return True, f"resource {name}={int(res.amount)} >= {int(need)} ({_src(res.evidence)})"
    return False, f"resource {name}={int(res.amount)} < {int(need)} ({_src(res.evidence)})"


def feature_unlocked(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """A named unlock flag is true, observed now (``world.unlocked[key]``).

    The flag is *written by an observer* (e.g. "the 谱尼 seal panel shows 已通关"),
    never by the skill that claims to have done the work.
    """

    key = str(_need(args, "feature"))
    fact = world.unlocked.get(key)
    if fact is None:
        # be forgiving about a fact stored under the generic fact table
        fact = world.facts.get(f"unlocked.{key}")
    if fact is None:
        return None, f"feature {key!r} never observed (no world.unlocked entry)"
    if not is_trusted(fact.evidence):
        return None, f"feature {key!r} UNKNOWN ({_src(fact.evidence)} evidence only)"
    if bool(fact.value):
        return True, f"feature {key!r} unlocked ({_src(fact.evidence)})"
    return False, f"feature {key!r} not unlocked ({_src(fact.evidence)})"


def counter_at_least(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """A monotonic counter (``world.facts["counters.<name>"]``) reached ``at_least``.

    Only ever set *after* an observed effect (a floor cleared, a level gained);
    it is not a place to record intentions.
    """

    name = str(_need(args, "counter"))
    need = _num(args, "at_least")
    fact = world.facts.get(f"counters.{name}") or world.facts.get(name)
    if fact is None or fact.value is None:
        return None, f"counter {name!r} not observed"
    if not is_trusted(fact.evidence):
        return None, f"counter {name!r} UNKNOWN ({_src(fact.evidence)} evidence only)"
    if float(fact.value) >= need:
        return True, f"counter {name}={int(fact.value)} >= {int(need)} ({_src(fact.evidence)})"
    return False, f"counter {name}={int(fact.value)} < {int(need)} ({_src(fact.evidence)})"


def capability_demonstrated(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """A capability was demonstrated end-to-end in a recorded run.

    This backs ``completion.type: capability_demonstrated`` nodes (cross-cutting
    skills such as "read the battle log").  It reads
    ``world.observations["capabilities"][<name>]`` and accepts either a bool or
    ``{"demonstrated": bool, "source": "<Source value>", "ref": "..."}``.

    Such nodes deliberately never auto-complete: the graph only lets
    ``verified_account_state`` complete a node, so a human/tool has to add the
    id to ``completed`` after reading the demo.
    """

    name = str(_need(args, "capability"))
    book = (world.observations or {}).get("capabilities") or {}
    if name not in book:
        return None, f"capability {name!r} never demonstrated in a recorded run"
    entry = book[name]
    if isinstance(entry, bool):
        return (True, f"capability {name!r} demonstrated") if entry else (
            False, f"capability {name!r} demo failed")
    if not isinstance(entry, Mapping):
        raise GraphError(
            f"observations['capabilities'][{name!r}] must be bool or mapping, "
            f"got {type(entry).__name__}")
    demonstrated = bool(entry.get("demonstrated"))
    raw_source = entry.get("source")
    if raw_source is None and isinstance(entry.get("evidence"), Mapping):
        raw_source = entry["evidence"].get("source")
    try:
        source = Source(str(raw_source))
    except ValueError:
        source = Source.UNKNOWN
    if not demonstrated:
        return False, f"capability {name!r} demo recorded as failed"
    if not source.can_complete_goal:
        return None, (f"capability {name!r} demonstrated ({source.value} evidence only)")
    ref = entry.get("ref")
    return True, f"capability {name!r} demonstrated ({source.value}{f'; {ref}' if ref else ''})"


def node_completed_before(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """Deliberately unusable stub — raises on every evaluation.

    ``completed`` is scheduler bookkeeping, not an account observation.  A node
    whose completion predicate is "the scheduler says it ran" could be completed
    by a model asserting success, which is exactly what this project forbids.
    The predicate is registered so the validator can name the mistake precisely.
    """

    node = args.get("node") if isinstance(args, Mapping) else None
    raise GraphError(
        "node_completed_before is a no-op stub and must never be used as a "
        f"completion predicate (node={node!r}); a goal node is completed by "
        "world evidence, not by the planner's own bookkeeping")


# --------------------------------------------------------------------------- #
# state predicates (prerequisites: "may I even try this now?")
# --------------------------------------------------------------------------- #
def active_pet_level_known(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """The active pet's level was observed this session."""

    pet = world.active
    if pet is None:
        return None, "no active pet observed (world.active_pet / pets[*].active unset)"
    if pet.level is None:
        return None, f"active pet {pet.name} level UNKNOWN ({_src(pet.evidence)} evidence)"
    if not is_trusted(pet.evidence):
        return None, _unknown_reason(f"active pet {pet.name}", pet.evidence)
    return True, f"active pet {pet.name} level {pet.level} ({_src(pet.evidence)})"


def scene_is(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """The session scene equals ``scene``; with no argument, "scene is known"."""

    want = args.get("scene")
    fact = world.scene
    if fact.value is None or not is_trusted(fact.evidence):
        return None, f"scene UNKNOWN ({_src(fact.evidence)} evidence)"
    if want is None:
        return True, f"scene {fact.value} observed ({_src(fact.evidence)})"
    if str(fact.value) == str(want):
        return True, f"scene is {want} ({_src(fact.evidence)})"
    return False, f"scene is {fact.value}, expected {want} ({_src(fact.evidence)})"


def logged_in(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """The account is logged in, observed this session."""

    fact = world.logged_in
    if fact.value is None or not is_trusted(fact.evidence):
        return None, f"logged_in UNKNOWN ({_src(fact.evidence)} evidence)"
    if fact.value:
        return True, f"logged in ({_src(fact.evidence)})"
    return False, f"not logged in ({_src(fact.evidence)})"


def spt_panel_open(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """The SPT 首领 panel is on screen (the 交易行 gate text lives there)."""

    fact = world.facts.get("panel.spt_open")
    if fact is None:
        fact = world.facts.get("spt.panel_open")
    if fact is None:
        return None, "SPT panel state never observed"
    if fact.value is None or not is_trusted(fact.evidence):
        return None, f"SPT panel state UNKNOWN ({_src(fact.evidence)} evidence)"
    if fact.value:
        return True, f"SPT panel open ({_src(fact.evidence)})"
    return False, f"SPT panel closed ({_src(fact.evidence)})"


def battle_capable(world: GameWorldState, args: Mapping[str, Any]) -> PredicateResult:
    """The Round-4 battle loop can run from here (in battle, or battle entered).

    Only ``battle.repeat`` is real-verified, and only for the scene the loop was
    proven in; anything weaker than a live-verified signal returns ``None``.
    """

    explicit = world.facts.get("battle.capable")
    if explicit is not None:
        if explicit.value is None or not is_trusted(explicit.evidence):
            return None, f"battle.capable UNKNOWN ({_src(explicit.evidence)} evidence)"
        return (bool(explicit.value),
                f"battle.capable={bool(explicit.value)} ({_src(explicit.evidence)})")
    battle = world.in_battle
    if battle.value is True and is_trusted(battle.evidence):
        return True, f"in battle ({_src(battle.evidence)})"
    scene = world.scene
    if scene.value == Scene.BATTLE.value and is_trusted(scene.evidence):
        return True, f"battle scene ({_src(scene.evidence)})"
    login = world.logged_in
    if login.value is False and is_trusted(login.evidence):
        return False, f"not logged in ({_src(login.evidence)})"
    # Standing in the world with a battle-ready pet IS battle capability: the farm loop
    # has entered hundreds of fights from exactly this state (2026-11-04 night).  Only
    # accepting "already in battle" made every pre-battle node unexecutable.
    ready = world.facts.get("session.battle_ready")
    if (scene.value == Scene.WORLD.value and is_trusted(scene.evidence)
            and ready is not None and ready.value is True and is_trusted(ready.evidence)):
        return True, (f"in the world with a battle-ready pet "
                      f"({_src(scene.evidence)}/{_src(ready.evidence)})")
    return None, ("battle capability not observable: session.in_battle / session.scene "
                  "are not live-verified")


# --------------------------------------------------------------------------- #
# registries
# --------------------------------------------------------------------------- #
#: Completion predicates: they answer "is this node's outcome true *now*?".
PREDICATES: dict[str, Predicate] = {
    "pet_level_at_least": pet_level_at_least,
    "pet_exists": pet_exists,
    "spt_defeated": spt_defeated,
    "spt_defeated_count_at_least": spt_defeated_count_at_least,
    "resource_at_least": resource_at_least,
    "feature_unlocked": feature_unlocked,
    "counter_at_least": counter_at_least,
    "capability_demonstrated": capability_demonstrated,
    "node_completed_before": node_completed_before,
}

#: State predicates: they answer "may this node be attempted at all?".
STATE_PREDICATES: dict[str, Predicate] = {
    "active_pet_level_known": active_pet_level_known,
    "scene_is": scene_is,
    "logged_in": logged_in,
    "spt_panel_open": spt_panel_open,
    "battle_capable": battle_capable,
}

#: ``state.<name>:<value>`` prerequisite syntax -> predicate argument name.
STATE_ARG_KEYS: dict[str, str] = {
    "scene_is": "scene",
}


def _spt_entry(world: GameWorldState, key: str):
    entry = world.spt.get(key)
    if entry is not None:
        return entry
    for candidate in world.spt.values():
        if candidate.name == key or candidate.boss_id == key:
            return candidate
    return None


def resolve(name: str) -> Predicate:
    """Look a predicate up by bare name or ``state.<name>``/``predicate.<name>``."""

    if not isinstance(name, str) or not name.strip():
        raise GraphError(f"predicate name must be a non-empty string, got {name!r}")
    key = name.strip()
    if key.startswith("state."):
        key = key[len("state."):]
        table = STATE_PREDICATES
    elif key.startswith("predicate."):
        key = key[len("predicate."):]
        table = PREDICATES
    else:
        table = PREDICATES
    fn = table.get(key)
    if fn is None:
        known = sorted(set(PREDICATES) | set(STATE_PREDICATES))
        raise GraphError(f"unknown predicate {name!r}; registered: {known}")
    return fn


def evaluate(predicate: str, world: GameWorldState,
             args: Mapping[str, Any] | None = None) -> PredicateResult:
    """Evaluate ``predicate`` against ``world`` and enforce the result contract."""

    if not isinstance(args, Mapping) and args is not None:
        raise GraphError(f"predicate args must be a mapping, got {type(args).__name__}")
    fn = resolve(predicate)
    argv: dict[str, Any] = dict(args or {})
    try:
        result = fn(world, argv)
    except GraphError:
        raise
    except TypeError as exc:  # missing/extra keyword in a predicate body
        raise GraphError(f"predicate {predicate!r} rejected args {sorted(argv)}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - a broken predicate must be loud
        raise GraphError(f"predicate {predicate!r} crashed: {type(exc).__name__}: {exc}") from exc
    if not (isinstance(result, tuple) and len(result) == 2):
        raise GraphError(
            f"predicate {predicate!r} returned {result!r}; expected (bool | None, str)")
    holds, why = result
    if holds is not None and not isinstance(holds, bool):
        raise GraphError(
            f"predicate {predicate!r} returned {holds!r}; expected bool or None "
            "(None = undecidable, never a silent False)")
    if not isinstance(why, str):
        raise GraphError(f"predicate {predicate!r} must explain itself with a str, got {why!r}")
    return holds, why


def names() -> list[str]:
    """All registered predicate names (completion + state), sorted."""

    return sorted(set(PREDICATES) | set(STATE_PREDICATES))


__all__ = [
    "PredicateResult",
    "Predicate",
    "GraphError",
    "PredicateError",
    "PREDICATES",
    "STATE_PREDICATES",
    "STATE_ARG_KEYS",
    "is_trusted",
    "evaluate",
    "resolve",
    "names",
    "pet_level_at_least",
    "pet_exists",
    "spt_defeated",
    "spt_defeated_count_at_least",
    "resource_at_least",
    "feature_unlocked",
    "counter_at_least",
    "capability_demonstrated",
    "node_completed_before",
    "active_pet_level_known",
    "scene_is",
    "logged_in",
    "spt_panel_open",
    "battle_capable",
]
