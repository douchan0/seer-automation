# PROGRESS — 轮次进度

> 规则：每轮结束追加一条。**新 Agent 接手时先读这里。**
> 详细问题见 `docs/KNOWN_PROBLEMS.md`，详细数据见 `docs/PERFORMANCE_BASELINE.md`。

---

## Round 0 — 工程接管与现状审计 ✅ 完成

**时间**：2026-11-01 16:21 – 16:40

### 完成了什么

1. 接管并审计了 2026-10-31 留下的工程，**没有新建无关项目**。
2. 建立了 git 仓库并打首个 checkpoint（`4ed6ff9`），此前**工程完全没有版本控制**。
3. 摸清环境：macOS 15.7.7 / Apple M2 arm64 / Python 3.10.13 / Swift 6.2.3 / Node 25.6.1。
4. 盘点全部资产：196MB 视频、417 帧、~85 张真实截图、客户端逆向产物、20+ 个实测 UI 坐标。
5. 明确列出 **NOT FOUND** 项（无测试、无依赖清单、无性能数据、无 DSH 游戏插件、无真机连续成功记录）。
6. 首次实测了旧链路的真实延迟（旧日志里的数字不可信）。
7. **发现并证实了本机最关键的硬约束**：`CGWindowListCreateImage` 与 `CGDisplayCreateImage` 在 macOS 15 已被移除，SCK 是唯一路径。

### 修改了哪些文件

| 文件 | 类型 |
|---|---|
| `.gitignore` | 新建 |
| `AGENTS.md` | 新建（工程入口） |
| `docs/PROJECT_AUDIT.md` | 新建 |
| `docs/EXISTING_IMPLEMENTATION.md` | 新建 |
| `docs/KNOWN_PROBLEMS.md` | 新建 |
| `docs/PROGRESS.md` | 新建（本文） |
| `docs/NEXT_ACTIONS.md` | 新建 |
| **原有文件一个都没有改** | — |

### 实际执行的命令与测试

```bash
sw_vers; uname -m; sysctl -n machdep.cpu.brand_string
python3 -V; swiftc --version; node -v
python3 -c "import cv2/objc/Quartz/PySide6..."        # 依赖逐项探测
git init; git add -A; git commit                       # checkpoint
printf '{"op":"win"}\n{"op":"quit"}\n' | ./scripts/seerd   # → {"error":"no window"}
screencapture -x /tmp/_bench_full.png                  # → 2940x1912，证明屏幕录制权限已授
swiftc ... avail.swift                                 # 编译探针：证伪旧截图 API
```

### 成功 / 失败 / 未运行

| 项 | 状态 |
|---|---|
| 环境审计 | ✅ 成功 |
| 资产盘点 | ✅ 成功 |
| 旧链路线图理解 | ✅ 成功（逐行读源码） |
| git checkpoint | ✅ 成功 |
| 旧截图 API 可用性 | ✅ 已证伪（编译期错误） |
| **真机任何操作** | ❌ **未运行 —— 游戏客户端未启动** |
| 辅助功能授权复核 | ❌ 未运行（依赖游戏启动） |

### 当前最严重的技术瓶颈

按代价排序（`docs/KNOWN_PROBLEMS.md`）：
1. **P3 用固定 `sleep` 代替状态检测**（1.35 s/回合，占单回合约 60%）
2. **P2 全图 Vision `.accurate` OCR**（240–685 ms/次）
3. **P1 子进程截图**（139 ms/次）

### 相比上一轮的改进

- 从"没有任何版本控制、状态只在聊天里" → 有 git、有 AGENTS.md、有可恢复的文档化状态。
- 从"凭感觉认为截图慢" → 有分项实测数据。
- 从"按研究报告假设实现" → 发现了会让假设直接失效的 SDK 约束。

---

## Round 1 — 截图与操作性能分析 🔄 进行中

**时间**：2026-11-01 16:35 – 17:0x

### 已完成的核心测量

| 阶段 | 旧方案实测 | 新方案实测 | 提升 |
|---|---|---|---|
| 截图（取当前画面） | `screencapture` 子进程 **139.4 ms** | SCStream 读最新帧 **0.52 ms** | **~268×** |
| 状态识别（判战斗/非战斗） | 全图 Vision `.accurate` **486–685 ms** | 像素指纹 **0.06 ms** / 降采样帧差 **0.32 ms** | **~8000×** |
| 输入（点击提交） | 0.015 ms + **67 ms 硬编码 sleep** | 0.015 ms（sleep 可去掉） | 取决于设计 |
| 回合等待 | **1350 ms 盲等** | 状态变化驱动（游戏动画本身不可压缩） | 消除浪费与"点太早" |

### 关键结论（都有实测支撑）

1. **旧日志里的"截图 138ms / OCR 240ms"基本可信，但没有 P50/P95，且 OCR 复测明显更慢。**
2. **单次截图 API 对尺寸不敏感**（2298×1428 全图 111.8 ms vs 300×200 ROI 124.7 ms）——
   "ROI 截图更快"这个直觉是**错的**，ROI 的收益只能来自下游像素量减少。
