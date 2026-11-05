# 真机执行协议（23:10 起，每次照抄）

> 唯一执行器 = 本仓库的 Python 进程。任何时刻只允许**一个**进程点游戏。
> 前台通道会真实移动鼠标；后台通道对本客户端**无效**（`data/overnight/bg_probe.json`）。

## S0 只读预检（不碰鼠标）

```bash
cd /Users/ceontou/Documents/code/seer-automation-night
printf '{"op":"win"}\n{"op":"quit"}\n' | ./native/bin/seerdx
./scripts/seerctl wl                     # 必须只有一个 1200x800 的 SeerGame 窗口
PYTHONPATH=.pydeps python3 tools/fix_startup.py --check   # 有辅助窗口就先处理
lsappinfo info -only name $(lsappinfo front)              # 前台是不是别人在用
```

判据：
- 有 aux 窗口 → `python3 tools/fix_startup.py`（会点一下「暂不导入」）
- 前台不是 SeerGame 且**用户可能正在用** → 不要动，等
- 主窗口停在「版本检测中」 → 同上，先 `fix_startup.py`

## S1 进世界

```bash
PYTHONPATH=.pydeps python3 tools/login.py --timeout 240
```

判据：日志出现 `confirmed in the world`（两次间隔 1.5s 的 `卫兵`/`已连接公屏聊天`，
且**不含**选服/登录/加载标记）。出现 `client is still loading` 属正常，继续等。

## S2 证据快照 + 后台通道 A/B

```bash
PYTHONPATH=.pydeps python3 tools/overnight_snapshot.py --label live_start
PYTHONPATH=.pydeps python3 tools/bg_probe.py        # 两条通道同一坐标各跑一次
```

判据：`BACKGROUND_INPUT_BLOCKED` = 后台无效（预期）；`PROBE_INCONCLUSIVE` = 前置不满足，先修前置。
无论结果，**不得**因为后台失败就自动改用前台——前台只在"确认没人用电脑"时用。

## S3 刷塔（第一批要小，先证明能进塔）

```bash
PYTHONPATH=.pydeps python3 tools/farm_tower_repeat.py \
  --rounds 1 --max-rounds 2 --max-no-work 2 --max-failures 2 \
  --deadline-at 2026-11-06T06:30:00+08:00
```

判据（缺一不可）：
- 日志有 `tower entry probe: floor=<数字>`（= 真的进了塔）
- 至少一轮 `PROGRESS`，`floors_won>=1`
- `data/overnight/frames/round_*.png` 里能看到塔内画面
- `data/overnight/tower_ledger.jsonl` 对应轮次 `xp_income > 0` 或有 `xp 未测到` 的说明

失败分支：
- `cannot reach the tower` → 先 `game.travel` 是否被别的东西挡住；不要连续重试
- `guardian click refused` → 输入被拒（前台/几何问题），查 `ensure_frontmost`
- `landed but no battle` → 守卫者坐标或场景不对，做一次 `probe_panels` 定位

## S4 长跑（S3 通过后）

```bash
nohup env PYTHONPATH=.pydeps python3 tools/farm_tower_repeat.py \
  --rounds 1 --max-rounds 40 --max-no-work 2 --max-failures 3 \
  --deadline-at 2026-11-06T06:30:00+08:00 \
  > data/overnight/farm_long.log 2>&1 &
```

每 10–15 分钟读一次账本，不要盯着屏幕空转。

## S5 经验分配器（穿插，一次 ≤15 分钟）

```bash
PYTHONPATH=.pydeps python3 tools/probe_panels.py \
  --wanted 经验分配器,积累经验 --region 900,440,2350,580 --step 64 --max-clicks 30
```

命中 → 记下坐标 → 用 `game.give_exp` 灌一次 → **用面板自身的 等级/积累经验 变化证明**。

## S6 交易行前置（只读优先）

打开交易行/商城面板，OCR 前置条目原文，写进世界状态。**不为了"试一下"去打明显打不过的首领。**

## 暂停 / 停止

- 用户说「暂停，保存」：`touch /tmp/seerauto.master.pause` →
  `python3 -m seerauto goal pause` → 等当前轮落到安全点 → 快照 → 报告"已保存"。
  **不 kill 进程，不删 checkpoint，不自行恢复。**
- 用户说「开始」：`rm /tmp/seerauto.master.pause` →
  `python3 -m seerauto goal resume --allow-input` → 重观察再动手。
- 全局停止：`/tmp/seerauto.stop`（工具会跑完当前轮再退出，退出码 20）。

## 06:30 前 15 分钟

不再开新长任务；写快照、整理 commit、更新报告。
