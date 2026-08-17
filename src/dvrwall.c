/*
 * dvrwall -- video wall compositor + live-thumbnail server for the DVR kiosk.
 *
 * Replaces the previous "one ffmpeg with N sequential -i inputs into xstack"
 * approach, which had two fatal properties on this hardware:
 *
 *   1. ffmpeg opens -i inputs strictly sequentially (~8s each, mostly waiting
 *      for an H.264 keyframe). With 16 tiles that is ~130s before the wall is
 *      complete, and streams opened first sit unread while their socket
 *      buffers fill -- so the first DVR ends up tens of seconds behind.
 *   2. xstack's framesync buffers rather than drops, so that backlog never
 *      clears; it only drains at whatever surplus speed the box has spare.
 *
 * This program fixes both by construction:
 *   - every stream connects concurrently, so startup is one connect, not N
 *   - each decoder keeps only its newest frame (latest-frame-wins); frames
 *     that arrive faster than they are displayed are decoded and dropped, so
 *     the socket never backs up and latency stays bounded to one frame period
 *   - a dead stream reconnects itself without disturbing the other tiles
 *
 * The decode roster is separate from what's on screen: CHANNELS connects
 * every configured DVR channel (measured to cost ~5% CPU for 16 of them, so
 * decoding the full ~28-channel fleet fits comfortably), while LAYOUT and
 * FULLSCREEN just pick which already-decoded streams get composited to
 * /dev/fb0 and where. The same always-decoding roster also backs a small
 * HTTP server (127.0.0.1:8590) that serves live JPEG/MJPEG for any channel,
 * including ones not currently on the TV -- the dashboard's channel pool can
 * show real live video instead of a periodic snapshot, at no extra decode or
 * connection cost, because the frame is already sitting in memory.
 *
 * Decoding is libavcodec (already NEON-optimised); this program exists for
 * control over concurrency and frame dropping, not for raw decode speed.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/imgutils.h>
#include <libswscale/swscale.h>

#define MAX_STREAMS 40
#define URL_MAX 512
#define NAME_MAX_LEN 96
#define SOCK_PATH "/run/dvrwall.sock"
#define FB_DEV "/dev/fb0"
#define DEFAULT_FPS 12
#define THUMB_W 320
#define THUMB_H 180
#define HTTP_PORT 8590
#define MJPEG_FPS_THUMB 2   /* grid/pool thumbnails -- 320x180, cheap to re-encode */
#define MJPEG_FPS_MAIN 2    /* fullscreen HD WebUI preview -- capped to 2fps to eliminate CPU spikes */
#define DEMAND_WINDOW_MS 3000   /* stop encoding a channel this long after its last request */

/* ---------------------------------------------------------------- logging */

static void logmsg(const char *fmt, ...) {
    char ts[32];
    time_t t = time(NULL);
    struct tm tm;
    localtime_r(&t, &tm);
    strftime(ts, sizeof ts, "%H:%M:%S", &tm);
    fprintf(stderr, "%s ", ts);
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    fflush(stderr);
}

static int64_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static void url_basename(const char *url, char *out, size_t outlen) {
    const char *slash = strrchr(url, '/');
    snprintf(out, outlen, "%s", slash ? slash + 1 : url);
}

/* --------------------------------------------------------------- fb target */

struct fbdev {
    int fd;
    uint8_t *mem;
    size_t len;
    int w, h, bpp, stride;
};

static struct fbdev FB;

static int fb_open(void) {
    struct fb_var_screeninfo var;
    struct fb_fix_screeninfo fix;
    FB.fd = open(FB_DEV, O_RDWR);
    if (FB.fd < 0) { logmsg("fb: open %s: %s", FB_DEV, strerror(errno)); return -1; }
    if (ioctl(FB.fd, FBIOGET_VSCREENINFO, &var) < 0 ||
        ioctl(FB.fd, FBIOGET_FSCREENINFO, &fix) < 0) {
        logmsg("fb: ioctl: %s", strerror(errno));
        return -1;
    }
    FB.w = var.xres;
    FB.h = var.yres;
    FB.bpp = var.bits_per_pixel;
    FB.stride = fix.line_length;
    FB.len = (size_t)FB.stride * FB.h;
    FB.mem = mmap(NULL, FB.len, PROT_READ | PROT_WRITE, MAP_SHARED, FB.fd, 0);
    if (FB.mem == MAP_FAILED) { logmsg("fb: mmap: %s", strerror(errno)); return -1; }
    logmsg("fb: %dx%d %dbpp stride=%d", FB.w, FB.h, FB.bpp, FB.stride);
    if (FB.bpp != 32) logmsg("fb: WARNING expected 32bpp");
    return 0;
}

/* Toggle display power so the TV can drop to standby on the off-schedule. */
static void fb_blank(int on) {
    int fd = open("/sys/class/graphics/fb0/blank", O_WRONLY);
    if (fd < 0) return;
    const char *v = on ? "4\n" : "0\n";  /* FB_BLANK_POWERDOWN : UNBLANK */
    ssize_t n = write(fd, v, strlen(v));
    (void)n;
    close(fd);
}

/* ------------------------------------------------------------------ roster
 *
 * The roster is the set of channels dvrwall keeps decoded at all times
 * (regardless of what's on screen). Each entry decodes to a fixed THUMB_W x
 * THUMB_H BGRA slot -- fixed so the decode/scale side never has to react to
 * layout changes, and so any roster stream can be JPEG-encoded on demand for
 * the HTTP thumbnail server without touching the compositor at all.
 */

struct stream {
    int used;
    uint64_t id;                /* monotonic, never reused for the life of the
                                  * process. Identifies *this* occupant of the
                                  * slot so a compose_slot built before a
                                  * teardown/reuse can never be blitted against
                                  * whatever channel got the index next --
                                  * see roster_set() and compositor_thread(). */
    char url[URL_MAX];
    char name[NAME_MAX_LEN];   /* basename of url, e.g. "dvr1_ch1" */

