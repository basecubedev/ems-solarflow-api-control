# SPDX-License-Identifier: AGPL-3.0-or-later
"""Release discovery and setup-resource caching for the Admin wizard."""

import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote


REPO = "basecubedev/ems-solarflow-api-control"
DOCKER_IMAGE_REPOSITORY = f"ghcr.io/{REPO}"
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=50"
TAG_PATTERN = re.compile(r"^v?[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")
VERSION_PATTERN = re.compile(
    r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)
MIN_ADMIN_VERSION = (0, 6, 0, (1,))
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_EXTRACTED_BYTES = 30 * 1024 * 1024

RESOURCE_PATHS = {
    "config_template": "config.template.json",
    "compose_example": "docker-compose.example.yml",
    "install_linux": "install-docker.sh",
    "install_windows": "install-docker.ps1",
    "deploy_docker_dir": "deploy/docker",
}
OPTIONAL_FILES = {"docs/install-docker.md", "docs/docker.md"}
CONFIG_TEMPLATE_NAME = "config.template.json"
# Release archives may ship the template at the canonical config/ path or, for
# older releases, at the archive root. Both are flattened to CONFIG_TEMPLATE_NAME
# in the cache so downstream cache checks stay stable.
ARCHIVE_TEMPLATE_PATHS = ("config/config.template.json", CONFIG_TEMPLATE_NAME)
REQUIRED_FILES = {
    CONFIG_TEMPLATE_NAME,
    "docker-compose.example.yml",
    "install-docker.sh",
    "install-docker.ps1",
}


