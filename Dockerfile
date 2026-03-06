# 1) Builder stage - PRODUCTION OPTIMIZED
###########################
FROM ubuntu:24.04 AS builder
ENV DEBIAN_FRONTEND=noninteractive

# Base tools & certificates
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      git \
      build-essential \
      cmake \
      ninja-build \
      pkg-config \
      python3 \
      perl \
      autoconf \
      libtool \
      # Core libraries
      libboost-all-dev \
      libfmt-dev \
      liblz4-dev \
      libzstd-dev \
      libssl-dev \
      libopus-dev \
      zlib1g-dev \
      libenet-dev \
      nlohmann-json3-dev \
      llvm-dev \
      # Additional dependencies
      libudev-dev \
      libopenal-dev \
      glslang-tools \
      libavcodec-dev \
      libavfilter-dev \
      libavutil-dev \
      libswscale-dev \
      libswresample-dev \
      # X11 libraries
      libx11-dev \
      libxrandr-dev \
      libxinerama-dev \
      libxcursor-dev \
      libxi-dev \
      # mbedtls
      libmbedtls-dev \
      # Optional dependencies (suppress CMake warnings)
      libusb-1.0-0-dev \
      gamemode-dev \
      libsdl2-dev \
      doxygen \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Clone latest Eden with submodules
RUN git clone --recursive https://git.eden-emu.dev/eden-emu/eden.git . && \
    echo "=== EDEN SOURCE ===" && \
    git log -1 --format="%H %s"

# ---------------------------------------------------------------------------
# PATCH 1: Fix stdin loop (prevents container hanging in Docker)
# Target: src/dedicated_room/yuzu_room.cpp (was citron_room.cpp in Citron)
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path
import re

p = Path("src/dedicated_room/yuzu_room.cpp")
content = p.read_text(encoding="utf-8")

match = re.search(r'while\s*\(\s*room-\>GetState\(\)\s*==\s*Network::Room::State::Open\s*\)\s*\{', content)

if match:
    start_idx = match.end()
    open_braces = 1
    end_idx = start_idx

    while open_braces > 0 and end_idx < len(content):
        if content[end_idx] == '{':
            open_braces += 1
        elif content[end_idx] == '}':
            open_braces -= 1
        end_idx += 1

    if open_braces == 0:
        replacement = '''while (room->GetState() == Network::Room::State::Open) {
            std::this_thread::sleep_for(std::chrono::seconds(1));
        }'''

        content = content[:match.start()] + replacement + content[end_idx:]
        print("[OK] Patched stdin loop")
        p.write_text(content, encoding="utf-8")
    else:
        print("ERROR: Could not find closing brace")
        exit(1)
else:
    print("ERROR: Could not find stdin loop")
    exit(1)
PY

# ---------------------------------------------------------------------------
# PATCH 2 (SKIPPED): lobby_api_url initialization
# Eden does not have a lobby_api_url setting - uses eden_username/eden_token directly.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# PATCH 3: Add error handling to Register()
# File: src/web_service/announce_room_json.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path
import re

p = Path("src/web_service/announce_room_json.cpp")
content = p.read_text(encoding="utf-8")

if '#include <stdexcept>' not in content:
    content = content.replace(
        '#include <nlohmann/json.hpp>',
        '#include <stdexcept>\n#include <nlohmann/json.hpp>'
    )

pattern = re.compile(
    r'auto reply_json = nlohmann::json::parse\(result\.returned_data\);\s+'
    r'room = reply_json\.get<AnnounceMultiplayerRoom::Room>\(\);\s+'
    r'room_id = reply_json\.at\("id"\)\.get<std::string>\(\);',
    re.DOTALL
)

new_code = '''try {
        if (result.returned_data.empty()) {
            LOG_ERROR(WebService, "Registration response is empty");
            return WebService::WebResult{WebService::WebResult::Code::WrongContent,
                                         "Empty response from server", ""};
        }

        auto reply_json = nlohmann::json::parse(result.returned_data);

        if (!reply_json.contains("id")) {
            LOG_ERROR(WebService, "Registration response missing 'id' field");
            return WebService::WebResult{WebService::WebResult::Code::WrongContent,
                                         "Missing room ID in response", ""};
        }

        room = reply_json.get<AnnounceMultiplayerRoom::Room>();
        room_id = reply_json.at("id").get<std::string>();

    } catch (const std::exception& e) {
        LOG_ERROR(WebService, "Registration parsing error: {}", e.what());
        return WebService::WebResult{WebService::WebResult::Code::WrongContent,
                                     "Invalid JSON in response", ""};
    }'''

