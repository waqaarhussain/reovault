"""Original-codec time-range exports, pinned against retention while FFmpeg reads them."""
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


class Exports:
    def __init__(self,store):
        self.store=store;self.jobs={};self.lock=threading.Lock()

    def start(self,camera_id,start,end):
        if not end>start or end-start>86400:raise ValueError('Choose a time range of up to 24 hours.')
        camera=self.store.camera(camera_id)
        if not camera:raise ValueError('Camera not found')
        if not self.lock.acquire(blocking=False):raise ValueError('An export is already running. Wait for it to finish.')
        job_id=uuid.uuid4().hex
        job={'id':job_id,'status':'preparing','error':None,'path':None,'warning':None}
        self.jobs[job_id]=job
        def run():
            try:self.export(camera,start,end,job)
            except Exception as error:
                job['status']='failed'
                job['error']=str(error) if isinstance(error,ValueError) else 'Export failed. Check available disk space and that the footage still exists.'
            finally:self.lock.release()
        threading.Thread(target=run,daemon=True).start()
        return dict(job)

    def export(self,camera,start,end,job):
        saves=self.store.camera_folder(camera['id'])/'Saves';saves.mkdir(exist_ok=True)
        staging=saves/('.export-'+job['id']);staging.mkdir()
        try:
            with self.store.lock:
                rows=self.store.rows('SELECT * FROM segments WHERE camera_id=? AND start+duration>? AND start<? ORDER BY start,id',(camera['id'],start,end))
                if not rows:raise ValueError('No recording exists for that time range.')
                coverage=start
                for row in rows:
                    if row['start']>coverage+.3:raise ValueError('The requested interval contains a recording gap. Select a continuous range shown on the timeline.')
                    coverage=max(coverage,row['start']+row['duration'])
                if coverage<end-.3:raise ValueError('The end of that interval has not been recorded yet or has expired.')
                disk=shutil.disk_usage(saves)
                if disk.free<sum(r['size'] for r in rows)+self.store.get('reserve_gb')*1e9:
                    raise ValueError('Not enough free space to save this clip while keeping the disk reserve.')
                for i,row in enumerate(rows):
                    target=staging/f'{i:08d}.mp4';source=self.store.archive_path(row)
                    if row.get('init_size'):
                        try:os.link(source,target)
                        except OSError:shutil.copyfile(source,target)
                    else:
                        with target.open('wb') as handle:
                            handle.write(self.store.initialization(row))
                            with source.open('rb') as fragment:shutil.copyfileobj(fragment,handle)
            lines=['ffconcat version 1.0'];signature=None
            for i,row in enumerate(rows):
                filename=f'{i:08d}.mp4'
                probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=start_time:stream=codec_type,codec_name,width,height,time_base','-of','json',str(staging/filename)],stderr=subprocess.DEVNULL,timeout=20))
                current=[(s.get('codec_type'),s.get('codec_name'),s.get('width'),s.get('height'),s.get('time_base')) for s in probe['streams']]
                if signature is not None and current!=signature:raise ValueError('Camera format changed within this interval. Export each side of the change separately to preserve original quality.')
                signature=current
                origin=float(probe['format'].get('start_time') or 0)
                first=max(start,row['start']);last=min(end,row['start']+row['duration'])
                lines += [f"file '{filename}'",f'inpoint {origin+first-row["start"]:.9f}',
                          f'outpoint {origin+last-row["start"]:.9f}',f'duration {last-first:.9f}']
            manifest=staging/'clip.ffconcat';manifest.write_text('\n'.join(lines)+'\n')
            zone=ZoneInfo(self.store.get('timezone'));a=datetime.fromtimestamp(start,zone);b=datetime.fromtimestamp(end,zone)
            name=a.strftime('%Y-%m-%d_%H-%M-%S%z')+'_to_'+b.strftime('%Y-%m-%d_%H-%M-%S%z')+'_'+job['id'][:8]+'.mp4'
            temporary=staging/'export.mp4';output=saves/name
            job['status']='exporting'
            result=subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-nostdin','-f','concat','-safe','0','-i',str(manifest),
                '-map','0:v:0','-map','0:a:0?','-c','copy','-avoid_negative_ts','make_zero','-movflags','+faststart',str(temporary)],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3600)
            if result.returncode or not temporary.exists() or temporary.stat().st_size==0:raise ValueError('FFmpeg could not join that range without re-encoding. Try a smaller interval.')
            temporary.replace(output)
            job['warning']='Original-quality cuts follow camera keyframes; the edge may extend slightly outside your requested times.'
            metadata={'camera':camera['device_name'] or camera['name'],'requested_start':a.isoformat(),'requested_end':b.isoformat(),
                'timezone':str(zone),'source_timing':sorted({r['timing_source'] for r in rows}),'video_reencoded':False,'boundary_note':job['warning']}
            output.with_suffix('.json').write_text(json.dumps(metadata,indent=2))
            job.update(status='complete',path=str(output),camera=camera['name'],start=start,end=end)
        finally:shutil.rmtree(staging,ignore_errors=True)
