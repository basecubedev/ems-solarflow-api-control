# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shape an appliance image has, declared once.

The appliance image carries one writable root filesystem and is patched by
``apt``. That is not a setting, and it is not a fact worth restating: it decides
which upstream image layer builds the image, which device the kernel is told to
boot, what the image inspector is entitled to assert, and -- on the running
appliance -- what the first-boot growth helper is allowed to repartition.

Those questions are asked from three different places. Three separate answers
would drift, and the one that drifted would be the one deciding a gate, so they
are declared here and everything else derives from them.

The module imports nothing from the rest of the appliance on purpose: the build
side, the inspector and the booted host all read it.
"""

from dataclasses import dataclass

# The marker the image writes into its own root, and the only thing that
# identifies a host as one this project imaged.
OS_BUILD_MARKER = "etc/ems-appliance-os-build"


@dataclass(frozen=True)
class ImageShape:
    """The image, and every fact that follows from it."""

    image_layer: str
    app_layer: str
    root_device: str
    description: str

    def artifact_suffix(self, board):
        return f"{board}-arm64"

    def to_dict(self):
        return {
            "image_layer": self.image_layer,
            "app_layer": self.app_layer,
            "root_device": self.root_device,
            "description": self.description,
        }


IMAGE = ImageShape(
    image_layer="image-rpios",
    app_layer="ems-appliance",
    root_device="/dev/disk/by-slot/system",
    description=(
        "One writable root filesystem, patched by apt. The operating system is "
        "updated in place; the image itself is only ever written to a card."
    ),
)


def image_layer_matches(name):
    """Whether an upstream image-layer name is the one this image is built from.

    Matching is exact: a layer name that differs only in case is a different
    name to upstream's resolver, so it must be one here too. Anything that is
    not a non-empty string is not a match rather than an error -- the callers
    are gates, and a gate that raises on a missing field fails open on the
    caller that forgot to catch.
    """

    return isinstance(name, str) and bool(name) and name == IMAGE.image_layer


def marker_is_ours(marker):
    """Whether a build marker says its host was imaged by this project.

    Fail closed by construction. A marker written before this field was read,
    one whose field is empty, and one naming an image layer this project does
    not build all answer ``False``, so no absence can turn a gate off -- only a
    positive, recognised statement identifies the image.
    """

    if not isinstance(marker, dict):
        return False
    return image_layer_matches(marker.get("image_layer"))
