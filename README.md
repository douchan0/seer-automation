# SeerAutomation

> 《赛尔号 Xin 计划》macOS 优先的视觉自动化系统 —— 一个 **不依赖大模型** 的纯视觉自动化内核，外加一层可选的 AI Agent 接口。

**当前状态：Round 0（审计）/ Round 1（性能）/ Round 2（视频分析）/ Round 3（内核）已完成。
Round 4（真机闭环）尚未开始 —— 游戏客户端从未运行，所有能力评级停在 replay。**

```
纯视觉内核  ·  167 个测试  ·  实测量化  ·  0 真机验证（诚实标注）
```

---

## 这是什么

两条路线共用**同一个自动化内核**，不做两套游戏识别逻辑：

| 模式 | 说明 | 状态 |
|---|---|---|
| **模式 A（主）** | 纯视觉、本地状态机、不调用任何 LLM 的独立自动化软件（BetterGI 风格） | 内核已建成，replay 可用 |
| **模式 B（后）** | 在同一内核上暴露 `game.get_status` / `game.start_task` 等语义接口，供 DSH 插件 / MCP / CLI 调用 | 未开始 |

**核心铁律：绝不让大模型参与每一次常规战斗点击的决策。** 高频、确定性、可重复的操作必须由本地状态机完成；AI 只做高层规划与异常分析。

---

## 最重要的技术发现

在本机 macOS 15.7.7 上用编译探针实测（不是查文档推测）：

```
CGWindowListCreateImage  → error: unavailable in macOS: obsoleted in macOS 15.0
CGDisplayCreateImage     → error: unavailable in macOS: obsoleted in macOS 15.0
```

**传统的 Quartz / PyObjC 快速截图路线在 macOS 15 上已经不存在了，不是"慢"，是编译不过。**
ScreenCaptureKit（SCK）是唯一可行的原生截图路径 —— 这是架构硬约束，决定了整个捕获层的设计。

顺带一个反直觉结论：**SCK 单次截图 API 对尺寸不敏感**（2298×1428 全图 vs 300×200 ROI 几乎同价），
所以"ROI 截图更快"是错的，ROI 的收益只能来自下游像素量减少。

---

## 性能（全部实测，非估算）

测量条件：Apple M2 / 8 核 / macOS 15.7.7，达成 load **5.74–7.48**（0.72–0.93/核）。
**不是空闲机器**，所以绝对毫秒数只应作参考；完整数据见 [`docs/PERFORMANCE_BASELINE.md`](docs/PERFORMANCE_BASELINE.md)。

**端到端"看一眼"闭环**（真实游戏画面，OCR 输出逐项一致 12 vs 12）：

| | 旧方案 | 新方案 | 提升 |
|---|---|---|---|
| 截图 + OCR 全流程 | **313.8 ms** | **190.5 ms** | **1.65×** |
| 截图（单次） | 133.6 ms | 44.1 ms | 3.03× |
| 截图（常驻流读最新帧） | — | **0.04 ms** | **3,349×** |
| 状态指纹（3 patch） | — | **0.0385 ms** | 亚毫秒 |

**瓶颈分解（真实游戏内容）**：截图 42.2% + PNG 解码 11.7% + Vision OCR 46.1%。
两边都不是压倒性的 —— 所以**单靠换截图 API 拿不到数量级收益**。

真正的大头在别处，这两条才是关键：

1. **盲目 `sleep`**：旧战斗循环每回合硬等 1.35 s，占单回合约 60%。
2. **每回合全图 OCR**：最便宜的可用 OCR 也要 41.8 ms，而亚毫秒指纹就能判状态。

| 原语 | 耗时 | 能否进 60 Hz 轮询 |
|---|---|---|
| 像素指纹 3-patch | 0.0385 ms | ✅ |
| 降采样帧差 229×142 | 0.008 ms | ✅ |
| ROI 模板 300×200 | 0.96 ms | ✅ |
| ROI 模板 900×400 | 7.84 ms | ⚠️ 仅非逐帧决策 |
| OCR `.fast` ROI | 41.8 ms | ❌ |
| OCR `.accurate` 全图 | 146.4 ms | ❌ |
| 全帧模板匹配 | 63.9 ms | ❌ **禁止** |

