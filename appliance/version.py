# SPDX-License-Identifier: AGPL-3.0-or-later
"""Appliance Manager package version, and how versions compare.

This is the version of the host package, deliberately independent from the EMS
Admin container version it manages.

The comparator lives here because two callers need the same answer and used to
compute it separately: the OS release gates and the container release index.
Both discarded everything after the first hyphen, which made ``0.1.0-rc1`` and
``0.1.0`` compare equal — so a candidate could not be told from its own final
release, in either direction, by either caller.
"""

import re

from appliance.validation import normalize_version

PACKAGE_NAME = "ems-appliance-manager"

# What a build with no tag behind it calls itself. dpkg refuses a package with
# no Version at all, so unlike the EMS `latest` channel -- which carries an
# empty runtime version so a channel can never pose as a release -- this side
# needs a string.
#
# The tilde is the load-bearing character, and it is the same choice the
# packaging already makes for a candidate: dpkg sorts `~` below everything, and
# ``version_key`` reads it as a prerelease marker, so both comparators agree
# that a development build precedes every release. `+dev` would have looked
# equivalent and is not -- ``version_key`` treats `+` in the core as part of a
# number and would have scored it equal to 0.0.0, while dpkg sorts it above.
DEVELOPMENT_VERSION_PREFIX = "0.0.0~dev"
# Two products publish releases from this repository, and for a long time only
# one of them had a tag namespace: every tag was an EMS version, so "the latest
# stable Appliance Manager" was not a thing that could be asked for. This prefix
# is what makes it askable, and it deliberately does not parse as a version --
# admin/releases.py offers every non-draft release of this repository as an EMS
# system build and decides eligibility by parsing the tag.
TAG_PREFIX = "appliance-manager-v"
SUPPORTED_ARCHITECTURES = ("arm64",)
# The boards this package runs on. Not the same list as the boards this project
# builds an image for: the package installs on Raspberry Pi OS anywhere.
SUPPORTED_PI_MODELS = ("Raspberry Pi 3", "Raspberry Pi 4", "Raspberry Pi 5")

_TRAILING_NUMBER = re.compile(r"^([^0-9]+)([0-9]+)$")
_INSTALLED_VERSION = None


def version_key(text):
    """A sortable key where a prerelease ranks below the release it precedes.

    Numeric identifiers compare numerically and rank below alphanumeric ones,
    which is what makes ``1.0.0-rc1`` order before ``1.0.0-rc.beta`` and both
    before ``1.0.0``. Anything unparseable degrades to zero rather than raising:
    these keys gate installs, and a refusal has to come from a rule, not from a
    manifest that happened to spell its version oddly.
    """

    raw = str(text or "").strip().lstrip("vV")
    # Debian spells a pre-release with a tilde, and sorts it below the release:
    # `0.1.0~rc1 < 0.1.0`, while `0.1.0-rc1` is a *revision* and sorts above.
    # Splitting only on the hyphen made the tilde form invisible here -- it
    # parsed as the release itself and compared equal, which is the same defect
    # this function was written to fix, in the spelling the packaging uses.
    marker = min((raw.find(c) for c in "~-" if c in raw), default=-1)
    core, prerelease = (raw, "") if marker < 0 else (raw[:marker], raw[marker + 1:])
    parts = [int(chunk) if chunk.isdigit() else 0 for chunk in core.split(".")]
    while len(parts) < 3:
        parts.append(0)
    release = tuple(parts[:3])

    if not prerelease:
        # A release outranks every prerelease that carries the same core.
        return (*release, 1, ())

    identifiers = []
    for chunk in prerelease.replace("+", ".").split("."):
        if chunk.isdigit():
            identifiers.append((0, "", int(chunk)))
            continue
        # A trailing number counts as a number. Strict semver compares "rc10"
        # and "rc2" as text and puts rc10 first; this project writes rc1, rc2,
        # rc10 without a separator, and ordering the tenth candidate before the
        # second is wrong in the direction that matters -- a release gate would
        # read the newest candidate as the oldest.
        match = _TRAILING_NUMBER.match(chunk)
        if match:
            identifiers.append((1, match.group(1), int(match.group(2))))
        else:
            identifiers.append((1, chunk, 0))
    return (*release, 0, tuple(identifiers))


