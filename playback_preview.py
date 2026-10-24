"""Disposable H.264 playback fragments. The source archive is only ever read."""
import os
import hashlib
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path


class PlaybackPreviews:
    def __init__(self,store):
        self.store=store
        self.root=store.root/'playback-cache';self.root.mkdir(exist_ok=True,mode=0o700)
        # Bound conversion load across viewers; live video and recording remain independent.
        self.lock=threading.Lock()

    def prune(self):
        for path in (*self.root.glob('*.mp4'),*self.root.glob('*.tmp')):
            if time.time()-path.stat().st_mtime>120:path.unlink(missing_ok=True)
        files=sorted(self.root.glob('*.ts'),key=lambda p:p.stat().st_mtime)
        total=sum(p.stat().st_size for p in files)
        now=time.time()
        for path in files:
            age=now-path.stat().st_mtime
            if age>1800 or (total>256*1024*1024 and age>60):
                total-=path.stat().st_size;path.unlink(missing_ok=True)

    def fragment(self,row):
        identity=json.dumps({k:row.get(k) for k in ('camera_id','session','name','start','duration','relative_path','byte_offset','payload_size')},sort_keys=True)
        key=str(row['id'])+'_'+hashlib.sha256(identity.encode()).hexdigest()[:20]
        output=self.root/(key+'.ts')
        if not self.lock.acquire(timeout=30):raise ValueError('Playback preview is busy. Try again shortly.')
        try:
            self.prune()
            if output.is_file():os.utime(output,None);return output
            required=(row.get('payload_size') or row['size'])*2+32*1024*1024+self.store.get('reserve_gb')*1e9
            if shutil.disk_usage(self.root).free<required:raise ValueError('Not enough free space for a browser preview. Download the original instead.')
            source=self.root/(key+'.mp4')
            temp=output.with_suffix('.tmp')
            try:
                with self.store.lock:self.store.standalone(row,source)
                # Independent MPEG-TS fragments retain a common camera-derived clock.
                # Reset decoded PTS, then offset to the indexed UTC time. MPEG-TS wraps
                # at 33 bits; adjacent fragments wrap together as required by HLS.
                command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-threads','2','-i',str(source),
                    '-map','0:v:0','-map','0:a:0?','-vf',
                    "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2,setpts=PTS-STARTPTS",
                    '-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-crf','25','-pix_fmt','yuv420p','-threads','2','-bf','0',
                    '-af','asetpts=PTS-STARTPTS,aresample=async=1','-c:a','aac','-b:a','96k',
                    '-output_ts_offset',str(row['start']),'-muxdelay','0','-muxpreload','0','-f','mpegts','-y',str(temp)]
                subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=60)
                temp.replace(output)
            except (OSError,subprocess.SubprocessError):
                raise ValueError('Browser preview could not be prepared. The original recording is still available.') from None
            finally:source.unlink(missing_ok=True);temp.unlink(missing_ok=True)
            return output
        finally:self.lock.release()
