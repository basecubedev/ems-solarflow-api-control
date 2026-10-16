# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dedicated real-Docker Admin replacement browser gate contracts.

The canary upgrades the Admin of the previous stable release to the Admin/EMS
pair of the latest stable release, the upgrade an installation takes. Which
releases those are is the Admin's own release listing; each image is pinned to
the digest of its registry manifest, and its build identity is read from its
labels and held to the Admin's own pairing rule. Both Admins must serve the
same page-object test hooks. The resolver, the runner and the shared page
objects are held to that here.
"""

import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import urllib.error
from pathlib import Path

import pytest

from admin import releases as admin_releases
from admin import system_build as admin_system_build

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.contract,
]


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "admin-replacement-canary.yml"
RUNNER = ROOT / "tests" / "e2e" / "run-admin-replacement-canary.sh"
SPEC = ROOT / "tests" / "e2e" / "admin-replacement-canary.spec.ts"
PAGES = ROOT / "tests" / "e2e" / "pages"
CONTRACT = ROOT / "tests" / "e2e" / "admin-test-contract.json"
MARKUP = ROOT / "admin" / "static" / "index.html"

ADMIN_REPO = "ghcr.io/basecubedev/ems-solarflow-admin"
EMS_REPO = "ghcr.io/basecubedev/ems-solarflow-api-control"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CHALLENGE = (
    'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
    'scope="repository:basecubedev/ems-solarflow-admin:pull"'
)


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


resolver = _load("resolve_canary_builds")
contract_probe = _load("admin_test_contract")
VERSION, HOOKS = contract_probe.load_contract()


def _digest(char):
    return "sha256:" + char * 64


def _sha(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _release(tag, *, prerelease=False, resources=True):
    return {
        "tag": tag,
        "name": tag,
        "channel": "rc" if prerelease or "-" in tag else "stable",
        "prerelease": prerelease,
        "published_at": "2026-10-01T08:00:00Z",
        "resources_present": resources,
    }


SOURCE_TAG = "v0.8.10"
TARGET_TAG = "v0.8.11"
CATALOGUE = {
    "main_resources_present": True,
    "releases": [
        _release("v0.9.0-RC1"),
        _release("v0.8.12", prerelease=True),
        _release("v0.8.9"),
        _release(TARGET_TAG),
        _release(SOURCE_TAG),
        _release("v0.5.0"),
    ],
}
SOURCE = {
    "tag": SOURCE_TAG,
    "revision": "a" * 40,
    "build_id": f"{SOURCE_TAG}-{'a' * 12}-100-1",
    "admin_digest": _digest("a"),
    "ems_digest": _digest("c"),
}
TARGET = {
    "tag": TARGET_TAG,
    "revision": "b" * 40,
    "build_id": f"{TARGET_TAG}-{'b' * 12}-200-1",
    "admin_digest": _digest("b"),
    "ems_digest": _digest("d"),
}


def _no_network(request, timeout=None):
    raise AssertionError(f"the release listing reached the network: {request.full_url}")


def _listing(mutate=None, *, urlopen=_no_network):
    payload = copy.deepcopy(CATALOGUE)
    if mutate is not None:
        mutate(payload)
    return resolver.admin_release_listing(lambda: payload, urlopen=urlopen)


def _labels(build, **overrides):
    labels = {
        "org.opencontainers.image.version": build["tag"],
        "org.opencontainers.image.revision": build["revision"],
        "de.basecubedev.ems.channel": "stable",
        "de.basecubedev.ems.build_serial": "41",
        "de.basecubedev.ems.build_id": build["build_id"],
        "de.basecubedev.ems.release_tag": build["tag"],
        "de.basecubedev.ems.contains_release": build["tag"],
    }
    labels.update(overrides)
    return labels


class _Registry:
    """Published images by repository and tag: a content digest and its labels."""

    def __init__(self, images):
        self.images = images

    def image(self, repository, tag):
        return self.images[(repository, tag)]


def _images(mutate=None):
    images = {}
    for build in (SOURCE, TARGET):
        images[(ADMIN_REPO, build["tag"])] = (build["admin_digest"], _labels(build))
        images[(EMS_REPO, build["tag"])] = (build["ems_digest"], _labels(build))
    if mutate is not None:
        mutate(images)
    return images


def _resolve(listing=None, images=None, **kwargs):
    return resolver.resolve(
        listing if listing is not None else _listing(),
        _Registry(images if images is not None else _images()),
        **kwargs,
    )


def _blocked(listing=None, images=None, **kwargs):
    with pytest.raises(resolver.BlockedPrecondition) as excinfo:
        _resolve(listing, images, **kwargs)
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


def test_the_pair_is_the_previous_and_the_latest_stable_release_the_admin_offers():
    listing = _listing()
    assert listing["latest_stable"] == TARGET_TAG

    source, target = _resolve(listing)

    assert source["tag"] == SOURCE_TAG
    assert target == {
        "tag": TARGET_TAG,
        "revision": TARGET["revision"],
        "build_id": TARGET["build_id"],
        "admin_image": f"{ADMIN_REPO}:{TARGET_TAG}",
        "admin_digest": TARGET["admin_digest"],
        "ems_image": f"{EMS_REPO}:{TARGET_TAG}",
        "ems_digest": TARGET["ems_digest"],
    }


def test_stable_is_what_the_admin_lists_as_stable_in_semantic_version_order():
    """v0.8.9 is catalogued first and sorts below both; v0.8.12 is a GitHub prerelease."""

    assert resolver.stable_releases(_listing()) == [TARGET_TAG, SOURCE_TAG, "v0.8.9"]


def test_the_canary_reuses_the_admins_release_and_pairing_rules():
    assert resolver.ReleaseManager is admin_releases.ReleaseManager
    assert resolver.validate_system_build_pair is admin_system_build.validate_system_build_pair
    assert resolver.classify_channel is admin_system_build.classify_channel


def test_a_release_the_admin_does_not_offer_is_never_a_canary_side():
    def without_resources(catalogue):
        for entry in catalogue["releases"]:
            if entry["tag"] == SOURCE_TAG:
                entry["resources_present"] = False

    listing = _listing(without_resources)
    assert resolver.choose_pair(listing) == ("v0.8.9", TARGET_TAG)


def test_explicit_tags_must_be_stable_releases_in_upgrade_order():
    listing = _listing()

    assert resolver.choose_pair(listing, target_tag=SOURCE_TAG) == ("v0.8.9", SOURCE_TAG)
    assert resolver.choose_pair(listing, source_tag="v0.8.9") == ("v0.8.9", TARGET_TAG)
    for target in ("v0.8.12", "v0.9.0-RC1", "v0.5.0", "v0.8.99"):
        message = _blocked(listing, target_tag=target)
        assert f"'{target}' is not a stable release the Admin offers" in message
    for source in (TARGET_TAG, "v0.8.12", "v0.9.0-RC1"):
        message = _blocked(listing, source_tag=source)
        assert f"'{source}' is not a stable release the Admin offers below" in message


def test_a_single_stable_release_is_a_blocked_precondition():
    def only_the_latest(catalogue):
        catalogue["releases"] = [
            entry for entry in catalogue["releases"] if entry["tag"] in (TARGET_TAG, "v0.9.0-RC1")
        ]

    message = _blocked(_listing(only_the_latest))
    assert f"no stable release below '{TARGET_TAG}'" in message


def test_unreadable_releases_are_a_blocked_precondition_not_an_empty_pair():
    def unreachable(request, timeout=None):
        raise OSError("network is unreachable")

    listing = resolver.admin_release_listing(lambda: None, urlopen=unreachable)
    message = _blocked(listing)
    assert "names no latest stable release" in message
    assert "GitHub releases are unavailable" in message


def test_a_listing_that_misses_a_release_page_is_a_blocked_precondition():
    def a_page_too_large(request, timeout=None):
        url = request.full_url
        if url.endswith("&page=1"):
            return io.BytesIO(b"[" + b" " * (2 * 1024 * 1024 + 10) + b"]")
        if url.endswith("&page=2"):
            releases = [
                {"tag_name": tag, "name": tag, "prerelease": False, "draft": False}
                for tag in (SOURCE_TAG, "v0.8.9")
            ]
            return io.BytesIO(json.dumps(releases).encode("utf-8"))
        raise OSError(f"no answer for {url}")

    listing = resolver.admin_release_listing(lambda: None, urlopen=a_page_too_large)
    assert listing["latest_stable"] == SOURCE_TAG
    message = _blocked(listing)
    assert "may be missing releases" in message
    assert "release page 1 could not be read" in message


def test_only_the_development_catalogue_warning_leaves_the_pair_standing():
    listing = _listing()
    listing["warnings"] = [admin_releases.DEVELOPMENT_CATALOGUE_UNREADABLE]
    assert resolver.choose_pair(listing) == (SOURCE_TAG, TARGET_TAG)

    listing["warnings"].append("The current release cannot be compared safely.")
    for named in (
        {},
        {"target_tag": TARGET_TAG},
        {"source_tag": "v0.8.9"},
        {"source_tag": "v0.8.9", "target_tag": TARGET_TAG},
    ):
        with pytest.raises(resolver.BlockedPrecondition):
            resolver.choose_pair(listing, **named)


def test_a_blocked_resolve_fails_the_step_and_writes_no_output(monkeypatch, tmp_path, capsys):
    output = tmp_path / "github-output"
    listing = _listing()
    monkeypatch.setattr(resolver, "Registry", lambda: _Registry(_images()))
    monkeypatch.setattr(resolver, "admin_release_listing", lambda: {"releases": []})
    assert resolver.main(["--github-output", str(output)]) == 1
    assert not output.exists()
    assert "replacement canary is blocked" in capsys.readouterr().err

    monkeypatch.setattr(resolver, "admin_release_listing", lambda: listing)
    assert resolver.main(["--github-output", str(output)]) == 0
    expected = resolver.outputs(*resolver.resolve(listing, _Registry(_images())))
    assert output.read_text(encoding="utf-8").splitlines() == expected


def test_every_side_is_pinned_to_a_content_digest_and_its_labels():
    source, target = _resolve()
    for entry in (source, target):
        assert DIGEST_RE.fullmatch(entry["admin_digest"])
        assert DIGEST_RE.fullmatch(entry["ems_digest"])
    assert (source["revision"], source["build_id"]) == (SOURCE["revision"], SOURCE["build_id"])


@pytest.mark.parametrize(
    "repository, label, value",
    [
        (EMS_REPO, "org.opencontainers.image.revision", "e" * 40),
        (EMS_REPO, "de.basecubedev.ems.build_id", f"{TARGET_TAG}-{'e' * 12}-300-1"),
        (ADMIN_REPO, "de.basecubedev.ems.channel", "development"),
        (ADMIN_REPO, "de.basecubedev.ems.release_tag", "v0.8.12"),
        (ADMIN_REPO, "org.opencontainers.image.version", "latest"),
    ],
)
def test_labels_that_are_not_one_stable_system_build_are_rejected(repository, label, value):
    def relabel(images):
        images[(repository, TARGET_TAG)][1][label] = value

    message = _blocked(images=_images(relabel))
    assert f"{TARGET_TAG} is not one published System Build" in message


def test_identical_source_and_target_admin_digest_is_rejected():
    def same_admin(images):
        _, labels = images[(ADMIN_REPO, SOURCE_TAG)]
        images[(ADMIN_REPO, SOURCE_TAG)] = (TARGET["admin_digest"], labels)

    assert "same Admin digest" in _blocked(images=_images(same_admin))

    result = _run_runner(CANARY_SOURCE_ADMIN_DIGEST=TARGET["admin_digest"])
    assert result.returncode != 0
    assert "replacement would assert nothing" in result.stderr


def test_outputs_name_every_value_the_workflow_reads():
    source, target = _resolve()
    written = {line.split("=", 1)[0] for line in resolver.outputs(source, target)}
    read = set(re.findall(r"steps\.pair\.outputs\.(\w+)", WORKFLOW.read_text(encoding="utf-8")))
    assert read, "the workflow reads nothing from the resolver"
    assert read <= written, sorted(read - written)


class _Ghcr:
    """A registry that challenges an anonymous read once, then serves documents."""

    def __init__(self, documents, *, token="anonymous-token"):
        self.documents = documents
        self.token = token
        self.requests = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append(request)
        if url.startswith("https://ghcr.io/token?"):
            return io.BytesIO(json.dumps({"token": self.token}).encode("utf-8"))
        if request.get_header("Authorization") != f"Bearer {self.token}":
            raise urllib.error.HTTPError(
                url, 401, "Unauthorized", {"WWW-Authenticate": CHALLENGE}, None
            )
        return io.BytesIO(self.documents[url])


def _published(tag, labels, *, platforms=(("linux", "amd64"), ("linux", "arm64"))):
    path = "basecubedev/ems-solarflow-admin"
    config = json.dumps({"architecture": "amd64", "config": {"Labels": labels}}).encode("utf-8")
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "config": {"digest": _sha(config), "size": len(config)},
            "layers": [],
        }
    ).encode("utf-8")
    entries = [
        {
            "mediaType": OCI_MANIFEST,
            "digest": _sha(manifest),
            "platform": {"os": os_name, "architecture": architecture},
        }
        for os_name, architecture in platforms
    ]
    entries.append(
        {
            "mediaType": OCI_MANIFEST,
            "digest": _digest("f"),
            "platform": {"os": "unknown", "architecture": "unknown"},
        }
    )
    index = json.dumps({"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": entries}).encode(
        "utf-8"
    )
    base = f"https://ghcr.io/v2/{path}"
    documents = {
        f"{base}/manifests/{tag}": index,
        f"{base}/manifests/{_sha(manifest)}": manifest,
        f"{base}/blobs/{_sha(config)}": config,
    }
    return _sha(index), documents


def test_a_tag_resolves_to_the_digest_of_its_index_and_the_labels_of_its_image():
    labels = _labels(TARGET)
    digest, documents = _published(TARGET_TAG, labels)
    registry = _Ghcr(documents)

    assert resolver.Registry(opener=registry).image(ADMIN_REPO, TARGET_TAG) == (digest, labels)

    manifest = next(
        request for request in registry.requests if request.full_url.endswith(f"/{TARGET_TAG}")
    )
    assert OCI_INDEX in manifest.get_header("Accept")


def test_the_registry_token_is_never_sent_past_a_redirect():
    digest, documents = _published(TARGET_TAG, _labels(TARGET))
    registry = _Ghcr(documents)
    resolver.Registry(opener=registry).image(ADMIN_REPO, TARGET_TAG)

    token_requests = [
        r for r in registry.requests if r.full_url.startswith("https://ghcr.io/token?")
    ]
    assert len(token_requests) == 1
    assert (
        "scope=repository%3Abasecubedev%2Fems-solarflow-admin%3Apull" in token_requests[0].full_url
    )
    authorized = [r for r in registry.requests if r.get_header("Authorization")]
    assert authorized
    for request in authorized:
        assert "Authorization" not in request.headers
        assert request.unredirected_hdrs["Authorization"] == "Bearer anonymous-token"


@pytest.mark.parametrize("document", ["manifests/sha256", "blobs/"])
def test_registry_content_that_does_not_match_its_digest_is_rejected(document):
    digest, documents = _published(TARGET_TAG, _labels(TARGET))
    url = next(url for url in documents if document in url)
    documents[url] = documents[url].replace(b"amd64", b"arm64").replace(b'"layers"', b'"Layers"')

    with pytest.raises(resolver.BlockedPrecondition) as excinfo:
        resolver.Registry(opener=_Ghcr(documents)).image(ADMIN_REPO, TARGET_TAG)
    assert "does not match its digest" in str(excinfo.value)


def test_an_index_without_a_linux_amd64_image_is_rejected():
    digest, documents = _published(TARGET_TAG, _labels(TARGET), platforms=(("linux", "arm64"),))

    with pytest.raises(resolver.BlockedPrecondition) as excinfo:
        resolver.Registry(opener=_Ghcr(documents)).image(ADMIN_REPO, TARGET_TAG)
    assert "publishes no linux/amd64 image" in str(excinfo.value)


def test_a_registry_that_cannot_be_read_is_a_blocked_precondition():
    def unreachable(request, timeout=None):
        raise urllib.error.URLError("no route to host")

    with pytest.raises(resolver.BlockedPrecondition) as excinfo:
        resolver.Registry(opener=unreachable).image(ADMIN_REPO, TARGET_TAG)
    assert "could not be read" in str(excinfo.value)


def test_the_runner_requires_every_digest():
    for name in ("CANARY_SOURCE_ADMIN_DIGEST", "CANARY_ADMIN_DIGEST", "CANARY_EMS_DIGEST"):
        result = _run_runner(**{name: None})
        assert result.returncode != 0
        assert f"{name} is required" in result.stderr


def test_the_runner_rejects_a_mutable_reference():
    for name in ("CANARY_SOURCE_ADMIN_DIGEST", "CANARY_ADMIN_DIGEST", "CANARY_EMS_DIGEST"):
        result = _run_runner(**{name: "latest"})
        assert result.returncode != 0
        assert "must be an immutable sha256: digest" in result.stderr


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


def test_both_admins_are_probed_for_the_contract_before_the_source_starts():
    runner = RUNNER.read_text(encoding="utf-8")
    start = runner.index('docker compose -f "$COMPOSE_FILE" up -d')
    for role in ("source", "target"):
        assert runner.index(f"--role {role}") < start


# --- workflow and journey --------------------------------------------------


def test_real_replacement_canary_is_scheduled_and_manually_runnable():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in text
    assert "schedule:" in text
    assert "admin-replacement-canary" in text
    assert "playwright.admin-replacement.config.ts" in text
    assert "/var/run/docker.sock" in RUNNER.read_text(encoding="utf-8")


def test_the_pair_comes_from_stable_releases_not_the_development_catalogue():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "python3 scripts/resolve_canary_builds.py" in text
    for retired in ("development-builds", "verify_development_catalogue", "Development"):
        assert retired not in text, retired


def test_canary_never_starts_a_mutable_source_admin():
    runner = RUNNER.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "ems-solarflow-admin:latest" not in runner
    assert "--tag latest" not in runner
    assert '--tag "$CANARY_SOURCE_TAG"' in runner
    assert "ems-solarflow-admin:latest" not in workflow
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


def test_the_journey_selects_and_asserts_a_stable_release():
    text = SPEC.read_text(encoding="utf-8")

    assert 'const CHANNEL = "stable";' in text
    assert "setup.selectChannelBuild(TAG, CHANNEL)" in text
    assert "channel: CHANNEL" in text
    assert '"de.basecubedev.ems.channel"], `${reference} channel`).toBe(CHANNEL)' in text
    assert '"de.basecubedev.ems.release_tag"' in text
    assert "development" not in text


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
