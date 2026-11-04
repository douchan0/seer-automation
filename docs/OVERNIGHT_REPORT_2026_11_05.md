# 夜间自主任务交付报告 · 2026-11-04 夜 → 2026-11-05 晨

> 项目：**seer-automation**（仓库目录 `seer-puni-quest`，remote `douchan0/seer-automation`）
> 工作窗口：2026-11-04 23:21 开始（任务书 23:10）→ 硬截止 2026-11-05 06:30（Asia/Shanghai）
> 分支：`main`（未 push；本地 4 个 commit）

---

## 0. 一句话结论

**今晚没有拿到谱尼，也没有假装拿到。**

真正的交付是三件事：

1. **长程执行器不再把"什么都没做"记成成功** —— 这曾经让 26 个批次全部空转还报告健康。
2. **刷塔节点真的接上了连战** —— 以前它只读层数，所以"完成"了却一层没多打。
3. **谱尼机制有了一份可追溯、可证伪的研究** —— 并且被 MiniMax 独立检索**推翻了一条我自己的结论**（"没有独立来源"是错的）。

**最大的诚实约束**：今晚**游戏客户端根本没开**（`scripts/seerd` 返回 `no window`），
所以**没有任何一条结论经过本机实时验证**。所有"真机"表述都来自此前 Round 4/6 的既有记录，
本轮没有新增任何真机事实。

---

## A. 项目进展

### A.1 本次真正完成并跑通的

| # | 内容 | 怎么算"真跑通" |
|---|---|---|
| 1 | **Supervisor 空转修复** | 用 `--dry-run` 实跑：2 个批次 → 两个都判 `NO_WORK` → 第 2 批后停机 → 退出码 **3**（不是 0）。`data/supervisor.json` 记录了完整 ledger |
| 2 | **`goal start` 退出码语义化** | 实跑 `python3 -m seerauto goal start puni --dry-run` → 打印 `outcome NO_WORK (exit 3)`，shell `$?` = 3 |
| 3 | **刷塔 playbook 接入连战** | `playbooks.playbook("puni.prepare.tower_farm")` 现在末步是 `game.farm_tower`；graph 节点 `skill` 同步改为 `game.farm_tower`；有测试锁死 |
| 4 | **计数器语义修正** | `tower.floors_cleared` 不再写层号；面板「到达第N层」→ `reached=N`、`cleared=N-1`；VLM 读数只进 INFERRED |
| 5 | **长战看护模块** | `seerauto/runner/battle_watch.py` 已接入 `RunBattle`，每回合一个快照，17 个单测通过 |
| 6 | **真身命数检测器**（新 Skill） | `game.observe_truebody_life`：104 帧真身画面里 74 帧可见，**解析 74/74**，命数 1→6 单调，封印映射 1虚无/2元素/3能量/4生命/5轮回 |
| 7 | **测试基线抬升** | 456 → **563 passed, 2 skipped**（新增 107 个用例，0 回归） |

### A.2 只通过单元测试、未在真机验证的

- `BattleWatchdog` 的**实时接入**（回合预算/卡死判定/JSONL 快照）：单测覆盖逻辑，但**没有在真实战斗里跑过一次**。
- `game.farm_tower` 的**新计数器写入路径**：单测覆盖，真机未跑。
- `game.tower_status` 的 `N-1` 推导：单测覆盖，真机未跑。

### A.3 经过真机验证的（**全部来自本轮之前**）

| 项 | 证据 | 时间 |
|---|---|---|
| 登录 → 进战斗 → 自动出招 → 胜利 → 连续 11 场 | `docs/progress.md` Round 4 | 2026-11-02 |
| 谱尼入口可达（SPT→太空站左翼→谱尼） | `artifacts/round5/09_puni_entrance.png` | 2026-11-03 |
| 塔层守护者一战 1300 积累经验 | `docs/DELIVERY_2026_11_04.md` | 2026-11-04 |
| 精灵恢复同时回 HP+PP（20 XIN豆） | 同上 | 2026-11-04 |

### A.4 仍然完全未验证的

- 七封印选择面板 UI（从未采样）
- 每日挑战次数（从未读到）
- 交易行是否可用（`market.buy` 仍 `unverified`）
- 谱尼真身任何一命
- 战败分支（全项目**没有任何失败样本**，G1）

---

## B. MiniMax 研究成果

