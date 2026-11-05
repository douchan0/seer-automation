I'll start by exploring the repository state and the commits under review.
Let me read the files under review in parallel.
Now let me verify several technical claims in the native daemon and skill base.
Let me verify the login classifier's false-positive path against the real captured loading frame.
审查完成。以下是完整报告。

---

# 代码审查报告 — `night/20261105-puni-v2`

**审查对象**：`/Users/ceontou/Documents/code/seer-automation-night`
**基线**：`ba83c05`（local main）→ `HEAD` = `43484e0`
**范围说明**：任务要求"最近 5 个 commit"，但 `ba83c05..HEAD` 实际是 7 个 commit（`e58a81b a1ff0a8 1eecfd2 84c0e60 599bd82 4fde870 43484e0`）。我审查了整段 diff，重点是指定的 5 个文件。

##0. 本次审查实际做了什么（可复现）

| 动作 | 结果 |
|---|---|
| `PYTHONPATH=.pydeps python3 -m pytest -q` | **619 passed, 2 skipped**（测试全绿，但下面大量问题不在测试覆盖范围内） |
| 对 `data/overnight/frames/*.png` 跑真实 OCR（`seerdx ocr`） | 用于复核探针、登录判据、刷塔账本的原始证据 |
| 用真实 OCR 文本喂 `tools/login.py::screen_kind` | 复现"加载画面被判为 world"（见 B-1） |
| 解析已提交的 `data/overnight/plan.json` | 复现"能力缺口统计虚高 ~7 倍"（见 B-9） |
| 读 `native/seerdx.swift` | 确认 `drag`/`type`/`click` 是**全局**坐标语义（见 B-3、B-4） |
| 读 `seerauto/skills/base.py` | 确认 `Skill.__call__` **不执行** `max_duration_s`/`max_actions` 预算 |

未修改任何文件（`git status` 干净）。**真机侧未执行任何点击**（游戏进程当前在运行，不打扰）。

## 1. 结论摘要

| 文件 | BLOCKER | MAJOR | MINOR | 一句话 |
|---|---|---|---|---|
| `tools/bg_probe.py` | 3 | 3 | 2 | 判据可假通过；结论被引用的证据文件**根本不存在** |
| `tools/farm_tower_repeat.py` | 2 | 8 | 3 | 暂停被当成失败；流程本身跑不赢（4/4 轮全败） |
| `seerauto/farm/ledger.py` | 0 | 4 | 2 | 分类靠"有没有备注"决定；守卫可被交替绕过 |
| `seerauto/skills/act.py::GiveExp` | 2 | 3 | 2 | **可能重复灌注**；永远无法满足节点谓词 |
| `tools/login.py` | 3 | 4 | 2 | 加载画面被当"已进世界"；服务器编号会累加 |
| `tools/explain_goal.py` | 2 | 3 | 2 | "建哪个能力最划算"的答案是错的 |

---

## 2. `tools/bg_probe.py` —— 后台输入探针

### B-1（BLOCKER）`tools/bg_probe.py:134-136` —— "焦点未变"可以**空转通过**
```python
evidence["checks"]["frontmost_unchanged"] = (
    before_front.get("asn") == after_front.get("asn")
    and before_front.get("name") == after_front.get("name"))
```
`frontmost()`（L52-68）在异常时返回 `{"error": str(exc)}`——**没有 `asn`、没有 `name`**。两次都失败时 `None == None` 成立 → 该检查**恒真**。也就是说："前台 App 没被抢"这条契约，在 `lsappinfo` 不可用时是**零信息**的，却会写进 JSON 当证据。

**修复**：先断言"测到了"，再比较"没变"：
```python
before_asn = before_front.get("asn"); after_asn = after_front.get("asn")
evidence["checks"]["frontmost_measured"] = bool(before_asn and after_asn)
evidence["checks"]["frontmost_unchanged"] = (
    bool(before_asn and after_asn) and before_asn == after_asn)
```
并把 `frontmost()` 的原始输出/异常原文写进 JSON。

### B-2（BLOCKER）`tools/bg_probe.py:144-153` —— 判决把"前置条件不满足"当成"输入通道被封"
`passed = all([world_before, spt_absent_before, spt_opened, spt_closed_again, frontmost_unchanged, cursor_unchanged])`，任何一项为假都输出 `BACKGROUND_INPUT_BLOCKED`。但这 6 项里只有 `spt_opened` 与"输入是否送达"有关，其余 5 项是**前置条件**（人在不在世界、面板在不在、焦点测没测到、指针读没读到）。所以：
- 客户端停在选服页 → 也报 `BACKGROUND_INPUT_BLOCKED`；
- 窗口不在 1200×800 / SPT 徽标坐标漂移 → 也报 `BLOCKED`；
- OCR 掉字 → 也报 `BLOCKED`。

这三种情况与"后台通道打不进 PepperFlash"毫无关系。判决词应该拆成三态：`BACKGROUND_INPUT_BLOCKED`（SPT 面板**确实**没开，而同一坐标前台**确实**能开）/ `PROBE_INCONCLUSIVE`（前置条件不满足）/ `BACKGROUND_INPUT_LIVE_PASS`。

**修复**：
```python
fatal = {"spt_opened"}                       # 唯一判定"输入是否送达"的项
if not checks.get("world_before") or not checks.get("spt_absent_before"):
    verdict = "PROBE_INCONCLUSIVE"
elif checks.get("spt_opened"):
    verdict = "BACKGROUND_INPUT_LIVE_PASS"
else:
    verdict = "BACKGROUND_INPUT_BLOCKED"    # 只有这一种情况才配得上这个词
```
并把失败项列进 JSON（`failed_checks: [...]`），否则"BLOCKED"不可行动。

### B-3（BLOCKER）`tools/bg_probe.py:96-97, 153-157` —— 被四处引用的证据文件**不存在**
`AGENTS.md:203`、`docs/OVERNIGHT_PLAN_2026_11_05.md:30`、`docs/OVERNIGHT_REPORT_2026_11_06.md:89`、`tools/farm_tower_repeat.py:272` 都写着"证据：`data/overnight/bg_probe.json`"。实测：

```
$ ls data/overnight/bg_probe.json
cat: data/overnight/bg_probe.json: No such file or directory
$ git ls-files data/overnight
data/overnight/panel_probe.jsonl  plan.json  research/...  snapshots/...  tower_ledger.jsonl  tower_state.json
```

