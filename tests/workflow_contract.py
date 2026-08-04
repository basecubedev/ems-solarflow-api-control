"""Helpers for executing small GitHub Actions ``run`` steps as contracts."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def extract_run_step(workflow: Path, step_name: str) -> str:
    lines = workflow.read_text(encoding="utf-8").splitlines()
    marker = f"- name: {step_name}"
    start = next(
        index for index, line in enumerate(lines) if line.strip() == marker
    )
    run_index = next(
        index
        for index in range(start + 1, len(lines))
        if lines[index].strip() == "run: |"
    )
    run_indent = len(lines[run_index]) - len(lines[run_index].lstrip())
    body: list[str] = []
    for line in lines[run_index + 1 :]:
        if line.strip():
            indent = len(line) - len(line.lstrip())
            if indent <= run_indent:
                break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


def run_output_step(
    workflow: Path,
    step_name: str,
    *,
    cwd: Path,
    tmp_path: Path,
    environ: dict[str, str],
) -> tuple[subprocess.CompletedProcess, dict[str, str]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    output = tmp_path / "github-output.txt"
    env = {**os.environ, **environ, "GITHUB_OUTPUT": str(output)}
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", extract_run_step(workflow, step_name)],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    values: dict[str, str] = {}
    if output.is_file():
        for line in output.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
    return result, values
