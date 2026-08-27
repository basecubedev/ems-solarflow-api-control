# SPDX-License-Identifier: AGPL-3.0-or-later
"""What has to be true about a builder and its output before anything is signed.

Three separate claims were asserted rather than proven, and each of them is the
difference between a signed release and a signed guess:

- a pinned *git* source proved only that ``.git/HEAD`` contained the right forty
  characters. A plain text file with those characters, a tree with uncommitted
  edits, a tree with a staged change, a tree carrying untracked files that
  shadow build inputs, and a tree whose ``.git`` does not hold the pinned commit
  object at all were each accepted as the pinned upstream.

- a *tarball* source was verified once, before extraction, and then trusted for
  the rest of the build. Everything after ``tar -x`` — including the layers and
  configs the image is made of — could be edited with the identity record left
  intact.

- the update wrapper wrote ``rpi_image_gen_revision`` from the lock, for any
  structurally valid archive handed to it. An artefact nobody built with the
  pinned generator could therefore be signed as if it had been.

The fix is one shape used three times: prove the tree immediately before the
step that consumes it, and never let metadata stand in for the bytes.
"""

import json
import os
import shutil
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from appliance import build_authority, rpi_image_gen

pytestmark = [pytest.mark.integration, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

requires_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is required to prove a git source tree"
)
requires_zstd = pytest.mark.skipif(
    shutil.which("zstd") is None or shutil.which("tar") is None,
    reason="zstd and tar are required to build an update artefact",
)


@pytest.fixture
def lock():
    """The shipped lock without its tree pin.

    These tests build synthetic trees, whose digest cannot be the pinned one.
    The pin is exercised against the real tree in the upstream tier.
    """

    from dataclasses import replace

    return replace(rpi_image_gen.read_lock(), tree_sha256="")


# --- a source tree that satisfies everything except its identity --------------


def write(root, relative, text, *, mode=0o644):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def source_tree(tmp_path, lock, *, name="rpi-image-gen"):
    """Everything the pinned contract requires, with no identity written yet."""

    root = tmp_path / name
    write(root, lock.executable, "#!/bin/bash\n", mode=0o755)
    write(root, "LICENSE", "upstream\n")
    write(root, lock.host_dependencies_file, "all:bash\nbuild:mmdebstrap\n")
    pinned = lock.image_layer
    write(
        root,
        pinned.path,
        "# METABEGIN\n"
        f"# X-Env-Layer-Name: {pinned.name}\n"
        f"# X-Env-Layer-Version: {pinned.version}\n"
        "# METAEND\n",
    )
    for relative in lock.required_paths:
        target = root / relative
        if not target.exists():
            write(root, relative, "#!/bin/sh\n", mode=0o755)
    write(root, "site/config_loader.py", "class ConfigLoader:\n    pass\n")
    return root


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"},
    )


def git_source(tmp_path, lock, **kwargs):
    """A real repository at a real commit, and the lock that pins that commit."""

    root = source_tree(tmp_path, lock, **kwargs)
    git(root, "init", "--quiet", "-b", "main")
    git(root, "config", "user.email", "builder@example.invalid")
    git(root, "config", "user.name", "builder")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "pinned release")
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    return root, replace(lock, commit=head)


def tarball_source(tmp_path, lock, **kwargs):
    """An extracted release tarball, recorded the way the fetch script records one."""

    root = source_tree(tmp_path, lock, **kwargs)
    rpi_image_gen.write_source_identity(
        root,
        form=rpi_image_gen.SOURCE_TARBALL,
        release=lock.release,
        commit=lock.commit,
        url=lock.tarball["url"],
        sha256=lock.tarball["sha256"],
        top_level_directory=lock.tarball["top_level_directory"],
    )
    return root


def satisfied(_binary):
    return "/usr/bin/stub"


def probe(root, lock):
    return rpi_image_gen.probe_checkout(root, lock, which=satisfied, package_query=lambda _p: True)


def source_finding(report):
    return next(
        finding for finding in report.findings if finding.check == "source_identity"
    )


# --- finding 3: a pinned git source is an object-backed, clean tree ------------


@requires_git
def test_a_clean_pinned_git_checkout_passes(tmp_path, lock):
    root, pinned = git_source(tmp_path, lock)

    report = probe(root, pinned)

    assert report.compatible, [finding.to_dict() for finding in report.findings]
    assert report.source_identity == rpi_image_gen.SOURCE_GIT
    assert report.revision == pinned.commit


