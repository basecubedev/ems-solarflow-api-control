#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Admin tier. Optional first argument narrows to one functional area:
#   all (default) authority setup maintenance workflow config mqtt
#   system_build backup_restore
# See docs/developer/testing.md.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/lib/pytest-tier.sh
. scripts/lib/pytest-tier.sh

area="all"
if [ $# -gt 0 ] && [ "${1#-}" = "$1" ]; then
    area="$1"
    shift
fi

case "$area" in
    all)
        selection="admin and not docker and not slow"
        ;;
    authority|setup|maintenance|workflow|config|mqtt|system_build|backup_restore)
        selection="admin and ${area} and not docker and not slow"
        ;;
    *)
        printf 'usage: %s [all|authority|setup|maintenance|workflow|config|mqtt|system_build|backup_restore] [pytest args]\n' \
            "$0" >&2
        exit 2
        ;;
esac

run_pytest_tier "admin/${area}" -q -m "$selection" "$@"
