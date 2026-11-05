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

import re
import time
from typing import Any, Iterable, Mapping, Sequence

from .base import Skill, SkillContext, SkillKind, SkillResult, SkillSpec, Status
from ..live import LiveError, TH_FIGHT, TH_VICTORY
from ..runner.battle_watch import (BattleLimits, BattleSnapshot, BattleWatchdog,
                                   append_snapshot)
from ..world.model import Evidence, Fact, Source, Scene, now_iso

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
#: Known full-screen panels and where their close button sits.
#:
#: NOTE: the chat 公屏聊天 header is *always* visible in the top-left of the
#: world view, so it must NOT be a closer marker: matching it made ensure_world
#: "close" a panel that was not open, three times, while the real 精灵仓库 panel
#: kept swallowing every click.  The bottom bar
#: shifts between scenes, but a panel's own close button does not move with the
#: window size — and every close is *verified* by the marker disappearing, so a
#: stale entry shows up as a failed close instead of a silent misfire.
PANEL_CLOSERS: dict[str, tuple[tuple[str, ...], tuple[int, int]]] = {
    "spt": (("星际挑战终端", "挑战入口", "共73个入口"), (2095, 346)),
    "map": (("啦公星", "全部区域", "帕诺星系", "卡兰星系"), (2008, 448)),
    "shop": (("商城", "购买"), (2006, 474)),
    "petpanel": (("升级所需经验值", "精灵仓库", "精英收藏", "放生仓库", "放入背包",
                  "全部精灵", "个体："), ((1141, 503), (2072, 312))),
    "event": (("万圣节活动", "本期回顾", "比巴卜酷滑"), (2113, 309)),
    "training": (("精灵模拟训练", "选择课程"), (1530, 544)),
    # The chat *header* is always on screen; only the expanded panel shows tabs.
    "chat_open": (("综合", "世界", "交易", "招募"), (2162, 587)),
    "promo": (("航行日志", "魔龙王", "立即前往"), (2100, 300)),
}

PANEL_MARKERS = {
    "spt": ("星际挑战终端", "SPT 挑战", "SPT挑战", "挑战入口"),
    "petbag": ("升级所需经验值", "个体：", "性格："),
    "bag": ("背包", "物品", "道具"),
    "map": ("帕诺星系", "卡兰星系", "星际地图", "全部区域"),
}

CONFIRM_WORDS = ("确认", "确定", "我已知晓", "我知道了", "关闭")

#: Phrases that identify a centre-screen modal whose 确认 button sits at a fixed
#: spot.  OCR sometimes misses 「确认」 itself (confidence dips on the small bold
#: glyph), so the body text is the more reliable detector.
MODAL_HINTS = ("你没有可出战", "您还没有加入战队", "恭喜你得到了", "请先", "无法",
               "不足", "已满", "超时", "掉线")

#: Close button of the pet bag panel (top-right of its list pane).  The storage
#: tab uses a different panel with a different corner, hence two constants.
PET_BAG_CLOSE_PX = (1149, 506)