if pattern.search(content):
    content = pattern.sub(new_code, content)
    print("[OK] Added Register() error handling")
    p.write_text(content, encoding="utf-8")
else:
    print("WARNING: Could not apply Register() fix - pattern not found")
PY

# ---------------------------------------------------------------------------
# PATCH 4 (ADAPTED): Thread safety wrapper for announce loop
# Eden uses std::jthread + lambda (no AnnounceMultiplayerLoop function).
# We wrap the lambda body in try/catch inside Start().
# File: src/network/announce_multiplayer_session.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path

p = Path("src/network/announce_multiplayer_session.cpp")
content = p.read_text(encoding="utf-8")

# Eden's announce loop starts the jthread lambda body with the register call
old_code = '''        if (!registered) {
            WebService::WebResult result = Register();
            if (result.result_code != WebService::WebResult::Code::Success) {
                ErrorCallback(result);
                return;
            }
        }'''

new_code = '''        try {
        if (!registered) {
            WebService::WebResult result = Register();
            if (result.result_code != WebService::WebResult::Code::Success) {
                ErrorCallback(result);
                return;
            }
        }'''

# Find the closing of the jthread lambda to add the catch
old_end = '''    });
}

void AnnounceMultiplayerSession::Stop()'''

new_end = '''        } catch (const std::exception& e) {
            LOG_ERROR(Network, "Announce thread crashed: {}", e.what());
        } catch (...) {
            LOG_ERROR(Network, "Announce thread crashed (unknown)");
        }
    });
}

void AnnounceMultiplayerSession::Stop()'''

if old_code in content and old_end in content:
    content = content.replace(old_code, new_code)
    content = content.replace(old_end, new_end)
    p.write_text(content, encoding="utf-8")
    print("[OK] Added thread safety wrapper to jthread announce loop")
else:
    if old_code not in content:
        print("WARNING: Could not find announce loop start pattern")
    if old_end not in content:
        print("WARNING: Could not find announce loop end pattern")
PY

# ---------------------------------------------------------------------------
# PATCH 5: Fix username NULL crash (optional_argument -> required_argument)
# Target: src/dedicated_room/yuzu_room.cpp (was citron_room.cpp)
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path

p = Path("src/dedicated_room/yuzu_room.cpp")
content = p.read_text(encoding="utf-8")

original = '{"username", optional_argument, 0, \'u\'}'
replacement = '{"username", required_argument, 0, \'u\'}'

if original in content:
    content = content.replace(original, replacement)
    print("[OK] Fixed username argument (required)")
    p.write_text(content, encoding="utf-8")
else:
    print("WARNING: Could not find username argument - may already be fixed")
PY

# ---------------------------------------------------------------------------
# PATCH 6: Add moderator join logging
# File: src/network/room.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path

p = Path("src/network/room.cpp")
content = p.read_text(encoding="utf-8")

search_pattern = """if (HasModPermission(event->peer)) {
        SendJoinSuccessAsMod(event->peer, preferred_fake_ip);
    } else {
        SendJoinSuccess(event->peer, preferred_fake_ip);
    }"""

replacement = """if (HasModPermission(event->peer)) {
        // Log moderator join
        std::lock_guard lock(member_mutex);
        const auto mod_member = std::find_if(members.begin(), members.end(),
            [&event](const auto& m) { return m.peer == event->peer; });
        if (mod_member != members.end()) {
            LOG_INFO(Network, "User '{}' ({}) joined as MODERATOR",
                     mod_member->nickname, mod_member->user_data.username);
        }
        SendJoinSuccessAsMod(event->peer, preferred_fake_ip);
    } else {
        SendJoinSuccess(event->peer, preferred_fake_ip);
    }"""

if search_pattern in content:
    content = content.replace(search_pattern, replacement)
    p.write_text(content, encoding="utf-8")
    print("[OK] Added moderator join logging")
else:
    print("WARNING: Could not apply moderator logging patch")
PY

# ---------------------------------------------------------------------------
# PATCH 7: Fix LAN moderator detection (check nickname when JWT fails)
# File: src/network/room.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path

p = Path("src/network/room.cpp")
content = p.read_text(encoding="utf-8")

