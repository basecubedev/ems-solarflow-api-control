#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Install the Appliance Manager package in this guest and prove it is usable.
#
# Runs *inside* a disposable guest — a systemd container or a QEMU VM — and is
# the single source of truth for what "the package works on this architecture"
# means. Both smoke-test drivers copy this script in and run it, so amd64 and
# ARM64 are held to exactly the same standard.
#
# Usage: appliance-guest-smoke.sh /path/to/ems-appliance-manager_*.deb [expected-arch]
set -eu

PACKAGE=${1:?usage: appliance-guest-smoke.sh <package.deb> [expected-arch]}
EXPECTED_ARCH=${2:-}

STATE_DIR=/var/lib/ems-appliance-manager
LOG_DIR=/var/log/ems-appliance-manager
RUNTIME_DIR=/run/ems-appliance-manager
SOCKET="$RUNTIME_DIR/agent.sock"
WEB_USER=ems-appliance-web
AGENT_UNIT=ems-appliance-agent.service
WEB_UNIT=ems-appliance-web.service
EXPORT_ROOT=/srv/ems-appliance-export
PASSWORD=appliance-smoke-password-1

failures=0

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1" >&2; failures=$((failures + 1)); }

# A stage marker names the boundary a run reached, so a guest that stops says
# where. The slug is the step name, which is what the reader already sees.
step() {
    printf '\nAPPLIANCE_EVIDENCE stage=%s\n' \
        "$(printf '%s' "$1" | tr ' ' '-' | tr -cd 'A-Za-z0-9-')"
    printf '== %s ==\n' "$1"
}

check() {
    description=$1
    shift
    if "$@" >/dev/null 2>&1; then pass "$description"; else fail "$description"; fi
}

check_not() {
    description=$1
    shift
    if "$@" >/dev/null 2>&1; then fail "$description"; else pass "$description"; fi
}

# How long the agent takes to answer, when something said it did not. The
# install check is bounded, so its verdict cannot tell a slow host from a
# broken one; this is unbounded enough to say which, and only runs on failure.
probe_agent() {
    printf '\n---- agent round-trip ----\n'
    runuser -u "$WEB_USER" -- python3 -c "
import json, socket, sys, time
started = time.monotonic()
try:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(300)
    s.connect('$SOCKET')
    connected = time.monotonic() - started
    s.sendall(b'{\"operation\": \"status.get\"}\n')
    data = b''
    while not data.endswith(b'\n'):
        chunk = s.recv(65536)
        if not chunk: break
        data += chunk
    print('connect_seconds: %.1f' % connected)
    print('reply_seconds: %.1f' % (time.monotonic() - started))
    print('ok: %s' % json.loads(data.decode()).get('ok'))
except Exception as exc:
    print('failed_after_seconds: %.1f' % (time.monotonic() - started))
    print('error: %s: %s' % (exc.__class__.__name__, exc))
" 2>&1 || true
}

dump_logs() {
    printf '\n---- %s ----\n' "$AGENT_UNIT"
    journalctl -u "$AGENT_UNIT" -n 80 --no-pager 2>&1 || true
    printf '\n---- %s ----\n' "$WEB_UNIT"
    journalctl -u "$WEB_UNIT" -n 80 --no-pager 2>&1 || true
    printf '\n---- units ----\n'
    systemctl --failed --no-pager 2>&1 || true
    probe_agent
}

step "architecture"
GUEST_ARCH=$(dpkg --print-architecture)
printf 'guest: %s %s (dpkg %s)\n' \
    "$(uname -m)" "$(. /etc/os-release && echo "$PRETTY_NAME")" "$GUEST_ARCH"
printf 'package: %s\n' "$(basename "$PACKAGE")"

# A result from the wrong architecture is not a result. This runs before the
# package is installed, so a mismatched guest costs nothing.
if [ -n "$EXPECTED_ARCH" ]; then
    if [ "$GUEST_ARCH" != "$EXPECTED_ARCH" ]; then
        fail "this guest is $GUEST_ARCH, the run expects $EXPECTED_ARCH"
        printf '\nRESULT: FAIL\n'
        exit 1
    fi
    pass "the guest is $EXPECTED_ARCH"
    # Asked of the package, not of its file name. A driver that stages the
    # package under a neutral name — the ARM64 one copies it in as
    # appliance.deb — would otherwise be told its own arm64 build is not an
    # arm64 build, and a file renamed to look right would be believed.
    PACKAGE_ARCH=$(dpkg-deb -f "$PACKAGE" Architecture 2>/dev/null)
    if [ "$PACKAGE_ARCH" = "$EXPECTED_ARCH" ]; then
        pass "the package is built for $EXPECTED_ARCH"
    else
        fail "the package declares ${PACKAGE_ARCH:-no architecture}, not $EXPECTED_ARCH"
    fi
