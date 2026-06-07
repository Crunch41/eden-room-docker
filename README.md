# Eden Room Server - Docker

Dockerized Eden dedicated server for hosting multiplayer rooms. Includes
reproducible builds, Docker-friendly shutdown, and 17 hardening and
latency-optimisation patches applied before compilation.

## Quick Start

### Private Room (LAN / Friends Only)

No account needed. Share your IP and port with friends directly.

```bash
docker run -d -p 24872:24872/tcp -p 24872:24872/udp \
  -e ROOM_NAME="My Room" \
  -e PREFERRED_GAME="Super Smash Bros" \
  crunch41/eden-room-server:latest
```

### Public Room (Listed in Lobby)

Requires Eden account credentials — see [Getting Credentials](#getting-credentials-public-rooms) below.

```bash
docker run -d -p 24872:24872/tcp -p 24872:24872/udp \
  -e ROOM_NAME="My Public Room" \
  -e PREFERRED_GAME="Super Smash Bros" \
  -e USERNAME="your_username" \
  -e TOKEN="your-token" \
  -e WEB_API_URL="https://api.ynet-fun.xyz" \
  crunch41/eden-room-server:latest
```

Port 24872 (TCP **and** UDP) must be open in your firewall and forwarded in
your router. Without this, players cannot connect even if your room appears in
the lobby.

---

## Getting Credentials (Public Rooms)

`WEB_API_URL` is the lobby registration server that lists your room in the Eden
client's room browser. The community-run instance at `https://api.ynet-fun.xyz`
is the standard server ([source](https://github.com/simvux/room-reg-impl)).
Your room announces itself every 15 seconds while the container is running.

`USERNAME` and `TOKEN` are your Eden account credentials:

1. Open the Eden client
2. Go to **Settings → Network**
3. Set the Web API URL to `https://api.ynet-fun.xyz`
4. Log in or create an account through the client
5. Copy your username and token from the network settings into your Docker
   environment variables

---

## Environment Variables

### Required

| Variable | Description |
|----------|-------------|
| `ROOM_NAME` | Room name displayed in the lobby |
| `PREFERRED_GAME` | Game name displayed in the lobby |

### Public Room Settings

| Variable | Description |
|----------|-------------|
| `USERNAME` | Your Eden account username |
| `TOKEN` | Your Eden authentication token (UUID format) |
| `WEB_API_URL` | Lobby API URL — use `https://api.ynet-fun.xyz` for the community server |

### Optional Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `ROOM_DESCRIPTION` | (empty) | Room description shown in the lobby |
| `PREFERRED_GAME_ID` | 0 | Switch title ID in hex (e.g. `0100152000022000` for MK8DX) |
| `MAX_MEMBERS` | 16 | Maximum players (2–254) |
| `PASSWORD` | (empty) | Password-protect the room |
| `BIND_ADDRESS` | 0.0.0.0 | Network interface to bind |
| `PORT` | 24872 | Server port |
| `TZ` | UTC | Container timezone (affects log timestamps) |
| `BAN_LIST_FILE` | /home/eden/.local/share/eden-room/ban_list.txt | Path to the ban list file |
| `EDEN_ROOM_UNKNOWN_IP_FALLBACK` | broadcast | `broadcast` fans unknown fake-IP packets to all peers; `drop` discards them |

### File Permissions (Unraid / NAS)

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | 99 | User ID for file ownership |
| `PGID` | 100 | Group ID for file ownership |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_DIR` | /home/eden/.local/share/eden-room | Session log directory |
| `MAX_LOG_FILES` | 10 | Number of session logs to retain |

Docker console output is mirrored to timestamped session log files. Old logs
are rotated automatically. Room activity is labelled for easy scanning:

```text
[10:23:45] JOIN  | [1.145.73.191] Jonathan has joined. (1/16)
[10:23:45] PING  | [1.145.73.191] Jonathan RTT 23ms
[10:23:46] GAME  | Jonathan is playing Mario Kart 8 Deluxe (3.0.3)
[10:24:10] CHAT  | Jonathan: yo
[10:27:57] STAT  | [118.92.194.254] lilboat final RTT 26ms loss 0.0% tx 0.2MB rx 0.1MB
[10:27:57] LEAVE | [118.92.194.254] lilboat has left. (0/16)
```

`STAT` fires before every `LEAVE`. Elevated loss (e.g. `loss 4.2%`) means the
extended peer timeout kept the player alive through transient packet loss;
`loss 0.0%` is a clean exit.

---

## Docker Compose

```yaml
services:
  eden-room:
    image: crunch41/eden-room-server:latest
    ports:
      - "24872:24872/tcp"
      - "24872:24872/udp"
    environment:
      ROOM_NAME: "My Server"
      PREFERRED_GAME: "Super Smash Bros"
      USERNAME: "your_username"
      TOKEN: "your-token"
      WEB_API_URL: "https://api.ynet-fun.xyz"
    volumes:
      - ./data:/home/eden/.local/share/eden-room
    restart: unless-stopped
```

---

## Persistent Data

Mount a volume to preserve ban list and session logs across container restarts:

```bash
-v /path/to/data:/home/eden/.local/share/eden-room
```

Without a volume, all data is lost when the container stops.

### What Gets Saved

- **Ban list** — persists username and IP bans
- **Session logs** — one timestamped log file per container run

### Ban List Format

Location: `ban_list.txt` inside the data directory.

```
YuzuRoom-BanList-1
BadUsername1
BadUsername2

192.168.1.100
10.0.0.50
```

Format:
1. First line: header (required — do not modify)
2. Banned usernames, one per line
3. Blank line separator
4. Banned IP addresses, one per line

### Log Files

- **Live output:** `docker logs <container-name>`
- **Session logs:** `session_DD-MM-YYYY_HH-MM-SS.log` in the data directory

---

## Troubleshooting

**Room shows in the lobby but nobody can connect**
Port 24872 (TCP and UDP) is not reachable from the internet. Check your
router's port forwarding rules and any firewall (UFW, iptables, cloud security
groups) blocking inbound traffic on that port.

**Room does not appear in the lobby**
- Confirm `USERNAME`, `TOKEN`, and `WEB_API_URL` are all set.
- Check `docker logs <container-name>` for `WrongContent` or `WebResult` errors
  from the announce thread — these usually mean a bad token or unreachable API.
- The room announces every 15 seconds; allow up to 30 seconds after startup.

**Players keep disconnecting on long-distance connections**
This image extends ENet's peer timeout to 12 s / 60 s (patch 14) for high-RTT
paths like AUS↔USA. If you still see drops, check the `STAT` line in the log —
`loss > 1%` at disconnect indicates network-level packet loss outside the
server's control.

**Token rejected / cannot authenticate**
Tokens are UUID format. Confirm you copied it correctly from the Eden client's
network settings with no extra whitespace.

---

## Moderator Setup

Set `USERNAME` to your Eden username. When you join the room you automatically
receive moderator privileges via:

- **Internet connections** — JWT verification against your token
- **LAN / direct connections** — nickname matching (patch 6)

```text
[10:23:45] User YourName is a moderator
[10:23:45] JOIN  | [192.168.1.100] YourName has joined. (1/16)
```

---

## Patches Included

<details>
<summary>17 patches — click to expand</summary>

Built from a pinned Eden source commit. GitHub Actions rebuilds the image
whenever the upstream Eden HEAD changes. `.last_eden_commit` records the commit
that CI last built. The Dockerfile `EDEN_REF` value is a fallback for manual
builds.

Patch application fails loudly if Eden moves any source block this image
depends on, so Docker and CI never silently produce an unpatched binary.

### Stability

| # | Issue | Fix |
|---|-------|-----|
| 1 | Container hangs or skips cleanup on `docker stop` | Signal-aware shutdown loop replaces the blocking stdin read |
| 2 | Crash on malformed or empty lobby API response | JSON error handling with explicit `WebResult` failure return |
| 3 | Silent thread crash terminates the process | Exception wrapper on the announce jthread logs failures instead of crashing |
| 4 | Crash when `--username` is passed without a value | Changed from `optional_argument` to `required_argument` |
| 5 | Data race on JWT public-key cache under concurrent requests | Mutex protection on the static key cache |
| 14 | Players dropped prematurely on high-RTT paths (AUS↔USA ~170 ms) | `enet_peer_timeout` raised to 12 000 / 60 000 ms on join |

### Features

| # | Issue | Fix |
|---|-------|-----|
| 6 | Host moderator powers fail when JWT user data is absent (LAN) | Host nickname matched as fallback when JWT data is absent |
| 7 | Noisy JWT error logs for unauthenticated clients | Common unauthenticated error suppressed at INFO level |
| 8 | Unknown fake-IP errors spam the log | Moved to DEBUG level |
| 9 | LDN / proxy packet loss for unrecognised fake IPs | Configurable broadcast fallback (`EDEN_ROOM_UNKNOWN_IP_FALLBACK`) |

### Security

| # | Issue | Fix |
|---|-------|-----|
| 10 | Empty room packet causes out-of-bounds read on `data[0]` | Empty and null packets dropped before dispatch |
| 11 | Malformed proxy / LDN packets bypass header validation | Minimum header size checks and parsed-packet state validation |
| 12 | Join request flooding from a single IP | Per-IP rate limiting with stale-entry pruning |
| 13 | Room member count read outside `member_mutex` | Member count serialised under the lock when broadcasting room info |

### Latency & Observability

| # | Issue | Fix |
|---|-------|-----|
| 15 | No RTT visibility at join | `PING` label logs each peer's measured round-trip time on connect |
| 16 | Head-of-line blocking stalls game relay on lossy paths | Relay packets changed `RELIABLE` → `UNSEQUENCED` to match real Switch LDN transport; control packets remain `RELIABLE` |
| 17 | No per-session disconnect diagnostics | `STAT` label logs final RTT, packet loss, and data volume before every `LEAVE` |

### Compatibility Guardrails

These patches do **not** change ENet channel count, flush cadence, packet
payload bytes, or `network_version` rejection. The only relay-level change is
`ENET_PACKET_FLAG_RELIABLE` → `ENET_PACKET_FLAG_UNSEQUENCED` for game data
packets, matching the real Switch LDN transport (raw 802.11 UDP). Games that
rely on server-side ordering for their own internal LDN messages may see
regressions — test each title before deploying to a community server.

For full patch rationale see [PATCHES.md](PATCHES.md).

</details>

---

## Building from Source

```bash
git clone https://github.com/Crunch41/eden-room-docker.git
cd eden-room-docker
docker build -t eden-room-server .
```

To build against a specific Eden commit:

```bash
docker build --build-arg EDEN_REF=<commit-sha> -t eden-room-server .
```

Build time is approximately 30–60 minutes (compiles the full Eden codebase).

---

## Credits

- [Eden Emulator Team](https://git.eden-emu.dev/eden-emu/eden)
- Community lobby server by [simvux](https://github.com/simvux/room-reg-impl)
- Docker packaging and patches by [Crunch41](https://github.com/Crunch41)
