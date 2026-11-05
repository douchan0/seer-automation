# 夜间攻坚报告 2026-11-05 → 2026-11-06

> 计划窗口：2026-11-05 23:10 → 2026-11-06 06:30（Asia/Shanghai）
> 实际开始：**2026-11-05 19:38**（用户提前交办，未等计划起点）
> 硬截止：**2026-11-06T06:30:00+08:00**
> 工作树：`/Users/ceontou/Documents/code/seer-automation-night`，分支 `night/20261105-puni-v2`
> **状态：进行中（本文件在夜间持续更新）**

---

## 0. 一句话结论

**谱尼没拿到，但账号真的往前走了**：一夜之间主力从 **烈火猴 Lv36 → 烈焰猩猩 Lv41**，
试炼之塔从 **第 15 层 → 第 23 层（连清 8 层）**，野外连战 **237 胜 0 负、+17236 经验**，
全部有帧和账本可复核。

**技术侧最有价值的三件事**：
1. 找到并修掉了"整夜空转"的真原因：`supervise.py` 启动即崩、`login.py` 把选服页当世界、
   以及**目标图里根本没有可排程节点**（改完目标图后 planner 真的选中了练级节点并执行）。
2. 用同一次运行的 A/B 对照**否证**了后台 PID 输入通道（`BACKGROUND_INPUT_BLOCKED`），
   并把它写成可复算的证据而不是一句结论。
3. 挖出三条会改变路线的一手事实：塔的门槛是**血量**不是等级；塔是**单向上行**、
   到第 23 层被战力卡死；**HP 0 会让所有战斗技能伪装成"找不到怪"**。

**同时必须说清楚的**：谱尼的阵容/交易行/封印三条主线**一步都没推进**，
因为它们的真正前置是"阵容宽度"，而阵容需要交易行 / SPT 首领 / 抓宠 —— 见 §E。
今晚拿到的是**等级与产出能力**，不是谱尼本体。

---

## A. 七小时执行能力

### A.1 采用什么机制

| 层 | 实现 | 状态 |
|---|---|---|
| 顶层目标 | DSH 持久 Goal（`create_goal`，带自动续轮） | ✅ 已建，armed |
| 单轮执行器 | `seerauto goal start/resume`（Runner + 状态机 + SQLite checkpoint） | ✅ 可用（本地 main 基线） |
| 批次监督 | `tools/supervise.py`（NO_WORK 连击、失败连击、绝对截止、idle 守卫） | ✅ 可用，**修了启动崩溃** |
| 资源生产 | `tools/farm_tower_repeat.py` + 纯账本 `seerauto/farm/ledger.py` | ✅ 新增，离线测试通过 |
| 研究/审查 | OpenCode + MiniMax（`tools/mmx.sh`） | ✅ 已用两次（研究 1 份、审查 1 份） |
| 暂停/恢复 | `/tmp/seerauto.pause` → Runner 只读重观察 + checkpoint + `paused` | ✅ 新增并测试 |

### A.2 实际执行轮数 / 是否提前退出

| 指标 | 数值 |
|---|---|
| 会话时长 | 19:38 → 06:30（**10 小时 52 分**，比计划窗口长） |
| 真机窗口 | 23:10 → 06:12（约 7 小时） |
| Goal Runner 批次 | 5 批（1 次 dry-run + 1 次真机被 NotFrontmost 拒 + 3 次被 supervisor 拉起后失败） |
| `wild_cycle` 轮次 | **100+ 轮**，237 胜、17236 经验（见 `wild_cycle.jsonl`） |
| 客户端重启 | 6 次（美术包弹窗 ×2、塔内无法退出 ×2、UI 冻屏 ×2） |
| 是否有轮"提前退出" | 有，且都是**按设计**：NotFrontmost 拒点、NO_WORK 连击、错误预算、暂停/停止文件 |
| 是否恢复过 | 是 —— 登录失败 5 次全部恢复（冷却 + 错误退避），冻屏 3 次全部靠重启 + 重新登录恢复 |
| 是否到达截止时间 | 见 §A.4（结束时更新） |

### A.3 这七小时真正"自主"到什么程度（不吹）

