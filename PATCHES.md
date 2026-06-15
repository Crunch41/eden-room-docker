# Eden Room Server - Patch Documentation

This image builds Eden's standalone dedicated room server and applies hardening
and latency-optimisation patches before compiling. GitHub Actions rebuilds
whenever the upstream Eden HEAD changes; the Dockerfile `EDEN_REF` value is a
manual-build fallback. The LDN protocol payload is intentionally preserved: one
ENet channel, normal flush cadence, strict `network_version` rejection, and
unchanged packet payload bytes.

## Build Pinning

| Property | Value |
|----------|-------|
| Dockerfile fallback Eden ref | `37026c8aaa9e1ce01026c2aa69b4b8af5842ec5a` |
| Build arg | `EDEN_REF` |
| GitHub Actions Eden ref | Latest upstream `HEAD` whenever it changes |
| Patch entrypoint | `scripts/apply-eden-room-patches.py` |
| Build type | Release, stripped |

`EDEN_REF` can be overridden at build time, and the CI workflow passes the
latest upstream Eden commit into Docker when it decides to build. Patch
application fails loudly if Eden moves the source blocks this image depends on.
`.last_eden_commit` records the latest upstream commit that CI actually built.

The scheduled workflow rebuilds whenever the upstream Eden HEAD commit changes.

## Patch Summary

