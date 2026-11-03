# 今晚（2026-11-03 21:50 → 明早）的任务安排

> 用户要求：**不要再抢焦点**；23:30 之后必须用"不抢焦点"的方式干活。
> 这份清单先定任务，再动代码。

---

## 0. 硬约束（先记住）

1. **不再调用 `activate`** —— 那是抢焦点的元凶（已从 `live.py` / `act.py` 删掉；`tools/*.py` 里的旧脚本没删，但今晚不用它们）。
2. 用户在 23:30 之前还在用电脑：**这段时间不做任何真实点击**，只写代码/文档/离线测试。
3. 23:30 之后：优先用**后台通道**（`clickpid`，事件直接投递给游戏进程）；只有在后台通道被证否时才回落到前台点击，并在报告里写清楚。
4. 每次真实操作前仍然要：几何复核 + 单一执行权 + 预算 + 可中断。

---

## 1. 任务优先级

| 序 | 任务 | 验收 | 何时做 |
|---|---|---|---|
| A | **证明后台通道是否可用**（`clickpid` + 窗口号戳字段） | 游戏不在最前、光标不动的情况下，点一个按钮，画面出现预期变化 | 23:30 后第一次交互时 |
| B | 如果 A 成立 → 用后台通道跑「练级闭环」，把烈火猴从 34 推到 35/36 | `puni.prepare.level_up` 的谓词成立，或 XP 差降到 0 | 23:30 后 |
| C | 如果 A 不成立 → 记录证据，改用前台点击但**集中在游戏窗口内**，并写清楚"会抢焦点" | 同上 + 一条技术结论 | 23:30 后 |
| D | 找「经验分配器」（视频里能把积累经验灌给精灵的机器） | 面板能打开并读到「积累经验：N」 | 穿插做（不需要点击太多） |
| E | 把 tonight 的机器清单补完（悬停识别） | `docs/MACHINE_MAP.md` 增补 | 穿插做 |
| F | 收尾：提交 + 推送 + 写报告 | `git push` 成功、`docs/REAL_DELIVERY_REPORT.md` 更新 | 结束前 |

---

## 2. 后台通道的技术现状（已经做完的部分）

- `native/seerdx.swift` 新增 `clickpid` / `movepid` / `keypid`：
  用 `CGEvent.postToPid(pid)` 把事件直接投递给**拥有游戏窗口的那个进程**。
- 事件上还戳了两个关键字段（Chromium 靠它们把事件路由到指定窗口，而不是"当前 key window"）：
  - `kCGEventMouseEventWindowUnderMousePointer`
  - `kCGEventMouseEventWindowUnderMousePointerThatCanHandleThisEvent`
- `gamePid()` 取"拥有最大游戏窗口的进程"，**不是**第一个名字匹配的进程
  （`SeerGame Helper (Renderer)` 也含 "SeerGame"，之前 `activate` 打到它上面，
  主窗口就会被推到后面 —— 这就是之前"窗口神秘消失"的真因）。
- `LiveSession(background=True)` 会把点击/移动都走后台上，并且**跳过"必须最前"的检查**。

**还没验证的一件事**：PepperFlash 插件认不认这种进程内投递的事件。
A 任务就是验这个：用画面变化判断，不看返回值（返回值永远 ok）。

---

## 3. 23:30 之后的第一条命令（A 任务）

```bash
# 1) 让游戏窗口不在最前（用户已经睡了，这一步只是构造条件）
# 2) 记录屏幕画面
PYTHONPATH=.pydeps python3 tools/vision_probe.py --capture /tmp/bg_before.png
# 3) 用后台通道点一个已知按钮（例如精灵背包图标 1940,1375）
PYTHONPATH=.pydeps python3 - <<'PY'
import sys; sys.path.insert(0,'.')
from seerauto import capture as C, geometry as G
cli = C.DaemonClient('native/bin/seerdx', timeout_s=30)
w = cli.win(); geo = G.WindowGeometry(origin_pt=G.Point(w['x'], w['y']),
                                      size_pt=G.Size(w['w'], w['h']), scale=2.0)
gp = geo.global_pt_from_game_px(1940, 1375)
print(cli.send('clickpid', x=gp.x, y=gp.y, n=1))
cli.close()
PY
# 4) 再截屏，看精灵背包有没有打开
PYTHONPATH=.pydeps python3 tools/vision_probe.py --capture /tmp/bg_after.png
```

判据：`/tmp/bg_after.png` 里出现精灵面板 → **后台通道可用**（B 任务直接跑）；
否则 → **不可用**（转 C，并在报告里写"PeakFlash 不接收进程内注入事件"）。

---

## 4. 已知的坑（今晚踩的，别再踩）

| 坑 | 结论 |
|---|---|
| `activate` 激活错进程 | 它会挑到 `SeerGame Helper`，主窗口被推到后面 → 已改成"按窗口 pid 激活"且**默认不调用** |
| 窗口"消失" | 有两个原因：①`activate` 打错进程；②用户在操作电脑。**不是崩溃**（无 crash 报告） |
| 客户端一直「版本检测中」 | 是**自动更新**（秋季版），等它自己跑完即可，不是服务端故障 |
| 登录页换皮 | 秋季版按钮叫「登录，开启秋日漫游」，位置不变 (1624,1157) |
| 服务器列表 | 按在线人数排序，1 号服不一定在前 10；`啦公星` 出现时点它的文字即可 |
| 悬停提示 | 必须**分段移动 + 停住**，一次瞬移基本不出提示（已实现 `_move_human`） |
| 纯文字弹窗 | 「恭喜你得到了N个积累经验」没有模板，只能 OCR + 固定按钮位 (1200,980) |