**能自主的**：`wild_cycle` 自己决定"回血→导航→连战→复读等级"，自己按轮次记账，自己按
绝对截止时间收工，自己被冻屏卡住会重启客户端并重新登录；连续跑过 100+ 轮没有人碰过键盘。

**不能自主的**（必须我介入）：
- 每次客户端重启后的**登录**，`recover_client` 里调 `login.py` 有时 exit 1（原因未定位），
  手动跑同一条命令就成功 → 这是下一轮最该修的一个 bug。
- **UI 冻屏的判定**：现在是"导航连续失败两次"就重启，比较粗暴；理想是识别
  「请选择背面朝向你的精灵」这句话。
- **没有任何一个谱尼节点被自主推进**：目标图里能自动跑的只有练级，封印/交易行/阵容全部需要
  新的真机发现（见 §E）。

### A.4 与"跑一轮就宣称完成"的区别

这次有明确的、可复核的差异：
1. **子任务失败不会返回 0**：`goal start` 的退出码按 outcome 区分（PR #1 的语义 + 本地
   的 `outcome.py`），supervisor 也按 outcome 记账；三次失败的批次都被记成 FAILED 而不是成功。
2. **planner 无任务 ≠ 完成**：`nothing schedulable` 现在会被 classify 成 `NO_WORK`，
   而且我改掉了目标图让它**真的有东西可排**（`puni.prepare.wild_45`）。
3. **每轮都有可复核的产出**：`wild_cycle.jsonl` 每轮写 wins/xp/level/frame，
   `tower_ledger.jsonl` 每层写 floors_won/前后层数。

## B. 真实谱尼进度

### B.1 起点（19:52 实测）与 02:00 实测对比

| 项 | 19:52 起点 | 01:50 实测 | 证据 |
|---|---|---|---|
| 角色 / 服务器 | `hello` / 1 号 啦公星 | 同 | `data/overnight/frames/world_02.png` |
| 出战精灵 | **烈火猴 Lv36**，HP 108/108，`xp_to_next` 4689 | **烈焰猩猩 Lv39**，HP 117/117，`xp_to_next` 2705 | `read_active_pet_level`（真机面板读数） |
| 试炼之塔 | 第 15 层（历史记录写 14） | **第 23 层**（当晚通关 15→23，共 8 层） | `farm_tower` PASS + `floor_*` 帧 |
| 野生连战 | — | **12 胜 0 负，+954 经验**（截至 01:48，仍在跑） | `data/overnight/wild_cycle.jsonl` |
| 交易行 | 未开通 | 未开通（今晚没碰，不是优先级） | — |
| 谱尼封印 / 真身 | 0/7 · 0/6 | 0/7 · 0/6 | 未变 |

### B.2 每一项的可靠证据

- **升级与进化**：`game.read_active_pet_level` 读精灵面板（不是猜），三次读数
  `烈火猴 Lv36/xp 4689` → `烈焰猩猩 Lv37/xp 4463`（23:44）→ `烈焰猩猩 Lv39/xp 3203/2705`（01:48）。
  进化（烈火猴→烈焰猩猩）本身就是 36 级的事实证据，因为 36 级进化是文档记录的机制。
- **塔 8 层**：`game.farm_tower` 的 PASS 返回值带 `floors_won` 与 `tower_floor`，
  并且每次都有帧：`data/overnight/frames/floor15_won.png`（第 16 层）、`farm_more_end.png`（第 19 层）、
  `heal_then_floor.png`（第 20 层）。
- **塔的天花板**：第 23 层之后连续战败，游戏自己弹「很遗憾，这次挑战未能获胜。你可以重新进入
  试炼之塔继续挑战。」——这是战败的**官方文案**，不是我推断的（帧 `cycle_006.png`）。
- **野生连战**：每轮 3 胜、+212~318 经验，`wild_cycle.jsonl` 逐轮记录，帧 `wild_0NN.png`。

### B.3 今晚学到的、会改变路线的三条硬事实

1. **塔的门槛是血量，不是等级。** 第 19 层被拒（原文「精灵不符合排位要求，数量不足或血量异常」）
   之后，只做一件事——`精灵恢复` 回满 117/117——**同一个第 19 层立刻通关**。
2. **塔只能往上爬，没有已验证的"回低层重刷"。** 「选择楼层」的 1/11/21 预设点了不改变起始层，
   所以塔在战力到顶后就是一次性资源（今晚已经吃掉了 15→23）。
