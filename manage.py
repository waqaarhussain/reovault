#!/usr/bin/env python3
"""Local administration. Passwords never appear in command arguments."""
import getpass
import json
import os
import secrets
import shutil
import sys
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
    print('\nReoVault setup. Recording stays OFF until you run record.\n')
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
    store.db.close()
    print('Dashboard configured. Add camera connections in the dashboard after installation.\n')


def control(action):
    if not CONFIG.exists():
        sys.exit('ReoVault is not configured. Run install.sh first.')
    config = json.loads(CONFIG.read_text())
    request = urllib.request.Request('http://127.0.0.1:8765/api/control',
            data=json.dumps({'action':action}).encode(),
            headers={'Content-Type':'application/json','X-Control-Token':config['control_token']})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.load(response)
    except (OSError, urllib.error.HTTPError):
        sys.exit('Cannot reach ReoVault. Check: sudo systemctl status reovault')
    print('Recording: '+('ON' if data['recording'] else 'OFF'))
    print('Configured cameras: '+str(data['cameras']))
    s = data['storage']
    print(f"Storage: {s['used_bytes']/1e9:.2f} GB / {s['quota_bytes']/1e9:g} GB")
    if action=='start':
        print('Recording stays enabled after reboots. First footage appears when a segment closes.')


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
    if args==['setup']:
        setup()
    elif args==['password']:
        reset_password()
    elif len(args)<=1 and (not args or args[0] in ('start','stop','status')):
        control(args[0] if args else 'start')
    else:
        sys.exit('Usage: record [start|stop|status]  |  reovault password')