search_string = """if (!room_information.host_username.empty() &&
        sending_member->user_data.username == room_information.host_username) { // Room host

        return true;
    }
    return false;"""

replacement_string = """if (!room_information.host_username.empty() &&
        sending_member->user_data.username == room_information.host_username) { // Room host

        return true;
    }
    // Also check nickname for LAN connections (when JWT verification fails)
    if (!room_information.host_username.empty() &&
        sending_member->nickname == room_information.host_username) { // Room host (LAN)

        return true;
    }
    return false;"""

if search_string in content:
    content = content.replace(search_string, replacement_string)
    p.write_text(content, encoding="utf-8")
    print("[OK] Added LAN moderator detection (nickname check)")
else:
    print("WARNING: Could not find HasModPermission pattern")
    if "HasModPermission" in content:
        print("INFO: HasModPermission function exists in file")
    if "room_information.host_username" in content:
        print("INFO: host_username check exists in file")
PY

# ---------------------------------------------------------------------------
# PATCH 8: Suppress JWT verification error code 2 (common for all clients)
# File: src/web_service/verify_user_jwt.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path

p = Path("src/web_service/verify_user_jwt.cpp")
content = p.read_text(encoding="utf-8")

search_string = '''if (error) {
        LOG_INFO(WebService, "Verification failed: category={}, code={}, message={}",
                 error.category().name(), error.value(), error.message());
        return {};
    }'''

replacement_string = '''if (error) {
        // Skip logging for error code 2 (signature verification skipped - common/expected)
        if (error.value() != 2) {
            LOG_INFO(WebService, "JWT verification failed: category={}, code={}, message={}",
                     error.category().name(), error.value(), error.message());
        }
        return {};
    }'''

if search_string in content:
    content = content.replace(search_string, replacement_string)
    p.write_text(content, encoding="utf-8")
    print("[OK] Suppressed JWT error code 2 logging")
else:
    print("WARNING: Could not find JWT verification error pattern")
PY

# ---------------------------------------------------------------------------
# PATCH 9: Suppress harmless unknown IP errors in HandleLdnPacket
# File: src/network/room.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path
import re

p = Path("src/network/room.cpp")
content = p.read_text(encoding="utf-8")

pattern = r'LOG_ERROR\(Network,\s*\n\s*"Attempting to send to unknown IP address: "\s*\n\s*"\{\}\.\{\}\.\{\}\.\{\}",\s*\n\s*destination_address\[0\], destination_address\[1\], destination_address\[2\],\s*\n\s*destination_address\[3\]\);'

replacement = '''LOG_DEBUG(Network,
                      "Packet to unknown IP (broadcasting instead): "
                      "{}.{}.{}.{}",
                      destination_address[0], destination_address[1], destination_address[2],
                      destination_address[3]);'''

matches = re.findall(pattern, content)
if len(matches) >= 1:
    content = re.sub(pattern, replacement, content)
    p.write_text(content, encoding="utf-8")
    print(f"[OK] Suppressed {len(matches)} unknown IP error(s) (moved to DEBUG level)")
else:
    print("INFO: PATCH 9 skipped (pattern not found in this Eden version)")
PY

# ---------------------------------------------------------------------------
# PATCH 10: Fix unknown IP packets with broadcast fallback
# File: src/network/room.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path
import re

p = Path("src/network/room.cpp")
content = p.read_text(encoding="utf-8")

pattern = r'(LOG_DEBUG\(Network,\s*\n\s*"Packet to unknown IP \(broadcasting instead\): "\s*\n\s*"\{\}\.\{\}\.\{\}\.\{\}",\s*\n\s*destination_address\[0\], destination_address\[1\], destination_address\[2\],\s*\n\s*destination_address\[3\]\);)\s*\n\s*enet_packet_destroy\(enet_packet\);'

replacement = r'''\1
                // Broadcast to all other members as fallback (safe for most LDN traffic)
                bool sent_packet = false;
                for (const auto& dest_member : members) {
                    if (dest_member.peer != event->peer) {
                        sent_packet = true;
                        enet_peer_send(dest_member.peer, 0, enet_packet);
                    }
                }
                if (!sent_packet) {
                    enet_packet_destroy(enet_packet);
                }'''

if re.search(pattern, content):
    content = re.sub(pattern, replacement, content)
    p.write_text(content, encoding="utf-8")
    print("[OK] Added broadcast fallback for unknown IP packets")
