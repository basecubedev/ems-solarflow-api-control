# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bring signed OS releases onto the appliance over HTTPS.

Without this there is no supported way for an OS update to reach a device at
all: the release directory is local, and nothing ever wrote to it. This module
is the transport and nothing else. It does not decide what is trustworthy —
``OsReleaseCatalogue`` still owns that, with the same detached-signature chain
it always used.

The order is the security property, and it is the whole point:

1. the index is fetched and may name candidates — it is never trusted,
2. the manifest and its detached signature are fetched into a staging
   directory,
3. the signature is verified against the root-owned keyring,
4. only then is the manifest's declared archive name, size and digest read,
5. the archive is fetched under exactly that declared size and hashed,
6. the digest is compared against the verified manifest,
7. only then is anything moved into the release directory.

An index that lies can therefore waste bandwidth and nothing else. Reading the
archive digest before the signature was checked would make the index the
authority, which is precisely the mistake this ordering exists to prevent.

The appliance has no real-time clock. TLS certificate validity and signature
validity are both judged against the system clock, so a fetch before the clock
is synchronised fails for reasons that look like anything but a wrong clock.
That is checked first, and a clock this cannot confirm counts as unsynchronised.
"""

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from appliance import os_releases
from appliance.version import version_key

TYPE_OS_FETCH = "ab.fetch"

INDEX_FORMAT_VERSION = 1

MAX_INDEX_BYTES = 1024 * 1024
MAX_SIGNATURE_BYTES = 64 * 1024
# Nothing this project publishes comes close; the cap exists so a hostile
# response cannot fill the release partition before the digest is checked.
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024

CONNECT_TIMEOUT = 30
CHUNK = 1024 * 1024

# Free space beyond the declared archive size. The staged copy and the placed
# copy never coexist -- the move is a rename -- but a release directory filled
# to the last byte is not one an update can work in.
FREE_SPACE_MARGIN = 256 * 1024 * 1024

STAGING_PREFIX = ".fetch-"


class FetchError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _https_url(value, *, label):
    """An absolute https URL, or a refusal naming which one was not.

    http is refused rather than upgraded: an operator who configured a plain
    URL should be told, not silently given a different one than they wrote.
    """

    text = str(value or "").strip()
    if not text:
        raise FetchError("release_source_unconfigured", f"no {label} is configured")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "https":
        raise FetchError(
            "release_source_insecure",
            f"the {label} is {parsed.scheme or 'relative'}, and only https is accepted",
        )
    if not parsed.netloc:
        raise FetchError("release_source_invalid", f"the {label} names no host")
    return text


class HttpsFetcher:
    """Read a URL, bounded, over https only — including after a redirect."""

    def __init__(self, *, opener=None, timeout=CONNECT_TIMEOUT):
        self._open = opener or urllib.request.urlopen
        self.timeout = timeout

    def _response(self, url, label):
        request = urllib.request.Request(_https_url(url, label=label))
        try:
            return self._open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise FetchError(
                "release_download_failed", f"{label} answered HTTP {exc.code}"
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise FetchError(
                "release_download_failed",
                f"{label} is unreachable: {exc.__class__.__name__}",
            )

    @staticmethod
    def _still_https(response, label):
        # urllib follows redirects on its own, so the scheme has to be checked
        # where the bytes actually came from and not only where they were asked
        # for. A redirect to http is a downgrade, not a detail.
        final = getattr(response, "url", "") or ""
        if final and urllib.parse.urlsplit(final).scheme != "https":
            raise FetchError(
                "release_source_insecure",
                f"{label} redirected to a non-https location",
            )

    def read(self, url, *, label, max_bytes):
        with self._response(url, label) as response:
            self._still_https(response, label)
            # One byte over the cap is read on purpose: it is what tells an
            # oversized body apart from one that exactly fits.
            payload = response.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise FetchError(
                "release_download_too_large",
                f"{label} is larger than the {max_bytes} byte limit this appliance accepts",
            )
        return payload

    def download(self, url, destination, *, label, expected_bytes):
        """Stream to ``destination``, hashing, refusing anything oversized."""

        digest = hashlib.sha256()
        written = 0
        with self._response(url, label) as response:
            self._still_https(response, label)
            with open(destination, "wb") as handle:
                while True:
                    block = response.read(CHUNK)
                    if not block:
                        break
                    written += len(block)
                    if written > expected_bytes:
                        raise FetchError(
                            "release_download_too_large",
                            f"{label} sends more than the {expected_bytes} bytes its "
                            "verified manifest declares",
                        )
                    digest.update(block)
                    handle.write(block)
                handle.flush()
                os.fsync(handle.fileno())
        if written != expected_bytes:
            raise FetchError(
                "release_download_truncated",
                f"{label} ended after {written} of {expected_bytes} declared bytes",
            )
        return f"sha256:{digest.hexdigest()}"


# What an index entry may say about a release beyond where to fetch it. None of
# it is trusted and none of it gates anything: it exists so an operator choosing
# between several published releases has something to choose by, rather than a
# column of opaque identifiers. The signed manifest remains the only authority,
# and every one of these is replaced by the manifest's own value once verified.
DESCRIPTION_FIELDS = ("release_version", "created_at", "build_id", "board", "variant")

MAX_DESCRIPTION = 64


def _description(entry):
    """The entry's own account of itself, bounded and stringified."""

    described = {}
    for field in DESCRIPTION_FIELDS:
        value = entry.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                described[field] = text[:MAX_DESCRIPTION]
    return described


