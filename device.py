"""Read-only ONVIF name and clock discovery."""
import base64
import hashlib
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime,timezone
from urllib.parse import unquote
from xml.sax.saxutils import escape


NS='http://www.onvif.org/ver10/device/wsdl'


def soap(camera,operation):
    port=int(camera.get('onvif_port') or 0)
    if not port:raise ValueError('Set the forwarded ONVIF port to read the device name and clock.')
    nonce=os.urandom(20);created=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    digest=base64.b64encode(hashlib.sha1(nonce+created.encode()+camera['password'].encode()).digest()).decode()
    user=escape(camera['username'])
    body=f'''<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="{NS}" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"><s:Header><wsse:Security><wsse:UsernameToken><wsse:Username>{user}</wsse:Username><wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password><wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security></s:Header><s:Body><tds:{operation}/></s:Body></s:Envelope>'''
    request=urllib.request.Request(f"http://{camera['host']}:{port}/onvif/device_service",data=body.encode(),headers={
        'Content-Type':f'application/soap+xml; charset=utf-8; action="{NS}/{operation}"'})
    before=time.time()
    with urllib.request.urlopen(request,timeout=6) as response:data=response.read(65537)
    after=time.time()
    if len(data)>65536 or b'<!DOCTYPE' in data or b'<!ENTITY' in data:raise ValueError('Unexpected ONVIF response')
    return ET.fromstring(data),(before+after)/2


def discover(camera):
    root,_=soap(camera,'GetScopes');name=None
    for node in root.iter():
        value=(node.text or '').strip()
        if node.tag.endswith('ScopeItem') and '/name/' in value:
            name=unquote(value.split('/name/',1)[1]).strip()
            break
    if not name or len(name)>60:raise ValueError('The camera did not return an ONVIF device name.')
    result={'name':name,'clock_offset':None,'checked':time.time()}
    try:
        root,midpoint=soap(camera,'GetSystemDateAndTime')
        utc=next(n for n in root.iter() if n.tag.endswith('UTCDateTime'))
        values={n.tag.rsplit('}',1)[-1]:int(n.text) for n in utc.iter() if n.text and n.text.strip().isdigit()}
        camera_time=datetime(values['Year'],values['Month'],values['Day'],values['Hour'],values['Minute'],values['Second'],tzinfo=timezone.utc).timestamp()
        result['clock_offset']=camera_time-midpoint
    except (OSError,ValueError,KeyError,StopIteration,ET.ParseError):pass
    return result
