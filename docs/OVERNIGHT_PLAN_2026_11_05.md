# 2026-11-05 → 11-06 夜间作战计划（23:10 起真机）

> 硬截止：**2026-11-06T06:30:00+08:00**（绝对时间，不允许顺延）
> 工作目录：`/Users/ceontou/Documents/code/seer-automation-night`（分支 `night/20261105-puni-v2`）
> 基线：**本地 `main` (ba83c05)** —— 不是 PR #1 分支，原因见 §0。

---

## 0. 基线与 PR 结论（先说清楚，否则后面全是错的）

| 事实 | 证据 |
|---|---|
| **PR #1 分支基于过期的 `origin/main`** | `origin/pr-1` 的共同祖先 = `365f6df`；本地 `main` 已在其上多 **10 个 commit / +15065 行** |
| PR 分支**缺失**本地已有的真能力 | battle log 解析（`parsers.py`+`battle_watch.py`）、试炼之塔循环修复、真身命数读取、7 个测试文件 |
| PR #1 独有且本地缺失的修复 | `--deadline-at` 绝对时区截止、`night_session.py` 退出码透传、暂停写 checkpoint、resume 保留输入通道 |
| PR #1 自带的**致命 bug** | `tools/supervise.py` 在 `sys.path.insert(ROOT)` **之前** `import seerauto` → 直接 `ModuleNotFoundError`，supervisor 一行都跑不了 |

**结论**：今晚以本地 `main` 为基线新建 `night/20261105-puni-v2`，把 PR 独有的四项修复移植过来
（已提交 `e58a81b`），PR 分支保留为 `night/20261105-puni` 供对照。
该结论需写进最终报告，并由人决定 PR 是关闭还是重基于本地 `main`。

---

## 1. 22:00 之前的准备（已完成，非真机）

| 项 | 状态 |
|---|---|
| PR #1 代码审查 + 全量测试 | ✅ PR 分支 476 passed；本地 main 基线 592→613 passed |
| 甲方验收环境 | ✅ `seerauto doctor` 全绿（窗口找到 / 截屏权限 OK / 内容在渲染） |
| 后台输入探针（§14.2） | ✅ **`BACKGROUND_INPUT_BLOCKED`**，含 A/B 对照（见 `data/overnight/bg_probe.json`） |
| 登录工具误判修复 | ✅ 「综合待命中」不再算"已进世界"；补上按服务器编号进入（真机实测有效） |
| 美术包弹窗 | ✅ 手动关闭成功一次；`login.py::clear_popup` 由 `clickpid` 改为前台点击 |
| 可重复刷塔工作流 | ✅ `tools/farm_tower_repeat.py` + 纯账本 `seerauto/farm/ledger.py` |
| 目标图解释器 | ✅ `tools/explain_goal.py`（回答"为什么 planner 拒绝"） |
| 经验分配器面板解析 | ✅ `parse_exp_allocator`，用视频真帧做 fixture，离线测试通过 |

---

## 2. 当前目标图的真实状态（`tools/explain_goal.py` 输出）

```
43 节点：completed=4  not_executable=29  unknown=8  blocked=4  missing_capability=1
ready now: NONE
已注册能力只有 11 个：battle.repeat / input.click / map.open / observe.* / puni.entrance / spt.navigate / spt.panel
```

**这是今晚最重要的诊断**：Goal Runner 不是"坏了"，是**目标图里没有一个节点可执行**。
矩阵按"卡住节点数"排序：

| 缺口能力 | 卡住节点 | 说明 |
|---|---|---|
| `battle.skill_select` | 13 | 技能格 2×2 坐标已标定，**从未真机点过一次** |
| `battle.switch_pet` / `battle.log_read` | 9 / 9 | log 解析已有代码+104 帧 fixture，但只到 replay 级 |
| `battle.pp_read` | 6 | 面板「使用次数：11/35」可读 |
| `market.buy` | 4 | 依赖交易行解锁 |
| `battle.stage_read` / `battle.item_use` / `battle.hp_read` | 4/4/3 | |

