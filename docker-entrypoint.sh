#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
set -eu

CONFIG_FILE="${EMS_CONFIG_FILE:-/app/config/config.json}"
TEMPLATE_FILE="${EMS_TEMPLATE_FILE:-/app/config.template.json}"
DATA_DIR="${EMS_DATA_DIR:-/app/data}"
RUN_AS_USER="${EMS_RUN_AS_USER:-ems}"

CONFIG_DIR="$(dirname "$CONFIG_FILE")"
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
Startup continues, but device settings may be incomplete.
EOF
fi

if [ "$(id -u)" = "0" ]; then
    if [ "$created_config" = "1" ]; then
        CONFIG_DIR_UID="$(stat -c '%u' "$CONFIG_DIR" 2>/dev/null || true)"
        CONFIG_DIR_GID="$(stat -c '%g' "$CONFIG_DIR" 2>/dev/null || true)"

        if [ -n "$CONFIG_DIR_UID" ] && [ -n "$CONFIG_DIR_GID" ] && [ "$CONFIG_DIR_UID" != "0" ]; then
            chown "$CONFIG_DIR_UID:$CONFIG_DIR_GID" "$CONFIG_FILE" 2>/dev/null || true
        fi
    fi

    if [ ! -w "$DATA_DIR" ]; then
        cat >&2 <<'EOF'
Unable to write to /app/data.
Please check permissions for the mounted ./data directory.
EOF
        exit 1
    fi

    if [ "${EMS_SKIP_PRIVILEGE_DROP:-0}" != "1" ]; then
        DATA_DIR_UID="$(stat -c '%u' "$DATA_DIR" 2>/dev/null || true)"
        DATA_DIR_GID="$(stat -c '%g' "$DATA_DIR" 2>/dev/null || true)"

        if [ -n "$DATA_DIR_UID" ] && [ -n "$DATA_DIR_GID" ] && [ "$DATA_DIR_UID" != "0" ]; then
            if command -v setpriv >/dev/null 2>&1; then
                if setpriv --reuid="$DATA_DIR_UID" --regid="$DATA_DIR_GID" --clear-groups test -w "$DATA_DIR"; then
                    exec setpriv --reuid="$DATA_DIR_UID" --regid="$DATA_DIR_GID" --clear-groups "$@"
                fi
            fi
        fi

        if command -v setpriv >/dev/null 2>&1; then
            RUN_AS_UID="$(id -u "$RUN_AS_USER")"
            RUN_AS_GID="$(id -g "$RUN_AS_USER")"
            if setpriv --reuid="$RUN_AS_UID" --regid="$RUN_AS_GID" --init-groups test -w "$DATA_DIR"; then
                exec setpriv --reuid="$RUN_AS_UID" --regid="$RUN_AS_GID" --init-groups "$@"
            fi
        fi

        if command -v runuser >/dev/null 2>&1; then
            if runuser -u "$RUN_AS_USER" -- test -w "$DATA_DIR"; then
                exec runuser -u "$RUN_AS_USER" -- "$@"
            fi
        fi
    fi
fi

exec "$@"