JSON 从未生成；`frames/*.png` 也**全部未被 git 跟踪**。也就是说"后台通道是死的"这条**已经成为项目硬事实**（AGENTS.md 5.6）的结论，其证据链是：未提交的本地文件 + 一个从未产出过的 JSON。换个机器、克隆仓库，结论归零。

**修复**：(a) 跑一次探针，把 JSON **提交进仓库**（含 `git rev`、窗口几何、坐标、每步 OCR 文本、时间戳）；(b) 帧文件要么提交（挑 3 张关键的），要么写清"证据在本地 `/data`，不入库"；(c) 在 JSON 里加 `verdict_basis` 字段说明结论基于哪几项。

### M-1（MAJOR）`tools/bg_probe.py:46, 49` —— 标记集选错方向
- `SPT_MARKERS = ("SPT","帕诺","卡兰","太空站左翼","勇者之塔")` **没有**包含已验证的面板标题（`act.py:56` 的 `星际挑战终端 / 挑战入口 / 共73个入口`），却包含了 `帕诺`/`卡兰`（星系名，会出现在地图、聊天、任务文本里）。这会**双向出错**：面板真开了也可能漏判（`SPT` 是 `SPT 挑战` 的子串，但 OCR 少一个字就没了）；别的界面出现"帕诺"则会被判成"面板开了"（假 PASS 的另一条路径）。
- `WORLD_MARKERS` 里含 `综合待命中`——**这正是 `tools/login.py:34-39` 的注释指认的"假世界标记"**（选服页会把世界 HUD 透在弹窗后面）。用它做 `world_before` 前置条件，等于允许在选服页上跑探针。

**修复**：`SPT_MARKERS` 换成 `("星际挑战终端", "SPT 挑战", "共73个入口", "找到73个挑战入口")`；`WORLD_MARKERS` 换成 `("卫兵", "已连接公屏聊天")`，并删掉 `综合待命中`/`公屏聊天`。

### M-2（MAJOR）`tools/bg_probe.py:115-127` —— 只实现了 A 臂，却被当成 A/B 证据
工具只跑后台通道；`ab_fg_00/01/02.png`（我已 OCR：`ab_fg_01` 里是完整的 SPT 面板——`星际挑战终端 / SPT 挑战 / 共73个入口 / 帕诺星系 / 卡兰星系 / 找到73个挑战入口`）是**另外一次手工/别处产生的**前台对照，**不在任何 JSON 里**（无时间戳、无坐标、无过程）。

顺带：`bgprobe_02_panel.png`（工具自己产出的"面板"帧）OCR 结果是 `公屏聊天 • 在线 | 系统：已连接公屏聊天 | 主播纽斯 | XIN先锋队任务 | SAT | 卫兵 ...`——**没有 SPT 面板**，但**多了一个 `XIN先锋队任务`**。也就是说这一帧**内容变了**（`BET` 消失、多了 `XIN先锋队任务`）。所以"后台点击什么也没发生"并没有被这批帧证实——至少有一处画面变化。判决词比证据强。

**修复**：给工具加 `--lane {background,foreground}`，两条臂**用同一段代码**跑同一坐标，把结果写进同一个 JSON 的 `arms: {"background": {...}, "foreground": {...}}`；判决改为"background 未开 且 foreground 开"才成立。同时把 `XIN先锋队任务` 这类意外变化也记进 JSON。

### M-3（MAJOR）`tools/bg_probe.py:121-127` —— 探针可能把客户端留在脏状态
如果 SPT 面板开了而关闭点击没生效，探针直接退出，**不验证面板是否真的关掉、也不做恢复**。下一次真机运行第一件事就是"清面板"（`ensure_world`），而探针恰恰制造了它没清理的东西。

**修复**：末尾加一次"面板是否还在"的检查；若还在，用**同一个** `SPT_CLOSE` 坐标再点一次并复检，仍不成功就写 `probe_left_dirty: true` 并让调用方拒绝继续。

### N-1（MINOR）`tools/bg_probe.py:140` —— `no_key_events_sent = True` 是硬编码的**断言**，不是观测
它不在 `passed` 列表里，但以 `checks` 的形式出现在 JSON 里，看起来像一项检查。改成 `"observations": {"key_events_sent": 0}` 或直接从代码结构断言（并在文档里注明"由实现保证，非观测"）。

### N-2（MINOR）`tools/bg_probe.py:61-65` —— `frontmost()` 里的解析分支是死代码
`for part in info.split('"')` 循环算出的 `name` 随后被 `if "=" in info:` 分支无条件覆盖（实测 `lsappinfo info -only name` 输出为 `"LSDisplayName"="访达"`）。删掉循环，只保留 `split("=",1)[1]` 分支，并加一条针对真实 `lsappinfo` 输出的单元测试。

---

## 3. `tools/farm_tower_repeat.py` + `seerauto/farm/ledger.py` —— 可重复刷塔账本

### B-4（BLOCKER）`tools/farm_tower_repeat.py:312-315, 352` —— 「暂停」被当成失败上报
```python
if pause_requested(control):
    log("PAUSED: no new round starts while the pause file exists")
    ledger.stopped_because = "paused by user"
    break          # ← 直接退出循环，不是"等待"
...
return 0 if s["floors_won"] or s["rounds"] == 0 else 10
```
用户喊「暂停，保存」（AGENTS.md 5.6 的契约：只读重观察 → 写 checkpoint → 状态置 paused → **等待**），这个工具做的是：**直接退出**，不写 checkpoint、不置 paused、退出码 **10**（与"失败"不可区分）、`stopped_because="paused by user"`。

而且暂停只在**轮次之间**检查——一轮最长 240 s（`farm(..., max_seconds=240.0)`），暂停后工具还会继续点击几分钟。

`RoundRecord.interrupted` 字段（`ledger.py:52`）**从未被置 True**（工具 L197-198 只传 `index`/`started_at`），所以 `ROUND_PAUSED` 分支是死代码。

**修复**：
```python
if pause_requested(control):
    ledger.stopped_because = "paused by user"
    write_state(ledger, {"dry_run": args.dry_run, "status": "paused"})
    control.wait_while_paused()          # 阻塞等待，stop 文件可打断
    log("resumed; re-observing before the next round")
    continue                             # 而不是 break
```
退出码分开：`0`=有产出，`10`=失败，**`20`=被用户暂停**（`farm_tower_repeat.py` 与 `tools/night_session.py` 都要透传）。另外在 `one_round` 里检查 `control.paused()`，把被中断的轮次标记 `rec.interrupted = True`。

