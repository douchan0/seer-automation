import json, sys
d = json.load(open('frames/_ocr.json'))
ch = sys.argv[1]
lo = int(sys.argv[2]) if len(sys.argv) > 2 else 0
hi = int(sys.argv[3]) if len(sys.argv) > 3 else 9999
rows = [r for r in d if r['chapter'] == ch]
rows.sort(key=lambda r: r['ts'])
for r in rows[lo:hi]:
    lines = {}
    for b in r['boxes']:
        if b['conf'] < 0.4: continue
        key = round(b['y'], 2)
        lines.setdefault(key, []).append(b)
    out = []
    for k in sorted(lines, reverse=True):
        bs = sorted(lines[k], key=lambda b: b['x'])
        out.append(''.join(b['text'] for b in bs))
    print(f"--- t={r['ts']:.0f}s {r['file']}")
    print(' / '.join(out))
