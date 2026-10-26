import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Engine, Worker, password_hash, hls_output, playlist_entries, rtsp_url


@pytest.fixture
def app_module(tmp_path, monkeypatch):
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'username':'admin','password_hash':password_hash('test-password-123'),
                                  'secret':'test-secret','control_token':'private-control'}))
    monkeypatch.setenv('REOVAULT_CONFIG',str(config))
    monkeypatch.setenv('REOVAULT_DATA',str(tmp_path/'data'))
    import app
    app=importlib.reload(app)
    yield app
    app.STORE.db.close()


@pytest.fixture
def client(app_module):
    client=TestClient(app_module.app)
    assert client.post('/api/login',json={'username':'admin','password':'test-password-123'}).status_code==200
    status=client.get('/api/status').json()
    client.headers['X-CSRF-Token']=status['csrf']
    yield client
    client.close()


def add_camera(client):
    r=client.post('/api/cameras',json={'name':'TrackMix','host':'home.eero.online','port':8551,
       'username':'admin','password':'p@ss:/?# words','path':'/Preview_01_main'})
    assert r.status_code==200,r.text
    return r.json()['id']


def test_auth_and_csrf(app_module,client):
    outsider=TestClient(app_module.app)
    assert outsider.get('/api/status').status_code==401
    assert outsider.get('/media/live/x/y/index.m3u8').status_code==401
    assert outsider.get('/api/playback-preview/1.ts').status_code==401
    assert outsider.post('/api/login',json={'username':'admin','password':'bad'}).status_code==401
    outsider.close()
    headers=dict(client.headers)
    del client.headers['X-CSRF-Token']
    assert client.post('/api/recording',json={'enabled':True}).status_code==403
    client.headers.update(headers)
    assert client.post('/api/recording',json={'enabled':True}).status_code==405
    assert client.post('/api/control',json={'action':'start'},headers={'X-Control-Token':'wrong'}).status_code==403
    assert client.post('/api/logout').status_code==200
    assert client.get('/api/status').status_code==401


def test_credentials_and_camera_lifecycle(app_module,client):
    cid=add_camera(client)
    status=client.get('/api/status').json()
    assert 'password' not in status['cameras'][0]
    assert 'p@ss' not in json.dumps(status)
    c=app_module.STORE.camera(cid)
    assert 'p%40ss%3A%2F%3F%23%20words' in rtsp_url(c)
    assert client.put('/api/cameras/'+cid,json={'name':'Driveway','password':''}).status_code==200
    assert app_module.STORE.camera(cid)['password']==c['password']
    assert client.post('/api/cameras',json={'name':'bad','host':'host; echo hello','password':'test'}).status_code==400
    assert client.delete('/api/cameras/'+cid).status_code==200
    assert not client.get('/api/status').json()['cameras']
    assert app_module.STORE.camera(cid)['password']==''
    assert client.get('/media/archive/%2e%2e/%2e%2e/config.json').status_code in (404,400)


def seed_segment(store,cid,session,name,start,duration,size=100):
    directory=store.root/'archive'/cid/session
    directory.mkdir(parents=True,exist_ok=True)
    (directory/'init.mp4').write_bytes(b'init')
    (directory/name).write_bytes(b'x'*size)
    return store.execute('INSERT INTO segments(camera_id,session,name,start,duration,size) VALUES(?,?,?,?,?,?)',
                         (cid,session,name,start,duration,size))


