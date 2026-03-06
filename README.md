# Eden Room Server - Docker

Dockerized Eden dedicated room server with critical bug fixes and security patches.

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

Logs use human-readable timestamps (`[HH:MM:SS]` format) and automatically rotate, keeping the most recent sessions.

---

## Bug Fixes Included

This image includes 15 patches that address critical issues in the vanilla Eden room server.
(Ported and adapted from [citron-room-docker](https://github.com/Crunch41/citron-room-docker))

### Stability Fixes

| # | Issue | Fix |
|---|-------|-----|
| 1 | Container hangs on startup | Removed stdin blocking loop |
| 3 | Crash on malformed API response | Added JSON error handling |
| 4 | Silent thread crashes | Added exception wrapper to announce loop |
| 5 | Crash with `--username` flag | Changed to required_argument |

### Feature Fixes

| # | Issue | Fix |
|---|-------|-----|
| 6 | No visibility of moderator joins | Added logging |
| 7 | Moderator powers fail on LAN | Check nickname when JWT fails |
| 8 | Noisy JWT error logs | Suppress common error code 2 |
| 9 | Spam from unknown IP errors | Moved to DEBUG level |
| 10 | LDN packet loss | Added broadcast fallback |

### Security Patches

| # | Issue | Fix |
|---|-------|-----|
| 11 | Server crash from bad packets | Added main loop exception handling |
| 12 | Join request flooding | Rate limiting per IP |
| 13 | Thread safety | Documented lock ordering |
| 14 | Data race in JWT key fetch | Added mutex protection |
| 15 | Buffer overread from small packets | Added size validation |
| 17 | Unreadable log format | Human-readable timestamps |

For technical details, see [PATCHES.md](PATCHES.md).

---

## Moderator Setup

### Automatic Moderator Powers

Set `USERNAME` to your Eden username. When you join the room, you automatically receive moderator privileges.

This works on both:
- **Internet connections** - Via JWT verification
- **LAN connections** - Via nickname matching (Patch #7)

### Log Output

```
[10:23:50] [192.168.1.100] YourName has joined.
[10:23:50] User 'YourName' (YourName) joined as MODERATOR
```

---

## Building from Source

```bash
git clone https://github.com/Crunch41/eden-room-docker.git
cd eden-room-docker
docker build -t eden-room-server .
```

Build time is approximately 30-60 minutes (compiles the full Eden codebase).

---

## Credits

- [Eden Emulator Team](https://git.eden-emu.dev/eden-emu/eden)
- Original Citron patches and Docker packaging by [Crunch41](https://github.com/Crunch41)
