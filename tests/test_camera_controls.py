import json
import sqlite3
import sys
import time
from datetime import datetime,date
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store
from clip_input import date_input,time_input,range_input,interval,local_time
from capture import packet_clock


def insert(store,cid,name):
    store.execute('INSERT INTO cameras(id,name,host,port,username,password,path) VALUES(?,?,?,?,?,?,?)',(cid,name,'camera.example',554,'admin','test','/Preview_01_main'))


def test_each_new_camera_stays_off_and_enabled_camera_survives_restart(tmp_path):
    store=Store(tmp_path/'data');insert(store,'a','Driveway')
    assert not store.camera('a')['record_enabled']
    store.record('a',True)
    insert(store,'b','Garden')
    assert store.camera('a')['record_enabled']==1
    assert store.camera('b')['record_enabled']==0
    store.db.close();store=Store(tmp_path/'data')
    assert store.camera('a')['record_enabled']==1 and store.camera('b')['record_enabled']==0
    store.record('b',True);store.record('a',False)
    assert not store.camera('a')['record_enabled'] and store.camera('b')['record_enabled']
    store.db.close()


def test_migration_preserves_existing_recording_but_not_future_cameras(tmp_path):
    root=tmp_path/'data';root.mkdir()
    db=sqlite3.connect(root/'index.sqlite')
    db.executescript("""CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    INSERT INTO settings VALUES('recording','true');INSERT INTO settings VALUES('record_since','123');
    CREATE TABLE cameras(id TEXT PRIMARY KEY,name TEXT,host TEXT,port INTEGER,username TEXT,password TEXT,path TEXT,enabled INTEGER DEFAULT 1,compatible INTEGER DEFAULT 0,deleted INTEGER DEFAULT 0);
    INSERT INTO cameras(id,name) VALUES('old','Old camera');""")
    db.close();store=Store(root)
    assert store.camera('old')['record_enabled']==1 and store.camera('old')['record_since']==123
    insert(store,'new','New camera')
    assert store.camera('new')['record_enabled']==0
    store.db.close()


def test_camera_folder_names_are_safe_unique_and_stable(tmp_path):
    store=Store(tmp_path/'data');insert(store,'a','../Front/Driveway');insert(store,'b','../Front/Driveway')
    first=store.camera_folder('a');second=store.camera_folder('b')
    assert first.parent==store.recordings_root and second.parent==store.recordings_root
    assert first!=second and '/' not in first.name
    store.execute("UPDATE cameras SET name='Changed' WHERE id='a'")
    assert store.camera_folder('a')==first
    store.db.close()


def test_human_date_time_range_and_dst():
    zone=ZoneInfo('Europe/London');now=datetime(2026,10,24,20,tzinfo=zone)
    assert date_input('24/10',zone,now)==date(2026,10,24)
    first,last=range_input('0757pm - 08:10pm')
    start,end=interval(date(2026,10,24),first,last,zone)
    assert (end.timestamp()-start.timestamp())/60==13
    assert start.hour==19 and end.hour==20
    start,end=interval(date(2026,10,24),time_input('11:57pm'),time_input('0010'),zone)
    assert end.date()==date(2026,10,25) and (end.timestamp()-start.timestamp())==780
    with pytest.raises(ValueError,match='twice'):local_time(date(2026,10,25),time_input('01:30'),zone)
    early=local_time(date(2026,10,25),time_input('01:30'),zone,lambda *_:1)
    late=local_time(date(2026,10,25),time_input('01:30'),zone,lambda *_:2)
    assert late.timestamp()-early.timestamp()==3600
    with pytest.raises(ValueError,match='does not exist'):local_time(date(2026,3,29),time_input('01:30'),zone)


def test_packet_timestamp_is_camera_reference_not_server_arrival():
    import struct
    class Packet:
        pts=900000;time_base=Fraction(1,90000)
        def has_sidedata(self,key):return key=='prft'
        def get_sidedata(self,key):return struct.pack('=qi',1_800_000_000_000_000,24)
    anchor=packet_clock(Packet())
    assert anchor==1_800_000_000-10
    class MuxerFallback(Packet):
        def get_sidedata(self,key):return struct.pack('=qi',1_800_000_000_000_000,0)
    assert packet_clock(MuxerFallback()) is None


