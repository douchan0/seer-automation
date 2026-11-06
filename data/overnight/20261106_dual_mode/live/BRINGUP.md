# BRINGUP — 2026-11-06 night live client (gameops)

Machine: macOS 15.7.7, 2 displays (main 1470×956 pt @ 0,0; second display above with negative y).
Client: `/Applications/SeerGame.app`, `xin计划 (1.0.8.9)`.
Authorization: user authorized 2026-11-06 22:00 → 2026-11-07 07:00, launch + **foreground** GUI input.
Scope actually used: launch, art-pack dialog dismissal, login, one battery-enable UI interaction. No battle / tower / PvP / SPT-opened.

## 1. Window geometry (measured this session, DO NOT hardcode)

| field | value |
|---|---|
| window id | **3284** |
| title | `xin计划 (1.0.8.9)` |
| bounds | **x=268, y=44, w=1200, h=800 pt** |
| screenshot | **2400×1600 px** (scale 2.0) |
| aux window (art-pack) | id=3285, 260×258 pt — **gone after dismissal** |
| frontmost at handover | `SeerGame` |

**Position changes during a single bring-up**: first read after launch = `(270,68)`; after the art-pack
dialog was dismissed the main window was `(268,44)`. Always re-read from `seerctl wl` right before any click.

### Coordinate conversion (verified twice this session)

```
global_pt = (268 + game_px_x / 2, 44 + game_px_y / 2)      # game_px = window-local device px, 2400×1600
```
The shot includes the client's own custom title bar ( Electron `xin计划 (1.0.8.9)` bar is part of the
1200×800 content ), so `win.y` is the content origin — no extra title-bar correction.

**Stage offset ≈ 0 this session.** SPT icon observed at game_px `(2149,347)`; docs say `(2151,347)` → dx=2, dy=0
(OCR jitter). Still measure it per session per AGENTS §5.5.6.

## 2. Commands that worked (exact)

```bash
cd /Users/ceontou/Documents/code/seer-automation-night
open -a /Applications/SeerGame.app                       # THE way to raise + focus the client
./scripts/seerctl wl                                     # window inventory, INCLUDES the aux window
printf '{"op":"win"}\n{"op":"quit"}\n' | ./native/bin/seerdx   # largest window only (never sees the aux dialog)
PYTHONPATH=.pydeps python3 tools/ctl.py shot  <path.png> # window capture, works occluded, 2400×1600
PYTHONPATH=.pydeps python3 tools/ctl.py look             # capture+OCR, prints game_px per text box
PYTHONPATH=.pydeps python3 tools/ctl.py clickpx X Y      # foreground click at game_px (has move guard + frontmost guard)
PYTHONPATH=.pydeps python3 tools/login.py --timeout 240  # announcement → login → server → confirmed in the world
```

Art-pack dialog (aux window) — the path that worked (because `tools/fix_startup.py` is broken, see §5):

```bash
screencapture -x -R<aux x>,<aux y>,<aux w>,<aux h> /tmp/aux.png
printf '{"op":"ocr","path":"/tmp/aux.png","min":0.25}\n{"op":"quit"}\n' | ./native/bin/seerdx
# 暂不导入 box is Vision-normalised bottom-left origin:
#   cx = win.x + (x + w/2) * win.w ;  cy = win.y + (1 - (y + h/2)) * win.h
printf '{"op":"clickpid","x":<cx>,"y":<cy>,"n":1}\n{"op":"quit"}\n' | ./native/bin/seerdx
```

## 3. Battery HUD (右下角电池) — coordinates + rules (verbatim)

| item | game_px | global_pt |
|---|---|---|
| battery widget (clicked here, opens the dialog) | **(2163, 1350)** | **(1349.5, 719.0)** |
| its countdown readout (OCR) | (2159–2161, 1351) | — |
| dialog 确认 button | (1047, 981) | (791.5, 534.5) |
| dialog 取消 button | (1364, 981) | (950.0, 534.5) |

**Verbatim text**

- Arrival modal: `检测到本日还未开启8倍经验与4倍学习力，可随时点击右下角电池主动开启` → button `确认` (this one dismisses fine; it is NOT the enable).
- Battery click → confirm dialog:
  ```
  本日还未开启8倍经验与4倍学习力，
  开启之后中途不可取消。
  确认要开启8倍经验与4倍学习力时间吗?
  ```
  buttons `确认` / `取消`.
