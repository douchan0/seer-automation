import sys, time, json
sys.path.insert(0, 'scripts')
import play
im, win, scale = play.grab()
items = play.ocr(play.GAME)
base = set(i['text'] for i in items)
pts = json.loads(sys.argv[1])
import subprocess
for (x, y) in pts:
    gx, gy = play.local2global(x, y, win, scale)
    subprocess.run([play.CTL, 'mouse', str(gx), str(gy), 'move'], capture_output=True)
    time.sleep(1.1)
    play.grab(activate=False)
    new = play.ocr(play.GAME)
    texts = [i['text'] for i in new if i['text'] not in base]
    print(f'({x},{y}) ->', ' | '.join(texts[:8]))
