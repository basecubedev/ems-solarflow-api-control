"""Static contract for the manual feature-image publish workflow.

Development builds are testing-only: they must never overwrite `latest` or any
release tag, and canonical install tags must identify one workflow attempt.
"""

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from workflow_contract import run_output_step

pytestmark = [
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "docker-feature-publish.yml"


def _step_with(job, step_name):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in workflow["jobs"][job]["steps"]:
        if step.get("name") == step_name:
            return step.get("with") or {}
    raise AssertionError(f"{job} has no step named {step_name!r}")


def _build_args(with_block):
    args = {}
    for line in str(with_block.get("build-args", "")).splitlines():
        line = line.strip()
        if "=" in line:
            key, _, value = line.partition("=")
            args[key.strip()] = value.strip()
    return args

EMS_IMAGE = "ghcr.io/basecubedev/ems-solarflow-api-control"
ADMIN_IMAGE = "ghcr.io/basecubedev/ems-solarflow-admin"

JOBS = (
    "resolve-source",
    "mqtt-release-contract",
    "packaged-system-build-smoke",
    "mosquitto-lifecycle",
    "package-smoke",
    "publish-feature-ghcr",
)

RESOLVED_SHA = "${{ needs.resolve-source.outputs.sha }}"


def _text():
    return WORKFLOW.read_text(encoding="utf-8")


def _job(name):
    text = _text()
    start = text.index(f"\n  {name}:\n")
    end = len(text)
    for other in JOBS:
        if other == name:
            continue
        position = text.find(f"\n  {other}:\n")
        if start < position < end:
            end = position
    return text[start:end]


def _job_permissions():
    """Map every workflow job to its explicit permission lines (least-privilege audit)."""
    body = _text().split("\njobs:", 1)[1]
    permissions = {}
    current = None
    reading = False
    for line in body.split("\n"):
        header = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if header:
            current = header.group(1)
            permissions[current] = []
            reading = False
            continue
        if current is None:
            continue
        if re.match(r"^    permissions:\s*$", line):
            reading = True
            continue
        if reading:
            entry = re.match(r"^      (\S.*?)\s*$", line)
            if entry:
                permissions[current].append(entry.group(1))
            else:
                reading = False
    return permissions


def test_workflow_file_exists():
    assert WORKFLOW.is_file()


def test_triggered_only_by_workflow_dispatch():
    text = _text()
    assert "workflow_dispatch:" in text
    # A manual feature build must not fire on push/schedule/tag events.
    assert "\n  push:" not in text
    assert "\n  schedule:" not in text


def test_required_ref_input():
    text = _text()
    assert "ref:" in text
    assert "required: true" in text


def test_mandatory_gates_cannot_be_disabled_by_dispatch_input():
    text = _text()
    assert "run_tests:" not in text
    assert "inputs.run_tests" not in text


def test_never_publishes_release_tags():
    text = _text()
    assert "value=latest" not in text
    assert "type=ref,event=tag" not in text
    assert 'channel="stable"' not in text
    assert 'channel="rc"' not in text


def test_emits_development_channel_label():
    text = _text()
    assert text.count("de.basecubedev.ems.channel=development") == 2
    assert "de.basecubedev.ems.branch=${{ inputs.ref }}" in text


def test_builds_both_images():
    text = _text()
    assert EMS_IMAGE in text
    assert ADMIN_IMAGE in text


def test_pushes_bare_and_sha_suffixed_feature_tags():
    text = _text()
    prefix = "${{ steps.build_identity.outputs.feature_tag_prefix }}"
    assert f"type=raw,value={prefix}\n" in text
    assert f"type=raw,value={prefix}-${{{{ steps.build_identity.outputs.git_commit_short }}}}" in text


def test_ems_build_args_use_canonical_development_release_tag_and_channel():
    # Both the local validation build and the pushed build are Buildx action
    # steps. Their runtime ENV / Dockerfile labels must describe the same
    # canonical install target, so the semantic build args must match exactly.
    immutable = "${{ steps.build_identity.outputs.immutable_tag }}"
    local = _build_args(_step_with("publish-feature-ghcr", "Build local EMS image for content validation"))
    pushed = _build_args(_step_with("publish-feature-ghcr", "Build and push EMS Docker image"))
    for args in (local, pushed):
        assert args.get("EMS_RELEASE_TAG") == immutable
        assert args.get("EMS_CHANNEL") == "development"
        assert args.get("EMS_GIT_DIRTY") == "false"
    assert local == pushed, f"local/pushed EMS build args differ: {local} != {pushed}"


def test_publish_depends_on_every_release_gate():
    text = _text()
    publish = text.split("  publish-feature-ghcr:", 1)[1]
    for gate in JOBS[:-1]:
        assert gate in publish.split("    steps:", 1)[0], gate


def test_publish_depends_on_the_fast_mqtt_release_contract():
    needs = _job("publish-feature-ghcr").split("    steps:", 1)[0]
    assert "mqtt-release-contract" in needs


def test_mqtt_release_contract_job_exists_and_is_read_only():
    section = _job("mqtt-release-contract")
    assert "runs-on: ubuntu-latest" in section
    assert _job_permissions()["mqtt-release-contract"] == ["contents: read"]


def test_mqtt_release_contract_builds_the_resolved_immutable_sha():
    section = _job("mqtt-release-contract")
    assert "resolve-source" in section.split("    steps:", 1)[0]
    assert f"ref: {RESOLVED_SHA}" in section
    assert "ref: ${{ inputs.ref }}" not in section


def test_mqtt_release_contract_runs_the_fast_release_gate():
    section = _job("mqtt-release-contract")
    assert "pytest -q -rs -m mqtt_release" in section
    assert 'pytest -q -rs -m "simulation and power_control"' in section
    assert "set -o pipefail" in section


def test_mqtt_release_contract_fails_when_a_selection_runs_no_tests():
    section = _job("mqtt-release-contract")
    # A marker typo must not turn "0 tests selected" into a green release gate.
    assert section.count('grep -E "[1-9][0-9]* passed"') >= 2


def test_mqtt_release_contract_runs_static_validation():
    section = _job("mqtt-release-contract")
    assert "ruff check ." in section
    assert "python -m compileall -q" in section
    assert "python tools/build_config_template.py --check" in section
    assert "node --check admin/static/admin.js" in section
    for package in ("admin", "ems", "dashboard", "scripts", "tests"):
        assert package in section, package
    assert "emsctl.py" in section
    assert "ems-solarflow-api-control.py" in section


def test_mqtt_release_contract_installs_runtime_and_dev_requirements():
    section = _job("mqtt-release-contract")
    assert "python -m pip install --upgrade pip" in section
    assert "-r requirements.txt -r requirements-dev.txt" in section
    assert 'python-version: "3.11"' in section


def test_workflow_defaults_to_read_only_permissions():
    head = _text().split("\njobs:", 1)[0]
    assert "permissions:\n  contents: read\n" in head
    assert "contents: write" not in head
    assert "packages: write" not in head


def test_only_the_publish_job_holds_write_permissions():
    # Least-privilege: the workflow default is read-only and every write scope is
    # confined to the jobs that need it:
    #   packages: write -> the GHCR image publisher and the retention pruner
    #   contents: write -> only the catalogue-branch committers
    # so no release-gate job can push images or write to the repository.
    permissions = _job_permissions()
    packages_write = {j for j, perms in permissions.items() if "packages: write" in perms}
    contents_write = {j for j, perms in permissions.items() if "contents: write" in perms}
    assert packages_write == {"publish-feature-ghcr", "prune-old-development-builds"}
    assert contents_write == {"publish-development-catalogue", "public-catalogue-verification"}
    # Publisher and pruner touch GHCR but must not also gain repository write.
    assert permissions["publish-feature-ghcr"] == ["contents: read", "packages: write"]
    assert permissions["prune-old-development-builds"] == ["contents: read", "packages: write"]
    # Every release-gate job stays strictly read-only.
    for job in JOBS[:-1]:
        assert permissions[job] == ["contents: read"], job


def test_mosquitto_gate_runs_the_complete_real_broker_contract():
    # tests/test_mqtt_release_fail_closed.py owns the documented real-broker set;
    # the publish gate must run exactly that set, so neither can drift alone.
    from tests.test_mqtt_release_fail_closed import GATE_FILES

    section = _job("mosquitto-lifecycle")
    for gate_file in GATE_FILES:
        assert gate_file in section, gate_file
    assert section.count("tests/test_mqtt_real") + section.count(
        "tests/test_zendure_mqtt_broker_mosquitto.py"
    ) == len(GATE_FILES)


def test_mosquitto_gate_fails_closed_in_release_ci():
    section = _job("mosquitto-lifecycle")
    # The broker environment is reported before the gate runs, the tests are
    # required (skips become failures), and an all-skipped run cannot pass.
    assert "docker version" in section
    assert "docker info" in section
    assert "docker pull eclipse-mosquitto:2" in section
    assert 'EMS_REQUIRE_REAL_MQTT_TESTS: "1"' in section
    assert "set -o pipefail" in section
    assert "No real Mosquitto lifecycle test executed" in section


def test_source_revision_is_resolved_exactly_once():
    text = _text()
    # Only the resolve-source job may consume the mutable dispatch input as a
    # checkout target; everything downstream builds the resolved commit.
    assert text.count("ref: ${{ inputs.ref }}") == 1
    resolve = _job("resolve-source")
    assert "ref: ${{ inputs.ref }}" in resolve
    assert "sha: ${{ steps.resolve.outputs.sha }}" in resolve
    assert "ref: ${{ steps.resolve.outputs.ref }}" in resolve


def test_every_gate_and_publish_check_out_the_resolved_revision():
    text = _text()
    for job in JOBS[1:]:
        section = _job(job)
        assert f"ref: {RESOLVED_SHA}" in section, job
        needs = section.split("    steps:", 1)[0]
        assert "resolve-source" in needs, job
    assert text.count(f"ref: {RESOLVED_SHA}") == len(JOBS) - 1


def test_no_gate_checks_out_the_mutable_input_ref():
    for job in JOBS[1:]:
        assert "ref: ${{ inputs.ref }}" not in _job(job), job


def test_publish_identity_verifies_the_resolved_revision():
    publish = _job("publish-feature-ghcr")
    assert f"RESOLVED_SHA: {RESOLVED_SHA}" in publish
    assert '"${git_commit}" != "${RESOLVED_SHA}"' in publish


def test_oci_and_system_build_metadata_use_the_resolved_commit():
    text = _text()
    git_commit = "${{ steps.build_identity.outputs.git_commit }}"
    assert text.count(f"org.opencontainers.image.revision={git_commit}") == 2
    ems_local = _build_args(_step_with("publish-feature-ghcr", "Build local EMS image for content validation"))
    ems_pushed = _build_args(_step_with("publish-feature-ghcr", "Build and push EMS Docker image"))
    assert ems_local.get("EMS_GIT_COMMIT") == git_commit
    assert ems_pushed.get("EMS_GIT_COMMIT") == git_commit
    admin_local = _build_args(_step_with("publish-feature-ghcr", "Build local Admin image for startup validation"))
    admin_pushed = _build_args(_step_with("publish-feature-ghcr", "Build and push Admin Docker image"))
    assert admin_local.get("EMS_REVISION") == git_commit
    assert admin_pushed.get("EMS_REVISION") == git_commit
    assert f"EXPECTED_REVISION: {git_commit}" in text
    assert f"REVISION: {git_commit}" in text


def _git(repo, *args):
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "contract",
            "GIT_AUTHOR_EMAIL": "contract@test",
            "GIT_COMMITTER_NAME": "contract",
            "GIT_COMMITTER_EMAIL": "contract@test",
        },
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def test_resolve_source_step_pins_the_commit_not_the_branch(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "feature/example")
    (repo / "file.txt").write_text("one", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")

    result, values = run_output_step(
        WORKFLOW,
        "Resolve source revision",
        cwd=repo,
        tmp_path=tmp_path / "run",
        environ={"INPUT_REF": "feature/example"},
    )
    assert result.returncode == 0, result.stderr
    assert values["sha"] == first
    assert re.fullmatch(r"[0-9a-f]{40}", values["sha"])
    assert values["ref"] == "feature/example"

    # The branch moves on after resolution; the captured output still names
    # the exact commit every downstream job checks out.
    (repo / "file.txt").write_text("two", encoding="utf-8")
    _git(repo, "commit", "-am", "two")
    assert _git(repo, "rev-parse", "feature/example") != first
    assert _git(repo, "rev-parse", f"{values['sha']}^{{commit}}") == first


# --- protected-ref guard (development-only publish) ------------------------

PROTECTED_REFS = (
    "main",
    "master",
    "latest",
    "stable",
    "rc",
    "v1.0.0",
    "v0.8.0-RC1",
    "V2.3",
    "refs/tags/v1.2.3",
    "refs/heads/main",
    "Main",
)

DEVELOPMENT_REFS = (
    "feature/example",
    "fix/bug-123",
    "develop/next",
    "feature/v2-rewrite",
    "develop",
)


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "feature/example")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "c")
    return repo