---

## 架构

```
ScreenCaptureKit 常驻流（变化驱动，帧龄 ≤ 17 ms）
        │  非阻塞读最新帧 —— 绝不阻塞等下一帧
        ▼
视觉层：模板匹配 / 像素指纹优先，OCR 仅作 UNKNOWN 超时后的升级手段
        ▼
状态机（9 状态，数据驱动，last-seen-with-TTL 信号）
        ▼
执行器（自定节拍轮询、次数+时长预算、暂停/停止、动作结果确认与升级）
        ▼
      游戏
        │
   ┌────┴─────────────────────┐
   ▼                          ▼
模式 A：本地 CLI/GUI      模式 B：Agent 接口（CLI / MCP / DSH 插件）
（无 LLM，默认路径）      （只能调已验证能力，不得绕过安全检查）
```

**三条被实测逼出来的架构约束**（写进 [`AGENTS.md`](AGENTS.md) 作为强制纪律）：

1. **SCK 是变化驱动的** —— 静止画面 5 秒只给 4 帧。执行器必须自己按节拍轮询读最新帧，
   **绝不能阻塞等下一帧**，否则在菜单/弹窗界面会永久挂起。
2. **绝不在主线程同步等 SCK 异步结果** —— 实测挂死 13 分钟（`_dispatch_semaphore_wait_slow`）。
   用专用线程/队列 + 自己的 run loop，或带超时地 pump run loop。
3. **全帧模板匹配和 OCR 不得进入轮询路径** —— 见上表。

---

## 快速开始

环境：macOS 14+（本机 15.7.7）、Python 3.10、Swift 6.2（Xcode CLT）。

```bash
# 依赖装到工作区本地目录（用户级 pip install 在受限环境下会被拒绝）
python3 -m pip install --target .pydeps numpy opencv-python-headless pytest

# 环境自检（会诚实报告游戏窗口是否存在）
PYTHONPATH=.pydeps python3 -m seerauto doctor

# 离线回放：不需要游戏，用 artifacts/ 里的真实截图跑完整视觉链路
PYTHONPATH=.pydeps python3 -m seerauto replay artifacts/

# 单元测试
PYTHONPATH=.pydeps python3 -m pytest -q        # → 167 passed

# 性能基准（26 个采集后端）
python3 bench/bench_all.py
```

`doctor` 的输出会明确写出 `game window: NOT FOUND` 和一行 honesty 声明 —— 这是刻意设计，不是缺陷。

### Swift 构建（可选的 native 工具）

```bash
# 三个在这个环境里必踩的坑，已在 AGENTS.md 记录
mkdir -p /tmp/mc
swiftc -O -parse-as-library -module-cache-path /tmp/mc \
       -o native/bin/bench_capture native/bench_capture.swift \
       -framework ScreenCaptureKit -framework CoreGraphics -framework Vision -framework AppKit
```

---

## 仓库结构

```
AGENTS.md                  工程入口：任何 Agent/开发者从这里开始
docs/
  PROJECT_AUDIT.md         环境事实、资产清单、阻塞项
  EXISTING_IMPLEMENTATION.md  上一轮工程到底建成了什么、什么从没验证过
  KNOWN_PROBLEMS.md        P1–P10 问题清单（按代价排序）
  PERFORMANCE_BASELINE.md  ⭐ 实测基准（626 行，含完整方法与复现命令）
  PERFORMANCE_LOG.md       每次测量的追加日志（含 loadavg）
  PROGRESS.md              轮次进度
  NEXT_ACTIONS.md          ⭐ 下一轮具体执行计划
  state.md                 ⭐ 已验证 UI 坐标（现场标定的最贵资产）
  action-log.md            真实操作日志（含失败记录）
  PUNI_VIDEO_ANALYSIS.md   谱尼视频结构化分析（436 行）
  PUNI_TASK_GRAPH.md       42 个任务节点 + Mermaid 图
  PUNI_UNKNOWN_MECHANICS.md  34 条未决缺口
  _RESEARCH_DIGEST.md      研究报告交叉核对（8 支持 / 7 证伪 / 5 未验证）
seerauto/                  纯视觉内核（13 模块，无 LLM / 无网络）
  capture.py window.py vision.py state.py actions.py executor.py
  perf.py config.py tasks/battle.py cli.py PROTOCOL.md
tests/                     167 个测试
bench/                     性能基准套件
native/                    Swift 源码（采集/输入）
scripts/                   上一轮工具链（保留作参考与对照基线，未改动）
artifacts/                 真实游戏截图 —— 同时是 replay 测试集
```