### B-5（BLOCKER）`tools/farm_tower_repeat.py:299-305, 329-330` —— 工作流本身跑不通，而文档已宣称 ✅
已提交的 `data/overnight/tower_ledger.jsonl` **4/4 轮全败**，错误都是 `guardian click did not start a battle`；第 4 轮的画面帧 `round_004.png` OCR 是 `公屏聊天 • 在线 | 主播纽斯 | xin计划（1.0.8.9） | BET | 卫兵` —— 客户端在**太空站大厅**，不在塔里。

根因：**没有任何一步打开试炼之塔入口**。`FarmTower`（`act.py:1895-1922`）直接点守卫者固定像素 `GUARDIAN=(1205,789)`，而进入塔的 `TOWER_ENTRY=(1195,1150)` 只存在于 `ReadTowerFloor`（`act.py:1781`）里，从未被调用。工具做的 `travel_to_tower(destination="tutor_room")` 去的是**训练室**，不是塔内。

而 `docs/OVERNIGHT_PLAN_2026_11_05.md:33` 写着"可重复刷塔工作流 | ✅"——这是**谎报**。

**修复**（三处）：
1. `one_round` 之前加一步"进塔"：调用 `game.tower_status`（它会点 `TOWER_ENTRY` 并读面板），或新增 `game.enter_tower`；并**用场景判定**确认已在塔内（`s.scene().scene` 或塔面板文字），不确认就**不点守卫者**。
2. `FarmTower.run` 里 `act()`/`_click()` 的返回值**必须检查**（L1910 现在丢弃了 `_click` 的返回值）——输入被拒（dry-run / 非前台）要报"输入被拒"，不能报"点了没开打"。
3. 修好之前，把 plan 文档第 33 行的 ✅ 改成 ❌（或写"未跑通"）。

### M-1（MAJOR）`seerauto/farm/ledger.py:64` —— 轮次分类由"有没有备注"决定
```python
return ROUND_IDLE if not self.notes else ROUND_FAILED
```
而工具**每轮都会追加备注**：`rec.notes.append(f"frame {path}")`（`farm_tower_repeat.py:221`）。于是：
- 任何"打了但没赢"的轮次 → `FAILED`（不是 `IDLE`）；
- 只有连截图都失败、且技能没给 reason 的轮次 → `IDLE`。

`IDLE`（没干活）和 `FAILED`（出错了）这个**模块存在的理由**被抹平了。已提交账本里 4 轮全是 `BLOCKED`（因为有 `error`），但如果技能只是"没赢"（无 error、有 frame 备注），就会全被记成 `FAILED`。

**修复**：`notes` 只做诊断，分类改用显式字段：
```python
# RoundRecord 增加 blocked: bool = False
if self.interrupted: return ROUND_PAUSED
if self.error: return ROUND_BLOCKED
if self.blocked: return ROUND_BLOCKED
if self.floors_won > 0: return ROUND_OK
return ROUND_IDLE
```
工具侧：`rec.error` 只装异常；技能状态非 PASS 时置 `rec.blocked = True`。

### M-2（MAJOR）`seerauto/farm/ledger.py:141-147` —— `min_xp_per_round` 是死参数，"零经验胜利"仍被算作产出
两个分支**代码完全相同**：
```python
if outcome == ROUND_OK and record.xp_income >= self.guards.min_xp_per_round:
    self.no_work_streak = 0; self.failure_streak = 0
elif outcome == ROUND_OK:
    self.no_work_streak = 0; self.failure_streak = 0     # ← 与上面一模一样
```
即"赢了一局但经验没测到"照样清零 no-work 计数。模块文档写的是"a PASS that won nothing is noise, not production"，代码做的是相反的事。

**修复**：让 `min_xp_per_round` 真正生效——`ROUND_OK` 但 `xp_income < min_xp_per_round` 时，`no_work_streak += 1`（进度是真的，但没有**可核验的产出**），并在 `summary()` 里分开报 `floors_won` / `xp_measured` / `xp_unverified`。

### M-3（MAJOR）`seerauto/farm/ledger.py:154-156` —— 守卫可被"失败/空转交替"绕过
```python
else:  # NO_WORK / BLOCKED
    self.failure_streak = 0 if outcome == ROUND_IDLE else self.failure_streak + 1
    self.no_work_streak += 1
```
一轮 `IDLE` 会把 **failure_streak 清零**。于是 `失败 → 空转 → 失败 → 空转 …` 永远不触发 `--max-failures`；`no_work_streak` 虽然在涨，但默认 `--max-no-work 2` 只要求"连续 2 轮无产出"就停——**一次真失败被一次空转洗白**。

**修复**：`IDLE` 不应清零 failure_streak（空转不是"错误被修复了"）。建议两个计数完全独立：`no_work_streak` 只由 `ROUND_IDLE`/`ROUND_OK` 驱动，`failure_streak` 只由 `ROUND_FAILED`/`ROUND_BLOCKED` 驱动，互不清零。

### M-4（MAJOR）`tools/farm_tower_repeat.py:287-298` —— 崩溃后恢复只恢复"序号"，守卫与时间预算全部归零
```python
previous = load_previous_rounds()      # 只用于打印
index = len(previous)                  # 只恢复序号
```
`ledger.rounds` 从空开始，`no_work_streak`/`failure_streak` 全0，`started_at = datetime.now()` → **`--max-minutes` 时间预算在每次重启后重新计时**。于是"崩溃 → 重启 → 再崩"的循环可以整夜空转，**所有守卫都不会触发**（只有绝对 `--deadline-at` 能兜住）。

**修复**：从 JSONL 尾部重建状态：
```python
ledger = TowerLedger(guards=..., started_at=first_record_start or datetime.now())
for r in previous[-20:]:
    ledger.add(RoundRecord.from_dict(r))
```
`RoundRecord` 加一个 `from_dict()`；`started_at` 取账本第一条记录的时间。

### M-5（MAJOR）`tools/farm_tower_repeat.py:210-213` —— 账本的"实测经验"结构上恒为 0
```python
rewards = read_rewards(ctx.session)     # 在 farm_tower 返回**之后**才 OCR
rec.xp_income = sum(...)
```
但 `RunBattle._read_reward`（`act.py:903-948`）在胜利后**已经读走并关闭了奖励弹窗**。所以工具这轮 OCR 时弹窗早已消失 → `xp_income` 永远 0 → 汇总行 `xp_measured=0`，只有 `notes` 里那句 `estimate {floors_won * 1300}` 在讲真话。**账本最核心的产出数字是空的**。

