# PROJECT_AUDIT — 《赛尔号 Xin 计划》macOS 自动化工程接管审计

> 审计时间：2026-11-01 16:21–16:5x（Asia/Shanghai）
> 审计执行：DSH Agent Team Lead
> 审计方式：直接检查文件系统 / 编译探针 / 实际计时，不依赖上一轮 Agent 的自述
> 上一轮工作时间：2026-10-31 17:20–18:45

---

## 0. 一句话结论

**上一轮的工程是真实存在且有价值的，不是失败品。** 它已经解决了最难的一层——macOS 上"能截图、能识别、能点击、能改窗口几何"这件事已经跑通了，并且做了 400+ 帧的视频拆解。
真正的问题不是"没有能力"，而是**每一帧都在付三重固定开销**（子进程截图 139ms + 全图 Vision OCR 469ms + 盲目 sleep），并且**上一个 macOS 版本让旧的快速截图 API 彻底失效了**——这是本轮最重要的技术发现。

---

## 1. 环境事实（已验证，非推断）

| 项目 | 实测值 | 验证方式 |
|---|---|---|
| macOS | 15.7.7（Build 24G720） | `sw_vers` |
| CPU / 架构 | Apple M2 / arm64 | `uname -m`、`sysctl` |
| 显示器 | 2940×1912 px（2x Retina，逻辑 1470×956） | 全屏截图实测尺寸 |
| 游戏窗口 | 1149×714 pt → 抓取 2298×1428 px（scale = 2.0） | `docs/state.md` + `artifacts/game.png` 实测 |
| Python | 3.10.13（pyenv shim） | `python3 -V` |
| Swift | 6.2.3、`swiftc` 可用（Xcode CLT） | `swiftc --version` |
| Node.js | v25.6.1 | `node -v` |
| 工作目录 | `/Users/ceontou/Documents/code/seer-puni-quest` | — |
| GitHub / Gitee 上游 | **无**（本工程从未初始化过 git） | `git status` → `not a git repository` |

### 1.1 依赖现状（逐项实测 import）

| 包 | 状态 |
|---|---|
| numpy | ✅ 可用（全局） |
| Pillow (PIL) | ✅ 可用（全局） |
| opencv-python-headless | ⚠️ 全局**不可用**；已装入 `./.pydeps`（cv2 5.0.0），需 `PYTHONPATH=.pydeps` |
| mss | ❌ MISSING |
| PyObjC（objc / Quartz / Vision） | ❌ MISSING |
| PySide6 | ❌ MISSING |
| paddleocr / pytesseract | ❌ MISSING |
| pytest | ❌ 全局 MISSING（可由 `.pydeps` 注入） |

> **沙箱限制（重要）**：`python3 -m pip install <pkg>` 写入用户 site-packages 会被拒绝
> （`OSError: Operation not permitted: '/Users/ceontou/.local/lib'`）。
> 唯一可行路径：`python3 -m pip install --target .pydeps <pkg>`，并以 `PYTHONPATH=.pydeps` 运行。
> 这直接影响技术选型：**任何依赖 PyObjC 的 Python 原生截图方案在本机当前环境下都不可用。**

---

## 2. ⚠️ 本轮最大技术发现：旧截图 API 在 macOS 15 已被彻底移除

用真实编译探针验证（不是查文档猜测）：

```
CGDisplayCreateImage(_:rect:)   → error: unavailable in macOS: Please use ScreenCaptureKit instead.
                                  note: obsoleted in macOS 15.0
CGWindowListCreateImage(...)    → error: unavailable in macOS: Please use ScreenCaptureKit instead.
                                  note: obsoleted in macOS 15.0
```

**含义（对本工程是决定性的）：**

1. 网上与各类报告里常见的 macOS 快速截图方案（Quartz `CGWindowListCreateImage`、`CGDisplayCreateImage`）**在本机 macOS 15.7.7 上根本无法编译**，不是"慢"，是"不存在"。
2. 任何"用 PyObjC 调 Quartz 截图"的 Python 路线同样不可行——PyObjC 既未安装，底层符号也已删除。
3. **ScreenCaptureKit（SCK）是唯一可行的原生高速截图路径。** 这是架构硬约束，不是偏好。
4. 这同时解释了为什么上一轮 Agent 只能退而使用 `/usr/sbin/screencapture` 子进程——它是当时唯一"一定能用"的方案，但代价是每次约 139ms 的进程启动开销。

