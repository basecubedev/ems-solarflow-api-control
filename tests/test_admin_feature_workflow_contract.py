# SPDX-License-Identifier: AGPL-3.0-or-later
"""Publication lifecycle contracts for the public development-build catalogue."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.workflow,
    pytest.mark.contract,
]


ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / ".github" / "workflows" / "docker-feature-publish.yml"
CLEANUP = ROOT / ".github" / "workflows" / "docker-feature-cleanup.yml"
MUTATOR = ROOT / "scripts" / "development_catalogue.py"
REMOTE_PULLBACK = ROOT / "scripts" / "verify_remote_system_build.sh"
REMOTE_BROWSER = ROOT / "tests" / "e2e" / "run-remote-packaged-admin.sh"
PUBLIC_VERIFY = ROOT / "scripts" / "verify_development_catalogue.py"
CATALOGUE_MAX_BYTES = 512 * 1024


def _text(path):
    return path.read_text(encoding="utf-8")


def test_feature_workflows_serialize_public_catalogue_updates():
    for workflow in (PUBLISH, CLEANUP):
        text = _text(workflow)
        assert "development-build-catalogue" in text
        assert "concurrency:" in text
        assert "cancel-in-progress: false" in text
        assert "contents: write" in text
        assert "packages: write" in text


def test_cleanup_uses_explicit_user_package_api_and_supports_organizations():
    text = _text(CLEANUP)

    assert "PACKAGE_OWNER_TYPE: user" in text
    assert 'user) echo "/users/${OWNER}"' in text
    assert 'organization) echo "/orgs/${OWNER}"' in text
    assert "Unknown package owner type" in text


def test_cleanup_fails_closed_except_for_documented_missing_packages_or_versions():
    text = _text(CLEANUP)

    assert "already absent (HTTP 404)" in text
    assert "Unexpected GitHub Packages API failure" in text
    assert "Could not list versions" not in text
    assert "skipping" not in text


def test_cleanup_documents_package_linkage_for_permission_failures():
    text = _text(CLEANUP)

    assert "HTTP 403" in text
    assert "linked to this repository" in text
    assert "packages: write" in text


def test_publish_documents_public_raw_catalogue_and_dedicated_branch_write():
    text = _text(PUBLISH)

    assert (
        "https://raw.githubusercontent.com/basecubedev/"
        "ems-solarflow-api-control/development-build-catalogue/"
        "development-builds.json"
    ) in text
    assert 'CATALOGUE_BRANCH: development-build-catalogue' in text
    assert "contents: write" in text


def test_cleanup_deletes_admin_then_ems_verifies_and_only_then_changes_catalogue():
    text = _text(CLEANUP)
    delete_step = text.index("Delete matching GHCR package versions")
    admin = text.index('delete_matching_versions "ems-solarflow-admin"', delete_step)
    ems = text.index('delete_matching_versions "ems-solarflow-api-control"', delete_step)
    verify = text.index('verify_no_matching_versions "ems-solarflow-admin"', delete_step)
    catalogue = text.index("Remove branch from development build catalogue")

    assert delete_step < admin < ems < verify < catalogue


def test_publish_updates_catalogue_only_after_both_remote_images_are_verified():
    text = _text(PUBLISH)
    ems_push = text.index("Build and push EMS Docker image")
    admin_push = text.index("Build and push Admin Docker image")
    pair_check = text.index("Verify published Admin/EMS image pair")
    catalogue = text.index("Publish development build catalogue entry")

    assert ems_push < pair_check
    assert admin_push < pair_check < catalogue
    verification = text[pair_check:catalogue]
    for required in (
        "org.opencontainers.image.revision",
        "de.basecubedev.ems.build_id",
        "de.basecubedev.ems.channel",
        "sha256:",
    ):
        assert required in verification


def test_publish_orders_remote_pullback_browser_and_public_verification():
    text = _text(PUBLISH)

    pushed = text.index("publish-feature-ghcr:")
    pullback = text.index("remote-image-pullback:")
    browser = text.index("remote-packaged-browser-canary:")
    catalogue = text.index("publish-development-catalogue:")
    public = text.index("public-catalogue-verification:")

    assert pushed < pullback < browser < catalogue < public
    assert "needs: publish-feature-ghcr" in text[pullback:browser]
    assert "remote-image-pullback" in text[browser:catalogue]
    assert "remote-packaged-browser-canary" in text[catalogue:public]
    assert "publish-development-catalogue" in text[public:]


def test_remote_pullback_inspects_exact_digest_images_and_runtime_content():
    text = _text(REMOTE_PULLBACK)

    assert '${ADMIN_IMAGE}@${ADMIN_DIGEST}' in text
    assert '${EMS_IMAGE}@${EMS_DIGEST}' in text
    assert "docker pull --platform" in text
    assert "org.opencontainers.image.revision" in text
    assert "de.basecubedev.ems.build_id" in text
    assert "de.basecubedev.ems.channel" in text
    assert "de.basecubedev.ems.release_tag" in text
    assert "/app/release-resources/system-build.json" in text
    assert "/app/release-resources/resource-manifest.json" in text
    assert "emsctl.py" in text and "--help" in text
    assert "/app/config.template.json" in text
    assert "/api/admin/auth/status" in text


def test_remote_browser_uses_production_admin_mode_and_catalogue_source():
    text = _text(REMOTE_BROWSER)

    assert "EMS_ADMIN_TEST_MODE" not in text
    assert "EMS_ADMIN_DEVELOPMENT_CATALOGUE" in text
    assert '"${REMOTE_ADMIN_IMAGE}"' in text
    assert "source=/var/run/docker.sock,target=/var/run/docker.sock" in text
    assert 'chmod 0777 "$RUNTIME_DIR"' in text
    assert 'DOCKER_CONFIG=/tmp/docker' in text


def test_public_verification_fails_closed_and_removes_bad_entry():
    text = _text(PUBLISH)
    public = text[text.index("public-catalogue-verification:") :]

    assert str(PUBLIC_VERIFY.relative_to(ROOT)) in public
    assert "Remove failed public catalogue entry" in public
    assert "remove-tag" in public
    assert "if: ${{ failure() }}" in public
    assert "catalogue_attempt=" in _text(PUBLIC_VERIFY)
    assert "&browser=1" in public


def test_failed_image_steps_cannot_publish_a_catalogue_entry():
    text = _text(PUBLISH)
    catalogue_section = text[text.index("Publish development build catalogue entry") :]
    assert "if: ${{ success() }}" in catalogue_section
    assert "installable" in catalogue_section


def _run_mutator(tmp_path, *args):
    catalogue = tmp_path / "development-builds.json"
    result = subprocess.run(
        [sys.executable, str(MUTATOR), "--catalogue", str(catalogue), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return catalogue, result


def _catalogue_entry(
    branch,
    run_id,
    *,
    run_attempt=1,
    created_at="2026-07-15T10:00:00Z",
):
    revision = f"{run_id:07x}".ljust(40, "a")
    prefix = f"dev-{branch}-abcdef1234"
    tag = f"{prefix}-{revision[:7]}-{run_id}-{run_attempt}"
    return {
        "tag": tag,
        "display_name": branch,
        "channel": "development",
        "revision": revision,
        "build_id": tag,
        "run_id": str(run_id),
        "run_attempt": run_attempt,
        "created_at": created_at,
        "admin_image": f"ghcr.io/basecubedev/ems-solarflow-admin:{tag}",
        "admin_digest": "sha256:" + "a" * 64,
        "ems_image": f"ghcr.io/basecubedev/ems-solarflow-api-control:{tag}",
        "ems_digest": "sha256:" + "b" * 64,
        "installable": True,
    }


def _upsert_entry(tmp_path, entry):
    entry_path = tmp_path / "entry.json"
    entry_path.write_text(json.dumps(entry), encoding="utf-8")
    return _run_mutator(tmp_path, "upsert", "--entry", str(entry_path))


def test_cleanup_removes_only_the_deleted_branch_entries(tmp_path):
    catalogue = tmp_path / "development-builds.json"
    catalogue.write_text(
        json.dumps(
            {
                "builds": [
                    {"tag": "dev-feature-one-abcd123456-aaaaaaa-1-1"},
                    {"tag": "dev-feature-two-fedcba6543-bbbbbbb-2-1"},
                ]
            }
        ),
        encoding="utf-8",
    )

    _, result = _run_mutator(
        tmp_path,
        "remove-prefix",
        "--tag-prefix",
        "dev-feature-one-abcd123456",
    )

    assert result.returncode == 0, result.stderr
    tags = [entry["tag"] for entry in json.loads(catalogue.read_text())["builds"]]
    assert tags == ["dev-feature-two-fedcba6543-bbbbbbb-2-1"]


def test_catalogue_mutator_upserts_one_complete_entry_atomically(tmp_path):
    entry = {
        "tag": "dev-feature-one-abcd123456-aaaaaaa-1-1",
        "display_name": "Feature one",
        "channel": "development",
        "revision": "a" * 40,
        "build_id": "dev-feature-one-abcd123456-aaaaaaa-1-1",
        "run_id": "1",
        "run_attempt": 1,
        "created_at": "2026-07-15T10:00:00Z",
        "admin_image": "ghcr.io/basecubedev/ems-solarflow-admin:dev-feature-one-abcd123456-aaaaaaa-1-1",
        "admin_digest": "sha256:" + "a" * 64,
        "ems_image": "ghcr.io/basecubedev/ems-solarflow-api-control:dev-feature-one-abcd123456-aaaaaaa-1-1",
        "ems_digest": "sha256:" + "b" * 64,
        "installable": True,
    }
    entry_path = tmp_path / "entry.json"
    entry_path.write_text(json.dumps(entry), encoding="utf-8")

    catalogue, result = _run_mutator(
        tmp_path, "upsert", "--entry", str(entry_path)
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(catalogue.read_text()) == {"builds": [entry]}
    assert not list(tmp_path.glob("*.tmp"))


def test_catalogue_mutator_removes_only_one_failed_immutable_tag(tmp_path):
    first = _catalogue_entry("feature-one", 1)
    second = _catalogue_entry("feature-one", 2)
    catalogue = tmp_path / "development-builds.json"
    catalogue.write_text(json.dumps({"builds": [first, second]}), encoding="utf-8")

    _, result = _run_mutator(tmp_path, "remove-tag", "--tag", first["tag"])

    assert result.returncode == 0, result.stderr
    assert json.loads(catalogue.read_text())["builds"] == [second]


def test_production_catalogue_verifier_checks_exact_pair(tmp_path):
    entry = _catalogue_entry("feature-one", 42)
    catalogue = tmp_path / "development-builds.json"
    catalogue.write_text(json.dumps({"builds": [entry]}), encoding="utf-8")
    base = [
        sys.executable,
        str(PUBLIC_VERIFY),
        "--source",
        str(catalogue),
        "--tag",
        entry["tag"],
        "--revision",
        entry["revision"],
        "--build-id",
        entry["build_id"],
        "--admin-image",
        entry["admin_image"],
        "--admin-digest",
        entry["admin_digest"],
        "--ems-image",
        entry["ems_image"],
        "--ems-digest",
        entry["ems_digest"],
    ]

    valid = subprocess.run(base, cwd=ROOT, text=True, capture_output=True, check=False)
    wrong = subprocess.run(
        [*base[:-1], "sha256:" + "0" * 64],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert valid.returncode == 0, valid.stderr
    assert json.loads(valid.stdout)["tag"] == entry["tag"]
    assert wrong.returncode == 1
    assert "catalogue verification failed" in wrong.stderr


def test_new_branch_build_keeps_only_that_branches_two_newest(tmp_path):
    catalogue = tmp_path / "development-builds.json"
    entries = [
        _catalogue_entry(
            "feature-one", run_id, created_at=f"2026-07-15T10:0{run_id}:00Z"
        )
        for run_id in range(1, 4)
    ]
    other = _catalogue_entry("feature-two", 8, created_at="2026-07-15T09:00:00Z")
    catalogue.write_text(json.dumps({"builds": entries + [other]}), encoding="utf-8")

    _, result = _upsert_entry(
        tmp_path,
        _catalogue_entry("feature-one", 4, created_at="2026-07-15T10:04:00Z"),
    )

    assert result.returncode == 0, result.stderr
    builds = json.loads(catalogue.read_text())["builds"]
    feature_one_runs = [
        int(entry["run_id"])
        for entry in builds
        if entry["display_name"] == "feature-one"
    ]
    assert feature_one_runs == [4, 3]
    assert other in builds


def test_catalogue_global_retention_is_100_newest_builds(tmp_path):
    catalogue = tmp_path / "development-builds.json"
    entries = [
        _catalogue_entry(
            f"feature-{run_id}",
            run_id,
            created_at=f"2026-07-{(run_id % 28) + 1:02d}T10:00:00Z",
        )
        for run_id in range(1, 101)
    ]
    catalogue.write_text(json.dumps({"builds": entries}), encoding="utf-8")
    newest = _catalogue_entry(
        "feature-newest", 101, created_at="2026-08-01T10:00:00Z"
    )

    _, result = _upsert_entry(tmp_path, newest)

    assert result.returncode == 0, result.stderr
    builds = json.loads(catalogue.read_text())["builds"]
    assert len(builds) == 100
    assert builds[0] == newest


def test_catalogue_sort_uses_numeric_run_and_attempt_tie_breakers(tmp_path):
    catalogue = tmp_path / "development-builds.json"
    entries = [
        _catalogue_entry("feature-one", 9, run_attempt=1),
        _catalogue_entry("feature-two", 10, run_attempt=1),
        _catalogue_entry("feature-three", 10, run_attempt=2),
    ]
    catalogue.write_text(json.dumps({"builds": entries[:2]}), encoding="utf-8")

    _, result = _upsert_entry(tmp_path, entries[2])

    assert result.returncode == 0, result.stderr
    builds = json.loads(catalogue.read_text())["builds"]
    assert [(int(item["run_id"]), item["run_attempt"]) for item in builds] == [
        (10, 2),
        (10, 1),
        (9, 1),
    ]


def test_repeated_catalogue_upsert_has_no_duplicate_and_stays_below_loader_limit(
    tmp_path,
):
    entry = _catalogue_entry("feature-one", 1)

    catalogue, first = _upsert_entry(tmp_path, entry)
    catalogue, second = _upsert_entry(tmp_path, entry)

    assert first.returncode == second.returncode == 0
    assert json.loads(catalogue.read_text())["builds"] == [entry]
    assert catalogue.stat().st_size < CATALOGUE_MAX_BYTES