### B.1 必须先说清楚的分工事实

任务书假设「MiniMax 负责联网，DeepSeek 不能搜」。今晚的真实情况是：

| 能力 | 实际状态 |
|---|---|
| 本机 `web_search` | ❌ 无 `DEEPSEEK_API_KEY`，**不可用**（任务书判断正确） |
| 本机 `web_fetch` | ✅ **可用**（不依赖搜索 API） |
| 本机图像理解（`read_image`） | ✅ **可用**——我能直接看图 |
| MiniMax（OpenCode） | 23:23 起可用；**23:29–23:49 额度耗尽（429）**；**00:00 后恢复** |

因此研究是**两段式**：前段我自己抓（web_fetch + B 站 API + 本地 417 帧），后段 MiniMax 接手做交叉验证。

### B.2 数量（不虚报）

| 项 | 数量 | 说明 |
|---|---|---|
| 我发起的 HTTP 抓取 | **11 次** | 其中 5 次拿到有效内容；3 次被搜索引擎/风控挡（Bing 返"赛"字词义、B 站搜索页验证码、DDG 超时） |
| MiniMax 成功调用 | **4 次** | 2 次能力探测（含真实 websearch/webfetch 结果）+ 1 次交叉验证研究（223 行）+ 1 次独立代码审查（28 KB） |
| MiniMax 被额度挡住 | **2 次** | 23:29（视觉探测中途）、23:49（复测），均 `429 Token Plan 用量上限 (2056)` |
| B 站视频元数据（API 实取） | **3 个** | BV1UUyiBhEqQ / BV1Fu8h6YEAz / BV1B4t462EeH |
| MiniMax 找到的独立来源 | **≥10 个** | 见 `mmx/RESEARCH_CROSSCHECK.md` §2 |
| **完整观看的视频** | **0 个** | 没有任何一个视频被完整观看 |
| 我亲自看的图像 | **3 张** | `artifacts/battle.png`、`frames/08_text_guide/f_00033.png`、`frames/07_truebody_s/f_00004.png` |
| 既有 OCR 帧索引 | **417 帧** | `frames/_ocr.json`（本轮读取并检索，非本轮生成） |

> **不夸大声明**：本轮**没有观看任何视频画面**。视频结论全部来自
> ①仓库既有的 417 帧 OCR 拆解（`docs/plan-from-video.md`）、②B 站 API 元数据、
> ③MiniMax 抓到的标题/简介/标签/评论/弹幕。三者都不是"看完视频"。

### B.3 版本判断

| 项 | 结论 |
|---|---|
| 游戏 | 《赛尔号》**XIN 计划**（Electron+PepperFlash，客户端 1.0.8.9） |
| 服务器 | 1号啦公星，角色 `hello` |
| 最强证据 | S2 = BV1Fu8h6YEAz，**2026-08-23**（距今约 2.5 个月），40:48，新号 3 天全流程 |
| 上一版打法 | S1 = BV1UUyiBhEqQ，2025-10-31，七封印 + S 级真身 |
| 判定 | S2 视为**当前版本**；S1 视为上一版（作者自称"补发被下架的视频"） |

### B.4 谱尼完整获取路线（浓缩）

```
新号 → 开通交易行（击败 SPT 首领）→ 三天把 7 只精灵练到 100 级
     → 每天最多打 4 个封印（孤证）→ 第2天清 1–4，第3天清 5–7
     → S 级真身 6 条命 → 获得谱尼
```
核心原理：**谱尼 PP 有限，耗光后只会放「圣光气」；此时它只是沙包。**

### B.5 七封印要点（详细见指南 §5）

| 封印 | 打法骨架 |
|---|---|
| 1 虚无 | 萨帕克单挑：感应叠特防 → 精神强化 → 七彩龙息；血控 1/3 |
| 2 元素 / 3 能量 | 哈莫龙之意志+回避耗 PP → 魔焰猩猩火焰漩涡烧伤（两者打法相同） |
| 4 生命 | 猩猩压迫消强 → 哈莫双龙之意志 → 回避 → 灭碎阵×2 |
| 5 轮回 ★ | 4 条命：猩猩+塞维尔消强 → 属性反转 → 哈莫耗 PP → 闪皮同生共死+纳多雷收 |
| 6 永恒 | 打「60 次」（孤证）→ 猩猩压迫 → 纳多雷反转 → 哈莫龙王波×4 轮 |
| 7 圣洁 | 属性反转成地面系 → 哈莫龙之意志×3 → 灭碎阵 |