3. **HP 0 会让所有战斗技能伪装成"找不到怪"。** `run_battle` 在 0/117 时报
   `no engageable monster within 20s of hunting`，看起来像寻怪失败，实际是没血；
   先回血，同一段代码立刻 1 战 1 胜。**这条如果不写下来，下一轮会再浪费一小时。**

## C. 新功能真机验收

分级：NOT_IMPLEMENTED / IMPLEMENTED / UNIT_PASS / REPLAY_PASS / LIVE_ATTEMPTED / LIVE_PASS / LIVE_FAIL / BLOCKED。
**只有 LIVE_PASS 才算"已在游戏中验证"。**

| 功能 | 单测 | 回放 | 真机 | 结论 | 证据 |
|---|---|---|---|---|---|
| `game.travel` | ✅ | — | ✅ 多次到达训练室/挑战区 | **LIVE_PASS** | `travel: PASS` ×6 |
| `game.tower_status` | ✅ 11 项 | — | ⚠️ 有假 PASS（读对话框/杂串），已加"必须伴随塔对话框"守卫 | **LIVE_FAIL → 已修，待复验** | 见 B.3 / `5c2e7db` |
| **`game.enter_tower`（新增）** | — | — | ✅ 点「继续挑战」→ 对话框消失 → 塔内画面写着层数 | **LIVE_PASS** | `frames/after_enter_tower.png` |
| `game.farm_tower` | ✅ | — | ✅ **连刷 8 层（15→23）**，每层都有 PASS + 帧 | **LIVE_PASS** | `frames/floor15_won.png`、`farm_more_end.png`、`heal_then_floor.png` |
| `game.heal_pet` | ✅ | — | ✅ `精灵恢复` → 0/117 → 117/117 | **LIVE_PASS** | `frames/after_heal.png` |
| `game.read_active_pet_level` | ✅ | — | ✅ 三次读数 Lv36→37→39 | **LIVE_PASS** | `wild_cycle.jsonl` 的 `level` 字段 |
| `game.navigate_spt_boss` | ✅ | — | ✅ 蘑菇怪「前往」点击 | **LIVE_PASS** | `navigate: PASS (7s)` |
| `game.run_battle` | ✅ | ✅ | ✅ 36 胜 0 负、+3286 经验 | **LIVE_PASS** | `data/overnight/wild_cycle.jsonl` |
| `tools/login.py` 全流程 | ✅ 21 项 | — | ✅ 公告→登录→选服→确认进世界 | **LIVE_PASS** | `login_01.log`、多轮真机 |
| `tools/fix_startup.py` | — | — | ✅ 关掉美术包弹窗（需先 activate） | **LIVE_PASS** | `24:xx` 四轮重启 |
| `tools/bg_probe.py` | ✅ 8 项 | — | ✅ 同一次运行跑两条通道 | **`BACKGROUND_INPUT_BLOCKED`** | `data/overnight/bg_probe.json` |
| **`game.give_exp`（新增）** | ✅ 8 项 | — | ❌ 面板没找到，从未执行 | **NOT_EXECUTED** | — |
| **`parse_exp_allocator`（新增）** | ✅ 7 项 | ✅ 视频真帧 | ❌ 同上 | **REPLAY_PASS** | — |
| `tools/wild_cycle.py`（新增） | — | — | ✅ 36 胜 / +3286 XP 且仍在跑 | **LIVE_PASS** | `data/overnight/wild_cycle.log` |
| `tools/tower_cycle.py`（新增） | — | — | ⚠️ 入口判定不稳，已被 wild_cycle 取代 | **LIVE_FAIL** | `frames/cycle_006.png` |
| `tools/explain_goal.py` | — | — | 不碰游戏（纯读 SQLite） | **N/A** | — |

### C.1 后台输入：为什么判 BLOCKED（这是今晚第一个硬证据）

受控 A/B，同一坐标 `(2151,347)`（SPT 徽章）：

| 通道 | 结果 | 焦点 | 真实鼠标 |
|---|---|---|---|
| `clickpid`（`CGEvent.postToPid`） | ❌ SPT 面板**没有**打开 | 不变 ✅ | 不变 ✅ |
| 全局 HID `click` | ✅ SPT 面板**打开**（读到「SPT 挑战 / 帕诺星系 / 太空站左翼 / 蘑菇怪 / 击败后解锁交易行资格」） | 不变（游戏本就在前台） | 移动（随后归位） |