class ReleaseError(Exception):
    """Expected release operation failure suitable for an API response."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def default_admin_data_dir():
    configured = os.environ.get("EMS_ADMIN_DATA_DIR")
    if configured:
        return Path(configured)
    project_root = Path(__file__).resolve().parent.parent
    return project_root / "data" / "admin"


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _version(tag):
    match = VERSION_PATTERN.fullmatch(str(tag))
    if not match:
        return None
    major, minor, patch = (int(part) for part in match.groups()[:3])
    prerelease = match.group(4)
    if prerelease is None:
        prerelease_key = (1,)
    else:
        prerelease_key = (0,) + tuple(
            (0, int(part)) if part.isdigit() else (1, part.lower())
            for part in prerelease.split(".")
        )
    return major, minor, patch, prerelease_key


def _version_text(tag):
    return str(tag)[1:] if str(tag).startswith("v") else str(tag)


def _is_admin_version(tag):
    return tag == "latest" or bool(
        _version(tag) and _version(tag) >= MIN_ADMIN_VERSION
    )


def _is_release_candidate(tag, github_prerelease=False):
    parsed = _version(tag)
    return bool(github_prerelease or (parsed and parsed[3][0] == 0))


def _downgrade_baseline(active, prepared):
    concrete = [tag for tag in (active, prepared) if _version(tag)]
    if concrete:
        return max(concrete, key=_version)
    return active or prepared


def _safe_tag(tag):
    value = str(tag or "").strip()
    if not TAG_PATTERN.fullmatch(value):
        raise ReleaseError("Invalid release tag.")
    return value


def _relative_archive_path(name):
    path = PurePosixPath(str(name).replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ReleaseError("Release archive contains an unsafe path.")
    parts = [part for part in path.parts if part not in ("", ".")]
    if len(parts) < 2:
        return None
    relative = PurePosixPath(*parts[1:])
    if relative.is_absolute() or ".." in relative.parts:
        raise ReleaseError("Release archive contains an unsafe path.")
    return relative


def _is_whitelisted(path):
    value = path.as_posix()
    return (
        value in REQUIRED_FILES
        or value in OPTIONAL_FILES
        or value in ARCHIVE_TEMPLATE_PATHS
        or value.startswith("deploy/docker/")
    )


def _cache_relative(relative):
    if relative.as_posix() in ARCHIVE_TEMPLATE_PATHS:
        return PurePosixPath(CONFIG_TEMPLATE_NAME)
    return relative


class ReleaseManager:
    def __init__(
        self,
        data_dir=None,
        project_dir=None,
        urlopen=None,
        resource_checker=None,
    ):
        self.data_dir = Path(data_dir) if data_dir else default_admin_data_dir()
        self.releases_dir = self.data_dir / "releases"
        self.state_dir = self.data_dir / "state"
        self.project_dir = (
            Path(project_dir) if project_dir else Path(__file__).resolve().parent.parent
        )
        self._urlopen = urlopen or urllib.request.urlopen
        self._resource_checker = resource_checker or self._check_remote_resources
        self._known_downloads = {}
        self._resource_checks = {}
        self._prepare_lock = threading.Lock()

    def list_releases(self):
        cached = self._cached_manifests()
        warnings = []
        remote = []
        remote_available = True
        try:
            remote = self._fetch_release_metadata()
        except (OSError, ValueError, urllib.error.URLError) as exc:
            remote_available = False
            warnings.append(f"GitHub releases are unavailable: {exc}")

        by_tag = {item["tag"]: item for item in remote}
        by_tag["latest"] = {
            "tag": "latest",
            "name": "latest",
            "published_at": None,
            "github_prerelease": False,
            "kind": "tag",
        }
        if remote_available:
            self._known_downloads["latest"] = (
                f"https://codeload.github.com/{REPO}/zip/refs/heads/main"
            )
        for tag, manifest in cached.items():
            item = by_tag.setdefault(
                tag,
                {
                    "tag": tag,
                    "name": tag,
                    "published_at": manifest.get("downloaded_at"),
                    "github_prerelease": False,
                    "kind": "tag" if tag == "latest" else "release",
                },
            )
            item["prepared"] = True

        active = self.detect_active_release()
        prepared = self._selected_release(cached)
        baseline = _downgrade_baseline(active, prepared)
        for item in by_tag.values():
            tag = item["tag"]
            item.setdefault("prepared", False)
            item["active"] = tag == active
            item["version"] = None if tag == "latest" else (
                _version_text(tag) if _version(tag) else None
            )
            item["kind"] = item.get("kind", "release")
            rc = _is_release_candidate(tag, item.pop("github_prerelease", False))
            eligible = _is_admin_version(tag)
            resources = (
                True
                if tag in cached
                else self._resource_status(
                    "main" if tag == "latest" else tag,
                    check_remote=remote_available,
                )
                if eligible
                else False
            )
            downgrade = self._is_downgrade(baseline, tag)
            item["channel"] = (
                "latest"
                if tag == "latest"
                else "legacy"
                if _version(tag) and not eligible
                else "rc"
                if rc
                else "stable"
                if _version(tag)
                else "unknown"
            )
            item["stable"] = item["channel"] in ("stable", "legacy") and not rc
            item["prerelease"] = rc
            item["admin_supported"] = eligible
            item["docker_supported"] = bool(eligible and resources is True)
            item["selectable"] = bool(
                item["admin_supported"] and item["docker_supported"] and not downgrade
            )
            if not eligible:
                item["reason"] = (
                    "Admin Setup supports Docker releases from v0.6.0 onward"
                    if _version(tag)
                    else "This tag is not supported by Admin Setup"
                )
            elif resources is False:
                item["reason"] = "Docker setup resources are missing for this release."
            elif resources is None:
                item["reason"] = "Docker setup resources could not be verified."
            elif downgrade:
                item["reason"] = (
                    "Downgrades are not supported by the setup assistant. "
                    "Use Backup/Restore flow instead."
                )
            elif tag == "latest":
                item["reason"] = (
                    "Rolling Docker channel, not a stable release"
                    + (
                        "; switching from a stable release requires extra caution"
                        if _version(baseline)
                        else ""
                    )
                )
            elif rc:
                item["reason"] = "Release candidate, not a stable release"
            else:
                item["reason"] = None

        if baseline and not _version(baseline) and baseline != "latest":
            warnings.append(
                "The current release cannot be compared safely; no downgrade claim "
                "was made."
            )
        releases = sorted(
            by_tag.values(),
            key=self._release_sort_key,
        )
        stable = next(
            (
                item["tag"]
                for item in releases
                if item["stable"] and item["selectable"] and item["channel"] == "stable"
            ),
            None,
        )
        default_release = stable or next(
            (item["tag"] for item in releases if item["tag"] == "latest" and item["selectable"]),
            None,
        )
        return {
            "active_release": active,
            "prepared_release": prepared,
            "latest": "latest",
            "latest_stable": stable,
            "default_release": default_release,
            "releases": releases,
            "warnings": warnings,
        }

    def prepare(self, tag):
        tag = _safe_tag(tag)
        try:
            with self._prepare_lock:
                self._ensure_data_directories()
                return self._prepare_locked(tag)
        except ReleaseError:
            raise
        except OSError as exc:
            raise self._data_directory_error() from exc

    def _prepare_locked(self, tag):
        cached = self._cached_manifests()
        active = self.detect_active_release()
        prepared = self._selected_release(cached)
        if self._is_downgrade(_downgrade_baseline(active, prepared), tag):
            raise ReleaseError(
                "Downgrades are not supported by the setup assistant. "
                "Use Backup/Restore flow instead.",
                status=409,
            )
        if not _is_admin_version(tag):
            raise ReleaseError(
                "Admin Setup supports Docker releases from v0.6.0 onward.", 400
            )

        manifest = cached.get(tag)
        if manifest and self._cache_complete(tag, manifest):
            manifest = self._normalized_manifest(tag, manifest)
            self._write_json(self.releases_dir / tag / "manifest.json", manifest)
            self._write_selected(tag)
            return self._ready_payload(tag, manifest, reused=True)

        if tag not in self._known_downloads:
            try:
                self._fetch_release_metadata()
            except (OSError, ValueError, urllib.error.URLError) as exc:
                raise ReleaseError(f"Could not validate release {tag}: {exc}", 503) from exc
        archive_url = self._known_downloads.get(tag)
        if not archive_url:
            raise ReleaseError(f"Unknown EMS release tag: {tag}", 404)
        ref = "main" if tag == "latest" else tag
        if self._resource_status(ref, check_remote=True) is not True:
            raise ReleaseError(
                "Docker setup resources are missing or could not be verified "
                "for this release.",
                422,
            )

        archive = self._download(archive_url)
        manifest = self._extract(tag, archive)
        self._write_selected(tag)
        return self._ready_payload(tag, manifest, reused=False)

    def config_template(self):
        tag = self._selected_release_tag()
        if not tag:
            raise ReleaseError("No release resources prepared yet.", 404)
        template_path = self.releases_dir / tag / "config.template.json"
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ReleaseError(
                f"Prepared release {tag} is missing config.template.json.", 500
            ) from exc
        except (OSError, ValueError) as exc:
            raise ReleaseError(
                f"Prepared release {tag} has an invalid config.template.json.", 500
            ) from exc
        if not isinstance(template, dict):
            raise ReleaseError(
                f"Prepared release {tag} has an invalid config.template.json.", 500
            )
        return {
            "tag": tag,
            "template": template,
            "source": str(template_path),
            "docker_image": self._docker_image(tag),
        }

    def detect_active_release(self):
        for name in ("docker-compose.yml", "compose.yml"):
            path = self.project_dir / name
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            match = re.search(
                r"ghcr\.io/basecubedev/ems-solarflow-api-control:([^\s\"'{}]+)",
                text,
                re.IGNORECASE,
            )
            if match and match.group(1).lower() != "latest":
                tag = match.group(1)
                return tag if TAG_PATTERN.fullmatch(tag) else None
        return None

    def _fetch_release_metadata(self):
        request = urllib.request.Request(
            GITHUB_RELEASES_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ems-solarflow-admin",
            },
        )
        with self._urlopen(request, timeout=10) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("GitHub response is too large")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, list):
            raise ValueError("GitHub returned an invalid release list")

        releases = []
        for entry in payload:
            if not isinstance(entry, dict) or entry.get("draft"):
                continue
            try:
                tag = _safe_tag(entry.get("tag_name"))
            except ReleaseError:
                continue
            prerelease = bool(entry.get("prerelease"))
            self._known_downloads[tag] = (
                f"https://codeload.github.com/{REPO}/zip/refs/tags/{tag}"
            )
            releases.append(
                {
                    "tag": tag,
                    "name": str(entry.get("name") or tag),
                    "published_at": entry.get("published_at"),
                    "github_prerelease": prerelease,
                    "kind": "release",
                }
            )
        self._known_downloads["latest"] = (
            f"https://codeload.github.com/{REPO}/zip/refs/heads/main"
        )
        return releases

    def _resource_status(self, ref, check_remote):
        if ref in self._resource_checks and self._resource_checks[ref] is not None:
            return self._resource_checks[ref]
        if not check_remote:
            return None
        try:
            status = bool(self._resource_checker(ref))
        except (OSError, ValueError, urllib.error.URLError):
            status = None
        self._resource_checks[ref] = status
        return status

    def _check_remote_resources(self, ref):
        url = (
            f"https://api.github.com/repos/{REPO}/git/trees/"
            f"{quote(ref, safe='')}?recursive=1"
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ems-solarflow-admin",
            },
        )
        with self._urlopen(request, timeout=10) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError("GitHub tree response is too large")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
            raise ValueError("GitHub returned an invalid resource tree")
        paths = {
            item.get("path")
            for item in payload["tree"]
            if isinstance(item, dict) and item.get("type") == "blob"
        }
        resource_files = REQUIRED_FILES - {CONFIG_TEMPLATE_NAME}
        has_template = any(path in paths for path in ARCHIVE_TEMPLATE_PATHS)
        return (
            resource_files.issubset(paths)
            and has_template
            and any(str(path).startswith("deploy/docker/") for path in paths)
        )

    @staticmethod
    def _release_sort_key(item):
        channel_order = {
            "stable": 0,
            "latest": 1,
            "rc": 2,
            "legacy": 3,
            "unknown": 4,
        }
        parsed = _version(item["tag"])
        core = parsed[:3] if parsed else (0, 0, 0)
        return (
            channel_order.get(item["channel"], 5),
            tuple(-part for part in core),
            item["tag"],
        )

    def _download(self, url):
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/octet-stream", "User-Agent": "ems-solarflow-admin"},
        )
        try:
            with self._urlopen(request, timeout=30) as response:
                chunks = []
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise ReleaseError(
                            "Release archive exceeds the download size limit.", 413
                        )
                    chunks.append(chunk)
        except ReleaseError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise ReleaseError(f"Could not download release archive: {exc}", 502) from exc
        return b"".join(chunks)

    def _extract(self, tag, archive):
        self.releases_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f".{tag}-", dir=self.releases_dir))
        try:
            extracted = self._extract_archive(archive, staging)
            missing = sorted(REQUIRED_FILES - extracted)
            if missing or not any(path.startswith("deploy/docker/") for path in extracted):
                detail = ", ".join(missing or ["deploy/docker/*"])
                raise ReleaseError(f"Release archive is missing required resources: {detail}")
            try:
                template = json.loads(
                    (staging / "config.template.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ReleaseError("Release config.template.json is invalid.") from exc
            if not isinstance(template, dict):
                raise ReleaseError("Release config.template.json is invalid.")

            manifest = self._normalized_manifest(tag, {
                "tag": tag,
                "source": "github",
                "downloaded_at": _utc_now(),
                "repo": REPO,
                "resources": dict(RESOURCE_PATHS),
            })
            self._write_json(staging / "manifest.json", manifest)
            target = self.releases_dir / tag
            if target.exists():
                shutil.rmtree(target)
            staging.replace(target)
            return manifest
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _extract_archive(self, archive, destination):
        if zipfile.is_zipfile(io.BytesIO(archive)):
            return self._extract_zip(archive, destination)
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as handle:
                return self._extract_tar(handle, destination)
        except tarfile.TarError as exc:
            raise ReleaseError("Downloaded release is not a supported archive.") from exc

    def _extract_zip(self, archive, destination):
        extracted = set()
        total = 0
        with zipfile.ZipFile(io.BytesIO(archive)) as handle:
            for info in handle.infolist():
                relative = _relative_archive_path(info.filename)
                if relative is None or info.is_dir() or not _is_whitelisted(relative):
                    continue
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ReleaseError("Release archive contains a symbolic link.")
                total = self._checked_total(total, info.file_size)
                cache_relative = _cache_relative(relative)
                with handle.open(info) as source:
                    self._write_resource(
                        destination, cache_relative, source.read(MAX_FILE_BYTES + 1)
                    )
                extracted.add(cache_relative.as_posix())
        return extracted

    def _extract_tar(self, handle, destination):
        extracted = set()
        total = 0
        for member in handle.getmembers():
            relative = _relative_archive_path(member.name)
            if relative is None or member.isdir() or not _is_whitelisted(relative):
                continue
            if not member.isfile():
                raise ReleaseError("Release archive contains an unsupported resource type.")
            total = self._checked_total(total, member.size)
            source = handle.extractfile(member)
            if source is None:
                raise ReleaseError("Release archive contains an unreadable resource.")
            cache_relative = _cache_relative(relative)
            self._write_resource(
                destination, cache_relative, source.read(MAX_FILE_BYTES + 1)
            )
            extracted.add(cache_relative.as_posix())
        return extracted

    @staticmethod
    def _checked_total(total, size):
        if size < 0 or size > MAX_FILE_BYTES:
            raise ReleaseError("A release resource exceeds the file size limit.", 413)
        total += size
        if total > MAX_EXTRACTED_BYTES:
            raise ReleaseError("Release resources exceed the extraction size limit.", 413)
        return total

    @staticmethod
    def _write_resource(destination, relative, content):
        if len(content) > MAX_FILE_BYTES:
            raise ReleaseError("A release resource exceeds the file size limit.", 413)
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def _cached_manifests(self):
        manifests = {}
        try:
            directories = list(self.releases_dir.iterdir())
        except OSError:
            return manifests
        for directory in directories:
            if not directory.is_dir() or not TAG_PATTERN.fullmatch(directory.name):
                continue
            try:
                manifest = json.loads(
                    (directory / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if (
                isinstance(manifest, dict)
                and manifest.get("tag") == directory.name
                and self._cache_complete(directory.name, manifest)
            ):
                manifests[directory.name] = manifest
        return manifests

    def _cache_complete(self, tag, manifest):
        root = self.releases_dir / tag
        if manifest.get("repo") != REPO:
            return False
        if not all((root / path).is_file() for path in REQUIRED_FILES) or not any(
            path.is_file() for path in (root / "deploy" / "docker").glob("**/*")
        ):
            return False
        try:
            template = json.loads(
                (root / "config.template.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        return isinstance(template, dict)

    def _selected_release_tag(self):
        try:
            selected = json.loads(
                (self.state_dir / "selected-release.json").read_text(encoding="utf-8")
            )
            tag = selected.get("tag")
        except (OSError, ValueError, AttributeError):
            return None
        return tag if isinstance(tag, str) and TAG_PATTERN.fullmatch(tag) else None

    def _selected_release(self, cached):
        selected = self._selected_release_tag()
        if selected in cached:
            return selected
        if not cached:
            return None
        return max(
            cached,
            key=lambda tag: cached[tag].get("downloaded_at") or "",
        )

    def _write_selected(self, tag):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(
            self.state_dir / "selected-release.json",
            {"tag": tag, "selected_at": _utc_now()},
        )

    def _ensure_data_directories(self):
        self.releases_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(dir=self.data_dir, prefix=".write-check-"):
                pass
        except OSError as exc:
            raise self._data_directory_error() from exc

    def _data_directory_error(self):
        return ReleaseError(
            f"Admin data directory is not writable: {self.data_dir}. "
            "Check the Docker volume mount for ./data/admin:/data.",
            500,
        )

    @staticmethod
    def _write_json(path, payload):
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    def _normalized_manifest(self, tag, manifest):
        normalized = dict(manifest)
        normalized.update(
            {
                "tag": tag,
                "release": tag,
                "repo": REPO,
                "config_template": RESOURCE_PATHS["config_template"],
                "docker_compose_template": RESOURCE_PATHS["compose_example"],
                "docker_image": self._docker_image(tag),
            }
        )
        normalized["prepared_at"] = (
            normalized.get("prepared_at")
            or normalized.get("downloaded_at")
            or _utc_now()
        )
        normalized["resources"] = {
            **normalized.get("resources", {}),
            **dict(RESOURCE_PATHS),
        }
        return normalized

    @staticmethod
    def _is_downgrade(active, selected):
        active_version = _version(active)
        selected_version = _version(selected)
        return bool(active_version and selected_version and selected_version < active_version)

    def _ready_payload(self, tag, manifest, reused):
        root = self.releases_dir / tag
        config_template_loaded = self._valid_cached_template(root)
        return {
            "status": "ready",
            "tag": tag,
            "manifest": manifest,
            "config_template_loaded": config_template_loaded,
            "docker_image": self._docker_image(tag),
            "resources": {
                "config_template_available": config_template_loaded,
                "config_template_loaded": config_template_loaded,
                "docker_install_available": (
                    (root / "install-docker.sh").is_file()
                    and (root / "install-docker.ps1").is_file()
                ),
                "compose_example_available": (
                    root / "docker-compose.example.yml"
                ).is_file(),
                "deploy_docker_available": any(
                    path.is_file() for path in (root / "deploy" / "docker").glob("**/*")
                ),
            },
            "reused": reused,
            "warnings": [],
        }

    @staticmethod
    def _valid_cached_template(root):
        try:
            template = json.loads(
                (root / "config.template.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        return isinstance(template, dict)

    @staticmethod
    def _docker_image(tag):
        return f"{DOCKER_IMAGE_REPOSITORY}:{tag}"