@requires_git
def test_a_modified_tracked_file_is_refused(tmp_path, lock):
    root, pinned = git_source(tmp_path, lock)
    (root / "config/trixie-minbase.yaml").write_text("image:\n  layer: other\n")

    report = probe(root, pinned)

    assert not report.compatible
    assert report.reason == rpi_image_gen.REASON_SOURCE_UNVERIFIED
    assert "modified" in source_finding(report).detail


@requires_git
def test_a_staged_change_is_refused(tmp_path, lock):
    root, pinned = git_source(tmp_path, lock)
    (root / "config/trixie-minbase.yaml").write_text("image:\n  layer: other\n")
    git(root, "add", "config/trixie-minbase.yaml")

    report = probe(root, pinned)

    assert not report.compatible
    assert report.reason == rpi_image_gen.REASON_SOURCE_UNVERIFIED


@requires_git
def test_a_missing_commit_object_is_refused(tmp_path, lock):
    root, _pinned = git_source(tmp_path, lock)

    report = probe(root, replace(lock, commit="0" * 40))

    assert not report.compatible
    assert report.reason == rpi_image_gen.REASON_SOURCE_UNVERIFIED


def test_a_hand_written_git_head_is_not_a_git_source(tmp_path, lock):
    """The whole of the old authority: forty characters in a text file."""

    root = source_tree(tmp_path, lock)
    write(root, ".git/HEAD", f"{lock.commit}\n")

    report = probe(root, lock)

    assert not report.compatible
    assert report.reason == rpi_image_gen.REASON_SOURCE_UNVERIFIED
    assert report.source_identity == rpi_image_gen.SOURCE_UNVERIFIED


@requires_git
@pytest.mark.parametrize(
    "relative",
    ["config/extra.yaml", "layer/extra.yaml", "image/extra.sh", "device/extra.yaml", "bin/extra"],
)
def test_an_untracked_build_input_is_refused(tmp_path, lock, relative):
    root, pinned = git_source(tmp_path, lock)
    write(root, relative, "smuggled\n")

    report = probe(root, pinned)

    assert not report.compatible
    assert "untracked" in source_finding(report).detail


@requires_git
def test_an_untracked_file_outside_the_build_inputs_is_tolerated(tmp_path, lock):
    root, pinned = git_source(tmp_path, lock)
    write(root, "notes.txt", "a build operator's scratch file\n")

    report = probe(root, pinned)

    assert report.compatible, [finding.to_dict() for finding in report.findings]


@requires_git
def test_git_being_unavailable_is_a_refusal_and_not_a_pass(tmp_path, lock):
    root, pinned = git_source(tmp_path, lock)

    report = rpi_image_gen.probe_checkout(
        root, pinned, which=satisfied, package_query=lambda _p: True, runner=_NoGitRunner()
    )

    assert not report.compatible
    assert report.reason == rpi_image_gen.REASON_SOURCE_UNVERIFIED


class _NoGitRunner:
    def available(self, tool):
        return tool != "git"

    def run(self, tool, args=(), **kwargs):
        raise AssertionError("git must not be run when it is unavailable")


# --- finding 4: a verified tarball stays verified -----------------------------


def test_an_extracted_tarball_records_its_tree(tmp_path, lock):
    root = tarball_source(tmp_path, lock)

    report = probe(root, lock)

    assert report.compatible, [finding.to_dict() for finding in report.findings]
    assert report.source_identity == rpi_image_gen.SOURCE_TARBALL
    assert report.tree_digest.startswith("sha256:")


def test_a_build_input_edited_after_extraction_is_refused(tmp_path, lock):
    root = tarball_source(tmp_path, lock)
    (root / "config/trixie-minbase.yaml").write_text("image:\n  layer: other\n")

    report = probe(root, lock)

    assert not report.compatible
    assert report.reason == rpi_image_gen.REASON_SOURCE_MODIFIED


def test_a_file_added_after_extraction_is_refused(tmp_path, lock):
    root = tarball_source(tmp_path, lock)
    write(root, "layer/smuggled.yaml", "layer: smuggled\n")

    report = probe(root, lock)

    assert report.reason == rpi_image_gen.REASON_SOURCE_MODIFIED


def test_a_file_removed_after_extraction_is_refused(tmp_path, lock):
    root = tarball_source(tmp_path, lock)
    (root / "config/trixie-minbase.yaml").unlink()

    report = probe(root, lock)

    assert report.reason == rpi_image_gen.REASON_SOURCE_MODIFIED


