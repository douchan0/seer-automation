# session4 — 手动刷塔（2026-11-07 03:37 → 04:22，EFFICIENT 模式，提前于 06:00 停）

**停止原因：HP 掉到 7/194（<30% 停止条件）+ 火焰车 PP 0**（不是打不过，是血量阈值触发）。
逐层记录：`live/tower_manual_log4.jsonl`（110 行）。窗口：win id=3927 → 重启后 **4062 @ (270,76)**。

## 1. 战果（一手 XP 原文合计）
- **48 层有「XIN精灵获得经验：N」原文，合计 157,842 XP**（另有 7 行等未读到 XP 文本 → **下界**）。
- 塔计数：进塔时 **第 25 层** → 结束时对话框 **第 26 层**（本轮推进 1 层历史最高）。
- HP 上限 **180 → 194**（又升约 5 级；每级 +3）。
- 火焰车 PP 每层 -1，到 0 后游戏自动改用 抓/火花，**无提示、不卡**（第 4 次验证）。

## 2. 失败（1 次）
HP 磨到 0 + PP 0 → 全队阵亡 → 两个失败弹窗 → 回太空站大厅，**层数不重置**（同前 3 次一致）。

## 3. `--recover-only` live 验证（本轮又跑 1 次，共 4 次）
第 4 次（重启客户端后立刻跑）新增一条**前置条件缺陷**：
```json
{"ok":false,"mode":"recover-only","steps":[{"step":"observe","ok":false,"reason":"popup/marker '确认' is on screen before RECOVER"}],"screen_text":["公屏聊天 •在线","系统：已连接公屏聊天","主播纽斯","卫燕","hello"],"heal_ok":null,"heal_unavailable":false,"floor_picked":null}
```
原因：登录后客户端会弹 **「检测到本日还未开启8倍经验与4倍学习力… [确认]」** 横幅，`recover_steps` 的 observe 步把它当成"未知弹窗"直接拒绝。
→ **修法：observe 步应先按已知安全词（确认/确定）关闭该横幅，再开始 RECOVER。** 我手动点掉 确认 后，heal/travel 两步立刻 PASS（HP 7 → 194/194）。

前 3 次结果（一字不差，均卡在 dialog 步）：见 `live/tower_manual_session3.md` §3。
**根因（已读源码确认）**：`tower_grind.py::recover_steps` 在 travel PASS 后**不做任何点击**，直接 `self._at_dialog(ctx)` 只做 OCR 判断；而对话框必须靠**点塔入口机器 (1195,1150)** 才出现 → 必然失败。
`_open_and_pick` 也直接点 `pick_floor_px`，同样假设对话框已开。
**修法**：travel PASS 后插入 `_click(ctx,(1195,1150))` + 2 s 等待（失败重试 2 个候选点 (1195,1180)/(1230,1150)），再检 dialog markers。

## 4. 交接状态
**辅导员训练室 + 塔对话框已打开**（`你当前到达第 26 层` / `选择楼层` / `继续挑战`）、**HP 194/194**（刚用精灵恢复回满，PP 同步回满）、
窗口 **win 4062, 1200×800 pt @ (270,76)**、frontmost=SeerGame、无其它弹窗。
