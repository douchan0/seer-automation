# NEXT_ACTIONS — 下一轮执行计划

> 给**下一个接手的 Agent**。每条都有：做什么、改哪个文件、怎么验收、卡在哪。
> 上一轮状态见 `docs/PROGRESS.md`；问题背景见 `docs/KNOWN_PROBLEMS.md`；数据见 `docs/PERFORMANCE_BASELINE.md`。

---

## 当前一句话状态

**性能问题已经被量化并解决了组合部分**：截图 139 ms → 0.4 ms（常驻流读最新帧），状态识别的正确做法（亚毫秒像素指纹）已被证明可行。
**但一个字都还没在真机上验证过** —— 游戏客户端今天全程未启动。

---

## 第 0 步（前置，5 分钟）— 解除阻塞 B1

**这是所有后续工作的前提，必须由用户完成：**

1. 启动 `/Applications/SeerGame.app`，登录账号 `hello / 188365991`。
2. 确认客户端窗口出现后，运行：
   ```bash
   printf '{"op":"win"}\n{"op":"quit"}\n' | ./scripts/seerd
   ```
   期望返回 `{"id":…,"x":…,"y":…,"w":…,"h":…}` 而不是 `{"error":"no window"}`。
3. 确认辅助功能授权仍有效（`AXIsProcessTrusted`，Round 1 实测为 `true`，但需在游戏上下文中复验）。
4. **不要**先急着跑战斗。先做第 1 步的只读采集。

> 若用户不启动游戏：第 1 步之外的**所有**工作仍可推进（离线 Replay、算法、打包、接口层），
> 但**不得**把任何结果写成"真机验证通过"。

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