#: The confirmation button of every centre-screen modal measured so far.
MODAL_CONFIRM_PX = (1200, 980)
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
            items = _ocr(ctx.session, min_conf=0.25)
        except Exception:  # noqa: BLE001
            return False
        text = " ".join(str(i.get("text") or "") for i in items)
        closed_any = False
        # The pet bag can render without any of its text markers (the detail pane
        # is sometimes blank), but it always shows HP rows like "0/95".  Two of
        # those on screen is the pet panel, and nothing else in this client
        # prints that shape twice.
        import re as _re

        hp_rows = [t for t in (str(i.get("text") or "") for i in items)
                   if _re.fullmatch(r"\s*\d+\s*/\s*\d+\s*", t)]
        if len(hp_rows) >= 2:
            closed = False
            for point in (PET_BAG_CLOSE_PX, (1141, 503)):
                ok, _why = _click(ctx, point, f"close pet panel at {point}")
                if not ok:
                    continue
                time.sleep(0.7)
                after = _ocr(ctx.session, min_conf=0.25)
                still = [str(i.get("text") or "") for i in after]
                if not any(_re.fullmatch(r"\s*\d+\s*/\s*\d+\s*", t) for t in still):
                    closed = True
                    break
            if closed:
                continue
        for _name, (markers, close_px) in PANEL_CLOSERS.items():
            if any(m in text for m in markers):
                # A panel's X moves: the pet panel grew a detail pane and its close
                # button jumped from (2072,312) to (1141,503).  Try every known point
                # and stop as soon as the panel markers are gone, instead of trusting
                # one coordinate forever.
                points = close_px if isinstance(close_px[0], (tuple, list)) else (close_px,)
                for point in points:
                    ok, _why = _click(ctx, point, f"close panel {_name} at {point}")
                    if not ok:
                        continue
                    closed_any = True
                    time.sleep(0.7)
                    after = _ocr(ctx.session, min_conf=0.25)
                    if not any(m in " ".join(str(i.get("text") or "") for i in after)
                               for m in markers):
                        break
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
        inputs={"boss": {"type": "str", "required": True},
                "category": {"type": "str", "required": False}},
        outputs={"location": "str"},
        preconditions=("scene.world",),
        max_duration_s=40.0,
        max_actions=3,
        requires_input=True,
        verified="real",
        evidence=("artifacts/round5/01_spt_panel.png",),
    )

    def run(self, ctx: SkillContext, boss: str = "", category: str = "") -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        opened = OpenPanel()(ctx, panel="spt")
        if not opened.ok:
            return SkillResult.blocked(self.id, f"cannot open SPT panel: {opened.reason}",
                                       escalation="pause")

        if category:
            # The panel filters by region; 谱尼 only exists under 太空站左翼, which is
            # an OCR-clicked category button rather than a coordinate.
            items0 = _ocr(ctx.session)
            cat = _find(items0, (category,))
            if cat is None:
                return SkillResult.fail(self.id, f"category {category!r} not on the SPT panel")
            ok, why = self.click_and_check(ctx, cat["px"], f"category {category}")
            if not ok:
                return SkillResult.blocked(self.id, f"click refused: {why}", escalation="pause")
            time.sleep(1.6)

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

        # A reward dialog from the PREVIOUS pass can still be on screen when this one
        # starts; the hunt then finds no monster for its whole timeout (observed:
        # 498 s, one battle, "no engageable monster within 20 s").  Clear first.
        try:
            _dismiss_dialogs(ctx, limit=3)
        except Exception:  # noqa: BLE001
            pass
        moves_used: list[str] = []
        hunt_started: float | None = None
        last_ocr = 0.0
        ally_level: int | None = None
        ally_hp: tuple[int, int] | None = None
        xp_to_next: int | None = None
        hud_attempts = 0
        hunt_seconds = float(ctx.options.get("hunt_seconds", 20.0)) if ctx.options else 20.0
        engage_tries = 0
        stall_seconds = float(ctx.options.get("stall_seconds", 150.0)) if ctx.options else 150.0
        hp_floor = float(ctx.options.get("hp_floor", 0.25)) if ctx.options else 0.25
        # Long-battle guards.  A truebody fight is 300-600 turns; a wild fight is 20
        # seconds.  Both run through this loop, so the budget is configurable and
        # defaults to "no turn cap" for the short fights that already work.
        opts = ctx.options or {}
        watch = BattleWatchdog(BattleLimits(
            max_turns=int(opts.get("battle_max_turns", 0) or 0),
            max_seconds=float(opts.get("battle_max_seconds", 0) or 0),
            # 0 = off.  The live fingerprint is coarse (ally HP is sampled a few
            # times, enemy_life is never filled), so a default stuck limit could
            # abort a verified-good fight and pause the night.  Truebody nodes turn
            # it on explicitly.
            max_stuck_turns=int(opts.get("battle_max_stuck_turns", 0) or 0),
            max_failures=int(opts.get("battle_max_failures", 0) or 0),
        ))
        battle_log = opts.get("battle_log_path")

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
            # Stop the pass before the pet faints.  Losing the last HP mid-pass is
            # what stranded the account on 2026-11-03: every later engage hit
            # 「你没有可出战的精灵哦」 and the recovery needs a human.  A pass that ends
            # at 25% HP is a *successful* pass; a pass that ends at 0 HP is a
            # stranded account.
            if ally_hp and ally_hp[1] and wins > 0:
                ratio = ally_hp[0] / float(ally_hp[1])
                if ratio <= hp_floor:
                    end_reason = (f"hp floor reached ({ally_hp[0]}/{ally_hp[1]} = "
                                  f"{ratio:.0%} <= {hp_floor:.0%}); stopping the pass "
                                  f"before the pet faints")
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
                    # The victory panel's 确认 drifts: it can sit at the bottom of the
                    # panel, inside a drop-list, or behind a second 「获得物品」 popup.
                    # Click what the OCR actually says before declaring a dead end.
                    for attempt in range(3):
                        items = s.ocr(min_conf=0.25)
                        btn = s.find_text(items, ("确认", "确定", "我知道了", "关闭"))
                        if btn:
                            act(btn["px"], "confirm victory (by text)")
                        else:
                            act((1200.0, 980.0 + 40 * attempt), "confirm victory (grid)")
                        if s.wait_for(
                                lambda f: s.score(f, "live_victory_word",
                                                  (980, 640, 1430, 790)).score < TH_VICTORY,
                                timeout_s=4.0):
                            gone = True
                            break
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
                if hunt_started is not None:
                    last_progress = time.perf_counter()   # a dialog is not a stall
                last_state = None
                continue

            if state is Scene.BATTLE:
                # The battle HUD is the cheapest *verified* level readout: ally
                # `LV:33` sits at (556,261) next to the HP bar.  The HUD animates
                # in, so the first OCR can miss it — retry while we stay in the
                # same battle instead of giving up on the whole fight.
                # The HUD used to be read at most three times and then frozen for the
                # rest of the pass, so the watchdog's "ally HP changed" signal never
                # fired and the HP-floor check worked off a stale number.  Re-read it
                # periodically (an OCR every 5 turns is cheap next to a 3 s turn).
                if (ally_level is None and hud_attempts < 3) or (watch.turns % 5 == 0):
                    hud_attempts += 1
                    lvl, hp = self._read_battle_hud(s)
                    ally_level = lvl or ally_level
                    ally_hp = hp or ally_hp
                # --- choose the move instead of letting the timer choose for us ---
                live_skills = self.read_skills(s)
                table = self._power_table(ctx)
                if live_skills and not table:
                    table = self.learn_power_table(ctx)
                best_now = self.pick_skill(live_skills, table) if live_skills else None
                if best_now is not None and best_now.get("pp", 0) <= 2:
                    # 精灵恢复 refills HP *and* uses for 20 XIN豆, so ending a pass one
                    # battle early is cheaper than stalling on an empty move list.
                    end_reason = (f"best move down to {best_now['pp']} uses; stopping the "
                                  f"pass so HP and PP can be restored together")
                    break
                if live_skills and self.pick_skill(live_skills, table) is None:
                    # Every move sits at 0/35 … 0/20.  The client will not advance and
                    # the run used to spin here until its stall timeout (150 s, twice in
                    # one night).  PP is restored by the same 精灵恢复 that heals HP, so
                    # end the pass and let the recovery step run.
                    end_reason = ("all moves out of uses (PP exhausted); stopping the "
                                  "pass so PP can be restored")
                    break
                if live_skills:
                    chosen = self.pick_skill(live_skills, table)
                    if chosen is not None:
                        moves_used.append(f"{chosen['name']}#{chosen.get('slot')}"
                                          f"(pp{chosen['pp']})")
                        act(chosen["px"], f"use {chosen['name']} "
                                          f"(power {chosen['power']}, pp {chosen['pp']})")
                    else:
                        act(probe.points["fight"], "attack (no usable move)")
                else:
                    # the command menu is showing: 战斗 opens the move list
                    act(probe.points["fight"], "open move list")
                    time.sleep(0.45)
                    live_skills = self.read_skills(s)
                    chosen = self.pick_skill(live_skills, self._power_table(ctx))
                    if chosen is not None:
                        moves_used.append(f"{chosen['name']}#{chosen.get('slot')}"
                                          f"(pp{chosen['pp']})")
                        act(chosen["px"], f"use {chosen['name']} "
                                          f"(power {chosen['power']}, pp {chosen['pp']})")
                before = s.log_signature(frame)
                advanced = s.wait_log_change(before, timeout_s=8.0) or s.wait_for(
                    lambda f: s.score(f, "live_victory_word", (980, 640, 1430, 790)).score >= TH_VICTORY,
                    timeout_s=0.1)
                if advanced:
                    s.wait_settle(stable_ms=220.0, timeout_s=3.0)
                # One snapshot per turn: this is the resume point if the process
                # dies mid-battle, and the evidence for "was this fight stuck?".
                snap = BattleSnapshot(
                    turn=watch.turns + 1,
                    scene=getattr(state, "value", str(state)),
                    ally_pet=ctx.world.active_pet,
                    ally_hp=ally_hp,
                    ally_level=ally_level,
                    last_move=moves_used[-1] if moves_used else None,
                    wins=wins,
                    losses=losses,
                    log_sig=str(before),
                )
                verdict = watch.observe(snap)
                if battle_log:
                    append_snapshot(str(battle_log), snap)
                if verdict.stop:
                    end_reason = verdict.reason
                    break
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
        proof = (f"{wins} 场胜利, {losses} 场失败, 经验 +{xp_total:.0f} ({end_reason}); "
                 f"moves: {','.join(moves_used[-12:]) or 'none read'}")
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

    #: The client labels a move's remaining uses 「使用次数：13/35」.  The battle panel
    #: is always on screen (no 战斗 press needed) — an earlier version looked for "PP"
    #: and therefore never found a single move.
    #: The battle move panel is a fixed 2x2 grid (measured on the 1200x800 client).
    #: The move NAMES OCR badly (「火花豆」 is 火花 plus its type icon), but the use
    #: counter 「使用次数：11/35」 reads reliably, so the slot — not the name — is the
    #: identity, and the name/power come from the pet panel table.
    SKILL_SLOTS = {1: (1200.0, 1246.0), 2: (1456.0, 1246.0),
                   3: (1200.0, 1368.0), 4: (1458.0, 1368.0)}
    #: Damage-first order used until a power table has been learned.
    DEFAULT_ORDER = (3, 1, 2, 4)
    _PP_RE = re.compile(r"(?:PP|使用次数)\s*[:：]?\s*(\d+)\s*/\s*(\d+)", re.I)

    @classmethod
    def read_skills(cls, session: Any) -> list[dict[str, Any]]:
        """Read the four move slots: remaining uses per slot.

        Returns ``[{slot, px, pp, pp_max}]``.  Power/name are filled in by
        :meth:`pick_skill` from the learned table, because the battle panel does not
        show 威力 at all.
        """

        items = session.ocr(min_conf=0.15)
        counters = []
        for it in items:
            m = cls._PP_RE.search(str(it.get("text") or ""))
            if m:
                counters.append((it["px"], int(m.group(1)), int(m.group(2))))
        cells: list[dict[str, Any]] = []
        for slot, (sx, sy) in cls.SKILL_SLOTS.items():
            best = None
            best_d = 1e9
            for (px, pp, pp_max) in counters:
                # Chebyshev distance: the four slots sit on a 256x122 grid, so a
                # Manhattan radius loose enough for the wide gap also lets slot 3
                # steal slot 1's counter (it did).
                d = max(abs(px[0] - sx), abs(px[1] - sy))
                if d < best_d and d < 120:
                    best_d = d
                    best = (pp, pp_max)
            if best is None:
                continue
            cells.append({"slot": slot, "px": (sx, sy), "pp": best[0],
                          "pp_max": best[1], "power": 0, "name": f"slot{slot}"})
        return cells

    @classmethod
    def learn_power_table(cls, ctx: SkillContext) -> dict[str, int]:
        """Ask a vision model to read the four move cells, once per run.

        The battle panel prints only 「使用次数」; the move name merges into its type
        icon (火花 OCRs as 火花豆) and 威力 is not shown at all.  A local rule therefore
        cannot rank the moves — but this is exactly the kind of reading a VLM is good
        at, and it happens once per run, not once per click.  Clicks stay in the local
        state machine; only this single reading is delegated.
        """

        import json as _json
        import subprocess as _sp

        session = ctx.session
        frame = session.latest()
        path = "/tmp/seerauto_skill_panel.png"
        try:
            # export the raw frame (the daemon's shot is blank while a stream is warm)
            from PIL import Image  # type: ignore
            Image.fromarray(frame).save(path)
        except Exception:  # noqa: BLE001
            try:
                session.export_png(path)
            except Exception:  # noqa: BLE001
                return {}
        prompt = ("这是赛尔号战斗界面的四个技能格（2x2）。请只输出 JSON："
                  '{"skills":[{"slot":1,"name":"技能名","power":85}, ...]}'
                  "，slot 按 左上=1 右上=2 左下=3 右下=4，power 是威力（属性技能为 0）。"
                  "只输出 JSON，不要解释。")
        try:
            out = _sp.run(["mmx", "vision", "describe", "--image", path,
                           "--prompt", prompt, "--output", "json", "--quiet"],
                          capture_output=True, text=True, timeout=90)
            raw = out.stdout or ""
            # mmx wraps the answer in its own envelope: {"content": "```json {...}```"}
            # so look inside `content` first; the naive first-{-to-last-} slice parsed
            # the envelope and found no "skills" key.
            try:
                envelope = _json.loads(raw)
            except Exception:  # noqa: BLE001
                envelope = {}
            text = str(envelope.get("content") or raw)
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end < 0:
                return {}
            data = _json.loads(text[start:end + 1])
        except Exception:  # noqa: BLE001
            return {}
        table: dict[str, int] = {}
        for item in data.get("skills") or []:
            try:
                table[f"slot{int(item['slot'])}"] = int(item.get("power") or 0)
            except Exception:  # noqa: BLE001
                continue
        if table:
            ctx.world.observations["skill_power"] = table
        return table

    @staticmethod
    def _power_table(ctx: SkillContext) -> dict[str, int]:
        """Learned move power per slot, if the run has read the pet panel."""

        table = ((ctx.world.observations or {}).get("skill_power") or {})
        return {str(k): int(v) for k, v in table.items()} if isinstance(table, dict) else {}

    @classmethod
    def pick_skill(cls, cells: list[dict[str, Any]], power_table: dict[str, int] | None = None
                   ) -> dict[str, Any] | None:
        """Highest-power move that still has uses; status moves only as a last resort.

        ``power_table`` maps a *slot key* ("slot1" … or a learned move name) to 威力.
        Without it the damage-first slot order is used, which matches the usual
        赛尔号 layout (slot 1 attack, slot 2 status, slot 3 本系 attack, slot 4 status).
        """

        if not cells:
            return None
        table = {str(k): int(v) for k, v in (power_table or {}).items()}
        for c in cells:
            key = str(c.get("name") or f"slot{c.get('slot')}")
            if not c.get("power"):
                c["power"] = table.get(key, table.get(f"slot{c.get('slot')}", 0))
        usable = [c for c in cells if c.get("pp", 0) > 0]
        if not usable:
            return None
        unknown = not any(c.get("power") for c in usable)
        if unknown:
            for slot in cls.DEFAULT_ORDER:
                for c in usable:
                    if c.get("slot") == slot:
                        return c
            return usable[0]
        damaging = [c for c in usable if c.get("power", 0) > 0]
        return max(damaging or usable, key=lambda c: (c.get("power", 0), c.get("pp", 0)))

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



