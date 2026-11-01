# KNOWN_PROBLEMS — 已知问题清单

> 排序依据：**对"闭环延迟"的影响 × 对"可靠性"的影响**，不是按代码美观度。
> 每条都标注：证据、当前代价、修复方向、验证方法。
> 最后更新：2026-11-01 Round 0/1

---

## 严重度总览

| # | 问题 | 类型 | 单次代价 | 状态 |
|---|---|---|---|---|
| P1 | 截图走子进程 + 落盘 + 重读 | 性能 | **~139 ms/次** | Round 1 测量中 |
| P2 | 全图 Vision `.accurate` OCR 只为匹配 2 个关键词 | 性能 | **~240–469 ms/次** | Round 1 |
| P3 | 用固定 `sleep` 代替状态变化检测 | 性能+可靠 | **~1.35 s/回合** | Round 3 |
| P4 | 每回合重复全图检测，无 ROI 概念 | 性能 | 放大 P1+P2 | Round 3 |
| P5 | 点击无结果确认 | 可靠 | 隐性失败 | Round 4 |
| P6 | `scale=2.0` 硬编码、窗口几何无运行时校验 | 可靠 | 静默错点 | Round 3 |
| P7 | 关键词硬匹配判状态，无状态机 | 可靠 | 误判 | Round 3 |
| P8 | 无统一预算/急停/暂停恢复语义 | 安全 | 可能失控 | Round 3 |
| P9 | 零测试、零依赖清单 | 工程 | 无法回归 | Round 1 |
| P10 | 工程状态只存在于聊天上下文 | 工程 | 换 Agent 即失忆 | ✅ 本轮已修 |

---

## P1 — 截图走子进程 + 落盘 + 重读

**位置**：`scripts/seerd.swift:73-82`

```swift
func shot(_ path: String, ...) -> [String: Any] {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    p.arguments = ["-x", "-R", "...", path]
    try p.run(); p.waitUntilExit()                    // ← 等进程退出
    let src = CGImageSourceCreateWithURL(...)          // ← 再从磁盘读回
    let img = CGImageSourceCreateImageAtIndex(src, 0, nil)
    ...
}
```

**为什么慢**：一次调用打包了 4 个可去掉的开销
1. `fork` + `execve` + dyld 加载 `/usr/sbin/screencapture`（最大头）
2. 该进程自行建立窗口服务器连接
3. 图像 **PNG 编码**并写盘
4. 本进程 **PNG 解码**读回

**实测**：`screencapture -x -R 300,120,1149,714 out.png` → mean **139.4 ms**，p50 142.9，p95 168.4（n=8）
产物 629 KB PNG。整条链路里**没有任何一步需要真的产生 PNG 文件**。

**修复方向**：进程内捕获，帧留在内存（`CVPixelBuffer`/`CGImage`），仅在需要人工排查时才落盘。
**关键约束**：本机 macOS 15.7.7 已**移除** `CGWindowListCreateImage` / `CGDisplayCreateImage`（obsoleted in macOS 15.0，实测编译报错），所以只能走 ScreenCaptureKit。

**验证方法**：同一区域、同样 n，对比 mean/p50/p95。

---

## P2 — 全图 `.accurate` OCR 只为匹配两个关键词

**位置**：`scripts/seerd.swift:84-103`（OCR 实现）、`scripts/tower.py:battle_state()`

```swift
req.recognitionLevel = .accurate          // ← 最高精度，最慢
req.recognitionLanguages = ["zh-Hans", "en-US"]
// 永远是整张 2298×1428
```

而调用方只想知道"有没有出现 `胜利` / `失败` / `当前回合`"：

```python
def battle_state(d):
    t = ' '.join(i['text'] for i in d.capture(0.3))
    if '胜利' in t or '失败' in t or '获得经验' in t: return 'end'
    if '当前回合' in t: return 'battle'
    return 'idle'
```

