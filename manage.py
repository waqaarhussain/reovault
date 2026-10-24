#!/usr/bin/env python3
"""Local administration. Passwords never appear in command arguments."""
import getpass
import json
import os
import secrets
import shutil
import sys
import time
from zoneinfo import ZoneInfo
import urllib.error
import urllib.request
from pathlib import Path

from core import Store, atomic_json, password_hash

CONFIG = Path(os.environ.get('REOVAULT_CONFIG', '/etc/reovault/config.json'))
DATA = Path(os.environ.get('REOVAULT_DATA', '/var/lib/reovault'))


def setup():
    if CONFIG.exists():
        print('Existing configuration preserved. Use reovault password to reset the dashboard password.')
        return
    print('\nReoVault setup. Each camera stays OFF until you run record followed by its name.\n')
    username = input('Dashboard username [admin]: ').strip() or 'admin'
    while True:
        password = getpass.getpass('Dashboard password (at least 12 characters): ')
        if len(password)<12:
            print('Use at least 12 characters.')
        elif password != getpass.getpass('Confirm password: '):
            print('Passwords did not match.')
        else:
            break
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    config = {'username':username,'password_hash':password_hash(password),
              'secret':secrets.token_hex(32),'control_token':secrets.token_hex(32)}
    atomic_json(CONFIG, config)
    store = Store(DATA)
    default = max(1, int(shutil.disk_usage(DATA).free/1e9*.75)-2)
    while True:
        try:
            quota = float(input(f'Recording storage allowance in GB [{default}]: ') or default)
            if not 1 <= quota <= 1000000:
                raise ValueError()
            break
        except ValueError:
            print('Enter a number from 1 to 1000000.')
    store.set('quota_gb',quota)
    choice=input('Mode: 1 storage only, 2 live only, 3 both [3]: ').strip() or '3'
    store.set_mode({'1':'storage','2':'live','3':'both'}.get(choice,'both'))
    store.db.close()
    print('Dashboard configured. Add camera connections in the dashboard after installation.\n')


def api(action,**values):
    if not CONFIG.exists():sys.exit('ReoVault is not configured. Run the installer first.')
    config=json.loads(CONFIG.read_text())
    request=urllib.request.Request('http://127.0.0.1:8765/api/control',data=json.dumps({'action':action,**values}).encode(),
        headers={'Content-Type':'application/json','X-Control-Token':config['control_token']})
    try:
        with urllib.request.urlopen(request,timeout=30) as response:return json.load(response)
    except urllib.error.HTTPError as e:
        try:message=json.load(e)['detail']
        except Exception:message='ReoVault rejected this request.'
        sys.exit(message)
    except OSError:sys.exit('Cannot reach ReoVault. Check: sudo systemctl status reovault')


def select_camera(cameras,name=None):
    if not cameras:sys.exit('No cameras configured. Add a camera in the dashboard first.')
    if name:
        matches=[c for c in cameras if name.casefold() in (c['name'].casefold(),c['id'].casefold())]
        if len(matches)==1:return matches[0]
        if not matches:print('Camera not found: '+name)
        else:print('More than one camera has that name. Choose its number.')
    for i,c in enumerate(cameras,1):
        print(f"{i}. {c['name']} [recording {'ON' if c['record_enabled'] else 'OFF'}]")
    choice=input('Camera name or number: ').strip()
    if choice.isdigit() and 1<=int(choice)<=len(cameras):return cameras[int(choice)-1]
    matches=[c for c in cameras if choice.casefold()==c['name'].casefold()]
    if len(matches)!=1:sys.exit('No unique camera selected. Run the helper again and choose its number.')
    return matches[0]


def record_command(args):
    if args==['status']:
        data=api('status')
        print('Mode: '+data.get('mode','both')+(' (archiving paused)' if data.get('mode')=='live' else ''))
        for c in data['cameras']:
            timing=data['statuses'].get(c['id'],{}).get('timing_source','waiting-camera-rtcp')
            print(f"{c['name']}: {'ON' if c['record_enabled'] else 'OFF'}; time source: {timing}")
        return
    action='start'
    if args and args[0] in ('stop','--stop','start'):
        action='stop' if args.pop(0) in ('stop','--stop') else 'start'
    data=api('list');camera=select_camera(data['cameras'],' '.join(args) or None)
    result=api(action,camera_id=camera['id'])
    print(f"{camera['name']}: recording {'ON' if action=='start' else 'OFF'}")
    if action=='start':print('Only this camera was enabled. Newly added cameras stay OFF. Capture timestamps must verify before footage is archived.')


