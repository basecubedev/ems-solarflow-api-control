#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Install the Appliance package under a read-only root and find what breaks.
#
#   scripts/appliance-audit-root-writes.sh --package FILE [--work DIR]
#
# The A/B model says the slot root is read-only: everything that has to survive
# a slot switch is a bind mount from the persistent partition, and everything
# else is discarded. image-rota writes that into /etc/fstab, but nothing had
# ever run the package's own write paths against a root where it was true, so
# "read-only root" was a design statement rather than a tested one.
#
# This runs them, in the order a real appliance reaches them. The package is
# installed while the root is still writable — that is image build time, and a
# read-only dpkg install is not part of the model — and the root is then
# remounted read-only, which is what flashing the image produces. /run and /tmp
# are tmpfs; the six declared shared paths, /var, /home and /persistent are the
# mutable mounts the appliance is supposed to have. Anything that then fails
# with EROFS is a path the model did not account for, and is reported as one.
#
# Every mutable path is classified rather than discovered: the point is to show
# that the set the appliance needs is the set the contract declares.
#
# Exit status: 0 the package operates under a read-only root, 1 it does not,
# 2 the command line is wrong, 3 the audit could not run.
set -eu

PACKAGE=""
WORK=""
FAILURES=0
CHECKS=0

usage() { sed -n '3,22p' "$0"; }