    pthread_mutex_t lock;
    uint8_t *slot;              /* front buffer, BGRA, slot_w*slot_h*4 -- what
                                  * the compositor and thumbnail encoder read */
    uint8_t *slot_back;         /* back buffer -- sws_scale's target; written
                                  * with no lock held, then swapped with `slot`
                                  * under the lock. Removes a full-frame
                                  * memcpy-under-lock every decoded frame. */
    int slot_w, slot_h;        /* THUMB_W/THUMB_H for grid substreams; sized to
                                 * fill the framebuffer for "_main" streams, so
                                 * fullscreen mainstream isn't decoded at full
                                 * resolution only to be squashed into a 320x180
                                 * thumbnail and blown back up on the TV. */
    int have_frame;
    int64_t frame_ms;          /* when the newest frame landed */
    int64_t frames;            /* total decoded+scaled, for measuring real fps */
    uint64_t seq;               /* bumped every time a new frame is published
                                  * (front/back swap). Lets the compositor blit
                                  * only on genuinely new frames instead of on
                                  * a fixed timer independent of the decoder's
                                  * -- see compositor_thread(). */
    uint64_t consumed_seq;      /* seq of the newest frame the compositor has
                                  * actually displayed. Mainstream-only
                                  * backpressure signal: if stream_session()
                                  * sees its last publish still hasn't been
                                  * consumed, the decoder is outrunning the
                                  * display and it skips the scale for the next
                                  * one rather than doing wasted work. */

    /* JPEG cache: encoded once per THUMB_FPS tick by thumb_encoder_thread,
     * regardless of how many HTTP viewers are watching this channel. Without
     * this, N simultaneous <img> tags on the same channel would each run
     * their own decode-to-JPEG pipeline (measured: ~30% CPU per viewer),
     * which does not scale to a dashboard with the grid and pool both open
     * (up to ~44 tiles, many showing the same channel twice). */
    uint8_t *jpeg;
    int jpeg_len;
    int64_t jpeg_ms;
    volatile int64_t requested_ms;   /* last HTTP request for this channel;
                                       * thumb_encoder_thread only bothers
                                       * encoding channels requested recently */

    pthread_t tid;
    int running;
    int connected;             /* purely for STATUS reporting */
};

static struct stream STREAMS[MAX_STREAMS];
static int NSTREAMS = 0;                 /* count of used roster slots */
static pthread_mutex_t ROSTER_LOCK = PTHREAD_MUTEX_INITIALIZER;
static volatile int64_t ROSTER_GEN = 0;  /* bumped on every roster_set/stop_all */
static uint64_t NEXT_STREAM_ID = 1;      /* guarded by ROSTER_LOCK */

/* What's actually on screen: indices into STREAMS plus tile geometry. */
struct compose_slot {
    int stream_idx;
    uint64_t stream_id;    /* must match STREAMS[stream_idx].id or this entry
                             * is stale (its slot was reused) and is skipped */
    uint64_t last_seq;     /* seq last blitted -- drives event-driven compositing */
    int x, y, w, h;
};
static struct compose_slot COMPOSE[MAX_STREAMS];
static int NCOMPOSE = 0;
static pthread_mutex_t COMPOSE_LOCK = PTHREAD_MUTEX_INITIALIZER;

static volatile int RUN = 1;
static volatile int BLANKED = 0;
static volatile int FB_DIRTY = 1;   /* clear the fb on next composite pass */
static int TARGET_FPS = DEFAULT_FPS;

/* One decode session. Returns when the stream dies so the caller can retry. */
static void stream_session(struct stream *s) {
    AVFormatContext *fmt = NULL;
    AVCodecContext *dec = NULL;
    struct SwsContext *sws = NULL;
    AVFrame *frame = NULL;
    AVPacket *pkt = NULL;
    int vstream = -1;

    int is_main = s->slot_w > THUMB_W;

    AVDictionary *opts = NULL;
    av_dict_set(&opts, "rtsp_transport", "tcp", 0);
    av_dict_set(&opts, "fflags", "nobuffer", 0);
    av_dict_set(&opts, "flags", "low_delay", 0);
    av_dict_set(&opts, "analyzeduration", "0", 0);
    /* A 1080p mainstream I-frame can be 100-250KB; the 32KB probesize used
     * for tiny grid substreams truncates it. */
    av_dict_set(&opts, "probesize", is_main ? "262144" : "32768", 0);
    av_dict_set(&opts, "stimeout", "10000000", 0);   /* 10s socket timeout */
    av_dict_set(&opts, "max_delay", "0", 0);

    if (avformat_open_input(&fmt, s->url, NULL, &opts) < 0) goto done;
    if (avformat_find_stream_info(fmt, NULL) < 0) goto done;

    for (unsigned i = 0; i < fmt->nb_streams; i++) {
        if (fmt->streams[i]->codecpar->codec_type == AVMEDIA_TYPE_VIDEO) { vstream = i; break; }
    }
    if (vstream < 0) goto done;

    AVCodecParameters *par = fmt->streams[vstream]->codecpar;
    const AVCodec *codec = avcodec_find_decoder(par->codec_id);
    if (!codec) goto done;
    dec = avcodec_alloc_context3(codec);
    if (!dec || avcodec_parameters_to_context(dec, par) < 0) goto done;
    /* Grid substreams are tiny (CIF-ish) -- one thread is plenty and extra
     * threads just add overhead. A "_main" fullscreen stream is full
     * resolution and is the only thing being decoded while fullscreen is
     * active (see dvr_control.py's launch_fullscreen), so it can use extra
     * decode threads -- but ONLY slice-threading (FF_THREAD_SLICE), never
     * frame-threading. FFmpeg's default with thread_count>1 is to also
     * allow frame-threading, which decodes N whole frames in a pipeline and
     * only emits the first one after all N are done -- exactly backwards
     * for live video, and most consumer DVRs encode single-slice frames
     * anyway (so slice-threading buys nothing here and just adds thread
     * overhead). This was silently turning fullscreen into a growing,
     * multi-second backlog under load, observed live via STATUS: frame age
     * climbing from tens of ms to 9+ seconds before snapping back once the
     * backlog drained. */
    dec->thread_count = (s->slot_w > THUMB_W) ? 2 : 1;
    dec->thread_type = FF_THREAD_SLICE;
    dec->flags |= AV_CODEC_FLAG_LOW_DELAY;
    if (avcodec_open2(dec, codec, NULL) < 0) goto done;

    frame = av_frame_alloc();
    pkt = av_packet_alloc();
    if (!frame || !pkt) goto done;

    int sw = s->slot_w, sh = s->slot_h;
    int stride = sw * 4;

    s->connected = 1;
    logmsg("stream[%s]: connected", s->name);

    while (RUN && s->running) {
        int r = av_read_frame(fmt, pkt);
        if (r < 0) break;
        if (pkt->stream_index != vstream) { av_packet_unref(pkt); continue; }

        r = avcodec_send_packet(dec, pkt);
        av_packet_unref(pkt);
        if (r < 0 && r != AVERROR(EAGAIN)) continue;

        while (avcodec_receive_frame(dec, frame) == 0) {
            if (!sws) {
                sws = sws_getContext(dec->width, dec->height, dec->pix_fmt,
                                     sw, sh, AV_PIX_FMT_BGRA,
                                     SWS_FAST_BILINEAR, NULL, NULL, NULL);
                if (!sws) { av_frame_unref(frame); goto done; }
            }

            /* Mainstream-only load shedding: the DVR's mainstream runs at its
             * own native fps (often higher than anything the compositor
             * needs -- e.g. 25fps for a stream only ever displayed at up to
             * COMPOSITOR_HZ), so the decoder can outrun the display under
             * load. If the frame we published last time hasn't been shown
             * yet, this one would only ever overwrite it unseen -- skip the
             * expensive scale-and-publish and keep decoding (decode itself
             * can't be skipped; Baseline profile has no B-frames, so every
             * frame is a reference frame -- see stream_session's caller
             * comment history / plan notes). Grid substreams don't need
             * this: they're cheap and, per ensure_roster() in
             * dvr_control.py, are only ever roster'd while on screen. */
            if (is_main) {
                pthread_mutex_lock(&s->lock);
                int behind = s->have_frame && s->seq != s->consumed_seq;
                pthread_mutex_unlock(&s->lock);
                if (behind) { av_frame_unref(frame); continue; }
            }

            pthread_mutex_lock(&s->lock);
            uint8_t *back = s->slot_back;
            pthread_mutex_unlock(&s->lock);

            uint8_t *dst_data[4]     = { back, NULL, NULL, NULL };
            int      dst_linesize[4] = { stride, 0, 0, 0 };
            sws_scale(sws, (const uint8_t *const *)frame->data, frame->linesize,
                      0, dec->height, dst_data, dst_linesize);

            /* Publish by swapping front/back -- no frame data is copied
             * here, sws_scale already wrote straight into `back`. This
             * replaces the old row-by-row memcpy-under-lock (~55MB/s across
             * the roster) with a pointer swap. */
            pthread_mutex_lock(&s->lock);
            s->slot_back = s->slot;
            s->slot = back;
            s->have_frame = 1;
            s->frame_ms = now_ms();
            s->frames++;
            s->seq++;
            pthread_mutex_unlock(&s->lock);
            av_frame_unref(frame);
        }
    }

done:
    s->connected = 0;
    if (sws) sws_freeContext(sws);
    if (pkt) av_packet_free(&pkt);
    if (frame) av_frame_free(&frame);
    if (dec) avcodec_free_context(&dec);
    if (fmt) avformat_close_input(&fmt);
    av_dict_free(&opts);
}

