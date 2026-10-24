"""Exercise deployment scripts with local downloads and fake system commands."""
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

import pytest

ROOT=Path(__file__).resolve().parents[1]
SHA='a'*40


def executable(path,content):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def scoped(script):
    return script.replace('source /etc/os-release', 'ID=ubuntu; VERSION_ID=22.04').replace(
        'if [[ ${EUID} -ne 0 ]]; then exec sudo bash "$0" "$@"; fi', ': # Privilege escalation disabled in sandbox tests')


def make_bootstrap_fixture(tmp_path,bad_revision=False):
    bin_dir=tmp_path/'bin';bin_dir.mkdir()
    archive=tmp_path/'source.tar.gz'
    files={name:'placeholder' for name in ['app.py','core.py','requirements.txt','static/vendor/hls.min.js']}
    files['install.sh']='''#!/usr/bin/env bash
set -euo pipefail
read -r answer
printf '%s:%s:%s' "$REOVAULT_REVISION" "$answer" "${1:-fresh}" > "$TEST_RESULT"
'''
    with tarfile.open(archive,'w:gz') as handle:
        for name,content in files.items():
            data=content.encode();entry=tarfile.TarInfo('reovault-'+SHA+'/'+name)
            entry.size=len(data);entry.mode=0o755 if name.endswith('.sh') else 0o644
            handle.addfile(entry,io.BytesIO(data))
    executable(bin_dir/'curl','''#!/usr/bin/env python3
import json,os,pathlib,shutil,sys
args=sys.argv[1:];url=next(x for x in args if x.startswith('https://'));out=pathlib.Path(args[args.index('-o')+1])
with open(os.environ['TEST_REQUESTS'],'a') as log:log.write(url+'\\n')
if '/commits/main' in url:out.write_text(json.dumps({'sha':os.environ['TEST_SHA']}))
else:shutil.copyfile(os.environ['TEST_ARCHIVE'],out)
''')
    # Use the actual current interpreter rather than depending on /usr/bin Python packages.
    env={**os.environ,'PATH':str(bin_dir)+os.pathsep+os.environ['PATH'],
         'TEST_SHA':'invalid' if bad_revision else SHA,'TEST_ARCHIVE':str(archive),
         'TEST_REQUESTS':str(tmp_path/'requests'),'TEST_RESULT':str(tmp_path/'result')}
    script=tmp_path/'bootstrap.sh';script.write_text(scoped((ROOT/'bootstrap.sh').read_text()))
    return script,env


def test_bootstrap_downloads_one_pinned_commit_and_preserves_stdin(tmp_path):
    script,env=make_bootstrap_fixture(tmp_path)
    result=subprocess.run(['bash',str(script)],input='interactive-input\n',text=True,capture_output=True,env=env)
    assert result.returncode==0,result.stderr
    assert (tmp_path/'result').read_text()==SHA+':interactive-input:fresh'
    assert (tmp_path/'requests').read_text().splitlines()==[
        'https://api.github.com/repos/waqaarhussain/reovault/commits/main',
        'https://codeload.github.com/waqaarhussain/reovault/tar.gz/'+SHA]


def test_bootstrap_rejects_invalid_revision_before_install(tmp_path):
    script,env=make_bootstrap_fixture(tmp_path,bad_revision=True)
    result=subprocess.run(['bash',str(script)],input='unused\n',text=True,capture_output=True,env=env)
    assert result.returncode!=0
    assert not (tmp_path/'result').exists()
    assert len((tmp_path/'requests').read_text().splitlines())==1


