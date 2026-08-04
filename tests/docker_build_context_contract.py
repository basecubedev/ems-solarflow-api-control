"""Small Docker build-context contract helper used by image tests.

This intentionally implements only the Dockerfile/.dockerignore forms used by
this repository: shell-form ``COPY`` instructions with repository-local
sources, directory exclusions, glob exclusions, and ordered ``!`` exceptions.
Stage-to-stage ``COPY --from=...`` instructions are not repository-context
reads and are skipped.
"""

from __future__ import annotations

import fnmatch
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class CopySourceIssue:
    dockerfile: Path
    line_number: int
    source: str
    reason: str

    def __str__(self) -> str:
        return (
            f"{self.dockerfile}:{self.line_number}: COPY source {self.source!r} "
            f"{self.reason}"
        )


class DockerIgnore:
    """Ordered matcher for the subset of Dockerignore syntax used here."""

    def __init__(self, path: Path):
        self.rules: list[tuple[bool, str]] = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            pattern = line[1:] if negated else line
            pattern = pattern.replace("\\", "/").lstrip("/")
            if pattern:
                self.rules.append((negated, pattern))

    @staticmethod
    def _matches(path: str, pattern: str) -> bool:
        path = path.strip("/")
        directory_rule = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        if not pattern:
            return False

        if directory_rule:
            return path == pattern or path.startswith(pattern + "/")

        if "/" not in pattern:
            return any(fnmatch.fnmatchcase(part, pattern) for part in path.split("/"))

        # PurePath.match handles the ``**/`` patterns present in this repo. An
        # exact fnmatch check covers ordinary rooted paths and exceptions.
        return fnmatch.fnmatchcase(path, pattern) or PurePosixPath(path).match(pattern)

    def is_ignored(self, relative_path: str) -> bool:
        ignored = False
        # Strip a leading ``./`` prefix and any trailing slash, but preserve a
        # leading dot that is part of a dotfile/dir name (``.env``, ``.claude/``)
        # so dot-prefixed rules match exactly as real Docker evaluates them.
        normalized = relative_path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.lstrip("/").rstrip("/")
        for negated, pattern in self.rules:
            if self._matches(normalized, pattern):
                ignored = not negated
        return ignored


def _logical_dockerfile_lines(path: Path):
    """Yield ``(first_line_number, instruction)`` with continuations joined."""

    pending: list[str] = []
    first_line = 0
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        if not pending:
            first_line = number
        continued = stripped.endswith("\\")
        pending.append(stripped[:-1].rstrip() if continued else stripped)
        if not continued:
            yield first_line, " ".join(pending)
            pending = []
    if pending:
        yield first_line, " ".join(pending)


def repository_copy_sources(dockerfile: Path):
    """Yield repository-context COPY sources with their Dockerfile line."""

    for line_number, instruction in _logical_dockerfile_lines(dockerfile):
        if not instruction.upper().startswith("COPY "):
            continue
        try:
            tokens = shlex.split(instruction, comments=False, posix=True)
        except ValueError as exc:  # malformed COPY should become a contract issue
            yield line_number, f"<unparseable: {exc}>"
            continue
        if len(tokens) < 3:
            yield line_number, "<missing source>"
            continue
        arguments = tokens[1:]
        options = []
        while arguments and arguments[0].startswith("--"):
            options.append(arguments.pop(0))
        if any(option == "--from" or option.startswith("--from=") for option in options):
            continue
        for source in arguments[:-1]:
            yield line_number, source


def _source_has_context_content(source_path: Path, relative: str, ignore: DockerIgnore) -> bool:
    if source_path.is_file():
        return not ignore.is_ignored(relative)
    if not source_path.is_dir():
        return False
    return any(
        path.is_file()
        and not ignore.is_ignored(
            (PurePosixPath(relative) / path.relative_to(source_path)).as_posix()
        )
        for path in source_path.rglob("*")
    )


def validate_repository_copy_sources(
    *, context_root: Path, dockerfile: Path, dockerignore: Path | None = None
) -> list[CopySourceIssue]:
    """Return missing/excluded repository-local COPY source issues."""

    context_root = context_root.resolve()
    dockerfile = dockerfile.resolve()
    ignore = DockerIgnore(dockerignore or context_root / ".dockerignore")
    issues: list[CopySourceIssue] = []

    for line_number, source in repository_copy_sources(dockerfile):
        if source.startswith("$"):
            issues.append(
                CopySourceIssue(
                    dockerfile,
                    line_number,
                    source,
                    "is dynamic and cannot be verified",
                )
            )
            continue
        matches = sorted(context_root.glob(source)) if any(c in source for c in "*?[") else []
        candidates = matches or [context_root / source]
        if not matches and not candidates[0].exists():
            issues.append(CopySourceIssue(dockerfile, line_number, source, "does not exist"))
            continue
        for candidate in candidates:
            try:
                relative = candidate.resolve().relative_to(context_root).as_posix()
            except ValueError:
                issues.append(
                    CopySourceIssue(dockerfile, line_number, source, "escapes the build context")
                )
                continue
            if not _source_has_context_content(candidate, relative, ignore):
                issues.append(
                    CopySourceIssue(dockerfile, line_number, source, "is excluded by .dockerignore")
                )
    return issues
