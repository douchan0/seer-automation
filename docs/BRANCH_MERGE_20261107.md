# 2026-11-07 分支合并与暂停交接

用户要求：以 `night/20261106-dual-mode` 为最新基准，完成其他分支合并后合入 `main`，然后暂停。

## 基准和合并范围

- 最新运行/报告基准：`fcb7ae3`（1106 dual-mode）。
- 原 main `ba83c05`、1105-puni-v2 `73299e0` 已在该基准历史中。
- 先合入 `night/20261105-puni`（`353cfff`）；其已包含 `fix/dsh-overnight-outcomes-and-runtime`（`3c46a3b`）。合并提交 `6931d75`。
- 再合入本地独立选服分支 `codex/20261107-server-policy`（`7b1bb71`，三项修复提交）。合并提交 `1096d6b`。
- 两次均为保留分支历史的实际 merge。main 可从原提交直接快进到合并后的 dual-mode，两个 remote 分支将指向同一最终提交。不删除旧分支、不重写其历史。

## 冲突处理

- 保留1106基准的 `runner/outcome.py`、现有CLI退出码、supervisor、登录错误退避、精确世界识别、当前账号证据和暂停checkpoint实现。
- 不引入旧分支的另一套 `outcomes.py` 协议；把其CI及测试校准到基准已有接口。
- 合入仍缺少的后台指针 `movepid` 派发，以及控制器默认pause/resume回调，让技能内部的合作式暂停也走既有checkpoint方法。移除自动合并产生的重复clear_stop定义。
- 旧运行手册归档为 `docs/archive/DSH_OVERNIGHT_RUNBOOK_20261105.md`，明确旧选项和旧API不能直接作为当前操作手册。
- 保留基准对机器本地 `.pydeps` 链接和SQLite修复备份的移除，不从旧分支重新引入。
- 选服修复按原独立分支合入；29项离线验证通过，未做真机重新登录。

## 验证记录

- 控制/登录/结果/后台探针专项：119 passed。
- 选服/登录专项：29 passed。
- 初次全套：35 failed / 870 passed / 3 skipped。逐项失败均显示读取真实master.pause或stop而提前PAUSED，涉及24项塔测试及11项捕捉测试。
- 最小测试隔离：仅修改塔和捕捉假执行器的测试辅助函数，使用每项测试的临时控制路径；保留显式pause/stop测试路径和全部断言。真实暂停标记未删除、未暂移。
- 隔离后塔/捕捉专项：67 passed / 1 skipped（clean clone不含忽略的实时塔截图）。
- 最终全量：**905 passed / 3 skipped（42.67s）**。跳过项为未构建原生seerdx的两项检查，以及clean clone不含实时塔截图的一项回放；未开展新真机测试。

## 继续暂停

这是分支整合与离线验证，未恢复游戏实验。此前A主动选技、B结算确认、C跨层识别仍未通过真机验收；合并不改变其状态。`/tmp/seerauto.master.pause` 和 `/tmp/seerauto.stop` 保持在位。只有用户明确说“开始”后才可恢复实验。
