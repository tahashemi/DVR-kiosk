"""Client for the dvrwall compositor's control socket.

dvrwall holds the DVR connections persistently, so switching profiles or going
fullscreen is a socket write rather than a teardown-and-rebuild -- the old
ffmpeg approach re-paid ~130s of sequential connect cost on every change.
"""
import json
import socket
import threading

SOCK_PATH = "/run/dvrwall.sock"
_lock = threading.Lock()


class WallError(Exception):
    pass


def _send(cmd, timeout=20):
    with _lock:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect(SOCK_PATH)
            s.sendall(cmd.encode())
            s.shutdown(socket.SHUT_WR)
            chunks = []
            while True:
                c = s.recv(65536)
                if not c:
                    break
                chunks.append(c)
            s.close()
            return b"".join(chunks).decode().strip()
        except (OSError, socket.timeout) as e:
            raise WallError(str(e))


def stream_url(dvr_key, ch, mainstream=False):
    import dvr_config
    return "rtsp://127.0.0.1:8554/" + dvr_config.stream_name(dvr_key, ch, mainstream=mainstream)


def set_channels(channels):
    """Establish decoding roster."""
    urls = [stream_url(c["dvr"], c["ch"], mainstream=c.get("mainstream", False)) for c in channels]
    return _send("CHANNELS " + " ".join(urls), timeout=30)


def set_layout(channels):
    """channels: [{"dvr":..., "ch":...}, ...] in display order."""
    urls = [stream_url(c["dvr"], c["ch"]) for c in channels]
    if not urls:
        return _send("STOP")
    return _send("LAYOUT " + " ".join(urls))


def set_fullscreen(dvr_key, ch, mainstream=True):
    return _send("FULLSCREEN " + stream_url(dvr_key, ch, mainstream=mainstream))


def clear():
    """Blank the TV output only. The roster keeps decoding, so the dashboard's
    channel pool keeps showing live thumbnails -- this is a display
    convenience (dashboard "Turn Off Kiosk"), not a power-saving action."""
    return _send("CLEAR")


def stop():
    """Full teardown: every DVR connection drops. Used for the power schedule
    and process shutdown, where nothing should be decoding until turned back
    on."""
    return _send("STOP")


def blank():
    """Full teardown plus display standby (FB_BLANK_POWERDOWN)."""
    return _send("BLANK")


def status():
    try:
        return json.loads(_send("STATUS", timeout=10))
    except (WallError, ValueError):
        return None


def alive():
    return status() is not None
