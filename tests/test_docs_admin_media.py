# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard the Admin Console documentation media.

The user docs lead with short demo videos and keep a couple of compact static
screenshots as a render-agnostic fallback; the remaining per-screen screenshots
are kept in the assets folder (and reused as video posters) but are no longer
embedded as a full gallery. Each demo ships in two formats — MP4/H.264
(preferred, best forum/mobile compatibility) and WebM/VP9 (fallback). These
tests check that both formats exist and are embedded with MP4 first, that the
static screenshot fallback is present with meaningful alt text, that the
screenshot files stay present, and that no committed Markdown references a
missing image, video or download link. They also protect the demo-fixture /
capture tooling contract so the media stays refreshable.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "assets" / "screenshots" / "admin"
VIDEOS = ROOT / "docs" / "assets" / "videos" / "admin"

REQUIRED_VIDEOS = [
    "admin-guided-setup-demo.webm",
    "admin-guided-upgrade-demo.webm",
]

# Each committed WebM demo also ships an MP4/H.264 copy for forum/mobile
# compatibility; the docs prefer the MP4 source first.
REQUIRED_MP4_VIDEOS = [name[: -len(".webm")] + ".mp4" for name in REQUIRED_VIDEOS]

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
ATTR_RE = re.compile(r'(?:src|poster)="([^"]+)"')
# Markdown links (e.g. the download links); the negative lookbehind skips the
# ![...](...) image form already covered by IMAGE_RE.
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\((?P<src>[^)]+)\)")
VIDEO_BLOCK_RE = re.compile(r"<video\b.*?</video>", re.DOTALL)


def read(path):
    return path.read_text(encoding="utf-8")


def test_required_screenshots_exist_and_are_pngs():
    for name in REQUIRED_SCREENSHOTS:
        path = SHOTS / name
        assert path.is_file(), name
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", name


def test_demo_videos_exist_and_are_small_webm():
    for name in REQUIRED_VIDEOS:
        path = VIDEOS / name
        assert path.is_file(), name
        assert path.read_bytes()[:4] == b"\x1aE\xdf\xa3", f"{name} is not a Matroska/webm"
        # Keep committed demo videos small; large media belongs in release assets.
        assert path.stat().st_size < 4 * 1024 * 1024, f"{name} too large for Git"


def test_demo_videos_exist_as_mp4():
    for name in REQUIRED_MP4_VIDEOS:
        path = VIDEOS / name
        assert path.is_file(), name
        # ISO base media / MP4 files carry an 'ftyp' box at bytes 4-8.
        assert path.read_bytes()[4:8] == b"ftyp", f"{name} is not an MP4 (missing ftyp box)"
        # Keep committed demo videos small; large media belongs in release assets.
        assert path.stat().st_size < 5 * 1024 * 1024, f"{name} too large for Git"


def test_admin_console_doc_embeds_the_demo_videos():
    text = read(ROOT / "docs" / "user" / "admin-console.md")
    # Both the WebM fallback and the MP4 preferred format must be referenced.
    for name in REQUIRED_VIDEOS + REQUIRED_MP4_VIDEOS:
        assert f"videos/admin/{name}" in text, f"{name} not embedded in admin-console.md"
    # Inline player plus a fallback download link, with the workflows named.
    assert "<video " in text
    lowered = text.lower()
    assert "hardware discovery" in lowered
    assert "software update" in lowered


def test_admin_console_video_prefers_mp4_before_webm():
    text = read(ROOT / "docs" / "user" / "admin-console.md")
    blocks = VIDEO_BLOCK_RE.findall(text)
    assert len(blocks) >= len(REQUIRED_VIDEOS), "expected an inline <video> per demo"
    for block in blocks:
        mp4 = block.find(".mp4")
        webm = block.find(".webm")
        assert mp4 != -1, f"video block missing an MP4 source:\n{block}"
        assert webm != -1, f"video block missing a WebM source:\n{block}"
        assert mp4 < webm, f"MP4 source must appear before WebM in:\n{block}"


