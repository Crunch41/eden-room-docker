# eden-room-docker

Dockerised Eden dedicated room server with hardening and latency patches applied
at build time. GitHub Actions checks for upstream Eden changes daily and rebuilds
automatically when they appear.

## Quick start

All configuration is through environment variables — do **not** pass CLI flags
after the image name.

**Private room** (direct-connect only, no lobby listing):

```bash
docker run -d \
  -p 24872:24872/udp \
  -p 24872:24872/tcp \
  -v eden-room-data:/home/eden/.local/share/eden-room \
  -e ROOM_NAME="My Room" \
  -e PREFERRED_GAME="Mario Kart 8 Deluxe" \
  -e PREFERRED_GAME_ID="0100152000022000" \
  -e MAX_MEMBERS="8" \
  crunch41/eden-room-server:latest
```

**Public room** (visible in the Eden lobby):

```bash
docker run -d \
  -p 24872:24872/udp \
  -p 24872:24872/tcp \
  -v eden-room-data:/home/eden/.local/share/eden-room \
  -e ROOM_NAME="My Room" \
  -e PREFERRED_GAME="Mario Kart 8 Deluxe" \
  -e PREFERRED_GAME_ID="0100152000022000" \
  -e MAX_MEMBERS="8" \
  -e USERNAME="your-eden-username" \
  -e TOKEN="your-eden-token" \
  -e WEB_API_URL="https://api.ynet-fun.xyz" \
  crunch41/eden-room-server:latest
```

## Making your room public

To appear in the Eden lobby browser, you need an Eden account and three
environment variables set together. If any of the three are missing or empty,
the room runs privately.

| Variable | Value |
|----------|-------|
| `USERNAME` | Your Eden account username |
| `TOKEN` | Your Eden account token (from your account settings on the Eden site) |
| `WEB_API_URL` | `https://api.ynet-fun.xyz` |

The room re-announces itself to the lobby every 15 seconds while running.

## Environment variables

### Room configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ROOM_NAME` | `Eden Room` | Room name shown in the lobby browser. |
| `ROOM_DESCRIPTION` | *(empty)* | Optional description shown to players. |
| `PORT` | `24872` | UDP/TCP port (1–65535). Must match the `-p` mapping. |
| `MAX_MEMBERS` | `16` | Maximum concurrent players (2–254). |
| `BIND_ADDRESS` | `0.0.0.0` | Interface address to bind. |
| `PASSWORD` | *(empty)* | Room password. Leave unset for an open room. |
| `PREFERRED_GAME` | `Any Game` | Preferred game name shown in the lobby. |
| `PREFERRED_GAME_ID` | `0` | Hex title ID without `0x` prefix (e.g. `0100152000022000` for Mario Kart 8 Deluxe, `01006A800016E000` for Smash Bros Ultimate). Set to `0` for any game. |
| `BAN_LIST_FILE` | `/home/eden/.local/share/eden-room/ban_list.txt` | Path to the persistent ban list inside the container. |
| `LOG_DIR` | `/home/eden/.local/share/eden-room` | Directory for session logs. Each container restart creates a new timestamped log file; old logs are deleted once `MAX_LOG_FILES` is reached. |
| `MAX_LOG_FILES` | `10` | Number of session logs to keep. |
| `USERNAME` | *(empty)* | Eden account username. Required with `TOKEN` and `WEB_API_URL` for public rooms. |
| `TOKEN` | *(empty)* | Eden account token. Required with `USERNAME` and `WEB_API_URL` for public rooms. |
| `WEB_API_URL` | *(empty)* | Lobby API URL. Use `https://api.ynet-fun.xyz` for the standard Eden lobby. |
| `TZ` | `UTC` | Container timezone for log timestamps (e.g. `Australia/Melbourne`, `America/New_York`). |
| `PUID` | `99` | UID the server process runs as. On Unraid this is `99` (nobody). Set to your host user's UID if you have volume permission issues. |
| `PGID` | `100` | GID the server process runs as. On Unraid this is `100` (users). |

### Runtime tuning

These have sensible defaults and rarely need changing.

| Variable | Default | Description |
|----------|---------|-------------|
| `EDEN_ROOM_UNKNOWN_IP_FALLBACK` | `broadcast` | `broadcast` fans unknown fake-IP packets to all members (best for most games). `drop` restores strict discard behaviour. |
| `EDEN_ROOM_PEER_TIMEOUT_MIN` | `12000` | ENet `timeoutMinimum` in ms. Earliest a dead peer is dropped; also the nickname rejoin-lockout window. |
| `EDEN_ROOM_PEER_TIMEOUT_MAX` | `60000` | ENet `timeoutMaximum` in ms. Clamped to >= minimum. |
| `EDEN_ROOM_PING_INTERVAL` | `100` | ENet ping interval in ms. Lower values keep RTT stats fresher; does not affect minimum drop time. |
| `EDEN_ROOM_RELAY_RELIABLE` | `0` | Set to `1` to restore upstream `ENET_PACKET_FLAG_RELIABLE` relay for per-title regression testing. |
| `EDEN_ROOM_MOD_USERNAME` | *(empty)* | Username to grant moderator status. Falls back to `USERNAME` when empty. Moderator is **only** granted to connections from RFC 1918 / loopback addresses — remote IPs are never elevated. |