**为什么慢**：整屏中文 OCR + `.accurate` 级别 + `usesLanguageCorrection=false` 但仍是 full pipeline。

**实测（分三个口径，别混用）**：
| 口径 | 耗时 | 条件 |
|---|---|---|
| `seerctl ocr`（子进程，含启动） | 469 ms | 接管当天，近空闲 |
| Vision 进程内 `.accurate` 全图 | **146.4 ms** | load 5.74–7.48（`QUIESCED-FINAL`） |
| 同上但 load 37 / load 85 | 483 ms / 685 ms | **OCR 延迟随负载放大约 4 倍** |
| `.fast` 全图 / `.accurate` ROI / `.fast` ROI | 62.3 / 64.8 / **41.8 ms** | 同上 |

> **注意**：`usesLanguageCorrection` 已实测**不是**影响因素（false 482.7 vs true 469.4 @ 同负载）。
> 真正的放大来自负载，所以**任何 OCR 数字不标 loadavg 都不可复现**。

**真正的错配**：用一个通用 OCR 引擎去做**二分类**（战斗界面 vs 非战斗界面）。这件事用**模板匹配或像素指纹**只需亚毫秒（实测 3-patch 指纹 **0.0385 ms**、ROI 300×200 模板 **0.96 ms**）。

**修复方向（分层）**：
1. 第一层：ROI 模板匹配/像素指纹判状态 → 目标 < 10 ms。
2. 第二层：确需读文字时，只 OCR 小 ROI，且优先 `.fast`。
3. 第三层：`.accurate` 全图仅用于离线建模板/调试。

**验证方法**：对 `artifacts/battle.png` vs `artifacts/game.png` 分别跑三层，比较耗时与判定一致性。

---

## P3 — 用固定 `sleep` 代替状态变化检测

**位置**：`scripts/tower.py:38-45`、`scripts/farm_tower.py:22-29`

```python
d.click_local(*FIGHT_BTN); time.sleep(0.25)
d.click_local(*SKILL_POS);  time.sleep(1.35)   # ← 1.35s 盲等
st = battle_state(d)
```

**为什么这是最贵的**：单回合约 2.0–2.2 s，其中**1.35 s 纯等待**，且这个数字是"猜"出来的——
- 技能动画短于 1.35 s → 白等。
- 长于 1.35 s → 下一回合点早了，**点空**，然后靠 `attempt` 重试掩盖。

**修复方向**：等待"状态变化"而不是等待"时间"——轮询廉价信号（ROI 像素差/按钮出现/回合数变化），一变化立即行动。
**验证方法**：真机记录"点击→状态变化"的真实分布（P50/P95），证明固定值要么白等要么点空。

---

## P4 — 每回合重复全图检测，无 ROI 概念

**位置**：`scripts/fast.py:39-49`（`shot()` 全窗口）+ 所有 `capture()` 调用点

整个 2298×1428 的窗口被反复完整抓取 + 完整 OCR，即使只关心右上角一行字或右下角一个按钮。
**代价**：P1 与 P2 的成本被乘上"每回合调用次数"（3-5 次/回合）。

**注意（本轮实测反直觉结果）**：ScreenCaptureKit 的**单次截图 API 对尺寸不敏感**——
实测 2298×1428 全图 111.8 ms vs 300×200 ROI 124.7 ms，ROI 并没有更快。
所以 **ROI 的收益必须来自下游（OCR/匹配的像素量），不能指望来自截图 API 本身**。
真正的截图级 ROI 收益只有两条路：① 改用 `SCStream` 常驻流（帧边际成本趋近 0）；② 从**内存中的整帧**裁剪（裁剪本身微秒级）。
> 这条已经修正了上一版"ROI 截图会更快"的假设，见 `docs/PERFORMANCE_BASELINE.md`。

---

## P5 — 点击无结果确认

**位置**：所有 `click_local` 调用点

点完立刻继续下一步，从不验证"这次点击有没有产生预期变化"。
`docs/action-log.md:19` 已经有真实事故记录：