def sort_key(entry):
    """Newest first, by what the entry claims, with the id as the tiebreak.

    An index that says nothing about its releases still sorts deterministically;
    it just sorts by identifier, which is the state this had before.
    """

    return (
        version_key(entry.get("release_version") or ""),
        str(entry.get("created_at") or ""),
        str(entry.get("build_id") or ""),
        str(entry.get("release_id") or ""),
    )


def parse_index(payload):
    """Candidate releases an index names. Nothing here is trusted.

    Every entry is checked for shape only — a release id this appliance would
    accept and three https URLs. What the entry claims about the release is
    irrelevant to whether it may be installed, because the signed manifest is
    what decides; the claims are kept only so the choice can be labelled.
    """

    if not isinstance(payload, dict):
        raise FetchError("release_index_invalid", "the release index is not an object")
    version = payload.get("format_version")
    if version != INDEX_FORMAT_VERSION:
        raise FetchError(
            "release_index_unsupported",
            f"the release index is format {version or 'unknown'}, this appliance reads "
            f"format {INDEX_FORMAT_VERSION}",
        )
    entries = payload.get("releases")
    if not isinstance(entries, list):
        raise FetchError("release_index_invalid", "the release index lists no releases")

    candidates = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            release_id = os_releases.validate_release_id(entry.get("release_id"))
            manifest_url = _https_url(entry.get("manifest_url"), label="the manifest url")
            signature_url = _https_url(entry.get("signature_url"), label="the signature url")
            archive_url = _https_url(entry.get("archive_url"), label="the archive url")
        except (FetchError, os_releases.ReleaseError):
            # A malformed entry is skipped, never repaired by guessing: the
            # index is a suggestion and one bad row does not poison the rest.
            continue
        if release_id in seen:
            continue
        seen.add(release_id)
        candidates.append(
            {
                "release_id": release_id,
                "manifest_url": manifest_url,
                "signature_url": signature_url,
                "archive_url": archive_url,
                "described": _description(entry),
            }
        )
    candidates.sort(key=lambda item: sort_key({**item, **item["described"]}), reverse=True)
    return candidates