def _run_guard(tmp_path, repo, input_ref):
    return run_output_step(
        WORKFLOW,
        "Reject protected release refs",
        cwd=repo,
        tmp_path=tmp_path / "run",
        environ={"INPUT_REF": input_ref},
    )


def test_guard_step_lives_in_resolve_job_with_no_write_permission():
    resolve = _job("resolve-source")
    assert "Reject protected release refs" in resolve
    assert "for protected in main master latest stable rc" in resolve
    assert "^v[0-9]" in resolve
    assert "refs/tags/v" in resolve
    # The resolved checkout context is validated too, not only the raw input.
    assert "git tag --points-at HEAD" in resolve
    # The guard runs in the resolve job, which every gate and publish depend on
    # and which holds no package/catalogue write permission.
    assert _job_permissions()["resolve-source"] == ["contents: read"]


@pytest.mark.parametrize("ref", PROTECTED_REFS)
def test_guard_rejects_protected_release_refs(tmp_path, ref):
    repo = _init_repo(tmp_path)
    result, _ = _run_guard(tmp_path, repo, ref)
    assert result.returncode != 0, f"{ref!r} should be rejected: {result.stdout}"


@pytest.mark.parametrize("ref", DEVELOPMENT_REFS)
def test_guard_allows_development_refs(tmp_path, ref):
    repo = _init_repo(tmp_path)
    result, _ = _run_guard(tmp_path, repo, ref)
    assert result.returncode == 0, f"{ref!r} should be allowed: {result.stderr}"


