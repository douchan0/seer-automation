import sys, time, json
sys.path.insert(0,'scripts')
import fast
d = fast.Driver()
pts = [(x,y) for y in (945,1035) for x in (427,530,640,745,855,960)]
res = {}
for i,(x,y) in enumerate(pts):
    d.click_local(1129,1300); time.sleep(1.3)
    d.click_local(x,y); time.sleep(2.2)
    items = d.capture(0.4)
    d.shot(activate=False)
    txt = ' '.join(i2['text'] for i2 in items)
    img = f'artifacts/scene_idx_{i}.png'
    import shutil; shutil.copy('artifacts/shot.png', img)
    res[i] = {'xy': [x,y], 'text': txt[:300], 'img': img}
    print(i, (x,y), txt[:120])
json.dump(res, open('artifacts/scene_index.json','w'), ensure_ascii=False, indent=1)
d.close()
