# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard the step-by-step user guides and their screenshots.

The Admin Console and EMS Dashboard guides under ``docs/user/admin/`` and
``docs/user/dashboard/`` are screenshot-led, so these tests protect the parts a
reader notices when they rot: a guide that lost its screenshot, an image path
that no longer resolves, a committed screenshot nobody embeds, a capture that
silently produced the same screen twice, and the capture tooling itself.

They are structural and file-level on purpose. There is no pixel comparison.
"""
import hashlib
import re
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
ADMIN_GUIDES = ROOT / "docs" / "user" / "admin"
DASHBOARD_GUIDES = ROOT / "docs" / "user" / "dashboard"
ADMIN_SHOTS = ROOT / "docs" / "assets" / "screenshots" / "admin"
DASHBOARD_SHOTS = ROOT / "docs" / "assets" / "screenshots" / "dashboard"

REQUIRED_ADMIN_GUIDES = [
    "index.md",
    "first-start.md",
    "guided-setup.md",
    "guided-upgrade.md",
    "maintenance.md",
    "device-management.md",
    "mqtt.md",
    "backup-restore.md",
    "diagnostics-recovery.md",
]

REQUIRED_DASHBOARD_GUIDES = [
    "index.md",
    "overview.md",
    "devices.md",
    "energy.md",
    "control.md",
    "runtime-settings.md",
    "diagnostics.md",
]

IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)")
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\((?P<src>[^)]+)\)")

# Values that must never reach a committed guide or capture fixture. Real
# secrets are kept out at the source (synthetic fixtures) rather than by
# inspecting the rendered images.
FORBIDDEN_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bmailbox\.org\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
]


def read(path):
    return path.read_text(encoding="utf-8")


def guide_files():
    return sorted(ADMIN_GUIDES.glob("*.md")) + sorted(DASHBOARD_GUIDES.glob("*.md"))


def screenshot_files():
    return sorted(ADMIN_SHOTS.glob("*.png")) + sorted(DASHBOARD_SHOTS.glob("*.png"))


def embedded_images(doc):
    """Return (alt, resolved path) for every Markdown image embed in a doc."""

    out = []
    for match in IMAGE_RE.finditer(read(doc)):
        src = match.group("src").split("#", 1)[0]
        if src.startswith(("http://", "https://")):
            continue
        out.append((match.group("alt").strip(), (doc.parent / src).resolve()))
    return out


# --- The guides exist and follow the shared shape --------------------------


def test_required_guides_exist():
    for name in REQUIRED_ADMIN_GUIDES:
        assert (ADMIN_GUIDES / name).is_file(), f"docs/user/admin/{name}"
    for name in REQUIRED_DASHBOARD_GUIDES:
        assert (DASHBOARD_GUIDES / name).is_file(), f"docs/user/dashboard/{name}"


def test_every_guide_has_a_single_title():
    for doc in guide_files():
        titles = [ln for ln in read(doc).splitlines() if ln.startswith("# ")]
        assert len(titles) == 1, f"{doc.relative_to(ROOT)}: expected one H1, got {titles}"


def test_workflow_guides_follow_the_documented_structure():
    # index pages are routers, not workflows, so they are exempt.
    for doc in guide_files():
        if doc.name == "index.md":
            continue
        text = read(doc)
        for heading in ("## Purpose", "## Recovery or next steps"):
            assert heading in text, f"{doc.relative_to(ROOT)} missing {heading!r}"


def test_every_guide_embeds_at_least_one_screenshot():
    for doc in guide_files():
        assert embedded_images(doc), f"{doc.relative_to(ROOT)} embeds no screenshot"


# --- Screenshots resolve, are used, and are real PNGs ----------------------


def test_guide_image_paths_resolve():
    for doc in guide_files():
        for _, target in embedded_images(doc):
            assert target.is_file(), f"{doc.relative_to(ROOT)}: missing image {target}"


def test_guide_relative_links_resolve():
    for doc in guide_files():
        for match in LINK_RE.finditer(read(doc)):
            src = match.group("src")
            if src.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target = (doc.parent / src.split("#", 1)[0]).resolve()
            assert target.exists(), f"{doc.relative_to(ROOT)}: broken link {src}"


def test_screenshots_are_pngs():
    shots = screenshot_files()
    assert shots, "no documentation screenshots found"
    for path in shots:
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", path.name


def test_every_committed_screenshot_is_embedded_somewhere():
    used = set()
    for doc in [ROOT / "README.md"] + sorted((ROOT / "docs").rglob("*.md")):
        for _, target in embedded_images(doc):
            used.add(target)
        # HTML poster/src attributes count as use too.
        for ref in re.findall(r'(?:src|poster)="([^"]+)"', read(doc)):
            used.add((doc.parent / ref.split("#", 1)[0]).resolve())
    unused = [p.name for p in screenshot_files() if p.resolve() not in used]
    assert unused == [], f"committed but never embedded: {unused}"


def test_screenshots_are_pairwise_distinct():
    # A capture that silently landed on the wrong screen produces a byte-identical
    # duplicate; that is exactly how the Guided Setup steps regressed before.
    digests = {}
    for path in screenshot_files():
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        digests.setdefault(digest, []).append(path.name)
    duplicates = [names for names in digests.values() if len(names) > 1]
    assert duplicates == [], f"identical screenshots: {duplicates}"


def test_embedded_screenshots_have_descriptive_alt_text():
    for doc in guide_files():
        for alt, target in embedded_images(doc):
            if target.suffix != ".png":
                continue
            assert len(alt) >= 12, f"{doc.relative_to(ROOT)}: alt too short: {alt!r}"
            assert ".png" not in alt.lower(), f"{doc.relative_to(ROOT)}: alt is a filename"
            assert len(alt.split()) >= 3, f"{doc.relative_to(ROOT)}: alt not descriptive"


def test_screenshots_stay_inside_the_documentation_asset_directories():
    allowed = {ADMIN_SHOTS.resolve(), DASHBOARD_SHOTS.resolve()}
    for doc in guide_files():
        for _, target in embedded_images(doc):
            assert target.parent.resolve() in allowed, (
                f"{doc.relative_to(ROOT)}: screenshot outside the asset dirs: {target}"
            )


# --- Privacy ---------------------------------------------------------------


def test_guides_and_capture_fixtures_carry_no_secrets():
    sources = guide_files() + sorted((ROOT / "tests" / "fixtures" / "admin_docs").glob("*.json"))
    offenders = []
    for path in sources:
        text = read(path)
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(ROOT)}: {pattern.pattern}")
    assert offenders == [], "\n".join(offenders)


def test_capture_fixtures_use_demo_identifiers():
    blob = "\n".join(
        read(p) for p in sorted((ROOT / "tests" / "fixtures" / "admin_docs").glob("*.json"))
    )
    assert "DEMO-" in blob, "capture fixtures must use DEMO- prefixed identifiers"


# --- The capture tooling stays present and consistent ----------------------


def test_capture_tooling_present():
    for rel in (
        "scripts/capture-docs-screenshots.sh",
        "scripts/capture_admin_docs.py",
        "scripts/capture_dashboard_docs.py",
        "scripts/serve_admin_docs_preview.py",
        "scripts/serve_dashboard_preview.py",
        "scripts/admin_docs_preview.js",
    ):
        assert (ROOT / rel).is_file(), rel
    assert (ADMIN_SHOTS / "README.md").is_file()
    assert (DASHBOARD_SHOTS / "README.md").is_file()


def _capture_outputs(module_path):
    """Basenames the given capture script declares in its SCREENS mapping."""

    text = read(module_path)
    body = text.split("SCREENS = {", 1)[1].split("\n}", 1)[0]
    return set(re.findall(r'"([a-z0-9-]+\.png)"', body))


def test_capture_manifest_matches_the_committed_screenshots():
    declared = _capture_outputs(ROOT / "scripts" / "capture_admin_docs.py")
    committed = {p.name for p in ADMIN_SHOTS.glob("*.png")}
    assert declared == committed, (
        f"admin capture manifest drifted: only declared={sorted(declared - committed)} "
        f"only committed={sorted(committed - declared)}"
    )

    declared = _capture_outputs(ROOT / "scripts" / "capture_dashboard_docs.py")
    committed = {p.name for p in DASHBOARD_SHOTS.glob("*.png")}
    assert declared == committed, (
        f"dashboard capture manifest drifted: only declared={sorted(declared - committed)} "
        f"only committed={sorted(committed - declared)}"
    )


def test_asset_readmes_list_every_screenshot():
    for shots in (ADMIN_SHOTS, DASHBOARD_SHOTS):
        listing = read(shots / "README.md")
        missing = [p.name for p in shots.glob("*.png") if p.name not in listing]
        assert missing == [], f"{shots.name} README does not list: {missing}"


def test_capture_script_is_documented_for_maintainers():
    text = read(ROOT / "docs" / "developer" / "testing.md")
    assert "capture-docs-screenshots.sh" in text


# --- Entry points route to the new guides ----------------------------------


def test_docs_index_links_the_step_by_step_guides():
    text = read(ROOT / "docs" / "README.md")
    assert "user/admin/index.md" in text
    assert "user/dashboard/index.md" in text


def test_project_overview_links_the_step_by_step_guides():
    text = read(ROOT / "docs" / "user" / "project-overview.md")
    assert "admin/index.md" in text
    assert "dashboard/index.md" in text


def test_readme_routes_to_the_step_by_step_guides():
    text = read(ROOT / "README.md")
    assert "docs/user/admin/index.md" in text
    assert "docs/user/dashboard/index.md" in text