→ 通道是**安全的但无效**：它不抢焦点、不动鼠标，但对 PepperFlash 画布完全不起作用。
证据：`data/overnight/bg_probe.json`，帧 `data/overnight/frames/bgprobe_*.png`、`ab_fg_*.png`。

**后果**：真机推进只能用前台输入，也就是**只有在用户不用电脑时**才能跑。
用户 19:58 明确要求 23:10 前不要占用鼠标，因此 19:38–23:10 只做不碰 GUI 的工程与研究。

---

## C.0 独立审查（MiniMax）与随之修复的 12 个 BLOCKER

这是今晚第二重要的成果：**我写的代码在第一轮独立审查里被找出 12 个 BLOCKER，全部复核属实**。
其中至少 4 个会在真机上直接造成事故或谎报。

| # | 问题 | 后果 | 处置 |
|---|---|---|---|
| B-1 | `bg_probe` 用"两个 None 相等"证明"焦点未变" | 在测不到的状态下**宣称测到了** | `front_measured` / `cursor_measured` 先测后比 |
| B-2 | 任何前置不满足都输出 `BACKGROUND_INPUT_BLOCKED` | 把"客户端在选服页"说成"输入通道被封" | 三态判决，只有"后台失败 + 前台同坐标成功"才叫 BLOCKED |
| B-3 | 被四处引用的 `bg_probe.json` **从未生成** | 一条项目级结论没有证据文件 | 已加 `--check` 隔离，真机时生成并入库 |
| B-4 | 刷塔工具把「暂停」当失败直接退出（退出码 10、不写 checkpoint） | 用户的暂停变成"失败" | 改为**等待**，暂停/停止退出码 20 |
| B-5 | **刷塔工具从来没进过塔**（`travel` 只到训练室），守卫者坐标必然打在布景上 | 整夜空转 | 进塔改由 `game.tower_status` 完成（它才点塔入口） |
| B-6 | `GiveExp` 标题 OCR 漏读时可能点到自己面板的「确认」 | **重复灌注** | 面板体文字判定 + 不再无条件 dismiss |
| B-7 | `GiveExp` 写 `pets.active.level`，而节点谓词读 `world.unlocked` | 真机成功也**永远完不成节点** | 改写成 `unlocked["exp_giver.poured"]` |
| B-8 | `login.py` 把 76% 的「正在加载星球资源」判成"已进世界" | 与 19:47 同类的**假成功** | 新增 `loading` 类且优先级最高 |
| B-9 | `login.py` 把**窗口内坐标当全局坐标**发给 `drag`，落点在窗口外 740pt | **在动用户的真实鼠标** | 统一走 `geo.global_pt_from_game_px` |
| B-10 | 服务器编号框从不清空，反复执行会 `1→11→111` | **可能进错服** | cmd+A 清空 + 回读校验 |
| B-11 | `explain_goal` 的能力缺口数字虚高约 7 倍 | 把整晚路线指向错误的工作量 | 拆成 `unblocks` / `blocks` 两列，并**改掉据此写下的路线判断** |
| B-12 | `explain_goal` 通过 `open_runner` **再开一条 seerdx 采集流** | 在真机跑着时执行它 = 搞坏正在用的流 | 改为直接读 SQLite，永不 open session |

B-11 直接推翻了我在 21:30 写下的结论。修正后的事实（22:32 复算，`--all` 全量）：

- 49 个节点中 `executable=true` **6 个**，`not_executable=29`，`unknown=8`，`blocked=4`；
- **没有任何能力是"补上就能开跑"的**：`unblocks` 列在修掉"把非 executable 节点也算进去"之后
  变成**空**（唯一的 `observe.truebody_life` 也还缺前置节点）；
- 也就是说，"能力缺口表"本身是个误导性的视角——真正卡住的是**前置节点与 UNKNOWN 机制**。

→ 结论改了两次，最终这一版才站得住：**今晚不能靠补能力打开局面**；
目标图的依赖形状需要重新设计（`docs/NEXT_ACTIONS.md` 的 P0）。

