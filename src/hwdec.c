/* hwdec -- pluggable H.264/HEVC decode for dvrwall. See hwdec.h. */
#define _GNU_SOURCE
#include "hwdec.h"

#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <libavutil/hwcontext.h>
#include <libavutil/imgutils.h>

#ifdef HAVE_GST
#include <gst/gst.h>
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/video/video.h>
#endif

#define HW_SEND_ERRORS_FATAL 25          /* consecutive send errors before giving up */
#define GST_BACKLOG_FATAL (1 << 20)      /* bytes queued in front of the decoder */
#define BACKEND_FAILS_DISABLE 3          /* consecutive open failures ... */
#define BACKEND_DISABLE_SEC 600          /* ... take a backend out for this long */

enum kind { K_SW, K_FFNAMED, K_HWACCEL, K_GST };

struct backend {
    const char *name;
    enum kind kind;
    const char *h264, *hevc;     /* element / decoder / hwaccel name per codec */
    long max_px;                 /* default pixel limit, 0 = none */
};

static const struct backend BACKENDS[] = {
    { "gst-v4l2sl", K_GST,     "v4l2slh264dec", "v4l2slh265dec", 1920L * 1088 },
    { "gst-nv",     K_GST,     "nvv4l2decoder", "nvv4l2decoder", 0 },
    { "v4l2m2m",    K_FFNAMED, "h264_v4l2m2m",  "hevc_v4l2m2m",  1920L * 1088 },
    { "rkmpp",      K_FFNAMED, "h264_rkmpp",    "hevc_rkmpp",    0 },
    { "vaapi",      K_HWACCEL, "vaapi",         "vaapi",         0 },
    { "cuda",       K_HWACCEL, "cuda",          "cuda",          0 },
    { "software",   K_SW,      NULL,            NULL,            0 },
};
#define NBACKENDS ((int)(sizeof(BACKENDS) / sizeof(BACKENDS[0])))

static char POLICY[32] = "auto";
static long MAX_PX = 0;                  /* 0 = per-backend default */
static int MAX_SESSIONS = 0;
static int HW_SESSIONS = 0;              /* atomic */
static int GST_OK = 0;

static pthread_mutex_t STATE_LOCK = PTHREAD_MUTEX_INITIALIZER;
static int AVAIL[NBACKENDS];             /* -1 unknown, 0 no, 1 yes */
static int FAILS[NBACKENDS];
static time_t DISABLED_UNTIL[NBACKENDS];
static AVBufferRef *HWDEV[NBACKENDS];    /* shared hwaccel device per backend */

