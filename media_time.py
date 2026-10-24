"""Read the presentation time of the first video sample in an ISO BMFF fragment."""
import struct


def boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    while start+8<=end:
        size,kind=struct.unpack_from('>I4s',data,start);header=8
        if size==1:
            if start+16>end:break
            size=struct.unpack_from('>Q',data,start+8)[0];header=16
        elif size==0:size=end-start
        if size<header or start+size>end:break
        yield kind,start+header,start+size
        start+=size


def children(data,kind,start=0,end=None):
    return [(a,b) for k,a,b in boxes(data,start,end) if k==kind]


def video_track(init):
    for moov,end in children(init,b'moov'):
        for trak,te in children(init,b'trak',moov,end):
            tk=children(init,b'tkhd',trak,te)
            for mdia,me in children(init,b'mdia',trak,te):
                h=children(init,b'hdlr',mdia,me);m=children(init,b'mdhd',mdia,me)
                if tk and h and m and init[h[0][0]+8:h[0][0]+12]==b'vide':
                    t=tk[0][0];d=m[0][0]
                    track=struct.unpack_from('>I',init,t+(20 if init[t]==1 else 12))[0]
                    scale=struct.unpack_from('>I',init,d+(20 if init[d]==1 else 12))[0]
                    return track,scale
    raise ValueError('No video track in camera initialization data')


def first_video_pts(init,fragment):
    track,scale=video_track(init)
    for moof,end in children(fragment,b'moof'):
        for traf,te in children(fragment,b'traf',moof,end):
            headers=children(fragment,b'tfhd',traf,te)
            if not headers or struct.unpack_from('>I',fragment,headers[0][0]+4)[0]!=track:continue
            t=children(fragment,b'tfdt',traf,te);r=children(fragment,b'trun',traf,te)
            if not t or not r:continue
            d=t[0][0]
            decode=struct.unpack_from('>Q' if fragment[d]==1 else '>I',fragment,d+4)[0]
            p=r[0][0];flags=int.from_bytes(fragment[p+1:p+4],'big');version=fragment[p]
            count=struct.unpack_from('>I',fragment,p+4)[0]
            if not count:continue
            offset=p+8+(4 if flags&1 else 0)+(4 if flags&4 else 0)
            offset+=(4 if flags&0x100 else 0)+(4 if flags&0x200 else 0)+(4 if flags&0x400 else 0)
            composition=struct.unpack_from('>i' if version else '>I',fragment,offset)[0] if flags&0x800 else 0
            return (decode+composition)/scale
    raise ValueError('No video sample timestamp in fragment')
