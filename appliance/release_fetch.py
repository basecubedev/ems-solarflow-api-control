# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bring a signed release onto the appliance over HTTPS.

This module is the transport and nothing else. It does not decide what is
trustworthy -- ``artifact_trust`` owns that, with a detached-signature chain
the transport never gets to shortcut.

The order is the security property, and it is the whole point:

1. the index is fetched and may name candidates -- it is never trusted,
2. the manifest and its detached signature are fetched into a staging
   directory,
3. the signature is verified against the root-owned keyring,
4. only then is the manifest's declared file name, size and digest read,
5. the file is fetched under exactly that declared size and hashed,
6. the digest is compared against the verified manifest,
7. only then is anything installed.

An index that lies can therefore waste bandwidth and nothing else. Reading the
digest before the signature was checked would make the index the authority,
which is precisely the mistake this ordering exists to prevent.

The appliance has no real-time clock. TLS certificate validity and signature
validity are both judged against the system clock, so a fetch before the clock
is synchronised fails for reasons that look like anything but a wrong clock.
That is checked first, and a clock this cannot confirm counts as unsynchronised.
"""

import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request

from appliance import artifact_trust
from appliance.version import version_key


INDEX_FORMAT_VERSION = 1

MAX_INDEX_BYTES = 1024 * 1024
MAX_SIGNATURE_BYTES = 64 * 1024

CONNECT_TIMEOUT = 30
CHUNK = 1024 * 1024


class FetchError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def https_url(value, *, label):
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
        request = urllib.request.Request(https_url(url, label=label))
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
DESCRIPTION_FIELDS = ("release_version", "created_at", "build_id", "board")

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
            release_id = artifact_trust.validate_release_id(entry.get("release_id"))
            manifest_url = https_url(entry.get("manifest_url"), label="the manifest url")
            signature_url = https_url(entry.get("signature_url"), label="the signature url")
            archive_url = https_url(entry.get("archive_url"), label="the archive url")
        except (FetchError, artifact_trust.ReleaseError):
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


__all__ = [
    "FetchError",
    "HttpsFetcher",
    "https_url",
    "parse_index",
]