def _bottom_bar_visible(session: Any) -> bool | None:
    """Is the normal scene's bottom bar on screen?

    The tower interior has no bottom bar, which is the cheapest reliable way to tell
    "inside the tower" from "standing in a room".  ``None`` means the template could
    not be scored at all, and callers must fall back to their old behaviour.
    """

    try:
        probe = session.score(session.latest(), "live_map_icon", (300, 1300, 2100, 1450))
        return bool(probe.score >= 0.55)
    except Exception:  # noqa: BLE001
        return None


def _dismiss_dialogs(ctx: SkillContext, *, limit: int = 4) -> int:
    """Dismiss up to ``limit`` known dialogs, returning how many were clicked.

    Two detectors, because either alone is fragile: an OCR'd confirm word, or a
    known modal *body* phrase plus the fixed button position.  Missing the second
    one is what left 「您还没有加入战队」 on screen swallowing every later click.
    """

    clicked = 0
    for _ in range(limit):
        try:
            items = _ocr(ctx.session, min_conf=0.25)
        except Exception:  # noqa: BLE001
            return clicked
        text = " ".join(str(i.get("text") or "") for i in items)
        hit = _find(items, CONFIRM_WORDS)
        if hit is not None:
            px = hit["px"]
            why = f"dismiss {hit['text']!r}"
        elif any(h in text for h in MODAL_HINTS):
            px = MODAL_CONFIRM_PX
            why = "dismiss modal by body text"
        else:
            return clicked
        ok, _ = _click(ctx, px, why)
        if not ok:
            return clicked
        clicked += 1
        time.sleep(0.8)
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


class ClickText(_Base):
    """OCR a label, click it, and return what the screen says afterwards.

    This is the *exploration* primitive: it lets the agent probe an unknown UI
    ("is there a 辅导员 here? what does the dialog say?") without hard-coded
    coordinates, and it always returns the resulting text so the observation is
    evidence rather than an assumption.
    """

    spec = SkillSpec(
        id="game.click_text",
        kind=SkillKind.ACT,
        summary="按屏幕文字点击一个标签，并把点击后的整屏文本返回（用于探索未知界面）",
        inputs={"text": {"type": "str", "required": True},
                "exact": {"type": "bool", "required": False},
                "settle_s": {"type": "float", "required": False}},
        outputs={"clicked": "str", "after_text": "list"},
        max_duration_s=25.0,
        max_actions=1,
        requires_input=True,
        verified="unverified",
    )

    def run(self, ctx: SkillContext, text: str = "", exact: bool = False,
            settle_s: float = 1.6) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        s = ctx.session
        items = _ocr(s, min_conf=0.3)
        hit = _find(items, (text,), exact=exact)
        if hit is None:
            return SkillResult(
                skill=self.id, status=Status.BLOCKED, changed=False,
                reason=f"text {text!r} not on screen",
                data={"visible": [i["text"] for i in items][:25]},
                escalation="replan")
        ok, why = self.click_and_check(ctx, hit["px"], f"click {hit['text']!r}")
        if not ok:
            return SkillResult.blocked(self.id, f"click refused: {why}", escalation="pause")
        time.sleep(settle_s)
        after = _ocr(s, min_conf=0.3)
        return SkillResult.pass_(
            self.id,
            proof=f"clicked {hit['text']!r}; after: "
                  f"{' | '.join(i['text'] for i in after[:6])[:120]}",
            before={"visible": [i["text"] for i in items][:15]},
            after={"clicked": hit["text"], "after_text": [i["text"] for i in after]},
            actions=1,
            data={"after_items": [(round(i["px"][0]), round(i["px"][1]), i["text"]) for i in after]},
        )


class ReadPetBag(_Base):
    """Open the pet bag, read every pet's HP/level, then close it.

    This is the *only* reliable way to see whether the account can still fight:
    the battle HUD shows the ally level but not HP, and a fainted pet makes the
    client answer 「你没有可出战的精灵哦」 to every engage.  Discovering that
    after burning a full pass is exactly the kind of waste a long run must not
    do, so the runner asks this skill first.

    Panel layout was measured live on 2026-11-03: the pet rows sit in the left
    pane (``烈火猴 0/95``), the detail pane on the right carries
    ``等级：34`` / ``升级所需经验值：2573``.
    """

    spec = SkillSpec(
        id="game.read_pet_bag",
        kind=SkillKind.OBSERVE,
        summary="打开精灵背包，读出所有精灵的血量/等级/升级所需经验，再关闭",
        inputs={"close": {"type": "bool", "required": False}},
        outputs={"pets": "list", "can_battle": "bool"},
        max_duration_s=30.0,
        max_actions=3,
        requires_input=True,
        verified="real",
        evidence=("artifacts/round5/08_pet_storage.png", "artifacts/round5/04_pet_panel.png"),
    )

    def run(self, ctx: SkillContext, close: bool = True) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        s = ctx.session
        ensure_world(ctx)
        _dismiss_dialogs(ctx, limit=2)
        opened = False
        try:
            probe = s.score(s.latest(), "live_petbag_icon", (300, 1300, 2100, 1450))
            if probe.score >= 0.75:
                _click(ctx, probe.point_px, "open pet bag (template)")
                time.sleep(1.3)
                opened = True
        except Exception:  # noqa: BLE001
            opened = False
        if not opened:
            return SkillResult.blocked(self.id, "pet-bag icon not found on screen",
                                       escalation="replan")
        items = _ocr(s, min_conf=0.25)
        text = " ".join(str(i.get("text") or "") for i in items)
        if "等级" not in text and "升级所需" not in text:
            return SkillResult.blocked(self.id, "pet panel did not open",
                                       escalation="replan")
        import re

        pets: list[dict[str, Any]] = []
        for it in items:
            m = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", str(it.get("text") or ""))
            if m:
                cur, mx = int(m.group(1)), int(m.group(2))
                pets.append({"hp": cur, "hp_max": mx,
                             "px": (round(it["px"][0]), round(it["px"][1]))})
        level = None
        xp_to_next = None
        for it in items:
            t = str(it.get("text") or "")
            if t.startswith("等级"):
                level = _int_after(t)
            elif "升级所需经验值" in t:
                xp_to_next = _int_after(t)
        # close again unless the caller still needs the panel (e.g. to hover the
        # icon row: probing a closed panel hovers over the world instead)
        if close:
            try:
                # The bag's X moved when the panel grew a detail pane; try both and
                # verify the panel is really gone (an open bag blocks every hunt).
                for point in ((1141, 503), (2072, 312)):
                    _click(ctx, point, f"close pet bag at {point}")
                    time.sleep(0.5)
                    left = [str(i.get("text") or "") for i in _ocr(ctx.session, min_conf=0.25)]
                    if not any("升级所需经验值" in t or "个体：" in t for t in left):
                        break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.6)
        alive = [p for p in pets if p["hp"] > 0]
        can_battle = bool(alive)
        proof = (f"{len(pets)} pets; hp alive={len(alive)}; "
                 f"level={level}; xp_to_next={xp_to_next}")
        result = SkillResult(
            skill=self.id,
            status=Status.PASS,
            after={"pets": pets, "can_battle": can_battle,
                   "level": level, "xp_to_next": xp_to_next},
            changed=True,
            proof=proof,
            actions=2,
            data={"raw": text[:300]},
            evidence=[Evidence.live("ocr:精灵背包", detail=proof)],
        )
        if not can_battle:
            result.escalation = "pause"
        return result


