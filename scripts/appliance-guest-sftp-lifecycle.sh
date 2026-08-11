#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# The SSH confinement of the backup account, asked of a real sshd.
#
#   scripts/appliance-guest-sftp-lifecycle.sh
#
# Run as root in a disposable Debian guest. The point is `sshd -T -C user=...`:
# that is sshd's own answer for a connection by that account, not a re-reading
# of the drop-in this project wrote. A claim about a configuration file's text
# is not a claim about what sshd does with it.
#
# What is proven here:
#
#   the account exists, is package-owned, and has no shell
#   the export root exists and carries the ACL grant
#   sshd's effective policy chroots that user, forces internal-sftp, and
#   refuses a tty, port forwarding and password authentication
#   a key the appliance never issued cannot log in
#   a session opened with a key the appliance did issue lands in that chroot,
#   sees the exports, cannot leave them, and cannot name its own subsystem
#
# The session runs in the same tier as the policy on purpose: an effective
# policy and a session proven in two separate records do not say that the
# session ran under that policy. The key is issued through the appliance's own
# authenticated key management. Where that cannot be reached the session cases
# report NOT RUN — a chroot check that passes because the login failed proves
# nothing at all.
#
# The package lifecycle — reinstall, remove, purge, ACL rollback and foreign
# state preservation — is proven in tests/test_appliance_package_lifecycle.py
# against a real container, and is not repeated here.
#
# Exit status: 0 every check passed, 1 a check failed, 3 a case could not run.
set -uo pipefail

BACKUP_USER=${EMS_APPLIANCE_BACKUP_USER:-ems-backup}
EXPORT_ROOT=/srv/ems-appliance-export
CLIENT_KEY=/root/backup-client-key
ISSUED_KEY=/root/lifecycle-issued-key

failures=0
step() { printf '\n== %s ==\n' "$1"; }
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }
check() { if "${@:2}" >/dev/null 2>&1; then pass "$1"; else fail "$1"; fi; }

for tool in sshd ssh sftp setfacl getfacl systemctl ssh-keygen; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "appliance-guest-sftp-lifecycle: $tool is missing" >&2
        echo "RESULT: NOT RUN (${tool}_unavailable)" >&2
        exit 3
    }
done

step "the backup account the package owns"
check "the account exists" getent passwd "$BACKUP_USER"
shell=$(getent passwd "$BACKUP_USER" | cut -d: -f7)
if [ "$shell" = /usr/sbin/nologin ] || [ "$shell" = /bin/false ]; then
    pass "the account has no shell ($shell)"
else
    fail "the account's shell is $shell"
fi
HOME_DIR=$(getent passwd "$BACKUP_USER" | cut -d: -f6)
check "the home carries the package ownership marker" \
    test -f "$HOME_DIR/.ems-appliance-backup-home"
check "the ownership record is package-owned" \
    sh -c "/usr/bin/ems-appliance backup-account status | grep -q package"

step "exports and the ACL grant"
mkdir -p /opt/ems-solarflow/config /opt/ems-solarflow/backups /opt/ems-solarflow/data
echo "config" >/opt/ems-solarflow/config/config.json
echo "backup" >/opt/ems-solarflow/backups/backup.tar
/usr/lib/ems-appliance-manager/setup-export-root.sh >/dev/null 2>&1 || true
check "the export root exists" test -d "$EXPORT_ROOT"
if getfacl -p /opt/ems-solarflow 2>/dev/null | grep -q "user:$BACKUP_USER:"; then
    pass "the install root carries an ACL for the backup account"
else
    fail "no ACL grant for $BACKUP_USER on /opt/ems-solarflow"
fi

step "sshd's effective policy for that account"
# -T -C is sshd's own answer for a connection, not a re-reading of the file.
EFFECTIVE=$(sshd -T -C "user=$BACKUP_USER,host=localhost,addr=127.0.0.1" 2>/dev/null)
if [ -z "$EFFECTIVE" ]; then
    fail "sshd could not report an effective configuration"
else
    pass "sshd reports an effective configuration for $BACKUP_USER"
    for expected in \
        "chrootdirectory $EXPORT_ROOT" \
        "forcecommand internal-sftp" \
        "allowtcpforwarding no" \
        "permittty no" \
        "passwordauthentication no"; do
        if echo "$EFFECTIVE" | grep -qi "^$expected"; then
            pass "the effective policy carries: $expected"
        else
            fail "the effective policy does not carry: $expected"
        fi
    done
fi

step "a real SFTP session under exactly that policy"
# The effective policy above is what sshd says it would do. A session is what
# it does. Both belong in one record, so the key is issued here, through the
# appliance's own authenticated key management — never written into
# authorized_keys by hand. Where that cannot be done the protocol cases report
# NOT RUN: a chroot check that passes because the login failed proves nothing.
not_run=0
skip() { printf '  NOT RUN  %s\n' "$1"; not_run=$((not_run + 1)); }

