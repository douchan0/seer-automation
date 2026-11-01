#!/bin/bash
# Build the native ScreenCaptureKit capture/input daemon.
#
# Two flags here are not optional on a sandboxed/CLT-only host:
#   -module-cache-path  : the default ModuleCache under /var/folders is not
#                         writable, so swiftc fails with "Operation not permitted"
#   DSH/Swift also trips on @main in a lone file (that one needs -parse-as-library,
#   not needed here because this file uses top-level code deliberately).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/native/seerdx.swift"
OUT="$ROOT/native/bin/seerdx"
MC="${TMPDIR:-/tmp}/seerdx_modulecache"

mkdir -p "$ROOT/native/bin" "$MC"

echo "building $OUT"
swiftc -O \
  -module-cache-path "$MC" \
  -o "$OUT" "$SRC" \
  -framework ScreenCaptureKit \
  -framework CoreGraphics \
  -framework CoreVideo \
  -framework Vision \
  -framework AppKit \
  -framework ImageIO

echo "ok: $OUT"
"$OUT" --help >/dev/null 2>&1 || true