def test_timeline_split_at_group_boundary_gaps_and_playlist(app_module,client):
    cid=add_camera(client)
    store=app_module.STORE
    seed_segment(store,cid,'session1','seg_000.m4s',1798,4,400)
    seed_segment(store,cid,'session1','seg_001.m4s',1802,4,400)
    seed_segment(store,cid,'session2','seg_000.m4s',1810,4,400)
    r=client.get('/api/timeline',params={'camera':cid,'start':0,'end':86400,'bucket':1800})
    assert r.status_code==200
    d=r.json()
    assert d['ranges']==[[1798,1806],[1810,1814]]
    assert [b['recorded_seconds'] for b in d['buckets']]==[2,10]
    assert sum(b['bytes'] for b in d['buckets'])==1200
    for bucket in (300,900,1800,3600):
        assert client.get('/api/timeline',params={'camera':cid,'start':0,'end':86400,'bucket':bucket}).status_code==200
    manifest=client.get('/api/playback/'+cid+'.m3u8',params={'start':1800,'end':3600})
    assert manifest.status_code==200
    assert manifest.text.count('#EXT-X-MAP:')==2
    assert '#EXT-X-DISCONTINUITY' in manifest.text
    assert manifest.text.rstrip().endswith('#EXT-X-ENDLIST')
    preview=client.get('/api/playback/'+cid+'.m3u8',params={'start':1800,'end':3600,'compatible':True})
    assert '/api/playback-preview/' in preview.text and '#EXT-X-MAP' not in preview.text
    assert '#EXT-X-DISCONTINUITY' in preview.text
    assert '#EXT-X-PROGRAM-DATE-TIME:' in preview.text
    assert client.get('/api/playback-preview/999999.ts').status_code==404
    sid=client.get('/api/recordings',params={'camera':cid,'start':0,'end':86400}).json()['segments'][0]['id']
    download=client.get('/api/download/'+str(sid))
    assert download.content==b'init'+b'x'*400
    assert download.headers['content-disposition'].endswith('.mp4"')
    assert client.get('/api/timeline',params={'camera':cid,'start':0,'end':86400,'bucket':123}).status_code==400


def test_oldest_rotation_and_restart_recovery(tmp_path):
    store=Store(tmp_path/'data')
    a=seed_segment(store,'cam','session1','a.m4s',100,4,500)
    b=seed_segment(store,'cam','session1','b.m4s',104,4,500)
    c=seed_segment(store,'cam','session2','a.m4s',108,4,500)
    store.set('quota_gb',800/1e9)
    store.set('reserve_gb',0)
    store.prune()
    rows=store.rows('SELECT * FROM segments')
    assert [r['id'] for r in rows]==[c]
    assert not (store.root/'archive/cam/session1/init.mp4').exists()
    assert (store.root/'archive/cam/session2/init.mp4').exists()
    (store.root/'archive/cam/session2/orphan.m4s').write_bytes(b'orphan')
    (store.root/'archive/cam/session2/a.m4s').unlink()
    store.reconcile()
    assert not store.rows('SELECT * FROM segments')
    assert not list((store.root/'archive').glob('*/*/*.m4s'))
    store.execute("INSERT INTO cameras(id,name,host,port,username,password,path) VALUES('cam','Camera','test',554,'u','p','/test')")
    store.record('cam',True)
    since=store.camera('cam')['record_since']
    store.record('cam',True)
    assert store.camera('cam')['record_since']==since
    store.db.close()
    restarted=Store(tmp_path/'data')
    assert restarted.camera('cam')['record_enabled']==1
    restarted.db.close()