def test_an_executable_bit_changed_after_extraction_is_refused(tmp_path, lock):
    root = tarball_source(tmp_path, lock)
    (root / lock.executable).chmod(0o644)

    report = probe(root, lock)

    assert report.reason == rpi_image_gen.REASON_SOURCE_MODIFIED


def test_a_file_replaced_by_a_symlink_after_extraction_is_refused(tmp_path, lock):
    root = tarball_source(tmp_path, lock)
    target = root / "config/trixie-minbase.yaml"
    elsewhere = tmp_path / "elsewhere.yaml"
    elsewhere.write_text(target.read_text(), encoding="utf-8")
    target.unlink()
    target.symlink_to(elsewhere)

    report = probe(root, lock)

    assert report.reason == rpi_image_gen.REASON_SOURCE_MODIFIED


def test_a_tarball_record_without_a_tree_hash_cannot_prove_itself(tmp_path, lock):
    """The old record shape: verified once, unprovable ever after."""

    root = source_tree(tmp_path, lock)
    (root / rpi_image_gen.SOURCE_IDENTITY_NAME).write_text(
        json.dumps(
            {
                "form": "tarball",
                "release": lock.release,
                "commit": lock.commit,
                "url": lock.tarball["url"],
                "sha256": lock.tarball["sha256"],
                "top_level_directory": lock.tarball["top_level_directory"],
            }
        ),
        encoding="utf-8",
    )

    report = probe(root, lock)

    assert not report.compatible
    assert report.reason in (
        rpi_image_gen.REASON_SOURCE_UNVERIFIED,
        rpi_image_gen.REASON_SOURCE_MODIFIED,
    )


def test_running_the_upstream_tooling_does_not_invalidate_the_tree(tmp_path, lock):
    """Upstream's own loader writes __pycache__ into site/ the first time it runs.

    A bytecode cache is not a build input, and an authority that counted one
    would refuse the second build on a tree the first build was fine with.
    """

    root = tarball_source(tmp_path, lock)
    cache = root / "site" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "config_loader.cpython-313.pyc").write_bytes(b"\x00compiled")
    (root / "site" / "layer_manager.pyc").write_bytes(b"\x00compiled")

    report = probe(root, lock)

    assert report.compatible, [finding.to_dict() for finding in report.findings]


def test_the_tree_manifest_records_modes_and_symlink_targets(tmp_path, lock):
    root = source_tree(tmp_path, lock)
    (root / "link").symlink_to("LICENSE")

    entries = {entry["path"]: entry for entry in rpi_image_gen.tree_manifest(root)}

    assert entries["link"]["type"] == "symlink"
    assert entries["link"]["target"] == "LICENSE"
    assert entries[lock.executable]["mode"] & 0o111
    assert entries["LICENSE"]["sha256"].startswith("sha256:")
    assert entries["LICENSE"]["mode"] & 0o111 == 0


# --- phase 31: the tree is proven immediately before the build ----------------


def test_the_build_wrapper_revalidates_the_source_before_invoking_it(tmp_path, lock):
    root = tarball_source(tmp_path, lock)
    assert rpi_image_gen.assert_buildable(root, lock, which=satisfied) is not None
    (root / "config/trixie-minbase.yaml").write_text("image:\n  layer: other\n")

    with pytest.raises(rpi_image_gen.ImageGenError) as excinfo:
        rpi_image_gen.assert_buildable(root, lock, which=satisfied)

    assert excinfo.value.code == rpi_image_gen.REASON_SOURCE_MODIFIED


@requires_git
def test_the_build_wrapper_revalidates_a_git_source_before_invoking_it(tmp_path, lock):
    root, pinned = git_source(tmp_path, lock)
    rpi_image_gen.assert_buildable(root, pinned, which=satisfied)
    write(root, "layer/smuggled.yaml", "layer: smuggled\n")

    with pytest.raises(rpi_image_gen.ImageGenError) as excinfo:
        rpi_image_gen.assert_buildable(root, pinned, which=satisfied)

    assert excinfo.value.code == rpi_image_gen.REASON_SOURCE_UNVERIFIED


