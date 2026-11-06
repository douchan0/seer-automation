# session3 — 手动刷塔（2026-11-07 02:15 → 03:50，EFFICIENT 模式）

窗口：win id=3927, 1200×800 pt @ (270,76) → 2400×1600 px（02:15 之后一直没变）。
逐层记录：`live/tower_manual_log3.jsonl`（108 行）。

## 1. 战果（一手 XP 原文合计）
- **48 层有「XIN精灵获得经验：N」原文，合计 241,845 XP**。
- 另有 **46 行 outcome=null / unknown**（我的胜利/结算检测漏检，但层数在推进、HP 上限在涨）→ **这 241,845 是下界，不是总数**。
- 等级：**HP 上限 144 → 180**（每级 +3 → 至少升 12 级；会话开始时 Lv49）。**结束时等级没读成**（最后一次开背包面板未成功，没编数字）。
- PP：火焰车每层 -1，**到 0/25 后游戏自动改用「抓/火花」继续打，无提示、不卡住**（今天再次验证）。
- 速率：单层 12–25 s（含结算链），偶发 40 s（守卫者换位置需要换候选点）。

## 2. 失败（2 次，均为全队阵亡）
- 共同特征：**火焰车 PP 已 0** + 连续多层，HP 被磨到 0 → 弹「很遗憾！这场较量你输了」→ 确认 → 「很遗憾，这次挑战未能获胜。你可以重新进入试炼之塔继续挑战。」→ 确认 → **回太空站大厅**（底栏出现），**层数不重置**（对话框计数仍是最高层）。
- 证据：`live/tower_defeat3.png`、`live/tower_defeat2_*.png`、log3 里 2 条 `outcome:"defeat"`。

## 3. `--recover-only` live 验证（**跑了 2 次，结果完全一致**）
```json
{"ok":false,"mode":"recover-only","steps":[{"step":"observe","ok":true,"reason":""},{"step":"heal","ok":true,"reason":"hp_after=[{'hp': 180, 'hp_max': 180, 'px': (842, 737)}, ...]","bottom_bar":true},{"step":"travel","ok":true,"reason":"tutor_room"},{"step":"dialog","ok":false,"reason":"tower dialog not found after travel"}],"screen_text":["| 公屏聊天 • 在线","系统：尊享入场|人修罗 登录了","目","xin计划（1.0.8.9）","8"],"heal_ok":null,"heal_unavailable":false,"floor_picked":null}
```
**结论：恢复链路 4 步里 3 步 live 通过（observe / heal 回满 / travel 到训练室），只有最后一步 `dialog` 失败。**
**关键对照**：同一时刻我在同一个画面上**手动单击 (1195,1150) 一次，对话框立刻打开**（`选择楼层`+`继续挑战`+`你当前到达第 25 层` 全部读出，`live/handover_dialog_*.png`）。
→ 所以不是"打不开对话框"，而是 **tower_grind 的 dialog 步骤用的点击坐标/时序不对**（很可能是 calibrated 的 dialog 坐标不是在训练室量的，或它在房间加载动画期间就点了）。修它只需要：travel 之后**用 OCR 找「选择楼层」再决定点哪**，或对 (1195,1150) 做 2–3 个候选点重试 + 每次间隔 ≥2 s。

## 4. 交接状态
**训练室 + 塔对话框已打开**（`你当前到达第 25 层` / `选择楼层` (1484,887) / `继续挑战` (1481,1007)），
**HP 180/180**（recover-only 的 heal 刚回满），窗口 3927 @ (270,76)，frontmost=SeerGame，无其它弹窗。
这正是 `tower_grind` 的前置条件（"start at the tower entry dialog or already inside"）。
