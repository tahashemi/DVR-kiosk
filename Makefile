CC ?= gcc
CFLAGS ?= -O3 -Wall -Wextra -pthread
LDFLAGS ?= -lavformat -lavcodec -lavutil -lswscale -pthread

SRC = src/dvrwall.c
TARGET = bin/dvrwall

.PHONY: all clean install

all: $(TARGET)

$(TARGET): $(SRC)
	@mkdir -p bin
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -rf bin

install: $(TARGET)
	install -d /usr/local/bin
	install -m 755 $(TARGET) /usr/local/bin/dvrwall