@pytest.mark.parametrize('codec',['h264','hevc'])
def test_real_ffmpeg_recording_pause_download_and_decode(tmp_path,codec):
    store=Store(tmp_path/'data')
    store.execute("INSERT INTO cameras(id,name,host,port,username,password,path) VALUES('camera','Test','test',554,'u','p','/test')")
    camera=store.camera('camera')
    worker=Worker(store,camera)
    encoder=['-c:v','libx264','-preset','ultrafast','-g','40'] if codec=='h264' else ['-c:v','libx265','-preset','ultrafast','-x265-params','pools=1:keyint=40:min-keyint=40']
    source=tmp_path/'source.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','testsrc2=size=640x360:rate=10',
        '-f','lavfi','-i','sine=frequency=500:sample_rate=48000','-t','12',*encoder,'-c:a','aac','-threads','2',str(source)],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=45)
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-i',str(source),*hls_output(worker.folder,codec=codec)],
        check=True,timeout=20)
    entries=playlist_entries(worker.folder/'index.m3u8')
    assert len(entries)==3
    worker.ingest()
    assert not store.rows('SELECT * FROM segments'),'Recording must be off initially'
    worker.seen.clear()
    from media_time import first_video_pts
    import time
    store.record('camera',True)
    store.execute("UPDATE cameras SET record_since=0 WHERE id='camera'")
    worker.ingest()
    assert not store.rows('SELECT * FROM segments'),'An enabled camera must wait for source timestamps'
    assert 'Waiting for camera RTCP' in worker.warning
    first_pts=first_video_pts((worker.folder/'init.mp4').read_bytes(),(worker.folder/entries[0][0]).read_bytes())
    (worker.folder/'timing.json').write_text(json.dumps({'source':'camera-rtcp','pts_to_utc':1792857600+60-first_pts,'updated':time.time()}))
    worker.ingest()
    rows=store.rows('SELECT * FROM segments ORDER BY start')
    assert len(rows)==3
    # A preview may share camera credentials/id, but must never enter the original index.
    preview=Worker(store,{**camera,'compatible':0},preview_only=True,folder=worker.folder/'preview'/'test')
    for item in worker.folder.iterdir():
        if item.is_file():shutil.copyfile(item,preview.folder/item.name)
    preview.ingest()
    assert len(store.rows('SELECT * FROM segments'))==3
    worker.ingest()
    assert len(store.rows('SELECT * FROM segments'))==3,'Repeated polling must not duplicate recordings'
    for row in rows:
        path=store.archive_path(row)
        assert store.fragment_bytes(row)==(worker.folder/row['name']).read_bytes()
    standalone=tmp_path/'download.mp4'
    store.standalone(rows[1],standalone)
    assert len({r['relative_path'] for r in rows})==1
    subprocess.run(['ffmpeg','-v','error','-i',str(store.archive_path(rows[0])),'-f','null','-'],check=True,timeout=15)
    subprocess.run(['ffmpeg','-v','error','-i',str(standalone),'-f','null','-'],check=True,timeout=15)
    details=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','stream=codec_name,codec_tag_string','-of','json',str(standalone)]))
    assert details['streams'][0]['codec_name']==codec
    if codec=='hevc':
        assert details['streams'][0]['codec_tag_string']=='hvc1'
    from exporter import Exports
    exports=Exports(store)
    start=rows[0]['start']+1
    end=rows[-1]['start']+rows[-1]['duration']-.5
    job={'id':'testexport','status':'preparing'}
    exports.export(store.camera('camera'),start,end,job)
    assert job['status']=='complete'
    saved=Path(job['path'])
    assert saved.parent.name=='Saves'
    assert saved.parent.parent.name=='Test'
    subprocess.run(['ffmpeg','-v','error','-i',str(saved),'-f','null','-'],check=True,timeout=20)
    exported=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_name','-of','json',str(saved)]))
    assert exported['streams'][0]['codec_name']==codec
    assert 9<=float(exported['format']['duration'])<=14,exported
    metadata=json.loads(saved.with_suffix('.json').read_text())
    assert metadata['video_reencoded'] is False and metadata['source_timing']==['camera-rtcp']
    # A manually saved clip survives removal of all continuous footage.
    store.set('quota_gb',0);store.set('reserve_gb',0);store.prune()
    assert saved.exists()
    store.db.close()


def test_ubuntu_ffmpeg_timeout_option(monkeypatch):
    import core
    core.rtsp_timeout.cache_clear()
    monkeypatch.setattr(core.subprocess,'check_output',lambda *a,**kw:b'  -stimeout <int> socket I/O timeout\n  -timeout <int> listen timeout\n')
    assert core.rtsp_timeout('ffmpeg',15000000)==['-stimeout','15000000']
    core.rtsp_timeout.cache_clear()
    monkeypatch.setattr(core.subprocess,'check_output',lambda *a,**kw:b'  -timeout <int64> socket I/O timeout\n')
    assert core.rtsp_timeout('ffmpeg',15000000)==['-timeout','15000000']
    core.rtsp_timeout.cache_clear()


def test_manual_names_and_local_helper_do_not_enable_future_cameras(app_module,client,monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app_module,'discover',lambda c:{'name':'Camera supplied name','clock_offset':0.2,'checked':time.time()})
    body={'name':'Front Left','host':'home.eero.online','port':8551,'onvif_port':8851,'username':'admin','password':'test','path':'/Preview_01_main'}
    result=client.post('/api/cameras',json=body)
    assert result.status_code==200,result.text
    first=result.json()['id']
    assert app_module.STORE.camera(first)['name']=='Front Left'
    assert app_module.STORE.camera(first)['device_name'] is None
    assert app_module.STORE.camera(first)['record_enabled']==0
    helper=TestClient(app_module.app,client=('127.0.0.1',12345))
    headers={'X-Control-Token':'private-control'}
    assert helper.post('/api/control',headers=headers,json={'action':'start','camera_id':first}).status_code==200
    monkeypatch.setattr(app_module,'discover',lambda c:{'name':'Second camera','clock_offset':0.1,'checked':time.time()})
    second=client.post('/api/cameras',json={**body,'name':'Rear','port':8552,'onvif_port':8852}).json()['id']
    assert app_module.STORE.camera(first)['record_enabled']==1
    assert app_module.STORE.camera(second)['record_enabled']==0
    assert helper.post('/api/control',headers=headers,json={'action':'start','camera_id':second}).status_code==200
    assert helper.post('/api/control',headers=headers,json={'action':'stop','camera_id':first}).status_code==200
    assert app_module.STORE.camera(first)['record_enabled']==0
    assert app_module.STORE.camera(second)['record_enabled']==1
    helper.close()


