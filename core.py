"""ReoVault recording engine. All timestamps in the index are UTC epoch seconds."""
import hashlib
import json
import logging
import traceback
import os
import re
import sys
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import quote


def safe_error(error):
    """Structural diagnostics only: no exception messages, source text, URLs or locals."""
    frames=[Path(f.filename).name+':'+str(f.lineno)+':'+f.name for f in traceback.extract_tb(error.__traceback__)]
    result={'type':type(error).__name__,'frames':frames[-8:]}
    number=getattr(error,'errno',None)
    if isinstance(number,int):result['errno']=number
    return result


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, indent=2))
    os.chmod(tmp, 0o640)
    tmp.replace(path)


def password_hash(password, salt=None):
    salt = salt or os.urandom(16).hex()
    result = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ':' + result.hex()


class Store:
    def __init__(self, root):
        self.root = Path(root)
        for name in ('archive', 'live'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'index.sqlite', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cameras(
              id TEXT PRIMARY KEY,name TEXT NOT NULL,host TEXT NOT NULL,port INTEGER NOT NULL,
              username TEXT NOT NULL,password TEXT NOT NULL,path TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,compatible INTEGER NOT NULL DEFAULT 0,
              deleted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS segments(
              id INTEGER PRIMARY KEY,camera_id TEXT NOT NULL,session TEXT NOT NULL,
              name TEXT NOT NULL,start REAL NOT NULL,duration REAL NOT NULL,size INTEGER NOT NULL,
              UNIQUE(camera_id,session,name));
            CREATE INDEX IF NOT EXISTS segment_time ON segments(start);
            CREATE INDEX IF NOT EXISTS camera_time ON segments(camera_id,start);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,expires REAL NOT NULL);
        ''')
        self.db.commit()
        columns={r[1] for r in self.db.execute('PRAGMA table_info(cameras)')}
        for name,definition in {'record_enabled':'INTEGER NOT NULL DEFAULT 0','record_since':'REAL NOT NULL DEFAULT 0',
                'folder':'TEXT','onvif_port':'INTEGER NOT NULL DEFAULT 0','device_name':'TEXT','clock_offset':'REAL'}.items():
            if name not in columns:self.db.execute(f'ALTER TABLE cameras ADD COLUMN {name} {definition}')
        columns={r[1] for r in self.db.execute('PRAGMA table_info(segments)')}
        for name,definition in {'relative_path':'TEXT','init_size':'INTEGER NOT NULL DEFAULT 0','timing_source':"TEXT NOT NULL DEFAULT 'legacy-server'",'byte_offset':'INTEGER NOT NULL DEFAULT 0','payload_size':'INTEGER NOT NULL DEFAULT 0'}.items():
            if name not in columns:self.db.execute(f'ALTER TABLE segments ADD COLUMN {name} {definition}')
        self.db.commit()
        self.setting_default('recording',False)
        self.setting_default('record_since',0)
        self.setting_default('quota_gb',100)
        self.setting_default('reserve_gb',2)
        self.setting_default('timezone','Europe/London')
        self.setting_default('mode','both')
        self.setting_default('recordings_root',os.environ.get('REOVAULT_RECORDINGS',str(self.root/'Reolink')))
        self.recordings_root=Path(os.environ.get('REOVAULT_RECORDINGS',self.get('recordings_root')))
        self.recordings_root.mkdir(parents=True,exist_ok=True)
        # Preserve existing enabled cameras once; future INSERTs always retain DEFAULT 0.
        if not self.rows("SELECT key FROM settings WHERE key='per_camera_recording_v1'"):
            if self.get('recording'):
                self.execute('UPDATE cameras SET record_enabled=1,record_since=? WHERE deleted=0',(self.get('record_since'),))
            self.set('per_camera_recording_v1',True)

    def execute(self, sql, args=()):
        with self.lock:
            cursor = self.db.execute(sql, args)
            self.db.commit()
            return cursor.lastrowid

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def setting_default(self, key, value):
        self.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (key, json.dumps(value)))

    def get(self, key):
        return json.loads(self.rows('SELECT value FROM settings WHERE key=?', (key,))[0]['value'])

    def set(self, key, value):
        self.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))

    def cameras(self, include_deleted=False):
        return self.rows('SELECT * FROM cameras' + ('' if include_deleted else ' WHERE deleted=0') + ' ORDER BY rowid')

    def camera(self, camera_id):
        rows = self.rows('SELECT * FROM cameras WHERE id=?', (camera_id,))
        return rows[0] if rows else None

    def record(self, camera_id, enabled):
        with self.lock:
            camera=self.camera(camera_id)
            if not camera or camera['deleted']:raise ValueError('Camera not found')
            since=time.time() if enabled and not camera['record_enabled'] else camera['record_since']
            self.camera_folder(camera_id)
            self.execute('UPDATE cameras SET record_enabled=?,record_since=? WHERE id=?',(int(enabled),since,camera_id))

    def set_mode(self, mode):
        if mode not in ('storage','live','both'):raise ValueError('Choose storage, live or both.')
        self.set('mode',mode)

    def fragment_bytes(self,row):
        with self.archive_path(row).open('rb') as handle:
            handle.seek(row.get('byte_offset') or row['init_size'])
            return handle.read(row['payload_size']) if row.get('payload_size') else handle.read()

    def standalone(self,row,target):
        # A bounded fragment copy also pins inputs against whole-hour retention.
        with Path(target).open('wb') as handle:
            handle.write(self.initialization(row))
            handle.write(self.fragment_bytes(row))

    def camera_folder(self,camera_id):
        with self.lock:
            camera=self.camera(camera_id)
            if not camera:raise ValueError('Camera not found')
            name=camera['folder']
            if not name:
                name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',camera['device_name'] or camera['name']).strip(' .')[:80] or 'Camera'
                used={c['folder'].casefold() for c in self.cameras(True) if c['folder']}
                if name.casefold() in used:name+=' '+camera_id[:8]
                self.execute('UPDATE cameras SET folder=? WHERE id=?',(name,camera_id))
            folder=self.recordings_root/name
            folder.mkdir(parents=True,exist_ok=True)
            return folder

    def archive_path(self,row):
        if row.get('relative_path'):
            return self.recordings_root/row['relative_path']
        return self.root/'archive'/row['camera_id']/row['session']/row['name']

    def initialization(self,row):
        path=self.archive_path(row)
        if row.get('init_size'):
            with path.open('rb') as handle:return handle.read(row['init_size'])
        return (path.parent/'init.mp4').read_bytes()

    def storage(self):
        total = self.rows('SELECT COALESCE(SUM(size),0) AS n FROM segments')[0]['n']
        # Include init segments, which are required to decode each archive session.
        total += sum(p.stat().st_size for p in (self.root / 'archive').glob('*/*/init.mp4') if p.exists())
        disk = shutil.disk_usage(self.recordings_root)
        return {'used_bytes': total, 'quota_bytes': self.get('quota_gb') * 10**9,
                'disk_free_bytes': disk.free, 'reserve_bytes': self.get('reserve_gb') * 10**9, 'recordings_root':str(self.recordings_root),
                'saved_bytes':sum(p.stat().st_size for p in self.recordings_root.glob('*/Saves/*.mp4') if p.exists())}

    def prune(self):
        with self.lock:return self._prune_locked()

    def _prune_locked(self):
        """Delete indexed, completed segments only. Never touch the live ring or active files."""
        info = self.storage()
        used, free = info['used_bytes'], info['disk_free_bytes']
        while used > info['quota_bytes'] or free < info['reserve_bytes']:
            oldest = self.rows('SELECT * FROM segments ORDER BY start,id LIMIT 1')
            if not oldest:
                break
            row = oldest[0]
            path = self.archive_path(row)
            size = path.stat().st_size if path.exists() and path.stat().st_nlink==1 else 0
            path.unlink(missing_ok=True)
            if row.get('payload_size'):
                # Rotate entire hourly files, never punch holes in an active MP4.
                removed=self.rows('SELECT COALESCE(SUM(size),0) AS n FROM segments WHERE relative_path=?',(row['relative_path'],))[0]['n']
                self.execute('DELETE FROM segments WHERE relative_path=?',(row['relative_path'],))
            else:
                removed=row['size']
                self.execute('DELETE FROM segments WHERE id=?', (row['id'],))
            used -= removed
            free += size
            if not self.rows('SELECT id FROM segments WHERE camera_id=? AND session=? LIMIT 1',
                             (row['camera_id'], row['session'])):
                init = path.parent / 'init.mp4' if not row.get('relative_path') else path.parent / '.no-init-file'
                if init.exists():
                    size = init.stat().st_size
                    init.unlink()
                    used -= size
                    free += size
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
        return self.storage()

    def reconcile(self):
        """Recover cleanly from a power cut between atomic file copy and index commit."""
        known = {self.archive_path(r) for r in self.rows('SELECT * FROM segments')}
        for row in self.rows('SELECT * FROM segments'):
            if not self.archive_path(row).exists():
                self.execute('DELETE FROM segments WHERE id=?', (row['id'],))
        # Ignore uncommitted append tails after a crash; keep every indexed fragment.
        for item in self.rows('SELECT relative_path,MAX(byte_offset+payload_size) AS end FROM segments WHERE payload_size>0 GROUP BY relative_path'):
            path=self.recordings_root/item['relative_path']
            if path.exists():
                with path.open('r+b') as handle:handle.truncate(item['end'])
        for p in self.recordings_root.glob('*/*.mp4'):
            if '_hour_' in p.name and p not in known:p.unlink(missing_ok=True)
            if re.fullmatch(r'\d{4}-\d{2}-\d{2}_.+_seg_\d+\.mp4',p.name) and p not in known:
                p.unlink(missing_ok=True)
        for p in (self.root / 'archive').glob('*/*/*'):
            if p.suffix == '.tmp' or (p.suffix == '.m4s' and p not in known):
                p.unlink(missing_ok=True)
        for init in (self.root / 'archive').glob('*/*/init.mp4'):
            if not list(init.parent.glob('*.m4s')):
                init.unlink(missing_ok=True)


def rtsp_url(camera):
    auth = quote(camera['username'], safe='') + ':' + quote(camera['password'], safe='')
    return f"rtsp://{auth}@{camera['host']}:{camera['port']}{camera['path']}"


@lru_cache(maxsize=4)
def rtsp_timeout(binary, microseconds):
    # Ubuntu 22.04 ships FFmpeg 4.4, whose socket option is named stimeout.
    # Newer releases renamed it to timeout; avoid the old listen-timeout option.
    help_text = subprocess.check_output([binary, '-hide_banner', '-h', 'demuxer=rtsp'],
                                        stderr=subprocess.STDOUT, timeout=5).decode(errors='replace')
    option = '-stimeout' if '-stimeout ' in help_text else '-timeout'
    return [option, str(microseconds)]


def hls_output(folder, compatible=False, codec=None, manifest="index.m3u8"):
    """FFmpeg HLS with atomic, finalized fMP4 segments. Video is copied unless opted in."""
    video = ['-c:v', 'copy'] + (['-tag:v', 'hvc1'] if codec == 'hevc' else [])
    if compatible:
        video = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
                 '-vf', "scale='min(1920,iw)':-2", '-force_key_frames', 'expr:gte(t,n_forced*2)', '-threads', '2']
    return ['-map', '0:v:0', '-map', '0:a:0?', *video, '-c:a', 'aac', '-b:a', '96k',
            '-f', 'hls', '-hls_time', '4', '-hls_list_size', '10', '-hls_delete_threshold', '6',
            '-hls_segment_type', 'fmp4', '-hls_fmp4_init_filename', 'init.mp4',
            '-hls_flags', 'delete_segments+temp_file+program_date_time+independent_segments',
            '-hls_segment_filename', str(folder / 'seg_%09d.m4s'), str(folder / manifest)]


def playlist_entries(path):
    """Return only segments advertised by the atomically published manifest."""
    if not path.exists():
        return []
    entries, start, duration = [], None, None
    for line in path.read_text().splitlines():
        if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
            # FFmpeg emits +0000; Python 3.10 requires +00:00 here.
            stamp=line.split(':',1)[1].replace('Z','+00:00')
            stamp=re.sub(r'([+-]\d{2})(\d{2})$',r'\1:\2',stamp)
            start = datetime.fromisoformat(stamp).timestamp()
        elif line.startswith('#EXTINF:'):
            duration = float(line.split(':', 1)[1].split(',')[0])
        elif line and not line.startswith('#') and start is not None and duration is not None:
            if Path(line).name != line:
                raise ValueError('Unexpected segment path')
            entries.append((line, start, duration))
            start += duration
            duration = None
    return entries


class Worker:
    def __init__(self, store, camera):
        self.store, self.camera = store, camera
        self.session = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid.uuid4().hex[:8]
        self.folder = store.root / 'live' / camera['id'] / self.session
        self.folder.mkdir(parents=True, exist_ok=True)
        self.process = None
        self.probe = None
        self.codec = None
        self.resolution = None
        self.transcoder = None
        self.started = time.time()
        self.last_segment = 0
        self.seen = set()
        self.warning = None
        self.timing_source = "waiting-camera-rtcp"
        self.latest_capture = None
        self.audio_warning = None
        self.mode = store.get('mode')
        self.archive_key = None
        self.archive_output = None

    def start(self):
        # Probe closes before streaming starts; do not consume a second permanent camera slot.
        self.probe = subprocess.Popen(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp',
            *rtsp_timeout('ffprobe', 10000000), '-select_streams', 'v:0', '-show_entries',
            'stream=codec_name,width,height', '-of', 'json', rtsp_url(self.camera)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.started = time.time()

    def poll_probe(self):
        if self.probe is None:
            return
        if self.probe.poll() is None:
            if time.time()-self.started > 20:
                self.probe.kill()
                self.probe.wait(timeout=3)
            else:
                return
        output = self.probe.communicate()[0]
        try:
            stream = json.loads(output)['streams'][0]
            self.codec = stream['codec_name']
            self.resolution = str(stream['width'])+' × '+str(stream['height'])
        except (ValueError, KeyError, IndexError):
            self.warning = 'Cannot read the camera stream. Check its hostname, port and credentials.'
            self.probe = None
            return
        self.probe = None
        # Do not persist FFmpeg stderr: it can contain credential-bearing input URLs.
        self.process = subprocess.Popen([sys.executable,str(Path(__file__).with_name('capture.py'))],
            stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,text=True)
        self.process.stdin.write(json.dumps({'url':rtsp_url(self.camera),'folder':str(self.folder)}))
        self.process.stdin.close()
        self.started = time.time()

    def ingest(self):
        from media_time import first_video_pts
        raw=self.folder/'raw.m3u8'
        # Test/legacy fixtures can still expose an index manifest directly.
        source=raw if raw.exists() else self.folder/'index.m3u8'
        entries=playlist_entries(source)
        timing=None
        try:timing=json.loads((self.folder/'timing.json').read_text())
        except (OSError,ValueError):pass
        self.audio_warning=(timing or {}).get('audio_warning')
        calibrated=[]
        init=(self.folder/'init.mp4').read_bytes() if entries else b''
        for name,start,duration in entries:
            path=self.folder/name
            if not path.exists():continue
            self.last_segment=max(self.last_segment,path.stat().st_mtime)
            if timing and timing.get('source')=='camera-rtcp':
                start=timing['pts_to_utc']+first_video_pts(init,path.read_bytes())
                self.timing_source='camera-rtcp'
                self.latest_capture=start+duration
            calibrated.append((name,start,duration))
        if raw.exists():
            # Publish a separate live manifest, so a raw server-time manifest never overwrites it.
            starts={name:start for name,start,_ in calibrated}
            lines=raw.read_text().splitlines();published=[];pending=None
            for line in lines:
                if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):continue
                if line and not line.startswith('#') and line in starts and self.timing_source=='camera-rtcp':
                    published.append('#EXT-X-PROGRAM-DATE-TIME:'+datetime.fromtimestamp(starts[line],timezone.utc).isoformat())
                published.append(line)
            temp=self.folder/'index.tmp';temp.write_text('\n'.join(published)+'\n');temp.replace(self.folder/'index.m3u8')
        for name,start,duration in calibrated:
            path=self.folder/name
            if name in self.seen:continue
            camera=self.store.camera(self.camera['id'])
            if self.mode=='live' or not camera or not camera['record_enabled'] or path.stat().st_mtime<=camera['record_since']:
                self.archive_key=None
                self.seen.add(name);continue
            if self.timing_source!='camera-rtcp':
                self.warning='Waiting for camera RTCP capture timestamps. Live view works; recording waits for verified camera time.'
                continue
            day=datetime.fromtimestamp(start,ZoneInfo(self.store.get('timezone')))
            key=day.strftime('%Y-%m-%d_%H%z')
            # One file per clock hour and connection session. Re-enabling also opens a new file.
            with self.store.lock:
                if self.archive_key!=key or not self.archive_output or not self.archive_output.exists():
                    target=self.store.camera_folder(camera['id'])
                    filename=day.strftime('%Y-%m-%d_%H-%M-%S%z')+'_hour_'+self.session+'_'+uuid.uuid4().hex[:6]+'.mp4'
                    self.archive_output=target/filename
                    self.archive_key=key
                output=self.archive_output
                if self.store.rows('SELECT id FROM segments WHERE camera_id=? AND session=? AND name=?',(camera['id'],self.session,name)):
                    self.seen.add(name);continue
                previous_size=output.stat().st_size if output.exists() else 0
                try:
                    fragment=path.read_bytes()
                    with output.open('ab') as handle:
                        if handle.tell()==0:handle.write(init)
                        offset=handle.tell()
                        handle.write(fragment)
                        handle.flush();os.fsync(handle.fileno())
                    relative=output.relative_to(self.store.recordings_root).as_posix()
                    self.store.execute('INSERT OR IGNORE INTO segments(camera_id,session,name,start,duration,size,relative_path,init_size,timing_source,byte_offset,payload_size) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (camera['id'],self.session,name,start,duration,len(fragment)+(len(init) if offset==len(init) else 0),relative,len(init),'camera-rtcp',offset,len(fragment)))
                except Exception:
                    # Roll back partial appends if disk writes or the index commit fail.
                    if output.exists():
                        with output.open('r+b') as handle:handle.truncate(previous_size)
                    raise
            self.seen.add(name)
            self.warning=None
        entries=calibrated
        # Keep memory bounded during long-running camera sessions.
        self.seen.intersection_update({e[0] for e in entries})
        if self.mode!='storage' and self.camera['compatible'] and entries and self.transcoder is None:
            target = self.folder / 'compatible'
            target.mkdir(exist_ok=True)
            self.transcoder = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-copyts','-avoid_negative_ts','disabled','-live_start_index', '-3', '-allowed_extensions', 'ALL', '-i', str(self.folder / 'index.m3u8'),
                *hls_output(target, compatible=True,manifest='raw.m3u8')], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        compatible_folder=self.folder/'compatible'
        compatible_raw=compatible_folder/'raw.m3u8'
        if compatible_raw.exists():
            content=compatible_raw.read_text().splitlines();published=[]
            mapping={}
            if timing and timing.get('source')=='camera-rtcp':
                compatible_init=(compatible_folder/'init.mp4').read_bytes()
                for filename,_,_ in playlist_entries(compatible_raw):
                    fragment=compatible_folder/filename
                    if fragment.exists():mapping[filename]=timing['pts_to_utc']+first_video_pts(compatible_init,fragment.read_bytes())
            for line in content:
                if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):continue
                if line in mapping:published.append('#EXT-X-PROGRAM-DATE-TIME:'+datetime.fromtimestamp(mapping[line],timezone.utc).isoformat())
                published.append(line)
            temp=compatible_folder/'index.tmp';temp.write_text('\n'.join(published)+'\n');temp.replace(compatible_folder/'index.m3u8')
        if self.transcoder is not None and self.transcoder.poll() is not None:
            self.transcoder = None
            self.warning = 'Compatible live conversion restarted. Original recording is unaffected.'

    def stop(self):
        for proc in (self.probe, self.transcoder, self.process):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        # Harvest FFmpeg's final closed segment after graceful shutdown.
        try:
            self.ingest_final()
        except (OSError, ValueError):
            pass

    def ingest_final(self):
        compatible = self.camera['compatible']
        self.camera['compatible'] = 0
        try:
            self.ingest()
        finally:
            self.camera['compatible'] = compatible

    def status(self):
        original = self.folder / 'index.m3u8'
        compatible = self.folder / 'compatible' / 'index.m3u8'
        online = self.process is not None and self.process.poll() is None and time.time() - self.last_segment < 35
        relative = self.folder.relative_to(self.store.root / 'live').as_posix()
        return {'online': online, 'state': 'online' if online else ('connecting' if time.time()-self.started<35 else 'reconnecting'),
                'live_url': '/media/live/' + relative + '/index.m3u8' if original.exists() and self.mode!='storage' else None,
                'compatible_url': '/media/live/' + relative + '/compatible/index.m3u8' if compatible.exists() and self.mode!='storage' else None,
                'warning': self.warning, 'session': self.session, 'codec': self.codec, 'resolution': self.resolution, 'timing_source':self.timing_source, 'latest_capture':self.latest_capture, 'audio_warning':self.audio_warning}


class Engine:
    def __init__(self, store):
        self.store = store
        self.workers = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None
        self.diagnostic = None

    def start(self):
        self.store.reconcile()
        # Live cache is disposable. Persistent archive lives in a separate directory.
        for child in (self.store.root / 'live').iterdir():
            if child.is_dir():
                shutil.rmtree(child)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=60)

    def run(self):
        last_prune = 0
        while not self.stop_event.is_set():
            try:
                # Prune before copying, including after an ENOSPC error in the previous tick.
                if time.time()-last_prune > 5:
                    self.store.prune()
                    last_prune = time.time()
                mode=self.store.get('mode')
                cameras = self.desired_cameras()
                with self.lock:
                    for cid in list(self.workers):
                        worker = self.workers[cid]
                        if worker.mode!=mode or cid not in cameras or any(worker.camera.get(k)!=cameras[cid].get(k) for k in ('host','port','username','password','path','compatible','enabled')):
                            worker.stop()
                            del self.workers[cid]
                    for cid, camera in cameras.items():
                        worker = self.workers.get(cid)
                        if worker:
                            worker.poll_probe()
                            worker.ingest()
                            dead = worker.probe is None and (worker.process is None or worker.process.poll() is not None)
                            stale = time.time() - max(worker.last_segment, worker.started) > 45
                            if (dead and time.time()-worker.started>10) or stale:
                                worker.stop()
                                del self.workers[cid]
                                worker = None
                        if worker is None:
                            worker = Worker(self.store, camera)
                            worker.start()
                            self.workers[cid] = worker
                if int(time.time()) % 10 == 0:
                    # Retired live sessions may still be in a viewer's buffer briefly.
                    active = {w.folder for w in self.workers.values()}
                    for folder in (self.store.root / 'live').glob('*/*'):
                        if folder.is_dir() and folder not in active and time.time()-folder.stat().st_mtime>120:
                            shutil.rmtree(folder, ignore_errors=True)
                self.error = None
            except Exception as error:
                detail=safe_error(error)
                if detail!=self.diagnostic:
                    logging.getLogger('reovault').error('Engine failure: %s',json.dumps(detail))
                self.diagnostic=detail
                self.error = 'Recording engine needs attention. Run reovault diagnose for the error location.'
            self.stop_event.wait(1)
        with self.lock:
            for worker in self.workers.values():
                worker.stop()
            self.workers.clear()

    def desired_cameras(self):
        storage_only=self.store.get('mode')=='storage'
        return {c['id']:c for c in self.store.cameras() if c['enabled'] and (not storage_only or c['record_enabled'])}

    def statuses(self):
        with self.lock:
            return {cid: worker.status() for cid, worker in self.workers.items()}