static void hlog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void hlog(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fprintf(stderr, "hwdec: ");
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

void hwdec_set_policy(const char *p) {
    if (!p || !*p) p = "auto";
    pthread_mutex_lock(&STATE_LOCK);
    snprintf(POLICY, sizeof POLICY, "%s", p);
    pthread_mutex_unlock(&STATE_LOCK);
}
const char *hwdec_policy(void) { return POLICY; }
void hwdec_set_max_pixels(long px) { MAX_PX = px > 0 ? px : 0; }
void hwdec_set_max_sessions(int n) { MAX_SESSIONS = n > 0 ? n : 0; }

void hwdec_init(void) {
    for (int i = 0; i < NBACKENDS; i++) AVAIL[i] = -1;
#ifdef HAVE_GST
    GError *err = NULL;
    GST_OK = gst_init_check(NULL, NULL, &err) ? 1 : 0;
    if (!GST_OK) {
        hlog("GStreamer unavailable: %s", err ? err->message : "?");
        if (err) g_error_free(err);
    }
#endif
}

/* ------------------------------------------------------------ the decoder */

struct vdec {
    const struct backend *b;
    int idx;
    int hw;
    int send_errors;
    /* FFmpeg backends */
    AVCodecContext *ctx;
    AVFrame *frame, *swframe;
    enum AVPixelFormat hw_pix_fmt;
#ifdef HAVE_GST
    GstElement *pipe, *src, *sink;
    GstBus *bus;
    GstSample *sample;
    GstVideoFrame vf;
    int mapped;
    uint8_t *detile;
    size_t detile_size;
    const uint8_t *prefix;       /* annex-b extradata sent ahead of packet 1 */
    int prefix_len;
#endif
};

static const char *codec_arg(const struct backend *b, enum AVCodecID id) {
    if (id == AV_CODEC_ID_H264) return b->h264;
    if (id == AV_CODEC_ID_HEVC) return b->hevc;
    return NULL;
}

/* Is backend i installed on this machine at all? Probed once. */
static int backend_available(int i) {
    const struct backend *b = &BACKENDS[i];
    pthread_mutex_lock(&STATE_LOCK);
    if (AVAIL[i] >= 0) {
        pthread_mutex_unlock(&STATE_LOCK);
        return AVAIL[i];
    }
    int ok = 0;
    switch (b->kind) {
    case K_SW:
        ok = 1;
        break;
    case K_FFNAMED:
        ok = avcodec_find_decoder_by_name(b->h264) != NULL;
        break;
    case K_HWACCEL: {
        enum AVHWDeviceType t = av_hwdevice_find_type_by_name(b->h264);
        ok = t != AV_HWDEVICE_TYPE_NONE &&
             av_hwdevice_ctx_create(&HWDEV[i], t, NULL, NULL, 0) == 0;
        break;
    }
    case K_GST:
#ifdef HAVE_GST
        if (GST_OK) {
            GstElementFactory *f = gst_element_factory_find(b->h264);
            ok = f != NULL;
            if (f) gst_object_unref(f);
        }
#endif
        break;
    }
    AVAIL[i] = ok;
    pthread_mutex_unlock(&STATE_LOCK);
    if (b->kind != K_SW) hlog("backend %s %s", b->name, ok ? "available" : "not available");
    return ok;
}

static void note_open_result(int i, int ok) {
    pthread_mutex_lock(&STATE_LOCK);
    if (ok) {
        FAILS[i] = 0;
    } else if (++FAILS[i] >= BACKEND_FAILS_DISABLE) {
        DISABLED_UNTIL[i] = time(NULL) + BACKEND_DISABLE_SEC;
        FAILS[i] = 0;
        hlog("backend %s failed %d times; disabled for %ds",
             BACKENDS[i].name, BACKEND_FAILS_DISABLE, BACKEND_DISABLE_SEC);
    }
    pthread_mutex_unlock(&STATE_LOCK);
}

/* ---- FFmpeg (software, named hardware decoders, hwaccels) ---- */

static enum AVPixelFormat pick_hw_fmt(AVCodecContext *ctx, const enum AVPixelFormat *fmts) {
    struct vdec *d = ctx->opaque;
    for (const enum AVPixelFormat *p = fmts; *p != AV_PIX_FMT_NONE; p++)
        if (*p == d->hw_pix_fmt) return *p;
    return AV_PIX_FMT_NONE;      /* no hardware format offered: fail, fall back */
}

static int open_ffmpeg(struct vdec *d, const AVCodecParameters *par, int threads) {
    const struct backend *b = d->b;
    const AVCodec *codec = b->kind == K_FFNAMED
        ? avcodec_find_decoder_by_name(codec_arg(b, par->codec_id))
        : avcodec_find_decoder(par->codec_id);
    if (!codec) return -1;
    d->ctx = avcodec_alloc_context3(codec);
    if (!d->ctx || avcodec_parameters_to_context(d->ctx, par) < 0) return -1;
    d->ctx->opaque = d;
    if (b->kind == K_SW) {
        /* Slice threads only: frame threading pipelines whole frames and
         * turns live video into a growing backlog (see dvrwall.c). */
        d->ctx->thread_count = threads;
        d->ctx->thread_type = FF_THREAD_SLICE;
    }
    d->ctx->flags |= AV_CODEC_FLAG_LOW_DELAY;
    if (b->kind == K_HWACCEL) {
        enum AVHWDeviceType t = av_hwdevice_find_type_by_name(b->h264);
        d->hw_pix_fmt = AV_PIX_FMT_NONE;
        for (int i = 0;; i++) {
            const AVCodecHWConfig *c = avcodec_get_hw_config(codec, i);
            if (!c) break;
            if ((c->methods & AV_CODEC_HW_CONFIG_METHOD_HW_DEVICE_CTX) && c->device_type == t) {
                d->hw_pix_fmt = c->pix_fmt;
                break;
            }
        }
        if (d->hw_pix_fmt == AV_PIX_FMT_NONE || !HWDEV[d->idx]) return -1;
        d->ctx->hw_device_ctx = av_buffer_ref(HWDEV[d->idx]);
        d->ctx->get_format = pick_hw_fmt;
    }
    if (avcodec_open2(d->ctx, codec, NULL) < 0) return -1;
    d->frame = av_frame_alloc();
    d->swframe = av_frame_alloc();
    return d->frame && d->swframe ? 0 : -1;
}

static int send_ffmpeg(struct vdec *d, const AVPacket *pkt) {
    int r = avcodec_send_packet(d->ctx, pkt);
    if (r >= 0 || r == AVERROR(EAGAIN)) {
        d->send_errors = 0;
        return 0;
    }
    /* A corrupt packet is normal on a live stream; software just skips it.
     * A hardware decoder that keeps rejecting packets is broken. */
    if (d->hw && ++d->send_errors >= HW_SEND_ERRORS_FATAL) return -1;
    return 0;
}

static int receive_ffmpeg(struct vdec *d, struct vframe *out) {
    av_frame_unref(d->frame);
    av_frame_unref(d->swframe);
    int r = avcodec_receive_frame(d->ctx, d->frame);
    if (r == AVERROR(EAGAIN) || r == AVERROR_EOF) return 0;
    if (r < 0) return d->hw ? -1 : 0;
    AVFrame *f = d->frame;
    if (f->hw_frames_ctx) {
        if (av_hwframe_transfer_data(d->swframe, f, 0) < 0) return -1;
        f = d->swframe;
    }
    out->w = f->width;
    out->h = f->height;
    out->fmt = f->format;
    for (int i = 0; i < 4; i++) {
        out->data[i] = f->data[i];
        out->linesize[i] = f->linesize[i];
    }
    return 1;
}

/* ---- GStreamer (stateless V4L2, Jetson) ---- */

#ifdef HAVE_GST
static int open_gst(struct vdec *d, const AVCodecParameters *par) {
    const struct backend *b = d->b;
    int hevc = par->codec_id == AV_CODEC_ID_HEVC;
    const char *dec = codec_arg(b, par->codec_id);
    const char *post = strcmp(b->name, "gst-nv") == 0
        ? "nvvidconv ! video/x-raw,format=NV12 ! " : "";
    char desc[512];
    snprintf(desc, sizeof desc,
             "appsrc name=src is-live=true format=time do-timestamp=true ! "
             "%s ! %s ! %s"
             "appsink name=sink sync=false max-buffers=2 drop=true",
             hevc ? "h265parse" : "h264parse", dec, post);
    GError *err = NULL;
    d->pipe = gst_parse_launch(desc, &err);
    if (!d->pipe) {
        hlog("%s: %s", b->name, err ? err->message : "pipeline failed");
        if (err) g_error_free(err);
        return -1;
    }
    if (err) g_error_free(err);
    d->src = gst_bin_get_by_name(GST_BIN(d->pipe), "src");
    d->sink = gst_bin_get_by_name(GST_BIN(d->pipe), "sink");
    d->bus = gst_element_get_bus(d->pipe);

    /* avcC/hvcC extradata (first byte 1) goes in caps; Annex-B extradata is
     * sent in front of the first packet so the parser sees SPS/PPS early. */
    GstCaps *caps;
    const uint8_t *x = par->extradata;
    if (x && par->extradata_size > 0 && x[0] == 1) {
        GstBuffer *cd = gst_buffer_new_memdup(x, par->extradata_size);
        caps = gst_caps_new_simple(hevc ? "video/x-h265" : "video/x-h264",
                                   "stream-format", G_TYPE_STRING, hevc ? "hvc1" : "avc",
                                   "alignment", G_TYPE_STRING, "au",
                                   "codec_data", GST_TYPE_BUFFER, cd, NULL);
        gst_buffer_unref(cd);
    } else {
        caps = gst_caps_new_simple(hevc ? "video/x-h265" : "video/x-h264",
                                   "stream-format", G_TYPE_STRING, "byte-stream",
                                   "alignment", G_TYPE_STRING, "au", NULL);
        if (x && par->extradata_size > 0) {
            d->prefix = x;
            d->prefix_len = par->extradata_size;
        }
    }
    gst_app_src_set_caps(GST_APP_SRC(d->src), caps);
    gst_caps_unref(caps);

    if (gst_element_set_state(d->pipe, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE)
        return -1;
    return 0;
}

static int send_gst(struct vdec *d, const AVPacket *pkt) {
    /* Nothing is ever dropped in front of the decoder (every H.264 frame is
     * a reference). If input piles up, this decoder can't keep pace with
     * the stream, so give the stream to another decoder instead. */
    if (gst_app_src_get_current_level_bytes(GST_APP_SRC(d->src)) > GST_BACKLOG_FATAL) {
        hlog("%s: falling behind the stream", d->b->name);
        return -1;
    }
    GstBuffer *buf;
    if (d->prefix) {
        buf = gst_buffer_new_allocate(NULL, d->prefix_len + pkt->size, NULL);
        gst_buffer_fill(buf, 0, d->prefix, d->prefix_len);
        gst_buffer_fill(buf, d->prefix_len, pkt->data, pkt->size);
        d->prefix = NULL;
    } else {
        buf = gst_buffer_new_memdup(pkt->data, pkt->size);
    }
    return gst_app_src_push_buffer(GST_APP_SRC(d->src), buf) == GST_FLOW_OK ? 0 : -1;
}

/* NV12_32L32 (Allwinner cedrus): each plane is 32x32-byte tiles stored one
 * after another, tile rows left to right. Copy it out to plain rows. */
static void detile_32l32(uint8_t *dst, int dst_stride, const uint8_t *src,
                         int x_tiles, int width, int rows) {
    for (int y = 0; y < rows; y++) {
        const uint8_t *row = src + (size_t)(y >> 5) * x_tiles * 1024 + (y & 31) * 32;
        uint8_t *o = dst + (size_t)y * dst_stride;
        for (int x = 0; x < width; x += 32)
            memcpy(o + x, row + (size_t)(x >> 5) * 1024, width - x < 32 ? width - x : 32);
    }
}

static void release_gst_frame(struct vdec *d) {
    if (d->mapped) gst_video_frame_unmap(&d->vf);
    d->mapped = 0;
    if (d->sample) gst_sample_unref(d->sample);
    d->sample = NULL;
}

static int receive_gst(struct vdec *d, struct vframe *out) {
    GstMessage *m = gst_bus_pop_filtered(d->bus, GST_MESSAGE_ERROR | GST_MESSAGE_EOS);
    if (m) {
        if (GST_MESSAGE_TYPE(m) == GST_MESSAGE_ERROR) {
            GError *e = NULL;
            gst_message_parse_error(m, &e, NULL);
            hlog("%s: %s", d->b->name, e ? e->message : "error");
            if (e) g_error_free(e);
        }
        gst_message_unref(m);
        return -1;
    }
    release_gst_frame(d);
    d->sample = gst_app_sink_try_pull_sample(GST_APP_SINK(d->sink), 0);
    if (!d->sample) return 0;
    GstVideoInfo info;
    GstCaps *caps = gst_sample_get_caps(d->sample);
    GstBuffer *buf = gst_sample_get_buffer(d->sample);
    if (!caps || !buf || !gst_video_info_from_caps(&info, caps)) return -1;
    if (!gst_video_frame_map(&d->vf, &info, buf, GST_MAP_READ)) return -1;
    d->mapped = 1;

    int w = GST_VIDEO_INFO_WIDTH(&info), h = GST_VIDEO_INFO_HEIGHT(&info);
    out->w = w;
    out->h = h;
    memset(out->data, 0, sizeof out->data);
    memset(out->linesize, 0, sizeof out->linesize);
    switch (GST_VIDEO_INFO_FORMAT(&info)) {
    case GST_VIDEO_FORMAT_NV12:
        out->fmt = AV_PIX_FMT_NV12;
        for (int p = 0; p < 2; p++) {
            out->data[p] = GST_VIDEO_FRAME_PLANE_DATA(&d->vf, p);
            out->linesize[p] = GST_VIDEO_FRAME_PLANE_STRIDE(&d->vf, p);
        }
        return 1;
    case GST_VIDEO_FORMAT_I420:
        out->fmt = AV_PIX_FMT_YUV420P;
        for (int p = 0; p < 3; p++) {
            out->data[p] = GST_VIDEO_FRAME_PLANE_DATA(&d->vf, p);
            out->linesize[p] = GST_VIDEO_FRAME_PLANE_STRIDE(&d->vf, p);
        }
        return 1;
    case GST_VIDEO_FORMAT_NV12_32L32: {
        int stride = (w + 31) & ~31;
        size_t need = (size_t)stride * h * 3 / 2;
        if (need > d->detile_size) {
            free(d->detile);
            d->detile = malloc(need);
            d->detile_size = d->detile ? need : 0;
            if (!d->detile) return -1;
        }
        uint8_t *y = d->detile, *uv = d->detile + (size_t)stride * h;
        detile_32l32(y, stride, GST_VIDEO_FRAME_PLANE_DATA(&d->vf, 0),
                     GST_VIDEO_TILE_X_TILES(GST_VIDEO_FRAME_PLANE_STRIDE(&d->vf, 0)), w, h);
        detile_32l32(uv, stride, GST_VIDEO_FRAME_PLANE_DATA(&d->vf, 1),
                     GST_VIDEO_TILE_X_TILES(GST_VIDEO_FRAME_PLANE_STRIDE(&d->vf, 1)), w, h / 2);
        out->fmt = AV_PIX_FMT_NV12;
        out->data[0] = y;
        out->data[1] = uv;
        out->linesize[0] = out->linesize[1] = stride;
        return 1;
    }
    default:
        hlog("%s: unsupported output format %s", d->b->name,
             gst_video_format_to_string(GST_VIDEO_INFO_FORMAT(&info)));
        return -1;
    }
}

static void close_gst(struct vdec *d) {
    release_gst_frame(d);
    if (d->pipe) gst_element_set_state(d->pipe, GST_STATE_NULL);
    if (d->bus) gst_object_unref(d->bus);
    if (d->src) gst_object_unref(d->src);
    if (d->sink) gst_object_unref(d->sink);
    if (d->pipe) gst_object_unref(d->pipe);
    free(d->detile);
}
#endif

void hwdec_report(void) {
    printf("decoders (auto order):");
    for (int i = 0; i < NBACKENDS; i++)
        printf(" %s=%s", BACKENDS[i].name, backend_available(i) ? "yes" : "no");
    printf("\n");
}

/* ------------------------------------------------------------- public API */

static void close_backend(struct vdec *d) {
#ifdef HAVE_GST
    if (d->b->kind == K_GST) close_gst(d);
#endif
    if (d->frame) av_frame_free(&d->frame);
    if (d->swframe) av_frame_free(&d->swframe);
    if (d->ctx) avcodec_free_context(&d->ctx);
}

static struct vdec *try_backend(int i, const AVCodecParameters *par, int threads) {
    struct vdec *d = calloc(1, sizeof *d);
    if (!d) return NULL;
    d->b = &BACKENDS[i];
    d->idx = i;
    d->hw = d->b->kind != K_SW;
    int r;
#ifdef HAVE_GST
    if (d->b->kind == K_GST) r = open_gst(d, par);
    else
#endif
    r = open_ffmpeg(d, par, threads);
    if (r < 0) {
        close_backend(d);
        free(d);
        return NULL;
    }
    return d;
}

struct vdec *vdec_open(const AVCodecParameters *par, int threads, int allow_hw) {
    char policy[32];
    pthread_mutex_lock(&STATE_LOCK);
    snprintf(policy, sizeof policy, "%s", POLICY);
    pthread_mutex_unlock(&STATE_LOCK);
    int any = strcmp(policy, "auto") == 0;
    long px = (long)par->width * par->height;

    for (int i = 0; i < NBACKENDS; i++) {
        const struct backend *b = &BACKENDS[i];
        if (b->kind == K_SW) break;
        if (!allow_hw || (!any && strcmp(policy, b->name) != 0)) continue;
        if (!codec_arg(b, par->codec_id) || !backend_available(i)) continue;
        long limit = MAX_PX ? MAX_PX : b->max_px;
        if (limit && px > limit) continue;               /* too big for it: don't even try */
        if (DISABLED_UNTIL[i] && time(NULL) < DISABLED_UNTIL[i]) continue;
        if (MAX_SESSIONS && __atomic_load_n(&HW_SESSIONS, __ATOMIC_RELAXED) >= MAX_SESSIONS) break;
        struct vdec *d = try_backend(i, par, threads);
        note_open_result(i, d != NULL);
        if (d) {
            __atomic_add_fetch(&HW_SESSIONS, 1, __ATOMIC_RELAXED);
            return d;
        }
        hlog("%s could not open a %dx%d stream", b->name, par->width, par->height);
    }
    return try_backend(NBACKENDS - 1, par, threads);
}

int vdec_send(struct vdec *d, const AVPacket *pkt) {
#ifdef HAVE_GST
    if (d->b->kind == K_GST) return send_gst(d, pkt);
#endif
    return send_ffmpeg(d, pkt);
}

int vdec_receive(struct vdec *d, struct vframe *out) {
#ifdef HAVE_GST
    if (d->b->kind == K_GST) return receive_gst(d, out);
#endif
    return receive_ffmpeg(d, out);
}

void vdec_flush(struct vdec *d) {
#ifdef HAVE_GST
    if (d->b->kind == K_GST) {
        /* The parser and decoder resync on the next keyframe by themselves. */
        return;
    }
#endif
    avcodec_flush_buffers(d->ctx);
}

int vdec_is_hw(const struct vdec *d) { return d->hw; }
const char *vdec_name(const struct vdec *d) { return d->b->name; }

void vdec_close(struct vdec **pd) {
    struct vdec *d = *pd;
    if (!d) return;
    if (d->hw) __atomic_sub_fetch(&HW_SESSIONS, 1, __ATOMIC_RELAXED);
    close_backend(d);
    free(d);
    *pd = NULL;
}
