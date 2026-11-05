# AGENTS.md — 工程入口（任何 Agent 从这里开始）

> 这是一个 **macOS 优先的游戏视觉自动化**项目，目标游戏：《赛尔号 Xin 计划》（Xin 服，客户端 `/Applications/SeerGame.app`，Electron + PepperFlash）。
> 本文档是唯一的"工程入口"。**不要靠聊天上下文理解本项目** —— 聊天会丢，文件不会。

---

## 0. 30 秒读懂本项目

**要做什么**：一套共享内核的双模式自动化系统。
- **模式 A（主）**：纯视觉、**不依赖任何大模型**的独立自动化软件（BetterGI 风格）。
- **模式 B（后）**：在同一内核上暴露高层语义接口（CLI / 本地服务 / MCP / DSH 插件），让 AI 做规划与异常分析。

**核心铁律**：**绝不让大模型参与每一次常规战斗点击的决策。** 高频、确定性、可重复的操作必须由本地状态机完成。

**当前阶段**：Round 0–4 完成；**Round 5（AI 辅助长期目标运行器）部分交付**——
Goal Runner / 目标图 / 世界状态 / 语义技能 / AI 规划器 / CLI 全部落地并在真机跑通，
但**谱尼节点完成数仍为 0**（练级未完成）。详见 `docs/REAL_DELIVERY_REPORT.md`。
真机已验证：登录 → 进入游戏 → 进战斗 → 自动出招 → 胜利 → 确认 → 连续 11 场全胜。
每回合 1 次点击、约 3 s/回合（游戏自身节奏），视觉循环只占回合时间 **~1.2%**。

**真机现状**：客户端 1.0.8.9，窗口 **1200×800 pt → 2400×1600 px**（scale 2.0）；
**窗口位置每次会话都会变**（Round 4: (225,53)，Round 5: (270,87)）——必须每次从 `seerdx win` 读，不能写死。
服务器 1 号啦公星，角色 `hello`。**坐标空间与 Round 3 之前不同，见 `docs/state.md`。**

---

## 1. 必读顺序（新 Agent 请严格按序）

| 顺序 | 文件 | 为什么读 |
|---|---|---|
| 1 | `AGENTS.md`（本文） | 规则、目录、命令 |
| 2 | `docs/PROJECT_AUDIT.md` | 环境事实、资产清单、**macOS 15 截图 API 已被移除**这一硬约束、阻塞项 |
| 3 | `docs/EXISTING_IMPLEMENTATION.md` | 上一轮到底建成了什么、哪些能复用、哪些从没验证过 |
| 4 | `docs/KNOWN_PROBLEMS.md` | 按代价排序的问题清单，决定你下一步改什么 |
| 5 | `docs/PERFORMANCE_BASELINE.md` | 实测延迟数据与瓶颈排序 |
| 6 | `docs/PROGRESS.md` | 已完成到哪一轮 |
| 7 | `docs/NEXT_ACTIONS.md` | 下一轮该做什么（具体到文件） |
| 8 | `docs/state.md` | 已验证的 UI 坐标（真金白银的现场标定） |

---

## 2. 目录结构

```
seer-puni-quest/
├── AGENTS.md                  ← 你在这里
├── docs/
│   ├── PROJECT_AUDIT.md           Round 0 审计（必读）
│   ├── EXISTING_IMPLEMENTATION.md Round 0 现状说明（必读）
│   ├── KNOWN_PROBLEMS.md          Round 0 问题清单（必读）
│   ├── PERFORMANCE_BASELINE.md    Round 1 实测基准
│   ├── PERFORMANCE_LOG.md         每次测量的追加日志
│   ├── PROGRESS.md                轮次进度
│   ├── NEXT_ACTIONS.md            下一轮任务
│   ├── state.md                   ⭐ 已验证坐标/账号状态（最贵资产）
│   ├── action-log.md              ⭐ 真实操作日志（含失败）
│   ├── plan-from-video.md         视频拆解原始记录（勿改）
│   ├── checklist.md               行动清单（勿改）
│   ├── PUNI_VIDEO_ANALYSIS.md     Round 2
│   ├── PUNI_TASK_GRAPH.md         Round 2
│   ├── PUNI_UNKNOWN_MECHANICS.md  Round 2
│   └── _RESEARCH_DIGEST.md        研究报告交叉核对
├── seerauto/                  ← 新内核（纯视觉，无 LLM 依赖）
├── tests/                     ← pytest
├── bench/                     ← 性能基准
├── native/                    ← Swift 源（截图/输入）
├── scripts/                   ← 上一轮工具链（保留，勿随意改）
│   ├── seerd.swift / seerd    常驻守护进程（JSON-line）
│   ├── seerctl.swift / seerctl 一次性 CLI（调试用）
│   ├── fast.py                Driver 兼容层
│   ├── play.py                旧的最慢方案（性能对标基线）
│   └── tower.py / farm_tower.py  旧战斗循环（反面教材）
├── artifacts/                 ← 真实截图（Replay 测试集，勿删）
│   ├── game.png               2298×1428 主参考帧
│   ├── battle.png / scene_*.png
│   ├── scene_index.json       地图 12 格 OCR 索引
│   ├── video/BV1Fu8h6YEAz.mp4 196MB《三天获取普尼》
│   └── unpacked/              客户端 Electron dist 逆向
└── frames/                    ← 417 帧视频抽帧
```