| Area | Change |
|------|--------|
| Docker lifecycle | Replaces the interactive stdin loop with signal-aware shutdown so Docker stops can reach announce cleanup, ban-list save, and `room->Destroy()`. |
| CLI safety | Changes `--username` from `optional_argument` to `required_argument` to avoid null `optarg` crashes. |
| Lobby registration | Validates empty/malformed lobby registration responses and converts JSON exceptions into `WebResult` failures. |
| Announce thread | Wraps the announce jthread body so unexpected exceptions are logged instead of terminating the process. |
| JWT verification | Protects the static public-key cache with a mutex. Suppresses the routine unauthenticated-client log noise only when the error is exactly `DecodeErrc::SignatureFormatError` (category `"decode"`, value 2 — the empty-token case). Real failures such as `TokenExpired` (`"verification"/2`) and bad-signature `VerificationErr` (`"algorithms"/2`) remain visible. |
| Console logging | Flushes Eden's console backend after each line, formats high-value room activity with `JOIN`, `LEAVE`, `CHAT`, `GAME`, `PING`, and `STAT` labels, and appends current player counts to join/leave/kick/ban logs. |
| Packet safety | Rejects empty room packets before reading `data[0]`, validates parsed packet state, and adds proxy/LDN minimum header checks before `IgnoreBytes`. |
| Room state | Serializes room member count under `member_mutex` when broadcasting room information. |
| Join flood protection | Adds per-IP join rate limiting with stale-entry pruning. |
| Moderator gate | Grants moderator status only to connections from RFC 1918 / loopback addresses (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8). Remote IPs are never elevated. The moderator username is read from `EDEN_ROOM_MOD_USERNAME`; when empty, falls back to the `--username` lobby account. Both the JWT username and the raw Eden nickname are checked so LAN players without JWT tokens can still receive moderator status. |
| Unknown IP routing | Keeps the current broadcast fallback for proxy/LDN packets by default, with `EDEN_ROOM_UNKNOWN_IP_FALLBACK=drop` available for game-specific troubleshooting. |
| Peer timeout | Sets `enet_peer_timeout` on join success using `EDEN_ROOM_PEER_TIMEOUT_MIN` / `EDEN_ROOM_PEER_TIMEOUT_MAX` (defaults 12 000 / 60 000 ms) so transient AUS↔USA routing loss (~3–5 s) does not drop players prematurely. |
| Ping interval | Sets `enet_peer_ping_interval` on join success using `EDEN_ROOM_PING_INTERVAL` (default 100 ms, ENet default 500 ms) to keep RTT/loss statistics fresh and arm the timeout machinery faster on idle links. |
| Peer RTT logging | Logs each peer's measured round-trip time at join alongside the `JOIN` line for instant desync diagnosis. |
| Peer snapshot cache | Snapshots each peer's RTT and join timestamp at join time (ENet zeroes these fields before firing `ENET_EVENT_TYPE_DISCONNECT`). |
| Session stats | Logs `STAT` before every `LEAVE` showing RTT measured at join and total session duration. |
| Unreliable game relay | Changes `HandleProxyPacket` and `HandleLdnPacket` relay packets from `ENET_PACKET_FLAG_RELIABLE` to `ENET_PACKET_FLAG_UNSEQUENCED` (configurable via `EDEN_ROOM_RELAY_RELIABLE`). The real Switch uses LDN (raw 802.11 UDP) with no reliability layer; games carry their own sequence numbers. ENet reliability causes head-of-line blocking on lossy high-RTT paths. Control packets remain `RELIABLE`. |
| Relay reliability opt-out | `EDEN_ROOM_RELAY_RELIABLE=1` restores upstream `ENET_PACKET_FLAG_RELIABLE` relay delivery without a rebuild, for per-title regression testing. |
| Relay payload cap | Drops proxy/LDN packets larger than 4096 bytes with a warning. Oversized UNSEQUENCED packets cannot be fragmented by ENet and would silently fall back to RELIABLE fragments (reintroducing head-of-line blocking); this cap also blocks broadcast amplification from malicious clients. |
| Event loop drain | Factors the event dispatch into a lambda and drains all already-queued ENet events via `enet_host_check_events` before blocking for new traffic (1 ms wait replacing 5 ms). Removes up to 5 ms of server-added relay latency per packet under burst load. |
| Rejected-join cleanup | Adds `enet_peer_disconnect_later` after every join rejection (`SendRoomIsFull`, `SendWrongPassword`, `SendNameCollision`, `SendIPCollision`, `SendVersionMismatch`, and both ban-check return paths in `HandleJoinRequest`). The ENet slot is reclaimed once the rejection packet is ACKed instead of after the client-side timeout. |
| Disconnect handler cleanup | Removes upstream's no-op `enet_peer_disconnect` in `HandleClientDisconnection` (the handler only runs after ENet has already reset the peer via `ENET_EVENT_TYPE_DISCONNECT`). |
| Status message locking | Snapshots the member count and performs all `enet_peer_send` calls inside `member_mutex`, then calls `enet_host_flush` after releasing the lock to avoid holding it during socket I/O. |
| Relay lock downgrade | Downgrades `HandleProxyPacket` and `HandleLdnPacket` from `std::lock_guard` (exclusive) to `std::shared_lock` (shared read) on `member_mutex`, which is already declared `std::shared_mutex`. Both handlers only read the member list; exclusive locking serialised all concurrent relay operations from every peer. Writers (join, disconnect, kick, ban) keep their exclusive lock. |
| Relay throttle pin | Calls `enet_peer_throttle_configure(client, 1000, ENET_PEER_PACKET_THROTTLE_ACCELERATION, 0)` in both join-success senders to pin ENet's packet throttle at 100 % permanently per peer. ENet's throttle (`enet_peer_throttle` in `peer.c`) lowers `packetThrottle` on RTT spikes and the drop gate in `protocol.c` applies to all non-nil outgoing commands including `SEND_UNSEQUENCED`. RELIABLE packets bypass the throttle via the acknowledge queue, so switching relay to UNSEQUENCED (see *Unreliable game relay* above) exposed every game packet to silent probabilistic drop on jittery internet paths. Setting deceleration to 0 prevents `packetThrottle` from ever falling below its maximum (32/32). |
| Nickname regex | Makes the `std::regex` in `IsValidNickname` `static const` so the NFA is compiled once at first use rather than on every join request. |

## Compatibility Notes

The server does not emulate individual game rules. It relays opaque proxy/LDN
envelopes between Eden clients. These patches avoid:

