# NEXT_ACTIONS — 下一轮执行计划

> 给**下一个接手的 Agent**。每条都有：做什么、改哪个文件、怎么验收、卡在哪。
> 上一轮状态见 `docs/PROGRESS.md`；问题背景见 `docs/KNOWN_PROBLEMS.md`；数据见 `docs/PERFORMANCE_BASELINE.md`。

---

## 当前一句话状态

**Round 4 的核心已经打通并在真机上验证**：客户端 1.0.8.9、窗口 1200×800pt → 2400×1600px、
服务器 1 号啦公星、角色 hello。**3~5 场连战全胜，0 失败**，每回合 1 次点击、约 3 s/回合，
视觉循环只占回合时间的 **~1.2%**（capture 27 ms + detect 8 ms）。

**缺的不是性能，是覆盖面**：失败分支、暂停/恢复、异常场景都还没有真机样本。
另外服务器登录页的公告明确「不允许使用第三方软件、脚本」，真机自动化需用户持续确认风险自担。

**下一轮请先做「第 A 步」，不要重新做 Round 0/1。**

---

## 第 A 步（P0，1 小时）— 补齐真机异常与生命周期验证

| 任务 | 文件 | 验收 |
|---|---|---|
| A1 **暂停 / 恢复** | `seerauto/executor.py` + `tools/battle_run.py` | 战斗中按下暂停，点击立即停止；恢复后从当前回合继续，不重复出招 |
| A2 **紧急停止** | `tools/battle_run.py` | SIGINT / Ctrl-C 后 1 s 内停止点击，进程干净退出，游戏保持运行 |
| A3 **失败分支样本** | `artifacts/live/` + `seerauto/templates/live/` | 在真机上**故意输一场**（带一只低等级精灵），截取失败面板，产出 `live_defeat_word` 模板并验证区分度 |
| A4 **战斗结束推断兜底** | `tools/battle_run.py` | 若"战斗按钮消失但没有胜利面板"→ 判定为失败并记录，不得继续盲点 |

> A3 是目前**唯一无法从视频补的样本**：视频 40 分钟里作者一次没输过
> （见 `docs/TEMPLATE_CANDIDATES.md`），只能真机制造。

---

## 第 B 步（P1）— 把已验证的能力接回内核

现在跑通的是 `tools/battle_run.py`（一个独立脚本）。**要接回 `seerauto/` 才算产品化**：

| 任务 | 文件 | 验收 |
|---|---|---|
| B1 把 `live_*` 模板接进配置，启用 `battle_result_victory` 规则 | `seerauto/config/default.json` | `pytest` 覆盖：胜利帧命中、战斗帧不命中 |
| B2 `BattleTask` 用内核状态机跑完一场 | `seerauto/tasks/battle.py` | `python3 -m seerauto replay artifacts/live/` 能正确判出胜利 |
| B3 把 `tools/ctl.py` 的单步能力提升为 `seerauto` 的 CLI 子命令 | `seerauto/cli.py` | `python3 -m seerauto look / click / step` 可用 |
| B4 用内核跑 3 场连战，与 `tools/battle_run.py` 对比 | — | 两者场次/胜率/回合数一致 |

> **注意 stage 偏移会随会话变化**：真机截图的 stage 起点是 (154,210)，而
> `seerauto` 参考几何是 (102,128)。**必须每次从画面算，不能写死**（见 `docs/TEMPLATE_CANDIDATES.md`）。

---

## 第 C 步（P2）— 扩大战斗覆盖面

| 任务 | 说明 |
|---|---|
| C1 四技能选择 | 技能格坐标已标定但**未逐个点击验证**；先验证坐标，再做选择策略（例如"优先高威力技能"） |
| C2 血量/PP 读取 | `89/92`、`使用次数：31/35` 都在屏幕上，可接 OCR 升级路径做资源判断与"该吃药了" |
| C3 多精灵/换精灵 | 右下「精灵」按钮流程未验证 |
| C4 连续刷塔 | 谱尼路线需要 试炼之塔 / 勇者之塔；入口尚未定位（SPT 里搜「试炼之塔」返回 0 个入口，需另找路径） |
| C5 交易行门槛推进 | 已确认门槛是**击败 SPT 首领**（面板原文「击败后解锁交易行资格」）；73 个入口里已击败 1 个（蘑菇怪） |

