"""GameWorldState — what we believe about the account, and *why* we believe it.

The single most expensive failure mode in this project is treating a document
as if it were an observation.  ``docs/state.md`` says the pet is level 15; that
is a *historical* claim from a previous session, not today's state.  Every
field in this model therefore carries :class:`Evidence`, and the evidence kind
is the only thing that decides whether a goal node may be marked complete.

Design rules (enforced by the validators, not by convention):

* unknown is not ``False`` — a missing observation is ``None`` + ``UNKNOWN``
  evidence, never a negative answer;
* a value is only as trustworthy as its weakest provenance tag;
* nothing here imports the capture layer, the store, or any planner: this is
  plain data so it can be serialised, diffed and replayed.
"""

from __future__ import annotations

import copy
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Iterator, Mapping, MutableMapping


# --------------------------------------------------------------------------- #
# provenance
# --------------------------------------------------------------------------- #
class Source(str, Enum):
    """Where a fact came from.  Ordered from most to least trustworthy."""

    #: Observed on the live client in this process, now.
    LIVE_VERIFIED = "LIVE_VERIFIED"
    #: Derived from a recorded frame set (``data/replay``, ``artifacts/live``).
    REPLAY_VERIFIED = "REPLAY_VERIFIED"
    #: Read out of a project document; true at the time it was written, possibly
    #: false today.  Never sufficient to mark a goal node complete.
    HISTORICAL = "HISTORICAL"
    #: Concluded by a rule (e.g. "win counter went up, so the boss is down").
    INFERRED = "INFERRED"
    #: Nothing was observed.  ``value`` must be ``None``.
    UNKNOWN = "UNKNOWN"

    @property
    def rank(self) -> int:
        return {
            Source.LIVE_VERIFIED: 4,
            Source.REPLAY_VERIFIED: 3,
            Source.INFERRED: 2,
            Source.HISTORICAL: 1,
            Source.UNKNOWN: 0,
        }[self]

    @property
    def can_complete_goal(self) -> bool:
        """Only observations of *this account, now* may complete a goal node."""

        return self in (Source.LIVE_VERIFIED, Source.REPLAY_VERIFIED, Source.INFERRED)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Evidence:
    """Provenance for one fact."""

    source: Source
    observed_at: str = ""
    origin: str = ""
    confidence: float = 1.0
    detail: str = ""
    #: Screenshot / log / replay path backing this fact, when one exists.
    ref: str | None = None

    def __post_init__(self) -> None:
        if not self.observed_at:
            object.__setattr__(self, "observed_at", now_iso())
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError(f"confidence must be within [0, 1], got {self.confidence}")

    @classmethod
    def live(cls, origin: str, **kw: Any) -> "Evidence":
        return cls(Source.LIVE_VERIFIED, origin=origin, **kw)

    @classmethod
    def unknown(cls, why: str = "not observed") -> "Evidence":
        return cls(Source.UNKNOWN, origin=why, confidence=0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "observed_at": self.observed_at,
            "origin": self.origin,
            "confidence": round(float(self.confidence), 3),
            "detail": self.detail,
            "ref": self.ref,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Evidence":
        return cls(
            source=Source(raw.get("source", "UNKNOWN")),
            observed_at=str(raw.get("observed_at") or ""),
            origin=str(raw.get("origin") or ""),
            confidence=float(raw.get("confidence", 0.0)),
            detail=str(raw.get("detail") or ""),
            ref=raw.get("ref"),
        )


class Fact:
    """A value plus the evidence that justifies it."""

    __slots__ = ("value", "evidence")

    def __init__(self, value: Any = None, evidence: Evidence | None = None):
        self.value = value
        self.evidence = evidence or Evidence.unknown()

    # -- constructors ------------------------------------------------------ #
    @classmethod
    def unknown(cls, why: str = "not observed") -> "Fact":
        return cls(None, Evidence.unknown(why))

    @classmethod
    def observed(cls, value: Any, origin: str, source: Source = Source.LIVE_VERIFIED,
                 **kw: Any) -> "Fact":
        return cls(value, Evidence(source, origin=origin, **kw))

    @property
    def known(self) -> bool:
        return self.value is not None and self.evidence.source is not Source.UNKNOWN

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Fact({self.value!r}, {self.evidence.source.value})"

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "evidence": self.evidence.to_dict()}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Fact":
        return cls(raw.get("value"), Evidence.from_dict(raw.get("evidence") or {}))