→ 所以今晚主线不是"跑 goal start"，而是**能力真机验证阶梯**：每升一级能力，解锁一批节点。
Goal Runner 在这些能力登记为 `real` 之前会（正确地）一直拒绝。

---

## 3. 夜间执行阶梯（每级都以真机证据结账）

### L1 — 进入可战斗状态（23:10→23:30）

```bash
# 0) 只读确认
./scripts/seerctl wl
PYTHONPATH=.pydeps python3 -m seerauto doctor

# 1) 登录/进世界（前台，仅在没有鼠标使用者时）
PYTHONPATH=.pydeps python3 tools/login.py --timeout 240

# 2) 进训练室 + 读塔层（真机）
PYTHONPATH=.pydeps python3 -m seerauto goal start puni --allow-input --max-minutes 3 --max-battles 0
```
**结账**：画面证据 + `goal status` 的 world.scene / pet。

### L2 — 刷塔连续产出（23:30→01:00）

```bash
PYTHONPATH=.pydeps python3 tools/farm_tower_repeat.py \
  --rounds 1 --max-rounds 30 --max-no-work 2 --max-failures 3 \
  --deadline-at 2026-11-06T06:30:00+08:00
```
**结账**：`data/overnight/tower_ledger.jsonl` 每轮 `floors_won` / `xp_income`；
至少一轮 `PROGRESS` 且画面里层数/经验变化可复核。

### L3 — 找到经验分配器（穿插，1 次 15 分钟）

`docs/MACHINE_MAP.md` 的三个线索，按顺序：
1. 精灵背包**顶部**图标行 `--region 900,440,2350,580 --step 64`
2. 战斗结算界面（先打一场，再对结算画面扫）
3. 未进入过的 `动力室` / `精灵中心`

```bash
PYTHONPATH=.pydeps python3 tools/probe_panels.py \
  --wanted 经验分配器,积累经验 --region 900,440,2350,580 --step 64 --max-clicks 30
```
**结账**：命中帧 `data/overnight/frames/panel_hit_*.png` + `data/overnight/panel_probe.jsonl`。
找到后：`game.give_exp` 技能 → 灌 XP → 复读等级证明上升。

### L4 — 交易行前置（低风险只读 + 可能一次战斗）

- 打开交易行面板，OCR 前置条目原文与进度（`0/3`？确切三项是什么）。
- 不为了"试一下"去打明显打不过的首领；先用现有阵容评估。

**结账**：面板原文截图；`world.unlocked.market` 只有真的可点才算。

### L5 — 能力登记（每级验证完立刻做）

真机通过后，把 `seerauto/goals/puni.yaml` 对应能力的 `verified: unverified` 改成 `real`，
并把证据文件路径写进 `evidence:`。**没真机通过的一律不动。**

---

## 4. 不变的硬规则

1. **单一 GUI 执行器**。任何时刻只有一个进程点游戏。
2. **前台通道会抢鼠标**。只有在确认用户不在用电脑时使用；否则只做只读观察。
3. 后台通道对本客户端**无效**，禁止自动回退前台。
4. 尊重 `/tmp/seerauto.pause` 与 `/tmp/seerauto.stop`；暂停是契约不是失败。
5. 所有"完成"必须写清三级：单测 / 回放 / 真机。只有真机通过才能叫 `LIVE_PASS`。
6. 06:30 前 15 分钟不再开新的长任务，转入结账与报告。

---

## 5. 已知的外部约束（原样保留）

登录页「系统公告」原文写明：**本服不允许使用第三方软件、脚本或变速工具**。
用户已明确表示自行承担账号风险、授权以学习研究为目的的自动化，但这不是服务器许可。
本计划不包含注入、内存读取、封包修改或反作弊规避，只使用正常截图与 GUI 输入。
