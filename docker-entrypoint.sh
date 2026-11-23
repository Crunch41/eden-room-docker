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
EDEN_ROOM_RELAY_MODE="${EDEN_ROOM_RELAY_MODE:-unsequenced}"
export EDEN_ROOM_RELAY_MODE
EDEN_ROOM_RELAY_BUDGET_KBPS="${EDEN_ROOM_RELAY_BUDGET_KBPS:-0}"
export EDEN_ROOM_RELAY_BUDGET_KBPS
EDEN_ROOM_DIAG_INTERVAL_SEC="${EDEN_ROOM_DIAG_INTERVAL_SEC:-0}"
export EDEN_ROOM_DIAG_INTERVAL_SEC
# One JSON line per finished session (who, when, how long, which games, ping and
# connection), for stats tools. Kept across restarts; never trimmed.
EDEN_ROOM_SESSIONS_FILE="${EDEN_ROOM_SESSIONS_FILE:-${LOG_DIR}/sessions.jsonl}"
export EDEN_ROOM_SESSIONS_FILE
# TCP port of the stats page (player dashboard over the sessions file); 0 = off.
STATS_PORT="${STATS_PORT:-8080}"
# Who may load it: IPv4 addresses or networks (1.2.3.4, 10.0.0.0/8), separated by
# spaces or commas; empty = anyone who can reach the port. Set it to a reverse
# proxy's address to make the proxy the only way in.
STATS_ALLOW="${STATS_ALLOW:-}"

require_number STATS_PORT "$STATS_PORT"
if [ "$STATS_PORT" -gt 65535 ]; then
    echo "ERROR: STATS_PORT must be 0 (off) or 1-65535 (got '$STATS_PORT')" >&2
    exit 1
fi
for entry in ${STATS_ALLOW//,/ }; do
    if ! [[ "$entry" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]]; then
        echo "ERROR: STATS_ALLOW takes IPv4 addresses or networks like 192.168.1.5 or 10.0.0.0/8 (got '$entry')" >&2
        exit 1
    fi
done
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
    unsequenced|sequenced|reliable) ;;
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

# Runs before this session's log exists, so it leaves room for it: MAX_LOG_FILES
# counts the new log too.
cleanup_old_logs() {
    local log_count
    log_count="$(find "$LOG_DIR" -maxdepth 1 -name 'session_*.log' -type f 2>/dev/null | wc -l)"
    if [ "$log_count" -ge "$MAX_LOG_FILES" ]; then
        local to_delete=$((log_count - MAX_LOG_FILES + 1))
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

# Eden dropped lobby tokens (upstream 5274828c9): a public room needs only the account name and
# the lobby service, and eden-room no longer accepts --token.
PUBLIC=""
if [ -n "${USERNAME:-}" ] && [ -n "${WEB_API_URL:-}" ]; then
    PUBLIC=1
fi
MODE="Private (not announcing)"
if [ -n "$PUBLIC" ]; then
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
    echo "  Sessions File: $EDEN_ROOM_SESSIONS_FILE"
    if [ "$STATS_PORT" = "0" ]; then
        echo "  Stats Page: off"
    else
        echo "  Stats Page: http://<container address>:$STATS_PORT/ (TCP; STATS_PORT=0 turns it off)"
        if [ -n "$STATS_ALLOW" ]; then
            echo "  Stats Allowed From: $STATS_ALLOW (everyone else gets 403)"
        else
            echo "  Stats Allowed From: anyone who can reach the port (set STATS_ALLOW to limit it)"
        fi
    fi
    echo "  Unknown IP Fallback: $EDEN_ROOM_UNKNOWN_IP_FALLBACK"
    echo "  Relay Mode: $EDEN_ROOM_RELAY_MODE"
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
    if [ -n "${TOKEN:-}" ]; then
        echo "  Note: TOKEN is no longer used by Eden and is ignored"
    fi
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

if [ -n "$PUBLIC" ]; then
    CMD+=("--username" "$USERNAME" \
          "--web-api-url" "$WEB_API_URL")
fi

EDEN_PID=""
TEE_PID=""

cleanup() {
    echo ""
    echo "Received shutdown signal, stopping eden-room..."
    if [ -n "$EDEN_PID" ] && kill -0 "$EDEN_PID" 2>/dev/null; then
        kill -TERM "$EDEN_PID" 2>/dev/null || true
        # Keep the room's own exit status: a crash while stopping must not be
        # reported as an ordinary stop.
        set +e
        wait "$EDEN_PID" 2>/dev/null
        ROOM_STATUS=$?
        set -e
    fi
}

ROOM_STATUS=""
trap cleanup SIGTERM SIGINT SIGHUP

# Writes the room's output to the session log as it is, and to the container log
# (docker logs, Unraid's log window) in colour. Both get a line when the day
# changes, since log lines only carry the time. The container log leaves out
# STAT lines unless DIAG reports are on: they are for chasing lag, and the LEAVE
# line already rates the connection. NO_COLOR turns the colour off.
log_sink() {
    local line day today label tag body main rest show_stat=""
    local grey="" green="" red="" yellow="" cyan="" magenta="" bold="" reset=""
    if [ "${EDEN_ROOM_DIAG_INTERVAL_SEC:-0}" != "0" ]; then
        show_stat=1
    fi
    if [ -z "${NO_COLOR:-}" ]; then
        grey=$'\e[90m' green=$'\e[32m' red=$'\e[31m' yellow=$'\e[33m'
        cyan=$'\e[36m' magenta=$'\e[35m' bold=$'\e[1m' reset=$'\e[0m'
    fi
    exec 3>> "$1"
    printf -v day '%(%Y-%m-%d)T' -1
    while IFS= read -r line || [ -n "$line" ]; do
        printf -v today '%(%Y-%m-%d)T' -1
        if [ "$today" != "$day" ]; then
            day=$today
            printf -v label '──────────────────── %(%a %d %b %Y)T ────────────────────' -1
            printf '%s\n' "$label" >&3
            printf '%s%s%s\n' "$grey" "$label" "$reset"
        fi
        printf '%s\n' "$line" >&3

        if ! [[ "$line" =~ ^(\[[0-9:]{8}\])\ (JOIN|LEAVE|KICK|BAN|CHAT|GAME|STAT|DIAG)\ +(.*)$ ]]; then
            case "$line" in
                *"<Error>"*|*"<Critical>"*|ERROR*) printf '%s%s%s\n' "$red" "$line" "$reset" ;;
                *"<Warning>"*) printf '%s%s%s\n' "$yellow" "$line" "$reset" ;;
                *) printf '%s\n' "$line" ;;
            esac
            continue
        fi
        printf -v tag '%-5s' "${BASH_REMATCH[2]}"
        body=${BASH_REMATCH[3]}
        # "<name> joined" or "<name> left after ...", then two spaces and the details.
        main=${body%%  *}
        rest=${body#"$main"}
        case "${BASH_REMATCH[2]}" in
            STAT)
                if [ -n "$show_stat" ]; then
                    printf '%s%s%s\n' "$grey" "$line" "$reset"
                fi
                ;;
            DIAG)
                printf '%s%s%s\n' "$grey" "$line" "$reset"
                ;;
            LEAVE)
                if [[ "$main" == *"didn't play"* ]]; then
                    printf '%s%s%s\n' "$grey" "$line" "$reset"
                    continue
                fi
                main=${main/laggy connection/$bold$yellow"laggy connection"$reset}
                main=${main/some lag spikes/$yellow"some lag spikes"$reset}
                main=${main/connection good/$grey"connection good"$reset}
                printf '%s%s%s %s%s%s  %s%s%s%s\n' "$grey" "${BASH_REMATCH[1]}" "$reset" \
                    "$red" "$tag" "$reset" "$main" "$grey" "$rest" "$reset"
                ;;
            *)
                case "${BASH_REMATCH[2]}" in
                    JOIN) label=$green ;;
                    KICK) label=$yellow ;;
                    BAN) label=$bold$red ;;
                    CHAT) label=$magenta ;;
                    *) label=$cyan ;;
                esac
                printf '%s%s%s %s%s%s  %s%s%s%s\n' "$grey" "${BASH_REMATCH[1]}" "$reset" \
                    "$label" "$tag" "$reset" "$main" "$grey" "$rest" "$reset"
                ;;
        esac
    done
}