class OsFetchService:
    """Fetch a signed OS release into the directory the catalogue reads."""

    def __init__(
        self,
        *,
        paths,
        config,
        catalogue,
        probe,
        operations,
        fetcher=None,
        time_fn=None,
        operation_log=None,
    ):
        self.paths = paths
        self.config = config
        self.catalogue = catalogue
        self.probe = probe
        self.operations = operations
        self.fetcher = fetcher or HttpsFetcher()
        self._time = time_fn
        self._operation_log = operation_log

    # --- preconditions ----------------------------------------------------

    @property
    def release_dir(self):
        return Path(self.config.os_release_dir or "")

    def _require_clock(self):
        """A clock this cannot confirm is a clock that has not been set.

        The board has no RTC, so after a power cut it starts somewhere in the
        past until timesyncd catches up. Both the TLS certificate and the
        release signature are judged against that time, and both then fail with
        errors that say nothing about a clock.
        """

        record = self.probe.system_time()
        if record.get("ntp_synchronized") is True:
            return record
        detail = (
            "the system clock has not been synchronised yet"
            if record.get("ntp_synchronized") is False
            else "this appliance cannot confirm that the system clock is synchronised"
        )
        raise FetchError(
            "clock_not_synchronised",
            f"{detail}; this board has no real-time clock, so a download now would fail "
            "certificate and signature checks for reasons that do not name the clock. "
            "Wait for time synchronisation and try again",
        )

    def _require_release_dir(self):
        directory = self.release_dir
        if not directory:
            raise FetchError(
                "release_directory_unconfigured",
                "no os_release_dir is configured, so a fetched release would have nowhere to go",
            )
        if not directory.is_dir():
            raise FetchError(
                "release_directory_missing", f"{directory} does not exist"
            )
        return directory

    def _require_space(self, directory, needed):
        usage = shutil.disk_usage(directory)
        required = needed + FREE_SPACE_MARGIN
        if usage.free < required:
            raise FetchError(
                "release_directory_full",
                f"{directory} has {usage.free} bytes free and this release needs {required}",
            )
        return usage.free

    # --- discovery --------------------------------------------------------

    def index(self):
        """What the configured index offers, and what is already here."""

        local = {release.release_id for release in self._local_releases()}
        try:
            url = _https_url(self.config.os_release_index_url, label="the release index url")
        except FetchError as exc:
            return {"configured": False, "error": exc.code, "releases": [], "local": sorted(local)}
        try:
            raw = self.fetcher.read(url, label="the release index", max_bytes=MAX_INDEX_BYTES)
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            candidates = parse_index(payload)
        except FetchError as exc:
            return {"configured": True, "error": exc.code, "releases": [], "local": sorted(local)}
        except ValueError:
            return {
                "configured": True,
                "error": "release_index_invalid",
                "releases": [],
                "local": sorted(local),
            }
        for candidate in candidates:
            candidate["present"] = candidate["release_id"] in local
        return {"configured": True, "error": "", "releases": candidates, "local": sorted(local)}

    def _local_releases(self):
        try:
            return self.catalogue.available()
        except os_releases.ReleaseError:
            return []

    def _candidate(self, release_id):
        wanted = os_releases.validate_release_id(release_id)
        listing = self.index()
        if not listing["configured"]:
            raise FetchError(
                "release_source_unconfigured",
                "no release index is configured, so this appliance cannot fetch an OS release",
            )
        if listing["error"]:
            raise FetchError(listing["error"], "the release index could not be read")
        for candidate in listing["releases"]:
            if candidate["release_id"] == wanted:
                return candidate
        raise FetchError("unknown_release", f"{wanted} is not offered by the release index")

    # --- planning ---------------------------------------------------------

    def plan_fetch(self, operation, release_id):
        """Say what would be downloaded. Nothing is written here."""

        self._advance(operation, "preflight")
        self._require_clock()
        directory = self._require_release_dir()
        candidate = self._candidate(release_id)

        if candidate["release_id"] in {item.release_id for item in self._local_releases()}:
            raise FetchError(
                "release_already_present",
                f"{candidate['release_id']} is already on this appliance; delete it first if "
                "it must be fetched again",
            )
        # The record, not the plan, is what execution reads. Sealing them
        # together is what the agent does next; writing the target here is what
        # gives it something to seal.
        self.operations.update_target(
            operation.operation_id,
            {
                "release_id": candidate["release_id"],
                "manifest_url": candidate["manifest_url"],
                "signature_url": candidate["signature_url"],
                "archive_url": candidate["archive_url"],
            },
        )
        return {
            "type": TYPE_OS_FETCH,
            "release_id": candidate["release_id"],
            "manifest_url": candidate["manifest_url"],
            "signature_url": candidate["signature_url"],
            "archive_url": candidate["archive_url"],
            "release_dir": str(directory),
            "free_bytes": shutil.disk_usage(directory).free,
            # The size is not known before the signed manifest is read, and the
            # plan says so rather than repeating a number the index supplied.
            "size_known": False,
            "warning": "The download size is taken from the release manifest after its "
            "signature has been verified, so it is not known yet.",
        }

    # --- execution --------------------------------------------------------

    def execute(self, operation):
        release_id = str((operation.requested_target or {}).get("release_id") or "")
        try:
            wanted = os_releases.validate_release_id(release_id)
        except os_releases.ReleaseError as exc:
            raise FetchError("unknown_release", exc.message)

        self._advance(operation, "preflight")
        self._require_clock()
        directory = self._require_release_dir()
        candidate = self._candidate(wanted)
        if wanted in {item.release_id for item in self._local_releases()}:
            raise FetchError(
                "release_already_present", f"{wanted} is already on this appliance"
            )

        staging = directory / f"{STAGING_PREFIX}{operation.operation_id}"
        try:
            staging.mkdir(mode=0o700, parents=False, exist_ok=False)
        except OSError as exc:
            raise FetchError(
                "release_staging_unavailable", f"{staging} could not be created: {exc}"
            )
        try:
            return self._fetch_into(operation, candidate, staging, directory)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _fetch_into(self, operation, candidate, staging, directory):
        release_id = candidate["release_id"]
        manifest_name = f"{release_id}.manifest.json"
        signature_name = f"{manifest_name}.asc"

        self._advance(operation, "fetching_manifest", detail=candidate["manifest_url"])
        manifest_bytes = self.fetcher.read(
            candidate["manifest_url"],
            label="the release manifest",
            max_bytes=os_releases.MAX_MANIFEST_BYTES,
        )
        signature_bytes = self.fetcher.read(
            candidate["signature_url"],
            label="the manifest signature",
            max_bytes=MAX_SIGNATURE_BYTES,
        )
        manifest_path = staging / manifest_name
        signature_path = staging / signature_name
        manifest_path.write_bytes(manifest_bytes)
        signature_path.write_bytes(signature_bytes)

        # Everything below this line reads the manifest as an authority, so
        # nothing above it may.
        self._advance(operation, "verifying_signature")
        self.catalogue.verifier.verify(manifest_path, signature_path)

        try:
            release = os_releases.parse_manifest(
                json.loads(manifest_bytes.decode("utf-8")),
                release_id=release_id,
                verified=os_releases.VERIFIED_SIGNATURE,
            )
        except ValueError:
            raise FetchError(
                "release_manifest_invalid", f"{manifest_name} is not valid JSON"
            )

        archive_name = Path(release.archive_name or f"{release_id}.tar.zst").name
        declared = int(release.archive_size or 0)
        if declared <= 0 or declared > MAX_ARCHIVE_BYTES:
            raise FetchError(
                "release_manifest_invalid",
                f"the verified manifest declares an archive size of {declared} bytes",
            )
        self._require_space(directory, declared)

        self._advance(operation, "fetching_archive", detail=archive_name)
        archive_path = staging / archive_name
        observed = self.fetcher.download(
            candidate["archive_url"],
            archive_path,
            label="the release archive",
            expected_bytes=declared,
        )
        if observed != release.archive_digest:
            raise FetchError(
                "artifact_digest_mismatch",
                f"the downloaded archive hashes to {observed}, the verified manifest "
                f"declares {release.archive_digest}",
            )

        self._advance(operation, "placing_release")
        placed = self._place(staging, directory, (archive_name, signature_name, manifest_name))
        return {
            "release_id": release_id,
            "archive": archive_name,
            "bytes": declared,
            "digest": observed,
            "verified": os_releases.VERIFIED_SIGNATURE,
            "files": placed,
        }

    @staticmethod
    def _place(staging, directory, names):
        """Move the verified files in, manifest last.

        The catalogue finds a release by its manifest, so the manifest is what
        makes it visible. Moving it last means a fetch interrupted by a power
        cut leaves files the catalogue does not offer, rather than a release
        whose archive is not there yet.
        """

        placed = []
        for name in names:
            source = staging / name
            if not source.is_file():
                continue
            target = directory / name
            os.replace(source, target)
            placed.append(str(target))
        handle = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)
        return placed

    def _advance(self, operation, stage, *, detail=None):
        self.operations.advance(operation.operation_id, stage, detail=detail)
        if self._operation_log is not None:
            self._operation_log.record(
                operation.operation_id, stage, operation_type=operation.type, detail=detail
            )
        return stage


__all__ = [
    "FetchError",
    "HttpsFetcher",
    "OsFetchService",
    "TYPE_OS_FETCH",
    "parse_index",
]