@pytest.mark.parametrize('fail_activation',[False,True])
def test_release_activation_and_rollback_preserve_runtime_data(tmp_path,fail_activation):
    bin_dir=tmp_path/'bin';bin_dir.mkdir()
    fs=tmp_path/'root';base=fs/'opt/reovault';base.mkdir(parents=True)
    config=fs/'etc/reovault/config.json';config.parent.mkdir(parents=True);config.write_text('private-settings')
    data=fs/'var/lib/reovault'
    (data/'archive').mkdir(parents=True);(data/'live').mkdir()
    (data/'archive/precious.mp4').write_bytes(b'original-footage')
    (data/'index.sqlite').write_bytes(b'original-recording-state')
    old=base/'releases/old';old.mkdir(parents=True)
    (old/'REVISION').write_text('old')
    (base/'current').symlink_to(old)
    service=fs/'etc/systemd/system/reovault.service';service.parent.mkdir(parents=True);service.write_text('old-service')
    (fs/'usr/local/bin').mkdir(parents=True)
    # Existing unrelated updater must be preserved.
    unrelated=fs/'usr/local/bin/update-live';executable(unrelated,'#!/bin/sh\necho another-app\n')
    executable(bin_dir/'update-live','#!/bin/sh\necho another-app\n')
    for name in ['apt-get','id','chown','systemctl','journalctl','groupadd','usermod','getent']:
        executable(bin_dir/name,'#!/bin/sh\nexit 0\n')
    executable(bin_dir/'install','''#!/usr/bin/env python3
import os,sys
args=sys.argv[1:];out=[];i=0
while i<len(args):
 if args[i] in ('-o','-g'):i+=2
 else:out.append(args[i]);i+=1
os.execv('/usr/bin/install',['install',*out])
''')
    executable(bin_dir/'curl','''#!/bin/sh
case "$*" in
 *api.ipify.org*) echo 203.0.113.7; exit 0 ;;
 *) if [ "$TEST_FAIL_ACTIVATION" = 1 ]; then exit 1; fi ;;
esac
exit 0
''')
    executable(bin_dir/'sleep','#!/bin/sh\nexit 0\n')
    executable(bin_dir/'python3','''#!/usr/bin/env python3
import os,pathlib,sys
if sys.argv[1:3]==['-m','venv']:
 path=pathlib.Path(sys.argv[3])/'bin';path.mkdir(parents=True)
 for name in ['python','pip']:
  f=path/name;f.write_text('#!/bin/sh\\nexit 0\\n');f.chmod(0o755)
'''.replace('#!/usr/bin/env python3','#!/usr/bin/python3',1))
    # Mock executables use /usr/bin/python3 to avoid recursively invoking the venv stub.
    for name in ('install',):
        p=bin_dir/name;p.write_text(p.read_text().replace('#!/usr/bin/env python3','#!/usr/bin/python3'))
    source=tmp_path/'source';source.mkdir()
    for name in ['app.py','core.py','capture.py','media_time.py','device.py','exporter.py','clip_input.py','requirements.txt','manage.py','bootstrap.sh','update-live.sh','static/vendor/hls.min.js']:
        p=source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('placeholder')
    (fs/'root').mkdir(exist_ok=True)
    script=scoped((ROOT/'install.sh').read_text())
    import re
    paths=['/opt/reovault','/etc/reovault','/var/lib/reovault','/etc/systemd/system/reovault.service','/usr/local/bin','/root/Reolink','/Reolink']
    script=re.sub('|'.join(re.escape(x) for x in sorted(paths,key=len,reverse=True)),lambda m:str(fs)+m.group(),script)
    script_path=source/'install.sh';script_path.write_text(script)
    env={**os.environ,'PATH':str(bin_dir)+os.pathsep+os.environ['PATH'],
         'TEST_FAIL_ACTIVATION':'1' if fail_activation else '0','REOVAULT_REVISION':SHA}
    result=subprocess.run(['bash',str(script_path),'--update'],capture_output=True,text=True,env=env)
    assert (config).read_text()=='private-settings'
    assert (data/'archive/precious.mp4').read_bytes()==b'original-footage'
    assert (data/'index.sqlite').read_bytes()==b'original-recording-state'
    assert unrelated.read_text()=='#!/bin/sh\necho another-app\n'
    if fail_activation:
        assert result.returncode!=0
        assert (base/'current').resolve()==old
        assert service.read_text()=='old-service'
    else:
        assert result.returncode==0,result.stdout+result.stderr
        assert (base/'current').resolve()!=old
        assert (base/'current/REVISION').read_text().strip()==SHA
        assert (base/'current/venv/bin/python').exists()
        assert 'Preserved it.' in result.stdout