> **交叉核对后的更正（重要，勿沿用旧结论）**：
> 我方最初的假设是"研究报告会推荐 Quartz/CGWindowListCreateImage 作为主截图路径"，**这个假设是错的**。
> 逐条核对 `docs/_RESEARCH_DIGEST.md` 后确认：**报告本来就推荐 ScreenCaptureKit**，并明确写了"旧 CoreGraphics capture 不应成为长期路径"。
> 也就是说，**报告在截图选型上与本轮实测方向一致，不应被"证伪"**。
>
> 真正被实测证伪的是报告里另外几条**承重假设**（详见 `docs/_RESEARCH_DIGEST.md` §2）：
> - **PyObjC 作为 Python→macOS 通道**：本机 `objc`/`Quartz`/`AppKit` 全部 import 失败，用户级 pip 被沙箱阻断 —— CONTRADICTED
> - **Python 3.12/3.13**：本机为 3.10.13，仓库自己的 `pyproject.toml` 也写 `>=3.10` —— CONTRADICTED
> - **最低 macOS 13**：`SCScreenshotManager` 实为 `macos(14.0)`，`captureImage(in:)` 为 **15.2**，只有 `SCStream` 是 12.3 —— CONTRADICTED
> - **"每个动作后都做视觉验证"**：没算过成本账；实测一次"看"需要 0.65–3.5 s，比它想取代的 `sleep(1.35)` 还长 —— CONTRADICTED
> - **验收标准"300 帧无异常"**：只证明不崩溃，不含任何延迟/帧率阈值，而计划明确要求了帧率控制 —— CONTRADICTED
> - **Harness 版本 `0.2.2-alpha.2` / Node>=24.9**：实测已安装为 `0.2.1-rc.5`，无核心包声明 `engines.node` —— CONTRADICTED
>
> 而报告**最致命的问题不是选错技术，是没有任何延迟模型**：422 行里零个延迟数据点，
> 却在设计一个"抓帧→判断→动作后再验证"的闭环。下游所有超时（5s/10s）、预算、lease TTL 都是凭空定的。

---

## 3. 资产清单

### 3.1 存在且可用的资产

| 类别 | 路径 | 规模 / 说明 |
|---|---|---|
| 游戏客户端 | `/Applications/SeerGame.app` | 存在（Electron + PepperFlash），二进制名 `SeerGame` |
| 客户端数据目录 | `~/Library/Application Support/seer-game` | 存在 |
| 客户端逆向产物 | `artifacts/unpacked/dist/` | 已解包 Electron dist，含 `config/Config.js`、`utils/socket.js` 等，并已做初步 deob |
| 游戏录制视频 | `artifacts/video/BV1Fu8h6YEAz.mp4` | 196MB，1280×720@30fps，**2447.94s = 40:48**，《三天获取普尼》 |
| 视频抽帧 | `frames/`（10 个章节目录） | **417 帧**，含 `_ocr.json` |
| 视频拆解文档 | `docs/plan-from-video.md` | 35,274 B，带时间戳的逐帧 OCR 拆解（上一轮成果，质量较高） |
| 行动清单 | `docs/checklist.md` | 8,435 B |
| 账号与坐标系 | `docs/state.md` | 含 20+ 个已验证 UI 坐标、货币、精灵状态 |
| 操作日志 | `docs/action-log.md` | 36 条带时间的真实操作记录 |
| 进度快照 | `docs/progress.md` | 推断到 18:45 的状态 |
| 真实游戏截图 | `artifacts/*.png` | ~85 张，含 `game.png`(2298×1428)、`battle.png`、`scene_0..11.png`、`scene_idx_0..11.png`、商店 12 页、地图面板 |
| 场景索引 | `artifacts/scene_index.json`、`artifacts/shop_pages.json` | 地图 12 格与商品页的结构化 OCR 结果 |
| 驱动源码 | `scripts/seerd.swift` + 已编译 `scripts/seerd` | 常驻 JSON-line 守护进程（截图/OCR/点击/按键/改窗口） |
| 控制源码 | `scripts/seerctl.swift` + 已编译 `scripts/seerctl` | 一次性 CLI 版本 |
| Python 驱动 | `scripts/fast.py` | `Driver` 类，走 seerd 管道 |
| 战斗脚本 | `scripts/tower.py`、`scripts/farm_tower.py` | 试炼之塔刷层循环（**核心性能问题所在**） |
| 辅助脚本 | `scripts/play.py`、`scan.py`、`scene_index.py`、`shoot.sh`、`dump_ocr.py`、`hover_*.py`、`deob.js` | 侦察工具 |