/* Per-stream supervisor: reconnects only this stream, never the whole wall. */
static void *stream_thread(void *arg) {
    struct stream *s = arg;
    int backoff = 1;
    while (RUN && s->running) {
        stream_session(s);
        if (!RUN || !s->running) break;
        logmsg("stream[%s]: dropped, retry in %ds", s->name, backoff);
        for (int i = 0; i < backoff * 10 && RUN && s->running; i++)
            usleep(100000);
        backoff = backoff < 15 ? backoff * 2 : 15;
    }
    return NULL;
}

static void compose_clear_locked(void) {
    NCOMPOSE = 0;
    FB_DIRTY = 1;
}

static void roster_stop_all(void) {
    /* Clear the compose list (under COMPOSE_LOCK) *before* touching any
     * stream, so no future compositor pass can reference a STREAMS[] entry
     * we're about to tear down. */
    pthread_mutex_lock(&COMPOSE_LOCK);
    compose_clear_locked();
    pthread_mutex_unlock(&COMPOSE_LOCK);

    pthread_mutex_lock(&ROSTER_LOCK);
    for (int i = 0; i < MAX_STREAMS; i++)
        if (STREAMS[i].used) STREAMS[i].running = 0;
    /* Wait for every decoder thread to actually exit before freeing
     * anything. Deliberately not under COMPOSE_LOCK -- NCOMPOSE is already 0
     * above, so the compositor has nothing left to reach into here, and this
     * join can take up to the ~10s socket timeout per stream. */
    for (int i = 0; i < MAX_STREAMS; i++)
        if (STREAMS[i].used) pthread_join(STREAMS[i].tid, NULL);

    pthread_mutex_lock(&COMPOSE_LOCK);   /* defense in depth, see roster_set() */
    for (int i = 0; i < MAX_STREAMS; i++) {
        if (STREAMS[i].used) {
            free(STREAMS[i].slot);
            free(STREAMS[i].slot_back);
            av_freep(&STREAMS[i].jpeg);
            memset(&STREAMS[i], 0, sizeof STREAMS[i]);
        }
    }
    pthread_mutex_unlock(&COMPOSE_LOCK);
    NSTREAMS = 0;
    ROSTER_GEN++;
    pthread_mutex_unlock(&ROSTER_LOCK);
}

/* Find a roster slot by name (basename), -1 if not decoding. Caller must
 * hold ROSTER_LOCK for the lifetime of any pointer use beyond the index. */
static int roster_find_locked(const char *name) {
    for (int i = 0; i < MAX_STREAMS; i++)
        if (STREAMS[i].used && strcmp(STREAMS[i].name, name) == 0) return i;
    return -1;
}

/* (Re)connect the full always-decoding channel set. Channels whose URL is
 * unchanged keep their live connection and slot -- calling this again with
 * the same roster (e.g. dvr_control.py re-asserting config) is a no-op.
 *
 * Critical invariant: a stream whose thread is still running must NEVER be
 * relocated to a different STREAMS[] index or have its struct rewritten in
 * place while alive. stream_thread() is started with a raw pointer to its
 * slot's address and never told if it moves; the previous implementation
 * copied "kept" structs into a compacted 0..n-1 layout via memset+struct-
 * copy while their threads were still executing, so a still-running thread
 * would keep locking a mutex / writing a buffer that had just been zeroed
 * or handed to a different stream underneath it -- a use-after-relocate bug
 * that crashed the whole process (SIGSEGV) periodically. Fixed by never
 * moving a used slot: kept streams stay at their existing index for their
 * entire lifetime; only genuinely-removed slots are torn down (and only
 * after their thread has actually exited via pthread_join), and only new
 * URLs are placed into free slots.
 *
 * Second invariant, added after the above fix did not fully stop the
 * crashes: freeing a torn-down slot's buffers and zeroing its struct
 * (mutex included) must never happen concurrently with compositor_thread()
 * dereferencing that same struct through a compose_slot. The two used to be
 * serialised by nothing at all -- this function only ever took ROSTER_LOCK,
 * which compositor_thread() never acquires -- so the compositor could lock
 * a just-zeroed mutex or blit from just-freed memory. Fixed by taking
 * COMPOSE_LOCK around the free+memset step specifically (not around the
 * potentially slow pthread_join -- see the phase comments below). Related:
 * compose_slot now also carries the stream's `id`, so even a slot that gets
 * torn down and reused for a *different* channel before the compositor's
 * next pass can never be blitted under a stale mapping. */