- HUD: countdown **05:36 @22:53 → 05:24 @23:06**, i.e. real-time HH:MM countdown (≈5.5 h left at start of
  session); gauge = 2 lit segments (red, orange) + black; two round buttons above the gauge (speaker icon,
  orange "slash" icon).
- **NOT FOUND**: any 每日次数 / 剩余次数 / 消耗道具 text; any N倍 badge on the HUD; any "开启成功" toast.

**Current enabled state: NOT ENABLED (unconfirmed).** Four separate attempts clicked the exact `确认` pixel
(game_px 1047,981 / 1045,985 / 1047,981 / 1047,981). Each time the dialog closed within 0.35 s, but:
1) an 8-frame × 0.35 s OCR loop after the click found no success text,
2) pixel-diff of the battery ROI before/after changed only the countdown digits,
3) re-clicking the battery still returns `本日还未开启8倍经验与4倍学习力`.
So the game itself still reports the 8×/4× as **not started**. I did **not** claim it as on.
Hand-over state: dialog closed with `取消` (23:06), world idle, no modal open.

Hypotheses for the next operator (not tested): the enable may need a different hit path (double press /
press-and-hold), or it is gated on something else — research (`docs/plan-from-video.md:44`,
`docs/research/PUNI_CURRENT_VERSION_GUIDE.md:225`) says the official route is `初级班辅导员`/`高级班辅导员`
in the 精灵太空站 training room, and the "time exhausted" message is
`当日 8 倍经验与 4 倍学习力时间已耗尽`. Server-side refusal was not distinguishable from a missed click.

## 4. Bring-up timeline (all times +08:00, 2026-11-06)

| time | event | evidence |
|---|---|---|
| 22:47:19 | `open -a` → main 1200×800 @ (270,68), aux 260×258 @ (433,-693) (2nd display) | `wl_before_launch.json` |
| 22:48 | aux dialog OCR: `未检测本地有美术包，是否需要导入？` / `立即导入` / `前往下载` / `暂不导入` | `aux_dialog_2248.png` |
| 22:49 | aux dismissed; aux window **moved** (433,-693)→(574,159) between read and click | `probe_2256.jsonl` |
| 22:51:20 | login page: `航行公告` + `游戏公告`/`我已知晓`; announcement says `1.11月份版本更新已发布` | `world_t0.png` |
| 22:52:49 | `login.py` → `confirmed in the world` (啦公星 not on page 1 → number field `1` + `快速进入`) | `world_t0.png` |
| 22:53 | arrival modal (8×/4× hint) → dismissed with `确认` | `world_t0.png`, `world_clean.png` |
| 22:53 | geometry stable 1200×800 @ (268,44) → 2400×1600 px | `world_clean.png` |
| 22:56–23:06 | battery experiments (see §3) | `battery_*.png` |
| 23:06:49 | handover: in world, idle, **frontmost = SeerGame**, no modal, no loop | `world_clean_final_2306.png` |

## 5. Failures (verbatim) — fix these before the next live session

1. **`tools/fix_startup.py` is broken** — unusable as shipped:
   `NameError: name 'time' is not defined` at `tools/fix_startup.py:68` (`time.sleep(0.8)`, file has no
   `import time`). The entire one-command art-pack unstick path is therefore dead; I had to reproduce the
   capture→OCR→clickpid sequence by hand.
2. **`seerdx activate` does not raise the window on macOS 15.** It returns `{"ok":true}` while the frontmost
   app stays whatever it was (`DeepSeek Harness`, `Microsoft Edge`). `.activateAllWindows` without
   `.activateIgnoringOtherApps` is not enough. Consequence: `tools/ctl.py` `ensure_frontmost()` (12 × activate)
   will burn its retries and refuse, and `tools/login.py`'s `clear_popup()` relies on the same lane.
   **Workaround that works: `open -a /Applications/SeerGame.app`** (then `lsappinfo front` = SeerGame).
3. **`osascript -e 'tell application "SeerGame" to activate'` is worse than useless here** — the shell/osascript
   call itself brought the *harness* app back to the front (`lsappinfo front` = `DeepSeek Harness` right after).
