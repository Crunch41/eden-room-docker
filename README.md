# eden-room-docker

Dockerised Eden dedicated room server with hardening and latency-optimisation
patches applied at build time. GitHub Actions rebuilds automatically whenever
upstream Eden HEAD changes.

## Quick start

All configuration is done through environment variables. The entrypoint builds
the binary command from them — do **not** pass CLI flags after the image name,
they are not forwarded to the binary.

```bash
docker run -d \
  -p 24872:24872/udp \
  -p 24872:24872/tcp \
  -v eden-room-data:/home/eden/.local/share/eden-room \
  -e ROOM_NAME="My Room" \
  -e PREFERRED_GAME="Mario Kart 8 Deluxe" \
  -e PREFERRED_GAME_ID="0100152000022000" \
  -e MAX_MEMBERS="8" \
  ghcr.io/crunch41/eden-room-docker:latest
```

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
- **Relay throttle pin** — ENet's packet throttle is pinned at 100 % per peer so
  RTT jitter on internet paths cannot silently drop UNSEQUENCED game packets.

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
- **Relay lock downgrade** — relay handlers use a shared read lock on the member
  list so concurrent relay operations from all peers proceed in parallel.

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
- **Local-subnet moderator gate** — moderator status is only granted to
  connections from RFC 1918 / loopback addresses (10.0.0.0/8, 172.16.0.0/12,
  192.168.0.0/16, 127.0.0.0/8). Remote IPs are never elevated even if the
  username matches, preventing impersonation from public internet clients.
  Configure the moderator username with `EDEN_ROOM_MOD_USERNAME`.

### Observability
- **Structured log labels** — `JOIN`, `LEAVE`, `CHAT`, `GAME`, `PING`, `STAT`
  with wall-clock timestamps replace the default elapsed-time format.
- **PING line** — each join logs the measured RTT immediately.
- **STAT line** — each leave logs RTT-at-join and session duration.
- **Player counts** — join/leave/kick/ban lines include current/max counts.

## Environment variables

### Room configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ROOM_NAME` | `Eden Room` | Room name shown in the lobby browser. |
| `ROOM_DESCRIPTION` | *(empty)* | Optional room description. |
| `PORT` | `24872` | UDP/TCP port the server listens on (1–65535). Must match the `-p` mapping. |
| `MAX_MEMBERS` | `16` | Maximum concurrent players (2–254). |
| `BIND_ADDRESS` | `0.0.0.0` | Interface address to bind. |
| `PASSWORD` | *(empty)* | Room password. Leave unset for a public room. |
| `PREFERRED_GAME` | `Any Game` | Game name shown in the lobby. |
| `PREFERRED_GAME_ID` | `0` | Hex title ID of the preferred game, without `0x` prefix (e.g. `0100152000022000` for Mario Kart 8 Deluxe). |
| `BAN_LIST_FILE` | `/home/eden/.local/share/eden-room/ban_list.txt` | Path to the persistent ban list file. |
| `LOG_DIR` | `/home/eden/.local/share/eden-room` | Directory for session log files. |
| `MAX_LOG_FILES` | `10` | Number of session logs to keep before the oldest is deleted. |
| `USERNAME` | *(empty)* | Lobby account username. Required together with `TOKEN` and `WEB_API_URL` to announce the room publicly. |
| `TOKEN` | *(empty)* | Lobby account token. |
| `WEB_API_URL` | *(empty)* | Lobby API endpoint URL. |
| `TZ` | `UTC` | Container timezone, used for log timestamps. |
| `PUID` | `99` | UID the server process runs as. Set to your host user's UID to avoid volume permission issues. |
| `PGID` | `100` | GID the server process runs as. |

### Runtime tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `EDEN_ROOM_UNKNOWN_IP_FALLBACK` | `broadcast` | `broadcast` fans unknown fake-IP packets to all members; `drop` restores strict behaviour. |
| `EDEN_ROOM_PEER_TIMEOUT_MIN` | `12000` | ENet `timeoutMinimum` in ms. Earliest a dead peer is dropped; also the nickname rejoin-lockout window. |
| `EDEN_ROOM_PEER_TIMEOUT_MAX` | `60000` | ENet `timeoutMaximum` in ms. Clamped to >= minimum. |
| `EDEN_ROOM_PING_INTERVAL` | `100` | ENet ping interval in ms. Lower values give fresher RTT stats; does not change minimum drop time. |
| `EDEN_ROOM_RELAY_RELIABLE` | `0` | Set to `1` to restore upstream `ENET_PACKET_FLAG_RELIABLE` relay for per-title regression testing. |
| `EDEN_ROOM_MOD_USERNAME` | *(empty)* | Username to grant moderator status. When empty, falls back to the `USERNAME` lobby account name. Moderator is **only** granted to connections from RFC 1918 / loopback addresses regardless of this value. |

## Log output

```
[10:23:45] JOIN  | [1.2.3.4] PlayerName has joined. (1/16)
[10:23:45] PING  | [1.2.3.4] PlayerName RTT 172ms
[10:23:46] GAME  | PlayerName is playing Mario Kart 8 Deluxe (3.0.3)
[10:24:10] CHAT  | PlayerName: gg
[10:27:57] STAT  | [1.2.3.4] PlayerName session RTT 172ms duration 4m12s
[10:27:57] LEAVE | [1.2.3.4] PlayerName has left. (0/16)
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
