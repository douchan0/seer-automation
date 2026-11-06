# 双模式自治实验报告 — 2026-11-06 夜 → 11-07 早

> 状态：**进行中（骨架，数据增量填入）**。所有数字必须可复验；来源写在括号里。
> 时区 Asia/Shanghai。硬截止 2026-11-07T07:00:00+08:00，交付 07:30。

## A. Executive Summary
（待填：真实完成了什么 / 没完成什么 / 离谱尼目标近了多少）

## B. 双模式对照
（待填：EFFICIENT vs MAX_RESULTS 的时间、Token、有效成果、任务难度、故障、人工介入）

## C. Token Efficiency Refactoring
（待填：实现了什么、改了哪些文件、活跃上下文是否真的缩短、逐请求证据）

## D. MAX_RESULTS 高强度探索
（待填：试过哪些路线、发现什么、哪些假设被证伪、哪些阻碍被突破）

## E. 游戏真实进展
（待填：等级/塔层/阵容/交易行/封印/真身/资源的 before→after + 证据）

## F. 长程自治能力
（待填：Goal/Supervisor/执行器/Watchdog 实际状态；跨轮续跑是否成立；人工介入）

## G. 故障与教训
（待填：失败、缓存异常、图片问题、客户端冻屏、时间损失）

## H. Git 交付
（待填：分支、提交、测试、可合并内容、未完成项、安全边界）

## I. 下一轮决策
（待填：默认模式、转 MAX_RESULTS 条件、最高优先三件事、最短可行路线）

| 14 | **8 倍经验/4 倍学习力：今晚被我们点电池开启并已耗尽**（用户权威陈述），弹窗「本日还未开启」是**误导性文字** | 用户 23:42 原话 + 经验跳变 6360→5830(=530) vs 23:31 战后 52 | 真机+用户 |
| 15 | 塔自动路径坏：`enter_tower` 报「第23层 reached」但点守卫 `no battle started (not in the tower interior?)`，2 轮 BLOCKED | `tower_farm.log`, `tower_ledger.jsonl` #9-#11 | 真机 |
| 16 | `farm_tower_repeat.py` 的重置分支会**重启客户端**，且 stop flag/SIGINT 在该路径不生效（真实缺陷） | `failures.jsonl` control_failure_stop_missed | 真机 |
| 17 | 塔每层 1300 XP 是**进「积累经验」池**而非直接给精灵（11-04 实测记录）；塔是否吃 8x = UNKNOWN | `seerauto/skills/act.py` FarmTower docstring, `DELIVERY_2026_11_04.md:90` | 本仓记录 |
| 18 | 抓宠经济：官方商城「比巴卜·高级捕捉胶囊」400 XIN豆/个，余额 5000 → 最多 12 个；精灵仓库 8×5=40 格/页；「放回仓库」可逆 ≠ 放生 | `artifacts/market.png`, `artifacts/round5/08_pet_storage.png` | 帧证据 |
| 19 | 服务端公告原文：**「本服不允许使用第三方软件、脚本或变速工具」**（登录页） | `live/tower_dialog_2332.png` OCR | 真机 |

---

## 附录 1：本轮已实锤的事实（增量追加，每条带来源）

| # | 事实 | 来源 | 等级 |
|---|---|---|---|
| 1 | 基线测试本轮独立复跑：649 passed / 2 skipped（41.07s） | `python3 -m pytest -q` 23:04 | 本轮实测 |
| 2 | 控制面加入后全量 785 → 795 passed / 2 skipped | infra-eng task-1/task-5 | 本轮实测 |
| 3 | 昨晚基线 202,265,396 tokens / 656 请求 = 308,331/请求；cache 98.46%；surface 414,213 | `session-fb389015` procache + raw log | 本轮实测 |
| 4 | 今晚 root 会话 22:41–23:16：63 请求 / 5.45M / 86.5K 每请求；surface 42K→113K | `model_usage.jsonl` | 本轮实测 |
| 5 | 子 Agent 花费主导：4 个 session 累计 44.06M（vs root 5.45M） | `usage_meter report` 23:16 | 本轮实测 |
| 6 | 定时唤醒有效；持久 Goal 未跨轮（roundsStarted 仍 0） | `goal_probe.json` / 23:00 唤醒 | 本轮实测 |
| 7 | 客户端 1.0.8.9 / win id=3284 / 1200×800pt @(268,44) / 2400×1600px / 角色 hello | `live/BRINGUP.md` | 真机 |
| 8 | 电池 = 8倍经验+4倍学习力；弹窗原文「开启之后中途不可取消」；8 次尝试均被游戏拒绝（BLOCKED） | `live/battery_confirm_dialog_2256.png`, A2 报告 | 真机 |
| 9 | 角色：烈焰猩猩 Lv41，HP 122/122，升级所需 6360，背包 6 只（1 主战 + 5 小蘑菇） | `live/char_panel_2312.png` | 真机 |
| 10 | `tools/fix_startup.py` 缺 `import time` 真机崩溃 → 已修 + 回归测试 | `docs/research/BUGFIX_FIX_STARTUP_20261106.md` | 真机+单元 |
| 11 | 交易行入口 = 商城首 tab；「击败后解锁交易行资格」不是判据（每张 SPT 卡都有） | `artifacts/market.png`, `docs/PUNI_LIVE_PROGRESS.md:33` | 帧证据 |
| 12 | 经验分配器从未在「基地」搜索过（老服四源一致指 基地→基地手册→家具 500 赛豆） | puni-research task-4 | 公开资料(低置信) |
| 13 | 外部模型通道：`tools/mmx.sh`(opencode) 不可用（缺 XDG_STATE_HOME + auth 失效）；`mmx search` 可用 | puni-research | 本轮实测 |
