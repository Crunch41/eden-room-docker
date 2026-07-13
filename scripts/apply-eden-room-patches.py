#!/usr/bin/env python3
"""Apply Eden dedicated-room hardening patches.

This script runs inside a checked-out Eden source tree. It intentionally fails
when expected upstream code moves, so Docker and CI do not silently build an
unpatched room server.
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path


ROOT = Path.cwd()


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    (ROOT / path).write_text(content, encoding="utf-8")


def replace_once(content: str, old: str, new: str, label: str) -> str:
    if old not in content:
        raise RuntimeError(_drift_message(label, old, content))
    return content.replace(old, new, 1)


def _drift_message(label: str, old: str, content: str) -> str:
    """Build a diagnostic showing how upstream likely drifted.

    When an anchor block is no longer present, the upstream source moved. Show
    the first expected line and the closest current lines so the fix is obvious
    without diffing the whole file by hand.
    """
    expected_lines = [ln for ln in old.splitlines() if ln.strip()]
    first_line = expected_lines[0] if expected_lines else ""
    msg = [
        f"{label}: expected source block not found (upstream has drifted).",
        f"  expected (first line): {first_line.strip()}",
    ]
    if first_line:
        close = difflib.get_close_matches(first_line, content.splitlines(), n=3, cutoff=0.5)
        if close:
            msg.append("  nearest current lines:")
            msg.extend(f"    {c.strip()}" for c in close)
        else:
            msg.append("  no similar line found nearby — the block may be removed or rewritten.")
    return "\n".join(msg)


def insert_include(content: str, include: str, after: str) -> str:
    if include in content:
        return content
    return replace_once(content, after, f"{after}\n{include}", f"insert {include}")


def patch_yuzu_room() -> None:
    path = "src/dedicated_room/yuzu_room.cpp"
    content = read(path)

    content = insert_include(content, "#include <atomic>", "#include <chrono>")
    content = insert_include(content, "#include <csignal>", "#include <chrono>")

    content = replace_once(
        content,
        '{"username", optional_argument, 0, \'u\'},',
        '{"username", required_argument, 0, \'u\'},',
        "make --username require an argument",
    )

    if "g_room_shutdown_requested" not in content:
        content = replace_once(
            content,
            'static constexpr char token_delimiter{\':\'};\n',
            """static constexpr char token_delimiter{':'};

namespace {
std::atomic_bool g_room_shutdown_requested{false};

void HandleShutdownSignal(int) {
    g_room_shutdown_requested.store(true);
}
} // namespace
""",
            "add room shutdown signal flag",
        )

    content = replace_once(
        content,
        """    Common::Log::Initialize();
    Common::Log::SetColorConsoleBackendEnabled(true);
    Common::Log::Start();
""",
        """    Common::Log::Initialize();
    Common::Log::SetColorConsoleBackendEnabled(true);
    Common::Log::Start();

    std::signal(SIGINT, HandleShutdownSignal);
    std::signal(SIGTERM, HandleShutdownSignal);
