"""Static contract for the feature-image cleanup workflow.

Deleting a feature branch must remove only that branch's `dev-<safe-ref>` image
tags. These assertions guard the trigger, the branch guard, both target
packages, and the tag-match safety rule.
"""

from pathlib import Path

from workflow_contract import run_output_step

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "docker-feature-cleanup.yml"


def _text():
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_file_exists():
    assert WORKFLOW.is_file()


def test_triggered_by_delete_event():
    text = _text()
    assert "\non:\n  delete:" in text


def test_runs_only_for_branch_deletions():
    text = _text()
    assert "github.event.ref_type == 'branch'" in text


def test_sanitizes_deleted_ref():
    text = _text()
    # Same sanitization pipeline as the publish workflow.
    assert "tr '[:upper:]' '[:lower:]'" in text
    assert "sed 's#[^a-z0-9._-]#-#g'" in text
    assert 'feature_tag_prefix=dev-${safe_ref}-${ref_hash}' in text


def _prefix(tmp_path, ref):
    result, values = run_output_step(
        WORKFLOW,
        "Resolve feature tag prefix",
        cwd=ROOT,
        tmp_path=tmp_path,
        environ={"DELETED_REF": ref},
    )
    assert result.returncode == 0, result.stderr
    return values["feature_tag_prefix"]


def test_similarly_named_branches_have_unambiguous_cleanup_prefixes(tmp_path):
    short = _prefix(tmp_path / "short", "feature/foo")
    longer = _prefix(tmp_path / "long", "feature/foo-bar")
    assert short != longer
    assert not longer.startswith(short + "-")


def test_targets_both_packages():
    text = _text()
    assert 'delete_matching_versions "ems-solarflow-api-control"' in text
    assert 'delete_matching_versions "ems-solarflow-admin"' in text


def test_matches_only_feature_tag_prefix():
    text = _text()
    # Exact prefix or its `-<sha>` variants only; never a substring match.
    assert 'select(. == $p or startswith($p + "-"))' in text


def test_deletes_via_packages_rest_api():
    text = _text()
    assert 'PACKAGE_OWNER_TYPE: user' in text
    assert 'user) echo "/users/${OWNER}"' in text
    assert 'organization) echo "/orgs/${OWNER}"' in text
    assert "gh api --method DELETE" in text
