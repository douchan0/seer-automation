# 谱尼 Goal Graph · 缺口分析（43 节点 × 当前版本攻略）

> 输入：`seerauto/goals/puni.yaml`（43 节点） × `docs/research/PUNI_CURRENT_VERSION_GUIDE.md`（B 级证据）
> 规则（不可违反）：
> 1. **外部攻略只能证明「机制存在」，不能证明「本账号已完成」**。任何节点状态升级仍只认本机 LIVE 观测。
> 2. 本轮**没有对真实客户端做实时操作**（服务器公告禁止第三方脚本；本夜未获实时授权窗口），
>    因此**本轮没有把任何节点改成 completed**。
> 3. 证据等级不足的结论只写进 `notes` / 本文件，**不改 completion 判据**。

---

## 0. 总览

| 项 | 数值 |
|---|---|
| 节点总数 | **43 → 46**（本轮新增 3 个横切能力节点） |
| 本机 runtime 已完成 | **1**（`puni.prepare.tower_farm`，判据 `tower.floors_cleared=14 ≥ 1`，LIVE） |
| partial | 4（tower_farm / tutor_room / entry_nav / long_battle_watch） |
| todo | 24 |
| unknown | 12 |
| blocked | 4（交易行购买类，缺 `market.buy`） |
| 本轮修正 | 6 个节点的语义/预算/标题 |
| 本轮新增 | 3 个节点 + 4 个 capability |
| 判定为「过时/误导」 | 0 个节点删除，但**2 类外部资料被判为不适用**（见 §5） |

> 说明：yaml 里的 `status` 是**人工维护的声明状态**；运行期真正的完成度由 predicate 读 `GameWorldState`
> 现算（`goal status` 只列出 predicate 成立的节点）。两者不一致时**以运行期为准**。

---

## 1. 逐节点对照（分类）

### 1.1 保留（机制与攻略一致，无需改动）

| 节点 | 为什么保留 |
|---|---|
| `puni.prepare.animation_off` | 攻略明确要求「呼吸模式 + 压缩超长出招动画」，否则长战崩溃 [B: S2 12:46] |
| `puni.prepare.daily_signin` | 双击签到 [B: S2 02:43] |
| `puni.prepare.exp_boost` | 8 倍经验/4 倍学习力每日限时 [B: S2 07:08、09:58] |
| `puni.prepare.daily_exp_pool` | 「找 50 级野怪开战直接逃跑」白嫖 50 万经验 [B: S2 02:21] |
| `puni.roster.catch_pipi` / `catch_jier` | 比波「手下留情」抓宠、吉尔作融合副宠 [B: S2 02:56–03:31] |
| `puni.roster.spt_hamoleite` / `spt_naduolei` / `spt_saiweier` | 三只 SPT 来源与攻略一致 [B: S2 05:16–05:23、07:50] |
| `puni.roster.train_effort` | 训练室一次 +4 [B: S2 04:48] |
| `puni.roster.exp_pour` | 经验分配器灌注 [B: S2 04:20、05:09] |
| `puni.roster.team_compose` | 封印 5/真身要求「7 只全带」 [B: S2 19:22] |
| `puni.seal.entry_nav` | 入口路径已真机验证（A） |
| `puni.crosscut.item_use` | 谱尼战刚需体力/活力药剂 [B: S2 10:58] |
| `puni.crosscut.switch_pet` | 真身/轮回必须换人 [B: S2 19:58–22:10] |
| `puni.seal.element_2` / `energy_3` | 「消强→属性反转→耗PP→烧伤」，2/3 打法相同 [B: S2 12:58] |
| `puni.seal.life_4` | 「猩猩消强 → 哈莫单挑」[B: S2 15:04] |
| `puni.seal.holy_7` | 「三次龙之意志 + 灭碎阵」[B: S2 25:16] |
| `puni.truebody.life_1_vacuum` ~ `life_6_final` | 逐命打法与攻略 §6 完全对应 [B: S2 39:56 文字版第 2 屏] |