### B.6 真身（S 级，6 条命，详细见指南 §6）

| 命 | 打法 |
|---|---|
| 1 虚无 | 萨帕克 感应+精神强化 → 七彩龙息 |
| 2 元素 | 塞维尔 锁敌 → 龙之毒 |
| 3 能量 | 萨帕克相位移动耗 PP → 克罗达 意念集中×2 → 漩涡点 7 下 |
| 4 生命 | 克罗达 **偏激防守** 反伤弹死（Lv1、特防=6） |
| 5 轮回 | 4 条 HP 循环：反转+同生共死+毒+仙女龙 |
| 6 终命 | 塞维尔 重踏 → 龙之毒 循环 |

### B.7 资源与道具预算

- 高级体力药剂 **×100**（视频明确数量）、超级体力药剂、**高级活力药剂（补 20 次 PP）刚需**
- 精灵还原药剂、基因重组、红色元神珠+元神赋形、精灵胶囊
- 交易行参考价：萨拉 5W / 卡西达 5.6W / 闪光皮皮 1.4W / 尼尔 3W（OCR 量级）
- ⚠️ **本账号余额 UNKNOWN**（从未读货币栏）

### B.8 来源与可信等级（详见 `PUNI_SOURCES.md`）

| 等级 | 内容 |
|---|---|
| **B+** | 七封印名称与顺序（C1）——被独立 UP「烂精灵酱」7 集连载独立证实 |
| **B** | S1/S2/S3 三视频（同一 UP，**不独立**）、逐命打法、精灵配置 |
| **B−** | 关键数字：每日 4 次 / 速度 319 / 永恒 60 次 / 真身 65000 HP —— **全部孤证** |
| **C** | 233乐园帖（转载，同源） |
| **D（不适用）** | TapTap《谱尼全攻略》= **《赛尔号星球大战》手游**，机制完全不同；7k7k/萌百 = 经典页游 |
| **A** | **本轮新增 0 条**（没有真机实测） |

> **本轮最重要的研究方法结论**：机制是真的，**数字是孤证的**。
> 「319」「4 次」「60 次」「65000」都只有单一同源出处，**不得写进谓词当硬约束**。

---

## C. 谱尼 Goal Graph

| 项 | 数值 |
|---|---|
| 节点总数 | **46**（43 → 46） |
| 运行期 completed | **1**（`puni.prepare.tower_farm`，predicate `tower.floors_cleared ≥ 1`，LIVE） |
| partial | 4 |
| todo | 29 |
| unknown | 8 |
| blocked | 4（交易行购买类） |
| unverified | 1 |
| executable | 4 |

### C.1 新增节点（3 个，均为横切能力）

| 节点 | 为什么必须加 |
|---|---|
| `puni.crosscut.attribute_reverse` | **属性反转**是 6/7 封印与真身 3/5 命的核心，43 节点里**完全没有表示** |
| `puni.crosscut.pp_drain` | **主动耗 PP** 是全部打法的共同前提；原图只有「空 PP 窗口识别」，缺动作本身 |
| `puni.truebody.detect_life` | 真身自动化的前提是**知道现在第几命**；客户端有「第N命」信号，但图里没有节点消费它 |

### C.2 修正节点（6 个）

| 节点 | 改了什么 |
|---|---|
| `puni.prepare.tower_farm` | `skill` 只读→`game.farm_tower`；playbook 加连战；计数器语义修正 |
| `puni.prepare.buy_potions` | 数量 1 → 100（视频量级） |
| `puni.seal.vacuum_1` | 回合 21 → **30** |
| `puni.seal.eternity_6` | 标题标注「60 次」**含义 UNKNOWN** |
| `puni.crosscut.long_battle_watch` | 真身全程 **300–600 回合** |
| `puni.roster.buy_flash_pipi` | 写明**速度 ≥319** 门槛（孤证，待谓词） |

### C.3 删除 / 废弃

**0 个。** 没有任何节点被证明"机制不存在"。被判定过时的是**外部资料**（TapTap 星球大战、2010 页游），不是节点。

### C.4 下一步最重要的前置条件

> **一次「只读采样」**：打开谱尼入口 → 七封印面板 → 截图 → 关闭。
> 不打任何一场、不消耗次数，却能把 7 个封印节点从"没有 UI 就只能猜坐标"变成"可以写模板"。

