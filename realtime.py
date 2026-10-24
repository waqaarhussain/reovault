"""Private go2rtc control API; authenticated browser signalling is proxied by app.py."""
import json
import os
import subprocess
import threading
import time
import uuid
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlencode

from core import atomic_json,rtsp_url,native_preview_camera


class Realtime:
    def __init__(self,store):
        self.store=store;self.process=None;self.signature=None;self.generation=None
        self.lock=threading.RLock();self.ready=False;self.error=None
        self.binary=Path(os.environ.get('REOVAULT_GO2RTC',str(Path(__file__).parent/'bin/go2rtc')))
        self.api_port=int(os.environ.get('REOVAULT_RTC_API_PORT','19844'))
        self.rtsp_port=int(os.environ.get('REOVAULT_RTC_RTSP_PORT','18555'))
        self.media_port=int(os.environ.get('REOVAULT_RTC_MEDIA_PORT','8555'))
        self.folder=store.root/'realtime';self.folder.mkdir(exist_ok=True,mode=0o700)

    def sync(self,cameras,mode):
        with self.lock:
            if mode=='storage' or not cameras or not self.binary.is_file():
                self.stop();self.error=None;return
            streams={}
            for camera in cameras.values():
                source=(native_preview_camera(camera) if camera['compatible'] else None) or camera
                name='camera_'+camera['id']
                # Opus conversion is audio-only and starts on demand. Video is passed through.
                streams[name]=[rtsp_url(source),'ffmpeg:'+name+'#audio=opus']
            public=os.environ.get('REOVAULT_WEBRTC_IP') or self.store.get('webrtc_public_ip')
            config={'api':{'listen':'127.0.0.1:'+str(self.api_port)},
                'rtsp':{'listen':'127.0.0.1:'+str(self.rtsp_port)},
                'webrtc':{'listen':':'+str(self.media_port),'candidates':[str(public)+':'+str(self.media_port)] if public else ['stun:'+str(self.media_port)]},
                'log':{'level':'error'},'streams':streams}
            if public:config['webrtc']['ice_servers']=[]
            signature=json.dumps(config,sort_keys=True)
            if self.signature==signature and self.process is not None and self.process.poll() is None:
                if not self.ready:
                    try:
                        with urllib.request.urlopen(self.base+'/api',timeout=.3):self.ready=True
                    except OSError:pass
                return
            self.stop()
            path=self.folder/'go2rtc.json';atomic_json(path,config);os.chmod(path,0o600)
            try:self.process=subprocess.Popen([str(self.binary),'-config',str(path)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            except OSError:
                self.error='Low-latency service could not start. Buffered live viewing remains available.'
                return
            self.signature=signature;self.generation=uuid.uuid4().hex;self.error=None

    @property
    def base(self):return 'http://127.0.0.1:'+str(self.api_port)

    def answer(self,camera_id,sdp):
        if not self.ready:raise ValueError('Low-latency service is starting. Try again shortly.')
        request=urllib.request.Request(self.base+'/api/webrtc?'+urlencode({'src':'camera_'+camera_id}),
            data=sdp.encode(),headers={'Content-Type':'application/sdp'})
        try:
            with urllib.request.urlopen(request,timeout=20) as response:result=response.read(65537)
        except urllib.error.HTTPError as error:
            message=error.read(8192).decode(errors='replace').lower()
            self.error=('WebRTC network discovery was denied. Check service address-family permissions.' if 'netlink' in message or 'operation not permitted' in message else 'WebRTC could not negotiate this camera stream. Use compatible viewing or buffered playback.')
            raise ValueError(self.error) from None
        except OSError:
            self.error='WebRTC negotiation timed out. Buffered playback remains available.'
            raise ValueError(self.error) from None
        if len(result)>65536:raise ValueError('Unexpected WebRTC response.')
        self.error=None
        return result.decode()

    def status(self):
        with self.lock:return {'available':self.ready and self.process is not None and self.process.poll() is None,'generation':self.generation,'port':self.media_port,'error':self.error,'installed':self.binary.is_file(),'exit_code':self.process.poll() if self.process is not None else None}

    def stop(self):
        with self.lock:
            self.ready=False
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()
                try:self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=3)
            self.process=None;self.signature=None
