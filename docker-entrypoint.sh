#!/usr/bin/env bash
#
# PID 1 of the HKMP server container, running under tini.
#
# The server reads its console with Console.ReadKey(), which throws when stdin
# is not a terminal, so mono is run inside a screen session that supplies a
# pty. screen is started with -D -m: that keeps it in the foreground as a
# direct child of this script, so `wait`, exit codes and signal handling all
# behave normally.

set -euo pipefail

readonly PAYLOAD_DIR="${HKMP_HOME:-/opt/hkmp}"
readonly DATA_DIR="${HKMP_DATA:-/data}"
readonly SCREEN_DIR="${SCREENDIR:-/run/hkmp/screen}"
readonly OWNER_FILE=/run/hkmp/owner
readonly SESSION=hkmp
readonly LOG_FILE="${DATA_DIR}/logs/server.log"
readonly CONSOLE_SETTINGS="${DATA_DIR}/consolesettings.json"

log() { printf '[entrypoint] %s\n' "$*"; }
die() { printf '[entrypoint] error: %s\n' "$*" >&2; exit 1; }

port=
stop_timeout=
run_as=
screen_pid=
tail_pid=
stopping=false
forced=false

# Validate the numeric settings early: an unusable port otherwise surfaces as
# an opaque mono stack trace several seconds into startup, and a non-numeric
# STOP_TIMEOUT would silently turn every clean stop into a forced kill.
resolve_settings() {
    port="${PORT:-2222}"
    [[ "$port" =~ ^[0-9]+$ ]] && (( port >= 1 && port <= 65535 )) \
        || die "PORT must be an integer between 1 and 65535, got '${PORT:-}'"

    stop_timeout="${STOP_TIMEOUT:-30}"
    [[ "$stop_timeout" =~ ^[0-9]+$ ]] \
        || die "STOP_TIMEOUT must be a whole number of seconds, got '${STOP_TIMEOUT:-}'"
}

# Decide which identity the server runs as. When the container was started
# with --user we are already unprivileged and simply stay put.
resolve_user() {
    if (( EUID != 0 )); then
        run_as=
        log "running as uid ${EUID}, leaving privileges untouched"

        # screen calls getpwuid() and quits outright when the uid has no
        # passwd entry, which is what --user with an arbitrary uid produces.
        if ! getent passwd "$EUID" >/dev/null; then
            if [[ -w /etc/passwd ]]; then
                printf 'hkmp:x:%s:%s:HKMP:%s:/sbin/nologin\n' \
                    "$EUID" "$(id -g)" "$DATA_DIR" >> /etc/passwd
                log "added a passwd entry for uid ${EUID}"
            else
                die "uid ${EUID} has no entry in /etc/passwd and screen needs one. Use the image's PUID/PGID variables instead of --user, or pick a uid that exists in the image (1000)."
            fi
        fi
        return
    fi

    local uid="${PUID:-1000}" gid="${PGID:-1000}"
    [[ "$uid" =~ ^[0-9]+$ && "$gid" =~ ^[0-9]+$ ]] || die "PUID and PGID must be numeric"

    # screen calls getpwuid() and refuses to start without a passwd entry.
    getent group "$gid" >/dev/null || addgroup -g "$gid" "hkmp${gid}"
    getent passwd "$uid" >/dev/null || adduser -u "$uid" \
        -G "$(getent group "$gid" | cut -d: -f1)" \
        -h "$DATA_DIR" -s /sbin/nologin -D -H "hkmp${uid}"

    run_as="${uid}:${gid}"
}

prepare_dirs() {
    local owner="${run_as:-$(id -u):$(id -g)}"

    # Without root there is nothing to fix up, so say so plainly instead of
    # letting a bare mkdir failure be the diagnosis.
    if [[ -z "$run_as" && ! -w "$DATA_DIR" ]]; then
        die "${DATA_DIR} is not writable by uid $(id -u). Either chown the volume to that uid, or drop --user and set PUID/PGID instead."
    fi

    mkdir -p "$DATA_DIR" "$(dirname "$OWNER_FILE")"

    if [[ -n "$run_as" ]]; then
        # Repair a tree left behind under the wrong owner, by an older
        # container or by a freshly created bind mount. Only walk it when the
        # ownership actually differs, so a large logs/ directory does not cost
        # a recursive chown on every start.
        if [[ "$(stat -c %u:%g "$DATA_DIR")" != "$owner" ]]; then
            log "adjusting ownership of ${DATA_DIR} to ${owner}"
            chown -R "$owner" "$DATA_DIR"
        fi
    fi

    # These are created here, as root, so their ownership has to be set
    # explicitly: the check above sees a correctly owned /data and would skip
    # right over a logs/ directory this line had just created for root. The
    # server would then silently fail to open its log, taking `docker logs`
    # and every command response down with it.
    mkdir -p "$DATA_DIR/logs" "$SCREEN_DIR"
    if [[ -n "$run_as" ]]; then
        chown "$owner" "$DATA_DIR/logs" "$SCREEN_DIR"
    fi

    # screen insists on a private socket directory.
    chmod 700 "$SCREEN_DIR"
    printf '%s\n' "$owner" > "$OWNER_FILE"
    chmod 644 "$OWNER_FILE"
}

