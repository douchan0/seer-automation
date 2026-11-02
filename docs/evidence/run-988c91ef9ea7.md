# 证据：`run 988c91ef9ea7`（目标图第一次出现 completed 节点）

> 这是 `python3 -m seerauto goal report` 在 2026-11-03 02:31 的输出转录。
> 原始 SQLite（`data/goalrunner.sqlite3`）按设计不入库（含账号状态），
> 需要复现时按下面的命令重跑即可。

```bash
rm -f data/goalrunner.sqlite3 data/goalrunner.sqlite3-*
PYTHONPATH=.pydeps python3 -m seerauto goal start puni --planner deterministic \
    --allow-input --max-minutes 15 --max-battles 0
PYTHONPATH=.pydeps python3 -m seerauto goal report
```

## 报告原文

```
# Goal run `988c91ef9ea7` report

- goal: **puni**
- planner: **deterministic**
- status: **finished**
- stop reason: blocked: no pet can battle (all pets at 0 HP); healing required
- started: 2026-11-03T02:30:04+08:00  updated: 2026-11-03T02:30:38+08:00
- budgets: {"max_actions": 400, "max_battles": 0, "max_errors": 3, "max_loops": 40,
            "max_minutes": 15.0, "max_spend": 0.0}
- stats: {"actions": 7, "battles": 0, "elapsed_s": 33.7, "errors": 0, "loops": 3}
- ai calls: 0 (failures 0, cost 0.0)

## Node states

- `puni.prepare.level_up` -> **blocked**
  - proof: no pet can battle (all pets at 0 HP); healing required
- `puni.prepare.tutor_room` -> **completed**
  - proof: feature 'training.tutor_room.reachable' unlocked (LIVE_VERIFIED)
- `puni.seal.entry_nav` -> **completed**
  - proof: feature 'puni.entrance.reachable' unlocked (LIVE_VERIFIED)
```

## 两次 completed 是怎么判定的

* `puni.seal.entry_nav` 的谓词是 `feature_unlocked("puni.entrance.reachable")`，
  而那个 fact 只有在 `game.goto_puni_entrance` **模板匹配到「勇者之塔」传送门**
  （自匹配 1.00 / 其它场景 ≤0.46）时才由技能写成 `LIVE_VERIFIED`。
* `puni.prepare.tutor_room` 的谓词是 `feature_unlocked("training.tutor_room.reachable")`，
  由 `game.goto_tutor_room` 在 OCR 读到「初级班辅导员/高级班辅导员」后才写入。
* 两个 fact 都不是文档值、不是推断值，是当次运行的观测值。

## 同一次运行里的第三行（负证据）

`puni.prepare.level_up -> blocked` 同样是真机结论：精灵背包 OCR 读到
`4 pets; hp alive=0; level=34; xp_to_next=2573`，运行器因此**拒绝**执行战斗步骤，
把 `next_recovery_action` 记成 `heal_pet` 并干净停机（0 errors）。
按验收规则，这一项记 **FAIL**，不记 PASS。
