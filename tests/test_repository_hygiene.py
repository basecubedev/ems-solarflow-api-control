# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a tracked source tree must never carry.

An 822 MB process core dump reached a commit on this branch. A dump is the
crashed process's memory: credentials, tokens, request headers and user data,
all of it in a file that a push, a source bundle and a review archive would
have carried onward. The name was ``core.1581108``, so nothing about it looked
like a source file, and nothing in the tree checked.

The gate therefore looks at what is *tracked*, not at what is on disk, and it
rejects by shape rather than by name alone: a dump renamed ``notes.txt`` is
still an ELF core file, and a private key is still a private key wherever it
sits. Oversized blobs need a named allowlist entry, so the next accidental
half-gigabyte arrives as a review question instead of as a release.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_repository_hygiene import check

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts/check_repository_hygiene.py"


def make_repo(tmp_path, files):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    subprocess.run(["git", "-C", str(repo), "add", "-A", "-f"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "test"], check=True)
    return repo


def elf_core_bytes(payload=b"secret-token"):
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[6] = 1
    header[16:18] = (4).to_bytes(2, "little")
    header[18:20] = (62).to_bytes(2, "little")
    return bytes(header) + payload


def rejected_paths(report):
    return {item["path"]: item["category"] for item in report["rejected"]}


def test_a_tracked_elf_core_dump_is_rejected_whatever_it_is_called(tmp_path):
    repo = make_repo(tmp_path, {"notes.txt": elf_core_bytes(), "ems/app.py": b"x = 1\n"})

    report = check(str(repo), "HEAD", 512 * 1024, [])

    assert rejected_paths(report) == {"notes.txt": "core_dump"}


def test_a_core_dump_name_is_rejected_before_its_content_is_read(tmp_path):
    repo = make_repo(tmp_path, {"core.1581108": b"not even an elf file"})

    report = check(str(repo), "HEAD", 512 * 1024, [])

    assert rejected_paths(report) == {"core.1581108": "core_dump"}


@pytest.mark.parametrize("name", ["core", "core.4242", "crash.core"])
def test_every_configured_core_dump_pattern_is_rejected(tmp_path, name):
    repo = make_repo(tmp_path, {name: b"anything"})

    assert rejected_paths(check(str(repo), "HEAD", 512 * 1024, [])) == {name: "core_dump"}


def test_an_oversized_source_blob_needs_an_allowlist_entry(tmp_path):
    repo = make_repo(tmp_path, {"ems/generated.py": b"#" * 4096})

    report = check(str(repo), "HEAD", 1024, [])
    assert rejected_paths(report) == {"ems/generated.py": "oversized_blob"}

    allowed = check(str(repo), "HEAD", 1024, ["ems/generated.py"])
    assert allowed["rejected"] == []
    assert allowed["allowlisted"][0]["path"] == "ems/generated.py"
    assert allowed["allowlisted"][0]["allowlisted"] is True


def test_project_media_stays_allowed_and_is_reported_as_media(tmp_path):
    repo = make_repo(tmp_path, {"docs/assets/preview.png": b"\x89PNG" + b"\0" * 8192})

    report = check(str(repo), "HEAD", 1024, [])

    assert report["rejected"] == []
    assert report["allowlisted"][0]["category"] == "project_media"


def test_a_tracked_private_key_is_rejected(tmp_path):
    key = b"-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk=\n"
    repo = make_repo(tmp_path, {"packaging/id_ed25519": key})

    assert rejected_paths(check(str(repo), "HEAD", 512 * 1024, [])) == {
        "packaging/id_ed25519": "private_key"
    }


@pytest.mark.parametrize("pad", [b"", b"x", b"xy"])
def test_a_base64_encoded_private_key_is_still_a_private_key(tmp_path, pad):
    """The shape this project now asks a maintainer to produce.

    ``scripts/appliance-new-release-identity.sh`` exports a signing subkey and
    base64-encodes it for a GitHub secret, so the armored marker is not in the
    file as text and every literal signature above misses it. It refuses to
    write inside the repository, which is the first net; this is the one behind
    it. Three alignments because base64 has three, and where the armor starts in
    the file decides which one it lands on.
    """

    import base64

    armored = b"-----BEGIN PGP PRIVATE KEY BLOCK-----\nlQVYBGiz\n-----END PGP PRIVATE KEY BLOCK-----\n"
    encoded = base64.b64encode(pad + armored)
    repo = make_repo(tmp_path, {"packaging/subkey.b64": encoded})

    assert rejected_paths(check(str(repo), "HEAD", 512 * 1024, [])) == {
        "packaging/subkey.b64": "private_key"
    }


def test_a_tracked_vm_disk_and_build_scratch_are_rejected(tmp_path):
    repo = make_repo(
        tmp_path,
        {"builder.qcow2": b"QFI\xfb", "dist/image.build-authority.json": b"{}"},
    )

    assert rejected_paths(check(str(repo), "HEAD", 512 * 1024, [])) == {
        "builder.qcow2": "vm_disk_image",
        "dist/image.build-authority.json": "builder_scratch",
    }


def test_a_symlink_is_not_measured_as_a_blob(tmp_path):
    repo = make_repo(tmp_path, {"real.txt": b"x"})
    (repo / "link.txt").symlink_to("real.txt")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "link"], check=True)

    report = check(str(repo), "HEAD", 1, [])

    assert report["rejected"] == []


def test_the_report_never_carries_the_content_of_a_rejected_file(tmp_path):
    repo = make_repo(tmp_path, {"core.99": elf_core_bytes(b"AUTHORIZATION: Bearer hunter2")})

    report = check(str(repo), "HEAD", 512 * 1024, [])

    assert b"hunter2" not in json.dumps(report).encode()
    assert set(report["rejected"][0]) == {
        "path",
        "size_bytes",
        "category",
        "reason",
        "allowlisted",
    }


def test_the_checker_exits_non_zero_on_a_rejection(tmp_path):
    repo = make_repo(tmp_path, {"core.7": b"x"})

    result = subprocess.run(
        [sys.executable, str(CHECKER), "--repo", str(repo)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "RESULT: FAIL" in result.stdout
    assert "REJECTED" in result.stdout


def test_this_repository_is_clean(tmp_path):
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--repo", str(ROOT), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )

    report = json.loads(result.stdout)
    assert report["rejected"] == [], report["rejected"]
    assert result.returncode == 0


def test_the_ignore_rules_cover_the_common_core_dump_names():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "core" in ignore
    assert "core.*" in ignore
    assert "*.core" in ignore


def test_committed_release_evidence_is_not_read_as_build_scratch(tmp_path):
    """reports/ is scratch; the bounded appliance evidence in it is not.

    A reviewer reads reports/appliance/<run-id>/ instead of the 16 GiB
    artefacts it describes, so it is committed on purpose.
    """

    repo = make_repo(
        tmp_path,
        {
            "reports/appliance/2026-08-09-rc/result.json": b"{}",
            "reports/coverage/index.html": b"<html>",
        },
    )

    report = check(str(repo), "HEAD", 512 * 1024, [])

    assert rejected_paths(report) == {"reports/coverage/index.html": "builder_scratch"}


def test_a_core_dump_inside_the_evidence_directory_is_still_rejected(tmp_path):
    repo = make_repo(
        tmp_path, {"reports/appliance/2026-08-09-rc/core.99": elf_core_bytes()}
    )

    report = check(str(repo), "HEAD", 512 * 1024, [])

    assert rejected_paths(report) == {
        "reports/appliance/2026-08-09-rc/core.99": "core_dump"
    }
