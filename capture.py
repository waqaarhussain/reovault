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


def mux_reason(logs):
    # Return fixed labels only. FFmpeg log strings can contain credential-bearing URLs.
    messages=' '.join(str(item[-1]).lower() for item in logs)
    if 'non monotonically increasing dts' in messages or 'non-monotonic' in messages:return 'non-monotonic-timestamps'
    if 'pts' in messages and '< dts' in messages:return 'presentation-before-decode-timestamp'
    if 'timestamps are unset' in messages:return 'missing-timestamps'
    if 'no space left on device' in messages:return 'disk-full'
    if 'could not write header' in messages:return 'output-header'
    return 'unclassified-mux-error'


def mux_packet(output,packet,context):
    import av
    # Capturing around one mux call bounds memory, including on a long-running stream.
    with av.logging.Capture() as logs:
        try:output.mux(packet)
        except Exception as error:
            failure=safe_error(error)
            failure.update(context)
            failure['mux_reason']=mux_reason(logs)
            raise CaptureFailure(failure) from None


class CaptureFailure(Exception):
    def __init__(self,detail):self.detail=detail


def capture(url,folder,stop=None):
    import av
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    av.logging.set_level(av.logging.ERROR)
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
    packet_counts={};clock_flags={};previous_dts={};begin=time.monotonic()
    try:
        for packet in source.demux(selected):
            if stop is not None and stop():break
            if packet.dts is None:continue
            index=packet.stream.index
            packet_counts[index]=packet_counts.get(index,0)+1
            if packet.has_sidedata('prft'):
                reference=bytes(packet.get_sidedata('prft'))
                if len(reference)>=12:
                    flag=struct.unpack_from('=qi',reference)[1]
                    clock_flags[str(flag)]=clock_flags.get(str(flag),0)+1
            if packet.stream.index==video.index:
                candidate=packet_clock(packet)
                if candidate is not None:
                    if anchor is not None and abs(candidate-anchor)>1:
                        raise ValueError('Camera capture clock jumped; reconnecting without rewriting old footage')
                    anchor=candidate
                if time.monotonic()-last_write>1:
                    atomic_json(folder/'timing.json',{'source':'camera-rtcp' if anchor is not None else 'waiting-camera-rtcp',
                        'pts_to_utc':anchor,'updated':time.time(),'audio_warning':audio_warning,
                        'packet_counts':dict(packet_counts),'prft_flags':dict(clock_flags),'elapsed_seconds':round(time.monotonic()-begin,2)})
                    last_write=time.monotonic()
                if not started:
                    if not packet.is_keyframe:continue
                    started=True
            if not started:continue
            context={'stage':'mux','stream_type':packet.stream.type,'codec':packet.stream.codec_context.name,
                'pts':packet.pts,'dts':packet.dts,'previous_dts':previous_dts.get(index),
                'time_base':str(packet.time_base),'duration':packet.duration,
                'packet_counts':dict(packet_counts),'prft_flags':dict(clock_flags),'elapsed_seconds':round(time.monotonic()-begin,2)}
            previous_dts[index]=packet.dts
            packet.stream=streams[index]
            mux_packet(output,packet,context)
    finally:
        active_error=sys.exc_info()[0] is not None
        try:output.close()
        except Exception:
            if not active_error:raise
        finally:source.close()


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
            try:atomic_json(Path(info['folder'])/'failure.json',error.detail if isinstance(error,CaptureFailure) else safe_error(error))
            except Exception:pass
        sys.exit(1)