## Log output

Each container restart begins a new timestamped session log in `LOG_DIR`.
Output is also written to Docker's stdout so `docker logs` works as normal.

```
[10:23:45] JOIN  | [1.2.3.4] PlayerName has joined. (1/16)
[10:23:45] PING  | [1.2.3.4] PlayerName RTT 172ms
[10:23:46] GAME  | PlayerName is playing Mario Kart 8 Deluxe (3.0.3)
[10:24:10] CHAT  | PlayerName: gg
[10:27:57] STAT  | [1.2.3.4] PlayerName session RTT 172ms duration 4m12s
[10:27:57] LEAVE | [1.2.3.4] PlayerName has left. (0/16)
[10:28:01] Network <Warning> Dropping malformed room packet
```

Each join produces a `JOIN` line followed immediately by a `PING` line showing
measured RTT. Each disconnect produces a `STAT` line (RTT at join + total
session duration) before the `LEAVE` line.

## What this image does differently

Patches are applied to the upstream Eden source before compiling. Full rationale
for every change is in [PATCHES.md](PATCHES.md). Key changes by area:

### Latency

- **Event loop drain** — queued ENet events are drained immediately on arrival
  instead of one per 5 ms poll, removing up to 5 ms of relay latency per packet
  under burst load.
- **Unreliable game relay** — proxy/LDN packets use `ENET_PACKET_FLAG_UNSEQUENCED`,
  matching real Switch LDN transport. ENet reliable delivery caused head-of-line
  blocking on lossy international paths. Control packets remain reliable.
- **Relay throttle pin** — ENet's packet throttle is pinned at 100 % per peer so
  RTT jitter cannot silently drop UNSEQUENCED game packets.
- **Ping interval** — reduced from 500 ms to 100 ms so RTT/loss statistics stay
  fresh and the timeout machinery arms faster on idle connections.

### Stability

- **Peer timeout** — raised to 12 s minimum / 60 s maximum (ENet defaults are
  5 s / 30 s), giving headroom over typical AUS↔USA transient routing loss
  (3–5 s) without holding slots indefinitely. Both values are env-tunable.
- **Rejected-join cleanup** — all rejection paths (`IdRoomIsFull`,
  `IdWrongPassword`, `IdNameCollision`, `IdIpCollision`, `IdVersionMismatch`,
  ban) call `enet_peer_disconnect_later` so ENet slots are reclaimed as soon as
  the rejection is ACKed, not after a full timeout.
- **Relay payload cap** — packets larger than 4096 bytes are dropped with a
  warning. Oversized UNSEQUENCED packets cannot be fragmented by ENet and would
  silently fall back to RELIABLE delivery; this cap also prevents broadcast
  amplification from malicious clients.
- **Signal-aware shutdown** — `SIGINT`/`SIGTERM` trigger a clean shutdown path
  reaching announce cleanup, ban-list save, and `room->Destroy()`.
- **Relay lock downgrade** — relay handlers use a shared read lock on the member
  list so concurrent relay operations from all peers proceed in parallel.

### Security

- **Join rate limiting** — each IP is limited to one join attempt per second.
  Stale entries are pruned after 10 minutes.
- **Local-subnet moderator gate** — moderator status is only granted to
  connections from RFC 1918 / loopback addresses (10.0.0.0/8, 172.16.0.0/12,
  192.168.0.0/16, 127.0.0.0/8). Remote IPs are never elevated even if the
  username matches. Configure with `EDEN_ROOM_MOD_USERNAME`.
- **Packet validation** — all incoming packet types are validated before parsing
  (minimum size check, field reads verified).
- **JWT public key mutex** — protects the static public-key cache against
  concurrent JWT verifications during simultaneous joins.
- **JWT error suppression** — only the routine unauthenticated-client case is
  suppressed. Real failures such as `TokenExpired` and bad-signature errors
  remain visible.
- **Member count under lock** — room information broadcasts serialize the member
  count inside `member_mutex`, fixing a data race.
- **Status flush outside lock** — `enet_host_flush` is called after releasing
  `member_mutex` so socket I/O does not stall other threads holding the lock.

### Observability

- **Structured log labels** — `JOIN`, `LEAVE`, `CHAT`, `GAME`, `PING`, `STAT`
  with wall-clock timestamps replace the default elapsed-time format.
- **Player counts** — join/leave/kick/ban lines include current/max counts.
- **PING line** — each join logs the measured RTT immediately.
- **STAT line** — each leave logs RTT-at-join and session duration.

## Building locally

```bash
# Build against latest upstream HEAD
docker build -t eden-room .

# Build against a specific Eden commit
docker build --build-arg EDEN_REF=<commit-sha> -t eden-room .
```

## How patches are applied

`scripts/apply-eden-room-patches.py` runs inside the Eden source tree during the
Docker build. It uses exact string matching (`replace_once`) and fails loudly if
the expected source blocks have moved, so broken patch application is always
caught at build time rather than producing a silently unpatched binary.

See [PATCHES.md](PATCHES.md) for the full patch list and rationale for every
change.
