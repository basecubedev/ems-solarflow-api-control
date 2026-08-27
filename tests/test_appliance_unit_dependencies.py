# SPDX-License-Identifier: AGPL-3.0-or-later
"""No shipped unit may depend on something the package does not install.

An unsatisfiable ``Requires=`` does not fail one unit — systemd fails the whole
job transaction, so a unit removed while another still names it takes the agent,
the web service and anything else in that transaction down with it. On an
appliance with no login account that is not a degraded service, it is a card
reader.

This matters most for a removal that has not happened yet. The A/B units are
going away, and seven surviving units name ``ems-appliance-persistence.service``
today — five with ``Requires=``. Those edges have to be cut *before* the unit
file is, and the coupling is invisible unless something asserts it. That is what
this module is for.

``Documentation=`` is checked for the same reason in the smaller key: a URI
pointing into ``/usr/share/doc`` for a file the package stopped shipping is a
dead reference that nothing else would notice.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "appliance"
UNIT_DIR = PACKAGING / "systemd"
BUILD = PACKAGING / "build-deb.sh"

pytestmark = [pytest.mark.contract, pytest.mark.simulation, pytest.mark.appliance]

DEPENDENCY_DIRECTIVES = ("Requires", "Wants", "Requisite", "BindsTo", "After", "Before", "PartOf")
DOC_PREFIX = "file:///usr/share/doc/ems-appliance-manager/"


def shipped_units():
    """Unit files the package installs, by name."""

    build = BUILD.read_text(encoding="utf-8")
    return {
        path.name
        for path in sorted(UNIT_DIR.glob("*"))
        if f'systemd/{path.name}"' in build
    }


def declared_dependencies(path):
    """Every ems-appliance unit this one names, by directive."""

    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry.startswith("#") or "=" not in entry:
            continue
        directive, _, value = entry.partition("=")
        if directive.strip() not in DEPENDENCY_DIRECTIVES:
            continue
        for name in value.split():
            if name.startswith("ems-appliance"):
                found.append((directive.strip(), name))
    return found


def test_the_package_ships_the_units_this_module_reasons_about():
    """A check over an empty set passes for the wrong reason."""

    assert len(shipped_units()) >= 8


@pytest.mark.parametrize("path", sorted(UNIT_DIR.glob("*")), ids=lambda p: p.name)
def test_every_unit_dependency_names_a_unit_the_package_installs(path):
    shipped = shipped_units()

    for directive, name in declared_dependencies(path):
        assert name in shipped, (
            f"{path.name} has {directive}={name}, which the package does not install. "
            "An unsatisfiable Requires= fails the whole systemd transaction, not one unit."
        )


@pytest.mark.parametrize("path", sorted(UNIT_DIR.glob("*")), ids=lambda p: p.name)
def test_every_documentation_uri_names_a_file_the_package_ships(path):
    build = BUILD.read_text(encoding="utf-8").replace("\\\n", " ")
    block = re.search(r"for document in (.+?); do", build, re.S)
    assert block, "build-deb.sh no longer installs a document list this can read"
    names = set(block.group(1).split())

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("Documentation="):
            continue
        for uri in line.partition("=")[2].split():
            if not uri.startswith(DOC_PREFIX):
                continue
            stem = uri[len(DOC_PREFIX) :].removesuffix(".md")
            assert stem in names or stem == "copyright", (
                f"{path.name} documents {stem}.md, which build-deb.sh does not install"
            )


def test_the_growth_unit_is_installed_and_removed_with_the_package():
    """A package on plain Raspberry Pi OS never grew its root without this."""

    postinst = (PACKAGING / "debian" / "postinst").read_text(encoding="utf-8")
    prerm = (PACKAGING / "debian" / "prerm").read_text(encoding="utf-8")

    assert "ems-appliance-grow-root.service" in postinst
    assert "ems-appliance-grow-root.service" in prerm