实测佐证：`tower_ledger.jsonl` 4 轮 `xp_events: []`、`xp_income: 0`。

**修复**：别再事后 OCR，用技能已经拿到的数据：
```python
rec.xp_events = list((res.after or {}).get("rewards") or [])     # RunBattle 已经解析好
rec.xp_income = sum(float(e.get("xp") or 0) for e in rec.xp_events)
```
（`FarmTower` 目前丢掉了 `RunBattle` 的 rewards，需要在 `act.py:1944-1954` 的 `after` 里透传 `rewards`。）估算值应单独放 `rec.xp_estimate`，绝不混入 `xp_income`。

### M-6（MAJOR）`tools/farm_tower_repeat.py:203` —— `--rounds > 1` 时时间预算被摊薄
`farm(ctx, floors=args.rounds, max_seconds=240.0)`：FarmTower 的 `max_seconds` 覆盖**全部**楼层（L1903 `while won < floors and ... < max_seconds`）。`--rounds 3` 意味着 3 层共享 240 s，而 `RunBattle` 内部单场就可能吃掉 240 s → 第 2 层必然被截断，`floors_won` 少报，且**没有任何提示**。

**修复**：`max_seconds` 按层数放大并显式说明：`max_seconds=240.0 * max(1, floors)`，或改成分层预算（每层一个上限）。

### M-7（MAJOR）`tools/farm_tower_repeat.py:334-337` —— 失败后的"重新寻路"结果被丢弃
```python
if ... and ledger.failure_streak == 2:
    log("two rounds failed in a row; re-travelling before the next attempt")
    travel_to_tower(skills, ctx, args.destination)      # ← 返回值没用
```
重新寻路失败时（正是最需要知道的时候），循环照旧去点守卫者固定像素 → 继续浪费轮次。

**修复**：
```python
ok = travel_to_tower(skills, ctx, args.destination)
if not ok:
    ledger.stopped_because = "cannot reach the tower after a failure streak"
    break
```
并把这次失败计入 `failure_streak`。

### M-8（MAJOR）`tools/farm_tower_repeat.py:240-244, 294-296` —— 默认配置可以整夜空转；宣称的预算不被执行
- `--max-rounds 0`（默认）= 不限轮数，`--minutes 0`（默认）= 不限时长，`--deadline-at` 可选 → **默认调用就是"无限跑到天亮"**。文档虽列了三种停止条件，但没有一条是强制的。
- `Budget(max_duration_s=300.0, max_actions=120)`（L294-296）是**摆设**：`Skill.__call__`（`skills/base.py:253-267`）只校验输入和前置条件，**从不检查 `ctx.budget`**；`SkillSpec.max_duration_s` 也没有任何执行点。

**修复**：
1. 启动时若三者（deadline / minutes / max-rounds）**全为 0**，直接 `ap.error("必须给出 --deadline-at / --minutes / --max-rounds 之一")`；
2. 在刷塔主循环里显式判 `ctx.budget.expired()`（或自建 `deadline` 变量），让300 s 这个承诺真的生效；
3. 顺带修 `skills/base.py`，让 `Skill.__call__` 在 `result.elapsed_s > spec.max_duration_s` 时降级为 FAIL + `escalation="pause"`（全局收益）。

### M-9（MAJOR）`tools/farm_tower_repeat.py:279-282` —— 不取执行器锁，可与 Goal Runner 双开
工具直接 `LiveSession(...).open()`，**没有** `InstanceLock`（`runner/control.py:101-139`）。AGENTS.md 5.5.4 明确："同一个窗口不要同时跑两个采集守护进程，第二个的 `shot` 会失败"。两个进程各开一条 `seerdx` + SCStream → 互相抢流、互相抢鼠标。

**修复**：
```python
from seerauto.runner.control import InstanceLock, LockHeld
try:
    lock = InstanceLock().acquire()
except LockHeld as exc:
    log(f"REFUSED: {exc}"); return 7
```

### M-10（MAJOR）`tools/farm_tower_repeat.py:200-230` —— dry-run 往生产账本里写"假失败"
dry-run 下 `click_px` 抛 `InputRefused`，但 `FarmTower` 丢弃了 `_click` 的返回值（L1910），于是 round 记录写成 `BLOCKED: farm_tower FAIL: guardian click did not start a battle` —— **这是对游戏状态的虚假陈述**（游戏根本没被点）。已提交的账本第 4 轮就是这样一条（`tower_state.json` 里 `"dry_run": true`）。

**修复**：`RoundRecord` 增加 `dry_run: bool` 字段并落盘；dry-run 时写独立的 `tower_ledger.dryrun.jsonl`，且 summary 里明确区分。配合 B-5 的第 2 点（检查 `_click` 返回值），dry-run 应报"输入被拒"，不是"点了没反应"。

### N-3（MINOR）`tools/farm_tower_repeat.py:239-240` —— `--rounds` / `--max-rounds` 命名极易混淆
`--rounds` = **每轮打几层**（默认 1），`--max-rounds` = **最多几轮**（默认 0 = 不限）。两者只差一个词。建议改名 `--floors-per-round` / `--max-rounds`，并在 help 里写"层 vs 轮"。

### N-4（MINOR）`tools/farm_tower_repeat.py:325-328` —— 恢复后"首轮/末轮"配对失效
`first_or_last = (index == 1 or (args.max_rounds and index == args.max_rounds) or ...)`：`index` 是**全局**序号（恢复时从 `len(previous)` 起），`args.max_rounds` 是**本次进程**的预算 → 恢复后 `index == args.max_rounds` 几乎不可能成立，"末轮读层数"永远不触发；`index == 1` 也永不成立。

**修复**：用**进程内**轮次计数（`local_rounds`）判断首/末，与全局 `index` 分开。

### N-5（MINOR）`seerauto/farm/ledger.py:66-72` —— 层数读数允许负值
已提交账本第 1 行：`"tower_floor_before": 10, "tower_floor_after": 1, "floors_gained": -9`。VLM 读错楼层是已知问题（`act.py:1846` 记录过"把 14 读成 7"），但账本把 **-9 当测量值发布**，没有任何合理性检查。

**修复**：`floors_gained` 在 `after < before` 时返回 `None` 并在 notes 里记"层数读数回退，判为无效"，或至少加 `if gained < 0: return None`。