else:
    print("INFO: PATCH 10 skipped (requires PATCH 9 - pattern not found)")
PY

# ---------------------------------------------------------------------------
# PATCH 11 (ADAPTED): StartLoop exception handling
# Eden uses StartLoop() with std::jthread (was ServerLoop in Citron)
# File: src/network/room.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path

p = Path("src/network/room.cpp")
content = p.read_text(encoding="utf-8")

# Eden's StartLoop wraps the server loop in a jthread lambda
old_code = '''while (state != State::Closed) {
            ENetEvent event;
            if (enet_host_service(server, &event, 5) > 0) {'''

new_code = '''while (state != State::Closed) {
            try {
            ENetEvent event;
            if (enet_host_service(server, &event, 5) > 0) {'''

# Find the end of the event-handling block inside StartLoop to add catch
old_end = '''            case ENET_EVENT_TYPE_NONE:
                case ENET_EVENT_TYPE_CONNECT:
                    break;
                }
            }
        }
        // Close the connection to all members:'''

new_end = '''            case ENET_EVENT_TYPE_NONE:
                case ENET_EVENT_TYPE_CONNECT:
                    break;
                }
            }
            } catch (const std::exception& e) {
                LOG_ERROR(Network, "StartLoop error: {}", e.what());
            } catch (...) {
                LOG_ERROR(Network, "StartLoop unknown error");
            }
        }
        // Close the connection to all members:'''

if old_code in content and old_end in content:
    content = content.replace(old_code, new_code)
    content = content.replace(old_end, new_end)
    p.write_text(content, encoding="utf-8")
    print("[OK] Added StartLoop exception handling")
else:
    if old_code not in content:
        print("WARNING: Could not find StartLoop start pattern")
    if old_end not in content:
        print("WARNING: Could not find StartLoop end pattern")
PY

# ---------------------------------------------------------------------------
# PATCH 12: Rate limiting for join requests (prevents DoS)
# File: src/network/room.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path

p = Path("src/network/room.cpp")
content = p.read_text(encoding="utf-8")

class_pattern = '''class Room::RoomImpl {
public:
    std::mt19937 random_gen;'''

class_replacement = '''class Room::RoomImpl {
public:
    std::mt19937 random_gen;

    // Rate limiting for join requests (Patch #12)
    std::unordered_map<u32, std::chrono::steady_clock::time_point> last_join_attempt;
    static constexpr auto JOIN_RATE_LIMIT = std::chrono::seconds(1);'''

if class_pattern in content:
    content = content.replace(class_pattern, class_replacement)

    join_start = '''void Room::RoomImpl::HandleJoinRequest(const ENetEvent* event) {
    {
        std::lock_guard lock(member_mutex);'''

    join_replacement = '''void Room::RoomImpl::HandleJoinRequest(const ENetEvent* event) {
    // Rate limiting check (Patch #12)
    {
        auto now = std::chrono::steady_clock::now();
        u32 client_ip = event->peer->address.host;
        auto it = last_join_attempt.find(client_ip);
        if (it != last_join_attempt.end()) {
            if (now - it->second < JOIN_RATE_LIMIT) {
                LOG_WARNING(Network, "Rate limiting join request");
                return;
            }
        }
        last_join_attempt[client_ip] = now;
    }
    {
        std::lock_guard lock(member_mutex);'''

    if join_start in content:
        content = content.replace(join_start, join_replacement)

        if '#include <chrono>' not in content:
            content = content.replace(
                '#include "network/room.h"',
                '#include "network/room.h"\n#include <chrono>\n#include <unordered_map>'
            )

        p.write_text(content, encoding="utf-8")
        print("[OK] Added join request rate limiting")
    else:
        print("WARNING: Could not find HandleJoinRequest start")
else:
    print("WARNING: Could not find RoomImpl class definition")
PY

# ---------------------------------------------------------------------------
# PATCH 13: Lock ordering documentation
# File: src/network/room.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path

p = Path("src/network/room.cpp")
content = p.read_text(encoding="utf-8")

old_pattern = '''    // Notify everyone that the room information has changed.
    BroadcastRoomInformation();
    if (HasModPermission(event->peer)) {'''

new_pattern = '''    // Notify everyone that the room information has changed.
    BroadcastRoomInformation();
    // Note: HasModPermission acquires its own lock, which is safe since we released ours
    if (HasModPermission(event->peer)) {'''