class GotoPuniEntrance(_Base):
    """Navigate to the Puni entrance and prove arrival by reading the scene.

    Path (verified live 2026-11-03): SPT icon -> category 「太空站左翼」 -> the
    single card 「谱尼 / 勇者之塔神秘领域」 -> 前往.  Arrival is confirmed by OCR
    seeing the 勇者之塔 portal plus the guard NPC, so a teleport that silently
    failed cannot be mistaken for success.
    """

    spec = SkillSpec(
        id="game.goto_puni_entrance",
        kind=SkillKind.ACT,
        summary="SPT→太空站左翼→谱尼→前往，并用画面文字证明真的到了勇者之塔神秘领域",
        inputs={},
        outputs={"unlocked": "dict", "scene_text": "list"},
        preconditions=("scene.world",),
        max_duration_s=90.0,
        max_actions=6,
        requires_input=True,
        verified="real",
        evidence=("artifacts/round5/09_puni_entrance.png",),
    )

    #: Words that can only be seen in 勇者之塔神秘领域.
    ARRIVAL_MARKERS = ("勇者之塔", "神秘领域", "卫兵")

    def run(self, ctx: SkillContext) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        nav = NavigateSptBoss()(ctx, boss="谱尼", category="太空站左翼")
        if not nav.ok:
            return SkillResult.fail(self.id, f"could not reach the Puni card: {nav.reason}",
                                    data={"navigation": nav.to_dict()})
        # The 勇者之塔 sign is drawn art, not text: OCR reads nothing there, so
        # arrival is proved by template-matching the portal itself (self 1.00,
        # other scenes <=0.46).  OCR text is kept as supporting evidence only.
        deadline = time.perf_counter() + 20.0
        seen: list[str] = []
        matched = 0.0
        while time.perf_counter() < deadline:
            frame = ctx.session.latest()
            probe = ctx.session.score(frame, "live_puni_tower_sign", (200, 340, 2260, 1330))
            matched = max(matched, probe.score)
            items = _ocr(ctx.session, min_conf=0.25)
            texts = [str(i.get("text") or "") for i in items]
            if probe.score >= 0.85:
                seen = texts
                break
            time.sleep(0.8)
        if matched < 0.85:
            return SkillResult.fail(
                self.id,
                f"teleport happened but the 勇者之塔 portal was not confirmed "
                f"(best template score {matched:.2f})",
                data={"after_nav": nav.to_dict()})
        return SkillResult.pass_(
            self.id,
            proof=f"arrived at 勇者之塔神秘领域 (portal template {matched:.2f})",
            after={"unlocked": {"puni.entrance.reachable": True},
                   "scene_text": seen[:20]},
            actions=3,
            evidence=[Evidence.live("ocr:勇者之塔神秘领域", detail=", ".join(seen[:6]))],
        )


class GotoTutorRoom(_Base):
    """Reach the 初级班/高级班辅导员 training room (the 8x-XP prerequisite).

    Delegates to :class:`Travel` so there is exactly one implementation of
    "open the star map and arrive": the earlier copy drifted out of sync and kept
    failing for a reason that had already been fixed elsewhere (the chat panel
    covers the map's destination icons — closing it moved from 0/2 to 2/2).
    """

    spec = SkillSpec(
        id="game.goto_tutor_room",
        kind=SkillKind.ACT,
        summary="地图→橙色调练室，用「初级班辅导员/高级班辅导员」文字确认到达（8倍经验前置）",
        inputs={},
        outputs={"unlocked": "dict", "scene_text": "list"},
        max_duration_s=90.0,
        max_actions=6,
        requires_input=True,
        verified="real",
        evidence=("artifacts/round5/10_tutor_dialog.png",),
    )

    def run(self, ctx: SkillContext) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        travelled = Travel()(ctx, destination="tutor_room")
        if not travelled.ok:
            return SkillResult.fail(self.id, f"travel failed: {travelled.reason}",
                                    data={"travel": travelled.to_dict()})
        return SkillResult.pass_(
            self.id,
            proof=travelled.proof,
            after={"unlocked": {"training.tutor_room.reachable": True},
                   "scene_text": travelled.after.get("scene_text", [])},
            actions=travelled.actions,
            evidence=list(travelled.evidence),
        )


#: The star map's destinations, measured live on 2026-11-03.  The map layout is
#: fixed (unlike the bottom bar, which drifts ~100 px between scenes), so these
#: points are stable; every travel is still verified by the OCR text it must show.
#: NOTE: the expanded chat panel covers the map's destination icons — it must be
#: closed first (that single miss made travel fail 0/2 before it worked 2/2).
MAP_DESTINATIONS: dict[str, dict[str, Any]] = {
    "ship_bridge": {"px": (571, 1027), "expect": ("迪恩", "爱丽丝", "探险家"),
                    "label": "赛尔号主控室（迪恩/爱丽丝）"},
    "tutor_room": {"px": (1613, 1045), "expect": ("辅导员",),
                   "label": "辅导员训练室（8倍经验前置）"},
    "station_arena": {"px": (1168, 1040), "expect": ("连胜场数", "等待挑战"),
                      "label": "精灵太空站 挑战区"},
    "station_hall": {"px": (571, 1120), "expect": ("主播纽斯", "小XIN"),
                     "label": "精灵太空站 大厅"},
    "mystic_realm": {"px": (1835, 1035), "expect": ("勇者之塔", "卫兵", "命运"),
                     "label": "勇者之塔神秘领域"},
    "teleport_cabin": {"px": (935, 1120), "expect": ("发明家肖恩", "传送"),
                       "label": "传送舱/发明室"},
    "fortress": {"px": (813, 1120), "expect": ("要塞", "保卫战"),
                 "label": "要塞"},
    # The four remaining grid slots were never named.  Expectations come from the
    # narration each room showed when visited on 2026-11-03.
    "hall_a": {"px": (450, 1027), "expect": ("精灵是XIN", "精灵", "伙伴"),
               "label": "星图第 1 格（精灵伙伴大厅）"},
    "chatter_room": {"px": (691, 1027), "expect": ("唔喱哇啦", "叽哩呱啦", "哇啦"),
                     "label": "星图第 3 格（唔喱哇啦/叽哩呱啦）"},
    "hall_b": {"px": (813, 1027), "expect": ("小XIN", "冲动"),
               "label": "星图第 4 格（小XIN 聚集地）"},
    "rank_room": {"px": (935, 1027), "expect": ("军阶", "万圣", "等级"),
                  "label": "星图第 5 格（军阶/活动）"},
    "guard_room": {"px": (691, 1120), "expect": ("卫兵",),
                   "label": "星图第 8 格（卫兵）"},
}


class Travel(_Base):
    """Open the star map and travel to a named destination, verifying arrival.

    This exists because "click the shop icon" is not a capability: the bottom bar
    moves between scenes, while the map is fixed.  Destinations are named so a
    planner can ask for ``tutor_room`` instead of a pixel pair.
    """

    spec = SkillSpec(
        id="game.travel",
        kind=SkillKind.ACT,
        summary="打开星际地图，前往命名的目的地（主控室/训练室/太空站/神秘领域…）并验证到达",
        inputs={"destination": {"type": "str", "required": True}},
        outputs={"arrived": "bool", "scene_text": "list"},
        max_duration_s=60.0,
        max_actions=4,
        requires_input=True,
        verified="real",
        evidence=("artifacts/round5/07_map.png", "artifacts/round5/10_tutor_dialog.png"),
    )

    MAP_BUTTON_PX = (1185, 1375)

    def run(self, ctx: SkillContext, destination: str = "") -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        spec = MAP_DESTINATIONS.get(destination)
        if spec is None:
            return SkillResult.fail(
                self.id, f"unknown destination {destination!r}; known: "
                         f"{sorted(MAP_DESTINATIONS)}")
        ensure_world(ctx)
        _dismiss_dialogs(ctx, limit=2)
        for attempt in range(2):
            target = self.MAP_BUTTON_PX
            try:  # prefer the template: the bar drifts, the icon does not
                probe = ctx.session.score(ctx.session.latest(), "live_map_icon",
                                          (300, 1300, 2100, 1450))
                if probe.score >= 0.75:
                    target = probe.point_px
            except Exception:  # noqa: BLE001
                pass
            ok, why = self.click_and_check(ctx, target, "open map")
            if not ok:
                return SkillResult.blocked(self.id, f"click refused: {why}",
                                           escalation="pause")
            # wait for the map instead of guessing how long it takes
            deadline = time.perf_counter() + 8.0
            opened = False
            while time.perf_counter() < deadline:
                time.sleep(0.5)
                if _has_text(_ocr(ctx.session, min_conf=0.25), ("啦公星",)) is not None:
                    opened = True
                    break
            if not opened:
                if attempt == 0:
                    continue
                return SkillResult.fail(self.id, "star map did not open")
            ok, why = self.click_and_check(ctx, spec["px"], f"travel {destination}")
            if not ok:
                return SkillResult.blocked(self.id, f"click refused: {why}",
                                           escalation="pause")
            deadline = time.perf_counter() + 12.0
            while time.perf_counter() < deadline:
                time.sleep(0.9)
                texts = [str(i.get("text") or "") for i in _ocr(ctx.session, min_conf=0.25)]
                if any(e in t for e in spec["expect"] for t in texts):
                    return SkillResult.pass_(
                        self.id,
                        proof=f"arrived at {spec['label']} ({destination})",
                        after={"arrived": True, "scene_text": texts[:20]},
                        actions=2,
                        evidence=[Evidence.live(f"ocr:{destination}",
                                                detail=", ".join(texts[:6]))])
        return SkillResult.fail(self.id, f"destination {destination!r} not confirmed")