---

## 3. 环境事实（别猜，已实测）

| 项 | 值 |
|---|---|
| macOS | 15.7.7 (24G720) |
| 芯片 | Apple M2 / arm64 |
| 屏幕 | 2940×1912 px（逻辑 1470×956），**2 个显示器** |
| 游戏窗口（当前） | 逻辑 **1200×800**，抓取 **2400×1600**（scale=2.0），位于 **(225,53)** |
| 游戏窗口（Round 3 前，已作废） | 1149×714 → 2298×1428 @ (300,120)；**宽高比不同，旧坐标不可缩放复用** |
| Python | 3.10.13（pyenv） |
| Swift | 6.2.3（Xcode CLT） |
| Node | v25.6.1 |

### 3.1 三个必然踩到的坑（已解决，照抄即可）

```bash
# 坑 1：swiftc 默认 ModuleCache 被沙箱拒绝
swiftc -O -module-cache-path /tmp/mc -o out src.swift    # mkdir -p /tmp/mc 先生效

# 坑 2：单文件 + @main 报 "cannot be used in a module that contains top-level code"
swiftc -parse-as-library ...

# 坑 3：SCContentFilter(desktopIndependentWindow:) 触发
#      Assertion failed: (did_initialize), function CGS_REQUIRE_INIT
#      → main() 第一行加：_ = NSApplication.shared
```

### 3.2 Python 依赖

全局只有 numpy / Pillow。其余必须装进工作区本地目录：

```bash
python3 -m pip install --target .pydeps <pkg>   # 用户级 pip install 会被沙箱拒绝
PYTHONPATH=.pydeps python3 ...                  # 运行时必须带
```

---

## 4. 关键命令

```bash
# —— 现有工具链（上一轮）——
printf '{"op":"win"}\n{"op":"quit"}\n' | ./scripts/seerd     # 查窗口（游戏没开会返回 no window）
./scripts/seerctl ocr artifacts/game.png 0.3                  # OCR 调试

# —— 内核（Round 3）——
PYTHONPATH=.pydeps python3 -m seerauto doctor                 # 环境自检
PYTHONPATH=.pydeps python3 -m seerauto replay artifacts/      # 离线回放，不需要游戏
PYTHONPATH=.pydeps python3 -m seerauto replay data/replay/    # 真机帧回放
PYTHONPATH=.pydeps python3 -m pytest -q                       # 426 passed, 2 skipped

# —— 真机（Round 4）——
./tools/build_seerdx.sh                                       # 构建原生 SCK 守护进程
PYTHONPATH=.pydeps python3 tools/ctl.py look                  # 单步：只看，不点
PYTHONPATH=.pydeps python3 tools/ctl.py clicktext "确认"       # 单步：按文字点击
PYTHONPATH=.pydeps python3 tools/battle_run.py --battles 3     # 多场连战（真机）
touch /tmp/seerauto.pause                                      # 暂停；删除即恢复

# —— 基准（Round 1）——
python3 bench/bench_all.py
```

---

## 5. 工作纪律（硬性）

### 5.1 性能
1. **不许凭感觉说"更快"**。任何"优化"必须有 before/after 实测（mean/p50/p95，n≥6，写明命令）。
   **并且必须记录当时的 `sysctl -n vm.loadavg` 与核数**——实测 OCR 延迟随负载放大约 4 倍，
   不标负载的延迟数字不可复现、不可比较。
