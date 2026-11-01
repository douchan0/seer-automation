# 深度研究报告 证据分级审计（Research Digest）

**被审对象**：`深度研究报告.md`（32KB / 422 行，`## Executive Summary` … `## 最终必须回答的问题`）
**需求基线**：`深度研究计划.md`（该报告原本要回答的问题）
**审计方式**：全文通读 + 对报告中每一条"承重"结论逐条对照本机实测 + 核对本机 SDK 头文件 / 已安装 DeepSeek Harness 运行时 / 现有仓库代码。
**审计日期**：2026-11-01
**裁决计数**：**8 SUPPORTED / 7 CONTRADICTED / 5 UNVERIFIED**（共 20 条承重结论，见 §2）

> **与 `docs/PROJECT_AUDIT.md` 的对齐说明（重要）**：本文件写完后才发现项目里已有 Round 0 审计。已逐条对齐，无冲突，但有两处必须修正外部预期：
> 1. **报告并不推荐 `screencapture` 子进程或 Quartz `CGWindowListCreateImage` 作为主截图路径——它推荐的是 ScreenCaptureKit，并且明确写了"旧 CoreGraphics capture 不应成为长期路径"。** 也就是说，在"主截图路径"这件事上报告与 `PROJECT_AUDIT.md` 的结论**是一致的**，不是被证伪的那一方。本轮真正的证伪对象是别的东西（见 §2-4/5/6/7/8）。
> 2. `PROJECT_AUDIT.md` 的 139.4ms / 185.3ms / 468.9ms **很可能是在机器空闲时**测得的（该文件未记录 loadavg）；我在负载 43–68 下复测同一命令得到 315–384ms / 550ms / 2.6s。两组数字都不假，差的是负载。§0 说明这一点，§9 给出复现方式。

一句话结论：**报告的"大方向"（ScreenCaptureKit 主路径 + Python 视觉核心 + Swift 系统层 + Agent 只做壳）是对的，而且现在第一次有了实测支持；但它的"小方向"（Python 3.12/3.13、PyObjC、PySide6、macOS 13 起步、逐动作后置验证）在成本模型和本机环境上是错的，最致命的是它通篇没有一个延迟预算。**

---

## 0. 先读这一节：本次实测的方法与可信度边界

报告里没有任何延迟数字（见 §3），所以本次审计补做了一组测量。**这些测量不是纯净基准**，必须先说清楚它们的水位：

| 事实 | 数值 | 含义 |
|---|---|---|
| 本机 load average（测量期间，8 核） | **68.21 / 65.13 / 43.07** | 严重超订（8.5×）。所有重 CPU/GPU 的测量都被拖慢 |
| `/usr/bin/true` 进程启动成本 | 4.5–8.6ms | 排除"是我的 shell/沙箱把启动拖慢了"这一解释 |
| `python3 -c pass` 启动成本 | ~29.8ms | 同上，进程派生本身是正常的 |

所以：**本文件里的绝对毫秒数只在"当时负载"下成立，不能当作产品基线。** 我在下文中对每个数字都标注了它的测量条件。团队此前记录的 139.4 / 185.3 / 468.9ms 大概率是**空闲机**数字（原文档未记 loadavg，这本身就是个方法缺口）；我在负载 43–68 下复现同一命令，得到的是它们的 2.3–5.5 倍。

**这不削弱报告的结论方向，反而加强了它**：真正要命的不是"139ms 还是 384ms"，而是**同一路径在不同负载下会差 2–5 倍**——任何依赖固定延时的设计（比如 `scripts/tower.py` 里那些 `time.sleep(1.35)`）都必然时不时对着一张过期帧做决策。

复现命令见 §9。

---

## 1. 报告给出的架构建议（压缩版，附报告自评置信度与出处）

