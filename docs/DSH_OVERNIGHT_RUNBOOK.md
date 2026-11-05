# DSH 夜间长程任务运行手册（2026-11-05）

> 本文是 **运行契约**，不是“已经通过真机”的交付声明。本分支仅经代码审查；需要在用户的 macOS / DSH / SeerGame 环境执行真实验收。
>
> **一次 DSH 回复、一次 `goal start`、一次测试通过，均不代表顶层“获得谱尼”完成。**

## 1. 为什么曾经在凌晨结束

- `tools/night_session.py` 只发起一次 `goal start`；之前忽略子进程返回码，永远返回 0。
- `seerauto/goalcli.py` 曾无条件返回 0；`tools/supervise.py` 因而误把拒绝规划当成功。
- 上次提交的 `data/supervisor.json` 存在 **26 批 `nothing schedulable`，全部 0 actions / 0 battles**，不能算有效进展。
- `runner.py` 记录过 blocked 后在最终收尾阶段又覆盖成 finished。
- `game.farm_tower` 已实现，但 `puni.prepare.tower_farm` Playbook 仍使用 `game.tower_status`，需要修复与真机验收。

## 2. 宿主是 DSH，不能只依赖一个 Prompt

在 Mac 启动前验证 DSH 当前版本、Profile、持久 Goal、自动续轮或外部守护能力。**不要猜测 CLI 参数名或工具名。** 如果 Headless 在任务结束后退出，应使用已验证的 DSH 长程能力或经用户同意的本地持久调度器；观察其 PID 和心跳证明真的持续存活。

单个 `goal start` 是 **可审计的有限批次**，而非整夜 Agent。处理每批结果：

| Outcome | DSH 处理 |
|---|---|
| PROGRESS | 读取证据、更新目标图并继续最高价值的下一批 |
| UNVERIFIED_ACTIVITY | 检查实际动作和状态，修复观测/验收；不可宣称完成 |
| NO_WORK | 不再重复原命令；转入缺失 Playbook/Skill/Predicate 的工程任务 |
| BLOCKED | 诊断具体环境、权限或状态；切换其他可执行任务 |
| FAILED | 读取 traceback、重现、修复，有限次数重试 |
| INTERRUPTED | 尊重停止请求 |
| DRY_RUN | 只算模拟 |
| DEADLINE_REACHED | 保存检查点，不再派发任务 |

`goal start` 退出码 10/11/12 不表示“环境坏了”，它们表示由 DSH 上层协调器处理的状态。**不要把这些码统一捕获后改为成功。**

## 3. 最高优先级：真实客户端闭环

以前一夜曾在真机上验证入口、训练室、升级和试炼之塔，说明新增十个 Mock 测试的边际收益往往不如修复一个真实阻塞。

按照动态依赖链执行：

1. **环境**：`python3 -m seerauto doctor`；确认客户端路径 `/Applications/SeerGame.app`、主窗口、屏幕录制和辅助功能。
2. **启动**：允许范围内用 `open -a /Applications/SeerGame.app` 启动；实际检查主窗口和画面，不能把 `open` 的退出码当启动成功。
3. **会话**：先复用已有合法登录；需要密码、验证码、系统权限时，不得猜测或绕过；将这条任务标记 HUMAN_REQUIRED，继续代码/研究。
4. **观察**：使用现有 `LiveSession` / `tools/login.py` / `tools/supervise.py`；只信新观测，不信“应该在世界里”。
5. **执行**：选择目前最有价值的可执行目标（塔连续刷、经验分配器、SPT、交易行、精灵阵容）。
6. **证明**：按客户端画面 + 数据库 + 回合日志验证状态变化，保存证据。只有真实闭环称 `LIVE_PASS`。
7. **补给**：用户允许正常游戏币和可再生游戏资源消耗；无资源应分析可获得方式并规划补充。不可推断用户同意真实货币充值或不可逆账户操作。

### 功能验收等级

`IMPLEMENTED`（代码存在）→ `UNIT_PASS`（单测）→ `REPLAY_PASS`（回放）→ `LIVE_ATTEMPTED` → **`LIVE_PASS`**（真实客户端，前后状态证据）。如果受到权限/服务不可用阻止，标记 `BLOCKED`，不要写“已完成”。

本分支的 Supervisor/CLI 修复 **尚未在 Mac 上真机跑过**，现在只能称工程候选改动。

## 4. 睡前授权与风险边界