2. **不许让 AI 模型进入高频环路**。常规战斗、日常点击由本地状态机完成。
3. **不许用固定 `sleep` 当主控**。等待"状态变化"，不是等待"时间"。
4. 截图优先走 **常驻 SCStream + 最新帧读取**；`screencapture` 子进程只作兜底。
5. **绝不在主线程上同步等待 SCK 的 async 结果**——会死锁（已实测 13 分钟挂死）。
   用专用线程/队列 + 自己的 run loop，或带超时地 pump run loop。
6. **绝不让全图模板匹配或 OCR 进入轮询路径**（实测全帧模板 63.9 ms、OCR 41.8–146 ms，
   而亚毫秒级指纹/ROI 模板才 0.04–1.0 ms）。
7. **计时前先确认自己量的是真东西**：`CGImageSourceCreateImageAtIndex` 是惰性的（句柄 0.44 ms
   vs 强制解码 36.9 ms）；Swift 数组字面量拷贝会走 CoW 快路径（量出 0.000 ms）。

### 5.2 安全
8. 只做**正常截图 + GUI 输入**。禁止内存注入、服务端篡改、反作弊规避。
9. **输入前必须校验窗口身份与几何**；窗口移动超容差即拒绝点击。
10. 未知状态**不许持续盲点**；必须有超时升级路径。
11. 任务必须有**次数预算 + 时间预算 + 紧急停止**。
12. 截图**未经授权不得上传外部模型**。
13. AI 层不可用时，纯视觉模式必须照常工作。

### 5.3 诚实
14. **区分三种状态**：模拟测试 / 真机测试 / 未执行。文档里必须分别标注。
15. **不许编造**性能数据、测试通过、游戏成功。
16. 找不到的东西写 **NOT FOUND**，不许假定存在。

### 5.4 工程
17. 优先**扩展现有工程**，不许未读代码就新建项目。
18. 不许为架构整洁做早期大重构；要重构先说明理由、小步迁移、保留回滚。
19. 每轮结束更新 `docs/PROGRESS.md`、`docs/PERFORMANCE_LOG.md`、`docs/NEXT_ACTIONS.md`。
20. 关键改动打 git checkpoint。

---

## 5.5 真机纪律（Round 4 踩坑总结，违反会静默出错）

1. **点击前必须确认游戏窗口在最前面。** ScreenCaptureKit 会照常截取**被遮挡**的窗口，
   而 `CGEvent` 点击会落到最前面的那个 App 上 —— 两者不一致就会"看得见、点不中"。
   `tools/ctl.py` 有 `ensure_frontmost()` 守卫，`tools/battle_run.py` 启动时检查一次。
2. **有常驻流时不要用 `shot`。** 实测返回近空白图（约 6 KB），OCR 会静默返回空。
   要 OCR 就把**暖流帧**导出成 PNG（见 `tools/battle_run.py::_ocr_escalate`）。
3. **回合推进靠"日志指纹变化 + 画面稳定"两个条件**，不要靠固定 sleep；
   也不要只看"战斗按钮在不在"—— **胜利遮罩下战斗按钮依然可见**。
4. **同一个窗口不要同时跑两个采集守护进程**，第二个的 `shot` 会失败。
5. **窗口选择要选面积最大的那个**，不能取"第一个匹配"：游戏 App 还拥有 52×20 的辅助窗口。
6. **stage 偏移每次会话都可能变**，必须从画面算，不能写死（`docs/TEMPLATE_CANDIDATES.md` 有实测对比）。


## 5.6 前台焦点、实验性后台输入与用户暂停协议

