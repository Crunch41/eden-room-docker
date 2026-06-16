# eden-room-docker

Dockerised Eden dedicated room server with hardening and latency patches applied
at build time. GitHub Actions checks for upstream Eden changes daily and rebuilds
when they appear.

## Quick start

**Private room** (direct connect only):

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
  -e TOKEN="your-token" \
  -e WEB_API_URL="https://api.ynet-fun.xyz" \
  crunch41/eden-room-server:latest
```

## Making your room public

Public rooms are announced to the Eden lobby every 15 seconds. You need a token
from the Eden GUI and the API URL. **All three variables must be set or the room
runs privately.**

| Variable | Value |
|----------|-------|
| `TOKEN` | Your token from Eden → Settings → Multiplayer. Copy the full token string. |
| `WEB_API_URL` | `https://api.ynet-fun.xyz` |
| `USERNAME` | Leave **unset**. Your username is extracted from the token automatically. |

> **Why leave USERNAME unset?** The token you copy from Eden's GUI is a display
> token containing your username and credentials combined. When `USERNAME` is not
> set, the server decodes it automatically. If you also set `USERNAME`, the server
> skips decoding and sends the raw token string to the API, which will likely
> fail authentication.

## Environment variables

### Room configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ROOM_NAME` | `Eden Room` | Room name shown in the lobby. |
| `ROOM_DESCRIPTION` | *(empty)* | Optional description shown to players. |
| `PORT` | `24872` | UDP/TCP port (1–65535). Must match the `-p` mapping. |
| `MAX_MEMBERS` | `16` | Maximum concurrent players (2–254). |
| `BIND_ADDRESS` | `0.0.0.0` | Interface to bind on. |
| `PASSWORD` | *(empty)* | Room password. Leave unset for an open room. |
| `PREFERRED_GAME` | `Any Game` | Preferred game name shown in the lobby. |
| `PREFERRED_GAME_ID` | `0` | Hex title ID without `0x` prefix. Examples: `0100152000022000` (Mario Kart 8 Deluxe), `01006A800016E000` (Smash Bros Ultimate). Set to `0` for any game. |
| `BAN_LIST_FILE` | `/home/eden/.local/share/eden-room/ban_list.txt` | Path to the ban list inside the container. |
| `LOG_DIR` | `/home/eden/.local/share/eden-room` | Session log directory. Each restart creates a new timestamped log; oldest logs are deleted once `MAX_LOG_FILES` is reached. |
| `MAX_LOG_FILES` | `10` | Number of session logs to keep. |
| `TOKEN` | *(empty)* | Display token from Eden GUI. Required for public rooms. |
| `USERNAME` | *(empty)* | Leave unset when using a display token. See [Making your room public](#making-your-room-public). |
| `WEB_API_URL` | *(empty)* | `https://api.ynet-fun.xyz` for the Eden lobby. Required for public rooms. |
| `TZ` | `UTC` | Timezone for log timestamps (e.g. `Australia/Melbourne`, `America/New_York`). |
| `PUID` | `99` | UID the server process runs as. |
| `PGID` | `100` | GID the server process runs as. |

### Runtime tuning

Rarely need changing from defaults.

| Variable | Default | Description |
|----------|---------|-------------|
| `EDEN_ROOM_UNKNOWN_IP_FALLBACK` | `broadcast` | `broadcast` fans unknown fake-IP packets to all members. `drop` restores strict discard. |
| `EDEN_ROOM_PEER_TIMEOUT_MIN` | `12000` | ENet timeout minimum in ms. Earliest a dead peer is dropped. |
| `EDEN_ROOM_PEER_TIMEOUT_MAX` | `60000` | ENet timeout maximum in ms. Clamped to >= minimum. |
| `EDEN_ROOM_PING_INTERVAL` | `100` | ENet ping interval in ms. |
| `EDEN_ROOM_RELAY_RELIABLE` | `0` | Set to `1` to restore reliable relay for per-title regression testing. |
| `EDEN_ROOM_MOD_USERNAME` | *(empty)* | Username to grant moderator. Falls back to the username in `TOKEN`. Only granted to RFC 1918 / loopback connections — remote IPs are never elevated. |

## Log output

Each restart writes a new timestamped session log to `LOG_DIR`. Output also
goes to stdout so `docker logs` works normally.

```
[10:23:45] JOIN  | [1.2.3.4] PlayerName has joined. (1/16)
[10:23:45] PING  | [1.2.3.4] PlayerName RTT 172ms
[10:23:46] GAME  | PlayerName is playing Mario Kart 8 Deluxe (3.0.3)
[10:24:10] CHAT  | PlayerName: gg
[10:27:57] STAT  | [1.2.3.4] PlayerName session RTT 172ms duration 4m12s
[10:27:57] LEAVE | [1.2.3.4] PlayerName has left. (0/16)
[10:28:01] Network <Warning> Dropping malformed room packet
```

## Unraid

Recommended settings:

| Setting | Value |
|---------|-------|
| Data Directory | `/mnt/user/appdata/eden-room` |
| PUID | `99` (Unraid nobody) |
| PGID | `100` (Unraid users) |
| TZ | Your local timezone, e.g. `Australia/Melbourne` |

## What this image does differently

Full rationale for every change is in [PATCHES.md](PATCHES.md).

### Latency
- **Event loop drain** — drains all queued ENet events immediately instead of one per 5 ms poll, removing up to 5 ms of relay latency under burst load.
- **Unreliable game relay** — proxy/LDN packets use `ENET_PACKET_FLAG_UNSEQUENCED`, matching real Switch LDN. ENet reliable delivery caused head-of-line blocking on lossy paths. Control packets remain reliable.
- **Relay throttle pin** — ENet's packet throttle pinned at 100 % per peer so RTT jitter cannot silently drop game packets.
- **Ping interval** — reduced from 500 ms to 100 ms for fresher RTT stats.

### Stability
- **Peer timeout** — raised to 12 s / 60 s (ENet defaults: 5 s / 30 s) to survive transient international packet loss. Env-tunable.
- **Rejected-join cleanup** — `enet_peer_disconnect_later` on all rejection paths so ENet slots are reclaimed immediately on ACK, not after timeout.
- **Relay payload cap** — packets over 4096 bytes dropped. Prevents ENet from silently falling back to reliable fragmentation and blocks broadcast amplification.
- **Signal-aware shutdown** — `SIGINT`/`SIGTERM` reach announce cleanup, ban-list save, and `room->Destroy()`.
- **Relay lock downgrade** — relay handlers use a shared read lock so concurrent relays proceed in parallel.

### Security
- **Join rate limiting** — one join attempt per IP per second; stale entries pruned after 10 minutes.
- **Local-subnet moderator gate** — moderator only granted to RFC 1918 / loopback connections. Remote IPs never elevated.
- **Packet validation** — all packet types checked for minimum size before parsing.
- **JWT public key mutex** — guards static key cache against concurrent joins.
- **Member count under lock** — room broadcast serializes member count inside `member_mutex`.

### Observability
- **Structured labels** — `JOIN`, `LEAVE`, `CHAT`, `GAME`, `PING`, `STAT` with wall-clock timestamps.
- **Player counts** — all status lines include current/max.
- **PING line** — RTT logged immediately on join.
- **STAT line** — RTT-at-join and session duration logged on every disconnect.

## Building locally

```bash
# Latest upstream HEAD
docker build -t eden-room .

# Specific Eden commit
docker build --build-arg EDEN_REF=<commit-sha> -t eden-room .
```

## How patches are applied

`scripts/apply-eden-room-patches.py` runs inside the Eden source tree at build
time. It uses exact string matching and fails loudly if the expected source
blocks have moved, so a broken patch is always caught at build time rather than
producing a silently unpatched binary.

See [PATCHES.md](PATCHES.md) for the full patch list.
