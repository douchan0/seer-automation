> **2026-11-07 最新交接：分支合并后暂停。** 权威代码基准已合并至main，见 `docs/BRANCH_MERGE_20261107.md`；A/B/C未通过真机验收。用户明确说“开始”前不恢复游戏输入，不执行下文历史实验计划。

# NEXT_ACTIONS — 下一轮执行计划

## 【2026-11-06 晨】下一轮计划（最新，优先读这一段）

> 本轮做了什么、证据在哪：`docs/OVERNIGHT_REPORT_2026_11_06.md`、
> `docs/OVERNIGHT_PLAN_2026_11_05.md`（作战计划）、`docs/LIVE_RUN_PROTOCOL.md`（真机照抄步骤）。

### 一句话状态

**目标图第一次有东西可排程**（新增 `puni.prepare.level_45/60/80` 刷塔练级节点，
`goal start --allow-input` 会被 planner 选中并执行），
**但"能力 → 节点"的依赖方向是错的**：46→49 个节点里 executable 只有 6 个，
补一个能力最多只立刻解开 1 个节点（不是 13 个，见审查 B-11）。

### P0 · 重建目标图的依赖形状（不做这个，后面每一步都卡）

| 任务 | 做什么 | 验收 |
|---|---|---|
| 0.1 | 把 `not_executable: 29` 逐个归类：**缺前置节点** / **缺能力** / **机制 UNKNOWN** | 一张表，每行给出"补什么会解开它" |
| 0.2 | 对"缺能力"的节点，先补**已实现但未登记**的能力（`battle.skill_select`/`pp_read`/`hp_read`/`log_read` 都在代码里跑着，只是 `verified: unverified`） | 真机打一场 → 用 `capability_demonstrated` 谓词登记，并把证据路径写进 yaml |
| 0.3 | 删掉/降级"按设计不该跑"的节点，让 planner 的 blocker 列表只剩真问题 | `goal start` 的拒绝理由 ≤ 5 条且每条可行动 |

### P1 · 找到经验分配器（唯一"不打架也能升级"的通道）

`docs/MACHINE_MAP.md` 仍记 NOT FOUND。三条线索按序：
1. 精灵背包**顶部**图标行（`tools/probe_panels.py --region 900,440,2350,580 --step 64`）；
2. 战斗结算界面（先打一场，再扫结算画面）；
3. 未进入过的 `动力室` / `精灵中心`。

找到后：`parse_exp_allocator` 已能读面板，`game.give_exp` 已能灌注并**用面板自身证明**等级上升。
验收：面板前后 `等级` / `积累经验` 两个数都变，且 `world.unlocked["exp_giver.poured"]` 被写入。

### P2 · 交易行前置的真机读数（只读，30 分钟）

打开交易行 → OCR 前置条目原文与进度；打开 SPT 面板 → 确认「谱尼 / 太空站左翼」卡片状态。
验收：`puni.prepare.market_unlock` 从 `unknown` 变成有具体证据的状态。

### P3 · PR #1 的去留（需要人决定）

PR #1 基于过期 `origin/main`，缺本地 10 个 commit / +15065 行；其独有修复（绝对截止、
退出码透传、暂停 checkpoint、resume 保留通道）已移植到 `night/20261105-puni-v2`。
建议：**关闭 PR #1**，或让它重基于本地 `main` 后重开。不要直接合并。

---

## 【2026-11-05 夜】本轮结束后的下一轮计划（最新，优先读这一段）

> 本轮做了什么、证据在哪：`docs/OVERNIGHT_REPORT_2026_11_05.md`。
> 谱尼机制：`docs/research/PUNI_CURRENT_VERSION_GUIDE.md`；缺口：`docs/research/PUNI_GOAL_GAP_ANALYSIS.md`。

### 一句话状态

**长程执行器不再是"空转也算成功"了**：`goal start` 现在把「无事可做」返回 exit 3，
supervisor 连续 2 批无任务就停机；刷塔节点真正接上了连战；谱尼机制研究有 B 级来源支撑。
**但谱尼进度仍然只有 1 个节点（tower_farm）是真机完成的**，其余全靠没解锁的 UI。

### P0 · 一次「只读采样」就能解锁 7 个节点（30 分钟，不打任何一场）

| 任务 | 做什么 | 验收 |
|---|---|---|
| 0.1 | 打开 SPT → 太空站左翼 → 谱尼，截「七封印选择面板」 | 得到 `artifacts/round7/seal_panel.png`，能读出 7 个封印名与通过状态 |
| 0.2 | 同一面板上读「今日剩余挑战次数」 | 得到一个数字 + 证据字符串，写入 `unlocked.puni.daily_attempts` |
| 0.3 | 打开交易行搜「萨拉」（**只看不买**） | 读到价格 + 是否可购买，解锁 `market.buy` 的 unverified 状态 |
| 0.4 | 打开商城读「高级体力药剂」价格与货币余额 | 写入 `resources.*`，解锁 `buy_potions` |

