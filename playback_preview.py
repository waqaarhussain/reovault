"""Disposable H.264 playback fragments. The source archive is only ever read."""
import os
import io
from functools import lru_cache
import hashlib
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path


@lru_cache(maxsize=128)
def initialization_video(data):
    """Read codec metadata from an MP4 init only. Never decode archived frames."""
    import av
    try:
        with av.open(io.BytesIO(data),format='mp4') as source:
            video=source.streams.video[0].codec_context
            if video.name not in ('h264','hevc'):return {}
            extra=video.extradata or b'';codecs=None
            if video.name=='h264' and len(extra)>=4 and extra[0]==1:
                codecs='avc1.'+extra[1:4].hex().upper()
            elif video.name=='hevc' and len(extra)>=13 and extra[0]==1:
                space=['','A','B','C'][extra[1]>>6];profile=extra[1]&31
                compatibility=int(f'{int.from_bytes(extra[2:6],"big"):032b}'[::-1],2)
                tier='H' if extra[1]&32 else 'L'
                codecs=f'hvc1.{space}{profile}.{compatibility:X}.{tier}{extra[12]}'
                constraints=extra[6:12].rstrip(b'\x00')
                if constraints:codecs+='.'+'.'.join(f'{byte:02X}' for byte in constraints)
            return {'codec':video.name,'width':video.width,'height':video.height,**({'codecs':codecs} if codecs else {})}
    except (av.error.FFmpegError,ValueError,IndexError):return {}


class PlaybackPreviews:
    def __init__(self,store):
        self.store=store
        self.root=store.root/'playback-cache';self.root.mkdir(exist_ok=True,mode=0o700)
        # Bound conversion load across viewers; live video and recording remain independent.
        self.lock=threading.Lock()
        self.stats_lock=threading.Lock();self.latest=None;self.last_completed=None;self.cache_hits=0;self.conversions=0

    def profile(self,row):
        try:return initialization_video(self.store.initialization(row))
        except (OSError,ValueError):return {}

    def diagnostics(self):
        with self.stats_lock:return {'latest':dict(self.latest) if self.latest else None,'last_completed':dict(self.last_completed) if self.last_completed else None,'cache_hits':self.cache_hits,'conversions':self.conversions}

    def cached(self,output):
        try:os.utime(output,None)
        except FileNotFoundError:return False
        with self.stats_lock:self.cache_hits+=1
        return True

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
        # A cached viewer must not queue behind someone else's new 4K conversion.
        if output.is_file() and self.cached(output):return output
        queued=time.monotonic()
        if not self.lock.acquire(timeout=30):raise ValueError('Playback preview is busy. Try again shortly.')
        try:
            self.prune()
            if output.is_file() and self.cached(output):return output
            required=(row.get('payload_size') or row['size'])*2+32*1024*1024+self.store.get('reserve_gb')*1e9
            if shutil.disk_usage(self.root).free<required:raise ValueError('Not enough free space for a browser preview. Download the original instead.')
            source=self.root/(key+'.mp4')
            temp=output.with_suffix('.tmp')
            started=time.monotonic()
            with self.stats_lock:self.latest={'state':'converting','queue_seconds':round(started-queued,3),'footage_seconds':row['duration']}
            try:
                with self.store.lock:self.store.standalone(row,source)
                # Independent MPEG-TS fragments retain a common camera-derived clock.
                # Reset decoded PTS, then offset to the indexed UTC time. MPEG-TS wraps
                # at 33 bits; adjacent fragments wrap together as required by HLS.
                command=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-filter_threads','2','-threads','2','-i',str(source),
                    '-map','0:v:0','-map','0:a:0?','-vf',
                    "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2,setpts=PTS-STARTPTS",
                    '-c:v','libx264','-preset','ultrafast','-tune','zerolatency','-crf','25','-pix_fmt','yuv420p','-threads','2','-bf','0',
                    '-af','asetpts=PTS-STARTPTS,aresample=async=1','-c:a','aac','-b:a','96k',
                    '-output_ts_offset',str(row['start']),'-muxdelay','0','-muxpreload','0','-f','mpegts','-y',str(temp)]
                subprocess.run(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=60)
                temp.replace(output)
                elapsed=max(.001,time.monotonic()-started)
                with self.stats_lock:
                    self.conversions+=1
                    self.latest={'state':'complete','queue_seconds':round(started-queued,3),'conversion_seconds':round(elapsed,3),'footage_seconds':row['duration'],'speed':round(row['duration']/elapsed,2)}
                    self.last_completed=dict(self.latest)
            except (OSError,subprocess.SubprocessError):
                with self.stats_lock:self.latest={'state':'failed'}
                raise ValueError('Browser preview could not be prepared. The original recording is still available.') from None
            finally:source.unlink(missing_ok=True);temp.unlink(missing_ok=True)
            return output
        finally:self.lock.release()
