# SPDX-License-Identifier: AGPL-3.0-or-later
"""How large a medium an appliance actually needs.

The image is not the requirement. It is the floor: a 256 MiB boot partition and
an 8 GiB root come to roughly 8.25 GiB before anything has run.

What the medium has to hold afterwards is the part that decides the answer. The
single-slot image carries no seed archive: the Admin, EMS and InfluxDB images
are downloaded on the first install, containerd keeps both the compressed blobs
and the unpacked snapshots, and an update keeps the previous Admin and EMS
images for going back. The root also carries the EMS data and the operator's
backups.

Measured on 2026-11-04 on the Raspberry Pi 3B+ test appliance, read only, with
Manager 0.4.1, the Admin, EMS and InfluxDB running and several updates behind
it: the three running images take 1.33 GB of the store, images earlier updates
left behind another 2.3 GB, the EMS data 0.44 GB after two months, the journal
76 MB and apt's cache 235 MB -- 5.8 GB of the root in use in all. The image's
8 GiB root is mostly empty space, so counting it whole keeps the sum below
conservative.

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

# The running Admin, EMS and InfluxDB images (1.33 GB measured) and the previous
# Admin and EMS pair an update keeps for going back.
DOCKER_STORE_BYTES = 2 * GIB + 256 * MIB
# EMS configuration, the dashboard's SQLite history, Influx data and the
# operator's backups.
APPLICATION_DATA_BYTES = 2 * GIB
SAFETY_MARGIN_BYTES = 1 * GIB

# What the root has to hold beyond the image it was flashed from. The OS is
# already inside those 8 GiB; the downloaded images, the application data and
# the margin are not.
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
