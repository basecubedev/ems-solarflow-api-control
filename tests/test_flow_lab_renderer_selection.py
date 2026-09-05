# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract for the flow lab's renderer selection.

``scripts/flow_lab/`` picks its rendering technique from ``?renderer=`` in the
URL, which is the only way the benchmark harness can ask for one. Indexing the
renderer table with that name looks like a lookup with a fallback and is not:
every object answers to ``constructor``, ``toString`` and ``valueOf``, so
``?renderer=constructor`` finds ``Object``, passes the truthiness test that was
meant to select the fallback, and gets called instead of a renderer. A lab page
is not a security boundary -- but a benchmark that silently renders something
other than what its URL asked for is worse there than almost anywhere else,
because the number it produces still looks like a measurement of the named
technique.

The selector is extracted from the file and run against a table of its own, so
these cases execute the real source text without a browser or a DOM.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.contract,
]

ROOT = Path(__file__).resolve().parents[1]
LAB_JS = ROOT / "scripts" / "flow_lab" / "lab.js"


def extract_function(source, name):
    """The text of one top-level ``function name(...) {...}``, braces balanced."""
    start = source.index(f"function {name}(")
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"{name} is not closed")


def pick(names):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for flow-lab tests")
    selector = extract_function(LAB_JS.read_text(), "pickRenderer")
    script = f"""
      "use strict";
      {selector}
      var renderers = {{}};
      ["dashoffset", "dom-tiles", "webgl", "none"].forEach(function (key) {{
        renderers[key] = function () {{ return key; }};
      }});
      var answers = {{}};
      {json.dumps(list(names))}.forEach(function (name) {{
        var picked = pickRenderer(renderers, name);
        answers[name] = typeof picked === "function" ? picked() : String(picked);
      }});
      console.log(JSON.stringify(answers));
    """
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_named_renderer_is_the_one_that_runs():
    answers = pick(["dashoffset", "dom-tiles", "webgl", "none"])
    assert answers == {
        "dashoffset": "dashoffset",
        "dom-tiles": "dom-tiles",
        "webgl": "webgl",
        "none": "none",
    }


def test_a_name_that_is_not_a_renderer_falls_back():
    answers = pick(["", "svg-transform-typo", "Dashoffset"])
    assert set(answers.values()) == {"dashoffset"}


def test_an_inherited_member_is_not_a_renderer():
    """The case the truthiness fallback got wrong: these all resolve to
    something callable when the table is indexed directly."""
    inherited = [
        "constructor",
        "toString",
        "valueOf",
        "hasOwnProperty",
        "__proto__",
        "propertyIsEnumerable",
    ]
    answers = pick(inherited)
    assert set(answers.values()) == {"dashoffset"}, answers


def test_the_lab_selects_through_the_selector():
    """A behavioural test of an extracted function proves nothing if the page
    stopped calling it."""
    source = LAB_JS.read_text()
    assert "pickRenderer(renderers, opts.renderer)" in source
    assert "renderers[opts.renderer]" not in source