| # | 建议 | 报告自评置信度 | 出处 |
|---|---|---|---|
| A1 | 产品形态 = macOS 平台层重写，而非移植 BetterGI | 强（"结论是"） | `## Executive Summary` L5 |
| A2 | **混合架构**：Python 做 CV/OCR/FSM/任务/adapter，小 Swift helper 做 ScreenCaptureKit/AX/CGEvent/TCC，PySide6 做首版 GUI；长期仍用混合 | 强（决策矩阵 4.6 分，"MVP 首选…长期仍首选"） | Exec L7；`### 技术决策矩阵` L93–111 |
| A3 | **ScreenCaptureKit 为正式截图层**，明确"旧 CoreGraphics capture 不应成为长期路径" | 强 | Exec L7；L111；`## 最终必须回答的问题` 二 L374 |
| A4 | 最低 macOS **13 Ventura**（SCK 技术底线 12.3，但 12.x 不作首发承诺） | 强（"推荐首发最低"） | Exec L7；`## 最终必须回答的问题` 十三 L407 |
| A5 | **AI 不进闭环**：模式 A 无任何 LLM/外部 API 也能跑；模式 B 只是加一层壳 | 强（加粗结论） | Exec L9；`### 推荐共享核心架构` L89 |
| A6 | 状态机 + 证据驱动 + 动作后置视觉验证 + 预算/重试 | 强（给出逐状态表） | `### 状态机与视觉策略` L161–177 |
| A7 | 传统 CV 优先，ONNX 只在 replay dataset 证明不够用时才上 | 中强（"应坚持"） | L177 |
| A8 | OCR 后端可抽象（Vision `VNRecognizeTextRequest` fast/accurate vs Paddle/ONNX），按同一组 ROI benchmark 再选 | 中（"应…而不是先验决定"） | L179 |
| A9 | 稳定核心 + 可版本化 Server Adapter Pack（`manifest.yaml` / layouts / templates / lexicon） | 中（设计提案） | `### 多私服扩展机制` L113–157 |
| A10 | 唯一 Task Service + `WindowLease`；GUI/CLI/MCP/Harness 都只是调用端，任何 Agent 不能直呼 CGEvent | 强 | L91、L302 |
| A11 | Harness 插件只做薄壳：`apply(ctx)` + `inject=['tools']` + `ctx.tools.register(defineTool(...))`，放在 `compat/<version>.ts` 做版本隔离 | 强（"已验证事实"） | Exec L9；`## Agent、MCP…` L215–248 |
| A12 | MCP 作为第二接口，**stdio 优先**，只有多客户端时才开 `127.0.0.1` Streamable HTTP + 随机 bearer token | 强 | L250 |
| A13 | 首个功能 = 用户手工进入固定 PVE 后的"重复战斗闭环"，不做地图导航 | 强 | `### 第一项自动化功能` L310–314 |
| A14 | 首版不发明 DSL，用版本化 YAML/JSON TaskPlan，Agent 只能引用已注册 `use:` capability | 中强 | `### 脚本格式` L183–211 |
| A15 | 客户端容器未知 → **Client Probe 是 P0**，必须实测 Ruffle / AIR / projector / Wine / 浏览器 | 强（"必须成为 P0，而不是隐藏在开发后期"） | `### Flash 客户端必须完成的十项验证` L44–50 |
| A16 | 许可证：BetterGI GPL-3.0（clean-room 借鉴）、DSH MIT、Ruffle MIT/Apache-2.0、`xin_tool` 无许可证 | 中 | `### 打包与发布` L366 |

> 报告的自评置信度写作风格值得肯定：它自己在 `### 已验证事实、源码推导与待验验证项` L34–42 里就分了"已验证事实 / 源码推导 / 待验证"三档，`### 技术决策矩阵` L95 也明说分值是"工程判断"。**但三档里被标成"已验证事实"的内容，有相当一部分其实只是搜索结果摘要**——见 §8。

---

## 2. 裁决表（逐条）

