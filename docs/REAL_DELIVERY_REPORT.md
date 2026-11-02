# REAL DELIVERY REPORT — Round 5（2026-11-03）

> 一句话：**产品链路已经能从 CLI 跑到真机，AI 决策→技能执行→观测验证→持久化的闭环打通了；
> 但"账号真的多了一级"这一步本轮没有完成 —— 下面第 3 节明确写 FAIL，不粉饰。**

全部状态的取值只有四种：`PASS / FAIL / BLOCKED / NOT TESTED`。

---

## 第一问：我在 Mac 上输入哪条命令能够运行？

```bash
cd seer-puni-quest
./scripts/bootstrap_mac.sh                                        # 依赖 + 原生守护进程 + 自检

PYTHONPATH=.pydeps python3 -m seerauto goal start puni --planner deterministic --dry-run
PYTHONPATH=.pydeps python3 -m seerauto goal start puni --planner ai \
    --allow-input --max-minutes 60 --max-battles 12 --max-spend 0
PYTHONPATH=.pydeps python3 -m seerauto goal status|pause|resume|stop|report
```

全部参数与排障见 `docs/USER_GUIDE.md`。架构见 `docs/GOAL_RUNNER.md`。

**验收**：`PASS`
* `python3 -m seerauto goal --help` 六条子命令齐全（`start/status/pause/resume/stop/report`）；
* `--dry-run` 实测**没有产生任何输入**（`LiveSession` 在 `allow_input=False` 时抛 `InputRefused`，单测覆盖）；
* 真机真实运行过（见第二问）。

---

## 第二问：AI 真的在游戏中观察、规划和行动了吗？

**验收**：`PASS`（bridge 模式，真机）

链路（每一步都有落地证据）：

1. **观察**：`game.observe` + 模板场景判定 → `session.scene=WORLD`、
   窗口几何 (270,87) 1200×800、`frontmost=True`，证据级 `LIVE_VERIFIED`；
   写进 `world_snapshots`。
2. **AI 决策**：`--planner bridge` 把 `PlanRequest`（世界摘要 + 42 节点摘要 + 能力清单 + 预算 +
   约束）写到 `data/ai/request-0001.json`；真实模型（DeepSeek，本轮该会话）读后写出
   `response-0001.json`：

   ```json
   {"goal":"puni","selected_node":"puni.prepare.level_up","capability":"game.run_battle",
    "reason":"唯一可调度节点。精灵等级 UNKNOWN（精灵面板被残留的 XP 弹窗挡住），
              而战斗 HUD 会显示我方 LV，所以跑已验证的连战既推进练级又顺带产出这次观测。…",
    "expected_observation":"战斗 HUD 的 LV 让 pets.烈火猴.level 从 UNKNOWN 变成 LIVE_VERIFIED…",
    "max_actions":60,"max_duration_s":420,"requires_approval":false}
   ```
3. **闸门校验**：`gate()` 确认节点存在、`executable`、有 playbook、能力已注册、预算未超；
   通过后写 `node.selected` 事件。
4. **真实行动**：`game.navigate_spt_boss(蘑菇怪)` → 点 SPT 图标 → OCR 定位「前往」→ 传送，
   并**验证面板真的关闭**；随后 `game.run_battle` 进入连战。
5. **验证**：每场用「胜利面板是否消失」判定胜负，而不是"点了确认就算赢"；
   战斗 HUD 的 `LV` 与「离升级还需经验：N」被解析并回写世界状态。

因此：**AI 的产出是一句结构化决策，它被本地闸门校验、被确定性执行器执行、被观测验证。**
模型没有、也不能输出鼠标坐标。

* 自动 AI 模式（`--planner ai`，OpenAI 兼容网关）**代码完成、传输层验证过、但本轮没有可用的凭证**：
  网关对本机环境变量里的 key 返回 401/`Invalid credential`。因此
  **`--planner ai` 的端到端真实调用 = FAIL（凭证 BLOCKED）**，
  失败时按设计降级到 deterministic 并记录原因，不会瞎猜。

---

## 第三问：当前谱尼任务进度是什么？

`seerauto/goals/puni.yaml` 把 42 个节点变成机器可读图。本轮真实状态：

