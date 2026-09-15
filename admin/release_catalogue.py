# SPDX-License-Identifier: AGPL-3.0-or-later
"""Load and validate the public release catalogue.

The catalogue is one JSON file that CI regenerates on every release publish,
published on the same branch as the development-build catalogue and read over
the raw content CDN rather than the GitHub API. It carries what the Admin used
to ask the API for on every visit to the upgrade page -- the release list and,
per release, whether its setup resources are present -- and answers the two
questions that used to cost a dozen unauthenticated API calls against a limit
of sixty an hour with a single request that is not counted at all.

``load_release_catalogue`` returns ``None`` when the catalogue cannot be used,
so the caller falls back to the API rather than showing an empty list; a
catalogue that is present but names no release is the empty list, and that
answer is trusted.
"""

import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_RELEASE_CATALOGUE_URL = (
    "https://raw.githubusercontent.com/basecubedev/ems-solarflow-api-control/"
    "development-build-catalogue/release-catalogue.json"
)
RELEASE_CATALOGUE_ENV = "EMS_ADMIN_RELEASE_CATALOGUE"
CATALOGUE_CACHE_NAME = "release-catalogue-cache.json"
CATALOGUE_TIMEOUT_SECONDS = 3
CATALOGUE_MAX_BYTES = 512 * 1024
CATALOGUE_CACHE_MAX_AGE_SECONDS = 24 * 60 * 60
CATALOGUE_SCHEMA = 1
CHANNELS = ("stable", "rc")


class _InvalidCatalogue(ValueError):
    pass


def default_catalogue_source():
    return os.environ.get(RELEASE_CATALOGUE_ENV) or DEFAULT_RELEASE_CATALOGUE_URL


def default_catalogue_cache_path() -> Path:
    configured = os.environ.get("EMS_ADMIN_DATA_DIR")
    data_dir = Path(configured) if configured else Path(__file__).resolve().parents[1] / "data" / "admin"
    return data_dir / CATALOGUE_CACHE_NAME


def load_release_catalogue(source=None, *, cache_path=None, urlopen=None, now=None):
    """Return the validated catalogue, or ``None`` when unusable.

    The result is a dict: ``releases`` (validated entries, newest first as
    published) and ``main_resources_present`` (whether the rolling ``latest``
    channel's setup resources are present on ``main``), so the listing has an
    answer for every row without a single API call.

    A transport failure may use a recent valid cache. A malformed, oversized or
    schema-invalid document is ``None`` and never falls back to older content,
    and never becomes an empty catalogue: the caller must not mistake "could
    not be read" for "there are no releases".
    """

    source = default_catalogue_source() if source is None else source
    if _is_remote(source):
        cache = Path(cache_path) if cache_path is not None else default_catalogue_cache_path()
        try:
            payload = _fetch_remote(source, urlopen=urlopen)
        except (OSError, TimeoutError, urllib.error.URLError):
            return _load_fresh_cache(cache, now=now)
        except _InvalidCatalogue:
            return None
        try:
            catalogue = _validated_catalogue(payload)
        except _InvalidCatalogue:
            return None
        _write_cache(cache, {"schema": CATALOGUE_SCHEMA, **catalogue})
        return catalogue

    try:
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
        return _validated_catalogue(payload)
    except (OSError, ValueError, _InvalidCatalogue):
        return None


def release_catalogue_source(source=None, *, cache_path=None, urlopen=None, now=None):
    """Return the zero-argument source consumed by ``ReleaseManager``."""

    return lambda: load_release_catalogue(
        source, cache_path=cache_path, urlopen=urlopen, now=now
    )


def _is_remote(source) -> bool:
    if isinstance(source, Path):
        return False
    return urlparse(str(source)).scheme in {"http", "https"}


def _fetch_remote(source, *, urlopen=None):
    opener = urlopen or urllib.request.urlopen
    request = urllib.request.Request(
        str(source),
        headers={"Accept": "application/json", "User-Agent": "ems-solarflow-admin"},
    )
    with opener(request, timeout=CATALOGUE_TIMEOUT_SECONDS) as response:
        raw = response.read(CATALOGUE_MAX_BYTES + 1)
    if len(raw) > CATALOGUE_MAX_BYTES:
        raise _InvalidCatalogue("release catalogue exceeds the size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _InvalidCatalogue("release catalogue is not valid JSON") from exc


def _validated_catalogue(payload) -> dict:
    if not isinstance(payload, dict):
        raise _InvalidCatalogue("release catalogue must be an object")
    if not isinstance(payload.get("main_resources_present"), bool):
        raise _InvalidCatalogue("release catalogue main_resources_present must be a boolean")
    return {
        "releases": _validated_releases(payload),
        "main_resources_present": payload["main_resources_present"],
    }


def _validated_releases(payload) -> list:
    from admin.releases import TAG_PATTERN, _version

    if not isinstance(payload, dict) or not isinstance(payload.get("releases"), list):
        raise _InvalidCatalogue("release catalogue must contain a releases array")
    if payload.get("schema") != CATALOGUE_SCHEMA:
        raise _InvalidCatalogue("release catalogue schema is not supported")

    result = []
    seen = set()
    for entry in payload["releases"]:
        if not isinstance(entry, dict):
            raise _InvalidCatalogue("release catalogue entries must be objects")
        tag = str(entry.get("tag") or "").strip()
        if not tag or not TAG_PATTERN.fullmatch(tag) or not _version(tag):
            raise _InvalidCatalogue("release catalogue names a tag that is not a version")
        if tag in seen:
            raise _InvalidCatalogue("release catalogue contains duplicate tags")
        seen.add(tag)
        if entry.get("channel") not in CHANNELS:
            raise _InvalidCatalogue("release catalogue names an unknown channel")
        if not isinstance(entry.get("prerelease"), bool):
            raise _InvalidCatalogue("release catalogue prerelease must be a boolean")
        if not isinstance(entry.get("resources_present"), bool):
            raise _InvalidCatalogue("release catalogue resources_present must be a boolean")
        published_at = entry.get("published_at")
        if published_at is not None and not _valid_timestamp(published_at):
            raise _InvalidCatalogue("release catalogue published_at is not a timestamp")
        result.append(
            {
                "tag": tag,
                "name": str(entry.get("name") or tag),
                "channel": entry["channel"],
                "prerelease": entry["prerelease"],
                "published_at": published_at,
                "resources_present": entry["resources_present"],
            }
        )
    return result


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


def _load_fresh_cache(path: Path, *, now=None):
    try:
        age = _now_timestamp(now) - path.stat().st_mtime
        if age < 0 or age > CATALOGUE_CACHE_MAX_AGE_SECONDS:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _validated_catalogue(payload)
    except (OSError, ValueError, _InvalidCatalogue):
        return None


def _write_cache(path: Path, payload) -> None:
    temporary = None
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
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
