# SPDX-License-Identifier: AGPL-3.0-or-later
"""How large a medium the appliance actually needs, and where that is written.

The image is about 16.5 GiB. A card marketed as "16 GB" holds roughly 14.8 to
15.9 GiB of addressable bytes, so it cannot hold the image at all — and nothing
said so: no document, no build metadata and no preflight named a minimum.

The requirement is not the image either. The persistent partition carries both
slots' Docker stores, the seed archives an offline reconstruction is rebuilt
from, a staged update, the EMS data and the operator's backups. Those are
measured; the policy that follows from them is 32 GB.
"""

import json
from pathlib import Path

import pytest

from appliance import media_sizing

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "packaging/appliance/image/shared/ems-appliance-ab.yaml"
BUILD_SCRIPT = ROOT / "scripts/appliance-build-rpi-ab-image.sh"
CHECKLIST = ROOT / "docs/appliance/ab-hardware-validation.md"

GIB = 1024 * 1024 * 1024


def test_the_declared_partition_sizes_are_the_ones_the_image_is_built_with():
    """If the profile changes, the sizing answer changes with it."""

    text = PROFILE.read_text(encoding="utf-8")

    assert "boot_part_size: 256M" in text
    assert "system_part_size: 4G" in text
    assert "data_part_size: 8G" in text
    assert media_sizing.BOOT_PARTITION_BYTES == 256 * 1024 * 1024
    assert media_sizing.SYSTEM_PARTITION_BYTES == 4 * GIB
    assert media_sizing.PERSISTENT_PARTITION_BYTES == 8 * GIB


def test_the_image_does_not_fit_a_sixteen_gigabyte_card():
    """The ambiguity the finding names, resolved with the actual numbers."""

    marketed_16gb = 16 * 1000 * 1000 * 1000

    assert media_sizing.IMAGE_BYTES > marketed_16gb
    assert not media_sizing.media_is_supported(marketed_16gb)


def test_a_thirty_two_gigabyte_card_is_supported_even_at_the_low_end():
    """Vendors differ by a few percent; a genuine 32 GB card must pass."""

    assert media_sizing.media_is_supported(30_500_000_000)
    assert media_sizing.media_is_supported(32 * 1000 * 1000 * 1000)


def test_the_measured_requirement_is_what_the_policy_rests_on():
    requirements = media_sizing.requirements()
    persistent = requirements["persistent_requirement"]

    assert persistent["total_bytes"] > media_sizing.PERSISTENT_PARTITION_BYTES
    assert requirements["recommended_media_bytes"] > 16 * 1000 * 1000 * 1000
    assert requirements["recommended_media_bytes"] < media_sizing.MINIMUM_MEDIA_BYTES
    assert requirements["supported_media_label"] == "32 GB"


def test_every_component_of_the_requirement_is_named():
    persistent = media_sizing.requirements()["persistent_requirement"]

    assert set(persistent) == {
        "docker_store_per_slot_bytes",
        "docker_seed_bytes",
        "update_staging_bytes",
        "application_data_bytes",
        "safety_margin_bytes",
        "total_bytes",
    }
    assert persistent["update_staging_bytes"] > media_sizing.SYSTEM_PARTITION_BYTES


def test_the_requirements_survive_json():
    assert json.loads(json.dumps(media_sizing.requirements()))


def test_the_build_metadata_records_the_minimum_medium():
    """An image that cannot say what it needs cannot be flashed safely."""

    text = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "minimum_media_bytes" in text
    assert "media_sizing.MINIMUM_MEDIA_BYTES" in text


def test_the_hardware_checklist_states_the_minimum_medium():
    text = CHECKLIST.read_text(encoding="utf-8")

    assert "32 GB" in text
    assert str(media_sizing.MINIMUM_MEDIA_BYTES) in text or "30,000,000,000" in text