def test_the_check_script_reports_a_modified_tarball_tree_as_a_failure(tmp_path, lock):
    root = tarball_source(tmp_path, lock)
    (root / "config/trixie-minbase.yaml").write_text("image:\n  layer: other\n")

    result = subprocess.run(
        ["sh", str(SCRIPTS / "appliance-check-rpi-image-gen.sh"), "--rpi-image-gen", str(root)],
        capture_output=True,
        text=True,
        timeout=300,
    )

    output = result.stdout + result.stderr

    assert result.returncode == 1
    # The script reads the shipped lock, which pins the digest of the real
    # tree, so a synthetic one is refused for disagreeing with the pin before
    # its own modification is reached. Both are refusals of the same thing:
    # this is not the source the release is built from.
    assert (
        rpi_image_gen.REASON_SOURCE_MODIFIED in output
        or rpi_image_gen.REASON_SOURCE_UNVERIFIED in output
    ), output


# --- finding 5: an artefact cannot claim a build it did not come from ---------


def build_authority_payload(update, *, profile="rpi5",
                            build_id="20260808-1", **overrides):
    payload = {
        "schema_version": build_authority.SCHEMA_VERSION,
        "builder": {
            "source_form": "tarball",
            "revision": rpi_image_gen.read_lock().commit,
            "source_tree_sha256": "sha256:" + "5" * 64,
        },
        "profile": profile,
        "project": {"revision": "a" * 40, "tree_sha256": "sha256:" + "6" * 64},
        "build_id": build_id,
        "completed": True,
        "image": {
            "path": str(update),
            "sha256": build_authority.file_sha256(update),
        },
    }
    payload.update(overrides)
    return payload


# --- phase 33: the build authority is itself hashed --------------------------


def test_the_build_authority_hash_is_canonical(tmp_path):
    update = tmp_path / "appliance.img"
    update.write_bytes(b"payload")
    payload = build_authority_payload(update)
    reordered = dict(reversed(list(payload.items())))

    assert build_authority.canonical_hash(payload) == build_authority.canonical_hash(reordered)
    assert build_authority.canonical_hash(payload).startswith("sha256:")


def test_a_build_authority_round_trips_through_its_own_reader(tmp_path):
    update = tmp_path / "appliance.img"
    update.write_bytes(b"payload")
    path = tmp_path / build_authority.AUTHORITY_NAME
    path.write_text(json.dumps(build_authority_payload(update)) + "\n", encoding="utf-8")

    authority = build_authority.read(path)

    assert authority.schema_version == build_authority.SCHEMA_VERSION
    assert authority.builder.source_form == "tarball"
    assert authority.image.sha256 == build_authority.file_sha256(update)
    assert authority.canonical_hash.startswith("sha256:")


def test_an_unknown_build_authority_schema_is_refused(tmp_path):
    update = tmp_path / "appliance.img"
    update.write_bytes(b"payload")
    payload = build_authority_payload(update)
    payload["schema_version"] = 99
    path = tmp_path / build_authority.AUTHORITY_NAME
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(build_authority.BuildAuthorityError) as excinfo:
        build_authority.read(path)

    assert excinfo.value.code == "build_authority_unsupported"


# --- phase 9: one build, one output directory --------------------------------


def test_a_build_directory_is_claimed_by_exactly_one_build(tmp_path):
    first = build_authority.prepare_output(tmp_path, build_id="20260808-1")
    second = build_authority.prepare_output(tmp_path, build_id="20260808-2")

    assert first != second
    assert first.is_dir() and not any(first.iterdir())
    assert second.is_dir()


def test_a_stale_artefact_cannot_be_signed_through_a_new_build(tmp_path):
    """The mixing this prevents: yesterday's update, today's metadata."""

    directory = build_authority.prepare_output(tmp_path, build_id="20260808-1")
    stale = directory / "appliance.img"
    stale.write_bytes(b"yesterday")
    fresh = tmp_path / "fresh.tar.zst"
    fresh.write_bytes(b"today")
    payload = build_authority_payload(fresh)

    problems = build_authority.verify_image(
        build_authority.parse(payload),
        stale,
        profile="rpi5",
        revision=rpi_image_gen.read_lock().commit,
        build_id="20260808-1",
    )

    assert any("build_authority_mismatch" in problem for problem in problems)


def test_a_completed_build_authority_accepts_its_own_artefact(tmp_path):
    update = tmp_path / "appliance.img"
    update.write_bytes(b"today")
    payload = build_authority_payload(update)

    problems = build_authority.verify_image(
        build_authority.parse(payload),
        update,
        profile="rpi5",
        revision=rpi_image_gen.read_lock().commit,
        build_id="20260808-1",
    )

    assert problems == ()


