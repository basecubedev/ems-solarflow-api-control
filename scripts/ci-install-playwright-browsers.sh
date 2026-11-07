#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Install the named Playwright browsers and their system packages on a CI
# runner: ci-install-playwright-browsers.sh chromium firefox
#
# The system packages come from the runner's apt mirror, which now and then
# stalls. apt is told to give up on a stalled transfer and retry it, and to wait
# for the dpkg lock instead of failing on it. An attempt that still hangs is
# killed, but the apt-get that sudo started survives in its own session and
# keeps the lock, so before the next attempt whatever apt left running is ended
# and dpkg is told to finish what it had begun; a plain retry could never get
# the lock.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <browser>..." >&2
  exit 2
fi

attempts="${PLAYWRIGHT_INSTALL_ATTEMPTS:-3}"
limit="${PLAYWRIGHT_INSTALL_TIMEOUT:-300}"
backoff="${PLAYWRIGHT_INSTALL_BACKOFF:-10}"
apt_conf_dir="${PLAYWRIGHT_APT_CONF_DIR:-/etc/apt/apt.conf.d}"

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

configure_apt() {
  printf '%s\n' \
    'Acquire::Retries "5";' \
    'Acquire::http::Timeout "30";' \
    'Acquire::https::Timeout "30";' \
    'DPkg::Lock::Timeout "180";' \
    | as_root tee "$apt_conf_dir/80-ci-playwright-install" >/dev/null
}

release_apt() {
  as_root pkill -KILL -x apt-get || true
  as_root pkill -KILL -x apt || true
  as_root pkill -KILL -f "^/usr/lib/apt/methods/" || true
  as_root dpkg --configure -a || true
}

configure_apt
for attempt in $(seq 1 "$attempts"); do
  if timeout --kill-after=30 "$limit" npx playwright install --with-deps "$@"; then
    exit 0
  fi
  echo "Playwright install attempt ${attempt} of ${attempts} failed or stalled." >&2
  if [ "$attempt" -lt "$attempts" ]; then
    release_apt
    sleep $((attempt * backoff))
  fi
done
exit 1
