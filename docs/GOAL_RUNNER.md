# GOAL RUNNER — 架构、契约与为何这样分层

> 这一轮把项目从"战斗脚本"升级为"AI 辅助的长期目标运行器"。
> 本文只讲**已实现并在真机跑过的**东西；未实现的部分在最后一节明确列出。

---

## 1. 分层（代码里的强制边界，不是口号）

```
User Goal                     "获取谱尼"
   │
   ▼
Planner / Supervisor          seerauto/planner/     只选节点 + 解释，不产生坐标
   │                          deterministic | ai(OpenAI 兼容) | bridge(文件)
   ▼
Goal Graph + World State      seerauto/goals/puni.yaml (42 节点)
   │                          seerauto/world/model.py (GameWorldState + Evidence)
   ▼
Task Scheduler (Gate)         seerauto/runner/runner.py  校验决策：图/能力/预算/授权
   │
   ▼
Verified Semantic Skills      seerauto/skills/{act,observe,parsers}.py
   │                          每个都有输入契约、前后置条件、预算、可观测后置条件
   ▼
Deterministic Vision Executor seerauto/live.py           唯一接触像素与鼠标的地方
   │
   ▼
Real Game                     /Applications/SeerGame.app
```

**铁律**：AI 不在高频环路里。一次运行的 AI 调用次数是个位数，
战斗循环、等待、重试全部由本地状态机完成。

---

## 2. 三份数据

| 数据 | 位置 | 特点 |
|---|---|---|
| 目标图 | `seerauto/goals/puni.yaml` | 42 节点，机器可读；每个节点有前置、完成谓词、预算、失败策略、证据来源 |
| 世界状态 | `seerauto/world/model.py` + SQLite | 每个值都带 `Evidence(source, observed_at, origin, confidence)` |
| 运行记录 | `data/goalrunner.sqlite3` | `goal_runs / node_states / world_snapshots / events / checkpoints / ai_calls` |

### 2.1 证据等级（决定"能不能算完成"）

```
LIVE_VERIFIED  >  REPLAY_VERIFIED  >  INFERRED  >  HISTORICAL  >  UNKNOWN
   本次真机观测      回放帧集          规则推断      文档旧说法       没看到
```

只有前三者能让节点完成。`docs/state.md` 说"约 15 级"是 `HISTORICAL`：
它**永远不能**让"练到 36 级"这个节点完成——必须先现场观测。
`UNKNOWN` 不是 `false`：没观测过 SPT 进度 ≠ 没打过 SPT。

### 2.2 完成判据只有一种可自动成立的类型

```yaml
completion:
  type: verified_account_state        # 唯一能自动完成的类型
  predicate: pet_level_at_least
  args: {pet: 烈火猴, level: 36, aliases: [小火猴, 烈焰猩猩]}
```

另一类是 `capability_demonstrated`（横切能力节点），**任何情况下都不会自动完成**：
必须由人/工具显式写入，防止"模型说它能跑就算它能跑"。

谓词读的是**当前世界的证据**。等级是 `HISTORICAL` 时谓词返回 `None`（不确定），
节点状态是 `missing_prereq`，不是 `blocked`，更不是 `completed`。

---

## 3. 一拍的完整生命周期

```
loop:
  1. observe()          # 抓帧 + 模板判定场景 + 需要时才 OCR；与上一步的世界合并，
                        # 已观测过的事实不会被无证据的新帧抹成 UNKNOWN
  2. preflight()        # 规划所必需但还没有的事实：先"看一眼"
                        # 例：精灵等级（战斗 HUD / 精灵面板）
  3. build_request()    # 世界摘要 + 42 节点摘要 + 能力清单 + 预算 + 最近事件
  4. planner.decide()   # → {selected_node, capability, reason, expected_observation,
                        #    max_actions, max_duration_s, requires_approval}
  5. gate()             # 拒绝：未知节点 / 不可执行 / 无 playbook / 超预算 / 未知能力
  6. execute_node()     # playbook → 技能序列（先导航，再战斗），每步都受预算与暂停约束
  7. fold()             # 技能执行中观测到的事实（战斗 HUD 的 LV:34）回写世界
  8. verify_node()      # 重新观测 → 用图里的谓词判定是否真的完成
  9. checkpoint()       # 世界快照 + 节点状态 + 证据 一次事务提交
 10. budget/stop/pause  # 下一拍前统一检查
```

关键点：**技能返回 `ok` 不算完成**。第 8 步是唯一的完成判定，
它读的是重新观测到的世界，而不是技能的自述。

---

## 4. Playbook：节点 → 技能序列

目标图说"要变成什么"，playbook 说"用什么已验证的动作去变"：

```python
"puni.prepare.level_up": (
    Step("game.navigate_spt_boss", {"boss": "蘑菇怪"}, when="no_monster_idle"),
    Step("game.run_battle", {"battles": 10, "monster": "蘑菇怪", "max_seconds": 420}),
)
```

* `when` 是**可验证的条件**（不在战斗中 / 屏幕上没有可打的怪），不是时间；
* 没有 playbook 的节点 **不会被调度**（`gate` 直接拒绝），
  不会被"即兴发挥"地执行 —— 这是防止点进未知 UI 的主要手段；
* 战斗次数在 playbook 里是"上限"，运行器还会用剩余预算再夹一次。

---

## 5. 技能契约

`seerauto/skills/base.py::SkillSpec` 强制每个技能声明：