---

## D. 工程交付

### D.1 修复的 Bug（8 个）

| # | Bug | 后果 | 修复 |
|---|---|---|---|
| 1 | `goal start` 永远返回 0 | "无事可做"被当成功，supervisor 空转 26 批 | 引入 `outcome.py`，按结局返回 0/3/1/124/0/7 |
| 2 | supervisor 只凭退出码判健康 | 25 批 `battles=0 actions=0` 仍 `failures=0` | 按「是否真的打了」判定 + 无任务/失败/闲置/截止四道闸 |
| 3 | 批次数双计 | 2 批记成 4 批 | `record()` 独占计数 |
| 4 | 刷塔 playbook 只读层数 | 节点"完成"却一层没多打 | 末步改为 `game.farm_tower` |
| 5 | `floors_cleared` 存的是层号 | 「到达 14 层」被记成「已清 14 层」 | reached=N / cleared=N-1，VLM 只进 INFERRED |
| 6 | `_trusted()` 恒返回 False | `evidence.rank` 不存在，异常被吞；VLM 猜测总覆盖已验证读数 | 改用 `evidence.source.rank >= 3` |
| 7 | `farm_tower` 绕过战斗预算 | 可超打 `--max-battles`，战报数字不实 | `floors` 按剩余预算裁剪；`floors_won` 计入战斗数 |
| 8 | 长战无回合预算/无快照 | 600 回合真身中断即全丢 | `battle_watch.py`：回合预算+卡死判定+JSONL 快照 |

### D.2 新增模块

| 文件 | 作用 |
|---|---|
| `seerauto/runner/outcome.py` | 结局分类（SUCCESS/NO_WORK/FAILED/TIMEOUT/PAUSED/BLOCKED）+ 退出码 + `verified_progress` |
| `seerauto/runner/battle_watch.py` | 长战看护：回合/时间/失败预算、卡死判定、逐回合快照与恢复点 |
| `tools/mmx.sh` | MiniMax 调用封装（XDG 重定向，否则沙箱下 OpenCode 起不来） |

### D.3 新增 Skill / Predicate / Playbook

- **新 Skill：1 个** —— `game.observe_truebody_life`（真身命数检测，离线 74/74 帧验证）
- **新 Predicate：0 个**（提出了 6 个，见缺口分析 §2.2；实现需要先有六维/技能组数据模型）
- **新 Playbook：0 个**（改了 1 个：tower_farm）
- **新 Capability：4 个**（`battle.attribute_reverse`、`battle.pp_drain`、`observe.truebody_life`、`observe.battle_log_event`，均为 `unverified`）

### D.4 Commit 列表

| Commit | 内容 |
|---|---|
| `aa7e4a1` | fix(supervisor): a batch that did nothing is NO_WORK, not success |
| `fbe3498` | fix(tower): the node now actually farms, and "cleared" means cleared |
| `41878c2` | docs(research): current-version Puni guide, source ledger, and 43-node gap analysis |
| `0f92144` | feat(battle): turn budget, stuck detection and a per-turn resume snapshot |

（另有 `docs/NEXT_ACTIONS.md`、`docs/research/*` 的更新，随最后一个 commit 或未提交，见 `git status`）

### D.5 测试结果

```
PYTHONPATH=.pydeps python3 -m pytest -q
563 passed, 2 skipped in 26.86s
```

- 基线：456 passed, 2 skipped
- 新增 **107** 个用例（outcome / supervise / tower / battle_watch / truebody_life）
- **0 回归**
- 2 个 skip 是既有的「需要 live OCR fixture」用例，与本轮无关

### D.6 代码审查

- **MiniMax 独立审查**（`docs/research/mmx/REVIEW_CODE.md`，28 KB）：2 blocker + 9 major，
  **已修 13 项**（见 §E.5 表），未修项写进下一轮。
- **自查**发现并修掉：批次数双计、`_trusted()` 恒假（`evidence.rank` 不存在）、
  `floors_won` 不计入战斗预算、拒绝原因内嵌 `status=blocked` 导致结局误判。

---

## E. 双模型协作效果

### E.1 DeepSeek（主控）完成

仓库架构理解、P0/P1/P2 全部代码、研究前段（web_fetch + B 站 API + 图像复核）、
缺口分析、Goal Graph 修改、全部测试、文档与报告。

