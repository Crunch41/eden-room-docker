# Eden Room Server - Patch Documentation

This image builds Eden's standalone dedicated room server and applies low-risk
hardening patches before compiling. GitHub Actions builds from the latest Eden
commit when relevant upstream room/network files change; the Dockerfile
`EDEN_REF` value is a manual-build fallback. The room/LDN protocol behavior is
intentionally preserved: one ENet channel, reliable packet sends, normal flush
cadence, strict `network_version` rejection, and unchanged packet payload bytes.

## Build Pinning

| Property | Value |
|----------|-------|
| Dockerfile fallback Eden ref | `37026c8aaa9e1ce01026c2aa69b4b8af5842ec5a` |
| Build arg | `EDEN_REF` |
| GitHub Actions Eden ref | Latest upstream `HEAD` when relevant room/network paths changed |
| Patch entrypoint | `scripts/apply-eden-room-patches.py` |
| Build type | Release, stripped |

`EDEN_REF` can be overridden at build time, and the CI workflow passes the
latest upstream Eden commit into Docker when it decides to build. Patch
application fails loudly if Eden moves the source blocks this image depends on.
`.last_eden_commit` records the latest upstream commit that CI actually built.

The scheduled workflow rebuilds when changes are detected in the dedicated room,
web service, room client/server, logging, socket types, internal networking, LDN,
socket service, or NIFM fake-IP paths.

## Patch Summary

| Area | Change |
|------|--------|
| Docker lifecycle | Replaces the interactive stdin loop with signal-aware shutdown so Docker stops can reach announce cleanup, ban-list save, and `room->Destroy()`. |
| CLI safety | Changes `--username` from `optional_argument` to `required_argument` to avoid null `optarg` crashes. |
| Lobby registration | Validates empty/malformed lobby registration responses and converts JSON exceptions into `WebResult` failures. |
| Announce thread | Wraps the announce jthread body so unexpected exceptions are logged instead of terminating the process. |
| JWT verification | Protects the static public-key cache with a mutex and suppresses the common unauthenticated-client JWT noise. |
| Console logging | Flushes Eden's console backend after each line and formats high-value room activity with `JOIN`, `LEAVE`, `CHAT`, and `GAME` labels. |
| Packet safety | Rejects empty room packets before reading `data[0]`, validates parsed packet state, and adds proxy/LDN minimum header checks before `IgnoreBytes`. |
| Room state | Serializes room member count under `member_mutex` when broadcasting room information. |
| Join flood protection | Adds per-IP join rate limiting with stale-entry pruning. |
| LAN host moderation | Allows host nickname matching when JWT user data is absent. |
| Unknown IP routing | Keeps the current broadcast fallback for proxy/LDN packets by default, with `EDEN_ROOM_UNKNOWN_IP_FALLBACK=drop` available for game-specific troubleshooting. |

## Compatibility Notes

The server does not emulate individual game rules. It relays opaque proxy/LDN
envelopes between Eden clients. Because specific Switch games can be sensitive
to timing, ordering, reliability, and routing, these patches avoid:

- changing ENet channel count
- changing `ENET_PACKET_FLAG_RELIABLE`
- batching or delaying `enet_host_flush()`
- allowing network-version mismatches
- modifying LDN/proxy payload data

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
[10:23:45] JOIN  | [1.1.1.1] User has joined.
[10:23:46] GAME  | User is playing Mario Kart 8 Deluxe (3.0.3)
[10:24:10] CHAT  | User: hello
[10:27:57] LEAVE | [1.1.1.1] User has left.
[10:28:12] Network <Warning> Dropping malformed room packet
```

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