---

## 4. `seerauto/skills/act.py::GiveExp` —— 经验灌注技能

### B-6（BLOCKER）`seerauto/skills/act.py:2050-2057` —— **可能一次点击变两次（重复灌注）**
```python
after_panel, _ = self._panel(ctx)
if after_panel is None:
    # Only clear a dialog when the panel actually disappeared. ...
    _dismiss_dialogs(ctx, limit=1)
```
`parse_exp_allocator` 判定"面板在不在"的**唯一**依据是标题 `经验分配器`（`parsers.py:1214`）。只要这一帧 OCR 丢了标题（`min_conf=0.2`，标题字小），`after_panel` 就是 `None` → 进入 `_dismiss_dialogs` → 该函数在**全屏**搜 `CONFIRM_WORDS = ("确认","确定","我已知晓","我知道了","关闭")`（`act.py:75`）并点击**第一个**匹配项（L1037-1046）。如果那个 `确认` 是**经验分配器面板自己的确认按钮**（面板只是被弹窗遮了一角、或 OCR 只读到了按钮没读到标题），就会**再灌一次**——而输入框里的金额还在。

也就是说：**"面板标题没读到" → 点自己的确认 → 第二次灌注**，这是题目里担心的"一次点击变两次"的真实路径。代码注释只防住了"面板完全消失时误点自己确认"，没防住"面板还在但标题没读到"。

**修复**（三选一，建议1+3）：
1. **记录并保护按钮位置**：按下 `确认` 之前记下它的坐标；事后若要 dismiss，**跳过距离该坐标 < 40 px 的任何 `确认`**；
2. **判定"面板还在"不要只看标题**：改成"标题 **或** `该精灵增加`/`积累经验` 任一在" ⇒ 面板还在 ⇒ **禁止**任何 `确认` 点击；
3. **把输入框清空/归零后再确认**，使第二次点击无金额可灌（灌注后显式清空输入框并复读验证）。

### B-7（BLOCKER）`seerauto/skills/act.py:2065-2067` —— 技能永远满足不了自己节点的完成谓词
```python
ctx.world.facts["pets.active.level"] = Fact(level_after, Evidence.live(...))
```
但节点 `puni.roster.exp_pour`（`seerauto/goals/puni.yaml:718-737`）的完成条件是：
```yaml
completion:
  type: verified_account_state
  predicate: feature_unlocked
  args: {feature: exp_giver.poured}
```
`feature_unlocked`（`predicates.py:221-239`）读的是 `world.unlocked["exp_giver.poured"]`（或 `world.facts["unlocked.exp_giver.poured"]`）——**没有任何代码写这个键**。我全仓 grep：`world.unlocked[...]` 只有 `unlocked` 的读取方；`facts["pets.active.level"]` 只有 `act.py:2065` 这一处写入，**没有任何谓词读它**。

后果：即使真机成功灌注，Goal Runner 也**永远不会**把这个节点判为完成 → "谱尼节点完成数仍为 0" 的老问题在这一节点上复现，而且原因在代码里是确定的。

**修复**：由**观察者**写解锁标记（`predicates.py:224-226` 的注释明确要求"the flag is written by an observer, never by the skill"）。即新增 `game.read_exp_allocator`（OBSERVE）读面板并写：
```python
ctx.world.unlocked["exp_giver.poured"] = Fact(True, Evidence.live("经验分配器: 等级 X->Y, 积累经验 A->B"))
```
或者让 runner 在校验节点时把技能的**已验证观测**（面板读到的 level/pool 前后值）提升成 `unlocked`。同时把 `pets.active.level` 改成 yaml 约定的 `world.pets[<名字>].level`，或删掉这条没人读的写入。

### M-3（MAJOR）`seerauto/skills/act.py:2047, 2085-2096` —— 确认前没有校验"这个确认属于经验分配器"
`_click_text` 在**全屏**按 needles 顺序找第一个含 `确认` 的 OCR 项，不限定面板区域、不核对它是否与面板同区。面板被别的浮层盖住时，可能点到别人的 `确认`。

**修复**：
```python
ok, why = self._click_text(ctx, ("确认","確定","确定"),
 region=(add_box.x0 - 40, add_box.y0 - 400, add_box.x1 + 400, add_box.y1 + 200))
```
即限定在"金额行右侧、面板标题下方"的范围内找按钮，并在点击前**同一帧**确认 `该精灵增加` 与 `点经验值` 两个 caption 仍然可见（否则面板已被别的层盖住 → 拒绝点击）。

### M-4（MAJOR）`seerauto/skills/act.py:2060-2076` —— "池子少了但等级没升"也算 PASS
```python
rose = (level_before is not None and level_after is not None and level_after > level_before)
spent = (pool - pool_after) if (pool_after is not None) else None
if rose or (spent is not None and spent > 0):
    ... status=Status.PASS
```
经验灌过头（超出本级需求、被客户端吞掉）→ 等级不变、池子变少 → **判 PASS**，reason 写"pool fell (level read missing)"。但 yaml 里解锁语义是"灌注过一次**且等级确实上升**"。等级没升的灌注不是产出。

**修复**：分成两档——等级上升 → PASS；仅池子下降 → `Status.FAIL`（reason: 经验被消耗但等级未上升，可能超出单级上限）。并把 `level_before/after` 的缺失与"没上升"区分开写 reason。

### M-5（MAJOR）`seerauto/skills/act.py:2019-2021, 2042-2046` —— 单次上限未知 ⇒ 节点写死的 `amount: 1000000` 必然失败
节点写死 `skill_inputs: {amount: 1000000}`（视频里的上限，非本服确认值，yaml 自己也注明了）。若本服单次上限更小，客户端会把输入框夹到上限 → 复读得到 `pending_amount != want` → 技能 **FAIL**："typed 1000000 but the panel reads 316; not pressing 确认"。**每次都失败，且不会自动降级**。

**修复**：把"复读不等于 want"从硬失败改成**自适应**：
```python
if typed is not None and 0 < int(typed) < want:
    want = int(typed)          # 面板自己夹了上限 → 接受它并继续
elif typed is None or int(typed) != want:
    return SkillResult.fail(...)   # 真的没打进去，才失败
```
并在 `after` 里回传"本服单次上限 = typed"，供后续调用直接使用。

