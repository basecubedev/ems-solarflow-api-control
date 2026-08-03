# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dedicated real-Docker Admin replacement browser gate contracts.

The canary replaces one published Development Admin with another. Both sides are
addressed by digest and both must serve the same page-object test hooks, so the
resolver, the runner and the shared page objects are all held to that here.
"""

import copy
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.contract,
]


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "admin-replacement-canary.yml"
PUBLISH_WORKFLOW = ROOT / ".github" / "workflows" / "docker-feature-publish.yml"
RUNNER = ROOT / "tests" / "e2e" / "run-admin-replacement-canary.sh"
SPEC = ROOT / "tests" / "e2e" / "admin-replacement-canary.spec.ts"
PAGES = ROOT / "tests" / "e2e" / "pages"
CONTRACT = ROOT / "tests" / "e2e" / "admin-test-contract.json"
MARKUP = ROOT / "admin" / "static" / "index.html"

ADMIN_REPO = "ghcr.io/basecubedev/ems-solarflow-admin"
EMS_REPO = "ghcr.io/basecubedev/ems-solarflow-api-control"
PREFIX = "dev-feature-canary-abc1234567"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolver = _load("resolve_canary_builds")
contract_probe = _load("admin_test_contract")


def _digest(char):
    return "sha256:" + char * 64


def _entry(short, run_id, created_at, *, admin_digest, ems_digest, **extra):
    tag = f"{PREFIX}-{short}-{run_id}-1"
    entry = {
        "tag": tag,
        "display_name": "Feature canary",
        "channel": "development",
        "revision": short + "0" * (40 - len(short)),
        "build_id": tag,
        "run_id": str(run_id),
        "run_attempt": 1,
        "created_at": created_at,
        "admin_image": f"{ADMIN_REPO}:{tag}",
        "admin_digest": admin_digest,
        "ems_image": f"{EMS_REPO}:{tag}",
        "ems_digest": ems_digest,
        "installable": True,
    }
    entry.update(extra)
    return entry


TARGET = _entry(
    "bbbbbbb", 200, "2026-08-02T08:00:00Z", admin_digest=_digest("b"), ems_digest=_digest("c")
)
SOURCE = _entry(
    "aaaaaaa", 100, "2026-08-01T08:00:00Z", admin_digest=_digest("a"), ems_digest=_digest("d")
)
CATALOGUE = {"builds": [TARGET, SOURCE]}
VERSION, HOOKS = contract_probe.load_contract()


def _catalogue(mutate=None):
    payload = copy.deepcopy(CATALOGUE)
    if mutate is not None:
        mutate(payload)
    return payload


def _resolve(payload, **kwargs):
    return resolver.resolve(payload, VERSION, **kwargs)


def _blocked(payload, **kwargs):
    with pytest.raises(resolver.BlockedPrecondition) as excinfo:
        _resolve(payload, **kwargs)
    return str(excinfo.value)


def _run_runner(**overrides):
    env = {key: value for key, value in os.environ.items() if not key.startswith("CANARY_")}
    env.update(
        {
            "ADMIN_REPLACEMENT_RUNTIME": "/tmp/ems-admin-replacement-contract",
            "ADMIN_REPLACEMENT_EVENTS": "/tmp/ems-admin-replacement-contract.log",
            "CANARY_SOURCE_TAG": SOURCE["tag"],
            "CANARY_SOURCE_REVISION": SOURCE["revision"],
            "CANARY_SOURCE_BUILD_ID": SOURCE["build_id"],
            "CANARY_SOURCE_ADMIN_DIGEST": SOURCE["admin_digest"],
            "CANARY_TAG": TARGET["tag"],
            "CANARY_REVISION": TARGET["revision"],
            "CANARY_BUILD_ID": TARGET["build_id"],
            "CANARY_ADMIN_DIGEST": TARGET["admin_digest"],
            "CANARY_EMS_DIGEST": TARGET["ems_digest"],
        }
    )
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        ["bash", str(RUNNER)], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )


# --- resolution ------------------------------------------------------------


def test_default_resolution_pairs_the_two_newest_builds_of_one_branch():
    source, target = _resolve(_catalogue())
    assert target["tag"] == TARGET["tag"]
    assert source["tag"] == SOURCE["tag"]


def test_source_admin_digest_is_mandatory():
    message = _blocked(_catalogue(lambda payload: payload["builds"][1].pop("admin_digest")))
    assert "older installable Development build" in message

    result = _run_runner(CANARY_SOURCE_ADMIN_DIGEST=None)
    assert result.returncode != 0
    assert "CANARY_SOURCE_ADMIN_DIGEST is required" in result.stderr


def test_target_digests_are_mandatory():
    for field in ("admin_digest", "ems_digest"):
        payload = _catalogue(lambda entries, key=field: entries["builds"][0].pop(key))
        assert _blocked(payload, target_tag=TARGET["tag"])

    for name in ("CANARY_ADMIN_DIGEST", "CANARY_EMS_DIGEST"):
        result = _run_runner(**{name: None})
        assert result.returncode != 0
        assert f"{name} is required" in result.stderr


def test_mutable_source_is_rejected():
    payload = _catalogue(lambda entries: entries["builds"][1].update(admin_digest="latest"))
    assert "not an immutable digest" in _blocked(payload)

    result = _run_runner(CANARY_SOURCE_ADMIN_DIGEST="latest")
    assert result.returncode != 0
    assert "must be an immutable sha256: digest" in result.stderr


def test_mutable_target_is_rejected():
    for field in ("admin_digest", "ems_digest"):
        payload = _catalogue(
            lambda entries, key=field: entries["builds"][0].update({key: "latest"})
        )
        assert "not an immutable digest" in _blocked(payload)

    for name in ("CANARY_ADMIN_DIGEST", "CANARY_EMS_DIGEST"):
        result = _run_runner(**{name: "latest"})
        assert result.returncode != 0
        assert "must be an immutable sha256: digest" in result.stderr


def test_identical_source_and_target_admin_digest_is_rejected():
    payload = _catalogue(
        lambda entries: entries["builds"][1].update(admin_digest=TARGET["admin_digest"])
    )
    assert "same Admin digest" in _blocked(payload)

    result = _run_runner(CANARY_SOURCE_ADMIN_DIGEST=TARGET["admin_digest"])
    assert result.returncode != 0
    assert "replacement would assert nothing" in result.stderr


def test_missing_source_catalogue_entry_is_rejected():
    message = _blocked(_catalogue(), source_tag=f"{PREFIX}-fffffff-900-1")
    assert "source of the replacement canary" in message

    only_target = _catalogue(lambda entries: entries["builds"].pop(1))
    assert "no older installable Development build" in _blocked(only_target)


def test_missing_target_catalogue_entry_is_rejected():
    message = _blocked(_catalogue(), target_tag=f"{PREFIX}-fffffff-900-1")
    assert "target of the replacement canary" in message


def test_incompatible_admin_test_contract_versions_are_rejected():
    incompatible = _catalogue(
        lambda entries: entries["builds"][1].update(admin_test_contract=VERSION + 1)
    )
    assert "no older installable Development build" in _blocked(incompatible)

    declared = _catalogue(
        lambda entries: [entry.update(admin_test_contract=VERSION) for entry in entries["builds"]]
    )
    source, target = _resolve(declared, require_declared=True)
    assert (source["tag"], target["tag"]) == (SOURCE["tag"], TARGET["tag"])
    assert "no installable Development build declares" in _blocked(
        _catalogue(), require_declared=True
    )


def test_published_catalogue_entries_declare_the_admin_test_contract():
    text = PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    assert text.count("admin_test_contract: $admin_test_contract") == 2
    assert text.count('--argjson admin_test_contract "$(jq -r .version') == 2


# --- one shared test-hook contract -----------------------------------------


def test_every_contract_hook_exists_in_the_admin_markup():
    markup = MARKUP.read_text(encoding="utf-8")
    missing = contract_probe.missing_hooks(markup, HOOKS)
    assert missing == [], missing


def test_shared_page_objects_use_only_contract_test_hooks():
    used = set()
    for path in sorted(PAGES.glob("*.ts")):
        used |= set(re.findall(r'getByTestId\("([^"]+)"\)', path.read_text(encoding="utf-8")))
    assert used, "the shared page objects must address the Admin through data-testid"
    assert used <= set(HOOKS), sorted(used - set(HOOKS))


def test_shared_page_objects_carry_no_legacy_selector_fallback():
    # A hooked element's own id is the markup generation the hooks replaced.
    # Reintroducing it in a shared page object is the fallback this forbids.
    markup = MARKUP.read_text(encoding="utf-8")
    legacy_ids = set()
    for hook in HOOKS:
        index = markup.index(f'data-testid="{hook}"')
        element = markup[markup.rindex("<", 0, index) : markup.index(">", index)]
        legacy_ids |= {f"#{value}" for value in re.findall(r'(?<![-\w])id="([^"]+)"', element)}

    for path in sorted(PAGES.glob("*.ts")):
        text = path.read_text(encoding="utf-8")
        for legacy in legacy_ids:
            assert legacy not in text, f"{path.name} falls back to legacy selector {legacy}"
        assert 'locator("[data-testid' not in text, path.name


def test_probe_reports_every_missing_hook_by_name():
    assert contract_probe.missing_hooks("", HOOKS) == HOOKS
    served = "".join(f'<b data-testid="{hook}"></b>' for hook in HOOKS[:-1])
    assert contract_probe.missing_hooks(served, HOOKS) == [HOOKS[-1]]


def test_contract_document_is_a_versioned_hook_list():
    payload = json.loads(CONTRACT.read_text(encoding="utf-8"))
    assert payload["version"] == VERSION
    assert payload["hooks"] == HOOKS


# --- workflow and journey --------------------------------------------------


def test_real_replacement_canary_is_scheduled_and_manually_runnable():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "schedule:" in text
    assert "admin-replacement-canary" in text
    assert "playwright.admin-replacement.config.ts" in text
    assert "/var/run/docker.sock" in RUNNER.read_text(encoding="utf-8")


def test_canary_never_starts_a_mutable_source_admin():
    runner = RUNNER.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "ems-solarflow-admin:latest" not in runner
    assert "--tag latest" not in runner
    assert '--tag "$CANARY_SOURCE_TAG"' in runner
    assert "ems-solarflow-admin:latest" not in workflow
    for role in ("source", "target"):
        assert f"--role {role}" in runner
    for name, output in {
        "CANARY_SOURCE_TAG": "source_tag",
        "CANARY_SOURCE_REVISION": "source_revision",
        "CANARY_SOURCE_BUILD_ID": "source_build_id",
        "CANARY_SOURCE_ADMIN_DIGEST": "source_admin_digest",
        "CANARY_TAG": "target_tag",
        "CANARY_REVISION": "target_revision",
        "CANARY_BUILD_ID": "target_build_id",
        "CANARY_ADMIN_DIGEST": "target_admin_digest",
        "CANARY_EMS_DIGEST": "target_ems_digest",
    }.items():
        assert f"{name}: ${{{{ steps.pair.outputs.{output} }}}}" in workflow


def test_real_replacement_browser_asserts_durable_reconnect_contract():
    text = SPEC.read_text(encoding="utf-8")

    for contract in (
        "admin_update",
        "old container no longer active",
        "target digest",
        "persistent Admin reference",
        "authenticated",
        "reauthenticateAfterReconnect",
        "resources_verified",
        "continueToDevices",
        "replacement events",
        "journey starts on the published source Admin",
        "target EMS identity",
        "a replaced Admin process serves the reconnect",
    ):
        assert contract in text
