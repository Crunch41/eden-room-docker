# syntax=docker/dockerfile:1.7
###########################
# 1) Builder stage
###########################
FROM ubuntu:24.04 AS builder

ARG EDEN_REF=37026c8aaa9e1ce01026c2aa69b4b8af5842ec5a
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      git \
      build-essential \
      cmake \
      ninja-build \
      pkg-config \
      python3 \
      perl \
      autoconf \
      libtool \
      libboost-all-dev \
      libfmt-dev \
      liblz4-dev \
      libzstd-dev \
      libssl-dev \
      libopus-dev \
      zlib1g-dev \
      libenet-dev \
      nlohmann-json3-dev \
      llvm-dev \
      libudev-dev \
      libopenal-dev \
      glslang-tools \
      libavcodec-dev \
      libavfilter-dev \
      libavutil-dev \
      libswscale-dev \
      libswresample-dev \
      libx11-dev \
      libxrandr-dev \
      libxinerama-dev \
      libxcursor-dev \
      libxi-dev \
      libmbedtls-dev \
      libusb-1.0-0-dev \
      gamemode-dev \
      libsdl2-dev \
      doxygen \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

RUN git clone --recursive https://git.eden-emu.dev/eden-emu/eden.git . && \
    git checkout "${EDEN_REF}" && \
    git submodule update --init --recursive && \
    echo "=== EDEN SOURCE ===" && \
    git log -1 --format="%H %s"

COPY scripts/apply-eden-room-patches.py /tmp/apply-eden-room-patches.py
RUN python3 /tmp/apply-eden-room-patches.py

# Release build for the standalone dedicated room. Keep the room protocol
# behavior unchanged: one ENet channel, reliable packets, and normal flushes.
RUN cmake -S . -B build \
      -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DENABLE_QT=OFF \
      -DENABLE_CUBEB=OFF \
      -DYUZU_TESTS=OFF \
      -DENABLE_UPDATE_CHECKER=OFF \
      -DUSE_DISCORD_PRESENCE=OFF \
      -DENABLE_WEB_SERVICE=ON \
      -DYUZU_ROOM=ON \
      -DYUZU_ROOM_STANDALONE=ON \
      -DYUZU_DISABLE_LLVM=ON \
      -DYUZU_CMD=OFF

RUN cmake --build build --target yuzu_room_standalone -j"$(nproc)" && \
    strip build/bin/eden-room && \
    echo "=== BUILD COMPLETE ===" && \
    ls -lh build/bin/eden-room


###########################
# 2) Runtime stage
###########################
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      libssl3 \
      libzstd1 \
      liblz4-1 \
      libopus0 \
      zlib1g \
      libboost-context1.83.0 \
      libenet7 \
      libfmt9 \
      libmbedtls14 \
      libopenal1 \
      libavcodec60 \
      libavfilter9 \
      libavutil58 \
      libswscale7 \
      libswresample4 \
      gzip \
      gosu \
      tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /src/build/bin/eden-room /usr/local/bin/eden-room

RUN groupadd -g 911 eden && \
    useradd -u 911 -g eden -m eden && \
    mkdir -p /home/eden/.local/share/eden-room && \
    chown -R eden:eden /home/eden

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PUID=99 \
    PGID=100 \
    EDEN_ROOM_UNKNOWN_IP_FALLBACK=broadcast

WORKDIR /home/eden

EXPOSE 24872/tcp
EXPOSE 24872/udp

VOLUME ["/home/eden/.local/share/eden-room"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