| 字段 | 用途 |
|---|---|
| `inputs` / `outputs` | 类型化契约，调用前校验（未知参数直接 `SkillError`） |
| `preconditions` / `postconditions` | 对世界状态的谓词 |
| `max_duration_s` / `max_actions` | 技能自己执行的硬预算 |
| `requires_input` / `requires_approval` | 安全闸门 |
| `verified` | `real`（真机验证过）/ `replay`（回放验证）/ `unverified` |
| `evidence` | 真实证据路径（截图、日志、文档锚点） |

返回值不是 `ok: true`，而是：

```json
{"status": "PASS", "before": {...}, "after": {"wins": 1, "xp": 106, "ally_level": 34},
 "changed": true, "proof": "1 场胜利 ... 经验 +106", "actions": 41, "evidence": [...]}
```

`changed` 由**观测**算出（战斗前后等级、胜利面板是否真的消失），不是由点击次数算出。

已实现的技能：

| 技能 | 类型 | 真机状态 |
|---|---|---|
| `game.get_status` / `game.get_current_scene` / `game.observe` | observe | ✅ real |
| `game.inspect_pets` / `game.inspect_resources` / `game.inspect_spt_progress` | observe | ⚠️ replay / unverified |
| `game.open_panel` / `game.close_panel` | act | ✅ real |
| `game.navigate_spt_boss` | act | ✅ real（蘑菇怪、钢牙鲨两张卡验证过） |
| `game.run_battle` | act | ✅ real |
| `game.read_active_pet_level` | act | ✅ real |
| `game.read_pet_bag` | observe | ✅ real（模板定位精灵背包；读全员 HP / 等级 / 升级所需经验，返回 `can_battle`） |
| `game.travel` | act | ✅ real（星图命名目的地 + 到达验证） |
| `game.goto_puni_entrance` | act | ✅ real（SPT→太空站左翼→谱尼；用模板确认「勇者之塔」传送门） |
| `game.goto_tutor_room` | act | ✅ real（8 倍经验前置；委托给 `game.travel`） |
| `game.click_text` | act | ⚠️ 探索用原语（点文字并把整屏文字返回） |
| `game.confirm_known_dialog` | act | ✅ real |

---

## 6. 中断与恢复

* **暂停**：`goal pause` 写控制文件；运行器在下个观测周期停止动作，
  恢复时**强制重新观测**（暂停期间游戏仍在推进，旧假设会点空）。
* **停止**：`goal stop` 写文件；当前技能结束后写 checkpoint 并退出。
* **崩溃**：`finally` 里仍会写 checkpoint 与 `run.stopped` 事件。
* **重启**：`goal resume` 用同一个 `run_id`，先重新观测、比对已完成节点，
  再继续。已完成的节点**不会重跑**（`node_states` 里是 `completed`）。
* **单一执行权**：`/tmp/seerauto.executor.lock` 上的 `flock`；
  第二个进程直接 `refused: another run already owns the executor`。

运行记录字段（`goal_runs`）：
`run_id / goal / planner / status / current_node / node_state / budgets / stats /
stop_reason / last_error / next_recovery_action / started_at / updated_at`。

---

## 7. AI 边界

| AI 做 | AI 不做 |
|---|---|
| 判断目标是否可达 | 逐帧识别画面 |
| 找缺失的前置条件 | 高频战斗循环 |
| 选高价值且当前可执行的子任务 | 生成像素坐标 |
| 解释阻塞 | 执行任意 shell |
| 依据新证据重规划 | 改内存 / 绕反作弊 |
| 识别需要人决策的情况 | 未确认就上传截图 / 花钱 |

三档规划器：

* `deterministic`：纯本地，按优先级 + 可执行性选；无网络；用于测试与降级；
* `ai`：OpenAI 兼容 `/chat/completions`（stdlib `urllib`，无 SDK）。
  有超时、调用上限、费用记录；异常一律抛错，**绝不返回编造的决策**；
* `bridge`：把 `PlanRequest` 写成文件，等上位 Agent（例如 DeepSeek Harness 会话）
  写回 `PlanDecision`。这是"人类/上位 Agent 在环"的正规通道，带 seq 与 mtime 防串话。

---

## 8. 调度闸门拒绝什么

`GoalRunner.gate()` 会拒绝（并把拒绝理由写进事件表）：

1. 不在目标图里的节点；
2. `executable: false` 的节点；
3. 没有 playbook 的节点；
4. 不在技能注册表里的能力/技能；
5. 超过运行预算的 `max_actions` / `max_duration_s`；
6. 需要人工批准但当前没开输入。

拒绝不是"修正"：运行器不会替模型把不合法决策改成合法决策。

---

## 9. 这一轮**没有**做的（明确边界）

* 42 个节点里只有 1 个是可执行的（`puni.prepare.level_up`），其余保持
  `blocked / unknown / not_executable`，并写明缺哪个能力或哪次观测；
* 失败（战败）分支仍无样本，规则保持禁用；
* 四技能选择、换精灵、用药、交易行、融合、抓宠仍未实现；
* 没有 GUI（CLI + SQLite + Markdown 报告优先）；
* 没有做 MCP Server / DSH 插件封装 —— 语义接口已经稳定，但这一轮不做包装。


---

## 10. 硬阻塞（halt）语义

技能可以返回 `status=BLOCKED` + `data.recovery`。运行器看到它会：

1. 把节点标成 `blocked`（不是 `attempted`）；
2. 把 `next_recovery_action` 写进 `goal_runs`；
3. **干净停机**，不消耗 error budget、不重试、不瞎点。

已实现的 recovery：`heal_pet`（全员 0 血）。这是长程任务里"知道自己该停下"的能力。
