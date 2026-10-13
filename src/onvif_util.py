"""Minimal ONVIF client: WS-Discovery and PTZ, standard library only.

The usual Python ONVIF stack (onvif-zeep + zeep + lxml) costs tens of MB of
RAM and seconds of import time, which this board can't spare for a feature that
is used occasionally. Everything here is a handful of fixed SOAP envelopes sent
with urllib and parsed with ElementTree -- namespace-agnostic, because cameras
disagree about prefixes and versions.

Only what the UI needs is implemented: find cameras, learn their PTZ endpoint
and profile token, ContinuousMove / Stop, and preset list / goto / set / remove.
"""

import base64
import datetime
import hashlib
import os
import re
import socket
import threading
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

TIMEOUT = 2.5

_discover_lock = threading.Lock()


# ---- XML helpers ------------------------------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find_all(root, name):
    return [el for el in root.iter() if _local(el.tag) == name]


def _find(root, name):
    for el in root.iter():
        if _local(el.tag) == name:
            return el
    return None


def _text(root, name):
    el = _find(root, name)
    return (el.text or "").strip() if el is not None else ""


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---- WS-Discovery -----------------------------------------------------------

_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header>
<w:MessageID>uuid:{mid}</w:MessageID>
<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
</e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>"""


def _scope_value(scopes, kind):
    # Scopes look like onvif://www.onvif.org/name/Front%20Door
    from urllib.parse import unquote
    for sc in scopes:
        m = re.search(r"onvif://www\.onvif\.org/" + kind + r"/(.+)$", sc)
        if m:
            return unquote(m.group(1)).replace("_", " ")
    return ""


def discover(timeout=3.0):
    """Multicast a WS-Discovery Probe and collect replies.

    Returns [{"host", "port", "xaddr", "name", "hardware"}], deduplicated by
    host. Only cameras on the same L2 segment answer -- multicast doesn't cross
    routers -- so manual entry must always remain possible.

    Serialised by a lock: one scan at a time, so repeated clicks can't occupy
    several of the web server's threads.
    """
    if not _discover_lock.acquire(blocking=False):
        raise RuntimeError("scan already running")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.settimeout(0.5)
        msg = _PROBE.format(mid=uuid.uuid4()).encode()
        try:
            sock.sendto(msg, ("239.255.255.250", 3702))
            found = {}
            deadline = datetime.datetime.now() + datetime.timedelta(seconds=timeout)
            while datetime.datetime.now() < deadline:
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    continue
                try:
                    root = ET.fromstring(data)
                except ET.ParseError:
                    continue
                for match in _find_all(root, "ProbeMatch"):
                    xaddrs = _text(match, "XAddrs").split()
                    scopes = _text(match, "Scopes").split()
                    # Prefer an IPv4 xaddr; some cameras also advertise IPv6.
                    xaddr = next((x for x in xaddrs if re.match(r"https?://\d", x)),
                                 xaddrs[0] if xaddrs else "")
                    parts = urlsplit(xaddr) if xaddr else None
                    host = (parts.hostname if parts else None) or addr[0]
                    if host in found:
                        continue
                    found[host] = {
                        "host": host,
                        "port": (parts.port if parts and parts.port else 80),
                        "xaddr": xaddr,
                        "name": _scope_value(scopes, "name"),
                        "hardware": _scope_value(scopes, "hardware"),
                    }
            return sorted(found.values(), key=lambda c: socket.inet_aton(c["host"])
                          if re.match(r"^\d+\.\d+\.\d+\.\d+$", c["host"]) else b"")
        finally:
            sock.close()
    finally:
        _discover_lock.release()


# ---- SOAP with WS-Security UsernameToken ------------------------------------

def _security_header(user, password):
    if not user:
        return ""
    nonce = os.urandom(16)
    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + str(password).encode()).digest()).decode()
    return f"""<s:Header><Security s:mustUnderstand="1"
 xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