# Send all output through log_sink, keeping the real eden-room PID for a clean
# docker stop. Keep the sink's PID too, to wait for it to write the last lines
# before exiting; exec > >(...) sets $! to the PID of the process substitution.
exec > >(log_sink "$LOG_FILE") 2>&1
TEE_PID=$!

# The stats page: BusyBox's httpd serving the page and the sessions file, nothing
# else. It runs at the lowest CPU and disk priority so it can never take time from
# the room, logs nothing, and the room doesn't depend on it: if it fails, the room
# carries on.
STATS_PID=""
start_stats() {
    if [ "${STATS_PORT:-0}" = "0" ]; then
        return 0
    fi
    local root=/tmp/eden-room-stats
    rm -rf "$root" "$root.conf"
    mkdir -p "$root/data"
    cp /usr/local/share/eden-room/stats/index.html "$root/"
    ln -s "$EDEN_ROOM_SESSIONS_FILE" "$root/data/sessions.jsonl"
    {
        printf '.jsonl:application/x-ndjson\n'
        if [ -n "${STATS_ALLOW:-}" ]; then
            for entry in ${STATS_ALLOW//,/ }; do
                printf 'A:%s\n' "$entry"
            done
            printf 'D:*\n'
        fi
    } > "$root.conf"
    nice -n 19 ionice -c 3 busybox httpd -f -p "${BIND_ADDRESS}:${STATS_PORT}" -h "$root" -c "$root.conf" \
        < /dev/null > /dev/null 2>&1 &
    STATS_PID=$!
    sleep 0.2
    if ! kill -0 "$STATS_PID" 2>/dev/null; then
        echo "WARNING: the stats page didn't start (is TCP port $STATS_PORT free?). The room runs without it."
        STATS_PID=""
    fi
}
start_stats || echo "WARNING: the stats page didn't start. The room runs without it."

echo "Starting eden-room..."
"${CMD[@]}" &
EDEN_PID=$!

set +e
wait "$EDEN_PID"
status=$?
set -e
# A shutdown signal interrupts the wait above (status 143); cleanup() waited
# for the room itself and kept its real exit status.
if [ -n "$ROOM_STATUS" ]; then
    status=$ROOM_STATUS
fi

echo "Eden Room Server stopped."
if [ -n "${STATS_PID:-}" ]; then
    kill "$STATS_PID" 2>/dev/null || true
fi
# Close the write end of the sink's pipe so it sees EOF and finishes writing.
exec 1>&- 2>&-
wait "$TEE_PID" 2>/dev/null || true
exit "$status"
