/*
 * hwdec -- pluggable H.264 decode for dvrwall.
 *
 * dvrwall reads packets with libavformat as before; only the decode step is
 * swappable. Every backend hands back a frame in system memory (planar or
 * semi-planar YUV) that the caller scales with swscale exactly like a
 * software-decoded frame, so nothing downstream knows which decoder ran.
 *
 * Backends, in "auto" order (first that opens wins, software always last):
 *   gst-v4l2sl  GStreamer v4l2slh264dec   -- stateless V4L2 (Allwinner cedrus,
 *               mainline Rockchip rkvdec/hantro, NXP, MediaTek). Mainline
 *               FFmpeg has no driver for these, GStreamer does.
 *   gst-nv      GStreamer nvv4l2decoder   -- NVIDIA Jetson
 *   v4l2m2m     FFmpeg h264_v4l2m2m       -- stateful V4L2 (Raspberry Pi 2-4,
 *               Amlogic, Qualcomm venus)
 *   rkmpp       FFmpeg h264_rkmpp         -- Rockchip vendor kernels (only in
 *               FFmpeg builds that include it)
 *   vaapi       FFmpeg VA-API hwaccel     -- Intel/AMD PCs
 *   cuda        FFmpeg NVDEC hwaccel      -- NVIDIA PCs
 *   software    libavcodec                -- everything else
 */
#ifndef DVRWALL_HWDEC_H
#define DVRWALL_HWDEC_H

#include <libavcodec/avcodec.h>

struct vdec;

/* A decoded frame, valid until the next vdec_receive()/vdec_close(). */
struct vframe {
    int w, h;
    enum AVPixelFormat fmt;
    uint8_t *data[4];
    int linesize[4];
};

/* Global decoder policy: "auto", "software", or one backend name. */
void hwdec_set_policy(const char *policy);
const char *hwdec_policy(void);
/* Streams above this many pixels skip hardware backends that can't take
 * them (0 = no limit). Most SBC decoders top out at 1080p. */
void hwdec_set_max_pixels(long px);
/* Cap on concurrent hardware sessions (0 = no cap). */
void hwdec_set_max_sessions(int n);
void hwdec_init(void);
/* Print which backends this machine has (probes them all). */
void hwdec_report(void);

/* Open the best allowed decoder. allow_hw=0 forces software. */
struct vdec *vdec_open(const AVCodecParameters *par, int threads, int allow_hw);
/* <0: decoder is broken (caller should fall back); 0: ok. */
int vdec_send(struct vdec *d, const AVPacket *pkt);
/* 1: frame in *out; 0: none yet; <0: decoder is broken. */
int vdec_receive(struct vdec *d, struct vframe *out);
void vdec_flush(struct vdec *d);
int vdec_is_hw(const struct vdec *d);
const char *vdec_name(const struct vdec *d);
void vdec_close(struct vdec **d);

#endif