class ProbeButtons(_Base):
    """Identify a row of icon buttons by hovering each one and reading its tooltip.

    Icons have no OCR-able text, so template matching is the only other option and
    it breaks whenever the artwork or the bar position changes.  Hovering is how a
    human tells them apart, and the client answers with a tooltip naming the
    button ("身边跟随", "恢复体力", …) — that text *is* machine-readable.

    Costs two OCR passes per button (~1 s each), so this is a calibration skill,
    not something to call in a loop.
    """

    spec = SkillSpec(
        id="game.probe_buttons",
        kind=SkillKind.OBSERVE,
        summary="沿一行按钮逐个悬停，读取每个按钮的提示文字（用于识别没有文字图标的按钮）",
        inputs={"x0": {"type": "int", "required": True},
                "x1": {"type": "int", "required": True},
                "y": {"type": "int", "required": True},
                "count": {"type": "int", "required": True},
                "settle_s": {"type": "float", "required": False}},
        outputs={"buttons": "list"},
        max_duration_s=90.0,
        max_actions=0,
        requires_input=True,
        verified="unverified",
    )

    def run(self, ctx: SkillContext, x0: int = 0, x1: int = 0, y: int = 0,
            count: int = 1, settle_s: float = 0.9) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        if count < 1 or x1 <= x0:
            return SkillResult.fail(self.id, f"bad row {x0}..{x1} count={count}")
        step = (x1 - x0) / max(1, count - 1)
        found: list[dict[str, Any]] = []
        for i in range(count):
            x = int(round(x0 + i * step))
            try:
                probe = ctx.session.hover_px(x, y, settle_s=settle_s)
            except Exception as exc:  # noqa: BLE001
                found.append({"px": (x, y), "text": "", "error": str(exc)})
                continue
            found.append({"px": (x, y), "text": probe.get("text", "")})
        named = [b for b in found if b.get("text")]
        return SkillResult.pass_(
            self.id,
            proof=f"probed {len(found)} buttons, {len(named)} had a tooltip: "
                  + "; ".join(f"({b['px'][0]}){b['text'][:12]}" for b in named[:6]),
            after={"buttons": found},
            changed=False,
            actions=0,
        )


class HealPet(_Base):
    """Restore pet HP using the bag's heal icon, identified by its tooltip.

    The icon is the right-most one in the bag's action row (the pet head with a red
    cross).  Its position drifts and its artwork is not recognisable by template,
    so the button is found the way a human finds it: hover the row, read the
    tooltips, click the one that says 恢复 / 治疗 / 体力 / 回血.
    """

    spec = SkillSpec(
        id="game.heal_pet",
        kind=SkillKind.ACT,
        summary="打开精灵背包，用悬停提示找到「恢复体力」按钮并点击，再复读血量确认",
        inputs={"row_y": {"type": "int", "required": False},
                "row_x0": {"type": "int", "required": False},
                "row_x1": {"type": "int", "required": False},
                "row_count": {"type": "int", "required": False}},
        outputs={"hp_before": "list", "hp_after": "list", "healed": "bool"},
        max_duration_s=120.0,
        max_actions=6,
        requires_input=True,
        verified="unverified",
        evidence=("artifacts/round5/13_pet_bag_full.png",),
    )

    #: Measured live 2026-11-03 on the 1200x800 client: the heal icon's tooltip
    #: 「精灵恢复」 answers at (1145,1115) and the neighbouring icons at
    #: 680=身边跟随, 860=放回仓库, 980=精灵图鉴.  The row is scanned at two heights
    #: because the tooltip only appears inside a ~20 px band.
    ROWS = ((1115, 640, 1180, 9), (1131, 660, 1160, 7))
    HEAL_WORDS = ("精灵恢复", "恢复", "治疗")

    def run(self, ctx: SkillContext, row_y: int = 0, row_x0: int = 0,
            row_x1: int = 0, row_count: int = 0) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        s = ctx.session
        row: dict[str, int] = {}
        if row_y or row_x0 or row_x1 or row_count:
            row = {"y": int(row_y) or self.ROWS[0][0],
                   "x0": int(row_x0) or self.ROWS[0][1],
                   "x1": int(row_x1) or self.ROWS[0][2],
                   "count": int(row_count) or self.ROWS[0][3]}

        before = ReadPetBag()(ctx, close=False)
        if not before.ok:
            return SkillResult.fail(self.id, f"cannot read the pet bag: {before.reason}")
        hp_before = before.after.get("pets") or []

        buttons: list[dict[str, Any]] = []
        target = None
        rows = [(row["y"], row["x0"], row["x1"], row["count"])] if row_y else self.ROWS
        for (ry, rx0, rx1, rcount) in rows:
            found = ProbeButtons()(ctx, x0=rx0, x1=rx1, y=ry, count=rcount)
            buttons.extend((found.after or {}).get("buttons") or [])
            target = next((b for b in buttons
                           if any(w in str(b.get("text") or "") for w in self.HEAL_WORDS)),
                          None)
            if target is not None:
                break
        if target is None:
            return SkillResult.fail(
                self.id,
                "no heal button found by tooltip; probed: "
                + "; ".join(f"({b['px'][0]}){b.get('text') or '-'}" for b in buttons[:8]),
                data={"buttons": buttons})

        ok, why = self.click_and_check(ctx, target["px"], f"heal via {target['text']!r}")
        if not ok:
            return SkillResult.blocked(self.id, f"click refused: {why}", escalation="pause")
        time.sleep(1.2)
        _dismiss_dialogs(ctx, limit=2)

        after = ReadPetBag()(ctx)
        hp_after = (after.after or {}).get("pets") or []
        healed = any(a.get("hp", 0) > b.get("hp", 0)
                     for a, b in zip(hp_after, hp_before))
        return SkillResult(
            skill=self.id,
            status=Status.PASS if healed else Status.FAIL,
            reason="" if healed else "hp did not increase after clicking the heal button",
            after={"hp_before": hp_before, "hp_after": hp_after, "healed": healed,
                   "button": target},
            changed=healed,
            proof=(f"clicked {target['text']!r} at {target['px']}; hp "
                   f"{[p.get('hp') for p in hp_before]} -> {[p.get('hp') for p in hp_after]}"),
            actions=2,
            escalation=None if healed else "pause",
        )


class DismissStartupPopup(_Base):
    """Close the client's auxiliary startup dialog (e.g. the art-pack prompt).

    The client owns a second, small window that is invisible to everything which
    only looks at the playfield: while it is open the playfield sits on
    「版本检测中」 and every later skill fails with a misleading reason.  This skill
    finds that window by size, OCRs *its own* pixels, and clicks the safe choice by
    text — no hard-coded coordinates, because the dialog moves between launches.

    Preference order is deliberately "do nothing irreversible": 暂不导入 before
    立即导入 before 前往下载.
    """

    spec = SkillSpec(
        id="game.dismiss_startup_popup",
        kind=SkillKind.ACT,
        summary="识别并关闭客户端的小窗口启动弹窗（美术包导入等），优先选可逆选项",
        inputs={"prefer": {"type": "list", "required": False}},
        outputs={"dismissed": "bool", "text": "list"},
        max_duration_s=45.0,
        max_actions=3,
        requires_input=True,
        verified="real",
        evidence=("artifacts/round5/17_artpack_popup.png",),
    )

    #: Safe-first.  Never auto-choose a download unless told to.
    DEFAULT_PREFERENCE = ("暂不导入", "取消", "关闭", "我知道了", "暂不", "确定")

    def run(self, ctx: SkillContext, prefer: Any = None) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        from ..live import list_game_windows

        order = tuple(prefer or self.DEFAULT_PREFERENCE)
        windows = list_game_windows()
        aux = [w for w in windows if w["aux"]]
        if not aux:
            return SkillResult(skill=self.id, status=Status.SKIPPED, changed=False,
                               reason="no auxiliary game window present")
        win = aux[0]
        path = "/tmp/seerauto_aux_window.png"
        try:
            import subprocess

            subprocess.run(
                ["screencapture", "-x", f"-R{int(win['x'])},{int(win['y'])},"
                                         f"{int(win['w'])},{int(win['h'])}", path],
                check=True, capture_output=True, timeout=10)
        except Exception as exc:  # noqa: BLE001
            return SkillResult.fail(self.id, f"cannot capture the popup window: {exc}")
        items = _ocr_path(ctx, path)
        texts = [str(i.get("text") or "") for i in items]
        chosen = None
        for word in order:
            for it in items:
                if word in str(it.get("text") or ""):
                    chosen = it
                    break
            if chosen is not None:
                break
        if chosen is None:
            return SkillResult.fail(
                self.id, "popup has no known button", data={"text": texts[:12]})
        # Vision boxes are normalised with a bottom-left origin, so they map
        # straight onto the window's logical rect regardless of the capture scale.
        nx, ny, nw, nh = chosen["norm"]
        w_px = int(win["w"])
        h_px = int(win["h"])
        cx = win["x"] + (nx + nw / 2.0) * win["w"]
        cy = win["y"] + (1.0 - (ny + nh / 2.0)) * win["h"]
        try:
            # No activate(): the dialog is in front of its own app already, and
            # raising the app would take focus away from whatever the user is doing.
            ctx.session.client.send("clickpid", x=cx, y=cy, n=1)
        except Exception as exc:  # noqa: BLE001
            return SkillResult.fail(self.id, f"click on the popup failed: {exc}")
        time.sleep(1.6)
        remaining = [w for w in list_game_windows() if w["aux"]]
        return SkillResult(
            skill=self.id,
            status=Status.PASS if not remaining else Status.FAIL,
            after={"dismissed": not remaining, "text": texts[:12],
                   "button": chosen.get("text")},
            changed=not remaining,
            proof=(f"clicked {chosen.get('text')!r} on the {w_px}x{h_px} auxiliary "
                   f"window at ({cx:.0f},{cy:.0f}); popup still open={bool(remaining)}"),
            actions=1,
        )


