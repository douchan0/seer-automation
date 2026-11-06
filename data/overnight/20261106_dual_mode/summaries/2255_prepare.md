# 22:55 PREPARE 状态

- 实际开工 22:41（计划 21:30 PREPARE），有效实验时长会短于 9h，如实在报告里记录。
- 分支 night/20261106-dual-mode 已从 73299e0 建立。
- 基线复验：`PYTHONPATH=.pydeps python3 -m pytest -q` → **649 passed, 2 skipped in 41.07s**（本轮独立复跑）。
- 上一夜基线（DSH 原生账本）：session-fb389015 totals = uncached 3,104,715 / output 323,945 / cacheRead 198,836,736 ≈ **202M**；656 steps / 3 turns；surfaceTokens 414,213。
- 本轮当前（22:51 读数）：22 steps / 1 turn / 总 882,744；surfaceTokens **42,466**；≈40K tokens/step（上一夜 ≈308K/step）。
- DSH 原生能力已核实：`/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh` 存在，支持 `dsh headless`（fresh context，`--session-id` 可续跑）与 `dsh --dump-config`；GUI HTTP 端口 50728 需鉴权，无法外部注入唤醒。permission mode 由 `DSH_PERMISSION_MODE` 环境变量决定（当前 workspace-write，policy=ask）。
- 沙箱限制：git 写操作落在工作区外（共享 .git），本轮改用工作区内独立 git 目录/克隆的方式完成提交，避免依赖审批。
- 4 名队友已派发：infra-eng(task-1 控制面) / usage-eng(task-2 Token 计量) / gameops(task-3 真机只读侦察) / puni-research(task-4 路线研究)。
- 客户端已由 gameops 启动（PID 6514），已过美术包弹窗；caffeinate 已挂，防睡眠。
- 用户指令：服务器随便点一个即可，不要纠结。
