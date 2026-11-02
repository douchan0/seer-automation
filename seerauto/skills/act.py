"""Action skills: the verified, semantic operations the runner may execute.

Every skill here is a *closed loop*, not a click helper:

* it observes before acting (`before`),
* it acts only on something it can see (a matched template, an OCR box),
* it observes again afterwards and reports whether the world actually changed
  (`after`, `changed`, `proof`),
* it stops instead of retrying blindly, and says why.

The state table and the pacing rules come from live Round-4 verification
(``docs/progress.md``); the coordinates are window-local ``game_px`` of the 2x
capture, never global screen points, and every entry-point click is verified by
observing the panel it is supposed to open.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Mapping, Sequence

from .base import Skill, SkillContext, SkillKind, SkillResult, SkillSpec, Status
from ..live import LiveError, TH_FIGHT, TH_VICTORY
from ..world.model import Evidence, Source, Scene, now_iso

# --------------------------------------------------------------------------- #
# Window-local entry points (game_px).  These are *targets of verified clicks*:
# each one is confirmed afterwards by the panel/scene it must produce, so a
# stale coordinate shows up as a BLOCKED result instead of a silent misfire.
# --------------------------------------------------------------------------- #
ICON = {
    "spt": (2151, 347),
    "petbag": (1945, 1390),
    "bag": (1440, 1390),
    "map": (1206, 1356),
    "activity": (2020, 1390),
    "storage": (1822, 1390),
    "daily": (1320, 1390),
}

#: Panels are recognised by an OCR word that only that panel has.  `None` means
#: the panel has no reliable text marker and must be verified another way.
#: Known full-screen panels and where their close button sits.  The bottom bar
#: shifts between scenes, but a panel's own close button does not move with the
#: window size — and every close is *verified* by the marker disappearing, so a
#: stale entry shows up as a failed close instead of a silent misfire.
PANEL_CLOSERS: dict[str, tuple[tuple[str, ...], tuple[int, int]]] = {
    "spt": (("星际挑战终端", "挑战入口", "共73个入口"), (2095, 346)),
    "map": (("全部区域", "帕诺星系", "卡兰星系"), (2006, 474)),
    "chat": (("公屏聊天",), (2186, 580)),
    "petbag": (("升级所需经验值", "个体："), (1171, 496)),
    "shop": (("商城", "购买"), (2006, 474)),
}

PANEL_MARKERS = {
    "spt": ("星际挑战终端", "SPT 挑战", "SPT挑战", "挑战入口"),
    "petbag": ("升级所需经验值", "个体：", "性格："),
    "bag": ("背包", "物品", "道具"),
    "map": ("帕诺星系", "卡兰星系", "星际地图", "全部区域"),
}

CONFIRM_WORDS = ("确认", "确定", "我已知晓", "我知道了", "关闭")
CANCEL_WORDS = ("取消", "返回")


def ensure_world(ctx: SkillContext, *, attempts: int = 3) -> bool:
    """Close any recognised panel until the plain world is visible.

    A leftover panel is the single most common cause of "the click did nothing":
    every later click lands on the panel instead of the game world.
    """

    if ctx.session is None:
        return False
    for _ in range(attempts):
        try:
            items = _ocr(ctx.session, min_conf=0.35)
        except Exception:  # noqa: BLE001
            return False
        text = " ".join(str(i.get("text") or "") for i in items)
        closed_any = False
        for _name, (markers, close_px) in PANEL_CLOSERS.items():
            if any(m in text for m in markers):
                ok, _why = _click(ctx, close_px, f"close panel {_name}")
                if ok:
                    closed_any = True
                    time.sleep(0.8)
                break
        if not closed_any:
            return True
    return True


def _ocr(session: Any, *, min_conf: float = 0.3) -> list[dict[str, Any]]:
    return session.ocr(min_conf=min_conf)


def _find(items: Iterable[Mapping[str, Any]], needles: Iterable[str], *,
          exact: bool = False) -> dict[str, Any] | None:
    for it in items:
        text = str(it.get("text") or "").strip()
        for needle in needles:
            if (text == needle) if exact else (needle in text):
                return dict(it)
    return None


def _has_text(items: Iterable[Mapping[str, Any]], needles: Iterable[str]) -> str | None:
    hit = _find(items, needles)
    return None if hit is None else str(hit.get("text"))


class _Base(Skill):
    """Shared plumbing: OCR/wait helpers and a single guarded click path."""

    requires_live = True

    def guard(self, ctx: SkillContext) -> SkillResult | None:
        if ctx.session is None:
            return SkillResult.blocked(self.id, "no live session (offline or dry run)",
                                       escalation="pause")
        return None

    def click_and_check(self, ctx: SkillContext, px: tuple[float, float],
                        reason: str) -> tuple[bool, str]:
        """Click, then let the caller observe.  Never swallows a refusal."""

        try:
            ctx.session.click_px(px[0], px[1], reason)
            return True, ""
        except LiveError as exc:
            return False, f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# navigation
# --------------------------------------------------------------------------- #
class OpenPanel(_Base):
    spec = SkillSpec(
        id="game.open_panel",
        kind=SkillKind.ACT,
        summary="打开一个已知面板（SPT/精灵背包/背包/地图）并验证它真的打开了",
        inputs={"panel": {"type": "str", "required": True}},
        outputs={"opened": "bool", "scene": "str"},
        max_duration_s=20.0,
        max_actions=2,
        requires_input=True,
        verified="real",
        evidence=("artifacts/round5/01_spt_panel.png", "artifacts/round5/04_pet_panel.png"),
    )

    def run(self, ctx: SkillContext, panel: str = "") -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        if panel not in ICON:
            return SkillResult.fail(self.id, f"unknown panel {panel!r}; known: {sorted(ICON)}")
        ensure_world(ctx)
        # A modal (the plain-text 「恭喜你得到了N个积累经验」 style) swallows the icon
        # click and the panel then "never appears".  Clear modals first, and give
        # the whole open one retry before declaring failure.
        for attempt in range(2):
            if attempt:
                _dismiss_dialogs(ctx, limit=3)
                ensure_world(ctx)
            ok, why = self.click_and_check(ctx, ICON[panel], f"open {panel}")
            if not ok:
                return SkillResult.blocked(self.id, f"click refused: {why}", escalation="pause")
            deadline = time.perf_counter() + 4.0
            seen = ""
            tried = 0
            while time.perf_counter() < deadline:
                items = _ocr(ctx.session)
                tried += 1
                seen = _has_text(items, PANEL_MARKERS.get(panel, ())) or ""
                if seen:
                    break
                # keep looking, but if a known confirm button shows up, it is
                # covering the panel we just opened
                if _find(items, CONFIRM_WORDS) is not None:
                    _dismiss_dialogs(ctx, limit=2)
                time.sleep(0.4)
            if seen:
                break
        if not seen:
            return SkillResult(
                skill=self.id, status=Status.FAIL, changed=False,
                reason=f"{panel} panel did not appear after click (no marker text)",
                after={"panel": panel, "marker": None}, actions=1,
                proof="panel marker not found",
            )
        return SkillResult.pass_(
            self.id, proof=f"panel {panel} visible (marker {seen!r})",
            after={"panel": panel, "marker": seen}, actions=1,
            data={"ocr_passes": tried},
        )


class ClosePanel(_Base):
    spec = SkillSpec(
        id="game.close_panel",
        kind=SkillKind.ACT,
        summary="用 ESC 关闭当前面板/弹窗并确认画面回到世界或战斗态",
        inputs={},
        max_duration_s=10.0,
        max_actions=2,
        requires_input=True,
        verified="unverified",
    )

    def run(self, ctx: SkillContext) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        before = ctx.session.scene().scene
        items = _ocr(ctx.session, min_conf=0.35)
        text = " ".join(str(i.get("text") or "") for i in items)
        recognised = [name for name, (markers, _px) in PANEL_CLOSERS.items()
                      if any(m in text for m in markers)]
        ensure_world(ctx)
        after = ctx.session.scene().scene
        return SkillResult(
            skill=self.id,
            status=Status.PASS,
            reason=f"closed {recognised or 'no recognised panel'}",
            before={"scene": before.value},
            after={"scene": after.value},
            changed=before != after,
            proof=f"scene {before.value} -> {after.value}",
            actions=1,
        )


class NavigateSptBoss(_Base):
    spec = SkillSpec(
        id="game.navigate_spt_boss",
        kind=SkillKind.ACT,
        summary="打开 SPT 面板，按首领名字找到它，点击该卡片的「前往」并确认离开主城",
        inputs={"boss": {"type": "str", "required": True}},
        outputs={"location": "str"},
        preconditions=("scene.world",),
        max_duration_s=40.0,
        max_actions=3,
        requires_input=True,
        verified="real",
        evidence=("artifacts/round5/01_spt_panel.png",),
    )

    def run(self, ctx: SkillContext, boss: str = "") -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        opened = OpenPanel()(ctx, panel="spt")
        if not opened.ok:
            return SkillResult.blocked(self.id, f"cannot open SPT panel: {opened.reason}",
                                       escalation="pause")

        # The panel lists cards as (name, state, 前往) in one row.  Find the row
        # by the boss name's y, then take the nearest 前往 on the same row, and
        # only accept it when it is to the right of the name -- that ordering is
        # what makes this layout-driven rather than coordinate-driven.
        items = _ocr(ctx.session)
        name_hit = _find(items, (boss,), exact=True) or _find(items, (boss,))
        if name_hit is None:
            return SkillResult.fail(self.id, f"boss {boss!r} not visible in SPT panel",
                                    data={"texts": [i["text"] for i in items][:20]})
        nx, ny = name_hit["px"]
        # Card geometry measured on the live panel: the 前往 button sits one
        # card-row below the boss name (~150 px down, ~230 px right), so the
        # candidate window is a rectangle below/right of the name rather than
        # the same baseline.  Verified on 蘑菇怪 (前往 (1017,807)) and
        # 钢牙鲨 (前往 (1535,809)) on 2026-11-03.
        candidates = []
        for it in items:
            if "前往" not in str(it["text"]):
                continue
            x, y = it["px"]
            dy, dx = y - ny, x - nx
            if 60 <= dy <= 280 and -80 <= dx <= 480:
                candidates.append((abs(dy - 153) + abs(dx - 236) / 10.0, it))
        if not candidates:
            return SkillResult.fail(
                self.id, f"no 前往 button found on the {boss!r} row",
                data={"name_px": [round(nx), round(ny)]})
        candidates.sort(key=lambda c: c[0])
        go = candidates[0][1]
        ok, why = self.click_and_check(ctx, go["px"], f"前往 {boss}")
        if not ok:
            return SkillResult.blocked(self.id, f"click refused: {why}", escalation="pause")

        # Confirmed by observation: the SPT panel disappears and the scene's
        # templates stop reporting a panel.  A teleport takes 2-5 s.
        deadline = time.perf_counter() + 15.0
        left = False
        while time.perf_counter() < deadline:
            time.sleep(0.5)
            items2 = _ocr(ctx.session, min_conf=0.4)
            if _has_text(items2, ("挑战入口", "共73个入口", "星际挑战终端")) is None:
                left = True
                break
        if not left:
            return SkillResult.fail(self.id, "SPT panel never closed; teleport did not happen")
        time.sleep(1.5)
        scene = ctx.session.scene().scene
        return SkillResult.pass_(
            self.id,
            proof=f"{boss} 前往 clicked at ({go['px'][0]:.0f},{go['px'][1]:.0f}), "
                  f"panel closed, scene={scene.value}",
            before={"scene": "PANEL"}, after={"scene": scene.value, "boss": boss},
            actions=2,
        )


# --------------------------------------------------------------------------- #
# battle
# --------------------------------------------------------------------------- #
class RunBattle(_Base):
    spec = SkillSpec(
        id="game.run_battle",
        kind=SkillKind.ACT,
        summary="连战：进入战斗→逐回合出招→确认胜利→读取奖励，返回可验证的战果",
        inputs={
            "battles": {"type": "int", "required": False},
            "max_seconds": {"type": "float", "required": False},
            "monster": {"type": "str", "required": False},
        },
        outputs={"wins": "int", "losses": "int", "rewards": "list", "xp": "float"},
        preconditions=("scene.world_or_battle",),
        postconditions=("battle_result_observed",),
        max_duration_s=900.0,
        max_actions=400,
        requires_input=True,
        verified="real",
        evidence=("docs/progress.md#round-4", "bench/results/round4_3battles_*.json"),
    )

    def run(self, ctx: SkillContext, battles: int = 1, max_seconds: float = 240.0,
            monster: str = "蘑菇怪") -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        s = ctx.session
        wins = 0
        losses = 0
        started = time.perf_counter()
        rewards: list[dict[str, Any]] = []
        xp_total = 0.0
        actions = 0
        last_state: Scene | None = None
        pending_win = False
        stall_deadline = started + max_seconds
        last_progress = time.perf_counter()
        end_reason = "budget"

        def act(px: tuple[float, float], why: str) -> bool:
            nonlocal actions
            ok, _why = self.click_and_check(ctx, px, why)
            if ok:
                actions += 1
            return ok

        hunt_started: float | None = None
        last_ocr = 0.0
        ally_level: int | None = None
        ally_hp: tuple[int, int] | None = None
        xp_to_next: int | None = None
        hud_attempts = 0
        hunt_seconds = float(ctx.options.get("hunt_seconds", 20.0)) if ctx.options else 20.0
        engage_tries = 0
        stall_seconds = float(ctx.options.get("stall_seconds", 150.0)) if ctx.options else 150.0

        while wins < battles and time.perf_counter() < stall_deadline:
            if ctx.options.get("control") and ctx.options["control"].stop_requested():
                end_reason = "stop requested"
                break
            if ctx.options.get("control") and ctx.options["control"].paused():
                ctx.options["control"].wait_while_paused()
                last_state = None      # resynchronise after a pause
                continue

            frame = s.latest()
            probe = s.scene(frame, previous=last_state)
            state = probe.scene
            if state is not last_state and state is not Scene.WORLD:
                last_progress = time.perf_counter()
            # Progress = a win or a fresh engagement.  Measuring "time since the
            # last non-WORLD state" instead cut passes short during a normal
            # hunt+fight cycle, which cost real throughput.
            if time.perf_counter() - last_progress > stall_seconds:
                end_reason = f"no progress for {stall_seconds:.0f}s"
                break

            if state is Scene.RESULT:
                if pending_win:
                    time.sleep(0.1)
                    continue
                pending_win = True
                act(probe.points["confirm"], "confirm victory")
                # a victory only counts once the glyph is gone
                gone = s.wait_for(
                    lambda f: s.score(f, "live_victory_word", (980, 640, 1430, 790)).score < TH_VICTORY,
                    timeout_s=8.0)
                if not gone:
                    end_reason = "victory panel would not dismiss (confirm click had no effect)"
                    pending_win = False
                    break
                wins += 1
                last_progress = time.perf_counter()
                # the XP / drop dialog follows the victory panel
                time.sleep(0.6)
                reward = self._read_reward(ctx)
                if reward:
                    rewards.append(reward)
                    xp_total += float(reward.get("xp") or 0.0)
                pending_win = False
                last_state = None
                continue

            if state is Scene.DIALOG:
                act(probe.points["confirm"], "dismiss dialog")
                time.sleep(0.7)
                last_state = None
                continue

            if state is Scene.BATTLE:
                # The battle HUD is the cheapest *verified* level readout: ally
                # `LV:33` sits at (556,261) next to the HP bar.  The HUD animates
                # in, so the first OCR can miss it — retry while we stay in the
                # same battle instead of giving up on the whole fight.
                if ally_level is None and hud_attempts < 3:
                    hud_attempts += 1
                    lvl, hp = self._read_battle_hud(s)
                    ally_level = lvl or ally_level
                    ally_hp = hp or ally_hp
                act(probe.points["fight"], "attack")
                before = s.log_signature(frame)
                advanced = s.wait_log_change(before, timeout_s=8.0) or s.wait_for(
                    lambda f: s.score(f, "live_victory_word", (980, 640, 1430, 790)).score >= TH_VICTORY,
                    timeout_s=0.1)
                if advanced:
                    s.wait_settle(stable_ms=220.0, timeout_s=3.0)
                last_state = state
                continue

            # WORLD: engage a monster we can actually see.
            #
            # The sprite is animated, so an exact template match is only a
            # *candidate generator* (cross-frame scores of the same 蘑菇怪 range
            # 0.47-0.71 depending on pose).  The click is therefore always
            # followed by a verification: if the battle UI does not appear, the
            # click merely walked the character and the hunt continues.  That is
            # why a low floor is safe here and a high one would simply starve.
            mob = self._best_monster(s)
            if mob is not None and mob.score >= 0.55:
                engage_tries += 1
                hunt_started = None
                last_progress = time.perf_counter()
                hud_attempts = 0
                act(mob.point_px, f"engage {monster} ({mob.template} {mob.score:.2f})")
                in_battle = s.wait_for(
                    lambda f: s.score(f, "live_fight_btn", (1710, 1130, 2040, 1260)).score
                    >= TH_FIGHT - 0.10, timeout_s=6.0)
                if not in_battle:
                    self._last_engage_miss = getattr(self, "_last_engage_miss", 0) + 1
                    # Unknown screen: the click did not start a battle.  The most
                    # common cause is a modal dialog whose button has no template
                    # (the XP「恭喜你得到了N个积累经验」style).  One OCR look, then
                    # dismiss it if it names a known confirm button.  This is the
                    # escalation path that keeps a modal from silently eating a
                    # whole pass.
                    try:
                        items = _ocr(s, min_conf=0.35)
                    except Exception:  # noqa: BLE001
                        items = []
                    hit = _find(items, CONFIRM_WORDS)
                    if hit is not None:
                        _click(s, (hit["px"][0], hit["px"][1]),
                               f"dismiss modal {hit['text']!r}")
                        end_reason = f"dismissed modal {hit['text']!r} blocking the battle"
                        time.sleep(0.8)
                    time.sleep(0.4)
                last_state = None
                continue
            # Nothing engaged yet.  Monsters wander in and out of the visible
            # area and a teleport takes a few seconds, so hunt for a bounded
            # window instead of declaring failure on the first empty frame.
            if hunt_started is None:
                hunt_started = time.perf_counter()
            hunting = time.perf_counter() - hunt_started
            if hunting > hunt_seconds:
                end_reason = (f"no engageable monster within {hunt_seconds:.0f}s of hunting")
                break
            if hunting > 6.0 and (time.perf_counter() - last_ocr) > 8.0:
                last_ocr = time.perf_counter()
                items = _ocr(s, min_conf=0.4)
                texts = [str(i.get("text") or "") for i in items][:6]
                self._last_unknown_texts = texts
            time.sleep(0.25)
            if time.perf_counter() > stall_deadline:
                end_reason = "budget"
                break

        elapsed = time.perf_counter() - started
        changed = wins > 0
        proof = f"{wins} 场胜利, {losses} 场失败, 经验 +{xp_total:.0f} ({end_reason})"
        result = SkillResult(
            skill=self.id,
            status=Status.PASS if changed else Status.FAIL,
            reason=end_reason,
            after={"wins": wins, "losses": losses, "rewards": rewards, "xp": xp_total,
                   "ally_level": ally_level, "ally_hp": list(ally_hp) if ally_hp else None,
                   "xp_to_next": xp_to_next},
            changed=changed,
            proof=proof,
            actions=actions,
            elapsed_s=elapsed,
            data={"end_reason": end_reason, "monster": monster},
            evidence=[Evidence.live("live battle loop", detail=proof, observed_at=now_iso())],
        )
        if not changed:
            result.escalation = "pause"
        return result

    MONSTER_TEMPLATES = ("live_monster_moguguai", "live_monster_moguguai_alt")

    def __init__(self) -> None:
        super().__init__()
        self._last_xp_to_next: float | None = None

    def _best_monster(self, session: Any) -> Any:
        """Best candidate over every known monster sprite (max score)."""

        best = None
        for name in self.MONSTER_TEMPLATES:
            try:
                probe = session.score(session.latest(), name, (200, 340, 2260, 1330))
            except Exception:  # noqa: BLE001 - a missing template is not fatal
                continue
            if best is None or probe.score > best.score:
                best = probe
        return best

    @staticmethod
    def _read_battle_hud(session: Any) -> tuple[int | None, tuple[int, int] | None]:
        """Ally level + HP from one OCR of the battle HUD."""

        items: list[dict[str, Any]] = []
        for conf in (0.3, 0.2):
            try:
                items = session.ocr(min_conf=conf)
            except Exception:  # noqa: BLE001
                return None, None
            if items:
                try:
                    from .parsers import parse_level

                    if parse_level(items, side="ally") is not None:
                        break
                except Exception:  # noqa: BLE001
                    pass
        level = hp = None
        try:
            from .parsers import parse_hp, parse_level

            level = parse_level(items, side="ally")
            hp = parse_hp(items)
        except Exception:  # noqa: BLE001
            return None, None
        return level, hp

    def _read_reward(self, ctx: SkillContext) -> dict[str, Any] | None:
        """One OCR look for the 积累经验/掉落 dialog, then dismiss it."""

        s = ctx.session
        items = _ocr(s, min_conf=0.3)
        reward: dict[str, Any] = {}
        # Only real payouts count as XP.  「离升级还需经验：N」 looks identical to a
        # naive regex but means "still missing" — counting it inflated one pass to
        # 7478 XP when the pet had actually gained 848.  parsers.parse_reward
        # encodes that distinction (and is tested against real frames).
        try:
            from .parsers import parse_reward

            for entry in parse_reward(items):
                if entry.get("kind") == "经验" and entry.get("amount") is not None:
                    reward["xp"] = float(entry["amount"])
                    reward["raw"] = entry.get("raw", "")
                    break
        except Exception:  # noqa: BLE001 - parser is optional at runtime
            pass
        prev = self._last_xp_to_next
        for it in items:
            text = str(it.get("text") or "")
            if "离升级" in text and any(ch.isdigit() for ch in text):
                import re as _re

                m = _re.search(r"(\d+)", text)
                if m:
                    value = float(m.group(1))
                    reward["xp_to_next"] = value
                    # 「离升级还需经验」 only ever falls while grinding; a jump *up*
                    # means the requirement was reset, i.e. the pet levelled up.
                    # That is an observation, not a guess: it cannot happen for
                    # any other reason while the pet line is unchanged.
                    if prev is not None and value > prev + 50:
                        reward["level_up_detected"] = True
                        reward["xp_to_next_before"] = prev
                    self._last_xp_to_next = value
                    break
        # dismiss whatever dialog is still up so the next loop starts clean
        hit = _find(items, CONFIRM_WORDS)
        if hit is not None and not ctx.dry_run:
            act_ok, _ = self.click_and_check(ctx, hit["px"], f"dismiss {hit['text']!r}")
            if act_ok:
                time.sleep(0.6)
        return reward or None


def _dry_run_ready(ctx: SkillContext) -> bool:
    return ctx.dry_run or not ctx.allow_input


# --------------------------------------------------------------------------- #
# pet panel: reading the level (the only way to *verify* a level-up)
# --------------------------------------------------------------------------- #
class ReadPetLevel(_Base):
    spec = SkillSpec(
        id="game.read_active_pet_level",
        kind=SkillKind.ACT,
        summary="打开精灵背包，读出出战精灵的名字/等级/升级所需经验，再关闭面板",
        inputs={},
        outputs={"name": "str", "level": "int", "xp_to_next": "int"},
        max_duration_s=30.0,
        max_actions=3,
        requires_input=True,
        verified="real",
        evidence=("artifacts/round5/04_pet_panel.png",),
    )

    def run(self, ctx: SkillContext) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        s = ctx.session
        # Reward dialogs are delivered one at a time and can pop *after* the
        # panel opened, hiding it again.  So: open once, then read/clear/read
        # rather than toggling the panel (a second icon click closes it).
        if not act_open(ctx, "petbag"):
            _dismiss_dialogs(ctx)
            if not act_open(ctx, "petbag"):
                return SkillResult.blocked(self.id, "pet bag panel did not open",
                                           escalation="pause")
        name = level = xp = None
        items: list[dict[str, Any]] = []
        for attempt in range(3):
            time.sleep(0.45)
            items = _ocr(s, min_conf=0.3)
            name = level = xp = None
            for it in items:
                text = str(it.get("text") or "")
                if text.startswith("名字"):
                    name = text.split("：")[-1].split(":")[-1].strip()
                elif text.startswith("等级"):
                    level = _int_after(text)
                elif "升级所需经验值" in text:
                    xp = _int_after(text)
            if level is not None:
                break
            _dismiss_dialogs(ctx, limit=2)
        # close again so the world is left in a known state
        client = s.client
        try:
            client.send("key", code=53)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6)
        if level is None:
            return SkillResult.fail(self.id, "pet panel open but level text not readable",
                                    data={"texts": [i["text"] for i in items][:15]})
        return SkillResult.pass_(
            self.id,
            proof=f"{name or '?'} Lv{level} (升级所需经验 {xp})",
            after={"name": name, "level": level, "xp_to_next": xp},
            actions=1,
            data={"raw": [i["text"] for i in items][:20]},
            evidence=[Evidence.live(f"ocr:等级：{level}", detail=f"名字={name}, 升级所需经验={xp}")],
        )


def _dismiss_dialogs(ctx: SkillContext, *, limit: int = 3) -> int:
    """Dismiss up to ``limit`` known dialogs, returning how many were clicked."""

    clicked = 0
    for _ in range(limit):
        try:
            items = _ocr(ctx.session, min_conf=0.35)
        except Exception:  # noqa: BLE001
            return clicked
        hit = _find(items, CONFIRM_WORDS)
        if hit is None:
            return clicked
        ok, _ = _click(ctx, hit["px"], f"dismiss {hit['text']!r}")
        if not ok:
            return clicked
        clicked += 1
        time.sleep(0.7)
    return clicked


def _click(ctx: SkillContext, px: tuple[float, float], reason: str) -> tuple[bool, str]:
    try:
        ctx.session.click_px(px[0], px[1], reason)
        return True, ""
    except LiveError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _int_after(text: str) -> int | None:
    import re

    m = re.search(r"(\d+)", text.replace("：", ":").split(":")[-1])
    return None if m is None else int(m.group(1))


def act_open(ctx: SkillContext, panel: str) -> bool:
    res = OpenPanel()(ctx, panel=panel)
    return res.ok


# --------------------------------------------------------------------------- #
# generic dialog confirm
# --------------------------------------------------------------------------- #
class ConfirmDialog(_Base):
    spec = SkillSpec(
        id="game.confirm_known_dialog",
        kind=SkillKind.ACT,
        summary="确认只包含已知按钮文字的弹窗（确认/确定/我已知晓），未知弹窗不点",
        inputs={},
        max_duration_s=15.0,
        max_actions=2,
        requires_input=True,
        verified="real",
        evidence=("docs/progress.md#round-4",),
    )

    def run(self, ctx: SkillContext) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        items = _ocr(ctx.session)
        hit = _find(items, CONFIRM_WORDS)
        if hit is None:
            return SkillResult(skill=self.id, status=Status.SKIPPED,
                               reason="no known confirm button on screen", changed=False)
        ok, why = self.click_and_check(ctx, hit["px"], f"confirm {hit['text']!r}")
        if not ok:
            return SkillResult.blocked(self.id, f"click refused: {why}", escalation="pause")
        time.sleep(0.8)
        return SkillResult.pass_(self.id, proof=f"clicked {hit['text']!r}",
                                 after={"text": hit["text"]}, actions=1)


def build_act_skills() -> list[Skill]:
    return [
        OpenPanel(),
        ClosePanel(),
        NavigateSptBoss(),
        RunBattle(),
        ReadPetLevel(),
        ConfirmDialog(),
    ]


__all__ = [
    "OpenPanel",
    "ClosePanel",
    "NavigateSptBoss",
    "RunBattle",
    "ReadPetLevel",
    "ConfirmDialog",
    "build_act_skills",
    "ICON",
    "PANEL_MARKERS",
]