def test_manual_camera_name_survives_onvif_refresh(app_module,client,monkeypatch):
    monkeypatch.setattr(app_module,'discover',lambda c:{'name':'IPC-BO','clock_offset':0.1})
    body={'name':'Front Left','host':'home.eero.online','port':2004,'onvif_port':2005,'username':'admin','password':'test','path':'/Preview_01_main'}
    result=client.post('/api/cameras',json=body)
    assert result.status_code==200
    cid=result.json()['id']
    assert app_module.STORE.camera(cid)['name']=='Front Left'
    assert client.put('/api/cameras/'+cid,json={**body,'name':'Driveway'}).status_code==200
    camera=app_module.STORE.camera(cid)
    assert camera['name']=='Driveway' and camera['device_name'] is None
    assert camera['record_enabled']==0
    assert client.post('/api/cameras',json={**body,'name':''}).status_code==400
    assert client.post('/api/camera-name',json=body).status_code==404


def test_mode_controls_preserve_camera_choices_and_block_live(app_module,client):
    cid=add_camera(client);store=app_module.STORE
    helper=TestClient(app_module.app,client=('127.0.0.1',12345))
    headers={'X-Control-Token':'private-control'}
    def control(**body):return helper.post('/api/control',headers=headers,json=body)
    assert control(action='mode',mode='invalid').status_code==400
    assert control(action='mode',mode='storage').status_code==200
    assert not app_module.ENGINE.desired_cameras()
    assert control(action='start',camera_id=cid).status_code==200
    assert cid in app_module.ENGINE.desired_cameras()
    assert client.get('/media/live/anything/index.m3u8').status_code==403
    assert control(action='mode',mode='live').status_code==200
    assert store.camera(cid)['record_enabled']==1
    state=client.get('/api/status').json()
    assert state['mode']=='live' and not state['recording'] and not state['cameras'][0]['record_active']
    assert control(action='start',camera_id=cid).status_code==400
    assert control(action='mode',mode='both').status_code==200
    assert client.get('/api/status').json()['cameras'][0]['record_active']
    second=add_camera(client)
    assert not store.camera(second)['record_enabled']
    helper.close()


