# SPDX-License-Identifier: AGPL-3.0-or-later
"""The gaps between the simulated A/B model and a real rpi-image-gen appliance.

Every test here describes something an operator would only discover on real
hardware: a build wrapper that rejects the official generator, a shared path
that was never actually mounted, a service that started anyway, a slot that
committed itself without the application runtime it is supposed to carry.

The upstream contract these are written against is pinned in
``packaging/appliance/image/rpi-image-gen.lock`` and explained in
``docs/appliance/adr/rpi-image-gen-image-rota.md``.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from appliance import ab_persistence

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "appliance"
IMAGE = PACKAGING / "image"
SYSTEMD = PACKAGING / "systemd"
SCRIPTS = ROOT / "scripts"


def read(path):
    return Path(path).read_text(encoding="utf-8")

def unit_directives(text, key):
    """Every value of a directive; systemd accumulates repeated keys."""

    values = set()
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(f"{key}="):
            values.update(line[len(key) + 1 :].split())
    return values


# --- the official generator --------------------------------------------------


def official_checkout(tmp_path):
    """A checkout shaped like the pinned upstream release, and nothing else.

    Deliberately has no ``build.sh``: the interface this project used to expect
    does not exist upstream and never did on the releases that provide A/B.
    """

    root = tmp_path / "rpi-image-gen"
    (root / "image" / "gpt" / "ab_userdata").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    generator = root / "rpi-image-gen"
    generator.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    generator.chmod(0o755)
    (root / "LICENSE").write_text("upstream\n", encoding="utf-8")
    (root / "depends").write_text("all:bash\n", encoding="utf-8")
    (root / "image" / "gpt" / "ab_userdata" / "image.yaml").write_text(
        "# METABEGIN\n# X-Env-Layer-Name: image-rota\n# METAEND\n", encoding="utf-8"
    )
    overlay = (
        root
        / "image"
        / "gpt"
        / "ab_userdata"
        / "device"
        / "rootfs-overlay"
        / "usr"
        / "lib"
        / "systemd"
        / "system-generators"
    )
    overlay.mkdir(parents=True)
    (overlay / "slot-shared-generator").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "config" / "trixie-minbase-ab.yaml").write_text(
        "image:\n  layer: image-rota\n", encoding="utf-8"
    )
    return root


def run_build_wrapper(checkout, tmp_path):
    # /sbin holds the partition tooling the wrapper probes for; without it the
    # run would abort before it ever looks at the generator.
    environment = dict(os.environ)
    environment["PATH"] = environment.get("PATH", "") + ":/sbin:/usr/sbin"
    return subprocess.run(
        [
            "sh",
            str(SCRIPTS / "appliance-build-rpi-ab-image.sh"),
            "--rpi-image-gen",
            str(checkout),
            "--output",
            str(tmp_path / "out"),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env=environment,
    )


def test_the_official_rpi_image_gen_checkout_is_not_rejected(tmp_path):
    """The wrapper must recognise the real generator, not a build.sh checkout."""

    result = run_build_wrapper(official_checkout(tmp_path), tmp_path)
    combined = result.stdout + result.stderr

    assert "rpi_image_gen_unavailable" not in combined, combined
    assert "does not look like an rpi-image-gen checkout" not in combined, combined


def test_the_build_wrapper_does_not_require_build_sh():
    """``build.sh`` is not part of any upstream release that provides A/B."""

    assert "build.sh" not in read(SCRIPTS / "appliance-build-rpi-ab-image.sh")


def test_the_upstream_contract_is_pinned_to_an_exact_revision():
    lock = json.loads(read(IMAGE / "rpi-image-gen.lock"))

    assert lock["repository"].endswith("rpi-image-gen")
    assert re.fullmatch(r"[0-9a-f]{40}", lock["commit"])
    assert lock["image_layer"] == "image-rota"
    assert lock["shared_slot_mechanism"] == "slot-shared"


# --- the layout is upstream's, not this project's ----------------------------


def test_no_committed_file_declares_a_fixed_partuuid():
    """Two appliances on one bus must not claim the same partition identity."""

    offenders = []
    for path in sorted(IMAGE.rglob("*")):
        if not path.is_file():
            continue
        for match in re.finditer(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            read(path).lower(),
        ):
            offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")

    assert not offenders, "fixed partition identities are still declared: " + "; ".join(
        offenders
    )


def test_the_project_does_not_generate_its_own_partition_table():
    """``image-rota`` owns the partition table; a second generator is drift."""

    assert not (IMAGE / "manifests" / "layout.json").exists()
    assert not (IMAGE / "hooks" / "10-partition.sh").exists()
    assert not (IMAGE / "hooks" / "50-duplicate-slots.sh").exists()


# --- persistence is actually mounted -----------------------------------------


def test_the_image_declares_every_shared_path_to_the_upstream_generator():
    """Without a slot-shared conf nothing is bound and the fallback is silent."""

    conf = read(
        IMAGE
        / "layer"
        / "ems-appliance.rootfs-overlay"
        / "etc"
        / "rpi-image-gen"
        / "slot-shared.d"
        / "50-ems-appliance.conf"
    )
    declared = {line.split("=", 1)[1].strip() for line in conf.splitlines() if line.startswith("Path=")}

    assert "Version=1" in conf
    required = {shared.target for shared in ab_persistence.SHARED_PATHS if shared.required}
    assert required <= declared, f"not declared to slot-shared: {sorted(required - declared)}"


def test_the_shared_paths_resolve_under_the_upstream_shared_root():
    for shared in ab_persistence.SHARED_PATHS:
        assert ab_persistence.source_path(shared) == f"/persistent/shared{shared.target}"


@pytest.mark.parametrize("unit", ["ems-appliance-agent.service", "ems-appliance-web.service"])
def test_the_appliance_services_require_verified_persistence(unit):
    """``Before=`` is ordering, not a failure dependency: these must not start."""

    text = read(SYSTEMD / unit)

    assert "ems-appliance-persistence.service" in unit_directives(text, "Requires")
    assert "ems-appliance-persistence.service" in unit_directives(text, "After")


def test_the_health_service_requires_verified_persistence():
    text = read(SYSTEMD / "ems-appliance-ab-health.service")

    assert "Requires=ems-appliance-persistence.service" in text


# --- host identity -----------------------------------------------------------


def test_the_whole_etc_ssh_is_not_shared_between_slots():
    """Sharing the distro's sshd config couples one slot's OS to the other's."""

    targets = {shared.target for shared in ab_persistence.SHARED_PATHS}

    assert "/etc/ssh" not in targets
    assert not any(target.rstrip("/") == "/etc/ssh" for target in targets)