def is_stable(text):
    """Whether a version names a release rather than a candidate for one.

    Derived from ``version_key`` rather than re-parsed, because a second reader
    of the same string is how ``0.1.0~rc1`` came to compare equal to ``0.1.0``
    in the first place. The key's fourth element carries the answer: a release
    outranks every prerelease sharing its core, and is marked with 1. It is
    only an answer for a string the key reads whole -- ``0.3.8+dev1`` and
    ``0.4.0~`` both score as releases -- so anything else is not stable.
    """

    return is_readable(text) and version_key(text)[3] == 1


TRACK_STABLE = "stable"
TRACK_UNSTABLE = "unstable"
TRACK_EXPERIMENTAL = "experimental"
# The Admin console's three groups and its rule for them: a release is stable,
# every prerelease is a candidate, and experimental is a development build --
# here the untagged 0.0.0~dev builds -- or anything nobody can read.
_READABLE_VERSION = re.compile(r"^v?\d+\.\d+\.\d+(?:[~-][0-9A-Za-z][0-9A-Za-z.~+-]*)?$")

DIRECTION_UPGRADE = "upgrade"
DIRECTION_DOWNGRADE = "downgrade"
DIRECTION_REINSTALL = "reinstall"
DIRECTION_UNKNOWN = "unknown"


def is_readable(text):
    """Whether ``version_key`` reads the whole string rather than guessing.

    An optional lowercase ``v``, as ``normalize_version`` strips; exactly three
    core numbers, because the key keeps three and drops the rest;
    a non-empty prerelease, because ``0.4.0~`` would read as the release; and
    no build metadata on the core, because ``0.3.8+dev1`` scores it as 0.3.0.
    After a prerelease the key splits on ``+`` like on ``.``, so it is read.
    """

    return bool(_READABLE_VERSION.match(str(text or "").strip()))


def is_development(text):
    """An untagged build: ordered by commit hash, so never by version."""

    return normalize_version(text).startswith(DEVELOPMENT_VERSION_PREFIX)


def is_comparable(text, *, package=False):
    """Whether ``version_key`` can place this version against another one.

    ``package`` is a Debian package version, where ``-N`` is a revision dpkg
    sorts above the release and ``version_key`` would sort below it.
    """

    if package and "-" in str(text or ""):
        return False
    return is_readable(text) and not is_development(text)


def release_track(text, *, package=False):
    """Stable, unstable or experimental, by the Admin console's rule.

    That rule is the one its version list is grouped by,
    ``admin/releases.py::_is_release_candidate``: every prerelease is a
    candidate. ``admin/system_build.classify_channel`` names build identities
    and is not what the list shows.
    """

    raw = str(text or "").strip()
    if not is_comparable(raw, package=package):
        return TRACK_EXPERIMENTAL
    return TRACK_STABLE if is_stable(raw) else TRACK_UNSTABLE


def same_version(left, right, *, package=False):
    """Equal but for a ``v`` prefix; a tag also ignores case, a package does not."""

    if package:
        return normalize_version(left) == normalize_version(right)
    return normalize_version(left).lower() == normalize_version(right).lower()


def compare(*, offered, installed, package=False):
    """The direction, and when it is unknown, which side made it so."""

    if not is_comparable(installed, package=package):
        return DIRECTION_UNKNOWN, "running"
    if not is_comparable(offered, package=package):
        return DIRECTION_UNKNOWN, "offered"
    if same_version(offered, installed, package=package):
        return DIRECTION_REINSTALL, ""
    offered_key, installed_key = version_key(offered), version_key(installed)
    if offered_key > installed_key:
        return DIRECTION_UPGRADE, ""
    if offered_key < installed_key:
        return DIRECTION_DOWNGRADE, ""
    return DIRECTION_UNKNOWN, "tie"


