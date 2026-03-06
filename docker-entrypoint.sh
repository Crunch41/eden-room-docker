#!/bin/bash
set -e

# ============================================================================
# PUID/PGID handling (LinuxServer.io style for Unraid compatibility)
# Defaults: PUID=99 (nobody), PGID=100 (users) - standard for Unraid
# ============================================================================
PUID="${PUID:-99}"
PGID="${PGID:-100}"

# Only run user modification if we're root
if [ "$(id -u)" = "0" ]; then
    echo "Setting up user eden with PUID=${PUID} PGID=${PGID}"

    # Modify the eden user/group to match requested IDs
    groupmod -o -g "$PGID" eden 2>/dev/null || true
    usermod -o -u "$PUID" eden 2>/dev/null || true

    # Fix ownership of home directory
    chown -R eden:eden /home/eden

    # Re-exec this script as the eden user
    exec gosu eden "$0" "$@"
fi

# ============================================================================
# Default values
# ============================================================================
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

# Log settings
MAX_LOG_FILES="${MAX_LOG_FILES:-10}"  # Keep last 10 session logs

# Generate timestamped log filename for this session
SESSION_TIMESTAMP=$(date +%d-%m-%Y_%H-%M-%S)
LOG_FILE="${LOG_DIR}/session_${SESSION_TIMESTAMP}.log"

# Ensure log directory exists
mkdir -p "$LOG_DIR"

# Create ban list file with correct header if it doesn't exist
if [ ! -f "$BAN_LIST_FILE" ]; then
    echo "YuzuRoom-BanList-1" > "$BAN_LIST_FILE"
    echo "" >> "$BAN_LIST_FILE"
fi

# Function: Cleanup old session logs (keep last N)
cleanup_old_logs() {
    local log_count=$(ls -1 "${LOG_DIR}"/session_*.log 2>/dev/null | wc -l)
    if [ "$log_count" -gt "$MAX_LOG_FILES" ]; then
        local to_delete=$((log_count - MAX_LOG_FILES))
        ls -1t "${LOG_DIR}"/session_*.log | tail -n "$to_delete" | while read -r old_log; do
            echo "Removing old session log: $(basename "$old_log")"
            rm -f "$old_log"
        done
    fi
}

# Cleanup old logs before starting
cleanup_old_logs

# Determine mode
MODE="Private (not announcing)"
if [ -n "$USERNAME" ] && [ -n "$TOKEN" ] && [ -n "$WEB_API_URL" ]; then
    MODE="Public (announcing to web service every 15s)"
fi

# Print session header
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
    echo "  Mode: $MODE"
    echo "================================================================================"
    echo ""
} | tee "$LOG_FILE"

# Build command
CMD=("/usr/local/bin/eden-room" \
  "--room-name" "$ROOM_NAME" \
  "--port" "$PORT" \
  "--max-members" "$MAX_MEMBERS" \
  "--bind-address" "$BIND_ADDRESS" \
  "--preferred-game" "$PREFERRED_GAME" \
  "--preferred-game-id" "$PREFERRED_GAME_ID" \
  "--ban-list-file" "$BAN_LIST_FILE")

# Add optional parameters
if [ -n "$ROOM_DESCRIPTION" ]; then
    CMD+=("--room-description" "$ROOM_DESCRIPTION")
fi

if [ -n "$PASSWORD" ]; then
    CMD+=("--password" "$PASSWORD")
fi

# Add public room credentials
if [ -n "$USERNAME" ] && [ -n "$TOKEN" ] && [ -n "$WEB_API_URL" ]; then
    CMD+=("--username" "$USERNAME" \
          "--token" "$TOKEN" \
          "--web-api-url" "$WEB_API_URL")
fi

# Signal handling for graceful shutdown
EDEN_PID=""
cleanup() {
    echo ""
    echo "Received shutdown signal, stopping eden-room..."
    if [ -n "$EDEN_PID" ]; then
        kill -TERM "$EDEN_PID" 2>/dev/null || true
        wait "$EDEN_PID" 2>/dev/null || true
    fi
    echo "Eden Room Server stopped."
    exit 0
}
trap cleanup SIGTERM SIGINT SIGHUP

# Execute and log (tee to both console and file)
# Run in background so we can handle signals
"${CMD[@]}" 2>&1 | tee -a "$LOG_FILE" &
EDEN_PID=$!

# Wait for the process to exit
wait $EDEN_PID