def test_download_helper_guides_camera_date_and_time(monkeypatch,capsys):
    import manage
    calls=[]
    def api(action,**values):
        calls.append((action,values))
        if action=='list':return {'timezone':'Europe/London','cameras':[{'id':'a','name':'Driveway','device_name':'Driveway','record_enabled':True}]}
        if action=='export':return {'id':'job','status':'preparing'}
        return {'id':'job','status':'complete','path':'/Reolink/Driveway/Saves/clip.mp4','warning':'Original camera keyframes retained.'}
    answers=iter(['1','24/10','0757pm - 08:10pm'])
    monkeypatch.setattr('builtins.input',lambda prompt:next(answers))
    monkeypatch.setattr(manage,'api',api)
    monkeypatch.setattr(manage.time,'sleep',lambda n:None)
    manage.download_command()
    export=next(value for action,value in calls if action=='export')
    assert export['camera_id']=='a' and export['end']-export['start']==13*60
    assert '/Reolink/Driveway/Saves/clip.mp4' in capsys.readouterr().out



def test_mux_failure_is_classified_without_retaining_ffmpeg_log_urls():
    import av
    from capture import mux_packet,CaptureFailure
    original=av.logging.get_level();av.logging.set_level(av.logging.ERROR)
    class InvalidOutput:
        def mux(self,packet):
            av.logging.log(av.logging.ERROR,'mp4','Application provided invalid, non monotonically increasing dts to muxer; rtsp://admin:private@example.invalid')
            raise ValueError('rtsp://admin:private@example.invalid')
    try:
        with pytest.raises(CaptureFailure) as failure:
            mux_packet(InvalidOutput(),object(),{'stage':'mux','stream_type':'audio','dts':5,'previous_dts':10})
        detail=failure.value.detail
        assert detail['mux_reason']=='non-monotonic-timestamps' and detail['stream_type']=='audio'
        assert 'private' not in json.dumps(detail) and 'example.invalid' not in json.dumps(detail)
    finally:av.logging.set_level(original)


def test_real_aac_timestamp_regression_produces_specific_mux_diagnostic(tmp_path):
    import av
    import subprocess
    from capture import mux_packet,CaptureFailure
    source=tmp_path/'audio.m4a'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=sample_rate=48000','-t','1','-c:a','aac',str(source)],check=True,timeout=15)
    original=av.logging.get_level();av.logging.set_level(av.logging.ERROR)
    try:
        with av.open(str(source)) as incoming:
            output=av.open(str(tmp_path/'out.mp4'),'w')
            stream=output.add_stream_from_template(incoming.streams.audio[0])
            try:
                with pytest.raises(CaptureFailure) as failure:
                    for i,packet in enumerate(incoming.demux(incoming.streams.audio[0])):
                        if packet.dts is None:continue
                        if i==15:packet.dts=-2048;packet.pts=-2048
                        packet.stream=stream
                        mux_packet(output,packet,{'stage':'mux','stream_type':'audio'})
                assert failure.value.detail['type']=='ArgumentError'
                assert failure.value.detail['mux_reason']=='non-monotonic-timestamps'
            finally:output.close()
    finally:av.logging.set_level(original)


@pytest.mark.parametrize('backstep',[0,8])
def test_repaired_aac_packets_mux_and_decode_without_reencoding(tmp_path,backstep):
    import av
    import subprocess
    from capture import AudioTimestampGuard
    source=tmp_path/'source.m4a';target=tmp_path/'out.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=sample_rate=16000','-t','2','-c:a','aac',str(source)],check=True,timeout=15)
    original_packets=[];previous=None;guard=AudioTimestampGuard()
    with av.open(str(source)) as incoming:
        output=av.open(str(target),'w');stream=output.add_stream_from_template(incoming.streams.audio[0])
        try:
            for i,packet in enumerate(incoming.demux(incoming.streams.audio[0])):
                if packet.dts is None:continue
                original_packets.append(bytes(packet))
                raw=packet.dts
                if i==15:packet.dts=previous-backstep;packet.pts=packet.dts
                guard.prepare(packet);previous=raw
                packet.stream=stream;output.mux(packet)
        finally:output.close()
    assert guard.repairs==1 and guard.max_adjustment==(backstep+1)/16000
    with av.open(str(target)) as output:
        packets=[bytes(p) for p in output.demux(audio=0) if p.size]
    assert packets==original_packets,'Encoded AAC payloads must remain unchanged'
    subprocess.run(['ffmpeg','-v','error','-i',str(target),'-f','null','-'],check=True,timeout=15)