def _ocr_path(ctx: SkillContext, path: str) -> list[dict[str, Any]]:
    """OCR an arbitrary image through the daemon; keep Vision-normalised boxes.

    ``norm`` is (x, y, w, h) with a bottom-left origin — the same space the client
    itself uses — so a caller can map it onto *any* window without knowing the
    capture's pixel size.  Converting to pixels here would be wrong for an
    auxiliary window whose size differs from the playfield (that bug put the first
    live popup click 500 pt off; it only looked right because the dialog had
    already closed).
    """

    client = getattr(ctx.session, "client", None)
    if client is None:
        return []
    raw = client.send("ocr", path=path, min=0.25).get("items", [])
    return [{"text": it["text"], "conf": it.get("conf", 0.0),
             "norm": (float(it["x"]), float(it["y"]), float(it["w"]), float(it["h"]))}
            for it in raw]


def _trusted(evidence: Any) -> bool:
    """Local copy of the trust rule: LIVE/REPLAY detail counts, inference does not.

    `rank` lives on `Source`, not on `Evidence`.  The previous version asked
    `evidence.rank`, which raised, was swallowed, and returned False for
    everything — so a verified prior reading was never protected and every VLM
    guess overwrote it.  Source ranks here are LIVE=4, REPLAY=3, INFERRED=2,
    HISTORICAL=1, UNKNOWN=0.
    """

    try:
        return evidence.source.rank >= 3     # LIVE_VERIFIED or REPLAY_VERIFIED
    except Exception:  # noqa: BLE001
        return False


class ReadTowerFloor(_Base):
    """Enter the 试炼之塔 and read the floor the account has reached.

    The tower panel prints 「你当前到达第 N 层」 verbatim, which is the only live,
    machine-readable evidence of tower progress — and it is what the goal's
    `counter_at_least(tower.floors_cleared)` predicate needs.  Reading is enough to
    satisfy "floors cleared >= 1"; actually clearing floor 13 would need a fight the
    account is not ready for, and the counter must never be guessed.
    """

    spec = SkillSpec(
        id="game.tower_status",
        kind=SkillKind.OBSERVE,
        summary="进入试炼之塔并读取「你当前到达第N层」，写入 counters.tower.floors_cleared",
        inputs={},
        outputs={"floors": "int"},
        max_duration_s=90.0,
        max_actions=4,
        requires_input=True,
        verified="real",
        evidence=("artifacts/round6/tower_floor13.png",),
    )

    TOWER_ENTRY = (1195, 1150)

    @staticmethod
    def _read_floor_by_vision(session: Any) -> int | None:
        """Ask the VLM which floor the stairway shows."""

        import json as _json
        import subprocess as _sp

        path = "/tmp/seerauto_tower_floor.png"
        try:
            session.export_png(path)
        except Exception:  # noqa: BLE001
            return None
        prompt = ("这是赛尔号试炼之塔内部。地板上有一串很大的数字，表示当前层数。"
                  '请只输出 JSON：{"floor": 数字}。只输出 JSON。')
        try:
            out = _sp.run(["mmx", "vision", "describe", "--image", path,
                           "--prompt", prompt, "--output", "json", "--quiet"],
                          capture_output=True, text=True, timeout=90)
            env = _json.loads(out.stdout or "{}")
            text = str(env.get("content") or "")
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end < 0:
                return None
            data = _json.loads(text[start:end + 1])
            value = data.get("floor")
            return int(value) if value is not None else None
        except Exception:  # noqa: BLE001
            return None

    def run(self, ctx: SkillContext) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        s = ctx.session
        ensure_world(ctx)
        _dismiss_dialogs(ctx, limit=3)
        inside = _bottom_bar_visible(s) is False
        if not inside:
            _click(ctx, self.TOWER_ENTRY, "点击 试炼之塔")
            time.sleep(2.0)
        items = s.ocr(min_conf=0.25)
        floor = None
        vision_only = False
        import re as _re
        # Only believe a floor number that came with the tower dialog.  Live 2026-11-06:
        # this skill reported 「第15层」 from a dialog a *previous* interaction had left
        # open, and later read 「第490层」 off unrelated text — both false PASSes that
        # then made the farm click scenery.
        joined = " ".join(str(it.get("text") or "") for it in items)
        dialog_up = ("挑战试炼之塔" in joined or "继续挑战" in joined
                     or "选择楼层" in joined)
        if dialog_up:
            for it in items:
                m = _re.search(r"第\s*(\d+)\s*层", str(it.get("text") or ""))
                if m:
                    floor = int(m.group(1))
                    break
        if floor is None:
            # Inside the tower the entry panel is unreachable, but the floor number is
            # painted on the stairway in artwork (no OCR).  One VLM read of a single
            # frame is enough, and it is the same "read a picture, decide locally"
            # split the project uses for the move list.
            floor = self._read_floor_by_vision(s)
            vision_only = floor is not None
            if floor is None:
                return SkillResult(
                    self.id, status=Status.FAIL,
                    reason="tower floor not readable (panel text and painted number both missed)",
                    data={"text": [str(i.get("text")) for i in items][:10]})
        if vision_only:
            ctx.world.observations["tower_floor_vlm"] = floor
            # A VLM read of a painted number is an inference, not instrumentation.
            # It may record what floor *appears* to be reached; it must never
            # advance the cleared counter (it misread 14 as 7 on 2026-11-04).
            ctx.world.facts["counters.tower.floors_reached"] = Fact(
                floor, Evidence(Source.INFERRED,
                                f"vision read of the painted floor number: {floor}"))
        else:
            ctx.world.facts["counters.tower.floors_reached"] = Fact(floor, Evidence.live(
                f"tower panel: 你当前到达第{floor}层"))
            # `tower.floors_cleared` has exactly ONE writer: game.farm_tower, which
            # increments it per won guardian battle.  Reading the panel must not
            # advance it, or the completion predicate becomes satisfiable by merely
            # standing on a floor -- which is the "reached 14 => cleared 14" bug in
            # a different disguise (found by the 2026-11-05 review).
        ctx.world.observations["tower_floor"] = floor
        cleared = ctx.world.facts.get("counters.tower.floors_cleared")
        cleared_txt = f"{cleared.value} cleared" if cleared is not None else "cleared unknown"
        return SkillResult.pass_(
            self.id,
            proof=f"tower panel reports 第{floor}层 reached ({cleared_txt})",
            after={"floors": floor},
            changed=False,
            actions=1,
        )