> `artifacts/` 里的截图是**测试 fixture**，`pytest` 与 `replay` 都依赖它们，因此随仓库分发。

---

## 下一步

**唯一的硬阻塞：启动 `/Applications/SeerGame.app` 并登录。**

在此之前所有能力评级停在 replay —— 93 个 fixture 里 `UNKNOWN` 占 60，
胜负/奖励识别规则因没有任何对应截图而被禁用。

解锁后按 [`docs/NEXT_ACTIONS.md`](docs/NEXT_ACTIONS.md) 走：

1. 用真实游戏窗口校准捕获层（当前所有捕获数字都是在别的窗口上测的）
2. 建立 replay 集 + 模板库（手动走一遍战斗，采集 ≥200 连续帧）
3. 内核接真机（原生 SCK 守护进程，`DaemonCapture` 目前只对假守护进程测过协议）
4. ⭐ **单场真实战斗闭环** —— 这一项是"整套东西成不成立"的唯一判据
5. 长战看护（谱尼封印 5 实测 125 回合、真身 165 回合，现有 `range(40)` 必须移除）

---

## ⚠️ 诚实边界

这个仓库刻意把"没做到的事"写得很清楚，请不要误读：

| 项 | 状态 |
|---|---|
| 真机验证 | **一次都没有**。游戏客户端全程未启动 |
| 能力评级 | 全部停在 replay。42 个谱尼任务节点里 `auto` 只有 3 个，`proven` 为 **0** |
| 战斗闭环 | 未跑通。上一轮留下的 `tower.py` 最后三条日志都是失败/停止 |
| 胜负/奖励识别 | 规则被禁用（无对应 fixture） |
| 原生 SCK 守护进程 | 未实现。当前可用路径仍是 139 ms/次的子进程 |
| 性能数字 | 非空闲机器测量，只应作参考；比值可信，绝对值不可 |
| 谱尼三天路线 | **当前状态不可达**，且不是代码问题 —— 7 只主力里 3 只交易行独占，缺萨帕克则封印 1 与真身第一命无解 |

**"视频里是这样" ≠ "当前版本支持"。** 所有涉及游戏机制的结论都在文档里标注了证据等级。

---

## 安全与合规

- 只使用**正常截图 + 正常 GUI 输入**。不做游戏内存注入、不篡改服务端数据、不规避反作弊。
- 输入前必须校验窗口身份与几何，窗口移动超容差即拒绝点击。
- 任务具备次数预算 + 时间预算，支持暂停、恢复与紧急停止。
- 未知状态不允许持续盲点，必须有超时升级路径。
- 截图未经授权不会上传至任何外部模型。
- 仅在用户授权的游戏环境中使用。自动化行为可能违反游戏用户协议，风险由使用者自行承担。

---

## 文档索引

新接手请按此顺序读：**`AGENTS.md` → `docs/PROJECT_AUDIT.md` → `docs/EXISTING_IMPLEMENTATION.md` → `docs/KNOWN_PROBLEMS.md` → `docs/PERFORMANCE_BASELINE.md` → `docs/PROGRESS.md` → `docs/NEXT_ACTIONS.md`**

工程状态不依赖聊天上下文 —— 换 Agent 或重开对话都能从这些文档恢复进度。