| # | 承重结论 | 裁决 | 依据 |
|---|---|---|---|
| 1 | A3 ScreenCaptureKit 作主截图路径 | **SUPPORTED BY OUR MEASUREMENT** | 同负载交叉测量：SCK `SCScreenshotManager.captureImage` 内部耗时 **179.6ms**（169–192，离散度 13%）vs `screencapture` 子进程 **383.7ms**（280–548，离散度 96%），**2.1×**，且输出正好 2298×1428。与 `PROJECT_AUDIT.md` 的结论一致（SCK 是唯一活路）。**注意：这是交叉核对，不是权威基准** —— 权威基准是 `native/bench_capture.swift`（1686 行，覆盖 SCScreenshotManager + SCStream，Round 1 在建） |
| 2 | A3 后半句"旧 CoreGraphics / screencapture 不应成为长期路径" | **SUPPORTED BY OUR MEASUREMENT** | 结论对，而且比报告自己说的更硬：`PROJECT_AUDIT.md` 已证实 `CGWindowListCreateImage` / `CGDisplayCreateImage` 在 macOS 15 **编译即失败**（"unavailable in macOS: Please use ScreenCaptureKit instead"）——不是慢，是不存在。我补的第二条理由是抖动：同负载下 screencapture 离散度 96% vs SCK 13%。报告给的理由（"Apple 已 deprecated"）是四条里最弱的，但它没测过 |
| 3 | A2/A10 进程边界：Core 持 lease 与输入权，Swift 只做系统调用 | **SUPPORTED BY OUR MEASUREMENT** | 冷启 CLI 跑 SCK：wall **713ms** vs 内部 **179.6ms** → 每次调用的枚举+启动约 **530ms** 纯浪费。只有常驻 helper 才拿得到 180ms。报告的边界设计正好成立，但它没写"必须常驻"这个前提（见 §7-G2） |
| 4 | A2 隐含前提：Python 侧能用 PyObjC 直连 macOS API（`### 技术决策矩阵` 把 "Python + PyObjC/PySide" 当作一个可选列并给 4.0 分） | **CONTRADICTED BY OUR MEASUREMENT** | 本机 `objc` / `Quartz` / `AppKit` / `Foundation` **全部 import 失败**；PySide6、PyQt6、mss 同样缺失；用户级 `pip install` 被沙箱阻断（只能 `--target .pydeps`）。矩阵里那一列的"部署条件"分数是凭 PyObjC 官网资料打的，不是本机事实 |
| 5 | A2 指定 **Python 3.12/3.13**（`## 最终必须回答的问题` 二 L374） | **CONTRADICTED BY OUR MEASUREMENT** | 本机 `python3` = **3.10.13**（pyenv），另有 homebrew 3.14（空环境）；**没有 3.12/3.13**。且仓库自己的 `pyproject.toml` 写的是 `requires-python = ">=3.10"`——**报告与项目既有事实直接冲突** |
| 6 | A4 最低 macOS **13 Ventura** | **CONTRADICTED BY OUR MEASUREMENT**（SDK 头文件） | `SCScreenshotManager` = `API_AVAILABLE(macos(14.0))`；`captureImageWithFilter:` = 14.0；最干净的 ROI 抓图 `captureImageInRect:` = **15.2**。只有 `SCStream` 才是 12.3。报告给的状态机是"抓一帧→判断→动作→再抓一帧"，这条路在 13 上**不存在**。门槛应为 14.0（用 `captureImage(in:)` 则 15.2）；本机 15.7.7 无所谓，但"13 起步"是错的承诺 |
| 7 | A6 "动作后视觉验证"作为每一步的必选项（`### 状态机与视觉策略` L170：BATTLE_TURN → `动作后视觉验证`） | **CONTRADICTED BY OUR MEASUREMENT**（成本模型缺失） | 一次验证 = 抓帧 + 识别。实测 179–384ms（抓帧）+ 469ms～2.6s（整帧 Vision OCR）= **0.65–3.0s 一次验证**。报告从没算过这笔账，也没给任何一步设"验证必须在 X ms 内完成否则降级"的规则。在负载 68 时，一次验证比 `tower.py` 的 `sleep(1.35)` 还长 |
| 8 | A13/T05 验收标准 "**300 帧无异常**"（`### 首批十个 Coding Agent 任务` L324；另 L48 要求"连续捕获 300 帧"） | **CONTRADICTED**（作为验收标准无效） | 计划 `### 1. 截图引擎` 明确要求"**帧率控制**"和"**截图延迟优化**"，报告交付的验收标准里**没有任何延迟/帧率阈值**——"300 帧无异常"只证明不崩溃。而 300 帧在 140–630ms/帧下要 42–189 秒，这个数字本身就不是性能指标 |
| 9 | A11 Harness 插件 API 形状（`export const name/inject/apply` + `ctx.tools.register(defineTool())`） | **SUPPORTED BY OUR MEASUREMENT**（对着已安装运行时核对） | 已安装运行时里真实插件 `dsh-tool-todo` 就是 `export const name = 'tool-todo'; export const inject = ['tools','sessionProjections']; export function apply(ctx, config)`；`@deepseek-ai/dsh-tools` 导出 `defineTool`，README 示例的 `parameters` / `output.schema` / `output.render` 与报告代码块**逐字对得上**；`tools/pre-execute`、`ctx.tools.guard`、`tools/execute`、`tools/post-execute` **全部真实存在**。这条报告写得对，且能用 |
| 10 | A11 "当前根 `package.json` 显示 **0.2.2-alpha.2** 且要求 **Node >=24.9.0**" | **CONTRADICTED BY OUR MEASUREMENT** | 已安装包：`app/package.json` = `@deepseek-ai/dsh-desktop` **0.2.1-rc.5**；`dsh/package.json` = `@deepseek-ai/dsh-desktop-runtime` **0.2.1-rc.5**；**没有任何核心包声明 `engines.node`**（360 个包中仅 3 个有，分别是 `>=18` / `>=20`）；本机 node = **v25.6.1**。结论（必须锁版本 + compat 层）仍然成立，但它给的版本号是错的，照它写 `compat/0.2.2-alpha.2.ts` 会挂错名字 |
| 11 | A12 MCP：stdio 优先、可回退 Streamable HTTP、规范版本 2026-07-28 | **SUPPORTED BY OUR MEASUREMENT**（部分） | 已安装 `@deepseek-ai/dsh-mcp-client` 真实存在，其文档原文："the official SDK selects the **2026-07-28** protocol when available and falls back to supported legacy revisions"、"本地程序使用 stdio，远端…"。规范号与传输选择都对得上。但报告转述的"规范要求服务器做输入验证/限流/审计"是规范条文，本机**无法验证**（无网络） |
| 12 | A16 "DeepSeek Harness 是 MIT" | **SUPPORTED BY OUR MEASUREMENT** | 已安装 `package.json` `"license": "MIT"`，`dsh-tools/LICENSE` 存在 |
| 13 | A16 其余许可证（BetterGI GPL-3.0 / Ruffle MIT+Apache-2.0 / `xin_tool` 无） | **UNVERIFIED** | 本会话无网络（`web_search` 无 API key），无法独立核实。按报告自述采信，但**不得**作为法务结论，只能作为"开工前必须人工确认"的事项 |
| 14 | A2 PySide6 作为 MVP GUI（含 `pyside6-deploy` 打包） | **UNVERIFIED** | 未安装、`.pydeps` 里也没有；在"用户级 pip 被阻断"的前提下，Qt 体积（数百 MB）能否落进 `--target .pydeps` 未测。且现有仓库**根本没有 GUI**，`scripts/` 是 CLI+daemon 形态，引入 GUI 是纯增量成本 |
| 15 | A7 传统 CV 优先、ONNX 后置 | **UNVERIFIED** | 无真实 Xin 图像（客户端当前未运行），无法证明模板匹配在真实 ROI 上能否达到报告自己要的 98% precision/recall |
| 16 | A8 Vision OCR 作为可抽象后端 | **SUPPORTED**（能力）/ 成本未建模 | 本机确证可用：`scripts/seerctl.swift` L35–39 用 `VNRecognizeTextRequest` + `.accurate` + `zh-Hans`，实跑返回 ~44 条中文文本，功能没问题。但见 §3：整帧 OCR 是整条流水线里最贵且最受负载影响的一环 |
| 17 | A15 "客户端容器未知，Ruffle / AIR / Wine / projector 都是候选，必须实测" | **CONTRADICTED（已被 Round 0 审计解决，非本报告之罪）** | `PROJECT_AUDIT.md` 已确定：客户端 = `/Applications/SeerGame.app`，**Electron + PepperFlash**，二进制 `SeerGame`，`artifacts/unpacked/dist/` 已解包。于是报告 `### Flash 客户端必须完成的十项验证` 里的 Ruffle / AIR / Wine / 独立 projector 四条分支**可以全部关闭**；其中 Ruffle 在 `## Executive Summary` L13 被列为"值得重点实验"，而 PepperFlash 是插件形态、不是 SWF 可迁移形态，这条重点实验是**白费功夫**。仍成立的只有"窗口可捕获性/焦点/遮挡/最小化"那几项 |
| 18 | A6/A10 WindowLease + TaskBudget + 停止条件 | **UNVERIFIED — 需实验** | 设计合理但未经任何时序压力检验：lease 获取/续租/超时在 180–600ms 的单帧成本下是否还能保证"同一窗口最多一个写任务"，需要真机+并发实验 |
| 19 | A9 Adapter Pack 结构 | **UNVERIFIED** | 只有一个目标服务器（Xin），且其 UI 尚未采集。第二/第三私服"素材同源"的假设没有任何样本支撑 |
| 20 | A5 "AI 不进闭环，模式 A 无 LLM 可跑" | **SUPPORTED BY OUR MEASUREMENT** | 现有 `pyproject.toml` 的依赖只有 `numpy` + `opencv-python-headless`，注释明写 "no LLM SDK, no network client"；`scripts/seerctl`/`tower.py` 是纯确定性实现，连 token 都没有。这条原则已经落地了 |

