#!/bin/bash
# tools/mmx.sh — invoke MiniMax through OpenCode in non-interactive mode.
#
# Why the XDG redirect: OpenCode writes its log/db under ~/.local/share/opencode.
# Under the DSH workspace-write sandbox that path is not writable, and OpenCode dies with
#   PlatformError: Unknown: FileSystem.open (.../opencode/log/opencode.log)
# before it can answer anything.  Pointing the four XDG dirs inside the workspace keeps
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
# OpenCode v2 also writes its service registry under XDG_STATE_HOME; without this it
# dies with EPERM: ~/.local/state/opencode/service.json (2026-11-09).
export XDG_STATE_HOME="$ROOT/.ocxdg/state"
# The minimax-cn-* providers are keyed by MINIMAX_API_KEY.  The key already lives in
# OpenCode's auth store, but OpenCode v2 only reads it when its data dir is the real
# one, which is read-only under the DSH sandbox.  Export it explicitly instead of
# printing it; nothing here writes the secret anywhere.
if [ -z "${MINIMAX_API_KEY:-}" ] && [ -f "$HOME/.local/share/opencode/auth.json" ]; then
  MINIMAX_API_KEY="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["minimax-cn-coding-plan"]["key"])' "$HOME/.local/share/opencode/auth.json" 2>/dev/null)"
  export MINIMAX_API_KEY
fi
MODEL="${MMX_MODEL:-minimax-cn-coding-plan/MiniMax-M3.1-Flash}"
cd "$ROOT" || exit 1
exec opencode run --standalone --model "$MODEL" --format json --auto "$@"
