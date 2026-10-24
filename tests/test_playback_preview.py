import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store,Worker,hls_output,playlist_entries
from media_time import first_video_pts
from playback_preview import PlaybackPreviews


def test_real_4k_hevc_preview_preserves_archive_and_camera_clock(tmp_path):
    store=Store(tmp_path/'data');store.set('reserve_gb',0)
    store.execute("INSERT INTO cameras(id,name,host,port,username,password,path) VALUES('cam','Front','home',554,'u','p','/main')")
    worker=Worker(store,store.camera('cam'))
    source=tmp_path/'4k.mp4'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=3840x2160:rate=4',
        '-f','lavfi','-i','sine=sample_rate=16000','-t','8','-c:v','libx265','-preset','ultrafast',
        '-x265-params','pools=1:frame-threads=1:keyint=16:min-keyint=16','-threads','2','-c:a','aac',str(source)],
        check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=90)
    subprocess.run(['ffmpeg','-v','error','-i',str(source),*hls_output(worker.folder,codec='hevc')],check=True,timeout=30)
    entries=playlist_entries(worker.folder/'index.m3u8')
    start=1792880700.125
    pts=first_video_pts((worker.folder/'init.mp4').read_bytes(),(worker.folder/entries[0][0]).read_bytes())
    (worker.folder/'timing.json').write_text(json.dumps({'source':'camera-rtcp','pts_to_utc':start-pts,'updated':time.time()}))
    store.record('cam',True);store.execute("UPDATE cameras SET record_since=0 WHERE id='cam'")
    worker.ingest();rows=store.rows('SELECT * FROM segments ORDER BY start')
    assert len(rows)==2
    archive=store.archive_path(rows[0]);before=hashlib.sha256(archive.read_bytes()).hexdigest()
    previews=PlaybackPreviews(store);paths=[previews.fragment(row) for row in rows]
    timestamps=[]
    for path,row in zip(paths,rows):
        details=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','stream=codec_name,width,height,start_time','-of','json',str(path)]))
        video=next(s for s in details['streams'] if s['codec_name']=='h264')
        assert video['width']==1280 and video['height']==720
        assert any(s['codec_name']=='aac' for s in details['streams'])
        timestamps.append(float(video['start_time']))
        subprocess.run([os.environ.get('REOVAULT_TEST_DECODER','ffmpeg'),'-v','error','-i',str(path),'-f','null','-'],check=True,timeout=20)
    assert abs(timestamps[1]-timestamps[0]-(rows[1]['start']-rows[0]['start']))<.05
    assert hashlib.sha256(archive.read_bytes()).hexdigest()==before
    cached=paths[0].read_bytes();assert previews.fragment(rows[0]).read_bytes()==cached
    assert not list(previews.root.glob('*.mp4')) and not list(previews.root.glob('*.tmp'))
    original=tmp_path/'original.mp4';store.standalone(rows[0],original)
    details=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','stream=codec_name,width,height','-of','json',str(original)]))
    assert details['streams'][0]['codec_name']=='hevc' and details['streams'][0]['width']==3840
    # Expired disposable cache entries are removed; archives remain intact.
    for path in paths:os.utime(path,(time.time()-1900,)*2)
    previews.prune();assert not list(previews.root.glob('*.ts'))
    assert archive.is_file() and hashlib.sha256(archive.read_bytes()).hexdigest()==before
    store.db.close()