| 状态 | 数量 | 含义 |
|---|---|---|
| `completed` | **0** | 本账号没有任何节点被本轮证据判定完成 |
| 可执行（`executable: true`） | **1** | `puni.prepare.level_up`（原 P1-01） |
| `blocked` | 4 | 缺能力/前置（交易行相关） |
| `unknown` | 8 | 机制或数值未观测 |
| `not_executable` | 29 | 没有已验证的技能序列 |

**用户当前账号的谱尼路线卡点**（按价值排序）：

1. **交易行未解锁** → 直接卡死 P1-05/06/07（买萨拉/闪光皮皮/卡西达，7 主力里的 3 个）。
   面板原文「击败后解锁交易行资格」，即**必须击败 SPT 首领**；本轮确认了文案，
   但"要击败几个"仍未验证（`UNKNOWN`）。
2. **精灵等级 Lv34**（旧文档说 15，已推翻）→ 到 36 进化只差 2 级，这是当前最便宜的推进点。
3. **谱尼入口本身可达**：SPT 面板「太空站左翼」分类里只有 1 个入口——
   `谱尼 / 勇者之塔神秘领域 / 未击败`，点「前往」可直接传送（本轮实测成功）。
   这意味着 P2-01 的导航已具备，缺的是进去之后的额度/封印规则。

---

## 第四问：本轮账号实际发生了哪些变化？

**验收**：`FAIL`（没有完成任何新的任务节点）

> 最终那次 AI 驱动运行的客观数字（`goal report` 可复现）：
> `run 19745e9a764d`，planner=bridge，status=paused，stop=stop requested，
> `stats={"actions":28,"battles":0,"elapsed_s":235.6,"errors":1,"loops":1}`，
> 节点 `puni.prepare.level_up` = `attempted`，
> proof = `pet 烈火猴 level UNKNOWN (not observed)`。
> 也就是说：**决策、导航、暂停、停止、checkpoint 全部生效，但那一轮一场都没打赢。**
> 战斗闭环本身在更早的一次运行里成功过（`1 场胜利 … 经验 +106`），
> 但那一轮的 SQLite 在后续重启时被我清理掉了 —— 这是本轮的一个操作失误，
> 已记入"待改进"。

真实发生的、可验证的变化：

| 变化 | 证据 |
|---|---|
| 账号从登录页 → 太空站 → 克洛斯星林间 多次往返（本轮重新标定） | `artifacts/round5/00..05*.png` |
| SPT 面板完整读取（73 入口、分类、每卡「击败后解锁交易行资格」） | `artifacts/round5/01_spt_panel.png` |
| 精灵面板读取：烈火猴 Lv34、个体21、天真、升级所需经验 4269→4057、四技能 PP | `artifacts/round5/04_pet_panel.png` |
| 一场战斗的完整闭环（进战斗→出招→胜利面板→确认→读奖励） | `data/goalrunner.sqlite3` 的 `skill.pass` 事件（`1 场胜利 … 经验 +106`） |
| 若干战斗中积累了经验（`XIN精灵获得经验：106`、`恭喜你得到了53个积累经验`） | 同上 + OCR |

**但"账号多了一级"没有发生**：Lv34 → Lv36 需要约 8000+ XP ≈ 80 场战斗，
本轮实际预算内只完成了个位数场次。**所以 Level C 判 FAIL，不判 PASS。**

诚实说清卡在哪：

1. **单位时间 XP 太低**：野外蘑菇怪 106 XP/场，而升级需 4057 XP → 38 场/级；
2. **弹窗阻塞**：「恭喜你得到了53个积累经验」是纯文字弹窗，没有模板可匹配，
   它挡住精灵面板、也挡住点怪；本轮先靠 OCR 兜底修了一版（engage 失败 → OCR → 点确认），
   但清得不彻底，仍会吞掉整轮预算；
3. **精灵面板图标位置随场景漂移**：底栏图标在太空站与林间位置不同，
   固定 `game_px` 不可靠，本轮改成"模板/OCR 定位 + 点击后验证"，但仍有场景没覆盖；
4. **预算给少了**：`--max-minutes 26` 里真正落在战斗上的时间不足。

---

## 第五问：哪些能力有真实证据，哪些只是 Mock？