### E.2 MiniMax 完成

| 任务 | 结果 |
|---|---|
| 能力探测（websearch / webfetch / 图片 read） | ✅ 三项均真实可用，有实际返回 |
| **交叉验证研究** | ✅ **223 行报告**，找到 ≥10 个独立来源，判定 6 条断言 |
| **独立代码审查** | ✅ **00:12 交付 28 KB**：2 个 blocker + 9 个 major + 若干 minor，全部复核后修复（见 §E.5） |
| 视觉分析（探测） | ❌ 中途 429，未产出结论 |

### E.3 MiniMax 联网研究成果（最有价值的部分）

1. **推翻了我的结论**：我说"没有第二个独立来源"——错。独立 UP「烂精灵酱」有 **7 集连载**，
   标题就是七封印顺序 → C1 升级为**独立证实**。
2. **推翻了我的技术结论**：我说"B 站 search/type 被 412 封禁"——不准确，是**没带 Cookie**；
   带 `buvid3` + Referer 后正常返回 1000 条结果。
3. **发现我漏掉的事实**：XIN **同时存在「普通版真身」与「S 级真身」两档**。
4. **守住了诚实边界**：5 个关键数字（4/319/60/65000/逐命映射）全部判 **UNVERIFIED**，
   并明确写出"未观看任何视频画面"。

### E.4 MiniMax 图像理解成果

**没有可用于交付的图像结论。** 视觉探测在 23:29 拿到图片、开始裁剪分析后遇到 429 中断。
本轮所有图像结论来自**我自己**的 `read_image`（3 张）。

### E.5 MiniMax 代码贡献：0 行代码，但**改掉了我 2 个 blocker**

MiniMax 没有写代码，但它的独立审查（`docs/research/mmx/REVIEW_CODE.md`，28 KB）**实质改变了交付质量**。
它找到的、并且我已经修复的问题：

| 级别 | 它指出的问题 | 我的修复 |
|---|---|---|
| **blocker** | supervisor 的 `snapshot()` 只看 `status == "completed"`，**不看 proof**；而 `classify_run` 要求 proof → 同一个节点在两处定义不一致，无 proof 的"完成"被算成验证进展 | 抽出唯一判据 `completed_with_proof()`，两处共用；加回归测试 |
| **blocker** | 战斗看护的指纹**几乎是常数**：`ally_hp` 只在 `ally_level is None` 时读 3 次就冻结，`enemy_life` 从未赋值，只剩一个粗哈希在变 → `max_stuck_turns=8` **默认开启**，可能误判"卡死"→ 战斗 FAIL+pause → **supervisor 直接停掉整夜** | 卡死判定默认 **0（关闭）**，只在真身显式开启；HP 改为每 5 回合重读 |
| major | `after.stats` 可能属于**上一个 run**（批处理没起来时），会让失败批次看起来"有效"，还会把 delta 算成负数 | 用 `run_changed` 门控；delta 一律 `max(0, …)` |
| major | 退出码 0 且**没有 outcome 行**时仍判 SUCCESS —— 正是这次要消灭的假设 | 改为 FAILED（"无法判定"），exit 4/5 也归 FAILED |
| major | `PAUSED` 与 `SUCCESS` **共用退出码 0**，人工暂停在 cron 眼里是健康 | PAUSED → **130**，并在 `main()` 里映射 |
| major | 回血片段没有 `try/finally`，中途抛异常会**遗留 seerdx 守护进程**占着采集流（下一批截图必失败） | 包 `try/finally: s.close()` |
| major | 我在 `puni.yaml` 里给 `buy_potions` 加了**第二个 `notes:` 键**，PyYAML 静默丢弃我的新注释 | 改名 `notes_amount:`；并加"YAML 节点键唯一性"测试 |
| major | `note_failure()` **没有任何生产代码调用**，`max_failures=3` 是永远不会触发的死限制 | 默认改为 0（关闭），并注明原因 |
| major | `battle_log_path` **从未被设置**，"快照即恢复点"在生产里**永远不写文件**；而危险的卡死判定却默认开着 | runner 里默认注入 `data/battle/<run_id>.jsonl` |
| minor | `max_turns` 用 `>` 导致预算 600 实际允许 601 | 改 `>=`（含端点） |
| minor | `load_snapshots` 只捕 `FileNotFoundError`，**被 kill 时写坏的最后一行**会让"恢复"路径抛异常 | 容忍坏行 |
| minor | `to_dict()` 不写 `log_sig`，审计日志无法解释"为什么判卡死" | 补上 |
| minor | `min_battles_for_work` 声明了但没用；`_V` 鸭子类型绕过了 `RunVerdict` | 都用上了 |

