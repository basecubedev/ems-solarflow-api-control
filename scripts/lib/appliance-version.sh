# SPDX-License-Identifier: AGPL-3.0-or-later
#
# The one place a shell script learns which version it is building.
#
# Nothing in the source tree records a version. A release takes its version from
# the tag CI was invoked with -- the same way an EMS image takes its version
# from the tag and carries it as an OCI label -- and a build with no tag behind
# it is a development build that says so.
#
#   appliance_version [given]
#
# Prints the version to use: `given` when a caller passed one through, otherwise
# a development version derived from HEAD. Fails only when neither is available,
# because a package with no version is one dpkg refuses to build and a name with
# an empty version in it is one nothing can match.
#
# Sourced rather than executed so the four callers cannot drift: build-deb.sh
# names the package, and the image build, the release gates and the finalizer
# all rebuild the same image name from it. A version they disagree about is a
# gate looking for a file the build did not write.

appliance_version() {
    _av_given=${1:-}
    if [ -n "$_av_given" ]; then
        printf '%s\n' "$_av_given"
        return 0
    fi

    _av_root=${APPLIANCE_VERSION_ROOT:-$ROOT}
    _av_revision=$(git -C "$_av_root" rev-parse HEAD 2>/dev/null || true)
    EMS_REVISION="$_av_revision" python3 -c '
import os
import sys

sys.path.insert(0, sys.argv[1])
from appliance.version import development_version

print(development_version(os.environ.get("EMS_REVISION", "")))
' "$_av_root"
}