> 为什么排第一：这 4 张截图能把 `puni.seal.*`（7 个）、`buy_*`（4 个）、`buy_potions` 从
> 「没有 UI 就只能猜坐标」变成「可以写模板」。**在拿到之前不要写任何封印相关的点击代码。**

### P1 · 真身命数检测 ✅ 已完成（2026-11-05）

| 任务 | 状态 |
|---|---|
| 1.1 `game.observe_truebody_life` | ✅ 已实现；104 帧里 74 帧可见，**解析 74/74**（`tests/test_truebody_life.py`，fixture `tests/fixtures/truebody_life_frames.json`） |
| 1.2 写 `counters.truebody.life` | ✅ 已写，证据 LIVE；节点 `puni.truebody.detect_life` todo → partial |
| 1.3 接 `BattleWatchdog` | ⚠️ 部分：快照路径已默认注入 `data/battle/<run>.jsonl`；**真身节点的 `battle_max_turns` 尚未在 playbook 里设定**（因为还没有真身 playbook） |

**剩下的一步**：在真身战斗里实时跑一次，把 `verified` 从 `replay` 升到 `real`。

### P2 · 战报事件解析（所有分支判断的公共依赖）

| 任务 | 文件 | 验收 |
|---|---|---|
| 2.1 | `game.observe_battle_log`：解析「当前战报」→ `被击败了/致命一击/使用了X/圣光气` | 离线用 `frames/_ocr.json` 做回放 |
| 2.2 | 用「圣光气」出现判空 PP，喂给 `puni.seal.boss_pp_empty` | 谓词可判 |

### P3 · 补谓词（现在表达不了攻略的门槛）

`pet_stat_at_least`（闪光波克尔速度≥319）、`pet_stat_equals`（克罗达特防=6）、
`pet_skill_known`（哈莫带回避/塞维尔带锁敌）、`daily_attempts_left`（每日 4 次）。

---

## 【历史】Round 5 及之前的计划（保留，勿删）

> 给**下一个接手的 Agent**。每条都有：做什么、改哪个文件、怎么验收、卡在哪。
> Round 5 的真机结果见 `docs/REAL_DELIVERY_REPORT.md`；本轮 FAIL 的那一项写在最前面。

---

## 当前一句话状态

**Goal Runner 全链路已通，但谱尼进度仍为 0 个节点完成。**
本轮真机做到：登录 → SPT 面板全量读取 → 精灵面板 Lv34 → 战斗 1 胜（+106 XP）→
暂停/恢复/停止 → AI bridge 决策闭环。**没做到：把等级从 34 推到 36。**

**下一轮唯一最重要的事：提高单位时间 XP，然后真正拿到第一级。**

---

## 第 0 步（P0，必须先做，30 分钟）— 清掉"53 经验弹窗"这个吞预算的洞

现象：战斗结束后会持续弹出「恭喜你得到了 53 个积累经验」，它是**纯文字弹窗**，
没有模板可匹配，会挡住精灵面板、也会吞掉点怪点击。本轮用 OCR 兜底清了一部分，
但一次运行里仍被它吞掉整轮预算（`run 19745e9a764d`: 28 次点击 / 0 场胜利）。

| 任务 | 文件 | 验收 |
|---|---|---|
| 0.1 给这类弹窗建模板（截图 + 匹配） | `seerauto/templates/live/live_reward_xp_btn.png` | 真机战斗中稳定命中 |
| 0.2 把"未知弹窗"做成通用处理：模板 → OCR 确认词 → 点；仍无解则暂停并报告 | `seerauto/skills/act.py::_dismiss_dialogs` | 单测 + 真机各一次 |
| 0.3 观测阶段先清屏再做识别 | `seerauto/runner/runner.py::observe` | 精灵面板读取成功率 100% |

---

## 第 1 步（P0）— 找到 8 倍经验（`puni.prepare.exp_boost`）

按 `docs/PUNI_TASK_GRAPH.md` P0-03：精灵太空站，与**初级班/高级班辅导员**对话，
开启「当日 8 倍经验 / 4 倍学习力」。这是把 38 场/级降到 5 场/级的关键。

