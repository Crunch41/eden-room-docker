#!/usr/bin/env python3
"""Apply Eden dedicated-room hardening patches.

This script runs inside a checked-out Eden source tree. It intentionally fails
when expected upstream code moves, so Docker and CI do not silently build an
unpatched room server.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path.cwd()


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    (ROOT / path).write_text(content, encoding="utf-8")


def replace_once(content: str, old: str, new: str, label: str) -> str:
    if old not in content:
        raise RuntimeError(f"{label}: expected source block not found")
    return content.replace(old, new, 1)


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
        if (error.value() != 2) {
            LOG_INFO(WebService, "JWT verification failed: category={}, code={}, message={}",
                     error.category().name(), error.value(), error.message());
        }
        return {};
    }""",
        "suppress expected unauthenticated JWT noise",
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
        """    if (!room_information.host_username.empty() &&
        sending_member->user_data.username == room_information.host_username) { // Room host

        return true;
    }
    if (!room_information.host_username.empty() &&
        sending_member->nickname == room_information.host_username) { // Room host over LAN

        return true;
    }
    return false;""",
        "allow host nickname moderation when JWT data is absent",
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


def patch_peer_timeout() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Increase ENet disconnect tolerance for high-RTT peers (e.g. AUS→USA ~170 ms).
    # Default timeoutMinimum=5000 drops players after ~3 s of transient loss.
    # 12 000 / 60 000 ms gives a comfortable margin for undersea-cable rerouting.
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


def patch_rtt_logging() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Log each peer's measured round-trip time at join so high-ping players
    # are immediately visible in the session log for desync diagnosis.
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

    # Switch proxy and LDN relay packets from RELIABLE to UNSEQUENCED.
    #
    # The real Switch uses LDN (802.11 ad-hoc raw UDP) — no reliability layer,
    # no ordering. Games already carry their own sequence numbers inside the LDN
    # envelope and handle loss themselves. ENet's RELIABLE flag adds head-of-line
    # blocking: a single dropped packet on an AUS→USA path (~170 ms RTT) stalls
    # all later packets until ENet retransmits and receives an ACK, causing the
    # burst-then-teleport desync pattern in games like Mario Kart 8 Deluxe.
    #
    # UNSEQUENCED delivers each packet as soon as it arrives with no buffering or
    # retransmit, matching the real Switch transport semantics.
    # Control packets (join, kick, chat, game info) are unaffected and remain RELIABLE.
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


def patch_disconnect_stats() -> None:
    path = "src/network/room.cpp"
    content = read(path)

    # Log final ENet peer stats before disconnecting so each session shows:
    #   STAT | [ip] Nick final RTT 174ms loss 0.0% tx 4.2MB rx 3.8MB
    # RTT and loss figures make it immediately clear whether a LEAVE was a clean
    # exit or a high-loss timeout that the extended peer timeout kept alive.
    # packetLoss is scaled by ENET_PEER_PACKET_LOSS_SCALE (1<<16); divide to get %.
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


def main() -> int:
    try:
        patch_yuzu_room()
        patch_announce_room_json()
        patch_announce_session()
        patch_verify_user_jwt()
        patch_console_log_flush()
        patch_room()
        patch_peer_timeout()
        patch_rtt_logging()
        patch_relay_flags()
        patch_disconnect_stats()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Applied Eden room hardening patches")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