class EnterTower(_Base):
    """Walk through the 挑战试炼之塔 dialog into the tower interior.

    Why this exists (2026-11-05, live): `game.tower_status` clicks the tower entrance
    and reads 「你当前到达第 N 层」 off the *dialog*, which it then reports as "reached".
    The dialog is not the tower.  `game.farm_tower` then clicks the guardian's pixel —
    which lands on the dialog's artwork — and every floor attempt dies with
    "guardian click landed but no battle started".

    The dialog has exactly two buttons, 「选择楼层」 and 「继续挑战」; entering is the
    second one.  It is found by its text, never by a coordinate, because the dialog
    position follows the window.
    """

    spec = SkillSpec(
        id="game.enter_tower",
        kind=SkillKind.ACT,
        summary="点「继续挑战」穿过试炼之塔对话框，真正进入塔内（可验证：对话框消失）",
        inputs={},
        outputs={"entered": "bool", "floor": "int"},
        max_duration_s=90.0,
        max_actions=5,
        requires_input=True,
        verified="real",
        evidence=("data/overnight/frames/live_after_batch1.png",),
    )

    TOWER_ENTRY = (1195, 1150)
    ENTER_WORDS = ("继续挑战",)
    #: Strings that only the dialog carries; their absence is how we know we left it.
    DIALOG_MARKERS = ("挑战试炼之塔", "选择楼层", "继续挑战")
    FLOOR_RE = None  # set in run(), keeps the import local

    def _dialog_open(self, ctx: SkillContext, items) -> bool:
        joined = " ".join(str(i.get("text") or "") for i in items)
        return any(m in joined for m in self.DIALOG_MARKERS)

    def run(self, ctx: SkillContext) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        import re

        s = ctx.session
        ensure_world(ctx)
        _dismiss_dialogs(ctx, limit=2)
        items = s.ocr(min_conf=0.25)
        opened = self._dialog_open(ctx, items)
        if not opened and _bottom_bar_visible(s) is False:
            # Already in the interior (no bottom bar, no dialog): re-entering is a
            # no-op, and clicking the entrance pixel from in here hits scenery.
            return SkillResult.pass_(
                self.id, proof="already inside the tower (no bottom bar, no dialog)",
                after={"entered": True, "already": True}, changed=False, actions=0)
        floor = None
        for it in items:
            m = re.search(r"第\s*(\d+)\s*层", str(it.get("text") or ""))
            if m:
                floor = int(m.group(1))
                break
        if not opened:
            # Not at the dialog yet: open it the same way tower_status does.
            ok, why = _click(ctx, self.TOWER_ENTRY, "点击 试炼之塔")
            if not ok:
                return SkillResult.blocked(self.id, f"click refused: {why}",
                                           escalation="pause")
            time.sleep(2.0)
            items = s.ocr(min_conf=0.25)
            opened = self._dialog_open(ctx, items)
            for it in items:
                m = re.search(r"第\s*(\d+)\s*层", str(it.get("text") or ""))
                if m:
                    floor = int(m.group(1))
                    break
        if not opened:
            return SkillResult.fail(
                self.id, "试炼之塔 dialog did not open (tower entrance not found)",
                data={"text": [str(i.get("text")) for i in items][:10]})

        hit = None
        for it in items:
            if any(w in str(it.get("text") or "") for w in self.ENTER_WORDS):
                hit = it
                break
        if hit is None:
            return SkillResult.fail(
                self.id, "dialog has no 继续挑战 button",
                data={"text": [str(i.get("text")) for i in items][:12]})
        ok, why = _click(ctx, hit["px"], "点击「继续挑战」进入试炼之塔")
        if not ok:
            return SkillResult.blocked(self.id, f"click refused: {why}",
                                       escalation="pause")
        # The dialog disappearing is the observable proof; wait for it, don't sleep.
        gone = False
        deadline = time.perf_counter() + 15.0
        while time.perf_counter() < deadline:
            time.sleep(0.8)
            items = s.ocr(min_conf=0.25)
            if not self._dialog_open(ctx, items):
                gone = True
                break
        if not gone:
            return SkillResult.fail(self.id, "dialog stayed open after 继续挑战")
        ctx.world.observations["tower_entered"] = True
        if floor is not None:
            ctx.world.observations["tower_floor"] = floor
        return SkillResult.pass_(
            self.id,
            proof=(f"entered the tower interior (dialog closed"
                   + (f", 第{floor}层" if floor is not None else "") + ")"),
            after={"entered": True, "floor": floor},
            changed=True,
            actions=2,
            evidence=[Evidence.live("enter_tower", detail="dialog closed after 继续挑战")],
        )



class FarmTower(_Base):
    """Fight the 试炼之塔 floor guardian repeatedly — the 25x experience route.

    Measured 2026-11-04: one tower floor pays **1300 积累经验**; a wild 蘑菇怪 pays 52.
    The tower has no bottom bar and no map, so the loop is purely: click the guardian,
    fight it out with the normal turn loop, clear the reward dialog, repeat.  Each win
    advances the painted floor number, which is read back with a VLM pass.
    """

    spec = SkillSpec(
        id="game.farm_tower",
        kind=SkillKind.ACT,
        summary="试炼之塔连战：点守护者→打完→清弹窗→读新层数，循环 N 层",
        inputs={"floors": {"type": "int", "required": False},
                "max_seconds": {"type": "float", "required": False}},
        outputs={"floors_won": "int", "tower_floor": "int"},
        max_duration_s=900.0,
        max_actions=400,
        requires_input=True,
        verified="real",
        evidence=("artifacts/round6/tower_floor14.png",),
    )

    GUARDIAN = (1205, 789)

    def run(self, ctx: SkillContext, floors: int = 3, max_seconds: float = 600.0) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        s = ctx.session
        won = 0
        started = time.perf_counter()
        notes: list[str] = []
        rewards_seen: list[dict[str, Any]] = []
        xp_seen = 0.0
        while won < floors and time.perf_counter() - started < max_seconds:
            control = ctx.options.get("control")
            if control and control.stop_requested():
                notes.append("stop requested")
                break
            if control:
                # A pause inside a long grind must take effect here: the runner's own
                # wait sits *between* skills, and one farm pass can run for minutes.
                paused_s = control.wait_while_paused()
                if paused_s > 0:
                    notes.append(f"resumed after {paused_s:.1f}s")
                    # The fight may have advanced while we were not looking.
                    _dismiss_dialogs(ctx, limit=2)
            scene = s.scene().scene
            if scene is not Scene.BATTLE:
                _dismiss_dialogs(ctx, limit=2)
                clicked, why = _click(ctx, self.GUARDIAN, "点击塔层守护者")
                if not clicked:
                    # "The click was refused" and "the click missed" are different
                    # faults with different fixes; reporting the second for the first
                    # (dry run / not frontmost) sends the reader hunting scenery.
                    refusal = f"guardian click refused: {why[:100]}"
                    return SkillResult.blocked(
                        self.id, refusal, escalation="pause",
                        data={"floors_won": won, "refused": True})

                entered = s.wait_for(lambda f: s.scene(f).scene is Scene.BATTLE, timeout_s=12.0)
                if not entered:
                    notes.append("guardian click landed but no battle started "
                                 "(not in the tower interior?)")
                    break
            battle = RunBattle()(ctx, battles=1, max_seconds=240.0)
            if battle.ok:
                won += 1
                notes.append(f"floor win #{won}")
                # Keep the payout the battle already parsed.  The reward dialog is
                # dismissed a few lines below, so an outside caller that OCRs the
                # screen afterwards sees nothing and would have to *estimate* the
                # experience — which is exactly the kind of number this project does
                # not allow into a ledger.
                payout = getattr(battle, "after", None) or {}
                for reward in payout.get("rewards") or ():
                    rewards_seen.append(reward)
                xp_seen += float(payout.get("xp") or 0.0)
            else:
                notes.append(f"fight failed: {battle.reason[:60]}")
                break
            _dismiss_dialogs(ctx, limit=3)
            time.sleep(0.8)
        # Clearing a floor is observed directly (a won battle), so it is the only
        # thing allowed to advance tower.floors_cleared.  The painted floor number
        # is read separately and only ever feeds "reached", because the VLM misread
        # 14 as 7 on 2026-11-04.
        prior = ctx.world.facts.get("counters.tower.floors_cleared")
        prior_value = int(prior.value) if prior is not None and prior.value is not None else 0
        if won:
            ctx.world.facts["counters.tower.floors_cleared"] = Fact(
                prior_value + won,
                Evidence.live(f"won {won} 塔层守护者 battle(s) in this pass"))
        floor = ReadTowerFloor._read_floor_by_vision(s)
        if floor is not None:
            ctx.world.observations["tower_floor_vlm"] = floor
            prior_reached = ctx.world.facts.get("counters.tower.floors_reached")
            if prior_reached is None or not _trusted(prior_reached.evidence):
                ctx.world.facts["counters.tower.floors_reached"] = Fact(
                    floor, Evidence(Source.INFERRED,
                                    f"vision read of the painted floor number: {floor}"))
        cleared = ctx.world.facts.get("counters.tower.floors_cleared")
        cleared_txt = f"{cleared.value} cleared" if cleared is not None else "cleared unknown"
        return SkillResult(
            skill=self.id,
            status=Status.PASS if won else Status.FAIL,
            reason="" if won else "; ".join(notes) or "no floor won",
            after={"floors_won": won, "wins": won, "tower_floor": floor,
                   "floors_cleared": prior_value + won,
                   # measured, not estimated: whatever the battle's own reward
                   # parser read out of the payout dialogs
                   "rewards": rewards_seen, "xp": xp_seen},
            changed=bool(won),
            proof=(f"{won} 层通关 ({cleared_txt}); 当前第 {floor} 层; "
                   + "; ".join(notes[-4:])),
            actions=won * 2,
        )




def _allocator_body_still_there(items) -> bool:
    """Is the allocator panel on screen even if its title was not OCR'd?

    「该精灵增加」 / 「积累经验」 / 「升级所需经验值」 are large strings that survive the
    same OCR pass that loses the small title, so they are the reliable "still open"
    signal.  Treating a lost title as "closed" is what could double-pour.
    """

    joined = " ".join(str(i.get("text") or "") for i in items or ())
    return any(m in joined for m in ("该精灵增加", "积累经验", "升级所需经验值"))


