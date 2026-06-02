# Eden Room Server - Docker

Dockerized Eden dedicated room server with reproducible builds, Docker-friendly
shutdown, and low-risk multiplayer room hardening patches.

## Quick Start

### Private Room (LAN/Friends Only)

```bash
docker run -d -p 24872:24872/tcp -p 24872:24872/udp \
  -e ROOM_NAME="My Room" \
  -e PREFERRED_GAME="Super Smash Bros" \
  crunch41/eden-room-server
```

### Public Room (Listed in Lobby)

```bash
docker run -d -p 24872:24872/tcp -p 24872:24872/udp \
  -e ROOM_NAME="My Public Room" \
  -e PREFERRED_GAME="Super Smash Bros" \
  -e USERNAME="your_username" \
  -e TOKEN="your-token" \
  -e WEB_API_URL="https://api.ynet-fun.xyz" \
  crunch41/eden-room-server
```

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
| `USERNAME` | Your Eden username (required for public rooms) |
| `TOKEN` | Your authentication token (UUID format) |
| `WEB_API_URL` | Lobby API URL (e.g., `https://api.ynet-fun.xyz`) |

### Optional Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `ROOM_DESCRIPTION` | (empty) | Room description |
| `PREFERRED_GAME_ID` | 0 | Game title ID in hex format |
| `MAX_MEMBERS` | 16 | Maximum players (2-254) |
| `PASSWORD` | (empty) | Room password |
| `BIND_ADDRESS` | 0.0.0.0 | Network interface to bind |
| `PORT` | 24872 | Server port |
| `EDEN_ROOM_UNKNOWN_IP_FALLBACK` | broadcast | Use `broadcast` for unknown fake-IP LDN/proxy packets, or `drop` for strict testing |

### File Permissions (Unraid/NAS)

| Variable | Default | Description |
|----------|---------|-------------|
| `PUID` | 99 | User ID for file ownership |
| `PGID` | 100 | Group ID for file ownership |

### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_DIR` | /home/eden/.local/share/eden-room | Log directory |
| `MAX_LOG_FILES` | 10 | Number of session logs to keep |

Docker console output is mirrored to timestamped session logs, and old session
logs are rotated automatically. Common room activity is labeled for scanning:

```text
[10:23:45] JOIN  | [1.145.73.191] Jonathan has joined.
[10:23:46] GAME  | Jonathan is playing Mario Kart 8 Deluxe (3.0.3)
[10:24:10] CHAT  | Jonathan: yo
[10:27:57] LEAVE | [118.92.194.254] lilboat has left.
```

---

## Docker Compose

```yaml
version: '3.8'
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

Mount a volume to preserve data across container restarts:

```bash
-v /path/to/data:/home/eden/.local/share/eden-room
```

### What Gets Saved

- **Ban list** - Persists username and IP bans
- **Session logs** - Timestamped log files for each session

Without a volume mount, all data is lost on container restart.

### Ban List Format

Location: `ban_list.txt` in the data directory

```
YuzuRoom-BanList-1
BadUsername1
BadUsername2

192.168.1.100
10.0.0.50
```

Format:
1. First line: Header (required, do not modify)
2. Banned usernames (one per line)
3. Empty line separator
4. Banned IP addresses (one per line)

### Log Files

- **Console output**: `docker logs <container-name>`
- **Session logs**: `session_DD-MM-YYYY_HH-MM-SS.log`

Logs use compact room activity labels and automatic rotation that keeps the most
recent sessions. Warning and error lines keep class and level metadata for
diagnostics.

---

## Bug Fixes Included

This image builds from a pinned Eden source commit and applies low-risk patches
from `scripts/apply-eden-room-patches.py`.

GitHub Actions overrides the Dockerfile fallback pin with the latest upstream
Eden commit whenever scheduled checks detect relevant room or networking
changes. `.last_eden_commit` records the latest upstream commit that CI actually
built.

### Stability Fixes

| # | Issue | Fix |
|---|-------|-----|
| 1 | Container hangs or skips cleanup | Signal-aware non-interactive shutdown loop |
| 2 | Crash on malformed API response | Added JSON error handling |
| 3 | Silent thread crashes | Added exception wrapper to announce loop |
| 4 | Crash with `--username` flag | Changed to `required_argument` |
| 5 | Data race in JWT key fetch | Added mutex protection |

### Feature Fixes

| # | Issue | Fix |
|---|-------|-----|
| 6 | Host moderator powers can fail without JWT user data | Check host nickname as fallback |
| 7 | Noisy JWT error logs | Suppress common unauthenticated-client error |
| 8 | Spam from unknown IP errors | Moved to DEBUG level |
| 9 | LDN/proxy packet loss for unknown fake IPs | Configurable broadcast fallback |

### Security Patches

| # | Issue | Fix |
|---|-------|-----|
| 11 | Empty room packets can read `data[0]` | Drop empty/null packets before dispatch |
| 12 | Malformed packet parsing | Validate parsed packet state and proxy/LDN header sizes |
| 13 | Join request flooding | Rate limiting per IP with pruning |
| 14 | Room member count race | Serialize member count under the member lock |

### Compatibility Guardrails

The room server relays opaque Eden proxy/LDN packet envelopes. To avoid breaking
specific games, this image does not change ENet channel count, reliable packet
flags, flush cadence, packet payload bytes, or strict `network_version`
rejection.

For technical details, see [PATCHES.md](PATCHES.md).

---

## Moderator Setup

### Automatic Moderator Powers

Set `USERNAME` to your Eden username. When you join the room, you automatically receive moderator privileges.

This works on both:
- **Internet connections** - Via JWT verification
- **LAN connections** - Via nickname matching (Patch #7)

### Log Output

```text
[10:23:45] User YourName is a moderator
[10:23:45] JOIN  | [192.168.1.100] YourName has joined.
```

---

## Building from Source

```bash
git clone https://github.com/Crunch41/eden-room-docker.git
cd eden-room-docker
docker build -t eden-room-server .
```

To build a specific Eden commit:

```bash
docker build --build-arg EDEN_REF=<commit-sha> -t eden-room-server .
```

If `EDEN_REF` is not supplied, Docker uses the fallback ref in the Dockerfile.
CI supplies `EDEN_REF` explicitly.

Build time is approximately 30-60 minutes (compiles the full Eden codebase).

---

## Credits

- [Eden Emulator Team](https://git.eden-emu.dev/eden-emu/eden)
- Original Citron patches and Docker packaging by [Crunch41](https://github.com/Crunch41)
