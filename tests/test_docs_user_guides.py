# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard the step-by-step user guides and their screenshots.

The Admin Console, EMS Dashboard and Appliance Manager guides under
``docs/user/admin/``, ``docs/user/dashboard/`` and ``docs/user/appliance/`` are
screenshot-led, so these tests protect the parts a
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
    pytest.mark.documentation,
]

ROOT = Path(__file__).resolve().parents[1]
ADMIN_GUIDES = ROOT / "docs" / "user" / "admin"
DASHBOARD_GUIDES = ROOT / "docs" / "user" / "dashboard"
ADMIN_SHOTS = ROOT / "docs" / "assets" / "screenshots" / "admin"
ADMIN_CAPTURE_SCRIPT = ROOT / "scripts" / "capture_admin_docs.mjs"
DASHBOARD_SHOTS = ROOT / "docs" / "assets" / "screenshots" / "dashboard"
DASHBOARD_PREVIEWS = ROOT / "docs" / "assets"
DASHBOARD_CAPTURE_SPEC = ROOT / "tests" / "e2e-dashboard" / "capture-docs.spec.ts"
APPLIANCE_GUIDES = ROOT / "docs" / "user" / "appliance"
APPLIANCE_SHOTS = ROOT / "docs" / "assets" / "screenshots" / "appliance"
APPLIANCE_CAPTURE_SPEC = ROOT / "tests" / "e2e-appliance" / "capture-docs.spec.ts"

REQUIRED_ADMIN_GUIDES = [
    "index.md",
    "assistant.md",
    "first-start.md",
    "guided-setup.md",
    "guided-upgrade.md",
    "maintenance.md",
    "device-management.md",
    "mqtt.md",
    "backup-restore.md",
    "diagnostics-recovery.md",
    "appearance.md",
]

REQUIRED_DASHBOARD_GUIDES = [
    "index.md",
    "overview.md",
    "devices.md",
    "energy.md",
    "control.md",
    "runtime-settings.md",
    "diagnostics.md",
    "appearance.md",
]

REQUIRED_APPLIANCE_GUIDES = [
    "index.md",
    "install.md",
    "first-start.md",
    "assistant.md",
    "overview.md",
    "updates.md",
    "network.md",
    "backup.md",
    "recovery.md",
    "appearance.md",
]

GUIDES_WITHOUT_SCREENSHOTS = {
    "appliance/install.md": "flashing the card happens in Raspberry Pi Imager, before the console exists",
}

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
    return (
        sorted(ADMIN_GUIDES.glob("*.md"))
        + sorted(DASHBOARD_GUIDES.glob("*.md"))
        + sorted(APPLIANCE_GUIDES.glob("*.md"))
    )


def screenshot_files():
    return (
        sorted(ADMIN_SHOTS.glob("*.png"))
        + sorted(DASHBOARD_SHOTS.glob("*.png"))
        + sorted(APPLIANCE_SHOTS.glob("*.png"))
    )


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
    for name in REQUIRED_APPLIANCE_GUIDES:
        assert (APPLIANCE_GUIDES / name).is_file(), f"docs/user/appliance/{name}"


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
        if doc.relative_to(ROOT / "docs" / "user").as_posix() in GUIDES_WITHOUT_SCREENSHOTS:
            continue
        assert embedded_images(doc), f"{doc.relative_to(ROOT)} embeds no screenshot"


def test_a_guide_without_a_screenshot_is_a_listed_exception():
    for name in GUIDES_WITHOUT_SCREENSHOTS:
        doc = ROOT / "docs" / "user" / name
        assert doc.is_file(), f"exception for a guide that does not exist: {name}"
        assert not embedded_images(doc), f"{name} embeds a screenshot now; drop its exception"


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
    allowed = {ADMIN_SHOTS.resolve(), DASHBOARD_SHOTS.resolve(), APPLIANCE_SHOTS.resolve()}
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
        "scripts/capture_admin_docs.mjs",
        "tests/e2e-dashboard/capture-docs.spec.ts",
        "tests/e2e-appliance/capture-docs.spec.ts",
        "scripts/serve_admin_docs_preview.py",
        "scripts/serve_dashboard_preview.py",
        "scripts/admin_docs_preview.js",
    ):
        assert (ROOT / rel).is_file(), rel
    assert (ADMIN_SHOTS / "README.md").is_file()
    assert (DASHBOARD_SHOTS / "README.md").is_file()
    assert (APPLIANCE_SHOTS / "README.md").is_file()


def _admin_captures():
    """Files the Admin capture script writes, named by the ``file:`` of each shot."""

    return set(re.findall(r'file: "(admin-[a-z0-9-]+\.png)"', read(ADMIN_CAPTURE_SCRIPT)))


def _dashboard_captures(prefix, suffix):
    """Files the dashboard capture spec writes, named by its test titles."""

    titles = re.findall(r'test\("(' + prefix + r'-[a-z0-9-]+)"', read(DASHBOARD_CAPTURE_SPEC))
    return {f"{title}{suffix}" for title in titles}


def _appliance_captures():
    """Files the Appliance Manager capture spec writes, named by its test titles."""

    return {f"{title}.png" for title in re.findall(r'test\("(appliance-[a-z0-9-]+)"', read(APPLIANCE_CAPTURE_SPEC))}


