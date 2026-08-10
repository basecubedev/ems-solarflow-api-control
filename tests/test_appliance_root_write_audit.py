# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Appliance package under a root that really is read-only.

"The slot root is read-only" was an architecture statement. image-rota writes it
into /etc/fstab, the persistence contract is built on it and the kit's expected
layout describes it — and nothing had ever run the package's own write paths
against a root where it was true.

Running them found two things that only a real read-only root can find: three
mount points the image never created, which systemd cannot create on a
filesystem it may not write and which would therefore be bind mounts that never
happen on hardware; and an export-root setup that chowned and chmodded
directories whose ownership and mode were already correct, which fails with
EROFS regardless.

The guest is a disposable container whose root is read-only from creation —
overlayfs cannot be remounted read-only afterwards — with /run and /tmp as
tmpfs and the per-slot and shared mounts made the way upstream's generators
make them.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.docker, pytest.mark.system_build]

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "packaging/appliance/image/layer/ems-appliance.rootfs-overlay"
AUDIT = "appliance-audit-root-writes.sh"

DOCKERFILE = """
FROM debian:trixie-slim
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \\
      systemd python3 acl iproute2 procps util-linux mount ca-certificates \\
      passwd zstd gpgv openssh-client openssh-server \\
    && rm -rf /var/lib/apt/lists/*
COPY package.deb /tmp/pkg.deb
RUN dpkg -i /tmp/pkg.deb && rm -f /tmp/pkg.deb
COPY overlay/ /
# What the image layer creates at build time, while the root is still writable.
RUN install -d -m 0755 /persistent /opt/ems-solarflow /var/lib/ems-appliance-manager \\
      /var/log/ems-appliance-manager /etc/ems-appliance-manager \\
      /var/lib/ems-appliance-os-update /etc/NetworkManager/system-connections
"""

requires_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="a real Docker daemon runs this tier"
)


@pytest.fixture(scope="module")
def audit(tmp_path_factory):
    """One guest build and one audit run; every case below reads its report."""

    if shutil.which("docker") is None:
        pytest.skip("no docker")
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True, text=True, check=False, timeout=120,
    )
    if probe.returncode != 0:
        pytest.skip(f"no reachable Docker daemon: {probe.stderr.strip()[:120]}")

    work = tmp_path_factory.mktemp("root-audit")
    built = subprocess.run(
        ["sh", str(ROOT / "packaging/appliance/build-deb.sh"),
         "--output", str(work / "deb"), "--arch", "amd64"],
        capture_output=True, text=True, check=False, timeout=900,
    )
    package = work / "deb/ems-appliance-manager_0.1.0_amd64.deb"
    if not package.is_file():
        pytest.skip(f"the package could not be built: {built.stderr.strip()[-200:]}")

    shutil.copy2(package, work / "package.deb")
    # symlinks=True: the overlay activates its bind mounts with links into
    # /run/systemd/generator, which only exists once systemd's fstab generator
    # has run. Following them here would resolve them on the build host, where
    # they are dangling, and copy nothing.
    shutil.copytree(OVERLAY, work / "overlay", symlinks=True)
    (work / "Dockerfile").write_text(DOCKERFILE)

    tag = "ems-appliance-root-audit:pytest"
    build = subprocess.run(
        ["docker", "build", "-q", "-t", tag, str(work)],
        capture_output=True, text=True, check=False, timeout=1800,
    )
    if build.returncode != 0:
        pytest.skip(f"the guest could not be built: {build.stderr.strip()[-300:]}")

    result = subprocess.run(
        ["docker", "run", "--rm", "--privileged", "--read-only",
         "--tmpfs", "/run", "--tmpfs", "/tmp",
         "-v", f"{ROOT}/scripts:/scripts:ro",
         "-v", f"{work / 'deb'}:/deb:ro",
         tag, "sh", "-c",
         f"sh /scripts/{AUDIT} --package /deb/{package.name} --work /tmp/audit"],
        capture_output=True, text=True, check=False, timeout=1800,
    )
    if "RESULT:" not in result.stdout:
        pytest.skip("the guest could not run the audit: " + result.stderr.strip()[-300:])
    return result


def assert_case(report, name):
    for line in report.stdout.splitlines():
        if name in line and line.rstrip().endswith("PASS"):
            return
    pytest.fail(f"{name!r} did not pass:\n{report.stdout}")


@requires_docker
def test_the_package_operates_on_a_read_only_slot_root(audit):
    assert "RESULT: PASS" in audit.stdout, audit.stdout + audit.stderr
    assert audit.returncode == 0


@requires_docker
def test_the_guest_really_had_a_read_only_root(audit):
    """A writable root would make every case below prove nothing."""

    assert_case(audit, "the slot root refuses a write")
    for path in ("/etc", "/usr", "/opt", "/srv", "/root"):
        assert_case(audit, f"{path} refuses a write")


@requires_docker
def test_every_mount_point_the_contract_needs_is_in_the_image(audit):
    """The defect: systemd cannot create one on a filesystem it may not write."""

    assert_case(audit, "the persistent partition has a mount point in the image")
    assert_case(audit, "every shared path has a mount point in the image")


@requires_docker
def test_the_declared_mutable_paths_really_are_mutable(audit):
    for path in ("/run", "/tmp", "/var", "/persistent", "/home"):
        assert_case(audit, f"{path} is mutable, as the contract declares")


@requires_docker
def test_the_boot_write_paths_run_without_writing_the_root(audit):
    """The other defect: a chown that changes nothing still fails with EROFS."""

    assert_case(audit, "the export root is built without writing the slot root")
    assert_case(audit, "the host identity is ensured")
    assert_case(audit, "no file was created outside the declared mutable set")
