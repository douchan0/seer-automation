# 下一次会话的行动手册（2026-11-03 夜 由 Agent 写）

> 目的：玩家/下一个 Agent 打开就能干活，不用重新摸索。
> 所有坐标/技能名都是**这一夜真机验证过**的；不确定的地方明确写"未验证"。

---

## 0. 先确认两件事

```bash
cd seer-puni-quest
PYTHONPATH=.pydeps python3 -m seerauto doctor          # 窗口 / 权限 / 客户端
PYTHONPATH=.pydeps python3 -m seerauto goal status     # 上次跑到哪
```

客户端如果在「版本检测中…」或白屏，先做这个诊断（2026-11-03 02:56 实测**后端宕机**）：

```bash
curl -sS -m 6 -o /dev/null -w "%{http_code}\n" http://119.45.28.34:3000/file
# 000 = 超时（后端没恢复）；版本接口是活的，别被它误导
```

后台守候脚本（每 2 分钟试一次，恢复后自动重开客户端）：

```bash
nohup ./tools/backoff_monitor.sh >/dev/null 2>&1 &
tail -f /tmp/seerauto_backend_watch.log
```

---

## 1. 第一件事：把精灵血量补满（当前唯一硬阻塞）

现状：**烈火猴 0/95、小蘑菇 ×3 全 0/12**。任何战斗都会弹「你没有可出战的精灵哦」。

三条路，任选其一：

| 路径 | 怎么做 | 备注 |
|---|---|---|
| A 体力药剂 | 底部**商城**买「初级体力药剂」→ 精灵背包 → 选中精灵 → 用道具 | 用 XIN豆；需要你点头（Agent 不会擅自买） |
| B 精灵中心 | 去太空站/飞船找护士或恢复装置 | 本项目**没定位到**，你手动一次即可 |
| C 授权购买 | 告诉 Agent「允许花 XIN豆买体力药剂」，再跑 `--max-spend 500` | 硬规则：货币购买需人工确认 |

补满后验证（这一条不用打架就能验证）：

```bash
PYTHONPATH=.pydeps python3 - <<'PY'
import sys; sys.path.insert(0,'.')
from seerauto.live import LiveSession
from seerauto.skills.base import SkillContext, Budget
from seerauto.skills.act import build_act_skills
from seerauto.world.model import GameWorldState
s = LiveSession(dry_run=False, enable_input=True); s.open()
ctx = SkillContext(session=s, world=GameWorldState(), dry_run=False, allow_input=True,
                   budget=Budget(max_duration_s=40, max_actions=4))
r = {k.id: k for k in build_act_skills()}['game.read_pet_bag'](ctx)
print(r.proof)          # 期望看到 hp alive>=1
s.close()
PY
```

---

## 2. 第二件事：把等级从 34 推到 36

已知经济账（真机实测）：

| 项 | 值 |
|---|---|
| 单场经验 | **106 XP** |
| 34→35 需要 | **2573 XP**（≈ 24 场） |
| 每次连战的血量成本 | 约 20 场耗尽全员（所以要分批） |

```bash
# 一批 15~18 场，跑完回血，再 resume
PYTHONPATH=.pydeps python3 -m seerauto goal resume --allow-input --planner deterministic \
    --max-minutes 30 --max-battles 18
```

跑完看 `goal status` 里的 `xp_to_next`（来自「离升级还需经验」）判断还差多少。
到 36 级会自动进化，节点 `puni.prepare.level_up` 由 `pet_level_at_least(…,36)` 判定完成。

**提速的关键**：先开 8 倍经验（下一节），能把 24 场压到 3~5 场。

---

## 3. 第三件事：8 倍经验开关（已定位房间，未定位开关）

已经验证：

* 星图第 **3** 个目的地（橙色图标）= 辅导员房间；
* 房间里有 `初级班辅导员` 与 `高级班辅导员`；
* 点任一 NPC 打开的是「精灵模拟训练」面板（选择课程 / 取消 / 1of1）。

**没验证**：8 倍/4 倍开关本身。视频 417 帧里也没抓到那个面板。

下次的做法（10 分钟）：