> 17:49 战斗中点「逃跑」坐标失准（窗口被拖动过），未立即逃出，浪费 1 回合

**修复方向**：每次有副作用的动作后，做一次廉价的状态断言（变化/未变化）；未变化则计数并在超阈值时停机。
**禁止**：无限盲点。用户明确要求"未知状态不允许持续盲点"。

---

## P6 — `scale=2.0` 硬编码，窗口几何无运行时校验

**位置**：`scripts/fast.py:17`（`self.scale = 2.0`）、`scripts/play.py:38`（按宽度猜 2.0/1.0）

如果显示器缩放改变、窗口被拖动/调整、或换到外接屏（本机实测有 **2 个显示器**），
所有点击坐标会**静默偏移**，且不会报错——只会表现为"点了没反应"，进而被 P3 的重试掩盖。

**修复方向**：每次动作前用 `win` 校验窗口身份与几何，超出容差即拒绝点击并抛错。
**验证方法**：单测坐标换算；真机故意拖动窗口后验证拒绝行为。

---

## P7 — 关键词硬匹配判状态，无状态机

**位置**：`scripts/tower.py:battle_state()`

只有三个结果（`end` / `battle` / `idle`），且依赖 OCR 恰好认出关键词：
- OCR 抖动一次（`.accurate` 也不是 100%）→ 误判 `idle` → 走 `clear_dialogs` 乱点。
- 战斗动画中间帧既无"当前回合"也无"胜利" → 落进 `idle`。
- 弹窗（成就/活动/公告）出现 → 完全无对应状态。

**修复方向**：显式状态机（UNKNOWN/IDLE/BATTLE_TURN/BATTLE_RESULT/REWARD/DIALOG/ERROR/PAUSED/STOPPED），
每个状态有进入谓词、允许动作、超时、转移；`UNKNOWN` 必须有超时升级路径而不是无限等待。

---

## P8 — 无统一预算/急停/暂停恢复语义

现状只有零散的 `for turn in range(40)`、`for attempt in range(3)`。
- 没有"总时长预算"、"总场次预算"。
- 没有全局紧急停止（只能 kill 进程，可能停在半场战斗中）。
- 没有暂停/恢复。
- 没有"连续错误 N 次即停"的统一策略。

用户硬性要求：**任务具备执行次数与时间预算**、**必须有紧急停止机制**、**AI 服务不可用时纯视觉模式仍应工作**。
当前三项均未实现。

---

## P9 — 零测试、零依赖清单

- 无 `tests/`、无 pytest、无 CI。
- 无 `requirements.txt` / `pyproject.toml`。
- 无任何可回归的断言。

后果：每一次改动都只能靠"再手动跑一次游戏"验证，而游戏不在时**完全无法验证**。
本轮已把 `artifacts/` 的真实截图转为 Replay 测试集来打破这个死结。

---

## P10 — 工程状态只存在于聊天上下文 ✅ 已修复

原状态：无 git、无 `AGENTS.md`、进度只在 `docs/progress.md` 的 10 行里且已过时。
本轮已建立：git 首个 checkpoint、`AGENTS.md`、`docs/PROGRESS.md`、`docs/NEXT_ACTIONS.md`、本审计三件套。

---

## 附：不在本轮修复范围的已知风险

| 风险 | 说明 |
|---|---|
| 客户端更新导致坐标漂移 | 版本 1.0.8.9；升级后 `docs/state.md` 坐标可能全部失效，需重新标定 |
| 反作弊/风控 | 采用正常 GUI 输入，不做内存注入；但自动化本身仍可能违反用户协议，属用户自担 |
| 交易行/经济不可控 | 价格、掉率、活动完全是服务端状态，自动化无法保证，视频路线的前提当前不成立 |
| 多显示器/分辨率变更 | 实测本机有 2 个显示器，当前代码未处理，实际投放前必须加 |