**未修的（已记录，非阻塞）**：`counters.tower.floors_cleared` 仍有两个写入者（面板推导 N-1 与
连战累加），审查建议改成单一写入者；SIGINT 处理器、守护进程复用、批间 sleep 也未做。
这些写进了下一轮任务，不在今晚硬改。

> **这正是任务书想要的双模型价值**：它抓到的两个 blocker 都是我自己写完还"自查通过"的地方。

### E.6 委派统计

| 指标 | 值 |
|---|---|
| 委派任务数 | 3（交叉验证、代码审查、视觉探测） |
| 成功 | 2（交叉验证 + 代码审查） |
| 失败（额度） | 1（视觉探测，429） |
| 超时未交付 | 0 |
| 任务委派成功率 | **2/3** |
| 模型调用失败次数 | 2 次 429 + 1 次 500 + 1 次长时间挂起 |
| 文件冲突 | **0**（两模型写的是不同文件；MiniMax 只被允许写 `docs/research/mmx/`） |

### E.7 协作结论

**MiniMax 在"联网检索 + 交叉验证"上的价值是真实且高的**——它两次推翻了我的结论，
这正是任务书想要的双模型互相纠错。但它的**额度极不稳定**（一个 40 分钟的深度任务就能打满），
且**长任务容易挂起**。下一轮应把它的任务切成 10 分钟内能出结论的小块。

---

## F. 长程执行可靠性

### F.1 修复前的实测数据（`data/supervisor.spin_2026-11-04.json`，已保留）

| 指标 | 值 |
|---|---|
| 批次 | **26** |
| 退出码 | **全部 0** |
| `battles=0` 且 `actions=0` 的批次 | **25** |
| `failures` | **0** |
| 总批次耗时 | 196 s |
| 停机原因 | 无（一直跑到被外部终止/截止） |

**这就是"退出码 0 但什么都没做"的确凿证据。**

### F.2 修复后的实测（`--dry-run` 实跑）

| 指标 | 值 |
|---|---|
| 批次 | **2**（然后停机） |
| 有效批次 `effective_batches` | 0 |
| 无任务批次 `no_work_batches` | 2 |
| 失败批次 `failed_batches` | 0 |
| 验证进展 `verified_progress` | 0 |
| 实际动作数 | 0 |
| 连续无进展 | 2 |
| 停机原因 | `2 consecutive batches had no schedulable work` |
| 退出码 | **3（NO_WORK）** |

### F.3 新增的可靠性护栏

| 护栏 | 触发条件 | 行为 |
|---|---|---|
| 无任务保护 | 连续 N 批（默认 2）无可调度节点 | 停机，退出码 3 |
| 连续失败保护 | 连续 N 次失败（默认 4） | 停机 |
| 闲置保护 | 超过 N 分钟（默认 90）没有有效批次 | 停机 |
| 从未有效保护 | 3 批且一次都没生效 | 停机 |
| 截止保护 | 到达 deadline | 停机 |
| 批次上限 | `--max-batches` | 停机 |
| 硬阻塞 | 节点 BLOCKED（如全员 0 血） | 立即停机，退出码 7 |
| 人工暂停 | 节点 PAUSED | 立即停机 |
| 长战回合预算 | `battle_max_turns` | 结束本趟战斗 |
| 长战卡死 | 连续 N 回合指纹不变 | 结束本趟战斗并给出原因 |

### F.4 恢复成功次数

**0**（本轮没有实时运行，无恢复场景可触发）。

### F.5 诚实说明

`data/supervisor.json` 现在记录的是那次 **dry-run 验证**（2 批），
旧的 26 批空转记录**已备份**为 `data/supervisor.spin_2026-11-04.json`，
两份都在仓库里，可对照。

---

## G. 下一轮任务（按优先级）

### G1 · 谱尼入口「只读采样」（最高优先，30 分钟）