def test_hourly_files_reconnect_ranges_and_crash_tail(app_module,client,monkeypatch):
    import media_time
    from datetime import datetime
    cid=add_camera(client);store=app_module.STORE
    store.record(cid,True);store.execute('UPDATE cameras SET record_since=0 WHERE id=?',(cid,))
    monkeypatch.setattr(media_time,'first_video_pts',lambda init,fragment:float(fragment))
    def stamp(value):return datetime.fromisoformat('2026-10-24T'+value+'+01:00').timestamp()
    def feed(worker,times):
        (worker.folder/'init.mp4').write_bytes(b'init')
        lines=['#EXTM3U']
        for i,value in enumerate(times):
            epoch=stamp(value);name=f'seg_{i:09d}.m4s'
            (worker.folder/name).write_bytes(str(epoch).encode())
            lines += ['#EXT-X-PROGRAM-DATE-TIME:'+datetime.fromtimestamp(epoch,timezone.utc).isoformat(),'#EXTINF:4,',name]
        (worker.folder/'index.m3u8').write_text('\n'.join(lines)+'\n')
        (worker.folder/'timing.json').write_text(json.dumps({'source':'camera-rtcp','pts_to_utc':0}))
        worker.ingest()
    from datetime import timezone
    first=Worker(store,store.camera(cid));feed(first,['19:59:56','20:00:00','20:19:56'])
    resumed=Worker(store,store.camera(cid));feed(resumed,['20:30:00','20:30:04','20:59:56','21:00:00'])
    rows=store.rows('SELECT * FROM segments ORDER BY start')
    paths={r['relative_path'] for r in rows}
    assert len(rows)==7 and len(paths)==4
    assert rows[0]['relative_path']!=rows[1]['relative_path']
    assert rows[1]['relative_path']==rows[2]['relative_path']
    assert rows[2]['relative_path']!=rows[3]['relative_path']
    assert rows[3]['relative_path']==rows[4]['relative_path']==rows[5]['relative_path']
    assert rows[5]['relative_path']!=rows[6]['relative_path']
    for row in rows:
        payload=str(row['start']).encode()
        assert store.fragment_bytes(row)==payload
        assert client.get(f'/api/fragments/{row["id"]}.m4s').content==payload
        assert client.get(f'/api/download/{row["id"]}').content==b'init'+payload
    timeline=client.get('/api/timeline',params={'camera':cid,'start':stamp('20:19:56'),'end':stamp('21:00:04'),'bucket':3600}).json()
    assert timeline['ranges'][0]==[stamp('20:19:56'),stamp('20:20:00')]
    assert timeline['ranges'][1][0]==stamp('20:30:00')
    path=store.archive_path(rows[3]);length=path.stat().st_size
    with path.open('ab') as handle:handle.write(b'uncommitted crash tail')
    store.reconcile();assert path.stat().st_size==length
    # Whole-file retention deletes every index row belonging to that hour.
    first_size=store.archive_path(rows[0]).stat().st_size
    second_size=store.archive_path(rows[1]).stat().st_size
    used=store.storage()['used_bytes']
    assert used==sum((store.recordings_root/p).stat().st_size for p in paths)
    store.set('quota_gb',(used-first_size-second_size+1)/1e9);store.set('reserve_gb',0)
    store.prune()
    remaining=store.rows('SELECT * FROM segments')
    assert {r['id'] for r in remaining}=={r['id'] for r in rows[3:]}
    assert list((store.camera_folder(cid)/'Saves').glob('*.mp4'))


def test_supervisor_keeps_retrying_then_resumes_without_record_helper(tmp_path,monkeypatch):
    import core
    store=Store(tmp_path/'data')
    store.execute("INSERT INTO cameras(id,name,host,port,username,password,path) VALUES('cam','Camera','test',554,'u','p','/test')")
    store.record('cam',True);store.set_mode('storage')
    clock=[1000];attempts=[]
    class Process:
        def __init__(self,alive):self.alive=alive
        def poll(self):return None if self.alive else 1
    class FakeWorker:
        def __init__(self,store,camera):
            self.camera=camera;self.mode=store.get('mode');self.folder=store.root/'live'/'cam'/str(len(attempts))
            self.probe=None;self.process=Process(len(attempts)>=2);self.started=clock[0];self.last_segment=0
        def start(self):attempts.append(self)
        def poll_probe(self):pass
        def ingest(self):
            if self.process.alive:self.last_segment=clock[0]
        def stop(self):pass
    monkeypatch.setattr(core,'Worker',FakeWorker)
    monkeypatch.setattr(core.time,'time',lambda:clock[0])
    engine=Engine(store)
    class Event:
        ticks=0
        def is_set(self):return self.ticks>=6
        def wait(self,seconds):self.ticks+=1;clock[0]+=11
    engine.stop_event=Event();engine.run()
    assert len(attempts)==3 and attempts[-1].process.alive
    assert attempts[-1].last_segment>0 and store.camera('cam')['record_enabled']
    assert len({w.folder for w in attempts})==3
    store.db.close()


@pytest.mark.parametrize('offset',['+0000','+0100','-0430','+00:00','Z'])
def test_ffmpeg_playlist_timezone_with_python310_parser(tmp_path,monkeypatch,offset):
    import core
    import re
    from datetime import datetime
    class Python310Datetime(datetime):
        @classmethod
        def fromisoformat(cls,value):
            if re.search(r'[+-]\d{4}$',value):raise ValueError('Python 3.10 rejects compact timezone offsets')
            return super().fromisoformat(value)
    monkeypatch.setattr(core,'datetime',Python310Datetime)
    path=tmp_path/'index.m3u8'
    path.write_text('#EXTM3U\n#EXT-X-PROGRAM-DATE-TIME:2026-10-24T20:00:00.123'+offset+'\n#EXTINF:4,\nseg_000.m4s\n')
    result=core.playlist_entries(path)
    assert result==[('seg_000.m4s',datetime.strptime('2026-10-24T20:00:00.123'+offset,'%Y-%m-%dT%H:%M:%S.%f%z').timestamp(),4)]


