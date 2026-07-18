#!/bin/bash
set -Eeuo pipefail

PUID="${PUID:-99}"
PGID="${PGID:-100}"

require_number() {
    local name="$1"
    local value="$2"
    if ! [[ "$value" =~ ^[0-9]+$ ]]; then
        echo "ERROR: $name must be numeric (got '$value')" >&2
        exit 1
    fi
}

require_number PUID "$PUID"
require_number PGID "$PGID"

if [ "$(id -u)" = "0" ]; then
    echo "Setting up user eden with PUID=${PUID} PGID=${PGID}"

    groupmod -o -g "$PGID" eden
    usermod -o -u "$PUID" eden

    data_dir="${LOG_DIR:-/home/eden/.local/share/eden-room}"
    mkdir -p "$data_dir"
    current_owner="$(stat -c '%u:%g' "$data_dir" 2>/dev/null || echo '')"
    if [ "$current_owner" != "${PUID}:${PGID}" ]; then
        chown -R eden:eden "$data_dir"
    fi

    exec gosu eden "$0" "$@"
fi

ROOM_NAME="${ROOM_NAME:-Eden Room}"
ROOM_DESCRIPTION="${ROOM_DESCRIPTION:-}"
PORT="${PORT:-24872}"
MAX_MEMBERS="${MAX_MEMBERS:-16}"
BIND_ADDRESS="${BIND_ADDRESS:-0.0.0.0}"
PASSWORD="${PASSWORD:-}"
PREFERRED_GAME="${PREFERRED_GAME:-Any Game}"
PREFERRED_GAME_ID="${PREFERRED_GAME_ID:-0}"
BAN_LIST_FILE="${BAN_LIST_FILE:-/home/eden/.local/share/eden-room/ban_list.txt}"
LOG_DIR="${LOG_DIR:-/home/eden/.local/share/eden-room}"
EDEN_INTERNAL_LOG_DIR="/home/eden/.local/share/eden/log"
MAX_LOG_FILES="${MAX_LOG_FILES:-10}"
EDEN_ROOM_UNKNOWN_IP_FALLBACK="${EDEN_ROOM_UNKNOWN_IP_FALLBACK:-broadcast}"
export EDEN_ROOM_UNKNOWN_IP_FALLBACK
EDEN_ROOM_MOD_USERNAME="${EDEN_ROOM_MOD_USERNAME:-}"
export EDEN_ROOM_MOD_USERNAME
EDEN_ROOM_RELAY_MODE="${EDEN_ROOM_RELAY_MODE:-}"
export EDEN_ROOM_RELAY_MODE
EDEN_ROOM_RELAY_BUDGET_KBPS="${EDEN_ROOM_RELAY_BUDGET_KBPS:-0}"
export EDEN_ROOM_RELAY_BUDGET_KBPS
EDEN_ROOM_DIAG_INTERVAL_SEC="${EDEN_ROOM_DIAG_INTERVAL_SEC:-0}"
export EDEN_ROOM_DIAG_INTERVAL_SEC

require_number PORT "$PORT"
require_number MAX_MEMBERS "$MAX_MEMBERS"
require_number MAX_LOG_FILES "$MAX_LOG_FILES"
require_number EDEN_ROOM_PEER_TIMEOUT_MIN "${EDEN_ROOM_PEER_TIMEOUT_MIN:-12000}"
require_number EDEN_ROOM_PEER_TIMEOUT_MAX "${EDEN_ROOM_PEER_TIMEOUT_MAX:-60000}"
require_number EDEN_ROOM_PING_INTERVAL "${EDEN_ROOM_PING_INTERVAL:-100}"

if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "ERROR: PORT must be 1-65535 (got '$PORT')" >&2
    exit 1
fi

if [ "$MAX_MEMBERS" -lt 2 ] || [ "$MAX_MEMBERS" -gt 254 ]; then
    echo "ERROR: MAX_MEMBERS must be 2-254 (got '$MAX_MEMBERS')" >&2
    exit 1
fi

