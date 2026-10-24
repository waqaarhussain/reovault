import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store
from realtime import Realtime


def test_realtime_config_is_private_lazy_and_preserves_main_stream(tmp_path,monkeypatch):
    store=Store(tmp_path/'data');binary=tmp_path/'go2rtc';binary.write_text('test')
    monkeypatch.setenv('REOVAULT_GO2RTC',str(binary));monkeypatch.setenv('REOVAULT_WEBRTC_IP','203.0.113.7')
    starts=[]
    class Process:
        def __init__(self,*args,**kwargs):self.done=False;starts.append((args,kwargs))
        def poll(self):return 0 if self.done else None
        def terminate(self):self.done=True
        def wait(self,timeout):return 0
    monkeypatch.setattr('realtime.subprocess.Popen',Process)
    rtc=Realtime(store)
    camera={'id':'a','host':'home.example','port':2004,'username':'admin','password':'private','path':'/Preview_01_main','compatible':1}
    rtc.sync({'a':camera},'both')
    config=rtc.folder/'go2rtc.json';data=json.loads(config.read_text())
    assert data['api']['listen']=='127.0.0.1:19844' and data['rtsp']['listen']=='127.0.0.1:18555'
    assert data['webrtc']['candidates']==['203.0.113.7:8555']
    assert data['streams']['camera_a'][0].endswith('/Preview_01_sub')
    assert data['streams']['camera_a'][1]=='ffmpeg:camera_a#audio=opus#raw=-ar:a 48000 -ac:a 2'
    assert camera['path']=='/Preview_01_main'
    assert config.stat().st_mode&0o777==0o600
    assert 'private' not in json.dumps(rtc.status())
    assert len(starts)==1 and 'private' not in str(starts[0][0])
    rtc.sync({'a':camera},'storage');assert rtc.process is None and not rtc.status()['available']
    store.db.close()


def test_realtime_start_failure_leaves_recording_engine_usable(tmp_path,monkeypatch):
    store=Store(tmp_path/'data');binary=tmp_path/'go2rtc';binary.write_text('test')
    monkeypatch.setenv('REOVAULT_GO2RTC',str(binary))
    def fail(*args,**kwargs):raise PermissionError('sensitive third-party text')
    monkeypatch.setattr('realtime.subprocess.Popen',fail)
    rtc=Realtime(store)
    camera={'id':'a','host':'home.example','port':2004,'username':'admin','password':'private','path':'/Preview_01_main','compatible':1}
    rtc.sync({'a':camera},'both')
    assert not rtc.status()['available'] and rtc.status()['error']
    assert 'sensitive' not in str(rtc.status()) and 'private' not in str(rtc.status())
    store.db.close()


def test_realtime_diagnostics_never_exposes_source_credentials(tmp_path,monkeypatch):
    from io import BytesIO
    store=Store(tmp_path/'data')
    store.execute("INSERT INTO cameras(id,name,host,port,username,password,path) VALUES('a','Front','home',554,'u','SECRET','/main')")
    rtc=Realtime(store)
    monkeypatch.setattr(rtc,'status',lambda:{'available':True})
    raw={'camera_a':{'producers':[{'url':'rtsp://u:SECRET@home/main','recv':123,'receivers':[{'codec':{'codec_name':'OPUS'},'packets':42}]}],
        'consumers':[{'url':'SECRET','send':456,'senders':[{'codec':{'codec_name':'OPUS'},'bytes':200}]}]}}
    monkeypatch.setattr('realtime.urllib.request.urlopen',lambda *a,**k:BytesIO(json.dumps(raw).encode()))
    result=rtc.diagnostics()
    assert 'SECRET' not in json.dumps(result)
    assert result['streams']['a']['producers'][0]['tracks']==[{'codec':'OPUS','packets':42}]
    store.db.close()