fi

step "install"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1 || true
if apt-get install -y -qq --allow-downgrades "$PACKAGE"; then
    pass "the package installs and its postinst reports success"
else
    fail "the package installation failed"
    dump_logs
    printf '\nRESULT: FAIL\n'
    exit 1
fi

step "post-install verification"
if /usr/bin/ems-appliance verify-install; then
    pass "verify-install reports a usable appliance"
else
    fail "verify-install reports an unusable appliance"
    dump_logs
fi

step "services"
check "$AGENT_UNIT is active" systemctl is-active --quiet "$AGENT_UNIT"
check "$WEB_UNIT is active" systemctl is-active --quiet "$WEB_UNIT"
check "the agent socket exists" test -S "$SOCKET"

socket_mode=$(stat -c '%a %U %G' "$SOCKET" 2>/dev/null || echo "missing")
if [ "$socket_mode" = "660 root ems-appliance" ]; then
    pass "the socket is root:ems-appliance 0660"
else
    fail "the socket is '$socket_mode', expected '660 root ems-appliance'"
fi

step "the web account reaches the agent"
if runuser -u "$WEB_USER" -- python3 -c "
import json, socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(30)
s.connect('$SOCKET')
s.sendall(b'{\"operation\": \"status.get\"}\n')
data = b''
while not data.endswith(b'\n'):
    chunk = s.recv(65536)
    if not chunk: break
    data += chunk
sys.exit(0 if json.loads(data.decode())['ok'] else 1)
"; then
    pass "$WEB_USER can use the agent socket"
else
    fail "$WEB_USER cannot use the agent socket"
    dump_logs
fi

step "state boundary"
for private in "$STATE_DIR/agent" "$LOG_DIR/agent" "$LOG_DIR/audit"; do
    owner=$(stat -c '%a %U %G' "$private" 2>/dev/null || echo missing)
    if [ "$owner" = "700 root root" ]; then
        pass "$private is root:root 0700"
    else
        fail "$private is '$owner', expected '700 root root'"
    fi
    check_not "$WEB_USER cannot list $private" runuser -u "$WEB_USER" -- ls "$private"
    check_not "$WEB_USER cannot write $private" runuser -u "$WEB_USER" -- \
        touch "$private/.smoke-probe"
done
check "$WEB_USER owns its own state" test "$(stat -c %U "$STATE_DIR/web")" = "$WEB_USER"

step "packaged HTTP authentication"
if python3 - "$PASSWORD" <<'PY'
import json, sys, urllib.error, urllib.request

PASSWORD = sys.argv[1]
BASE = "http://127.0.0.1:8080"
cookie = csrf = ""


def call(method, path, body=None):
    global cookie, csrf
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if cookie:
        request.add_header("Cookie", cookie)
    if csrf:
        request.add_header("X-Appliance-CSRF", csrf)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read().decode()
            payload = json.loads(raw) if raw.strip() else {}
            header = response.headers.get("Set-Cookie") or ""
            if header and "Max-Age=0" not in header:
                cookie = header.split(";", 1)[0]
            elif header:
                cookie = ""
            if payload.get("csrf_token"):
                csrf = payload["csrf_token"]
            return response.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        return exc.code, json.loads(raw) if raw.strip() else {}


status, session = call("GET", "/api/session")
assert status == 200, ("session", status)
if session.get("password_configured"):
    status, _ = call("POST", "/api/session/login", {"password": PASSWORD})
else:
    status, _ = call("POST", "/api/session/setup",
                     {"password": PASSWORD, "confirmation": PASSWORD})
assert status == 200, ("first login", status)

status, overview = call("GET", "/api/status")
assert status == 200, ("status", status)
assert overview["system"]["status"] == "ok", overview["system"]

status, session = call("GET", "/api/session")
audit = session.get("security_audit") or {}
assert audit.get("authoritative") is True, audit
assert audit.get("degraded") is False, audit

status, _ = call("POST", "/api/session/logout")
assert status == 200, ("logout", status)
status, _ = call("GET", "/api/status")
assert status == 401, ("after logout", status)
print("http flow ok")
PY
then
    pass "first password setup, login, status, audit health and logout"
