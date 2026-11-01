import sys, time, subprocess, re
sys.path.insert(0,'scripts')
import play
im, win, scale = play.grab()
pts = [(x,y) for y in (940,1034) for x in (393,493,587,692,798,903)]
for (x,y) in pts:
    gx, gy = play.local2global(x, y, win, scale)
    subprocess.run([play.CTL,'mouse',str(gx),str(gy),'move'],capture_output=True)
    time.sleep(0.75)
    play.grab(activate=False)
    items = play.ocr(play.GAME, 0.4)
    cands = [i['text'] for i in items if re.match(r'^\s*\d+[\.、]', i['text']) or '星' in i['text'] or '封印' in i['text'] or '谱' in i['text']]
    print(f'({x},{y}) =>', ' || '.join(cands[:4]))