def test_error_diagnostics_never_include_url_password_or_exception_message():
    import core
    secret='rtsp://admin:very-private-password@example.invalid/camera'
    try:raise ValueError(secret)
    except ValueError as error:detail=core.safe_error(error)
    value=json.dumps(detail)
    assert detail['type']=='ValueError' and detail['frames']
    assert 'very-private-password' not in value and 'example.invalid' not in value and 'rtsp://' not in value


def test_webrtc_signalling_requires_login_csrf_and_live_mode(app_module,client,monkeypatch):
    cid=add_camera(client);calls=[]
    def answer(camera_id,sdp):calls.append((camera_id,sdp));return 'v=0\r\ns=answer\r\n'
    monkeypatch.setattr(app_module.ENGINE.realtime,'answer',answer)
    outsider=TestClient(app_module.app)
    payload={'type':'offer','sdp':'v=0\r\ns=offer\r\n'}
    assert outsider.post('/api/live/'+cid+'/webrtc',json=payload).status_code==401
    outsider.close()
    token=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/live/'+cid+'/webrtc',json=payload).status_code==403
    client.headers['X-CSRF-Token']=token
    result=client.post('/api/live/'+cid+'/webrtc',json=payload)
    assert result.status_code==200 and result.json()['type']=='answer' and calls==[(cid,payload['sdp'])]
    assert client.post('/api/live/'+cid+'/webrtc',json={'type':'answer','sdp':'bad'}).status_code==400
    app_module.STORE.set_mode('storage')
    assert client.post('/api/live/'+cid+'/webrtc',json=payload).status_code==403
    assert client.post('/api/live/'+cid+'/fallback').status_code==403
    assert len(calls)==1


def test_optional_onvif_clock_never_requests_device_name(monkeypatch):
    import device
    calls=[]
    def unavailable(camera,operation):
        calls.append(operation)
        raise ValueError('Clock unavailable')
    monkeypatch.setattr(device,'soap',unavailable)
    assert device.discover({'onvif_port':2005})['clock_offset'] is None
    assert calls==['GetSystemDateAndTime']


def test_browser_downloads_selected_five_minute_original(app_module,client,tmp_path):
    import hashlib
    from exporter import Exports
    store=app_module.STORE;store.set('reserve_gb',0)
    cid=add_camera(client);worker=Worker(store,store.camera(cid))
    source=tmp_path/'five-minutes.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=size=320x180:rate=2',
        '-t','300','-c:v','libx265','-preset','ultrafast','-x265-params',
        'pools=1:frame-threads=1:keyint=8:min-keyint=8:bframes=0','-threads','2',str(source)],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
    options=hls_output(worker.folder,codec='hevc');options[options.index('-hls_list_size')+1]='0'
    options[options.index('-hls_flags')+1]='temp_file+program_date_time+independent_segments'
    subprocess.run(['ffmpeg','-v','error','-i',str(source),*options],check=True,timeout=30)
    entries=playlist_entries(worker.folder/'index.m3u8');assert len(entries)==75
    from media_time import first_video_pts
    start=1792884000
    pts=first_video_pts((worker.folder/'init.mp4').read_bytes(),(worker.folder/entries[0][0]).read_bytes())
    (worker.folder/'timing.json').write_text(json.dumps({'source':'camera-rtcp','pts_to_utc':start-pts,'updated':time.time()}))
    store.record(cid,True);store.execute('UPDATE cameras SET record_since=0 WHERE id=?',(cid,));worker.ingest()
    rows=store.rows('SELECT * FROM segments WHERE camera_id=? ORDER BY start',(cid,));assert len(rows)==75
    archive=store.archive_path(rows[0]);before=hashlib.sha256(archive.read_bytes()).hexdigest()
    app_module.EXPORTS=Exports(store)
    original=client.get('/api/playback/'+cid+'.m3u8',params={'start':start,'end':start+300})
    assert original.headers['x-reovault-codec']=='hevc' and original.headers['x-reovault-width']=='320'
    assert original.headers['x-reovault-codecs'].startswith('hvc1.')
    assert app_module.PREVIEWS.diagnostics()['conversions']==0
    outsider=TestClient(app_module.app)
    assert outsider.post('/api/exports',json={}).status_code==401
    assert outsider.get('/api/exports/missing/file').status_code==401
    outsider.close()
    assert client.post('/api/exports',json={'camera_id':cid,'start':start,'end':start+300},headers={'X-CSRF-Token':'bad'}).status_code==403
    assert client.post('/api/exports',json={'camera_id':cid,'start':'nan','end':start+300}).status_code==400
    assert client.get('/api/exports/missing').status_code==404

    def download_block():
        response=client.post('/api/exports',json={'camera_id':cid,'start':start,'end':start+300})
        assert response.status_code==200,response.text
        job=response.json();deadline=time.monotonic()+90
        assert 'path' not in job
        while job['status'] not in ('complete','failed') and time.monotonic()<deadline:
            time.sleep(.05);job=client.get('/api/exports/'+job['id']).json()
        assert job['status']=='complete',job
        response=client.get(job['url']);assert response.status_code==200
        assert response.headers['content-disposition'].startswith('attachment;')
        target=tmp_path/(job['id']+'.mp4');target.write_bytes(response.content)
        probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_name,width,height','-of','json',str(target)]))
        assert probe['streams'][0]['codec_name']=='hevc' and probe['streams'][0]['width']==320
        assert hashlib.sha256(archive.read_bytes()).hexdigest()==before
        return job,float(probe['format']['duration'])

    job,length=download_block();assert 299<=length<=301,length
    assert 'omitted' not in job['warning']
    # Missing footage must not turn a block download into an error or fabricate frames.
    store.execute('DELETE FROM segments WHERE id=?',(rows[30]['id'],))
    with pytest.raises(ValueError,match='gap'):
        app_module.EXPORTS.export(store.camera(cid),start,start+300,{'id':'strict-gap'})
    job,length=download_block();assert 295<=length<=297,length
    assert 'Unrecorded portions' in job['warning']


