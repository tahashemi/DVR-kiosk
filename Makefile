CC ?= gcc
CFLAGS ?= -O3 -Wall -Wextra -pthread
LDFLAGS ?= -lavformat -lavcodec -lavutil -lswscale -pthread

# Hardware decoding through GStreamer (stateless V4L2 decoders such as
# Allwinner cedrus and mainline Rockchip, plus NVIDIA Jetson) is built in when
# its development files are present; without them dvrwall still builds and
# uses FFmpeg's hardware paths and software decoding.
GST_PKGS = gstreamer-app-1.0 gstreamer-video-1.0
GST_LIBS := $(shell pkg-config --libs $(GST_PKGS) 2>/dev/null)
ifneq ($(GST_LIBS),)
CFLAGS += -DHAVE_GST $(shell pkg-config --cflags $(GST_PKGS))
LDFLAGS += $(GST_LIBS)
endif

SRC = src/dvrwall.c src/hwdec.c
HDR = src/hwdec.h
TARGET = bin/dvrwall

.PHONY: all clean install

all: $(TARGET)

$(TARGET): $(SRC) $(HDR)
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ $(SRC) $(LDFLAGS)

clean:
	rm -rf bin

install: $(TARGET)
	install -d /usr/local/bin
	install -m 755 $(TARGET) /usr/local/bin/dvrwall