### 1.2 修正（机制对，但数值/判据/预算错了）

| 节点 | 现状 | 攻略发现 | 本轮处理 |
|---|---|---|---|
| `puni.prepare.tower_farm` | skill=`game.tower_status`（只读层数） | 只读层数不推进任何东西 | ✅ **已改** skill→`game.farm_tower`；playbook 加连战步骤；`floors_cleared` 语义修正（见 §3.1） |
| `puni.prepare.buy_potions` | `resource_at_least(高级体力药剂, 1)` | 攻略买 **×100** | ✅ **已改** `amount: 100`，并注明「100 是视频数量，本账号余额 UNKNOWN」 |
| `puni.seal.vacuum_1` | 标题写「约 21 回合」 | 视频实测**约 30 回合**（1 分 20 秒）[B: S2 11:16–12:40] | ✅ **已改** 标题 21→30 |
| `puni.seal.eternity_6` | 标题写「打 60 次」 | 「60 次」含义 **UNKNOWN**（回合 or 命中，旁白未说明） | ✅ **已改** 标题标注 UNKNOWN |
| `puni.crosscut.long_battle_watch` | 预算按 165 回合 | 真身全程 **300–600 回合**（视频 2 倍速）[B: S2 33:55] | ✅ **已改** notes + 预算上界 |
| `puni.roster.buy_flash_pipi` | 只要求「存在闪光波克尔」 | **速度必须 ≥ 319** 才能快过第五封印轮回 [B: S1 简介 + S4] | ✅ **已改** notes 写明速度门槛（判据暂不改，缺 `pet_stat_at_least` 谓词） |
| `puni.roster.kelida_awaken` | 单节点：抓克里→克罗达 | 实为 4 步：抓 → 练到 54 → 100 赛尔豆技能唤醒「偏激防守」→ 还原到 1 级 → **验特防=6** [B: S2 26:16–27:46] | ⚠️ **建议拆分**（本轮只加 notes，不拆——拆分需要新谓词 `pet_stat_equals`） |

### 1.3 拆分（建议，未执行）

| 节点 | 建议拆成 | 理由 |
|---|---|---|
| `puni.roster.kelida_awaken` | ①`catch_keli` ②`awaken_kelida`（54 级+技能唤醒）③`reset_kelida`（还原+特防=6 验证） | 三步各自有独立失败模式，合在一起无法定位失败原因 |
| `puni.truebody.life_5_samsara` | ①`life_5a_pp_drain` ②`life_5b_poison` ③`life_5c_sapat` | 它本身是「4 条 HP 循环」，一个节点装不下 4 条命的断点恢复 |

### 1.4 合并（建议，未执行）

| 节点 | 合并到 | 理由 |
|---|---|---|
| `puni.seal.element_2` + `puni.seal.energy_3` | 保留两个 id，但**共用同一 playbook** | 攻略明说两者打法完全相同；分开维护会漂移 |

### 1.5 新增（本轮已加 3 个）

| 新节点 | 类型 | 为什么必须新增 |
|---|---|---|
| `puni.crosscut.attribute_reverse` | capability_demonstrated | **属性反转是整个谱尼路线的核心机制**（6/7 封印、真身 3/5 命都依赖纳多雷），但 43 节点里**没有任何节点或 capability 覆盖它**。没有它，`seal_6/7` 的成功无法归因 |
| `puni.crosscut.pp_drain` | capability_demonstrated | 「耗光谱尼 PP」是全部打法的**共同前提**。现有 `puni.seal.boss_pp_empty` 只做「空 PP 窗口识别」，缺「主动耗 PP」这个动作本身 |
| `puni.truebody.detect_life` | verified_account_state | 真身 6 条命的自动化前提是**知道现在第几命**。客户端会打「第N命（X封印）」（U1）。**2026-11-05 已实现并离线验证（74/74）**，节点状态 todo → partial |

### 1.6 暂时阻塞（保留 blocked/unknown，不硬推）

