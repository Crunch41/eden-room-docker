# Eden room server, patched

[![Build](https://github.com/Crunch41/eden-room-docker/actions/workflows/build.yml/badge.svg)](https://github.com/Crunch41/eden-room-docker/actions/workflows/build.yml)

A multiplayer room for the [Eden](https://git.eden-emu.dev/eden-emu/eden) Switch emulator, in a small Docker image. It is Eden's own standalone room server, built every week from Eden's `master` with [six patches](#what-the-patches-change) for running a room on the internet: it stays listed in the public lobby, stops cleanly, refuses bad packets and uninvited peers, relays game traffic with less lag, and logs who joined, played and left.

```
docker pull crunch41/eden-room-server
```

Any Eden client with the same network version joins as it would join any Eden room. Nothing changes on the players' side.

## What the patches change

Each patch's file explains its change in full.

1. **[Lobby web service in the static build](https://github.com/Crunch41/eden-room-docker/blob/main/patches/001-build-the-static-room-with-the-lobby-web-service.patch).** Upstream's lean room-only build leaves out the lobby, so the room could never be public.
2. **[Container service](https://github.com/Crunch41/eden-room-docker/blob/main/patches/002-run-the-room-as-a-container-service.patch).** `docker stop` shuts the room down properly (ban list saved, lobby told) instead of killing it. Logs use local time, are flushed line by line, and can't deadlock when the disk is full.
3. **[Public lobby fixes](https://github.com/Crunch41/eden-room-docker/blob/main/patches/003-fix-and-harden-the-public-lobby-announce.patch).** Since Eden's November 2026 rewrite of its web code, a room the lobby had dropped never came back. The room registers again whenever the lobby has dropped it, a broken lobby reply is reported instead of trusted, and stopping no longer crashes the room or outlasts `docker stop`.
4. **[Packet validation and admission](https://github.com/Crunch41/eden-room-docker/blob/main/patches/004-validate-packets-and-enforce-room-admission.patch).** Malformed and oversized packets are dropped, only players who passed the password and ban checks can send game traffic, rejected clients are released at once, and joins are rate-limited. Moderator rights need a local-network address, so nobody can moderate your room by borrowing your name.
5. **[Relay for internet play](https://github.com/Crunch41/eden-room-docker/blob/main/patches/005-relay-game-traffic-for-internet-play.patch).** Game packets go out at once and unsequenced, so one lost packet no longer freezes everyone behind it. Session setup stays reliable, timeouts suit long routes, and packets to players the room hasn't placed yet are delivered instead of dropped.
6. **[Activity log and diagnostics](https://github.com/Crunch41/eden-room-docker/blob/main/patches/006-log-room-activity-and-transport-diagnostics.patch).** Labelled join, leave, kick, ban, chat and game lines with player counts, a one-line summary of each session with a connection rating when a player leaves, a sessions file for stats tools, and optional `DIAG` reports on lag.
7. **[Room-open message](https://github.com/Crunch41/eden-room-docker/blob/main/patches/007-say-how-to-stop-the-room-in-the-room-open-log-line.patch).** The startup log says to stop the process to close the room, not to press Q+Enter.

The room protocol is unchanged: one ENet channel, game data relayed byte for byte, and upstream's `network_version` check.

| Patch | Upstream files |
|---|---|
| 001 | `CMakeLists.txt`, `src/CMakeLists.txt` |
| 002 | `src/dedicated_room/yuzu_room.cpp`, `src/common/logging.cpp` |
| 003 | `src/web_service/*`, `src/network/announce_multiplayer_session.cpp` |
| 004 | `src/network/room.cpp` |
| 005 | `src/network/room.cpp` |
| 006 | `src/network/room.cpp`, `src/common/logging.cpp` |

## Use the image

```yaml
services:
  eden-room:
    image: crunch41/eden-room-server:latest
    restart: unless-stopped
    ports:
      - 24872:24872/udp
      - 8080:8080/tcp   # the stats page; keep it on your own network
    volumes:
      - ./eden-room:/home/eden/.local/share/eden-room
    environment:
      - ROOM_NAME=My Room
      - MAX_MEMBERS=8
      - TZ=Australia/Sydney
      # For a public room in the Eden lobby, set both:
      # - USERNAME=your-eden-username
      # - WEB_API_URL=https://api.ynet-fun.xyz
```

The room uses UDP only; TCP 8080 is the [stats page](#stats-page), for your own network. Forward the room's port on your router to the host, and publish it directly: a reverse proxy or port forwarder that rewrites source addresses breaks [moderator rights](#troubleshooting).

The volume keeps the ban list, `sessions.jsonl` (see [Logs](#logs)) and one log file per start, up to `MAX_LOG_FILES` (the oldest are deleted first). `docker logs` shows the same output in colour, without the `STAT` lines.

### Unraid

**Docker → Add Container**, then:

| Field | Value |
|---|---|
| Repository | `crunch41/eden-room-server:latest` |
| Network type | `bridge` |
| Port | container `24872`, host `24872`, connection type **UDP** |
| Port | container `8080`, host `8080`, connection type **TCP** (the stats page), and **WebUI** `http://[IP]:[PORT:8080]/` |
| Path | container `/home/eden/.local/share/eden-room`, host `/mnt/user/appdata/eden-room` |
| Variables | the [settings](#settings) you need, at least `ROOM_NAME` and `TZ` |

The image's default `PUID` 99 and `PGID` 100 are Unraid's `nobody:users`, so appdata permissions work as is.

### Settings

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

**Public lobby.** Set both, or the room stays private:

| Variable | |
|---|---|
| `USERNAME` | Your Eden account name |
| `WEB_API_URL` | The lobby service, e.g. `https://api.ynet-fun.xyz` |

Eden dropped lobby tokens in November 2026, so `TOKEN` is no longer needed; a `TOKEN` left in an existing setup is ignored.

**Files and user:**

| Variable | Default | |
|---|---|---|
| `PUID`, `PGID` | `99`, `100` | User and group the room runs as |
| `BAN_LIST_FILE` | `<volume>/ban_list.txt` | Ban list |
| `LOG_DIR`, `MAX_LOG_FILES` | `<volume>`, `10` | Session logs kept, including the current one |
| `EDEN_ROOM_SESSIONS_FILE` | `<volume>/sessions.jsonl` | One JSON line per finished session |
| `NO_COLOR` | | Set to anything to log without colour |
| `STATS_PORT` | `8080` | TCP port of the [stats page](#stats-page); `0` turns it off |
| `STATS_ALLOW` | | Who may load the stats page: IPv4 addresses or networks (`192.168.1.5`, `10.0.0.0/8`), space or comma separated. Empty: anyone who can reach the port |

**Tuning.** The defaults suit internet play; most rooms never change these:

| Variable | Default | |
|---|---|---|
| `EDEN_ROOM_MOD_USERNAME` | | Who else moderates (kick, ban) besides `USERNAME`, by nickname. Only from your local network |
| `EDEN_ROOM_RELAY_MODE` | `unsequenced` | How game packets are relayed: `unsequenced`, `sequenced`, or `reliable` (upstream's; laggy over the internet) |
| `EDEN_ROOM_UNKNOWN_IP_FALLBACK` | `broadcast` | Packets to a player the room doesn't know yet go to everyone; `drop` restores upstream |
| `EDEN_ROOM_PEER_TIMEOUT_MIN` / `_MAX` | `12000` / `60000` | How long (ms) a silent player is kept before being dropped |
| `EDEN_ROOM_PING_INTERVAL` | `100` | How often (ms) players are pinged |
| `EDEN_ROOM_RELAY_BUDGET_KBPS` | `0` (off) | Cap on what one player may relay, in KB/s. Too low drops real game traffic |
| `EDEN_ROOM_DIAG_INTERVAL_SEC` | `0` (off) | Seconds between `DIAG` reports; 10–30 is useful when chasing lag. Also shows `STAT` lines in `docker logs` |

### Logs

```
[10:23:45] JOIN   PlayerName joined  1/16 · 172ms · 1.2.3.4
[10:23:46] GAME   PlayerName is playing Mario Kart 8 Deluxe (3.0.3)
[10:24:10] CHAT   PlayerName: gg
[10:27:57] LEAVE  PlayerName left after 4m12s · played Mario Kart 8 Deluxe · ping 180ms · some lag spikes  0/16
[10:28:01] Network <Warning> Dropping malformed LDN packet (3 bytes)
[10:31:02] JOIN   Visitor joined  1/16 · 220ms · 5.6.7.8
[10:31:02] LEAVE  Visitor left after 0s, didn't play  0/16
──────────────────── Tue 24 Nov 2026 ────────────────────
```

`docker logs` (and Unraid's log window) colour the labels and grey out visits where nobody played. A line marks each new day, since log lines only carry the time.

The `LEAVE` line rates the player's connection to the room from the `up_holds` below, for sessions of a minute or more: **laggy connection** when held 1% of the time or 3 times a minute, **some lag spikes** when held once a minute or once for half a second, **connection good** otherwise. A player who sent fewer than 100 game packets "didn't play": lobby probes and games' network scans.

The session log file also has a `STAT` line before each `LEAVE` of a player who sent game packets, with the raw figures:

```
[10:27:57] STAT   [1.2.3.4] PlayerName session join_rtt 172ms last_rtt 180ms peak_rtt 210ms game_pkts 41800 up_holds 12 (2950ms, max 260ms) gaps 14 (max 410ms, 2 unheld) burst_max 46 duration 4m12s
```

`STAT` and `DIAG` describe each player's game packets on their way to the room:

| Field | Meaning |
|---|---|
| `up_holds` | Times a packet from this player was lost and everything after it waited for the resend (Eden clients send reliably). Others see the player freeze, then jump. This is the player's own connection: wired beats Wi-Fi |
| `gaps … unheld` | Pauses of 100 ms–5 s with no loss to explain them: the player's PC or emulator stalled (shader compiles, frame drops) |
| `burst_max` | Most packets that arrived back to back; a hold or a stall ends in a burst |

The room can't see packets lost on the way *to* players, or anything inside the game. `DIAG` reports (`EDEN_ROOM_DIAG_INTERVAL_SEC`) add round-trip times, relay counts, drops by reason, a line per player while games run, and an advice line naming who is losing packets or stalling.

**Sessions file.** `sessions.jsonl` in the volume gets one line per finished session, for stats tools (most active players, time per game, busy hours). It is kept across restarts and never trimmed:

```json
{"name":"PlayerName","ip":"1.2.3.4","joined":"2026-11-23T02:23:45Z","left":"2026-11-23T02:27:57Z","seconds":252,"ended":"left","played":true,"games":[{"name":"Mario Kart 8 Deluxe","id":"0100152000022000","seconds":250}],"ping_ms":180,"peak_ping_ms":210,"rating":"some lag spikes","game_packets":41800,"holds":12,"held_ms":2950,"longest_hold_ms":260,"gaps":14,"longest_gap_ms":410}
```

`ended` is `left`, `kicked`, `banned` or `room stopped` (still in the room when it stopped). Times are UTC. `rating` is empty for sessions too short to rate. Players are known only by the nickname they type.

### Stats page

`http://<host>:8080/` is a player dashboard over the sessions file: players, hours played, the most at once and connection ratings; the most active players with their time, sessions, ping, ratings and main game; time per game; when people play (average players online by day and hour); players per day; and recent sessions. Click a player for their sessions, games, ping per session and the addresses they used. Sessions show when each player joined and left. Visits where nobody played are hidden unless **show visits that didn't play** is ticked.

Players are known only by the nickname they type, and many keep the default (`Eden`). A name used from more than one network (the first two parts of an IPv4 address, the first three groups of an IPv6 one) is shown once per network, like `Eden · 189.45.x.x`; one person on a new network shows up twice.

Game icons come from the title ID in the sessions file and are loaded by the browser from tinfoil.media, or nlib.cc when that fails. The page counts everything in the browser and reloads the sessions file every minute.

It is served by BusyBox's `httpd`, a separate process at the lowest CPU and disk priority: it can't take time from the room, and if it fails the room carries on. It serves the page and `sessions.jsonl`, nothing else from the volume. The page shows player addresses, so keep it to your own network: don't forward its port, and set `STATS_ALLOW` to limit who it answers. Behind a reverse proxy, set `STATS_ALLOW` to the proxy's address so the proxy is the only way in, and let the proxy decide who gets through. For Caddy, a LAN-only site:

```
:8081 {
	@outside not remote_ip private_ranges
	respond @outside 403
	reverse_proxy http://<room address>:8080
}
```

`STATS_PORT=0` turns the page off.

### Troubleshooting

**The room isn't in the public lobby.** Check that the start banner says `Mode: Public`; otherwise `USERNAME` or `WEB_API_URL` is missing. Then look for `WebService <Error>` lines:

- A single `404` followed by `Room has been registered` is normal: the lobby forgot the room (for example after a restart) and the room registered again.
- A `404` or `422` every 15 seconds that never recovers is the November 2026 lobby bug. Pull the current image.
- `Connection timed out` or `returned null` means the room can't reach the lobby. Check outbound HTTPS from the host.

**Players can't connect.** The port must be published and forwarded as **UDP**, and `PORT` must match the published port. A host behind carrier-grade NAT can't take incoming connections at all.

**You aren't a moderator.** Join from your local network (10.x, 172.16–31.x, 192.168.x) with `USERNAME` or `EDEN_ROOM_MOD_USERNAME` as your nickname. If everyone shows up with a local address, a proxy is rewriting addresses: publish the port directly.

**Players freeze or teleport.** Set `EDEN_ROOM_DIAG_INTERVAL_SEC=15` and watch the `DIAG advice` lines. `up_holds` point at that player's connection; `unheld` gaps point at their PC.

**"Version mismatch" on join.** The client's network version differs from the room's. `docker exec <container> eden-room --version` shows which Eden commit the room was built from.

### Tags

- `latest`: the newest build of Eden's `master`.
- `<eden-commit>-<repo-commit>`, for example `0bba074040-1a2b3c4`: one exact build, for pinning.

## How updates work

[`build.yml`](https://github.com/Crunch41/eden-room-docker/blob/main/.github/workflows/build.yml) runs every Monday (Sydney time), on every push to `main`, and on demand (**Actions → Build Eden Room → Run workflow**, optionally with an upstream branch, tag or commit).

1. **Decide.** [`scripts/upstream.py`](https://github.com/Crunch41/eden-room-docker/blob/main/scripts/upstream.py) finds the tip of Eden's `master` and compares it, the base image and this repository with the labels on the published `latest` image. If nothing changed, the run stops, unless the image is a week old: it installs Ubuntu packages, so it is rebuilt weekly for their updates. A manual run with an upstream ref always builds that ref.
2. **Patch.** The patches are applied with a 3-way merge, so small upstream changes around them are absorbed. [`scripts/patch-watch.py`](https://github.com/Crunch41/eden-room-docker/blob/main/scripts/patch-watch.py) lists upstream commits that touched files a patch depends on.
3. **Build and test the room.** [`scripts/build-room.sh`](https://github.com/Crunch41/eden-room-docker/blob/main/scripts/build-room.sh) builds the static room (ccache and downloaded dependencies are cached between runs), and [`tests/test_room_protocol.py`](https://github.com/Crunch41/eden-room-docker/blob/main/tests/test_room_protocol.py) runs it against generated ENet clients and a stand-in lobby.
4. **Build and test the image.** The image this run built must start as the `eden` user, stop with exit code 0, and pass the protocol tests again (the image reaches the stand-in lobby through `host.docker.internal`).
5. **Publish.** Only from `main`: the image goes to Docker Hub with its tags, and this README becomes the Docker Hub description. The image's labels record the exact upstream commit, which is how the next run knows what was built. Nothing is committed back to this repository.

A manual run with an upstream ref publishes its pinning tag but never moves `latest`. A manual run on another branch builds and tests but publishes nothing.

When something goes wrong:

- **A patch no longer applies, or the build or tests fail:** an issue titled **Build failed** opens with the failed step and its output (for a patch, the conflicts and git's message), and the last good image stays published. The next successful build closes it. A run that GitHub doesn't start at all, for example because of a billing problem, can't open an issue.
- **Upstream touched code a patch depends on** (even if the patch still applies): an issue titled **Patch review needed** lists the upstream commits. Read the patch against them, and when it is still right, set its `reviewed_against` in `patch-watch.json`. A clean apply and green tests don't prove a patch still works: in November 2026 upstream's move to Jacinth and Hydrocarbon broke the lobby with every patch applying and every test passing, which is why the stand-in lobby now checks the update format and the re-registration after a 404.

`tests/test_room_protocol.py` runs the room over the real ENet protocol:

- admission, rejection, the join rate limit and moderator rights;
- packet validation and byte-exact relay, including to unknown addresses, and the delivery flags each kind of packet gets;
- the log labels, `STAT`, `DIAG` and the sessions file, including for a player behind a link that delays, loses and pauses their packets, and in the image the stats page (it serves the sessions file and nothing else from the volume);
- a clean `SIGTERM` stop, also of a public room while it unregisters from a stand-in lobby that takes 25 seconds to answer, with its ban list saved;
- lobby updates in the lobby's player format, re-registration after a 404, and requests without `Expect: 100-continue`;
- with `--binary`, a room whose log file can't be written.

The other tests cover the scripts: `tests/test_entrypoint.py` (exit status and signal handling), `tests/test_stats_page.py` (the stats page's script parses; needs Node.js), `tests/test_upstream.py` and `tests/test_patch_watch.py`.

## Changing a patch

You need Git, Bash and Python 3. Building needs Linux with CMake, Ninja, `patch`, Clang 19+ (with `clang-scan-deps`) or GCC 16+ (upstream uses C++20 modules) and the OpenSSL development files; the tests need `libenet7`. On Ubuntu 24.04: `clang-20 clang-tools-20 ninja-build patch libssl-dev libenet7`, with `CC=/usr/lib/llvm-20/bin/clang CXX=/usr/lib/llvm-20/bin/clang++`.

```sh
scripts/patches.sh prepare      # clone Eden into work/upstream and apply the patches as commits
# edit work/upstream with ordinary git: amend the patch's commit, or add a commit for a new patch
scripts/build-room.sh work/upstream build                       # build build/eden-room
python3 tests/test_room_protocol.py --binary build/eden-room    # the protocol tests
python3 tests/test_room_protocol.py --binary build/eden-room --stress 120   # optional: relay under churn
scripts/patches.sh export       # write the commits back to patches/
git diff patches/               # review, then commit and push
```

Each commit becomes one patch file; its subject becomes the file name. A new or renamed patch needs a matching entry in `patch-watch.json` (CI checks that every patch has one). If a patch stops applying, `prepare` stops at it with the conflicts marked: fix them, run `git -C work/upstream am --continue`, then `export`.

Conventions in the patches:

- Every `EDEN_ROOM_*` setting is a `const` member of `RoomImpl`, in the settings block, read once when the room is created (`EnvNumber`, `EnvString`).
- Room activity is logged as a Network Info message whose first word is a tag (`JOIN`, `LEAVE`, `KICK`, `BAN`, `CHAT`, `GAME`, `STAT`, `DIAG`); `FormatLogMessage` turns the tag into the label. A new kind of activity line needs its tag added to `ActivityTags` in `logging.cpp`, and its colour in `log_sink` in `docker-entrypoint.sh`.
- The image's defaults live in the `Dockerfile` and `docker-entrypoint.sh`, which validates every setting before starting the room.

## Repository layout

| Path | Purpose |
|---|---|
| `patches/` | The patch series |
| `upstream.json` | Which upstream to build |
| `Dockerfile` | The runtime image around the prebuilt static `eden-room` |
| `docker-entrypoint.sh` | Validates the settings, drops to `PUID`/`PGID`, writes session logs, starts the stats page and the room |
| `stats/index.html` | The stats page |
| `patch-watch.json` | Upstream files and keywords each patch depends on |
| `scripts/build-room.sh` | Build the static room from a patched checkout |
| `scripts/patches.sh` | Fetch upstream, apply, prepare for editing, export |
| `scripts/upstream.py` | Find the commit to build and decide whether to build |
| `scripts/patch-watch.py` | Flag upstream changes that may affect a patch |
| `scripts/issue.sh` | Open and close the CI issues |
| `scripts/check-shared.sh`, `scripts/shared.sha256` | Check the scripts shared with the other Crunch41 patch repositories are unchanged copies |
| `tests/` | The tests above |
| `LICENSE.txt` | Eden's GPL-3.0 licence, which the patches are under |

## Why `master` and a static build

Eden's last release is months older than `master`, and both speak the same room protocol (`network_version` 1), so the image follows `master` and any Eden client with that network version can join. The room is built with upstream's `YUZU_STATIC_ROOM` option: it configures nothing of the emulator, so the build is fast and immune to the emulator's own dependencies (Qt, SDL, FFmpeg, Vulkan), and the result needs no shared libraries. The image is plain Ubuntu with `gosu`, `tini`, CA certificates and time zones.
