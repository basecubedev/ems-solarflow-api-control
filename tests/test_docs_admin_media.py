# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard the Admin Console documentation media.

Checks that the required Admin Console screenshots exist, are embedded with
meaningful alt text, and that no committed Markdown references a missing image.
Also protects the demo-fixture / capture tooling contract so the screenshots
stay refreshable.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "assets" / "screenshots" / "admin"

REQUIRED_SCREENSHOTS = [
    "admin-landing.png",
    "admin-guided-setup-start.png",
    "admin-guided-setup-config-preview.png",
    "admin-discovery-preview.png",
    "admin-maintenance-overview.png",
    "admin-backup-restore.png",
    "admin-guided-upgrade-plan.png",
    "admin-admin-update-reconnect.png",
]

IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)")


def read(path):
    return path.read_text(encoding="utf-8")


def test_required_screenshots_exist_and_are_pngs():
    for name in REQUIRED_SCREENSHOTS:
        path = SHOTS / name
        assert path.is_file(), name
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", name


def test_admin_console_doc_embeds_every_screenshot_with_alt_text():
    doc = ROOT / "docs" / "user" / "admin-console.md"
    text = read(doc)
    images = {
        m.group("src").rsplit("/", 1)[-1]: m.group("alt").strip()
        for m in IMAGE_RE.finditer(text)
        if "screenshots/admin/" in m.group("src")
    }
    for name in REQUIRED_SCREENSHOTS:
        assert name in images, f"{name} not embedded in admin-console.md"
        # Meaningful alt text: not empty and not a bare placeholder.
        alt = images[name]
        assert len(alt) >= 12, f"weak alt text for {name}: {alt!r}"
        assert alt.lower() not in {"screenshot", "image"}, name


def test_no_committed_markdown_references_a_missing_admin_image():
    docs = [ROOT / "README.md"] + sorted((ROOT / "docs").rglob("*.md"))
    for doc in docs:
        text = read(doc)
        for match in IMAGE_RE.finditer(text):
            src = match.group("src").split("#", 1)[0]
            if "assets/screenshots/admin/" not in src:
                continue
            target = (doc.parent / src).resolve()
            assert target.is_file(), f"{doc}: missing image {src}"


def test_readme_previews_admin_console():
    text = read(ROOT / "README.md")
    assert "docs/assets/screenshots/admin/admin-landing.png" in text
    assert "what-the-admin-console-looks-like" in text


def test_capture_tooling_and_video_plan_present():
    assert (SHOTS / "README.md").is_file()
    assert (ROOT / "docs" / "assets" / "videos" / "admin" / "README.md").is_file()
    assert (ROOT / "scripts" / "capture_admin_docs.py").is_file()
    assert (ROOT / "scripts" / "serve_admin_docs_preview.py").is_file()
    assert (ROOT / "scripts" / "admin_docs_preview.js").is_file()


def test_demo_fixtures_present_and_non_secret():
    fixtures = ROOT / "tests" / "fixtures" / "admin_docs"
    files = sorted(p.name for p in fixtures.glob("*.json"))
    assert files, "demo fixtures missing"
    blob = "\n".join(read(p) for p in fixtures.glob("*.json"))
    # Only demo values are allowed in committed fixtures.
    assert "DEMO-" in blob
    for forbidden in ("mailbox.org", "BEGIN PRIVATE KEY", "ghp_", "AKIA"):
        assert forbidden not in blob, forbidden
