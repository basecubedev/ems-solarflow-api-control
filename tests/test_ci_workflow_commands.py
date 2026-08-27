# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: the shell a workflow runs is shell that runs.

A workflow step's script is never executed until the run that needs it. The
image build's first step opened with ``df -PB1 --output=avail /`` -- coreutils
refuses that combination, ``-P`` and ``--output`` being mutually exclusive -- so
three matrix jobs died on their first line, before the checkout, after the plan
and package jobs had already passed. Nothing local could have caught it: YAML
parses it, ``bash -n`` parses it, and the flags are only rejected when df runs.

So the flags are run here. Every ``df`` invocation in every workflow is executed
against a directory that exists, with each operand replaced, which leaves the
option list as the only thing under test.
"""

import re
import shlex
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.S)
# A shell variable, in either form. Replaced before the line is cut up, so that
# the brace in "${root}" is not read as the end of a command.
VARIABLE = re.compile(r"\$\{[^{}]*\}|\$[A-Za-z_0-9]+")
# Where a command ends and the next thing begins, for the purpose of lifting one
# invocation out of a line.
TERMINATOR = re.compile(r"[|;>&)}\n]")


def scripts():
    """(workflow, job, step, script) for every shell step in the repository."""

    for path in WORKFLOWS:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (document.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if step.get("run") and step.get("shell") in (None, "bash", "sh"):
                    yield path.name, job_name, step.get("name") or "?", step["run"]


def test_there_are_shell_steps_to_check():
    assert list(scripts())


@pytest.mark.parametrize("workflow", [path.name for path in WORKFLOWS])
def test_every_shell_step_parses_as_bash(workflow):
    """The cheap half. It would not have caught the df line, and it catches the
    quoting mistake that the df line taught us to look for."""

    for name, job, step, script in scripts():
        if name != workflow:
            continue
        parsed = subprocess.run(
            ["bash", "-n"],
            input=EXPRESSION.sub("GHEXPR", script),
            text=True,
            capture_output=True,
            timeout=60,
        )

        assert parsed.returncode == 0, f"{name}: {job} / {step}\n{parsed.stderr}"


def df_invocations():
    for name, job, step, script in scripts():
        for line in script.splitlines():
            if line.lstrip().startswith("#"):
                continue
            line = VARIABLE.sub("OPERAND", EXPRESSION.sub("OPERAND", line))
            for match in re.finditer(r"\bdf\b", line):
                rest = line[match.end() :]
                end = TERMINATOR.search(rest)
                yield name, job, step, rest[: end.start()] if end else rest


def test_there_are_df_invocations_to_run():
    """The workflows measure free space in three places. If they stop, this file
    stops proving anything and should be reconsidered rather than left green."""

    assert list(df_invocations())


def options(argument_text):
    """The invocation with every operand replaced, so only the flags are tested."""

    return [token for token in shlex.split(argument_text) if token.startswith("-")]


@pytest.mark.parametrize(
    "where,arguments",
    [
        (f"{name}: {job} / {step}", arguments)
        for name, job, step, arguments in df_invocations()
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_every_df_invocation_is_a_form_df_accepts(where, arguments, tmp_path):
    run = subprocess.run(
        ["df", *options(arguments), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert run.returncode == 0, f"{where}: df {arguments.strip()}\n{run.stderr}"


def test_the_combination_that_failed_is_still_one_df_refuses():
    """The premise. If a future coreutils accepts -P beside --output, the test
    above stops distinguishing the defect from the fix and should be revisited."""

    run = subprocess.run(
        ["df", "-PB1", "--output=avail", "/"], capture_output=True, text=True, timeout=60
    )

    assert run.returncode != 0, "this df accepts the combination that broke the image build"