```python
# 1) 先去房间
game.travel(destination="tutor_room")
# 2) 点 NPC，把整屏文字 dump 出来，找含「8」「倍」「学习力」「开启」的行
game.click_text(text="初级班辅导员")
# 3) 若面板里有开关，按其文字位置点击并二次确认提示文本变化
```

技能都在 `seerauto/skills/act.py`，可以直接在 python 里调用（见上面那段样板）。

---

## 4. 第四条路（可能直接跳过练级）：经验分配器

视频里 `经验分配器`（`frames/02_spirit_day1/f_00018~f_00053.png`）面板长这样：

```
经验分配器
  序号：008  名字：烈火猴  等级：17  性格：孤独
  升级所需经验值：316
  该精灵增加 ____ 点经验值      [确认]
  积累经验：18744927
```

它就是**把「积累经验」灌进某只精灵**。如果我们账号的积累经验池有足够点数，
**可以直接把烈火猴灌到 36 级，完全绕过 0 血**。

面板长什么样（`frames/02_spirit_day1/f_00019.png` 1280×720，可放大看）：

```
┌────────────────── 经验分配器 ──────────────────[X]┐
│ [精灵头像]   [6 格精灵选择]     序号：008            │
│              ▣ □ □              名字：烈火猴        │
│              □ □ □              等级：17            │
│                                 性格：孤独          │
│                                 升级所需经验值：316  │
│ 该精灵增加 (500) 点经验值   积累经验：18744927       │
│                    [ 确认 ]                        │
└────────────────────────────────────────────────────┘
```

**面板本身是模态的，右上角是一个圆形蓝底 X**（和精灵背包同款按钮样式）。
背景是**太空站内部**（橙色圆顶大厅），所以它是某台"分配器"机器/NPC 打开的，
不是从精灵背包里点的（我今晚试过背包详情面板里所有小图标，都不是它）。

已知：`积累经验` 池确实在涨（战斗后弹「恭喜你得到了 53 个积累经验」）。
未知：这台机器在哪个房间。**下次优先在精灵太空站大厅/发明室里找一台"分配器"机器**，
点开对照上面的面板样式确认。

下次的做法（15 分钟，值得优先试）：

1. 打开精灵背包（模板定位：`live_petbag_icon`）；
2. 选中烈火猴，逐个小图标点一遍，每点一次 OCR 一次，找 `经验分配器` / `积累经验`；
3. 找到后：填数值 → 确认 → 用 `game.read_pet_bag` 复读等级。

---

## 5. 顺带能做的（不需要战斗）

| 任务 | 技能 | 完成判据 |
|---|---|---|
| 谱尼入口 | `game.goto_puni_entrance` | `unlocked.puni.entrance.reachable`（**已完成**） |
| 辅导员房间 | `game.goto_tutor_room` | `unlocked.training.tutor_room.reachable`（**已完成**） |
| 交易行门槛 | 待做：打开商城面板读「交易行」标签是否可点 | 面板原文（P0-01 仍是 unknown） |

交易行如果打开成功，请把面板原文（尤其是"需要击败 N 个首领"之类）记进
`seerauto/goals/puni.yaml` 的 `puni.prepare.market_unlock.completion.args`。

---

## 6. 已知的坑（今晚踩过的，别再踩）

1. **底栏图标会随场景漂移约 100px** → 一律用模板定位（`live_petbag_icon` / `live_map_icon`），不要写死坐标；
2. **星图中央按钮位置稳定**（`(1185,1375)`，模板 `live_map_icon` 跨场景 1.00）；
3. **展开的聊天面板会盖住星图目的地图标** → 先关聊天（X 在 `(2162,587)`）；
4. **「恭喜你得到了N个积累经验」是纯文字弹窗**，没有模板就关不掉 → 已加 `live_confirm_xp2`；
5. **战斗按钮有两套画法** → `live_fight_btn` + `live_fight_btn2` 取最大；
6. **胜利面板下战斗按钮仍可见** → 判定胜负只看「胜利」字形是否消失；
7. **精灵背包关不掉的两种形态**：仓库页 X 在 `(2072,312)`，背包页 X 在 `(1149,506)`。