**计数**：SUPPORTED = 1, 2, 3, 9, 11, 12, 16, 20 → **8**；CONTRADICTED = 4, 5, 6, 7, 8, 10, 17 → **7**；UNVERIFIED = 13, 14, 15, 18, 19 → **5**。

---

## 3. 性能声明审计（报告里出现的每一个数与"没有出现的数"）

先把最刺眼的事说完：**全文 422 行、73 个引用标记，没有一个延迟数字。** 而 `深度研究计划.md` `### 1. 截图引擎` 明确要求交付"**帧率控制**"和"**截图延迟优化**"。这是需求漏项，不是风格差异——一套每步都要"抓帧→识别→动作→再抓帧验证"的架构，不给延迟预算就等于没设计。

### 3.1 报告断言的数字（逐条贴标签）

| 出处 | 断言 | 证据标签 | 审计意见 |
|---|---|---|---|
| `### 状态机与视觉策略` L167 | UNKNOWN 状态"5 秒后暂停" | **estimate** | 无依据。5s 在 140–630ms/帧的粒度下等于盲等 8–35 帧。方向安全（宁停不点），但数值是拍的 |
| L168 | MAP "3 次失败暂停" | **estimate** | 拍定；方向安全 |
| L169 | BATTLE_START "10 秒恢复" | **estimate** | 拍定；未说明 10s 与"战斗开场动画通常多久"的关系 |
| `### Flash 客户端必须完成的十项验证` L48 | "连续捕获 300 帧" | **estimate**（作为实验设计） | 帧率未定义，所以 300 帧 = 12 秒或 195 秒都成立，实验不可复现 |
| `### 首批十个 Coding Agent 任务` L324 | T05 完成标准"300 帧无异常" | **estimate** | 见 §2-8：验收标准不含延迟，与计划要求不匹配 |
| `### 脚本格式` L190–193 | budget 20 场 / 45 分钟 / 3 次连续错误 | **estimate** | 配置示例，本身无害 |
| `## Agent、MCP…` L240 | `timeoutMs: 10_000` | **cited-from-docs**（API 约束） | 真实：`defineTool` 明确校验 `timeoutMs` 必须为正有限数（`dsh-tools/lib/index.js:848`） |
| `### 测试矩阵` L337 | 核心模块覆盖率 ≥85% | **estimate** | 目标值，非测量 |
| L338 | 主状态 precision/recall ≥98% | **estimate** | 无真图，当前不可验收 |
| L341 | 50 场完成率 ≥98% | **estimate** | 同上 |
| L340 | 20 次无越权点击 | **estimate** | 安全指标，合理 |
| L342 | 权限撤销后 ≤2s 停止输入 | **estimate** | 需实测 TCC 撤销传播延迟，报告未测 |
| `### 首批十个 Coding Agent 任务` T03 L322 | 坐标 roundtrip ≤0.5pt | **estimate** | 可实现，但与延迟无关 |
| `### 多私服扩展机制` L416 | 采集 10–30 张核心状态图 | **estimate** | 与 `### 用户需要提供的最少真实材料` L54 的 8–12 张**口径不一致**（同一件事给了两个区间） |
| L54 | 60–120 秒录像 + 8–12 张 PNG | **estimate** | 经验值，合理 |
| `## Executive Summary` L7 | SCK 自 12.3 引入 | **cited-from-docs** | **部分正确**：`SCStream`/`SCShareableContent` 确为 12.3（本机 SDK 头文件已验证），但 `SCScreenshotManager` 是 14.0——报告把"SCK 12.3"直接当成"截图能力 12.3" |
| L7 | 最低 macOS 13 | **estimate** | 与 14.0 事实冲突，见 §2-6 |
| `### 技术决策矩阵` L97–109 | 加权 4.0 / 4.1 / 3.2 / 4.6 | **estimate**（自评"工程判断"） | 算术复核：Python 列实为 **3.90**（报告写 4.0），Swift 4.05→4.1、混合 4.55→4.6、Tauri 3.20→3.2（后三者为四舍五入，可接受）。**Python 列的 +0.1 无解释** |
| `## Executive Summary` L9 | Harness 0.2.2-alpha.2 / Node ≥24.9.0 | **cited-from-docs（错误）** | 实测 0.2.1-rc.5，无 engines 字段，见 §2-10 |
| L13 | Flash EOL 2020-12-31、2021 起阻断 | **cited-from-docs** | 本机无法验证（无网络）。与普遍认知一致 |
| L11 | `xin_tool` v1.4.1、2025-05 更新 | **cited-from-docs** | 本机无法验证 |

