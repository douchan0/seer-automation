I'll start by examining the repository state and the changes.
# 第二轮审查报告（修复复核）

范围：`git diff 7c6f2b8^..HEAD`（8 个 commit）。基线：`637 passed / 2 skipped`（我本地复跑一致）。
只审修复本身，不重述背景。

## 结论概览

| # | 修复项 | 判决 |
|---|---|---|
| 1 | `bg_probe` 三态判决 + 同代码双通道 | **部分修复**（判决逻辑已正确；证据链反而更弱了） |
| 2 | `login.screen_kind` / `PRIMARY_WORLD_MARKERS` | **部分修复**（loading 过度优先；世界判定实际只剩一个标记） |
| 3 | `drag_server_list` 坐标转换 | **部分修复**（换算正确；缺前置/焦点/几何守卫） |
| 4 | `enter_server_by_number` 清空 + 回读 | **未修复**（`key code=0` 不是全选，是打了一个 "a"） |
| 5 | 进塔顺序 + `reset_between_rounds` | **部分修复**（顺序对；reset 在塔内跑不动，且几乎永不触发） |
| 6 | `ledger` `blocked` / `error` 语义 | **部分修复**（分类对了；计数仍与注释承诺相反） |
| 7 | `GiveExp` 重复灌注 | **部分修复**（主要路径堵死，兜底解析有假阳性） |
| 8 | `FarmTower` refused vs landed-but-no-battle | **部分修复**（文本分得开，状态没分开） |
| 9 | `explain_goal` 拆分 + 不开 session | **部分修复**（不开 session 已修；`unblocks` 6/6 条都是不可执行节点） |

---

## 1. `tools/bg_probe.py` — 部分修复

**已修复（判决逻辑）**：`tools/bg_probe.py:164-201` 现在要求「同坐标的前台对照成功」才允许输出 `BACKGROUND_INPUT_BLOCKED`；`--check` 走 `:112-113` 提前返回，`:171-173` 因为缺 `spt_opened` 只能得到 INCONCLUSIVE。无证据下判决的路径找不到了。`:144-155` 的 `front_measured` / `cursor_measured` 也修对了（两个 `None` 不再算「未变」）。
SPT 标记集我用真帧 `data/overnight/frames/ab_fg_01.png` 离线 OCR 复核过：`星际挑战终端 / SPT 挑战 / 共73个入口 / 太空站左翼 / 击败后解锁交易行资格` 全部命中，`:57` 的标记可用。

**新问题 1a（证据链断了，比修复前更糟）**：仓库里**没有** `data/overnight/bg_probe.json`——它只存在于旧分支 `night/20261105-puni`（`git log --all --diff-filter=D` 无删除记录，是换分支时就没带过来）。当前分支唯一的产物是 `data/overnight/bg_probe.check.json`，判决 `PROBE_INCONCLUSIVE`。而 `AGENTS.md` 5.6、`tools/farm_tower_repeat.py:336-338`、`tools/login.py:102-103`、`docs/OVERNIGHT_PLAN_2026_11_05.md:31` 仍在把 `data/overnight/bg_probe.json` 当作「PID 通道被封」的既证引用。7f59a9a 的提交信息说「--check cannot overwrite the evidence」，实际效果是：**旧证据（本来就不可采信的手测对照）没了，新证据也没生成**，这条结论现在零支撑。

**新问题 1b（前台臂几乎必然被拒）**：前台臂走 `session.click_px` → `seerauto/live.py:470` 的 `ensure_frontmost()`，游戏不在最前就抛 `NotFrontmost`。`bg_probe.py` 从不 `activate`（对比 `tools/login.py:299` 会 activate）。已提交的 `bg_probe.check.json` 里 `front_before.name = Safari浏览器` —— 也就是说按现状跑 A/B，前台臂会直接 `click refused`。工具没有任何提示说「需要先把游戏窗口点到最前」。

