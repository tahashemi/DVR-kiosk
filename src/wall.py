"""Client for the dvrwall compositor's control socket.

dvrwall holds the DVR connections persistently, so switching profiles or going
fullscreen is a socket write rather than a teardown-and-rebuild -- the old
ffmpeg approach re-paid ~130s of sequential connect cost on every change.
"""
import json
import socket

SOCK_PATH = "/run/dvrwall.sock"

# Fast-fail by default: a hung or dying compositor must never stall a
# dashboard request thread. dvrwall's control socket is handled by a single
# serial accept() loop (see control_thread() in dvrwall.c), so one slow
# command already queues behind it; this used to be made much worse by a
# 20s default timeout plus a single lock serializing *every* wall.py caller
# in this process, so one stuck call blocked all of them -- that combination
# is what exhausted the dashboard's waitress worker pool and produced the
# "TCP accepts, TLS handshake never completes" hang even while the dashboard
# process itself was alive and healthy.
DEFAULT_TIMEOUT = 2
SET_CHANNELS_TIMEOUT = 15   # legitimately slower: dropping old streams can
                            # take up to dvrwall's ~10s per-stream socket
                            # timeout before it frees them -- see roster_set()


class WallError(Exception):
    pass


def _send(cmd, timeout=DEFAULT_TIMEOUT):
    # No cross-call lock: each call opens its own socket and touches no
    # shared state, so there's nothing here that needs serializing -- and
    # serializing anyway just meant one slow caller stalled every other
    # dashboard thread waiting on this lock, on top of dvrwall's own
    # single-threaded control socket queue.
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
    return _send("CHANNELS " + " ".join(urls), timeout=SET_CHANNELS_TIMEOUT)


def set_layout(channels):
    """channels: [{"dvr":..., "ch":...}, ...] in display order."""
    urls = [stream_url(c["dvr"], c["ch"]) for c in channels]
    if not urls:
        return _send("STOP", timeout=SET_CHANNELS_TIMEOUT)
    return _send("LAYOUT " + " ".join(urls))


def set_fullscreen(dvr_key, ch, mainstream=True):
    return _send("FULLSCREEN " + stream_url(dvr_key, ch, mainstream=mainstream))


def set_fps(target_fps):
    """Dynamically set the compositor target framerate in dvrwall."""
    return _send(f"FPS {max(1, min(30, int(target_fps)))}")


def clear():
    """Blank the TV output only. The roster keeps decoding, so the dashboard's
    channel pool keeps showing live thumbnails -- this is a display
    convenience (dashboard "Turn Off Kiosk"), not a power-saving action."""
    return _send("CLEAR")


def stop():
    """Full teardown: every DVR connection drops. Used for the power schedule
    and process shutdown, where nothing should be decoding until turned back
    on."""
    return _send("STOP", timeout=SET_CHANNELS_TIMEOUT)


def blank():
    """Full teardown plus display standby (FB_BLANK_POWERDOWN)."""
    return _send("BLANK", timeout=SET_CHANNELS_TIMEOUT)


def status():
    try:
        return json.loads(_send("STATUS"))
    except (WallError, ValueError):
        return None


def alive():
    return status() is not None