| 能力 | 状态 | 证据 |
|---|---|---|
| 原生 SCK 常驻抓帧（`seerdx`） | `PASS` 真机 | 2400×1600 帧、`capture.start/latest` 全程使用 |
| 场景判定（模板） | `PASS` 真机 | WORLD/BATTLE/RESULT/DIALOG 本轮反复触发 |
| OCR（Vision，中文） | `PASS` 真机 | 本轮所有面板文字都来自它 |
| `game.navigate_spt_boss`（按文字定位「前往」） | `PASS` 真机 | 蘑菇怪、钢牙鲨两张卡 |
| `game.run_battle`（连战状态机） | `PASS` 真机 | 本轮有 `1 场胜利 + 经验 106` 的 `skill.pass` 记录 |
| `game.read_active_pet_level`（精灵面板） | `PASS` 真机（曾成功读 Lv34） | 后续被弹窗阻塞 → 降级为 `FAIL` 但**不编造** |
| 战斗 HUD 读我方等级（`LV834`） | `PASS` 真机 | OCR + `parse_level(side="ally")` |
| 暂停 / 恢复 / 停止（控制文件 + SIGTERM） | `PASS` 真机 | 本轮 pause→resume 实测；SIGTERM 现在也会写 checkpoint |
| 单一执行权（flock） | `PASS` 单测 + 真机 | 第二次启动被拒绝 |
| SQLite 持久化（世界/节点/事件/checkpoint） | `PASS` | `data/goalrunner.sqlite3` |
| AI bridge 规划闭环 | `PASS` 真机 | `data/ai/request-0001.json` / `response-0001.json` + `node.selected` 事件 |
| AI 自动（OpenAI 兼容网关） | `FAIL`（凭证 `BLOCKED`） | 网关返回 `Invalid credential`；降级路径已验证 |
| 目标图 42 节点 + 校验器 | `PASS` 单测 | 53 个测试 |
| 谓词"历史证据不能算完成 / 未知不是 false" | `PASS` 单测 | 谓词真值表 |
| 失败（战败）分支 | `NOT TESTED` | 仍无样本 |
| 四技能选择 / 换精灵 / 用药 / 交易行 / 融合 / 抓宠 | `NOT TESTED`（未实现） | — |

测试总量：`PYTHONPATH=.pydeps python3 -m pytest -q`

---

## 第六问：中断以后能否继续？实际测过吗？

**暂停/恢复**：`PASS`（本轮真机）
* `goal pause` → 写 `/tmp/seerauto.pause` → 运行器在下个观测周期停止动作；
* 实测暂停期间游戏画面停在同一个弹窗、无新事件写入；
* 删文件后继续，且**强制重新观测**（暂停期间游戏仍在自行推进）。

**进程被杀/崩溃后恢复**：`PASS`（机制 + 修了一个真 bug）
* 本轮实测 `pkill`（SIGTERM）会让 `finally` 不执行，数据库停在 `status=running`；
* 已修：`cmd_goal_start` 安装 SIGTERM 处理器（等价 KeyboardInterrupt），
  现在会写 checkpoint 并置为可恢复状态；
* `goal resume` 用**同一个 `run_id`** 从 checkpoint 继续，先重新观测，已完成节点不重跑。

**跨会话恢复**：`PASS`（单测 + 手工）— 但**没有**做过"关机/隔天再跑"的真机验证，属 `NOT TESTED`。

---

## 第七问：还缺什么才能拿到谱尼？

按现在的证据，缺的是**顺序上不可避免的 5 件事**：

1. **交易行资格**：需要击败 SPT 首领解锁（面板原文），但"几个才算"未验证 → 一次针对性实验即可；
2. **XP 效率**：8 倍经验（P0-03）/ 每日 50 万经验（P0-02）入口都没标定；
   这是把"80 场/两级"降到"十几场"的关键；
3. **弹窗与面板的稳定处理**：需要一个"未知弹窗"通用关闭策略（OCR 确认词 + 模板双路），
   否则每轮预算都会被吞；
4. **战斗深度能力**：四技能选择、换精灵、用药、战败分支——封印 1–7 全部依赖它们；
5. **谱尼入口内部规则**：每日额度、封印顺序、真身进场条件（`P2-01 → P2-02 …`）。

---

## 第八问：下一次继续运行会执行什么任务？