4. **`screencapture -R` with a negative y (second display) captures the wrong content when the target window is
   occluded.** The art-pack dialog sat at (433,-693) behind the Edge/bilibili window; `-R433,-693,260,258` returned
   pixels of the *video*, not the dialog. Use `-l <windowid>` or raise the app first.
5. **Aux window moved between geometry read and click.** The click was computed from the stale rect and only
   landed on `暂不导入` by luck. Re-read `seerctl wl` after raising and immediately before the click.
6. Login ran while the user was actively using the machine (Edge/bilibili, Safari, WeChat all open). Foreground
   clicks necessarily steal focus — done only for the login/battery steps, nothing else.

## 6. Still open (read-only recon c/d/e/f + b were NOT done)

`b` character panel (pet/level/HP/PP/xp_to_next): NOT DONE (needs opening the 精灵/属性 panel).
`c` 谱尼 entry (SPT → 太空站左翼 → 谱尼, seven-seal panel): NOT DONE.
`d` 交易行 gating text: NOT DONE. Lead's note (unverified): first tab of 商城 (`交易行 / 官方商城 / 运行商人`).
`e` 经验分配器 entry: NOT DONE. Lead's note (unverified): `基地 → 基地手册 → 家具`.
`f` 试炼之塔 floor + 精灵恢复 reachability: NOT DONE.
Everything above needs a new authorized live slot; none of it is claimed as found.

---

## 7. 23:12–23:40 追加（MAX_RESULTS 时段 A，gameops）

| time | event | evidence |
|---|---|---|
| 23:12 | 精灵背包 A1：烈焰猩猩 **Lv41 HP 122/122** 个体21 天真，**升级所需经验值 6360**，PP 抓35/35 火焰车20/25 火花25/25 挑拨20/20；6 只（1 烈焰猩猩 + 5 小蘑菇，1 只 12/12 其余 0/12） | `char_panel_2312.png` |
| 23:05–23:20 | 电池 8×/4× 共 8 次尝试全部失败 → **BLOCKED_CONFIRMED（游戏/服务器拒绝，非点击问题）**：双击12ms / 长按840ms / 等2–3s再点 / 悬停500ms再点；每次弹窗正常关闭但重开仍原文「本日还未开启…」；HUD ROI 像素差仅倒计时数字。对照：点弹窗**空白处不关窗**（只有按钮关），且 x=2163 的电池、x=1627 的登录按钮都点得中 → 排除坐标漂移 | `battery_attempt*_*.png` |
| 23:21 | `battle_run.py --battles 1` 失败：wins=0 events=0 "no progress for 60s"；其日志反复 `OCR: no confirm button` → **该场景「确认」模板假阳性，卡在 DIALOG 状态，从未寻怪**（不是执行器坏） | `battle_xp_test.json` |
| 23:23 | 单击蘑菇怪（模板分 0.882，game_px 1400,1130）→ 角色走到蘑菇上、**未进战斗** | `battle_enter_2323.png` |
| 23:31 | 已验证技能 `game.run_battle(battles=1)`（seerauto 状态机）→ **PASS 1胜0负 42s**，结算原文 **`XIN精灵获得经验：52`**，reward xp_to_next=5830；PP 火焰车20→18，HP 122/122 未掉 | `char_panel_after_battle_2330.png` |
| 23:30 | 战后精灵面板：**Lv41，升级所需经验值 5830**（23:12 为 6360，delta 530；我这一场只有 52 → 缺口无法归属，怀疑有人 23:12–23:30 手动打过，佐证：公屏 `交易 hello：不是只有金豆吗`，hello 是我们的角色名） | 同上 |
| 23:32 | **客户端自掉回登录页**（无 500033 弹窗被捕获），23:34:35 `login.py` 重登成功 confirmed in the world | — |
| 23:38 | 交接态：在世界（太空站卫兵房）、无弹窗、电池 HUD 04:56、窗口 1200×800 pt @ (268,44) → 2400×1600 px、frontmost SeerGame、**无任何点击循环** | `state_now_2338.png` |

