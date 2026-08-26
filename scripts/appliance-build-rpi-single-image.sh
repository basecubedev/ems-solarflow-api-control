#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
# Build the single-slot appliance image: one writable root, patched by apt.
#
#   scripts/appliance-build-rpi-single-image.sh --profile rpi3|rpi4|rpi5 [--output DIR]
#                                               [--build-id ID] [--rpi-image-gen DIR]
#
# A thin entry point, not a second implementation. Everything a release is
# signed on -- the source proofs before and after the build, the refusal to
# publish an ambiguous artefact, the NOT RUN discipline -- lives in one script,
# so the two variants cannot come to disagree about it.
#
# What this variant gives up is stated where an owner will read it before
# flashing: docs/appliance/installation.md and
# docs/appliance/adr/single-slot-image-variant.md.
#
# Exit status is the builder's: 0 built, 1 failed, 2 wrong command line, 3 the
# host cannot build. A host that cannot build reports NOT RUN, never a pass.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec "$ROOT/scripts/appliance-build-rpi-ab-image.sh" --variant single "$@"