# --------------------------------------------------------------------------- #
# domain records
# --------------------------------------------------------------------------- #
@dataclass
class PetState:
    name: str
    level: int | None = None
    hp: int | None = None
    hp_max: int | None = None
    pp: Mapping[str, tuple[int, int]] = field(default_factory=dict)
    skills: tuple[str, ...] = ()
    active: bool | None = None
    evidence: Evidence = field(default_factory=Evidence.unknown)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def hp_ratio(self) -> float | None:
        if self.hp is None or not self.hp_max:
            return None
        return float(self.hp) / float(self.hp_max)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level,
            "hp": self.hp,
            "hp_max": self.hp_max,
            "pp": {k: list(v) for k, v in self.pp.items()},
            "skills": list(self.skills),
            "active": self.active,
            "evidence": self.evidence.to_dict(),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PetState":
        return cls(
            name=str(raw.get("name") or ""),
            level=raw.get("level"),
            hp=raw.get("hp"),
            hp_max=raw.get("hp_max"),
            pp={k: tuple(v) for k, v in (raw.get("pp") or {}).items()},
            skills=tuple(raw.get("skills") or ()),
            active=raw.get("active"),
            evidence=Evidence.from_dict(raw.get("evidence") or {}),
            extra=dict(raw.get("extra") or {}),
        )


@dataclass
class ResourceState:
    name: str
    amount: float | None = None
    unit: str = ""
    evidence: Evidence = field(default_factory=Evidence.unknown)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "amount": self.amount,
            "unit": self.unit,
            "evidence": self.evidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ResourceState":
        return cls(
            name=str(raw.get("name") or ""),
            amount=raw.get("amount"),
            unit=str(raw.get("unit") or ""),
            evidence=Evidence.from_dict(raw.get("evidence") or {}),
        )