def test_audio_guard_leaves_video_camera_timestamps_untouched_and_rejects_large_jump():
    from types import SimpleNamespace
    from capture import AudioTimestampGuard,CaptureFailure
    guard=AudioTimestampGuard()
    codec=SimpleNamespace(name='hevc',sample_rate=0)
    video=SimpleNamespace(stream=SimpleNamespace(type='video',index=0,codec_context=codec),dts=100,pts=200,time_base=Fraction(1,90000),duration=3600)
    before=vars(video).copy();guard.prepare(video)
    assert vars(video)==before and guard.repairs==0
    audio=SimpleNamespace(stream=SimpleNamespace(type='audio',index=1,codec_context=SimpleNamespace(name='aac',sample_rate=16000)),dts=16000,pts=16000,time_base=Fraction(1,16000),duration=1024)
    guard.prepare(audio);audio.dts=0;audio.pts=0
    with pytest.raises(CaptureFailure) as error:guard.prepare(audio)
    assert error.value.detail['mux_reason']=='audio-clock-jump'


@pytest.mark.parametrize('codec',['h264','hevc'])
def test_capture_survives_duplicate_aac_with_original_video(tmp_path,monkeypatch,codec):
    import av
    import subprocess
    import capture as module
    source=tmp_path/'source.mp4';folder=tmp_path/'capture'
    video=['-c:v','libx264','-preset','ultrafast','-g','20'] if codec=='h264' else ['-c:v','libx265','-preset','ultrafast','-x265-params','pools=1:keyint=20']
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=640x360:rate=10','-f','lavfi','-i','sine=sample_rate=16000','-t','5',*video,'-c:a','aac','-threads','2',str(source)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
    original_open=av.open;guards=[]
    class Guard(module.AudioTimestampGuard):
        def __init__(self):super().__init__();guards.append(self)
    class JitterInput:
        def __init__(self,container):self.container=container;self.streams=container.streams
        def close(self):self.container.close()
        def demux(self,selected):
            previous=None;count=0
            for packet in self.container.demux(selected):
                if packet.stream.type=='audio' and packet.dts is not None:
                    count+=1;raw=packet.dts
                    if count in (15,45):packet.dts=previous;packet.pts=previous
                    previous=raw
                yield packet
    def open_input(url,*args,**kwargs):
        result=original_open(url,*args,**kwargs)
        return JitterInput(result) if str(url)==str(source) else result
    monkeypatch.setattr(av,'open',open_input);monkeypatch.setattr(module,'AudioTimestampGuard',Guard)
    module.capture(str(source),folder)
    assert guards[0].repairs==2
    standalone=tmp_path/'joined.mp4'
    standalone.write_bytes((folder/'init.mp4').read_bytes()+b''.join(p.read_bytes() for p in sorted(folder.glob('seg_*.m4s'))))
    subprocess.run(['ffmpeg','-v','error','-i',str(standalone),'-f','null','-'],check=True,timeout=15)
    with original_open(str(standalone)) as result:
        assert result.streams.video[0].codec_context.name==codec
        assert result.streams.video[0].codec_context.width==640
        assert result.streams.audio[0].codec_context.name=='aac'