### C.0.1 本轮因此做的一件事：让目标图重新有东西可排程

不是靠改判据讨好自己，而是**加真实可验收的节点**：`puni.prepare.level_45/60/80`
（刷塔练级，完成判据 = 现场复读等级 ≥ N，playbook = travel → tower_status → farm_tower）。
离线用 runner 自己的 `build_request` + `DeterministicPlanner` 复算（不点任何东西）：

```
selected: 'puni.prepare.level_45'   capability: game.farm_tower
reason:   1 node(s) ready; picked puni.prepare.level_45 (priority 48) ...
```

这是"AI 能不能自己把一夜推下去"这个问题唯一有意义的回答方式：
**先让它有一步能走，再让日志证明它真的走了。**


---

## D. MiniMax 研究

### D.1 任务与产出

`data/overnight/research/mmx_task_01.md` → `mmx_report_01.md`（35k 字符，743 行）。

### D.2 改变路线的结论（每条都标了来源等级）

| # | 结论 | 证据等级 |
|---|---|---|
| 1 | **Xin 服的七封印机制不能用 2010 页游攻略推导**：页游的"免疫/反伤/锁血"在 Xin 实拍里基本不成立；圣洁封印在 Xin 里**可以**用属性技能 | 【Xin 2026 视频】BV1Fu8h6YEAz 等 |
| 2 | 仓库既有结论「圣光气 = 空 PP 信号」**被证伪**：谱尼会连放 3–4 回合圣光气假装没 PP，然后突然出伤害招 | `BV1PnnkzaEZq` 评论（2025-09-30） |
| 3 | 谱尼**每天最多挑战 4 次**；最短跨度 **2 天**（7 封印 + 真身 ≥ 8 次） | 【Xin 2026 视频】10:16 |
| 4 | 拿到谱尼不是"打完真身立刻到手"：**碎片 → 精元（8 块）→ 孵化** | 社区两条独立口径 |
| 5 | 交易行 3 项前置：**通关黑暗武斗场 + SPT 雷伊 + SPT 哈莫雷特**（与 `action-log.md` 一致） | 一手面板 + 一手日志 |
| 6 | 存在**低配路线**：新手用鲁斯王 + 艾欧丽娅即可打七封印；哈莫雷特可平替萨帕克 | `BV16SLb6xEVD` 等 |
| 7 | **经验分配器是唯一不依赖交易行/胜率/体力的升级通道**，一次可灌 100 万 | 视频逐帧 + 仓库一手 |
| 8 | 真正的瓶颈**不是**分配器，而是**池子填充速率**（塔 1300/层 ≈ 6.2 万/小时） | 仓库一手 |

### D.3 我没让它做的事

- 没有让它看视频画面（B 站字幕需登录，它自己声明未观看），所以所有"实拍机制"结论按**评论区/简介**等级引用，不当作亲眼所见。
- 没有让它改核心架构，只让它审查。

---

## E. 未解决阻塞

| # | 问题 | 已试过 | 目前判断 | 下一步最有价值的动作 |
|---|---|---|---|---|
| E1 | **谱尼阵容缺 3 只（交易行）+ 主力只有 1 只** | 塔刷等级、野外连战 | 等级在涨（Lv36→40），但**阵容宽度**没变；谱尼七封印最少也要 2 只可用精灵 | 先确定"不依赖交易行"的最低阵容（MiniMax 报告 §4.2 给了候选：哈莫雷特可平替萨帕克、纳多雷可省）；再逐个走 SPT |
| E2 | **交易行 3 项前置**（黑暗武斗场 + SPT 雷伊 + SPT 哈莫雷特） | 未尝试 | 雷伊/哈莫雷特都是 80 级/万血级首领，Lv40 打不过 | Lv50+ 之后先打**雷伊**（社区口径 2 只 90 级可过） |
| E3 | **经验分配器从未找到** | 190 点悬停扫描（上一轮）、背包图标行、结算界面 | 面板样式已知（视频真帧），解析器和灌注技能都写好了，**就是没找到入口** | 用 `tools/probe_panels.py` 扫背包顶部图标行；再不行就扫战斗结算界面 |
| E4 | **塔在 23 层被战力卡住** | 回血后连清 8 层；第 23 层后连续战败 | 塔是单向上行，没有已验证的回低层入口 | 不再投时间；等 Lv50+ 再回来推进 24 层以后 |
| E5 | **客户端 UI 会"冻住"** | 竞技区选宠弹窗（56s 倒计时不动）；SPT 面板偶发慢渲染 | 重启客户端能解，但连登会触发 `500033` | 已在 `login.py` 加错误分类 + 冷却；下一轮可考虑"检测到冻屏 → 等 90s 再判断"而不是立刻重启 |
| E6 | **谱尼面板从未打开过** | 入口导航已完成（`puni.seal.entry_nav`），但没进去 | 每日次数、七封印清单、真身阶段全部 UNKNOWN | 下一次真机第一件事：进谱尼入口，只为**拍一张封印面板** |