### 3.2 报告**没有**给出的数字（这才是问题）

| 缺失项 | 为什么必须有 | 我们现在的实测替代 |
|---|---|---|
| 单帧抓取延迟预算 | 决定 FSM 的状态确认能多快 | SCK 179.6ms（负载 68；空载值未测，应交给 `native/bench_capture.swift`）；screencapture 139ms（空载）–384ms（负载 68） |
| 整帧 OCR 成本 | 决定"每步验证"是否可行 | 整帧 2298×1428 Vision `.accurate`：空载 469ms，负载 68 时 **2.1–3.2s** |
| 帧率目标（抓帧循环 Hz） | 计划点名要求 | 无。按 180ms/帧算上限 5.5Hz；按 384ms 算 2.6Hz |
| 端到端"动作→确认"时间 | 决定是否会对着过期帧决策 | 抓帧+OCR = **0.65–3.5s**（负载相关），比 `tower.py` 的盲等 1.35s 还长 |
| 每步的后置验证超时/降级规则 | 没有它，验证失败时 FSM 只能干等 | 现有代码的做法是 `time.sleep(1.35)` 后直接点下一发——**这正是报告要避免、但报告也没给出替代方案的东西** |

**结论**：报告在"性能"这件事上交付的是**零个数据点**，而计划明确点名要它。§2-7、§2-8 两条 CONTRADICTED 的根都在这里。

---

## 4. 工具与环境声明审计

| 声明 | 出处 | 标签 | 核实结果 |
|---|---|---|---|
| "PyObjC 当前支持 macOS/arm64 并暴露 Quartz/ApplicationServices" | `### 技术决策矩阵` L95 | **cited-from-docs（上游为真，本机为假）** | 上游确为真，但**本机 PyObjC 未安装**（`objc`/`Quartz`/`AppKit`/`Foundation` 全 missing），且 `pip install` 到用户站点被沙箱阻断（只能 `--target .pydeps`）。报告把"上游支持"当成"这里能用" |
| PySide6 + `pyside6-deploy` | L349、L374 | **cited-from-docs** | 本机未安装，未验证能否装进 `.pydeps` |
| `mss` | — | **N/A** | 报告**全文 0 次**提及 `mss`（明确核查过）。它没推荐这个"捷径"，这点比同类报告干净 |
| Quartz/CoreGraphics 截图 | L111、L374 | **cited-from-docs** | 说法("不要用旧 CG 窗口截图")方向对，但作者**没测过**该路径的成本；而仓库现有实现（`scripts/seerd.swift:74-75`）恰恰就是这条路——报告对此一无所知（见 §5-G1） |
| Harness 插件 API（`apply`/`inject`/`ctx.tools.register`/`defineTool`/`pre-execute`/`guard`/`post-execute`） | Exec L9、L215–248 | **VERIFIED（对着已安装运行时）** | **全部为真**。`dsh-tool-todo` 是活样板；`defineTool` 从 `@deepseek-ai/dsh-tools` 导出；生命周期钩子名一字不差。报告这段代码基本可直接改用 |
| "Harness 仍标记 Developer Preview" | Exec L9、L248 | **部分可验证** | 版本号 `0.2.1-rc.5` 是预发布通道（`dshDesktopUpdateChannel: nightly`），与"Preview"自洽；但确切文案无法验证 |
| Harness 0.2.2-alpha.2 / Node ≥24.9.0 | Exec L9 | **VERIFIED-FALSE** | 见 §2-10 |
| MCP 规范 2026-07-28、stdio 与 Streamable HTTP | L250 | **VERIFIED（客户端实现侧）** | `dsh-mcp-client` 文档原文可对上 |
| MCP "必须做输入验证/访问控制/限流/输出清洗/审计" | L250 | **UNVERIFIED** | 规范条文，无网络无法核对 |
| Ruffle 可作 Flash 容器候选 / AVM1+AVM2 未 100% | Exec L13、L46 | **UNVERIFIED** | 无网络；且 Xin 到底跑在什么容器里仍然未知 |
| BetterGI 内部实现（Paddle 检测 / ONNX Runtime / OpenCvSharp / `CaptureContent` 派生统一分辨率） | L36 | **UNVERIFIED（细节），但结论可用** | 本机无 BetterGI 源码，无法逐条核对。作为"借鉴架构不借鉴实现"的论证足够，作为事实陈述风险偏高 |
| 12 份配套文档 `00_…`–`11_…` | L15–30 | **CONTRADICTED（在本仓库内）** | 链接指向 `sandbox:/mnt/data/seer_automation_research/`，该路径**不存在**，`docs/` 下也没有任何 `00_`–`11_` 命名文件。要么没生成，要么没落到项目里 |
| 73 处 `turn…view/search…` 引用标记 | 全文 | **不可核对** | 全文 **0 个 URL**。这些是浏览器工具的会话内句柄，读者无法复现任何一条"已验证事实"。这是全报告最系统的证据缺陷 |

