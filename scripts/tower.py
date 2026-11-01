import sys, time
sys.path.insert(0,'scripts')
import fast

CONFIRM = ('确认', '确定', '我知道了', '确i')
SKILL_POS = (1151, 1281)
FIGHT_BTN = (1789, 1103)
ENEMY_POS = (1178, 730)

def clear_dialogs(d, rounds=10, quiet=False):
    cleared = 0
    for _ in range(rounds):
        items = d.capture(0.3)
        hit = None
        for x in items:
            if any(k in x['text'] for k in CONFIRM):
                hit = (round((x['x'] + x['w'] / 2) * 2298), round((1 - (x['y'] + x['h'] / 2)) * 1428))
                break
        if not hit:
            break
        d.click_local(*hit)
        cleared += 1
        time.sleep(0.9)
    return cleared

def battle_state(d):
    t = ' '.join(i['text'] for i in d.capture(0.3))
    if '胜利' in t or '失败' in t or '获得经验' in t:
        return 'end'
    if '当前回合' in t:
        return 'battle'
    return 'idle'

def farm(d, floors=10, log=print):
    for f in range(floors):
        clear_dialogs(d)
        started = False
        for attempt in range(3):
            d.click_local(*ENEMY_POS); time.sleep(2.6)
            st = battle_state(d)
            if st == 'battle':
                started = True; break
            clear_dialogs(d)
            time.sleep(0.5)
        if not started:
            log(f'[floor {f}] cannot start battle'); return False
        for turn in range(40):
            d.click_local(*FIGHT_BTN); time.sleep(0.25)
            d.click_local(*SKILL_POS); time.sleep(1.35)
            st = battle_state(d)
            if st == 'end':
                break
        time.sleep(0.6)
        n = clear_dialogs(d)
        log(f'[floor {f}] battle done, dialogs cleared={n}')
    return True

if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    d = fast.Driver()
    farm(d, n)
    d.close()

def finish_battle(d, max_turns=40, log=print):
    for turn in range(max_turns):
        t = ' '.join(i['text'] for i in d.capture(0.3))
        if '当前回合' not in t:
            break
        d.click_local(*FIGHT_BTN); time.sleep(0.25)
        d.click_local(*SKILL_POS); time.sleep(1.35)
    return clear_dialogs(d)
