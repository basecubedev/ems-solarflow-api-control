#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
set -eu

CONFIG_FILE="${EMS_CONFIG_FILE:-/app/config/config.json}"
TEMPLATE_FILE="${EMS_TEMPLATE_FILE:-/app/config.template.json}"
DATA_DIR="${EMS_DATA_DIR:-/app/data}"
RUN_AS_USER="${EMS_RUN_AS_USER:-ems}"
CONFIG_DIR="$(dirname "$CONFIG_FILE")"

root_refusal() {
    cat >&2 <<'EOF'
EMS refuses to start as root.
The mounted /app/data or /app/config directory is not writable by the non-root runtime user.
Create the host directories as your normal user or set PUID/PGID:
  mkdir -p config data
  sudo chown $(id -u):$(id -g) config data
  PUID=$(id -u) PGID=$(id -g) docker compose up -d
EOF
    exit 1
}

invalid_uid_gid() {
    cat >&2 <<'EOF'
EMS refuses to start as root.
PUID and PGID must both be set to non-zero numeric values.
Example:
  PUID=$(id -u) PGID=$(id -g) docker compose up -d
EOF
    exit 1
}

is_positive_id() {
    case "${1:-}" in
        ''|*[!0-9]*|0) return 1 ;;
        *) return 0 ;;
    esac
}

path_uid() {
    stat -c '%u' "$1" 2>/dev/null || printf ''
}

path_gid() {
    stat -c '%g' "$1" 2>/dev/null || printf ''
}

select_runtime_ids() {
    DATA_UID="$(path_uid "$DATA_DIR")"
    DATA_GID="$(path_gid "$DATA_DIR")"
    CONFIG_UID="$(path_uid "$CONFIG_DIR")"
    CONFIG_GID="$(path_gid "$CONFIG_DIR")"

    if [ -n "${PUID:-}" ] || [ -n "${PGID:-}" ]; then
        if ! is_positive_id "${PUID:-}" || ! is_positive_id "${PGID:-}"; then
            invalid_uid_gid
        fi
        if [ "$DATA_UID" = "0" ] || [ "$CONFIG_UID" = "0" ]; then
            root_refusal
        fi
        RUNTIME_UID="$PUID"
        RUNTIME_GID="$PGID"
        return
    fi

    if [ "$DATA_UID" = "0" ] || [ "$CONFIG_UID" = "0" ]; then
        root_refusal
    fi

    if is_positive_id "$DATA_UID" && is_positive_id "$DATA_GID"; then
        RUNTIME_UID="$DATA_UID"
        RUNTIME_GID="$DATA_GID"
        return
    fi

    if is_positive_id "$CONFIG_UID" && is_positive_id "$CONFIG_GID"; then
        RUNTIME_UID="$CONFIG_UID"
        RUNTIME_GID="$CONFIG_GID"
        return
    fi

    if id "$RUN_AS_USER" >/dev/null 2>&1; then
        RUNTIME_UID="$(id -u "$RUN_AS_USER")"
        RUNTIME_GID="$(id -g "$RUN_AS_USER")"
        if is_positive_id "$RUNTIME_UID" && is_positive_id "$RUNTIME_GID"; then
            return
        fi
    fi

    root_refusal
}

test_as_runtime_user() {
    setpriv --reuid="$RUNTIME_UID" --regid="$RUNTIME_GID" --clear-groups "$@"
}

drop_privileges_or_fail() {
    if ! command -v setpriv >/dev/null 2>&1; then
        cat >&2 <<'EOF'
EMS refuses to start as root.
The container image is missing setpriv, so it cannot safely drop privileges.
EOF
        exit 1
    fi

    select_runtime_ids

    if [ ! -d "$CONFIG_DIR" ] || [ ! -d "$DATA_DIR" ]; then
        root_refusal
    fi

    if [ ! -f "$CONFIG_FILE" ] && ! test_as_runtime_user test -w "$CONFIG_DIR"; then
        root_refusal
    fi

    if [ -f "$CONFIG_FILE" ] && ! test_as_runtime_user test -w "$CONFIG_FILE"; then
        root_refusal
    fi

    if ! test_as_runtime_user test -w "$DATA_DIR"; then
        root_refusal
    fi

    EMS_PRIVILEGE_DROPPED=1 exec setpriv \
        --reuid="$RUNTIME_UID" \
        --regid="$RUNTIME_GID" \
        --clear-groups \
        "$0" "$@"
}

if [ "$(id -u)" = "0" ] && [ "${EMS_SKIP_PRIVILEGE_DROP:-0}" != "1" ]; then
    drop_privileges_or_fail "$@"
fi

created_config=0

if ! mkdir -p "$CONFIG_DIR"; then
    cat >&2 <<'EOF'
Unable to create /app/config/config.json.
Please check permissions for the mounted ./config directory.
EOF
    exit 1
fi

if ! mkdir -p "$DATA_DIR"; then
    cat >&2 <<'EOF'
Unable to create /app/data.
Please check permissions for the mounted ./data directory.
EOF
    exit 1
fi

if [ ! -w "$DATA_DIR" ]; then
    cat >&2 <<'EOF'
Unable to write to /app/data.
Please check permissions for the mounted ./data directory.
EOF
    exit 1
fi

if [ -f "$CONFIG_FILE" ] && [ ! -w "$CONFIG_FILE" ]; then
    cat >&2 <<'EOF'
Unable to write to /app/config/config.json.
Please check permissions for the mounted ./config directory and config.json.
EOF
    exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
    if ! cp "$TEMPLATE_FILE" "$CONFIG_FILE"; then
        cat >&2 <<'EOF'
Unable to create /app/config/config.json.
Please check permissions for the mounted ./config directory.
EOF
        exit 1
    fi
    created_config=1

    cat >&2 <<'EOF'
No config.json found.
Created /app/config/config.json from config.template.json.

Please review and edit ./config/config.json for your installation.
EOF
fi

if [ -f "$CONFIG_FILE" ] && [ -f "$TEMPLATE_FILE" ] && cmp -s "$CONFIG_FILE" "$TEMPLATE_FILE"; then
    cat >&2 <<'EOF'
WARNING: config.json still matches the shipped template.
Please review ./config/config.json and configure your installation.
Startup continues in safe mode until required placeholders are replaced.
Hardware writes are disabled while template placeholders remain.
EOF
fi

if [ "$(id -u)" = "0" ] && [ "${EMS_SKIP_PRIVILEGE_DROP:-0}" != "1" ]; then
    root_refusal
fi

exec "$@"
