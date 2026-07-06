# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract checks for the Admin deployment-capable default (Docker-out-of-Docker).

The Admin image ships a Docker *client* only. The default compose is
deployment-capable: it mounts the host Docker socket and provides ``DOCKER_GID``
so Step 04 can control the host Docker engine. The restricted discovery-only
compose never mounts the socket.
"""

import os
import stat

import pytest

pytestmark = pytest.mark.simulation

DEPLOY_ADMIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy", "admin"
)

SOCKET_MOUNT = "/var/run/docker.sock:/var/run/docker.sock"


def _read(name):
    with open(os.path.join(DEPLOY_ADMIN_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def test_dockerfile_installs_docker_client_and_compose_plugin():
    dockerfile = _read("Dockerfile")
    # CLI + Compose plugin, host-socket control (Docker-out-of-Docker)...
    assert "docker --version" in dockerfile
    assert "cli-plugins/docker-compose" in dockerfile
    # ...but never a daemon inside the container. Ignore comment lines, which
    # deliberately document that no daemon is installed.
    instructions = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    ).lower()
    assert "dockerd" not in instructions
    assert "--privileged" not in instructions
    assert "docker-ce " not in instructions and "docker.io" not in instructions


def test_default_compose_mounts_host_docker_socket_and_group():
    compose = _read("docker-compose.yml")
    assert SOCKET_MOUNT in compose
    # The container joins the host Docker socket group so the non-root user can
    # reach the socket; the launcher fills DOCKER_GID in automatically.
    assert 'group_add' in compose
    assert "${DOCKER_GID" in compose
    # Hardened runtime settings stay in place alongside socket access.
    assert "read_only: true" in compose
    assert "DOCKER_CONFIG: /tmp/docker" in compose
    assert 'PUID: "${PUID:-1000}"' in compose
    assert 'PGID: "${PGID:-1000}"' in compose
    # Same-path mounting: env and volumes reference the real host paths so bind
    # mounts forwarded to the host Docker daemon stay host-valid.
    assert 'EMS_INSTALL_DIR: "${EMS_INSTALL_DIR}"' in compose
    assert 'EMS_ADMIN_DATA_DIR: "${EMS_ADMIN_DATA_DIR}"' in compose
    assert '"${EMS_INSTALL_DIR}:${EMS_INSTALL_DIR}"' in compose
    assert '"${EMS_ADMIN_DATA_DIR}:${EMS_ADMIN_DATA_DIR}"' in compose
    # The security impact of mounting the socket is documented in-file.
    assert "SECURITY" in compose


def test_discovery_only_and_hostnet_do_not_mount_docker_socket():
    for name in ("docker-compose.discovery-only.yml", "docker-compose.hostnet.yml"):
        content = _read(name)
        assert "docker.sock" not in content, name
        assert "DOCKER_GID" not in content, name


def test_discovery_only_uses_host_visible_admin_data_path_contract():
    compose = _read("docker-compose.discovery-only.yml")
    # Restricted mode mounts no socket but still uses same-path mounting so the
    # Admin server resolves the real install context consistently.
    assert 'EMS_INSTALL_DIR: "${EMS_INSTALL_DIR}"' in compose
    assert 'EMS_ADMIN_DATA_DIR: "${EMS_ADMIN_DATA_DIR}"' in compose
    assert '"${EMS_INSTALL_DIR}:${EMS_INSTALL_DIR}"' in compose
    assert '"${EMS_ADMIN_DATA_DIR}:${EMS_ADMIN_DATA_DIR}"' in compose
    assert 'PUID: "${PUID:-1000}"' in compose
    assert 'PGID: "${PGID:-1000}"' in compose


def test_hostnet_does_not_override_admin_data_path():
    compose = _read("docker-compose.hostnet.yml")
    assert "EMS_ADMIN_DATA_DIR" not in compose
    assert "../../data/admin" not in compose


def test_launcher_exports_docker_gid_automatically():
    script = _read("start-admin-setup.sh")
    # The default path derives DOCKER_GID from the socket owner group, with a
    # getent fallback, and exports it for Compose — no manual step required.
    assert "stat -c '%g'" in script
    assert "getent group docker" in script
    assert "export DOCKER_GID" in script
    assert "export PUID PGID" in script
    assert 'PUID="${PUID:-$(id -u)}"' in script
    assert 'PGID="${PGID:-$(id -g)}"' in script
    assert 'project_root="$(CDPATH= cd -- "$here/../.." && pwd)"' in script
    # The install root is exported from the computed project root so the container
    # resolves the real EMS layout instead of the /app image fallback.
    assert 'export EMS_INSTALL_DIR="$project_root"' in script
    assert 'export EMS_ADMIN_DATA_DIR="$admin_data_dir"' in script
    # Admin-owned dirs only; the live EMS runtime lives in the install root, not
    # in a transitional data/admin/deployment/ workspace.
    assert '"$admin_data_dir/staging"' in script
    assert '"$admin_data_dir/backups"' in script
    assert "/deployment/config" not in script
    assert "/deployment/data" not in script
    # The default file is deployment-capable; --discovery-only selects restricted.
    assert "docker-compose.yml" in script
    assert "--discovery-only" in script
    assert "docker-compose.discovery-only.yml" in script


def test_launcher_is_executable():
    path = os.path.join(DEPLOY_ADMIN_DIR, "start-admin-setup.sh")
    mode = os.stat(path).st_mode
    assert mode & stat.S_IXUSR


def test_source_compose_files_build_locally_not_from_ghcr():
    # These are the source/developer local-build files. The runtime/end-user
    # counterparts (docker-compose.runtime*.yml) use the published GHCR image
    # with no build section — covered in test_admin_installer.py.
    for name in ("docker-compose.yml", "docker-compose.discovery-only.yml"):
        compose = _read(name)
        assert "build:" in compose, name
        assert "dockerfile: deploy/admin/Dockerfile" in compose, name
        assert "ghcr.io/basecubedev/ems-solarflow-admin" not in compose, name