static void roster_set(char urls[][URL_MAX], int n) {
    if (n > MAX_STREAMS) n = MAX_STREAMS;
    pthread_mutex_lock(&ROSTER_LOCK);

    int url_matched[MAX_STREAMS];
    memset(url_matched, 0, sizeof url_matched);

    /* Phase 1: mark no-longer-wanted slots for shutdown; leave wanted ones
     * untouched. Nothing here is visible to the compositor yet. */
    for (int i = 0; i < MAX_STREAMS; i++) {
        if (!STREAMS[i].used) continue;
        int found = -1;
        for (int j = 0; j < n; j++)
            if (!url_matched[j] && strcmp(STREAMS[i].url, urls[j]) == 0) { found = j; break; }
        if (found >= 0) url_matched[found] = 1;
        else STREAMS[i].running = 0;
    }

    /* Phase 2: wait for those decoder threads to actually exit. Deliberately
     * NOT under COMPOSE_LOCK -- stream_session() can take up to its ~10s
     * socket timeout to unwind, and the compositor must keep blitting the
     * still-valid, still-allocated slot throughout, not stall for 10s. */
    for (int i = 0; i < MAX_STREAMS; i++)
        if (STREAMS[i].used && !STREAMS[i].running)
            pthread_join(STREAMS[i].tid, NULL);

    /* Phase 3: now that every torn-down stream's thread has genuinely
     * exited (nothing is writing its slot any more), free and zero it. This
     * is the only part that actually invalidates memory the compositor
     * might touch, so it's the only part that needs to be mutually
     * exclusive with compositor_thread()'s blit pass -- hence COMPOSE_LOCK,
     * held only for this brief step. */
    pthread_mutex_lock(&COMPOSE_LOCK);
    for (int i = 0; i < MAX_STREAMS; i++) {
        if (STREAMS[i].used && !STREAMS[i].running) {
            free(STREAMS[i].slot);
            free(STREAMS[i].slot_back);
            av_freep(&STREAMS[i].jpeg);
            memset(&STREAMS[i], 0, sizeof STREAMS[i]);
        }
    }
    pthread_mutex_unlock(&COMPOSE_LOCK);

    /* Phase 4: start any wanted URL that isn't already running, into a free
     * slot. A free slot can't be referenced by any existing compose_slot
     * (those only ever point at slots that were `used` when created, and
     * the id check guards against reuse), so no COMPOSE_LOCK needed here. */
    for (int j = 0; j < n; j++) {
        if (url_matched[j]) continue;
        int slot = -1;
        for (int i = 0; i < MAX_STREAMS; i++) {
            if (!STREAMS[i].used) { slot = i; break; }
        }
        if (slot < 0) { logmsg("roster: no free slot for %s", urls[j]); continue; }
        struct stream *s = &STREAMS[slot];
        pthread_mutex_init(&s->lock, NULL);
        snprintf(s->url, URL_MAX, "%s", urls[j]);
        url_basename(urls[j], s->name, sizeof s->name);
        size_t nlen = strlen(s->name);
        int is_main = nlen > 5 && strcmp(s->name + nlen - 5, "_main") == 0;
        s->slot_w = is_main ? FB.w : THUMB_W;
        s->slot_h = is_main ? FB.h : THUMB_H;
        size_t need = (size_t)s->slot_w * s->slot_h * 4;
        s->slot = calloc(1, need);
        s->slot_back = calloc(1, need);
        s->id = NEXT_STREAM_ID++;
        s->running = 1;
        s->used = 1;
        pthread_create(&s->tid, NULL, stream_thread, s);
    }

    int count = 0;
    for (int i = 0; i < MAX_STREAMS; i++) if (STREAMS[i].used) count++;
    NSTREAMS = count;
    ROSTER_GEN++;
    pthread_mutex_unlock(&ROSTER_LOCK);
    logmsg("roster: %d channels", n);
}

/* ---------------------------------------------------------------- layout */

/* Point the composite at n already-decoding roster streams, arranged in a
 * grid; cols = ceil(sqrt(n)) so any channel count from the dashboard's
 * drag-and-drop works, not just 16. Streams not found in the roster (should
 * not normally happen -- dvr_control.py sends the full CHANNELS roster at
 * startup) are silently skipped rather than failing the whole layout. */
static void compose_set(char urls[][URL_MAX], int n) {
    if (n > MAX_STREAMS) n = MAX_STREAMS;
    int cols = 1;
    while (cols * cols < n) cols++;
    int rows = (n + cols - 1) / cols;
    int tw = FB.w / cols;
    int th = FB.h / rows;

    pthread_mutex_lock(&ROSTER_LOCK);
    pthread_mutex_lock(&COMPOSE_LOCK);
    int m = 0;
    for (int i = 0; i < n; i++) {
        char name[NAME_MAX_LEN];
        url_basename(urls[i], name, sizeof name);
        int idx = roster_find_locked(name);
        if (idx < 0) { logmsg("layout: %s not in roster, skipped", name); continue; }
        COMPOSE[m].stream_idx = idx;
        COMPOSE[m].stream_id = STREAMS[idx].id;
        COMPOSE[m].last_seq = (uint64_t)-1;   /* force an immediate first blit */
        COMPOSE[m].x = (i % cols) * tw;
        COMPOSE[m].y = (i / cols) * th;
        COMPOSE[m].w = tw;
        COMPOSE[m].h = th;
        m++;
    }
    NCOMPOSE = m;
    FB_DIRTY = 1;
    pthread_mutex_unlock(&COMPOSE_LOCK);
    pthread_mutex_unlock(&ROSTER_LOCK);
    logmsg("layout: %d/%d tiles placed, %dx%d grid, tile %dx%d", m, n, cols, rows, tw, th);
}

/* ------------------------------------------------------------ compositor */

/* Nearest-neighbour scale-blit from a sw x sh source slot into a dw x dh
 * region of the framebuffer at (dx,dy). Falls back to a straight memcpy per
 * row when sizes match (the common grid case, and also the common
 * fullscreen-mainstream case now that mainstream slots are sized to the
 * framebuffer), which is what actually runs at 12fps for every tile, so it
 * stays on the cheap path. */