def test_admin_console_doc_has_static_screenshot_fallback():
    text = read(ROOT / "docs" / "user" / "admin-console.md")
    # Two videos are committed, so the doc must not claim three demos.
    assert "Three short demos" not in text

    # Collect Markdown image embeds (![alt](src)) — the renderer-agnostic
    # fallback that shows the Admin Console even where inline <video> does not
    # play. HTML <video>/poster attributes are intentionally not counted here.
    images = {}
    for match in IMAGE_RE.finditer(text):
        name = Path(match.group("src").split("#", 1)[0]).name
        images[name] = match.group("alt").strip()

    # The landing page must appear as a plain Markdown screenshot.
    assert "admin-landing.png" in images, "admin-landing.png not embedded as Markdown image"

    # At least one workflow (non-landing) screenshot must appear too.
    workflow_shots = set(REQUIRED_SCREENSHOTS) - {"admin-landing.png"}
    assert workflow_shots & set(images), "no workflow screenshot embedded as Markdown image"

    # Alt text on every embedded admin screenshot must be meaningful: not empty,
    # not a bare filename, and more than a single word.
    for name, alt in images.items():
        if name not in REQUIRED_SCREENSHOTS:
            continue
        assert len(alt) >= 12, f"{name} alt text too short: {alt!r}"
        assert ".png" not in alt.lower(), f"{name} alt text is a filename: {alt!r}"
        assert len(alt.split()) >= 3, f"{name} alt text not descriptive: {alt!r}"


def test_no_committed_markdown_references_missing_admin_media():
    docs = [ROOT / "README.md"] + sorted((ROOT / "docs").rglob("*.md"))
    for doc in docs:
        text = read(doc)
        # Image embeds, HTML src/poster attributes (video <source> included) and
        # plain Markdown download links must all resolve.
        refs = [m.group("src") for m in IMAGE_RE.finditer(text)]
        refs += ATTR_RE.findall(text)
        refs += [m.group("src") for m in LINK_RE.finditer(text)]
        for ref in refs:
            src = ref.split("#", 1)[0]
            if "assets/screenshots/admin/" not in src and "assets/videos/admin/" not in src:
                continue
            target = (doc.parent / src).resolve()
            if src.endswith("/"):
                # A link to the assets folder itself (not a media file).
                assert target.is_dir(), f"{doc}: missing directory {src}"
            else:
                assert target.is_file(), f"{doc}: missing media {src}"


def test_readme_points_to_admin_media_guide():
    text = read(ROOT / "README.md")
    assert "docs/user/admin-console.md#what-the-admin-console-looks-like" in text
    assert "demo video" in text.lower()


def test_video_readme_documents_both_formats():
    text = read(VIDEOS / "README.md")
    lowered = text.lower()
    assert "mp4" in lowered, "video README must mention MP4"
    assert "webm" in lowered, "video README must mention WebM"
    # Both committed demos must appear as MP4 and WebM in the README.
    for name in REQUIRED_VIDEOS + REQUIRED_MP4_VIDEOS:
        assert name in text, f"{name} not listed in video README"
    # Recommend MP4 for forum posts where only one format can be attached.
    assert "forum" in lowered, "video README should recommend MP4 for forums"


def test_capture_and_render_tooling_present():
    assert (SHOTS / "README.md").is_file()
    assert (VIDEOS / "README.md").is_file()
    for script in (
        "capture_admin_docs.py",
        "serve_admin_docs_preview.py",
        "admin_docs_preview.js",
        "render_admin_docs_video.py",
    ):
        assert (ROOT / "scripts" / script).is_file(), script


def test_demo_fixtures_present_and_non_secret():
    fixtures = ROOT / "tests" / "fixtures" / "admin_docs"
    files = sorted(p.name for p in fixtures.glob("*.json"))
    assert files, "demo fixtures missing"
    blob = "\n".join(read(p) for p in fixtures.glob("*.json"))
    # Only demo values are allowed in committed fixtures.
    assert "DEMO-" in blob
    for forbidden in ("mailbox.org", "BEGIN PRIVATE KEY", "ghp_", "AKIA"):
        assert forbidden not in blob, forbidden
