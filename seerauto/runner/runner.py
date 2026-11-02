"""The Goal Runner: observe -> plan -> execute -> verify -> checkpoint -> repeat.

Layering (enforced here, not by convention)::

    User Goal
      -> planner          (AI or deterministic; picks ONE node, explains why)
      -> scheduler gate   (this module: validates the decision against the graph,
                           the skill registry and the run budgets)
      -> semantic skills  (verified capabilities only)
      -> LiveSession      (the only thing that touches pixels and the mouse)
      -> the real game

Properties that matter for a *long* run:

* every loop iteration starts from a fresh observation, never from a memory of
  what the previous iteration believed;
* a node is marked complete only when the graph's ``verified_account_state``
  predicate evaluates on **live evidence**, never because a skill returned 0;
* the world snapshot, the node state and a checkpoint are committed together
  before any further action, so a crash resumes from the last true state;
* pause/stop are checked between skills, and a pause forces a re-observation;
* budgets (minutes / battles / actions / spend) are enforced by the runner, so
  a misbehaving planner cannot run away;
* when the AI planner is unavailable the run degrades to the deterministic
  planner and records why — the local executor keeps working.
"""

from __future__ import annotations

import json
import os
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..goals.graph import GoalGraph, GoalNode
from ..planner import (PlanDecision, PlanRequest, Planner, PlannerError,
                       PlannerUnavailable, build_planner)
from ..skills.base import Budget, SkillContext, SkillRegistry, SkillResult, Status
from ..world.model import Evidence, Fact, GameWorldState, Scene, Source, now_iso
from ..world.store import WorldStore
from .control import (DEFAULT_LOCK_FILE, DEFAULT_PAUSE_FILE, DEFAULT_STOP_FILE,
                      InstanceLock, RunControl)