### 3.2 NOT FOUND（明确不存在，不得假定其存在）

| 缺失项 | 说明 |
|---|---|
| **Git 仓库** | 审计开始时 `seer-puni-quest` 不是 git 仓库，无任何提交历史。已由本轮建立首个 checkpoint。 |
| **DSH 游戏插件** | `docs/action-log.md` 18:03 提到"学习 dsh-game-daily 的工具思路"，但全盘搜索 `~/.dsh/`（仅 `ultramath` 技能）、`~` 下 `*game-daily*`、`*dsh-game*` 均为 **NOT FOUND**。该工具思路无法复用，只能参考其日志中的结论。 |
| **任何测试套件** | 无 `tests/`、无 pytest 配置、无 CI。全部验证均为人工现场操作。 |
| **依赖清单** | 无 `requirements.txt` / `pyproject.toml` / `package.json`。 |
| **性能测量数据** | 无任何 benchmark 结果文件、无分阶段计时记录。`docs/action-log.md` 中"单击 78ms、截图 138ms、OCR 240ms"是**单次手动观感值，无样本量、无 P50/P95，且本轮复测 OCR 实测为 469ms**（见 §4）。 |
| **真机自动化成功记录** | 无"连续 N 场战斗全部成功"的记录。日志最后三条是失败：`18:41 [f0] floor cleared`、`18:42 [f1] cannot engage - stop`、`18:45 [f0] cannot engage - stop`。 |
| **AI Agent 接入层** | 无 MCP server、无本地任务服务、无 Harness 插件。纯视觉内核与 AI 层**都还没有**。 |
| **视频以外的真实游戏资料** | 无录屏、无音效资源、无客户端资源解包以外的东西。 |

### 3.3 当前运行状态

| 检查 | 结果 |
|---|---|
| 游戏客户端是否运行 | **否**。`echo '{"op":"win"}' \| ./scripts/seerd` 返回 `{"error":"no window"}` |
| 佐证方式 | `pgrep -fl SeerGame` → 被沙箱拦截；`osascript System Events` → `-10004 权限违例`。故以 seerd 的窗口枚举为权威判断。 |
| 屏幕录制权限 | **已授予**（`screencapture` 能正常输出 2940×1912 PNG） |
| 辅助功能权限 | **历史已授予**（`docs/action-log.md` 17:26 确认 `AXIsProcessTrusted=true`），本轮未复核 |
| 能否自动化点击 | 未复核（无窗口）。高风险，需 Round 4 在真机重新确认 |

---

## 4. 性能基线（本轮首次真实测量）

所有数字均为**本机实测**，命令与样本量可复现。

| 测量项 | n | mean | p50 | p95 | min | 备注 |
|---|---|---|---|---|---|---|
| `screencapture -x -R 300,120,1149,714 out.png` | 8 | **139.4 ms** | 142.9 | 168.4 | 110.5 | **= `seerd` 每次截图的真实路径** |
| `screencapture -x out.png`（全屏） | 8 | **185.3 ms** | 184.0 | 196.2 | 176.5 | `play.py` 走的是这条更慢的路 |
| `screencapture -x -o`（去阴影全屏） | 8 | 173.5 ms | 176.3 | 177.7 | 164.2 | — |
| `./scripts/seerctl ocr artifacts/game.png 0.3` | 6 | **468.9 ms** | 455.9 | 516.6 | — | Vision `.accurate`，含进程启动，12 个文本框 |
| `./scripts/seerctl ocr artifacts/battle.png 0.3` | 6 | 455.6 ms | 468.7 | 472.9 | — | 14 个文本框 |

