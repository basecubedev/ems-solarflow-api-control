#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# A real SFTP session, with a key the appliance itself issued.
#
#   scripts/appliance-guest-sftp-session.sh
#
# Run as root in a disposable Debian guest that has the package installed and a
# running systemd.
#
# The confinement tier beside this asks `sshd -T -C user=...` what sshd would do
# for the backup account. That is sshd's own answer and it is worth having, but
# it is not a session: it cannot show that a login succeeds, that the session
# root really is the chroot, that a file under an exported directory can be
# fetched, or that a path outside the chroot is unreachable. Those cases used to
# report NOT RUN, because the appliance refuses any key it cannot attribute and
# nothing here issued an attributable one.
#
# So the key is issued the way an operator's is: through the Appliance Manager's
# authenticated HTTP API, as a planned operation that is confirmed and executed
# by the agent. Nothing is written into authorized_keys by hand — a session that
# only works because the test put the key there proves nothing about the
# appliance.
#
# Exit status: 0 every check passed, 1 a check failed, 3 a prerequisite is
# missing and the session could not be attempted.
set -uo pipefail

BACKUP_USER=${EMS_APPLIANCE_BACKUP_USER:-ems-backup}
CLIENT_KEY=/root/appliance-issued-key
PASSWORD=${EMS_APPLIANCE_TEST_PASSWORD:-appliance-session-test-password}

failures=0
step() { printf '\n== %s ==\n' "$1"; }
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }

not_run() {
    echo "appliance-guest-sftp-session: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

for tool in sshd ssh sftp ssh-keygen curl jq systemctl; do
    command -v "$tool" >/dev/null 2>&1 || not_run "$tool is missing" "${tool}_unavailable"
done
command -v ems-appliance >/dev/null 2>&1 || not_run "the package is not installed" package_missing

step "a key issued through the appliance's key management"
ISSUE=$(bash "$(dirname "$0")/appliance-guest-issue-backup-key.sh" \
    --account "$BACKUP_USER" --key "$CLIENT_KEY" --password "$PASSWORD" 2>&1) \
    || not_run "the appliance issued no key: $(printf '%s' "$ISSUE" | tail -2 | tr '\n' ' ')" \
               key_issue_refused
FINGERPRINT=$(printf '%s\n' "$ISSUE" | sed -n 's/^fingerprint: //p')
if [ "$(printf '%s\n' "$ISSUE" | sed -n 's/^listed: //p')" = true ]; then
    pass "the appliance reports the key it issued ($FINGERPRINT)"
else
    fail "the issued key is not in the appliance's own key list"
fi
if [ "$(printf '%s\n' "$ISSUE" | sed -n 's/^activation_state: //p')" = active ]; then
    pass "backup access is active"
else
    fail "backup access is not active"
    # This log is read for its last RESULT line; a nested one would be a
    # second tier's answer inside this tier's record.
    printf '%s\n' "$ISSUE" | grep -v '^RESULT:' | sed 's/^/    /'
fi

SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=10 -i "$CLIENT_KEY")

sftp_batch() {
    printf '%s\n' "$@" | sftp -b - "${SSH_OPTS[@]}" "$BACKUP_USER@127.0.0.1" 2>&1
}

step "a real SFTP session"
SESSION=$(sftp_batch "pwd")
if [ $? -eq 0 ] && echo "$SESSION" | grep -q 'Remote working directory: /'; then
    pass "the appliance-issued key completes an sftp login"
