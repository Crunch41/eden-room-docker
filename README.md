# eden-room-docker

Dockerised Eden dedicated room server with hardening and latency-optimisation
patches applied at build time. GitHub Actions rebuilds automatically whenever
upstream Eden HEAD changes.

## Quick start

```bash
docker run -d \
  -p 24872:24872/udp \
  -p 24872:24872/tcp \
  -v eden-room-data:/home/eden/.local/share/eden-room \
  -e ROOM_NAME="My Room" \
  -e PREFERRED_GAME="Mario Kart 8 Deluxe" \
  -e PREFERRED_GAME_ID="0100152000022000" \
  ghcr.io/crunch41/eden-room-docker:latest \
  --room-name "My Room" \
  --preferred-game "Mario Kart 8 Deluxe" \
  --preferred-game-id 0x0100152000022000 \
  --max-members 8
```

See [docs/user/ServerHosting.md](https://git.eden-emu.dev/eden-emu/eden/src/branch/master/docs/user/ServerHosting.md)
in the upstream Eden repo for full CLI reference.

## What this image does differently

This image applies a set of patches to the upstream Eden source before
compiling. The patches are documented in full in [PATCHES.md](PATCHES.md).
Key changes:

### Latency improvements
- **Event loop drain** — ENet events are drained immediately when they arrive
  instead of one per 5 ms poll, removing up to 5 ms of server-added relay
  latency per packet under burst load.
- **Unreliable game relay** — proxy/LDN packets use `ENET_PACKET_FLAG_UNSEQUENCED`
  matching real Switch LDN transport (raw 802.11 UDP). ENet's reliable delivery
  caused head-of-line blocking on lossy international paths (burst/teleport
  desync pattern). Control packets remain reliable.
- **Ping interval** — reduced from 500 ms to 100 ms so RTT/loss statistics stay
  current and the timeout machinery arms faster on idle connections.

### Stability improvements
- **Peer timeout** — raised from ENet's 5 s default to 12 s minimum / 60 s
  maximum, giving 2–4× headroom over typical AUS↔USA transient routing loss
  (3–5 s) without holding slots indefinitely.
- **Rejected-join cleanup** — all rejection paths (`IdRoomIsFull`,
  `IdWrongPassword`, `IdNameCollision`, `IdIpCollision`, `IdVersionMismatch`,
  ban) now call `enet_peer_disconnect_later` so ENet slots are reclaimed as
  soon as the rejection is ACKed, not after a full timeout.
- **Relay payload cap** — packets larger than 4096 bytes are dropped. Oversized
  UNSEQUENCED packets cannot be fragmented by ENet and would silently fall back
  to RELIABLE delivery (reintroducing head-of-line blocking); this cap also
  prevents broadcast amplification from malicious clients.
- **Signal-aware shutdown** — `SIGINT`/`SIGTERM` trigger a clean shutdown path
  reaching announce cleanup, ban-list save, and `room->Destroy()`.

### Security / correctness fixes
- **JWT error suppression** — the previous patch suppressed all JWT errors with
  `error.value() == 2` regardless of category, which would have hidden real
  `TokenExpired` and bad-signature failures. Now only the routine
  unauthenticated-client case (`DecodeErrc::SignatureFormatError`, category
  `"decode"`, value 2) is suppressed.
- **JWT public key mutex** — protects the static public-key cache against
  concurrent JWT verifications during simultaneous joins.
- **Packet validation** — all incoming packet types are validated before
  parsing (minimum size, field reads succeed).
- **Member count under lock** — the room information broadcast now serializes
  the member count inside `member_mutex`, fixing a data race.
- **Status flush outside lock** — `enet_host_flush` (socket I/O) is now called
  after releasing `member_mutex` instead of while holding it.

### Observability
- **Structured log labels** — `JOIN`, `LEAVE`, `CHAT`, `GAME`, `PING`, `STAT`
  with wall-clock timestamps replace the default elapsed-time format.
- **PING line** — each join logs the measured RTT immediately.
- **STAT line** — each leave logs RTT-at-join and session duration.
- **Player counts** — join/leave/kick/ban lines include current/max counts.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `EDEN_ROOM_UNKNOWN_IP_FALLBACK` | `broadcast` | `broadcast` fans unknown fake-IP packets to all members; `drop` restores strict behaviour. |
| `EDEN_ROOM_PEER_TIMEOUT_MIN` | `12000` | ENet `timeoutMinimum` in ms. Earliest a dead peer is dropped; also the nickname rejoin-lockout window. |
| `EDEN_ROOM_PEER_TIMEOUT_MAX` | `60000` | ENet `timeoutMaximum` in ms. Clamped to >= minimum. |
| `EDEN_ROOM_PING_INTERVAL` | `100` | ENet ping interval in ms. Lower values give fresher RTT stats; does not change minimum drop time. |
| `EDEN_ROOM_RELAY_RELIABLE` | `0` | Set to `1` to restore upstream `ENET_PACKET_FLAG_RELIABLE` relay for per-title regression testing. |

## Log output

```
[10:23:45] JOIN  | [1.2.3.4] PlayerName has joined. (1/8)
[10:23:45] PING  | [1.2.3.4] PlayerName RTT 172ms
[10:23:46] GAME  | PlayerName is playing Mario Kart 8 Deluxe (3.0.3)
[10:24:10] CHAT  | PlayerName: gg
[10:27:57] STAT  | [1.2.3.4] PlayerName session RTT 172ms duration 4m12s
[10:27:57] LEAVE | [1.2.3.4] PlayerName has left. (0/8)
[10:28:01] Network <Warning> Dropping malformed room packet
```

## Building locally

```bash
# Build against a specific Eden commit
docker build --build-arg EDEN_REF=<commit-sha> -t eden-room .

# Build against latest upstream HEAD
docker build -t eden-room .
```

## How patches are applied

`scripts/apply-eden-room-patches.py` runs inside the Eden source tree during
the Docker build. It uses exact string matching (`replace_once`) and fails
loudly if the expected source blocks have moved, so broken patch application is
always caught at build time rather than producing a silently unpatched binary.

See [PATCHES.md](PATCHES.md) for the full patch list, audit results, and
rationale for every change.