if [ "${EDEN_ROOM_PEER_TIMEOUT_MIN:-12000}" -gt "${EDEN_ROOM_PEER_TIMEOUT_MAX:-60000}" ]; then
    echo "ERROR: EDEN_ROOM_PEER_TIMEOUT_MIN cannot exceed EDEN_ROOM_PEER_TIMEOUT_MAX" >&2
    exit 1
fi

if ! [[ "$PREFERRED_GAME_ID" =~ ^(0|[0-9A-Fa-f]{16})$ ]]; then
    echo "ERROR: PREFERRED_GAME_ID must be 0 or a 16-digit hexadecimal title ID" >&2
    exit 1
fi

if [ "$EDEN_ROOM_UNKNOWN_IP_FALLBACK" != "broadcast" ] && [ "$EDEN_ROOM_UNKNOWN_IP_FALLBACK" != "drop" ]; then
    echo "ERROR: EDEN_ROOM_UNKNOWN_IP_FALLBACK must be 'broadcast' or 'drop' (got '$EDEN_ROOM_UNKNOWN_IP_FALLBACK')" >&2
    exit 1
fi

case "$EDEN_ROOM_RELAY_MODE" in
    ""|unsequenced|sequenced|reliable) ;;
    *)
        echo "ERROR: EDEN_ROOM_RELAY_MODE must be 'unsequenced', 'sequenced', or 'reliable' (got '$EDEN_ROOM_RELAY_MODE')" >&2
        exit 1
        ;;
esac

require_number EDEN_ROOM_RELAY_BUDGET_KBPS "$EDEN_ROOM_RELAY_BUDGET_KBPS"
require_number EDEN_ROOM_DIAG_INTERVAL_SEC "$EDEN_ROOM_DIAG_INTERVAL_SEC"

SESSION_TIMESTAMP="$(date +%d-%m-%Y_%H-%M-%S)"
LOG_FILE="${LOG_DIR}/session_${SESSION_TIMESTAMP}.log"

mkdir -p "$LOG_DIR" "$(dirname "$BAN_LIST_FILE")" "$EDEN_INTERNAL_LOG_DIR"
touch "${EDEN_INTERNAL_LOG_DIR}/eden_log.txt"

if [ ! -f "$BAN_LIST_FILE" ]; then
    {
        echo "YuzuRoom-BanList-1"
        echo ""
    } > "$BAN_LIST_FILE"
fi

cleanup_old_logs() {
    local log_count
    log_count="$(find "$LOG_DIR" -maxdepth 1 -name 'session_*.log' -type f 2>/dev/null | wc -l)"
    if [ "$log_count" -gt "$MAX_LOG_FILES" ]; then
        local to_delete=$((log_count - MAX_LOG_FILES))
        find "$LOG_DIR" -maxdepth 1 -name 'session_*.log' -type f -printf '%T@ %p\n' |
            sort -rn |
            tail -n "$to_delete" |
            while read -r _ old_log; do
                echo "Removing old session log: $(basename "$old_log")"
                rm -f "$old_log"
            done
    fi
}

cleanup_old_logs

MODE="Private (not announcing)"
if [ -n "${USERNAME:-}" ] && [ -n "${TOKEN:-}" ] && [ -n "${WEB_API_URL:-}" ]; then
    MODE="Public (announcing to web service every 15s)"
fi

RELAY_MODE_EFFECTIVE="$EDEN_ROOM_RELAY_MODE"
if [ -z "$RELAY_MODE_EFFECTIVE" ]; then
    if [ "${EDEN_ROOM_RELAY_RELIABLE:-0}" = "1" ]; then
        RELAY_MODE_EFFECTIVE="reliable (legacy EDEN_ROOM_RELAY_RELIABLE=1)"
    else
        RELAY_MODE_EFFECTIVE="reliable"
    fi
fi