### 4.1 由此推出的闭环成本

上一轮 `fast.py` 的 `Driver.capture()` = `shot()` + `win()` + `ocr()`：

- 截图（子进程）：~139 ms
- `win` 二次 round-trip：~1–2 ms
- 全图 OCR（进程内 seerd，Vision `.accurate`）：**≥ 240 ms，实测含子进程为 469 ms**
- **一次"看一眼"的固定成本：约 380–610 ms**

而 `tower.py` 的回合循环里，**每一回合至少调用一次 `capture()`，外加两处盲等**：

```python
d.click_local(*FIGHT_BTN); time.sleep(0.25)
d.click_local(*SKILL_POS); time.sleep(1.35)     # ← 盲等 1.35s，占整个回合的主导成本
st = battle_state(d)                             # ← 又一次 380–610ms 的看
```

**结论：单回合实际耗时约 2.0–2.2 s，其中真正必要的图像处理不到 600ms，另外 1.6 s 是固定 sleep。** 也就是说，**当前瓶颈的第一名不是 OCR，而是"用固定延时代替状态检测"**，第二名才是全图 OCR，第三名是子进程截图。

> 这三个问题各自独立可解，且互不冲突。Round 1 的实测矩阵详见 `docs/PERFORMANCE_BASELINE.md`。

---

## 5. 现有代码可运行性判定

| 文件 | 能否直接运行 | 判定依据 |
|---|---|---|
| `scripts/seerd`（已编译二进制） | ✅ 能启动，管道协议正常 | 实测返回 `{"error":"no window"}`（协议正确，只是没窗口） |
| `scripts/seerctl` | ✅ 能运行 | 实测 OCR 成功输出 12/14 个文本框 |
| `scripts/fast.py` | ⚠️ 依赖游戏窗口 | 需 `pin` 窗口；游戏未运行则失败 |
| `scripts/tower.py` / `farm_tower.py` | ❌ 当前不可运行 | 依赖 `fast.Driver`，且需要游戏窗口 + 已在塔内 |
| `scripts/play.py` | ⚠️ 慢但可用 | 全屏截图 + PIL 裁剪 + 子进程 OCR，是最慢的一版 |
| `scripts/scene_index.py` / `scan.py` | ⚠️ 需窗口 | — |
| `scripts/seerd.swift` / `seerctl.swift` | ✅ 可重新编译 | 但需 `swiftc -module-cache-path /tmp/...`，默认 ModuleCache 被沙箱拒绝 |

> **重编译注意**：本沙箱下 `swiftc` 默认会失败：
> `unable to open output file '.../ModuleCache/...': Operation not permitted`。
> 必须显式传 `-module-cache-path /tmp/<dir>/mc`。

---

## 6. 与研究报告的关系

`深度研究报告.md`（32KB，422 行）是上一轮产出的**架构参考**，不是实验记录。其定位已确认：

- 可用于：模块划分思路、双模式产品设计、状态机概念、Agent 接口命名、**ScreenCaptureKit 选型**（方向正确）。
- **不可直接用于**：任何具体延迟数字（它一个都没有）、PyObjC/Python 3.12/macOS 13 等技术前提（均被本机证伪）、
  以及关于 DSH 插件版本号的断言（报告写 `0.2.2-alpha.2`，实测为 `0.2.1-rc.5`）。
- **已核实为真的部分**：DSH 插件 API 形状（`export const name/inject/apply` + `defineTool`）、
  `tools/pre-execute`/`ctx.tools.guard`/`tools/post-execute` 实际存在、MCP `2026-07-28` 协议号、MIT 许可证。
- **不存在的部分**：报告声称的 12 份配套文档 `00_…`–`11_…` 在仓库中 **NOT FOUND**（链接指向不存在的 `sandbox:/mnt/data/…`）。

