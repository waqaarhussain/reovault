"""One RTSP connection, original packet remuxing and camera RTCP timestamp extraction.
Credentials arrive through stdin, never process arguments or persistent error logs.
"""
import json
import signal
import struct
import sys
import time
from pathlib import Path

from core import atomic_json, safe_error


def packet_clock(packet):
    if packet.pts is None or not packet.has_sidedata('prft'):return None
    side=bytes(packet.get_sidedata('prft'))
    if len(side)<12:return None
    wall_us,flags=struct.unpack_from('=qi',side)
    # FFmpeg flags 24 identify the RTP sender-report reference, not muxer wallclock fallback.
    if flags!=24 or wall_us<=0:return None
    return wall_us/1e6-float(packet.pts*packet.time_base)


def capture(url,folder,stop=None):
    import av
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    av.logging.set_level(av.logging.PANIC)
    options={'rtsp_transport':'tcp','timeout':'15000000'} if url.startswith('rtsp:') else {}
    source=av.open(url,options=options,timeout=(15,15))
    video=source.streams.video[0]
    audio_warning=None
    if source.streams.audio and source.streams.audio[0].codec_context.name!='aac':audio_warning='The source audio codec is not AAC; this version is recording video only.'
    atomic_json(folder/'timing.json',{'source':'waiting-camera-rtcp','audio_warning':audio_warning})
    selected=[video]+([source.streams.audio[0]] if source.streams.audio and source.streams.audio[0].codec_context.name=='aac' else [])
    output=av.open(str(folder/'raw.m3u8'),'w',format='hls',options={
        'hls_time':'4','hls_list_size':'16','hls_delete_threshold':'16','hls_segment_type':'fmp4',
        'hls_fmp4_init_filename':'init.mp4','hls_flags':'delete_segments+temp_file+program_date_time+independent_segments',
        'hls_segment_filename':str(folder/'seg_%09d.m4s'),'avoid_negative_ts':'disabled'})
    streams={s.index:output.add_stream_from_template(s) for s in selected}
    if video.codec_context.name=='hevc':streams[video.index].codec_context.codec_tag='hvc1'
    last_write=0;anchor=None;started=False
    try:
        for packet in source.demux(selected):
            if stop is not None and stop():break
            if packet.dts is None:continue
            if packet.stream.index==video.index:
                candidate=packet_clock(packet)
                if candidate is not None:
                    if anchor is not None and abs(candidate-anchor)>1:
                        raise ValueError('Camera capture clock jumped; reconnecting without rewriting old footage')
                    anchor=candidate
                    if time.monotonic()-last_write>1:
                        atomic_json(folder/'timing.json',{'source':'camera-rtcp','pts_to_utc':anchor,'updated':time.time(),'audio_warning':audio_warning})
                        last_write=time.monotonic()
                if not started:
                    if not packet.is_keyframe:continue
                    started=True
            if not started:continue
            packet.stream=streams[packet.stream.index]
            output.mux(packet)
    finally:
        output.close();source.close()


if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_:sys.exit(0))
    info=None
    try:
        info=json.load(sys.stdin)
        capture(info['url'],info['folder'])
    except SystemExit:raise
    except Exception as error:
        # Persist structural diagnostics, never raw third-party messages containing input URLs.
        if info:
            try:atomic_json(Path(info['folder'])/'failure.json',safe_error(error))
            except Exception:pass
        sys.exit(1)