**部分修复 1c（`--check` 仍能覆盖真证据）**：`:211-213` 只是改默认文件名；`--check --json data/overnight/bg_probe.json` 会直接覆盖。而且即使不是 `--check`，一次 INCONCLUSIVE 的运行也会把上一份 BLOCKED 证据原地覆盖掉。建议：`--check` 或 `verdict != BLOCKED/PASS` 时拒绝写入已存在的证据文件（append-only + 时间戳）。

**已修复（`left_dirty` 语义）**：`:140` `left_dirty = spt_opened and not closed`——「探针改变了画面且没能还原」这个定义是对的，`:126-136` 的 `closed` 逻辑也对。
**残留 1d**：`left_dirty` 只覆盖 SPT 面板这一个变化；点击若打开了**别的**面板（`spt_opened=False` → `closed=True` → `left_dirty=False`），`unexpected_change`（`:123-124`）记了但没参与判决。实际后果是好的（前台臂会因 `spt_absent_before=False` 判 INCONCLUSIVE），但语义不完整。

---

## 2. `tools/login.py::screen_kind` — 部分修复

**已修复**：`tests/test_login_flow.py:61-74,109-121` 用 `server_entered.png` 真帧钉住了 76%「正在加载星球资源」不再被当成世界；`screen_kind` 里 loading 优先于 server/login（`:62-63`），`PRIMARY_WORLD_MARKERS` 删掉 `XIN计划，启动`/`当前回合`（`:45`）确实堵住了那个假成功。

**部分修复 2a（现在实际只剩一个标记在起作用）**：`PRIMARY_WORLD_MARKERS = ("卫兵", "已连接公屏聊天")` 是**或**关系，但「已连接公屏聊天」是一条**会滚走的系统聊天**——本轮唯一一份真机世界帧 `data/overnight/bg_probe.check.json` 的 `texts_before` 里只有 `卫兵`，系统消息是「系统：尊享入场| hello 登录了」。所以真世界判定实际退化成「画面上有个叫卫兵的 NPC」。卫兵是训练室场景的 NPC，一旦角色不在该场景（或 OCR漏读），`screen_kind` → `unknown` → `main()` 的 `else: time.sleep(3)` 分支**没有任何日志**（`tools/login.py:337-338`），用户只会看到「timed out before reaching the world」。
真正的判别位是**在线/离线后缀**：选服页是「公屏聊天 • 离线」，世界是「公屏聊天 • 在线」（`tests/test_login_flow.py:36,46` 有真帧佐证）。建议把 `公屏聊天 • 在线` 加进 PRIMARY 集合（并保留 `卫兵`），比现在稳。

**新问题 2b（loading 优先级过高的副作用）**：`LOADING_MARKERS` 里的「加载中」是**全屏子串**匹配，会命中聊天频道里任何一句带「加载中」的话；且 loading排在 login 之前，一旦登录页角落常驻「版本检测中…」（`AGENTS.md` 5.6 说客户端启动会显示它），`screen_kind` 永远返回 `loading`，**登录按钮永远不会被点**（`:328-333` 的 `elif kind == "login"` 不可达）。失败模式是 fail-closed（超时退出4），但会烧掉整个 `--timeout`。建议把「版本检测中」单独成类（或按区域限定匹配），不要放进 world 判定的前置。

**新问题 2c（死代码 + 注释自相矛盾）**：`tools/login.py:40` 的 `WORLD_MARKERS`（含 `公屏聊天`）在本文件内**没有任何使用者**（只有 `:45`/`:70` 的 PRIMARY 在用），而它上面的注释（`:35-39`）写的是「『公屏聊天』…deliberately **not** here」——注释描述的是旧集合，代码里却留着旧集合。文件头 docstring `:13` 也还写着「wait until the world is really there (公屏聊天/卫兵/当前回合 …)」。

---

## 3. `tools/login.py::drag_server_list` — 部分修复