static void blit_tile(const uint8_t *slot, int sw, int sh, int dx, int dy, int dw, int dh) {
    if (dw == sw && dh == sh) {
        for (int row = 0; row < dh; row++) {
            int fy = dy + row;
            if (fy < 0 || fy >= FB.h) continue;
            memcpy(FB.mem + (size_t)fy * FB.stride + (size_t)dx * 4,
                   slot + (size_t)row * sw * 4, (size_t)dw * 4);
        }
        return;
    }
    for (int row = 0; row < dh; row++) {
        int fy = dy + row;
        if (fy < 0 || fy >= FB.h) continue;
        int sy = row * sh / dh;
        uint32_t *dst = (uint32_t *)(FB.mem + (size_t)fy * FB.stride + (size_t)dx * 4);
        const uint32_t *src = (const uint32_t *)(slot + (size_t)sy * sw * 4);
        for (int col = 0; col < dw; col++) {
            int sx = col * sw / dw;
            dst[col] = src[sx];
        }
    }
}

/* Runs well above any decoder's fps (grid substreams ~12fps, mainstream up
 * to whatever the DVR sends) purely so a new frame is picked up promptly;
 * the seq check below means a tile is only actually re-blitted when its
 * stream really published something new, so the extra ticks cost almost
 * nothing (an integer compare) rather than N redundant full blits/sec. This
 * decouples display cadence from the compositor's own timer -- see the seq
 * field comment on struct stream for why that matters. */
#define COMPOSITOR_HZ 30

static void *compositor_thread(void *arg) {
    (void)arg;
    struct timespec next;
    clock_gettime(CLOCK_MONOTONIC, &next);

    while (RUN) {
        long period_ns = 1000000000L / COMPOSITOR_HZ;
        next.tv_nsec += period_ns;
        while (next.tv_nsec >= 1000000000L) { next.tv_nsec -= 1000000000L; next.tv_sec++; }
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);

        if (BLANKED) continue;

        pthread_mutex_lock(&COMPOSE_LOCK);
        if (FB_DIRTY) { memset(FB.mem, 0, FB.len); FB_DIRTY = 0; }

        for (int i = 0; i < NCOMPOSE; i++) {
            struct compose_slot *c = &COMPOSE[i];
            struct stream *s = &STREAMS[c->stream_idx];
            /* id check: guards against a torn-down-and-reused slot index
             * being blitted under a stale mapping -- see roster_set(). */
            if (!s->used || s->id != c->stream_id) continue;
            pthread_mutex_lock(&s->lock);
            if (s->have_frame && s->slot && s->seq != c->last_seq) {
                blit_tile(s->slot, s->slot_w, s->slot_h, c->x, c->y, c->w, c->h);
                c->last_seq = s->seq;
            }
            /* Mark the newest published frame as displayed regardless of
             * whether this tick needed a fresh blit -- this is the
             * backpressure signal stream_session() reads for mainstream
             * load-shedding (see struct stream's consumed_seq comment). */
            s->consumed_seq = s->seq;
            pthread_mutex_unlock(&s->lock);
        }
        pthread_mutex_unlock(&COMPOSE_LOCK);
    }
    return NULL;
}

/* --------------------------------------------------------- control socket */

static void handle_cmd(int fd, char *line) {
    char *cmd = strtok(line, " \t\r\n");
    if (!cmd) return;

    if (strcmp(cmd, "CHANNELS") == 0) {
        static char urls[MAX_STREAMS][URL_MAX];
        int n = 0;
        char *tok;
        while ((tok = strtok(NULL, " \t\r\n")) && n < MAX_STREAMS)
            snprintf(urls[n++], URL_MAX, "%s", tok);
        roster_set(urls, n);
        dprintf(fd, "OK %d\n", n);

    } else if (strcmp(cmd, "LAYOUT") == 0) {
        static char urls[MAX_STREAMS][URL_MAX];
        int n = 0;
        char *tok;
        while ((tok = strtok(NULL, " \t\r\n")) && n < MAX_STREAMS)
            snprintf(urls[n++], URL_MAX, "%s", tok);
        BLANKED = 0;
        fb_blank(0);
        compose_set(urls, n);
        dprintf(fd, "OK %d\n", n);

    } else if (strcmp(cmd, "FULLSCREEN") == 0) {
        char *url = strtok(NULL, " \t\r\n");
        if (!url) { dprintf(fd, "ERR need url\n"); return; }
        static char one[1][URL_MAX];
        snprintf(one[0], URL_MAX, "%s", url);
        BLANKED = 0;
        fb_blank(0);
        compose_set(one, 1);
        dprintf(fd, "OK\n");

    } else if (strcmp(cmd, "CLEAR") == 0) {
        /* Stop showing anything on the TV, but keep the roster decoding --
         * used by the dashboard's manual "Turn Off Kiosk", which is a display
         * convenience, not a power-saving action. The channel pool keeps
         * showing live thumbnails while the TV is blanked. */
        pthread_mutex_lock(&COMPOSE_LOCK);
        compose_clear_locked();
        pthread_mutex_unlock(&COMPOSE_LOCK);
        BLANKED = 1;
        memset(FB.mem, 0, FB.len);
        dprintf(fd, "OK\n");

    } else if (strcmp(cmd, "STOP") == 0) {
        /* Full teardown: every roster connection drops too. Used for the
         * night-time power schedule and process shutdown. */
        roster_stop_all();
        BLANKED = 1;
        memset(FB.mem, 0, FB.len);
        dprintf(fd, "OK\n");

    } else if (strcmp(cmd, "BLANK") == 0) {
        roster_stop_all();
        BLANKED = 1;
        memset(FB.mem, 0, FB.len);
        fb_blank(1);
        dprintf(fd, "OK\n");

    } else if (strcmp(cmd, "FPS") == 0) {
        char *v = strtok(NULL, " \t\r\n");
        if (v) TARGET_FPS = atoi(v);
        dprintf(fd, "OK %d\n", TARGET_FPS);

    } else if (strcmp(cmd, "STATUS") == 0) {
        int64_t t = now_ms();
        pthread_mutex_lock(&ROSTER_LOCK);
        dprintf(fd, "{\"blanked\":%d,\"fps\":%d,\"streams\":[", BLANKED ? 1 : 0, TARGET_FPS);
        int first = 1;
        for (int i = 0; i < MAX_STREAMS; i++) {
            struct stream *s = &STREAMS[i];
            if (!s->used) continue;
            pthread_mutex_lock(&s->lock);
            int hf = s->have_frame;
            int64_t age = hf ? (t - s->frame_ms) : -1;
            if (hf && age < 0) age = 0;
            int64_t frames = s->frames;
            pthread_mutex_unlock(&s->lock);
            dprintf(fd,
                    "%s{\"url\":\"%s\",\"name\":\"%s\",\"connected\":%d,"
                    "\"have_frame\":%d,\"age_ms\":%lld,\"frames\":%lld}",
                    first ? "" : ",", s->url, s->name, s->connected ? 1 : 0, hf,
                    (long long)age, (long long)frames);
            first = 0;
        }
        dprintf(fd, "]}\n");
        pthread_mutex_unlock(&ROSTER_LOCK);

    } else {
        dprintf(fd, "ERR unknown\n");
    }
}

