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
API=http://127.0.0.1:8080
COOKIES=/root/.appliance-session
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

step "the appliance's own services"
systemctl start ems-appliance-agent.service ems-appliance-web.service \
    || not_run "the appliance services would not start" services_unavailable
for _ in $(seq 1 60); do
    curl -fsS -o /dev/null "$API/api/session" 2>/dev/null && break
    sleep 1
done
curl -fsS -o /dev/null "$API/api/session" 2>/dev/null \
    || not_run "the appliance web interface never answered" web_unreachable
pass "the agent and the web interface are running"

step "an authenticated operator session"
# The password is reset through the privileged CLI rather than guessed: an
# earlier tier in the same guest may already have configured one, and a login
# that fails because somebody else set the password is not evidence about
# anything this tier is asking.
ems-appliance password-reset --password "$PASSWORD" >/dev/null \
    || not_run "the appliance password could not be set" password_reset_failed
rm -f "$COOKIES"
curl -fsS -o /dev/null -c "$COOKIES" "$API/api/session"
LOGIN=$(curl -fsS -b "$COOKIES" -c "$COOKIES" -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg p "$PASSWORD" '{password:$p}')" "$API/api/session/login") \
    || not_run "the appliance refused the session" session_refused
CSRF=$(echo "$LOGIN" | jq -r '.csrf_token // empty')
[ -n "$CSRF" ] || CSRF=$(curl -fsS -b "$COOKIES" "$API/api/session" | jq -r '.csrf_token // empty')
[ -n "$CSRF" ] || not_run "the session carries no CSRF token" session_incomplete
pass "an authenticated session with a CSRF token"

api_post() {
    curl -fsS -b "$COOKIES" -c "$COOKIES" -H 'Content-Type: application/json' \
        -H "X-Appliance-CSRF: $CSRF" -d "$2" "$API$1"
}

step "a key issued through the appliance's key management"
rm -f "$CLIENT_KEY" "$CLIENT_KEY.pub"
ssh-keygen -q -t ed25519 -N '' -C appliance-issued -f "$CLIENT_KEY"
PUBLIC=$(cat "$CLIENT_KEY.pub")
PLAN=$(api_post /api/ssh/keys "$(jq -nc --arg a "$BACKUP_USER" --arg k "$PUBLIC" \
    '{account:$a,public_key:$k}')") \
    || not_run "the appliance refused to plan the key addition" key_plan_refused
OPERATION=$(echo "$PLAN" | jq -r '.operation.operation_id // .operation_id // empty')
TOKEN=$(echo "$PLAN" | jq -r '.confirmation_token // empty')
[ -n "$OPERATION" ] || not_run "the key plan named no operation" key_plan_incomplete
# The plan is confirmed with the token the appliance issued for it, which is
# what binds the execution to the plan an operator was shown.
[ -n "$TOKEN" ] || not_run "the key plan issued no confirmation token" key_plan_incomplete
api_post /api/operations/confirm \
    "$(jq -nc --arg o "$OPERATION" --arg t "$TOKEN" \
        '{operation_id:$o,confirmation_token:$t}')" >/dev/null \
    || not_run "the appliance refused to execute the key addition" key_execute_refused
KEYS=$(curl -fsS -b "$COOKIES" "$API/api/ssh/keys")
FINGERPRINT=$(ssh-keygen -lf "$CLIENT_KEY.pub" | awk '{print $2}')
if echo "$KEYS" | jq -r '..|.fingerprint? // empty' | grep -qF "$FINGERPRINT"; then
    pass "the appliance reports the key it issued ($FINGERPRINT)"
else
    fail "the issued key is not in the appliance's own key list"
fi

step "the backup account, activated by the appliance"
# activate is the call that reports a state; status reports the observation it
# was derived from. Activation is also what restores the key into the account,
# so a session before it would be testing the wrong thing.
ACTIVATION=$(ems-appliance backup-access activate --json 2>&1)
STATE=$(echo "$ACTIVATION" | jq -r '.state // empty' 2>/dev/null)
if [ "$STATE" = active ]; then
    pass "backup access is active"
else
    fail "backup access is ${STATE:-unreported}: $(echo "$ACTIVATION" | jq -r '.reason // empty' 2>/dev/null)"
    echo "$ACTIVATION" | head -40 | sed 's/^/    /'
fi
systemctl reload ssh.service 2>/dev/null || systemctl restart ssh.service

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

step "the session is sftp and nothing else"
if ssh "${SSH_OPTS[@]}" "$BACKUP_USER@127.0.0.1" 'id' >/dev/null 2>&1; then
    fail "the confined account executed a shell command"
else
    pass "shell and command execution are refused"
fi

ssh "${SSH_OPTS[@]}" -N -L 127.0.0.1:18022:127.0.0.1:22 \
    -o ExitOnForwardFailure=yes "$BACKUP_USER@127.0.0.1" >/dev/null 2>&1 &
forward_pid=$!
sleep 3
if kill -0 "$forward_pid" 2>/dev/null && (exec 3<>/dev/tcp/127.0.0.1/18022) 2>/dev/null; then
    fail "local port forwarding was granted"
else
    pass "local port forwarding (-L) is refused"
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

printf '\n'
if [ "$failures" -ne 0 ]; then
    echo "RESULT: FAIL ($failures)"
    exit 1
fi
echo "RESULT: PASS"