**已修复（换算正确）**：`tools/login.py:189-190` 用 `geo.global_pt_from_game_px`（`seerauto/geometry.py:274-279` = `origin + px/scale`），`SCROLLBAR_X=2023` → 窗口内 1011 pt（窗口宽 1200 pt ✓），不再越窗 740 pt。daemon 侧 `native/seerdx.swift:672-676` 的 `drag` 确实是全局 HID拖拽，参数名 `x1/y1/x2/y2/steps` 也对得上。

**残留 3a（同一个 bug 还能再犯）**：这条路径**绕过了 `LiveSession.click_px` 的全部守卫**——没有 `ensure_frontmost()`，也没有 `_revalidate_geometry()`（`seerauto/live.py:427-447`）。而 `session.geo` 只在 `open()` 时设一次（`seerauto/live.py:200`），窗口一旦移动（`AGENTS.md` 说每会话都会变），拖拽就又落到别的 App 上——正是这条修复想消灭的那个症状。同理，游戏不在前台时，拖拽会去滚**别的程序**的列表。
建议：`drag_server_list` 先调 `session.ensure_frontmost()` + `session._revalidate_geometry()`，或干脆改用 `LiveSession` 的受控拖拽方法。

---

## 4. `tools/login.py::enter_server_by_number` — 未修复

**未修复 4a（`key code=0` 不是全选）**：`tools/login.py:228` 注释写 `# cmd+A`，但 daemon 的 `key` op（`native/seerdx.swift:688-689` → `keyPress` `:436-441`）只发 `CGEvent(keyboardEventSource:virtualKey:code)`，**不带任何 modifier flags**。macOS virtual keycode `0` = `kVK_ANSI_A`，所以这一步实际是**往输入框里打了一个字母 "a"**，既没选中，也没清空。仓库里没有 cmd+key 的任何实现（`seerdx.swift` 没有 flags 参数）。
后果链：字段里留下 `a`，再 `type "1"` → 字段内容 `a1`；`_field_shows` 只抽数字（`:258`）→ `got="1"` → **判定通过** → 点「快速进入」时带的是 `a1`。这比修复前更糟：以前是 `1→11→111`，现在会静默带一个字母。
正确修法二选一：(a) 给 `seerdx.swift` 加一个带 `flags: .maskCommand` 的 `cmd+a` op；(b) 用 `key code=51`（退格）连发 N 次清空。

**部分修复 4b（`_field_shows` 容差不可靠）**：`tools/login.py:256` 的 ±220/±40 是 **game_px**（=±110 pt 横向）。横向110 pt 足以把同一行上别的数字框进来；而 `:258` 只保留数字，等于把 `a1`、`1` 判成同一个东西。纵向 ±40 game_px（20 pt）尚可。另外点击点 `box["px"][0]+120`（`:224`）——「请输入服务器编号」7 个汉字在 2400×1600 的画面上本身就超过 120 game_px 的一半，偏移 120 px **很可能仍落在占位文字上**而不是输入框里；真值验证只能靠 `_field_shows` 兜（fail-closed ✓，但那就等于这个功能从来没成功过）。

---

## 5. `tools/farm_tower_repeat.py` — 部分修复

**已修复（进塔顺序）**：`tools/farm_tower_repeat.py:364-373` = `travel` → `tower_status`（`seerauto/skills/act.py:1819` 点 `TOWER_ENTRY`，唯一会点塔入口的技能）→ `:409` 的 `farm`。这确实修掉了「travel 只到训练室 → 点守护者像素打在景物上」的真因。

**未修复 5a（`reset_between_rounds` 在塔内跑不动）**：`tools/farm_tower_repeat.py:205-224` 的第一步是 `travel_to_tower`（`:215`，**返回值被丢弃**），但 `Travel` 靠点底栏的地图图标（`seerauto/skills/act.py:1425` `MAP_BUTTON_PX`、`:1441-1444` 模板探测）——而 `reset_between_rounds` 自己的 docstring 就写着「塔内没有底栏也没有地图」。于是：**塔内 → travel 失败（`star map did not open`）→ fallback `goto_tutor_room` 也是 Travel，同样失败 → heal 需要开精灵背包（`act.py:1583`），塔内也开不了 → 第三次 travel 同样失败 → 最后 `enter_tower_and_read_floor` 在塔内点 `TOWER_ENTRY`（`act.py:1819`）这个盲点**。
结论：**这个 reset 走不出塔、治不了伤、也回不去**，只是白白多花一次 ~40 s 的 VLM 读数。真正能出塔的机制在代码里根本不存在。