| 节点 | 阻塞原因 | 解锁条件 |
|---|---|---|
| `puni.roster.buy_salake` / `buy_flash_pipi` / `buy_kaolu` / `buy_nier` | 缺 capability `market.buy`（verified=unverified）；交易行门槛虽已知（击败 SPT 首领），但**从未在本账号观测到交易行可用** | 先做一次真机：打开交易行 → 搜「萨拉」→ 读到价格（**只读，不买**） |
| `puni.prepare.market_unlock` | `feature 'market'` 从未被观测 | 同上 |
| `puni.prepare.buy_potions` | 货币余额/药剂价格本轮都未观测 | 读货币栏 + 商城价格 |
| `puni.seal.*`（7 个） | 缺 `battle.log_read` / 封印面板 UI 采样 | 先采样七封印面板 |
| `puni.truebody.*` | 缺 `battle.long_watch`；命数检测**已实现**（离线） | 先在真身战斗里实时跑一次 `observe_truebody_life` |

### 1.7 已经过时 / 判定为不适用的外部资料

| 资料 | 判定 | 理由 |
|---|---|---|
| TapTap《谱尼全攻略》（2023） | ❌ **不适用** | 是**《赛尔号星球大战》手游**：靠「封印值」累积、每次降 18/21 点、**最快 6 天过一关**；本服是「一场打完即过、每天限 4 次」。精灵池也不同（阿葵丽亚/米瑞斯/雷伊 vs 哈莫/萨帕克/塞维尔） |
| 7k7k《谱尼技能表》（2010） | ❌ 仅作技能名参考 | 15 年前页游资料 |
| 作者文字版「6.圣洁/7.永恒」 | ⚠️ **名称互换** | 画面 23:40=永恒（打 60 次）、25:10=圣洁（反转+单挑）；以画面为准 |

> **没有删除任何节点**：43 个节点里没有一个被证明「机制不存在」。被判定过时的是**外部资料**，不是节点。

---

## 2. 缺失清单（Skill / Predicate / Playbook）

### 2.1 缺失 Skill（攻略要求但代码没有）

| Skill | 用途 | 状态 | 难度 |
|---|---|---|---|
| `observe.truebody_life` | 读「第N命（X封印）」→ `counters.truebody.life` | ✅ **2026-11-05 已实现**：离线 104 帧中 74 帧可见，解析 74/74，命数 1→6 单调 | — |
| `observe.battle_log_event` | 解析「当前战报」→ 击败/暴击/空 PP 事件 | ❌ 未实现 | 中 |
| `observe.seal_panel` | 读七封印面板通过状态 | ❌ 未采样 | 中（需真机） |
| `battle.attribute_reverse` | 纳多雷「属性反转」并确认生效 | ❌ 未实现 | 中 |
| `battle.pp_drain` | 用「回避/相位移动」耗 PP 并确认对方空 PP | ❌ 未实现 | 中 |
| `game.escape_battle` | 开战即逃跑拿 50 万经验 | ❌ 未实现 | 低 |
| `game.skill_awaken` / `game.pet_reset` | 技能唤醒 100 赛尔豆 / 精灵还原 | ❌ 未实现 | 中（面板） |
| `game.market_buy` | 交易行搜索/筛选/购买 | ❌ 未实现（`unverified`） | 高 |

### 2.2 缺失 Predicate（攻略有门槛，谓词表达不了）

| 需要的谓词 | 用在 | 为什么现有谓词不够 |
|---|---|---|
| `pet_stat_at_least(pet, stat, n)` | 闪光波克尔 **速度 ≥ 319** | 现有只有 `pet_level_at_least` / `pet_exists`，**读不到六维** |
| `pet_stat_equals(pet, stat, n)` | 克罗达**还原后特防 = 6** | 同上（精确等于，不是 ≥） |
| `pet_trait_is(pet, trait)` | 闪光尼尔「瞬杀」、哈莫「坚硬」 | 特性没有建模 |
| `pet_skill_known(pet, skill)` | 哈莫必须带「回避/龙之意志」；塞维尔必须带「锁敌」 | 技能组没有建模 |
| `battle_pp_empty(enemy)` | 判「谱尼已空 PP」 | `battle.log_read` 能力本身还是 unverified |
| `daily_attempts_left(feature, n)` | 每天最多 4 个封印 | CAP-DAILY 未实现 |

