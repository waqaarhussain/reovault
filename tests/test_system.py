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
    assert outsider.post('/api/login',json={'username':'admin','password':'bad'}).status_code==401
    outsider.close()
    headers=dict(client.headers)
    del client.headers['X-CSRF-Token']
    assert client.post('/api/recording',json={'enabled':True}).status_code==403
    client.headers.update(headers)
    assert client.post('/api/recording',json={'enabled':True}).status_code==200
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
    store.record(True)
    since=store.get('record_since')
    store.record(True)
    assert store.get('record_since')==since
    store.db.close()
    restarted=Store(tmp_path/'data')
    assert restarted.get('recording') is True
    restarted.db.close()


@pytest.mark.parametrize('codec',['h264','hevc'])
def test_real_ffmpeg_recording_pause_download_and_decode(tmp_path,codec):
    store=Store(tmp_path/'data')
    camera={'id':'camera','name':'Test','compatible':False}
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
    store.record(True)
    store.set('record_since',entries[0][1])
    worker.ingest()
    rows=store.rows('SELECT * FROM segments ORDER BY start')
    assert len(rows)==3
    worker.ingest()
    assert len(store.rows('SELECT * FROM segments'))==3,'Repeated polling must not duplicate recordings'
    for row in rows:
        path=store.archive_path(row)
        assert path.read_bytes()==(worker.folder/row['name']).read_bytes()
    standalone=tmp_path/'download.mp4'
    standalone.write_bytes((store.archive_path(rows[1]).parent/'init.mp4').read_bytes()+store.archive_path(rows[1]).read_bytes())
    subprocess.run(['ffmpeg','-v','error','-i',str(standalone),'-f','null','-'],check=True,timeout=15)
    details=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','stream=codec_name,codec_tag_string','-of','json',str(standalone)]))
    assert details['streams'][0]['codec_name']==codec
    if codec=='hevc':
        assert details['streams'][0]['codec_tag_string']=='hvc1'
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
