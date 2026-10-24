import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from core import Store, Engine, password_hash
from device import discover
from exporter import Exports

ROOT = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get('REOVAULT_CONFIG', '/etc/reovault/config.json'))
SETTINGS = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
STORE = Store(os.environ.get('REOVAULT_DATA', '/var/lib/reovault'))
ENGINE = Engine(STORE)
EXPORTS = Exports(STORE)
FAILURES = defaultdict(list)


@asynccontextmanager
async def lifespan(app):
    if not SETTINGS.get('password_hash'):
        raise RuntimeError('Run setup with manage.py before starting ReoVault')
    ENGINE.start()
    yield
    ENGINE.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def csrf(token):
    return hmac.new(SETTINGS['secret'].encode(), token.encode(), hashlib.sha256).hexdigest()


@app.middleware('http')
async def guard(request: Request, call_next):
    public = request.url.path in ('/', '/static/app.css', '/static/app.js', '/api/login', '/favicon.svg')
    control = request.url.path == '/api/control'
    token = request.cookies.get('reovault_session', '')
    if not public and not control:
        found = STORE.rows('SELECT token FROM sessions WHERE token=? AND expires>?', (digest(token), time.time()))
        if not found:
            return JSONResponse({'detail': 'Sign in to continue.'}, status_code=401)
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), csrf(token)):
                return JSONResponse({'detail': 'Refresh the page and try again.'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.url.path.startswith(('/api/', '/media/')):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.get('/')
def home():
    return FileResponse(ROOT / 'static/index.html')


@app.get('/favicon.svg')
def favicon():
    return FileResponse(ROOT / 'static/favicon.svg')


@app.get('/static/{name:path}')
def static(name: str):
    allowed = {'app.css', 'app.js', 'vendor/hls.min.js'}
    if name not in allowed:
        raise HTTPException(404)
    return FileResponse(ROOT / 'static' / name)


@app.post('/api/login')
async def login(request: Request):
    # Limit request bodies before JSON parsing, including requests without Content-Length.
    raw = await request.body()
    if len(raw) > 4096:
        raise HTTPException(413)
    try:
        body = json.loads(raw)
        username, password = str(body.get('username', '')), str(body.get('password', ''))
    except (ValueError, AttributeError):
        raise HTTPException(400, 'Invalid sign-in request')
    origin = request.headers.get('origin')
    if origin and origin != str(request.base_url).rstrip('/'):
        raise HTTPException(403)
    ip = request.client.host
    now = time.time()
    for key in list(FAILURES):
        FAILURES[key] = [t for t in FAILURES[key] if now-t<600]
        if not FAILURES[key]:
            del FAILURES[key]
    if len(FAILURES[ip]) >= 8:
        raise HTTPException(429, 'Too many attempts. Wait 10 minutes.')
    expected = SETTINGS.get('password_hash', '')
    salt = expected.split(':')[0]
    candidate = await run_in_threadpool(password_hash, password, salt)
    if not (hmac.compare_digest(username, SETTINGS.get('username', 'admin')) and hmac.compare_digest(candidate, expected)):
        FAILURES[ip].append(now)
        raise HTTPException(401, 'Username or password is incorrect.')
    FAILURES.pop(ip, None)
    token = secrets.token_urlsafe(32)
    STORE.execute('DELETE FROM sessions WHERE expires<=?', (now,))
    STORE.execute('INSERT INTO sessions VALUES(?,?)', (digest(token), now+86400))
    response = JSONResponse({'ok': True})
    response.set_cookie('reovault_session', token, max_age=86400, httponly=True,
                        secure=request.url.scheme=='https', samesite='strict')
    return response


@app.post('/api/logout')
def logout(request: Request):
    STORE.execute('DELETE FROM sessions WHERE token=?', (digest(request.cookies.get('reovault_session', '')),))
    response = JSONResponse({'ok': True})
    response.delete_cookie('reovault_session')
    return response


@app.get('/api/status')
def status(request: Request):
    statuses = ENGINE.statuses()
    cameras = []
    for c in STORE.cameras():
        c.pop('password')
        c.update(statuses.get(c['id'], {'online': False, 'state': 'disabled' if not c['enabled'] else 'connecting',
                                       'live_url': None, 'compatible_url': None, 'session': None}))
        cameras.append(c)
    return {'cameras': cameras, 'recording':any(c['record_enabled'] for c in cameras), 'timezone':STORE.get('timezone'), 'storage': STORE.storage(),
            'engine_warning': ENGINE.error, 'csrf': csrf(request.cookies.get('reovault_session', '')),
            'username': SETTINGS.get('username', 'admin'), 'server_time': time.time()}


async def small_json(request):
    raw = await request.body()
    if len(raw) > 16384:
        raise HTTPException(413)
    try:
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError()
        return body
    except ValueError:
        raise HTTPException(400, 'Expected a JSON object')


def clean_camera(body, existing=None):
    existing = existing or {}
    name = str(body.get('name', existing.get('name', ''))).strip()
    host = str(body.get('host', existing.get('host', ''))).strip().lower()
    path = str(body.get('path', existing.get('path', '/Preview_01_main'))).strip()
    username = str(body.get('username', existing.get('username', 'admin')))
    password = str(body.get('password') or existing.get('password', ''))
    try:
        onvif_port=int(body.get('onvif_port',existing.get('onvif_port',0)) or 0)
        if not 0<=onvif_port<=65535:raise ValueError()
        port = int(body.get('port', existing.get('port', 554)))
    except (ValueError, TypeError):
        raise HTTPException(400, 'Port must be a number')
    if not name and onvif_port:name='Camera awaiting device name'
    if not name or len(name)>60:
        raise HTTPException(400, 'Enter a camera name, up to 60 characters.')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', host):
        raise HTTPException(400, 'Enter a hostname or IPv4 address, without a scheme, port or path.')
    if not 1 <= port <= 65535:
        raise HTTPException(400, 'Port must be between 1 and 65535')
    if not path.startswith('/') or len(path)>300 or any(x in path for x in ('?', '#', '@', '\\', '\n', '\r')):
        raise HTTPException(400, 'Enter only the RTSP path, for example /Preview_01_main')
    if not username or not password or len(username)>128 or len(password)>256:
        raise HTTPException(400, 'Camera username and password are required.')
    return {'id': existing.get('id', uuid_hex()), 'name': name, 'host': host, 'port': port,
            'username': username, 'password': password, 'path': path,
            'enabled': int(bool(body.get('enabled', existing.get('enabled', True)))),
            'compatible': int(bool(body.get('compatible', existing.get('compatible', False)))),
            'onvif_port':onvif_port}


def uuid_hex():
    return secrets.token_hex(8)


@app.post('/api/camera-name')
async def camera_name(request:Request):
    body=await small_json(request)
    old=STORE.camera(str(body.get('id',''))) or {}
    c=clean_camera(body,old)
    try:return await run_in_threadpool(discover,c)
    except Exception:raise HTTPException(400,'Cannot read the camera name. Check the forwarded ONVIF port and device credentials.')


@app.post('/api/cameras')
async def add_camera(request:Request):
    c=clean_camera(await small_json(request))
    device=None
    if c['onvif_port']:
        try:device=await run_in_threadpool(discover,c);c['name']=device['name']
        except Exception:raise HTTPException(400,'Cannot read the camera name. Check its ONVIF port, or enter a manual name and leave ONVIF blank.')
    STORE.execute('INSERT INTO cameras(id,name,host,port,username,password,path,enabled,compatible,onvif_port) VALUES(?,?,?,?,?,?,?,?,?,?)',tuple(c.values()))
    if device:STORE.execute('UPDATE cameras SET device_name=?,clock_offset=? WHERE id=?',(device['name'],device['clock_offset'],c['id']))
    return {'ok':True,'id':c['id'],'record_enabled':False}


@app.put('/api/cameras/{cid}')
async def edit_camera(cid: str, request: Request):
    old = STORE.camera(cid)
    if not old or old['deleted']:
        raise HTTPException(404)
    c = clean_camera(await small_json(request), old)
    if c['onvif_port']:
        try:
            device=await run_in_threadpool(discover,c);c['name']=device['name']
            STORE.execute('UPDATE cameras SET device_name=?,clock_offset=? WHERE id=?',(device['name'],device['clock_offset'],cid))
        except Exception:raise HTTPException(400,'Cannot read the camera name. Check its ONVIF port, or leave it blank to use a manual name.')
    if not c['onvif_port']:STORE.execute('UPDATE cameras SET device_name=NULL,clock_offset=NULL WHERE id=?',(cid,))
    STORE.execute('UPDATE cameras SET name=?,host=?,port=?,username=?,password=?,path=?,enabled=?,compatible=?,onvif_port=? WHERE id=?',
                  tuple(c[k] for k in ('name','host','port','username','password','path','enabled','compatible','onvif_port','id')))
    return {'ok': True}


@app.delete('/api/cameras/{cid}')
def remove_camera(cid: str):
    # Preserve recordings and camera metadata for playback until retention removes them.
    STORE.execute('UPDATE cameras SET deleted=1,enabled=0,record_enabled=0,password="" WHERE id=?', (cid,))
    return {'ok': True}


@app.post('/api/recording')
def recording():
    raise HTTPException(405,'Recording is controlled per camera with the local record helper.')


@app.post('/api/storage')
async def storage_settings(request: Request):
    body = await small_json(request)
    try:
        quota = float(body['quota_gb'])
        reserve = float(body.get('reserve_gb', STORE.get('reserve_gb')))
        if not (1<=quota<=1000000 and 1<=reserve<=1000):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, 'Set a recording allowance of at least 1 GB and free-space reserve of at least 1 GB.')
    STORE.set('quota_gb', quota)
    STORE.set('reserve_gb', reserve)
    return {'ok': True}


@app.post('/api/control')
async def control(request: Request):
    key = request.headers.get('x-control-token', '')
    if request.client.host not in ('127.0.0.1', '::1') or not hmac.compare_digest(key, SETTINGS.get('control_token', '')):
        raise HTTPException(403)
    body = await small_json(request)
    action=body.get('action');cid=str(body.get('camera_id',''))
    if action in ('start','stop'):
        try:STORE.record(cid,action=='start')
        except ValueError as e:raise HTTPException(400,str(e))
    elif action=='export':
        try:return EXPORTS.start(cid,float(body['start']),float(body['end']))
        except (ValueError,KeyError,TypeError) as e:raise HTTPException(400,str(e))
    elif action=='export-status':
        job=EXPORTS.jobs.get(str(body.get('job_id','')))
        if not job:raise HTTPException(404,'Export status was lost after a server restart. Run download again.')
        return dict(job)
    elif action not in ('status','list'):raise HTTPException(400)
    cameras=[{k:c[k] for k in ('id','name','device_name','record_enabled','onvif_port','clock_offset')} for c in STORE.cameras(bool(body.get('include_deleted')))]
    return {'cameras':cameras,'storage':STORE.storage(),'timezone':STORE.get('timezone'),'statuses':ENGINE.statuses()}


def safe_path(base, relative):
    target = (base / relative).resolve()
    if not target.is_relative_to(base.resolve()) or not target.is_file():
        raise HTTPException(404)
    if target.suffix not in ('.m3u8', '.mp4', '.m4s'):
        raise HTTPException(404)
    return target


@app.get('/media/{kind}/{relative:path}')
def media(kind: str, relative: str):
    if kind not in ('live', 'archive'):
        raise HTTPException(404)
    path = safe_path(STORE.root / kind, relative)
    types = {'.m3u8': 'application/vnd.apple.mpegurl', '.m4s': 'video/mp4', '.mp4': 'video/mp4'}
    return FileResponse(path, media_type=types[path.suffix])


@app.get('/api/recordings')
def recordings(camera: str = '', start: float = 0, end: float = 0, cursor: int = 0):
    end = end or time.time()+60
    if end-start>86400*32 or start<0 or end<start:
        raise HTTPException(400, 'Choose a range of up to 32 days.')
    sql = 'SELECT s.*,c.name AS camera_name FROM segments s JOIN cameras c ON c.id=s.camera_id WHERE s.start+s.duration>? AND s.start<?'
    args = [start,end]
    if camera:
        sql += ' AND s.camera_id=?'
        args.append(camera)
    if cursor:
        sql += ' AND s.id<?'
        args.append(cursor)
    rows = STORE.rows(sql+' ORDER BY s.id DESC LIMIT 301', args)
    return {'segments': rows[:300], 'next_cursor': rows[299]['id'] if len(rows)>300 else None,
            'cameras': [{'id':c['id'],'name':c['name']} for c in STORE.cameras(True)]}


@app.get('/api/playback/{cid}.m3u8')
def playback(cid: str, start: float, end: float):
    if end<=start or end-start>86400:
        raise HTTPException(400, 'Playback is limited to one day per playlist.')
    rows = STORE.rows('SELECT * FROM segments WHERE camera_id=? AND start+duration>? AND start<? ORDER BY start,id', (cid,start,end))
    if not rows:
        raise HTTPException(404, 'No recordings in this range.')
    import math
    lines = ['#EXTM3U', '#EXT-X-VERSION:7', '#EXT-X-TARGETDURATION:'+str(math.ceil(max(r['duration'] for r in rows))),
             '#EXT-X-MEDIA-SEQUENCE:0', '#EXT-X-PLAYLIST-TYPE:VOD']
    previous, previous_end = None, None
    for row in rows:
        session = row['session']
        if session != previous or (previous_end is not None and row['start']-previous_end>1):
            if previous is not None:
                lines.append('#EXT-X-DISCONTINUITY')
            lines.append(f'#EXT-X-MAP:URI="/api/inits/{cid}/{session}.mp4"')
        stamp = datetime.fromtimestamp(row['start'],timezone.utc).isoformat()
        lines += [f'#EXT-X-PROGRAM-DATE-TIME:{stamp}', f"#EXTINF:{row['duration']:.6f},", f"/api/fragments/{row['id']}.m4s"]
        previous, previous_end = session, row['start']+row['duration']
    lines.append('#EXT-X-ENDLIST')
    return PlainTextResponse('\n'.join(lines)+'\n', media_type='application/vnd.apple.mpegurl')


@app.get('/api/inits/{cid}/{session}.mp4')
def initialization(cid:str,session:str):
    rows=STORE.rows('SELECT * FROM segments WHERE camera_id=? AND session=? ORDER BY start LIMIT 1',(cid,session))
    if not rows:raise HTTPException(404)
    try:data=STORE.initialization(rows[0])
    except OSError:raise HTTPException(404)
    return StreamingResponse(iter([data]),media_type='video/mp4')


@app.get('/api/fragments/{sid}.m4s')
def fragment(sid:int):
    rows=STORE.rows('SELECT * FROM segments WHERE id=?',(sid,))
    if not rows:raise HTTPException(404)
    row=rows[0]
    try:handle=STORE.archive_path(row).open('rb');handle.seek(row['init_size'])
    except OSError:raise HTTPException(404)
    def stream():
        try:
            while block:=handle.read(1024*1024):yield block
        finally:handle.close()
    return StreamingResponse(stream(),media_type='video/mp4')


@app.get('/api/download/{sid}')
def download(sid:int):
    import shutil
    rows=STORE.rows('SELECT * FROM segments WHERE id=?',(sid,))
    if not rows:raise HTTPException(404,'This recording has expired.')
    row=rows[0]
    saves=STORE.camera_folder(row['camera_id'])/'Saves';saves.mkdir(exist_ok=True)
    stamp=datetime.fromtimestamp(row['start'],timezone.utc).strftime('%Y-%m-%d_%H-%M-%SZ')
    output=saves/(stamp+'_'+str(sid)+'_original.mp4')
    with STORE.lock:
        try:
            if not output.exists():
                if shutil.disk_usage(saves).free<row['size']+STORE.get('reserve_gb')*1e9:raise HTTPException(507,'Not enough free space to save this clip.')
                temp=output.with_suffix('.tmp')
                with temp.open('wb') as handle:
                    if not row['init_size']:handle.write(STORE.initialization(row))
                    with STORE.archive_path(row).open('rb') as source:shutil.copyfileobj(source,handle)
                temp.replace(output)
        except OSError:raise HTTPException(404,'The recording expired or could not be saved.')
    return FileResponse(output,media_type='video/mp4',filename=output.name)


@app.get('/api/timeline')
def timeline(camera: str, start: float, end: float, bucket: int = 1800):
    if end<=start or end-start>90001 or bucket not in (300,900,1800,3600):
        raise HTTPException(400, 'Choose a single day and a supported grouping.')
    rows = STORE.rows('SELECT start,duration,size FROM segments WHERE camera_id=? AND start+duration>? AND start<? ORDER BY start,id', (camera,start,end))
    ranges = []
    buckets = {}
    for row in rows:
        a, b = max(row['start'],start), min(row['start']+row['duration'],end)
        if ranges and a-ranges[-1][1]<0.25:
            ranges[-1][1] = max(ranges[-1][1], b)
        else:
            ranges.append([a,b])
        # Split duration and byte estimates at bucket boundaries.
        while a < b:
            idx = int((a-start)//bucket)
            right = min(b, start+(idx+1)*bucket)
            item = buckets.setdefault(idx, {'start':start+idx*bucket,'end':min(end,start+(idx+1)*bucket),'recorded_seconds':0,'bytes':0})
            item['recorded_seconds'] += right-a
            item['bytes'] += round(row['size']*(right-a)/row['duration'])
            a = right
    return {'ranges':ranges,'buckets':[buckets[i] for i in sorted(buckets)],'segment_count':len(rows)}

@app.get('/api/recording-cameras')
def recording_cameras():
    return [{'id':c['id'],'name':c['name']} for c in STORE.cameras(True)]