if old_pattern in content:
    content = content.replace(old_pattern, new_pattern)
    p.write_text(content, encoding="utf-8")
    print("[OK] Added race condition documentation (lock order is correct)")
else:
    print("INFO: PATCH 13 - Pattern not found, lock order may already be documented")
PY

# ---------------------------------------------------------------------------
# PATCH 14: Thread-safe GetPublicKey
# File: src/web_service/verify_user_jwt.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path

p = Path("src/web_service/verify_user_jwt.cpp")
content = p.read_text(encoding="utf-8")

old_static = '''static std::string public_key;
std::string GetPublicKey(const std::string& host) {
    if (public_key.empty()) {'''

new_static = '''static std::string public_key;
static std::mutex public_key_mutex;

std::string GetPublicKey(const std::string& host) {
    std::lock_guard<std::mutex> lock(public_key_mutex);
    if (public_key.empty()) {'''

if old_static in content:
    content = content.replace(old_static, new_static)

    if '#include <mutex>' not in content:
        content = content.replace(
            '#include <system_error>',
            '#include <system_error>\n#include <mutex>'
        )

    p.write_text(content, encoding="utf-8")
    print("[OK] Added thread-safe GetPublicKey")
else:
    print("WARNING: Could not find GetPublicKey pattern")
PY

# ---------------------------------------------------------------------------
# PATCH 15: Basic packet bounds validation
# File: src/network/room.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path

p = Path("src/network/room.cpp")
content = p.read_text(encoding="utf-8")

# Try after rate-limiting patch first
old_join = '''void Room::RoomImpl::HandleJoinRequest(const ENetEvent* event) {
    // Rate limiting check (Patch #12)'''

new_join = '''void Room::RoomImpl::HandleJoinRequest(const ENetEvent* event) {
    // Packet bounds validation (Patch #15)
    if (event->packet->dataLength < 12) {
        LOG_WARNING(Network, "Malformed join request: packet too small ({} bytes)",
                   event->packet->dataLength);
        return;
    }
    // Rate limiting check (Patch #12)'''

if old_join in content:
    content = content.replace(old_join, new_join)
    p.write_text(content, encoding="utf-8")
    print("[OK] Added packet bounds validation to HandleJoinRequest")
else:
    # Fallback: without rate limiting patch applied
    old_join_fallback = '''void Room::RoomImpl::HandleJoinRequest(const ENetEvent* event) {
    {
        std::lock_guard lock(member_mutex);'''

    new_join_fallback = '''void Room::RoomImpl::HandleJoinRequest(const ENetEvent* event) {
    // Packet bounds validation (Patch #15)
    if (event->packet->dataLength < 12) {
        LOG_WARNING(Network, "Malformed join request: packet too small ({} bytes)",
                   event->packet->dataLength);
        return;
    }
    {
        std::lock_guard lock(member_mutex);'''

    if old_join_fallback in content:
        content = content.replace(old_join_fallback, new_join_fallback)
        p.write_text(content, encoding="utf-8")
        print("[OK] Added packet bounds validation to HandleJoinRequest (fallback)")
    else:
        print("WARNING: Could not find HandleJoinRequest for packet validation")
PY

# ---------------------------------------------------------------------------
# PATCH 16 (SKIPPED): IP generation infinite loop safeguard
# Eden completely rewrote GenerateFakeIPAddress() with an exhaustive for-loop
# that cannot infinite-loop. No action needed.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# PATCH 17: Cleaner, human-readable log format
# File: src/common/logging/text_formatter.cpp
# ---------------------------------------------------------------------------
RUN python3 - <<'PY'
from pathlib import Path

p = Path("src/common/logging/text_formatter.cpp")
content = p.read_text(encoding="utf-8")

# Add chrono and ctime includes for real timestamps
if '#include <ctime>' not in content:
    content = content.replace(
        '#include <array>',
        '#include <array>\n#include <ctime>\n#include <chrono>'
    )

# Replace FormatLogMessage - match Eden's EXACT signature (noexcept, uint32_t, null guard)
old_format = '''std::string FormatLogMessage(const Entry& entry) noexcept {
    if (!entry.filename) return "";

    auto const time_seconds = uint32_t(entry.timestamp.count() / 1000000);
    auto const time_fractional = uint32_t(entry.timestamp.count() % 1000000);
    auto const class_name = GetLogClassName(entry.log_class);
    auto const level_name = GetLevelName(entry.log_level);
    return fmt::format("[{:4d}.{:06d}] {} <{}> {}:{}:{}: {}", time_seconds, time_fractional,
                       class_name, level_name, entry.filename, entry.line_num, entry.function,
                       entry.message);
}'''

