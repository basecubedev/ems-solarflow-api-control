#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pull the Dockerfiles' base images before a CI job builds an image.

    scripts/prepull_base_images.py [--print] [--attempts N] [DOCKERFILE...]

Docker Hub is pulled anonymously here, and an anonymous pull is the part of a
build nobody owns. On 2026-09-12 the token endpoint reset the connection twice
in a row and ``docker build`` died on line 6 of the root Dockerfile with
"failed to resolve source metadata"; the third run passed with no change. A
gate that reports a registry hiccup as a red branch costs a person a manual
re-run and teaches everyone to re-run before reading.

The answer is not to retry the build. A build that is retried three times
turns a broken Dockerfile into a slow flake and hides the failure it should be
shouting about. So the registry work is lifted out into this step, which is
allowed to retry because pulling is the only thing it does, and the build that
follows keeps failing on its first try. ``docker build`` without ``--pull``
resolves a tag that is already in the local image store without contacting the
registry at all, which is what makes the split work: once this step succeeds,
the build has no registry left to fail against.

The images come out of the Dockerfiles rather than a list kept here, so a base
image bump is one edit. A ``FROM`` this cannot resolve -- a build argument in
the reference -- is an error rather than a skip: a pre-pull that silently stops
covering an image looks exactly like one that works.

Exit status: 0 everything is local, 1 a pull did not succeed, 2 the Dockerfiles
or the command line are wrong.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Every Dockerfile this repository builds in CI. tests/test_ci_base_image_prepull.py
# fails when a tracked Dockerfile outside the fixtures is not on this list.
DOCKERFILES = ("Dockerfile", "deploy/admin/Dockerfile")

DEFAULT_ATTEMPTS = 5
BACKOFF_SECONDS = 3

FROM_LINE = re.compile(r"^\s*FROM\s+(?P<rest>.+?)\s*$", re.IGNORECASE)
CONTINUED_LINE = re.compile(r"\\\s*\n")


class UnresolvedBaseImage(Exception):
    """A ``FROM`` reference this cannot turn into something to pull."""


def base_images(dockerfile: Path) -> list[str]:
    """External base images of one Dockerfile, in first-seen order.

    ``scratch`` and references to an earlier stage are not registry reads and
    are left out.
    """

    text = CONTINUED_LINE.sub(" ", dockerfile.read_text(encoding="utf-8"))
    stages: set[str] = set()
    found: list[str] = []

    for line in text.splitlines():
        match = FROM_LINE.match(line)
        if not match:
            continue
        words = match.group("rest").split()
        while words and words[0].startswith("--"):
            words.pop(0)
        if not words:
            raise UnresolvedBaseImage(f"{dockerfile}: FROM without a reference")

        reference = words[0]
        if len(words) >= 3 and words[1].upper() == "AS":
            stages.add(words[2].lower())

        if "$" in reference:
            raise UnresolvedBaseImage(
                f"{dockerfile}: FROM {reference} is built from an argument; "
                "this script cannot know what to pull, and pulling nothing "
                "would leave the build exposed to the registry again"
            )
        if reference.lower() == "scratch" or reference.lower() in stages:
            continue
        if reference not in found:
            found.append(reference)

    return found


def resolve_dockerfiles(names) -> list[Path]:
    paths = []
    for name in names:
        path = Path(name)
        if not path.is_absolute():
            path = ROOT / path
        if not path.is_file():
            raise UnresolvedBaseImage(f"{path}: no such Dockerfile")
        paths.append(path)
    return paths


def collect(dockerfiles) -> list[str]:
    images: list[str] = []
    for dockerfile in dockerfiles:
        for image in base_images(dockerfile):
            if image not in images:
                images.append(image)
    if not images:
        raise UnresolvedBaseImage("no base images were found to pull")
    return images


def pull(image, *, attempts=DEFAULT_ATTEMPTS, run=None, sleep=None, log=print) -> bool:
    """Pull one image, retrying a failure up to ``attempts`` times."""

    run = run or (lambda argv: subprocess.run(argv, check=False).returncode)
    sleep = sleep or time.sleep

    for attempt in range(1, attempts + 1):
        if run(["docker", "pull", image]) == 0:
            return True
        if attempt < attempts:
            delay = attempt * BACKOFF_SECONDS
            log(f"registry pull of {image} failed (attempt {attempt}/{attempts}); "
                f"retrying in {delay}s")
            sleep(delay)
    return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dockerfiles", nargs="*", default=None)
    parser.add_argument("--print", dest="only_print", action="store_true",
                        help="name the base images and pull nothing")
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    args = parser.parse_args(argv)

    try:
        images = collect(resolve_dockerfiles(args.dockerfiles or DOCKERFILES))
    except UnresolvedBaseImage as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.only_print:
        for image in images:
            print(image)
        return 0

    for image in images:
        if not pull(image, attempts=args.attempts):
            print(
                f"error: could not pull the base image {image} after "
                f"{args.attempts} attempts. This is a registry failure, not a "
                "build failure: nothing in this repository was compiled yet.",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
