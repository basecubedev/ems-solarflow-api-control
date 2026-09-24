# SPDX-License-Identifier: AGPL-3.0-or-later
"""Resolve and edit the Admin container's deployment files.

The appliance never invents a deployment layout. It discovers the compose and
environment files an existing EMS installation already uses and edits exactly
one value in them: the Admin image reference. The previous bytes are always
kept so a rollback can restore them unchanged.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from appliance.auth import deployment_owner
from appliance.paths import atomic_write

COMPOSE_CANDIDATES = ("docker-compose.admin.yml", "docker-compose.yml")
ENV_CANDIDATES = (".env.admin", "admin/environment")

ENV_IMAGE_KEY = "EMS_ADMIN_IMAGE"
ENV_TAG_KEY = "EMS_ADMIN_TAG"
ENV_DIGEST_KEY = "EMS_ADMIN_DIGEST"

TAG_SOURCE_ENV = "environment"
TAG_SOURCE_COMPOSE = "compose"

_SERVICES = re.compile(r"^services:\s*$")
_ENVIRONMENT = re.compile(r"^(\s*)environment:\s*$")
_IMAGE_LINE = re.compile(r"^(\s*image:\s*)(\S.*?)\s*$")
_TAG_VARIABLE = re.compile(r"\$\{" + ENV_TAG_KEY + r"(:?-[^}]*)?\}")


class DeploymentError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AdminDeployment:
    compose_file: Path
    env_file: Path
    service: str
    container: str
    compose_exists: bool
    env_exists: bool
    service_defined: bool
    image_reference: str
    tag_source: str

    def to_dict(self):
        return {
            "compose_file": str(self.compose_file),
            "env_file": str(self.env_file),
            "service": self.service,
            "container": self.container,
            "compose_exists": self.compose_exists,
            "env_exists": self.env_exists,
            "service_defined": self.service_defined,
            "image_reference": self.image_reference,
            "tag_source": self.tag_source,
        }


def deployment_file_owner(compose_file):
    """The identity a deployment's own files belong to, or ``None``.

    The Admin container runs as the owner of the install root and updates itself
    from inside, so these files stay with that owner rather than becoming the
    agent's. The agent writes them as root with the shared socket group, and that
    group is a grant on the socket, not a read grant on the deployment.
    """

    return deployment_owner(Path(compose_file).parent)


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _service_block(lines, service):
    """Return ``(start, end)`` line indexes of ``service`` inside ``services:``."""

    in_services = False
    service_indent = None
    start = None
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _SERVICES.match(line):
            in_services = True
            continue
        if not in_services:
            continue
        indent = _indent(line)
        if indent == 0:
            if start is not None:
                return start, index
            in_services = False
            continue
        if service_indent is None:
            service_indent = indent
        if indent == service_indent:
            if start is not None:
                return start, index
            if line.strip().rstrip(":") == service:
                start = index
    if start is not None:
        return start, len(lines)
    return None, None


def read_service_image(compose_text, service):
    lines = compose_text.splitlines()
    start, end = _service_block(lines, service)
    if start is None:
        return None
    for line in lines[start + 1 : end]:
        match = _IMAGE_LINE.match(line)
        if match:
            return match.group(2).strip().strip("'\"")
    return None


def set_service_image(compose_text, service, image_reference):
    lines = compose_text.splitlines(keepends=True)
    plain = compose_text.splitlines()
    start, end = _service_block(plain, service)
    if start is None:
        raise DeploymentError("admin_service_missing", f"service {service} is not in the compose file")
    for index in range(start + 1, end):
        match = _IMAGE_LINE.match(plain[index])
        if match:
            newline = "\n" if lines[index].endswith("\n") else ""
            lines[index] = f"{match.group(1)}{image_reference}{newline}"
            return "".join(lines)
    raise DeploymentError("admin_image_missing", f"service {service} has no image entry")


def set_service_environment(compose_text, service, values):
    """Set literal ``KEY: "value"`` entries in the service's environment block.

    The block is opened when the service has none. A list-form environment is a
    different shape and is left alone rather than rewritten into something the
    operator did not write.
    """

    lines = compose_text.splitlines(keepends=True)
    plain = compose_text.splitlines()
    start, end = _service_block(plain, service)
    if start is None:
        raise DeploymentError(
            "admin_service_missing", f"service {service} is not in the compose file"
        )

    env_index = None
    env_indent = 0
    for index in range(start + 1, end):
        match = _ENVIRONMENT.match(plain[index])
        if match:
            env_index, env_indent = index, len(match.group(1))
            break

    if env_index is None:
        keys = [i for i in range(start + 1, end) if plain[i].strip()]
        indent = _indent(plain[keys[0]]) if keys else _indent(plain[start]) + 2
        opened = [f"{' ' * indent}environment:\n"]
        opened += [f'{" " * (indent + 2)}{key}: "{value}"\n' for key, value in values.items()]
        return "".join(lines[: start + 1] + opened + lines[start + 1 :])

    entries = []
    for index in range(env_index + 1, end):
        if not plain[index].strip():
            continue
        if _indent(plain[index]) <= env_indent:
            break
        entries.append(index)
    if any(plain[index].lstrip().startswith("- ") for index in entries):
        return compose_text

    indent = _indent(plain[entries[0]]) if entries else env_indent + 2
    remaining = dict(values)
    for index in entries:
        key = plain[index].lstrip().partition(":")[0].strip()
        if key in remaining:
            newline = "\n" if lines[index].endswith("\n") else ""
            lines[index] = f'{" " * indent}{key}: "{remaining.pop(key)}"{newline}'
    if remaining:
        after = entries[-1] if entries else env_index
        if not lines[after].endswith("\n"):
            lines[after] += "\n"
        added = [f'{" " * indent}{key}: "{value}"\n' for key, value in remaining.items()]
        lines = lines[: after + 1] + added + lines[after + 1 :]
    return "".join(lines)


def read_env(env_text):
    values = {}
    for line in (env_text or "").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        values[key.strip()] = value.strip()
    return values


def set_env(env_text, key, value):
    lines = (env_text or "").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        entry = line.strip()
        if entry.startswith("#") or "=" not in entry:
            continue
        if entry.partition("=")[0].strip() == key:
            lines[index] = f"{key}={value}"
            replaced = True
    if not replaced:
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def compose_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def resolve_deployment(paths, config):
    """Discover which compose/env files carry the Admin service."""

    service = config.admin_service
    compose_file = paths.install_root / COMPOSE_CANDIDATES[0]
    compose_text = None
    service_defined = False

    for candidate in COMPOSE_CANDIDATES:
        path = paths.install_root / candidate
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if read_service_image(text, service) is not None:
            compose_file, compose_text, service_defined = path, text, True
            break
        if compose_text is None:
            compose_file, compose_text = path, text

    env_file = paths.admin_env_file
    env_text = None
    for candidate in ENV_CANDIDATES:
        path = paths.install_root / candidate
        if path.is_file():
            try:
                env_text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            env_file = path
            break

    image_reference = read_service_image(compose_text or "", service) or ""
    tag_source = TAG_SOURCE_ENV if _TAG_VARIABLE.search(image_reference) else TAG_SOURCE_COMPOSE

    return AdminDeployment(
        compose_file=compose_file,
        env_file=env_file,
        service=service,
        container=config.admin_container,
        compose_exists=compose_text is not None,
        env_exists=env_text is not None,
        service_defined=service_defined,
        image_reference=image_reference,
        tag_source=tag_source,
    )


@dataclass(frozen=True)
class DeploymentSnapshot:
    compose_file: Path
    compose_text: str
    env_file: Path
    env_text: str
    compose_hash: str

    def restore(self):
        """Write the saved bytes back unchanged (rollback safety)."""

        owner = deployment_file_owner(self.compose_file)
        if self.compose_text is not None:
            atomic_write(self.compose_file, self.compose_text, owner=owner)
        if self.env_text is not None:
            atomic_write(self.env_file, self.env_text, owner=owner)
        return True


def snapshot(deployment):
    try:
        compose_text = deployment.compose_file.read_text(encoding="utf-8")
    except OSError:
        compose_text = None
    try:
        env_text = deployment.env_file.read_text(encoding="utf-8")
    except OSError:
        env_text = None
    return DeploymentSnapshot(
        compose_file=deployment.compose_file,
        compose_text=compose_text,
        env_file=deployment.env_file,
        env_text=env_text,
        compose_hash=compose_hash(compose_text or ""),
    )


def apply_image(deployment, repository, tag):
    """Point the Admin service at ``repository:tag`` using the file that owns it."""

    owner = deployment_file_owner(deployment.compose_file)
    if deployment.tag_source == TAG_SOURCE_ENV:
        try:
            env_text = deployment.env_file.read_text(encoding="utf-8")
        except OSError:
            env_text = ""
        env_text = set_env(env_text, ENV_IMAGE_KEY, repository)
        env_text = set_env(env_text, ENV_TAG_KEY, tag)
        atomic_write(deployment.env_file, env_text, owner=owner)
        return {"file": str(deployment.env_file), "mode": TAG_SOURCE_ENV}

    try:
        compose_text = deployment.compose_file.read_text(encoding="utf-8")
    except OSError:
        raise DeploymentError("compose_file_missing", "the Admin compose file is unreadable")
    updated = set_service_image(compose_text, deployment.service, f"{repository}:{tag}")
    atomic_write(deployment.compose_file, updated, owner=owner)
    return {"file": str(deployment.compose_file), "mode": TAG_SOURCE_COMPOSE}


def apply_digest(deployment, repository, digest, *, tag=""):
    """Pin the Admin service to ``repository@sha256:...``.

    The compose file always carries the immutable reference, whatever the
    original deployment used, because a tag variable cannot express a digest.
    The human-readable tag is only mirrored into the environment file so an
    operator can still see which release the digest belongs to.
    """

    reference = f"{repository}@{digest}"
    owner = deployment_file_owner(deployment.compose_file)
    try:
        compose_text = deployment.compose_file.read_text(encoding="utf-8")
    except OSError:
        raise DeploymentError("compose_file_missing", "the Admin compose file is unreadable")

    updated = set_service_image(compose_text, deployment.service, reference)
    if tag:
        # The container reports its own version from here, not from the
        # environment file, which this install only keeps for reference.
        updated = set_service_environment(updated, deployment.service, {ENV_TAG_KEY: tag})
    atomic_write(deployment.compose_file, updated, owner=owner)

    if deployment.env_file.is_file():
        try:
            env_text = deployment.env_file.read_text(encoding="utf-8")
            env_text = set_env(env_text, ENV_IMAGE_KEY, repository)
            if tag:
                env_text = set_env(env_text, ENV_TAG_KEY, tag)
            env_text = set_env(env_text, ENV_DIGEST_KEY, digest)
            atomic_write(deployment.env_file, env_text, owner=owner)
        except OSError:
            raise DeploymentError(
                "environment_write_failed", "the Admin environment file could not be updated"
            )

    return {"reference": reference, "compose_file": str(deployment.compose_file)}


def environment_hash(deployment):
    try:
        return compose_hash(deployment.env_file.read_text(encoding="utf-8"))
    except OSError:
        return ""