from . import playbooks

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "data" / "goalrunner.sqlite3"
DEFAULT_AI_DIR = ROOT / "data" / "ai"


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
@dataclass
class RunConfig:
    goal: str = "puni"
    planner: str = "deterministic"
    dry_run: bool = True
    allow_input: bool = False
    max_minutes: float = 60.0
    max_battles: int = 12
    max_actions: int = 400
    max_spend: float = 0.0
    max_errors: int = 3
    max_loops: int = 40
    db_path: str = str(DEFAULT_DB)
    ai_dir: str = str(DEFAULT_AI_DIR)
    pause_file: str = DEFAULT_PAUSE_FILE
    stop_file: str = DEFAULT_STOP_FILE
    lock_file: str = DEFAULT_LOCK_FILE
    run_id: str | None = None
    #: Opt-in: open the pet panel during preflight (needs a template-located icon).
    preflight_pet_panel: bool = True
    planner_options: dict[str, Any] = field(default_factory=dict)

    @property
    def budgets(self) -> dict[str, Any]:
        return {
            "max_minutes": self.max_minutes,
            "max_battles": self.max_battles,
            "max_actions": self.max_actions,
            "max_spend": self.max_spend,
            "max_errors": self.max_errors,
            "max_loops": self.max_loops,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal, "planner": self.planner, "dry_run": self.dry_run,
            "allow_input": self.allow_input, "budgets": self.budgets,
            "db_path": self.db_path, "ai_dir": self.ai_dir, "run_id": self.run_id,
        }


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #
class GoalRunner:
    """One process, one goal, one game window."""

    def __init__(self, config: RunConfig, *,
                 store: WorldStore | None = None,
                 graph: GoalGraph | None = None,
                 registry: SkillRegistry | None = None,
                 planner: Planner | None = None,
                 session: Any = None,
                 control: RunControl | None = None,
                 log: Any = print):
        self.config = config
        self.store = store
        self.graph = graph
        self.registry = registry
        self.planner = planner
        self.session = session
        self.control = control or RunControl(config.pause_file, config.stop_file)
        self.log = log or (lambda *a, **k: None)
        self.run_id: str | None = config.run_id
        self._lock: InstanceLock | None = None
        self._started = 0.0
        self._battles = 0
        self._actions = 0
        self._errors = 0
        self._loops = 0
        self._last_world: GameWorldState | None = None
        self._planner_degraded: str | None = None
        self._node_attempts: dict[str, int] = {}
        self._next_observations: list[dict[str, Any]] = []
        self._can_battle: bool | None = None

    # -- lifecycle --------------------------------------------------------- #
    def open(self) -> "GoalRunner":
        cfg = self.config
        Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.store = self.store or WorldStore(cfg.db_path)
        self.graph = self.graph or GoalGraph.load()
        if self.registry is None:
            from ..skills.act import build_act_skills

            registry = SkillRegistry(build_act_skills())
            try:  # observation skills are optional so the runner works alone
                from ..skills.observe import build_observe_skills

                for skill in build_observe_skills():
                    if skill.id not in registry:
                        registry.register(skill)
            except Exception as exc:  # noqa: BLE001
                self.log(f"[runner] observe skills unavailable, template-only observation: {exc}")
            self.registry = registry
        if self.planner is None:
            self.planner = self._make_planner()
        self._started = time.monotonic()
        return self

    def _make_planner(self) -> Planner:
        cfg = self.config
        kw = dict(cfg.planner_options)
        if cfg.planner == "bridge":
            kw.setdefault("directory", cfg.ai_dir)
        try:
            return build_planner(cfg.planner, **kw)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[runner] planner {cfg.planner!r} unavailable ({exc}); "
                     f"degrading to deterministic")
            self._planner_degraded = f"{cfg.planner}: {exc}"
            return build_planner("deterministic")

    def acquire_lock(self) -> None:
        self._lock = InstanceLock(self.config.lock_file).acquire()

    def release_lock(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None

    def close(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            except Exception:  # noqa: BLE001
                pass
        if self.store is not None:
            try:
                self.store.close()
            except Exception:  # noqa: BLE001
                pass
        self.release_lock()

    def __enter__(self) -> "GoalRunner":
        return self.open()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- helpers ----------------------------------------------------------- #
    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._started if self._started else 0.0

    def _budget_exhausted(self) -> str | None:
        cfg = self.config
        if cfg.max_minutes and self.elapsed_s / 60.0 >= cfg.max_minutes:
            return f"time budget {cfg.max_minutes:.0f} min reached"
        if cfg.max_battles and self._battles >= cfg.max_battles:
            return f"battle budget {cfg.max_battles} reached"
        if self._actions >= cfg.max_actions:
            return f"action budget {cfg.max_actions} reached"
        if self._errors >= cfg.max_errors:
            return f"error budget {cfg.max_errors} reached"
        if self._loops >= cfg.max_loops:
            return f"loop budget {cfg.max_loops} reached"
        return None

    def completed_nodes(self, world: GameWorldState | None = None) -> set[str]:
        assert self.store is not None and self.graph is not None
        done: set[str] = set()
        if self.run_id:
            for node_id, row in self.store.node_states(self.run_id).items():
                if str(row.get("status")) == "completed":
                    done.add(node_id)
        if world is not None:
            # a node whose predicate is already true never runs again, even if
            # this run has never seen it
            for node in self.graph:
                if self.graph.status_of(node.id, world=world, completed=done) == "completed":
                    done.add(node.id)
        return done

    # -- observation ------------------------------------------------------- #
    def observe(self, *, full: bool = True) -> GameWorldState:
        """One evidence-tagged observation of the live client (read-only)."""

        if self.session is None:
            world = GameWorldState(run_id=self.run_id)
            world.notes.append("no live session (dry run without --live)")
            return world
        # Carry the previous observation forward and merge: an unobserved field
        # must keep its last known value *with its evidence*, never silently
        # reset to UNKNOWN (that is how a run loses a fact it already paid for).
        previous = self._last_world
        if previous is None and self.store is not None:
            try:
                previous = self.store.latest_world()
            except Exception:  # noqa: BLE001
                previous = None
        world = previous.copy() if previous is not None else GameWorldState()
        world.run_id = self.run_id
        # window + frontmost are free
        raw = getattr(self.session, "window_raw", {}) or {}
        if raw:
            from ..world.model import WindowState

            world.window = WindowState(
                window_id=raw.get("id"), x=raw.get("x"), y=raw.get("y"),
                w=raw.get("w"), h=raw.get("h"), scale=2.0,
                frontmost=self.session.ensure_frontmost(),
                title="xin计划",
                evidence=Evidence.live("seerauto.live.win"),
            )
        ctx = SkillContext(session=self.session, world=world,
                           dry_run=self.config.dry_run,
                           allow_input=self.config.allow_input,
                           options={"control": self.control})
        skill_id = "game.observe" if "game.observe" in (self.registry or ()) else None
        if skill_id:
            try:
                result = self.registry.get(skill_id)(ctx)
                payload = result.data.get("world") if isinstance(result.data, dict) else None
                if isinstance(payload, Mapping):
                    world = GameWorldState.from_dict(payload)
                    world.run_id = self.run_id
                elif result.ok:
                    world = ctx.world
            except Exception as exc:  # noqa: BLE001
                world.notes.append(f"game.observe failed: {exc}")
        if world.scene.value is None:
            world.set_unknown("session.scene")
        # live distance measurement always runs: it is 8 ms and decides safety
        try:
            sp = self.session.scene()
            world.set_fact("session.scene", sp.scene.value,
                           Evidence.live(f"template:{','.join(f'{k}={v:.2f}' for k, v in sp.scores.items())}"))
            world.set_fact("session.in_battle", sp.scene in (Scene.BATTLE, Scene.RESULT),
                           Evidence.live("template:live_fight_btn"))
            world.set_fact("session.logged_in", sp.scene not in (Scene.LOGIN, Scene.SERVER_LIST),
                           Evidence.live("template:scene"))
            world.observations["scene_scores"] = sp.scores
        except Exception as exc:  # noqa: BLE001
            world.notes.append(f"scene probe failed: {exc}")
        world.captured_at = now_iso()
        self._last_world = world
        return world

    def preflight(self, world: GameWorldState) -> GameWorldState:
        """Read the facts planning cannot work without, before planning.

        The pet level is the canonical example: the goal graph refuses to
        schedule 练级 while the level is UNKNOWN, which is correct — but the
        answer is not "give up", it is "open the pet panel and look".  A dry run
        cannot open the panel (that is input), so it says so explicitly instead
        of pretending the level is known.
        """

        if self.session is None or self.registry is None:
            return world
        if not (self.config.allow_input and not self.config.dry_run):
            world.notes.append(
                "dry run: pet level / currency NOT read (panel inspection needs "
                "input); the plan below is conditional on an unobserved level")
            return world
        # The client may have been left mid-battle or with a panel open by a
        # previous run (or by the user): settle those first, otherwise opening a
        # panel is impossible and every observation after it is garbage.
        world = self._settle_pending_battle(world)
        # The pet-panel icon lives in a bottom bar whose position drifts between
        # scenes (measured: ~100 px between 太空站 and 克洛斯星林间), so opening it
        # by coordinate is unreliable and can open a stray panel that then blocks
        # the battle loop.  The battle HUD reports the ally level anyway, so the
        # panel path is opt-in rather than part of every preflight.
        if not self.config.preflight_pet_panel:
            world.notes.append(
                "preflight: pet panel skipped (opt-in); ally level will be read "
                "from the battle HUD")
            return world
        # Prefer the bag reader: it is template-located (the bottom bar drifts),
        # and it yields HP as well as level — HP is what decides whether a battle
        # node can run at all.
        reader = ("game.read_pet_bag" if "game.read_pet_bag" in self.registry
                  else "game.read_active_pet_level")
        if reader not in self.registry:
            return world
        ctx = self._skill_ctx(world, max_duration_s=60.0, max_actions=4)
        try:
            result = self.registry.get(reader)(ctx)
        except Exception as exc:  # noqa: BLE001
            world.notes.append(f"preflight pet read failed: {exc}")
            return world
        if not result.ok:
            world.notes.append(f"preflight pet read failed: {result.reason}")
            return world
        from ..world.model import PetState

        name = str(result.after.get("name") or "烈火猴")
        evidence = Evidence.live(f"ocr:{result.proof}", detail=result.proof)
        pet = world.pets.get(name) or PetState(name=name)
        if result.after.get("level") is not None:
            pet.level = result.after.get("level")
        pet.evidence = evidence
        pet.active = True
        bag = result.after.get("pets") or []
        if bag:
            pet.hp = bag[0].get("hp")
            pet.hp_max = bag[0].get("hp_max")
        world.pets[name] = pet
        world.active_pet = name
        world.set_fact("state.active_pet_level_known", True, evidence)
        world.set_fact(f"pets.{name}.level", pet.level, evidence)
        can_battle = result.after.get("can_battle")
        if can_battle is not None:
            world.set_fact("session.battle_ready", bool(can_battle), evidence)
            self._can_battle = bool(can_battle)
        if result.after.get("xp_to_next") is not None:
            world.observations["pet_xp_to_next"] = result.after["xp_to_next"]
        world.notes.append(f"preflight: {result.proof}")
        if self.store is not None and self.run_id:
            self.store.save_world(world, run_id=self.run_id, note="preflight observation")
            self.store.append_event(self.run_id, "observe.preflight", result.proof,
                                    result.to_dict())
        self._last_world = world
        return world

    def _settle_pending_battle(self, world: GameWorldState, *, max_dialogs: int = 8) -> GameWorldState:
        """Dismiss victory/XP dialogs left over from an earlier interruption."""

        if self.session is None:
            return world
        for _ in range(max_dialogs):
            try:
                probe = self.session.scene()
            except Exception:  # noqa: BLE001
                return world
            if probe.scene in (Scene.RESULT, Scene.DIALOG):
                try:
                    self.session.click_px(*probe.points["confirm"],
                                          f"settle leftover {probe.scene.value}")
                except Exception as exc:  # noqa: BLE001
                    world.notes.append(f"cannot settle leftover dialog: {exc}")
                    break
                time.sleep(0.9)
                continue
            # Some dialogs (XP / drop / achievement) use a plain text button that
            # no template covers, so a template-only sweep leaves the screen
            # blocked and every later panel open fails.  One OCR look settles it.
            try:
                items = self.session.ocr(min_conf=0.35)
            except Exception:  # noqa: BLE001
                break
            hit = None
            for needle in ("确认", "确定", "我已知晓", "我知道了"):
                hit = self.session.find_text(items, (needle,))
                if hit:
                    break
            if hit is None:
                # two clean passes in a row: the queue is really empty
                self._settle_clean = getattr(self, "_settle_clean", 0) + 1
                if self._settle_clean >= 2:
                    break
                time.sleep(0.4)
                continue
            self._settle_clean = 0
            try:
                self.session.click_px(hit["px"][0], hit["px"][1], f"settle OCR {hit['text']!r}")
            except Exception as exc:  # noqa: BLE001
                world.notes.append(f"cannot settle OCR dialog: {exc}")
                break
            time.sleep(0.8)
        try:
            alive = self.session.scene()
            world.set_fact("session.scene", alive.scene.value,
                           Evidence.live("post-settle template probe"))
        except Exception:  # noqa: BLE001
            pass
        return world

    @staticmethod
    def _pet_level_known(world: GameWorldState) -> bool:
        pet = world.active
        return bool(pet is not None and pet.level is not None
                    and pet.evidence.source.can_complete_goal)

    # -- planning ---------------------------------------------------------- #
    def build_request(self, world: GameWorldState,
                      events: Iterable[Mapping[str, Any]] = (),
                      last_result: Mapping[str, Any] | None = None) -> PlanRequest:
        assert self.graph is not None and self.registry is not None
        completed = self.completed_nodes(world)
        # Bootstrap rule: when the only thing standing between us and a playbook
        # is a fact the playbook itself will observe (the battle HUD reports the
        # ally level), planning it is correct — refusing would mean "we cannot
        # look because we have not looked".  It is surfaced, not hidden.
        bootstrap = set(self._bootstrap_candidates(world))
        digest = []
        for node in sorted(self.graph, key=lambda n: (-n.priority, n.id)):
            status = self.graph.status_of(node.id, world=world, completed=completed)
            if node.id in bootstrap and status != "ready":
                status = "ready"
            digest.append({
                "id": node.id,
                "legacy_id": node.legacy_id,
                "title": node.title,
                "phase": node.phase,
                "priority": node.priority,
                "status": status,
                "bootstrap": node.id in bootstrap,
                "executable": node.executable,
                "skill": node.skill,
                "budget": dict(node.budget),
                "blocked_reason": (self.graph.unmet(node.id, world=world, completed=completed)
                                   or [])[:3],
                "playbook": playbooks.has_playbook(node.id),
            })
        return PlanRequest.build(
            self.config.goal, world,
            graph_digest=digest,
            capabilities=self.registry.specs(),
            completed_nodes=sorted(completed),
            recent_events=list(events),
            last_result=last_result,
            budgets=self.config.budgets,
            constraints={"allow_input": self.config.allow_input,
                         "dry_run": self.config.dry_run,
                         "max_spend": self.config.max_spend},
            run_id=self.run_id,
        )

    def decide(self, request: PlanRequest) -> PlanDecision:
        assert self.planner is not None
        try:
            decision = self.planner.decide(request)
        except PlannerUnavailable as exc:
            self._planner_degraded = str(exc)
            self.log(f"[runner] planner unavailable ({exc}); degrading to deterministic")
            self.planner = build_planner("deterministic")
            decision = self.planner.decide(request)
        return decision

    #: The scheduler gate.  A decision that fails these checks is refused, not
    #: "fixed up": the planner is untrusted input, even when it is deterministic.
    def gate(self, decision: PlanDecision) -> str | None:
        assert self.graph is not None and self.registry is not None
        if decision.is_refusal:
            return None
        node_id = decision.selected_node
        if node_id not in self.graph:
            return f"unknown node {node_id!r}"
        node = self.graph.node(node_id)
        if not node.executable:
            return f"node {node_id} is not executable (status={node.status})"
        if not playbooks.has_playbook(node_id):
            return f"node {node_id} has no playbook (no verified skill sequence)"
        if decision.capability and decision.capability not in self.registry:
            return f"unknown capability {decision.capability!r}"
        for skill in (node.skill,):
            if skill and skill not in self.registry:
                return f"node skill {skill!r} is not registered"
        if decision.max_actions > self.config.max_actions:
            return (f"planner asked for {decision.max_actions} actions, "
                    f"budget is {self.config.max_actions}")
        if decision.max_duration_s > self.config.max_minutes * 60:
            return (f"planner asked for {decision.max_duration_s}s, "
                    f"run budget is {self.config.max_minutes * 60:.0f}s")
        if decision.requires_approval and not self.config.allow_input:
            return "decision needs approval while input is disabled"
        world = self._last_world
        if world is not None and node_id in set(self._bootstrap_candidates(world)):
            self.log(f"[runner] bootstrap: {node_id} is scheduled to obtain the "
                     f"observation it needs ({node_id} not complete)")
        return None

    # -- execution --------------------------------------------------------- #
    def _skill_ctx(self, world: GameWorldState, *, max_duration_s: float,
                   max_actions: int) -> SkillContext:
        return SkillContext(
            session=self.session, world=world, store=self.store, run=self.run_id,
            budget=Budget(max_duration_s=max_duration_s, max_actions=max_actions),
            dry_run=self.config.dry_run, allow_input=self.config.allow_input,
            max_spend=self.config.max_spend,
            options={"control": self.control, "run_id": self.run_id},
        )

    def _scene_has_monster(self) -> bool:
        if self.session is None:
            return False
        try:
            frame = self.session.latest()
            probe = self.session.score(frame, "live_monster_moguguai", (200, 340, 2260, 1330))
            return probe.score >= 0.72
        except Exception:  # noqa: BLE001
            return False

    def _in_combat(self, world: GameWorldState) -> bool:
        scene = str(world.fact("session.scene") or "")
        return scene in (Scene.BATTLE.value, Scene.RESULT.value, Scene.DIALOG.value)

    def _step_when(self, when: str, world: GameWorldState) -> bool:
        if when in ("always", ""):
            return True
        if when in ("no_monster", "no_monster_idle"):
            if self._in_combat(world):
                return False
            return not self._scene_has_monster()
        if when == "not_in_battle":
            scene = str(world.fact("session.scene") or "")
            return scene not in (Scene.BATTLE.value, Scene.RESULT.value)
        return False

    def execute_node(self, node: GoalNode, world: GameWorldState,
                     decision: PlanDecision) -> tuple[list[SkillResult], SkillResult | None]:
        """Run the node's playbook.  Returns (all results, the mutating result)."""

        assert self.registry is not None
        steps = playbooks.playbook(node.id)
        results: list[SkillResult] = []
        last: SkillResult | None = None
        if self.config.dry_run or not self.config.allow_input:
            # DRY RUN: plan and observe only; never send input.
            for step in steps:
                results.append(SkillResult(
                    skill=step.skill, status=Status.SKIPPED,
                    reason="dry run: input disabled, step not executed",
                    data={"inputs": dict(step.inputs), "when": step.when},
                ))
            return results, None
        for step in steps:
            if self.control.stop_requested():
                break
            # A battle step must not even be attempted when every pet is at 0 HP:
            # the client answers 「你没有可出战的精灵哦」 and the pass burns its whole
            # budget on clicks that cannot start a fight (measured 2026-11-03).
            if "battle" in step.skill and self._can_battle is False:
                results.append(SkillResult(
                    skill=step.skill, status=Status.BLOCKED,
                    reason="no pet can battle (all pets at 0 HP); healing required",
                    escalation="pause",
                    data={"recovery": "heal_pet"}))
                break
            self.control.wait_while_paused(
                on_pause=lambda: self.log("[runner] PAUSED (control file)"),
                on_resume=lambda s: self.log(f"[runner] RESUMED after {s:.1f}s"))
            if not self._step_when(step.when, world):
                results.append(SkillResult(skill=step.skill, status=Status.SKIPPED,
                                           reason=f"condition {step.when!r} not met"))
                continue
            inputs = dict(step.inputs)
            if step.skill == "game.run_battle":
                remaining = self.config.max_battles - self._battles
                if remaining <= 0:
                    break
                inputs["battles"] = min(int(inputs.get("battles", 1)), max(1, remaining))
            ctx = self._skill_ctx(world, max_duration_s=float(decision.max_duration_s or 600),
                                  max_actions=int(decision.max_actions or 200))
            self.log(f"[runner]   -> {step.skill} {inputs}")
            result = self.registry.get(step.skill)(ctx, **inputs)
            results.append(result)
            last = result
            self._actions += int(result.actions or 0)
            if result.after.get("wins"):
                self._battles += int(result.after["wins"])
            if result.escalation == "pause" or result.status is Status.BLOCKED:
                if step.required:
                    break
        return results, last

    def _apply_skill_observations(self, world: GameWorldState,
                                 results: Iterable[SkillResult]) -> GameWorldState:
        """Fold facts a skill observed *during* execution back into the world.

        A battle is where the ally level is actually visible (``LV:33`` in the
        HUD); without this the run would know the level only from the pet panel,
        which sits behind an icon whose position moves between scenes.
        """

        from ..world.model import PetState

        changed = False
        for result in results:
            after = getattr(result, "after", None) or {}
            level = after.get("ally_level")
            if level:
                name = world.active_pet or "烈火猴"
                evidence = Evidence.live(f"battle HUD ally LV:{int(level)}",
                                         detail=f"skill {result.skill}")
                pet = world.pets.get(name) or PetState(name=name)
                pet.level = int(level)
                pet.evidence = evidence
                pet.active = True
                world.pets[name] = pet
                world.active_pet = name
                world.set_fact(f"pets.{name}.level", int(level), evidence)
                world.set_fact("state.active_pet_level_known", True, evidence)
                changed = True
            unlocked = after.get("unlocked")
            if isinstance(unlocked, Mapping):
                for key, value in unlocked.items():
                    world.set_fact(f"unlocked.{key}", value,
                                   Evidence.live(f"skill {result.skill}",
                                                 detail=str(result.proof)[:200]))
                    world.unlocked[str(key)] = Fact(value, Evidence.live(
                        f"skill {result.skill}", detail=str(result.proof)[:200]))
                    changed = True
                    world.notes.append(f"unlocked: {key} = {value}")
            xp_to_next = after.get("xp_to_next")
            if xp_to_next is not None:
                world.observations["pet_xp_to_next"] = float(xp_to_next)
                world.notes.append(f"battle UI: 离升级还需经验 {int(float(xp_to_next))}")
        if changed:
            world.notes.append(
                f"level verified from battle HUD: "
                f"{world.active.level if world.active else '?'}")
            self._last_world = world
        return world

    def verify_node(self, node: GoalNode) -> tuple[bool | None, str, GameWorldState]:
        """Re-observe and evaluate the node's completion predicate on live evidence."""

        world = self.observe()
        assert self.graph is not None and self.store is not None
        if self.run_id:
            self.store.save_world(world, run_id=self.run_id, note=f"verify {node.id}")
        status = self.graph.status_of(node.id, world=world, completed=self.completed_nodes())
        evidence = self.graph._completion_result(node, world)  # noqa: SLF001 - same package
        return (status == "completed"), evidence[1], world

    # -- the loop ---------------------------------------------------------- #
    def run(self) -> dict[str, Any]:
        assert self.store is not None and self.graph is not None
        cfg = self.config
        self._started = time.monotonic()
        self.control.clear()
        if self.run_id and self.store.get_run(self.run_id):
            run_id = self.run_id                     # resuming an existing run
        else:
            run_id = self.store.start_run(cfg.goal, cfg.planner, budgets=cfg.budgets,
                                          options=cfg.to_dict())
        self.run_id = run_id
        self.store.update_run(run_id, status="running")
        self.store.append_event(run_id, "run.started",
                                f"goal={cfg.goal} planner={cfg.planner} dry_run={cfg.dry_run}",
                                cfg.to_dict())
        self.log(f"[runner] run {run_id}: goal={cfg.goal} planner={cfg.planner} "
                 f"dry_run={cfg.dry_run} budgets={cfg.budgets}")

        end_reason = "unknown"
        last_result: dict[str, Any] | None = None
        try:
            while True:
                stop = self._budget_exhausted()
                if stop:
                    end_reason = stop
                    break
                if self.control.stop_requested():
                    end_reason = "stop requested"
                    break
                if self.control.wait_while_paused(
                        on_pause=lambda: (self.log("[runner] PAUSED (control file)"),
                                          self.store.update_run(run_id, status="paused")),
                        on_resume=lambda s: (self.log(f"[runner] RESUMED after {s:.1f}s"),
                                             self.store.update_run(run_id, status="running"))):
                    pass

                self._loops += 1
                world = self.observe()
                if not self._pet_level_known(world):
                    world = self.preflight(world)
                self.store.save_world(world, run_id=run_id, note=f"loop {self._loops} observe")
                self.store.update_run(run_id, node_state="observed",
                                      current_node=str(world.fact("session.scene") or ""))
                events = list(reversed(self.store.events(run_id, limit=8)))
                request = self.build_request(world, events, last_result)
                try:
                    decision = self.decide(request)
                except PlannerError as exc:
                    self._errors += 1
                    self.store.append_event(run_id, "planner.error", str(exc))
                    self.store.update_run(run_id, last_error=str(exc))
                    last_result = {"error": str(exc)}
                    continue

                if decision.is_refusal:
                    end_reason = "planner refused: " + (decision.stop_reason
                                                        or decision.human_question or "no reason")
                    self.store.append_event(run_id, "planner.refused", end_reason,
                                            {"decision": decision.to_dict()})
                    nxt = self.next_observations(world)
                    self.store.update_run(run_id, status="blocked", stop_reason=end_reason,
                                          next_recovery_action=(nxt[0]["node"] if nxt else None))
                    if nxt:
                        self.store.append_event(run_id, "plan.next_observation",
                                                f"{nxt[0]['node']}: {nxt[0]['reasons'][0]}",
                                                {"candidates": nxt})
                    self._next_observations = nxt
                    self.log(f"[runner] planner refused: {end_reason}")
                    if nxt:
                        self.log("[runner] unblocked by observing: " + "; ".join(
                            f"{c['node']} (" + ", ".join(c["reasons"][:1]) + ")" for c in nxt[:3]))
                    break

                refusal = self.gate(decision)
                if refusal:
                    self._errors += 1
                    self.store.append_event(run_id, "gate.refused", refusal,
                                            {"decision": decision.to_dict()})
                    self.store.update_run(run_id, last_error=refusal)
                    last_result = {"gate_refusal": refusal}
                    self.log(f"[runner] gate refused decision: {refusal}")
                    continue

                node = self.graph.node(decision.selected_node)
                self.store.append_event(run_id, "node.selected",
                                        f"{node.id}: {decision.reason}",
                                        decision.to_dict())
                self.store.set_node_state(run_id, node.id, "in_progress",
                                          detail={"decision": decision.to_dict()})
                self.store.update_run(run_id, current_node=node.id, node_state="in_progress")
                self.log(f"[runner] node {node.id} ({node.title}) — {decision.reason}")

                results, payload = self.execute_node(node, world, decision)
                world = self._apply_skill_observations(world, results)
                last_result = {
                    "node": node.id,
                    "decision": decision.to_dict(),
                    "results": [r.to_dict() for r in results],
                }
                for r in results:
                    self.store.append_event(run_id, f"skill.{r.status.value.lower()}",
                                            f"{r.skill}: {r.proof or r.reason}", r.to_dict())
                # heartbeat stats: a 45-minute run must be observable while it
                # runs, not only after it stops
                self.store.update_run(run_id, stats={
                    "battles": self._battles, "actions": self._actions,
                    "errors": self._errors, "loops": self._loops,
                    "elapsed_s": round(self.elapsed_s, 1)})

                # A hard blocker (no usable pet) is not an error to retry: stop
                # cleanly, record the recovery action, and leave the run resumable.
                hard = next((r for r in results
                             if r.status is Status.BLOCKED
                             and isinstance(r.data, Mapping)
                             and r.data.get("recovery")), None)
                if hard is not None:
                    end_reason = f"blocked: {hard.reason}"
                    self.store.set_node_state(run_id, node.id, "blocked",
                                              proof=hard.reason,
                                              detail={"recovery": hard.data.get("recovery"),
                                                      "results": last_result["results"]})
                    self.store.update_run(run_id, next_recovery_action=hard.data.get("recovery"),
                                          stop_reason=end_reason)
                    self.store.append_event(run_id, "run.blocked", end_reason,
                                            {"recovery": hard.data.get("recovery")})
                    self.log(f"[runner] BLOCKED: {end_reason} "
                             f"(recovery: {hard.data.get('recovery')})")
                    break

                if self.config.dry_run or not self.config.allow_input:
                    end_reason = "dry run complete (no input was sent)"
                    self.store.set_node_state(run_id, node.id, "planned",
                                              detail={"results": last_result["results"]})
                    self.store.checkpoint(run_id, f"dry-run {node.id}", world,
                                          payload=last_result, verified=False)
                    break

                completed, proof, after = self.verify_node(node)
                self.store.save_world(after, run_id=run_id, note=f"after {node.id}")
                if completed:
                    self.store.set_node_state(run_id, node.id, "completed", proof=proof,
                                              detail={"results": last_result["results"]})
                    self.store.checkpoint(run_id, f"completed {node.id}", after,
                                          payload={"proof": proof}, verified=True)
                    self.store.append_event(run_id, "node.completed", f"{node.id}: {proof}")
                    self.log(f"[runner] node COMPLETE {node.id}: {proof}")
                    end_reason = "goal progressed: node completed"
                    if self._all_executable_done():
                        end_reason = "all executable nodes complete"
                        break
                    continue
                self._node_attempts[node.id] = self._node_attempts.get(node.id, 0) + 1
                self.store.bump_attempt(run_id, node.id)
                self.store.set_node_state(run_id, node.id, "attempted", proof=proof,
                                          detail={"results": last_result["results"]})
                self.store.checkpoint(run_id, f"attempted {node.id}", after,
                                      payload={"proof": proof}, verified=False)
                failed_required = any(
                    r.status in (Status.FAIL, Status.BLOCKED) for r in results)
                if failed_required:
                    self._errors += 1
                self.log(f"[runner] node not complete yet: {proof}")
                # A grind node legitimately takes several attempts (each attempt is
                # one playbook pass, e.g. 10 battles).  The cap exists to stop a
                # *stuck* node, not to stop slow progress, so it scales with budget.
                attempt_cap = max(4, int(self.config.max_battles / 8) + 2, self.config.max_loops // 4)
                if self._node_attempts[node.id] > attempt_cap:
                    end_reason = (f"node {node.id} attempted {attempt_cap} times "
                                  f"without the completion predicate turning true")
                    break
        except KeyboardInterrupt:
            end_reason = "interrupted (SIGINT)"
        except Exception as exc:  # noqa: BLE001 - a crash must still checkpoint
            end_reason = f"crashed: {type(exc).__name__}: {exc}"
            self.store.append_event(run_id, "run.crash", end_reason,
                                    {"traceback": traceback.format_exc()[-2000:]})
            self.log(f"[runner] {end_reason}")
        finally:
            world = self._last_world
            if world is not None:
                try:
                    self.store.checkpoint(run_id, f"final: {end_reason}", world,
                                          payload={"end_reason": end_reason}, verified=False)
                except Exception:  # noqa: BLE001
                    pass
            self.store.append_event(run_id, "run.stopped", end_reason)
            self.store.update_run(run_id, status="paused" if "stop requested" in end_reason
                                  else "finished", stop_reason=end_reason,
                                  stats={"battles": self._battles, "actions": self._actions,
                                         "errors": self._errors, "loops": self._loops,
                                         "elapsed_s": round(self.elapsed_s, 1)})
            self.log(f"[runner] stopped: {end_reason} "
                     f"(battles={self._battles} actions={self._actions} "
                     f"errors={self._errors} {self.elapsed_s/60:.1f} min)")
        return self.summary()

    def _bootstrap_candidates(self, world: GameWorldState) -> list[str]:
        """Executable nodes whose only blocker is a fact their playbook observes."""

        assert self.graph is not None
        done = self.completed_nodes(world)
        out: list[str] = []
        for node in self.graph:
            if not node.executable or node.id in done:
                continue
            if not playbooks.has_playbook(node.id):
                continue
            reasons = self.graph.unmet(node.id, world=world, completed=done)
            if reasons and all(_looks_unobserved(r) for r in reasons):
                out.append(node.id)
        return out

    def next_observations(self, world: GameWorldState) -> list[dict[str, Any]]:
        """What the run must *look at* before anything can be scheduled.

        A refusal is not a dead end: when every remaining blocker is an
        unobserved fact, the honest next action is an observation, not a guess.
        This is what a dry run prints (and what the next real run performs).
        """

        assert self.graph is not None
        done = self.completed_nodes(world)
        out: list[dict[str, Any]] = []
        for node in sorted(self.graph, key=lambda n: (-n.priority, n.id)):
            if not node.executable:
                continue
            if self.graph.status_of(node.id, world=world, completed=done) == "completed":
                continue
            reasons = self.graph.unmet(node.id, world=world, completed=done)
            if not reasons:
                continue
            if all(_looks_unobserved(r) for r in reasons):
                out.append({"node": node.id, "title": node.title,
                            "playbook": playbooks.has_playbook(node.id),
                            "reasons": reasons[:3]})
        return out

    def _all_executable_done(self) -> bool:
        assert self.graph is not None
        done = self.completed_nodes()
        return all(n.id in done for n in self.graph if n.executable)

    # -- reporting --------------------------------------------------------- #
    def summary(self) -> dict[str, Any]:
        assert self.store is not None and self.run_id is not None
        run = self.store.get_run(self.run_id) or {}
        nodes = self.store.node_states(self.run_id)
        return {
            "run_id": self.run_id,
            "goal": run.get("goal"),
            "planner": run.get("planner"),
            "status": run.get("status"),
            "stop_reason": run.get("stop_reason"),
            "current_node": run.get("current_node"),
            "stats": run.get("stats"),
            "budgets": run.get("budgets"),
            "nodes": {k: v.get("status") for k, v in nodes.items()},
            "proofs": {k: v.get("proof") for k, v in nodes.items() if v.get("proof")},
            "planner_degraded": self._planner_degraded,
            "next_observations": self._next_observations,
            "battle_ready": self._can_battle,
            "world": None if self._last_world is None else self._last_world.summary(),
            "notes": [] if self._last_world is None else list(self._last_world.notes)[-6:],
        }

    def report_markdown(self) -> str:
        assert self.store is not None and self.run_id is not None
        s = self.summary()
        lines = [
            f"# Goal run `{s['run_id']}`",
            "",
            f"- goal: **{s['goal']}**  planner: **{s['planner']}**  status: **{s['status']}**",
            f"- stopped because: {s['stop_reason']}",
            f"- stats: {json.dumps(s['stats'], ensure_ascii=False)}",
        ]
        if s.get("planner_degraded"):
            lines.append(f"- planner degraded: {s['planner_degraded']}")
        lines += ["", "## Nodes", ""]
        for node_id, status in sorted((s.get("nodes") or {}).items()):
            proof = (s.get("proofs") or {}).get(node_id) or ""
            lines.append(f"- `{node_id}` -> **{status}** {('— ' + proof) if proof else ''}")
        ck = self.store.last_checkpoint(self.run_id)
        if ck:
            lines += ["", "## Last checkpoint", "",
                      f"- {ck.get('ts')} `{ck.get('label')}` verified={ck.get('verified')}"]
        return "\n".join(lines) + "\n"


def open_runner(config: RunConfig, *, log: Any = print) -> GoalRunner:
    """Build a runner with a live session when input is allowed."""

    from ..live import LiveSession

    runner = GoalRunner(config, log=log)
    runner.open()
    # Observation is always allowed (it is read-only screenshots); input is not.
    # A dry run that could not even look would be useless: the whole point is to
    # show what the agent *would* decide from the real account state.
    try:
        runner.session = LiveSession(
            dry_run=config.dry_run, enable_input=config.allow_input and not config.dry_run
        ).open()
    except Exception as exc:  # noqa: BLE001 - no game window is a valid dry-run state
        log(f"[runner] no live session ({exc}); running observation-free")
        runner.session = None
    return runner


__all__ = ["RunConfig", "GoalRunner", "open_runner", "DEFAULT_DB", "DEFAULT_AI_DIR"]


_UNOBSERVED = (
    "UNKNOWN", "unobserv", "unset", "not observed", "no active pet observed",
    "no observation", "never observed", "not live-verified", "not read",
    "never read", "not surveyed",
)


def _looks_unobserved(reason: str) -> bool:
    """True when a blocker is "we never looked", not "it is impossible"."""

    return any(n in reason for n in _UNOBSERVED)
