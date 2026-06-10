# SPDX-License-Identifier: AGPL-3.0-or-later
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml


ROOT = Path(__file__).resolve().parents[1]
ISSUE_TEMPLATES = [
    ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml",
    ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.yml",
]
REPOSITORY_URL_PREFIX = "/basecubedev/ems-solarflow-api-control/blob/main/"
EXPECTED_DOCUMENTATION_PATHS = {
    "README.md",
    "docs/cli.md",
    "docs/troubleshooting.md",
}


def extract_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from extract_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from extract_strings(item)


def extract_markdown_links(value):
    for text in extract_strings(value):
        yield from re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)


def test_issue_template_documentation_links_are_valid_repo_urls():
    linked_paths = set()

    for template in ISSUE_TEMPLATES:
        payload = yaml.safe_load(template.read_text())

        assert isinstance(payload, dict)
        for link in extract_markdown_links(payload):
            parsed = urlparse(link)
            assert parsed.scheme == "https"
            assert parsed.netloc == "github.com"
            assert parsed.path.startswith(REPOSITORY_URL_PREFIX)

            repo_path = parsed.path.removeprefix(REPOSITORY_URL_PREFIX)
            assert (ROOT / repo_path).is_file()
            linked_paths.add(repo_path)

    assert EXPECTED_DOCUMENTATION_PATHS <= linked_paths
