#!/usr/bin/env python3
"""Fast driver for the XIN计划 client: talks to seerd daemon over a pipe."""
import json, os, subprocess, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEERD = os.path.join(ROOT, 'scripts', 'seerd')
SHOT = os.path.join(ROOT, 'artifacts', 'shot.png')
_log_path = os.path.join(ROOT, 'docs', 'action-log.md')

class Driver:
    def __init__(self, pin=True):
        self.p = subprocess.Popen([SEERD], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True, bufsize=1)
        if pin:
            self.send({'op': 'pin', 'x': 300, 'y': 120, 'w': 1149, 'h': 714})
        self.win = self.send({'op': 'win'})
        self.scale = 2.0

    def send(self, obj):
        self.p.stdin.write(json.dumps(obj) + '\n')
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        try:
            return json.loads(line)
        except Exception:
            return {'error': 'bad reply', 'raw': line}

    def close(self):
        try:
            self.send({'op': 'quit'})
            self.p.terminate()
        except Exception:
            pass

    # --- primitives ---
    def activate(self):
        return self.send({'op': 'activate'})

    def shot(self, path=SHOT, activate=False):
        r = self.send({'op': 'shot', 'path': path, 'activate': activate})
        self.win = self.send({'op': 'win'})
        return r

    def ocr(self, path=SHOT, minconf=0.35):
        return self.send({'op': 'ocr', 'path': path, 'min': minconf}).get('items', [])

    def capture(self, minconf=0.35, activate=True):
        self.shot(SHOT, activate=activate)
        return self.ocr(SHOT, minconf)

    def click_local(self, px, py, n=1):
        return self.send({'op': 'clicklocal', 'x': px, 'y': py, 'n': n, 'scale': self.scale})

    def click_text(self, needle, minconf=0.4, timeout=6.0, exact=False, nth=0):
        """click the first OCR hit containing needle; retries until timeout"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            items = self.capture(minconf)
            hits = [i for i in items if (i['text'] == needle) if exact] if exact else \
                   [i for i in items if needle in i['text']]
            if hits:
                it = hits[min(nth, len(hits) - 1)]
                px = (it['x'] + it['w'] / 2) * 2298
                py = (1 - (it['y'] + it['h'] / 2)) * 1428
                self.click_local(px, py)
                return it['text']
            time.sleep(0.15)
        return None

    def wait_text(self, needle, timeout=8.0, minconf=0.4):
        t0 = time.time()
        while time.time() - t0 < timeout:
            items = self.capture(minconf)
            if any(needle in i['text'] for i in items):
                return True
            time.sleep(0.2)
        return False

    def lines(self, minconf=0.35):
        items = self.capture(minconf)
        return group(items)

    def reset(self, pos=(427, 1035), wait=2.2):
        """travel via map to reset all panels (pos = map grid cell)"""
        self.click_local(1129, 1300); time.sleep(1.3)
        self.click_local(pos[0], pos[1]); time.sleep(wait)

    def key(self, code):
        return self.send({'op': 'key', 'code': code})

    def text_input(self, s):
        return self.send({'op': 'type', 'text': s})

def group(items):
    items = sorted(items, key=lambda i: -i['y'])
    lines = []
    for it in items:
        cy = it['y'] + it['h'] / 2
        for ln in lines:
            if abs(ln['cy'] - cy) < 0.014:
                ln['items'].append(it)
                ln['cy'] = sum(i['y'] + i['h'] / 2 for i in ln['items']) / len(ln['items'])
                break
        else:
            lines.append({'cy': cy, 'items': [it]})
    for ln in lines:
        ln['items'].sort(key=lambda i: i['x'])
        ln['text'] = ' '.join(i['text'] for i in ln['items'])
    lines.sort(key=lambda l: -l['cy'])
    return lines

def log(entry, path=_log_path):
    ts = time.strftime('%H:%M:%S')
    with open(path, 'a', encoding='utf-8') as f:
        f.write(f'- {ts} {entry}\n')

def save_thumb(src, dst, size=(1000, 620)):
    from PIL import Image
    im = Image.open(src)
    im.thumbnail(size)
    im.save(dst)

def hover(self, px, py):
    return self.send({'op': 'move', 'x': px, 'y': py, 'scale': self.scale})

Driver.hover = hover

def click_hover_button(self, px, py, labels=('前往', '挑战', '进入', '领取'), wait=0.9):
    """hover a card then click the revealed action button"""
    self.hover(px, py)
    time.sleep(wait)
    items = self.capture(0.4)
    for it in items:
        if any(lbl in it['text'] for lbl in labels):
            cx = (it['x'] + it['w'] / 2) * 2298
            cy = (1 - (it['y'] + it['h'] / 2)) * 1428
            self.click_local(cx, cy)
            return it['text']
    return None

Driver.click_hover_button = click_hover_button