def test_native_preview_path_preserves_credentials_channel_and_main_config(tmp_path):
    from core import native_preview_camera,Worker
    store=Store(tmp_path/'data');insert(store,'a','Driveway')
    camera=store.camera('a');camera['path']='/Preview_02_main';camera['compatible']=1
    sub=native_preview_camera(camera)
    assert sub['path']=='/Preview_02_sub' and sub['compatible']==0
    assert sub['username']==camera['username'] and sub['password']==camera['password'] and sub['port']==camera['port']
    assert camera['path']=='/Preview_02_main' and camera['compatible']==1
    assert native_preview_camera({**camera,'path':'/custom-stream'}) is None
    worker=Worker(store,camera,preview_only=True,folder=tmp_path/'data/live/a/preview')
    assert worker.preview_only and worker.folder.name=='preview'
    store.db.close()


@pytest.mark.parametrize('confirm,stop_fails',[('FORMAT',False),('no',False),('FORMAT',True)])
def test_format_helper_quiesces_service_clears_footage_and_preserves_config(tmp_path,monkeypatch,capsys,confirm,stop_fails):
    import manage
    import subprocess
    root=tmp_path/'data';store=Store(root)
    for cid in ('a','b','disabled','deleted'):insert(store,cid,cid)
    store.execute("UPDATE cameras SET enabled=0 WHERE id='disabled'")
    store.execute("UPDATE cameras SET deleted=1 WHERE id='deleted'")
    store.set_mode('live');store.set('quota_gb',123)
    folder=store.camera_folder('a')
    original=folder/'2026-10-26_hour_test.mp4';original.write_bytes(b'4k')
    preview=folder/'Browser';preview.mkdir();(preview/'hour.mp4').write_bytes(b'small')
    saves=folder/'Saves';saves.mkdir();(saves/'clip.mp4').write_bytes(b'clip');(saves/'clip.json').write_text('{}')
    notes=folder/'notes.txt';notes.write_text('unrelated')
    for name in ('archive','live','playback-cache'):
        directory=root/name;directory.mkdir(exist_ok=True);(directory/'fixture').mkdir();(directory/'fixture'/'file').write_bytes(b'old')
    store.execute("INSERT INTO segments(camera_id,session,name,start,duration,size) VALUES('a','s','x',1,4,3)")
    store.execute("INSERT INTO browser_segments(camera_id,session,name,start,duration,size) VALUES('a','s','x',1,4,5)")
    credentials=store.camera('a')['password'];store.db.close()
    config=tmp_path/'config.json';config.write_text('private-login')
    monkeypatch.setattr(manage,'DATA',root)
    monkeypatch.setattr('builtins.input',lambda prompt:confirm)
    calls=[]
    def run(args,check):
        calls.append(args)
        if args[1]=='stop':
            assert original.exists() and (saves/'clip.mp4').exists()
            if stop_fails:raise subprocess.CalledProcessError(1,args)
        else:
            assert not original.exists() and not preview.exists() and not saves.exists()
    monkeypatch.setattr(subprocess,'run',run)
    if stop_fails:
        with pytest.raises(subprocess.CalledProcessError):manage.format_command()
    else:manage.format_command()
    after=Store(root)
    if confirm!='FORMAT' or stop_fails:
        assert original.read_bytes()==b'4k' and (saves/'clip.mp4').read_bytes()==b'clip'
        assert after.rows('SELECT * FROM segments') and after.rows('SELECT * FROM browser_segments')
        assert not after.camera('a')['record_enabled'] and after.get('mode')=='live'
        assert len(calls)==(1 if stop_fails else 0)
    else:
        assert calls==[['systemctl','stop','reovault'],['systemctl','start','reovault']]
        assert not after.rows('SELECT * FROM segments') and not after.rows('SELECT * FROM browser_segments')
        assert after.camera('a')['record_enabled'] and after.camera('b')['record_enabled']
        assert not after.camera('disabled')['record_enabled'] and not after.camera('deleted')['record_enabled']
        assert after.camera('a')['record_since']>0 and after.get('mode')=='both'
        for name in ('archive','live','playback-cache'):assert not list((root/name).iterdir())
    assert after.camera('a')['password']==credentials and after.get('quota_gb')==123
    assert config.read_text()=='private-login' and notes.read_text()=='unrelated'
    after.db.close()