def test_ssh_host_identity_survives_a_slot_switch():
    """The keys are shared even though ``/etc/ssh`` as a whole is not."""

    assert ab_persistence.SSH_HOST_KEY_DIRECTORY.startswith("/var/lib/ems-appliance-manager")
    targets = {shared.target for shared in ab_persistence.SHARED_PATHS}
    assert any(ab_persistence.SSH_HOST_KEY_DIRECTORY.startswith(target) for target in targets)


def test_the_machine_identity_is_stable_across_a_slot_switch():
    """One physical appliance is one Linux machine, whichever slot booted."""

    contract = ab_persistence.contract()

    assert contract["machine_identity"]["source"] == "/persistent/common/etc/machine-id"
    assert contract["machine_identity"]["stable_across_slots"] is True
    assert "/etc/machine-id" not in contract["slot_local"]


# --- the trial slot ----------------------------------------------------------


def test_the_pending_trial_record_lives_on_shared_storage():
    """A trial record inside the source slot is invisible to the trial slot."""

    targets = {shared.target for shared in ab_persistence.SHARED_PATHS if shared.required}

    assert "/var/lib/ems-appliance-os-update" in targets


def test_a_trial_slot_without_shared_ab_state_is_not_healthy(tmp_path):
    """A trial that cannot see the shared record cannot prove what it is."""

    from appliance.ab_state import AbStateStore
    from tests.helpers.appliance_ab import ApplianceAbHost, build_health_service

    host = ApplianceAbHost(tmp_path, slot="B", tryboot=True)
    state = AbStateStore(host.ab_state_dir)
    state.ensure()
    host.unmount("/var/lib/ems-appliance-os-update")
    host.apply_mounts()

    gates = build_health_service(host, state).gates(host.discover())
    names = {gate.name for gate in gates}

    assert "ab_state_shared" in names
    assert not next(gate for gate in gates if gate.name == "ab_state_shared").passed


def test_a_slot_without_an_admin_runtime_does_not_become_known_good(tmp_path):
    """A committed slot whose application runtime is gone is not recoverable."""

    from appliance.ab_health import RESULT_UNHEALTHY
    from appliance.ab_state import AbStateStore, PendingTrial
    from tests.helpers.appliance_ab import (
        ApplianceAbHost,
        FakeDocker,
        build_health_service,
    )

    host = ApplianceAbHost(tmp_path, slot="B", tryboot=True)
    host.write_os_build({"release_version": "1.5.0", "build_id": "b-1"})
    state = AbStateStore(host.ab_state_dir)
    state.ensure()
    state.set_pending(
        PendingTrial(
            operation_id="op-1",
            source_slot="A",
            target_slot="B",
            target_release="r",
            target_build_id="b-1",
            artifact_digest="sha256:" + "c" * 64,
            expected_boot_partition=host.slot_boot_partition("B"),
            expected_root_partuuid="",
            trial_requested_at=1000.0,
        )
    )
    service = build_health_service(
        host, state, docker=FakeDocker(admin=False), time_fn=lambda: 1100.0
    )

    report = service.evaluate()

    assert report.result == RESULT_UNHEALTHY
    assert any("admin" in reason for reason in report.reasons)


def test_the_inactive_root_filesystem_is_inspected_before_tryboot_is_armed(tmp_path):
    """Matching bytes are not proof that the slot carries a bootable appliance."""

    from appliance.os_update import OsUpdateService

    assert "inspector" in OsUpdateService.__init__.__code__.co_varnames, (
        "the update service has no inactive-slot inspection step"
    )
