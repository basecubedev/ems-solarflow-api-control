# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build real OS update artifacts for the release-authority tests.

The archives here are genuine ``tar`` streams holding genuine Android Sparse
containers, which is the shape image-rota actually produces. Fixtures that used
plain payload bytes were the reason a writer that copied member bytes onto a
partition looked correct for as long as it did.

Signature verification is driven through the recording command runner, because
``gpg`` behaviour is not what these tests are about — what is under test is that
the appliance refuses an artifact whose signature did not verify.

The fake still has to answer the way gpg answers. A verifier that reads the
signing key out of ``--status-fd`` output cannot be proven by a fake that
returns exit 0 and says nothing, so the runner emits the same NEWSIG/GOODSIG/
VALIDSIG lines a real verification produces.
"""

import io
import json
import tarfile
from pathlib import Path

from appliance.commands import CommandResult, RecordingRunner
from appliance.os_releases import (
    MANIFEST_FORMAT_VERSION,
    OsReleaseCatalogue,
    ReleaseSource,
)
from appliance.sparse import ENCODING_ANDROID_SPARSE
from appliance.ab_persistence import PERSISTENT_SCHEMA_VERSION
from tests.helpers import android_sparse

LAYOUT_ID = "ems-appliance-rota-v1"
# The bounded board class the appliance normalises its device tree to, not a
# raw compatible string: one board answers to several, and an artefact is
# matched against the class its device layer was built for.
BOARD = "pi4"

DEVICE_LAYER = "rpi4"

# The expanded filesystems, and the sparse containers image-rota wraps them in.
BOOT_EXPANDED = android_sparse.expanded(android_sparse.image_of(b"bootfs" * 512, tail_blocks=2))
ROOT_EXPANDED = android_sparse.expanded(android_sparse.mixed_chunks())
BOOT = android_sparse.build(android_sparse.image_of(b"bootfs" * 512, tail_blocks=2))
ROOT = android_sparse.build(android_sparse.mixed_chunks())


def digest_of(blob):
    import hashlib

    return "sha256:" + hashlib.sha256(blob).hexdigest()


def build_archive(path, members):
    """A deterministic ``.tar`` holding exactly ``members``."""

    with tarfile.open(path, "w") as archive:
        for name, blob in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(blob))
    return Path(path)


def build_manifest(
    *,
    release_version="1.5.0",
    build_id="20260807-1",
    archive_name="release.tar",
    archive_digest,
    archive_size,
    members,
    layout_id=LAYOUT_ID,
    slot_schema_version=2,
    persistent_schema_version=PERSISTENT_SCHEMA_VERSION,
    minimum_appliance_manager_version="0.1.0",
    architecture="arm64",
    device_layer=DEVICE_LAYER,
    compatible_hardware=(BOARD,),
    **overrides,
):
    payload = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "release_version": release_version,
        "build_id": build_id,
        "created_at": "2026-08-07T00:00:00Z",
        "architecture": architecture,
        "device_layer": device_layer,
        "compatible_hardware": list(compatible_hardware),
        "os_release": "Raspberry Pi OS Trixie arm64",
        "rpi_image_gen_revision": "abc1234",
        "project_revision": "def5678",
        "appliance_manager_version": "0.9.0",
        "minimum_appliance_manager_version": minimum_appliance_manager_version,
        "layout_id": layout_id,
        "slot_schema_version": slot_schema_version,
        "persistent_schema_version": persistent_schema_version,
        "archive": {
            "name": archive_name,
            "digest": archive_digest,
            "size_bytes": archive_size,
            "compression": "none",
        },
        "members": members,
    }
    payload.update(overrides)
    return payload


# rpi-image-gen's own member names, produced by image-rota's post-image.sh.
# One boot payload, because upstream builds one bit-for-bit identical slot pair.
DEFAULT_MEMBERS = {
    "boot": {
        "role": "boot",
        "encoding": ENCODING_ANDROID_SPARSE,
        "encoded_sha256": digest_of(BOOT),
        "expanded_sha256": digest_of(BOOT_EXPANDED),
        "expanded_size": len(BOOT_EXPANDED),
        "filesystem": "vfat",
    },
    "system": {
        "role": "root",
        "encoding": ENCODING_ANDROID_SPARSE,
        "encoded_sha256": digest_of(ROOT),
        "expanded_sha256": digest_of(ROOT_EXPANDED),
        "expanded_size": len(ROOT_EXPANDED),
        "filesystem": "ext4",
    },
}


class ReleaseDirectory:
    """A root-owned release directory holding one or more artifacts."""

    def __init__(self, tmp_path, *, keyring=True, allow_unsigned=False):
        self.root = Path(tmp_path) / "releases"
        self.root.mkdir(parents=True, exist_ok=True)
        self.keyring_path = Path(tmp_path) / "os-release.gpg"
        if keyring:
            self.keyring_path.write_bytes(b"fake-keyring")
        self.allow_unsigned = allow_unsigned
        self.gpg_ok = True
        self.signing_fingerprint = "B6B70E6B2B7FEB0D649D8748480FAA4AAE458FC7"

    def publish(
        self,
        release_id="ems-solarflow-appliance-1.5.0-arm64-ab",
        *,
        blobs=None,
        signed=True,
        manifest_overrides=None,
        member_overrides=None,
        archive_members=None,
    ):
        blobs = blobs or {"boot": BOOT, "system": ROOT}
        archive_path = self.root / f"{release_id}.tar"
        build_archive(archive_path, archive_members if archive_members is not None else blobs)
        payload = archive_path.read_bytes()

        members = {
            name: dict(entry) for name, entry in (member_overrides or DEFAULT_MEMBERS).items()
        }
        manifest = build_manifest(
            archive_name=archive_path.name,
            archive_digest=digest_of(payload),
            archive_size=len(payload),
            members=members,
            **(manifest_overrides or {}),
        )
        manifest_path = self.root / f"{release_id}.manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if signed:
            manifest_path.with_suffix(".json.asc").write_text("signature\n", encoding="utf-8")
        return release_id

    def unsign(self, release_id):
        (self.root / f"{release_id}.manifest.json.asc").unlink()
        return release_id

    def rewrite_manifest(self, release_id, mutate):
        path = self.root / f"{release_id}.manifest.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return release_id

    def source(self, **overrides):
        values = {
            "directory": str(self.root),
            "keyring": str(self.keyring_path),
            "allow_unsigned": self.allow_unsigned,
        }
        values.update(overrides)
        return ReleaseSource(**values)

    def runner(self):
        directory = self

        class _Runner(RecordingRunner):
            def run(self, tool, args=(), **kwargs):
                if tool == "gpgv":
                    self.calls.append((tool, tuple(args), None))
                    if not directory.gpg_ok:
                        return CommandResult(tool, tuple(args), 1, "", "BAD signature")
                    fingerprint = directory.signing_fingerprint
                    status = (
                        "[GNUPG:] NEWSIG\n"
                        f"[GNUPG:] GOODSIG {fingerprint[-16:]} EMS Appliance Release\n"
                        f"[GNUPG:] VALIDSIG {fingerprint} 2026-01-01 1767225600 0 4 0 22 8 00 "
                        f"{fingerprint}\n"
                    )
                    return CommandResult(tool, tuple(args), 0, status, "")
                return super().run(tool, args, **kwargs)

        return _Runner({})

    def catalogue(self, **overrides):
        return OsReleaseCatalogue(self.source(**overrides), runner=self.runner())