new_format = '''std::string FormatLogMessage(const Entry& entry) noexcept {
    if (!entry.filename) return "";

    // Real wall-clock time instead of container uptime (Patch #17)
    auto now = std::chrono::system_clock::now();
    auto time_t_now = std::chrono::system_clock::to_time_t(now);
    auto tm_now = std::localtime(&time_t_now);

    auto const level_name = GetLevelName(entry.log_level);

    // Warnings/errors include class and level; info just shows time + message
    if (entry.log_level >= Level::Warning) {
        return fmt::format("[{:02d}:{:02d}:{:02d}] {} <{}> {}",
                           tm_now->tm_hour, tm_now->tm_min, tm_now->tm_sec,
                           GetLogClassName(entry.log_class), level_name,
                           entry.message);
    }

    return fmt::format("[{:02d}:{:02d}:{:02d}] {}",
                       tm_now->tm_hour, tm_now->tm_min, tm_now->tm_sec,
                       entry.message);
}'''

if old_format in content:
    content = content.replace(old_format, new_format)
    p.write_text(content, encoding="utf-8")
    print("[OK] Patched log format to be cleaner and human-readable")
else:
    print("WARNING: Could not find FormatLogMessage function - may have changed in Eden")
PY


# Configure - RELEASE BUILD (optimized, no debug symbols)
# We cannot use YUZU_STATIC_ROOM=ON because it force-disables ENABLE_WEB_SERVICE.
# Instead disable only what we don't need: Qt, cubeb, tests, discord, update checker.
# SDL2 is always required by Eden on Linux (install libsdl2-dev above).
# Target: yuzu_room_standalone (produces binary named 'eden-room')
RUN cmake -S . -B build \
      -G Ninja \
      -DCMAKE_BUILD_TYPE=Release \
      -DENABLE_QT=OFF \
      -DENABLE_CUBEB=OFF \
      -DYUZU_TESTS=OFF \
      -DENABLE_UPDATE_CHECKER=OFF \
      -DUSE_DISCORD_PRESENCE=OFF \
      -DENABLE_WEB_SERVICE=ON \
      -DYUZU_ROOM=ON \
      -DYUZU_ROOM_STANDALONE=ON \
      -DYUZU_DISABLE_LLVM=ON \
      -DYUZU_CMD=OFF

# Build and STRIP to reduce size
# CMake target: yuzu_room_standalone  |  Output binary: eden-room
RUN cmake --build build --target yuzu_room_standalone -j"$(nproc)" && \
    strip build/bin/eden-room && \
    echo "=== BUILD COMPLETE ===" && \
    ls -lh build/bin/eden-room


###########################
# 2) Runtime stage - MINIMAL
###########################
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive

# Runtime libraries + gosu for PUID/PGID support
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      libssl3 \
      libzstd1 \
      liblz4-1 \
      libopus0 \
      zlib1g \
      libboost-context1.83.0 \
      libenet7 \
      libfmt9 \
      libmbedtls14 \
      libopenal1 \
      libavcodec60 \
      libavfilter9 \
      libavutil58 \
      libswscale7 \
      libswresample4 \
      gzip \
      gosu \
      tini \
    && rm -rf /var/lib/apt/lists/*

# Copy stripped binary
COPY --from=builder /src/build/bin/eden-room /usr/local/bin/eden-room

# Create eden user with UID/GID 911 (LinuxServer.io standard, avoids conflicts)
# Will be modified at runtime by entrypoint to match PUID/PGID
RUN groupadd -g 911 eden && \
    useradd -u 911 -g eden -m eden && \
    mkdir -p /home/eden/.local/share/eden-room && \
    chown -R eden:eden /home/eden

# Copy entrypoint (root-owned, will drop privileges at runtime)
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Environment variables for Unraid/LinuxServer.io compatibility
# Defaults: PUID=99 (nobody), PGID=100 (users) - standard for Unraid
ENV PUID=99
ENV PGID=100

WORKDIR /home/eden

EXPOSE 24872/tcp
EXPOSE 24872/udp

VOLUME ["/home/eden/.local/share/eden-room"]

# Use tini as init for proper signal handling (faster stops)
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