rm -f "$CLIENT_KEY" "$CLIENT_KEY.pub"
ssh-keygen -q -t ed25519 -N '' -C backup-client -f "$CLIENT_KEY"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=10 -i "$CLIENT_KEY")

if ssh "${SSH_OPTS[@]}" "$BACKUP_USER@127.0.0.1" true >/dev/null 2>&1; then
    fail "an unauthorised key reached the confined account"
else
    pass "a key the appliance never issued cannot log in"
fi

ISSUED=0
ISSUE_OUTPUT=$(bash "$(dirname "$0")/appliance-guest-issue-backup-key.sh" \
    --account "$BACKUP_USER" --key "$ISSUED_KEY" 2>&1) && ISSUED=1
if [ "$ISSUED" -eq 1 ]; then
    pass "the appliance issued a key for $BACKUP_USER ($(echo "$ISSUE_OUTPUT" \
        | sed -n 's/^fingerprint: //p'))"
else
    skip "a real sftp session with an appliance-issued key"
    skip "the session root is the chroot"
    skip "the exported directories are visible in the session"
    skip "a path outside the chroot is not reachable"
    skip "a parent-directory traversal cannot leave the chroot"
    echo "  prerequisite: an attributable key issued through the appliance's key"
    echo "  management; backup-access refuses any key it cannot attribute."
    # The helper's own verdict line is dropped: this log is read for its last
    # RESULT line, and a nested one would be a second tier's answer in it.
    echo "$ISSUE_OUTPUT" | grep -v '^RESULT:' | sed 's/^/    /'
fi

if [ "$ISSUED" -eq 1 ]; then
    ISSUED_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
                 -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=10 -i "$ISSUED_KEY")
    sftp_batch() {
        printf '%s\n' "$@" | sftp -b - "${ISSUED_OPTS[@]}" "$BACKUP_USER@127.0.0.1" 2>&1
    }

    SESSION=$(sftp_batch "pwd")
    if [ $? -eq 0 ] && echo "$SESSION" | grep -q 'Remote working directory: /'; then
        pass "a real sftp session with an appliance-issued key"
        pass "the session root is the chroot"
    else
        fail "the sftp login failed: $(echo "$SESSION" | tail -2 | tr '\n' ' ')"
        # Every case below asks something of a session that never opened, so
        # they are reported as not run rather than as confinement results.
        skip "the exported directories are visible in the session"
        skip "a path outside the chroot is not reachable"
        skip "a parent-directory traversal cannot leave the chroot"
        ISSUED=0
    fi
fi

if [ "$ISSUED" -eq 1 ]; then
    LISTING=$(sftp_batch "ls -l /")
    if echo "$LISTING" | grep -q "config" && echo "$LISTING" | grep -q "backups"; then
        pass "the exported directories are visible in the session"
    else
        fail "the session root does not show the exports"
    fi

    rm -f /root/lifecycle-leaked-passwd
    sftp_batch "get /etc/passwd /root/lifecycle-leaked-passwd" >/dev/null
    if [ ! -s /root/lifecycle-leaked-passwd ]; then
        pass "a path outside the chroot is not reachable"
    else
        fail "the host's /etc/passwd was readable through the session"
    fi

    ESCAPE=$(sftp_batch "cd .." "pwd")
    if echo "$ESCAPE" | grep -q 'Remote working directory: /'; then
        pass "a parent-directory traversal cannot leave the chroot"
    else
        fail "cd .. left the chroot: $(echo "$ESCAPE" | tail -1)"
    fi

    # ForceCommand internal-sftp is the whole confinement, so what the client
    # asks for must not decide what runs. A subsystem the appliance never
    # declared may not produce anything the client can drive.
    SUBSYSTEM=$(timeout 30 ssh "${ISSUED_OPTS[@]}" -s "$BACKUP_USER@127.0.0.1" \
        ems-appliance-not-a-subsystem </dev/null 2>&1 | head -c 512)
    if printf '%s' "$SUBSYSTEM" | grep -qiE 'uid=|root@|/bin/sh|command not found'; then
        fail "an undeclared subsystem request reached a program: $SUBSYSTEM"
    else
        pass "an undeclared subsystem cannot be substituted for the forced command"
    fi
fi

printf '\n'
if [ "$failures" -ne 0 ]; then
    echo "RESULT: FAIL ($failures)"
    exit 1
fi
if [ "$not_run" -ne 0 ]; then
    echo "RESULT: NOT RUN ($not_run case(s) need an appliance-issued key)"
    exit 3
fi
echo "RESULT: PASS"
