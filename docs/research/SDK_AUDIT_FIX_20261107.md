# DSH 离线修复记录 — CX-SDK-AUDIT-20261107-01 / -02

**状态**：`WAITING_FOR_USER_START`。游戏输入保持暂停（`/tmp/seerauto.master.pause` 与
`/tmp/seerauto.stop` 全程在位，未创建、未删除、未修改）。本记录描述的工作**全部离线**：
只读源码、离线测试、故障注入；没有启动/登录/激活/截图/点击客户端，没有运行任何真机
runner，没有改全局配置，没有上传任何截图。

- **基线与隔离**：从 origin/main 的 `ecd33ca2f39b048193a91d2ffacdfbda7f6be616` 另建
  worktree `.sdk-fix/repo`，分支 `fix/sdk-audit-20261107`；原夜间工作区保留不动。
- 审计材料（只读）：`.codex-sdk-audit/docs/research/20261107-sdk/{AUDIT,TOWER_REVIEWED,
  PUNI_REVIEWED,ACCEPTANCE,LIVE_ACCEPTANCE}.md`。
- 基线复核：`905 passed / 3 skipped`（43.72 s，与此前审计一致）。
- 真机验收由 Codex 的 Computer Use 独占完成（`LIVE_ACCEPTANCE.md`：入口写 4、地面 28→29、
  七张结算内容不同、同一位置多次确认、火焰车为客户端代选）。**这是 GUI 验证，不是 runner 的
  真机 PASS**；本记录不据此声明任何 runner 真机通过。

## 1. 修了什么（按审计优先级）

### P1-A 单次输入边界（`tools/tower_live.py`）

新增 `ActionGuard`：**每一次派发**都按顺序复检

1. 用户的 **stop / pause** 标记（路径可注入，离线测试用 `tmp_path` 中的假标记，绝不碰真实 `/tmp` 控制文件）；
2. **任务预算**（派发次数 + 已用时间）；
3. **窗口身份**（id；probe 提供 pid 时也比对）与**几何**（原点/宽高，容差可配，默认 2 pt）；
4. **前台**（top layer-0 窗口 owner 必须是 SeerGame）。

拒绝即抛 `InputRefused`，写入 `guard.history`（`input_refused` + 检查项 + 原因），**不派发**。
`DaemonSession.click_px` 在发送前必经 `guard.allow()`；`ensure_frontmost(expected_id=…)`
由"窗口存在即成功"改为**身份比对**。仍保留发送前的几何复检（双保险）。
本 runner 只有前台通道：**后台失败不会自动退回前台**（代码里没有后台分支）。

### P1-B 结算确认（`seerauto/skills/tower.py::TowerSettlement`）

- **冻结弹窗不得二次确认**：同一弹窗在确认一次后若画面无变化，直接返回
  `status="unverified"`、`frozen_popups>=1`、`closed is False`；
- **点击被拒/异常**：`status="failed"`、`rejected_clicks++`、`closed is False`；
- **OCR 故障**：`status="unverified"`、`ocr_failures++`、`closed is False`（不再把空 OCR 当"已关闭"）；
- **身份门槛**：只有确实像结算 UI（XP 行 / 胜利 / 奖励词）才允许点，孤立的「确认」子串不再触发点击；
- **一次一弹窗**，且必须**观察到场景变化**才推进；延迟从该弹窗**首次可见帧**开始计时；
- `SettlementResult` 增加 `status/frozen_popups/rejected_clicks/ocr_failures`，`closed` 仅在
  `status=="closed"` 时为真。

### P1-C 回合统计（`TowerBattleLocal`）

- 只有"**输入被接受 + 抢在倒计时前 + 观察到回合效果**"三条同时成立才计入
  `active_success_turns`；`clicked=False` 计入 `rejected`，**不计**主动出招；
- **同回合不得重点击**：记录上一回合号，若回合号未变则拒绝再次点击（未知则比较战报指纹，
  指纹不变同样拒绝）；
- 掉血/击杀这类"没有下一回合"的效果视为可观察效果（胜利/失败紧接点击）；
- `BattleOutcome` 新增 `attempts/delivered/rejected`，与 `active_success_turns` 分开。

### P2 跨层（`tools/tower_live.py`）

- `FLOOR_RE` 从两位改为 **`\d{1,3}`**（100+ 不再被拒）；
- `read_floor_stable()`：**连续两次读数一致**才采信；
- `one_battle` 的跨层判定改为**严格 `after == before + 1`**，且**与 XP 无关**：XP 原文单独记录，
  仅凭 XP 不再判 PROGRESS；不是稳定 +1 一律 `UNVERIFIED_ACTIVITY` 并在原因里写明结算状态；
