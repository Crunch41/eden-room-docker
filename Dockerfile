# Eden's standalone room server. CI builds eden-room as a static executable
# (.github/workflows/build.yml); the build context is a directory holding that
# binary and docker-entrypoint.sh.
FROM ubuntu:24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gosu tini tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY eden-room docker-entrypoint.sh /usr/local/bin/
# eden-room is static: it must not need any shared library at runtime.
RUN chmod 755 /usr/local/bin/eden-room /usr/local/bin/docker-entrypoint.sh \
    && { ldd /usr/local/bin/eden-room 2>&1 || true; } | grep -q "not a dynamic executable" \
    && /usr/local/bin/eden-room --version \
    && groupadd -g 911 eden \
    && useradd -u 911 -g eden -m eden \
    && mkdir -p /home/eden/.local/share/eden-room \
    && chown -R eden:eden /home/eden

ENV PUID=99 \
    PGID=100 \
    EDEN_ROOM_UNKNOWN_IP_FALLBACK=broadcast \
    EDEN_ROOM_PEER_TIMEOUT_MIN=12000 \
    EDEN_ROOM_PEER_TIMEOUT_MAX=60000 \
    EDEN_ROOM_PING_INTERVAL=100 \
    EDEN_ROOM_RELAY_MODE=unsequenced \
    EDEN_ROOM_RELAY_BUDGET_KBPS=0 \
    EDEN_ROOM_DIAG_INTERVAL_SEC=0 \
    EDEN_ROOM_MOD_USERNAME= \
    TZ=UTC

WORKDIR /home/eden

EXPOSE 24872/tcp
EXPOSE 24872/udp

VOLUME ["/home/eden/.local/share/eden-room"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