def test_guard_rejects_a_release_tag_pointing_at_head_even_for_a_benign_ref(tmp_path):
    # A benign-looking ref (or a raw SHA) whose commit a release tag also points
    # at must be rejected on the resolved checkout context.
    repo = _init_repo(tmp_path)
    _git(repo, "tag", "v9.9.9")
    result, _ = _run_guard(tmp_path, repo, "feature/example")
    assert result.returncode != 0, result.stdout


def test_publish_identity_rejects_a_source_moved_after_resolution(tmp_path):
    bin_dir = _mock_checked_out_git(tmp_path, "b" * 40)
    result, _ = run_output_step(
        WORKFLOW,
        "Resolve feature build identity",
        cwd=ROOT,
        tmp_path=tmp_path,
        environ={
            "INPUT_REF": "feature/example",
            "CHECKED_OUT_SHA": "b" * 40,
            "RESOLVED_SHA": "a" * 40,
            "GITHUB_SHA": "c" * 40,
            "GITHUB_RUN_ID": "123456789",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_RUN_NUMBER": "42",
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
        },
    )
    assert result.returncode != 0
    assert "resolved source" in result.stderr


def test_release_gate_commands_cover_required_contracts_once():
    text = _text()
    # The pre-publish gates that already run on the branch via CI are trimmed;
    # only the gates that do not run there remain in this workflow.
    assert "npm run test:e2e:system-build-packaged" in text
    assert "tests/test_zendure_mqtt_broker_mosquitto.py" in text
    assert "tests/test_mqtt_real_legacy_flow.py" in text
    assert "ems-solarflow-api-control:package-smoke" in text
    assert "ems-solarflow-admin:package-smoke" in text


