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
