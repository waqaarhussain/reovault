"""Private go2rtc control API; authenticated browser signalling is proxied by app.py."""
import json
import os
import re
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
            streams={};preload={}
            for camera in cameras.values():
                source=(native_preview_camera(camera) if camera['compatible'] else None) or camera
                name='camera_'+camera['id']
                # A separate local source avoids converter recursion and shares one camera connection.
                source_name=name+'_source'
                streams[source_name]=[rtsp_url(source)]
                # Keep only the native low-resolution input warm. Opus remains viewer-demanded.
                if camera['compatible'] and native_preview_camera(camera):preload[source_name]='video&audio'
                local='rtsp://127.0.0.1:'+str(self.rtsp_port)+'/'+source_name
                # go2rtc's ffmpeg: module rejects FFmpeg <5 (Ubuntu 22.04 ships 4.x).
                # exec: uses an explicit command compatible with both 4.x and newer.
                # Only loopback URLs reach argv; camera credentials stay in the private config.
                audio=('exec:ffmpeg -nostdin -hide_banner -loglevel error -rtsp_transport tcp '
                    '-i '+local+'?audio -map 0:a:0 -vn -c:a libopus -application lowdelay '
                    '-ar 48000 -ac 2 -af aresample=async=1:first_pts=0 '
                    '-rtsp_transport tcp -f rtsp {output}')
                streams[name]=[local,audio]
            public=os.environ.get('REOVAULT_WEBRTC_IP') or self.store.get('webrtc_public_ip')
            config={'api':{'listen':'127.0.0.1:'+str(self.api_port)},
                'rtsp':{'listen':'127.0.0.1:'+str(self.rtsp_port)},
                'webrtc':{'listen':':'+str(self.media_port),'candidates':[str(public)+':'+str(self.media_port)] if public else ['stun:'+str(self.media_port)]},
                'log':{'level':'error'},'streams':streams}
            if preload:config['preload']=preload
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

    def diagnostics(self):
        result=self.status();result['streams']={}
        try:
            version=subprocess.run(['ffmpeg','-version'],capture_output=True,text=True,timeout=3)
            match=re.search(r'^ffmpeg version ([0-9][0-9A-Za-z.+:~_-]*)',version.stdout)
            result['ffmpeg_version']=match.group(1) if match else 'unknown'
        except (OSError,subprocess.TimeoutExpired):result['ffmpeg_version']='unavailable'
        if not result['available']:return result
        try:
            with urllib.request.urlopen(self.base+'/api/streams',timeout=2) as response:
                data=json.loads(response.read(1048576))
            # Never return raw gateway objects: URLs, SDP and commands contain credentials.
            for camera in self.store.cameras():
                stream=data.get('camera_'+camera['id'],{})
                summary={}
                for side,tracks in (('producers','receivers'),('consumers','senders')):
                    summary[side]=[]
                    for peer in stream.get(side,[]) or []:
                        codecs=[]
                        for track in peer.get(tracks,[]) or []:
                            codec=track.get('codec',{})
                            name=codec.get('codec_name') or codec.get('name')
                            if isinstance(name,str) and name.lower() in ('h264','h265','hevc','aac','opus','pcma','pcmu','mpeg4-generic'):
                                codecs.append({'codec':name,**{k:track[k] for k in ('bytes','packets') if isinstance(track.get(k),(int,float))}})
                        summary[side].append({'tracks':codecs,**{k:peer[k] for k in ('recv','send') if isinstance(peer.get(k),(int,float))}})
                result['streams'][camera['id']]=summary
        except (OSError,ValueError,TypeError,AttributeError):result['diagnostic_unavailable']=True
        return result

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