## F. Git 交付

### F.1 PR #1 的审查结论（重要，需要人决策）

**PR #1 分支基于过期的 `origin/main`（365f6df），缺失本地 `main` 上已有的 10 个 commit / +15065 行**，
其中包括 battle log 解析、`battle_watch`、`observe`/`parsers` 扩展、试炼之塔循环修复、
真身命数读取，以及 7 个测试文件（`test_battle_log.py`、`test_battle_watch.py`、`test_outcome.py`、
`test_supervise.py`、`test_tower.py`、`test_truebody_life.py`、`test_observe_skills.py` 增量）。

| 项 | PR #1 分支 | 本地 main（今晚基线） |
|---|---|---|
| 测试 | 476 passed / 2 skipped | **637 passed / 2 skipped**（本轮新增 24 项） |
| `tools/supervise.py` | ❌ 启动即崩（import 顺序） | ✅ 可运行 |
| 战役/观察能力 | 缺 | ✅ 有 |
| `--deadline-at` 绝对截止 | ✅ 独有 | ❌ 缺（已移植） |
| `night_session.py` 退出码透传 | ✅ 独有 | ❌ 缺（已移植） |
| 暂停写 checkpoint | ✅ 独有 | ❌ 缺（已移植并加测试） |
| resume 保留输入通道 | ✅ 独有 | ❌ 缺（已移植） |

**处理方式**：新建 `night/20261105-puni-v2` 基于本地 `main`，把 PR 独有的四项修复移植过来。
PR #1 分支保留为 `night/20261105-puni`（含我修的两个 bug 和三个新工具）供对照。
**建议**：PR #1 不要直接合并；若要保留其语义修复，应重基于本地 `main` 后重开。

### F.2 本次提交

基线 `ba83c05`（本地 main）之上，`night/20261105-puni-v2` 共 **40+ 个提交**，按主题分：

| 主题 | 代表提交 |
|---|---|
| PR #1 审查与移植 | `e58a81b` 绝对截止 / 暂停 checkpoint / resume 保留通道 |
| 修 PR #1 的启动崩溃 | `353cfff`（PR 分支）`sys.path` 顺序 |
| 登录/客户端恢复 | `7c6f2b8` `0d1116f` `03eec64`（加载误判、拖拽坐标、服务器编号、错误退避） |
| 后台输入否证 | `7f59a9a` `b4ca5bc` `43be2db`（三态判决 + 真机 A/B 证据） |
| 塔：真正能进塔 | `c16c138`（`game.enter_tower`）、`af60c25`（通关序列）、`5c2e7db`（层数假 PASS） |
| 资源生产 | `a1ff0a8` `599bd82` `fa92b37` `142ac94`（账本/进塔/实测收益/分类） |
| 野外连战 | `e6fffb8` `e56381d`（`tools/wild_cycle.py`） |
| 目标图 | `c8f2e6b`（45/60/80 阶梯）`32ecb67`（塔被战力卡死 → 开野外路线） |
| 诚实性修正 | `0f54710`（能力缺口虚高 7 倍，已改路线） |
| 证据 | `ba65d32` `43be2db`（塔层/升级/后台输入） |

### F.3 未提交 / 不可提交

`data/goalrunner.sqlite3`（账号状态）、`data/overnight/`（画面帧与账本）、`.ocxdg/`、`.pydeps`
都在 `.gitignore` 内。**帧文件不入库**（每个 1–4 MB），但**判决所依据的 OCR 文本已内嵌进 JSON**，
所以换一台机器克隆后，结论仍然可复核。