| 任务 | 怎么验 |
|---|---|
| 1.1 在太空站里找到辅导员 NPC 并标定 | OCR 出现「8倍经验」字样 |
| 1.2 点击开关，读回提示「当日8倍经验与4倍学习力时间已开启」 | 截图 + 事件 |
| 1.3 加进 playbook：`puni.prepare.exp_boost` 节点 + `game.enable_exp_boost` 技能 | 谓词 `feature_unlocked(exp_boost_8x)` |

---

## 第 2 步（P0）— 把 `puni.prepare.level_up` 真正跑完

* 预算要够：`--max-battles 45 --max-minutes 50`，可中断可 resume；
* 每场战斗后把「离升级还需经验：N」写进事件（本轮已实现解析，未稳定落地）；
* 完成判据不变：`pet_level_at_least(烈火猴, 36)` 在**实时证据**上成立。

---

## 第 3 步（P1）— 交易行门槛的一次针对性实验

面板原文「击败后解锁交易行资格」。要回答的是**"击败几个/哪些"**：

1. 现在已击败 1 个（蘑菇怪）→ 打开交易行入口看是否可用（入口未找到，先找入口）；
2. 若不可用，再击败 1 个（推荐 钢牙鲨：同一帕诺星系、Lv34 可尝试）→ 再看；
3. 结论写进 `puni.prepare.market_unlock` 的 `completion.args`。

---

## 第 4 步（P1）— 补战斗深度能力

`battle.skill_select`（四技能）、`battle.switch_pet`、`battle.use_item`、
`battle.defeat`（战败分支，仍需先造样本）。
这三个是封印 1–7 的前置，优先级高于任何 UI 打磨。

---

## 环境与命令速查（真机已验证）

```bash
./scripts/bootstrap_mac.sh                                # 装依赖 + 编译 seerdx + 自检
PYTHONPATH=.pydeps python3 -m pytest -q                   # 426 passed, 2 skipped
PYTHONPATH=.pydeps python3 -m seerauto doctor             # 如实报窗口/权限
PYTHONPATH=.pydeps python3 -m seerauto goal start puni --planner deterministic --dry-run
PYTHONPATH=.pydeps python3 -m seerauto goal start puni --planner ai --allow-input \
    --max-minutes 60 --max-battles 12 --max-spend 0
PYTHONPATH=.pydeps python3 -m seerauto goal status|pause|resume|stop|report
```

**三条必须遵守的真机纪律**（本轮又踩到）：
1. 点击前确认游戏窗口在最前（`ensure_frontmost`）；
2. 有常驻流时不要用 `shot`（返回近空白图），要用暖流帧导出；
3. **面板/弹窗必须先清干净再操作**——底栏图标位置随场景漂移，固定坐标不可靠。

---

## 历史步骤（Round 4 及之前，已完成，保留追溯）

- ~~B1 启动游戏客户端~~ ✅ 2026-11-01
- ~~重新标定坐标系~~ ✅ `docs/state.md`
- ~~单场真实战斗闭环 / 3 场连战~~ ✅ Round 4
- ~~暂停/恢复/停止~~ ✅ Round 4 + Round 5（SIGTERM 修复）

---

## 仍待标定（`checklist.md` / `docs/state.md` 的旧表）

底栏消息/表情/公告/任务/赛尔资料/战队/背包/仓库/精灵背包、左上官方商城、
顶部万圣活动/巅峰排位/任务档案、右列白卷轴、地图面板关闭 X 与星球格
——**Round 5 又确认了：底栏图标位置随场景改变**（太空站 vs 林间不同），
所以不要补"固定坐标表"，要做模板定位表。

## 2026-11-07 下一轮（由昨夜双模式实验得出，按优先级）
1. **让塔无人值守**（决定"能不能整夜自动练级"）：补怪物模板/通用战斗检测（当前只认少数怪，第 2 层 `no engageable monster`）；补"出塔→大厅回血→训练室→预设 1"闭环（`--recover-only` 已修到六步但未再真机验证）。
2. **读一次谱尼封印面板**（10 秒只读）：七封印条目 / 每日次数 / 前置条件原文 —— 决定路线要不要继续。
3. **经验分配器**（用户确认在基地）：基地是空房间，需要找"基地手册/家具"入口或确认本服未保留；顺便读 `积累经验` 池值。
4. **抓宠标定**：`seerauto/config/catch_calibration.json` 三个 game_px（道具按钮/胶囊格/使用按钮）+ 战报 ROI；塔结算已确认会掉「3 个中级精灵胶囊」，胶囊来源不用另找。
5. **Token 架构**：每阶段开新 session（HANDOVER），限制子 agent 会话长度 —— 昨夜 78% Token 花在子 agent 上（207.6M vs root 58.4M）。
6. 交易行真门槛：商城首 tab（3 分钟只读）。
