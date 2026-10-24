"""ReoVault recording engine. All timestamps in the index are UTC epoch seconds."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from functools import lru_cache
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote


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
        self.setting_default('recording', False)
        self.setting_default('record_since', 0)
        self.setting_default('quota_gb', 100)
        self.setting_default('reserve_gb', 2)

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

    def record(self, enabled):
        with self.lock:
            # One transaction: the worker cannot observe a new switch with an old timestamp.
            if enabled and not self.get('recording'):
                self.set('record_since', time.time())
            self.set('recording', bool(enabled))

    def archive_path(self, row):
        return self.root / 'archive' / row['camera_id'] / row['session'] / row['name']

    def storage(self):
        total = self.rows('SELECT COALESCE(SUM(size),0) AS n FROM segments')[0]['n']
        # Include init segments, which are required to decode each archive session.
        total += sum(p.stat().st_size for p in (self.root / 'archive').glob('*/*/init.mp4') if p.exists())
        disk = shutil.disk_usage(self.root)
        return {'used_bytes': total, 'quota_bytes': self.get('quota_gb') * 10**9,
                'disk_free_bytes': disk.free, 'reserve_bytes': self.get('reserve_gb') * 10**9}

    def prune(self):
        """Delete indexed, completed segments only. Never touch the live ring or active files."""
        info = self.storage()
        used, free = info['used_bytes'], info['disk_free_bytes']
        while used > info['quota_bytes'] or free < info['reserve_bytes']:
            oldest = self.rows('SELECT * FROM segments ORDER BY start,id LIMIT 1')
            if not oldest:
                break
            row = oldest[0]
            path = self.archive_path(row)
            size = path.stat().st_size if path.exists() else 0
            path.unlink(missing_ok=True)
            self.execute('DELETE FROM segments WHERE id=?', (row['id'],))
            used -= row['size']
            free += size
            if not self.rows('SELECT id FROM segments WHERE camera_id=? AND session=? LIMIT 1',
                             (row['camera_id'], row['session'])):
                init = path.parent / 'init.mp4'
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


def hls_output(folder, compatible=False, codec=None):
    """FFmpeg HLS with atomic, finalized fMP4 segments. Video is copied unless opted in."""
    video = ['-c:v', 'copy'] + (['-tag:v', 'hvc1'] if codec == 'hevc' else [])
    if compatible:
        video = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p',
                 '-vf', "scale='min(1920,iw)':-2", '-force_key_frames', 'expr:gte(t,n_forced*2)', '-threads', '2']
    return ['-map', '0:v:0', '-map', '0:a:0?', *video, '-c:a', 'aac', '-b:a', '96k',
            '-f', 'hls', '-hls_time', '4', '-hls_list_size', '10', '-hls_delete_threshold', '6',
            '-hls_segment_type', 'fmp4', '-hls_fmp4_init_filename', 'init.mp4',
            '-hls_flags', 'delete_segments+temp_file+program_date_time+independent_segments',
            '-hls_segment_filename', str(folder / 'seg_%09d.m4s'), str(folder / 'index.m3u8')]


def playlist_entries(path):
    """Return only segments advertised by the atomically published manifest."""
    if not path.exists():
        return []
    entries, start, duration = [], None, None
    for line in path.read_text().splitlines():
        if line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
            start = datetime.fromisoformat(line.split(':', 1)[1].replace('Z', '+00:00')).timestamp()
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
        self.process = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-rtsp_transport', 'tcp', *rtsp_timeout('ffmpeg', 15000000), '-fflags', '+genpts+discardcorrupt',
            '-i', rtsp_url(self.camera), *hls_output(self.folder, codec=self.codec)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.started = time.time()

    def ingest(self):
        entries = playlist_entries(self.folder / 'index.m3u8')
        for name, start, duration in entries:
            path = self.folder / name
            if path.exists():
                self.last_segment = max(self.last_segment, path.stat().st_mtime)
            if name in self.seen:
                continue
            with self.store.lock:
                recording = self.store.get('recording')
                since = self.store.get('record_since')
            if not recording or start + duration <= since:
                self.seen.add(name)
                continue
            target = self.store.root / 'archive' / self.camera['id'] / self.session
            target.mkdir(parents=True, exist_ok=True)
            if not (target / 'init.mp4').exists():
                shutil.copyfile(self.folder / 'init.mp4', target / 'init.tmp')
                (target / 'init.tmp').replace(target / 'init.mp4')
            try:
                shutil.copyfile(path, target / (name + '.tmp'))
            except FileNotFoundError:
                self.warning = 'A source segment expired before it could be archived.'
                continue
            (target / (name + '.tmp')).replace(target / name)
            size = (target / name).stat().st_size
            self.store.execute('INSERT OR IGNORE INTO segments(camera_id,session,name,start,duration,size) VALUES(?,?,?,?,?,?)',
                               (self.camera['id'], self.session, name, start, duration, size))
            self.seen.add(name)
        # Keep memory bounded during long-running camera sessions.
        self.seen.intersection_update({e[0] for e in entries})
        if self.camera['compatible'] and entries and self.transcoder is None:
            target = self.folder / 'compatible'
            target.mkdir(exist_ok=True)
            self.transcoder = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                '-live_start_index', '-3', '-allowed_extensions', 'ALL', '-i', str(self.folder / 'index.m3u8'),
                *hls_output(target, compatible=True)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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
                'live_url': '/media/live/' + relative + '/index.m3u8' if original.exists() else None,
                'compatible_url': '/media/live/' + relative + '/compatible/index.m3u8' if compatible.exists() else None,
                'warning': self.warning, 'session': self.session, 'codec': self.codec, 'resolution': self.resolution}


class Engine:
    def __init__(self, store):
        self.store = store
        self.workers = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None

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
                cameras = {c['id']: c for c in self.store.cameras() if c['enabled']}
                with self.lock:
                    for cid in list(self.workers):
                        worker = self.workers[cid]
                        if cid not in cameras or worker.camera != cameras[cid]:
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
            except Exception:
                # Never return raw third-party exception text or input URLs to the web UI.
                self.error = 'Recording engine needs attention. Check disk space, camera connection and configured ports.'
            self.stop_event.wait(1)
        with self.lock:
            for worker in self.workers.values():
                worker.stop()
            self.workers.clear()

    def statuses(self):
        with self.lock:
            return {cid: worker.status() for cid, worker in self.workers.items()}