static void *control_thread(void *arg) {
    (void)arg;
    unlink(SOCK_PATH);
    int srv = socket(AF_UNIX, SOCK_STREAM, 0);
    if (srv < 0) { logmsg("ctl: socket: %s", strerror(errno)); return NULL; }
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    snprintf(addr.sun_path, sizeof addr.sun_path, "%s", SOCK_PATH);
    if (bind(srv, (struct sockaddr *)&addr, sizeof addr) < 0) {
        logmsg("ctl: bind: %s", strerror(errno));
        return NULL;
    }
    chmod(SOCK_PATH, 0666);
    listen(srv, 8);
    logmsg("ctl: listening on %s", SOCK_PATH);

    while (RUN) {
        int c = accept(srv, NULL, NULL);
        if (c < 0) continue;
        char buf[16384];
        ssize_t n = read(c, buf, sizeof buf - 1);
        if (n > 0) { buf[n] = 0; handle_cmd(c, buf); }
        close(c);
    }
    close(srv);
    unlink(SOCK_PATH);
    return NULL;
}

/* ------------------------------------------------------------- http/jpeg
 *
 * Tiny loopback-only HTTP server so the dashboard can show real live video
 * for any configured channel (not just the ones on the TV) by reading the
 * frame dvrwall already has decoded in memory -- no extra RTSP connection,
 * no extra decode. dvr_control.py proxies these over HTTPS to the browser.
 */

struct jpeg_enc {
    AVCodecContext *ctx;
    struct SwsContext *sws;
    AVFrame *yuv;
    uint8_t *yuv_buf;
    AVPacket *pkt;
    int64_t pts;
    int w, h;                  /* size this encoder is currently configured for */
};

static int jpeg_enc_init(struct jpeg_enc *je, int w, int h) {
    const AVCodec *codec = avcodec_find_encoder(AV_CODEC_ID_MJPEG);
    if (!codec) return -1;
    je->ctx = avcodec_alloc_context3(codec);
    if (!je->ctx) return -1;
    je->ctx->width = w;
    je->ctx->height = h;
    je->ctx->pix_fmt = AV_PIX_FMT_YUVJ420P;
    je->ctx->time_base = (AVRational){1, 25};
    je->ctx->color_range = AVCOL_RANGE_JPEG;
    if (avcodec_open2(je->ctx, codec, NULL) < 0) return -1;

    je->sws = sws_getContext(w, h, AV_PIX_FMT_BGRA,
                             w, h, AV_PIX_FMT_YUVJ420P,
                             SWS_FAST_BILINEAR, NULL, NULL, NULL);
    je->yuv = av_frame_alloc();
    int need = av_image_get_buffer_size(AV_PIX_FMT_YUVJ420P, w, h, 1);
    je->yuv_buf = av_malloc(need);
    av_image_fill_arrays(je->yuv->data, je->yuv->linesize, je->yuv_buf,
                         AV_PIX_FMT_YUVJ420P, w, h, 1);
    je->yuv->width = w;
    je->yuv->height = h;
    je->yuv->format = AV_PIX_FMT_YUVJ420P;
    je->pkt = av_packet_alloc();
    je->w = w;
    je->h = h;
    return (je->sws && je->yuv && je->yuv_buf && je->pkt) ? 0 : -1;
}

static void jpeg_enc_free(struct jpeg_enc *je) {
    if (je->sws) sws_freeContext(je->sws);
    if (je->yuv_buf) av_freep(&je->yuv_buf);
    if (je->yuv) av_frame_free(&je->yuv);
    if (je->pkt) av_packet_free(&je->pkt);
    if (je->ctx) avcodec_free_context(&je->ctx);
    memset(je, 0, sizeof *je);
}

/* (Re)configure an encoder for a given size, reusing it as-is if already
 * configured for that size. Streams are either all THUMB_W x THUMB_H (grid
 * mode) or a single framebuffer-sized mainstream (fullscreen mode) -- never
 * a mix -- so in practice this only actually reinitializes when switching
 * between those two modes, not per-stream. */
static int jpeg_enc_ensure(struct jpeg_enc *je, int w, int h) {
    if (je->ctx && je->w == w && je->h == h) return 0;
    if (je->ctx) jpeg_enc_free(je);
    return jpeg_enc_init(je, w, h);
}

/* Encode one BGRA slot (w x h, matching how je was last configured) to a
 * JPEG buffer. Returns malloc'd data (caller frees with av_free) via *out,
 * length via *outlen. 0 on success. */
static int jpeg_encode(struct jpeg_enc *je, const uint8_t *bgra, uint8_t **out, int *outlen) {
    uint8_t *planes[1] = { (uint8_t *)bgra };
    int stride[1] = { je->w * 4 };
    sws_scale(je->sws, (const uint8_t *const *)planes, stride, 0, je->h,
              je->yuv->data, je->yuv->linesize);
    je->yuv->pts = je->pts++;   /* must strictly increase or send_frame errors */
    int r = avcodec_send_frame(je->ctx, je->yuv);
    if (r < 0) return -1;
    r = avcodec_receive_packet(je->ctx, je->pkt);
    if (r < 0) return -1;
    *out = av_malloc(je->pkt->size);
    if (!*out) { av_packet_unref(je->pkt); return -1; }
    memcpy(*out, je->pkt->data, je->pkt->size);
    *outlen = je->pkt->size;
    av_packet_unref(je->pkt);
    return 0;
}

/* Background thread: encodes every roster channel's current frame to JPEG
 * once per tick and caches it on the stream, so any number of HTTP viewers
 * (grid tile + pool tile + multiple browser tabs, all possibly watching the
 * same channel) cost nothing extra -- they just read the cached bytes.
 *
 * ROSTER_LOCK is only held for brief O(1) checks here, never across the
 * (slow, ~ms-scale) encode itself -- holding it for the whole 28-channel
 * pass serialised every HTTP reader behind the encoder and collapsed
 * measured throughput to under 1fps with ~40 concurrent viewers. ROSTER_GEN
 * detects the rare case where roster_set() reassigns this index to a
 * different channel mid-encode, so a stale result is dropped instead of
 * corrupting the new channel's cache. */
