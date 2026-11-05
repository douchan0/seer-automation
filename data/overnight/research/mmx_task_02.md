你是严格但建设性的代码审查者。仓库：/Users/ceontou/Documents/code/seer-automation-night
分支：night/20261105-puni-v2（基于本地 main ba83c05）

请审查最近 5 个 commit（git log --oneline -5，git diff ba83c05..HEAD），重点：

1. tools/bg_probe.py —— 后台输入探针。判定逻辑是否可能误报 PASS？证据是否足够支撑
   `BACKGROUND_INPUT_BLOCKED`？A/B 对照写在同一份 JSON 里是否恰当？
2. tools/farm_tower_repeat.py + seerauto/farm/ledger.py —— 可重复刷塔账本。
   停止条件是否可能整夜空转？暂停是否会被误判为失败？账本能否在崩溃后恢复？
   `--rounds`/`--max-rounds` 语义是否清晰？有没有"假装有进展"的路径？
3. seerauto/skills/act.py::GiveExp —— 经验灌注技能。
   有没有可能重复灌注（一次点击变两次）？确认前校验够不够？金额上限处理是否正确？
4. tools/login.py 的 screen_kind / confirmed_world / enter_server_by_number —— 判据是否还有漏洞？
5. tools/explain_goal.py —— 诊断工具的统计是否会把"能力缺口"算错。

对每个文件给出：
- BLOCKER（必须改，会导致真机事故或谎报）
- MAJOR（应该改）
- MINOR（可选）
每条给出：文件:行号、问题、具体修复建议（不要泛泛而谈）。

禁止修改任何文件，只输出审查报告 Markdown。
