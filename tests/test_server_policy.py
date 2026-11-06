"""Exercise actual login lookup with changing OCR, without GUI input."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock
spec=importlib.util.spec_from_file_location('login_policy',Path(__file__).parents[1]/'tools/login.py')
login=importlib.util.module_from_spec(spec);spec.loader.exec_module(login)
def session(frames):
 s=Mock();s.ocr.side_effect=frames
 s.find_text.side_effect=lambda items,aliases:next((i for i in items if any(a in i['text'] for a in aliases)),None)
 return s
def test_unknown_screen_never_drags(monkeypatch):
 s=session([[{'text':'航行公告','px':[4,4]}]])
 drag=Mock();monkeypatch.setattr(login,'drag_server_list',drag)
 assert login.find_server(s,'啦公星',tries=1) is None
 drag.assert_not_called()
def test_other_star_does_not_match_requested_server(monkeypatch):
 s=session([[{'text':'选择服务器','px':[1,1]},{'text':'其他公星','px':[8,8]}]])
 monkeypatch.setattr(login,'drag_server_list',Mock());monkeypatch.setattr(login.time,'sleep',lambda _:None)
 assert login.find_server(s,'啦公星',tries=1) is None
def test_visible_exact_target_needs_no_scroll(monkeypatch):
 s=session([[{'text':'选择服务器','px':[1,1]},{'text':'啦公星','px':[8,8]}]])
 drag=Mock();monkeypatch.setattr(login,'drag_server_list',drag)
 assert login.find_server(s,'啦公星',tries=1)==(8,8)
 drag.assert_not_called()
def test_scene_change_after_scroll_stops(monkeypatch):
 s=session([[{'text':'选择服务器','px':[1,1]}],[{'text':'正在加载星球资源','px':[1,1]}]])
 drag=Mock();monkeypatch.setattr(login,'drag_server_list',drag);monkeypatch.setattr(login.time,'sleep',lambda _:None)
 assert login.find_server(s,'啦公星',tries=3) is None
 assert drag.call_count==1

def test_default_login_does_not_type_server_one(monkeypatch):
 s=Mock();s.client=Mock();s.ocr.return_value=[]
 monkeypatch.setattr(login, 'list_game_windows', lambda: [{}])
 monkeypatch.setattr(login, 'clear_popup', lambda: True)
 monkeypatch.setattr(login, 'LiveSession', lambda **kwargs: Mock(open=lambda:s))
 monkeypatch.setattr(login, 'screen_text', lambda _: ['选择服务器'])
 monkeypatch.setattr(login, 'find_server', lambda *a,**k: None)
 numeric=Mock();monkeypatch.setattr(login,'enter_server_by_number',numeric)
 monkeypatch.setattr(login.time,'sleep',lambda _:None)
 ticks=iter([0,0,1000]);monkeypatch.setattr(login.time,'time',lambda:next(ticks))
 monkeypatch.setattr('sys.argv',['login.py'])
 login.main()
 numeric.assert_not_called()

def test_any_visible_server_uses_measured_rows_and_skips_closed_pvp():
 import json
 raw=json.loads((Path(__file__).parent/'fixtures/server_history_ocr.json').read_text())
 items=[{'text':i['text'],'conf':i['conf'],'norm':(i['x'],i['y'],i['w'],i['h']),
         'px':((i['x']+i['w']/2)*2400,(1-i['y']-i['h']/2)*1600)} for i in raw]
 chosen=login.visible_server(items)
 assert chosen['number']==26
 assert chosen['text']=='123'
 assert chosen['px'][0] > 400

def test_any_visible_server_rejects_missing_layout_anchor():
 assert login.visible_server([{'text':'选择服务器','px':[1,1]}]) is None
