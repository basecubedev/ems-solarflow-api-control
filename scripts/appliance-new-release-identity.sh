#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Create the release identity the Appliance Manager is signed with.
#
#   scripts/appliance-new-release-identity.sh --uid "Name <mail>"
#                                             --secret-out PATH
#                                             [--force]
#
# Three artefacts have to agree or nothing installs, and each is easy to get
# wrong by hand:
#
#   packaging/appliance/config/release-keyring.gpg  the public half, shipped
#   packaging/appliance/config/appliance.conf       the PRIMARY's fingerprint
#   the GitHub environment secret + variable        the SUBKEY, and its own
#                                                   fingerprint
#
# The two fingerprints are different keys and naming the wrong one fails in
# opposite directions: gpg reports the primary for a subkey signature, so the
# appliance pins the primary; the runner is given only the subkey's secret, so
# signing with the primary's fingerprint fails with "no secret key".
#
# The primary is certify-only, so it cannot sign even by accident, and the
# subkey signs only. No passphrase: the subkey has to be usable unattended by a
# workflow, and a passphrase stored beside the key it protects is not a
# boundary -- the GitHub environment, with required reviewers, is. Keep the
# primary's secret somewhere offline once this has run.
#
# Exit status: 0 the identity was created, 1 it failed, 2 the command line is
# wrong, 3 gpg is not installed.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
CONFIG="$ROOT/packaging/appliance/config"
KEYRING="$CONFIG/release-keyring.gpg"
CONF="$CONFIG/appliance.conf"
UID_TEXT=""
SECRET_OUT=""
FORCE=no

usage() {
    sed -n '3,30p' "$0"
}

fail() {
    echo "appliance-new-release-identity: $1" >&2
    exit "${2:-1}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --uid) UID_TEXT=${2:?--uid needs a user id}; shift 2 ;;
        --uid=*) UID_TEXT=${1#*=}; shift ;;
        --secret-out) SECRET_OUT=${2:?--secret-out needs a path}; shift 2 ;;
        --secret-out=*) SECRET_OUT=${1#*=}; shift ;;
        --force) FORCE=yes; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$UID_TEXT" ] || { echo "--uid is required" >&2; usage >&2; exit 2; }
[ -n "$SECRET_OUT" ] || { echo "--secret-out is required" >&2; usage >&2; exit 2; }
command -v gpg >/dev/null 2>&1 || fail "gpg is not installed" 3

# The CI copy of a signing key must not be able to end up in a commit, and a
# path inside the working tree is the one way that happens by accident.
case $(cd "$(dirname "$SECRET_OUT")" 2>/dev/null && pwd)/ in
    "$ROOT"/*) fail "--secret-out is inside the repository; put it somewhere it cannot be committed" ;;
esac

if [ -s "$KEYRING" ] && [ "$FORCE" != yes ]; then
    fail "$KEYRING already holds an identity; --force to replace it (every appliance
  flashed with the old one will refuse packages signed by the new one)"
fi

PRIMARY_UID=$UID_TEXT
echo "== creating a certify-only primary =="
gpg --batch --quiet --passphrase '' \
    --quick-generate-key "$PRIMARY_UID" ed25519 cert never \
    || fail "the primary could not be created"
PRIMARY=$(gpg --batch --with-colons --list-keys "$PRIMARY_UID" \
    | awk -F: '/^fpr:/{print $10; exit}')
[ -n "$PRIMARY" ] || fail "the primary was created but cannot be found again"

echo "== adding a sign-only subkey =="
gpg --batch --quiet --passphrase '' \
    --quick-add-key "$PRIMARY" ed25519 sign never \
    || fail "the signing subkey could not be added"
SUBKEY=$(gpg --batch --with-colons --list-keys "$PRIMARY" \
    | awk -F: '$1=="sub"{found=1; next} found && $1=="fpr"{print $10; exit}')
[ -n "$SUBKEY" ] || fail "the subkey was created but cannot be found again"

echo "== writing the public half the appliance ships =="
gpg --batch --export "$PRIMARY" > "$KEYRING" || fail "the keyring could not be written"
[ -s "$KEYRING" ] || fail "the exported keyring is empty"

echo "== pinning the primary in the shipped configuration =="
# Written here rather than by hand so the pin and the keyring cannot drift; a
# test reads the keyring with gpg and fails if they do.
tmp=$(mktemp)
sed "s|^release_fingerprints = .*|release_fingerprints = $PRIMARY|" "$CONF" > "$tmp"
grep -q "^release_fingerprints = $PRIMARY\$" "$tmp" \
    || { rm -f "$tmp"; fail "$CONF has no release_fingerprints line to pin"; }
cat "$tmp" > "$CONF"
rm -f "$tmp"

echo "== exporting the subkey the workflow signs with =="
# The '!' keeps the primary at home. Without it gpg exports that too, and the
# primary is the whole reason for having a subkey.
( umask 0077; gpg --armor --export-secret-subkeys "${SUBKEY}!" | base64 -w0 > "$SECRET_OUT" ) \
    || fail "the subkey could not be exported"
[ -s "$SECRET_OUT" ] || fail "the exported secret is empty"

cat <<REPORT

RESULT: PASS (release identity created)

  primary   $PRIMARY
  subkey    $SUBKEY

Written:
  $KEYRING
  $CONF                (release_fingerprints = the primary)
  $SECRET_OUT          (mode 0600, the subkey, base64)

Next, and only you can do these:

  1. GitHub -> Settings -> Environments -> new environment
     "appliance-manager-signing", with required reviewers.
       secret    APPLIANCE_MANAGER_SIGNING_KEY          = the file above
       variable  APPLIANCE_MANAGER_SIGNING_FINGERPRINT  = $SUBKEY

     The variable holds the SUBKEY. The appliance pins the PRIMARY. They are
     different keys, and swapping them breaks signing in one direction and
     every future rotation in the other.

  2. Delete $SECRET_OUT once GitHub has it.

  3. Back the primary's secret up offline, then consider removing it from this
     machine's keyring. Only the subkey is needed from here on, and it is
     already in GitHub.

  4. Commit the keyring and the configuration together. They are checked
     against each other by tests/test_appliance_project_identity.py.
REPORT
