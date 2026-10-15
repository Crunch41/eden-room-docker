# Eden room server, patched

[![Build](https://github.com/Crunch41/eden-room-docker/actions/workflows/build.yml/badge.svg)](https://github.com/Crunch41/eden-room-docker/actions/workflows/build.yml)

[Eden](https://git.eden-emu.dev/eden-emu/eden)'s standalone multiplayer room server, built from the tip of Eden's `master` with hardening, latency and logging patches, in a small non-root image.

```
crunch41/eden-room-server:latest
```

The room protocol is unchanged: Eden clients of the same network version join as they would join any Eden room. One ENet channel, the relayed game data byte for byte, and a strict `network_version` check, as upstream.

## What the patches change

**[001 · Build the static room with the lobby web service](patches/001-build-the-static-room-with-the-lobby-web-service.patch)**

Eden's `YUZU_STATIC_ROOM` build configures only the room and the libraries it links, which makes it fast and immune to the emulator's own dependencies (Qt, SDL, FFmpeg, Vulkan and more). Upstream turns the lobby web service off in that build, so the room could not appear in the public lobby. The patch keeps it.

**[002 · Run the dedicated room unattended](patches/002-run-the-dedicated-room-unattended.patch)**

The room waited for "Q+Enter" on stdin and ignored signals, so `docker stop` killed it before it stopped announcing, saved the ban list or closed the room. `SIGINT` and `SIGTERM` now run the normal shutdown. `--username` requires its value (it crashed on `--username name`).

**[003 · Harden lobby registration and JWT verification](patches/003-harden-lobby-registration-and-jwt-verification.patch)**

An empty or invalid reply from the lobby, or any exception in the announce thread, terminated the room; they are now logged as announce errors. The cached lobby public key is locked against concurrent joins, and the "Verification failed" line every player without a lobby token caused is gone (expired tokens and bad signatures are still logged).

**[004 · Validate room packets and bound relay traffic](patches/004-validate-room-packets-and-bound-relay-traffic.patch)**

Empty, truncated or unparsable packets are dropped instead of being read past their end, and an exception in a packet handler no longer ends the room. Relayed game packets are capped at 1536 bytes (real game traffic tops out around 1493), and `EDEN_ROOM_RELAY_BUDGET_KBPS` can limit how much one member may relay per second, since every relayed packet goes out to every other member.

**[005 · Require room admission and release rejected peers](patches/005-require-room-admission-and-release-rejected-peers.patch)**

A client could skip the join request (password, bans, capacity) and still relay game traffic; only admitted members can now. Rejected clients are disconnected as soon as they have their answer instead of holding a slot until they time out, join requests are limited to one per IP address per second, and a no-op disconnect is removed.

**[006 · Grant moderator rights only to local peers](patches/006-grant-moderator-rights-only-to-local-peers.patch)**

Upstream made anyone who chose the room owner's nickname a moderator, and lobby moderators moderated every room. Moderator rights now require a local address (10/8, 172.16/12, 192.168/16, 127/8). From there, the moderator is the user named by `EDEN_ROOM_MOD_USERNAME` (default: the lobby account in `USERNAME`), the host nickname, or a lobby moderator.

**[007 · Flush relayed packets promptly and lock less](patches/007-flush-relayed-packets-promptly-and-lock-less.patch)**

The room handles every queued network event and then flushes once, so packets relayed in a burst go out together instead of one per 5 ms service call. Relays take a shared lock on the member list, status messages no longer do socket I/O under the lock, the member count in room information is read under the lock with the list it describes, and the nickname regex is compiled once.

**[008 · Tune ENet delivery and timeouts for internet play](patches/008-tune-enet-delivery-and-timeouts-for-internet-play.patch)**

`EDEN_ROOM_RELAY_MODE` chooses how game packets are relayed: `reliable` (upstream, what the image uses), `sequenced` or `unsequenced`. ENet's packet throttle is kept at 100% so the unreliable modes don't lose game packets to it, peers time out after 12–60 s instead of 5–30 s, and pings go out every 100 ms instead of 500 ms (all adjustable, see below).

**[009 · Broadcast relay packets for unknown fake IPs](patches/009-broadcast-relay-packets-for-unknown-fake-ips.patch)**

A game packet addressed to a member address the room doesn't know yet (common while a session forms) was dropped with an error. It now goes to every other member, like a broadcast; `EDEN_ROOM_UNKNOWN_IP_FALLBACK=drop` restores upstream.

**[010 · Log room activity and transport diagnostics](patches/010-log-room-activity-and-transport-diagnostics.patch)**

Log lines get the local time and a label instead of uptime, source locations and colour codes, and are flushed line by line:

```
[10:23:45] JOIN  | [1.2.3.4] PlayerName has joined. (1/16)
[10:23:45] PING  | [1.2.3.4] PlayerName RTT 172ms
[10:23:46] GAME  | PlayerName is playing Mario Kart 8 Deluxe (3.0.3)
[10:24:10] CHAT  | PlayerName: gg
[10:27:57] STAT  | [1.2.3.4] PlayerName session join_rtt 172ms last_rtt 180ms peak_rtt 210ms last_loss 0.10% peak_loss 0.50% duration 4m12s
[10:27:57] LEAVE | [1.2.3.4] PlayerName has left. (0/16)
[10:28:01] Network <Warning> Dropping malformed LDN packet (3 bytes)
```

`EDEN_ROOM_DIAG_INTERVAL_SEC` (off by default) adds periodic `DIAG` reports: RTT and loss per member, relayed packets and bytes, drops by reason, packet sizes and a short reading of them. They describe the network path only; the room cannot see game state, and while nobody is playing the numbers come from control traffic.

## Use the image

```yaml
services:
  eden-room:
    image: crunch41/eden-room-server:latest
    environment:
      - ROOM_NAME=My Room
      - PREFERRED_GAME=Mario Kart 8 Deluxe
      - PREFERRED_GAME_ID=0100152000022000
      - MAX_MEMBERS=8
      - TZ=Australia/Sydney
      # For a public room in the Eden lobby, set all three:
      # - USERNAME=your-eden-username
      # - TOKEN=your-token           # Eden → Settings → Multiplayer
      # - WEB_API_URL=https://api.ynet-fun.xyz
    volumes:
      - /path/to/appdata/eden-room:/home/eden/.local/share/eden-room
    ports:
      - 24872:24872/udp
    restart: unless-stopped
```

The server runs as `PUID`/`PGID` (default 99/100) and keeps its ban list and session logs in the volume. Each start writes a new session log there; the oldest are deleted beyond `MAX_LOG_FILES`. `docker logs` shows the same output.

| Variable | Default | Meaning |
|---|---|---|
| `ROOM_NAME` | `Eden Room` | Room name in the lobby |
| `ROOM_DESCRIPTION` | | Optional description |
| `PORT` | `24872` | UDP port (1–65535); publish the same port |
| `MAX_MEMBERS` | `16` | Players (2–254) |
| `BIND_ADDRESS` | `0.0.0.0` | Interface to listen on |
| `PASSWORD` | | Room password; empty for an open room |
| `PREFERRED_GAME` / `PREFERRED_GAME_ID` | `Any Game` / `0` | Game shown in the lobby; the ID is the 16-digit hex title ID |
| `USERNAME`, `TOKEN`, `WEB_API_URL` | | All three make the room public in the lobby |
| `BAN_LIST_FILE` | `<volume>/ban_list.txt` | Ban list |
| `LOG_DIR`, `MAX_LOG_FILES` | `<volume>`, `10` | Session logs |
| `PUID`, `PGID`, `TZ` | `99`, `100`, `UTC` | User, group and time zone for log times |

Tuning (patches 004–010); the defaults rarely need changing:

| Variable | Default | Meaning |
|---|---|---|
| `EDEN_ROOM_RELAY_MODE` | `reliable` | `reliable` (upstream), `sequenced` (try first if a game stutters) or `unsequenced` |
| `EDEN_ROOM_RELAY_RELIABLE` | `1` | Legacy: `1` means `reliable` when `EDEN_ROOM_RELAY_MODE` is empty. With both empty the room uses `unsequenced` |
| `EDEN_ROOM_PEER_TIMEOUT_MIN` / `_MAX` | `12000` / `60000` | ENet peer timeout in ms |
| `EDEN_ROOM_PING_INTERVAL` | `100` | ENet ping interval in ms |
| `EDEN_ROOM_UNKNOWN_IP_FALLBACK` | `broadcast` | `drop` restores upstream's handling of unknown addresses |
| `EDEN_ROOM_RELAY_BUDGET_KBPS` | `0` | Relay limit per member in KB/s; `0` is off. Too low drops real game traffic |
| `EDEN_ROOM_MOD_USERNAME` | `USERNAME` | Moderator's Eden username or nickname; local network only |
| `EDEN_ROOM_DIAG_INTERVAL_SEC` | `0` | Seconds between `DIAG` reports; `0` is off |

Tags: `latest`, the Eden commit (`aaa0bcb9bf`), and the commit plus this repository's commit (`aaa0bcb9bf-1a2b3c4`) for pinning.

## How updates work

[`build.yml`](.github/workflows/build.yml) runs every Sunday at 3:23 am Sydney time, on every push to `main` that changes the build, and on demand (Actions → Build Eden Room → Run workflow, optionally with an upstream branch, tag or commit).

1. It finds the tip of Eden's `master` ([`upstream.json`](upstream.json)) and compares it, the base image and this repository with the labels on the published image. If nothing changed, it stops there, unless the image is a week old (it installs Ubuntu packages, so it is rebuilt weekly for their updates).
2. It applies the patches with a 3-way merge, so small upstream changes around a patch are absorbed.
3. It builds the static room with [`scripts/build-room.sh`](scripts/build-room.sh) (ccache and the downloaded dependencies are cached between runs) and runs [`tests/test_room_protocol.py`](tests/test_room_protocol.py) against it: generated ENet clients check admission, rejection, rate limiting, moderator rights, packet validation, byte-exact relay, the log labels, DIAG and a clean `SIGTERM` stop.
4. It builds the image, checks that it starts as the `eden` user and stops cleanly, runs the protocol test against the image, and publishes it. The image's labels record the exact upstream commit, which is also how the next run knows what was built. Nothing is committed back to this repository.

When something goes wrong:

- **A patch no longer applies, or the build or tests fail:** an issue titled **Build failed** opens with the conflict or the failing step, and the last good image stays published. The next successful build closes it.
- **Upstream touched code a patch depends on** (even if the patch still applies): an issue titled **Patch review needed** lists the upstream commits, so the patch can be checked for being redundant. The rules are in [`patch-watch.json`](patch-watch.json).

## Changing a patch

Needs Git, Bash and Python 3; building needs CMake, Ninja, a C++20 compiler and OpenSSL and Boost development files (see [`scripts/build-room.sh`](scripts/build-room.sh)).

```sh
scripts/patches.sh prepare      # clone Eden master into work/upstream and apply the patches as commits
# edit work/upstream with ordinary git: amend the patch's commit, or add a commit for a new patch
scripts/build-room.sh work/upstream build                       # build build/eden-room
python3 tests/test_room_protocol.py --binary build/eden-room    # needs libenet7
scripts/patches.sh export       # write the commits back to patches/
git diff patches/               # review, then commit and push
```

Each commit becomes one patch file; the commit message is the patch's description and its subject becomes the file name. A new patch also needs an entry in `patch-watch.json` (CI checks every patch has one). If a patch stops applying, `prepare` stops at it with the conflicts marked: fix them, run `git -C work/upstream am --continue`, then export.

## Repository layout

| Path | Purpose |
|---|---|
| `patches/` | The patch series, `git format-patch` files applied in order |
| `upstream.json` | Which upstream to build (tip of Eden `master`) |
| `Dockerfile` | The runtime image around the prebuilt static `eden-room` |
| `docker-entrypoint.sh` | Validates the settings, drops to `PUID`/`PGID`, writes session logs, starts the room |
| `patch-watch.json` | Upstream files each patch depends on |
| `scripts/build-room.sh` | Build the static room from a patched checkout |
| `scripts/patches.sh` | Fetch upstream, apply, prepare for editing, export |
| `scripts/upstream.py` | Find the commit to build and decide whether to build |
| `scripts/patch-watch.py` | Flag upstream changes that may make a patch redundant |
| `scripts/issue.sh` | Open and close the CI issues |
| `tests/test_room_protocol.py` | The room over the real ENet protocol (a binary or the image) |
| `tests/test_entrypoint.py`, `tests/test_relay_banner.py` | The entrypoint's process handling and relay-mode banner |
| `tests/test_upstream.py`, `tests/test_patch_watch.py` | Tests for the shared scripts |

## Why master and a static build

Eden's last release is months older than `master`, and both speak the same room protocol (`network_version` 1), so the image follows `master` and any Eden client with that network version can join. The room is built with upstream's `YUZU_STATIC_ROOM` option: it configures nothing of the emulator, and the result needs no shared libraries, so the image is plain Ubuntu with `gosu`, `tini`, CA certificates and time zones.
