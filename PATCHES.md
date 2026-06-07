# Eden Room Server - Patch Documentation

This image builds Eden's standalone dedicated room server and applies hardening
and latency-optimisation patches before compiling. GitHub Actions rebuilds
whenever the upstream Eden HEAD changes; the Dockerfile `EDEN_REF` value is a
manual-build fallback. The LDN protocol
payload is intentionally preserved: one ENet channel, normal flush cadence,
strict `network_version` rejection, and unchanged packet payload bytes. Game
data relay packets use `ENET_PACKET_FLAG_UNSEQUENCED` to match real Switch LDN
transport semantics (see Patch Summary).

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
| JWT verification | Protects the static public-key cache with a mutex and suppresses the common unauthenticated-client JWT noise. |
| Console logging | Flushes Eden's console backend after each line, formats high-value room activity with `JOIN`, `LEAVE`, `CHAT`, `GAME`, `PING`, and `STAT` labels, and appends current player counts to join/leave/kick/ban logs. |
| Packet safety | Rejects empty room packets before reading `data[0]`, validates parsed packet state, and adds proxy/LDN minimum header checks before `IgnoreBytes`. |
| Room state | Serializes room member count under `member_mutex` when broadcasting room information. |
| Join flood protection | Adds per-IP join rate limiting with stale-entry pruning. |
| LAN host moderation | Allows host nickname matching when JWT user data is absent. |
| Unknown IP routing | Keeps the current broadcast fallback for proxy/LDN packets by default, with `EDEN_ROOM_UNKNOWN_IP_FALLBACK=drop` available for game-specific troubleshooting. |
| Peer timeout | Sets `enet_peer_timeout` to 12 000 / 60 000 ms on join success so transient AUS↔USA routing loss (~3–5 s) does not drop players prematurely. |
| Peer RTT logging | Logs each peer's measured round-trip time at join alongside the `JOIN` line for instant desync diagnosis without connecting to the client. |
| Unreliable game relay | Changes `HandleProxyPacket` and `HandleLdnPacket` relay packets from `ENET_PACKET_FLAG_RELIABLE` to `ENET_PACKET_FLAG_UNSEQUENCED`. The real Switch uses LDN (raw 802.11 UDP) with no reliability layer; games carry their own sequence numbers and handle loss. ENet reliability causes head-of-line blocking on lossy high-RTT paths that produces burst/teleport desync. Control packets (join, chat, kick, game info) remain `RELIABLE`. |

## Compatibility Notes

The server does not emulate individual game rules. It relays opaque proxy/LDN
envelopes between Eden clients. These patches avoid:

- changing ENet channel count
- batching or delaying `enet_host_flush()`
- allowing network-version mismatches
- modifying LDN/proxy payload data

Game relay packets intentionally use `ENET_PACKET_FLAG_UNSEQUENCED` rather than
`ENET_PACKET_FLAG_RELIABLE`. This matches the real Switch LDN transport (raw
UDP, no ordering) and eliminates server-side head-of-line blocking on high-RTT
paths. Games that require ordered delivery for their own internal LDN messages
may see regressions — test each title before deploying to a community server.

## Citron Comparison

Citron Neo still carries several yuzu-era room traits that Eden also inherited,
including the stdin loop, optional `--username`, weak packet validation, and
unguarded lobby JSON parsing. Its version-mismatch tolerance is not copied here
because accepting unknown wire formats is risky for public rooms.

## Runtime Knobs

| Variable | Default | Description |
|----------|---------|-------------|
| `EDEN_ROOM_UNKNOWN_IP_FALLBACK` | `broadcast` | Use `broadcast` to fan unknown fake-IP packets to other members, or `drop` to restore strict drop behavior for testing. |

## Log Format

Docker console output and session logs use compact labels for common room
activity while warnings and errors keep class and level metadata:

```text
[10:23:45] JOIN  | [1.1.1.1] User has joined. (1/16)
[10:23:45] PING  | [1.1.1.1] User RTT 172ms
[10:23:46] GAME  | User is playing Mario Kart 8 Deluxe (3.0.3)
[10:24:10] CHAT  | User: hello
[10:27:57] STAT  | [1.1.1.1] User final RTT 174ms loss 0.3% tx 4.2MB rx 3.8MB
[10:27:57] LEAVE | [1.1.1.1] User has left. (1/16)
[10:28:12] Network <Warning> Dropping malformed room packet
```

`STAT` fires before every `LEAVE` — a sudden leave with elevated loss (e.g. `loss 4.2%`) signals the
extended peer timeout kept them connected longer than the default would have; a clean exit shows `loss 0.0%`.

## Verification Expectations

Before publishing a new image, verify:

- patch script applies to a fresh Eden checkout
- Dockerfile builds with a selected `EDEN_REF`
- scheduled CI rebuilds when relevant Eden room/network paths change
- the room exits cleanly on `docker stop`
- private room join/chat/game-info/disconnect works
- public room registration handles API errors without crashing
- malformed packet injection does not crash the process
- game smoke tests match the previous image for your community's main titles
