#!/bin/bash
# usage: shoot.sh [output.png]  -> activates SeerGame, captures full screen, crops to game window
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$DIR")"
OUT="${1:-$ROOT/artifacts/game.png}"
TMP="$ROOT/artifacts/full_screen.png"
WIN=$("$DIR/seerctl" wl | python3 -c "
import json,sys
ws=json.load(sys.stdin)
for w in ws:
    if w['owner']=='SeerGame' and w['layer']==0:
        b=w['bounds']; print(int(b['x']),int(b['y']),int(b['w']),int(b['h'])); break
")
if [ -z "$WIN" ]; then echo "SeerGame window not found"; exit 1; fi
set -- $WIN
"$DIR/seerctl" activate SeerGame >/dev/null
sleep 0.7
screencapture -x "$TMP"
python3 - "$TMP" "$OUT" "$1" "$2" "$3" "$4" <<'PY'
import sys
from PIL import Image
src, out = sys.argv[1], sys.argv[2]
x, y, w, h = map(int, sys.argv[3:7])
im = Image.open(src).convert('RGB')
sw, sh = im.size
scale = 2.0 if sw >= 2880 else 1.0
im.crop((int(x*scale), int(y*scale), int((x+w)*scale), int((y+h)*scale))).save(out)
print(f"captured {im.size} -> cropped {out} ({w}x{h}@{scale}x)")
PY
