#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate the Admin image's embedded ``/app/release-resources`` bundle.

Run at Admin image build time (see ``deploy/admin/Dockerfile``) with the SAME
build args the image's OCI labels use, so the embedded ``system-build.json`` /
``resource-manifest.json`` and the image labels describe one paired system build.
The ``--source-root`` is a staged tree whose files already sit at their bundle
paths (``config.template.json``, ``docker-compose.example.yml``,
``install-docker.sh``, ``install-docker.ps1``, ``deploy/docker/``).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from admin.embedded_resources import write_release_resources  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--system-tag", required=True)
    parser.add_argument("--channel", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--build-id", required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--admin-image", required=True)
    parser.add_argument("--ems-image", required=True)
    args = parser.parse_args(argv)
    out = write_release_resources(
        args.output_dir,
        source_root=args.source_root,
        system_tag=args.system_tag,
        channel=args.channel,
        revision=args.revision,
        build_id=args.build_id,
        release_tag=args.release_tag,
        admin_image=args.admin_image,
        ems_image=args.ems_image,
    )
    print(f"wrote embedded release resources to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