def direction(*, offered, installed, package=False):
    """Which way installing ``offered`` moves from ``installed``.

    Unknown unless both sides are comparable: readable, not an untagged
    development build, and not a spelling ``version_key`` scores equal to a
    different string. The same tag in another case is a reinstall; a package
    version is compared exactly, as dpkg does.
    """

    return compare(offered=offered, installed=installed, package=package)[0]


def newest_stable(entries, *, version_of, stable=None):
    """The stable entry with the highest version, or None.

    The one answer to "which is the latest stable" -- for the image build, the
    package fetch, the Admin catalogue and the Updates page. ``stable``
    defaults to ``is_stable`` on the entry's version; a caller whose source can
    also demote an entry (an index's own prerelease flag) passes its own.
    """

    stable = stable or (lambda entry: is_stable(version_of(entry)))
    candidates = [entry for entry in entries if stable(entry)]
    return max(candidates, key=lambda entry: version_key(version_of(entry)), default=None)


def tag_for(text):
    """The git tag that names a Manager version."""

    return TAG_PREFIX + str(text or "").strip().lstrip("vV")


def version_from_tag(tag):
    """The Manager version a tag names, or an empty string for any other tag.

    Empty rather than an exception: this reads a list of tags that belongs to
    two products, and an EMS tag is not an error.
    """

    text = str(tag or "").strip()
    return text[len(TAG_PREFIX):] if text.startswith(TAG_PREFIX) else ""


def latest_stable(tags):
    """The newest stable Manager version among tags, or an empty string.

    A prerelease tag never wins here even when it is the newest thing there is:
    the image bakes in what this returns, and "latest" answering with a release
    candidate is the difference between shipping a candidate to every card and
    shipping nothing until a release exists.
    """

    versions = [version for version in map(version_from_tag, tags) if version]
    return newest_stable(versions, version_of=str) or ""


def installed_version(package_version=None, *, refresh=False):
    """The version dpkg reports for the installed Manager, or an empty string.

    The package is the authority on which Manager is running, the same way an
    image's OCI labels are the authority on which EMS release is running (see
    ``admin/installed_release.py``). Nothing in this tree records a version, so
    there is no second answer that could disagree with this one.

    Empty rather than an exception on every path that is not an installed
    package -- a source checkout, a test, a host without dpkg. A caller showing
    this to a person renders the empty string as unknown, and ``direction``
    answers unknown for it rather than guessing which way an install moves.
    """

    if package_version is None and not refresh and _INSTALLED_VERSION is not None:
        return _INSTALLED_VERSION

    query = package_version or default_package_version()
    if query is None:
        answer = ""
    else:
        try:
            answer = query(PACKAGE_NAME) or ""
        except Exception:
            answer = ""

    if package_version is None:
        # Cached for the life of the process, which is exactly how long the
        # answer can stay true: installing a Manager restarts the service that
        # is asking, so a stale value has no window to be read in.
        globals()["_INSTALLED_VERSION"] = answer
    return answer


def default_package_version(runner=None):
    """``dpkg-query`` for one field, or nothing on a host without dpkg.

    Routed through the one allowlisted command runner rather than a bare
    subprocess, so this module starts no host process of its own. Sibling of
    ``rpi_image_gen.default_package_query``, which asks the same tool whether a
    package is installed.
    """

    from appliance.commands import CommandRunner

    runner = runner or CommandRunner()
    if not runner.available("dpkg-query"):
        return None

    def query(package):
        result = runner.run(
            "dpkg-query",
            ["-W", "-f=${Version}", "--", str(package)],
            timeout=15,
        )
        return result.stdout.strip() if result.ok else ""

    return query


def development_version(revision=""):
    """What a build with no release tag behind it is called."""

    short = str(revision or "").strip()[:12]
    return f"{DEVELOPMENT_VERSION_PREFIX}.{short}" if short else DEVELOPMENT_VERSION_PREFIX
