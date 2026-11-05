你是严格但建设性的代码审查者。仓库：/Users/ceontou/Documents/code/seer-automation-night
这是**第二轮审查**：你上一轮提了 12 个 BLOCKER，作者已逐条修。现在请只审**修复本身**。

命令：`git log --oneline -10`、`git diff 7c6f2b8^..HEAD`（或逐个 commit 看）。

重点复核这 9 条修复是否**真的修好了**，有没有引入新问题：

1. `tools/bg_probe.py` 重写：三态判决 + 同代码双通道。检查 `verdict_for` 是否还存在"无证据也下判决"的路径；
   `left_dirty` 的语义是否正确；`--check` 是否可能覆盖真证据。
2. `tools/login.py`：`screen_kind` 新增 `loading` 类且优先级最高；`PRIMARY_WORLD_MARKERS` 删掉
   `XIN计划，启动`/`当前回合` 后，**是否又变得太严**（真世界也可能读不到 `卫兵`/`已连接公屏聊天`？）。
3. `tools/login.py::drag_server_list`：game_px→global 转换是否正确，是否还有别处混用坐标。
4. `tools/login.py::enter_server_by_number`：`_field_shows` 的容差（±220/±40 px）是否可靠，cmd+A 用 `key code=0` 是否真的是全选。
5. `tools/farm_tower_repeat.py`：进塔顺序（travel→tower_status→farm）是否正确？`reset_between_rounds` 是否可能无限循环或误伤？
6. `seerauto/farm/ledger.py`：`blocked` 与 `error` 的语义是否重叠、计数是否仍然正确。
7. `seerauto/skills/act.py::GiveExp` + `_allocator_body_still_there`：重复灌注路径是否真的堵死？
   `_parse_allocator_body` 的正则能否处理「等级：18」与「积累经验：18744927」两种格式？
8. `seerauto/skills/act.py::FarmTower`：现在会检查 `_click` 返回值，`refused` 与 `landed but no battle` 是否分得清。
9. `tools/explain_goal.py` 重写：`unblocks`/`blocks` 的拆分是否正确，是否仍会 open 任何 session/daemon。

输出格式：每条给 `已修复` / `部分修复` / `未修复` / `新问题`，并给出文件:行号与具体理由。
禁止修改任何文件，只输出 Markdown 报告。务实、简短，不要复述背景。