### N-6（MINOR）`seerauto/skills/act.py:2040` —— 输入框不清空就打字
`client.send("type", text=str(want))` 之前没有全选/清空。输入框若已有内容（面板默认值、上次残留），会**拼接**（"500" + "1300" = "5001300"）。读回校验会挡住灌注（好），但每次都白跑一轮。

**修复**：点击后先发 `cmd+a`（或 `key` code 0/ 全选），再 type；或在读回不符时先清空重试一次。

### N-7（MINOR）`seerauto/skills/act.py:1985-1986` —— spec 的 `max_duration_s/max_actions` 无执行者
与 M-8 同源：`Skill.__call__` 不查预算，spec 里的120 s / 8 actions 无人执行。技能内部也没有自查。至少在 `GiveExp.run` 内部加一句 `if ctx.budget.expired(): return SkillResult.fail(...)`。

---

## 5. `tools/login.py` —— screen_kind / confirmed_world / enter_server_by_number

### B-8（BLOCKER）`tools/login.py:41, 215-223` —— **加载画面被判为"已在世界"**（已用真帧复现）
```python
PRIMARY_WORLD_MARKERS = ("卫兵", "当前回合", "XIN计划，启动", "已连接公屏聊天")
```
我用 `seerdx ocr` 读了**这个工具自己 19:50 拍下的** `data/overnight/frames/server_entered.png`，OCR 结果是：
`公屏聊天 •离线 | 综合待命中 | seer.61.com | xin计划（1.0.8.9） | XIN计划，启动！ | 76% | 正在加载星球资源`
→ 含 `XIN计划，启动` → `screen_kind(...) == "world"`。

实测：
```
server_entered.png (loading, 19:50)  -> screen_kind='world'
world_01.png                         -> screen_kind='server'
```
`confirmed_world` 要求的"两次间隔 1.5 s 的观测"在加载期间（76% → 更高）**两帧都会命中** → 工具 `return 0`，日志写"confirmed in the world"——**而客户端还在加载星球资源**。这与 2026-11-05 那次"误报已进世界"是**同一类**缺陷，只是触发串换成了 `XIN计划，启动`（它是客户端的启动/加载文案，不是世界标记）。

**修复**：
1. 从 `PRIMARY_WORLD_MARKERS` 删掉 `XIN计划，启动`（它是 splash/加载文案）；
2. `当前回合` 是**战斗 HUD** 字符串，也不能当世界标记（删掉，或改成"仅在非战斗场景接受"）；
3. 新增一个优先级更高的 `loading` 分类：`("正在加载", "正在加载星球资源", "%")`——并让 `screen_kind` **先**判 loading（modal 之外的最高优先级），`confirmed_world` 遇到 loading 直接返回 False；
4. `confirmed_world` 改成"两次都必须是 world **且** scene 判定为 WORLD"，不要只靠字符串。

### B-9（BLOCKER）`tools/login.py:176-180` —— 滚动条拖拽用的是**窗口内坐标当全局坐标**
```python
session.client.send("drag", x1=SCROLLBAR_X, y1=SCROLL_TOP,
                    x2=SCROLLBAR_X, y2=SCROLL_BOTTOM, steps=18)
```
`SCROLLBAR_X = 2023`、`SCROLL_TOP/BOTTOM = 640/1000` 是**窗口内 game_px**（`act.py` 的 `ICON` 同空间）。但 `native/seerdx.swift:406` 的 `drag()` 直接把这两个数当 `CGPoint` 发 `cghidEventTap` —— **全局屏幕点**。

窗口在 (270,87)、1200×800 pt、scale 2 ⇒ 目标全局坐标约 (270+1011, 87+320) = **(1281, 407)**，而代码发的是 **(2023, 640)**，偏右 ~740 pt、偏下 ~230 pt：拖拽落在**窗口之外的桌面/别的 App** 上，真实移动并拖拽了用户的鼠标。

**修复**：所有 `client.send("drag"/"click")` 必须先过坐标转换（和 `LiveSession.click_px` 一样）：
```python
gp = ctx.geo.global_pt_from_game_px(SCROLLBAR_X, SCROLL_TOP)
session.client.send("drag", x1=gp.x, y1=gp.y, x2=gp.x, y2=gp.y +180, steps=18)
```
更稳的做法：把 `drag` 收进 `LiveSession`（`drag_px(x1,y1,x2,y2)`），从根上杜绝窗口内/全局混用。**注意 `type`（`seerdx.swift:443`）也是全局的**，`GiveExp` 和 `enter_server_by_number` 都用它——全局键盘事件会打到当前焦点 App；`click_px` 有前台校验兜底，但仍建议在文档里写明"打字必须前台"。

### B-10（BLOCKER）`tools/login.py:200-212` —— 服务器编号会**累加**，可能进错服
```python
session.click_px(box["px"][0] + 120, box["px"][1], "focus server number field")
time.sleep(0.7)
session.client.send("type", text=str(number))
...
session.click_px(go["px"][0], go["px"][1], f"quick-enter server {number}")
```
输入框**没有清空、没有全选、没有回读校验**。主循环在 `kind == "server"` 时每轮都会重跑 `enter_server_by_number`（L258-270），所以字段内容会变成 `"1"` → `"11"` → `"111"` …… 点 `快速进入` 时传的是 `111`，不是 `1`。

**修复**：
```python
session.client.send("key", code=0)          # 或 cmd+a 全选
time.sleep(0.2)
session.client.send("type", text=str(number))
time.sleep(0.9)
# 回读字段内容，确认就是目标编号再按快速进入
field_text = _read_field(session, box)
if field_text.strip() != str(number):
    log(f"server number field shows {field_text!r}, not {number}; refusing to enter")
    return False
```
并在 `enter_server_by_number` 里把"每次调用都清空"作为前置条件。

### M-6（MAJOR）`tools/login.py:137-162, 277-278` —— 登录按钮仍可能点错（老 bug 未根治）
```python
click_text_or(session, ("登录，开启", "登录", "账密登录"), LOGIN_BTN, "log in", region=LOGIN_REGION)
```
`find_text` 的 needles 顺序是 `("登录，开启", "登录", "账密登录")`，而匹配是**子串**匹配：`"账密登录"` **包含** `"登录"`，所以 needle `"登录"` 会先命中 `账密登录`（一个**标签页**），根本轮不到更具体的 `"账密登录"`，而真正的按钮文案是 `"登录，开启"`（若 OCR 少读一个字变成 `"登录,开启"`，就完全匹配不上）。这正是注释里写的"点了4 次错误位置 (1294,744)"的根因——**加 region 没有解决 needle 顺序问题**。