{
    echo "================================================================================"
    echo "Eden Room Server - Session Started"
    echo "================================================================================"
    echo "Timestamp: $(date -Iseconds)"
    echo "Log File:  $(basename "$LOG_FILE")"
    echo "User:      $(id)"
    echo ""
    echo "Configuration:"
    echo "  Room Name: $ROOM_NAME"
    if [ -n "$ROOM_DESCRIPTION" ]; then
        echo "  Description: $ROOM_DESCRIPTION"
    fi
    echo "  Port: $PORT"
    echo "  Max Members: $MAX_MEMBERS (max: 254)"
    echo "  Bind Address: $BIND_ADDRESS"
    echo "  Ban List: $BAN_LIST_FILE"
    echo "  Unknown IP Fallback: $EDEN_ROOM_UNKNOWN_IP_FALLBACK"
    echo "  Relay Mode: $RELAY_MODE_EFFECTIVE"
    if [ "$EDEN_ROOM_DIAG_INTERVAL_SEC" = "0" ]; then
        echo "  Diagnostics: off (set EDEN_ROOM_DIAG_INTERVAL_SEC=10..30 to enable DIAG lines)"
    else
        echo "  Diagnostics: every ${EDEN_ROOM_DIAG_INTERVAL_SEC}s (look for DIAG lines)"
    fi
    if [ "$EDEN_ROOM_RELAY_BUDGET_KBPS" != "0" ]; then
        echo "  Relay Budget: ${EDEN_ROOM_RELAY_BUDGET_KBPS} KB/s per member"
    fi
    if [ -n "$EDEN_ROOM_MOD_USERNAME" ]; then
        echo "  Mod Username: $EDEN_ROOM_MOD_USERNAME (local subnet only)"
    fi
    echo "  Timezone: ${TZ:-UTC}"
    echo "  Mode: $MODE"
    echo "================================================================================"
    echo ""
} | tee "$LOG_FILE"

CMD=("/usr/local/bin/eden-room" \
  "--room-name" "$ROOM_NAME" \
  "--port" "$PORT" \
  "--max-members" "$MAX_MEMBERS" \
  "--bind-address" "$BIND_ADDRESS" \
  "--preferred-game" "$PREFERRED_GAME" \
  "--preferred-game-id" "$PREFERRED_GAME_ID" \
  "--ban-list-file" "$BAN_LIST_FILE")

if [ -n "$ROOM_DESCRIPTION" ]; then
    CMD+=("--room-description" "$ROOM_DESCRIPTION")
fi

if [ -n "$PASSWORD" ]; then
    CMD+=("--password" "$PASSWORD")
fi

if [ -n "${USERNAME:-}" ] && [ -n "${TOKEN:-}" ] && [ -n "${WEB_API_URL:-}" ]; then
    CMD+=("--username" "$USERNAME" \
          "--token" "$TOKEN" \
          "--web-api-url" "$WEB_API_URL")
fi

EDEN_PID=""
TEE_PID=""

cleanup() {
    echo ""
    echo "Received shutdown signal, stopping eden-room..."
    if [ -n "$EDEN_PID" ] && kill -0 "$EDEN_PID" 2>/dev/null; then
        kill -TERM "$EDEN_PID" 2>/dev/null || true
        wait "$EDEN_PID" 2>/dev/null || true
    fi
}

trap cleanup SIGTERM SIGINT SIGHUP

# Mirror all Eden output to both Docker logs and the session log while keeping
# the real eden-room PID available for graceful Docker stop handling.
# Capture the tee PID so we can wait for it to flush before exiting — without
# this, bash may exit before tee drains its buffer and the last log lines get
# lost. exec > >(...) sets $! to the PID of the process substitution.
exec > >(tee -a "$LOG_FILE") 2>&1
TEE_PID=$!

echo "Starting eden-room..."
"${CMD[@]}" &
EDEN_PID=$!

set +e
wait "$EDEN_PID"
status=$?
set -e

echo "Eden Room Server stopped."
# Close the write end of the tee pipe so tee sees EOF and flushes cleanly.
exec 1>&-
wait "$TEE_PID" 2>/dev/null || true
exit "$status"