### 结论（一手可引用）
- **单场野怪经验（蘑菇怪，Lv41 烈焰猩猩）原文 = 52** → 与老基线一致，**8 倍经验未生效**；不要用 xp_to_next 的 530 缺口反推 8 倍（缺口另有来源）。
- 8×/4× 电池路径：**BLOCKED_CONFIRMED**，今晚不必再试。
- 执行器：`game.run_battle` 单场链路**正常**（自动打赢）；`tools/battle_run.py` 在克洛斯星林间有「确认」模板假阳性，需要修模板/ROI 才能在此场景用。
- 未做（时间不足 / Lead 接手）：STEP 2 辅导员 NPC、STEP 3 交易行 tab、STEP 4 谱尼封印面板、STEP 5 阵容配置入口、A2.5 roster 全量（仅有 A1 的 6 只读数）。

---

## 8. 试炼之塔 — 实测定标（2026-11-06 23:47 → 2026-11-07 00:27，可交给 tower_grind）

> 全部为**一手真机读数**。几何：win id=3706, **1200×800 pt @ (270,74) → 2400×1600 px**（23:43 重启后的新位置，旧的 (268,44) 作废）。
> 换算：`global_pt = (270 + game_px_x/2, 74 + game_px_y/2)`。

### 8.1 路径常数（game_px）

| 步骤 | 坐标 / 判据 |
|---|---|
| 到训练室 | `game.travel(destination="tutor_room")` → 房内 OCR 有 `初级班辅导员`/`高级班辅导员` |
| 开塔对话框 | 点机器 **(1195,1150)** → 出现 `选择楼层` / `继续挑战` |
| 对话框原文 | `挑战试炼之塔`；`试炼之塔是锻炼赛尔精灵训练技术的第一步，进行训练的同时也能获得一些奖励哦，不过一旦失败就需要重新开始了，如果你准备好了就开始挑战吧。`；`你当前到达第 23层`（=历史最高，不是当前层） |
| 选择楼层 | **(1484,887)** → 弹出 **1 / 11 / 21** 三预设：**(1895,795) / (1895,877) / (1895,956)** |
| 继续挑战 | **(1481,1007)**（进"当前最高层"） |
| **点预设 1 的实测结果** | **直接传送进塔内第 1 层**（地面画 `01`，底栏消失）→ **回低层可行**，`你当前到达第23层` 不变 |
| 塔内层数 | 地面大数字，ROI 约 **(1170,1225)-(1310,1300)**，OCR 得到 `01` / `02`（两位数字，青色） |
| 塔内左下装置 | **(720,1090)** = `精灵背包`（悬停实测）→ 塔内也能读精灵面板 |
| 塔内右下装置 | (1730,1090) 点/悬停无反应（no-op） |
| **开战** | 点**守卫者精灵本体 (1190,760)**：**第 1 次点击只是走过去，第 2 次点同一位置才开战**（第 1 层实测通过） |
| 战斗按钮 | 战斗 ≈ (1838,1190)（模板 `live_fight_btn`，ROI 1710,1130–2040,1260）；`道具` (2015,1190)、`精灵` (1840,1325)、`逃跑` (2013,1323) |
| 胜利 | `live_victory_word` ROI 980,640–1430,790；`确认` **(1191,997)** |
| 结算链（依次点 确认） | ①`XIN精灵获得经验：N` + `离升级还需经验：M` → 确认 **(1188,999)** ②`恭喜你得到了N个积累经验` → 确认 **(1198,983)** ③`恭喜你得到了N个XIN豆` → 确认 **(1198,983)** ④`1个神奇扭蛋牌已经放入你的储存箱！` → 确认 **(1198,988)** |
| 胜利后 | 地面层数自动 +1（01→02），**不需要重新进塔**；塔内**无底栏、ESC 无效、右下装置无效**，离开塔的方式**未测定** |

### 8.2 一手读数

