# USER GUIDE — 在你这台 Mac 上运行 SeerAutomation

> 面向"我要用它"的人。工程规则见 `AGENTS.md`，架构见 `docs/GOAL_RUNNER.md`，
> 这一轮的交付验收见 `docs/REAL_DELIVERY_REPORT.md`。

---

## 0. 30 秒版本

```bash
cd seer-puni-quest

./scripts/bootstrap_mac.sh                       # 一次性：装依赖 + 编译原生守护进程 + 自检

# 1) 只看不动（绝不产生任何点击）
PYTHONPATH=.pydeps python3 -m seerauto goal start puni --planner deterministic --dry-run

# 2) 真正开始推进（会产生鼠标点击，见 §安全）
PYTHONPATH=.pydeps python3 -m seerauto goal start puni --planner ai \
    --allow-input --max-minutes 60 --max-battles 12 --max-spend 0

# 3) 随时查看 / 暂停 / 恢复 / 停止 / 出报告
PYTHONPATH=.pydeps python3 -m seerauto goal status
PYTHONPATH=.pydeps python3 -m seerauto goal pause
PYTHONPATH=.pydeps python3 -m seerauto goal resume --allow-input
PYTHONPATH=.pydeps python3 -m seerauto goal stop
PYTHONPATH=.pydeps python3 -m seerauto goal report
```

`--max-spend 0` 是默认值：**不允许花任何货币**。要买东西必须显式放开并人工确认。

---

## 1. 一次性准备

| 步骤 | 命令 | 说明 |
|---|---|---|
| 依赖 | `./scripts/bootstrap_mac.sh` | 装到 `./.pydeps`（本机用户级 `pip install` 会被沙箱拒绝） |
| 原生守护进程 | 同上（内部调 `tools/build_seerdx.sh`） | `native/bin/seerdx`：ScreenCaptureKit 抓帧 + CGEvent 输入。**macOS 15 已移除 `CGWindowListCreateImage`，SCK 是唯一路径** |
| 自检 | `PYTHONPATH=.pydeps python3 -m seerauto doctor` | 会如实报告"屏幕录制权限 / 辅助功能 / 游戏窗口"三项，缺哪项说哪项 |
| 游戏 | 手动启动 `/Applications/SeerGame.app` 并登录 | 本工具**不会**替你登录、不会绕过客户端校验 |

首次运行系统会要求授予：**屏幕录制**（抓帧）与**辅助功能**（点击）。没有这两项，程序会明确报错而不是静默空转。

---

## 2. 两种模式

### 2.1 `--dry-run`（默认安全模式）

* 抓帧、识别、读 OCR、跑规划器、写报告；
* **不发送任何鼠标/键盘事件**（`LiveSession` 的输入闸门在 `allow_input=False` 时直接抛 `InputRefused`）；
* 干跑会明确告诉你**它打算做什么、缺少哪个观测**，例如：

```
[runner] planner refused: nothing schedulable: ...
[runner] unblocked by observing: puni.prepare.level_up (state prerequisite
         'state.active_pet_level_known' not satisfied: no active pet observed)
```

这句话的含义是："我现在不能规划练级，因为**还没观测过精灵等级**；观测一下就能解锁。"
未知就是未知，不会被当成 false。

### 2.2 `--allow-input`（真实执行）

必须同时满足：

1. 命令行显式给 `--allow-input`；
2. 拿到执行器互斥锁（`/tmp/seerauto.executor.lock`）——**同一个游戏窗口永远只有一个执行者**；
3. 每次点击前重新校验前台窗口与窗口几何（窗口被移动 > 4pt 就拒绝点击并报 `WindowMoved`）。

真实执行受四个预算同时约束：`--max-minutes` / `--max-battles` / `--max-actions` / `--max-errors`。

---

## 3. 命令详解

### `goal start <goal> [options]`

| 参数 | 默认 | 说明 |
|---|---|---|
| `--planner` | `deterministic` | `deterministic`（本地，无网络）/ `ai`（OpenAI 兼容网关）/ `bridge`（把请求写成文件，由人/上位 Agent 回答） |
| `--dry-run` | 关 | 只看不动；与 `--allow-input` 互斥 |
| `--allow-input` | 关 | 允许真实输入；不给就是干跑 |
| `--max-minutes` | 60 | 墙钟预算 |
| `--max-battles` | 12 | 战斗次数预算（由战果验证后计数） |
| `--max-actions` | 400 | 点击上限 |
| `--max-spend` | 0 | 允许消耗的货币上限；0 = 一分不花 |
| `--max-errors` | 3 | 连续失败上限，超了安全停下 |
| `--db` | `data/goalrunner.sqlite3` | 运行记录（可换成自己的路径） |

### `goal status`

读 SQLite 里的最新一次运行：状态、预算、已观测世界（场景/精灵等级/SPT 进度）、
节点状态与证据、最近事件。

### `goal pause` / `goal resume` / `goal stop`