---

## 第 D 步（P3）— 产品化（前几步验收通过后才做）

按用户要求顺序：任务调度器 → GUI（先 CLI/TUI 更省）→ 本地任务服务 → MCP Server →
DSH 插件（已核实真实 API：`export const name/inject/apply` + `defineTool`，版本 `0.2.1-rc.5`）→ `.app` 打包。

对外语义接口固定：`game.get_status` / `game.list_tasks` / `game.start_task` /
`game.pause_task` / `game.stop_task` / `game.get_progress`。

---

## 环境与命令速查（真机已验证）

```bash
PYTHONPATH=.pydeps python3 -m pytest -q                 # 171 passed
PYTHONPATH=.pydeps python3 -m seerauto doctor           # 会如实报 game window 状态

./tools/build_seerdx.sh                                  # 构建原生 SCK 守护进程
PYTHONPATH=.pydeps python3 tools/ctl.py look             # 单步：看一眼（绝不点击）
PYTHONPATH=.pydeps python3 tools/ctl.py clicktext "确认"  # 单步：点文字
PYTHONPATH=.pydeps python3 tools/battle_run.py --battles 3   # 多场连战（真机）
```

**三条必须遵守的真机纪律**（都是踩过坑总结的）：
1. **点击前必须确认游戏窗口在最前面** —— `ScreenCaptureKit` 会照常截取被遮挡的窗口，
   但 `CGEvent` 点击会落到最前面的那个 App 上。`tools/ctl.py` 已有 `ensure_frontmost` 守卫。
2. **常驻流激活时不要用 `shot`** —— 实测会返回近空白图（6 KB），导致 OCR 静默返回空。
   要 OCR 就**直接把暖流帧导出成 PNG**。
3. **回合推进靠"日志指纹变化 + 画面稳定"两个条件**，不要靠固定 sleep，也不要只看按钮在不在
   （战斗按钮在胜利遮罩下依然可见）。

---

## 历史步骤（Round 4 之前的阻塞项，已完成，保留供追溯）

- ~~B1 启动游戏客户端~~ ✅ 2026-11-01 23:35 用户启动并授权
- ~~重新标定坐标系~~ ✅ 见 `docs/state.md`（1200×800 新坐标系）
- ~~单场真实战斗闭环~~ ✅ 见 `docs/PROGRESS.md` Round 4
- ~~多次连续战斗~~ ✅ 3 场连战全胜


---

## 第 1 步（P0，零风险，先做）— 用真实窗口校准捕获层

**为什么先做**：目前所有捕获数字都是在**别的窗口**上测的（WPS / Finder / 自建窗口），
游戏窗口是 Flash/Electron 渲染，捕获路径可能不同。必须先确认。

| 任务 | 文件 | 验收 |
|---|---|---|
| 1.1 把 SCK 常驻流封装成 `seerdx` 守护进程（JSON-line：`stream.start` / `latest` / `roi` / `stream.stop`） | `native/seerdx.swift` → `bin/seerdx` | 对游戏窗口启动流，能持续拿到帧 |
| 1.2 实测游戏窗口：per-call 截图 vs 常驻流读最新帧 | `bench/bench_game_window.py` | 出一张真实游戏窗口的对比表，写入 `docs/PERFORMANCE_LOG.md` |
| 1.3 确认 Retina 与坐标：`game.png` 尺寸必须仍是 2298×1428 | `seerauto/window.py` | 尺寸/原点与 `docs/state.md` 一致，否则坐标全部重标 |
| 1.4 多显示器检查（本机实测有 2 个显示器） | `seerauto/window.py` | 明确游戏在哪个 display，坐标系不串 |

**注意**：`SCStream` 是**变化驱动**的。静止画面下 5 秒只有 14 帧（实测）。
所以 `latest` 必须非阻塞返回旧帧；执行器**按自己的节拍轮询**，**绝不阻塞等下一帧**，否则在菜单/弹窗界面永久挂起。