else
    fail "the sftp login failed: $(echo "$SESSION" | tail -2 | tr '\n' ' ')"
    echo "$SESSION" | sed 's/^/    /'
    echo "  -- what sshd said --"
    journalctl -u ssh.service -n 40 --no-pager 2>&1 | tail -25 | sed 's/^/    /'
    echo "  -- the account and its keys --"
    HOME_DIR=$(getent passwd "$BACKUP_USER" | cut -d: -f6)
    ls -lad "$HOME_DIR" "$HOME_DIR/.ssh" "$HOME_DIR/.ssh/authorized_keys" 2>&1 | sed 's/^/    /'
    getent shadow "$BACKUP_USER" 2>/dev/null | cut -d: -f1,2 | sed 's/^/    shadow: /'
    sshd -T -C "user=$BACKUP_USER,host=localhost,addr=127.0.0.1" 2>&1 \
        | grep -Ei "authorizedkeysfile|pubkey|usepam|chroot|forcecommand|permitrootlogin" \
        | sed 's/^/    /'
    echo "    client key: $(ssh-keygen -lf "$CLIENT_KEY.pub" | awk '{print $2}')"
    while read -r line; do
        [ -n "$line" ] || continue
        printf '    authorized: %s\n' \
            "$(printf '%s\n' "$line" | ssh-keygen -lf - 2>/dev/null | awk '{print $2}')"
    done <"$HOME_DIR/.ssh/authorized_keys"
    echo "    chroot root: $(stat -c '%U:%G %a' /srv/ems-appliance-export 2>&1)"
    printf '\nRESULT: FAIL (%s)\n' "$((failures))"
    exit 1
fi

LISTING=$(sftp_batch "ls -l /")
if echo "$LISTING" | grep -q "config" && echo "$LISTING" | grep -q "backups"; then
    pass "the exported directories are visible in the session"
else
    fail "the session root does not show the exports"
fi

echo "a fetched backup" >/opt/ems-solarflow/backups/known-file.txt
/usr/lib/ems-appliance-manager/setup-export-root.sh >/dev/null 2>&1 || true
rm -f /root/fetched.txt
FETCH=$(sftp_batch "get /backups/known-file.txt /root/fetched.txt")
if [ -s /root/fetched.txt ] && grep -q "a fetched backup" /root/fetched.txt; then
    pass "a known file can be fetched out of an exported directory"
else
    fail "the known file could not be fetched: $(echo "$FETCH" | tail -1)"
fi

step "the chroot really is a boundary"
ESCAPE=$(sftp_batch "cd .." "pwd")
if echo "$ESCAPE" | grep -q 'Remote working directory: /'; then
    pass "a parent-directory traversal cannot leave the chroot"
else
    fail "cd .. left the chroot: $(echo "$ESCAPE" | tail -1)"
fi

rm -f /root/leaked-passwd
sftp_batch "get /etc/passwd /root/leaked-passwd" >/dev/null
if [ ! -s /root/leaked-passwd ]; then
    pass "a path outside the chroot is not reachable"
else
    fail "the host's /etc/passwd was readable through the session"
fi

step "the session can read and nothing else"
# The forced command carries -P with the write operations removed, and the
# export tree is bind-mounted read-only under the chroot. Both are asked here:
# a refusal is only worth having if the tree is still untouched afterwards.
refused() {
    description=$1
    shift
    output=$(sftp_batch "$@")
    if [ $? -ne 0 ]; then
        pass "$description is refused"
    else
        fail "$description was allowed: $(printf '%s' "$output" | tail -1)"
    fi
}

echo "an upload the appliance must refuse" >/root/upload-attempt.txt
refused "writing a new file"        "put /root/upload-attempt.txt /backups/uploaded.txt"
refused "overwriting an export"     "put /root/upload-attempt.txt /backups/known-file.txt"
refused "renaming a file"           "rename /backups/known-file.txt /backups/moved.txt"
refused "creating a directory"      "mkdir /backups/created-by-client"
refused "removing a directory"      "rmdir /config"
refused "removing a file"           "rm /backups/known-file.txt"
refused "changing a mode"           "chmod 777 /backups/known-file.txt"
refused "creating a symlink"        "ln -s /etc/passwd /backups/escape"

AFTER=$(sftp_batch "ls /backups")
if printf '%s' "$AFTER" | grep -q "known-file.txt" \
   && ! printf '%s' "$AFTER" | grep -qE "uploaded.txt|moved.txt|created-by-client|escape"; then
    pass "the exported tree is exactly as it was before the attempts"
else
    fail "the exported tree changed: $(printf '%s' "$AFTER" | tr '\n' ' ')"
fi
if grep -q "a fetched backup" /opt/ems-solarflow/backups/known-file.txt 2>/dev/null; then
    pass "the file behind the export is unchanged on the host"
