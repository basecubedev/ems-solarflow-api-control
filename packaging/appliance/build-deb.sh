#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build ems-appliance-manager_<version>_arm64.deb plus its SHA-256 checksum.
#
# Usage:
#   packaging/appliance/build-deb.sh [--output DIR] [--arch arm64]
#
# The package installs the appliance Python package, the CLI, both systemd
# units, the tmpfiles and logrotate configuration and the host configuration
# files. It never moves or rewrites an existing EMS installation.
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PACKAGING="$ROOT/packaging/appliance"
OUTPUT="$ROOT/dist"
ARCH=arm64

while [ $# -gt 0 ]; do
    case "$1" in
        --output) OUTPUT=$2; shift 2 ;;
        --arch) ARCH=$2; shift 2 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

VERSION=$(sed -n 's/^APPLIANCE_VERSION = "\(.*\)"$/\1/p' "$ROOT/appliance/version.py")
[ -n "$VERSION" ] || { echo "cannot read APPLIANCE_VERSION" >&2; exit 1; }

NAME="ems-appliance-manager_${VERSION}_${ARCH}"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

install -d "$STAGE/DEBIAN"
install -d "$STAGE/usr/lib/ems-appliance-manager/appliance"
install -d "$STAGE/usr/lib/systemd/system"
install -d "$STAGE/usr/lib/tmpfiles.d"
install -d "$STAGE/etc/logrotate.d"
install -d "$STAGE/etc/ems-appliance-manager"
install -d "$STAGE/etc/ssh/sshd_config.d"
install -d "$STAGE/usr/lib/ems-appliance-manager"
install -d "$STAGE/usr/bin"
install -d "$STAGE/usr/share/doc/ems-appliance-manager"

# Python package (no build artefacts, no tests).
for file in "$ROOT"/appliance/*.py; do
    install -m 0644 "$file" "$STAGE/usr/lib/ems-appliance-manager/appliance/"
done
install -d "$STAGE/usr/lib/ems-appliance-manager/appliance/static"
for file in "$ROOT"/appliance/static/*; do
    install -m 0644 "$file" "$STAGE/usr/lib/ems-appliance-manager/appliance/static/"
done

install -m 0755 "$PACKAGING/bin/ems-appliance" "$STAGE/usr/bin/ems-appliance"
install -m 0755 "$PACKAGING/bin/setup-export-root.sh" \
        "$STAGE/usr/lib/ems-appliance-manager/setup-export-root.sh"
install -m 0755 "$PACKAGING/bin/backup-account.sh" \
        "$STAGE/usr/lib/ems-appliance-manager/backup-account.sh"
# The project's own Admin installer, unmodified. It is what writes the Admin
# compose and environment files on a host that has none, so it is the appliance's
# bootstrap too rather than a second installer that would drift from it.
install -m 0755 "$ROOT/deploy/admin/install-admin-console.sh" \
        "$STAGE/usr/lib/ems-appliance-manager/install-admin-console.sh"
install -m 0644 "$PACKAGING/systemd/ems-appliance-agent.service" "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$PACKAGING/systemd/ems-appliance-web.service" "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$PACKAGING/systemd/ems-appliance-export.service" "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$PACKAGING/systemd/ems-appliance-export.path" "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$PACKAGING/systemd/ems-appliance-backup-access-disable.service" \
        "$STAGE/usr/lib/systemd/system/"
# The A/B units ship in every slot of an image-managed appliance and are inert
# on a single-slot host; both carry ConditionPathExists on the layout manifest.
install -m 0644 "$PACKAGING/systemd/ems-appliance-persistence.service" \
        "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$PACKAGING/systemd/ems-appliance-host-identity.service" \
        "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$PACKAGING/systemd/ems-appliance-ab-health.service" \
        "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$PACKAGING/systemd/ems-appliance-slot-bootstrap.service" \
        "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$PACKAGING/systemd/ems-appliance-grow-persistent.service" \
        "$STAGE/usr/lib/systemd/system/"
install -m 0755 "$PACKAGING/bin/grow-persistent.sh" \
        "$STAGE/usr/lib/ems-appliance-manager/grow-persistent.sh"
install -m 0644 "$PACKAGING/systemd/ems-appliance-grow-root.service" \
        "$STAGE/usr/lib/systemd/system/"
install -m 0755 "$PACKAGING/bin/grow-root.sh" \
        "$STAGE/usr/lib/ems-appliance-manager/grow-root.sh"
install -m 0644 "$PACKAGING/tmpfiles/ems-appliance-manager.conf" "$STAGE/usr/lib/tmpfiles.d/"
install -m 0644 "$PACKAGING/logrotate/ems-appliance-manager" "$STAGE/etc/logrotate.d/"
install -m 0644 "$PACKAGING/config/appliance.conf" "$STAGE/etc/ems-appliance-manager/"
install -m 0644 "$PACKAGING/config/allowed-images.conf" "$STAGE/etc/ems-appliance-manager/"
# Deliberately not a conffile: it is the trust anchor for OS updates, not a
# setting, and a local edit must not survive an upgrade that rotates the key.
install -m 0644 "$PACKAGING/config/os-release-keyring.gpg" "$STAGE/etc/ems-appliance-manager/"

for document in architecture installation admin-recovery os-updates ab-os-updates \
                ab-hardware-validation ab-persistence-contract ssh-backup-access \
                network-recovery security-model troubleshooting; do
    [ -f "$ROOT/docs/appliance/$document.md" ] && \
        install -m 0644 "$ROOT/docs/appliance/$document.md" \
                "$STAGE/usr/share/doc/ems-appliance-manager/"
done
install -m 0644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/ems-appliance-manager/copyright"

sed "s/^Version: .*/Version: ${VERSION}/; s/^Architecture: .*/Architecture: ${ARCH}/" \
    "$PACKAGING/debian/control" > "$STAGE/DEBIAN/control"
install -m 0644 "$PACKAGING/debian/conffiles" "$STAGE/DEBIAN/conffiles"
install -m 0755 "$PACKAGING/debian/postinst" "$STAGE/DEBIAN/postinst"
install -m 0755 "$PACKAGING/debian/prerm" "$STAGE/DEBIAN/prerm"
install -m 0755 "$PACKAGING/debian/postrm" "$STAGE/DEBIAN/postrm"

mkdir -p "$OUTPUT"
if command -v dpkg-deb >/dev/null 2>&1; then
    dpkg-deb --root-owner-group --build "$STAGE" "$OUTPUT/$NAME.deb" >/dev/null
else
    echo "dpkg-deb is not installed; staging a tarball instead" >&2
    tar -C "$STAGE" -czf "$OUTPUT/$NAME.tar.gz" .
    NAME="$NAME.tar"
fi

ARTIFACT=$(ls "$OUTPUT/$NAME".deb 2>/dev/null || ls "$OUTPUT/$NAME".gz)
( cd "$OUTPUT" && sha256sum "$(basename "$ARTIFACT")" > "$(basename "$ARTIFACT").sha256" )

echo "built $ARTIFACT"
echo "checksum $ARTIFACT.sha256"
echo
echo "Sign the artefact before publishing, for example:"
echo "  gpg --armor --detach-sign $ARTIFACT"
