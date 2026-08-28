# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which Manager version an image is judged against.

``inspect-image-<board>`` is a required gate, and it compared the version dpkg
recorded inside the image against ``appliance/version.py``. That was right while
the image built its own package from the checkout: the two could not disagree.

``--manager-package`` broke the equivalence. The image now bakes in the newest
*published stable* Manager, chosen from the index and independent of this tree,
so the two agree only between releases. Bump ``version.py`` toward the next one
and every image build fails the gate -- during exactly the window where the next
release is waiting for a human to approve its signing key, which is unbounded.
A Monday build inside that window loses the whole week, and it fails looking like
a broken image rather than a policy mismatch.

So the gate reads what the build recorded it actually baked in.
"""

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
GATES = ROOT / "scripts" / "appliance-release-gates.sh"
BUILD = ROOT / "scripts" / "appliance-build-rpi-image.sh"


def extraction():
    """The line the gate runner uses, lifted out and run on its own."""

    text = GATES.read_text(encoding="utf-8")
    start = text.index("        BAKED=$(sed -n")
    end = text.index("[ -n \"$BAKED\" ] || BAKED=$VERSION", start)
    return text[start:end].strip()


def baked(tmp_path, record):
    """What the gate would pass as --appliance-version for this build record."""

    output = tmp_path / "out"
    output.mkdir(exist_ok=True)
    name = "ems-solarflow-appliance-0.2.0-rpi5-arm64"
    if record is not None:
        (output / f"{name}.build.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

    script = (
        f'OUTPUT="{output}"\nNAME="{name}"\nVERSION="0.2.0"\n'
        + extraction()
        + '\n[ -n "$BAKED" ] || BAKED=$VERSION\nprintf %s "$BAKED"\n'
    )
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, check=True, timeout=60
    ).stdout


def test_the_gate_judges_the_package_the_image_carries(tmp_path):
    """The checkout says 0.2.0 and the image carries the published 0.1.0. That
    is the normal state of a repository between releases, not a defect."""

    assert baked(tmp_path, {
        "release_version": "0.2.0",
        "appliance_package": "ems-appliance-manager_0.1.0_arm64.deb",
        "appliance_package_version": "0.1.0",
    }) == "0.1.0"


def test_a_record_from_before_this_field_falls_back(tmp_path):
    """Older artefacts are inspected against the release version, which is what
    they were built with -- a gate that cannot read a record must not silently
    check nothing."""

    assert baked(tmp_path, {
        "release_version": "0.2.0",
        "appliance_package": "ems-appliance-manager_0.2.0_arm64.deb",
    }) == "0.2.0"


def test_a_missing_record_falls_back_rather_than_passing_empty(tmp_path):
    """An empty --appliance-version makes image_inspect record package_version
    as a pass without comparing anything."""

    assert baked(tmp_path, None) == "0.2.0"


def test_the_build_records_the_version_it_baked_in():
    """Read from the package with dpkg-deb rather than from appliance/version.py,
    because the supplied package is the one case where they differ."""

    text = BUILD.read_text(encoding="utf-8")

    assert 'PACKAGE_VERSION=$(dpkg-deb -f "$PACKAGE" Version' in text
    assert '"appliance_package_version": "$PACKAGE_VERSION"' in text
    assert text.index("PACKAGE_VERSION=$(dpkg-deb") < text.index(
        '"appliance_package_version"'
    ), "the field is written before it is set"


def test_an_empty_version_still_means_nothing_is_compared():
    """Why the fallback matters: image_inspect treats an empty expectation as
    'no version declared' and records a pass."""

    from appliance import image_inspect

    source = Path(image_inspect.__file__).read_text(encoding="utf-8")
    guard = source.split("if appliance_version:", 1)[1].split("if architecture:", 1)[0]

    assert "PASS" in guard, "an absent expectation no longer passes; the fallback may be dead"