**新问题 5b（reset 几乎永不触发）**：触发条件是 `ledger.failure_streak == 2`（`:420-423`），但默认 `--max-no-work 2` / `--max-failures 3` 下，两轮 BLOCKED 会**同时**把 `no_work_streak` 顶到 2（`seerauto/farm/ledger.py:164-166`），下一轮 `should_continue` 先撞 no-work 上限退出。净效果：reset 每次都在「马上就要收工」的那一刻跑一次，然后循环结束。作者自己的测试 `tests/test_farm_ledger.py` 用 `Guards(max_failures=2, max_no_work=99)` 绕开了这个交互。

**新问题 5c（`tower_status` 不幂等，被当成读数用）**：`game.tower_status` 每次调用都先盲点一次塔入口。`:371-373` 已经进塔并读过一次，`:240-241` 第一轮又读一次（`index==1` → `read_floor_wanted=True`），`:265-266` 打完再读一次——**塔内多点两下 `TOWER_ENTRY=(1195,1150)`**，这是塔内部的随机像素（可能是楼梯/守护者）。`read_floor` 本意是只读，却在改变场景。

**无限循环？** 没有。`reset_between_rounds` 内无循环，每步都包了 try/except（`:216-222`），不会死循环也不会误伤账本。这条 OK。

---

## 6. `seerauto/farm/ledger.py` — 部分修复

**已修复**：`ledger.py:59-73` 的 `outcome` 不再从 `notes` 推导（那正是每轮都追加 frame 路径导致 NO_WORK 全变 FAILED 的根因，`tests/test_farm_ledger.py:152-157` 已钉住）。

**部分修复 6a（`blocked` 与 `error` 仍然重叠）**：`ledger.py:67-70` 里 `error` 优先于 `blocked`，两者都归 `ROUND_BLOCKED`——所以 `blocked` 标志对 outcome **零信息增量**；而且 `ROUND_FAILED` 现在**不可达**，`ledger.py:161-163` 是死分支。
**新问题 6b（计数仍与注释承诺相反）**：`ledger.py:164-166` 里 BLOCKED 的轮次**同时**涨 `no_work_streak` 和 `failure_streak`。而 `tools/farm_tower_repeat.py:249-251` 的注释写着「A refusal … is BLOCKED, not 'it ran and found nothing'. Keeping them apart is what stops a NO_WORK guard from firing on a configuration problem」——实际结果：**两轮配置性拒绝照样触发 `--max-no-work 2` 退出**。要兑现注释，BLOCKED 应当只涨 `failure_streak`、不涨 `no_work_streak`。
另外 `ledger.py:165` 里 NO_WORK 轮会把 `failure_streak` 清零，所以「连续两轮失败」必须两轮都是 BLOCKED 才会成立。

---

## 7. `seerauto/skills/act.py::GiveExp` — 部分修复

**已修复（主路径）**：`act.py:2096-2100` 的「打字后必须回读到同一个数字才按确认」和 `:2121-2124` 的「等级必须上升 / 池必须下降」两道闸是实的；`:2128-2136` 也把 `exp_giver.poured` 写进了节点谓词真正读的 `world.unlocked`（`tests/test_give_exp.py:179-194` 钉住）。

**部分修复 7a（重复灌注没有完全堵死）**：`act.py:2106-2113` 的新守卫是「标题读不到 **且** 正文标记也读不到 → 才 `_dismiss_dialogs`」。`_dismiss_dialogs`（`act.py:1022-1051`）点的仍是**全屏第一个 `确认`**。所以「面板还开着、但标题和 `该精灵增加/积累经验/升级所需经验值` 全被 OCR 丢掉」时，仍会点到本面板自己的确认 → 再灌一次。残留窗口比原来小得多，但没有归零。真正的封死做法是：恢复路径里排除落在分配器面板区域内的确认按钮，或干脆不做「点第一个确认」这种盲恢复。

