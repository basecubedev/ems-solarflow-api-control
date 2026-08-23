# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two shapes an appliance image can have, declared once.

An A/B image carries two slot roots, mounts the booted one read-only, and is
patched by replacing a whole slot. A single-slot image carries one writable
root and is patched by ``apt``. That is not a setting: it decides which
upstream image layer builds the image, which device the kernel is told to boot,
what the image inspector is entitled to assert, and — on the running appliance
— whether the shared persistence contract applies at all.

Those questions are asked from three different places. Three separate answers
would drift, and the one that drifted would be the one deciding a gate, so they
are declared here and everything else derives from them.

The module imports nothing from the rest of the appliance on purpose: the build
side, the inspector and the booted host all read it.
"""

from dataclasses import dataclass

VARIANT_AB = "ab"
VARIANT_SINGLE = "single"


@dataclass(frozen=True)
class ImageVariant:
    """One image shape, and every fact that follows from choosing it."""

    slug: str
    image_layer: str
    app_layer: str
    profile_suffix: str
    root_device: str
    root_readonly: bool
    has_ab_layout: bool
    has_update_archive: bool
    description: str

    def artifact_suffix(self, board):
        return f"{board}-arm64-{self.slug}"

    def to_dict(self):
        return {
            "slug": self.slug,
            "image_layer": self.image_layer,
            "app_layer": self.app_layer,
            "profile_suffix": self.profile_suffix,
            "root_device": self.root_device,
            "root_readonly": self.root_readonly,
            "has_ab_layout": self.has_ab_layout,
            "has_update_archive": self.has_update_archive,
            "description": self.description,
        }


VARIANTS = {
    VARIANT_AB: ImageVariant(
        slug=VARIANT_AB,
        image_layer="image-rota",
        app_layer="ems-appliance",
        profile_suffix="ab",
        root_device="/dev/disk/by-slot/active/system",
        root_readonly=True,
        has_ab_layout=True,
        has_update_archive=True,
        description=(
            "Two boot slots, a read-only slot root and a shared persistent "
            "partition. OS updates replace the inactive slot and commit only "
            "after a trial boot proves itself."
        ),
    ),
    VARIANT_SINGLE: ImageVariant(
        slug=VARIANT_SINGLE,
        image_layer="image-rpios",
        app_layer="ems-appliance-single",
        profile_suffix="single",
        root_device="/dev/disk/by-slot/system",
        root_readonly=False,
        has_ab_layout=False,
        has_update_archive=False,
        description=(
            "One writable root filesystem, patched by apt. No slot to fall "
            "back to, and no signed image needed to patch the OS."
        ),
    ),
}


def variant(slug):
    """The variant a slug names, or ``KeyError``.

    Deliberately not a ``get``: a caller holding a slug this table does not
    know has a bug, and guessing which of two images it meant is how a gate
    ends up applied to the wrong one.
    """

    return VARIANTS[slug]


def variant_of_image_layer(name):
    """Which variant an upstream image-layer name belongs to, or ``None``.

    ``None`` means "this does not identify a variant" and every caller must
    treat it as such. Matching is exact: a layer name that differs only in case
    is a different name to upstream's resolver, so it must be one here too.
    """

    if not isinstance(name, str) or not name:
        return None
    for candidate in VARIANTS.values():
        if candidate.image_layer == name:
            return candidate
    return None


def variant_of_build_marker(marker):
    """Which variant a build marker says its image is, or ``None``.

    Fail closed by construction. A marker written before this field was read,
    one whose field is empty, and one naming an image layer this table does not
    know all answer ``None``, so no absence can turn a gate off — only a
    positive, recognised statement identifies a variant.
    """

    if not isinstance(marker, dict):
        return None
    return variant_of_image_layer(marker.get("image_layer"))