def test_capture_manifest_matches_the_committed_screenshots():
    """Each screenshot directory has exactly one writer, and its manifest says which files.

    Every Admin Console image comes from capture_admin_docs.mjs, named by the
    ``file:`` of each shot; every dashboard image and every Appliance Manager
    image from its own Playwright spec, named by its test titles. The retired
    appearance gallery and the Classic capture scripts write into none of them.
    """

    declared = _admin_captures()
    committed = {p.name for p in ADMIN_SHOTS.glob("*.png")}
    assert declared == committed, (
        f"admin capture manifest drifted: only declared={sorted(declared - committed)} "
        f"only committed={sorted(committed - declared)}"
    )

    declared = _dashboard_captures("dashboard", ".png")
    committed = {p.name for p in DASHBOARD_SHOTS.glob("*.png")}
    assert declared == committed, (
        f"dashboard capture manifest drifted: only declared={sorted(declared - committed)} "
        f"only committed={sorted(committed - declared)}"
    )

    declared = _appliance_captures()
    committed = {p.name for p in APPLIANCE_SHOTS.glob("*.png")}
    assert declared == committed, (
        f"appliance capture manifest drifted: only declared={sorted(declared - committed)} "
        f"only committed={sorted(committed - declared)}"
    )


def test_dashboard_previews_match_the_capture_spec():
    """The README-level preview JPGs are captured by the same spec as the guides."""

    declared = _dashboard_captures("preview", ".jpg")
    committed = {p.name for p in DASHBOARD_PREVIEWS.glob("preview-*.jpg")}
    assert declared, "the capture spec declares no preview"
    assert declared == committed, (
        f"dashboard preview manifest drifted: only declared={sorted(declared - committed)} "
        f"only committed={sorted(committed - declared)}"
    )


def test_every_dashboard_preview_is_embedded_and_listed():
    used = set()
    for doc in [ROOT / "README.md"] + sorted((ROOT / "docs").rglob("*.md")):
        used |= {target for _, target in embedded_images(doc)}
    listing = read(DASHBOARD_SHOTS / "README.md")
    for path in sorted(DASHBOARD_PREVIEWS.glob("preview-*.jpg")):
        assert path.resolve() in used, f"{path.name} is never embedded"
        assert path.name in listing, f"dashboard screenshot README does not list {path.name}"


def test_every_admin_capture_declares_a_preview_scenario():
    """A shot names the installation it shows with a scenario the preview serves."""

    text = read(ADMIN_CAPTURE_SCRIPT)
    preview = read(ROOT / "scripts" / "serve_admin_docs_preview.py")
    served = set(re.findall(r'"([a-z-]+)"', preview.split("SCENARIOS = (", 1)[1].split(")", 1)[0]))
    used = set(re.findall(r'scenario: "([a-z-]+)"', text))
    assert used, "the capture script names no scenario"
    assert used <= served, f"scenarios the preview does not serve: {sorted(used - served)}"


def test_admin_guides_describe_the_modern_console():
    """Classic is named only as the deprecated fallback, never as the console the steps show."""

    stale = re.compile(r"/classic/#setup|Maintenance hub|Something looks wrong\?|Recommended path|Verify System Build\b")
    offenders = [
        f"{doc.relative_to(ROOT)}: {match.group(0)}"
        for doc in sorted(ADMIN_GUIDES.glob("*.md"))
        for match in stale.finditer(read(doc))
    ]
    assert offenders == [], "\n".join(offenders)


def test_appliance_guides_describe_the_modern_console():
    """Classic's labels are gone from the guides; Classic is named only as the deprecated fallback."""

    stale = re.compile(
        r"\*\*(?:Restart Admin|Repair Admin|Install Admin|Enable SSH|Disable SSH|Theme|Style|Scan)\*\*"
        r"|SSH & Backup Access|System Updates|/classic/#"
    )
    offenders = [
        f"{doc.relative_to(ROOT)}: {match.group(0)}"
        for doc in sorted(APPLIANCE_GUIDES.glob("*.md"))
        for match in stale.finditer(read(doc))
    ]
    assert offenders == [], "\n".join(offenders)


def _listed_as(path):
    """The Admin and dashboard READMEs list files; the appliance README lists capture IDs."""

    return f"`{path.stem}`" if path.parent == APPLIANCE_SHOTS else path.name


def test_asset_readmes_list_every_screenshot():
    for shots in (ADMIN_SHOTS, DASHBOARD_SHOTS, APPLIANCE_SHOTS):
        listing = read(shots / "README.md")
        missing = [p.name for p in shots.glob("*.png") if _listed_as(p) not in listing]
        assert missing == [], f"{shots.name} README does not list: {missing}"


def test_capture_script_is_documented_for_maintainers():
    text = read(ROOT / "docs" / "developer" / "testing.md")
    assert "capture-docs-screenshots.sh" in text


# --- Entry points route to the new guides ----------------------------------


def test_docs_index_links_the_step_by_step_guides():
    text = read(ROOT / "docs" / "README.md")
    assert "user/admin/index.md" in text
    assert "user/dashboard/index.md" in text
    assert "user/appliance/index.md" in text


def test_project_overview_links_the_step_by_step_guides():
    text = read(ROOT / "docs" / "user" / "project-overview.md")
    assert "admin/index.md" in text
    assert "dashboard/index.md" in text


def test_readme_routes_to_the_step_by_step_guides():
    text = read(ROOT / "README.md")
    assert "docs/user/admin/index.md" in text
    assert "docs/user/dashboard/index.md" in text
    assert "docs/user/appliance/index.md" in text