@dataclass
class SptEntry:
    """One row of the SPT 首领 panel (the 交易行 gate lives here)."""

    boss_id: str
    name: str = ""
    location: str = ""
    #: ``True``/``False`` only when the panel actually showed 已击败/未击败.
    defeated: bool | None = None
    reward_unlock: str = ""
    evidence: Evidence = field(default_factory=Evidence.unknown)

    def to_dict(self) -> dict[str, Any]:
        return {
            "boss_id": self.boss_id,
            "name": self.name,
            "location": self.location,
            "defeated": self.defeated,
            "reward_unlock": self.reward_unlock,
            "evidence": self.evidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SptEntry":
        return cls(
            boss_id=str(raw.get("boss_id") or ""),
            name=str(raw.get("name") or ""),
            location=str(raw.get("location") or ""),
            defeated=raw.get("defeated"),
            reward_unlock=str(raw.get("reward_unlock") or ""),
            evidence=Evidence.from_dict(raw.get("evidence") or {}),
        )


class Scene(str, Enum):
    LOGIN = "LOGIN"
    SERVER_LIST = "SERVER_LIST"
    WORLD = "WORLD"
    BATTLE = "BATTLE"
    RESULT = "RESULT"
    DIALOG = "DIALOG"
    PANEL = "PANEL"
    LOADING = "LOADING"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def parse(cls, raw: Any) -> "Scene":
        try:
            return cls(str(raw))
        except ValueError:
            return cls.UNKNOWN


# --------------------------------------------------------------------------- #
# the world
# --------------------------------------------------------------------------- #
@dataclass
class WindowState:
    window_id: int | None = None
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None
    scale: float = 2.0
    frontmost: bool | None = None
    title: str = ""
    evidence: Evidence = field(default_factory=Evidence.unknown)

    @property
    def pixel_size(self) -> tuple[int, int] | None:
        if self.w is None or self.h is None:
            return None
        return (int(round(self.w * self.scale)), int(round(self.h * self.scale)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "scale": self.scale,
            "frontmost": self.frontmost,
            "title": self.title,
            "evidence": self.evidence.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "WindowState":
        return cls(
            window_id=raw.get("window_id"),
            x=raw.get("x"),
            y=raw.get("y"),
            w=raw.get("w"),
            h=raw.get("h"),
            scale=float(raw.get("scale") or 2.0),
            frontmost=raw.get("frontmost"),
            title=str(raw.get("title") or ""),
            evidence=Evidence.from_dict(raw.get("evidence") or {}),
        )


#: Field path -> Fact.  Paths are dotted, e.g. ``session.scene``,
#: ``resources.赛尔豆``, ``pets.烈火猴.level``, ``spt.蘑菇怪.defeated``.
Facts = MutableMapping[str, Fact]


@dataclass
class GameWorldState:
    """A point-in-time, evidence-tagged snapshot of the account."""

    captured_at: str = field(default_factory=now_iso)
    run_id: str | None = None
    window: WindowState | None = None
    pets: dict[str, PetState] = field(default_factory=dict)
    active_pet: str | None = None
    resources: dict[str, ResourceState] = field(default_factory=dict)
    spt: dict[str, SptEntry] = field(default_factory=dict)
    unlocked: dict[str, Fact] = field(default_factory=dict)
    dailies: dict[str, Fact] = field(default_factory=dict)
    facts: Facts = field(default_factory=dict)
    unknowns: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    #: Structured observation payloads (scene, OCR text, template scores...).
    observations: dict[str, Any] = field(default_factory=dict)

    # -- session level shortcuts ------------------------------------------ #
    @property
    def scene(self) -> Fact:
        return self.facts.get("session.scene", Fact.unknown())

    @property
    def logged_in(self) -> Fact:
        return self.facts.get("session.logged_in", Fact.unknown())

    @property
    def in_battle(self) -> Fact:
        return self.facts.get("session.in_battle", Fact.unknown())

    def pet(self, name: str) -> PetState | None:
        return self.pets.get(name)

    @property
    def active(self) -> PetState | None:
        if self.active_pet and self.active_pet in self.pets:
            return self.pets[self.active_pet]
        for p in self.pets.values():
            if p.active:
                return p
        return None

    def resource(self, name: str) -> ResourceState | None:
        return self.resources.get(name)

    def amount(self, name: str) -> float | None:
        r = self.resources.get(name)
        return None if r is None else r.amount

    # -- fact plumbing ----------------------------------------------------- #
    def set_fact(self, path: str, value: Any, evidence: Evidence) -> Fact:
        fact = Fact(value, evidence)
        self.facts[path] = fact
        return fact

    def set_unknown(self, path: str, why: str = "not observed") -> Fact:
        return self.set_fact(path, None, Evidence.unknown(why))

    def fact(self, path: str, default: Any = None) -> Any:
        f = self.facts.get(path)
        return default if f is None else f.value

    def evidence_for(self, path: str) -> Evidence:
        f = self.facts.get(path)
        return f.evidence if f else Evidence.unknown()

    def mark_unknown(self, path: str) -> None:
        if path not in self.unknowns:
            self.unknowns.append(path)

    def iter_facts(self) -> Iterator[tuple[str, Fact]]:
        return iter(sorted(self.facts.items()))

    # -- merging ----------------------------------------------------------- #
    def merge(self, other: "GameWorldState") -> "GameWorldState":
        """Layer ``other`` over ``self`` without letting weak evidence win.

        Used by the observer to combine cheap template probes (scene) with
        expensive OCR probes (pets, resources): a later weaker source must not
        overwrite a stronger observation of the same path.
        """

        out = self.copy()
        for path, fact in other.facts.items():
            cur = out.facts.get(path)
            if cur is None or fact.evidence.source.rank >= cur.evidence.source.rank:
                out.facts[path] = fact
        out.pets.update({k: v for k, v in other.pets.items() if v.evidence.source.rank >= 1})
        out.resources.update(other.resources)
        out.spt.update(other.spt)
        out.unlocked.update(other.unlocked)
        out.dailies.update(other.dailies)
        out.unknowns = sorted(set(self.unknowns) | set(other.unknowns))
        out.notes.extend(n for n in other.notes if n not in out.notes)
        out.observations.update(other.observations)
        if other.window is not None:
            out.window = other.window
        if other.active_pet:
            out.active_pet = other.active_pet
        out.captured_at = other.captured_at or out.captured_at
        out.run_id = other.run_id or out.run_id
        return out

    def copy(self) -> "GameWorldState":
        return copy.deepcopy(self)

    # -- serialisation ----------------------------------------------------- #
    def to_dict(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at,
            "run_id": self.run_id,
            "window": None if self.window is None else self.window.to_dict(),
            "pets": {k: v.to_dict() for k, v in self.pets.items()},
            "active_pet": self.active_pet,
            "resources": {k: v.to_dict() for k, v in self.resources.items()},
            "spt": {k: v.to_dict() for k, v in self.spt.items()},
            "unlocked": {k: v.to_dict() for k, v in self.unlocked.items()},
            "dailies": {k: v.to_dict() for k, v in self.dailies.items()},
            "facts": {k: v.to_dict() for k, v in self.facts.items()},
            "unknowns": list(self.unknowns),
            "notes": list(self.notes),
            "observations": _jsonable(self.observations),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "GameWorldState":
        return cls(
            captured_at=str(raw.get("captured_at") or now_iso()),
            run_id=raw.get("run_id"),
            window=None if raw.get("window") is None else WindowState.from_dict(raw["window"]),
            pets={k: PetState.from_dict(v) for k, v in (raw.get("pets") or {}).items()},
            active_pet=raw.get("active_pet"),
            resources={k: ResourceState.from_dict(v) for k, v in (raw.get("resources") or {}).items()},
            spt={k: SptEntry.from_dict(v) for k, v in (raw.get("spt") or {}).items()},
            unlocked={k: Fact.from_dict(v) for k, v in (raw.get("unlocked") or {}).items()},
            dailies={k: Fact.from_dict(v) for k, v in (raw.get("dailies") or {}).items()},
            facts={k: Fact.from_dict(v) for k, v in (raw.get("facts") or {}).items()},
            unknowns=list(raw.get("unknowns") or ()),
            notes=list(raw.get("notes") or ()),
            observations=dict(raw.get("observations") or {}),
        )

    # -- views ------------------------------------------------------------- #
    def summary(self) -> dict[str, Any]:
        """Compact, planner-facing view (no per-field evidence noise)."""

        return {
            "captured_at": self.captured_at,
            "scene": self.fact("session.scene"),
            "scene_source": self.evidence_for("session.scene").source.value,
            "logged_in": self.fact("session.logged_in"),
            "frontmost": None if self.window is None else self.window.frontmost,
            "active_pet": None if self.active is None else {
                "name": self.active.name,
                "level": self.active.level,
                "hp": self.active.hp,
                "hp_max": self.active.hp_max,
                "skills": list(self.active.skills),
            },
            "pets": {p.name: {"level": p.level, "source": p.evidence.source.value}
                     for p in self.pets.values()},
            "resources": {r.name: r.amount for r in self.resources.values()},
            "spt_defeated": sorted(k for k, v in self.spt.items() if v.defeated is True),
            "spt_unknown": sorted(k for k, v in self.spt.items() if v.defeated is None),
            "unlocked": {k: v.value for k, v in self.unlocked.items()},
            "unknowns": list(self.unknowns),
            "notes": list(self.notes),
            # battle-UI progress signal: how much XP is still missing for the
            # next level.  This is what makes "a level is close" observable
            # without opening the pet panel.
            "xp_to_next": self.observations.get("pet_xp_to_next"),
        }

    def describe(self) -> str:  # pragma: no cover - human/debug output
        s = self.summary()
        lines = [f"world @ {s['captured_at']} scene={s['scene']} ({s['scene_source']})"]
        if s["active_pet"]:
            p = s["active_pet"]
            lines.append(f"  pet {p['name']} Lv{p['level']} hp {p['hp']}/{p['hp_max']}")
        if s["spt_defeated"]:
            lines.append(f"  spt defeated: {', '.join(s['spt_defeated'])}")
        if s["resources"]:
            lines.append("  resources: " + ", ".join(f"{k}={v}" for k, v in s["resources"].items()))
        if s["unknowns"]:
            lines.append(f"  unknown: {', '.join(s['unknowns'])}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _jsonable(value: Any) -> Any:
    """Best-effort conversion so observations can always be persisted."""

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "tolist"):
        try:
            return _jsonable(value.tolist())
        except Exception:  # noqa: BLE001
            pass
    if isinstance(value, Enum):
        return value.value
    return repr(value)


def empty_world(run_id: str | None = None) -> GameWorldState:
    w = GameWorldState(run_id=run_id)
    for path in ("session.scene", "session.logged_in", "session.in_battle", "session.server"):
        w.set_unknown(path)
    return w


def utc_stamp() -> str:
    """Session stamp used in file names (``YYYYmmddTHHMMSSZ``)."""

    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


#: Convenience re-export so callers can build a modified copy tersely.
def with_fact(world: GameWorldState, path: str, value: Any, origin: str,
              source: Source = Source.LIVE_VERIFIED, **kw: Any) -> GameWorldState:
    world = world.copy()
    world.set_fact(path, value, Evidence(source, origin=origin, **kw))
    return world


__all__ = [
    "Source",
    "Evidence",
    "Fact",
    "PetState",
    "ResourceState",
    "SptEntry",
    "Scene",
    "WindowState",
    "GameWorldState",
    "empty_world",
    "with_fact",
    "now_iso",
    "utc_stamp",
]