**修复**：优先精确匹配，再退子串：
```python
hit = session.find_text(inside, ("登录，开启"), exact=True) \
   or session.find_text(inside, ("开始游戏", "进入游戏")) \
   or session.find_text(inside, ("账密登录",), exact=True)
```
并且**排除** `"账密登录"/"忘记密码"/"前往注册"` 这类非按钮项（它们同样含"登录"）。

### M-7（MAJOR）`tools/login.py:87-128` —— 弹窗用前台全局点击，与 `act.py` 的实现不一致，且理由被夸大
`clear_popup` 用 `client.send("click", ...)`（**全局 HID**，会抢焦点、移动用户鼠标），注释写"the PID lane (`clickpid`) provably does not reach this client"；但同一仓库的 `DismissStartupPopup`（`act.py:1705`）对**同一个**美术包弹窗用的正是 `clickpid`。而探针证明的只是"PID 打不进**画布里的 SPT 面板**"，不是"打不进原生弹窗"——注释把结论扩大了。

**修复**：统一走 `game.dismiss_startup_popup`（`clickpid`，不抢焦点）；或至少把注释改成"探针只证明了画布不可达，原生弹窗未测"。AGENTS.md 5.6 明确"不要在无人值守时依赖 `tools/login.py` 的前台登录去抢焦点"。

### M-8（MAJOR）`tools/login.py:282` —— 超时时不报告屏幕上到底有什么
`log("timed out before reaching the world")` 之后直接 `return 4`。一个新出现的界面（更新提示、顶号提示、验证码）只会让工具空转 180 s，最后给一句无信息的话。

**修复**：保留最后一次 `texts` 与一张帧，超时时打印/落盘：
```python
log(f"timed out; last screen kind={kind!r} texts={texts[:12]}")
shot(session, "timeout")
```

### N-8（MINOR）`tools/login.py:40` —— `WORLD_MARKERS` 是死代码且内容有害
文件里定义了 `WORLD_MARKERS`（含 `公屏聊天`、`当前回合`）却从未使用（`screen_kind` 只用 `PRIMARY_WORLD_MARKERS`）。它还留着本模块自己注释里点名的两个"假世界标记"。删掉，避免下一个人误用。

### N-9（MINOR）`tools/login.py:168` —— 服务器名别名是子串匹配
`aliases = (name, name[-2:], "公星")` + 子串匹配，可能命中"啦公星A"这类别的行。至少改成 `exact=True` 优先、子串兜底。

---

## 6. `tools/explain_goal.py` —— 诊断工具的统计

### B-11（BLOCKER）`tools/explain_goal.py:83-107` —— "建哪个能力最划算"的数字**错了约 7 倍**
工具对**每个节点**（不论状态）都调用 `graph.unmet()` 并把里面的 `missing capability` 原因计入 `capability_gaps`（L84-87）。于是：
- 已 `completed` 的节点、`not_executable`（按设计就禁用）的节点、`blocked` 的节点，只要 `unmet` 里含能力缺失，就都进"能力缺口"表；
- 同时被"前置节点未完成"挡住、**又**缺能力的节点也被计入——但**只补上能力并不会解开它**。

我用已提交的 `data/overnight/plan.json` 复算：

| 工具输出（`plan.json`） | 实际 |
|---|---|
| `battle.skill_select` blocks **15** | 只有 **2** 个节点是"仅缺这个能力"；另 13 个还缺前置节点，其中 **12 个是 `not_executable`**（按设计禁用，补能力也不会跑） |
| 全部能力缺口合计 **78** 个节点 |真正的"仅缺能力"节点只有 **16** 个 |
| `by_status` 里 `missing_capability=1` | 与它自己的 "15 / 11 / 10" 互相矛盾 |

这个数字已经被抄进 `docs/OVERNIGHT_PLAN_2026_11_05.md:50-56`（"缺口能力 | 卡住节点"，`battle.skill_select` 写 13），并据此决定了"今晚主线是能力真机验证阶梯"。**这个结论指向了错误的工作量**。

**修复**：区分"卡住"和"只卡在这"：
```python
for r in rows:
    caps = [x for x in r["unmet"] if x.startswith("missing capability ")]
    others = [x for x in r["unmet"] if not x.startswith("missing capability ")]
    if r["status"] in ("completed", "not_executable", "blocked"):
        continue                       # 这些节点本来就不会跑，不该进缺口表
    for c in caps:
        cap_blockers[name].append(r["id"]) if not others else multi_blockers[name].append(r["id"])
```
输出两列：`blocks`（连带被挡）与 `unblocks`（仅此一项，补上即可跑），并把 `unblocks` 作为排序依据。`by_status` 同理应按"首要阻塞"归类，而不是"unmet 里出现过什么"。

### B-12（BLOCKER）`tools/explain_goal.py:30-41` —— 一个自称"从不碰游戏"的工具，会**再开一条采集流**
`collect()`走 `open_runner(cfg, ...)`，而 `open_runner`（`runner/runner.py:1222-1227`）**无条件**执行：
```python
runner.session = LiveSession(dry_run=config.dry_run, enable_input=..., background=...).open()
```
`LiveSession.open()`（`live.py:188-216`）会 `DaemonClient.start()` → **再拉起一个 `seerdx` 进程**并在同一个游戏窗口上开一条 SCStream。工具自己却不用这个 session（只用 `runner.store`）。

AGENTS.md 5.5.4 明确警告"同一个窗口不要同时跑两个采集守护进程，第二个的 `shot` 会失败"。也就是说：**在夜间真机跑着的时候执行 `tools/explain_goal.py`，可能把正在跑的那条流搞坏**——而这个工具的使用场景恰恰就是"跑不动了，来看看为什么"。

**修复**：不要用 `open_runner`。直接：
```python
from seerauto.goals.graph import GoalGraph
from seerauto.world.store import WorldStore
graph = GoalGraph.load()
store = WorldStore(db)
world = store.latest_world()
```
（`completed_nodes` 需要 `run_id` 才能读历史 node_states —— 见 M-10。）这样诊断工具**零副作用**。

### M-9（MAJOR）`tools/explain_goal.py:84-93` —— 靠字符串前缀解析原因，解析不了就**静默消失**
`reason.startswith("missing capability ")` 等三段前缀硬编码在 `graph.py:324/336/344` 的 f-string 里。任何一次措辞调整 → 全部原因都不匹配 → `capability_gaps` 为空 → 工具打印"没有能力缺口"（**假全清**）。

