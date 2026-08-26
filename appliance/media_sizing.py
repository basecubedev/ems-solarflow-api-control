# SPDX-License-Identifier: AGPL-3.0-or-later
"""How large a medium an appliance actually needs, by image shape.

The image is not the requirement. It is the floor: two 4 GiB slot roots, two
256 MiB boot partitions, a bootconfig partition and an 8 GiB persistent
partition come to roughly 16.5 GiB before anything has run. A card marketed as
"16 GB" holds about 14.8-15.9 GiB of addressable bytes, so it cannot hold the
image at all — the flash either fails or, worse, truncates.

What the medium has to hold afterwards is the part that decides the answer. The
persistent partition carries both slots' /var (each with its own Docker store),
the Docker seed archives an offline reconstruction is rebuilt from, a staged OS
update, the EMS data and the operator's backups. Those are measured, not
guessed: see ``reports/appliance/*/media-sizing.json`` for the run that produced
the numbers below.

The conclusion is Policy A from the task: 32 GB is the smallest supported
medium. The image is then grown to fill it on first boot, which is the one
partition change this project makes.

That number is about the A/B shape and does not carry over. A single-slot image
has one root instead of two, one Docker store instead of two, no persistent
partition beside them and no staged update to hold -- it is patched by apt in
place. Its floor is derived from the same measured components rather than
measured again: what changed is how many of each there are.
"""

from appliance import image_variants

MIB = 1024 * 1024
GIB = 1024 * MIB

# What the profile declares. packaging/appliance/image/shared/ems-appliance-ab.yaml
# is the source of these, and tests/test_appliance_media_sizing.py keeps them equal.
BOOT_PARTITION_BYTES = 256 * MIB
SYSTEM_PARTITION_BYTES = 4 * GIB
PERSISTENT_PARTITION_BYTES = 8 * GIB
# image-rota's own first partition. Small and fixed; it holds autoboot.txt.
BOOTCONFIG_PARTITION_BYTES = 32 * MIB

IMAGE_BYTES = (
    BOOTCONFIG_PARTITION_BYTES
    + 2 * BOOT_PARTITION_BYTES
    + 2 * SYSTEM_PARTITION_BYTES
    + PERSISTENT_PARTITION_BYTES
)

# Measured on 2026-08-09 from the images this project publishes: `docker save`
# of the Admin and EMS images is 340 MiB, the Influx image 366 MiB.
DOCKER_SEED_BYTES = 706 * MIB
# The same images unpacked into a Docker store, per slot. Both slots keep their
# own /var through image-rota's slot-perst policy, so this is paid twice.
DOCKER_STORE_BYTES = 900 * MIB
# An update stages its two members before writing them: a boot member and a
# system member, the second of which is the whole 4 GiB root as a sparse
# container, plus the compressed archive it came out of.
UPDATE_STAGING_BYTES = SYSTEM_PARTITION_BYTES + BOOT_PARTITION_BYTES + 1500 * MIB
# EMS configuration, the dashboard's SQLite history, Influx data and the
# operator's backups.
APPLICATION_DATA_BYTES = 2 * GIB
SAFETY_MARGIN_BYTES = 1 * GIB

PERSISTENT_REQUIREMENT_BYTES = (
    2 * DOCKER_STORE_BYTES
    + DOCKER_SEED_BYTES
    + UPDATE_STAGING_BYTES
    + APPLICATION_DATA_BYTES
    + SAFETY_MARGIN_BYTES
)

RECOMMENDED_MEDIA_BYTES = (
    IMAGE_BYTES - PERSISTENT_PARTITION_BYTES + PERSISTENT_REQUIREMENT_BYTES
)

# The policy, and the number a preflight enforces. Media are marketed in
# decimal gigabytes and vendors differ by a few percent, so the floor is below
# the nominal 32 GB rather than equal to it: a genuine 32 GB card must pass.
SUPPORTED_MEDIA_LABEL = "32 GB"
MINIMUM_MEDIA_BYTES = 30_000_000_000

# Why 16 GB is not on the list, in the form an installer can print.
UNSUPPORTED_MEDIA_REASON = (
    "the appliance image is about 16.5 GiB and a card marketed as 16 GB holds "
    "less than that"
)


# --- the single-slot shape ---------------------------------------------------

# packaging/appliance/image/shared/ems-appliance-single.yaml is the source of
# these, and tests/test_appliance_media_sizing.py keeps them equal.
SINGLE_BOOT_PARTITION_BYTES = 256 * MIB
SINGLE_ROOT_PARTITION_BYTES = 8 * GIB

