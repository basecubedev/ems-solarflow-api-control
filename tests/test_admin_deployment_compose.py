# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract checks for the Admin deployment-capable default (Docker-out-of-Docker).

The Admin image ships a Docker *client* only. The default compose is
deployment-capable: it mounts the host Docker socket and provides ``DOCKER_GID``
so Step 04 can control the host Docker engine. The restricted discovery-only
compose never mounts the socket.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.contract,
    pytest.mark.simulation,
]

DEPLOY_ADMIN_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "deploy", "admin"
)
ROOT = Path(DEPLOY_ADMIN_DIR).parents[1]

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


# --- paired local System Build identity ----------------------------------


LOCAL_REVISION = "c7b2f136c5cc7d0a1a00002fd183baa21869799f"


def _write_fake_launcher_tools(
    tmp_path, *, revision=LOCAL_REVISION, dirty=False, git_available=True
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "docker-env.txt"

    git = bin_dir / "git"
    if git_available:
        git.write_text(
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            "  *\" --is-inside-work-tree \"*) echo true ;;\n"
            "  *\" rev-parse \"*\" --short\"*) printf '%.7s\\n' \"$FAKE_GIT_REVISION\" ;;\n"
            "  *\" rev-parse \"*\" HEAD \"*) printf '%s\\n' \"$FAKE_GIT_REVISION\" ;;\n"
            "  *\" status \"*) [ \"${FAKE_GIT_DIRTY:-0}\" = 1 ] && "
            "printf ' M admin/server.py\\n'; exit 0 ;;\n"
            "  *\" diff-index \"*) [ \"${FAKE_GIT_DIRTY:-0}\" = 1 ] && exit 1; exit 0 ;;\n"
            "  *) exit 0 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
    else:
        git.write_text(
            "#!/bin/sh\necho 'git metadata unavailable' >&2\nexit 127\n",
            encoding="utf-8",
        )
    git.chmod(0o755)

    docker = bin_dir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        "{\n"
        "  printf 'SYSTEM_TAG=%s\\n' \"${SYSTEM_TAG-}\"\n"
        "  printf 'SYSTEM_CHANNEL=%s\\n' \"${SYSTEM_CHANNEL-}\"\n"
        "  printf 'SYSTEM_REVISION=%s\\n' \"${SYSTEM_REVISION-}\"\n"
        "  printf 'SYSTEM_BUILD_ID=%s\\n' \"${SYSTEM_BUILD_ID-}\"\n"
        "  printf 'SYSTEM_RELEASE_TAG=%s\\n' \"${SYSTEM_RELEASE_TAG-}\"\n"
        "  printf 'ARGV=%s\\n' \"$*\"\n"
        "} > \"$FAKE_DOCKER_CAPTURE\"\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PUID": str(os.getuid()),
        "PGID": str(os.getgid()),
        "FAKE_GIT_REVISION": revision,
        "FAKE_GIT_DIRTY": "1" if dirty else "0",
        "FAKE_DOCKER_CAPTURE": str(capture),
        "EMS_ADMIN_DATA_DIR": str(tmp_path / "admin-data"),
    }
    return env, capture


def _run_local_launcher(tmp_path, **fake_git):
    env, capture = _write_fake_launcher_tools(tmp_path, **fake_git)
    result = subprocess.run(
        [str(Path(DEPLOY_ADMIN_DIR) / "start-admin-setup.sh"), "--discovery-only"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    values = {}
    if capture.is_file():
        for line in capture.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            values[key] = value
    return result, values


@pytest.mark.skipif(not hasattr(os, "getuid") or os.getuid() == 0,
                    reason="local launcher requires a non-root POSIX uid")
def test_local_launcher_exports_clean_repository_system_build_identity(tmp_path):
    result, values = _run_local_launcher(tmp_path)

    assert result.returncode == 0, result.stderr
    assert values == {
        "SYSTEM_TAG": "local",
        "SYSTEM_CHANNEL": "development",
        "SYSTEM_REVISION": LOCAL_REVISION,
        "SYSTEM_BUILD_ID": "local-c7b2f13",
        "SYSTEM_RELEASE_TAG": "local",
        "ARGV": (
            f"compose -f {Path(DEPLOY_ADMIN_DIR) / 'docker-compose.discovery-only.yml'} "
            "up"
        ),
    }


@pytest.mark.skipif(not hasattr(os, "getuid") or os.getuid() == 0,
                    reason="local launcher requires a non-root POSIX uid")
def test_local_launcher_marks_dirty_repository_identity(tmp_path):
    result, values = _run_local_launcher(tmp_path, dirty=True)

    assert result.returncode == 0, result.stderr
    assert values["SYSTEM_BUILD_ID"] == "local-c7b2f13-dirty"
    assert values["SYSTEM_REVISION"] == LOCAL_REVISION


def test_local_launcher_builds_and_tags_both_fixed_repository_images():
    script = _read("start-admin-setup.sh")

    assert "docker compose $files build" in script
    assert (
        "ghcr.io/basecubedev/ems-solarflow-admin:local" in script
    )
    assert (
        "ghcr.io/basecubedev/ems-solarflow-api-control:local" in script
    )
    assert 'docker build \\\n' in script
    for build_arg in (
        "EMS_RELEASE_TAG",
        "EMS_GIT_COMMIT",
        "EMS_GIT_COMMIT_SHORT",
        "EMS_GIT_DIRTY",
        "EMS_BUILD_ID",
        "EMS_BUILD_SERIAL",
        "EMS_CHANNEL",
    ):
        assert f"--build-arg {build_arg}=" in script


@pytest.mark.skipif(not hasattr(os, "getuid") or os.getuid() == 0,
                    reason="local launcher requires a non-root POSIX uid")
def test_local_launcher_fails_actionably_without_git_metadata(tmp_path):
    result, values = _run_local_launcher(tmp_path, git_available=False)

    assert result.returncode != 0
    assert values == {}
    assert "git" in result.stderr.lower()
    assert any(word in result.stderr.lower() for word in ("revision", "metadata", "repository"))


@pytest.mark.skipif(not hasattr(os, "getuid") or os.getuid() == 0,
                    reason="local launcher requires a non-root POSIX uid")
def test_local_launcher_rejects_invalid_full_revision(tmp_path):
    result, values = _run_local_launcher(tmp_path, revision="not-a-git-revision")

    assert result.returncode != 0
    assert values == {}
    assert "revision" in result.stderr.lower()


@pytest.mark.parametrize(
    "compose_name", ("docker-compose.yml", "docker-compose.discovery-only.yml")
)
def test_local_compose_forwards_every_system_build_argument(compose_name):
    compose = _read(compose_name)
    expected = {
        "EMS_SYSTEM_TAG": "SYSTEM_TAG",
        "EMS_CHANNEL": "SYSTEM_CHANNEL",
        "EMS_REVISION": "SYSTEM_REVISION",
        "EMS_BUILD_ID": "SYSTEM_BUILD_ID",
        "EMS_RELEASE_TAG": "SYSTEM_RELEASE_TAG",
    }
    assert "args:" in compose
    for build_arg, environment_name in expected.items():
        assert build_arg in compose, f"{compose_name} does not pass {build_arg}"
        assert f"${{{environment_name}" in compose, (
            f"{compose_name} does not source {build_arg} from {environment_name}"
        )
