#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Issue a backup key the way an operator's is issued, and report what happened.
#
#   scripts/appliance-guest-issue-backup-key.sh --account NAME --key PATH
#                                               [--password PASS]
#
# Run as root in a disposable Debian guest that has the package installed. The
# key goes through the Appliance Manager's authenticated HTTP API as a planned
# operation, confirmed with the token the appliance issued for it. Nothing is
# written into authorized_keys by hand: a session that only works because the
# test put the key there proves nothing about the appliance.
#
# stdout is machine-readable, one `field: value` per line, so a caller decides
# for itself which fields are a pass and which are a failure:
#
#   fingerprint:       the issued key's SHA256 fingerprint
#   listed:            true when the appliance itself reports that key
#   activation_state:  what `backup-access activate` reported
#   activation_reason: why, when it is not active
#
# Exit status: 0 the key was issued and backup access was activated, 3 a
# prerequisite is missing and no key could be issued. A caller that needs a
# session treats 3 as NOT RUN — never as a confinement result.
set -uo pipefail

ACCOUNT=${EMS_APPLIANCE_BACKUP_USER:-ems-backup}
KEY=""
PASSWORD=${EMS_APPLIANCE_TEST_PASSWORD:-appliance-session-test-password}
API=${EMS_APPLIANCE_API:-http://127.0.0.1:8080}
COOKIES=$(mktemp /tmp/appliance-issue-key.XXXXXX)

cleanup() { rm -f "$COOKIES"; }
trap cleanup EXIT

not_run() {
    echo "appliance-guest-issue-backup-key: $1" >&2
    echo "RESULT: NOT RUN ($2)" >&2
    exit 3
}

while [ $# -gt 0 ]; do
    case "$1" in
        --account) ACCOUNT=${2:-}; shift 2 ;;
        --key) KEY=${2:-}; shift 2 ;;
        --password) PASSWORD=${2:-}; shift 2 ;;
        *) not_run "unknown argument: $1" usage_error ;;
    esac
done
[ -n "$KEY" ] || not_run "--key is required" usage_error

for tool in ssh-keygen curl jq systemctl; do
    command -v "$tool" >/dev/null 2>&1 || not_run "$tool is missing" "${tool}_unavailable"
done
command -v ems-appliance >/dev/null 2>&1 || not_run "the package is not installed" package_missing

systemctl start ems-appliance-agent.service ems-appliance-web.service \
    || not_run "the appliance services would not start" services_unavailable
for _ in $(seq 1 60); do
    curl -fsS -o /dev/null "$API/api/session" 2>/dev/null && break
    sleep 1
done
curl -fsS -o /dev/null "$API/api/session" 2>/dev/null \
    || not_run "the appliance web interface never answered" web_unreachable

# The password is reset through the privileged CLI rather than guessed: an
# earlier tier in the same guest may already have configured one, and a login
# that fails because somebody else set the password is not evidence.
ems-appliance password-reset --password "$PASSWORD" >/dev/null \
    || not_run "the appliance password could not be set" password_reset_failed
curl -fsS -o /dev/null -c "$COOKIES" "$API/api/session"
LOGIN=$(curl -fsS -b "$COOKIES" -c "$COOKIES" -H 'Content-Type: application/json' \
    -d "$(jq -nc --arg p "$PASSWORD" '{password:$p}')" "$API/api/session/login") \
    || not_run "the appliance refused the session" session_refused
CSRF=$(echo "$LOGIN" | jq -r '.csrf_token // empty')
[ -n "$CSRF" ] || CSRF=$(curl -fsS -b "$COOKIES" "$API/api/session" | jq -r '.csrf_token // empty')
[ -n "$CSRF" ] || not_run "the session carries no CSRF token" session_incomplete

api_post() {
    curl -fsS -b "$COOKIES" -c "$COOKIES" -H 'Content-Type: application/json' \
        -H "X-Appliance-CSRF: $CSRF" -d "$2" "$API$1"
}

rm -f "$KEY" "$KEY.pub"
ssh-keygen -q -t ed25519 -N '' -C appliance-issued -f "$KEY" \
    || not_run "a client key could not be generated" keygen_failed
PLAN=$(api_post /api/ssh/keys "$(jq -nc --arg a "$ACCOUNT" --arg k "$(cat "$KEY.pub")" \
    '{account:$a,public_key:$k}')") \
    || not_run "the appliance refused to plan the key addition" key_plan_refused
OPERATION=$(echo "$PLAN" | jq -r '.operation.operation_id // .operation_id // empty')
TOKEN=$(echo "$PLAN" | jq -r '.confirmation_token // empty')
[ -n "$OPERATION" ] || not_run "the key plan named no operation" key_plan_incomplete
# The plan is confirmed with the token the appliance issued for it, which is
# what binds the execution to the plan an operator was shown.
[ -n "$TOKEN" ] || not_run "the key plan issued no confirmation token" key_plan_incomplete
api_post /api/operations/confirm \
    "$(jq -nc --arg o "$OPERATION" --arg t "$TOKEN" '{operation_id:$o,confirmation_token:$t}')" \
    >/dev/null || not_run "the appliance refused to execute the key addition" key_execute_refused

FINGERPRINT=$(ssh-keygen -lf "$KEY.pub" | awk '{print $2}')
LISTED=false
curl -fsS -b "$COOKIES" "$API/api/ssh/keys" 2>/dev/null \
    | jq -r '..|.fingerprint? // empty' | grep -qF "$FINGERPRINT" && LISTED=true

# activate is the call that restores the key into the account, so a session
# before it would be testing the wrong thing.
ACTIVATION=$(ems-appliance backup-access activate --json 2>&1)
STATE=$(echo "$ACTIVATION" | jq -r '.state // empty' 2>/dev/null)
REASON=$(echo "$ACTIVATION" | jq -r '.reason // empty' 2>/dev/null)
systemctl reload ssh.service 2>/dev/null || systemctl restart ssh.service

printf 'fingerprint: %s\n' "$FINGERPRINT"
printf 'listed: %s\n' "$LISTED"
printf 'activation_state: %s\n' "${STATE:-unreported}"
printf 'activation_reason: %s\n' "${REASON:-none}"
[ "$STATE" = active ] || not_run "backup access is ${STATE:-unreported}" backup_access_inactive
exit 0
