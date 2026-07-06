#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generate the committed EMS config template from the config catalog."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ems.config_catalog import render_default_template  # noqa: E402

TEMPLATE_PATH = ROOT / "config" / "config.template.json"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the committed template differs from generated output",
    )
    parser.add_argument(
        "--devices",
        type=int,
        default=2,
        help="number of sample devices to generate (default: 2)",
    )
    args = parser.parse_args(argv)

    try:
        generated = render_default_template(args.devices)
    except ValueError as exc:
        parser.error(str(exc))

    if args.check:
        current = (
            TEMPLATE_PATH.read_text(encoding="utf-8")
            if TEMPLATE_PATH.exists()
            else None
        )
        if current != generated:
            print(
                "Generated config template is stale. Run "
                "'python tools/build_config_template.py' and commit "
                "config/config.template.json.",
                file=sys.stderr,
            )
            return 1
        return 0

    TEMPLATE_PATH.write_text(generated, encoding="utf-8")
    print(f"Wrote {TEMPLATE_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