def test_an_image_modified_after_the_build_authority_is_refused(tmp_path):
    update = tmp_path / "appliance.img"
    update.write_bytes(b"payload")
    image = tmp_path / "image.img"
    image.write_bytes(b"an image")
    payload = build_authority_payload(update)
    payload["image"] = {"path": str(image), "sha256": build_authority.file_sha256(image)}
    image.write_bytes(b"another image")

    problems = build_authority.verify_image(build_authority.parse(payload), image)

    assert any("build_authority_mismatch" in problem for problem in problems)


def test_the_authority_file_is_written_with_a_stable_layout(tmp_path):
    update = tmp_path / "appliance.img"
    update.write_bytes(b"payload")
    authority = build_authority.parse(build_authority_payload(update))

    path = build_authority.write(tmp_path, authority)

    assert path.name == build_authority.AUTHORITY_NAME
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == (
        build_authority.SCHEMA_VERSION
    )


# --- phase 34: the release gate driver ---------------------------------------


def test_the_release_gate_driver_reports_not_run_rather_than_a_pass(tmp_path):
    """A host that cannot build says so per gate, and that is not a release."""

    result = subprocess.run(
        [
            "sh",
            str(SCRIPTS / "appliance-release-gates.sh"),
            "--output",
            str(tmp_path / "out"),
            "--rpi-image-gen",
            str(tmp_path / "absent"),
            "--profile",
            "rpi5",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )

    assert result.returncode == 3, result.stdout + result.stderr
    assert "NOT RUN" in result.stdout
    assert "RESULT: PASS" not in result.stdout
    assert "required gates that did not run:" in result.stdout
    assert "Nothing was published, tagged or uploaded." in result.stdout


def test_the_release_gate_driver_names_the_gates_in_order(tmp_path):
    text = (SCRIPTS / "appliance-release-gates.sh").read_text(encoding="utf-8")

    for gate in (
        "source-authority",
        "build-$profile",
        "inspect-image-$profile",
        "source-bundle",
    ):
        assert gate in text, gate
    assert "Nothing is published" in text
    assert "no release is created" in text


# --- which file the build is allowed to call the image ------------------------

BUILD_SCRIPT = ROOT / "scripts" / "appliance-build-rpi-image.sh"


def test_the_image_is_taken_from_the_generators_output_directory():
    """``find -name '*.img' | head -1`` is not a way to identify an artefact.

    A finished build tree contains the chroot it built, and that chroot has
    ``/boot/firmware/kernel_2712.img`` in it. The first real rpi5 build
    therefore published a 10 MB Raspberry Pi kernel blob as the appliance
    image, hashed it, and wrote a *completed* build authority binding that
    digest — with every gate upstream of it reporting PASS.

    genimage writes the image to a directory named after the image, so that
    is where it is read from.
    """

    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "find \"$WORK\" -name '*.img'" not in script
    assert 'image-$IMAGE_NAME' in script or 'image-${IMAGE_NAME}' in script


def test_an_ambiguous_image_is_refused_rather_than_picked():
    """Two candidates mean the build does not know what it built."""

    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "image_ambiguous" in script


def test_the_image_name_comes_from_the_profile_the_build_was_given():
    """One authority: the profile declares it, the wrapper reads it."""

    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "IMAGE_NAME=" in script
    assert "$CONFIG" in script.split("IMAGE_NAME=")[1].split("\n")[0] or (
        "image_name_unknown" in script
    )


def test_a_second_profile_does_not_overwrite_the_first_authority():
    """Two boards built into one output directory left one authority.

    The image, the update and the build metadata are all named per profile;
    the authority was not. Building rpi4 and then rpi5 into one --output left
    only the rpi5 authority, so the rpi4 artefact — sitting right beside it,
    with its own digests — could no longer be signed at all.
    """

    script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert '"$OUTPUT/build-authority.json"' not in script
    assert '$NAME.build-authority.json' in script


BUILDER_VM_SCRIPT = SCRIPTS / "appliance-builder-vm.sh"


def test_the_builder_can_run_the_release_gates_it_is_the_only_host_for():
    """The strict gate was unobtainable on every host it could run on.

    The gate builds the images itself, so it needs mmdebstrap, podman, loop
    devices and a qemu-aarch64 binfmt handler. The one component that has
    those is the disposable builder guest — and it could only ever run the
    build script. So a host with the prerequisites did not exist, and
    ``RESULT: PASS`` was not reachable from anywhere.
    """

    script = BUILDER_VM_SCRIPT.read_text(encoding="utf-8")

    assert "--release-gate" in script
    assert "appliance-release-gates.sh" in script


def test_the_gate_run_carries_the_source_bundle_gate_its_input():
    """``source-bundle`` is a required gate and needs a bundle to check."""

    script = BUILDER_VM_SCRIPT.read_text(encoding="utf-8")
    gate_call = script.split("appliance-release-gates.sh")[-1].split("status=$?")[0]

    assert "--source-bundle" in gate_call
    assert "appliance-create-source-bundle.sh" in script


def test_the_gate_verdict_is_the_builders_verdict():
    """A gate that exited 3 is NOT RUN, and must not return success."""

    script = BUILDER_VM_SCRIPT.read_text(encoding="utf-8")

    assert 'exit "$status"' in script


def test_the_gate_logs_come_back_out_of_the_guest():
    """The report is the evidence; a verdict with no logs proves nothing.

    Artefact collection is ``find -maxdepth 1 -type f``, which walks straight
    past ``dist/gates/`` — the directory holding every gate's log.
    """

    script = BUILDER_VM_SCRIPT.read_text(encoding="utf-8")

    assert "gates" in script.split("== collecting artefacts ==")[1]


def test_a_tree_that_agrees_with_the_lock_and_was_then_modified_is_modified(tmp_path, lock):
    """The modification path stays reachable: the pin shadows it only for a tree
    that was never the pinned one to begin with."""

    from dataclasses import replace

    root = tarball_source(tmp_path, lock)
    recorded = rpi_image_gen.tree_digest(root)
    pinned = replace(lock, tree_sha256=recorded)

    (root / "config/trixie-minbase.yaml").write_text("image:\n  layer: other\n")
    report = rpi_image_gen.probe_checkout(root, pinned, which=lambda tool: f"/usr/bin/{tool}")

    assert report.reason == rpi_image_gen.REASON_SOURCE_MODIFIED




# --- which Manager package the image is made to carry ------------------------

BUILDER = SCRIPTS / "appliance-build-rpi-image.sh"


def build_with(tmp_path, *arguments, environment=None):
    """Run the image builder far enough to see how it treats its arguments.

    It never reaches a build here: this host has no rpi-image-gen dependencies,
    so a well-formed run stops at NOT RUN (3). That is exactly the boundary
    being tested -- a command-line mistake has to be told apart from a host that
    cannot build, and be found before twenty-five minutes are spent.
    """

    return subprocess.run(
        ["sh", str(BUILDER), "--profile", "rpi3", *arguments],
        capture_output=True, text=True, check=False, timeout=300,
        cwd=str(ROOT), env={**os.environ, **(environment or {})},
    )


def test_a_supplied_package_that_is_not_there_is_a_command_line_error(tmp_path):
    run = build_with(tmp_path, "--manager-package", str(tmp_path / "absent.deb"))

    assert run.returncode == 2, run.stderr
    assert "is not a file" in run.stderr


def test_a_supplied_file_that_is_not_a_package_is_a_command_line_error(tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("not a package", encoding="utf-8")

    run = build_with(tmp_path, "--manager-package", str(other))

    assert run.returncode == 2, run.stderr
    assert "is not a .deb" in run.stderr


def test_the_package_may_also_arrive_through_the_environment(tmp_path):
    """The caller that knows which package to use is two layers above the one
    that runs the builder: the workflow resolves it and the gate runner in
    between has no opinion about it, exactly as with the generator path."""

    run = build_with(
        tmp_path, environment={"EMS_APPLIANCE_MANAGER_PACKAGE": str(tmp_path / "absent.deb")}
    )

    assert run.returncode == 2, run.stderr
    assert "is not a file" in run.stderr


def test_a_well_formed_package_gets_past_the_command_line(tmp_path):
    """The counter-case, so the two tests above are about the argument and not
    about the flag being rejected outright."""

    package = tmp_path / "ems-appliance-manager_0.1.0_arm64.deb"
    package.write_bytes(b"stand-in")

    run = build_with(tmp_path, "--manager-package", str(package))

    assert run.returncode == 3, run.stderr
    assert "NOT RUN" in run.stderr
