# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one table that says what the appliance image is.

The build side, the release inspector and the booted host all read this module.
Three separate answers would drift, and the one that drifted would be the one
deciding a gate -- so what is asserted here is that every other place derives
its answer from here rather than restating it.
"""

from pathlib import Path

import pytest

from appliance import image_shape, rpi_image_gen

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

IMAGE_DIR = Path(__file__).resolve().parents[1] / "packaging" / "appliance" / "image"


def test_the_image_layer_is_the_one_upstream_the_lock_pins():
    assert image_shape.IMAGE.image_layer == rpi_image_gen.read_lock().image_layer.name


def test_the_shared_config_builds_the_layer_this_table_declares():
    shared = (IMAGE_DIR / "shared" / "ems-appliance.yaml").read_text(encoding="utf-8")

    assert f"layer: {image_shape.IMAGE.image_layer}" in shared
    assert f"app: {image_shape.IMAGE.app_layer}" in shared


def test_every_profile_resolves_to_that_layer():
    for profile in rpi_image_gen.profiles().values():
        assert profile.image_layer == image_shape.IMAGE.image_layer, profile.name


def test_the_artifact_suffix_names_the_board_and_the_architecture():
    assert image_shape.IMAGE.artifact_suffix("rpi5") == "rpi5-arm64"


@pytest.mark.parametrize(
    "name",
    ["", "image-rota", "IMAGE-RPIOS", None, 5],
)
def test_only_the_exact_layer_name_matches(name):
    """Upstream resolves a layer by exact name, so a gate must too.

    Anything that is not that name answers False rather than raising: these are
    gates, and a gate that raises on a missing field fails open on the caller
    that forgot to catch.
    """

    assert image_shape.image_layer_matches(name) is False


def test_the_layer_this_project_builds_matches():
    assert image_shape.image_layer_matches(image_shape.IMAGE.image_layer) is True


@pytest.mark.parametrize(
    "marker",
    [
        {},
        {"image_layer": ""},
        {"image_layer": "image-rota"},
        {"build_id": "20260809120000"},
        None,
        "image-rpios",
    ],
)
def test_a_marker_that_does_not_positively_say_so_is_not_ours(marker):
    """Fail closed by construction.

    A marker written before this field was read, one whose field is empty, and
    one naming a layer this project does not build all answer False -- so no
    absence can turn the first-boot growth gate off.
    """

    assert image_shape.marker_is_ours(marker) is False


def test_a_marker_naming_this_layer_is_ours():
    marker = {"build_id": "20260809120000", "image_layer": image_shape.IMAGE.image_layer}

    assert image_shape.marker_is_ours(marker) is True


def test_the_build_marker_path_is_where_the_layer_writes_it():
    layer = (IMAGE_DIR / "layer" / "ems-appliance.yaml").read_text(encoding="utf-8")

    assert f"/{image_shape.OS_BUILD_MARKER}" in layer