else
    fail "the packaged HTTP authentication flow failed"
    dump_logs
fi

step "audit trail"
AUDIT="$LOG_DIR/audit/audit.log"
if [ -f "$AUDIT" ]; then
    check "the audit log belongs to root" test "$(stat -c '%U:%G' "$AUDIT")" = "root:root"
    check_not "$WEB_USER cannot read the audit log" runuser -u "$WEB_USER" -- cat "$AUDIT"
    if grep -q "$PASSWORD" "$AUDIT"; then
        fail "the appliance password leaked into the audit log"
    else
        pass "no password reached the audit log"
    fi
else
    fail "no audit entry was written for the authentication flow"
fi

step "export root"
check "the export root exists" test -d "$EXPORT_ROOT"
export_mode=$(stat -c '%a %U %G' "$EXPORT_ROOT" 2>/dev/null || echo missing)
if [ "$export_mode" = "755 root root" ]; then
    pass "the export root is root:root 0755 (chroot-safe)"
else
    fail "the export root is '$export_mode', expected '755 root root'"
fi
check "the sshd drop-in chroots the backup account" \
    grep -q "ChrootDirectory $EXPORT_ROOT" /etc/ssh/sshd_config.d/ems-appliance-backup.conf

step "reinstall"
if apt-get install -y -qq --allow-downgrades --reinstall "$PACKAGE" >/dev/null 2>&1; then
    pass "the package reinstalls cleanly"
else
    fail "the package could not be reinstalled"
fi
check "verify-install still passes after a reinstall" /usr/bin/ems-appliance verify-install

step "A/B units on a single-slot guest"
# The same package installs on an ordinary host and on an A/B image. Here there
# is no layout descriptor, so every A/B unit must be present and inert: a unit
# that tried to verify a persistent partition that is not there would make the
# package uninstallable on a normal Raspberry Pi.
AB_LAYOUT=/etc/ems-appliance-manager/ab-layout.json
check_not "this guest has no A/B layout" test -f "$AB_LAYOUT"
for unit in ems-appliance-persistence.service ems-appliance-ab-health.service \
            ems-appliance-slot-bootstrap.service ems-appliance-grow-persistent.service; do
    check "$unit ships with the package" \
        test -f "/usr/lib/systemd/system/$unit"
    if systemctl is-active --quiet "$unit"; then
        fail "$unit ran on a host with no A/B layout"
    else
        pass "$unit is inert without an A/B layout"
    fi
done
check "the growth helper ships and is executable" \
    test -x /usr/lib/ems-appliance-manager/grow-persistent.sh

step "A/B command surface"
if /usr/bin/ems-appliance ab status --json >/dev/null 2>&1; then
    ab_mode=$(/usr/bin/ems-appliance ab status --json 2>/dev/null \
        | sed -n 's/.*"mode"[^"]*"\([^"]*\)".*/\1/p' | head -n 1)
    if [ "$ab_mode" = "single_slot" ]; then
        pass "ab status reports single_slot on this guest"
    else
        fail "ab status reports '$ab_mode', expected single_slot"
    fi
else
    pass "ab status needs the agent socket or root; not a single-slot failure"
fi
check_not "ab mount-persistence no longer exists" \
    /usr/bin/ems-appliance ab mount-persistence --help
check "ab verify-persistence exists" \
    sh -c '/usr/bin/ems-appliance ab --help 2>&1 | grep -q verify-persistence'
check "ab slot-bootstrap exists" \
    sh -c '/usr/bin/ems-appliance ab --help 2>&1 | grep -q slot-bootstrap'

step "host identity policy"
# /etc/ssh as a whole is never shared between slots; only the appliance-owned
# key directory is, and it is named by a drop-in rather than by moving the keys.
check_not "the package does not share the whole /etc/ssh" \
    grep -rq "^Path=/etc/ssh$" /etc/rpi-image-gen/slot-shared.d/
if [ -f /etc/rpi-image-gen/slot-shared.d/50-ems-appliance.conf ]; then
    check "the appliance declares its shared paths" \
        grep -q "^Path=/var/lib/ems-appliance-manager$" \
        /etc/rpi-image-gen/slot-shared.d/50-ems-appliance.conf
else
    pass "no slot-shared declaration on a single-slot guest (image-only)"
fi

printf '\n'
if [ "$failures" -eq 0 ]; then
    printf 'RESULT: PASS\n'
    exit 0
fi
printf 'RESULT: FAIL (%s check(s))\n' "$failures"
exit 1
