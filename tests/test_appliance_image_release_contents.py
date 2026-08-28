# SPDX-License-Identifier: AGPL-3.0-or-later
"""The `.deb` published beside the images, and whether it is the one they carry.

The `manager-package` job builds a package from the checkout. The `image` jobs
bake in the newest *published stable* Manager, fetched from the index. Between
releases those are the same bytes; from the first release onwards they are not,
and the publish step copied the checkout build into the release under a sentence
calling it "the Appliance Manager `.deb` these images carry".

Because the file name comes from `APPLIANCE_VERSION`, the divergent package
usually carries the *same* name as the released one, with a `.sha256` that
contradicts it. An operator following that sentence hand-installs an unsigned
HEAD build over the released package, outside the update path, with no
verification deadline behind it.

The step now decides from the images' own build records instead of assuming, so
the step is executed here against fabricated artefacts.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "appliance-image.yml"

BOARDS = ("rpi4", "rpi5")


def layout_step():
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    found = [
        step["run"]
        for step in jobs["publish"]["steps"]
        if "Lay out the files" in str(step.get("name", ""))
    ]
    assert len(found) == 1, "the layout step was renamed or split"
    return found[0]


def collected(tmp_path, *, baked_digest, baked_version, built_digest, second_record=False):
    root = tmp_path / "collected"
    for board in BOARDS:
        image = root / f"appliance-image-{board}"
        image.mkdir(parents=True)
        stem = f"ems-solarflow-appliance-0.1.0-{board}-arm64"
        (image / f"{stem}.img.xz").write_bytes(b"image")
        (image / f"{stem}.img.xz.sha256").write_text("d  x\n", encoding="utf-8")
        (image / f"{stem}.build-authority.json").write_text("{}", encoding="utf-8")
        (image / "builder-environment.json").write_text("{}", encoding="utf-8")
        (image / f"{stem}.build.json").write_text(json.dumps({
            "appliance_package": "ems-appliance-manager_x_arm64.deb",
            "appliance_package_version": baked_version,
            "appliance_package_sha256": baked_digest,
        }), encoding="utf-8")
        if second_record:
            # What build-deb.sh leaves in the same directory when the image
            # built its own package -- no image fields, and a name that sorts
            # first.
            (image / "ems-appliance-manager_0.1.0_arm64.build.json").write_text(
                json.dumps({"package": "ems-appliance-manager", "version": "0.1.0"}),
                encoding="utf-8",
            )

        gates = root / f"appliance-gates-{board}"
        gates.mkdir(parents=True)
        (gates / "release-gates.log").write_text("RESULT: PASS\n", encoding="utf-8")

    package = root / "appliance-manager-deb"
    package.mkdir(parents=True)
    (package / "ems-appliance-manager_0.1.0_arm64.deb").write_bytes(b"checkout build")
    (package / "ems-appliance-manager_0.1.0_arm64.deb.sha256").write_text(
        f"{built_digest}  ems-appliance-manager_0.1.0_arm64.deb\n", encoding="utf-8"
    )
    (package / "ems-appliance-manager_0.1.0_arm64.build.json").write_text("{}", encoding="utf-8")
    return root


def run_layout(tmp_path, *, baked_digest, baked_version="0.1.0", built_digest=None,
               second_record=False):
    built_digest = baked_digest if built_digest is None else built_digest
    collected(tmp_path, baked_digest=baked_digest, baked_version=baked_version,
              built_digest=built_digest, second_record=second_record)
    env_file = tmp_path / "github_env"
    env_file.write_text("", encoding="utf-8")

    result = subprocess.run(
        ["bash", "-c", layout_step()],
        capture_output=True, text=True, check=False, timeout=120,
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "PROFILES": json.dumps(list(BOARDS)),
            "GITHUB_ENV": str(env_file),
        },
    )
    exported = dict(
        line.split("=", 1)
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return result, sorted(p.name for p in (tmp_path / "release").iterdir()), exported


def test_the_package_is_published_when_it_is_the_one_the_images_carry(tmp_path):
    """The state before the first Manager release, and the only state in which
    that sentence in the notes is true."""

    result, files, exported = run_layout(tmp_path, baked_digest="a" * 64)

    assert result.returncode == 0, result.stderr
    assert "ems-appliance-manager_0.1.0_arm64.deb" in files
    assert exported["MANAGER_BESIDE"] == "yes"


def test_a_checkout_build_is_not_published_beside_images_that_carry_another(tmp_path):
    """Same file name, contradicting checksum, unsigned, and described as the
    package the images carry -- an operator would install it over the released
    one and never learn the difference."""

    result, files, exported = run_layout(
        tmp_path, baked_digest="a" * 64, baked_version="0.3.0", built_digest="b" * 64
    )

    assert result.returncode == 0, result.stderr
    assert not [name for name in files if name.endswith(".deb")]
    assert exported["MANAGER_BESIDE"] == "no"
    assert exported["MANAGER_VERSION"] == "0.3.0"
    assert "0.3.0" in result.stdout


def test_the_build_record_reaches_the_release(tmp_path):
    """It is the only place the Manager an image carries is written down, and
    it was left in the artefact."""

    _, files, _ = run_layout(tmp_path, baked_digest="a" * 64)

    assert [name for name in files if name.endswith(".build.json")]


def test_boards_carrying_different_managers_are_refused(tmp_path):
    """Three images from one run must be one product. Divergence here means the
    index moved mid-run, and publishing the set would ship two answers."""

    collected(tmp_path, baked_digest="a" * 64, baked_version="0.1.0", built_digest="a" * 64)
    odd = tmp_path / "collected" / "appliance-image-rpi5"
    record = next(odd.glob("*.build.json"))
    record.write_text(json.dumps({
        "appliance_package_version": "0.2.0",
        "appliance_package_sha256": "c" * 64,
    }), encoding="utf-8")
    env_file = tmp_path / "github_env"
    env_file.write_text("", encoding="utf-8")

    result = subprocess.run(
        ["bash", "-c", layout_step()],
        capture_output=True, text=True, check=False, timeout=120,
        env={
            **os.environ,
            "RUNNER_TEMP": str(tmp_path),
            "PROFILES": json.dumps(list(BOARDS)),
            "GITHUB_ENV": str(env_file),
        },
    )

    assert result.returncode == 1
    assert "do not carry the same Manager" in result.stdout + result.stderr


def test_the_image_record_is_found_beside_the_packages_own(tmp_path):
    """The state this repository is in, and the one the first version missed.

    With no stable Manager published the image builds its own .deb, and
    build-deb.sh writes ``ems-appliance-manager_<v>_arm64.build.json`` into the
    same directory the artefact glob collects. That name sorts before the
    image's own record and carries none of its fields, so picking the first one
    by name refused every release with "recorded no manager package digest" --
    at the end of a three-hour build.
    """

    result, files, exported = run_layout(
        tmp_path, baked_digest="a" * 64, second_record=True
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert exported["MANAGER_BESIDE"] == "yes"
    published = [name for name in files if name.endswith(".build.json")]
    assert any(name.startswith("ems-solarflow-appliance-") for name in published), published
