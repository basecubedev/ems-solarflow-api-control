#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build ems-appliance-manager_<version>_arm64.deb plus its SHA-256 checksum.
#
# Usage:
#   packaging/appliance/build-deb.sh [--output DIR] [--arch arm64]
#                                    [--allow-tarball]
#
# The artefact is reproducible: two builds of one commit produce identical
# bytes, so anyone can re-derive the package from the tag and compare digests
# rather than trusting the machine that built it. That property is what lets
# this run on a builder nobody attested. It rests on SOURCE_DATE_EPOCH and on a
# pinned compressor, and both are refused rather than defaulted.
#
# The package installs the appliance Python package, the CLI, both systemd
# units, the tmpfiles and logrotate configuration and the host configuration
# files. It never moves or rewrites an existing EMS installation.
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PACKAGING="$ROOT/packaging/appliance"
OUTPUT="$ROOT/dist"
ARCH=arm64
# A tarball is not a release. Without dpkg-deb this script used to print
# "built ...", a checksum and "sign the artefact before publishing" for
# something no appliance can install -- a thing shaped like a release with no
# way to notice. Asking for it is fine; getting it by accident is not.
ALLOW_TARBALL=no

while [ $# -gt 0 ]; do
    case "$1" in
        --output) OUTPUT=$2; shift 2 ;;
        --arch) ARCH=$2; shift 2 ;;
        --allow-tarball) ALLOW_TARBALL=yes; shift ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

VERSION=$(sed -n 's/^APPLIANCE_VERSION = "\(.*\)"$/\1/p' "$ROOT/appliance/version.py")
[ -n "$VERSION" ] || { echo "cannot read APPLIANCE_VERSION" >&2; exit 1; }

# Every timestamp dpkg-deb writes comes from here, so this is what makes two
# builds of one commit identical. Inherited when the caller pinned it, otherwise
# taken from the commit being built. Refused when neither yields a number: an
# empty value is silently ignored by dpkg-deb, which would produce a
# non-reproducible artefact that a signing step would sign without complaint.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    SOURCE_DATE_EPOCH=$(git -C "$ROOT" log -1 --pretty=%ct 2>/dev/null || true)
fi
case "${SOURCE_DATE_EPOCH:-}" in
    ''|*[!0-9]*)
        echo "build-deb: SOURCE_DATE_EPOCH is unset and no commit date could be read" >&2
        echo "build-deb: set it to the release commit's date to build reproducibly" >&2
        exit 1
        ;;
esac
export SOURCE_DATE_EPOCH

DPKG_DEB_VERSION=$(dpkg-deb --version 2>/dev/null | head -1 || echo "unavailable")

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
install -d "$STAGE/usr/share/ems-appliance-manager"

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
install -m 0755 "$PACKAGING/bin/rescue-account.sh" \
        "$STAGE/usr/lib/ems-appliance-manager/rescue-account.sh"
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
install -m 0644 "$PACKAGING/systemd/ems-appliance-config-seed.service" \
        "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$PACKAGING/systemd/ems-appliance-manager-install.service" \
        "$STAGE/usr/lib/systemd/system/"
install -m 0755 "$PACKAGING/bin/install-manager.sh" \
        "$STAGE/usr/lib/ems-appliance-manager/install-manager.sh"
# The deadline that reverts an install nobody confirmed. Neither unit is enabled
# by the package: arming copies the reverter out of this tree first, so the
# script that judges an install is the one shipped by the package it replaces.
install -m 0644 "$PACKAGING/systemd/ems-appliance-manager-verify.service" \
        "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$PACKAGING/systemd/ems-appliance-manager-verify.timer" \
        "$STAGE/usr/lib/systemd/system/"
install -m 0755 "$PACKAGING/bin/verify-manager.sh" \
        "$STAGE/usr/lib/ems-appliance-manager/verify-manager.sh"
install -m 0644 "$PACKAGING/systemd/ems-appliance-grow-root.service" \
        "$STAGE/usr/lib/systemd/system/"
