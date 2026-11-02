#!/usr/bin/env bash
# bootstrap_mac.sh — one-shot setup for SeerAutomation on a clean macOS checkout.
#
#   ./scripts/bootstrap_mac.sh            # install deps + build the native daemon + doctor
#   ./scripts/bootstrap_mac.sh --check    # verify only, install nothing
#
# What it does (and why):
#   1. checks the macOS / python / swift facts the project actually depends on;
#   2. installs the Python deps into a workspace-local .pydeps (a user-level
#      `pip install` is refused by the sandbox on this machine);
#   3. builds native/bin/seerdx (ScreenCaptureKit capture + CGEvent input).
#      CGDisplayCreateImage/CGWindowListCreateImage are *removed* in macOS 15,
#      so SCK is the only capture path and the daemon is mandatory;
#   4. runs `python3 -m seerauto doctor`, which reports what is still missing
#      (screen-recording permission, accessibility, game window) instead of
#      pretending everything is fine.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$*"; }
die() { printf '\033[31mxx %s\033[0m\n' "$*" >&2; exit 1; }

say "1/5 environment"
[[ "$(uname -s)" == "Darwin" ]] || die "macOS only (this project drives a macOS Electron client)"
sw_vers -productVersion | sed 's/^/   macOS /'
echo "   arch      $(uname -m)"
command -v python3 >/dev/null || die "python3 not found"
echo "   python    $(python3 -V 2>&1)"
command -v swiftc >/dev/null || die "swiftc not found (install Xcode Command Line Tools)"
echo "   swift     $(swiftc --version | head -1)"
[[ -d /Applications/SeerGame.app ]] && echo "   game       /Applications/SeerGame.app" \
    || warn "game client not found at /Applications/SeerGame.app (you can still run dry runs)"

say "2/5 python dependencies"
if [[ ! -d .pydeps ]]; then
  [[ $CHECK_ONLY -eq 1 ]] && die ".pydeps missing (run without --check to install)"
  python3 -m pip install --quiet --target .pydeps -r requirements.txt
  echo "   installed into .pydeps"
else
  echo "   .pydeps present"
fi
PYTHONPATH=.pydeps python3 -c "import numpy, cv2, yaml, PIL" \
  || die "deps incomplete: expected numpy, opencv-python-headless (cv2), PyYAML, Pillow"
echo "   numpy/cv2/yaml/PIL import OK"

say "3/5 native daemon (ScreenCaptureKit)"
if [[ $CHECK_ONLY -eq 1 ]]; then
  [[ -x native/bin/seerdx ]] && echo "   native/bin/seerdx present" \
      || die "native/bin/seerdx missing (run without --check to build)"
else
  ./tools/build_seerdx.sh
fi

say "4/5 tests"
PYTHONPATH=.pydeps python3 -m pytest -q || die "test suite failing — fix before driving the game"

say "5/5 doctor"
PYTHONPATH=.pydeps python3 -m seerauto doctor || true

cat <<'EOF'

Next:
  # read-only: observe + plan, never sends input
  PYTHONPATH=.pydeps python3 -m seerauto goal start puni --planner deterministic --dry-run

  # REAL input (only after you accept the risk — see docs/USER_GUIDE.md §安全)
  PYTHONPATH=.pydeps python3 -m seerauto goal start puni --planner ai --allow-input \
      --max-minutes 60 --max-battles 12 --max-spend 0

  PYTHONPATH=.pydeps python3 -m seerauto goal status
  PYTHONPATH=.pydeps python3 -m seerauto goal report

⚠  The Xin server's login notice says third-party scripts are not allowed.
   Automation is at your own risk; this project never bypasses game limits.
EOF
