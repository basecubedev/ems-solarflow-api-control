# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contracts of the Modern Admin console, the second entry page at ``/modern/``.

Modern is a build output committed under ``admin/static/modern/``, made from
``frontend/admin-modern/`` and the shared design system in ``frontend/shared/``.
These tests hold the output to the Admin server's CSP, the shared colours and
the licence inventory, and hold the console's vocabulary to the server's: the
words it has for a code are only right while the server still sends that code.
"""

import re
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from admin.server import SECURITY_HEADERS, ScanRegistry, create_server
from dashboard.static_files import build_static_asset_index, lookup_static_asset
from tests.admin_auth_helpers import authenticate
from tests.modern_frontend import (
    assert_build_matches_source,
    assert_bundled_packages_inventoried,
    assert_font_and_icons_carry_their_notices,
    assert_font_and_icons_inventoried,
    assert_no_colour_literal_outside_the_token_blocks,
    assert_no_inline_script_or_style,
    assert_tab_icon_is_the_mark,
    run_npm_script,
)

pytestmark = [
    pytest.mark.admin,
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "admin" / "static"
MODERN = STATIC / "modern"
SOURCE = ROOT / "frontend" / "admin-modern"
MODERN_JS = MODERN / "assets" / "admin.js"
MODERN_CSS = MODERN / "assets" / "admin.css"


def js_list(relative, name):
    """The string items of ``export const NAME = Object.freeze([...])`` in a source module."""

    source = (SOURCE / "src" / relative).read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = Object\.freeze\(\[([^\]]*)\]\)", source)
    assert match, f"{name} not found in {relative}"
    return re.findall(r'"([^"]+)"', match.group(1))


def js_keys(relative, name):
    """The keys of ``export const NAME = Object.freeze({...})`` in a source module."""

    source = (SOURCE / "src" / relative).read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = Object\.freeze\(\{{(.*?)\n?\}}\)", source, re.S)
    assert match, f"{name} not found in {relative}"
    keys = re.findall(r'^\s*(?:"([^"]+)"|([^":\s]+))\s*:', match.group(1), re.M)
    return [quoted or bare for quoted, bare in keys]


@pytest.fixture()
def admin(isolated_install_root):
    srv = create_server("127.0.0.1", 0, registry=ScanRegistry(scan_runner=lambda *a, **k: ([], [])))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_admin_serves_modern_beside_classic_under_its_own_headers(admin):
    for path in ("/modern/", "/modern"):
        with urllib.request.urlopen(admin + path) as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/html")
            assert response.headers["Content-Security-Policy"] == SECURITY_HEADERS["Content-Security-Policy"]
            assert b'<div id="app">' in response.read()
    with urllib.request.urlopen(admin + "/modern/assets/admin.js") as response:
        assert response.headers["Content-Type"].startswith("application/javascript")
    with urllib.request.urlopen(admin + "/modern/assets/inter-latin-wght-normal.woff2") as response:
        assert response.headers["Content-Type"] == "font/woff2"
    with urllib.request.urlopen(admin + "/") as response:
        assert b"EMS SolarFlow Admin" in response.read()


def test_the_modern_page_needs_no_session_but_every_api_still_does(admin):
    with urllib.request.urlopen(admin + "/modern/") as response:
        assert response.status == 200
    request = urllib.request.Request(admin + "/api/admin/maintenance/overview")
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(request)
    assert refused.value.code in (401, 403)
    authenticate(admin)


def test_the_static_index_resolves_both_spellings_and_nothing_outside():
    assets = build_static_asset_index(str(STATIC))
    for path in ("/modern/", "/modern"):
        assert lookup_static_asset(assets, path)[0].endswith("modern/index.html")
    assert lookup_static_asset(assets, "/")[0].endswith("static/index.html")
    assert lookup_static_asset(assets, "/modern/../../server.py") is None


def test_the_page_carries_no_inline_script_or_style():
    assert_no_inline_script_or_style((MODERN / "index.html").read_text(encoding="utf-8"), MODERN_JS.read_text(encoding="utf-8"))


def test_the_tab_icon_is_the_mark_in_the_token_colours():
    assert_tab_icon_is_the_mark((MODERN / "index.html").read_text(encoding="utf-8"))


def test_no_colour_literal_outside_the_token_blocks():
    assert_no_colour_literal_outside_the_token_blocks(MODERN_CSS.read_text(encoding="utf-8"))


def test_the_bundle_fits_under_the_repository_size_limit():
    assert MODERN_JS.stat().st_size < 512 * 1024


def test_every_bundled_package_is_in_the_licence_inventory():
    assert_bundled_packages_inventoried(MODERN)


def test_the_font_and_icons_shipped_beside_the_bundle_are_inventoried():
    assert_font_and_icons_inventoried(MODERN)


def test_the_font_and_icons_ship_with_their_licence_texts():
    assert_font_and_icons_carry_their_notices(MODERN)


def test_the_classic_page_offers_the_switch_and_decides_before_it_paints():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'href="/modern/"' in page
    early = (STATIC / "admin-theme.js").read_text(encoding="utf-8")
    assert 'var UI_KEY = "ems-admin-ui";' in early
    assert 'var DEFAULT_UI = "modern";' in early
    assert 'window.location.replace("/modern/")' in early
    prefs = (SOURCE / "src" / "state" / "prefs.svelte.js").read_text(encoding="utf-8")
    assert 'UI_CHOICE_KEY = "ems-admin-ui"' in prefs


def test_the_server_sentences_this_console_translates_are_still_the_servers():
    from admin import backup_restore, server

    source = "".join(Path(module.__file__).read_text(encoding="utf-8") for module in (backup_restore, server))
    for sentence in js_keys("lib/errors.js", "KNOWN_SENTENCES"):
        assert f'"{sentence}"' in source, f"the Admin server no longer says {sentence!r}"


def test_the_backup_list_warnings_this_console_translates_are_still_the_servers():
    from admin import backup_restore

    source = Path(backup_restore.__file__).read_text(encoding="utf-8")
    for sentence in js_keys("lib/backups.js", "LIST_WARNINGS"):
        assert f'"{sentence}"' in source, f"backup_restore.py no longer says {sentence!r}"


def test_the_release_notes_this_console_translates_are_still_the_servers():
    import ast

    from admin import image_identity, releases

    sentences = set()
    for module in (releases, image_identity):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        sentences |= {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    for note in js_keys("lib/updates.js", "RELEASE_NOTES"):
        assert note in sentences or any(note == head + tail for head in sentences for tail in sentences if note.startswith(head)), (
            f"the release catalogue no longer says {note!r}"
        )


def test_the_analytics_backup_reasons_are_the_ones_the_backup_gives():
    from ems import backup

    source = Path(backup.__file__).read_text(encoding="utf-8")
    for reason in js_list("lib/backups.js", "INFLUX_REASONS"):
        assert f'"reason": "{reason}"' in source


def test_automatic_features_are_catalogue_features_without_a_switch():
    from ems.config_catalog import get_config_feature_sections

    switchless = {
        section["id"]
        for section in get_config_feature_sections(mode="maintenance")
        if section.get("kind") == "feature"
        and not any(field["path"] == f"{section['path']}.enabled" for field in section.get("fields", []))
    }
    assert set(js_list("lib/settings.js", "AUTOMATIC_FEATURES")) == switchless


def test_the_plan_sentence_this_console_translates_is_still_the_servers():
    from admin import container_actions

    source = Path(container_actions.__file__).read_text(encoding="utf-8")
    for sentence in js_keys("lib/services.js", "PLAN_MESSAGES"):
        assert f'"{sentence}"' in source, f"container_actions.py no longer says {sentence!r}"


def test_the_diagnostics_sentence_this_console_translates_is_still_the_servers():
    from admin import ems_cli

    for sentence in js_keys("lib/diagnostics.js", "UNAVAILABLE_MESSAGES"):
        assert sentence == ems_cli.UNAVAILABLE_MESSAGE


def test_every_grid_meter_the_settings_name_has_a_label_in_the_catalogue():
    from admin.setup_config import grid_meter_variant_catalog
    from ems.config_catalog import GRID_METER_VARIANTS

    catalogue = grid_meter_variant_catalog()
    assert set(catalogue) == set(GRID_METER_VARIANTS)
    assert all(str(variant.get("label") or "").strip() for variant in catalogue.values())


def test_backup_purposes_are_the_backups_own():
    from ems.backup import BACKUP_PURPOSES

    assert tuple(js_list("lib/backups.js", "BACKUP_PURPOSES")) == tuple(BACKUP_PURPOSES)


def test_feature_sections_with_words_here_are_catalogue_features():
    from ems.config_catalog import get_config_feature_sections

    features = {section["id"] for section in get_config_feature_sections(mode="maintenance") if section.get("kind") == "feature"}
    assert set(js_list("lib/settings.js", "FEATURE_IDS")) <= features


def test_device_kinds_are_the_ones_the_settings_view_draws():
    from admin import maintenance_config
    from ems.device_identity import EXTERNAL_MQTT_TYPE
    from ems.read_only_devices import E3DC_MODBUS_DEVICE_TYPE

    source = Path(maintenance_config.__file__).read_text(encoding="utf-8")
    kinds = {"local_api", "zendure_mqtt", E3DC_MODBUS_DEVICE_TYPE, EXTERNAL_MQTT_TYPE}
    assert '"kind": "local_api"' in source
    assert set(js_list("lib/settings.js", "DEVICE_KINDS")) == kinds


def test_install_states_are_the_detectors():
    from admin import install_state

    states = {value for name, value in vars(install_state).items() if name.startswith("STATE_") and isinstance(value, str)}
    assert set(js_list("lib/health.js", "INSTALL_STATES")) == states


def test_the_unit_tests_of_the_admin_frontend_pass():
    assert len(list((SOURCE / "tests").glob("*.test.js"))) >= 10
    for script in ("test:frontend-shared", "test:admin-modern"):
        if run_npm_script(script) is None:
            pytest.skip("npm is required for the Modern frontend unit tests")


def test_the_committed_build_is_the_build_of_the_committed_source(tmp_path):
    if not assert_build_matches_source(SOURCE / "vite.config.js", MODERN, tmp_path / "modern", "npm run build:admin-modern"):
        pytest.skip("npm ci has not been run; CI runs the same check in its own job")