<UsernameToken><Username>{_esc(user)}</Username>
<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>
<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{created}</Created>
</UsernameToken></Security></s:Header>"""


class OnvifError(Exception):
    pass


def _call(xaddr, user, password, body, timeout=TIMEOUT):
    envelope = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema">'
        + _security_header(user, password)
        + f"<s:Body>{body}</s:Body></s:Envelope>")
    req = urllib.request.Request(
        xaddr, data=envelope.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
    except urllib.error.HTTPError as e:
        data = e.read()
        fault = ""
        try:
            fault = _text(ET.fromstring(data), "Text") or _text(ET.fromstring(data), "Reason")
        except ET.ParseError:
            pass
        if e.code in (400, 401) and ("auth" in fault.lower() or "sender" in fault.lower() or e.code == 401):
            raise OnvifError("authentication failed")
        raise OnvifError(fault or f"HTTP {e.code}")
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise OnvifError(f"unreachable ({getattr(e, 'reason', e)})")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise OnvifError("bad response")
    if _find(root, "Fault") is not None:
        raise OnvifError(_text(root, "Text") or "camera returned a fault")
    return root


def device_xaddr(host, port):
    return f"http://{host}:{port}/onvif/device_service"


def probe(host, port, user, password):
    """Learn Media/PTZ endpoints and the main profile token.

    Returns {"ptz", "ptz_xaddr", "media_xaddr", "profile_token", "name"}.
    Raises OnvifError on auth/reachability problems, which the UI shows as-is.
    """
    dev = device_xaddr(host, port)
    caps = _call(dev, user, password,
                 '<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>')
    media_xaddr = ptz_xaddr = ""
    for el in caps.iter():
        name = _local(el.tag)
        if name in ("Media", "PTZ"):
            x = _text(el, "XAddr")
            if name == "Media" and not media_xaddr:
                media_xaddr = x
            elif name == "PTZ" and not ptz_xaddr:
                ptz_xaddr = x
    if not media_xaddr:
        raise OnvifError("camera has no media service")
    profs = _call(media_xaddr, user, password, "<trt:GetProfiles/>")
    profiles = _find_all(profs, "Profiles")
    if not profiles:
        raise OnvifError("camera reports no media profiles")
    token, has_ptz_cfg = "", False
    for p in profiles:
        if _find(p, "PTZConfiguration") is not None:
            token, has_ptz_cfg = p.get("token", ""), True
            break
    if not token:
        token = profiles[0].get("token", "")
    name = ""
    try:
        info = _call(dev, user, password, "<tds:GetDeviceInformation/>")
        name = " ".join(x for x in (_text(info, "Manufacturer"), _text(info, "Model")) if x)
    except OnvifError:
        pass
    return {"ptz": bool(ptz_xaddr and has_ptz_cfg), "ptz_xaddr": ptz_xaddr,
            "media_xaddr": media_xaddr, "profile_token": token, "name": name}


def _media_xaddr(host, port, user, password):
    caps = _call(device_xaddr(host, port), user, password,
                 '<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>')
    for el in caps.iter():
        if _local(el.tag) == "Media":
            x = _text(el, "XAddr")
            if x:
                return x
    raise OnvifError("camera has no media service")


def _with_credentials(uri, host, user, password):
    """Cameras return stream URIs without credentials (and sometimes with an
    internal or wildcard address); make the URI usable as-is by go2rtc."""
    from urllib.parse import quote, urlsplit as _split, urlunsplit
    p = _split(uri)
    hostname = p.hostname or host
    if hostname in ("0.0.0.0", "127.0.0.1", "localhost"):
        hostname = host
    netloc = hostname + (f":{p.port}" if p.port else "")
    if user and p.username is None:
        netloc = f"{quote(user, safe='')}:{quote(password or '', safe='')}@{netloc}"
    elif p.username is not None:
        netloc = p.netloc
    return urlunsplit((p.scheme, netloc, p.path, p.query, ""))


def stream_profiles(host, port, user, password):
    """Every media profile with its resolution, codec and real RTSP address.

    Asking the camera for each profile's stream URI is the only reliable way
    to tell sub from main: profile order and naming vary by vendor, so
    "profile #2 is the sub-stream" guesses pick the wrong one on many models.
    Returned URIs contain credentials -- keep them server-side.
    """
    media = _media_xaddr(host, port, user, password)
    root = _call(media, user, password, "<trt:GetProfiles/>")
    out = []
    for p in _find_all(root, "Profiles"):
        token = p.get("token", "")
        if not token:
            continue
        enc = _find(p, "VideoEncoderConfiguration")
        res = _find(enc, "Resolution") if enc is not None else None
        w = int(_text(res, "Width") or 0) if res is not None else 0
        h = int(_text(res, "Height") or 0) if res is not None else 0
        codec = (_text(enc, "Encoding") if enc is not None else "") or "?"
        try:
            r = _call(media, user, password,
                      '<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>'
                      '<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>'
                      f'<trt:ProfileToken>{_esc(token)}</trt:ProfileToken></trt:GetStreamUri>')
            uri = _text(r, "Uri")
        except OnvifError:
            uri = ""
        if not uri.startswith("rtsp"):
            continue
        out.append({"token": token, "name": _text(p, "Name") or token, "width": w, "height": h,
                    "codec": codec.upper(), "uri": _with_credentials(uri, host, user, password)})
    if not out:
        raise OnvifError("camera reported no RTSP streams")
    return out


def pick_streams(profiles):
    """(sub, main) choice. Main = highest resolution. Sub = lowest-resolution
    H.264 stream, because every grid tile is decoded in software on this board
    and H.265 costs several times more CPU per pixel."""
    by_px = sorted(profiles, key=lambda p: p["width"] * p["height"])
    main = by_px[-1]
    h264 = [p for p in by_px if "264" in p["codec"]]
    sub = (h264 or by_px)[0]
    return sub, main


def _clamp(v):
    try:
        return max(-1.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def continuous_move(cam, pan, tilt, zoom):
    pan, tilt, zoom = _clamp(pan), _clamp(tilt), _clamp(zoom)
    body = (f'<tptz:ContinuousMove><tptz:ProfileToken>{_esc(cam["profile_token"])}</tptz:ProfileToken>'
            f'<tptz:Velocity><tt:PanTilt x="{pan:.3f}" y="{tilt:.3f}"/>'
            f'<tt:Zoom x="{zoom:.3f}"/></tptz:Velocity></tptz:ContinuousMove>')
    _call(cam["ptz_xaddr"], cam.get("user"), cam.get("password"), body)


def stop(cam):
    body = (f'<tptz:Stop><tptz:ProfileToken>{_esc(cam["profile_token"])}</tptz:ProfileToken>'
            '<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>')
    _call(cam["ptz_xaddr"], cam.get("user"), cam.get("password"), body)


def get_presets(cam):
    root = _call(cam["ptz_xaddr"], cam.get("user"), cam.get("password"),
                 f'<tptz:GetPresets><tptz:ProfileToken>{_esc(cam["profile_token"])}</tptz:ProfileToken></tptz:GetPresets>')
    out = []
    for p in _find_all(root, "Preset"):
        tok = p.get("token", "")
        if tok:
            out.append({"token": tok, "name": _text(p, "Name") or f"Preset {tok}"})
    return out


def goto_preset(cam, token):
    _call(cam["ptz_xaddr"], cam.get("user"), cam.get("password"),
          f'<tptz:GotoPreset><tptz:ProfileToken>{_esc(cam["profile_token"])}</tptz:ProfileToken>'
          f'<tptz:PresetToken>{_esc(token)}</tptz:PresetToken></tptz:GotoPreset>')


def goto_home(cam):
    _call(cam["ptz_xaddr"], cam.get("user"), cam.get("password"),
          f'<tptz:GotoHomePosition><tptz:ProfileToken>{_esc(cam["profile_token"])}</tptz:ProfileToken></tptz:GotoHomePosition>')


def set_preset(cam, name, token=None):
    """Save the current position. Passing an existing token renames/overwrites it."""
    tok = f"<tptz:PresetToken>{_esc(token)}</tptz:PresetToken>" if token else ""
    root = _call(cam["ptz_xaddr"], cam.get("user"), cam.get("password"),
                 f'<tptz:SetPreset><tptz:ProfileToken>{_esc(cam["profile_token"])}</tptz:ProfileToken>'
                 f'<tptz:PresetName>{_esc(name)}</tptz:PresetName>{tok}</tptz:SetPreset>')
    return _text(root, "PresetToken") or token


def remove_preset(cam, token):
    _call(cam["ptz_xaddr"], cam.get("user"), cam.get("password"),
          f'<tptz:RemovePreset><tptz:ProfileToken>{_esc(cam["profile_token"])}</tptz:ProfileToken>'
          f'<tptz:PresetToken>{_esc(token)}</tptz:PresetToken></tptz:RemovePreset>')