* `pause`：写 `/tmp/seerauto.pause`。运行器在**下个观测周期**停止动作；
  恢复时会**重新观测**（暂停期间游戏仍在自行推进，沿用旧假设会出错）。
* `stop`：写 `/tmp/seerauto.stop`。当前技能结束后写 checkpoint 并干净退出。
* `resume`：两种语义，命令会自己说明用的是哪种：
  * 进程还活着 → 删掉 pause 文件；
  * 进程已退出 → 用**同一个 `run_id`** 从最后一个 checkpoint 重新开始，先重新观测再行动。

### `goal report`

把一次运行写成 Markdown（默认 `data/reports/goal-<run_id>.md`）或 `--format json`，
包含：节点状态、证据、最后一次观测的世界快照、checkpoint 列表、事件日志、AI 调用与费用。

---

## 4. AI 规划器怎么配

AI 只做**高层决策**（选哪个节点、为什么、期望看到什么），不做逐帧识别、不产生坐标。

```bash
export SEERAUTO_AI_BASE_URL="https://opencode.ai/inference/go/openai/v1"
export SEERAUTO_AI_API_KEY="..."          # 只放环境变量，永远不要写进仓库
export SEERAUTO_AI_MODEL="deepseek-v4.1-flash"
export SEERAUTO_AI_SESSION="..."          # 该网关要求的 x-opencode-session
export SEERAUTO_AI_MAX_CALLS=20           # 本次进程最多调用次数
export SEERAUTO_AI_TIMEOUT_S=45
```

* 调用有**超时、次数上限、费用记录**（`ai_calls` 表 + `goal report` 里的 `ai calls` 行）；
* 返回必须是严格的 JSON 契约，否则拒绝执行（`PlanError`）；
* **不发送任何截图**，只发世界摘要 / 节点摘要 / 能力清单；
* AI 不可用（无 key、401、超时）时：**降级到本地 deterministic 规划器**并记录原因，
  本地执行能力照常工作。

没有可用 AI 时，`--planner deterministic` 是完整可用的（默认就是这个）。

---

## 5. 安全

1. **服务器公告**：登录页「系统公告」写明本服**不允许第三方软件、脚本或变速工具**。
   本项目只做"正常截图 + 正常 GUI 输入"，不注入内存、不修改客户端、不绕过任何限制；
   是否使用由你决定并承担风险。若你认为该公告禁止本用法，请**不要**加 `--allow-input`。
2. **AI 不碰像素**：模型输出是 `{selected_node, capability, reason, expected_observation, max_actions, max_duration_s}`，
   坐标永远来自本地模板/OCR 的即时匹配。
3. **不可逆操作需人工确认**：购买、消耗货币/道具、放生精灵、任何永久选择都要求 `requires_approval`，
   而 `--allow-input` 之外还有 `--max-spend` 双保险。
4. **单一执行权**：一个游戏窗口同时只允许一个执行者（文件锁）。
5. **随时急停**：`goal stop` 或 `touch /tmp/seerauto.stop`；进程收到 SIGINT 也会写 checkpoint 再退出。
6. **上传**：默认不上传任何截图。AI 规划器只发送结构化文本。
7. **账号信息**：仓库里不再包含邮箱/QQ 号（已脱敏，见 `docs/REAL_DELIVERY_REPORT.md` §安全）。

---

## 6. 出问题时先看哪里

| 现象 | 先看 | 常见原因 |
|---|---|---|
| `capture unavailable` | `python3 -m seerauto doctor` | 没给屏幕录制权限 / 游戏没开 |
| `game window: NOT FOUND` | 同上 | 客户端没启动，或窗口被最小化 |
| `input disabled (dry-run ...)` | 命令 | 忘了 `--allow-input`，或同时给了 `--dry-run` |
| `window moved/resized ... refusing` | `goal status` | 你手动挪了窗口；重启一次运行即可（几何会重新读取） |
| `another run already owns the executor` | `goal status` | 上一次没退干净：`goal stop`，或删 `/tmp/seerauto.executor.lock` |
| `planner refused: nothing schedulable` | `goal status` 的 `next` 段 | 只剩"未观测"这一种阻塞：先跑一次真实观测 |
| 点不动、画面没反应 | `goal status` 的事件 | 面板没关（残留弹窗），或窗口不在最前 |

日志：`data/live_run.log`（如果按本文档的方式重定向），
运行记录：`data/goalrunner.sqlite3`（`events` 表是最细的真相）。

---

## 7. 目录里什么能删

| 路径 | 能不能删 |
|---|---|
| `data/goalrunner.sqlite3` | 能（会丢历史进度） |
| `data/reports/` | 能 |
| `.pydeps/` | 能（用 bootstrap 重装） |
| `native/bin/` | 能（bootstrap 重新编译） |
| `artifacts/` | **不要删**，是回放测试与证据 |
| `seerauto/templates/live/` | **不要删**，是识别的基础 |
