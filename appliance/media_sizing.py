# SPDX-License-Identifier: AGPL-3.0-or-later
"""How large a medium an appliance actually needs.

The image is not the requirement. It is the floor: a 256 MiB boot partition and
an 8 GiB root come to roughly 8.25 GiB before anything has run.

What the medium has to hold afterwards is the part that decides the answer. The
root carries the Docker store the seed archive is unpacked into, the EMS data
and the operator's backups. Those are measured, not guessed: see
``reports/appliance/*/media-sizing.json`` for the run that produced the numbers
below.

The image is grown to fill the medium on first boot, which is the one partition
change this project makes.
"""

MIB = 1024 * 1024
GIB = 1024 * MIB

# What the profile declares. packaging/appliance/image/shared/ems-appliance.yaml
# is the source of these, and tests/test_appliance_media_sizing.py keeps them equal.
BOOT_PARTITION_BYTES = 256 * MIB
ROOT_PARTITION_BYTES = 8 * GIB

IMAGE_BYTES = BOOT_PARTITION_BYTES + ROOT_PARTITION_BYTES

# Measured on 2026-08-09 from the images this project publishes: the Admin and
# EMS images unpacked into a Docker store, plus Influx.
DOCKER_STORE_BYTES = 900 * MIB
# EMS configuration, the dashboard's SQLite history, Influx data and the
# operator's backups.
APPLICATION_DATA_BYTES = 2 * GIB
SAFETY_MARGIN_BYTES = 1 * GIB

# What the root has to hold beyond the image it was flashed from. The seed
# archive and the OS are already inside those 8 GiB; the store the seed is
# unpacked into, the application data and the margin are not.
ROOT_GROWTH_BYTES = DOCKER_STORE_BYTES + APPLICATION_DATA_BYTES + SAFETY_MARGIN_BYTES

RECOMMENDED_MEDIA_BYTES = IMAGE_BYTES + ROOT_GROWTH_BYTES

# The policy. Media are marketed in decimal gigabytes and vendors differ by a
# few percent, so the floor sits below the nominal figure: a genuine 16 GB card
# must pass.
SUPPORTED_MEDIA_LABEL = "16 GB"
MINIMUM_MEDIA_BYTES = 14_500_000_000

UNSUPPORTED_MEDIA_REASON = (
    "the appliance image is about 8.25 GiB and has to grow to hold a Docker "
    "store, the EMS data and the operator's backups"
)


def media_is_supported(byte_count):
    """Is this medium large enough?"""

    return int(byte_count) >= MINIMUM_MEDIA_BYTES


def requirements():
    """The measured numbers behind the policy, for a report or a checklist."""

    return {
        "image_bytes": IMAGE_BYTES,
        "partitions": {
            "boot_bytes": BOOT_PARTITION_BYTES,
            "root_initial_bytes": ROOT_PARTITION_BYTES,
        },
        "root_growth": {
            "docker_store_bytes": DOCKER_STORE_BYTES,
            "application_data_bytes": APPLICATION_DATA_BYTES,
            "safety_margin_bytes": SAFETY_MARGIN_BYTES,
            "total_bytes": ROOT_GROWTH_BYTES,
        },
        "recommended_media_bytes": RECOMMENDED_MEDIA_BYTES,
        "minimum_media_bytes": MINIMUM_MEDIA_BYTES,
        "supported_media_label": SUPPORTED_MEDIA_LABEL,
        "unsupported_media_reason": UNSUPPORTED_MEDIA_REASON,
    }