3. **常驻 SCStream 才是正确的截图架构**：帧边际成本趋近 0，读最新帧 0.52 ms，帧龄 ≤ 17 ms。
4. **SCK 是变化驱动的**：静止画面下 5 秒只给 14 帧。所以执行器必须"自己按节拍轮询读最新帧"，
   **绝不能阻塞等下一帧**，否则在菜单/弹窗界面会永久挂起。
5. **OCR 必须完全移出热路径**：最便宜的可用 OCR 也要约 120 ms，全图 `.accurate` 高达 685 ms。
   状态识别应基于亚毫秒的像素指纹/降采样帧差，模板匹配只在很小的 ROI 上做。
6. **输入 API 本身不是瓶颈**（提交 0.015 ms），但旧代码在每次点击里硬编码了 **67 ms** 的 `usleep`。

### 修改了哪些文件

| 文件 | 归属 | 状态 |
|---|---|---|
| `bench/`（基准套件） | bench-engineer | 进行中 |
| `native/bench_capture.swift` | bench-engineer | 进行中 |
| `docs/PERFORMANCE_BASELINE.md` | bench-engineer | 进行中 |
| `docs/PERFORMANCE_LOG.md` | bench-engineer | 进行中 |
| `seerauto/`（内核） | core-engineer | 进行中 |
| `tests/` | core-engineer | 进行中 |
| `docs/PUNI_*.md` | video-analyst | 进行中 |
| `docs/_RESEARCH_DIGEST.md` | research-digester | 进行中 |

### 测量方法学问题（重要）

**所有本轮测量都在 4 个 Agent 并行占用机器的条件下完成**，`vm.loadavg` 一度高达 **85**（8 核）。
因此：
- **绝对毫秒数被系统性夸大**，只能作为上界参考。
- **同一次运行内的比值是可信的**（如 .fast vs .accurate、ROI vs 全图）。
- 收尾时会**静默重跑一次 headline 表**，得到可信的绝对数字。
- 文档中两组数字必须分开标注 `LOADED RUN` / `QUIESCED RUN`。

### 成功 / 失败 / 未运行

| 项 | 状态 |
|---|---|
| 旧方案基线计时 | ✅ 成功（真实数据） |
| 旧截图 API 可用性判定 | ✅ 成功（编译期证伪） |
| SCK 单次截图计时 | ✅ 成功 |
| SCK 常驻流计时 | ✅ 成功 |
| Vision OCR 分级/分 ROI 计时 | ✅ 成功 |
| OpenCV 模板/指纹/帧差计时 | ✅ 成功 |
| 输入提交延迟 | ✅ 成功（`AXIsProcessTrusted = true`） |
| 输入→游戏反应的**真实**延迟 | ❌ **未运行**（无游戏窗口，无法观测效果） |
| 窗口级 SCK 截图（对游戏窗口） | ❌ 未运行（游戏未启动；已用普通窗口替代并标注） |

### 当前最严重的技术瓶颈（优化后）

1. **游戏自身动画延迟** —— 这是唯一不可压缩的部分，必须通过"检测动画结束"而非"猜时长"来处理。
2. **尚未有真机验证的一切** —— 捕获与识别链路理论上已快 2 个数量级，但**没有一次真实战斗证明它成立**。

### 下一轮最值得执行的任务

见 `docs/NEXT_ACTIONS.md`。优先级最高的是 **Round 4 的单场真实战斗闭环**，前置条件是用户启动游戏。

---

## 附录 A — 上一轮（2026-10-31）进度快照（历史原文，勿删）

> 来源：接管前的 `docs/progress.md`，原文保存在 git checkpoint `4ed6ff9` 中。
> 保留原因：这是**唯一**逐小时记录了上一轮真实进展的文档，含"单次点击 78ms"这类当时的一手观感值。
> 注意：接管时将 `progress.md` 合并进本文件（macOS 文件系统不分大小写，`PROGRESS.md` 与 `progress.md` 是同一个文件）。

| 时间 | 内容 |
|---|---|
| 17:20 | 接手：账号 hello/188365991，小火猴 Lv5，赛尔豆 0，XIN豆 5000 |
| 17:45 | 与海盗对战（失败/逃跑），小火猴升到 Lv12 |
| 18:03 | 建成常驻驱动 seerd（单次点击 78ms） |
| 18:15 | 通过 SPT 找到谱尼入口：太空站左翼 → 勇者之塔神秘领域 |
| 18:15 | 试炼之塔开刷：Lv12 → 16（进化 烈火猴）→ 32 |
| 18:35 | 继续刷塔中（目标 Lv36，进化烈焰猩猩） |

> **对上述数字的复核（Round 1）**：`单次点击 78ms` 与实测不矛盾——
> `seerd.clickAt()` 内部硬编码了 15 + 12 + 40 = **67 ms** 的 `usleep`，加上 CGEvent 提交与往返开销正好落在 70–80 ms 区间。
> 这 67 ms **是可以去掉的**，不是 API 的固有成本（CGEvent 提交实测仅 0.015 ms）。