用户已明确同意，在自担账号风险的理解下，研究和运行游戏内自动化，并允许花费普通可再生游戏币。因此，不必为每次正常补给/刷资源而在午夜请求人工批准。由于项目 `max_spend` 默认 0，DSH 应明确评估是否使用带界限的游戏资源策略，不能通过随意删除引擎安全检查解决；优先区分普通游戏币、不可恢复资源、真实货币。

服务器的现有公告对第三方脚本持禁止态度：**用户愿意承担风险不等于服务器授予许可**。保留这一事实、告知封号风险，不提供反作弊规避；若选择只进行离线或读屏研究，也必须明确记录原因。

允许的预设动作：本地项目改动、Git 工作分支/本地提交、公开网页/B站研究、正常客户端启动、已授权的截图；任何向外部模型上传本地截图/账号信息应另行确认。账号登录凭证、系统 TCC 权限、Apple 安全授权、真实货币支付、不可逆资产处分不可从“夜间自主”推导出许可。

睡前必须在 **实际执行动作的程序** 上验证：
- Screen Recording 权限 + 截图探针；
- Accessibility 权限 + 有条件的输入探针；
- DSH 实际 Profile 的工具、沙箱与审批策略；
- 已启动客户端和登录状态；
- 电源/睡眠管理、执行器独占锁、停止信号；
- 不向公共仓库推送账号快照。

未授予系统权限时不可绕过 TCC、sudo、验证码或审批闸门。阻塞对应任务，保留其他工作流。

## 5. 动态优先级，不做分钟级硬排程

排序原则：`目标推进价值 × 成功概率 × 能力复用价值 / (时间 + 风险 + 资源成本)`。

**P0** 先证实恢复/状态反馈：已修正的 outcome 分类 → 目标图中可调度能力。**P1** 补 `game.farm_tower` 与 Playbook 的接线、补给/PP循环；`game.tower_status` 只是读数，不是刷塔。**P2** 经验分配器 → SPT 首领 → 交易行 → 阵容。**P3** 谱尼封印/真身。每次遇见实测证据都重新排序，拒绝为刷提交数而做大型重构。

**MiniMax 经 OpenCode** 承担最新版本攻略、B站视频检索、图像理解和局部代码；先核实模型 ID、搜索工具与图片传输实际可用。任何声称“观看视频”都需来源 URL 和时间戳；不可获取的标记 UNKNOWN。MiniMax 代码进入独立 Worktree，DeepSeek 审查与合并；游戏 GUI 始终单一执行者。

## 6. 真实运行命令与截止时间

本分支增加了 `tools/supervise.py --deadline-at`，必须传入**绝对时区**：

```bash
# 仅在符合当前使用规则并完成用户与 macOS 授权后运行：
PYTHONPATH=.pydeps python3 tools/supervise.py \
    --deadline-at '2026-11-06T06:30:00+08:00' \
    --batch-minutes 8 --batch-battles 8 \
    --max-no-progress 2
```

Supervisor 在 NO_WORK / BLOCKED / 连续无验证进展时**有意退出**，由 DSH 上层切到工程/研究任务；不要用 `while true` 重新执行它。硬截止只停止新任务，不得再延后 24 小时。

项目默认 `goal --max-spend 0` 与现有 Supervisor 用的独立 `game.heal_pet` 可能采用不同预算路径：**务必对普通货币消费策略和实际账单做单独审计**。不要把“用户愿意花金币”误当已实现可靠的资源余额识别。

## 7. 工程验收与 DSH 夜间交付

运行仓库完整测试套件，另检查：

```bash
PYTHONPATH=.pydeps python3 -m pytest -q tests/test_outcomes.py
python3 -m py_compile seerauto/runner/outcomes.py seerauto/goalcli.py \
    seerauto/runner/runner.py tools/night_session.py tools/supervise.py
```

这只能证明 outcome/代码逻辑；**不能给刷塔、谱尼、客户端恢复授予 LIVE_PASS**。

夜间 DSH 要持续选择独立任务，直到绝对截止或真正全局阻塞。06:30 报告必须包括：
- DSH 实际启用的持久 Goal、轮次、进程和心跳证据；
- 完成节点数、真机画面证据和前后状态；
- 0 动作、NO_WORK、BLOCKED、失败批次数；
- 每项功能当前验收等级；
- MiniMax 真正搜索到的资料和未证实之处；
- 资源消耗及再获取方法；
- 当前最接近谱尼的下一项动作。

不要因为结束一轮而结束顶层目标。不要因为所有已可执行节点完成而宣称谱尼获取完成。