---

## 5. 报告说对了、应当直接采纳的 5 项

1. **ScreenCaptureKit 作为唯一正式截图层，且必须常驻。**（§2-1/§2-3：我的探针 180ms vs screencapture 384ms，抖动 13% vs 96%；冷启 CLI 要多付 ~530ms。权威数字以 `native/bench_capture.swift` 为准。）→ 落法：`seerauto/capture.py` 里的 `DaemonCapture` 走 JSON-lines 的常驻 Swift 守护进程；`SubprocessScreencapture` 只留作诚实降级（与 `AGENTS.md` §5.1 第 4 条一致）。
2. **Python 核心 + Swift 系统层的边界，且 Core 独占输入权与 lease。**（§2-3/§2-20 支持；现有的 `seerctl`/`seerd` 拆分已是这个形状。）→ 落法：任何 `CGEvent` 只能在 Swift 侧、由 Core 凭 lease 调用；GUI/CLI/MCP/Harness 一律走 Task Service。
3. **AI 不进闭环。**（§2-20 已被现有 `pyproject.toml` 兑现：只有 numpy + opencv-headless，无 LLM SDK。）→ 落法：把这条写进 CI——核心包出现 LLM 依赖即失败。
4. **Harness 插件 = 薄壳 + `compat/<version>.ts` 版本隔离。**（§2-9 已对着运行时验证 API 形状；`@deepseek-ai/dsh-tools` + `defineTool` 可直接用，`tools/pre-execute` / `ctx.tools.guard` 就是报告说的那层 allow/deny 挂点。）→ 唯一要改的是版本号（0.2.1-rc.5，不是 0.2.2-alpha.2）。
5. **"不承诺最小化/后台一定可捕获"，并让 adapter 显式声明 `requires_visible_window: true`。**（报告 L50/L345 两次强调，是对的：SCK 是窗口捕获，不是离屏渲染钩子。）→ 落法：这条要保留进 manifest schema，即使 `PROJECT_AUDIT.md` 已经把"容器未知"解决了——**容器知道了，不代表最小化后还继续绘制**，那仍然要实测。

**附带第 6 项（不是报告的功劳，但值得制度化）**：把"记录 loadavg + n + p50/p95"写进所有 benchmark（`AGENTS.md` §5.1 第 1 条已要求 n≥6）。本机 load 43→68 时同一命令成本差 2.3–5.5 倍；不记负载的基准数字会直接误导架构选型，也会让后来者误以为前人的数字是错的。

---

## 6. 应当丢弃或推迟的

| 项 | 处置 | 理由 |
|---|---|---|
| Python 3.12/3.13 作为 MVP 基线 | **丢弃** | 本机无 3.12/3.13；`pyproject.toml` 已定 `>=3.10`；换解释器意味着 `.pydeps` 全部重装，而用户级 pip 被阻断。**维持 3.10.13 + `.pydeps`** |
| PyObjC 作为 Python 直连 macOS API 的通道 | **丢弃** | 未安装 + 装不进去（沙箱）。所有系统能力走 Swift helper |
| PySide6 作为首版 GUI | **推迟** | 未安装、体积大、当前项目无 GUI 需求；先把 capture→FSM→action 闭环和 `bench` 打磨好 |
| "最低 macOS 13" | **改为 14.0** | `SCScreenshotManager` = 14.0；想用 `captureImage(in:)` 抓 ROI 则 15.2。若真要支持 13，唯一路径是 `SCStream` 持续流（12.3），那是另一套架构，需要单独论证 |
| "每步动作后置视觉验证"作为无条件步骤 | **改为"按预算可降级的验证"** | 一次验证 0.65–3.5s，比很多动作的动画还长。必须给"验证超时→沿用上次证据/降级为保守等待"的规则，否则 FSM 会卡死或对着过期帧决策 |
| T05 验收"300 帧无异常" | **改为带阈值的验收** | 例：p95 抓帧 ≤ 250ms、连续 300 帧 p99 抖动 ≤ 1.5×、丢帧 0；否则"300 帧"什么都证明不了 |
| 首批 10 个任务（T01–T10）作为"从零起步"的后备 | **重写为增量** | 见 §7-G1：仓库里 `seerauto/`+`tests/`+`bench/` 已经存在，T01–T10 里至少 T02/T03/T07/T09/T10 已有部分实现，照抄会造成大规模重复建设 |
| 12 份配套文档的链接 | **失效，删除引用或补齐** | `sandbox:/mnt/data/...` 在本机不存在 |