def _parse_allocator_body(items):
    """Numbers-only read of the allocator body (title-independent)."""

    import re

    from .parsers import box_of

    out: dict[str, Any] = {"level": None, "pool": None, "pending_amount": None}
    for item in items or ():
        text = str(item.get("text") or "")
        # Take the number that follows the label itself, not the last number on the
        # line: OCR sometimes merges two rows ("等级：18 积累经验：18744927"), and the
        # old `split("：")[-1]` then read level=18744927 — which made the "level rose"
        # check pass on garbage.
        for label, key in (("等级", "level"), ("积累经验", "pool")):
            if label not in text:
                continue
            tail = text.split(label, 1)[1]
            match = re.search(r"(\d[\d,]*)", tail)
            if match:
                out[key] = float(match.group(1).replace(",", ""))
        if text.strip().endswith("g") and text.strip()[:-1].isdigit():
            out["pending_amount"] = float(text.strip()[:-1])
    return out if (out["level"] is not None or out["pool"] is not None) else None


class GiveExp(_Base):
    """Pour 积累经验 into the selected pet through the 经验分配器 panel.

    Why this is worth a skill: the pool grows from every fight, but the *spending*
    side has never been automated, so the account has been accumulating a currency it
    cannot use.  `docs/MACHINE_MAP.md` records the panel as NOT FOUND after a 190-point
    hover sweep, so this skill does not try to open it: it assumes the panel is on
    screen (a caller can find it with ``tools/probe_panels.py``) and refuses otherwise.

    The verification is self-contained, which is the point: the panel itself prints
    「等级：N」 and 「积累经验：M」, so after pressing 确认 the *same panel* proves or
    disproves the effect — 等级 must rise and the pool must fall.  No second
    navigation, no guess.

    Every click is text-anchored.  The amount box sits between 「该精灵增加」 and
    「点经验值」, so the midpoint of those two boxes is the input, whatever the panel's
    absolute position turns out to be.
    """

    spec = SkillSpec(
        id="game.give_exp",
        kind=SkillKind.ACT,
        summary="经验分配器：读面板→填数值→确认→用面板自身证明等级上升",
        inputs={"amount": {"type": "int", "required": False},
                "fraction": {"type": "float", "required": False}},
        outputs={"pool_before": "float", "pool_after": "float", "level_before": "int",
                 "level_after": "int", "granted": "float"},
        max_duration_s=120.0,
        max_actions=8,
        requires_input=True,
        verified="unverified",
        evidence=("docs/NEXT_SESSION_PLAYBOOK.md#4",),
    )

    #: Never pour the whole pool in one shot: the panel's per-pour cap is unknown for
    #: this server (the guide video shows 1,000,000 once).  A caller can pass an
    #: explicit amount to override.
    DEFAULT_FRACTION = 0.5

    @staticmethod
    def _panel(ctx: SkillContext) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        from .parsers import parse_exp_allocator

        items = ctx.session.ocr(min_conf=0.2)
        return parse_exp_allocator(items), items

    def run(self, ctx: SkillContext, amount: int | None = None,
            fraction: float | None = None) -> SkillResult:
        guard = self.guard(ctx)
        if guard:
            return guard
        panel, items = self._panel(ctx)
        if panel is None:
            return SkillResult(skill=self.id, status=Status.SKIPPED, changed=False,
                               reason="经验分配器 panel is not on screen")
        pool = panel.get("pool")
        level_before = panel.get("level")
        if not pool or pool <= 0:
            return SkillResult(skill=self.id, status=Status.SKIPPED, changed=False,
                               reason=f"积累经验 pool is {pool!r}; nothing to pour",
                               after={"pool_before": pool})
        want = int(amount) if amount else int(pool * float(
            self.DEFAULT_FRACTION if fraction is None else fraction))
        want = max(1, min(want, int(pool)))
        # Locate the input comfortably between the two captions.  ``box_of`` is used
        # rather than ``item["px"]`` because it works from either the canonical
        # pixel pair or Vision's normalised box, and the canonical space *is* the
        # 2400x1600 game_px space that click_px expects.
        from .parsers import box_of

        add_box = next((box_of(i) for i in items
                        if "该精灵增加" in str(i.get("text") or "")), None)
        tail_box = next((box_of(i) for i in items
                         if "点经验值" in str(i.get("text") or "")), None)
        if add_box is None or tail_box is None:
            return SkillResult.fail(
                self.id, "cannot locate the amount row (该精灵增加 / 点经验值 missing)")
        input_px = ((add_box.cx + tail_box.cx) / 2.0, (add_box.cy + tail_box.cy) / 2.0)
        ok, why = _click(ctx, input_px, "聚焦「该精灵增加」输入框")
        if not ok:
            return SkillResult.fail(self.id, f"cannot focus the amount field: {why}")
        time.sleep(0.4)
        ctx.session.client.send("type", text=str(want))
        time.sleep(0.6)
        after_typing, _ = self._panel(ctx)
        typed = None if after_typing is None else after_typing.get("pending_amount")
        if typed is None or int(typed) != want:
            return SkillResult.fail(
                self.id, f"typed {want} but the panel reads {typed!r}; not pressing 确认")
        ok, why = self._click_text(ctx, ("确认", "確定", "确定"))
        if not ok:
            return SkillResult.fail(self.id, f"cannot press 确认: {why}")
        time.sleep(1.2)
        after_panel, after_items = self._panel(ctx)
        if after_panel is None and not _allocator_body_still_there(after_items):
            # Only clear a dialog when the panel is genuinely gone.  A missing *title*
            # is not a missing panel: the title is small text and the OCR drops it,
            # and the old recovery would then click the first 确认 on screen — which
            # can be this panel's own button, pouring a second time.
            _dismiss_dialogs(ctx, limit=1)
            time.sleep(0.8)
            after_panel, after_items = self._panel(ctx)
        if after_panel is None and _allocator_body_still_there(after_items):
            # The panel is still up but its title was unreadable: re-read the numbers
            # off the body so the result is still honest.
            from .parsers import parse_exp_allocator  # noqa: F401
            after_panel = _parse_allocator_body(after_items)
        level_after = None if after_panel is None else after_panel.get("level")
        pool_after = None if after_panel is None else after_panel.get("pool")
        rose = (level_before is not None and level_after is not None
                and level_after > level_before)
        spent = (pool - pool_after) if (pool_after is not None) else None
        if rose or (spent is not None and spent > 0):
            # The goal node's predicate is `feature_unlocked: exp_giver.poured`, and
            # only `world.unlocked[...]` is read by that predicate.  Writing the level
            # alone meant a real, verified pour could never complete the node.
            try:
                ctx.world.unlocked["exp_giver.poured"] = Fact(
                    True, Evidence.live(
                        f"经验分配器: 等级 {level_before}->{level_after}, "
                        f"积累经验 {pool:.0f}->{pool_after}"))
            except Exception:  # noqa: BLE001 - world shapes differ between callers
                ctx.world.facts["unlocked.exp_giver.poured"] = Fact(
                    True, Evidence.live(
                        f"经验分配器: 等级 {level_before}->{level_after}"))
            return SkillResult(
                skill=self.id, status=Status.PASS,
                reason="level rose" if rose else "pool fell (level read missing)",
                after={"pool_before": pool, "pool_after": pool_after,
                       "level_before": level_before, "level_after": level_after,
                       "granted": spent},
                changed=True,
                proof=(f"poured {want}; 等级 {level_before}->{level_after}; "
                       f"积累经验 {pool:.0f}->{pool_after if pool_after is not None else '?'}"))
        return SkillResult(
            skill=self.id, status=Status.FAIL, changed=False,
            reason=(f"panel shows no change after 确认 (等级 {level_before}->{level_after}, "
                    f"pool {pool}->{pool_after})"),
            after={"pool_before": pool, "pool_after": pool_after,
                   "level_before": level_before, "level_after": level_after})

    @staticmethod
    def _click_text(ctx: SkillContext, words) -> tuple[bool, str]:
        from .parsers import box_of

        items = ctx.session.ocr(min_conf=0.2)
        for word in words:
            for item in items:
                if word in str(item.get("text") or ""):
                    box = box_of(item)
                    if box is None:
                        continue
                    return _click(ctx, (box.cx, box.cy), f"点击「{word}」")
        return False, f"no text matching {words}"



def build_act_skills() -> list[Skill]:
    return [
        OpenPanel(),
        ClosePanel(),
        NavigateSptBoss(),
        RunBattle(),
        ReadPetLevel(),
        ConfirmDialog(),
        ClickText(),
        ReadPetBag(),
        GotoPuniEntrance(),
        GotoTutorRoom(),
        Travel(),
        ProbeButtons(),
        HealPet(),
        DismissStartupPopup(),
        ReadTowerFloor(),
        EnterTower(),
        FarmTower(),
        GiveExp(),
    ]


__all__ = [
    "OpenPanel",
    "ClosePanel",
    "NavigateSptBoss",
    "RunBattle",
    "ReadPetLevel",
    "ConfirmDialog",
    "ClickText",
    "ReadPetBag",
    "GotoPuniEntrance",
    "GotoTutorRoom",
    "Travel",
    "ProbeButtons",
    "HealPet",
    "DismissStartupPopup",
    "ReadTowerFloor",
    "EnterTower",
    "FarmTower",
    "GiveExp",
    "MAP_DESTINATIONS",
    "build_act_skills",
    "ICON",
    "PANEL_MARKERS",
]
