# SPDX-License-Identifier: AGPL-3.0-or-later
"""How large a medium the appliance actually needs.

The image is the floor, not the requirement: what the root has to hold once the
appliance is running is what decides the answer. The numbers here are the ones a
release attestation carries and an installation checklist prints, so they are
derived from the profile rather than typed out twice.
"""

from pathlib import Path

import pytest

from appliance import media_sizing

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.appliance]

SHARED = (
    Path(__file__).resolve().parents[1]
    / "packaging" / "appliance" / "image" / "shared" / "ems-appliance.yaml"
)


def test_the_partition_sizes_are_the_ones_the_profile_declares():
    """A number that drifts from the profile sizes an image nobody builds."""

    text = SHARED.read_text(encoding="utf-8")

    assert f"boot_part_size: {media_sizing.BOOT_PARTITION_BYTES // media_sizing.MIB}M" in text
    assert f"root_part_size: {media_sizing.ROOT_PARTITION_BYTES // media_sizing.GIB}G" in text


def test_the_image_is_the_two_partitions_and_nothing_else():
    assert media_sizing.IMAGE_BYTES == (
        media_sizing.BOOT_PARTITION_BYTES + media_sizing.ROOT_PARTITION_BYTES
    )


def test_the_recommendation_is_the_image_plus_what_the_root_grows_to_hold():
    assert media_sizing.RECOMMENDED_MEDIA_BYTES == (
        media_sizing.IMAGE_BYTES + media_sizing.ROOT_GROWTH_BYTES
    )
    assert media_sizing.ROOT_GROWTH_BYTES == (
        media_sizing.DOCKER_STORE_BYTES
        + media_sizing.APPLICATION_DATA_BYTES
        + media_sizing.SAFETY_MARGIN_BYTES
    )


def test_the_floor_sits_below_the_nominal_figure_so_a_genuine_card_passes():
    """Media are marketed in decimal gigabytes and vendors differ by a percent
    or two. A floor equal to the nominal figure refuses cards that are exactly
    what they say they are."""

    nominal = 16_000_000_000

    assert media_sizing.MINIMUM_MEDIA_BYTES < nominal
    assert media_sizing.media_is_supported(nominal) is True


def test_a_card_below_the_floor_is_not_supported():
    assert media_sizing.media_is_supported(media_sizing.MINIMUM_MEDIA_BYTES - 1) is False
    assert media_sizing.media_is_supported(8_000_000_000) is False


def test_the_image_itself_fits_inside_the_floor():
    """Otherwise the flash truncates, or fails, on a card the policy allows."""

    assert media_sizing.IMAGE_BYTES < media_sizing.MINIMUM_MEDIA_BYTES


def test_the_requirements_report_names_every_number_behind_the_policy():
    report = media_sizing.requirements()

    assert report["minimum_media_bytes"] == media_sizing.MINIMUM_MEDIA_BYTES
    assert report["recommended_media_bytes"] == media_sizing.RECOMMENDED_MEDIA_BYTES
    assert report["supported_media_label"] == media_sizing.SUPPORTED_MEDIA_LABEL
    assert report["image_bytes"] == media_sizing.IMAGE_BYTES
    assert report["partitions"]["boot_bytes"] == media_sizing.BOOT_PARTITION_BYTES
    assert report["partitions"]["root_initial_bytes"] == media_sizing.ROOT_PARTITION_BYTES
    assert report["root_growth"]["total_bytes"] == media_sizing.ROOT_GROWTH_BYTES


def test_the_unsupported_reason_says_what_the_number_is_about():
    reason = media_sizing.UNSUPPORTED_MEDIA_REASON

    assert "8.25" in reason
    assert "grow" in reason