---

## 7. 报告留下的缺口（后几轮必须回答）

**G1（最严重）：报告完全不知道本项目已有实现。**
全文对 `seerd` / `seerctl` / `tower.py` / `scripts/` 的提及次数 = **0**。而现实是：`scripts/seerd.swift` 已经在做窗口钉住 + `screencapture` 抓图，`scripts/seerctl.swift` 已有 Vision OCR + click/key/type/scroll，`seerauto/` 已有 `capture.py`（四种后端，含 `DaemonCapture` 协议）、`geometry.py`、`perf.py`（分阶段计时器）、`bench/`、`tests/`。报告设计了一套 10 任务的重建 backlog，等于把已有的 ~800 行 Swift 和整套 Python 骨架判了死刑却没提一句。**Lead 必须先决定：是"重构现有"，还是"按报告重建"——不能默认后者。**

**G2：常驻 helper 的形态没定义。**
报告只说"一个很小的 Swift helper"。实测显示常驻与否差 ~530ms/次（713ms 冷 CLI vs 180ms 内部）。这个 helper 是 XPC？stdin/stdout JSON？（`seerauto/capture.py` 已经选了后者，报告不知道。）它崩了怎么办？lease 怎么跟它绑定？都空白。

**G3：OCR 的成本被完全忽略。**
Vision `.accurate` 在整帧 2298×1428 上 469ms（空载）–3.2s（负载）。报告把 OCR 当成"抽象后端"就结束了，没有"只 OCR 小 ROI"、"先模板匹配快速否决再 OCR"、"必要时降到 `.fast`"这类策略。**这是当前流水线的头号成本项。**

**G4：TCC 权限的真实行为未测。**
报告正确指出需要用户授权，但没回答：撤销授权后正在跑的任务多久停？`SCScreenshotManager` 在无授权时抛 `SCStreamErrorUserDeclined`（-3801）还是返回黑帧？CI/无头场景怎么办？（本次实测里 SCK 一次就成功，说明 harness 进程本身已有录屏权限——**这只证明"能用"，没证明"用户装完之后能用"**。）

**G5：多屏 / 负 origin / 缩放切换。**
报告把它们列进测试矩阵，但没有一个具体场景与期望值。本机 2940×1912 @2x，抓 1149×714 逻辑区得到 2298×1428——这个 2× 映射必须写成契约（现有 `seerauto/geometry.py` 已经在做）。

**G6：Xin 的容器（已答）、最小化后是否继续绘制、战斗动画真实时长。**
容器问题已由 `PROJECT_AUDIT.md` 关闭（Electron + PepperFlash）。但**后两件仍然是空的**，而它们直接决定 FSM 的 deadline 该填 5s 还是 15s。报告把它们列为待验证，却没和 FSM 表里的数值（5s/10s）联动——**一旦真实开场动画是 2.5s，"10 秒恢复"就是在白等 7.5 秒**；一旦 PepperFlash 在窗口被遮挡时停止绘制，所有"后台跑"的设想当场作废。

**G7：`bench/` 的指标口径（Round 1 正在建）。**
仓库里 `native/bench_capture.swift`（1686 行）+ `bench/bench_cv.py`、`bench_ocr.py`、`bench_vision.py` 已经就位，`bench/results/` 还是空的。报告对此零输入。需要定：测哪个 stage、样本量、p50/p95、**是否记录 loadavg**、是否固定窗口位置、以及"每次测量前是否确认游戏在跑"（否则测的是桌面）。

---

## 8. 内部矛盾与事实错误（可直接对照原文）

1. **"已验证事实"标签通胀**（`### 已验证事实、源码推导与待验证项` L34–42）。三处"**已验证事实。**"里，BetterGI 内部实现（Paddle/ONNX/OpenCvSharp）和 Harness 版本号都是浏览器搜索摘要的转述，其中 Harness 版本号**现在是可证伪地错了**。全文 73 个 `turn…` 句柄、0 个 URL——"已验证"三个字没有可核对物支撑。
2. **矩阵算术与结论表不一致**：Python + PyObjC/PySide 列按权重实算 **3.90**，报告写 **4.0**（其余三列是四舍五入，可接受）。见 §3.1。
3. **最低版本与状态机互斥**：`SCScreenshotManager` 需 14.0，报告却把 13 定为门槛（§2-6）。同一份文档里既写"SCK 12.3 引入"又写"13 起步"，两句话指的其实是两个不同的 API，没有一处说明。
4. **推荐栈与评分表错位**：最终答案是"Python + Swift helper"（L374），但矩阵里被评分的却是 "Python + PyObjC/PySide" 一整列；报告既用 PyObjC 给这一列打分，又在结论里把它悄悄换成了 Swift helper。
5. **"很小的 Swift helper"与交付物规模不符**：L7 称 helper"很小"，但 T01 要 `Package.swift`、T04–T06 是三个 Swift 模块 + `swift test`、`### 打包与发布` L351–364 还要独立签名/公证的 `Helpers/seer-macos-bridge`。这是一个独立二进制分发物，不是"很小的 helper"。
6. **取证材料数量口径不一致**：L54 要 8–12 张 PNG，L416 要 10–30 张。
7. **两份"证据"互斥**：L11/L42 明确说 `xin_tool` **不能**当作 Xin 客户端运行方式的证据（正确），但 L36/L383 又把 BetterGI 的实现细节当作本项目可照搬的"已验证事实"——同样是第三方仓库，一个采信一个不采信，标准不一致。
8. **配套文件不存在**：L15–30 承诺的 12 份文档全部指向 `sandbox:/mnt/data/…`（本机不存在，`docs/` 里也没有）。
9. **性能需求整体缺席**（§3.2）：计划点名要"帧率控制 / 截图延迟优化"，报告 422 行里 0 个延迟数字、测试矩阵 0 个延迟指标。