""",
        "install signal handlers",
    )

    content = replace_once(
        content,
        '        LOG_INFO(Network, "Room is open. Close with Q+Enter...");',
        '        LOG_INFO(Network, "Room is open. Stop the process to close it cleanly.");',
        "update room open message",
    )

    content = replace_once(
        content,
        """        while (room->GetState() == Network::Room::State::Open) {
            std::string in;
            std::cin >> in;
            if (in.size() > 0) {
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }""",
        """        while (room->GetState() == Network::Room::State::Open &&
               !g_room_shutdown_requested.load()) {
            std::this_thread::sleep_for(std::chrono::seconds(1));
        }""",
        "replace blocking stdin loop",
    )

    write(path, content)


def patch_announce_room_json() -> None:
    path = "src/web_service/announce_room_json.cpp"
    content = read(path)
    content = insert_include(content, "#include <exception>", "#include <future>")

    content = replace_once(
        content,
        """    auto reply_json = nlohmann::json::parse(result.returned_data);
    room = reply_json.get<AnnounceMultiplayerRoom::Room>();
    room_id = reply_json.at("id").get<std::string>();
    return WebService::WebResult{WebService::WebResult::Code::Success, "", room.verify_uid};""",
        """    try {
        if (result.returned_data.empty()) {
            LOG_ERROR(WebService, "Registration response is empty");
            return WebService::WebResult{WebService::WebResult::Code::WrongContent,
                                         "Empty response from server", ""};
        }

        const auto reply_json = nlohmann::json::parse(result.returned_data);
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
    }
    return WebService::WebResult{WebService::WebResult::Code::Success, "", room.verify_uid};""",
        "harden lobby registration parsing",
    )

    write(path, content)


def patch_announce_session() -> None:
    path = "src/network/announce_multiplayer_session.cpp"
    content = read(path)
    content = insert_include(content, "#include <exception>", "#include <chrono>")
    content = insert_include(content, '#include "common/logging.h"', '#include "common/assert.h"')

    content = replace_once(
        content,
        """    announce_multiplayer_thread.emplace([&](std::stop_token stoken) {
        // Invokes all current bound error callbacks.""",
        """    announce_multiplayer_thread.emplace([&](std::stop_token stoken) {
        try {
        // Invokes all current bound error callbacks.""",
        "wrap announce thread start",
    )

    content = replace_once(
        content,
        """        }
    });
}

void AnnounceMultiplayerSession::Stop()""",
        """        }
        } catch (const std::exception& e) {
            LOG_ERROR(WebService, "Announce thread failed: {}", e.what());
        } catch (...) {
            LOG_ERROR(WebService, "Announce thread failed with an unknown exception");
        }
    });
}

void AnnounceMultiplayerSession::Stop()""",
        "wrap announce thread end",
    )

    write(path, content)


def patch_verify_user_jwt() -> None:
    path = "src/web_service/verify_user_jwt.cpp"
    content = read(path)
    content = insert_include(content, "#include <mutex>", "#include <system_error>")
    content = insert_include(content, "#include <cstring>", "#include <system_error>")

    content = replace_once(
        content,
        """static std::string public_key;
std::string GetPublicKey(const std::string& host) {
    if (public_key.empty()) {
        Client client(host, "", ""); // no need for credentials here
        public_key = client.GetPlain("/jwt/external/key.pem", true).returned_data;
        if (public_key.empty()) {
            LOG_ERROR(WebService, "Could not fetch external JWT public key, verification may fail");
        } else {
            LOG_INFO(WebService, "Fetched external JWT public key (size={})", public_key.size());
        }
    }
    return public_key;
}""",
        """static std::string public_key;
static std::mutex public_key_mutex;

std::string GetPublicKey(const std::string& host) {
    std::lock_guard lock(public_key_mutex);
    if (public_key.empty()) {
        Client client(host, "", ""); // no need for credentials here
        public_key = client.GetPlain("/jwt/external/key.pem", true).returned_data;
        if (public_key.empty()) {
            LOG_ERROR(WebService, "Could not fetch external JWT public key, verification may fail");
        } else {
            LOG_INFO(WebService, "Fetched external JWT public key (size={})", public_key.size());
        }
    }
    return public_key;
}""",
        "protect JWT public key cache",
    )

    content = replace_once(
        content,
        """    if (error) {
        LOG_INFO(WebService, "Verification failed: category={}, code={}, message={}",
                 error.category().name(), error.value(), error.message());
        return {};
    }""",
        """    if (error) {
        // Unauthenticated clients send an empty token, which cpp-jwt reports as
        // DecodeErrc::SignatureFormatError (value 2, category "decode") because
        // the token has fewer than two dots. Only that exact case is routine
        // noise. Value 2 in OTHER categories is a real failure that must stay
        // visible: "verification"/2 is TokenExpired and "algorithms"/2 is
        // VerificationErr (bad signature).
        const bool expected_unauthenticated =
            error.value() == 2 && std::strcmp(error.category().name(), "decode") == 0;
        if (!expected_unauthenticated) {
            LOG_INFO(WebService, "JWT verification failed: category={}, code={}, message={}",
                     error.category().name(), error.value(), error.message());
        }
        return {};
    }""",
        "suppress expected unauthenticated JWT noise (decode/2 only)",
    )

    write(path, content)


def patch_console_log_flush() -> None:
    path = "src/common/logging.cpp"
    content = read(path)

    content = insert_include(content, "#include <cstdio>", "#include <climits>")
    content = insert_include(content, "#include <ctime>", "#include <cstdio>")
    content = insert_include(content, "#include <cstring>", "#include <ctime>")

    content = replace_once(
        content,
        """std::string FormatLogMessage(const Entry& entry) noexcept {
    if (!entry.filename) return "";
    auto const time_seconds = uint32_t(entry.timestamp.count() / 1000000);
    auto const time_fractional = uint32_t(entry.timestamp.count() % 1000000);
    auto const class_name = GetLogClassName(entry.log_class);
    auto const level_name = GetLevelName(entry.log_level);
    return fmt::format("[{:4d}.{:06d}] {} <{}> {}:{}:{}: {}", time_seconds, time_fractional, class_name, level_name, entry.filename, entry.line_num, entry.function, entry.message);
}""",
        """std::string FormatLogMessage(const Entry& entry) noexcept {
    if (!entry.filename) return "";

    const auto now = std::chrono::system_clock::now();
    const auto time_now = std::chrono::system_clock::to_time_t(now);
    std::tm local_time{};
#ifdef _WIN32
    localtime_s(&local_time, &time_now);
#else
    localtime_r(&time_now, &local_time);
#endif

    auto const class_name = GetLogClassName(entry.log_class);
    auto const level_name = GetLevelName(entry.log_level);
    const auto function_name = entry.function ? entry.function : "";
    const auto& message = entry.message;

    const bool is_network_info =
        entry.log_level == Level::Info && std::strcmp(class_name, "Network") == 0;
    const bool is_status_message = std::strcmp(function_name, "SendStatusMessage") == 0;
    const bool is_chat_message = std::strcmp(function_name, "HandleChatPacket") == 0;
    const bool is_game_info = std::strcmp(function_name, "HandleGameInfoPacket") == 0;
    const bool is_rtt_log = std::strcmp(function_name, "HandleJoinRequest") == 0 &&
        message.find("RTT") != std::string::npos;
    const bool is_stat = std::strcmp(function_name, "HandleClientDisconnection") == 0 &&
        message.find("session RTT") != std::string::npos;

    if (is_network_info && is_status_message &&
        message.find("has joined.") != std::string::npos) {
        return fmt::format("[{:02d}:{:02d}:{:02d}] JOIN  | {}", local_time.tm_hour,
                           local_time.tm_min, local_time.tm_sec, message);
    }
    if (is_network_info && is_status_message &&
        message.find("has left.") != std::string::npos) {
        return fmt::format("[{:02d}:{:02d}:{:02d}] LEAVE | {}", local_time.tm_hour,
                           local_time.tm_min, local_time.tm_sec, message);
    }
    if (is_network_info && is_chat_message) {
        return fmt::format("[{:02d}:{:02d}:{:02d}] CHAT  | {}", local_time.tm_hour,
                           local_time.tm_min, local_time.tm_sec, message);
    }
    if (is_network_info && is_game_info &&
        (message.find(" is playing ") != std::string::npos ||
         message.find(" is not playing") != std::string::npos)) {
        return fmt::format("[{:02d}:{:02d}:{:02d}] GAME  | {}", local_time.tm_hour,
                           local_time.tm_min, local_time.tm_sec, message);
    }
    if (is_network_info && is_rtt_log) {
        return fmt::format("[{:02d}:{:02d}:{:02d}] PING  | {}", local_time.tm_hour,
                           local_time.tm_min, local_time.tm_sec, message);
    }
    if (is_network_info && is_stat) {
        return fmt::format("[{:02d}:{:02d}:{:02d}] STAT  | {}", local_time.tm_hour,
                           local_time.tm_min, local_time.tm_sec, message);
    }

    if (entry.log_level >= Level::Warning) {
        return fmt::format("[{:02d}:{:02d}:{:02d}] {} <{}> {}", local_time.tm_hour,
                           local_time.tm_min, local_time.tm_sec, class_name, level_name,
                           message);
    }

    return fmt::format("[{:02d}:{:02d}:{:02d}] {}", local_time.tm_hour, local_time.tm_min,
                       local_time.tm_sec, message);
}""",
        "format Docker room activity logs",
    )

    content = replace_once(
        content,
        """            auto const df = GetDirectFormatArgs(entry);
            std::fprintf(stdout, CCB_PRINTF_FMT "\\n", df.time_seconds, df.time_fractional, df.class_name, df.level_name, entry.filename, entry.line_num, entry.function, entry.message.c_str());""",
        """            const auto message = FormatLogMessage(entry);
            std::fprintf(stdout, "%s\\n", message.c_str());
            std::fflush(stdout);""",
        "flush Windows console log writes",
    )

    content = replace_once(
        content,
        """#define ESC "\\x1b"
            auto const color_str = [&entry]() -> const char* {
                switch (entry.log_level) {
#define CCB_MAKE_COLOR_FMT(X) ESC X CCB_PRINTF_FMT ESC "[0m\\n"
                case Level::Debug: return CCB_MAKE_COLOR_FMT("[0;36m"); // Cyan
                case Level::Info: return CCB_MAKE_COLOR_FMT("[0;37m"); // Bright gray
                case Level::Warning: return CCB_MAKE_COLOR_FMT("[1;33m"); // Bright yellow
                case Level::Error: return CCB_MAKE_COLOR_FMT("[1;31m"); // Bright red
                case Level::Critical: return CCB_MAKE_COLOR_FMT("[1;35m"); // Bright magenta
                default: return CCB_MAKE_COLOR_FMT("[1;30m"); // Grey
#undef CCB_MAKE_COLOR_FMT
                }
            }();
            auto const df = GetDirectFormatArgs(entry);
            std::fprintf(stdout, color_str, df.time_seconds, df.time_fractional, df.class_name, df.level_name, entry.filename, entry.line_num, entry.function, entry.message.c_str());
#undef ESC""",
        """            const auto message = FormatLogMessage(entry);
            std::fprintf(stdout, "%s\\n", message.c_str());
            std::fflush(stdout);""",
        "flush POSIX console log writes",
    )

    content = replace_once(
        content,
        "    void Flush() noexcept override {}\n    std::atomic_bool enabled = false;\n#endif",
        "    void Flush() noexcept override { std::fflush(stdout); }\n    std::atomic_bool enabled = false;\n#endif",
        "flush POSIX console backend",
    )

    write(path, content)


def patch_room() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    content = insert_include(content, "#include <chrono>", "#include <atomic>")
    content = insert_include(content, "#include <cstdlib>", "#include <chrono>")
    content = insert_include(content, "#include <cstring>", "#include <cstdlib>")
    content = insert_include(content, "#include <exception>", "#include <cstring>")
    content = insert_include(content, "#include <unordered_map>", "#include <thread>")

    if "UnknownIpFallbackEnabled" not in content:
        content = replace_once(
            content,
            "namespace Network {\n",
            """namespace Network {

namespace {
bool UnknownIpFallbackEnabled() {
    const char* value = std::getenv("EDEN_ROOM_UNKNOWN_IP_FALLBACK");
    return value == nullptr || std::strcmp(value, "drop") != 0;
}
} // namespace
""",
            "add unknown IP fallback helper",
        )

    content = replace_once(
        content,
        """    IPBanList ip_ban_list;             ///< List of banned IP addresses
    mutable std::mutex ban_list_mutex; ///< Mutex for the ban lists

    RoomImpl() {}""",
        """    IPBanList ip_ban_list;             ///< List of banned IP addresses
    mutable std::mutex ban_list_mutex; ///< Mutex for the ban lists

    std::unordered_map<u32, std::chrono::steady_clock::time_point> last_join_attempt;
    static constexpr auto JoinRateLimit = std::chrono::seconds(1);
    static constexpr auto JoinRateLimitPruneAge = std::chrono::minutes(10);

    // ENet calls enet_peer_reset() before firing ENET_EVENT_TYPE_DISCONNECT,
    // which zeroes roundTripTime (back to the 500ms default), packetLoss, and
    // all data counters. Snapshot RTT and join timestamp here so disconnect
    // logging can report real values instead of the wiped ENet defaults.
    struct PeerSnapshot {
        u32 rtt{500};
        std::chrono::steady_clock::time_point join_time;
    };
    std::unordered_map<ENetPeer*, PeerSnapshot> peer_snapshot_cache;

    RoomImpl() {}""",
        "add join rate-limit state and peer snapshot cache",
    )

    content = replace_once(
        content,
        """        while (state != State::Closed) {
            ENetEvent event;
            if (enet_host_service(server, &event, 5) > 0) {
                switch (event.type) {
                case ENET_EVENT_TYPE_RECEIVE:
                    switch (event.packet->data[0]) {""",
        """        while (state != State::Closed) {
            try {
            ENetEvent event;
            if (enet_host_service(server, &event, 5) > 0) {
                switch (event.type) {
                case ENET_EVENT_TYPE_RECEIVE:
                    if (event.packet == nullptr || event.packet->data == nullptr ||
                        event.packet->dataLength == 0) {
                        LOG_WARNING(Network, "Dropping empty room packet");
                        if (event.packet != nullptr) {
                            enet_packet_destroy(event.packet);
                        }
                        break;
                    }
                    switch (event.packet->data[0]) {""",
        "guard empty receive packets",
    )

    content = replace_once(
        content,
        """                }
            }
        }
        // Close the connection to all members:""",
        """                }
            }
            } catch (const std::exception& e) {
                LOG_ERROR(Network, "Room loop error: {}", e.what());
            } catch (...) {
                LOG_ERROR(Network, "Room loop unknown error");
            }
        }
        // Close the connection to all members:""",
        "wrap room event loop",
    )

    content = replace_once(
        content,
        """void Room::RoomImpl::HandleJoinRequest(const ENetEvent* event) {
    {
        std::lock_guard lock(member_mutex);""",
        """void Room::RoomImpl::HandleJoinRequest(const ENetEvent* event) {
    {
        auto now = std::chrono::steady_clock::now();
        u32 client_ip = event->peer->address.host;

        for (auto it = last_join_attempt.begin(); it != last_join_attempt.end();) {
            if (now - it->second > JoinRateLimitPruneAge) {
                it = last_join_attempt.erase(it);
            } else {
                ++it;
            }
        }

        auto it = last_join_attempt.find(client_ip);
        if (it != last_join_attempt.end() && now - it->second < JoinRateLimit) {
            LOG_WARNING(Network, "Rate limiting join request");
            return;
        }
        last_join_attempt[client_ip] = now;
    }

    {
        std::lock_guard lock(member_mutex);""",
        "rate-limit join requests",
    )

    content = replace_once(
        content,
        """    std::string token;
    packet.Read(token);

    if (pass != password) {""",
        """    std::string token;
    packet.Read(token);

    if (!packet) {
        LOG_WARNING(Network, "Malformed join request");
        return;
    }

    if (pass != password) {""",
        "validate join packet parse",
    )

    content = replace_once(
        content,
        """    if (!room_information.host_username.empty() &&
        sending_member->user_data.username == room_information.host_username) { // Room host

        return true;
    }
    return false;""",
        """    // EDEN_ROOM_MOD_USERNAME sets the moderator username independently of the
    // lobby --username so the mod nick can differ from the announcing account.
    // Falls back to host_username when the env var is absent or empty.
    const char* mod_env = std::getenv("EDEN_ROOM_MOD_USERNAME");
    const std::string mod_username =
        (mod_env != nullptr && mod_env[0] != '\\0')
            ? std::string(mod_env)
            : room_information.host_username;

    if (mod_username.empty()) {
        return false;
    }

    // Moderator status is only granted to connections from RFC 1918 / loopback
    // addresses. Remote IPs are never elevated even if the username matches.
    const auto* peer_ip =
        reinterpret_cast<const uint8_t*>(&sending_member->peer->address.host);
    const bool is_local =
        peer_ip[0] == 10 ||
        (peer_ip[0] == 172 && peer_ip[1] >= 16 && peer_ip[1] <= 31) ||
        (peer_ip[0] == 192 && peer_ip[1] == 168) ||
        peer_ip[0] == 127;

    if (!is_local) {
        return false;
    }

    if (sending_member->user_data.username == mod_username) { // JWT path
        return true;
    }
    if (sending_member->nickname == mod_username) { // LAN / no-JWT path
        return true;
    }
    return false;""",
        "moderator: local-subnet gate + EDEN_ROOM_MOD_USERNAME",
    )

    content = replace_once(
        content,
        """    packet.Write(room_information.host_username);

    packet.Write(static_cast<u32>(members.size()));
    {
        std::lock_guard lock(member_mutex);
        for (const auto& member : members) {""",
        """    packet.Write(room_information.host_username);

    {
        std::lock_guard lock(member_mutex);
        packet.Write(static_cast<u32>(members.size()));
        for (const auto& member : members) {""",
        "serialize room member count under lock",
    )

    content = replace_once(
        content,
        """    const std::string display_name =
        username.empty() ? nickname : fmt::format("{} ({})", nickname, username);

    switch (type) {""",
        """    const std::string display_name =
        username.empty() ? nickname : fmt::format("{} ({})", nickname, username);
    const auto displayed_member_count =
        type == IdMemberJoin ? members.size() + 1 : members.size();
    const auto member_slots = room_information.member_slots;

    switch (type) {""",
        "add player count to status logs",
    )

    content = replace_once(
        content,
        """    case IdMemberJoin:
        LOG_INFO(Network, "[{}] {} has joined.", ip, display_name);
        break;
    case IdMemberLeave:
        LOG_INFO(Network, "[{}] {} has left.", ip, display_name);
        break;
    case IdMemberKicked:
        LOG_INFO(Network, "[{}] {} has been kicked.", ip, display_name);
        break;
    case IdMemberBanned:
        LOG_INFO(Network, "[{}] {} has been banned.", ip, display_name);
        break;""",
        """    case IdMemberJoin:
        LOG_INFO(Network, "[{}] {} has joined. ({}/{})", ip, display_name,
                 displayed_member_count, member_slots);
        break;
    case IdMemberLeave:
        LOG_INFO(Network, "[{}] {} has left. ({}/{})", ip, display_name,
                 displayed_member_count, member_slots);
        break;
    case IdMemberKicked:
        LOG_INFO(Network, "[{}] {} has been kicked. ({}/{})", ip, display_name,
                 displayed_member_count, member_slots);
        break;
    case IdMemberBanned:
        LOG_INFO(Network, "[{}] {} has been banned. ({}/{})", ip, display_name,
                 displayed_member_count, member_slots);
        break;""",
        "format player count in status logs",
    )

    content = replace_once(
        content,
        """void Room::RoomImpl::HandleProxyPacket(const ENetEvent* event) {
    Packet in_packet;""",
        """void Room::RoomImpl::HandleProxyPacket(const ENetEvent* event) {
    constexpr std::size_t ProxyHeaderSize =
        sizeof(u8) + sizeof(u8) + sizeof(IPv4Address) + sizeof(u16) + sizeof(u8) +
        sizeof(IPv4Address) + sizeof(u16) + sizeof(u8) + sizeof(u8);
    if (event->packet->dataLength < ProxyHeaderSize) {
        LOG_WARNING(Network, "Dropping malformed proxy packet ({} bytes)",
                    event->packet->dataLength);
        return;
    }

    Packet in_packet;""",
        "add proxy packet minimum size check",
    )

    content = replace_once(
        content,
        """    bool broadcast;
    in_packet.Read(broadcast); // Broadcast

    Packet out_packet;""",
        """    bool broadcast;
    in_packet.Read(broadcast); // Broadcast

    if (!in_packet) {
        LOG_WARNING(Network, "Dropping malformed proxy packet");
        return;
    }

    Packet out_packet;""",
        "validate proxy packet parse",
    )

    content = replace_once(
        content,
        """            LOG_ERROR(Network,
                      "Attempting to send to unknown IP address: "
                      "{}.{}.{}.{}",
                      destination_address[0], destination_address[1], destination_address[2],
                      destination_address[3]);
            enet_packet_destroy(enet_packet);""",
        """            LOG_DEBUG(Network,
                      "Proxy packet to unknown IP address: "
                      "{}.{}.{}.{}",
                      destination_address[0], destination_address[1], destination_address[2],
                      destination_address[3]);
            if (UnknownIpFallbackEnabled()) {
                bool sent_packet = false;
                for (const auto& dest_member : members) {
                    if (dest_member.peer != event->peer) {
                        sent_packet = true;
                        enet_peer_send(dest_member.peer, 0, enet_packet);
                    }
                }
                if (!sent_packet) {
                    enet_packet_destroy(enet_packet);
                }
            } else {
                enet_packet_destroy(enet_packet);
            }""",
        "proxy unknown-IP fallback",
    )

    content = replace_once(
        content,
        """void Room::RoomImpl::HandleLdnPacket(const ENetEvent* event) {
    Packet in_packet;""",
        """void Room::RoomImpl::HandleLdnPacket(const ENetEvent* event) {
    constexpr std::size_t LdnHeaderSize =
        sizeof(u8) + sizeof(u8) + sizeof(IPv4Address) + sizeof(IPv4Address) + sizeof(u8);
    if (event->packet->dataLength < LdnHeaderSize) {
        LOG_WARNING(Network, "Dropping malformed LDN packet ({} bytes)",
                    event->packet->dataLength);
        return;
    }

    Packet in_packet;""",
        "add LDN packet minimum size check",
    )

    content = replace_once(
        content,
        """    bool broadcast;
    in_packet.Read(broadcast); // Broadcast

    Packet out_packet;""",
        """    bool broadcast;
    in_packet.Read(broadcast); // Broadcast

    if (!in_packet) {
        LOG_WARNING(Network, "Dropping malformed LDN packet");
        return;
    }

    Packet out_packet;""",
        "validate LDN packet parse",
    )

    content = replace_once(
        content,
        """            LOG_ERROR(Network,
                      "Attempting to send to unknown IP address: "
                      "{}.{}.{}.{}",
                      destination_address[0], destination_address[1], destination_address[2],
                      destination_address[3]);
            enet_packet_destroy(enet_packet);""",
        """            LOG_DEBUG(Network,
                      "LDN packet to unknown IP address: "
                      "{}.{}.{}.{}",
                      destination_address[0], destination_address[1], destination_address[2],
                      destination_address[3]);
            if (UnknownIpFallbackEnabled()) {
                bool sent_packet = false;
                for (const auto& dest_member : members) {
                    if (dest_member.peer != event->peer) {
                        sent_packet = true;
                        enet_peer_send(dest_member.peer, 0, enet_packet);
                    }
                }
                if (!sent_packet) {
                    enet_packet_destroy(enet_packet);
                }
            } else {
                enet_packet_destroy(enet_packet);
            }""",
        "LDN unknown-IP fallback",
    )

    for label, needle in [
        ("kick", "    std::string nickname;\n    packet.Read(nickname);\n\n    std::string username, ip;"),
        ("ban", "    std::string nickname;\n    packet.Read(nickname);\n\n    std::string username, ip;"),
        ("unban", "    std::string address;\n    packet.Read(address);\n\n    bool unbanned = false;"),
    ]:
        replacement = needle.replace(
            "\n\n",
            f'\n\n    if (!packet) {{\n        LOG_WARNING(Network, "Malformed moderation {label} request");\n        return;\n    }}\n\n',
            1,
        )
        content = replace_once(content, needle, replacement, f"validate moderation {label}")

    content = replace_once(
        content,
        """    std::string message;
    in_packet.Read(message);
    auto CompareNetworkAddress = [event](const Member member) -> bool {""",
        """    std::string message;
    in_packet.Read(message);
    if (!in_packet) {
        LOG_WARNING(Network, "Dropping malformed chat packet");
        return;
    }
    auto CompareNetworkAddress = [event](const Member member) -> bool {""",
        "validate chat packet parse",
    )

    content = replace_once(
        content,
        """    in_packet.Read(game_info.name);
    in_packet.Read(game_info.id);
    in_packet.Read(game_info.version);

    {""",
        """    in_packet.Read(game_info.name);
    in_packet.Read(game_info.id);
    in_packet.Read(game_info.version);

    if (!in_packet) {
        LOG_WARNING(Network, "Dropping malformed game info packet");
        return;
    }

    {""",
        "validate game info packet parse",
    )

    write(path, content)


def patch_loop_drain() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Runs AFTER patch_room(): anchors target the try/catch-wrapped loop so the
    # dispatch lambda inherits the exception guard and empty-packet checks.
    content = replace_once(
        content,
        """            ENetEvent event;
            if (enet_host_service(server, &event, 5) > 0) {
                switch (event.type) {""",
        """            ENetEvent event;
            const auto dispatch = [&] {
                switch (event.type) {""",
        "loop drain: open dispatch lambda",
    )

    content = replace_once(
        content,
        """                }
            }
            } catch (const std::exception& e) {""",
        """                }
            };

            // Drain every event ENet has already queued, then flush so relayed
            // packets those dispatches produced are transmitted before we block.
            // enet_host_check_events does no socket I/O, so without the flush a
            // relayed packet would sit in ENet's send queue until the next
            // enet_host_service call. (enet_host_service itself already returns
            // queued events without waiting, so draining adds no dispatch-latency
            // win on its own — the flush is the point.)
            bool dispatched_any = false;
            while (enet_host_check_events(server, &event) > 0) {
                dispatch();
                dispatched_any = true;
            }
            if (dispatched_any) {
                enet_host_flush(server);
            }
            if (enet_host_service(server, &event, 1) > 0) {
                dispatch();
            }
            } catch (const std::exception& e) {""",
        "loop drain: drain queued events, flush, then short service wait",
    )

    write(path, content)


def patch_peer_timeout() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    content = replace_once(
        content,
        """void Room::RoomImpl::SendJoinSuccess(ENetPeer* client, IPv4Address fake_ip) {
    Packet packet;
    packet.Write(static_cast<u8>(IdJoinSuccess));
    packet.Write(fake_ip);
    ENetPacket* enet_packet =
        enet_packet_create(packet.GetData(), packet.GetDataSize(), ENET_PACKET_FLAG_RELIABLE);
    enet_peer_send(client, 0, enet_packet);
    enet_host_flush(server);
}""",
        """void Room::RoomImpl::SendJoinSuccess(ENetPeer* client, IPv4Address fake_ip) {
    Packet packet;
    packet.Write(static_cast<u8>(IdJoinSuccess));
    packet.Write(fake_ip);
    ENetPacket* enet_packet =
        enet_packet_create(packet.GetData(), packet.GetDataSize(), ENET_PACKET_FLAG_RELIABLE);
    enet_peer_send(client, 0, enet_packet);
    enet_peer_timeout(client, ENET_PEER_TIMEOUT_LIMIT, 12000, 60000);
    enet_host_flush(server);
}""",
        "set peer timeout in SendJoinSuccess",
    )

    content = replace_once(
        content,
        """void Room::RoomImpl::SendJoinSuccessAsMod(ENetPeer* client, IPv4Address fake_ip) {
    Packet packet;
    packet.Write(static_cast<u8>(IdJoinSuccessAsMod));
    packet.Write(fake_ip);
    ENetPacket* enet_packet =
        enet_packet_create(packet.GetData(), packet.GetDataSize(), ENET_PACKET_FLAG_RELIABLE);
    enet_peer_send(client, 0, enet_packet);
    enet_host_flush(server);
}""",
        """void Room::RoomImpl::SendJoinSuccessAsMod(ENetPeer* client, IPv4Address fake_ip) {
    Packet packet;
    packet.Write(static_cast<u8>(IdJoinSuccessAsMod));
    packet.Write(fake_ip);
    ENetPacket* enet_packet =
        enet_packet_create(packet.GetData(), packet.GetDataSize(), ENET_PACKET_FLAG_RELIABLE);
    enet_peer_send(client, 0, enet_packet);
    enet_peer_timeout(client, ENET_PEER_TIMEOUT_LIMIT, 12000, 60000);
    enet_host_flush(server);
}""",
        "set peer timeout in SendJoinSuccessAsMod",
    )

    write(path, content)


def patch_ping_interval() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Runs AFTER patch_peer_timeout(): anchors include the timeout call it adds.
    for func, msg_id in (("SendJoinSuccess", "IdJoinSuccess"),
                         ("SendJoinSuccessAsMod", "IdJoinSuccessAsMod")):
        old = (
            f"void Room::RoomImpl::{func}(ENetPeer* client, IPv4Address fake_ip) {{\n"
            "    Packet packet;\n"
            f"    packet.Write(static_cast<u8>({msg_id}));\n"
            "    packet.Write(fake_ip);\n"
            "    ENetPacket* enet_packet =\n"
            "        enet_packet_create(packet.GetData(), packet.GetDataSize(), ENET_PACKET_FLAG_RELIABLE);\n"
            "    enet_peer_send(client, 0, enet_packet);\n"
            "    enet_peer_timeout(client, ENET_PEER_TIMEOUT_LIMIT, 12000, 60000);\n"
            "    enet_host_flush(server);\n"
            "}"
        )
        new = old.replace(
            "    enet_peer_timeout(client, ENET_PEER_TIMEOUT_LIMIT, 12000, 60000);\n",
            "    enet_peer_timeout(client, ENET_PEER_TIMEOUT_LIMIT, 12000, 60000);\n"
            "    enet_peer_ping_interval(client, 100);\n",
            1,
        )
        content = replace_once(content, old, new, f"set ping interval in {func}")

    write(path, content)


def patch_env_tunables() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Runs AFTER patch_room() (helper namespace + RoomImpl snapshot block) and
    # AFTER patch_ping_interval() (timeout + ping lines exist in both senders).
    content = replace_once(
        content,
        """    return value == nullptr || std::strcmp(value, "drop") != 0;
}
} // namespace""",
        """    return value == nullptr || std::strcmp(value, "drop") != 0;
}

u32 EnvMs(const char* name, u32 fallback) {
    const char* value = std::getenv(name);
    if (value == nullptr || *value == '\\0') {
        return fallback;
    }
    char* end = nullptr;
    const unsigned long parsed = std::strtoul(value, &end, 10);
    if (end == value || *end != '\\0' || parsed == 0 || parsed > 3600000UL) {
        LOG_WARNING(Network, "Ignoring invalid {} value '{}'", name, value);
        return fallback;
    }
    return static_cast<u32>(parsed);
}
} // namespace""",
        "add millisecond env parser",
    )

    content = replace_once(
        content,
        """    std::unordered_map<ENetPeer*, PeerSnapshot> peer_snapshot_cache;

    RoomImpl() {}""",
        """    std::unordered_map<ENetPeer*, PeerSnapshot> peer_snapshot_cache;

    // ENet peer tuning, read once per room. Max is clamped to >= min.
    const u32 peer_timeout_min_ms = EnvMs("EDEN_ROOM_PEER_TIMEOUT_MIN", 12000);
    const u32 peer_timeout_max_ms =
        (std::max)(peer_timeout_min_ms, EnvMs("EDEN_ROOM_PEER_TIMEOUT_MAX", 60000));
    const u32 ping_interval_ms = EnvMs("EDEN_ROOM_PING_INTERVAL", 100);

    RoomImpl() {}""",
        "add env-tunable peer timing fields",
    )

    # Replace the hardcoded values in both join-success senders (two occurrences).
    for _ in range(2):
        content = replace_once(
            content,
            "    enet_peer_timeout(client, ENET_PEER_TIMEOUT_LIMIT, 12000, 60000);\n"
            "    enet_peer_ping_interval(client, 100);",
            "    enet_peer_timeout(client, ENET_PEER_TIMEOUT_LIMIT, peer_timeout_min_ms, peer_timeout_max_ms);\n"
            "    enet_peer_ping_interval(client, ping_interval_ms);",
            "apply env peer tuning",
        )

    write(path, content)


def patch_rtt_logging() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    content = replace_once(
        content,
        """    // Notify everyone that the user has joined.
    SendStatusMessage(IdMemberJoin, member.nickname, member.user_data.username, ip);

    {
        std::lock_guard lock(member_mutex);""",
        """    // Notify everyone that the user has joined.
    SendStatusMessage(IdMemberJoin, member.nickname, member.user_data.username, ip);
    LOG_INFO(Network, "[{}] {} RTT {}ms", ip, member.nickname, event->peer->roundTripTime);
    // Save RTT and join time now — ENet will zero these before the disconnect event fires.
    peer_snapshot_cache[event->peer] = {event->peer->roundTripTime, std::chrono::steady_clock::now()};

    {
        std::lock_guard lock(member_mutex);""",
        "log peer RTT at join and snapshot peer state",
    )

    write(path, content)


def patch_relay_flags() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    content = replace_once(
        content,
        """        LOG_WARNING(Network, "Dropping malformed proxy packet");
        return;
    }

    Packet out_packet;
    out_packet.Append(event->packet->data, event->packet->dataLength);
    ENetPacket* enet_packet = enet_packet_create(out_packet.GetData(), out_packet.GetDataSize(),
                                                 ENET_PACKET_FLAG_RELIABLE);""",
        """        LOG_WARNING(Network, "Dropping malformed proxy packet");
        return;
    }

    Packet out_packet;
    out_packet.Append(event->packet->data, event->packet->dataLength);
    ENetPacket* enet_packet = enet_packet_create(out_packet.GetData(), out_packet.GetDataSize(),
                                                 ENET_PACKET_FLAG_UNSEQUENCED);""",
        "proxy relay: RELIABLE -> UNSEQUENCED",
    )

    content = replace_once(
        content,
        """        LOG_WARNING(Network, "Dropping malformed LDN packet");
        return;
    }

    Packet out_packet;
    out_packet.Append(event->packet->data, event->packet->dataLength);
    ENetPacket* enet_packet = enet_packet_create(out_packet.GetData(), out_packet.GetDataSize(),
                                                 ENET_PACKET_FLAG_RELIABLE);""",
        """        LOG_WARNING(Network, "Dropping malformed LDN packet");
        return;
    }

    Packet out_packet;
    out_packet.Append(event->packet->data, event->packet->dataLength);
    ENetPacket* enet_packet = enet_packet_create(out_packet.GetData(), out_packet.GetDataSize(),
                                                 ENET_PACKET_FLAG_UNSEQUENCED);""",
        "LDN relay: RELIABLE -> UNSEQUENCED",
    )

    write(path, content)


def patch_relay_flag_env() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Runs AFTER patch_relay_flags(): rewrites UNSEQUENCED to RelayPacketFlag().
    content = replace_once(
        content,
        "void Room::RoomImpl::HandleProxyPacket(const ENetEvent* event) {",
        """namespace {
// Relay delivery mode for proxy/LDN game packets (EDEN_ROOM_RELAY_MODE):
//   unsequenced (default) - ENET_PACKET_FLAG_UNSEQUENCED. No retransmission
//       and no ordering. Lowest latency, but internet reordering reaches the
//       game — something real 802.11 LDN never shows, because the Wi-Fi MAC
//       ACKs and retransmits unicast frames (near-lossless, in-order).
//   sequenced - flag 0, ENet unreliable-sequenced. No retransmission; late
//       out-of-order packets are DISCARDED, so the game sees an in-order
//       stream with gaps. Closest match to real 802.11 delivery over lossy
//       internet paths; first thing to try when a title desyncs.
//   reliable - upstream ENET_PACKET_FLAG_RELIABLE. Retransmits everything
//       (head-of-line blocking on lossy paths).
// Legacy EDEN_ROOM_RELAY_RELIABLE=1 still maps to reliable when
// EDEN_ROOM_RELAY_MODE is unset, so existing deployments keep working.
//
// Non-reliable modes also set ENET_PACKET_FLAG_UNRELIABLE_FRAGMENT: Pia
// frames can legitimately exceed ENet's fragmentation threshold (~1366
// bytes at the default 1392 MTU), and without this flag enet_peer_send
// silently sends such packets as RELIABLE fragments — reintroducing
// head-of-line blocking for exactly the largest game packets.
enet_uint32 RelayPacketFlag() {
    static const enet_uint32 flag = [] {
        const char* mode = std::getenv("EDEN_ROOM_RELAY_MODE");
        constexpr enet_uint32 unsequenced =
            ENET_PACKET_FLAG_UNSEQUENCED | ENET_PACKET_FLAG_UNRELIABLE_FRAGMENT;
        constexpr enet_uint32 sequenced = ENET_PACKET_FLAG_UNRELIABLE_FRAGMENT;
        if (mode == nullptr || *mode == '\\0') {
            const char* legacy = std::getenv("EDEN_ROOM_RELAY_RELIABLE");
            if (legacy != nullptr && std::strcmp(legacy, "1") == 0) {
                return static_cast<enet_uint32>(ENET_PACKET_FLAG_RELIABLE);
            }
            return unsequenced;
        }
        if (std::strcmp(mode, "reliable") == 0) {
            return static_cast<enet_uint32>(ENET_PACKET_FLAG_RELIABLE);
        }
        if (std::strcmp(mode, "sequenced") == 0) {
            return sequenced; // unreliable-sequenced
        }
        if (std::strcmp(mode, "unsequenced") != 0) {
            LOG_WARNING(Network, "Unknown EDEN_ROOM_RELAY_MODE '{}', using unsequenced", mode);
        }
        return unsequenced;
    }();
    return flag;
}

// Per-sender relay byte budget per second (EDEN_ROOM_RELAY_BUDGET_KBPS).
// 0 (default) disables the budget. Each relayed packet fans out to up to
// member_slots-1 peers, so egress amplification is ingress x fan-out; the
// budget bounds what one member can make the server transmit.
u64 RelayBudgetBytesPerSec() {
    static const u64 budget = [] {
        const char* value = std::getenv("EDEN_ROOM_RELAY_BUDGET_KBPS");
        if (value == nullptr || *value == '\\0') {
            return u64{0};
        }
        char* end = nullptr;
        const unsigned long long parsed = std::strtoull(value, &end, 10);
        if (end == value || *end != '\\0' || parsed > 1000000ULL) {
            LOG_WARNING(Network, "Ignoring invalid EDEN_ROOM_RELAY_BUDGET_KBPS '{}'", value);
            return u64{0};
        }
        return static_cast<u64>(parsed) * 1024;
    }();
    return budget;
}
} // namespace

void Room::RoomImpl::HandleProxyPacket(const ENetEvent* event) {""",
        "add relay mode and rate budget env helpers",
    )

    # Replace UNSEQUENCED in both proxy and LDN packet create calls.
    for _ in range(2):
        content = replace_once(
            content,
            "    ENetPacket* enet_packet = enet_packet_create(out_packet.GetData(), out_packet.GetDataSize(),\n"
            "                                                 ENET_PACKET_FLAG_UNSEQUENCED);",
            "    ENetPacket* enet_packet = enet_packet_create(out_packet.GetData(), out_packet.GetDataSize(),\n"
            "                                                 RelayPacketFlag());",
            "relay flag via env",
        )

    write(path, content)


def patch_relay_size_cap() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Runs AFTER patch_room(): anchors are the minimum-header checks it adds.
    # Nintendo Pia (the netcode library behind MK8DX/Smash/Splatoon LDN play)
    # emits UDP payloads up to ~1472 bytes (sized to fit a 1500-byte Ethernet
    # MTU), and Eden's room wrapper adds ~15-21 bytes, so LEGITIMATE relay
    # packets can reach ~1493 bytes. The cap must sit above that — an earlier
    # 1350 cap would have dropped real game traffic. 1536 passes all of it
    # with margin while still bounding per-packet broadcast amplification.
    # Packets above ENet's true fragmentation threshold (peer->mtu 1392 minus
    # protocol headers, ~1366 bytes — ENET_PROTOCOL_MAXIMUM_MTU (4096) is NOT
    # the threshold) get fragmented; RelayPacketFlag() sets
    # ENET_PACKET_FLAG_UNRELIABLE_FRAGMENT in the non-reliable modes so those
    # fragments stay unreliable instead of taking ENet's silent RELIABLE
    # fallback (which would reintroduce head-of-line blocking).
    for kind in ("proxy", "LDN"):
        content = replace_once(
            content,
            f'        LOG_WARNING(Network, "Dropping malformed {kind} packet ({{}} bytes)",\n'
            "                    event->packet->dataLength);\n"
            "        return;\n"
            "    }\n"
            "\n"
            "    Packet in_packet;",
            f'        LOG_WARNING(Network, "Dropping malformed {kind} packet ({{}} bytes)",\n'
            "                    event->packet->dataLength);\n"
            "        return;\n"
            "    }\n"
            "    constexpr std::size_t MaxRelayPayloadSize = 1536; // > max legit Pia frame (~1472) + room wrapper\n"
            "    if (event->packet->dataLength > MaxRelayPayloadSize) {\n"
            f'        LOG_WARNING(Network, "Dropping oversized {kind} packet ({{}} bytes)",\n'
            "                    event->packet->dataLength);\n"
            "        return;\n"
            "    }\n"
            "\n"
            "    Packet in_packet;",
            f"cap {kind} relay payload size",
        )

    write(path, content)


def patch_relay_rate_budget() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Runs AFTER patch_env_tunables() (RoomImpl field anchor), AFTER
    # patch_relay_size_cap() (handler anchors), AFTER patch_relay_flag_env()
    # (RelayBudgetBytesPerSec helper), and AFTER patch_disconnect_stats()
    # (cleanup anchor). Disabled by default (EDEN_ROOM_RELAY_BUDGET_KBPS=0):
    # a too-low budget would drop legitimate game traffic, so operators opt in
    # when they need fan-out abuse protection on a public room.
    content = replace_once(
        content,
        """    const u32 ping_interval_ms = EnvMs("EDEN_ROOM_PING_INTERVAL", 100);

    RoomImpl() {}""",
        """    const u32 ping_interval_ms = EnvMs("EDEN_ROOM_PING_INTERVAL", 100);

    // Per-sender relay byte budget (EDEN_ROOM_RELAY_BUDGET_KBPS, 0 = disabled).
    // Only touched from the room thread, like last_join_attempt above.
    struct RelayBudget {
        std::chrono::steady_clock::time_point window_start{};
        u64 bytes{0};
    };
    std::unordered_map<ENetPeer*, RelayBudget> relay_budget;

    RoomImpl() {}""",
        "add relay budget state",
    )

    for kind in ("proxy", "LDN"):
        content = replace_once(
            content,
            f'        LOG_WARNING(Network, "Dropping oversized {kind} packet ({{}} bytes)",\n'
            "                    event->packet->dataLength);\n"
            "        return;\n"
            "    }\n"
            "\n"
            "    Packet in_packet;",
            f'        LOG_WARNING(Network, "Dropping oversized {kind} packet ({{}} bytes)",\n'
            "                    event->packet->dataLength);\n"
            "        return;\n"
            "    }\n"
            "    if (const u64 relay_budget_bytes = RelayBudgetBytesPerSec(); relay_budget_bytes != 0) {\n"
            "        const auto budget_now = std::chrono::steady_clock::now();\n"
            "        auto& budget_bucket = relay_budget[event->peer];\n"
            "        if (budget_now - budget_bucket.window_start >= std::chrono::seconds(1)) {\n"
            "            budget_bucket.window_start = budget_now;\n"
            "            budget_bucket.bytes = 0;\n"
            "        }\n"
            "        budget_bucket.bytes += event->packet->dataLength;\n"
            "        if (budget_bucket.bytes > relay_budget_bytes) {\n"
            "            if (budget_bucket.bytes - event->packet->dataLength <= relay_budget_bytes) {\n"
            f'                LOG_WARNING(Network, "Relay budget exceeded, dropping {kind} packets for up to 1s");\n'
            "            }\n"
            "            return;\n"
            "        }\n"
            "    }\n"
            "\n"
            "    Packet in_packet;",
            f"enforce relay budget in {kind} handler",
        )

    content = replace_once(
        content,
        """    // Announce the change to all clients.
    // NOTE: ENet has already called enet_peer_reset() before firing""",
        """    relay_budget.erase(client);
    // Announce the change to all clients.
    // NOTE: ENet has already called enet_peer_reset() before firing""",
        "clear relay budget entry on disconnect",
    )

    write(path, content)


def patch_reject_disconnect() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Add enet_peer_disconnect_later to all five rejection senders so the ENet
    # slot is reclaimed once the reliable rejection packet is ACKed, instead of
    # waiting for the client-side timeout (up to 60 s).
    rejection_senders = [
        ("SendNameCollision", "IdNameCollision", ""),
        ("SendIPCollision", "IdIpCollision", ""),
        ("SendWrongPassword", "IdWrongPassword", ""),
        ("SendRoomIsFull", "IdRoomIsFull", ""),
        ("SendVersionMismatch", "IdVersionMismatch", "    packet.Write(network_version);\n"),
    ]
    for func, msg_id, extra in rejection_senders:
        old = (
            f"void Room::RoomImpl::{func}(ENetPeer* client) {{\n"
            "    Packet packet;\n"
            f"    packet.Write(static_cast<u8>({msg_id}));\n"
            f"{extra}"
            "\n"
            "    ENetPacket* enet_packet =\n"
            "        enet_packet_create(packet.GetData(), packet.GetDataSize(), ENET_PACKET_FLAG_RELIABLE);\n"
            "    enet_peer_send(client, 0, enet_packet);\n"
            "    enet_host_flush(server);\n"
            "}"
        )
        new = (
            old[:-1]
            + "    // Rejected peers never join members and the client does not tear the\n"
            "    // link down; reclaim the slot once the rejection packet is ACKed.\n"
            "    enet_peer_disconnect_later(client, 0);\n"
            "}"
        )
        content = replace_once(content, old, new, f"disconnect rejected peer in {func}")

    # Both ban-rejection paths inside HandleJoinRequest. SendUserBanned itself
    # is shared with HandleModBanPacket (which already disconnects), so the
    # call is added at the two HandleJoinRequest call sites only.
    for index in range(2):
        content = replace_once(
            content,
            "            SendUserBanned(event->peer);\n"
            "            return;",
            "            SendUserBanned(event->peer);\n"
            "            enet_peer_disconnect_later(event->peer, 0);\n"
            "            return;",
            f"disconnect banned joiner (site {index + 1})",
        )

    write(path, content)


def patch_disconnect_stats() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    content = replace_once(
        content,
        """    // Announce the change to all clients.
    enet_peer_disconnect(client, 0);
    if (!nickname.empty())
        SendStatusMessage(IdMemberLeave, nickname, username, ip);
    BroadcastRoomInformation();""",
        """    // Announce the change to all clients.
    // NOTE: ENet has already called enet_peer_reset() before firing
    // ENET_EVENT_TYPE_DISCONNECT, so client->roundTripTime is 500 (the ENet
    // default) and data counters are 0. Read from the snapshot saved at join.
    if (!nickname.empty()) {
        auto snap_it = peer_snapshot_cache.find(client);
        u32 stat_rtt = 500;
        std::string stat_duration = "?s";
        if (snap_it != peer_snapshot_cache.end()) {
            stat_rtt = snap_it->second.rtt;
            const auto secs = std::chrono::duration_cast<std::chrono::seconds>(
                std::chrono::steady_clock::now() - snap_it->second.join_time).count();
            stat_duration = secs >= 60
                ? fmt::format("{}m{}s", secs / 60, secs % 60)
                : fmt::format("{}s", secs);
            peer_snapshot_cache.erase(snap_it);
        }
        LOG_INFO(Network, "[{}] {} session RTT {}ms duration {}", ip, nickname, stat_rtt, stat_duration);
    }
    enet_peer_disconnect(client, 0);
    if (!nickname.empty())
        SendStatusMessage(IdMemberLeave, nickname, username, ip);
    BroadcastRoomInformation();""",
        "log disconnect stats from peer snapshot (ENet resets peer before disconnect event)",
    )

    write(path, content)


def patch_remove_redundant_disconnect() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Runs AFTER patch_disconnect_stats(): the anchor includes the STAT log
    # line that patch introduces. HandleClientDisconnection only runs from
    # ENET_EVENT_TYPE_DISCONNECT — ENet has already reset the peer, so the
    # upstream enet_peer_disconnect() call here was a no-op.
    content = replace_once(
        content,
        "        LOG_INFO(Network, \"[{}] {} session RTT {}ms duration {}\", ip, nickname, stat_rtt, stat_duration);\n"
        "    }\n"
        "    enet_peer_disconnect(client, 0);\n"
        "    if (!nickname.empty())",
        "        LOG_INFO(Network, \"[{}] {} session RTT {}ms duration {}\", ip, nickname, stat_rtt, stat_duration);\n"
        "    }\n"
        "    // This handler only runs from ENET_EVENT_TYPE_DISCONNECT: ENet has already\n"
        "    // reset the peer, so upstream's enet_peer_disconnect() here was a no-op.\n"
        "    if (!nickname.empty())",
        "remove redundant enet_peer_disconnect in HandleClientDisconnection",
    )

    write(path, content)


def patch_status_flush_outside_lock() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Runs AFTER patch_room(). enet_host_flush only drains ENet's internal send
    # queues and never touches RoomImpl::members, so it does not need the lock.
    content = replace_once(
        content,
        "    packet.Write(nickname);\n"
        "    packet.Write(username);\n"
        "    std::lock_guard lock(member_mutex);\n"
        "    if (!members.empty()) {\n"
        "        ENetPacket* enet_packet =\n"
        "            enet_packet_create(packet.GetData(), packet.GetDataSize(), ENET_PACKET_FLAG_RELIABLE);\n"
        "        for (auto& member : members) {\n"
        "            enet_peer_send(member.peer, 0, enet_packet);\n"
        "        }\n"
        "    }\n"
        "    enet_host_flush(server);",
        "    packet.Write(nickname);\n"
        "    packet.Write(username);\n"
        "    std::size_t current_member_count = 0;\n"
        "    {\n"
        "        std::lock_guard lock(member_mutex);\n"
        "        current_member_count = members.size();\n"
        "        if (!members.empty()) {\n"
        "            ENetPacket* enet_packet =\n"
        "                enet_packet_create(packet.GetData(), packet.GetDataSize(), ENET_PACKET_FLAG_RELIABLE);\n"
        "            for (auto& member : members) {\n"
        "                enet_peer_send(member.peer, 0, enet_packet);\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "    // Flush after releasing member_mutex: flushing does socket I/O and only\n"
        "    // drains ENet's queues, so holding the lock would stall other threads\n"
        "    // (e.g. the announce thread's GetRoomMemberList) on syscalls.\n"
        "    enet_host_flush(server);",
        "send status message and flush outside member_mutex",
    )

    content = replace_once(
        content,
        "    const auto displayed_member_count =\n"
        "        type == IdMemberJoin ? members.size() + 1 : members.size();",
        "    const auto displayed_member_count =\n"
        "        type == IdMemberJoin ? current_member_count + 1 : current_member_count;",
        "use snapshotted member count in status logs",
    )

    write(path, content)


def patch_throttle() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Runs AFTER patch_env_tunables(): anchors on the env-var ping-interval line
    # that patch introduces in both SendJoinSuccess and SendJoinSuccessAsMod.
    #
    # ENet's probabilistic packet throttle (enet_peer_throttle in peer.c) lowers
    # packetThrottle when RTT spikes.  The drop gate in protocol.c applies to ALL
    # non-nil, non-fragment outgoing commands — including SEND_UNSEQUENCED — so
    # every relay packet is subject to silent drop once the throttle decays.
    # RELIABLE packets bypass this because they travel through the acknowledge-
    # command queue which has no throttle check; switching relay to UNSEQUENCED
    # (patch_relay_flags) exposed game packets to the drop mechanism for the first
    # time.  Setting deceleration=0 pins packetThrottle at its maximum (32/32 =
    # 100 %) permanently, so RTT jitter on internet paths never silently discards
    # relay frames.  This matches real Switch LDN, which has no application-layer
    # drop mechanism.  The throttle interval and acceleration values are kept at
    # their ENet defaults; only deceleration is overridden.
    for _ in range(2):
        content = replace_once(
            content,
            "    enet_peer_timeout(client, ENET_PEER_TIMEOUT_LIMIT, peer_timeout_min_ms, peer_timeout_max_ms);\n"
            "    enet_peer_ping_interval(client, ping_interval_ms);\n"
            "    enet_host_flush(server);\n"
            "}",
            "    enet_peer_timeout(client, ENET_PEER_TIMEOUT_LIMIT, peer_timeout_min_ms, peer_timeout_max_ms);\n"
            "    enet_peer_ping_interval(client, ping_interval_ms);\n"
            "    enet_peer_throttle_configure(client, 1000, ENET_PEER_PACKET_THROTTLE_ACCELERATION, 0);\n"
            "    enet_host_flush(server);\n"
            "}",
            "pin relay throttle at max in join-success sender",
        )

    write(path, content)


def patch_relay_shared_lock() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Runs AFTER patch_relay_size_cap(): relay handlers only READ the member
    # list (find peer, iterate for broadcast) and never modify it.  member_mutex
    # is declared std::shared_mutex, so a shared_lock is sufficient.  NOTE: all
    # packet handlers run on the single room thread, so no two relays are ever
    # concurrent with each other — the benefit is only that a relay does not
    # block, or get blocked by, OTHER threads that read members (e.g. the
    # announce thread's GetRoomInformation).  Relay handling must STAY on one
    # thread: enet_peer_send is not thread-safe.  Writers (HandleJoinRequest,
    # HandleClientDisconnection, kick, ban) keep their exclusive lock_guard.

    # HandleProxyPacket — uniquely identified by "// Send the data only to the
    # destination client" on the else branch (absent in HandleLdnPacket).
    content = replace_once(
        content,
        "    if (broadcast) { // Send the data to everyone except the sender\n"
        "        std::lock_guard lock(member_mutex);\n"
        "        bool sent_packet = false;\n"
        "        for (const auto& member : members) {\n"
        "            if (member.peer != event->peer) {\n"
        "                sent_packet = true;\n"
        "                enet_peer_send(member.peer, 0, enet_packet);\n"
        "            }\n"
        "        }\n"
        "\n"
        "        if (!sent_packet) {\n"
        "            enet_packet_destroy(enet_packet);\n"
        "        }\n"
        "    } else { // Send the data only to the destination client\n"
        "        std::lock_guard lock(member_mutex);",
        "    if (broadcast) { // Send the data to everyone except the sender\n"
        "        std::shared_lock lock(member_mutex);\n"
        "        bool sent_packet = false;\n"
        "        for (const auto& member : members) {\n"
        "            if (member.peer != event->peer) {\n"
        "                sent_packet = true;\n"
        "                enet_peer_send(member.peer, 0, enet_packet);\n"
        "            }\n"
        "        }\n"
        "\n"
        "        if (!sent_packet) {\n"
        "            enet_packet_destroy(enet_packet);\n"
        "        }\n"
        "    } else { // Send the data only to the destination client\n"
        "        std::shared_lock lock(member_mutex);",
        "proxy relay: lock_guard -> shared_lock",
    )

    # HandleLdnPacket — uniquely identified by bare "} else {" (no comment).
    content = replace_once(
        content,
        "    if (broadcast) { // Send the data to everyone except the sender\n"
        "        std::lock_guard lock(member_mutex);\n"
        "        bool sent_packet = false;\n"
        "        for (const auto& member : members) {\n"
        "            if (member.peer != event->peer) {\n"
        "                sent_packet = true;\n"
        "                enet_peer_send(member.peer, 0, enet_packet);\n"
        "            }\n"
        "        }\n"
        "\n"
        "        if (!sent_packet) {\n"
        "            enet_packet_destroy(enet_packet);\n"
        "        }\n"
        "    } else {\n"
        "        std::lock_guard lock(member_mutex);",
        "    if (broadcast) { // Send the data to everyone except the sender\n"
        "        std::shared_lock lock(member_mutex);\n"
        "        bool sent_packet = false;\n"
        "        for (const auto& member : members) {\n"
        "            if (member.peer != event->peer) {\n"
        "                sent_packet = true;\n"
        "                enet_peer_send(member.peer, 0, enet_packet);\n"
        "            }\n"
        "        }\n"
        "\n"
        "        if (!sent_packet) {\n"
        "            enet_packet_destroy(enet_packet);\n"
        "        }\n"
        "    } else {\n"
        "        std::shared_lock lock(member_mutex);",
        "LDN relay: lock_guard -> shared_lock",
    )

    write(path, content)


def patch_nickname_regex() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Independent patch. std::regex construction compiles an NFA and is
    # expensive; it ran on every join request. static const compiles once.
    content = replace_once(
        content,
        "    const std::regex nickname_regex(\"^[ a-zA-Z0-9._-]{4,20}$\");",
        "    static const std::regex nickname_regex(\"^[ a-zA-Z0-9._-]{4,20}$\");",
        "make nickname regex static const",
    )

    write(path, content)


def patch_relay_diagnostics() -> None:
    """Transport telemetry + heuristic advice.

    The room cannot see in-game desync. It CAN measure ENet RTT/loss, relay
    sizes, and drop reasons, then log a DIAG line so operators (or an auditor)
    can decide whether the path looks clean (blame clients/emulation) or
    lossy/jittery (relay mode / timeouts matter).
    """
    path = "src/network/room.cpp"
    content = read(path)

    # --- Expand PeerSnapshot for mid-session samples ---
    content = replace_once(
        content,
        """    struct PeerSnapshot {
        u32 rtt{500};
        std::chrono::steady_clock::time_point join_time;
    };
    std::unordered_map<ENetPeer*, PeerSnapshot> peer_snapshot_cache;
""",
        """    struct PeerSnapshot {
        u32 rtt{500};           // RTT at join (original)
        u32 last_rtt{500};      // last sampled mid-session RTT
        u32 peak_rtt{500};      // max observed RTT
        u32 last_loss{0};       // ENet packetLoss scale units
        u32 peak_loss{0};
        std::chrono::steady_clock::time_point join_time;
    };
    std::unordered_map<ENetPeer*, PeerSnapshot> peer_snapshot_cache;

    // Transport diagnostics (room thread only). EDEN_ROOM_DIAG_INTERVAL_SEC
    // (default 30, 0 = off) emits DIAG lines with counters + a heuristic.
    struct RelayDiag {
        u64 proxy_packets{0};
        u64 ldn_packets{0};
        u64 proxy_bytes{0};
        u64 ldn_bytes{0};
        u64 drop_oversize{0};
        u64 drop_malformed{0};
        u64 drop_budget{0};
        u64 drop_unknown_ip{0};
        u64 broadcast_sends{0};
        // Size histogram of accepted relay packets (ingress envelope bytes).
        u64 size_le_512{0};
        u64 size_513_1024{0};
        u64 size_1025_1366{0};   // typically single ENet datagram
        u64 size_1367_1536{0};   // may fragment (unreliable fragment flag)
        std::chrono::steady_clock::time_point window_start{std::chrono::steady_clock::now()};
        bool boot_logged{false};
    } relay_diag;

    u32 diag_interval_sec = 30;

    void NoteRelaySize(std::size_t bytes) {
        if (bytes <= 512) {
            ++relay_diag.size_le_512;
        } else if (bytes <= 1024) {
            ++relay_diag.size_513_1024;
        } else if (bytes <= 1366) {
            ++relay_diag.size_1025_1366;
        } else {
            ++relay_diag.size_1367_1536;
        }
    }

    void MaybeLogRelayDiagnostics();
""",
        "add relay diagnostics state and helpers",
    )

    # Read diag interval once in RoomImpl ctor body — RoomImpl() {} is empty;
    # initialize diag_interval_sec via a small init after EnvMs fields exist.
    content = replace_once(
        content,
        """    std::unordered_map<ENetPeer*, RelayBudget> relay_budget;

    RoomImpl() {}
""",
        """    std::unordered_map<ENetPeer*, RelayBudget> relay_budget;

    RoomImpl() {
        // 0 disables periodic DIAG. Default 30s is light enough for public rooms.
        const char* diag_env = std::getenv("EDEN_ROOM_DIAG_INTERVAL_SEC");
        if (diag_env != nullptr && diag_env[0] != '\\0') {
            char* end = nullptr;
            const unsigned long parsed = std::strtoul(diag_env, &end, 10);
            if (end != diag_env && *end == '\\0' && parsed <= 3600UL) {
                diag_interval_sec = static_cast<u32>(parsed);
            } else {
                LOG_WARNING(Network, "Ignoring invalid EDEN_ROOM_DIAG_INTERVAL_SEC '{}'",
                            diag_env);
            }
        }
    }
""",
        "init diag interval from env",
    )

    # Join snapshot: seed last/peak fields
    content = replace_once(
        content,
        """    peer_snapshot_cache[event->peer] = {event->peer->roundTripTime, std::chrono::steady_clock::now()};
""",
        """    {
        const u32 join_rtt = event->peer->roundTripTime;
        peer_snapshot_cache[event->peer] = PeerSnapshot{
            join_rtt, join_rtt, join_rtt, event->peer->packetLoss, event->peer->packetLoss,
            std::chrono::steady_clock::now()};
    }
""",
        "seed full peer snapshot at join",
    )

    # Count oversize/malformed/budget in proxy handler
    content = replace_once(
        content,
        """    if (event->packet->dataLength > MaxRelayPayloadSize) {
        LOG_WARNING(Network, "Dropping oversized proxy packet ({} bytes)",
                    event->packet->dataLength);
        return;
    }
""",
        """    if (event->packet->dataLength > MaxRelayPayloadSize) {
        ++relay_diag.drop_oversize;
        LOG_WARNING(Network, "Dropping oversized proxy packet ({} bytes)",
                    event->packet->dataLength);
        return;
    }
""",
        "count oversized proxy drops",
    )
    content = replace_once(
        content,
        """                LOG_WARNING(Network, "Relay budget exceeded, dropping proxy packets for up to 1s");
            }
            return;
        }
    }

    Packet in_packet;
    in_packet.Append(event->packet->data, event->packet->dataLength);
    in_packet.IgnoreBytes(sizeof(u8)); // Message type

    in_packet.IgnoreBytes(sizeof(u8));          // Domain
""",
        """                LOG_WARNING(Network, "Relay budget exceeded, dropping proxy packets for up to 1s");
            }
            ++relay_diag.drop_budget;
            return;
        }
    }

    Packet in_packet;
    in_packet.Append(event->packet->data, event->packet->dataLength);
    in_packet.IgnoreBytes(sizeof(u8)); // Message type

    in_packet.IgnoreBytes(sizeof(u8));          // Domain
""",
        "count proxy budget drops",
    )
    content = replace_once(
        content,
        """    if (!in_packet) {
        LOG_WARNING(Network, "Dropping malformed proxy packet");
        return;
    }

    Packet out_packet;
    out_packet.Append(event->packet->data, event->packet->dataLength);
    ENetPacket* enet_packet = enet_packet_create(out_packet.GetData(), out_packet.GetDataSize(),
                                                 RelayPacketFlag());
""",
        """    if (!in_packet) {
        ++relay_diag.drop_malformed;
        LOG_WARNING(Network, "Dropping malformed proxy packet");
        return;
    }

    ++relay_diag.proxy_packets;
    relay_diag.proxy_bytes += event->packet->dataLength;
    NoteRelaySize(event->packet->dataLength);

    Packet out_packet;
    out_packet.Append(event->packet->data, event->packet->dataLength);
    ENetPacket* enet_packet = enet_packet_create(out_packet.GetData(), out_packet.GetDataSize(),
                                                 RelayPacketFlag());
""",
        "count accepted proxy relay",
    )

    # LDN oversize / budget / accept
    content = replace_once(
        content,
        """    if (event->packet->dataLength > MaxRelayPayloadSize) {
        LOG_WARNING(Network, "Dropping oversized LDN packet ({} bytes)",
                    event->packet->dataLength);
        return;
    }
""",
        """    if (event->packet->dataLength > MaxRelayPayloadSize) {
        ++relay_diag.drop_oversize;
        LOG_WARNING(Network, "Dropping oversized LDN packet ({} bytes)",
                    event->packet->dataLength);
        return;
    }
""",
        "count oversized LDN drops",
    )
    content = replace_once(
        content,
        """                LOG_WARNING(Network, "Relay budget exceeded, dropping LDN packets for up to 1s");
            }
            return;
        }
    }

    Packet in_packet;
    in_packet.Append(event->packet->data, event->packet->dataLength);

    in_packet.IgnoreBytes(sizeof(u8)); // Message type
""",
        """                LOG_WARNING(Network, "Relay budget exceeded, dropping LDN packets for up to 1s");
            }
            ++relay_diag.drop_budget;
            return;
        }
    }

    Packet in_packet;
    in_packet.Append(event->packet->data, event->packet->dataLength);

    in_packet.IgnoreBytes(sizeof(u8)); // Message type
""",
        "count LDN budget drops",
    )

    # LDN malformed + accept — need unique context after LDN header parse
    content = replace_once(
        content,
        """    if (!in_packet) {
        LOG_WARNING(Network, "Dropping malformed LDN packet");
        return;
    }

    Packet out_packet;
    out_packet.Append(event->packet->data, event->packet->dataLength);
    ENetPacket* enet_packet = enet_packet_create(out_packet.GetData(), out_packet.GetDataSize(),
                                                 RelayPacketFlag());
""",
        """    if (!in_packet) {
        ++relay_diag.drop_malformed;
        LOG_WARNING(Network, "Dropping malformed LDN packet");
        return;
    }

    ++relay_diag.ldn_packets;
    relay_diag.ldn_bytes += event->packet->dataLength;
    NoteRelaySize(event->packet->dataLength);

    Packet out_packet;
    out_packet.Append(event->packet->data, event->packet->dataLength);
    ENetPacket* enet_packet = enet_packet_create(out_packet.GetData(), out_packet.GetDataSize(),
                                                 RelayPacketFlag());
""",
        "count accepted LDN relay",
    )

    # Unknown-IP drops (proxy then LDN) — both destroy without send in drop mode
    content = replace_once(
        content,
        """            } else {
                enet_packet_destroy(enet_packet);
            }
        }
    }
    enet_host_flush(server);
}

void Room::RoomImpl::HandleLdnPacket(const ENetEvent* event) {
""",
        """            } else {
                ++relay_diag.drop_unknown_ip;
                enet_packet_destroy(enet_packet);
            }
        }
    }
    enet_host_flush(server);
}

void Room::RoomImpl::HandleLdnPacket(const ENetEvent* event) {
""",
        "count proxy unknown-IP drops",
    )
    content = replace_once(
        content,
        """            } else {
                enet_packet_destroy(enet_packet);
            }
        }
    }
    enet_host_flush(server);
}

void Room::RoomImpl::HandleChatPacket(const ENetEvent* event) {
""",
        """            } else {
                ++relay_diag.drop_unknown_ip;
                enet_packet_destroy(enet_packet);
            }
        }
    }
    enet_host_flush(server);
}

void Room::RoomImpl::HandleChatPacket(const ENetEvent* event) {
""",
        "count LDN unknown-IP drops",
    )

    # Broadcast send counters (proxy branch has unique comment)
    content = replace_once(
        content,
        """    if (broadcast) { // Send the data to everyone except the sender
        std::shared_lock lock(member_mutex);
        bool sent_packet = false;
        for (const auto& member : members) {
            if (member.peer != event->peer) {
                sent_packet = true;
                enet_peer_send(member.peer, 0, enet_packet);
            }
        }

        if (!sent_packet) {
            enet_packet_destroy(enet_packet);
        }
    } else { // Send the data only to the destination client
""",
        """    if (broadcast) { // Send the data to everyone except the sender
        std::shared_lock lock(member_mutex);
        bool sent_packet = false;
        for (const auto& member : members) {
            if (member.peer != event->peer) {
                sent_packet = true;
                ++relay_diag.broadcast_sends;
                enet_peer_send(member.peer, 0, enet_packet);
            }
        }

        if (!sent_packet) {
            enet_packet_destroy(enet_packet);
        }
    } else { // Send the data only to the destination client
""",
        "count proxy broadcast fan-out",
    )
    content = replace_once(
        content,
        """    if (broadcast) { // Send the data to everyone except the sender
        std::shared_lock lock(member_mutex);
        bool sent_packet = false;
        for (const auto& member : members) {
            if (member.peer != event->peer) {
                sent_packet = true;
                enet_peer_send(member.peer, 0, enet_packet);
            }
        }

        if (!sent_packet) {
            enet_packet_destroy(enet_packet);
        }
    } else {
        std::shared_lock lock(member_mutex);
""",
        """    if (broadcast) { // Send the data to everyone except the sender
        std::shared_lock lock(member_mutex);
        bool sent_packet = false;
        for (const auto& member : members) {
            if (member.peer != event->peer) {
                sent_packet = true;
                ++relay_diag.broadcast_sends;
                enet_peer_send(member.peer, 0, enet_packet);
            }
        }

        if (!sent_packet) {
            enet_packet_destroy(enet_packet);
        }
    } else {
        std::shared_lock lock(member_mutex);
""",
        "count LDN broadcast fan-out",
    )

    # Event loop: call MaybeLogRelayDiagnostics each iteration
    content = replace_once(
        content,
        """            if (enet_host_service(server, &event, 1) > 0) {
                dispatch();
            }
            } catch (const std::exception& e) {
                LOG_ERROR(Network, "Room loop error: {}", e.what());
""",
        """            if (enet_host_service(server, &event, 1) > 0) {
                dispatch();
            }
            MaybeLogRelayDiagnostics();
            } catch (const std::exception& e) {
                LOG_ERROR(Network, "Room loop error: {}", e.what());
""",
        "call relay diagnostics from event loop",
    )

    # Richer STAT line
    content = replace_once(
        content,
        """    if (!nickname.empty()) {
        auto snap_it = peer_snapshot_cache.find(client);
        u32 stat_rtt = 500;
        std::string stat_duration = "?s";
        if (snap_it != peer_snapshot_cache.end()) {
            stat_rtt = snap_it->second.rtt;
            const auto secs = std::chrono::duration_cast<std::chrono::seconds>(
                std::chrono::steady_clock::now() - snap_it->second.join_time).count();
            stat_duration = secs >= 60
                ? fmt::format("{}m{}s", secs / 60, secs % 60)
                : fmt::format("{}s", secs);
            peer_snapshot_cache.erase(snap_it);
        }
        LOG_INFO(Network, "[{}] {} session RTT {}ms duration {}", ip, nickname, stat_rtt, stat_duration);
    }
""",
        """    if (!nickname.empty()) {
        auto snap_it = peer_snapshot_cache.find(client);
        u32 join_rtt = 500;
        u32 last_rtt = 500;
        u32 peak_rtt = 500;
        u32 last_loss = 0;
        u32 peak_loss = 0;
        std::string stat_duration = "?s";
        if (snap_it != peer_snapshot_cache.end()) {
            join_rtt = snap_it->second.rtt;
            last_rtt = snap_it->second.last_rtt;
            peak_rtt = snap_it->second.peak_rtt;
            last_loss = snap_it->second.last_loss;
            peak_loss = snap_it->second.peak_loss;
            const auto secs = std::chrono::duration_cast<std::chrono::seconds>(
                std::chrono::steady_clock::now() - snap_it->second.join_time).count();
            stat_duration = secs >= 60
                ? fmt::format("{}m{}s", secs / 60, secs % 60)
                : fmt::format("{}s", secs);
            peer_snapshot_cache.erase(snap_it);
        }
        // loss_pct uses ENet's reliable-packet loss scale (ENET_PEER_PACKET_LOSS_SCALE=65536).
        const double last_loss_pct =
            100.0 * static_cast<double>(last_loss) / static_cast<double>(ENET_PEER_PACKET_LOSS_SCALE);
        const double peak_loss_pct =
            100.0 * static_cast<double>(peak_loss) / static_cast<double>(ENET_PEER_PACKET_LOSS_SCALE);
        LOG_INFO(Network,
                 "[{}] {} session join_rtt {}ms last_rtt {}ms peak_rtt {}ms "
                 "last_loss {:.2f}% peak_loss {:.2f}% duration {}",
                 ip, nickname, join_rtt, last_rtt, peak_rtt, last_loss_pct, peak_loss_pct,
                 stat_duration);
    }
""",
        "rich STAT with mid-session RTT/loss",
    )

    # Implement MaybeLogRelayDiagnostics before Room::Room ctor
    content = replace_once(
        content,
        """// Room
Room::Room() : room_impl{std::make_unique<RoomImpl>()} {}
""",
        """void Room::RoomImpl::MaybeLogRelayDiagnostics() {
    const auto now = std::chrono::steady_clock::now();

    // One-shot boot line so session logs always record effective mode + knobs.
    if (!relay_diag.boot_logged) {
        relay_diag.boot_logged = true;
        relay_diag.window_start = now;
        const char* mode = std::getenv("EDEN_ROOM_RELAY_MODE");
        std::string mode_name = "unsequenced";
        if (mode != nullptr && mode[0] != '\\0') {
            mode_name = mode;
        } else {
            const char* legacy = std::getenv("EDEN_ROOM_RELAY_RELIABLE");
            if (legacy != nullptr && std::strcmp(legacy, "1") == 0) {
                mode_name = "reliable";
            }
        }
        const char* unk = std::getenv("EDEN_ROOM_UNKNOWN_IP_FALLBACK");
        const char* budget_env = std::getenv("EDEN_ROOM_RELAY_BUDGET_KBPS");
        const char* budget_str =
            (budget_env != nullptr && budget_env[0] != '\\0') ? budget_env : "0";
        LOG_INFO(Network,
                 "DIAG boot mode={} unknown_ip={} timeout_ms={}/{} ping_ms={} "
                 "diag_interval_s={} budget_kbps={} "
                 "(server cannot detect game desync; DIAG reports transport only)",
                 mode_name, (unk != nullptr && std::strcmp(unk, "drop") == 0) ? "drop" : "broadcast",
                 peer_timeout_min_ms, peer_timeout_max_ms, ping_interval_ms, diag_interval_sec,
                 budget_str);
    }

    if (diag_interval_sec == 0) {
        return;
    }
    if (now - relay_diag.window_start < std::chrono::seconds(diag_interval_sec)) {
        return;
    }
    relay_diag.window_start = now;

    // Sample live peer RTT/loss into snapshots and aggregate for the DIAG line.
    u32 members_n = 0;
    u32 rtt_min = 0;
    u32 rtt_max = 0;
    u64 rtt_sum = 0;
    u32 loss_max = 0;
    u64 loss_sum = 0;
    u32 rtt_var_max = 0;
    {
        std::shared_lock lock(member_mutex);
        members_n = static_cast<u32>(members.size());
        bool first = true;
        for (const auto& member : members) {
            if (member.peer == nullptr) {
                continue;
            }
            const u32 rtt = member.peer->roundTripTime;
            const u32 loss = member.peer->packetLoss;
            const u32 rtt_var = member.peer->roundTripTimeVariance;
            if (first) {
                rtt_min = rtt_max = rtt;
                first = false;
            } else {
                rtt_min = (std::min)(rtt_min, rtt);
                rtt_max = (std::max)(rtt_max, rtt);
            }
            rtt_sum += rtt;
            loss_sum += loss;
            loss_max = (std::max)(loss_max, loss);
            rtt_var_max = (std::max)(rtt_var_max, rtt_var);

            auto snap_it = peer_snapshot_cache.find(member.peer);
            if (snap_it != peer_snapshot_cache.end()) {
                snap_it->second.last_rtt = rtt;
                snap_it->second.peak_rtt = (std::max)(snap_it->second.peak_rtt, rtt);
                snap_it->second.last_loss = loss;
                snap_it->second.peak_loss = (std::max)(snap_it->second.peak_loss, loss);
            }
        }
    }

    const u32 rtt_avg = members_n > 0 ? static_cast<u32>(rtt_sum / members_n) : 0;
    const double loss_avg_pct =
        members_n > 0
            ? 100.0 * static_cast<double>(loss_sum) /
                  (static_cast<double>(members_n) * static_cast<double>(ENET_PEER_PACKET_LOSS_SCALE))
            : 0.0;
    const double loss_max_pct =
        100.0 * static_cast<double>(loss_max) / static_cast<double>(ENET_PEER_PACKET_LOSS_SCALE);

    const char* mode = std::getenv("EDEN_ROOM_RELAY_MODE");
    std::string mode_name = "unsequenced";
    if (mode != nullptr && mode[0] != '\\0') {
        mode_name = mode;
    } else if (const char* legacy = std::getenv("EDEN_ROOM_RELAY_RELIABLE");
               legacy != nullptr && std::strcmp(legacy, "1") == 0) {
        mode_name = "reliable";
    }

    LOG_INFO(Network,
             "DIAG mode={} members={} rtt_ms min/avg/max={}/{}/{} rtt_var_max={} "
             "loss_pct avg/max={:.2f}/{:.2f} "
             "proxy_pkts={} ldn_pkts={} proxy_B={} ldn_B={} bcast_sends={} "
             "drops oversize/malformed/budget/unk_ip={}/{}/{}/{} "
             "sizes <=512/513-1024/1025-1366/1367-1536={}/{}/{}/{}",
             mode_name, members_n, rtt_min, rtt_avg, rtt_max, rtt_var_max, loss_avg_pct,
             loss_max_pct, relay_diag.proxy_packets, relay_diag.ldn_packets, relay_diag.proxy_bytes,
             relay_diag.ldn_bytes, relay_diag.broadcast_sends, relay_diag.drop_oversize,
             relay_diag.drop_malformed, relay_diag.drop_budget, relay_diag.drop_unknown_ip,
             relay_diag.size_le_512, relay_diag.size_513_1024, relay_diag.size_1025_1366,
             relay_diag.size_1367_1536);

    // Heuristic is TRANSPORT-ONLY. It does not observe FPS or game desync.
    std::string advice;
    if (members_n == 0) {
        advice = "no members; idle";
    } else if (relay_diag.drop_oversize > 0) {
        advice = "oversize drops>0: investigate clients/caps before blaming relay mode";
    } else if (loss_max_pct >= 2.0 || rtt_var_max >= 80) {
        advice = "lossy/jittery path: prefer sequenced (not unsequenced); if still unstable try "
                 "reliable; also tighten timeouts only if zombies linger";
    } else if (loss_max_pct >= 0.5 || rtt_max >= 200) {
        advice = "moderate WAN stress: keep sequenced; unsequenced risks reorder desync; "
                 "if rubber-banding only, compare reliable vs sequenced offline";
    } else if (relay_diag.drop_unknown_ip > 0) {
        advice = "unknown-IP drops>0: fake-IP table miss (join race or stale target); "
                 "if connect fails, briefly test UNKNOWN_IP=broadcast";
    } else if (rtt_max < 80 && loss_max_pct < 0.25) {
        advice = "transport looks clean: if races still desync, check client FPS/same Eden "
                 "build/limit-speed (emulation) — relay mode changes unlikely to help";
    } else {
        advice = "mixed path: keep sequenced as default; collect client FPS logs alongside DIAG";
    }
    LOG_INFO(Network, "DIAG advice: {}", advice);
}

// Room
Room::Room() : room_impl{std::make_unique<RoomImpl>()} {}
""",
        "implement MaybeLogRelayDiagnostics",
    )

    write(path, content)


def patch_diag_log_label() -> None:
    """Pretty-print DIAG lines in the Docker console formatter."""
    path = "src/common/logging.cpp"
    content = read(path)
    content = replace_once(
        content,
        """    if (is_network_info && is_stat) {
        return fmt::format("[{:02d}:{:02d}:{:02d}] STAT  | {}", local_time.tm_hour,
                           local_time.tm_min, local_time.tm_sec, message);
    }

    if (entry.log_level >= Level::Warning) {
""",
        """    if (is_network_info && is_stat) {
        return fmt::format("[{:02d}:{:02d}:{:02d}] STAT  | {}", local_time.tm_hour,
                           local_time.tm_min, local_time.tm_sec, message);
    }
    if (is_network_info && message.rfind("DIAG ", 0) == 0) {
        return fmt::format("[{:02d}:{:02d}:{:02d}] DIAG  | {}", local_time.tm_hour,
                           local_time.tm_min, local_time.tm_sec, message.substr(5));
    }

    if (entry.log_level >= Level::Warning) {
""",
        "format DIAG log label",
    )
    write(path, content)


def patch_logging_h() -> None:
    # fmt 9 removed basic_format_string::get() (it existed in fmt 8).
    # Ubuntu 24.04 ships fmt 9, so format.get() fails to compile.
    # Use the implicit string_view conversion instead, which works in fmt 8-10+.
    path = "src/common/logging.h"
    content = read(path)
    content = replace_once(
        content,
        "format.get(), fmt::make_format_args(args...)",
        "fmt::string_view(format), fmt::make_format_args(args...)",
        "fix logging.h format.get() removed in fmt 9",
    )
    write(path, content)


def main() -> int:
    try:
        patch_logging_h()
        patch_yuzu_room()
        patch_announce_room_json()
        patch_announce_session()
        patch_verify_user_jwt()
        patch_console_log_flush()

        # room.cpp — order matters from here down.
        patch_room()                        # base hardening; creates anchors used below
        patch_loop_drain()                  # needs patch_room's try/catch loop text
        patch_peer_timeout()                # adds enet_peer_timeout lines
        patch_ping_interval()               # anchors on patch_peer_timeout output
        patch_env_tunables()                # anchors on patch_room blocks AND ping+timeout lines
        patch_rtt_logging()                 # independent; needs patch_room only
        patch_relay_flags()                 # RELIABLE -> UNSEQUENCED on relay paths
        patch_relay_flag_env()              # rewrites UNSEQUENCED lines; needs patch_relay_flags
        patch_relay_size_cap()              # anchors on patch_room's header checks
        patch_relay_shared_lock()           # shared_lock on relay read paths; after all relay patches
        patch_throttle()                    # pin throttle at max; anchors on patch_env_tunables output
        patch_reject_disconnect()           # position-independent (untouched rejection senders)
        patch_disconnect_stats()            # adds STAT logging in HandleClientDisconnection
        patch_remove_redundant_disconnect() # anchors on patch_disconnect_stats output
        patch_relay_rate_budget()           # anchors on env_tunables, size cap, flag env, disconnect_stats
        patch_status_flush_outside_lock()   # anchors on patch_room's count lines
        patch_nickname_regex()              # independent; anchors on const std::regex line
        patch_relay_diagnostics()           # counters + periodic DIAG/advice; last room patch
        patch_diag_log_label()              # console formatter DIAG label
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Applied Eden room hardening patches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
