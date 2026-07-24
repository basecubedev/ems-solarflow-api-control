# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load and validate the public development System Build catalogue."""

import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from admin.admin_update import ADMIN_IMAGE_REPO, EMS_IMAGE_REPO
from admin.releases import TAG_PATTERN
from admin.system_build import classify_channel, is_immutable_dev_tag

DEFAULT_DEVELOPMENT_CATALOGUE_URL = (
    "https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/"
    "development-build-catalogue/development-builds.json"
)
DEVELOPMENT_CATALOGUE_ENV = "EMS_ADMIN_DEVELOPMENT_CATALOGUE"
CATALOGUE_CACHE_NAME = "development-builds-cache.json"
CATALOGUE_TIMEOUT_SECONDS = 3
CATALOGUE_MAX_BYTES = 512 * 1024
CATALOGUE_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60

_FULL_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TAG_SUFFIX_RE = re.compile(
    r"^(?P<prefix>dev-.+)-(?P<sha>[0-9a-f]{7,40})-"
    r"(?P<run>[1-9][0-9]*)-(?P<attempt>[1-9][0-9]*)$"
)


class _InvalidCatalogue(ValueError):
    pass


def default_catalogue_source():
    return os.environ.get(DEVELOPMENT_CATALOGUE_ENV) or DEFAULT_DEVELOPMENT_CATALOGUE_URL


def default_catalogue_cache_path() -> Path:
    configured = os.environ.get("EMS_ADMIN_DATA_DIR")
    data_dir = Path(configured) if configured else Path(__file__).resolve().parents[1] / "data" / "admin"
    return data_dir / CATALOGUE_CACHE_NAME


def load_development_builds(
    source=None,
    *,
    cache_path=None,
    urlopen=None,
    now=None,
) -> list:
    """Load a local or remote catalogue and return only complete build pairs.

    Transport failures may use a recent valid cache. Malformed, oversized, or
    schema-invalid responses fail closed and do not fall back to older content.
    """

    source = default_catalogue_source() if source is None else source
    if _is_remote(source):
        cache = Path(cache_path) if cache_path is not None else default_catalogue_cache_path()
        try:
            payload = _fetch_remote(source, urlopen=urlopen)
        except (OSError, TimeoutError, urllib.error.URLError):
            return _load_fresh_cache(cache, now=now)
        except _InvalidCatalogue:
            return []
        try:
            builds = _validated_builds(payload)
        except _InvalidCatalogue:
            return []
        _write_cache(cache, {"builds": builds})
        return builds

    try:
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        return _validated_builds(payload)
    except (OSError, ValueError, _InvalidCatalogue):
        return []


def development_catalogue_source(
    source=None,
    *,
    cache_path=None,
    urlopen=None,
    now=None,
):
    """Return the zero-argument source consumed by ``ReleaseManager``."""

    return lambda: load_development_builds(
        source,
        cache_path=cache_path,
        urlopen=urlopen,
        now=now,
    )


def _is_remote(source) -> bool:
    if isinstance(source, Path):
        return False
    return urlparse(str(source)).scheme in {"http", "https"}


def _fetch_remote(source, *, urlopen=None):
    opener = urlopen or urllib.request.urlopen
    request = urllib.request.Request(
        str(source),
        headers={
            "Accept": "application/json",
            "User-Agent": "ems-solarflow-admin",
        },
    )
    with opener(request, timeout=CATALOGUE_TIMEOUT_SECONDS) as response:
        raw = response.read(CATALOGUE_MAX_BYTES + 1)
    if len(raw) > CATALOGUE_MAX_BYTES:
        raise _InvalidCatalogue("development catalogue exceeds the size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _InvalidCatalogue("development catalogue is not valid JSON") from exc


def _validated_builds(payload) -> list:
    if not isinstance(payload, dict) or not isinstance(payload.get("builds"), list):
        raise _InvalidCatalogue("development catalogue must contain a builds array")

    result = []
    seen = set()
    for entry in payload["builds"]:
        normalized = _normalize_entry(entry)
        if normalized is None:
            continue
        tag = normalized["tag"]
        if tag in seen:
            raise _InvalidCatalogue("development catalogue contains duplicate tags")
        seen.add(tag)
        result.append(normalized)
    return result


def _normalize_entry(entry):
    if not isinstance(entry, dict):
        return None
    tag = str(entry.get("tag") or "").strip()
    suffix = _TAG_SUFFIX_RE.fullmatch(tag)
    if (
        not tag
        or not TAG_PATTERN.fullmatch(tag)
        or classify_channel(tag) != "development"
        or not is_immutable_dev_tag(tag)
        or suffix is None
    ):
        return None
    if entry.get("channel") != "development" or entry.get("installable") is not True:
        return None

    revision = str(entry.get("revision") or "").strip()
    if not _FULL_REVISION_RE.fullmatch(revision) or not revision.startswith(suffix["sha"]):
        return None
    if entry.get("build_id") != tag:
        return None
    if str(entry.get("run_id") or "") != suffix["run"]:
        return None
    run_attempt = entry.get("run_attempt")
    if isinstance(run_attempt, bool) or not isinstance(run_attempt, int):
        return None
    if str(run_attempt) != suffix["attempt"]:
        return None

    if entry.get("admin_image") != f"{ADMIN_IMAGE_REPO}:{tag}":
        return None
    if entry.get("ems_image") != f"{EMS_IMAGE_REPO}:{tag}":
        return None
    if not _DIGEST_RE.fullmatch(str(entry.get("admin_digest") or "")):
        return None
    if not _DIGEST_RE.fullmatch(str(entry.get("ems_digest") or "")):
        return None

    created_at = entry.get("created_at")
    if not _valid_timestamp(created_at):
        return None
    return {
        "tag": tag,
        "display_name": str(entry.get("display_name") or "").strip() or tag,
        "channel": "development",
        "revision": revision,
        "build_id": tag,
        "run_id": suffix["run"],
        "run_attempt": run_attempt,
        "created_at": created_at,
        "admin_image": entry["admin_image"],
        "admin_digest": entry["admin_digest"],
        "ems_image": entry["ems_image"],
        "ems_digest": entry["ems_digest"],
        "installable": True,
    }


def _valid_timestamp(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _now_timestamp(now=None) -> float:
    value = now() if callable(now) else datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return float(value)


def _load_fresh_cache(path: Path, *, now=None) -> list:
    try:
        age = _now_timestamp(now) - path.stat().st_mtime
        if age < 0 or age > CATALOGUE_CACHE_MAX_AGE_SECONDS:
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _validated_builds(payload)
    except (OSError, ValueError, _InvalidCatalogue):
        return []


def _write_cache(path: Path, payload) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass
