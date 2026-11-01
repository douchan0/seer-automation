# EXISTING_IMPLEMENTATION — 已有工程实现说明

> 用途：让任何新接入的 Agent/开发者在不重读全部源码的情况下，准确知道上一轮**实际建成了什么、怎么用、边界在哪**。
> 依据：源码逐行阅读 + 实测运行，不是复述上一轮的自述。
> 对应审计：`docs/PROJECT_AUDIT.md`

---

## 1. 整体形态

上一轮交付的是一套**手工工具链**，不是产品，也不是框架：

```
            ┌──────────────────────────────────────────┐
            │  scripts/seerctl (Swift CLI, 一次性)      │  调试用
            └──────────────────────────────────────────┘
                             ▲
   Python 侧                 │ stdin/stdout JSON-line
   ┌───────────────┐   ┌─────┴──────────────────────────┐
   │ play.py       │   │ scripts/seerd (Swift 守护进程)  │
   │ fast.py       │──▶│  win / pin / shot / ocr /      │
   │ tower.py      │   │  click / clicklocal / move /   │
   │ farm_tower.py │   │  key / type / activate / quit  │
   └───────────────┘   └────────────────────────────────┘
                             │
                             ▼
                  /usr/sbin/screencapture（子进程）
                  Vision.framework（OCR）
                  CGEvent（输入）
```

**关键事实**：所有"聪明"的部分都不存在。没有状态机、没有模板匹配、没有任务调度、没有 GUI、没有 AI 层。它是一个**能看、能点、能认字的遥控器**，外加几个把遥控器按顺序按下去的脚本。

---

## 2. 逐个模块说明

### 2.1 `scripts/seerd.swift`（核心，198 行）→ 已编译为 `scripts/seerd`（arm64 Mach-O）

常驻守护进程，一次启动、多次调用，避免每次重启的开销。协议极简：stdin 一行 JSON → stdout 一行 JSON。

| op | 参数 | 作用 | 实现要点 | 评价 |
|---|---|---|---|---|
| `win` | — | 返回 SeerGame 窗口 id + 边界 | `CGWindowListCopyWindowInfo`，筛 `owner=="SeerGame" && layer==0` | ✅ 正确可靠 |
| `pin` | `x,y,w,h` | 强制窗口几何 | 走 Accessibility API `AXUIElementSetAttributeValue` 设 position/size，激活后 sleep 120ms | ✅ 解决坐标漂移的关键 |
| `shot` | `path`, `activate` | 截图 | **`Process()` 启动 `/usr/sbin/screencapture -x -R x,y,w,h`，等它退出，再从磁盘读回 PNG** | ❌ **头号性能瓶颈**，见 `KNOWN_PROBLEMS.md` P1 |
| `ocr` | `path`, `min` | Vision OCR | `.accurate` 级别 + `["zh-Hans","en-US"]`，返回 normalized bbox (0..1) | ⚠️ 级别过高、永远全图 |
| `click` | `x,y,n` | 全局坐标点击 | CGEvent mouseMoved → 15ms → down/up + clickState，再 40ms | ✅ 可用；固定 40ms 尾延时偏保守 |
| `clicklocal` | `x,y,n,scale` | 游戏像素坐标点击 | `全局 = 窗口原点 + 局部/scale` | ✅ 坐标系设计正确 |
| `move` | 同上 | 悬停 | CGEvent mouseMoved | ✅ |
| `key` / `type` | code / text | 键盘 | CGEvent keyboard，type 用 `keyboardSetUnicodeString` | ✅ 中文字符可用 |
| `activate` | — | 激活客户端 | `NSWorkspace.activate`，按 bundleId/名称模糊匹配 `yahaha`/`seergame` | ⚠️ 模糊匹配有误伤风险 |

**好的设计（必须保留）：**
- `clicklocal` 的 Retina 换算思路（局部像素 ÷ scale + 窗口原点）是对的。
- 窗口几何由 AX API 强制固定，让坐标可复现——这是整个方案能成立的前提。
- JSON-line 常驻协议，避免了每次调用重启进程（这是上一轮最大的架构进步）。

**坏的设计（必须替换）：**
- 截图走子进程 + 落盘 + 再读回，打包了三次可去掉的开销（进程启动、PNG 编码、PNG 解码）。
- OCR 永远是全图、永远 `.accurate`，即使只想判断"有没有出现'胜利'两个字"。

### 2.2 `scripts/seerctl.swift`（265 行）→ `scripts/seerctl`

同一个守护进程的"一次性 CLI"版本，额外带 `wl`（窗口列表）、`winset`、`ocr` 等子命令。
用途是侦察与调试，**不该出现在运行热路径上**（每次调用 = 一次进程启动 + 一次 Swift 运行时初始化）。

### 2.3 `scripts/fast.py`（141 行）— `Driver` 类

Python 侧对 seerd 的封装。要点：

- `__init__` 里 `pin` 到 `(300,120,1149,714)`，然后缓存 `self.win`，`self.scale = 2.0` **硬编码**。
- `shot()` 之后**又调一次 `win`**（多一个 round-trip，且在截图后立刻重查窗口，逻辑上无必要）。
- `click_text()` / `wait_text()`：**循环里每次都 `capture()`（截图+全图 OCR）**，`time.sleep(0.15/0.2)`。这是"慢"的第二来源：等待文本时每 350ms 付出约 600ms 的代价。
- `capture()` = `shot()` + `ocr()`，是最小的"看一眼"单位。
- `reset()` / `key()` / `text_input()` / `group()`（OCR 文本行合并）/ `log()`（写 `docs/action-log.md`）。
- 末尾用 `Driver.hover = hover` 这种**猴子补丁**给类加方法——风格随意，但能用。

**问题**：`self.scale` 写死 2.0，未做运行时校验；一旦显示器缩放/窗口尺寸变化，所有点击坐标静默偏移，且**不会报错**。

### 2.4 `scripts/play.py`（159 行）— 更早、更慢的一版

纯子进程实现：`screencapture` 全屏 → PIL 裁剪 → `seerctl ocr`。
每次 `grab()` 约 **185ms（截图）+ 469ms（OCR 子进程）+ 裁剪/落盘**。`grab()` 里还有 `sleep(0.55)` 等激活。
**保留价值**：它是最诚实的"旧方案"基线，Round 1 的对标对象就是它。

### 2.5 `scripts/tower.py` / `scripts/farm_tower.py` — 战斗循环

真正的自动化尝试，也是失败的地方。硬编码坐标：

```python
SKILL_POS = (1151, 1281)   # 技能按钮
FIGHT_BTN = (1789, 1103)   # 战斗/普攻
ENEMY_POS = (1178, 730)    # 敌人
```

`battle_state()` 用**关键词判状态**：`胜利/失败/获得经验` → end，`当前回合` → battle，否则 idle。

回合循环：
```python
d.click_local(*FIGHT_BTN); time.sleep(0.25)
d.click_local(*SKILL_POS); time.sleep(1.35)
st = battle_state(d)          # 又一次 capture() ≈ 600ms
if st == 'end': break
```

**四个结构性缺陷**（详见 `KNOWN_PROBLEMS.md`）：
1. 用固定 1.35s 盲等代替"技能动画结束"检测。
2. 每回合 `capture()` 全图 OCR 只为匹配两个关键词。
3. 没有任何"点了没反应"的确认，靠 `attempt` 循环硬试。
4. 战斗结束后靠 `clear_dialogs()` 连点确认框，**没有次数上限之外的语义**。

`farm_tower.py` 稍好：会试 3 个敌人坐标、能识别"挑战结束"，但同样盲等。

### 2.6 侦察类脚本

| 文件 | 作用 |
|---|---|
| `scene_index.py` | 遍历地图 6×2 共 12 格，逐格进入 + OCR + 截图，产出 `artifacts/scene_index.json` 与 `scene_idx_N.png` |
| `scan.py` | 截图 + OCR，按关键词打印命中项的局部坐标 |
| `shoot.sh` | 激活 → 全屏截图 → 按窗口裁剪，产出 `artifacts/game.png` |
| `dump_ocr.py`、`hover_names.py`、`hover_probe.py` | OCR 转储 / 悬停探测 |
| `deob.js` | Electron 反混淆辅助 |

这些**游客价值高**：`scene_index.json` 与 `scene_idx_*.png` 直接就是 Round 3 视觉状态机的素材。

### 2.7 文档资产

| 文件 | 价值评估 |
|---|---|
| `docs/state.md` | ⭐⭐⭐⭐⭐ 20+ 个**实测**坐标 + 货币/精灵状态。这是最贵的产物，直接沿用。 |
| `docs/plan-from-video.md` | ⭐⭐⭐⭐ 417 帧 OCR 拆解，带时间戳，有校对表。Round 2 基本不用重做。 |
| `docs/checklist.md` | ⭐⭐⭐ 可勾选流程清单，但**未区分"视频里如此"与"当前版本已验证"**。 |
| `docs/action-log.md` | ⭐⭐⭐⭐ 36 条真实操作记录，含失败。是唯一的行为证据链。 |
| `docs/progress.md` | ⭐⭐ 10 行，已过时（停在 18:35）。 |

---

## 3. 已验证可用的能力（可直接继承）

| 能力 | 证据 |
|---|---|
| 找到并固定游戏窗口几何 | `seerd pin` 成功把窗口固定到 (300,120,1149,714)，坐标此后稳定 |
| 抓取窗口区域像素 | `artifacts/game.png` = 2298×1428，与 1149×714 × 2.0 完全吻合 |
| 中文 OCR | `seerctl ocr artifacts/game.png` 稳定返回 12 个中文文本框 |
| 局部像素坐标 → 屏幕点击 | `click_local` 公式正确；日志中多次成功点击底栏/面板 |
| 修改窗口尺寸/位置 | AX API 路径可用 |
| 发送键盘/中文输入 | `keyboardSetUnicodeString` 路径存在，但**未见成功用例**，需真机复核 |

## 4. 从未验证过的能力（不要假定可用）

| 能力 | 状态 |
|---|---|
| 连续 N 场战斗稳定完成 | ❌ 无记录。最后三条日志均为失败/停止。 |
| 战斗状态可靠分类 | ⚠️ 仅靠 `当前回合`/`胜利`/`失败` 关键词，遇到"回合切换动画中"会误判 |
| 点击失败检测 | ❌ 完全没有。点了没反应 = 继续盲点 |
| 窗口失焦后的输入可靠性 | ❌ 未测 |
| 后台/被遮挡窗口截图 | ❌ 未测（当前方案依赖前台 + `screencapture`） |
| 多显示器 | ❌ 未测 |
| 不同窗口尺寸下的坐标自适应 | ❌ 写死 scale=2.0 与固定几何 |
| 长时运行稳定性/内存 | ❌ 未测 |

---

## 5. 复用矩阵（Round 1+ 的执行依据）

| 模块 | 决策 | 理由 |
|---|---|---|
| `seerd` 窗口查找 + AX 固定 | ✅ **直接复用** | 逻辑正确，已验证 |
| `seerd` 的 `clicklocal` 坐标换算 | ✅ **复用并加校验** | 公式对，但需加 scale 运行时检测 |
| `seerd` 的 `shot`（screencapture 子进程） | 🔄 **替换为 ScreenCaptureKit** | 139ms 里大头是进程启动；且 SCK 是 macOS 15 唯一选择 |
| `seerd` 的 `ocr` | 🔄 **改为 ROI + `.fast` + 模板优先** | 469ms 全图 OCR 只为找两个词，性价比极低 |
| `fast.py` `Driver` | ✅ 保留为兼容层 | 新内核并排引入，不破坏旧工具 |
| `tower.py` 战斗循环 | 🔄 **重写为状态驱动** | 盲 sleep 是单回合最大耗时 |
| `docs/state.md` 坐标 | ✅ **原样沿用，转为配置** | 现场标定成果 |
| `artifacts/*.png` | ✅ **转为测试 fixture** | 唯一离线真值来源 |
| `scripts/seerctl` | ✅ 保留为调试工具 | 不在热路径即可 |
| `play.py` | ✅ 保留为"旧方案基线" | 性能对标用 |

---

## 6. 新内核的引入方式（不重写、不并存两套游戏逻辑）

```
seerauto/                     ← 新增：纯视觉内核（无 LLM 依赖）
  capture.py    ← 抽象：SubprocessScreencapture（旧，能跑）/ DaemonCapture（新，SCK）
  vision.py     ← 模板匹配优先，OCR 兜底
  state.py      ← 数据驱动状态机
  executor.py   ← 状态变化驱动，非 sleep 驱动
  perf.py       ← 分阶段计时（用户硬性要求）
  tasks/battle.py

scripts/        ← 原样保留，仅作为参考实现与调试工具
```

迁移原则：
1. `seerauto` 通过**同一套坐标约定**（game-pixel, scale=2.0）与旧代码互操作。
2. 旧脚本一行不改即可继续工作，回滚成本为零。
3. 只有在新内核于 Replay 集上稳定、且真机单场战斗通过后，才考虑停用 `tower.py`。