SINGLE_IMAGE_BYTES = SINGLE_BOOT_PARTITION_BYTES + SINGLE_ROOT_PARTITION_BYTES

# What the root has to hold beyond the image it was flashed from. The seed
# archive and the OS are already inside those 8 GiB; the store the seed is
# unpacked into, the application data and the margin are not. There is no
# update staging term at all -- a single-slot appliance has no second slot to
# write and no signed image to stage.
SINGLE_ROOT_GROWTH_BYTES = DOCKER_STORE_BYTES + APPLICATION_DATA_BYTES + SAFETY_MARGIN_BYTES

SINGLE_RECOMMENDED_MEDIA_BYTES = SINGLE_IMAGE_BYTES + SINGLE_ROOT_GROWTH_BYTES

# Media are marketed in decimal gigabytes and vendors differ by a few percent,
# so the floor sits below the nominal figure: a genuine 16 GB card must pass.
SINGLE_SUPPORTED_MEDIA_LABEL = "16 GB"
SINGLE_MINIMUM_MEDIA_BYTES = 14_500_000_000

SINGLE_UNSUPPORTED_MEDIA_REASON = (
    "the single-slot image is about 8.25 GiB and has to grow to hold a Docker "
    "store, the EMS data and the operator's backups"
)

MINIMUM_BY_VARIANT = {
    image_variants.VARIANT_AB: MINIMUM_MEDIA_BYTES,
    image_variants.VARIANT_SINGLE: SINGLE_MINIMUM_MEDIA_BYTES,
}


def media_is_supported(byte_count, *, variant=image_variants.VARIANT_AB):
    """Is this medium large enough for an image of this shape?

    The A/B answer is the default because it is the stricter one: a caller that
    does not know which image it is sizing for must not be told the smaller
    number.
    """

    floor = MINIMUM_BY_VARIANT.get(str(variant))
    if floor is None:
        raise ValueError(f"{variant!r} is not an image variant this project builds")
    return int(byte_count) >= floor


def requirements():
    """The measured numbers behind the policy, for a report or a checklist."""

    return {
        "image_bytes": IMAGE_BYTES,
        "partitions": {
            "bootconfig_bytes": BOOTCONFIG_PARTITION_BYTES,
            "boot_bytes": BOOT_PARTITION_BYTES,
            "system_bytes": SYSTEM_PARTITION_BYTES,
            "persistent_initial_bytes": PERSISTENT_PARTITION_BYTES,
        },
        "persistent_requirement": {
            "docker_store_per_slot_bytes": DOCKER_STORE_BYTES,
            "docker_seed_bytes": DOCKER_SEED_BYTES,
            "update_staging_bytes": UPDATE_STAGING_BYTES,
            "application_data_bytes": APPLICATION_DATA_BYTES,
            "safety_margin_bytes": SAFETY_MARGIN_BYTES,
            "total_bytes": PERSISTENT_REQUIREMENT_BYTES,
        },
        "recommended_media_bytes": RECOMMENDED_MEDIA_BYTES,
        "minimum_media_bytes": MINIMUM_MEDIA_BYTES,
        "supported_media_label": SUPPORTED_MEDIA_LABEL,
        "unsupported_media_reason": UNSUPPORTED_MEDIA_REASON,
        # The top-level numbers stay the A/B ones the release evidence has
        # always carried. The shapes differ by more than a constant, so both
        # are named rather than one being derived from the other by a reader.
        "by_variant": {
            image_variants.VARIANT_AB: {
                "image_bytes": IMAGE_BYTES,
                "recommended_media_bytes": RECOMMENDED_MEDIA_BYTES,
                "minimum_media_bytes": MINIMUM_MEDIA_BYTES,
                "supported_media_label": SUPPORTED_MEDIA_LABEL,
                "unsupported_media_reason": UNSUPPORTED_MEDIA_REASON,
            },
            image_variants.VARIANT_SINGLE: {
                "image_bytes": SINGLE_IMAGE_BYTES,
                "root_growth_bytes": SINGLE_ROOT_GROWTH_BYTES,
                "recommended_media_bytes": SINGLE_RECOMMENDED_MEDIA_BYTES,
                "minimum_media_bytes": SINGLE_MINIMUM_MEDIA_BYTES,
                "supported_media_label": SINGLE_SUPPORTED_MEDIA_LABEL,
                "unsupported_media_reason": SINGLE_UNSUPPORTED_MEDIA_REASON,
            },
        },
    }
