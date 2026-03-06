# Eden Room Server - Patch Documentation

Technical documentation for all patches applied to the Eden dedicated room server.

## Overview

| Property | Value |
|----------|-------|
| Total Patches | 15 (of 17 original; #2 and #16 not needed in Eden) |
| Image Size | ~380MB (compressed ~130MB) |
| Build Type | Release (optimized, stripped) |
| Status | Production Ready |

---

## Patch Summary

### Critical Fixes

| # | File | Issue | Fix |
|---|------|-------|-----|
| 1 | yuzu_room.cpp | NULL crash on public room registration | Initialize `lobby_api_url` from `web_api_url` |
| 5 | yuzu_room.cpp | Segfault with `--username` flag | Change `optional_argument` to `required_argument` |
| 7 | room.cpp | Moderator powers fail on LAN | Check nickname when JWT verification fails |
| 10 | room.cpp | LDN packet loss for unknown IPs | Broadcast fallback when destination unknown |
| 11 | room.cpp | Server crash from exceptions | Add try/catch to main StartLoop |
| 15 | room.cpp | Buffer overread from small packets | Validate packet size before parsing |

### Stability Fixes

| # | File | Issue | Fix |
|---|------|-------|-----|
| 1 | yuzu_room.cpp | Container hangs on startup | Remove stdin blocking loop |
| 3 | announce_room_json.cpp | Crash on malformed API response | Add JSON parsing error handling |
| 4 | announce_multiplayer_session.cpp | Silent thread crashes | Add exception wrapper to jthread announce loop |
| 14 | verify_user_jwt.cpp | Data race in JWT key fetch | Add mutex protection |

### Quality of Life

| # | File | Issue | Fix |
|---|------|-------|-----|
| 6 | room.cpp | No visibility when moderators join | Add logging for moderator joins |
| 8 | verify_user_jwt.cpp | Noisy JWT error logs | Suppress common error code 2 |
| 9 | room.cpp | Log spam from unknown IP routing | Move to DEBUG level |
| 12 | room.cpp | No protection against join flooding | Rate limit per IP |
| 13 | room.cpp | Lock ordering concerns | Document correct ordering |
| 17 | text_formatter.cpp | Unreadable log timestamps | Human-readable HH:MM:SS format |

### Skipped (Not Needed in Eden)

| # | Reason |
|---|--------|
| #2 | Eden has no `lobby_api_url` setting — uses `eden_username`/`eden_token` directly |
| #16 | Eden rewrote `GenerateFakeIPAddress()` with a safe exhaustive `for` loop — cannot infinite-loop |

---

## Key Differences from Citron

| Area | Citron | Eden |
|------|--------|------|
| Entry point file | `citron_room.cpp` | `yuzu_room.cpp` |
| Main loop | `ServerLoop()` | `StartLoop()` (std::jthread) |
| Announce loop | `AnnounceMultiplayerLoop()` | Inline jthread lambda in `Start()` |
| Settings keys | `citron_username`, `citron_token` | `eden_username`, `eden_token` |
| IP generation | Probabilistic do-while (could loop) | Exhaustive for-loop (safe) |
| CMake target | `citron-room` | `yuzu_room_standalone` → binary `eden-room` |

---

## Detailed Patch Descriptions

### Patch #1: Stdin Loop Fix

**File**: `src/dedicated_room/yuzu_room.cpp`

**Problem**: The server waits for console input in a loop, which blocks forever in Docker containers that have no interactive stdin.

**Before**:
```cpp
while (room->GetState() == Network::Room::State::Open) {
    std::string in;
    std::cin >> in;  // Blocks waiting for input
    if (in.size() > 0) {
        break;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
}
```

**After**:
```cpp
while (room->GetState() == Network::Room::State::Open) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
}
```

---

### Patch #3: Register() Error Handling

**File**: `src/web_service/announce_room_json.cpp`

**Problem**: JSON parsing exceptions crash the server when API returns malformed data.

**Fix**: Wrap JSON operations in try/catch, validate response contains required fields before access.

---

### Patch #4: Thread Safety Wrapper (Adapted for Eden)

**File**: `src/network/announce_multiplayer_session.cpp`

**Problem**: Uncaught exceptions in announcement thread call `std::terminate()`, crashing the entire server silently.

**Eden change**: No `AnnounceMultiplayerLoop()` function — logic is an inline lambda in `Start()` using `std::jthread`. Wrap the lambda body in try/catch instead.

---

### Patch #5: Username Argument Fix (CRITICAL)

**File**: `src/dedicated_room/yuzu_room.cpp`

**Problem**: Using `--username "value"` with a space causes segfault because `optarg` is NULL.

**Before**:
```cpp
{"username", optional_argument, 0, 'u'}
```

**After**:
```cpp
{"username", required_argument, 0, 'u'}
```

---

### Patch #6: Moderator Join Logging

**File**: `src/network/room.cpp`

**Fix**: Log message when user is granted moderator status:
```
User 'YourName' (YourName) joined as MODERATOR
```

---

### Patch #7: LAN Moderator Detection (CRITICAL)

**File**: `src/network/room.cpp`

**Problem**: Moderator powers don't work on LAN because JWT verification always fails, leaving `user_data.username` empty.

**Fix**: Also check `nickname` against `host_username` when JWT-derived username is empty.

**Before**:
```cpp
if (sending_member->user_data.username == room_information.host_username) {
    return true;
}
```

**After**:
```cpp
if (sending_member->user_data.username == room_information.host_username) {
    return true;
}
if (sending_member->nickname == room_information.host_username) {
    return true;
}
```

---

### Patch #8: JWT Error Suppression

**File**: `src/web_service/verify_user_jwt.cpp`

**Problem**: Error code 2 (signature format incorrect) is logged for every client that doesn't authenticate via the web API, which is most players.

**Fix**: Suppress logging for error code 2 specifically. Other JWT errors are still logged.

---

### Patch #9: Unknown IP Error Suppression

**File**: `src/network/room.cpp`

**Problem**: LDN packets contain players' home network IPs which the server can't route, generating constant error logs.

**Fix**: Move from ERROR to DEBUG level.

---

### Patch #10: LDN Broadcast Fallback (CRITICAL)

**File**: `src/network/room.cpp`

**Problem**: Packets to unknown IPs are dropped, causing packet loss in LDN games.

**Fix**: Broadcast to all members when destination IP is unknown instead of dropping.

---

### Patch #11: StartLoop Exception Handling (CRITICAL, Adapted for Eden)

**File**: `src/network/room.cpp`

**Problem**: Any exception in the main server loop terminates the process.

**Eden change**: Function renamed from `ServerLoop()` to `StartLoop()` and uses `std::jthread`. Wrap the while loop body in try/catch.

---

### Patch #12: Rate Limiting

**File**: `src/network/room.cpp`

**Problem**: No protection against join request flooding (DoS).

**Fix**: Track last join attempt per IP, reject if less than 1 second since previous attempt.

---

### Patch #13: Lock Ordering Documentation

**File**: `src/network/room.cpp`

**Result**: After analysis, lock ordering is correct. Added documentation comments.

---

### Patch #14: Thread-Safe GetPublicKey

**File**: `src/web_service/verify_user_jwt.cpp`

**Problem**: Multiple threads can race to fetch and write the static public key.

**Fix**: Add mutex protection around the key fetch and storage.

---

### Patch #15: Packet Bounds Validation (CRITICAL)

**File**: `src/network/room.cpp`

**Problem**: Packets are read without checking if sufficient data exists.

**Fix**: Validate minimum packet size (12 bytes) before parsing join requests.

---

### Patch #17: Log Format Cleanup

**File**: `src/common/logging/text_formatter.cpp`

**Problem**: Logs show uptime in seconds and verbose file paths, difficult to read.

**Before**:
```
[359443.476077] Network <Info> network/room.cpp:SendStatusMessage:770: [1.1.1.1] User joined.
```

**After**:
```
[10:23:45] [1.1.1.1] User joined.
```

---

## Build Verification

All patches apply successfully during Docker build:

```
Patched stdin loop
Added Register() error handling
Added thread safety wrapper to jthread announce loop
Fixed username argument (required)
Added moderator join logging
Added LAN moderator detection (nickname check)
Suppressed JWT error code 2 logging
Suppressed unknown IP errors (moved to DEBUG level)
Added broadcast fallback for unknown IP packets
Added StartLoop exception handling
Added join request rate limiting
Added race condition documentation
Added thread-safe GetPublicKey
Added packet bounds validation
Patched log format to be cleaner and human-readable
```

---

## Files Modified

- `src/dedicated_room/yuzu_room.cpp` (Patches 1, 5)
- `src/network/room.cpp` (Patches 6, 7, 9, 10, 11, 12, 13, 15)
- `src/web_service/verify_user_jwt.cpp` (Patches 8, 14)
- `src/web_service/announce_room_json.cpp` (Patch 3)
- `src/network/announce_multiplayer_session.cpp` (Patch 4)
- `src/common/logging/text_formatter.cpp` (Patch 17)
