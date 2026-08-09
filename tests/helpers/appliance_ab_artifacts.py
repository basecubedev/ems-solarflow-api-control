# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build real OS update artifacts for the release-authority tests.

The archives here are genuine ``tar`` streams, so the extractor is exercised
against actual members rather than a mock: a traversal test that never produced
a traversing member would prove nothing. Signature verification is driven
through the recording command runner, because ``gpg`` behaviour is not what
these tests are about — what is under test is that the appliance refuses an
artifact whose signature did not verify.
"""

import io
import json
import tarfile
from pathlib import Path

from appliance.commands import CommandResult, RecordingRunner
from appliance.os_releases import OsReleaseCatalogue, ReleaseSource

LAYOUT_ID = "ems-appliance-rota-v1"
BOARD = "raspberrypi,4-model-b"

BOOT = b"bootfs" * 512
ROOT = b"rootfs" * 4096


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
    persistent_schema_version=2,
    minimum_appliance_manager_version="0.1.0",
    architecture="arm64",
    compatible_hardware=(BOARD,),
    **overrides,
):
    payload = {
        "format_version": 1,
        "release_version": release_version,
        "build_id": build_id,
        "created_at": "2026-08-07T00:00:00Z",
        "architecture": architecture,
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
    "boot": {"digest": digest_of(BOOT), "role": "boot"},
    "system": {"digest": digest_of(ROOT), "role": "root"},
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
                if tool == "gpg":
                    self.calls.append((tool, tuple(args), None))
                    return CommandResult(
                        tool, tuple(args), 0 if directory.gpg_ok else 1, "", ""
                    )
                return super().run(tool, args, **kwargs)

        return _Runner({})

    def catalogue(self, **overrides):
        return OsReleaseCatalogue(self.source(**overrides), runner=self.runner())