**部分修复 7b（`_parse_allocator_body` 的正则）**：`act.py:1992-2009`
- 「等级：18」→ `split("：")[-1]="18"` → `level=18` ✓
- 「积累经验：18744927」→ `split("：")[-1]="18744927"` → `pool=18744927` ✓
两种格式**单独**出现时都对。
**但**它假定「一个 OCR item = 一行标签+数字」。若 OCR 把两行并成一条 `等级：18 积累经验：18744927`，`split("：")[-1]` 取到**最后一段**，`"等级" in text` 先命中 → `level = 18744927`、`pool` 保持 None（`elif` 被跳过）→ `act.py:2121` 的 `rose = 18744927 > 18` 为真 → **凭一个错读的数字伪造 PASS**。建议按「取第一个数字」而不是「最后一段」，或对合并行做二次切分。

**残留 7c**：兜底解析的 `pending_amount`（`act.py:2007-2008`，要求以 `g` 结尾）与主解析器 `seerauto/skills/parsers.py:1250-1278`（`^(\d[\d,]*)\D{0,2}$`，接受裸数字）规则不一致。技能的打字校验走的是主解析器，所以只是兜底与主路径口径不同，不是 bug，但值得统一。

---

## 8. `seerauto/skills/act.py::FarmTower` — 部分修复

**已修复（文本上分得开）**：`act.py:1912-1918` 现在区分 `guardian click refused: …` 与 `act.py:1919-1923` 的 `guardian click landed but no battle started (not in the tower interior?)`，两种故障的修法确实不同（前者是输入通道/焦点，后者是场景不对）。

**部分修复 8a（状态没分开，靠字符串兜）**：两条路径最终都返回 `Status.FAIL`（`act.py:1962-1965`），而全仓约定是「点击被拒 = `SkillResult.blocked(..., escalation="pause")`」（`seerauto/skills/base.py:176`、`act.py:1101`、`:1449`）。于是账本只能靠**字符串匹配**来还原语义：`tools/farm_tower_repeat.py:252-253` 的 `or "refused" in (res.reason or "").lower()`。改一个字（refused → rejected）分类就翻转。
建议：`FarmTower` 对 refused 直接返回 `Status.BLOCKED`，账本改判 `res.status`，字符串匹配只留作日志。

**新问题 8b（区分出来了但没人用）**：既然两种故障修法不同，`reset_between_rounds`（`:420-423`）就应该按原因选药——「refused」该做的是把窗口置前/退出，「not in the tower」该做的是重进塔。现在只按 `failure_streak` 无差别处理，而 reset 本身在塔内又无效（见 5a）。

---

## 9. `tools/explain_goal.py` — 部分修复

**已修复（不开 session/daemon）**：`tools/explain_goal.py:46-91` 的 `collect()` 只用 `GoalGraph.load()`（读 YAML）+ `WorldStore`（sqlite，`seerauto/world/store.py` 只 import json/os/sqlite3/threading/uuid）；`available_capabilities()`（`seerauto/goals/graph.py:228-236`）也只读注册表。整条链**没有** `live` 导入，不会再开第二条采集流 ✓。

**已修复（拆分结构）**：`tools/explain_goal.py:101-115` 的 `unblocks`/`blocks` 判定逻辑本身是对的：有其它 unmet 原因 → `blocks`，只有能力缺口 → `unblocks`。