def test_saved_substream_playback_copies_packets_and_keeps_original_downloads(app_module,client,tmp_path,monkeypatch):
    import hashlib
    import av
    from media_time import first_video_pts
    store=app_module.STORE;cid=add_camera(client)
    # Fixture workers never open a real network connection.
    store.execute("UPDATE cameras SET path='/fixture',record_enabled=1,record_since=0 WHERE id=?",(cid,))
    workers=[];start=1793008800.0
    for small,width,height in ((False,1280,720),(True,896,512)):
        worker=Worker(store,store.camera(cid),preview_only=small);worker.codec='h264'
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'testsrc2=size={width}x{height}:rate=8',
            '-f','lavfi','-i','sine=frequency=500:sample_rate=16000','-t','12',
            '-c:v','libx264','-preset','ultrafast','-g','32','-threads','2','-c:a','aac',
            *['-f','hls','-hls_time','4','-hls_list_size','0','-hls_flags','program_date_time','-hls_segment_type','fmp4',
              '-hls_segment_filename',str(worker.folder/'seg_%09d.m4s'),str(worker.folder/'raw.m3u8')]],check=True,timeout=30)
        entries=playlist_entries(worker.folder/'raw.m3u8')
        pts=first_video_pts((worker.folder/'init.mp4').read_bytes(),(worker.folder/entries[0][0]).read_bytes())
        (worker.folder/'timing.json').write_text(json.dumps({'source':'camera-rtcp','pts_to_utc':start-pts}))
        worker.ingest();worker.ingest();workers.append(worker)
    originals=store.rows('SELECT * FROM segments ORDER BY start')
    saved=store.rows('SELECT * FROM browser_segments ORDER BY start')
    assert len(originals)==len(saved)==3
    assert all('/Browser/' in row['relative_path'] for row in saved)
    assert len({row['relative_path'] for row in saved})==1
    before={row['relative_path']:hashlib.sha256(store.archive_path(row).read_bytes()).hexdigest() for row in originals}
    # A direct browser request may not invoke FFmpeg conversion at all.
    def no_conversion(*args):raise AssertionError('Saved preview must bypass conversion')
    monkeypatch.setattr(app_module.PREVIEWS,'fragment',no_conversion)
    for compatible in (False,True):
        response=client.get(f'/api/playback/{cid}.m3u8',params={'start':start,'end':start+12,'browser':True,'compatible':compatible})
        assert response.status_code==200
        assert response.headers['X-Reovault-Source']=='camera-substream'
        assert response.headers['X-Reovault-Width']=='896' and response.headers['X-Reovault-Height']=='512'
        assert '/api/playback-preview/' not in response.text and '?browser=true' in response.text
        assert response.text.count('#EXT-X-MAP:')==1
        lines=response.text.splitlines()
        init_url=next(line.split('"')[1] for line in lines if line.startswith('#EXT-X-MAP:'))
        init=client.get(init_url).content
        for row,url in zip(saved,[line for line in lines if line and not line.startswith('#')]):
            data=client.get(url).content
            assert data==store.fragment_bytes(row)==(workers[1].folder/row['name']).read_bytes()
            standalone=tmp_path/'saved-browser.mp4';standalone.write_bytes(init+data)
            with av.open(str(standalone)) as source:
                assert source.streams.video[0].codec_context.width==896
                assert source.streams.audio[0].codec_context.name=='aac'
                assert sum(1 for _ in source.decode(video=0))>0
    assert app_module.PREVIEWS.diagnostics()['conversions']==0
    original_manifest=client.get(f'/api/playback/{cid}.m3u8',params={'start':start,'end':start+12})
    assert original_manifest.headers['X-Reovault-Source']=='original'
    assert original_manifest.headers['X-Reovault-Width']=='1280'
    assert '?browser=true' not in original_manifest.text
    # Colliding numeric row IDs never substitute preview bytes for original downloads.
    for row in originals:
        assert client.get('/api/download/'+str(row['id'])).content==store.initialization(row)+store.fragment_bytes(row)
        assert hashlib.sha256(store.archive_path(row).read_bytes()).hexdigest()==before[row['relative_path']]
    assert store.storage()['used_bytes']==sum(store.archive_path(row).stat().st_size for row in (originals[0],saved[0]))
    assert client.get('/api/recordings',params={'camera':cid,'start':start,'end':start+12}).json()['segments'][0]['relative_path']==originals[0]['relative_path']
    # Missing interior coverage falls back for the whole selection, preserving its time range.
    store.execute('DELETE FROM browser_segments WHERE id=?',(saved[1]['id'],))
    fallback=client.get(f'/api/playback/{cid}.m3u8',params={'start':start,'end':start+12,'browser':True,'compatible':True})
    assert fallback.headers['X-Reovault-Source']=='converted' and '/api/playback-preview/' in fallback.text
    store.db.close()
    # An existing index can be reopened/migrated without losing either archive.
    store=Store(app_module.STORE.root);app_module.STORE=store
    preview_path=store.archive_path(saved[0]);committed=max(row['byte_offset']+row['payload_size'] for row in (saved[0],saved[2]))
    with preview_path.open('ab') as handle:handle.write(b'crash tail')
    orphan=preview_path.parent/'old_hour_orphan.mp4';orphan.write_bytes(b'orphan')
    store.reconcile();assert preview_path.stat().st_size==committed and not orphan.exists()
    saves=store.camera_folder(cid)/'Saves';saves.mkdir(exist_ok=True);protected=saves/'keep.mp4';protected.write_bytes(b'keep')
    store.set('quota_gb',0);store.set('reserve_gb',0);store.prune()
    assert not store.rows('SELECT * FROM segments') and not store.rows('SELECT * FROM browser_segments')
    assert not preview_path.exists() and protected.read_bytes()==b'keep'


def test_substream_recording_runs_without_live_view_and_stops_with_recording(tmp_path,monkeypatch):
    store=Store(tmp_path/'data')
    store.execute("INSERT INTO cameras(id,name,host,port,username,password,path,compatible) VALUES('c','Front','home',554,'u','p','/Preview_01_main',0)")
    store.record('c',True);store.set_mode('storage')
    starts=[]
    monkeypatch.setattr(Worker,'start',lambda self:starts.append(self.camera['path']))
    worker=Worker(store,store.camera('c'));worker.realtime_ready=True
    assert worker.sync_native_preview() and starts==['/Preview_01_sub']
    child=worker.preview_worker
    assert child.mode=='storage' and child.preview_only
    # Final capture harvest must never respawn another substream.
    child.ingest_final();assert starts==['/Preview_01_sub']
    store.record('c',False)
    assert not worker.sync_native_preview() and worker.preview_worker is None
    store.db.close()