def mode_command(args):
    labels={'storage':'Storage only','live':'Live view only','both':'Storage and live view'}
    current=api('status')['mode']
    if args==['status']:
        print('Current mode: '+labels[current]);return
    value=args[0] if len(args)==1 else None
    if value is None:
        print('Current mode: '+labels[current])
        for i,(key,label) in enumerate(labels.items(),1):print(f'{i}. {label} ({key})')
        value=input('Choose mode [1/2/3]: ').strip().lower()
        value={'1':'storage','2':'live','3':'both'}.get(value,value)
    if value not in labels:sys.exit('Usage: reovault mode [storage|live|both|status]')
    api('mode',mode=value)
    print('Mode: '+labels[value])
    print('Per-camera recording choices are preserved. New cameras stay OFF until record "Camera Name".')
    if value=='storage':print('Live serving and compatible conversion are disabled. Only recording-enabled cameras connect.')
    if value=='live':print('Archiving is paused for every camera. Existing footage remains available.')
    print('Camera connections may restart briefly while the mode changes.')


def diagnose_command():
    data=api('status')
    print('Mode: '+data.get('mode','both'))
    print('WebRTC: '+json.dumps(api('rtc-status')))
    print('Engine warning: '+str(data.get('engine_warning') or 'none'))
    print('Engine diagnostic: '+json.dumps(data.get('engine_diagnostic')))
    for c in data['cameras']:
        status=data['statuses'].get(c['id'],{})
        print(c['name']+': '+json.dumps({k:status.get(k) for k in ('state','session','codec','resolution','timing_source','warning','preview_method','preview')}))
        timings=sorted((DATA/'live'/c['id']).glob('*/timing.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        if timings:
            try:print('Latest source timing: '+timings[0].read_text())
            except OSError:pass
        failures=sorted((DATA/'live'/c['id']).glob('*/failure.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        if failures:
            try:
                print('Latest capture failure age (seconds): '+str(round(time.time()-failures[0].stat().st_mtime,1)))
                print('Latest capture failure: '+failures[0].read_text())
            except OSError:pass
    import subprocess
    result=subprocess.run([sys.executable,'-c','import av; print("PyAV:",av.__version__)'],capture_output=True,text=True)
    print(result.stdout.strip() if result.returncode==0 else 'PyAV import failed; dependency or shared-library issue.')


def download_command():
    from clip_input import date_input,range_input,interval
    data=api('list',include_deleted=True);camera=select_camera(data['cameras'])
    zone=ZoneInfo(data['timezone'])
    print('Timezone: '+str(zone)+'. Date has no year; current year is used.')
    def occurrence(naive,candidates):
        print(str(naive)+' occurs twice when the clocks change.')
        for i,c in enumerate(candidates,1):print(f"{i}. UTC offset {c.utcoffset()}")
        return input('First or second occurrence (1/2): ')
    try:
        day=date_input(input('Date DD/MM [today]: '),zone)
        value=input('Start - end time (e.g. 0757pm - 08:10pm): ')
        first,last=range_input(value)
        start,end=interval(day,first,last,zone,occurrence)
    except ValueError as e:sys.exit(str(e))
    print('Saving '+camera['name']+': '+start.isoformat()+' to '+end.isoformat())
    job=api('export',camera_id=camera['id'],start=start.timestamp(),end=end.timestamp())
    while job['status'] not in ('complete','failed'):
        print('Export: '+job['status'],end='\r',flush=True)
        time.sleep(1)
        job=api('export-status',job_id=job['id'])
    print()
    if job['status']=='failed':sys.exit(job['error'])
    print('Saved on the VPS: '+job['path'])
    print('Refresh Termius SFTP and download this file to your device.')
    print(job['warning'])


def reset_password():
    config = json.loads(CONFIG.read_text())
    while True:
        password = getpass.getpass('New dashboard password (at least 12 characters): ')
        if len(password)>=12 and password==getpass.getpass('Confirm password: '):
            break
        print('Password too short or confirmation did not match.')
    config['password_hash'] = password_hash(password)
    atomic_json(CONFIG,config)
    store = Store(DATA)
    store.execute('DELETE FROM sessions')
    print('Password updated and sessions revoked. Restart: sudo systemctl restart reovault')


if __name__=='__main__':
    args=sys.argv[1:]
    if args==['setup']:setup()
    elif args==['password']:reset_password()
    elif args==['download']:download_command()
    elif args==['diagnose']:diagnose_command()
    elif args==['network']:
        import ipaddress
        value=str(ipaddress.IPv4Address(os.environ['REOVAULT_PUBLIC_IP']))
        store=Store(DATA);store.set('webrtc_public_ip',value);store.db.close()
    elif args and args[0]=='mode':mode_command(args[1:])
    else:record_command(args)