- **目标**：拿到七封印面板截图 + 每日次数读数，解锁 7 个封印节点与 CAP-DAILY
- **前置**：游戏客户端启动并登录（本夜未开）
- **所需 Skill**：`game.open_panel`（已有）、`observe.ocr`（已有）
- **所需模型**：**不需要 LLM**（纯视觉状态机）；若面板文字糊，可让 MiniMax 读一次图
- **验收**：`artifacts/round7/seal_panel.png` + 能读出 7 个封印名 + 一个"今日剩余次数"数字
- **难度**：低
- **阻塞**：客户端没开；服务器公告禁止第三方脚本（需用户明确授权实时操作）

### G2 · 真身命数检测 `game.observe_truebody_life`（不需要新采样）

- **目标**：读「谱尼真身 / 第N命 /（X封印）」→ `counters.truebody.life`
- **前置**：无（信号已在 `frames/07_truebody_s/` 的 104 帧中确认）
- **所需 Skill**：新增 `observe.truebody_life`（模板/OCR）
- **所需模型**：不需要 LLM（确定性模板）
- **验收**：离线回放 104 帧，≥95% 帧读对命数
- **难度**：低
- **阻塞**：无——**这是今晚就能做但没时间做完的事**

### G3 · 战报事件解析 `game.observe_battle_log`

- **目标**：解析「当前战报」→ `被击败了 / 致命一击 / 使用了X / 圣光气` 结构化事件
- **前置**：G2 同源（都是 OCR 区域解析）
- **所需 Skill**：新增 `observe.battle_log_event`
- **所需模型**：不需要 LLM
- **验收**：用 `frames/_ocr.json` 回放，能正确切分 3 类事件
- **难度**：中
- **阻塞**：无

### G4 · 补 6 个谓词（表达攻略门槛）

- **目标**：`pet_stat_at_least`（速度≥319）、`pet_stat_equals`（特防=6）、`pet_skill_known`、
  `daily_attempts_left`、`battle_pp_empty`、`pet_trait_is`
- **前置**：需要先扩展 `GameWorldState` 的 pet 模型（六维/技能组/特性）
- **所需 Skill**：`game.inspect_pets`（已有）
- **所需模型**：不需要
- **验收**：单测覆盖每个谓词 + `counter_at_least` 回归不破
- **难度**：中
- **阻塞**：需要一次真机读精灵面板拿到六维样本

### G5 · 交易行只读采样 → 解锁 `market.buy`

- **目标**：打开交易行搜「萨拉」，**只读价格不购买**，解锁 4 个 `buy_*` 节点
- **前置**：交易行已解锁（需先击败 SPT 首领）
- **所需 Skill**：`game.market_buy`（需新增，只读模式先行）
- **所需模型**：不需要
- **验收**：读到价格 + 是否可购买；`market.buy` 从 unverified 升级
- **难度**：高（UI 未采样 + 需真机）
- **阻塞**：交易行解锁前置未完成；服务器规则约束

---

## H. 本轮没做到 / 做错的（自查）

| 项 | 说明 |
|---|---|
| 没有真机验证 | 客户端未开 + 服务器禁止第三方脚本 → 全部结论停在单测/离线层 |
| 只实现了 1 个新 Skill | 提出了 4 个能力、6 个谓词；实现的是 `observe.truebody_life`，其余（属性反转/耗 PP/战报解析）仍未实现 |
| 真身档位没进图 | 交叉验证发现「普通版/S 级」两档，但 Graph 里 `puni.truebody.*` 仍未区分（只写进了文档） |
| ~~MiniMax 审查未交付~~ | **订正**：审查 00:12 交付，并改掉了 2 个 blocker（见 §E.5） |
| 首版 P0 有 2 个 bug | 大小写匹配、拒绝原因内嵌 `status=blocked` 导致误判 —— 被自己写的测试抓住 |
| `data/supervisor.json` 被覆盖 | 已备份为 `supervisor.spin_2026-11-04.json`，两份都在 |

---

## I. 一句话给下一个 Agent

**别急着写谱尼的点击代码。** 先去开一次客户端，把七封印面板截下来。
在你拿到那张截图之前，`puni.seal.*` 的 7 个节点全是"没有素材"的状态——
而本项目的铁律是**不许猜坐标**。

---

*生成时间：2026-11-05 00:30（Asia/Shanghai）*
*测试基线：563 passed, 2 skipped*
*Commit：aa7e4a1 / fbe3498 / 41878c2 / 0f92144*
