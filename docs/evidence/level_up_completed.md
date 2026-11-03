# 目标节点完成：`puni.prepare.level_up`（Lv34 → Lv36 并进化）

**时间**：2026-11-04 02:21（Asia/Shanghai）
**证据**：`artifacts/round6/level36_pet_panel.png`、`data/goalrunner.sqlite3`、`data/supervisor.log`

---

## 结果

| 项 | 之前 | 之后 |
|---|---|---|
| 精灵 | 烈火猴 | **烈焰猩猩**（已进化，序号 009） |
| 等级 | 34 | **36** |
| 体力 | 95 | **108** |
| 攻击 / 特攻 / 速度 / 防御 / 特防 | 66 / 66 / 74 / 53 / 48 | **88 / 88 / 99 / 77 / 69** |
| 技能 | 抓 40 / 瞪眼 0 / 火花 40 / 挑拨 0 | **抓 40 / 火焰车 95 / 火花 40 / 挑拨 0** |
| 目标节点 | `attempted` | **`completed`** |

world 事实（`LIVE_VERIFIED`）：

```
pets.烈火猴.level = 36
session.scene     = WORLD
unlocked.puni.entrance.reachable      = True
unlocked.training.tutor_room.reachable = True
```

图谱求解结果：

```
puni.seal.entry_nav      -> completed
puni.prepare.tutor_room  -> completed
puni.prepare.level_up    -> completed     <-- 本轮目标
```

---

## 怎么打下来的（每一环都有真机记录）

1. **先解决"打不了"**：全员 0 血 → 用悬停识别出的「精灵恢复」按钮（20 XIN豆/次）回满，
   并且发现它**同时恢复 PP**（抓 35/35、火焰车 25/25、火花 24/25、挑拨 20/20）。
2. **再解决"打得慢"**：技能唤醒仪把 瞪眼(威力0) 换成 **火焰车(威力95)**，单场从 56 s 降到 26.8 s。
3. **再解决"选错招"**：战斗面板只有「使用次数」没有威力 → 由视觉模型读一次技能表
   （抓40/火焰车95/火花40/挑拨0），点击仍由本地状态机执行。
4. **再解决"跑不久"**：`tools/supervise.py` 分批跑 + 自动恢复（重开客户端/重登/回血/清弹窗），
   五轮迭代修掉 5 个真机故障后，夜间连续跑通。
5. 结果：`supervisor.log` 记录 13 批，`xp_to_next` 4269 → 4187 → 2491 → 升级 → 36。

## 途中修掉的 5 个真机故障（都会让长程任务停摆）

| 故障 | 现象 | 修复 |
|---|---|---|
| 技能面板标识不是 `PP:` | 读到 0 个技能，永远用第 1 格 | 改读「使用次数」，按固定 2×2 格位识别 |
| 四个招式全部 0 次数 | 战斗不再推进，空转到 150 s 超时 | 检测到全空 → 结束本批 → 回血恢复（HP+PP 一起） |
| 战后奖励弹窗残留 | 下一轮寻怪 20 s 找不到怪 | 每批开始前先清弹窗 |
| 精灵面板 X 移位 | 面板盖住画面，寻怪全失败 | 关闭点改多候选 + 关后复验 |
| 回血检查误判 | 背包读空被当"血满"，每批只打 1 场 | 读不到血量 → 强制回血 |

## 复现

```bash
PYTHONPATH=.pydeps python3 tools/supervise.py --until 06:30 --batch-minutes 12 --batch-battles 16
```

单批：`PYTHONPATH=.pydeps python3 -m seerauto goal start puni --allow-input --max-battles 16`
