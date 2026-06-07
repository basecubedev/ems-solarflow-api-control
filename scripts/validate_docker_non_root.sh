#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
set -eu

ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
IMAGE="${IMAGE:-ems-solarflow-non-root-validation}"
HOST_UID="${PUID:-$(id -u)}"
HOST_GID="${PGID:-$(id -g)}"
TMP_ROOT="$(mktemp -d)"

cleanup() {
    if [ -n "${TMP_ROOT:-}" ] && [ -d "$TMP_ROOT" ]; then
        docker run --rm --entrypoint sh -v "$TMP_ROOT:/case" "$IMAGE" \
            -c "chown -R $(id -u):$(id -g) /case" >/dev/null 2>&1 || true
        rm -rf "$TMP_ROOT"
    fi
}
trap cleanup EXIT INT TERM

echo "Building $IMAGE ..."
docker build -t "$IMAGE" "$ROOT_DIR"

echo "Checking non-root PID 1 and generated file ownership ..."
RUN_DIR="$TMP_ROOT/run"
mkdir -p "$RUN_DIR/config" "$RUN_DIR/data"

docker run --rm \
    -e PUID="$HOST_UID" \
    -e PGID="$HOST_GID" \
    -v "$RUN_DIR/config:/app/config" \
    -v "$RUN_DIR/data:/app/data" \
    "$IMAGE" \
    sh -c '
        uid="$(awk "/^Uid:/ { print \$2 }" /proc/1/status)"
        gid="$(awk "/^Gid:/ { print \$2 }" /proc/1/status)"
        test "$uid" != "0"
        test "$gid" != "0"
        touch /app/config/non-root-config-write-test
        touch /app/data/non-root-data-write-test
        printf "pid1_uid=%s pid1_gid=%s\n" "$uid" "$gid"
    '

for path in \
    "$RUN_DIR/config/config.json" \
    "$RUN_DIR/config/non-root-config-write-test" \
    "$RUN_DIR/data/non-root-data-write-test"
do
    owner="$(stat -c '%u:%g' "$path")"
    expected="$HOST_UID:$HOST_GID"
    if [ "$owner" != "$expected" ]; then
        echo "Unexpected owner for $path: $owner, expected $expected" >&2
        exit 1
    fi
done

echo "Checking root-owned bind mount refusal ..."
FAIL_DIR="$TMP_ROOT/root-owned"
mkdir -p "$FAIL_DIR"
docker run --rm --entrypoint sh -v "$FAIL_DIR:/case" "$IMAGE" \
    -c 'mkdir -p /case/config /case/data && chown 0:0 /case/config /case/data'

set +e
failure_output="$(
    docker run --rm \
        -e PUID="$HOST_UID" \
        -e PGID="$HOST_GID" \
        -v "$FAIL_DIR/config:/app/config" \
        -v "$FAIL_DIR/data:/app/data" \
        "$IMAGE" \
        sh -c 'echo should-not-run' 2>&1
)"
failure_status=$?
set -e

if [ "$failure_status" -eq 0 ]; then
    echo "Expected root-owned bind mount startup to fail." >&2
    exit 1
fi

printf '%s\n' "$failure_output" | grep -q "EMS refuses to start as root."

echo "Docker non-root validation passed."