static void *thumb_encoder_thread(void *arg) {
    (void)arg;
    struct jpeg_enc je = {0};
    if (jpeg_enc_init(&je, THUMB_W, THUMB_H) < 0) { logmsg("thumb: jpeg encoder init failed"); return NULL; }
    /* Sized for the largest possible slot -- THUMB_W x THUMB_H grid
     * substreams, or a single framebuffer-sized "_main" fullscreen stream
     * (never both at once, see ensure_roster() in dvr_control.py). */
    size_t max_px = (size_t)THUMB_W * THUMB_H;
    if ((size_t)FB.w * FB.h > max_px) max_px = (size_t)FB.w * FB.h;
    uint8_t *scratch = av_malloc(max_px * 4);
    if (!scratch) { jpeg_enc_free(&je); return NULL; }

    struct timespec next;
    clock_gettime(CLOCK_MONOTONIC, &next);
    while (RUN) {
        int64_t tick_now = now_ms();
        for (int i = 0; i < MAX_STREAMS; i++) {
            pthread_mutex_lock(&ROSTER_LOCK);
            int64_t gen = ROSTER_GEN;
            struct stream *s = &STREAMS[i];
            int used = s->used;
            pthread_mutex_unlock(&ROSTER_LOCK);
            if (!used) continue;

            pthread_mutex_lock(&s->lock);
            int have = s->have_frame;
            int64_t requested = s->requested_ms;
            int64_t last_jpeg = s->jpeg_ms;
            int sw = s->slot_w, sh = s->slot_h;
            if (have && s->slot) memcpy(scratch, s->slot, (size_t)sw * sh * 4);
            pthread_mutex_unlock(&s->lock);
            if (!have) continue;
            /* Nobody's asked for this channel recently -- decoding stays on
             * (cheap, and needed for the TV grid regardless), but skip the
             * JPEG encode. This is what keeps an idle dashboard from paying
             * continuous encode cost for channels nobody is looking at;
             * roster_get_jpeg's synchronous fallback covers the next time
             * someone actually opens it. */
            if (requested == 0 || tick_now - requested > DEMAND_WINDOW_MS) continue;
            /* This loop ticks at MJPEG_FPS_MAIN (the faster of the two
             * rates) so fullscreen stays responsive; thumbnails don't need
             * that cadence, so a non-main stream is only actually
             * re-encoded once its own slower MJPEG_FPS_THUMB interval has
             * elapsed, even though every stream is visited every tick. */
            int is_main = sw > THUMB_W;
            if (!is_main && tick_now - last_jpeg < 1000 / MJPEG_FPS_THUMB) continue;

            if (jpeg_enc_ensure(&je, sw, sh) < 0) continue;
            uint8_t *jpg = NULL; int jlen = 0;
            if (jpeg_encode(&je, scratch, &jpg, &jlen) != 0) continue;

            pthread_mutex_lock(&ROSTER_LOCK);
            if (ROSTER_GEN == gen && STREAMS[i].used) {
                pthread_mutex_lock(&STREAMS[i].lock);
                av_freep(&STREAMS[i].jpeg);
                STREAMS[i].jpeg = jpg;
                STREAMS[i].jpeg_len = jlen;
                STREAMS[i].jpeg_ms = now_ms();
                pthread_mutex_unlock(&STREAMS[i].lock);
                jpg = NULL;
            }
            pthread_mutex_unlock(&ROSTER_LOCK);
            if (jpg) av_free(jpg);   /* roster changed under us; discard */
        }

        next.tv_nsec += 1000000000L / MJPEG_FPS_MAIN;
        while (next.tv_nsec >= 1000000000L) { next.tv_nsec -= 1000000000L; next.tv_sec++; }
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
    }
    av_free(scratch);
    jpeg_enc_free(&je);
    return NULL;
}

/* Copy the cached JPEG for a channel and mark it as currently wanted (so
 * thumb_encoder_thread keeps refreshing it -- see DEMAND_WINDOW_MS). Returns
 * -1 if the channel isn't in the roster, or has no cached JPEG yet (either
 * genuinely no frame decoded yet, or it's an idle channel the encoder has
 * stopped refreshing; caller falls back to a one-off synchronous encode in
 * that case, see roster_get_jpeg). */
static int roster_copy_jpeg(const char *name, uint8_t **out, int *outlen) {
    pthread_mutex_lock(&ROSTER_LOCK);
    int idx = roster_find_locked(name);
    if (idx < 0) { pthread_mutex_unlock(&ROSTER_LOCK); return -1; }
    struct stream *s = &STREAMS[idx];
    pthread_mutex_lock(&s->lock);
    s->requested_ms = now_ms();
    int ok = -1;
    if (s->jpeg && s->jpeg_len > 0) {
        *out = av_malloc(s->jpeg_len);
        if (*out) { memcpy(*out, s->jpeg, s->jpeg_len); *outlen = s->jpeg_len; ok = 0; }
    }
    pthread_mutex_unlock(&s->lock);
    pthread_mutex_unlock(&ROSTER_LOCK);
    return ok;
}

/* Cache hit -> free. Cache miss (channel just came under demand, or the
 * encoder hasn't caught up yet) -> one-off synchronous encode straight from
 * the raw decoded slot, using a connection-local encoder passed in by the
 * caller. Keeps first-view latency low without paying continuous per-viewer
 * encode cost once the shared cache is warm. */
static int roster_get_jpeg(const char *name, struct jpeg_enc *fallback_je,
                            uint8_t **out, int *outlen) {
    if (roster_copy_jpeg(name, out, outlen) == 0) return 0;

    pthread_mutex_lock(&ROSTER_LOCK);
    int idx = roster_find_locked(name);
    if (idx < 0) { pthread_mutex_unlock(&ROSTER_LOCK); return -1; }
    struct stream *s = &STREAMS[idx];
    pthread_mutex_lock(&s->lock);
    s->requested_ms = now_ms();
    int have = s->have_frame;
    int sw = s->slot_w, sh = s->slot_h;
    uint8_t *scratch = NULL;
    if (have && s->slot) {
        scratch = av_malloc((size_t)sw * sh * 4);
        if (scratch) memcpy(scratch, s->slot, (size_t)sw * sh * 4);
    }
    pthread_mutex_unlock(&s->lock);
    pthread_mutex_unlock(&ROSTER_LOCK);

    if (!scratch) return -1;   /* known channel, but nothing decoded yet */

    if (jpeg_enc_ensure(fallback_je, sw, sh) < 0) { av_free(scratch); return -1; }
    int r = jpeg_encode(fallback_je, scratch, out, outlen);
    av_free(scratch);
    return r;
}