| 项 | 值 | 证据 |
|---|---|---|
| 第 1 层守卫者 | **LV82 布布花**，2 回合被击杀（火焰车），我方 HP 122/122 未掉 | `tower_engage_guardian_body_0001.png` |
| 第 1 层经验 | **`XIN精灵获得经验：1710`**，`离升级还需经验：4650` | `towerD_after_victory_0002.png` |
| 额外奖励 | `5 个积累经验`、`200 XIN豆`、`1 个神奇扭蛋牌` | `towerD_dialog1..3_0002.png` |
| PP 成本 | 火焰车 17/25 → 16/25（**每层约 1 点**）→ 25 点≈12–20 层后需要补 PP；**塔内/背包均未发现「精灵恢复」或 PP 恢复入口** | 面板 + 战斗 UI OCR |
| 精灵面板（塔内读） | Lv41，升级所需 4650，HP 122/122，PP 抓35/35 火焰车16/25 火花25/25 挑拨20/20 | `tower_petbag_0004.png` |
| 完整一轮计时 | **未干净测量**（我的操作含摸索）：第1层 从点守护者到关完全部结算 ≈ **2.5 min（含我 4 次 OCR 等待）**，不含 OCR 冗余估计 60–90 s。**不要用这个外推速率** | — |
| 失败路径 | **未观测**（今天没打输过）；文档记载弹 `很遗憾，这次挑战未能获胜`，需自然输一局复核 | — |
| 第 2 层 | 尝试开战失败：点两次守卫者后**没有进战斗，直接回到了一个有底栏的房间**（原因未查明，可能是点到了楼梯/被判定离开）。**这是 tower_grind 必须先用 1 层验证的风险点** | `tower2_engaged_0006.png` |

### 8.3 失败关键词（供 tower_grind 判定）
- 未进塔：「挑战试炼之塔」对话框仍在 → 继续点 `选择楼层` 预设
- 未开战：画面仍是塔内、无 `当前回合：` → 再点一次守卫者本体 (1190,760)
- 已胜利：`胜利` + `确认` → 走 8.1 的结算链
- 疑似失败：出现 `很遗憾`/`未能获胜`（**未验证**）

### 8.4 P2（经验分配器／基地）— PARTIAL
- 底栏图标悬停标定（本场景）：截图底部 聊天(470) 表情(540) 组队(960) 地图(1140) 储存箱(1400) 战队(1600) 好友(1700) **基地(1800)**。
- 点 **基地 (1800,1355)** → 进入基地房间（大空房间，顶部有 `hello` 名牌，场景 OCR：`汇聚各种挑战难度的太空站是展现精灵能力的最佳场所` + `荣誉大厅的克罗和小克` 提示）。**基地是空的：没看到经验分配器家具**。
- **基地手册 / 家具** 入口：点名牌 σ点右侧图标栏 4 个位置悬停均无提示 → **NOT FOUND（10 分钟预算内）**。`积累经验` 当前值也**没读到**（赛尔资料面板入口同样未定位）。
- 发明室经验接收器 (1850,1130) 的 +2000 可重复性：**未测试**（时间不够）。

### 8.5 第 2 层实测（2026-11-07 00:20–00:32）— 补充 8.2

| 项 | 一手读数 | 证据 |
|---|---|---|
| 第 2 层守卫者 | **LV84 巴鲁斯**，2 回合被击杀（火焰车），HP 122/122 未掉 | `f2fight_end_0029.png` |
| 第 2 层经验 | **`XIN精灵获得经验：3003`**（OCR 分两段：`XIN精灵获得经验：-` + `3003`） | 见下 |
| **升级** | **`成功升级到了42级`** + `等级：+1 体力：+3 攻击：+2 防守：+2 特攻：+2 特防：+2 速度：+3`；新 HP 上限 125 | `floor2_rewards_done_0030.png` |
| 额外奖励 | `24 个积累经验`、`200 XIN豆`、`1 个神奇扭蛋牌` | 同上 |
| 层数 | 01 → 02 → **03**（每层 +1） | 同上 |
| PP | 火焰车 16/25 → 15/25 → 14/25（**每层 1 点**） | 战斗 UI |

**层间经验是递增的**：第 1 层 1710、第 2 层 3003（差 1293）→ 不要用 1710 外推全部层。
**塔内升级会多一个「成功升级到了42级」面板**（带属性变化），tower_grind 的结算链必须能识别并关掉它（实测它是排在 XP 面板后的第 2 个 确认）。

**下一层（03）时的状态**：Lv42、HP 125、火焰车 14/25、层数 03，仍在塔内，**未离开**。
