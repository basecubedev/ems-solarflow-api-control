# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static contract: every workflow uses contexts where they actually exist.

A workflow that names a context GitHub does not provide at that position is
rejected *whole*. Not the step, not the job -- the file. The run that reports it
carries the file path as its name, `event: push` whatever the triggers say, and
no jobs at all, which reads like an infrastructure problem rather than a typo.

`.github/workflows/appliance-image.yml` shipped with
``EMS_RPI_IMAGE_GEN: ${{ runner.temp }}/rpi-image-gen`` in job-level ``env`` and
failed exactly that way. YAML parses it, ``bash -n`` never sees it, and the
project has no actionlint, so nothing local caught it.
"""

import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.contract]

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

# What a job-level key is evaluated against, before any step exists. `runner`
# describes the machine a step runs on, `steps` and `job` describe a run in
# progress, and `env` is being defined at that moment.
STEP_ONLY_CONTEXTS = ("runner", "steps", "job", "env")

REFERENCE = re.compile(r"\$\{\{\s*([a-z_]+)\.")


def contexts(value):
    return set(REFERENCE.findall(str(value)))


def jobs(path):
    return (yaml.safe_load(path.read_text(encoding="utf-8")).get("jobs") or {}).items()


def test_there_are_workflows_to_check():
    assert WORKFLOWS


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_job_level_env_uses_no_step_only_context(path):
    for name, job in jobs(path):
        for key, value in (job.get("env") or {}).items():
            used = contexts(value) & set(STEP_ONLY_CONTEXTS)

            assert not used, f"{path.name}: {name}.env.{key} names {sorted(used)}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_job_level_keys_use_no_step_only_context(path):
    """`runs-on`, `if`, `timeout-minutes` and `continue-on-error` are resolved
    before the job has a runner too."""

    for name, job in jobs(path):
        for key in ("runs-on", "if", "timeout-minutes", "continue-on-error", "name"):
            used = contexts(job.get(key, "")) & set(STEP_ONLY_CONTEXTS)

            assert not used, f"{path.name}: {name}.{key} names {sorted(used)}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda path: path.name)
def test_concurrency_and_triggers_use_no_step_only_context(path):
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    concurrency = document.get("concurrency")
    value = concurrency.get("group") if isinstance(concurrency, dict) else concurrency
    used = contexts(value or "") & set(STEP_ONLY_CONTEXTS)

    assert not used, f"{path.name}: concurrency names {sorted(used)}"
