# SPDX-License-Identifier: AGPL-3.0-or-later
"""The chroot chain rule, judged without depending on who runs the tests.

``sshd`` refuses a ``ChrootDirectory`` unless every component of its path is
owned by root and writable by nobody else, so the appliance has to refuse the
same thing before it promises a confined export. That check only fires for a
real root process, which is why it used to be invisible to the normal test run
and to fail the root run for the temporary tree rather than for the rule.

The observations are injected here: the rule is what is under test, not the
ownership of the developer's ``/tmp``.
"""

import os
import stat

import pytest

from appliance.paths import chroot_chain_problems

pytestmark = [pytest.mark.unit, pytest.mark.simulation, pytest.mark.backup_restore, pytest.mark.appliance]

EXPORT_ROOT = "/srv/ems-appliance-export"


def chain(**overrides):
    """A ``stat`` that answers for a production-shaped chroot chain."""

    def observed(component):
        mode, uid, gid = overrides.get(str(component), (0o755, 0, 0))
        return os.stat_result(
            (stat.S_IFDIR | mode, 0, 0, 1, uid, gid, 0, 0, 0, 0)
        )

    return observed


def problems(**overrides):
    return chroot_chain_problems(EXPORT_ROOT, stat_fn=chain(**overrides), euid=0)


def test_a_root_owned_chain_is_accepted():
    assert problems() == []


def test_a_component_owned_by_somebody_else_is_refused():
    assert problems(**{"/srv": (0o755, 1000, 0)}) == ["/srv is not owned by root"]


def test_the_export_root_itself_must_be_root_owned():
    assert problems(**{EXPORT_ROOT: (0o755, 1000, 0)}) == [
        f"{EXPORT_ROOT} is not owned by root"
    ]


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777, 0o733])
def test_a_component_writable_by_group_or_others_is_refused(mode):
    assert problems(**{"/srv": (mode, 0, 0)}) == ["/srv is writable by group or others"]


def test_every_failing_component_is_named():
    assert problems(**{"/srv": (0o777, 1000, 0)}) == [
        "/srv is not owned by root",
        "/srv is writable by group or others",
    ]


def test_a_component_that_cannot_be_stated_is_skipped_not_guessed():
    def missing(component):
        if str(component) == "/srv":
            raise FileNotFoundError(component)
        return os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    assert chroot_chain_problems(EXPORT_ROOT, stat_fn=missing, euid=0) == []


def test_an_unprivileged_process_cannot_judge_the_chain():
    """Only root sees the whole chain, so an unprivileged answer is no answer."""

    assert chroot_chain_problems(EXPORT_ROOT, stat_fn=chain(), euid=1000) == []
    assert (
        chroot_chain_problems(
            EXPORT_ROOT, stat_fn=chain(**{"/srv": (0o777, 1000, 0)}), euid=1000
        )
        == []
    )
