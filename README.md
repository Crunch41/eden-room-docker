# Eden room server

[![Build](https://github.com/Crunch41/eden-room-docker/actions/workflows/build.yml/badge.svg)](https://github.com/Crunch41/eden-room-docker/actions/workflows/build.yml)

A multiplayer room for the [Eden](https://git.eden-emu.dev/eden-emu/eden) Switch emulator, in a small Docker image. It is Eden's own standalone room server, built every week from Eden's `master` with [six patches](#whats-different-from-upstream) for running a room on the internet: it stays listed in the public lobby, stops cleanly, refuses bad packets and uninvited peers, relays game traffic with less lag, and logs who joined, played and left.

```
docker pull crunch41/eden-room-server
```

Any Eden client with the same network version joins as it would join any Eden room. Nothing changes on the players' side.

## Quick start

```yaml
services:
  eden-room:
    image: crunch41/eden-room-server:latest
    restart: unless-stopped
    ports:
      - 24872:24872/udp
    volumes:
      - ./eden-room:/home/eden/.local/share/eden-room
    environment:
      - ROOM_NAME=My Room
      - MAX_MEMBERS=8
      - TZ=Australia/Sydney
      # For a public room in the Eden lobby, set all three:
      # - USERNAME=your-eden-username
      # - TOKEN=your-token             # Eden → Settings → Multiplayer
      # - WEB_API_URL=https://api.ynet-fun.xyz
```

The room uses UDP only. Forward the port on your router to the host, and publish it directly: a reverse proxy or port forwarder that rewrites source addresses breaks [moderator rights](#troubleshooting).

The volume keeps the ban list and one log file per start (the oldest are deleted beyond `MAX_LOG_FILES`). `docker logs` shows the same output.

### Unraid

**Docker → Add Container**, then:

| Field | Value |
|---|---|
| Repository | `crunch41/eden-room-server:latest` |
| Network type | `bridge` |
| Port | container `24872`, host `24872`, connection type **UDP** |
| Path | container `/home/eden/.local/share/eden-room`, host `/mnt/user/appdata/eden-room` |
| Variables | the [settings](#settings) you need, at least `ROOM_NAME` and `TZ` |

The image's default `PUID` 99 and `PGID` 100 are Unraid's `nobody:users`, so appdata permissions work as is.

## Settings

| Variable | Default | |
|---|---|---|
| `ROOM_NAME` | `Eden Room` | Name shown in the lobby |
| `ROOM_DESCRIPTION` | | Optional description |
| `PREFERRED_GAME` | `Any Game` | Game shown in the lobby |
| `PREFERRED_GAME_ID` | `0` | That game's 16-digit hex title ID, or `0` |
| `MAX_MEMBERS` | `16` | Players, 2–254 |
| `PASSWORD` | | Leave empty for an open room |
| `PORT` | `24872` | UDP port; publish the same one |
| `BIND_ADDRESS` | `0.0.0.0` | Interface to listen on |
| `TZ` | `UTC` | Time zone for log times |

**Public lobby.** Set all three, or the room stays private:

| Variable | |
|---|---|
| `USERNAME` | Your Eden account name |
| `TOKEN` | Your token, from Eden → Settings → Multiplayer |
| `WEB_API_URL` | The lobby service, e.g. `https://api.ynet-fun.xyz` |

**Files and user:**

| Variable | Default | |
|---|---|---|
| `PUID`, `PGID` | `99`, `100` | User and group the room runs as |
| `BAN_LIST_FILE` | `<volume>/ban_list.txt` | Ban list |
| `LOG_DIR`, `MAX_LOG_FILES` | `<volume>`, `10` | Session logs kept |

**Tuning.** The defaults suit internet play; most rooms never change these:

| Variable | Default | |
|---|---|---|
| `EDEN_ROOM_MOD_USERNAME` | `USERNAME` | Who moderates (kick, ban), by Eden username or nickname. Only from your local network |
| `EDEN_ROOM_RELAY_MODE` | `unsequenced` | How game packets are relayed: `unsequenced`, `sequenced`, or `reliable` (upstream's; laggy over the internet) |
| `EDEN_ROOM_UNKNOWN_IP_FALLBACK` | `broadcast` | Packets to a player the room doesn't know yet go to everyone; `drop` restores upstream |
| `EDEN_ROOM_PEER_TIMEOUT_MIN` / `_MAX` | `12000` / `60000` | How long (ms) a silent player is kept before being dropped |
| `EDEN_ROOM_PING_INTERVAL` | `100` | How often (ms) players are pinged |
| `EDEN_ROOM_RELAY_BUDGET_KBPS` | `0` (off) | Cap on what one player may relay, in KB/s. Too low drops real game traffic |
| `EDEN_ROOM_DIAG_INTERVAL_SEC` | `0` (off) | Seconds between `DIAG` reports; 10–30 is useful when chasing lag |

## What's different from upstream

Each patch's file explains its change in full.

1. **[Lobby web service in the static build](https://github.com/Crunch41/eden-room-docker/blob/main/patches/001-build-the-static-room-with-the-lobby-web-service.patch).** Upstream's lean room-only build leaves out the lobby, so the room could never be public.
2. **[Container service](https://github.com/Crunch41/eden-room-docker/blob/main/patches/002-run-the-room-as-a-container-service.patch).** `docker stop` shuts the room down properly (ban list saved, lobby told) instead of killing it. Logs use local time, are flushed line by line, and can't deadlock when the disk is full.
3. **[Public lobby fixes](https://github.com/Crunch41/eden-room-docker/blob/main/patches/003-fix-and-harden-the-public-lobby-announce.patch).** Since Eden's November 2026 rewrite of its web code, a room dropped out of the lobby the first time someone joined and never came back. Players are sent in the lobby's format again, and the room registers again whenever the lobby has dropped it. A stopped room also unregisters, and stopping no longer crashes it.
4. **[Packet validation and admission](https://github.com/Crunch41/eden-room-docker/blob/main/patches/004-validate-packets-and-enforce-room-admission.patch).** Malformed and oversized packets are dropped, only players who passed the password and ban checks can send game traffic, rejected clients are released at once, and joins are rate-limited. Moderator rights need a local-network address, so nobody can moderate your room by borrowing your name.
5. **[Relay for internet play](https://github.com/Crunch41/eden-room-docker/blob/main/patches/005-relay-game-traffic-for-internet-play.patch).** Game packets go out at once and unsequenced, so one lost packet no longer freezes everyone behind it. Session setup stays reliable, timeouts suit long routes, and packets to players the room hasn't placed yet are delivered instead of dropped.
6. **[Activity log and diagnostics](https://github.com/Crunch41/eden-room-docker/blob/main/patches/006-log-room-activity-and-transport-diagnostics.patch).** Labelled join, leave, chat and game lines with player counts, a per-player summary when they leave, and optional `DIAG` reports on lag.

The room protocol is unchanged: one ENet channel, game data relayed byte for byte, and upstream's `network_version` check.

## Logs

```
[10:23:45] JOIN  | [1.2.3.4] PlayerName has joined. (1/16)
[10:23:45] PING  | [1.2.3.4] PlayerName RTT 172ms
[10:23:46] GAME  | PlayerName is playing Mario Kart 8 Deluxe (3.0.3)
[10:24:10] CHAT  | PlayerName: gg
[10:27:57] STAT  | [1.2.3.4] PlayerName session join_rtt 172ms last_rtt 180ms peak_rtt 210ms game_pkts 41800 up_holds 12 (2950ms, max 260ms) gaps 14 (max 410ms, 2 unheld) burst_max 46 duration 4m12s
[10:27:57] LEAVE | [1.2.3.4] PlayerName has left. (0/16)
[10:28:01] Network <Warning> Dropping malformed LDN packet (3 bytes)
```

`STAT` (when a player leaves) and `DIAG` describe each player's game packets on their way to the room:

| Field | Meaning |
|---|---|
| `up_holds` | Times a packet from this player was lost and everything after it waited for the resend (Eden clients send reliably). Others see the player freeze, then jump. This is the player's own connection: wired beats Wi-Fi |
| `gaps … unheld` | Pauses of 100 ms–5 s with no loss to explain them: the player's PC or emulator stalled (shader compiles, frame drops) |
| `burst_max` | Most packets that arrived back to back; a hold or a stall ends in a burst |

The room can't see packets lost on the way *to* players, or anything inside the game. `DIAG` reports (`EDEN_ROOM_DIAG_INTERVAL_SEC`) add round-trip times, relay counts, drops by reason, a line per player while games run, and an advice line naming who is losing packets or stalling.

## Troubleshooting

**The room isn't in the public lobby.** Check that the start banner says `Mode: Public`; otherwise one of `USERNAME`, `TOKEN` or `WEB_API_URL` is missing. Then look for `WebService <Error>` lines:

- A single `404` followed by `Room has been registered` is normal: the lobby forgot the room (for example after a restart) and the room registered again.
- A `404` or `422` every 15 seconds that never recovers is the November 2026 lobby bug. Pull the current image.
- `Connection timed out` or `returned null` means the room can't reach the lobby. Check outbound HTTPS from the host.

**Players can't connect.** The port must be published and forwarded as **UDP**, and `PORT` must match the published port. A host behind carrier-grade NAT can't take incoming connections at all.

**You aren't a moderator.** Join from your local network (10.x, 172.16–31.x, 192.168.x) with the name in `EDEN_ROOM_MOD_USERNAME` (or `USERNAME`). If everyone shows up with a local address, a proxy is rewriting addresses: publish the port directly.

**Players freeze or teleport.** Set `EDEN_ROOM_DIAG_INTERVAL_SEC=15` and watch the `DIAG advice` lines. `up_holds` point at that player's connection; `unheld` gaps point at their PC.

**"Version mismatch" on join.** The client's network version differs from the room's. `docker exec <container> eden-room --version` shows which Eden commit the room was built from.

## Tags and updates

- `latest`: the newest build of Eden's `master`.
- `<eden-commit>-<repo-commit>`, for example `cf654052c4-1a2b3c4`: one exact build, for pinning.

A new image is built every Monday (11 am Sydney time) when Eden, the base image or this repository changed, and at least weekly for Ubuntu security updates. Every build runs the full protocol test suite first; a failed build leaves the last good image published.

Building it yourself, changing a patch and how CI works: [docs/maintaining.md](https://github.com/Crunch41/eden-room-docker/blob/main/docs/maintaining.md).
