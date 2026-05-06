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

    groupmod -o -g "$PGID" eden 2>/dev/null || true
    usermod -o -u "$PUID" eden 2>/dev/null || true

    current_owner="$(stat -c '%u:%g' /home/eden 2>/dev/null || echo '')"
    if [ "$current_owner" != "${PUID}:${PGID}" ]; then
        chown -R eden:eden /home/eden
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

require_number PORT "$PORT"
require_number MAX_MEMBERS "$MAX_MEMBERS"
require_number MAX_LOG_FILES "$MAX_LOG_FILES"

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
exec > >(tee -a "$LOG_FILE") 2>&1

echo "Starting eden-room..."
"${CMD[@]}" &
EDEN_PID=$!

set +e
wait "$EDEN_PID"
status=$?
set -e

echo "Eden Room Server stopped."
exit "$status"