**修复**：把 `unmet()` 改成返回结构化对象（`Unmet(kind="capability", ref="battle.skill_select", note=...)`），工具按 `kind` 聚合；同时**统计并打印未分类的原因**，让"解析不了"变成可见的失败而不是空表。

### M-10（MAJOR）`tools/explain_goal.py:43-44, 62-67` —— 用**没有时间戳**的快照下"ready now"的结论
`world.json` 里 `observed_at` 是**空字符串**（`plan.json` 里 `"observed_at": ""`），而工具照样打印 `ready now: [...]`。`runner.completed_nodes(world)` 在 `run_id=None` 时**不读历史 node_states**（`runner.py:227-230` 需要 `self.run_id`），所以"上次跑完的已完成节点"在这个报告里全部丢失。

**修复**：
- 世界快照为空或 `observed_at` 为空 → 打印 `world_age=UNKNOWN`，并把 `ready` 列表改名为 `ready (按快照 <时间>)`；快照缺失时直接拒绝输出 `ready`。
- 让 `collect()` 接受 `--run-id`（或读库里最近一次 run），把该 run 的 `node_states` 一起纳入 `completed`。

### M-11（MAJOR）`tools/explain_goal.py:114, 153-156` —— 报告被静默覆盖，且没有版本戳
默认写 `data/overnight/plan.json`，每次运行覆盖，没有时间戳/提交号/世界快照 id。而仓库里提交的 `plan.json`（21:38）已经和 `docs/OVERNIGHT_PLAN_2026_11_05.md:42` 里引用的数字（"43 节点" vs实际 46）对不上。

**修复**：默认写**带时间戳**的文件（`plan-YYYYMMDD-HHMM.json`），同时保留一个 `plan.latest.json`；并在文件里加 `generated_at` / `git_rev` / `world_run_id` / `world_observed_at`。

### N-10（MINOR）`data/overnight/snapshots/.../explain_goal.txt` —— 提交的"证据"是一句报错
该文件内容是 `python3: can't find '__main__' module in '.../tools'`——快照脚本把 `python3 tools` 当成可执行文件跑了。同时 `tools/overnight_snapshot.py` 本身未被 git 跟踪（快照里的 `git.txt` 显示 `?? tools/overnight_snapshot.py`）。

**修复**：快照脚本改成 `python3 tools/explain_goal.py --json <tmp>`，并把脚本入库；快照失败时**不要**把报错当证据提交。

---

## 7. 跨文件的系统性问题（不在单个文件里，但必须一起改）

| # | 问题 | 证据 | 建议 |
|---|---|---|---|
| S-1 | **"证据"没有入库**：仓库里被文档引用的 `bg_probe.json` 不存在，`frames/*.png` 未跟踪，`explain_goal.txt` 是报错 | `git ls-files data/overnight` | 所有结论性证据（JSON + 关键帧 + 提交号）入库；未入库的证据在文档里必须标"仅本地" |
| S-2 | **窗口内 / 全局坐标混用**：`client.send("drag"/"click"/"type")` 直接收 game_px | `seerdx.swift:406/443`；`login.py:176-180` | 把 `drag`/`type` 收进 `LiveSession`（`drag_px`），只允许坐标转换后下发 |
| S-3 | **预算不执行**：`Skill.__call__` 不查 `ctx.budget`，`SkillSpec.max_duration_s` 无执行者 | `skills/base.py:253-267` | 在 `__call__` 里超时降级为 FAIL+pause；工具循环显式判 `budget.expired()` |
| S-4 | **没有执行器锁**：只有 Goal Runner 取 `InstanceLock`，`farm_tower_repeat.py` / `login.py` / `bg_probe.py` 都不取 | `runner/control.py:101` | 所有会驱动窗口的工具统一取锁 |
| S-5 | **失败被当成"没干活"**：`_click` 返回值被丢弃（`act.py:1198/1910`），dry-run 与真机失败在账本里长得一样 | `tower_ledger.jsonl` 第 4 轮 | 检查 `_click` 返回值，把 `InputRefused` 与"点了没反应"分成两种状态 |
| S-6 | **观测层缺失**：`world.unlocked[...]` 没有任何写入方，导致 `feature_unlocked` 类谓词永远无法满足 | `grep -rn "unlocked\[" seerauto/` | 明确"观察者写解锁标记"的接口，否则所有 `verified_account_state` 节点都是死节点 |

---

## 8. 建议的修复顺序

1. **先止血（会毁证据/会误报成功）**：B-3（证据入库）、B-8+B-9+B-10（登录三条）、B-12（诊断工具零副作用）
2. **再修"会重复花钱/重复动作"**：B-6（重复灌注）、B-7（谓词接线）
3. **然后修"会整夜空转"**：B-4（暂停语义）、M-4（恢复守卫）、M-8（默认无界）、M-1/M-2/M-3（账本分类与守卫）
4. **最后修"统计与叙述"**：B-11（能力缺口统计）、M-5（xp 恒为 0）、M-3（塔入口流程）、M-6（确认按钮定位）

**总体评价**：这 5 个文件的**结构与注释质量明显高于平均水平**（每个数字都标了出处、每个失败都写了原因、默认档位偏保守），但**注释里的结论比代码验证的结论更强**，这正是 AGENTS.md 5.3 要防的事：探针把"没打开面板"写成"后台通道被封"，登录把"加载中"写成"已在世界"，诊断把"连带被挡"写成"卡住"，刷塔把"4轮全败"写成"✅ 工作流"。建议下一轮把"证据文件必须入库 + 判决词必须与证据一一对应"当成硬性验收项。

---

**未做/未验证的部分（诚实标注）**：
- 所有结论均为**静态审查 + 离线复算**，未在真机上重跑探针/刷塔/登录（游戏进程当前在运行，不做前台打扰）。
- `bg_probe.py` 的"前台臂"我没有实机跑过；`ab_fg_01.png` 的 OCR 是我离线读的既有帧。
- `GiveExp` 的"重复灌注"路径是**代码级可达性分析**（未真机复现）；`parse_exp_allocator` 的标题漏读概率无法从离线 fixture 估计。
- 仓库里 `docs/OVERNIGHT_REPORT_2026_11_06.md`（提交 `a3d578d`，在基线侧）也引用了 `bg_probe.json`，但按任务范围未纳入逐行审查。