逐条交叉核对（20 条承重结论：8 SUPPORTED / 7 CONTRADICTED / 5 UNVERIFIED）见 `docs/_RESEARCH_DIGEST.md`（由 research-digester 产出）。

---

## 7. 复用结论（本项目不新建工程的原因）

**直接复用、不动：**

1. `docs/state.md` 的 20+ 个已验证 UI 坐标 —— 这是真金白银的现场标定成果。
2. `artifacts/` 的 ~85 张真实截图 —— 是离线 Replay 测试集的唯一来源，也是模板匹配的素材。
3. `scripts/seerd.swift` 的**协议设计与窗口管理逻辑**（`findWin` / `pinWindow` / `clicklocal` / Retina 换算）—— 这部分是对的，只是**截图后端要换掉**。
4. `docs/plan-from-video.md` 的 417 帧拆解 —— Round 2 基本不用重做。
5. `artifacts/unpacked/dist/` 的客户端逆向 —— 后续理解本地服务端口/协议时可查。

**保留但改造：**

6. `scripts/seerd.swift` 的 `shot()` —— 子进程 `screencapture` 换成进程内 ScreenCaptureKit。
7. `scripts/fast.py` 的 `Driver` —— 保留为兼容层，新内核 `seerauto/` 与它并存。

**替换：**

8. `tower.py` / `farm_tower.py` 的回合循环 —— 盲 sleep → 状态变化驱动。

**不丢弃但暂不使用：**

9. `play.py`（最慢路径，仅作对照基线）、`seerctl`（保留为调试工具）。

---

## 8. 阻塞项（本轮无法推进的）

| # | 阻塞项 | 影响 | 需要的动作 |
|---|---|---|---|
| B1 | **游戏客户端未运行** | 所有真机测试（Round 1 的窗口级截图、Round 4 的完整闭环）无法执行 | 用户启动 `/Applications/SeerGame.app` 并登录（hello / 188365991） |
| B2 | **无实时游戏画面** | 模板库无法新增，只能用昨日截图 | 同 B1 |
| B3 | **辅助功能/输入授权未复核** | 自动点击能否生效未知 | 同 B1 后立即验证 |
| B4 | pip 用户级安装被沙箱拒绝 | 依赖必须放 `.pydeps` | 可绕过，不阻塞 |
| B5 | `System Events` / `pgrep` 被沙箱拒绝 | 无法用 AppleScript 兜底 | 已改用 CGWindowList / seerd 替代，不阻塞 |
| B6 | 交易行未开通、赛尔豆为 0 | 视频路线的第一前提不成立，普尼任务无法端到端推进 | 需要真实游戏内长期推进，非本轮可解 |

> **B1–B3 是可解的用户侧动作，不是工程缺陷。** 在缺失它们的情况下，本轮的策略是：
> 用 `artifacts/` 的真实截图做 **Replay 测试**、用**非侵入式**手段做**捕获/识别性能测量**，
> 并把所有"未在真机验证"的地方显式标注。绝不把模拟测试报告为真机测试。

---

## 9. 本轮已执行的接管动作

| 动作 | 结果 |
|---|---|
| 建立 git 仓库并打首个 checkpoint | `4ed6ff9 checkpoint: import existing seer-puni-quest workspace (pre-takeover state)`，116 个受版本控制文件，`.git` 97MB |
| `.gitignore` 排除 196MB 视频、417 帧、Swift build cache、`.pydeps` | 避免仓库膨胀（曾误纳 214MB build cache，已 amend + gc 清理） |
| 建立共享任务板 | 4 个任务：`task-1` 基准测量、`task-2` 内核、`task-3` 视频结构化、`task-4` 报告交叉核对 |
| 启动 4 个并行 Agent | bench-engineer / core-engineer / video-analyst / research-digester |
| 编译探针验证旧截图 API | 确认 macOS 15 已移除，SCK 为唯一路径 |
| 建立性能基线 | 见 §4 |

**未执行**：任何对游戏窗口的真实输入（游戏未运行，且无授权窗口可点）。
