#!/bin/bash
# tools/mmx.sh — invoke MiniMax through OpenCode in non-interactive mode.
#
# Why the XDG redirect: OpenCode writes its log/db under ~/.local/share/opencode.
# Under the DSH workspace-write sandbox that path is not writable, and OpenCode dies with
#   PlatformError: Unknown: FileSystem.open (.../opencode/log/opencode.log)
# before it can answer anything.  Pointing the three XDG dirs inside the workspace keeps
# the tool runnable without widening the sandbox.
#
# Usage:
#   tools/mmx.sh "prompt text"
#   tools/mmx.sh --file docs/research/foo.md "prompt text"
#   MMX_MODEL=minimax-cn-coding-plan/MiniMax-M3 tools/mmx.sh "..."
#
# The caller is responsible for the timeout; run long tasks in the background.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export XDG_DATA_HOME="$ROOT/.ocxdg/data"
export XDG_CONFIG_HOME="$ROOT/.ocxdg/config"
export XDG_CACHE_HOME="$ROOT/.ocxdg/cache"
MODEL="${MMX_MODEL:-minimax-cn-coding-plan/MiniMax-M3.1-Flash}"
cd "$ROOT" || exit 1
exec opencode run --model "$MODEL" --format json --auto "$@"