- 当前帧找不到守卫 → `BLOCKED`（不扫固定坐标），输入被拒 → `BLOCKED`。

## 2. 离线验证

```sh
cd /Users/ceontou/Documents/code/seer-automation-night/.sdk-fix/repo
export PYTHONPATH=/Users/ceontou/Documents/code/seer-automation-night/.pydeps
/Users/ceontou/.pyenv/versions/3.10.13/bin/python3 -m pytest -q tests/test_tower_local.py
/Users/ceontou/.pyenv/versions/3.10.13/bin/python3 -m pytest -q
```

- `tests/test_tower_local.py`：**10 passed**
- 全量：**904 passed / 1 failed / 3 skipped**（82.85 s）。唯一失败是
  `tests/test_executor.py::test_tick_p95_stays_inside_16ms_frame_budget`——**负载敏感的计时用例**，
  当时 `vm.loadavg=8.45`（8 核），单独重跑同样失败；与本补丁无关，基线审计运行时负载为 3.1–3.8。
  该结论是"负载导致的计时用例失败"，不是功能回归，也没有据此做任何性能声明。
- 故障注入（可由测试复现）：见 §3 与两份独立复核测试文件。

## 3. 独立复核（并行离线子 agent）

| agent | 范围 | 产出（自有文件，不改生产代码） |
|---|---|---|
| A | P1-A 输入边界 + P1-B 结算 | `tests/test_tower_guard_review.py` |
| B | P1-C 回合统计 + P2 跨层 | `tests/test_tower_turn_review.py` |

两个子 agent 均为**离线**：不开 GUI、不跑真机 runner、不碰真实暂停标记、不写 git。
主会话负责复核、集成与唯一一次提交。

## 4. 未验证项（明确）

1. **没有任何真机 runner 验证**：`tower_live.py` 的 dry-run 也会打开采集守护进程，因此本轮
   连 dry-run 都没有跑；本轮全部结论来自离线测试与模拟。
2. **C（每层按当前帧定位）仍未完成**：`GuardianFinder` 在真实第 2/第 3 层帧上仍返回 0 候选
   （需要在用户说"开始"后用真实帧重标定；颜色不能作为敌我判据，已按审计改正）。
3. **结算链与倒计时的真实时序**未验证：本地计时/一次一确认的行为只有单测；
   Codex 的 GUI 验收里火焰车是客户端代选，不能当主动出招证据。
4. **100+ 楼层**只有正则单测，没有真实三位楼层样本（审计亦列为待验证）。
5. **故障恢复**（掉线、黑帧、runner 中断）未构造；本记录不声明已覆盖。
6. 未修改 `seerauto/cli.py`、`seerauto/vision.py` 或 CLI 回放测试（按交接要求，由 Codex 单独修）。
7. 未推送、未合并到 main；提交仅存在于本地 `fix/sdk-audit-20261107`。

## 5. 服务器规则

登录页公告「本服不允许使用第三方软件、脚本或变速工具」仍然有效；本轮为离线代码与测试，
未使用任何规避手段，也不构成服务方许可。

## 6. 测试等级的黄金标准（2026-11-07 追加，用户裁定）

**任何"测试通过"，最终都必须在真实客户端（真机）验收通过后才算数。** 离线/单测通过只
是里程碑，不是完成：

| 等级 | 本轮实际 |
|---|---|
| UNIT_PASS（离线） | 本分支 69 个塔专项 + 全量 964 passed（含本补丁的 3 个塔文件）都是这一级 |
| REPLAY_PASS | ecd33ca 基线回放（audit evidence/replay.txt）；本补丁未新增回放样本 |
| LIVE_PASS | **仅 Codex Computer Use 的 GUI 记录**：入口 4 / 地面 28→29 / 七张结算 / 一次战斗 —— 这是唯一算数的 pass，且**火焰车属客户端代选，主动出招一项仍然 FAIL** |

结论（诚实版）：本轮离线修了 7 个缺陷、影子验收（两子 agent + 错误注入）全绿、真机
路径与停机逻辑齐备，但**没有任何一项获得真机 PASS**。等用户说「开始」后，验收顺序：
`tower_live.py --dry-run` → 单场（A 看首击是否 <10s + PP 证据、B 结算延迟并抄 XP 原文、
C 守卫者识别）、每场之后必须把"地面层数 === 上一场+1"和"结算确认链 closed"同时满足
才算 PROGRESS。**离线全绿 ≠ 真机通过**。