- changing ENet channel count
- batching or delaying `enet_host_flush()`
- allowing network-version mismatches
- modifying LDN/proxy payload data

Game relay packets use `ENET_PACKET_FLAG_UNSEQUENCED` by default. This matches
the real Switch LDN transport (raw UDP, no ordering) and eliminates server-side
head-of-line blocking on high-RTT paths. Set `EDEN_ROOM_RELAY_RELIABLE=1` to
restore ordered/reliable delivery for titles that regress under unordered relay.
Relay packets larger than 4096 bytes are dropped regardless of flag setting.

## Citron Comparison

Citron Neo still carries several yuzu-era room traits that Eden also inherited,
including the stdin loop, optional `--username`, weak packet validation, and
unguarded lobby JSON parsing. Its version-mismatch tolerance is not copied here
because accepting unknown wire formats is risky for public rooms.

## Runtime Knobs

| Variable | Default | Description |
|----------|---------|-------------|
| `EDEN_ROOM_UNKNOWN_IP_FALLBACK` | `broadcast` | Use `broadcast` to fan unknown fake-IP packets to all other members, or `drop` to restore strict drop behavior for testing. |
| `EDEN_ROOM_PEER_TIMEOUT_MIN` | `12000` | ENet `timeoutMinimum` in ms. Earliest point a dead peer can be dropped; also the rejoin-lockout window for a crashed player's nickname. |
| `EDEN_ROOM_PEER_TIMEOUT_MAX` | `60000` | ENet `timeoutMaximum` in ms. Hard cutoff for zombie peers that still ACK intermittently. Clamped to >= the minimum. |
| `EDEN_ROOM_PING_INTERVAL` | `100` | ENet peer ping interval in ms for joined peers. Keeps RTT/loss statistics fresh; does not by itself lower the minimum dead-peer drop time. |
| `EDEN_ROOM_RELAY_RELIABLE` | `0` | Set to `1` to restore upstream `ENET_PACKET_FLAG_RELIABLE` delivery on proxy/LDN relay paths for per-title regression testing. |
| `EDEN_ROOM_MOD_USERNAME` | *(empty)* | Username to grant moderator status. When empty, falls back to the `--username` lobby account name. Moderator is only granted to connections from RFC 1918 / loopback addresses regardless of this value. |

## Log Format

Docker console output and session logs use compact labels for common room
activity while warnings and errors keep class and level metadata:

```text
[10:23:45] JOIN  | [1.1.1.1] User has joined. (1/16)
[10:23:45] PING  | [1.1.1.1] User RTT 172ms
[10:23:46] GAME  | User is playing Mario Kart 8 Deluxe (3.0.3)
[10:24:10] CHAT  | User: hello
[10:27:57] STAT  | [1.1.1.1] User session RTT 172ms duration 4m12s
[10:27:57] LEAVE | [1.1.1.1] User has left. (0/16)
[10:28:12] Network <Warning> Dropping malformed room packet
```

`STAT` fires before every `LEAVE` and shows the RTT measured at join and total
session duration. ENet calls `enet_peer_reset()` before firing
`ENET_EVENT_TYPE_DISCONNECT`, which zeroes `roundTripTime` back to its 500 ms
default and clears all data counters — so stats must be snapshotted at join
time rather than read from the peer at disconnect.

## Verification Expectations

Before publishing a new image, verify:

- patch script applies to a fresh Eden checkout
- Dockerfile builds with a selected `EDEN_REF`
- scheduled CI rebuilds when Eden upstream HEAD changes
- the room exits cleanly on `docker stop`
- private room join/chat/game-info/disconnect works
- public room registration handles API errors without crashing
- malformed packet injection does not crash the process
- game smoke tests match the previous image for your community's main titles
- oversized relay packets (>4096 bytes) are dropped with a warning
- rejected joins (wrong password, full room, etc.) do not leave ENet slots open