# Images up to v2.4.3 kept server state in /HKMP, and plenty of people have a
# bind mount pointing there. Without this they would come back to a server that
# had forgotten its whitelist, its bans and every setting.
migrate_legacy_data() {
    local legacy=/HKMP
    [[ -d "$legacy" ]] || return 0
    compgen -G "${legacy}/*.json" >/dev/null || return 0

    if compgen -G "${DATA_DIR}/*.json" >/dev/null; then
        log "note: ${legacy} holds data but ${DATA_DIR} is already populated, leaving ${legacy} alone"
        return 0
    fi

    log "moving server state from ${legacy} to ${DATA_DIR}; mount ${DATA_DIR} instead, ${legacy} is deprecated"
    cp -a "${legacy}/." "${DATA_DIR}/"
    [[ -n "$run_as" ]] && chown -R "$run_as" "$DATA_DIR"
    return 0
}

# The image is immutable: the assembly shipped in the image always wins over
# whatever an older container left behind in the volume.
install_payload() {
    local version
    version="$(cat "${PAYLOAD_DIR}/version" 2>/dev/null || echo unknown)"
    log "installing HKMP ${version}"

    install -m 644 "${PAYLOAD_DIR}/HKMPServer.exe" "${DATA_DIR}/HKMPServer.exe"
    [[ -f "${PAYLOAD_DIR}/HKMPServer.pdb" ]] \
        && install -m 644 "${PAYLOAD_DIR}/HKMPServer.pdb" "${DATA_DIR}/HKMPServer.pdb"

    printf '%s\n' "$version" > "${DATA_DIR}/version"

    if [[ -n "$run_as" ]]; then
        chown "$run_as" "${DATA_DIR}/HKMPServer.exe" "${DATA_DIR}/version"
        [[ -f "${DATA_DIR}/HKMPServer.pdb" ]] && chown "$run_as" "${DATA_DIR}/HKMPServer.pdb"
    fi
    return 0
}

# FullSynchronisation (bosses, enemies, world and save state) only exists in
# consolesettings.json, so expose it as an environment variable. The port is
# written alongside it purely to keep the file honest; the command line
# argument is what the server actually uses.
configure_console_settings() {
    local full_sync="${FULL_SYNC:-}"
    [[ -n "$full_sync" ]] || return 0

    case "${full_sync,,}" in
        1|true|yes|on)   full_sync=true ;;
        0|false|no|off)  full_sync=false ;;
        *) die "FULL_SYNC must be true or false, got '${FULL_SYNC}'" ;;
    esac

    local tmp="${CONSOLE_SETTINGS}.tmp"
    if [[ -f "$CONSOLE_SETTINGS" ]]; then
        jq --argjson fs "$full_sync" --argjson p "$port" \
            '.FullSynchronisation = $fs | .Port = $p' "$CONSOLE_SETTINGS" > "$tmp"
    else
        jq -n --argjson fs "$full_sync" --argjson p "$port" \
            '{Port: $p, FullSynchronisation: $fs}' > "$tmp"
    fi
    mv "$tmp" "$CONSOLE_SETTINGS"
    [[ -n "$run_as" ]] && chown "$run_as" "$CONSOLE_SETTINGS"

    log "full synchronisation: ${full_sync}"
    return 0
}

start_server() {
    local cmd=(screen -DmS "$SESSION" mono "${DATA_DIR}/HKMPServer.exe" "$port")
    [[ -n "$run_as" ]] && cmd=(su-exec "$run_as" "${cmd[@]}")

    log "starting server on UDP port ${port}"
    "${cmd[@]}" &
    screen_pid=$!
}

# The console logger writes into screen's pty, which nothing reads. The rolling
# file logger receives the same messages, so that file is what `docker logs`
# gets to see.
start_log_relay() {
    tail -n 0 -F "$LOG_FILE" 2>/dev/null &
    tail_pid=$!
}

send_console() {
    local args=(screen -S "$SESSION" -p 0 -X stuff "$1"$'\r')
    [[ -n "$run_as" ]] && args=(su-exec "$run_as" "${args[@]}")
    "${args[@]}" 2>/dev/null || true
}

# `docker stop` must mean a clean shutdown: the server has to disconnect its
# players and flush state, which only the `exit` command does.
shutdown() {
    trap '' TERM INT
    stopping=true
    log "stop requested, asking the server to exit"
    send_console exit

    local waited=0
    while kill -0 "$screen_pid" 2>/dev/null && (( waited < stop_timeout )); do
        sleep 1
        waited=$(( waited + 1 ))
    done

    if kill -0 "$screen_pid" 2>/dev/null; then
        log "no clean exit after ${stop_timeout}s, terminating"
        forced=true
        kill -TERM "$screen_pid" 2>/dev/null || true
    fi
}

cleanup() {
    [[ -n "$tail_pid" ]] && kill "$tail_pid" 2>/dev/null
    return 0
}

main() {
    resolve_settings
    resolve_user
    prepare_dirs
    migrate_legacy_data
    install_payload
    configure_console_settings

    start_server
    start_log_relay

    trap shutdown TERM INT
    trap cleanup EXIT

    # `wait` returns early whenever a trap fires, so keep waiting until the
    # child is really gone and its own status is what we report.
    local rc=0
    set +e
    while :; do
        wait "$screen_pid"
        rc=$?
        kill -0 "$screen_pid" 2>/dev/null || break
    done
    set -e

    # A signal that arrives while `wait` is blocking makes it return 128+signo
    # regardless of how the child actually ended, so a stop we asked for and
    # that the server honoured is reported as the success it is.
    if $stopping && ! $forced; then
        rc=0
    fi

    log "server stopped (exit code ${rc})"
    exit "$rc"
}

main "$@"
