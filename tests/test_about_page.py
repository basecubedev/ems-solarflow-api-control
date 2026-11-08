# SPDX-FileCopyrightText: 2026 basecubedev
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The About page shows every notice the Modern front ends ship, and names its build.

Each Modern build ships the notices of what it bundles beside the bundle: a
``licenses.json`` Vite writes and one ``*.LICENSE`` file per copied font, icon
set or helper. The About page reads them at run time, so a notice file the page
does not ask for would ship unseen. The image builds write ``build.json`` beside
the bundle with the version and commit the page links the source of.
"""

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.documentation]

ROOT = Path(__file__).resolve().parents[1]
APPS = {
    "dashboard": ("frontend/dashboard-modern", {"uPlot.LICENSE": "dashboard/static/uPlot.LICENSE"}),
    "admin": ("frontend/admin-modern", {}),
    "appliance": ("frontend/appliance-modern", {}),
}
SHARED_ABOUT = ROOT / "frontend" / "shared" / "lib" / "about.js"


def requested(app):
    """Every notice path the app's About page fetches: the shared ones and the app's own."""

    source, _ = APPS[app]
    text = SHARED_ABOUT.read_text(encoding="utf-8") + (ROOT / source / "src" / "App.svelte").read_text(encoding="utf-8")
    return set(re.findall(r'"/([\w.-]+\.(?:LICENSE|json))"', text))


@pytest.mark.parametrize("app", sorted(APPS))
def test_the_about_page_reads_every_notice_its_build_ships(app):
    _, extra = APPS[app]
    shipped = {path.name for path in (ROOT / app / "static" / "modern").glob("*.LICENSE")} | set(extra)
    assert shipped
    asked = requested(app)
    assert "licenses.json" in asked and "build.json" in asked
    assert shipped <= asked, sorted(shipped - asked)
    missing = sorted(name for name in asked if name.endswith(".LICENSE") and name not in shipped)
    assert not missing, f"the About page asks for notices the {app} build does not ship: {missing}"
    for target in extra.values():
        assert (ROOT / target).is_file()


@pytest.mark.parametrize("app", sorted(APPS))
def test_the_about_page_is_reachable_in_every_app(app):
    source, _ = APPS[app]
    pages = (ROOT / source / "src" / "app" / "pages.js").read_text(encoding="utf-8")
    assert re.search(r'\{ id: "about", icon: "info", group: "system",[^}]*hidden: true \}', pages)
    app_source = (ROOT / source / "src" / "App.svelte").read_text(encoding="utf-8")
    assert re.search(r'about: (?:About\b|\(\) => import\("\.\./\.\./shared/widgets/About\.svelte"\))', app_source)
    assert 'import About from "../../shared/widgets/About.svelte"' in app_source or "shared/widgets/About.svelte" in app_source


@pytest.mark.parametrize(
    ("dockerfile", "target", "revision"),
    (
        ("Dockerfile", "/app/dashboard/static/modern/build.json", "EMS_GIT_COMMIT"),
        ("deploy/admin/Dockerfile", "/app/admin/static/modern/build.json", "EMS_REVISION"),
    ),
)
def test_each_image_build_names_its_version_and_commit_for_the_about_page(dockerfile, target, revision):
    text = (ROOT / dockerfile).read_text(encoding="utf-8")
    line = next((line for line in text.splitlines() if target in line), "")
    assert line.startswith("RUN python"), dockerfile
    assert '"version": os.environ["EMS_RELEASE_TAG"]' in line
    assert f'"revision": os.environ["{revision}"]' in line
    assert text.index(f"ARG {revision}") < text.index(line)