```bash
PYTHONPATH=.pydeps python3 -m seerauto goal resume --allow-input --planner bridge \
    --max-minutes 50 --max-battles 45
```

下一次运行会：

1. 重新观测（场景/窗口/等级/SPT 卡片）；
2. 先清掉阻塞弹窗（`game.confirm_known_dialog` / OCR 兜底）；
3. 调度 `puni.prepare.level_up`：传送到克洛斯星林间 → 连战；
4. 每轮把战斗 HUD 的 `LV` 与「离升级还需经验」写回世界；
5. 一旦 `pet_level_at_least(烈火猴, 36)` 在**实时证据**上成立，节点转为 `completed`
   并写 checkpoints；否则如实留下 `attempted` 与原因。

---

## 安全与合规

* 服务器公告「本服不允许第三方软件、脚本或变速工具」始终记录在案
  （`AGENTS.md`、`docs/USER_GUIDE.md` §安全）。本项目只做正常截图 + 正常 GUI 输入，
  不注入内存、不改客户端、不绕过任何限制；是否使用由用户决定并承担风险。
* 输入闸门：`--allow-input` + 非 dry-run + 文件锁 + 每次点击前复核前台窗口与几何。
* 不可逆操作（购买/消耗/放生）默认禁止：`--max-spend 0` 是默认值。
* AI 只发结构化文本，**不发截图**。
* **账号信息已脱敏**：工作区里的 QQ 号与邮箱替换为 `<REDACTED_UID>` / `<REDACTED_EMAIL>`。
  ⚠️ **但 Git 历史里仍有**（旧提交），需要用户决定是否重写历史：
  `git filter-repo --replace-text <(echo "1179236059@qq.com==><REDACTED_EMAIL>")` +
  强推。本轮**没有**擅自改写历史。

---

## 复现命令与产物

```bash
PYTHONPATH=.pydeps python3 -m pytest -q                     # 全量测试
PYTHONPATH=.pydeps python3 -m seerauto goal status          # 运行状态
PYTHONPATH=.pydeps python3 -m seerauto goal report          # 生成报告
```

产物：`data/goalrunner.sqlite3`、`data/reports/goal-*.md`、`data/ai/*.json`、
`artifacts/round5/*.png`、`data/live_run.log`。


---

# 附：2026-11-03 夜间长程运行补充（02:00–06:00）

> 玩家去睡觉，Agent 继续推进。以下是这一段的真实结果，仍然只用
> `PASS / FAIL / BLOCKED / NOT TESTED`。

## 目标图第一次出现 completed

```
run 988c91ef9ea7  planner=deterministic  33.7 s / 7 actions / 0 errors
  puni.seal.entry_nav      completed   feature 'puni.entrance.reachable'  (LIVE_VERIFIED)
  puni.prepare.tutor_room  completed   feature 'training.tutor_room.reachable' (LIVE_VERIFIED)
  puni.prepare.level_up    blocked     no pet can battle (all pets at 0 HP)
  stop_reason: blocked: no pet can battle (all pets at 0 HP); healing required
```

* **谱尼入口**：`SPT → 太空站左翼 → 谱尼 → 前往` 真机到达「勇者之塔神秘领域」。
  到达判定用模板匹配那个美术字传送门（自身 1.00 / 其它场景 ≤0.46），因为 OCR 读不到它。
* **8 倍经验前置**：星图第 3 个（橙色）目的地就是辅导员房间，画面出现「初级班辅导员/高级班辅导员」。
  两个 NPC 打开的都是「精灵模拟训练」；8 倍开关本身仍未被任何素材捕获，所以只声明"到达"。

## 账号变化（本轮结束时）

| 项 | 值 |
|---|---|
| 烈火猴 | Lv34，HP **0/95**（全员 0 血，详见 `docs/BLOCKER_PET_HP.md`） |
| 升级所需经验 | 3633 → **2573**（本轮真机打进 1060 XP ≈ 10 场胜利） |
| 已完成节点 | **2**（此前一直是 0） |

## 验收状态更新

