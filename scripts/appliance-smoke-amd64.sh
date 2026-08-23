#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the amd64 package and smoke-test it in a clean Debian 13 systemd guest.
#
#   scripts/appliance-smoke-amd64.sh [--keep] [--image debian:trixie-slim]
#
# Everything happens inside a disposable privileged container with systemd as
# PID 1, so the shipped units are started by a real systemd. The container and
# the build directory are removed on exit; nothing on the developer host is
# modified.
#
# Exit status: 0 every check passed, 1 a check failed, 3 the environment cannot
# run the test. A skipped run is never reported as a pass.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
IMAGE=debian:trixie-slim
KEEP=0
NAME="ems-appliance-smoke-amd64-$$"

while [ $# -gt 0 ]; do
    case "$1" in
        --keep) KEEP=1; shift ;;
        --image) IMAGE=$2; shift 2 ;;
        -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

fail_environment() {
    echo "appliance-smoke-amd64: $1" >&2
    echo "RESULT: NOT RUN" >&2
    exit 3
}

command -v docker >/dev/null 2>&1 || fail_environment "docker is not installed"
docker info >/dev/null 2>&1 || fail_environment "the Docker daemon is not reachable"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/ems-appliance-smoke.XXXXXX")
cleanup() {
    if [ "$KEEP" -eq 1 ]; then
        echo "kept: container $NAME, build directory $WORK"
        return
    fi
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT

echo "== building the amd64 package =="
"$ROOT/packaging/appliance/build-deb.sh" --output "$WORK" --arch amd64 >/dev/null
PACKAGE=$(ls "$WORK"/*.deb)
( cd "$WORK" && sha256sum -c "$(basename "$PACKAGE").sha256" )

echo "== booting a clean $IMAGE guest =="
docker run -d --name "$NAME" --privileged --cgroupns=host \
    -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
    "$IMAGE" /bin/bash -c "
        apt-get update -qq >/dev/null 2>&1
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
            systemd systemd-sysv adduser python3 >/dev/null 2>&1
        exec /lib/systemd/systemd" >/dev/null \
    || fail_environment "cannot start a privileged systemd container"

for _ in $(seq 1 40); do
    state=$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)
    case "$state" in running|degraded) break ;; esac
    sleep 3
done
case "$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)" in
    running|degraded) ;;
    *) docker logs "$NAME" 2>&1 | tail -40 >&2
       fail_environment "systemd did not finish booting in the guest" ;;
esac

docker cp "$PACKAGE" "$NAME:/root/$(basename "$PACKAGE")"
docker cp "$ROOT/scripts/appliance-guest-smoke.sh" "$NAME:/root/guest-smoke.sh"

echo "== running the guest smoke test =="
set +e
docker exec "$NAME" /bin/sh /root/guest-smoke.sh "/root/$(basename "$PACKAGE")" amd64
status=$?
set -e

if [ "$status" -ne 0 ]; then
    echo "== guest journals ==" >&2
    docker exec "$NAME" journalctl -n 200 --no-pager >&2 2>&1 || true
fi
exit "$status"
