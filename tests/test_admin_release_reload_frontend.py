# SPDX-License-Identifier: AGPL-3.0-or-later
"""Frontend contract: the guided-setup release step can reload the build list."""

import os

import pytest

pytestmark = [
    pytest.mark.admin,
    pytest.mark.system_build,
    pytest.mark.contract,
    pytest.mark.simulation,
]

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "admin", "static"
)


def _read(name):
    with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as handle:
        return handle.read()


def _release_form(html):
    return html.split('id="release-form"', 1)[1].split('id="release-status"', 1)[0]


def test_release_form_offers_a_reload_button():
    form = _release_form(_read("index.html"))
    assert 'id="release-reload"' in form
    assert 'data-testid="system-build-reload"' in form
    assert "Reload" in form


def test_reload_button_refetches_the_release_list():
    js = _read("admin.js")
    assert 'releaseReload: document.getElementById("release-reload")' in js
    anchor = 'setupEls.releaseReload.addEventListener("click"'
    assert anchor in js
    assert "loadReleases" in js.split(anchor, 1)[1][:160]


def _upgrade_release_form(html):
    return html.split('id="upgrade-release-form"', 1)[1].split(
        'id="upgrade-release-badges"', 1
    )[0]


def test_upgrade_release_form_offers_a_reload_button():
    form = _upgrade_release_form(_read("index.html"))
    assert 'id="upgrade-release-reload"' in form
    assert 'data-testid="upgrade-system-build-reload"' in form
    assert "Reload" in form


def test_upgrade_reload_button_refetches_the_release_list():
    js = _read("admin.js")
    assert 'reload: document.getElementById("upgrade-release-reload")' in js
    anchor = 'upgradeEls.reload.addEventListener("click"'
    assert anchor in js
    assert "loadUpgradeReleases" in js.split(anchor, 1)[1][:160]
