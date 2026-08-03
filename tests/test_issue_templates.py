# SPDX-License-Identifier: AGPL-3.0-or-later
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

pytestmark = [
    pytest.mark.contract,
    pytest.mark.documentation,
]


ROOT = Path(__file__).resolve().parents[1]
ISSUE_TEMPLATES = [
    ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml",
    ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.yml",
]
REPOSITORY_URL_PREFIX = "/basecubedev/ems-solarflow-api-control/blob/main/"
EXPECTED_DOCUMENTATION_PATHS = {
    "README.md",
    "docs/cli.md",
    "docs/user/troubleshooting.md",
}


def extract_markdown_links(text):
    yield from re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)


def assert_issue_form_shape(text):
    assert "\t" not in text
    for key in ("name", "description", "title", "body"):
        assert re.search(rf"^{key}:", text, re.MULTILINE)
    assert re.search(r"^body:\n  - type:", text, re.MULTILINE)
    assert re.search(r"^    attributes:", text, re.MULTILINE)
    assert re.search(r"^    validations:", text, re.MULTILINE)


def test_issue_template_documentation_links_are_valid_repo_urls():
    linked_paths = set()

    for template in ISSUE_TEMPLATES:
        text = template.read_text()
        assert_issue_form_shape(text)

        for link in extract_markdown_links(text):
            parsed = urlparse(link)
            assert parsed.scheme == "https"
            assert parsed.netloc == "github.com"
            assert parsed.path.startswith(REPOSITORY_URL_PREFIX)

            repo_path = parsed.path.removeprefix(REPOSITORY_URL_PREFIX)
            assert (ROOT / repo_path).is_file()
            linked_paths.add(repo_path)

    assert EXPECTED_DOCUMENTATION_PATHS <= linked_paths
