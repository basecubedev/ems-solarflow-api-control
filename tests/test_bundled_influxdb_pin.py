# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The bundled InfluxDB image is pinned once and every copy repeats that pin.

An installation runs the image its compose file names, and four files write or
document that line. When only one of them moved, an installer kept setting up
the old version while the release claimed the new one -- which is how 2.7
stayed in every file long after 2.9 shipped. The walk below finds any file
that names an InfluxDB image and holds it to the pin, so a fifth copy cannot
appear without the sync tool learning about it.
"""

import importlib.util
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from ems import influx_image
from ems.influx_image import (
    InfluxVersion,
    compose_image,
    is_upgrade,
    parse_image,
    pinned_image,
    replace_image,
)

pytestmark = [pytest.mark.unit, pytest.mark.config]

ROOT = Path(__file__).resolve().parents[1]
_ANY_INFLUX_IMAGE = re.compile(r"(?:image:\s*[\"']?|FROM\s+)(influxdb[:@][^\s\"'`|]+)")

# Files that name an InfluxDB image other than the pin, each with its reason.
NOT_THE_SERVER = {
    # The EMS image's build stage copies only the influx CLI out of it; the
    # Dependabot "docker" block watches it, and the licence test pairs its tag
    # with the CLI's notices.
    "Dockerfile",
}


def _tool():
    spec = importlib.util.spec_from_file_location("bundled_influxdb", ROOT / "tools" / "bundled_influxdb.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tracked_files():
    listed = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True)
    return [line for line in listed.stdout.splitlines() if line]


def test_the_pin_is_one_exact_influxdb_2_release():
    version = parse_image(pinned_image(ROOT))

    assert version is not None and version.major == 2 and version.patch is not None


def test_every_copy_repeats_the_pin():
    image, stale = _tool().differing(ROOT)

    assert stale == [], f"{stale} differ from {image}; run python3 tools/bundled_influxdb.py --sync"


def test_every_product_file_that_names_an_influxdb_image_names_the_pin():
    pin = pinned_image(ROOT)
    found = {}
    for relative in _tracked_files():
        # Python names images only as test data or in prose; the copies an
        # installation runs are compose files, installers and workflows.
        if "/tests/" in f"/{relative}" or relative.startswith("reports/") or relative.endswith(".py"):
            continue
        if relative in NOT_THE_SERVER:
            continue
        path = ROOT / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        images = set(_ANY_INFLUX_IMAGE.findall(text))
        if images:
            found[relative] = images
    tool = _tool()
    named = {relative for relative, images in found.items() if images}

    assert all(images == {pin} for images in found.values()), found
    assert named == {influx_image.PIN_FILE, *tool.COMPOSE_COPIES}, named


def test_the_ci_analytics_job_runs_the_pinned_image_rather_than_a_copy():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "simulated-regression-tests.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["influx-e2e"]
    runs = "\n".join(step.get("run", "") for step in job["steps"])

    assert "services" not in job
    assert 'image="$(python3 -I tools/bundled_influxdb.py --print)"' in runs


def test_dependabot_watches_the_pin_and_only_the_pin():
    config = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    compose = [update for update in config["updates"] if update["package-ecosystem"] == "docker-compose"]

    assert len(compose) == 1
    assert compose[0].get("directory") == "/" + str(Path(influx_image.PIN_FILE).parent)
    assert "directories" not in compose[0]


def test_sync_rewrites_a_copy_that_fell_behind(tmp_path):
    tool = _tool()
    for relative in (influx_image.PIN_FILE, *tool.COMPOSE_COPIES, tool.INVENTORY):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text(encoding="utf-8"), encoding="utf-8")
    pin_path = tmp_path / influx_image.PIN_FILE
    pin_path.write_text(replace_image(pin_path.read_text(encoding="utf-8"), "influxdb:2.10.1"), encoding="utf-8")

    image, stale = tool.differing(tmp_path)
    assert image == "influxdb:2.10.1"
    assert stale == sorted((*tool.COMPOSE_COPIES, tool.INVENTORY))

    tool.sync(tmp_path)

    assert tool.differing(tmp_path) == ("influxdb:2.10.1", [])
    assert "| `influxdb:2.10.1` | 2.10.1 | MIT | Optional bundled Analytics database" in (
        tmp_path / tool.INVENTORY
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("image", "version"),
    [
        ("influxdb:2.7", InfluxVersion(2, 7, None)),
        ("influxdb:2.9.1", InfluxVersion(2, 9, 1)),
        ("influxdb:2.10.1", InfluxVersion(2, 10, 1)),
        ("influxdb:3.12-core", None),
        ("influxdb:3.1.0", None),
        ("influxdb:1.11", None),
        ("influxdb:2.7-alpine", None),
        ("influxdb:latest", None),
        ("influxdb@sha256:" + "a" * 64, None),
        ("docker.io/library/influxdb:2.7", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_image_reads_only_an_influxdb_2_tag(image, version):
    assert parse_image(image) == version


@pytest.mark.parametrize(
    ("current", "target", "upgrade"),
    [
        ("influxdb:2.7", "influxdb:2.9.1", True),
        ("influxdb:2.7.12", "influxdb:2.9.1", True),
        ("influxdb:2.9", "influxdb:2.9.1", False),
        ("influxdb:2.9.1", "influxdb:2.9.1", False),
        ("influxdb:2.9.1", "influxdb:2.9.2", True),
        ("influxdb:2.9.1", "influxdb:2.10.1", True),
        ("influxdb:2.10.1", "influxdb:2.9.1", False),
        ("influxdb:2.9.1", "influxdb:2.7", False),
        ("influxdb:2.7-alpine", "influxdb:2.9.1", False),
        ("influxdb:2.9.1", "influxdb:3.12-core", False),
        ("influxdb:latest", "influxdb:2.9.1", False),
    ],
)
def test_is_upgrade_moves_only_forward_within_2_x(current, target, upgrade):
    assert is_upgrade(current, target) is upgrade


def test_compose_image_reads_one_line_and_refuses_two():
    one = "services:\n  influxdb:\n    image: influxdb:2.7\n"
    quoted = "services:\n  influxdb:\n    image: \"influxdb:2.7\"\n"
    two = one + "  other:\n    image: influxdb:2.9.1\n"

    assert compose_image(one) == "influxdb:2.7"
    assert compose_image(quoted) == "influxdb:2.7"
    assert compose_image(two) is None
    assert compose_image("services: {}\n") is None


def test_replace_image_keeps_the_rest_of_the_file_byte_for_byte():
    text = (
        "services:\n  ems:\n    image: ghcr.io/basecubedev/ems-solarflow-api-control:v1\n"
        "  influxdb:\n    image: 'influxdb:2.7'   \n    restart: unless-stopped\n"
    )

    replaced = replace_image(text, "influxdb:2.9.1")

    assert replaced == text.replace("image: 'influxdb:2.7'   \n", "image: 'influxdb:2.9.1'\n")


@pytest.mark.parametrize(
    "text",
    [
        "services: {}\n",
        "a:\n  image: influxdb:2.7\nb:\n  image: influxdb:2.7\n",
    ],
)
def test_replace_image_refuses_a_file_without_exactly_one_line(text):
    with pytest.raises(ValueError):
        replace_image(text, "influxdb:2.9.1")


def test_replace_image_refuses_anything_but_an_influxdb_2_image():
    with pytest.raises(ValueError):
        replace_image("x:\n  image: influxdb:2.7\n", "influxdb:3.12-core")


def test_pinned_image_refuses_a_moving_tag(tmp_path):
    pin = tmp_path / influx_image.PIN_FILE
    pin.parent.mkdir(parents=True)
    pin.write_text("services:\n  influxdb:\n    image: influxdb:2.9\n", encoding="utf-8")

    with pytest.raises(ValueError):
        pinned_image(tmp_path)