---

## 9. 附录：本次实测记录（含复现方式）

机器：macOS 15.7.7 (24G720) / Apple M2 / 2940×1912 @2x / Swift 6.2.3 / Python 3.10.13 / node v25.6.1。
测量时段：2026-11-01 16:26–16:40，**load average 43–68（8 核）**，见 §0。

| 测量 | 方法 | 结果 |
|---|---|---|
| `screencapture -x -R 300,120,1149,714` | `subprocess.run` + `perf_counter`，n=9（去冷启） | mean **315.2ms** / median 269.9 / min 250.2 / max 496.7 |
| 同上，另一轮 n=8 | 同上 | mean **367.8ms** / median 344.8 / max 627.9 |
| 同上，与 SCK 交叉测量 n=8（各 4 次交替） | 交替执行以抵消负载漂移 | mean **383.7ms**（280–548） |
| `screencapture -x` 全屏 | n=5 | mean **550.4ms**（535–593） |
| `scripts/seerctl ocr <2298×1428.png>` | 现有二进制，含进程启动 | mean **2599ms**（2277–2876），空载基准 468.9ms |
| 同上（并发编译时测） | — | 3.0–6.1s —— **说明负载会 6× 以上污染基准** |
| **SCK `SCScreenshotManager.captureImage`**（新写 Swift 探针，2298×1428 输出） | 进程内计时，与 screencapture 交替 | mean **179.6ms** / 169–192（离散度 **13%**） |
| 同一探针的 wall time（冷启 CLI） | 外部计时 | **713ms** → 枚举+启动约 **530ms** |
| 进程派生校准 | `/usr/bin/true` / `python3 -c pass` | 4.5–8.6ms / 29.8ms（正常，排除 shell 慢） |
| SDK 可用性 | `grep API_AVAILABLE` macOS 15.7 SDK 头文件 | `SCStream` 12.3；`SCShareableContent` 12.3；**`SCScreenshotManager` 14.0**；`captureImageInRect:` **15.2** |
| Harness 运行时 | 解包 `app.asar` 读取 `package.json` / `dsh-tools` / `dsh-tool-todo` | 0.2.1-rc.5；MIT；`defineTool` 与生命周期钩子全部存在；无 `engines.node` |
| MCP | `dsh-mcp-client` 文档 | "2026-07-28 protocol when available and falls back to legacy revisions" |

复现要点：

```bash
# 1) 抓帧成本（务必同时记 loadavg）
sysctl -n vm.loadavg
python3 - <<'PY'
import subprocess,time,statistics as st
ts=[]
for _ in range(8):
    t0=time.perf_counter()
    subprocess.run(["/usr/sbin/screencapture","-x","-R","300,120,1149,714","/tmp/r.png"])
    ts.append((time.perf_counter()-t0)*1000)
print("mean=%.1f median=%.1f"%(st.mean(ts),st.median(ts)))
PY

# 2) 同一区域的 SCK 对照：用 SCScreenshotManager.captureImage 写一个小 Swift 探针，
#    与上面交替执行；进程内计时（不要用 wall，冷启会多 ~530ms）
#    注意：SCScreenshotManager 需 macOS 14.0+，captureImage(in:) 需 15.2+
```

**这一节的目的是让 Lead 能反驳我**：所有数字都附了条件。唯一不依赖绝对值的结论是那条比值（2.1×）和离散度（13% vs 96%）——因为它们是同负载交替测出来的，负载漂移对两边同等生效。

---

## 10. 给 Lead 的三句话

1. **报告的方向可以信，数字不能信。** 唯一实测站得住的核心决策是 ScreenCaptureKit 作主路径（2.1× 且稳定 7 倍），其余涉及具体版本/包/API 可获取性的断言，至少三条在本机是假的（PyObjC、Python 3.12/3.13、DSH 0.2.2-alpha.2）。
2. **最该补的不是架构图，是延迟模型。** 计划点名要的"帧率控制 / 截图延迟优化"，报告一个数字都没给；而我们实测一次"抓帧+OCR"要 0.65–3.5s，`tower.py` 靠 `sleep(1.35)` 硬扛——这个洞不补，FSM 和 lease 都在猜。
3. **先看仓库再看报告。** 报告对 `seerauto/`、`scripts/` 零认知，照它的 T01–T10 干会重复建设；建议把它降级为"设计参考"，把 §5 的 5 条 + §9 的测量纪律并入现有实现。