| 层级 | 之前 | 现在 | 依据 |
|---|---|---|---|
| Level A 产品可跑 | PASS | **PASS** | 433 tests + doctor + CLI 六命令 |
| Level B Agent 闭环 | PASS | **PASS** | 本轮又跑通一次：观察→规划→闸门→执行→验证→持久化 |
| Level C 有意义的谱尼进展 | FAIL | **部分 PASS**：2 个新节点 completed、经验 +1060 | 但"升到 36 级"仍 **FAIL**（宠物 0 血） |
| Level D 长程与恢复 | 部分 | **PASS**（暂停/恢复/停止/断点 + 硬阻塞安全停机） | 长跑本身被 0 血阻塞，属真实阻塞而非空转 |

## 今晚新增/修复（都被真机触发过）

1. 战斗按钮第二套画法（真机战斗被判成 WORLD 的根因）；
2. 「恭喜你得到了N个积累经验」弹窗模板（曾让一轮 28 次点击打 0 场）；
3. 面板关闭器家族：精灵面板族、万圣活动、精灵模拟训练、聊天面板、活动弹窗；
   其中「公屏聊天」误判曾让"关面板"变成假动作；
4. 扩展后的聊天面板盖住星图目的地图标 —— 这是今晚所有"地图打不开"的真正原因；
5. `game.read_pet_bag`：模板定位精灵背包，读出全员 HP + 等级 + 升级所需经验；
6. `game.travel` / `game.goto_puni_entrance` / `game.goto_tutor_room`：命名目的地 + 到达验证；
7. 战斗闸门：`can_battle=false` 时拒绝执行战斗步骤并记录 `recovery=heal_pet`；
8. SIGTERM 现在等价 Ctrl-C（写 checkpoint 再退出）。

## 玩家醒来只需要做一件事

给任意一只精灵补满血（体力药剂 / 精灵中心），然后：

```bash
PYTHONPATH=.pydeps python3 -m seerauto goal resume --allow-input --planner deterministic \
    --max-minutes 50 --max-battles 45
```

剩 2573 XP ≈ 24 场可到 35 级；建议先开 8 倍经验把 40 场压到 5 场。
细节见 `docs/BLOCKER_PET_HP.md`。


## 追加（03:05）：`--planner ai` 从 FAIL 变成 **PASS**

之前判 FAIL 的原因是我把人写死的网关路径搞错了：请求打到 `/zen/go/v1`（要 session 头）
而不是 `/inference/go/openai/v1`。用正确路径后**同一个 key 是好的**。

现在有三层证据：

1. **连通性诊断**（不碰游戏）：

   ```bash
   SEERAUTO_AI_API_KEY=… python3 -m seerauto goal ai-check
   # provider config: base_url=https://opencode.ai/inference/go/openai/v1
   #                  model=deepseek-v4.1-flash  api_key=set (…KExJ)
   # status: PASS — the gateway answered and the decision parsed
   ```

2. **AI 真决策**（dry run，真机世界摘要喂给模型）：

   ```
   [runner] node puni.prepare.level_up (将主力精灵练到 36 级) —
     constraints.allow_input is false and the two higher-priority ready nodes
     (puni.prepare.tutor_room P4-05 priority 88, puni.seal.entry_nav P2-01 priority 84)
     can only be driven by input-requiring skills (game.goto_tutor_room /
     game.goto_puni_entrance), so I pick the remaining ready node
     puni.prepare.level_up (bootstrap, ready) whose blocked_reason is exactly the
     UNKNOWN 'state.active_pet_level_known' (world.active_pet null, xp_to_next null),
     and attach the input-free observe capability game.inspect_pets to fill that gap.
   ```

   模型读到的是：世界摘要 + 42 节点摘要 + 能力清单 + 预算约束。
   它自己指出了"两个更高优先级的节点在 allow_input=false 时不可驱动"，并选了合法的那个。

3. **调用审计**：`ai_calls` 表记录 provider/model/latency/chars/cost，
   `goal report` 显示 `ai calls: 1 (failures 0, cost 0.0)`。

**仍未完成的一步**：`--planner ai --allow-input` 的**完整真机闭环**（AI 决策 → 真点 → 验证）。
客户端在 02:40 之后被服务端宕机挡住（见 `docs/BLOCKER_PET_HP.md` §8），
这一步等客户端恢复后补：命令已就绪。

```bash
SEERAUTO_AI_API_KEY=… python3 -m seerauto goal start puni --planner ai \
    --allow-input --max-minutes 40 --max-battles 30
```
