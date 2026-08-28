#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reject tracked files a source tree must never carry.

    scripts/check_repository_hygiene.py [--repo DIR] [--rev REV] [--json]
                                        [--max-bytes N] [--allow GLOB]...

A process core dump, a private key or a virtual machine disk in the tracked
tree is a supply-chain problem, not an untidy checkout: whatever is tracked is
what a push, a release bundle and a reviewer receive. The report names paths,
sizes and categories only -- it never prints the content of a rejected file.

Exit status: 0 the tracked tree is clean, 1 something was rejected, 2 the
command line is wrong.
"""

import argparse
import fnmatch
import json
import subprocess
import sys

DEFAULT_MAX_BYTES = 512 * 1024

CORE_DUMP_PATTERNS = ("core", "core.*", "*.core")

VM_DISK_PATTERNS = ("*.qcow2", "*.qcow", "*.vmdk", "*.vdi", "*.raw", "*.iso")

TEMPORARY_PATTERNS = ("*.log", "*.tmp", "*.bak", "*.orig", "*.rej", "*.swp", "*~")

PYTHON_CACHE_PATTERNS = ("*.pyc", "*.pyo", "*/__pycache__/*", "__pycache__/*")

# Bounded release evidence is committed on purpose: it is what a reviewer reads
# instead of the 16 GiB artefacts it describes.
SCRATCH_EXCEPTIONS = ("reports/appliance/*",)

SCRATCH_PATTERNS = (
    "dist/*",
    "reports/*",
    "test-results/*",
    "playwright-report/*",
    "blob-report/*",
    "node_modules/*",
    ".venv/*",
    "build-*/*",
)

PRIVATE_KEY_MARKERS = (
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
    b"-----BEGIN PGP PRIVATE KEY BLOCK-----",
    # The armored form base64-encoded, which is what a signing subkey looks like
    # on its way to a CI secret, and which carries no literal marker at all.
    # Three fragments because base64 has three alignments and the armor's offset
    # in the file decides which one it lands on.
    b"QkVHSU4gUEdQIFBSSVZBVEUgS0VZIEJM",
    b"R0lOIFBHUCBQUklWQVRFIEtFWSBCTE9D",
    b"RUdJTiBQR1AgUFJJVkFURSBLRVkgQkxP",
)

# Deliberate project media and one generated single-file frontend. Everything
# else above the size threshold has to be argued for here before it is tracked.
SIZE_ALLOWLIST = (
    "docs/assets/*",
    "admin/static/admin.js",
)

# A test fixture may legitimately carry a key-shaped or archive-shaped file.
CONTENT_ALLOWLIST = (
    "tests/fixtures/*",
)

ELF_MAGIC = b"\x7fELF"
ET_CORE = 4
PREFIX_BYTES = 512


class Rejection:
    def __init__(self, path, size, category, reason):
        self.path = path
        self.size = size
        self.category = category
        self.reason = reason

    def as_dict(self):
        return {
            "path": self.path,
            "size_bytes": self.size,
            "category": self.category,
            "reason": self.reason,
            "allowlisted": False,
        }


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Check the tracked tree for files it must never carry."
    )
    parser.add_argument("--repo", default=".", help="Repository to check (default: .)")
    parser.add_argument("--rev", default="HEAD", help="Revision to check (default: HEAD)")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=DEFAULT_MAX_BYTES,
        help="Largest tracked blob that needs no allowlist entry.",
    )
    parser.add_argument(
        "--allow",
        action="append",
        default=[],
        help="Additional allowlist glob for oversized files. Can be repeated.",
    )
    parser.add_argument("--json", action="store_true", help="Write a machine-readable report.")
    return parser.parse_args(argv)


def git(repo, *args, binary=False):
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"git {' '.join(args)}: {message}")
    return result.stdout if binary else result.stdout.decode("utf-8")


def tracked_blobs(repo, rev):
    entries = []
    for line in git(repo, "ls-tree", "-r", "-l", "-z", rev).split("\0"):
        if not line:
            continue
        meta, path = line.split("\t", 1)
        mode, kind, sha, size = meta.split()
        if kind != "blob":
            continue
        entries.append((path, sha, mode, -1 if size == "-" else int(size)))
    return entries


def blob_prefixes(repo, shas):
    """Read a bounded prefix of every blob through one git process."""
    if not shas:
        return {}
    process = subprocess.Popen(
        ["git", "-C", repo, "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    request = "".join(f"{sha}\n" for sha in shas).encode("ascii")
    process.stdin.write(request)
    process.stdin.flush()
    process.stdin.close()

    prefixes = {}
    out = process.stdout
    try:
        for sha in shas:
            header = out.readline().decode("ascii", "replace").split()
            if len(header) != 3:
                break
            size = int(header[2])
            remaining = size
            prefix = b""
            while remaining > 0:
                chunk = out.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
                if len(prefix) < PREFIX_BYTES:
                    prefix += chunk[: PREFIX_BYTES - len(prefix)]
            out.read(1)
            prefixes[sha] = prefix
    finally:
        out.close()
        process.wait()
    return prefixes


def matches_any(path, patterns):
    name = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern):
            return True
    return False


def is_elf_core(prefix):
    if not prefix.startswith(ELF_MAGIC) or len(prefix) < 18:
        return False
    return int.from_bytes(prefix[16:18], "little") == ET_CORE


def carries_private_key(prefix):
    return any(marker in prefix for marker in PRIVATE_KEY_MARKERS)


def categorize(path):
    if any(fnmatch.fnmatch(path, pattern) for pattern in SCRATCH_EXCEPTIONS):
        return None
    if matches_any(path, CORE_DUMP_PATTERNS):
        return "core_dump"
    if matches_any(path, VM_DISK_PATTERNS):
        return "vm_disk_image"
    if matches_any(path, PYTHON_CACHE_PATTERNS):
        return "python_cache"
    if matches_any(path, SCRATCH_PATTERNS):
        return "builder_scratch"
    if matches_any(path, TEMPORARY_PATTERNS):
        return "temporary_output"
    return None


def check(repo, rev, max_bytes, extra_allow):
    size_allowlist = tuple(SIZE_ALLOWLIST) + tuple(extra_allow)
    entries = tracked_blobs(repo, rev)
    rejections = []
    allowed = []

    candidates = [
        sha
        for path, sha, _mode, size in entries
        if not matches_any(path, CONTENT_ALLOWLIST) and (size < 0 or size >= len(ELF_MAGIC))
    ]
    prefixes = blob_prefixes(repo, candidates)

    for path, sha, mode, size in entries:
        if mode == "120000":
            continue
        prefix = prefixes.get(sha, b"")
        category = categorize(path)
        if category:
            rejections.append(
                Rejection(path, size, category, f"tracked {category.replace('_', ' ')}")
            )
            continue
        if is_elf_core(prefix):
            rejections.append(Rejection(path, size, "core_dump", "tracked ELF core dump"))
            continue
        if carries_private_key(prefix) and not matches_any(path, CONTENT_ALLOWLIST):
            rejections.append(Rejection(path, size, "private_key", "tracked private key"))
            continue
        if size > max_bytes:
            if matches_any(path, size_allowlist):
                allowed.append(
                    {
                        "path": path,
                        "size_bytes": size,
                        "category": "project_media",
                        "allowlisted": True,
                    }
                )
                continue
            rejections.append(
                Rejection(
                    path,
                    size,
                    "oversized_blob",
                    f"{size} bytes exceeds the {max_bytes} byte source limit",
                )
            )

    return {
        "revision": git(repo, "rev-parse", rev).strip(),
        "tracked_files": len(entries),
        "max_bytes": max_bytes,
        "rejected": [item.as_dict() for item in rejections],
        "allowlisted": sorted(allowed, key=lambda item: -item["size_bytes"]),
    }


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = check(args.repo, args.rev, args.max_bytes, args.allow)
    except RuntimeError as error:
        print(f"repository-hygiene: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for item in report["allowlisted"]:
            print(f"allowed   {item['size_bytes']:>10}  {item['category']:<16} {item['path']}")
        for item in report["rejected"]:
            print(f"REJECTED  {item['size_bytes']:>10}  {item['category']:<16} {item['path']}")
        print()
        print(f"revision:  {report['revision']}")
        print(f"tracked:   {report['tracked_files']} files")
        print(f"rejected:  {len(report['rejected'])}")

    if report["rejected"]:
        if not args.json:
            print("RESULT: FAIL")
        return 1
    if not args.json:
        print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
