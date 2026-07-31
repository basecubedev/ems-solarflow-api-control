# SPDX-License-Identifier: AGPL-3.0-or-later
"""Guard the canonical agent-rule document and its repository entry points."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PATH = ROOT / "docs" / "developer" / "agent-rules.md"
CANONICAL_REFERENCE = "docs/developer/agent-rules.md"
GITNEXUS_BLOCK = re.compile(
    r"<!-- gitnexus:start -->.*?<!-- gitnexus:end -->", re.DOTALL
)


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def without_gitnexus_block(text: str) -> str:
    return GITNEXUS_BLOCK.sub("", text)


def test_canonical_agent_rules_exist_with_semantic_anchors():
    assert CANONICAL_PATH.is_file()
    text = CANONICAL_PATH.read_text(encoding="utf-8")
    lowered = " ".join(text.lower().split())

    for anchor in (
        "single source of truth",
        "config/config.json",
        "runtime-state.json",
        "docker",
        "durable workflow",
        "browser state",
        "enabled",
        "transport",
        "contract-first",
        "fail closed",
        "no co-author",
        "no push without explicit instruction",
        "cleanup scope",
        "durable artifact claim",
        "exact owner and workflow identity",
        "canonical workflow-scoped path",
        "exact workflow id",
        "file existence, file name or a known global location",
        "exact ownership proof and canonical-path validation",
    ):
        assert anchor in lowered, anchor


def test_agent_entry_points_reference_canonical_rules_without_copying_them():
    agents = without_gitnexus_block(read("AGENTS.md"))
    claude = without_gitnexus_block(read("CLAUDE.md"))
    copilot = read(".github/copilot-instructions.md")

    assert CANONICAL_REFERENCE in agents
    assert "Before planning, editing or committing" in agents
    assert CANONICAL_REFERENCE in claude
    assert "../docs/developer/agent-rules.md" in copilot
    assert "code, review and refactoring suggestions" in copilot

    canonical_only_headings = (
        "## Instruction precedence and task intent",
        "## Project-wide Single Source of Truth",
        "## Prohibited anti-patterns",
    )
    for entry_text in (agents, claude, copilot):
        for heading in canonical_only_headings:
            assert heading not in entry_text
    assert len(copilot.splitlines()) <= 20


def test_documentation_index_and_architecture_link_the_canonical_rules():
    assert "developer/agent-rules.md" in read("docs/README.md")

    architecture = read("docs/technical/architecture.md")
    assert "## Authority and Single Source of Truth" in architecture
    assert "../developer/agent-rules.md" in architecture
    for anchor in (
        "config/config.json",
        "data/runtime-state.json",
        "Docker daemon",
        "docker-compose.yml",
        "EMS/Core",
        "durable Admin workflow",
        "sidecars",
        "browser/UI/cache",
    ):
        assert anchor in architecture, anchor
