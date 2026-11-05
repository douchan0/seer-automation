# 夜间攻坚报告 2026-11-05 → 2026-11-06

> 计划窗口：2026-11-05 23:10 → 2026-11-06 06:30（Asia/Shanghai）
> 实际开始：**2026-11-05 19:38**（用户提前交办，未等计划起点）
> 硬截止：**2026-11-06T06:30:00+08:00**
> 工作树：`/Users/ceontou/Documents/code/seer-automation-night`，分支 `night/20261105-puni-v2`
> **状态：进行中（本文件在夜间持续更新）**

---

## 0. 一句话结论（每阶段刷新）

截至 21:50：**代码与基础设施验收完成，真机阶梯尚未开始**。
发现并修复了两个会让整夜空转的硬故障（supervisor 无法启动、登录工具误报已进世界），
完成了后台输入通道的受控否证，并把"为什么 planner 一直拒绝"从一句日志变成了可复算的诊断。
真机推进按用户要求推迟到 23:10（用户当时正在使用电脑）。

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

（夜间结束时填）

### A.3 三个必须解决的可靠性问题及结论

| # | 问题 | 结论 |
|---|---|---|
| 1 | `tools/supervise.py` **在 `sys.path.insert(ROOT)` 之前 import `seerauto`** | 真故障。`python3 tools/supervise.py` 直接 `ModuleNotFoundError`，supervisor 一行都跑不了。已修（PR #1 分支） |
| 2 | `tools/login.py` 把「综合待命中」当"已进世界" | 真故障。选服界面会把世界 HUD 透在弹窗后面，工具因此**谎报成功并卡在选服页**（19:47 实测）。已改为 `PRIMARY_WORLD_MARKERS` + 连续两帧确认 + 排除选服标记 |
| 3 | 客户端停在「版本检测中…」 | 不是网络问题。是**独立小窗口**「未检测本地有美术包，是否需要导入？」挡住了主窗口。`seerdx win` 只返回最大窗口，所以只信它的代码会一直等。已记录进 `AGENTS.md §5.6`，并让 `login.py::clear_popup` 用前台点击（`clickpid` 打不中） |

---

## B. 真实谱尼进度

（夜间结束时填；每项必须有截图或 SQLite 证据）

### B.1 起点（22:00 实测）

| 项 | 值 | 证据 |
|---|---|---|
| 角色 / 服务器 | `hello` / 1 号 啦公星 | `data/overnight/frames/world_02.png`（公屏在线） |
| 出战精灵 | 烈火猴 Lv36，HP 108/108，`xp_to_next` 4689 | `goal status` 的 `world` 行（LIVE_VERIFIED） |
| 谱尼封印 / 真身 | 0/7 / 0/6 | `REAL_DELIVERY_REPORT.md` |
| 试炼之塔 | 已清 14 层（历史） | `docs/state.md` |
| 交易行 | 未开通 | `docs/action-log.md` 17:37 |
| SPT 已击败 | 仅蘑菇怪 | `artifacts/round5/01_spt_panel.png` |

---

## C. 新功能真机验收

分三级记录：**单测 / 回放 / 真机**。只有真机通过才写 `LIVE_PASS`。

| 功能 | 单测 | 回放 | 真机 | 结论 |
|---|---|---|---|---|
| `tools/bg_probe.py` 后台输入探针 | — | — | ✅ 跑过（含 A/B 对照） | **`BACKGROUND_INPUT_BLOCKED`** |
| `tools/login.py` 世界判据 / 按编号进服 | ✅ 11 项 | — | ✅ 按编号进服实测成功 | `LIVE_PASS` |
| 美术包弹窗关闭 | — | — | ✅ 手动关过一次（19:46） | 真机成功，但**尚无一键自动化**（`clickpid` 无效已改前台点击，待复验） |
| Runner 暂停 checkpoint | ✅ 3 项 | — | 待真机 | — |
| `--deadline-at` 绝对截止 | ✅ 4 项 | — | — | 单元级 |
| `tools/farm_tower_repeat.py` | ✅ 16 项（账本） | — | ⚠️ 只有 dry-run（4/4 `BLOCKED`，符合预期） | **未跑通**：首版从未进塔（只到训练室），已改为 `game.tower_status` 先进塔 |
| `parse_exp_allocator` 面板解析 | ✅ 5 项（真帧 fixture） | ✅ 用视频帧 | 待真机（面板未找到） | — |
| `game.give_exp` 经验灌注 | ✅ 6 项 | — | 待真机（面板未找到） | — |

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

B-11 直接推翻了我在 21:30 写下的结论。修正后的事实：

- 46 个节点中 `executable=true` 只有 **5 个**，`not_executable=29`；
- 补 `battle.skill_select` **只会立刻解开 1 个节点**（不是 13 个）；
- 全图"仅缺一个能力即可开跑"的节点共 **6 个**。

→ 所以今晚不能靠"补能力"打开局面；目标图本身需要重新设计（这也是**下一轮最该做的事**）。


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

（夜间结束时填）

---

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

（见 `git log --oneline ba83c05..HEAD`；夜间结束时补全清单）

### F.3 未提交 / 不可提交

`data/goalrunner.sqlite3`（账号状态）、`data/overnight/`（含画面证据）、`.ocxdg/`、`.pydeps`
均在 `.gitignore` 内，不进仓库。