def _mock_checked_out_git(tmp_path, sha):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    git = bin_dir / "git"
    git.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  rev-parse) printf '%s\\n' \"$CHECKED_OUT_SHA\" ;;\n"
        "  describe) printf '%s\\n' \"${CHECKED_OUT_SHA%${CHECKED_OUT_SHA#???????}}\" ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    git.chmod(0o755)
    return bin_dir


def _resolve(tmp_path, *, ref="feature/example", sha="a" * 40, run_id="123456789",
             attempt="1", trigger_sha="c" * 40):
    bin_dir = _mock_checked_out_git(tmp_path, sha)
    result, values = run_output_step(
        WORKFLOW,
        "Resolve feature build identity",
        cwd=ROOT,
        tmp_path=tmp_path,
        environ={
            "INPUT_REF": ref,
            "CHECKED_OUT_SHA": sha,
            "RESOLVED_SHA": sha,
            "GITHUB_SHA": trigger_sha,
            "GITHUB_RUN_ID": run_id,
            "GITHUB_RUN_ATTEMPT": attempt,
            "GITHUB_RUN_NUMBER": "42",
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
        },
    )
    assert result.returncode == 0, result.stderr
    return values


def test_first_workflow_attempt_is_part_of_canonical_install_tag(tmp_path):
    identity = _resolve(tmp_path, attempt="1")
    ref_hash = hashlib.sha256(b"feature/example").hexdigest()[:10]
    assert identity["immutable_tag"] == (
        f"dev-feature-example-{ref_hash}-aaaaaaa-123456789-1"
    )