---

## 第 2 步（P0）— 建立真实截图 Replay 集与模板库

**为什么**：`artifacts/` 只有昨天的 ~85 张静态图，不足以覆盖战斗全过程。

| 任务 | 文件 | 验收 |
|---|---|---|
| 2.1 在游戏内**手动**走一遍：主城 → 地图 → 试炼之塔 → 进入战斗 → 打完一场 → 结算 | `bench/capture_session.py` | 采集 ≥200 帧带时间戳的连续帧 |
| 2.2 标注状态：为每帧打 `IDLE / BATTLE_TURN / BATTLE_RESULT / DIALOG / REWARD` | `data/replay/<session>/labels.json` | ≥200 帧有标签 |
| 2.3 从帧里抠模板：技能按钮、战斗按钮、胜利/失败字样、当前回合区域 | `seerauto/templates/*.png` | 每个模板跑一次匹配能稳定命中 |
| 2.4 用 Replay 集跑状态分类准确率 | `bench/bench_state_accuracy.py` | 报告 precision/recall/混淆矩阵 |

> **这一步是把"能在真机上跑"变成"可回归"的关键。** 没有 Replay 集，之后每次改状态机都要重新开游戏验证。

---

## 第 3 步（P0）— 接通内核到真实游戏（替代 seerd）

| 任务 | 文件 | 验收 |
|---|---|---|
| 3.1 实现 `DaemonCapture` 真后端（对接 `bin/seerdx`） | `seerauto/capture.py` | `python3 -m seerauto doctor` 显示窗口 FOUND + 捕获正常 |
| 3.2 状态机接入真实帧 | `seerauto/state.py` | 在真实游戏里连续 60 秒，状态序列无 `UNKNOWN` 卡死 |
| 3.3 动作层窗口身份校验上线 | `seerauto/actions.py` | 故意拖动窗口 → 必须拒绝点击并抛错 |
| 3.4 把 `perf.py` 接到真实循环 | `seerauto/perf.py` | 输出真实分阶段 P50/P95 |

---

## 第 4 步（P1）— 单场真实战斗闭环 ⭐ 本轮核心验收

**这是用户明确要求的 Round 4。必须逐步验证，不许跳步。**

| 顺序 | 验证项 | 通过标准 |
|---|---|---|
| 4.1 | 单次视觉识别 | 能正确说出"现在是战斗中/不在战斗中"，人工对照 20 次全对 |
| 4.2 | 单次授权输入 | 点一次技能，肉眼确认游戏有反应 |
| 4.3 | 操作后状态变化确认 | 输入后能检测到"回合数 +1"或"动画结束" |
| 4.4 | **一次完整战斗** | 从"进入战斗"到"看到胜利/失败"，全程无人工干预 |
| 4.5 | 多次连续战斗 | 连续 5 场，全部正确结算，计数正确 |
| 4.6 | 超时与异常 | 人为制造卡死（如战斗中打开背包），必须触发超时并安全退出 |
| 4.7 | 暂停/恢复/停止 | 三个操作都能在中途正确响应 |

**必须记录的指标**（写入 `docs/PERFORMANCE_LOG.md`）：
- 每回合闭环延迟 P50/P95（capture + detect + decide + input + wait + confirm）
- 与旧方案 `tower.py` 的对比（旧的单回合约 2.0–2.2 s，其中 1.35 s 是盲等）
- 点击失败次数、状态误判次数

**失败时的处置**：先修到 4.4 通过，再谈 4.5。**不许在 4.4 未通过时声称闭环完成。**

---

## 第 5 步（P1）— 长战看护

普尼战实测：封印 5 = **125 回合**，真身 = **165 回合**，且真身是**两场连战**。
现有 `for turn in range(40)` 必须彻底移除，改为配置驱动 + 时间预算。

| 任务 | 验收 |
|---|---|
| 5.1 回合上限改为 `config` 可配（默认 ≥200） | 单测覆盖 |
| 5.2 回合数重置检测（真身第 1 命结束 → 第 2 场从 1 开始） | Replay 集里能正确识别两场连战 |
| 5.3 `圣光气` = 敌方空 PP 的字符串匹配判据 | 在 Replay 集上验证命中率 |
| 5.4 勇者之塔失败重启策略（失败即需重来） | 单测覆盖 |

