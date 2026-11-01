import sys, time
sys.path.insert(0,'scripts')
import play
needles = sys.argv[1:]
im, win, scale = play.grab()
items = play.ocr(play.GAME, 0.4)
for it in items:
    if any(n in it['text'] for n in needles):
        px, py = play.center(it)
        print(f"{it['text'][:24]:26s} local=({px:.0f},{py:.0f}) conf={it['conf']:.2f}")
