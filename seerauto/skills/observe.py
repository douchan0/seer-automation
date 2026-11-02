"""Observe skills -- reading the game, and recording *why* we believe it.

This is the reading half of the semantic layer.  Every skill here:

* is ``kind=OBSERVE``, ``requires_input=False``, ``max_actions=0`` -- it never
  moves the mouse or presses a key, so it is safe while a human is driving;
* takes the world from :class:`~seerauto.skills.base.SkillContext`, writes into a
  **copy** and returns it (``ctx.world`` is never mutated, so a failed run cannot
  corrupt the caller's state);
* does all I/O through ``ctx.session`` (the duck-typed ``LiveSession``), which is
  why the whole module is unit-testable with a fake session;
* attaches one :class:`~seerauto.world.model.Evidence` per fact, whose ``origin``
  names the exact probe that produced it -- ``ocr:我方【烈火猴】``,
  ``template:live_fight_btn@ROI_FIGHT``, ``window:seerdx.win`` -- and whose
  ``source`` says how much that probe is worth;
* never invents a value: a field the probe did not see is either left as an
  explicit ``UNKNOWN`` fact (first look) or flagged in ``world.unknowns`` while its
  previous value keeps the evidence that dates it (sticky knowledge, see
  :func:`_record_unknown`).

What "verified" means per skill
-------------------------------
Round 4 ran on the live client (``docs/progress.md``): the scene template table
and the OCR reads of ``当前回合：N`` / ``我方【…】`` / ``对方【…】`` / ``89/92`` /
``使用次数`` / the XP payout are real.  Everything else is labelled honestly:

===============  ========  =====================================================
skill            verified  why
===============  ========  =====================================================
game.get_status  real      scene templates + window geometry ran live in Round 4
game.observe     real      composes only Round-4-verified probes
game.get_current_scene real scene templates
game.inspect_pets     replay    pet *panel* shape only exists in a 2026-10-19
                               screenshot; the battle-HUD path is real
game.inspect_resources replay   currency pairing comes from a recorded shop frame
game.inspect_spt_progress unverified  no SPT panel screenshot exists in the repo
===============  ========  =====================================================

``Source`` per field is independent of that table and is set where the fact is
recorded: a replay-derived shape still produces ``REPLAY_VERIFIED`` data when it
is actually read, and an unchecked guess is never recorded at all (``None`` /
``UNKNOWN`` instead).

Two caveats, stated so nobody has to infer them:

* ``real`` on ``game.observe`` means "every primitive it composes was verified
  live in Round 4", **not** "this module ran on the live client in Round 4" --
  the composition itself is covered by the replay fixtures in
  ``tests/test_observe_skills.py`` until the live lane exercises it;
* no observe skill ever loops, so :class:`_ObserveSkill` enforces the declared
  ``max_duration_s`` after the fact (it reports the overrun and asks for a replan
  instead of silently returning a stale-looking answer).
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from ..world.model import (
    Evidence,
    GameWorldState,
    PetState,
    ResourceState,
    Scene,
    Source,
    SptEntry,
    WindowState,
)
from . import parsers as P
from .base import (Skill, SkillContext, SkillKind, SkillResult, SkillSpec, Status,
                   skill)

#: OCR confidence floor handed to the session; Round 4's escalation path used 0.3.
OCR_MIN_CONF = 0.3
#: Window scale of this host (``docs/state.md``: 1200x800 pt -> 2400x1600 px).
DEFAULT_SCALE = 2.0

#: The three templates that decide the scene (``seerauto/live.py::LiveSession.scene``).
SCENE_TEMPLATE_ORIGIN = (
    "template:live_fight_btn@ROI_FIGHT"
    "|template:live_victory_word@ROI_VICTORY"
    "|template:live_confirm_btn|live_confirm_xp@ROI_DIALOG"
)

#: Scenes in which the battle HUD (turn / HP / PP) is expected on screen.
_BATTLE_SCENES = frozenset({Scene.BATTLE, Scene.RESULT})
#: Scenes in which a payout line may be on screen (victory panel / XP dialog).
_REWARD_SCENES = frozenset({Scene.RESULT, Scene.DIALOG})

#: Confidence policy.  There are only two values on purpose: a number nobody can
#: measure is worse than a coarse, documented split.
CONF_SHAPE_VERIFIED = 1.0
#: Facts whose *shape* had to be repaired or has no live sample (the LV colon
#: glyph read as ``8``; SPT card layout; the pet-detail panel).
CONF_SHAPE_REPAIRED = 0.5

_PANEL_NAME = re.compile(r"名字\s*[：:]\s*([^\s，。]{1,12})")
_SPT_STATE_WORD = re.compile(r"已击败|未击败")


class _ObserveSkill(Skill):
    """Base for the skills below: one shot, and the declared time budget is real.

    No observe skill loops or polls (an OCR look is the expensive step and this
    project never puts one in a polling path), so the worst case is one scene
    probe plus one OCR look.  If that still overruns ``max_duration_s`` the
    observation is returned -- the pixels were real -- but the result says so and
    asks the caller to replan instead of trusting a stale answer.
    """

    def __call__(self, ctx: SkillContext, **kwargs: Any) -> SkillResult:  # noqa: D401
        result = super().__call__(ctx, **kwargs)
        if result.status is Status.PASS and result.elapsed_s > self.spec.max_duration_s:
            result.reason = (f"observation took {result.elapsed_s:.2f}s, over its "
                             f"{self.spec.max_duration_s:.0f}s budget")
            result.escalation = "replan"
        return result


# --------------------------------------------------------------------------- #
# session plumbing (all I/O goes through here, so a fake session is enough)
# --------------------------------------------------------------------------- #
def _as_scene(value: Any) -> Scene:
    """``Scene`` from an enum, its value, or anything else (``UNKNOWN``)."""

    if isinstance(value, Scene):
        return value
    return Scene.parse(value)


def _probe_scene(session: Any, previous: Scene | None = None
                 ) -> tuple[Scene, dict[str, float], dict[str, Any]]:
    """Run the template scene classifier and normalise its output.

    ``LiveSession.scene`` returns a ``SceneProbe``; a fake session may return a
    plain dict.  Both are accepted so tests do not have to import ``live``
    (which drags cv2 into the observation tests).
    """

    try:
        probe = session.scene(previous=previous)
    except TypeError:
        probe = session.scene()

    scene: Any = _as_scene(getattr(probe, "scene", None))
    scores = getattr(probe, "scores", None)
    points = getattr(probe, "points", None)
    if isinstance(probe, Mapping):
        scene = _as_scene(probe.get("scene"))
        scores = probe.get("scores")
        points = probe.get("points")
    return (
        _as_scene(scene),
        {str(k): float(v) for k, v in (scores or {}).items()},
        dict(points or {}),
    )


def _describe(session: Any) -> dict[str, Any]:
    fn = getattr(session, "describe", None)
    if not callable(fn):
        return {}
    try:
        return dict(fn() or {})
    except Exception:  # noqa: BLE001 - diagnostics must never break observation
        return {}


def _frontmost(session: Any) -> bool | None:
    fn = getattr(session, "ensure_frontmost", None)
    if not callable(fn):
        return None
    try:
        value = fn()
    except Exception:  # noqa: BLE001 - "cannot check" is not "not frontmost"
        return None
    return None if value is None else bool(value)


def _window_state(session: Any, desc: Mapping[str, Any]) -> WindowState:
    raw = dict(desc.get("window") or {})
    if not raw:
        raw = dict(getattr(session, "window_raw", None) or {})
    scale = float(desc.get("scale") or raw.get("scale") or DEFAULT_SCALE)

    def _num(key: str) -> float | None:
        value = raw.get(key)
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    width, height = _num("w"), _num("h")
    return WindowState(
        window_id=raw.get("id"),
        x=_num("x"),
        y=_num("y"),
        w=width,
        h=height,
        scale=scale,
        frontmost=_frontmost(session),
        title=str(raw.get("title") or raw.get("name") or ""),
        evidence=Evidence(
            Source.LIVE_VERIFIED,
            origin="window:seerdx.win",
            detail=(f"w={width} h={height} scale={scale}" + (
                "" if width is None or height is None
                else f" -> capture {int(width * scale)}x{int(height * scale)}")),
        ),
    )


def _ocr_items(session: Any, enabled: bool = True) -> list[dict[str, Any]]:
    """One OCR look.  Returns ``[]`` when disabled, never raises on ``None``."""

    if not enabled or session is None:
        return []
    items = session.ocr(min_conf=OCR_MIN_CONF)
    return [dict(item) for item in (items or [])]


def _texts(items: Sequence[Mapping[str, Any]]) -> list[str]:
    return [P.text_of(item) for item in items if P.text_of(item)]


def _first_text(items: Sequence[Mapping[str, Any]], needle: str) -> str:
    """Raw OCR line containing ``needle`` (used to build an honest ``origin``)."""

    found = _match_text(items, needle)
    return needle if found is None else found


def _match_text(items: Sequence[Mapping[str, Any]], needle: str) -> str | None:
    for item in items:
        text = P.text_of(item)
        if needle in text:
            return text
    return None


# --------------------------------------------------------------------------- #
# world helpers
# --------------------------------------------------------------------------- #
def _in_battle(scene: Scene) -> bool | None:
    """Battle state from the scene, or ``None`` when the scene does not decide it."""

    if scene in _BATTLE_SCENES:
        return True
    if scene is Scene.WORLD:
        return False
    return None


def _record(world: GameWorldState, path: str, value: Any, evidence: Evidence,
            collected: list[Evidence]) -> None:
    world.set_fact(path, value, evidence)
    collected.append(evidence)


def _record_unknown(world: GameWorldState, path: str, why: str) -> None:
    """Explicit "looked and did not see it" -- never a ``False``/``0`` stand-in.

    Sticky-knowledge rule (matches ``seerauto.runner.runner.GoalRunner.observe``):
    a field that already carries an observed value is **not** wiped just because
    this frame did not show it -- the old value keeps its own evidence (whose
    ``observed_at`` dates it) and the path is added to ``world.unknowns`` so the
    planner can see it was not re-observed.  Only a field with no value at all
    becomes an explicit ``UNKNOWN`` fact.
    """

    existing = world.facts.get(path)
    if existing is not None and existing.value is not None:
        world.mark_unknown(path)
        return
    world.set_unknown(path, why)


def _apply_scene(world: GameWorldState, scene: Scene, scores: Mapping[str, float],
                 collected: list[Evidence]) -> Evidence:
    evidence = Evidence(
        Source.LIVE_VERIFIED,
        origin=SCENE_TEMPLATE_ORIGIN,
        detail="scores " + ", ".join(f"{k}={v:.3f}" for k, v in sorted(scores.items())),
    )
    _record(world, "session.scene", scene.value, evidence, collected)
    in_battle = _in_battle(scene)
    if in_battle is None:
        _record_unknown(world, "session.in_battle",
                        f"scene {scene.value} does not decide battle state")
    else:
        inference = Evidence(
            Source.INFERRED,
            origin=f"rule:scene=={scene.value}->in_battle={in_battle}",
            detail="victory panel is drawn over the battle frame (Round 4 bug #1)",
        )
        _record(world, "session.in_battle", in_battle, inference, collected)
    return evidence


def _pet_evidence(origin: str, detail: str = "",
                  confidence: float = CONF_SHAPE_VERIFIED) -> Evidence:
    return Evidence(Source.LIVE_VERIFIED, origin=origin, detail=detail,
                    confidence=confidence)


def _apply_pet(world: GameWorldState, items: Sequence[Mapping[str, Any]],
               *, name: str, mode: str, collected: list[Evidence]) -> tuple[PetState, dict[str, Evidence]]:
    """Build/merge one :class:`PetState` plus the per-field evidence behind it."""

    # a detail panel prints exactly one level; the battle HUD prints both sides,
    # so only the HUD needs the side filter
    level = P.parse_level(items) if mode == "panel" else P.parse_level(items, side="ally")
    hp = P.parse_hp(items)
    pp = P.parse_pp(items)
    ally_line = _match_text(items, "我方")
    name_evidence = _pet_evidence(
        f"ocr:{ally_line}" if ally_line else f"ocr:{mode}:{name}",
        detail="battle log line read in Round 4 (docs/progress.md 4.1)",
    )
    level_line = _match_text(items, "LV") or _match_text(items, "级") or "no-level-badge"
    level_evidence = _pet_evidence(
        f"ocr:{level_line}",
        detail="HUD level badge; the colon glyph is OCR'd as '8' (LV:33 -> 'LV833')",
        confidence=CONF_SHAPE_REPAIRED,
    )
    hp_evidence = _pet_evidence(
        f"ocr:{hp[0]}/{hp[1]}" if hp else "ocr:no-a/b-in-HP-band",
        detail=f"x band {P.HP_X_BAND} (docs/state.md: ally HP at x=816)",
    )
    pp_evidence = _pet_evidence(
        f"ocr:使用次数 x{len(pp)}",
        detail="skill PP counters in the battle skill cells",
    )

    evidence = name_evidence
    extra = {
        "level_evidence": level_evidence.to_dict(),
        "hp_evidence": hp_evidence.to_dict(),
        "pp_evidence": pp_evidence.to_dict(),
    }
    state = PetState(
        name=name,
        level=level,
        hp=None if hp is None else hp[0],
        hp_max=None if hp is None else hp[1],
        pp=dict(pp),
        active=True if mode == "battle_hud" else None,
        evidence=evidence,
        extra=extra,
    )
    if name:
        world.pets[name] = state
    else:
        # HP was readable but no pet name was: keep the number, do not invent a name
        if hp is None:
            _record_unknown(world, "pets.<unnamed>.hp", "no HP readout in the HP band")
        else:
            _record(world, "pets.<unnamed>.hp", hp, hp_evidence, collected)
        _record_unknown(world, "pets.<unnamed>.name", "no 我方【…】 line readable")
    return state, {"level": level_evidence, "hp": hp_evidence, "pp": pp_evidence}


def _apply_opponent(world: GameWorldState, items: Sequence[Mapping[str, Any]], name: str,
                    collected: list[Evidence]) -> None:
    line = _first_text(items, "对方")
    evidence = _pet_evidence(f"ocr:{line}", detail="opponent log line")
    _record(world, "battle.opponent.name", name, evidence, collected)
    level = P.parse_level(items, side="enemy")
    if level is None:
        _record_unknown(world, "battle.opponent.level", "no readable enemy LV badge")
    else:
        badge = _first_text(items, "LV")
        _record(world, "battle.opponent.level", level,
                _pet_evidence(f"ocr:{badge}", detail="enemy HUD level badge",
                              confidence=CONF_SHAPE_REPAIRED), collected)


def _apply_turn(world: GameWorldState, items: Sequence[Mapping[str, Any]],
                collected: list[Evidence], *, expected: bool) -> int | None:
    turn = P.parse_turn(items)
    if turn is None:
        if expected:
            _record_unknown(world, "battle.turn", "no 当前回合 line readable")
        return None
    label = _first_text(items, "当前回合")
    detail = "caption and number may be two OCR boxes"
    _record(world, "battle.turn", turn,
            _pet_evidence(f"ocr:{label}+{turn}", detail=detail), collected)
    return turn


def _apply_rewards(world: GameWorldState, items: Sequence[Mapping[str, Any]],
                   collected: list[Evidence], *, expected: bool) -> list[dict[str, Any]]:
    rewards = P.parse_reward(items)
    if not rewards:
        if expected:
            _record_unknown(world, "rewards", "no 获得/得到 line with an amount readable")
        return []
    evidence = _pet_evidence(
        "ocr:" + "|".join(reward["raw"] for reward in rewards),
        detail="+".join(f"{r['kind']}:{r['amount']:g}" for r in rewards),
    )
    _record(world, "rewards", rewards, evidence, collected)
    world.observations["rewards"] = rewards
    return rewards


def _apply_currency(world: GameWorldState, items: Sequence[Mapping[str, Any]],
                    collected: list[Evidence]) -> dict[str, float]:
    amounts = P.parse_currency(items)
    texts = set(_texts(items))
    for label in P.CURRENCY_LABELS:
        amount = amounts.get(label)
        if amount is None:
            if label in texts:
                world.mark_unknown(f"resources.{label}")
            continue
        evidence = Evidence(
            Source.REPLAY_VERIFIED,
            origin=f"ocr:{label}={amount:g}",
            detail="caption+value pairing measured on artifacts/shop.png (2026-10-31); "
                   "the live HUD has not been re-calibrated for it",
            confidence=CONF_SHAPE_REPAIRED,
        )
        _record(world, f"resources.{label}", amount, evidence, collected)
        world.resources[label] = ResourceState(name=label, amount=amount, evidence=evidence)
    return amounts


def _apply_spt(world: GameWorldState, items: Sequence[Mapping[str, Any]],
               collected: list[Evidence]) -> tuple[list[dict[str, Any]], bool]:
    """Fill ``world.spt`` from the visible boss cards.  Returns (cards, visible)."""

    cards = P.parse_spt_cards(items)
    visible = bool(cards) or any(_SPT_STATE_WORD.search(t) or P.SPT_UNLOCK_LINE in t
                                 for t in _texts(items))
    for card in cards:
        name = str(card.get("name") or "")
        if not name:
            continue
        evidence = Evidence(
            Source.LIVE_VERIFIED,
            origin=f"ocr:spt_card:{name}:"
                   + ("已击败" if card.get("defeated") is True else
                      "未击败" if card.get("defeated") is False else "no-state-word"),
            detail=("card layout is only documented (docs/state.md), no panel screenshot "
                    "exists in artifacts/ -> shape unverified"),
            confidence=CONF_SHAPE_REPAIRED,
        )
        world.spt[name] = SptEntry(
            boss_id=name,
            name=name,
            defeated=card.get("defeated"),
            reward_unlock=str(card.get("reward_unlock") or ""),
            evidence=evidence,
        )
        if card.get("defeated") is None:
            _record_unknown(world, f"spt.{name}.defeated",
                            "card shows no 已击败/未击败 word")
        else:
            _record(world, f"spt.{name}.defeated", bool(card["defeated"]), evidence, collected)
        if card.get("reward_unlock"):
            _record(world, f"spt.{name}.reward_unlock", str(card["reward_unlock"]),
                    evidence, collected)
    if not visible:
        _record_unknown(world, "spt.panel_visible",
                        "no SPT state word or unlock caption on screen")
    else:
        _record(world, "spt.panel_visible", True,
                Evidence(Source.LIVE_VERIFIED,
                         origin=f"ocr:spt_cards={len(cards)}",
                         detail="presence proven by 已击败/未击败 or 击败后解锁交易行资格",
                         confidence=CONF_SHAPE_REPAIRED),
                collected)
    return cards, visible


def _panel_pet_name(items: Sequence[Mapping[str, Any]]) -> str | None:
    for item in items:
        match = _PANEL_NAME.search(P.text_of(item))
        if match:
            name = match.group(1).strip()
            if name and not name.isdigit():
                return name
    return None


# --------------------------------------------------------------------------- #
# skills
# --------------------------------------------------------------------------- #
@skill(SkillSpec(
    id="game.get_status",
    kind=SkillKind.OBSERVE,
    summary=("Cheap look: window geometry, frontmost flag and the template-level scene. "
             "No OCR, no input."),
    outputs={"scene": "str", "scores": "dict", "window": "dict"},
    max_duration_s=5.0,
    max_actions=0,
    requires_input=False,
    verified="real",
    evidence=(
        "Round 4 (docs/progress.md 4.1-4.5): scene template table ran on the live client",
        "seerauto/live.py ROI_* / TH_* measured on live frames",
        "docs/state.md: window 1200x800 pt @ (225,53) -> 2400x1600 px",
    ),
))
class GetStatus(_ObserveSkill):
    """Window + scene only; the cheapest thing that can tell 'where are we'."""

    def run(self, ctx: SkillContext, **_: Any) -> SkillResult:
        session = ctx.session
        if session is None:
            return SkillResult.fail(self.id, "no session: cannot observe the client",
                                    escalation="pause")
        previous = _as_scene(ctx.world.fact("session.scene"))
        scene, scores, points = _probe_scene(session, None if previous is Scene.UNKNOWN else previous)
        desc = _describe(session)
        world = ctx.world.copy()
        world.window = _window_state(session, desc)

        collected: list[Evidence] = []
        _apply_scene(world, scene, scores, collected)
        collected.append(world.window.evidence)
        return SkillResult.pass_(
            self.id,
            changed=False,
            proof=(f"scene={scene.value} "
                   + " ".join(f"{k}={v:.2f}" for k, v in sorted(scores.items()))),
            data={
                "world": world.to_dict(),
                "scene": scene.value,
                "scores": {k: round(v, 3) for k, v in scores.items()},
                "points": {k: [round(v[0]), round(v[1])] for k, v in points.items()
                           if isinstance(v, (tuple, list)) and len(v) == 2},
                "window": None if world.window is None else world.window.to_dict(),
                "frontmost": None if world.window is None else world.window.frontmost,
            },
            evidence=collected,
        )


@skill(SkillSpec(
    id="game.get_current_scene",
    kind=SkillKind.OBSERVE,
    summary="Scene enum plus every template score that produced it.",
    outputs={"scene": "str", "scores": "dict"},
    max_duration_s=5.0,
    max_actions=0,
    requires_input=False,
    verified="real",
    evidence=("Round 4: scene decision table + hysteresis on live frames",
              "docs/progress.md: victory panel keeps the 战斗 button visible, so the "
              "胜利 glyph is tested separately"),
))
class GetCurrentScene(_ObserveSkill):
    """The scene classifier, exposed on its own for debugging a stuck run."""

    def run(self, ctx: SkillContext, **_: Any) -> SkillResult:
        session = ctx.session
        if session is None:
            return SkillResult.fail(self.id, "no session: cannot observe the client",
                                    escalation="pause")
        previous = _as_scene(ctx.world.fact("session.scene"))
        scene, scores, points = _probe_scene(session, None if previous is Scene.UNKNOWN else previous)
        world = ctx.world.copy()
        collected: list[Evidence] = []
        evidence = _apply_scene(world, scene, scores, collected)
        return SkillResult.pass_(
            self.id,
            changed=False,
            proof=f"scene={scene.value}",
            data={
                "world": world.to_dict(),
                "scene": scene.value,
                "scores": {k: round(v, 3) for k, v in scores.items()},
                "points": {k: [round(v[0]), round(v[1])] for k, v in points.items()
                           if isinstance(v, (tuple, list)) and len(v) == 2},
            },
            evidence=[evidence],
        )


@skill(SkillSpec(
    id="game.observe",
    kind=SkillKind.OBSERVE,
    summary=("Full observation: scene + one OCR look; fills scene, active pet, level, "
             "HP, turn, rewards, currencies and (optionally) the SPT cards."),
    inputs={
        "spt_panel": {"type": "bool", "required": False,
                      "description": "also parse the SPT boss cards from this frame"},
        "ocr": {"type": "bool", "required": False,
                "description": "do one OCR look (default true); false = templates only"},
    },
    outputs={"world": "dict", "texts": "list"},
    max_duration_s=20.0,
    max_actions=0,
    requires_input=False,
    verified="real",
    evidence=("Round 4 4.1: OCR read 当前回合：1, 我方【烈火猴】, "
              "对方【蘑菇怪】 on the live client",
              "Round 4 4.4: victory -> 确认 -> 领取经验 106",
              "replay: artifacts/live/*.png (2400x1600) parsed by seerauto/skills/parsers.py"),
))
class Observe(_ObserveSkill):
    """One frame -> one evidence-tagged world state."""

    def run(self, ctx: SkillContext, *, spt_panel: bool = False,
            ocr: bool = True, **_: Any) -> SkillResult:
        session = ctx.session
        if session is None:
            return SkillResult.fail(self.id, "no session: cannot observe the client",
                                    escalation="pause")

        previous = _as_scene(ctx.world.fact("session.scene"))
        scene, scores, points = _probe_scene(session, None if previous is Scene.UNKNOWN else previous)
        items = _ocr_items(session, ocr)

        world = ctx.world.copy()
        world.window = _window_state(session, _describe(session))
        collected: list[Evidence] = [world.window.evidence]
        _apply_scene(world, scene, scores, collected)

        names = P.parse_pet_names(items)
        ally = names.get("ally")
        if ally:
            world.active_pet = ally
            _apply_pet(world, items, name=ally, mode="battle_hud", collected=collected)
        else:
            _record_unknown(world, "pets.<active>.name",
                            "no 我方【...】 line and no pet panel on screen")
        enemy = names.get("enemy")
        if enemy:
            _apply_opponent(world, items, enemy, collected)

        expected = scene in _BATTLE_SCENES
        turn = _apply_turn(world, items, collected, expected=expected)
        rewards = _apply_rewards(world, items, collected, expected=scene in _REWARD_SCENES)
        amounts = _apply_currency(world, items, collected)

        cards: list[dict[str, Any]] = []
        if spt_panel:
            cards, visible = _apply_spt(world, items, collected)
            world.observations["spt_visible"] = visible

        buttons = P.parse_dialog_buttons(items)
        world.observations.update({
            "ocr_items": len(items),
            "ocr_min_conf": OCR_MIN_CONF,
            "texts": _texts(items),
            "buttons": buttons,
            "turn": turn,
        })

        proof_bits = [f"scene={scene.value}"]
        if turn is not None:
            proof_bits.append(f"turn={turn}")
        if ally:
            pet = world.pets.get(ally)
            if pet is not None and pet.hp is not None:
                proof_bits.append(f"hp={pet.hp}/{pet.hp_max}")
            if pet is not None and pet.level is not None:
                proof_bits.append(f"lv={pet.level}")
        if rewards:
            proof_bits.append(
                "rewards=" + ",".join(f"{r['kind']}:{r['amount']:g}" for r in rewards))
        if amounts:
            proof_bits.append("resources=" + ",".join(f"{k}:{v:g}" for k, v in amounts.items()))
        if cards:
            proof_bits.append(f"spt_cards={len(cards)}")
        if buttons:
            proof_bits.append("buttons=" + ",".join(b["text"] for b in buttons))

        return SkillResult.pass_(
            self.id,
            changed=False,
            proof=" ".join(proof_bits),
            data={
                "world": world.to_dict(),
                "texts": _texts(items),
                "scene": scene.value,
                "scores": {k: round(v, 3) for k, v in scores.items()},
                "points": {k: [round(v[0]), round(v[1])] for k, v in points.items()
                           if isinstance(v, (tuple, list)) and len(v) == 2},
                "ocr": bool(ocr),
                "spt": cards,
            },
            evidence=collected,
        )


@skill(SkillSpec(
    id="game.inspect_pets",
    kind=SkillKind.OBSERVE,
    summary=("Read the pet panel, or the battle HUD when a fight is on: name, level, "
             "HP, PP.  BLOCKED when neither is visible."),
    outputs={"pets": "list", "mode": "str"},
    max_duration_s=25.0,
    max_actions=0,
    requires_input=False,
    verified="replay",
    evidence=("replay: artifacts/live/*.png battle HUD (name/level/HP live-verified in Round 4)",
              "replay: artifacts/petpanel.png (2026-10-19, 小火猴 Lv5, pre-evolution) is the "
              "only pet-panel sample; the panel was never reopened live in Round 4",
              "the bag grid that lists several pets has no verified OCR shape -> not parsed"),
))
class InspectPets(_ObserveSkill):
    """One pet record from either the detail panel or the battle HUD."""

    def run(self, ctx: SkillContext, **_: Any) -> SkillResult:
        session = ctx.session
        if session is None:
            return SkillResult.fail(self.id, "no session: cannot observe the client",
                                    escalation="pause")
        items = _ocr_items(session, True)
        world = ctx.world.copy()
        collected: list[Evidence] = []

        panel_name = _panel_pet_name(items)
        names = P.parse_pet_names(items)
        hp = P.parse_hp(items)
        if panel_name:
            mode, name = "panel", panel_name
        elif names.get("ally") or hp is not None:
            mode, name = "battle_hud", names.get("ally") or ""
        else:
            return SkillResult.blocked(
                self.id,
                "pet panel not visible",
                escalation="replan",
                data={"mode": None, "texts": _texts(items)},
            )

        pet, per_field = _apply_pet(world, items, name=name, mode=mode, collected=collected)
        if mode == "panel":
            pet.active = None
            pet.evidence = _pet_evidence(
                f"ocr:{_match_text(items, '名字') or '名字'}",
                detail="pet detail panel; its shape is only known from a 2026-10-19 "
                       "screenshot (小火猴 Lv5, before the evolution to 烈火猴)",
                confidence=CONF_SHAPE_REPAIRED,
            )
            if pet.level is None:
                _record_unknown(world, "pets.<active>.level",
                                "pet panel visible but no 级/等级 line readable")
        else:
            world.active_pet = name or None
        collected.extend([pet.evidence, *per_field.values()])
        return SkillResult.pass_(
            self.id,
            changed=False,
            proof=(f"mode={mode} pet={name or '?'} level={pet.level} "
                   f"hp={None if pet.hp is None else f'{pet.hp}/{pet.hp_max}'} pp={len(pet.pp)}"),
            data={
                "world": world.to_dict(),
                "mode": mode,
                "pets": [pet.to_dict()],
            },
            evidence=collected,
        )


@skill(SkillSpec(
    id="game.inspect_resources",
    kind=SkillKind.OBSERVE,
    summary="Wallet amounts (赛尔豆 / XIN豆 / 金豆) from the HUD or the shop header.",
    outputs={"resources": "dict"},
    max_duration_s=25.0,
    max_actions=0,
    requires_input=False,
    verified="replay",
    evidence=("replay: artifacts/shop.png (2026-10-31) header 'XIN豆 5,000' / '金豆' with the "
              "value dropped by the OCR",
              "no verified shape for bag consumable counts -> consumables are NOT parsed",
              "the live HUD currency row has not been re-calibrated since the 1200x800 change"),
))
class InspectResources(_ObserveSkill):
    """Currencies only; a missing amount is omitted, never invented."""

    def run(self, ctx: SkillContext, **_: Any) -> SkillResult:
        session = ctx.session
        if session is None:
            return SkillResult.fail(self.id, "no session: cannot observe the client",
                                    escalation="pause")
        items = _ocr_items(session, True)
        world = ctx.world.copy()
        collected: list[Evidence] = []
        amounts = _apply_currency(world, items, collected)
        texts = _texts(items)
        labels_seen = [label for label in P.CURRENCY_LABELS if label in texts]

        if not amounts:
            reason = ("resource HUD not visible" if not labels_seen
                      else "currency labels visible but no amount was readable")
            return SkillResult.blocked(
                self.id,
                reason,
                escalation="replan",
                data={"resources": {}, "labels_seen": labels_seen, "texts": texts,
                      "consumables": [], "consumables_parsed": False},
            )
        return SkillResult.pass_(
            self.id,
            changed=False,
            proof="resources=" + ",".join(f"{k}:{v:g}" for k, v in amounts.items()),
            data={
                "world": world.to_dict(),
                "resources": amounts,
                "labels_seen": labels_seen,
                "consumables": [],
                "consumables_parsed": False,
                "consumables_note": ("bag consumable counts have no verified OCR shape in this "
                                     "repo, so they are not parsed rather than guessed"),
            },
            evidence=collected,
        )


@skill(SkillSpec(
    id="game.inspect_spt_progress",
    kind=SkillKind.OBSERVE,
    summary="SPT boss cards from the open SPT panel: name, defeated, unlock caption.",
    outputs={"bosses": "list"},
    max_duration_s=25.0,
    max_actions=0,
    requires_input=False,
    verified="unverified",
    evidence=("docs/state.md / docs/progress.md: panel text 未击败 / 已击败 / "
              "击败后解锁交易行资格 was read on the live client 2026-11-02",
              "NOT VERIFIED: no SPT panel screenshot exists in artifacts/, so the card "
              "geometry used to group name+state is a documented heuristic, not a measurement"),
))
class InspectSptProgress(_ObserveSkill):
    """Needs the SPT panel open; BLOCKED otherwise (never a fabricated 0/73)."""

    def run(self, ctx: SkillContext, **_: Any) -> SkillResult:
        session = ctx.session
        if session is None:
            return SkillResult.fail(self.id, "no session: cannot observe the client",
                                    escalation="pause")
        items = _ocr_items(session, True)
        world = ctx.world.copy()
        collected: list[Evidence] = []
        cards, visible = _apply_spt(world, items, collected)
        if not visible:
            return SkillResult.blocked(
                self.id,
                "SPT panel not visible",
                escalation="replan",
                data={"bosses": [], "world": world.to_dict(), "texts": _texts(items)},
            )
        if not cards:
            return SkillResult.blocked(
                self.id,
                "SPT panel visible but no boss card with a state word was readable",
                escalation="replan",
                data={"bosses": [], "world": world.to_dict(), "texts": _texts(items)},
            )
        proof = "; ".join(
            f"{card['name'] or '?'}="
            + ("已击败" if card.get("defeated") is True else "未击败")
            for card in cards)
        return SkillResult.pass_(
            self.id,
            changed=False,
            proof=proof,
            data={
                "bosses": cards,
                "world": world.to_dict(),
                "texts": _texts(items),
            },
            evidence=collected,
        )


def build_observe_skills() -> list[Skill]:
    """Every observe skill, ready for a :class:`~seerauto.skills.base.SkillRegistry`.

    Order is cheapest-first, which is also the order a long-horizon runner should
    probe in before spending an OCR look.
    """

    return [
        GetStatus(),
        GetCurrentScene(),
        Observe(),
        InspectPets(),
        InspectResources(),
        InspectSptProgress(),
    ]


__all__ = [
    "OCR_MIN_CONF",
    "SCENE_TEMPLATE_ORIGIN",
    "CONF_SHAPE_VERIFIED",
    "CONF_SHAPE_REPAIRED",
    "GetStatus",
    "GetCurrentScene",
    "Observe",
    "InspectPets",
    "InspectResources",
    "InspectSptProgress",
    "build_observe_skills",
]
