# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two image variants, and what is allowed to tell them apart.

The build side, the image inspector and the booted appliance each have to
answer "which variant is this". Three answers would drift, and the one that
drifted would be the one deciding a safety gate — so they read one table, and
this pins what that table says.
"""

import pytest

from appliance import image_variants

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]


def test_exactly_two_variants_exist():
    assert set(image_variants.VARIANTS) == {"ab", "single"}


def test_the_ab_variant_still_says_what_the_code_said_before_the_table():
    """The A/B image is unchanged by the arrival of a second variant.

    Every value here was a literal somewhere else first. If the table
    disagrees with any of them, the refactor moved the A/B image.
    """

    variant = image_variants.variant("ab")
    assert variant.image_layer == "image-rota"
    assert variant.app_layer == "ems-appliance"
    assert variant.root_device == "/dev/disk/by-slot/active/system"
    assert variant.root_readonly is True
    assert variant.has_ab_layout is True
    assert variant.has_update_archive is True
    assert variant.artifact_suffix("rpi5") == "rpi5-arm64-ab"


def test_the_single_slot_variant_is_the_writable_one():
    variant = image_variants.variant("single")
    assert variant.image_layer == "image-rpios"
    assert variant.app_layer == "ems-appliance-single"
    assert variant.root_device == "/dev/disk/by-slot/system"
    assert variant.root_readonly is False
    assert variant.has_ab_layout is False
    assert variant.has_update_archive is False
    assert variant.artifact_suffix("rpi4") == "rpi4-arm64-single"


def test_the_two_variants_share_no_identity():
    """Nothing that names a variant may name both."""

    variants = [image_variants.variant(slug) for slug in image_variants.VARIANTS]
    for field in ("slug", "image_layer", "app_layer", "profile_suffix"):
        values = [getattr(variant, field) for variant in variants]
        assert len(set(values)) == len(values), field


def test_an_unknown_slug_is_refused_rather_than_guessed():
    with pytest.raises(KeyError):
        image_variants.variant("rota")


def test_an_image_layer_resolves_back_to_its_variant():
    assert image_variants.variant_of_image_layer("image-rota").slug == "ab"
    assert image_variants.variant_of_image_layer("image-rpios").slug == "single"


def test_an_unrecognised_image_layer_identifies_nothing():
    """Fail closed: only a positive, known statement identifies a variant."""

    for name in ("", None, "image-unknown", "IMAGE-ROTA", 17):
        assert image_variants.variant_of_image_layer(name) is None


def test_a_build_marker_identifies_its_variant():
    marker = {"build_id": "b1", "image_layer": "image-rpios"}
    assert image_variants.variant_of_build_marker(marker).slug == "single"


def test_a_marker_that_does_not_say_identifies_nothing():
    """A marker written before this field existed must not answer for it."""

    for marker in ({}, {"build_id": "b1"}, {"image_layer": ""}, None, "image-rpios"):
        assert image_variants.variant_of_build_marker(marker) is None