def test_identity_uses_checked_out_ref_sha_not_dispatch_trigger_sha(tmp_path):
    identity = _resolve(
        tmp_path, sha="b" * 40, trigger_sha="a" * 40
    )
    assert identity["git_commit"] == "b" * 40
    assert "-bbbbbbb-" in identity["immutable_tag"]


def test_development_build_id_is_the_canonical_install_identity(tmp_path):
    identity = _resolve(tmp_path, attempt="1")
    assert identity["build_id"] == identity["immutable_tag"]


def test_retry_gets_a_different_canonical_install_tag(tmp_path):
    first = _resolve(tmp_path / "first", attempt="1")
    second = _resolve(tmp_path / "second", attempt="2")
    assert first["immutable_tag"] != second["immutable_tag"]
    assert first["immutable_tag"].endswith("-123456789-1")
    assert second["immutable_tag"].endswith("-123456789-2")


def test_same_commit_rebuilt_in_another_run_does_not_collide(tmp_path):
    first = _resolve(tmp_path / "first", run_id="100", attempt="1")
    second = _resolve(tmp_path / "second", run_id="101", attempt="1")
    assert first["immutable_tag"] != second["immutable_tag"]


def test_different_commits_do_not_collide(tmp_path):
    first = _resolve(tmp_path / "first", sha="a" * 40)
    second = _resolve(tmp_path / "second", sha="b" * 40)
    assert first["immutable_tag"] != second["immutable_tag"]


@pytest.mark.parametrize(
    ("ref", "prefix"),
    (
        ("Feature/Zendure-MQTT", "dev-feature-zendure-mqtt"),
        ("UPPERCASE", "dev-uppercase"),
        ("feature/nested/name", "dev-feature-nested-name"),
    ),
)
def test_branch_name_sanitization_is_stable(tmp_path, ref, prefix):
    identity = _resolve(tmp_path, ref=ref)
    ref_hash = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:10]
    expected = f"{prefix}-{ref_hash}"
    assert identity["feature_tag_prefix"] == expected
    assert identity["immutable_tag"].startswith(expected + "-")


def test_very_long_branch_keeps_unique_suffix_within_docker_tag_limit(tmp_path):
    identity = _resolve(tmp_path, ref="feature/" + "x" * 240)
    tag = identity["immutable_tag"]
    assert len(tag) <= 128
    assert tag.endswith("-aaaaaaa-123456789-1")


def test_both_images_publish_the_same_canonical_development_metadata():
    text = _text()
    immutable = "${{ steps.build_identity.outputs.immutable_tag }}"
    build_id = "${{ steps.build_identity.outputs.build_id }}"
    assert text.count(f"type=raw,value={immutable}") == 2
    assert text.count(f"org.opencontainers.image.version={immutable}") == 2
    assert text.count(f"de.basecubedev.ems.release_tag={immutable}") == 2
    assert text.count(f"de.basecubedev.ems.build_id={build_id}") == 2
    assert f"EMS_SYSTEM_TAG={immutable}" in text
    assert "${{ steps.build_identity.outputs.git_commit }}" in text
    assert "${{ github.sha }}" not in text


def test_feature_pair_verification_checks_complete_runtime_and_bundle_identity():
    text = _text()
    start = text.index("      - name: Verify Admin/EMS feature-build pair metadata")
    end = text.index("      - name: Build and push Admin Docker image", start)
    verification = text[start:end]

    # Both locally built images must be checked by OCI metadata, while EMS also
    # exposes the same values through runtime ENV.
    assert verification.count("org.opencontainers.image.version") >= 2
    assert verification.count("org.opencontainers.image.revision") >= 2
    assert verification.count("de.basecubedev.ems.channel") >= 2
    assert verification.count("de.basecubedev.ems.build_id") >= 2
    assert verification.count("de.basecubedev.ems.release_tag") >= 2
    assert "printenv EMS_GIT_COMMIT" in verification
    assert "printenv EMS_BUILD_ID" in verification
    assert "printenv EMS_CHANNEL" in verification
    assert "printenv EMS_RELEASE_TAG" in verification

    # CI must read both embedded descriptors and compare their complete
    # non-recursive System Build identity, not only revision/build_id.
    assert "/app/release-resources/system-build.json" in verification
    assert "/app/release-resources/resource-manifest.json" in verification
    for field in (
        "system_tag",
        "channel",
        "revision",
        "build_id",
        "release_tag",
        "admin_image",
        "ems_image",
    ):
        assert verification.count(f"['{field}']") >= 2, field