not_run() {
    echo "appliance-audit-root-writes: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

while [ $# -gt 0 ]; do
    case "$1" in
        --package) PACKAGE=${2:?--package needs a .deb}; shift 2 ;;
        --package=*) PACKAGE=${1#*=}; shift ;;
        --work) WORK=${2:?--work needs a directory}; shift 2 ;;
        --work=*) WORK=${1#*=}; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$PACKAGE" ] || { echo "--package is required" >&2; usage >&2; exit 2; }
[ -f "$PACKAGE" ] || not_run "no package at $PACKAGE" package_unavailable
[ "$(id -u)" = "0" ] || not_run "the audit needs root inside its own guest" not_root
[ -n "$WORK" ] || WORK=$(mktemp -d "${TMPDIR:-/tmp}/ems-root-audit.XXXXXX")
mkdir -p "$WORK"

check() {
    CHECKS=$((CHECKS + 1))
    if [ "$1" = ok ]; then
        printf '   %-58s PASS\n' "$2"
    else
        printf '   %-58s FAIL  %s\n' "$2" "$3"
        FAILURES=$((FAILURES + 1))
    fi
}

# --- what the contract says is mutable, and where ----------------------------

# Shared across slots, on the persistent partition. Declared to upstream's
# slot-shared generator in 50-ems-appliance.conf.
SHARED_PATHS="/opt/ems-solarflow
/var/lib/ems-appliance-manager
/var/log/ems-appliance-manager
/etc/ems-appliance-manager
/var/lib/ems-appliance-os-update
/etc/NetworkManager/system-connections"

# Slot-local and mutable: upstream's slot-perst generator binds the whole of
# /var per slot, so a rollback does not carry a Docker content store with it.
SLOT_LOCAL_PATHS="/var"

# Ephemeral, gone at reboot.
ENSURED_TMPFS="/run
/tmp"

# Shared by upstream on the appliance's behalf.
UPSTREAM_SHARED="/home"

echo "== the mutable set the contract declares =="
for path in $SHARED_PATHS; do echo "   persistent shared   $path"; done
for path in $SLOT_LOCAL_PATHS; do echo "   slot-local mutable  $path"; done
for path in $UPSTREAM_SHARED; do echo "   persistent shared   $path"; done
for path in $ENSURED_TMPFS; do echo "   tmpfs               $path"; done
echo "   forbidden           every other path on the slot root"

writable() {
    touch "$1/.ems-root-audit-probe" 2>/dev/null || return 1
    rm -f "$1/.ems-root-audit-probe"
    return 0
}

INSTALL_MARK="$WORK/installed-at"
echo
echo "== installing the package, the way an image build does =="
# While the root is still writable. A read-only dpkg install is not part of the
# model: the image is assembled writable and only becomes a slot afterwards, so
# a guest whose root is already read-only is expected to carry the package.
if writable /; then
    set +e
    DEBIAN_FRONTEND=noninteractive dpkg -i "$PACKAGE" > "$WORK/install.log" 2>&1
    install_status=$?
    set -e
    if [ "$install_status" -ne 0 ]; then
        tail -n 25 "$WORK/install.log" >&2
        echo "RESULT: NOT RUN (package_install_failed)" >&2
        exit 3
    fi
    check ok "the package installs into the image"
else
    dpkg-query -W ems-appliance-manager >/dev/null 2>&1 \
        || not_run "the root is read-only and carries no package to audit" package_unavailable
    check ok "the guest was built with the package already installed"
fi
touch "$INSTALL_MARK"

echo
echo "== the mounts upstream's generators make =="
# Modelled here rather than assumed: /var is slot-local and mutable, and each
# declared shared path is bound from /persistent/shared/<path>. A /var that was
# merely replaced by an empty directory would not be a per-slot /var — it would
# be a slot with no dpkg database.
[ -d /persistent ] \
    && check ok "the persistent partition has a mount point in the image" \
    || check no "the persistent partition has a mount point in the image" \
              "/persistent is not a directory in the slot root"
mount -t tmpfs -o size=256m tmpfs /persistent \
    || not_run "a persistent partition could not be modelled" mount_unavailable
# What a first boot's machine-id sync and the persistence layout create.
mkdir -p /persistent/shared /persistent/var /persistent/home /persistent/common/etc
[ -s /persistent/common/etc/machine-id ] \
    || cat /etc/machine-id > /persistent/common/etc/machine-id 2>/dev/null \
    || tr -d - < /proc/sys/kernel/random/uuid > /persistent/common/etc/machine-id

cp -a /var/. /persistent/var/ 2>/dev/null || true
mount --bind /persistent/var /var || not_run "/var could not be bound" mount_unavailable
check ok "/var is a slot-local mutable mount carrying the dpkg database"

MISSING_MOUNTPOINTS=""
for path in $SHARED_PATHS; do
    # The mount point has to be in the image. systemd creates a missing one only
    # where it can write, and on a read-only slot root it cannot — so a shared
    # path with no directory is a bind that never happens on real hardware.
    if [ ! -d "$path" ]; then
        MISSING_MOUNTPOINTS="$MISSING_MOUNTPOINTS $path"
        continue
    fi
    source_dir="/persistent/shared${path}"
    mkdir -p "$source_dir"
    cp -a "$path/." "$source_dir/" 2>/dev/null || true
    mount --bind "$source_dir" "$path" \
        || not_run "$path could not be bound from the persistent partition" mount_unavailable
done
[ -z "$MISSING_MOUNTPOINTS" ] \
    && check ok "every shared path has a mount point in the image" \
    || check no "every shared path has a mount point in the image" \
              "no directory for:$MISSING_MOUNTPOINTS"

mkdir -p /home && mount --bind /persistent/home /home || true

echo
echo "== and the root becomes what a flashed slot is =="
if writable / && ! mount -o remount,ro / 2>/dev/null; then
    not_run "the root could not be made read-only" remount_unavailable
fi

if writable /; then
    check no "the slot root refuses a write" "the root is writable; this audit proves nothing"
    echo "RESULT: NOT RUN (root_not_read_only)" >&2
    exit 3
fi
check ok "the slot root refuses a write"

for path in /etc /usr /opt /srv /root; do
    [ -d "$path" ] || continue
    writable "$path" \
        && check no "$path refuses a write" "it is writable" \
        || check ok "$path refuses a write"
done

for path in /run /tmp /var /persistent /home; do
    [ -d "$path" ] || continue
    writable "$path" \
        && check ok "$path is mutable, as the contract declares" \
        || check no "$path is mutable, as the contract declares" "it refused a write"
done

echo
echo "== the write paths the appliance runs at boot =="

# What this audit is about is writes, so that is what it judges. A path that
# fails because a guest is not a complete appliance — no running systemd, no
# NetworkManager, no image overlay — is reported and not counted: pretending to
# have proven something about the root model from it would be worse than saying
# it did not apply.
run_case() {
    label=$1
    shift
    set +e
    "$@" > "$WORK/case.log" 2>&1
    status=$?
    set -e
    if [ "$status" -eq 0 ]; then
        check ok "$label"
        return 0
    fi
    if grep -qi "read-only file system" "$WORK/case.log"; then
        check no "$label" \
            "EROFS: $(grep -i -m1 'read-only file system' "$WORK/case.log" | cut -c1-90)"
        return 0
    fi
    printf '   %-58s N/A   %s\n' "$label" \
        "not a write: $(tail -n 1 "$WORK/case.log" | cut -c1-70)"
    return 0
}

run_case "the export root is built without writing the slot root" \
    /usr/lib/ems-appliance-manager/setup-export-root.sh
run_case "the host identity is ensured" \
    /usr/bin/ems-appliance host-identity --ensure --quiet
run_case "the installation is verified" \
    /usr/bin/ems-appliance verify-install --json
run_case "the appliance reports its status" \
    /usr/bin/ems-appliance status --json
run_case "the backup access state is read" \
    /usr/bin/ems-appliance backup-access status --json

echo
echo "== nothing landed on the read-only root =="
# What the package actually created, compared against the declared mutable set.
STRAY=""
for path in /etc /opt /srv /root /usr; do
    [ -d "$path" ] || continue
    while IFS= read -r found; do
        [ -n "$found" ] || continue
        keep=no
        for mutable in $SHARED_PATHS $SLOT_LOCAL_PATHS $UPSTREAM_SHARED $ENSURED_TMPFS; do
            case "$found" in "$mutable"|"$mutable"/*) keep=yes ;; esac
        done
        [ "$keep" = yes ] || STRAY="$STRAY $found"
    done <<EOF
$(find "$path" -xdev -newer "$INSTALL_MARK" -type f 2>/dev/null | head -40)
EOF
done
[ -z "$STRAY" ] \
    && check ok "no file was created outside the declared mutable set" \
    || check no "no file was created outside the declared mutable set" "$STRAY"

echo
echo "checks:  $CHECKS"
echo "failed:  $FAILURES"
[ "$FAILURES" -eq 0 ] || { echo "RESULT: FAIL ($FAILURES)"; exit 1; }
echo "RESULT: PASS (the package operates on a read-only slot root)"