### 2.3 缺失 Playbook（有节点但没打法序列）

现有 4 个 playbook 覆盖 4 个节点；其余 **39 个 executable=false 的节点没有 playbook**，这是设计如此
（没有 playbook 就不许执行，防止乱点）。但下面这些**必须补**，否则节点永远无法推进：

| 节点 | 需要什么 playbook |
|---|---|
| `puni.seal.vacuum_1` | 萨帕克：感应→精神强化→七彩龙息（含血量维持规则） |
| `puni.seal.element_2` / `energy_3` | 哈莫：龙之意志→回避循环→龙王波；切猩猩火焰漩涡 |
| `puni.seal.life_4` | 猩猩压迫→哈莫双龙之意志→回避→灭碎阵×2 |
| `puni.seal.samsara_5` | 4 条命的分段循环（最难，需要换人状态机） |
| `puni.seal.eternity_6` / `holy_7` | 消强→反转→耗PP→稳定输出 |
| `puni.truebody.*` | 6 条命各自序列 + 命数检测 |

---

## 3. 本轮已落地的 yaml 修改（可 diff 复核）

### 3.1 `puni.prepare.tower_farm`
- `skill`: `game.tower_status` → **`game.farm_tower`**（只读层数永远不推进）
- `skill_inputs`: `{floors: 3}`
- 计数器语义修正：`tower.floors_cleared` 不再直接写层号；新增 `tower.floors_reached`
- 证据补 `artifacts/round6/tower_floor14.png`

### 3.2 `puni.prepare.buy_potions`
- `completion.args.amount`: `1` → **`100`**（视频明确买 100 瓶）；notes 注明本账号余额 UNKNOWN

### 3.3 `puni.seal.vacuum_1`
- 标题「约 21 回合」→ **「约 30 回合」**（视频 1 分 20 秒）

### 3.4 `puni.seal.eternity_6`
- 标题标注「60 次」含义 **UNKNOWN**

### 3.5 `puni.crosscut.long_battle_watch`
- notes 写明真身全程 **300–600 回合**、单命 20–80 回合；预算上界上调

### 3.6 `puni.roster.buy_flash_pipi`
- notes 写明**速度 ≥ 319** 是第五封印轮回的硬门槛（判据待 `pet_stat_at_least` 谓词）

### 3.7 新增 capability
`battle.attribute_reverse`、`battle.pp_drain`、`observe.truebody_life`、`observe.battle_log_event`

### 3.8 新增节点
`puni.crosscut.attribute_reverse`、`puni.crosscut.pp_drain`、`puni.truebody.detect_life`

---

## 4. 仍然 UNKNOWN（写进下一轮任务，不要猜）

1. 每日挑战次数的**重置时刻**；失败是否扣次数。
2. 「圣能爆发」活动**是否当前生效**、有无期限。
3. 封印 6「60 次」的准确含义（回合 / 命中 / 次数）。
4. 谱尼真身**精确血量**（65000 只是标题量级）。
5. 真身击败后的**结算/成就 UI**（从未采样）。
6. 七封印**选择面板**的布局与通过状态显示。
7. 本账号**货币余额**（赛尔豆/XIN 豆/金豆）与交易行价格。
8. 视频中的角色名也是 `hello`：**是否与本仓库同一账号 UNKNOWN**（不能据此认为本账号已完成视频进度）。

---

## 5. 下一步最重要的一件事

> **在真实客户端做一次「只读采样」**：打开谱尼入口 → 七封印面板 → 截图 → 关闭。
> 这一步不需要打任何一场、不消耗次数，却能把 `puni.seal.*` 的 7 个节点从「无 UI」变成「可写模板」。
> 在拿到这组截图之前，任何关于封印的自动化实现都是在猜坐标 —— 而本项目的第一铁律就是不许猜。