- 默认前台输入会占用系统焦点；`tools/ctl.py` 的 click/type/key、`tools/battle_run.py` 和 `tools/login.py` 含前台激活/前台输入路径。用户要同时使用 Mac 时，不得擅自调用这些路径。
- 已有实验性无焦点通道：`LiveSession(background=True)` → `clickpid`/`movepid` → `CGEvent.postToPid`。CLI 可通过 `goal start --background` 或 `tools/supervise.py --background` opt-in。它不应移动真实鼠标，也不应调用 `activate`。
- **不要把实验通道当成可用事实。** `docs/DELIVERY_2026_11_04.md` 的真机结论是：PID 定向事件对原生窗口有效，但对本客户端的 Flash/PepperFlash 游戏画布无效。必须先做可逆、无消耗的后台点击探针，同时证明游戏状态变化、frontmost App 不变、真实鼠标坐标不变，才能标记 `BACKGROUND_INPUT_LIVE_PASS`。
- 探针失败时标记 `BACKGROUND_INPUT_BLOCKED`；不允许自动退回全局 HID/前台点击，不允许私有 API、内存注入或反作弊规避。改用暂停协议或只做不需要 GUI 输入的研究/开发。
- 后台模式遇到登录/验证码时不调用 `tools/login.py`；它会激活客户端。不得为了后台运行而自动抢焦点。
- 用户说 **“暂停，保存”**：DSH 停止派发所有新任务/子 Agent，写入 `/tmp/seerauto.master.pause`，并执行 `PYTHONPATH=.pydeps python3 -m seerauto goal pause`。Runner 在下一个合作式安全点做只读重观察、写 SQLite checkpoint、更新为 `paused`，保持执行器锁并等待；不要 kill 进程。
- 用户说 **“开始”**：清除 `/tmp/seerauto.master.pause`，执行 `PYTHONPATH=.pydeps python3 -m seerauto goal resume --allow-input --planner <原planner>`。后台模式默认从该 run 保存的配置恢复。恢复后必须重新观察游戏场景、窗口几何、战斗和资源，再执行下一步。
- 暂停只冻结 Agent 的输入与任务派发；**不保证冻结游戏服务器或游戏内计时**。恢复必须重观察，不得沿用暂停前的旧场景假设。

## 6. 当前阻塞项 / 已知缺口

| # | 项 | 状态 |
|---|---|---|
| B1 | 游戏客户端 | ✅ 已解除（用户 2026-11-01 23:35 启动并授权真机自动化） |
| B2 | 坐标系 | ✅ 已按 1200×800 重新标定，见 `docs/state.md` |
| B3 | 交易行门槛 | ✅ 已查明是**击败 SPT 首领**（面板原文「击败后解锁交易行资格」），非时间门槛 |
| G1 | **失败（Defeat）分支** | ❌ **没有任何样本**：`artifacts/` 没有、40 分钟视频里作者一次没输、真机 11 场全胜。规则保持禁用 |
| G2 | 暂停/恢复/停止 | ✅ 真机验证通过（控制文件暂停、SIGINT 停止、异常自动停机） |
| G3 | 异常场景 | ✅ 已验证"点击不生效"→ 带原因安全停止；❌ 掉线/卡死未构造 |
| G4 | 四技能选择 | ❌ 坐标已标定但未逐个点击验证；当前只用「战斗」普攻 |
| G5 | 长时稳定性 | ❌ 未测（最长单次约 8 分钟，`run 19745e9a764d`） |
| G6 | **底栏图标位置随场景漂移** | ❌ Round 5 实测：太空站 vs 林间相差约 100px，固定坐标不可用，须模板定位 |
| G7 | **纯文字弹窗**（「恭喜你得到了 N 个积累经验」） | ⚠️ 无模板，只能 OCR 兜底；会吞掉整轮预算 |
| G8 | 谱尼入口 | ✅ SPT→太空站左翼→谱尼 可传送（2026-11-03 实测）；内部规则未探索 |

> ⚠️ **服务器公告**：登录页「系统公告」明确写「本服不允许使用第三方软件、脚本或变速工具」。
> 真机自动化已获用户明确授权（风险自担），但这条外部约束必须始终记录在案。

---

## 7. 双模式的边界（架构强制）

```
截图层（SCStream 常驻）→ 视觉层（模板/ROI/OCR兜底）→ 状态机 → 执行器 → 游戏
                                                              │
                                        ┌─────────────────────┴──────────────┐
                                        ▼                                    ▼
                                 模式 A：本地 GUI/CLI              模式 B：Agent 接口
                                 （无 LLM，默认路径）              （CLI / MCP / DSH 插件）
```

- 内核 (`seerauto/`) **禁止** import 任何 LLM/Agent SDK。
- 模式 B **只能**通过语义接口调用已验证能力，例如：
  `game.get_status` / `game.list_tasks` / `game.start_task` / `game.pause_task` / `game.stop_task` / `game.get_progress`
- Agent **不得绕过**内核安全检查去任意点击未知区域。