else
    fail "the host-side file behind the export was modified"
fi

step "the session is sftp and nothing else"
if ssh "${SSH_OPTS[@]}" "$BACKUP_USER@127.0.0.1" 'id' >/dev/null 2>&1; then
    fail "the confined account executed a shell command"
else
    pass "shell and command execution are refused"
fi

# -L is asked differently from -R and -D. The local listener belongs to the
# client, so it appears whatever the server allows and connecting to it proves
# nothing; ExitOnForwardFailure does not cover it either. What decides is
# whether the server opens the channel, so the forward is used: a granted one
# would carry sshd's banner back from port 22, a refused one carries nothing.
ssh "${SSH_OPTS[@]}" -N -L 127.0.0.1:18022:127.0.0.1:22 \
    "$BACKUP_USER@127.0.0.1" >/dev/null 2>&1 &
forward_pid=$!
sleep 3
BANNER=$(timeout 8 bash -c '
    exec 3<>/dev/tcp/127.0.0.1/18022 2>/dev/null || exit 1
    head -c 4 <&3
' 2>/dev/null)
if [ "$BANNER" = "SSH-" ]; then
    fail "local port forwarding carried a connection to the host's sshd"
else
    pass "local port forwarding (-L) carries nothing"
fi
kill "$forward_pid" 2>/dev/null
wait "$forward_pid" 2>/dev/null

for flag in "-R 127.0.0.1:18023:127.0.0.1:22" "-D 127.0.0.1:18024"; do
    # shellcheck disable=SC2086
    if timeout 15 ssh "${SSH_OPTS[@]}" -N -o ExitOnForwardFailure=yes $flag \
            "$BACKUP_USER@127.0.0.1" >/dev/null 2>&1; then
        fail "ssh ${flag%% *} was granted"
    else
        pass "ssh ${flag%% *} is refused"
    fi
done

AGENT=$(ssh "${SSH_OPTS[@]}" -A "$BACKUP_USER@127.0.0.1" 'echo $SSH_AUTH_SOCK' 2>&1)
if [ -n "$(echo "$AGENT" | grep -v '^$' | grep '^/')" ]; then
    fail "agent forwarding produced a socket in the session"
else
    pass "agent forwarding (-A) grants no session"
fi

step "a key the appliance never issued"
rm -f /root/stranger-key /root/stranger-key.pub
ssh-keygen -q -t ed25519 -N '' -C stranger -f /root/stranger-key
if sftp -b /dev/null -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o BatchMode=yes -o ConnectTimeout=10 -i /root/stranger-key \
        "$BACKUP_USER@127.0.0.1" >/dev/null 2>&1; then
    fail "an unauthorised key reached the confined account"
else
    pass "a key the appliance never issued cannot log in"
fi

step "an appliance upgraded from the release that could not authenticate"
# The defect shipped as a root-owned 0700 key directory. An upgrade has to
# repair it, because the account can never fix it itself and a reinstall of the
# same package used to skip the hardening for an account it already owned.
HOME_DIR=$(getent passwd "$BACKUP_USER" | cut -d: -f6)
chown root:root "$HOME_DIR/.ssh"
chmod 0700 "$HOME_DIR/.ssh"
if sftp_batch "pwd" >/dev/null 2>&1; then
    fail "the reintroduced 0700 key directory still authenticated; the case proves nothing"
else
    pass "the old combination really does refuse the login"
fi
/usr/lib/ems-appliance-manager/backup-account.sh ensure >/dev/null 2>&1 || true
REPAIRED=$(sftp_batch "pwd")
if [ $? -eq 0 ] && printf '%s' "$REPAIRED" | grep -q 'Remote working directory: /'; then
    pass "the package repaired the key directory and the login works again"
    printf '    now: %s\n' "$(stat -c '%U:%G %a' "$HOME_DIR/.ssh")"
else
    fail "an upgrade did not repair the key directory: $(stat -c '%U:%G %a' "$HOME_DIR/.ssh")"
fi

printf '\n'
if [ "$failures" -ne 0 ]; then
    echo "RESULT: FAIL ($failures)"
    exit 1
fi
echo "RESULT: PASS"
