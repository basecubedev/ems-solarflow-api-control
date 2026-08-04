#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail-closed probe for the Admin Page Object test-hook contract.

The replacement canary drives two published Admin images with one shared set of
page objects, so both images have to serve the same ``data-testid`` hooks. A
missing hook must fail here, naming the hook and the image, instead of inside a
Playwright locator timeout — and it must never be worked around by teaching the
shared page objects a second markup contract.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "tests" / "e2e" / "admin-test-contract.json"
ADMIN_MARKUP_PATH = "/app/admin/static/index.html"


def load_contract(path=CONTRACT_PATH):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    version = payload.get("version")
    hooks = payload.get("hooks")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("admin test contract needs a positive integer version")
    if not isinstance(hooks, list) or not hooks:
        raise ValueError("admin test contract needs a non-empty hooks array")
    if not all(isinstance(hook, str) and hook for hook in hooks):
        raise ValueError("every admin test contract hook must be a non-empty string")
    if len(set(hooks)) != len(hooks):
        raise ValueError("admin test contract hooks must be unique")
    return version, list(hooks)


def missing_hooks(markup, hooks):
    return [hook for hook in hooks if f'data-testid="{hook}"' not in markup]


def read_admin_markup(image, run=None):
    runner = run or subprocess.run
    result = runner(
        ["docker", "run", "--rm", "--entrypoint", "cat", image, ADMIN_MARKUP_PATH],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"could not read {ADMIN_MARKUP_PATH} from {image}: "
            f"{(result.stderr or '').strip() or result.returncode}"
        )
    return result.stdout


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    args = parser.parse_args(argv)

    version, hooks = load_contract(args.contract)
    if "@sha256:" not in args.image:
        print(
            f"{args.role} Admin image must be digest-pinned, got: {args.image}",
            file=sys.stderr,
        )
        return 1
    try:
        markup = read_admin_markup(args.image)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    absent = missing_hooks(markup, hooks)
    if absent:
        print(
            f"{args.role} Admin image {args.image} does not implement admin test "
            f"contract {version}: missing {', '.join(absent)}.\n"
            "Pick a published Development build whose Admin serves these hooks. "
            "Do not add legacy selectors to the shared page objects.",
            file=sys.stderr,
        )
        return 1
    print(f"{args.role} Admin implements admin test contract {version}: {args.image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