static void http_404(int fd) {
    const char *body = "not found";
    dprintf(fd, "HTTP/1.1 404 Not Found\r\nContent-Length: %zu\r\nConnection: close\r\n\r\n%s",
            strlen(body), body);
}

static void handle_http(int fd) {
    char req[1024];
    ssize_t n = read(fd, req, sizeof req - 1);
    if (n <= 0) return;
    req[n] = 0;

    char method[8] = "", path[512] = "";
    sscanf(req, "%7s %511s", method, path);
    if (strcmp(method, "GET") != 0) { http_404(fd); return; }

    int is_mjpeg = strncmp(path, "/mjpeg/", 7) == 0;
    int is_jpeg = strncmp(path, "/jpeg/", 6) == 0;
    if (!is_mjpeg && !is_jpeg) { http_404(fd); return; }
    const char *name = path + (is_mjpeg ? 7 : 6);
    size_t name_len = strlen(name);
    int name_is_main = name_len > 5 && strcmp(name + name_len - 5, "_main") == 0;

    /* Only used as a fallback when the shared cache is cold (channel just
     * came under demand, or thumb_encoder_thread hasn't caught up in the
     * last DEMAND_WINDOW_MS) -- see roster_get_jpeg. Zero-cost to declare;
     * only actually initialised if a fallback encode is ever needed. */
    struct jpeg_enc fallback_je = {0};

    if (is_jpeg) {
        uint8_t *jpg = NULL; int jlen = 0;
        if (roster_get_jpeg(name, &fallback_je, &jpg, &jlen) < 0) {
            http_404(fd);
            jpeg_enc_free(&fallback_je);
            return;
        }
        dprintf(fd, "HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n"
                    "Cache-Control: no-store\r\nContent-Length: %d\r\nConnection: close\r\n\r\n", jlen);
        ssize_t wn = write(fd, jpg, jlen);
        (void)wn;
        av_free(jpg);
        jpeg_enc_free(&fallback_je);
        return;
    }

    /* MJPEG: multipart/x-mixed-replace at MJPEG_FPS_MAIN/_THUMB while the client (an
     * <img> tag) keeps the connection open. Every request here also marks
     * the channel as under demand (see roster_copy_jpeg), so after the
     * first frame or two the shared cache is warm and the fallback encoder
     * is never touched again for the life of this connection. */
    uint8_t *probe = NULL; int probe_len = 0;
    if (roster_get_jpeg(name, &fallback_je, &probe, &probe_len) < 0) {
        http_404(fd);
        jpeg_enc_free(&fallback_je);
        return;
    }
    av_free(probe);

    const char *boundary = "dvrwallframe";
    dprintf(fd, "HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=%s\r\n"
                "Cache-Control: no-store\r\nConnection: close\r\n\r\n", boundary);
    int flag = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &flag, sizeof flag);

    struct timespec next;
    clock_gettime(CLOCK_MONOTONIC, &next);
    while (RUN) {
        uint8_t *jpg = NULL; int jlen = 0;
        if (roster_get_jpeg(name, &fallback_je, &jpg, &jlen) == 0) {
            char hdr[128];
            int hn = snprintf(hdr, sizeof hdr,
                              "--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n",
                              boundary, jlen);
            if (write(fd, hdr, hn) < 0 || write(fd, jpg, jlen) < 0 || write(fd, "\r\n", 2) < 0) {
                av_free(jpg);
                break;
            }
            av_free(jpg);
        } else {
            break;   /* channel removed from roster, or never got a frame */
        }
        next.tv_nsec += 1000000000L / (name_is_main ? MJPEG_FPS_MAIN : MJPEG_FPS_THUMB);
        while (next.tv_nsec >= 1000000000L) { next.tv_nsec -= 1000000000L; next.tv_sec++; }
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &next, NULL);
    }
    jpeg_enc_free(&fallback_je);
}

struct http_conn { int fd; };

static void *http_conn_thread(void *arg) {
    struct http_conn *hc = arg;
    handle_http(hc->fd);
    close(hc->fd);
    free(hc);
    return NULL;
}

static void *http_thread(void *arg) {
    (void)arg;
    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) { logmsg("http: socket: %s", strerror(errno)); return NULL; }
    int one = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in addr = {0};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);   /* loopback only */
    addr.sin_port = htons(HTTP_PORT);
    if (bind(srv, (struct sockaddr *)&addr, sizeof addr) < 0) {
        logmsg("http: bind: %s", strerror(errno));
        return NULL;
    }
    listen(srv, 32);
    logmsg("http: listening on 127.0.0.1:%d", HTTP_PORT);

    while (RUN) {
        int c = accept(srv, NULL, NULL);
        if (c < 0) continue;
        struct http_conn *hc = malloc(sizeof *hc);
        hc->fd = c;
        pthread_t t;
        if (pthread_create(&t, NULL, http_conn_thread, hc) == 0) pthread_detach(t);
        else { close(c); free(hc); }
    }
    close(srv);
    return NULL;
}

/* ------------------------------------------------------------------ main */

static void on_signal(int sig) { (void)sig; RUN = 0; }

int main(int argc, char **argv) {
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    signal(SIGPIPE, SIG_IGN);

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--fps") == 0 && i + 1 < argc) TARGET_FPS = atoi(argv[++i]);
    }

    avformat_network_init();
    av_log_set_level(AV_LOG_FATAL);

    if (fb_open() < 0) return 1;

    pthread_t ctl, comp, http, thumb;
    pthread_create(&ctl, NULL, control_thread, NULL);
    pthread_create(&comp, NULL, compositor_thread, NULL);
    pthread_create(&http, NULL, http_thread, NULL);
    pthread_create(&thumb, NULL, thumb_encoder_thread, NULL);

    logmsg("dvrwall: ready, %d fps target", TARGET_FPS);

    /* Roster arrives via CHANNELS, composite selection via LAYOUT/FULLSCREEN,
     * both on the control socket. */
    while (RUN) sleep(1);

    roster_stop_all();
    pthread_join(comp, NULL);
    memset(FB.mem, 0, FB.len);
    munmap(FB.mem, FB.len);
    close(FB.fd);
    unlink(SOCK_PATH);
    logmsg("dvrwall: exit");
    return 0;
}