---

## 第 6 步（P2）— 交易行门槛查证（零风险，但需要游戏在线）

视频分析师指出这是**最高杠杆的未知**：交易行到底是"注册满 3 天的时间门槛"还是"3 个任务门槛"，
两者互斥，且决定整张任务图左半张能否推进。

| 任务 | 验收 |
|---|---|
| 6.1 全屏 OCR 交易行门槛面板，记录**完整原文** | 写入 `docs/PUNI_UNKNOWN_MECHANICS.md` 的 U-A1 |
| 6.2 核对 `state.md` 里"需通关黑暗武斗场+SPT雷伊+SPT哈莫雷特"这条记录的真实出处 | 标注是实测还是推断 |
| 6.3 若确认是任务门槛：评估 3 个任务各自的自动化可行性 | 更新任务图评级 |

> 注意：视频里"官方商城买闪光尼尔"经复核实为**交易行**（07:29 画面有性格/特性过滤，商城没有）。
> 所以 7 只主力里有 3 只是交易行独占，**缺萨帕克则封印 1 与真身第一命无解**。

---

## 第 7 步（P2）— 产品化（在闭环稳定之后才做）

按用户要求的顺序，**前面没验收通过不许开工**：

1. 任务调度器（多任务队列、优先级、互斥）
2. PySide6 GUI（或先做 CLI + 简单 TUI，成本更低）
3. 本地任务服务 + MCP Server
4. DSH 插件（已核实真实 API：`export const name/inject/apply` + `defineTool`，版本 `0.2.1-rc.5`）
5. `.app` 打包

对外语义接口固定为：
`game.get_status` / `game.list_tasks` / `game.start_task` / `game.pause_task` / `game.stop_task` / `game.get_progress`

---

## 已知的坑（避免重复踩）

| 坑 | 现象 | 解法 |
|---|---|---|
| swiftc ModuleCache | `Operation not permitted ... .pcm` | `-module-cache-path /tmp/x/mc` |
| swiftc 单文件 @main | `cannot be used in a module that contains top-level code` | `-parse-as-library` |
| SCK 窗口过滤器 | `Assertion failed: (did_initialize), CGS_REQUIRE_INIT` | `main()` 第一行 `_ = NSApplication.shared` |
| SCK 列表缺窗口 | 自己刚创建的窗口不在 `SCShareableContent` 里 | `orderFrontRegardless()` 后跑一次 runloop |
| Swift CoW 假计时 | 读帧测出 0.000 ms | 必须 memcpy 进预分配缓冲，不能用数组字面量拷贝 |
| pip 用户级安装 | `Operation not permitted: ~/.local/lib` | `--target .pydeps` + `PYTHONPATH=.pydeps` |
| macOS 大小写不敏感 | `PROGRESS.md` 覆盖了 `progress.md` | 新建文档前先 `ls docs/ \| grep -i` |
| 测量失真 | 多 Agent 并行时 loadavg 高达 85 | 测量前确认 loadavg/核数 < 0.5，并在结果里记录 |

---

## 优先级总表

| 优先 | 任务 | 阻塞来源 | 预计 |
|---|---|---|---|
| P0 | 第 0 步 启动游戏 | **用户** | 5 min |
| P0 | 第 1 步 真实窗口捕获校准 | 第 0 步 | 2 h |
| P0 | 第 2 步 Replay 集 + 模板库 | 第 0 步 | 3 h |
| P0 | 第 3 步 内核接真机 | 第 1–2 步 | 2 h |
| P1 | 第 4 步 单场真实战斗闭环 ⭐ | 第 3 步 | 3 h |
| P1 | 第 5 步 长战看护 | 第 4 步 | 2 h |
| P2 | 第 6 步 交易行门槛查证 | 第 0 步 | 30 min |
| P2 | 第 7 步 产品化 | 第 4–5 步 | 数天 |

**如果只能做一件事**：做第 4 步。它是"这套东西到底成不成立"的唯一判据。