**新问题 9a（`unblocks` 六条全部是不可执行节点）**：`tools/explain_goal.py:43` 的 `NOT_SCHEDULABLE` 是按**状态字符串**过滤的，而 `seerauto/goals/graph.py:283-292` 的顺序是 `blocked → unknown → not_executable`：**YAML 里 `status: unknown` 的节点永远不会返回 `not_executable`**（被 `unknown` 盖住），于是它们穿过滤网进了 `unblocks`。
用提交里的 `data/overnight/plan.json` 实测：`capability_unblocks` 的 6 条（`shop.buy`/`puni.panel`/`catch.pet`/`battle.skill_select`/`battle.defeat_branch`/`daily.quota`）对应的节点 `executable` **全是 false**（`puni.roster.kelida_awaken`、`puni.crosscut.defeat_retry`、`puni.prepare.buy_potions`、`puni.roster.spt_saiweier`）。而 runner 只调度 `node.executable` 的节点（`seerauto/runner/runner.py:585`、`:1050`、`:1071`）。
所以「adding one of these would let a node run immediately」**6/6 为假**。唯一 executable 且缺能力的节点是 `puni.truebody.detect_life`，但它同时缺前置节点（按本工具自己的定义属于 `blocks`）。
**结论：当前目标图下「补一个能力就能立刻开跑」的真实答案应该是 0 条。** 修法一行：用 `r["executable"]`（行里已经有了）而不是状态字符串来过滤。
（`docs/OVERNIGHT_PLAN_2026_11_05.md:41-45` 的文字结论其实是对的，但工具输出和 `plan.json` 这份给规划器读的产物仍在自相矛盾。）

**新问题 9b（一个节点缺两个能力，被数成两条「unblocks」）**：`puni.roster.kelida_awaken` 缺 `catch.pet` + `battle.skill_select`，两个都进了 `unblocks`；`puni.crosscut.defeat_retry` 同理。补其中一个并不能让它跑。建议：`unblocks` 只收「恰好缺 1 个能力」的节点，其余归 `blocks` 并标 `needs N`。

---

## 跨条目的新问题汇总（不属于某一编号）

1. **`data/overnight/bg_probe.json` 不存在**（见 1a）。三处引用（`AGENTS.md` 5.6、`tools/farm_tower_repeat.py:337`、`tools/login.py:102`）现在指向空文件。要么用新代码真跑一次 A/B 落盘，要么把这些引用改成「尚未复验」。
2. **`tools/farm_tower_repeat.py:336-338` 的 REFUSED 依据同上**：在一个没有可采信证据的结论上，工具直接拒绝后台通道（行为上是对的，但理由是空头支票）。
3. **`tools/login.py:40` 死代码 + 与 `:35-39` 注释矛盾**（见 2c）。
4. **`docs/OVERNIGHT_PLAN_2026_11_05.md:31`** 仍把「含 A/B 对照」写成已完成项，实际只有一次 `--check` 观察。按 AGENTS.md 5.3，应改成「判据已就位，A/B 未跑」。

## 建议的最小修复顺序（按性价比）

1. `tools/login.py:228` —— 给 `seerdx.swift` 加 cmd+a（或退格 N 次），否则「进服」这条路带着 `a1` 走。
2. `tools/explain_goal.py:43` —— 改用 `executable` 过滤；`unblocks` 要求恰好缺 1 个能力。这条会让 `plan.json` 立刻变成诚实的。
3. `tools/farm_tower_repeat.py:205-224` —— 在没有「出塔」机制之前，删掉或明确标注 reset 无效；并把触发条件从 `failure_streak == 2` 改成「因场景错误失败」而非「任何失败」。
4. `seerauto/farm/ledger.py:164-166` —— BLOCKED 不涨 `no_work_streak`，兑现 `farm_tower_repeat.py:249-251` 的注释。
5. `seerauto/skills/act.py:1962-1965` —— refused 返 `Status.BLOCKED`，账本改判状态而非字符串。
6. `tools/login.py:45` —— 把「公屏聊天 • 在线」加进 PRIMARY；把「版本检测中」从 loading 里拆出来。
7. `tools/login.py:189` —— 拖拽前补 frontmost + 几何校验。
8. `tools/bg_probe.py` —— 证据文件改 append-only；前台臂前提示「需要游戏窗口在最前」；然后**真跑一次**并把 `bg_probe.json` 提交进仓库。

（本轮未修改任何文件；全量测试 637 passed / 2 skipped。）