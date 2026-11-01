import sys, time
sys.path.insert(0,'scripts')
import fast, tower

def main(n):
    d = fast.Driver()
    log = lambda m: (print(m, flush=True), fast.log(m))[1]
    for f in range(n):
        tower.clear_dialogs(d)
        started = False
        for pos in [(1178,730),(1178,690),(1178,770)]:
            d.click_local(*pos); time.sleep(2.4)
            t = ' '.join(x['text'] for x in d.capture(0.25))
            if '当前回合' in t:
                started = True; break
            if '失败' in t or '挑战结束' in t:
                log('tower run ended (loss)'); return
            tower.clear_dialogs(d)
        if not started:
            log(f'[f{f}] cannot engage - stop'); return
        for turn in range(40):
            t = ' '.join(x['text'] for x in d.capture(0.22))
            if '当前回合' not in t:
                break
            d.click_local(*tower.FIGHT_BTN); time.sleep(0.18)
            d.click_local(*tower.SKILL_POS); time.sleep(1.15)
        time.sleep(0.4)
        nclr = tower.clear_dialogs(d)
        log(f'[f{f}] floor cleared (dialogs={nclr})')
    d.close()

if __name__ == '__main__':
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