install -m 0755 "$PACKAGING/bin/grow-root.sh" \
        "$STAGE/usr/lib/ems-appliance-manager/grow-root.sh"
install -m 0644 "$PACKAGING/tmpfiles/ems-appliance-manager.conf" "$STAGE/usr/lib/tmpfiles.d/"
install -m 0644 "$PACKAGING/logrotate/ems-appliance-manager" "$STAGE/etc/logrotate.d/"
# Templates, not conffiles, and deliberately not under /etc: a packaged copy
# under /etc/ems-appliance-manager would put an operator edit and a package file
# at the same path, and dpkg is entitled to the second.
# ems-appliance-config-seed.service creates what is missing from these, once.
install -m 0644 "$PACKAGING/config/appliance.conf" "$STAGE/usr/share/ems-appliance-manager/"
install -m 0644 "$PACKAGING/config/allowed-images.conf" "$STAGE/usr/share/ems-appliance-manager/"
# The rescue password, as the one hash the postinst sets and the console
# compares against. Declaring it twice would let "still the default" drift
# away from what was actually written.
install -m 0644 "$PACKAGING/config/rescue-password.hash" \
        "$STAGE/usr/share/ems-appliance-manager/"
# Deliberately not a conffile: it is the trust anchor for every signed artifact
# this appliance installs, not a setting, and a local edit must not survive an
# upgrade that rotates the key.
install -m 0644 "$PACKAGING/config/release-keyring.gpg" "$STAGE/etc/ems-appliance-manager/"

# Named, and required. The previous form guarded each install with
# `[ -f ... ] &&`, which does not trip `set -e` because the test is not the last
# command of the AND-list -- so a renamed document silently vanished from a
# package that still reported success, taking its Documentation= URI with it.
for document in architecture installation admin-recovery console-recovery os-updates \
                hardware-validation ssh-backup-access \
                network-recovery security-model troubleshooting; do
    [ -f "$ROOT/docs/appliance/$document.md" ] || {
        echo "build-deb: docs/appliance/$document.md is missing" >&2
        exit 1
    }
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
    # -Zxz -z6 pinned rather than left to the local dpkg-deb's default: the
    # compressor and its level are inputs to the bytes, so an unpinned one makes
    # the digest depend on which machine ran the build, which is the property
    # this whole path is built on.
    dpkg-deb -Zxz -z6 --root-owner-group --build "$STAGE" "$OUTPUT/$NAME.deb" >/dev/null
elif [ "$ALLOW_TARBALL" = yes ]; then
    echo "dpkg-deb is not installed; staging a tarball as asked" >&2
    tar -C "$STAGE" -czf "$OUTPUT/$NAME.tar.gz" .
    NAME="$NAME.tar"
else
    echo "build-deb: dpkg-deb is not installed, so no package can be built" >&2
    echo "build-deb: pass --allow-tarball to stage an uninstallable tree instead" >&2
    exit 3
fi

ARTIFACT=$(ls "$OUTPUT/$NAME".deb 2>/dev/null || ls "$OUTPUT/$NAME".gz)
( cd "$OUTPUT" && sha256sum "$(basename "$ARTIFACT")" > "$(basename "$ARTIFACT").sha256" )

# What the digest above depends on, recorded beside it: a rebuild that differs
# should be diagnosable without guessing which of the two machines moved.
cat > "$OUTPUT/$NAME.build.json" <<EOF
{
  "artifact": "$(basename "$ARTIFACT")",
  "version": "$VERSION",
  "architecture": "$ARCH",
  "source_date_epoch": $SOURCE_DATE_EPOCH,
  "dpkg_deb": "$DPKG_DEB_VERSION",
  "compression": "xz -6"
}
EOF

echo "built $ARTIFACT"
echo "checksum $ARTIFACT.sha256"
echo
echo "Sign the artefact before publishing, for example:"
echo "  gpg --armor --detach-sign $ARTIFACT"
