#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 basecubedev
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
#
# With PLAYWRIGHT_APT_ARCHIVES set, apt keeps the .deb archives it downloads in
# that directory and installs from what is already there, so a CI cache of it
# leaves the mirror only the package index and what changed. Afterwards apt
# drops the archives the index no longer offers, so a cache carried over from an
# older runner image does not grow, apt goes back to its own archive directory
# for the rest of the job, and the directory is handed back to the calling user,
# as apt writes it as root.
#
# --with-deps runs apt-get update over every source the runner image lists. On
# 2026-11-27 packages.microsoft.com refused ubuntu-26.04's index with a 403, the
# update failed and all three attempts with it, though the browsers' packages
# come from the Ubuntu archive alone. A source naming one of the hosts in
# PLAYWRIGHT_APT_UNNEEDED_HOSTS is therefore set aside before the install, and
# stays aside for the rest of the job; no step after the install uses it.
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <browser>..." >&2
  exit 2
fi

attempts="${PLAYWRIGHT_INSTALL_ATTEMPTS:-3}"
limit="${PLAYWRIGHT_INSTALL_TIMEOUT:-300}"
backoff="${PLAYWRIGHT_INSTALL_BACKOFF:-10}"
apt_conf_dir="${PLAYWRIGHT_APT_CONF_DIR:-/etc/apt/apt.conf.d}"
apt_archives="${PLAYWRIGHT_APT_ARCHIVES:-}"
apt_sources_dir="${PLAYWRIGHT_APT_SOURCES_DIR:-/etc/apt/sources.list.d}"
unneeded_hosts="${PLAYWRIGHT_APT_UNNEEDED_HOSTS:-packages.microsoft.com}"

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo -n "$@"
  fi
}

configure_apt() {
  local archives="$1"
  {
    printf '%s\n' \
      'Acquire::Retries "5";' \
      'Acquire::http::Timeout "30";' \
      'Acquire::https::Timeout "30";' \
      'DPkg::Lock::Timeout "180";'
    if [ -n "$archives" ]; then
      printf 'Dir::Cache::Archives "%s/";\n' "$archives"
      printf '%s\n' 'APT::Keep-Downloaded-Packages "true";'
    fi
  } | as_root tee "$apt_conf_dir/80-ci-playwright-install" >/dev/null
}

hand_back_archives() {
  if [ -n "$apt_archives" ]; then
    as_root apt-get autoclean -qq || echo "Could not drop obsolete archives from $apt_archives." >&2
    configure_apt "" || echo "Could not point apt back at its own archive directory." >&2
    as_root chown -R "$(id -u):$(id -g)" "$apt_archives" \
      || echo "Could not hand $apt_archives back; the cache may not save it." >&2
  fi
}

set_aside_unneeded_sources() {
  local source host
  for source in "$apt_sources_dir"/*.list "$apt_sources_dir"/*.sources; do
    [ -f "$source" ] || continue
    for host in $unneeded_hosts; do
      if grep -qF "$host" "$source"; then
        as_root mv "$source" "$source.playwright-unneeded" \
          && echo "Set aside $source ($host) for the browser install." >&2
        break
      fi
    done
  done
}

release_apt() {
  as_root pkill -KILL -x apt-get || true
  as_root pkill -KILL -x apt || true
  as_root pkill -KILL -f "^/usr/lib/apt/methods/" || true
  as_root dpkg --configure -a || true
}

if [ -n "$apt_archives" ]; then
  mkdir -p "$apt_archives/partial"
fi
configure_apt "$apt_archives"
set_aside_unneeded_sources
for attempt in $(seq 1 "$attempts"); do
  if timeout --kill-after=30 "$limit" npx playwright install --with-deps "$@"; then
    hand_back_archives
    exit 0
  fi
  echo "Playwright install attempt ${attempt} of ${attempts} failed or stalled." >&2
  if [ "$attempt" -lt "$attempts" ]; then
    release_apt
    sleep $((attempt * backoff))
  fi
done
exit 1
